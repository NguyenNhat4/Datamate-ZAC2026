"""Model-provider selection tests with synthetic requests and no network."""
import os
import unittest
from unittest.mock import patch

from openai.types.chat import ChatCompletion
from smolagents.models import ChatMessage, MessageRole, ChatMessageToolCall, ChatMessageToolCallFunction
from tabular_agent.cli import parser, run
from tabular_agent.model import create_model, create_openai_model, openai_api_key, openai_model_name, normalize_final_json


class ProviderTests(unittest.TestCase):
    def test_only_valid_explicit_final_json_becomes_code(self):
        original = 'Thought: done.\nFinal Answer:\n```json\n{"type":"percent","answer":1.66}\n```</code>'
        self.assertIn("final_answer({'type': 'percent', 'answer': 1.66})", normalize_final_json(original))
        for content in ['```json\n{"type":"number","answer":1}\n```',
                        'Final Answer: ```json\n{"type":"number","answer":"bad"}\n```',
                        '<code>print(1)</code>\nFinal Answer: ```json\n{"type":"number","answer":1}\n```']:
            self.assertEqual(normalize_final_json(content), content)

    def test_local_model_accepts_native_tool_call_finish(self):
        model = create_model('fixture', 'http://127.0.0.1:8000/v1')
        call = ChatMessageToolCall(function=ChatMessageToolCallFunction(name='solver', arguments={'task': 'solve'}),
                                   id='call1', type='function')
        response = ChatMessage(role=MessageRole.ASSISTANT, tool_calls=[call],
                               raw={'choices': [{'finish_reason': 'tool_calls'}]})
        try:
            with patch('smolagents.OpenAIModel.generate', return_value=response):
                self.assertEqual(model.generate([], tools_to_call_from=['sentinel']).tool_calls[0].function.name, 'solver')
        finally:
            model.client.close()

    def test_environment_key_takes_precedence_over_dotenv(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'environment-key'}, clear=True):
            with patch('tabular_agent.model.dotenv_values') as dotenv:
                self.assertEqual(openai_api_key(), 'environment-key')
                dotenv.assert_not_called()

    def test_dotenv_is_used_when_environment_key_missing(self):
        with patch.dict(os.environ, {}, clear=True), patch('tabular_agent.model.Path.is_file', return_value=True), \
             patch('tabular_agent.model.dotenv_values', return_value={'OPENAI_API_KEY': 'file-key'}):
            self.assertEqual(openai_api_key(), 'file-key')

    def test_missing_key_gives_actionable_error(self):
        with patch.dict(os.environ, {}, clear=True), patch('tabular_agent.model.Path.is_file', return_value=False):
            with self.assertRaisesRegex(ValueError, 'OPENAI_API_KEY'):
                create_openai_model('gpt-5.4-mini')

    def test_model_name_can_be_configured_in_dotenv(self):
        with patch.dict(os.environ, {}, clear=True), patch('tabular_agent.model.Path.is_file', return_value=True), \
             patch('tabular_agent.model.dotenv_values', return_value={'OPENAI_MODEL': 'configured-model'}):
            self.assertEqual(openai_model_name(), 'configured-model')


    def test_local_model_sends_seed_and_zero_temperature_by_default(self):
        response = ChatCompletion.model_validate({
            'id': 'synthetic', 'object': 'chat.completion', 'created': 0, 'model': 'fixture',
            'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 1, 'total_tokens': 4},
        })
        for options, expected in (({}, 0.0), ({'temperature': 0.7}, 0.7)):
            model = create_model('fixture', 'http://127.0.0.1:8000/v1', **options)
            try:
                with patch.object(model.client.chat.completions, 'create', return_value=response) as create:
                    model.generate([{'role': 'user', 'content': 'synthetic'}])
                self.assertEqual(create.call_args.kwargs['temperature'], expected)
                self.assertEqual(create.call_args.kwargs['seed'], 42)
            finally:
                model.client.close()

    def test_openai_request_parameters_use_hosted_endpoint(self):
        response = ChatCompletion.model_validate({
            'id': 'synthetic', 'object': 'chat.completion', 'created': 0, 'model': 'fixture',
            'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 1, 'total_tokens': 4},
        })
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-key'}, clear=True):
            model = create_openai_model('gpt-5.4-mini', max_tokens=321)
            try:
                self.assertEqual(str(model.client.base_url), 'https://api.openai.com/v1/')
                with patch.object(model.client.chat.completions, 'create', return_value=response) as create:
                    self.assertEqual(model.generate([{'role': 'user', 'content': 'synthetic'}],
                                                    stop_sequences=['Observation:']).content, 'ok')
                payload = create.call_args.kwargs
                self.assertEqual(payload['max_completion_tokens'], 321)
                self.assertFalse(payload['store'])
                self.assertNotIn('max_tokens', payload)
                self.assertNotIn('stop', payload)
                self.assertNotIn('synthetic-key', str(payload))
            finally:
                model.client.close()

    def test_local_provider_rejects_remote_endpoint(self):
        with self.assertRaisesRegex(ValueError, 'local HTTP'):
            create_model('fixture', 'https://api.openai.com/v1')

    def test_provider_error_does_not_expose_key(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-secret'}, clear=True):
            model = create_openai_model('fixture')
            try:
                with patch.object(model.client.chat.completions, 'create', side_effect=RuntimeError('bad synthetic-secret')):
                    with self.assertRaises(RuntimeError) as raised:
                        model.generate([{'role': 'user', 'content': 'synthetic'}])
                self.assertNotIn('synthetic-secret', str(raised.exception))
                self.assertIn('[redacted]', str(raised.exception))
            finally:
                model.client.close()

    def test_openai_provider_ignores_local_environment_settings(self):
        args = parser().parse_args(['doctor', '--provider', 'openai'])
        with patch.dict(os.environ, {'MODEL': 'local-alias', 'BASE_URL': 'http://127.0.0.1:1234/v1'}, clear=True), \
             patch('tabular_agent.cli.openai_model_name', return_value='gpt-5.4-mini'), \
             patch('tabular_agent.cli.create_openai_model') as create:
            self.assertEqual(run(args), 0)
        self.assertEqual(create.call_args.args[0], 'gpt-5.4-mini')

    def test_openai_provider_disallows_custom_endpoint(self):
        args = parser().parse_args(['doctor', '--provider', 'openai', '--base-url', 'http://127.0.0.1:8000/v1'])
        with self.assertRaisesRegex(ValueError, '--base-url'):
            run(args)

    def test_default_provider_remains_local(self):
        args = parser().parse_args(['doctor', '--model', 'fixture'])
        with patch.dict(os.environ, {}, clear=True), patch('tabular_agent.cli.create_model') as create:
            self.assertEqual(run(args), 0)
        self.assertEqual(create.call_args.args[:2], ('fixture', 'http://127.0.0.1:18234/v1'))

    def test_local_defaults_follow_the_benchmark_server(self):
        args = parser().parse_args(['doctor'])
        with patch.dict(os.environ, {'LOCAL_PORT': '19000', 'LOCAL_MODEL_ALIAS': 'alias'}, clear=True), \
             patch('tabular_agent.cli.create_model') as create:
            self.assertEqual(run(args), 0)
        self.assertEqual(create.call_args.args[:2], ('alias', 'http://127.0.0.1:19000/v1'))

    def test_local_defaults_ignore_generic_environment_names(self):
        # Importing smolagents loads .env into the environment; its MODEL or BASE_URL may be meant for other tools.
        args = parser().parse_args(['doctor'])
        with patch.dict(os.environ, {'MODEL': 'gpt-4o-mini', 'BASE_URL': 'http://127.0.0.1:1234/v1', 'BACKEND': 'ollama'},
                        clear=True), patch('tabular_agent.cli.create_model') as create:
            self.assertEqual(run(args), 0)
        self.assertEqual(create.call_args.args[:2], ('local-coder-7b', 'http://127.0.0.1:18234/v1'))
