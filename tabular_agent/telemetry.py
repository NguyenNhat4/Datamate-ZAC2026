"""Optional local OpenTelemetry tracing for inspecting smolagents runs."""
from contextlib import contextmanager, nullcontext
import socket

_provider = None


def is_enabled():
    return _provider is not None


@contextmanager
def tracing(enabled=False, *, exporter=None):
    """Instrument smolagents for one run; export only to local Phoenix by default."""
    if not enabled:
        yield
        return

    from openinference.instrumentation.smolagents import SmolagentsInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        import requests

        try:
            with socket.create_connection(('127.0.0.1', 6006), timeout=1):
                pass
        except OSError as error:
            raise RuntimeError('Local Phoenix is not running; start it with make telemetry') from error
        class LocalSession(requests.Session):
            def request(self, method, url, **kwargs):
                kwargs['allow_redirects'] = False
                return super().request(method, url, **kwargs)

        session = LocalSession()
        session.trust_env = False
        exporter = OTLPSpanExporter(endpoint='http://127.0.0.1:6006/v1/traces',
                                    headers={'Content-Type': 'application/x-protobuf'},
                                    session=session, timeout=2)
        processor = BatchSpanProcessor(exporter, schedule_delay_millis=500)
    else:
        processor = SimpleSpanProcessor(exporter)

    global _provider
    if _provider is not None:
        raise RuntimeError('Tracing is already active')
    provider = TracerProvider(resource=Resource.create({'service.name': 'tabular-agent',
                                                          'openinference.project.name': 'tabular-agent'}))
    provider.add_span_processor(processor)
    instrumentor = SmolagentsInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        provider.shutdown()
        raise RuntimeError('Smolagents is already instrumented by another tracer')
    try:
        instrumentor.instrument(tracer_provider=provider)
        _provider = provider
        yield
    finally:
        _provider = None
        instrumentor.uninstrument()
        provider.force_flush(timeout_millis=3000)
        provider.shutdown()


def question_span(question):
    """Associate a trace with the question used for routing and result matching."""
    if _provider is None:
        return nullcontext()
    return _provider.get_tracer(__name__).start_as_current_span(
        'answer_question', attributes={'question.id': question.get('id', ''),
                                       'business.id': question.get('business_id', '')})


def record_tool_calls(calls):
    """Attach worker-observed helper calls to the active smolagents step."""
    if _provider is None:
        return
    from opentelemetry.trace import Status, StatusCode

    tracer = _provider.get_tracer(__name__)
    for call in calls:
        span = tracer.start_span(call['name'], start_time=call['started_ns'], attributes={
            'openinference.span.kind': 'TOOL', 'tool.name': call['name'],
            'input.value': call['arguments'], 'output.value': call.get('result', ''),
        })
        if call.get('error'):
            span.set_status(Status(StatusCode.ERROR, call['error']))
        span.end(end_time=call['ended_ns'])
