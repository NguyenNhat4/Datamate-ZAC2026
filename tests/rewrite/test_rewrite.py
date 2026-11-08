"""Synthetic checks for the rewritten agent; no model service or test answers are used."""
import json
import importlib.util
import importlib.resources
import contextlib
import io
from decimal import Decimal
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import yaml

from smolagents import Model
from smolagents.models import ChatMessage, MessageRole
from smolagents.local_python_executor import CodeOutput
from tabular_agent.agent import build_agent
from tabular_agent.cli import parser, run, time_limit, QuestionDeadlineExceeded
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
    def test_question_deadline_cannot_be_swallowed_by_agent_exception_handler(self):
        with self.assertRaises(QuestionDeadlineExceeded):
            with time_limit(0.02):
                try:
                    time.sleep(0.2)
                except Exception:
                    self.fail('agent caught the deadline')


    def test_single_agent_retries_truncated_response_without_execution(self):
        class TruncatedThenAnswer(Model):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def generate(self, messages, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise ValueError("Incomplete model response ('length'); generated code was not executed")
                return ChatMessage(role=MessageRole.ASSISTANT,
                                   content="Thought: finish.\n<code>\nfinal_answer({'type':'number','answer':2})\n</code>")

        model = TruncatedThenAnswer()
        with patch('tabular_agent.agent.IsolatedExecutor', FakeExecutor):
            agent, _ = build_agent(model, None, None, max_steps=3, limit_repairs=True)
            self.assertEqual(agent.run('Synthetic question'), {'type': 'number', 'answer': 2})
        self.assertEqual(model.calls, 2)

    def test_distinct_percentage_counts_entities_and_checks_categories(self):
        import pandas as pd
        from tabular_agent.tools import distinct_percentage
        frame = pd.DataFrame({'id': ['A', 'A', 'B', 'C', 'C'],
                              'status': ['complete', 'complete', 'cancelled', 'complete', 'complete']})
        self.assertAlmostEqual(distinct_percentage.forward(frame, 'id', 'status', ['cancelled']), 100 / 3)
        with self.assertRaisesRegex(ValueError, 'not present'):
            distinct_percentage.forward(frame, 'id', 'status', ['cancel'])
        self.assertEqual(distinct_percentage.forward(frame, 'id', 'status', ['cancel'], True), 0)
        frame.loc[0, 'status'] = 'cancelled'
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            distinct_percentage.forward(frame, 'id', 'status', ['cancelled'])

    def test_column_profile_reports_missing_and_observed_values(self):
        import pandas as pd
        from tabular_agent.tools import profile_columns
        df = pd.DataFrame({'status': ['done', None, 'cancelled', 'done']})
        result = profile_columns.forward(df, ['status'])['status']
        self.assertEqual((result['rows'], result['nulls'], result['distinct']), (4, 1, 2))
        self.assertEqual(result['frequent_values'][0], {'value': 'done', 'count': 2})
        with self.assertRaisesRegex(ValueError, 'available columns'):
            profile_columns.forward(df, ['missing'])

    def test_explicit_date_parser_preserves_index_and_rejects_bad_values(self):
        import pandas as pd
        from tabular_agent.tools import parse_dates
        series = pd.Series(['02/03/2025', None], index=[4, 7])
        result = parse_dates.forward(series, '%d/%m/%Y')
        self.assertEqual(list(result.index), [4, 7])
        self.assertEqual(result.iloc[0].strftime('%Y-%m-%d'), '2025-03-02')
        self.assertTrue(pd.isna(result.iloc[1]))
        with self.assertRaises(ValueError):
            parse_dates.forward(['31/02/2025'], '%d/%m/%Y')

    def test_bare_inspections_are_visible_and_evaluated_once(self):
        spec = importlib.util.spec_from_file_location('worker_for_test', Path(__file__).parents[2] / 'tabular_agent' / 'worker.py')
        module = importlib.util.module_from_spec(spec)
        from tabular_agent import tools
        with patch.dict('sys.modules', {'tools': tools}):
            spec.loader.exec_module(module)
        calls = []
        output = io.StringIO()
        def listing():
            calls.append(1)
            return [{'alias': 'F1'}]
        scope = {'list_files': listing, '_show_inspection': lambda value: print(repr(value))}
        with contextlib.redirect_stdout(output):
            exec(module.compile_action('list_files()\nitems = list_files()'), scope)
        self.assertIn('F1', output.getvalue())
        self.assertEqual(len(calls), 2)
        self.assertEqual(scope['items'], [{'alias': 'F1'}])

    def test_single_excel_sheet_can_be_read_without_sheet_argument(self):
        import openpyxl
        from tabular_agent.tools import read_table
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'orders.xlsx'
            book = openpyxl.Workbook()
            book.active.append(['amount'])
            book.active.append([12])
            book.save(path)
            with patch('tabular_agent.tools._source', return_value=path):
                self.assertEqual(read_table.forward('F1')['amount'].tolist(), [12])
                book.create_sheet('Other')
                book.save(path)
                with self.assertRaisesRegex(ValueError, 'available sheets'):
                    read_table.forward('F1')

    def test_decimal_encoding_preserves_values_and_rejects_precision_loss(self):
        spec = importlib.util.spec_from_file_location('worker_for_test', Path(__file__).parents[2] / 'tabular_agent' / 'worker.py')
        module = importlib.util.module_from_spec(spec)
        from tabular_agent import tools
        with patch.dict('sys.modules', {'tools': tools}):
            spec.loader.exec_module(module)
        values = tools.parse_numbers.forward(['12', '1.25'], '.', '')
        encoded = json.dumps({'answer': [values.iloc[0], values.iloc[1]]}, default=module.encode_decimal)
        self.assertEqual(json.loads(encoded), {'answer': [12, 1.25]})
        with self.assertRaisesRegex(ValueError, 'without loss'):
            json.dumps({'answer': Decimal('1.234567890123456789')}, default=module.encode_decimal)
        with self.assertRaisesRegex(ValueError, 'non-finite'):
            json.dumps({'answer': Decimal('NaN')}, default=module.encode_decimal)

    def test_exhausted_steps_abstain_without_another_model_call(self):
        class NonFinalExecutor(FakeExecutor):
            def __call__(self, code):
                return CodeOutput(None, '', False)

        class CountingModel(ScriptedModel):
            calls = 0
            def generate(self, messages, **kwargs):
                self.calls += 1
                return super().generate(messages, **kwargs)

        model = CountingModel()
        with patch('tabular_agent.agent.IsolatedExecutor', NonFinalExecutor):
            agent, _ = build_agent(model, None, None, max_steps=1)
            answer = agent.run('question')
        self.assertEqual(answer['type'], 'abstain')
        self.assertIn('step limit', answer['reason'])
        self.assertEqual(model.calls, 1)

    def test_tools_are_registered_and_descriptions_reach_model(self):
        model = ScriptedModel()
        with patch('tabular_agent.agent.IsolatedExecutor', FakeExecutor):
            agent, executor = build_agent(model, None, None)
            template = yaml.safe_load(importlib.resources.files('smolagents.prompts').joinpath('code_agent.yaml').read_text())
            self.assertEqual(agent.prompt_templates, template)
            self.assertEqual(agent.run('question'), {'type': 'number', 'answer': 2})
        self.assertEqual(set(executor.tools), {tool.name for tool in AGENT_TOOLS} | {'final_answer'})
        text = str(model.messages)
        self.assertIn('0.36', text)
        self.assertIn('reload data and fix the cause', text)
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
            with patch('tabular_agent.cli.create_model', return_value=object()), patch('tabular_agent.cli.source_preview', return_value='synthetic preview'), patch('tabular_agent.cli.answer_question', return_value={'type': 'number', 'answer': 2}) as answer:
                self.assertEqual(run(args), 0)
            self.assertEqual(json.loads((out / 'submission.jsonl').read_text()), {'id':'q1','type':'number','answer':2})
            self.assertEqual(answer.call_args.args[4], 'Current rules')


if __name__ == '__main__':
    unittest.main()
