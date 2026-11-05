"""Synthetic OpenTelemetry checks; no model or collector service is required."""
import unittest
from unittest.mock import patch

from openai.types.chat import ChatCompletion
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tabular_agent.model import create_model
from tabular_agent.telemetry import is_enabled, tracing


class TelemetryTests(unittest.TestCase):
    def test_disabled_does_not_instrument(self):
        with tracing(False):
            self.assertFalse(is_enabled())

    def test_local_model_call_is_instrumented(self):
        response = ChatCompletion.model_validate({
            'id': 'synthetic', 'object': 'chat.completion', 'created': 0, 'model': 'fixture',
            'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'hello'}}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 1, 'total_tokens': 4},
        })
        model = create_model('fixture', 'http://127.0.0.1:8000/v1')
        exporter = InMemorySpanExporter()
        try:
            with tracing(True, exporter=exporter):
                with patch.object(model.client.chat.completions, 'create', return_value=response):
                    self.assertEqual(model.generate([{'role': 'user', 'content': 'hi'}]).content, 'hello')
                spans = exporter.get_finished_spans()
                self.assertTrue(any(s.attributes.get('openinference.span.kind') == 'LLM' for s in spans))
            self.assertFalse(is_enabled())
        finally:
            model.client.close()

    def test_missing_local_collector_has_actionable_error(self):
        with patch('tabular_agent.telemetry.socket.create_connection', side_effect=ConnectionRefusedError):
            with self.assertRaisesRegex(RuntimeError, 'make telemetry'):
                with tracing(True):
                    pass
