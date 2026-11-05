"""Synthetic checks for the rewritten agent; no model service or test answers are used."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from smolagents import Model
from smolagents.models import ChatMessage, MessageRole
from smolagents.local_python_executor import CodeOutput
from tabular_agent.agent import build_agent
from tabular_agent.cli import parser, run
from tabular_agent.contracts import validate_answer
from tabular_agent.evaluate import match, score
from tabular_agent.model import create_model
from tabular_agent.tools import AGENT_TOOLS


class ScriptedModel(Model):
    def generate(self, messages, **kwargs):
        self.messages = messages
        return ChatMessage(role=MessageRole.ASSISTANT, content="Thought: done.\n<code>\nfinal_answer({'type':'number','answer': 2})\n</code>")


class FakeExecutor:
    def __init__(self, *args, **kwargs): pass
    def send_tools(self, tools): self.tools = tools
    def send_variables(self, variables): pass
    def __call__(self, code): return CodeOutput({'type': 'number', 'answer': 2}, '', True)
    def cleanup(self): pass


class RewriteTests(unittest.TestCase):
    def test_tools_are_registered_and_descriptions_reach_model(self):
        model = ScriptedModel()
        with patch('tabular_agent.agent.IsolatedExecutor', FakeExecutor):
            agent, executor = build_agent(model, None, None)
            self.assertEqual(agent.run('question'), {'type': 'number', 'answer': 2})
        self.assertEqual(set(executor.tools), {tool.name for tool in AGENT_TOOLS} | {'final_answer'})
        text = str(model.messages)
        for tool in AGENT_TOOLS:
            self.assertIn(tool.name, text)
            self.assertIn(tool.description, text)

    def test_answer_contract(self):
        self.assertEqual(validate_answer({'type': 'number', 'answer': 2}), {'type': 'number', 'answer': 2})
        for value in (True, float('nan'), '2'):
            with self.assertRaises(ValueError):
                validate_answer({'type': 'number', 'answer': value})
        with self.assertRaises(ValueError):
            validate_answer({'type': 'date', 'answer': '2026-02-31'})

    def test_scoring_aliases_rows_and_penalty(self):
        self.assertTrue(match({'type': 'string', 'answer': 'CÀ PHÊ'}, {'type': 'string', 'answer': 'Cà phê'}))
        self.assertTrue(match({'type': 'list', 'answer': ['B']}, {'type': 'list', 'answer': ['A'], 'alias': [['B']]}))
        report = score([{'id': 'a'}, {'id': 'b'}], [{'id': 'a', 'type': 'number', 'answer': 1}],
                       [{'id': 'a', 'type': 'number', 'answer': 1}, {'id': 'b', 'type': 'number', 'answer': 2}])
        self.assertEqual(report['penalized_accuracy'], 0.5)

    def test_truncated_model_response_is_rejected(self):
        model = create_model('stub', 'http://127.0.0.1:8000/v1')
        incomplete = ChatMessage(role=MessageRole.ASSISTANT, content='<code>x=1</code>',
                                 raw={'choices': [{'finish_reason': 'length'}]})
        try:
            with patch('smolagents.OpenAIModel.generate', return_value=incomplete):
                with self.assertRaisesRegex(ValueError, 'generated code was not executed'):
                    model.generate([])
        finally:
            model.client.close()

    def test_predict_uses_current_rulebook_and_flushes_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            split = root / 'split'
            business = split / 'data' / 'shop'
            business.mkdir(parents=True)
            (split / 'calendar.csv').write_text('date\n')
            (split / 'rulebook.md').write_text('Current rules')
            (business / 'README.md').write_text('Business notes')
            (split / 'questions.jsonl').write_text(json.dumps({'id':'q1','business_id':'shop','question':'Q?', 'as_of_date':'2026-01-01'}) + '\n')
            out = root / 'out'
            args = parser().parse_args(['predict', '--split', str(split), '--output', str(out), '--model', 'stub'])
            with patch('tabular_agent.cli.create_model', return_value=object()), patch('tabular_agent.cli.answer_question', return_value={'type': 'number', 'answer': 2}) as answer:
                self.assertEqual(run(args), 0)
            self.assertEqual(json.loads((out / 'submission.jsonl').read_text()), {'id':'q1','type':'number','answer':2})
            self.assertEqual(answer.call_args.args[4], 'Current rules')


if __name__ == '__main__':
    unittest.main()
