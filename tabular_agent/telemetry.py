"""Optional local OpenTelemetry tracing for inspecting smolagents runs."""
from contextlib import contextmanager, nullcontext
import socket

_provider = None


def is_enabled():
    """Report whether this module currently owns an active tracing provider.

    Returns:
        True inside an enabled tracing context, otherwise False.
    """
    return _provider is not None


@contextmanager
def tracing(enabled=False, *, exporter=None):
    """Temporarily instrument smolagents and export execution traces.

    Args:
        enabled: Whether to activate instrumentation; false is a no-op context.
        exporter: Optional OpenTelemetry exporter, typically for in-memory tests.
            If absent, use local Phoenix at 127.0.0.1:6006 without redirects
            or environment proxies.

    Yields:
        None while instrumentation is active. Always uninstalls it, flushes
        pending spans and shuts down the provider when leaving the block.

    Raises:
        RuntimeError: Local Phoenix is unavailable, tracing is already active,
            or another tracer has already instrumented smolagents.

    Notes:
        Trace payloads can contain questions, business data and model context.
    """
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
                """Send an exporter HTTP request without following redirects.

                Args:
                    method: HTTP method forwarded to requests.Session.request.
                    url: Destination URL selected by the exporter.
                    **kwargs: Request options; allow_redirects is always set to False.

                Returns:
                    requests.Response from the underlying session; request errors propagate.
                """
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
    """Create a trace context associating execution with a question.

    Args:
        question: Mapping with optional id and business_id used as span attributes.

    Returns:
        Context manager creating the current answer_question span, or a no-op
        context manager when tracing is disabled.
    """
    if _provider is None:
        return nullcontext()
    return _provider.get_tracer(__name__).start_as_current_span(
        'answer_question', attributes={'question.id': question.get('id', ''),
                                       'business.id': question.get('business_id', '')})


def record_tool_calls(calls):
    """Export worker-observed helper calls beneath the current agent step.

    Args:
        calls: Iterable of dictionaries with name, started_ns, ended_ns and
            summarized arguments, plus optional result and error text.

    Returns:
        None. Creates TOOL spans with recorded timestamps and marks failed
        calls as errors. Does nothing when tracing is disabled.
    """
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
