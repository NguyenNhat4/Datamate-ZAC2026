"""Synthetic tests for smolagents memory capture and question isolation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from smolagents import Model
from smolagents.models import ChatMessage, MessageRole, TokenUsage
from smolagents.local_python_executor import CodeOutput
from tabular_agent.agent import answer_question, build_agent


class ScriptedModel(Model):
    def __init__(self, outputs):
        super().__init__()
        self.outputs = iter(outputs)

    def generate(self, messages, **kwargs):
        return ChatMessage(role=MessageRole.ASSISTANT,
                           content='Thought: work.\n<code>\n' + next(self.outputs) + '\n</code>')


class ScriptedExecutor:
    def __init__(self, *args, **kwargs):
        self.calls = 0

    def send_tools(self, tools):
        pass

    def send_variables(self, variables):
        pass

    def __call__(self, code):
        self.calls += 1
        if self.calls == 1 and not code.startswith('final_answer'):
            return CodeOutput(None, 'synthetic observation', False)
        return CodeOutput({'type': 'number', 'answer': 2}, '', True)

    def cleanup(self):
        pass


class MemoryTests(unittest.TestCase):
    def test_step_callback_saves_the_framework_memory_before_each_call(self):
        model = ScriptedModel(['print(2)', "final_answer({'type':'number','answer':2})"])
        events = []
        with patch('tabular_agent.agent.IsolatedExecutor', ScriptedExecutor):
            agent, _ = build_agent(model, None, None, emit=events.append)
            answer = agent.run('synthetic task')
        self.assertEqual(answer['answer'], 2)
        self.assertEqual([event['step'] for event in events], [1, 2])
        self.assertIn('synthetic task', json.dumps(events[0]['model_input_messages']))
        self.assertNotIn('synthetic observation', json.dumps(events[0]['model_input_messages']))
        self.assertIn('synthetic observation', json.dumps(events[1]['model_input_messages']))
        self.assertIn('synthetic observation', events[0]['observations'])
        self.assertTrue(events[1]['is_final_answer'])
        self.assertEqual(len(agent.memory.get_succinct_steps()), 3)

    def test_separate_questions_do_not_share_memory(self):
        events1, events2 = [], []
        model = ScriptedModel(["final_answer({'type':'number','answer':2})"] * 2)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'calendar.csv').write_text('date\n')
            with patch('tabular_agent.agent.IsolatedExecutor', ScriptedExecutor):
                for text, events in [('first secret question', events1), ('second question', events2)]:
                    answer_question(model, {'question': text, 'as_of_date': '2026-01-01'}, root,
                                    root / 'calendar.csv', 'synthetic rules', emit=events.append)
        second_memory = json.dumps(events2[0]['model_input_messages'])
        self.assertIn('second question', second_memory)
        self.assertNotIn('first secret question', second_memory)

    def test_recorded_steps_are_json_serializable(self):
        model = ScriptedModel(["final_answer({'type':'number','answer':2})"])
        lines = []
        with patch('tabular_agent.agent.IsolatedExecutor', ScriptedExecutor):
            agent, _ = build_agent(model, None, None, emit=lambda event: lines.append(json.dumps(event)))
            agent.run('synthetic task')
        self.assertEqual(json.loads(lines[0])['stage'], 'memory_step')

    def test_reported_model_token_usage_is_saved(self):
        class UsageModel(Model):
            def generate(self, messages, **kwargs):
                return ChatMessage(role=MessageRole.ASSISTANT,
                                   content="Thought: answer.\n<code>\nfinal_answer({'type':'number','answer':2})\n</code>",
                                   token_usage=TokenUsage(input_tokens=125, output_tokens=20))
        events = []
        with patch('tabular_agent.agent.IsolatedExecutor', ScriptedExecutor):
            agent, _ = build_agent(UsageModel(), None, None, emit=events.append)
            agent.run('synthetic task')
        self.assertEqual(events[0]['token_usage']['input_tokens'], 125)
        self.assertEqual(events[0]['token_usage']['output_tokens'], 20)


if __name__ == '__main__':
    unittest.main()
