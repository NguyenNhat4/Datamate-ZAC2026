"""Opt-in isolated execution tests using invented data."""
import os
import json
import socket
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from smolagents import Model
from smolagents.models import ChatMessage, MessageRole, ChatMessageToolCall, ChatMessageToolCallFunction
from tabular_agent.agent import answer_question, answer_with_verification, source_preview
from tabular_agent.cli import parser, run
from tabular_agent.executor import IsolatedExecutor
from tabular_agent.telemetry import tracing


class ScriptedModel(Model):
    def __init__(self, actions):
        super().__init__()
        self.actions = iter(actions)
    def generate(self, messages, **kwargs):
        action = next(self.actions)
        if isinstance(action, Exception):
            raise action
        return ChatMessage(role=MessageRole.ASSISTANT, content='Thought: calculate.\n<code>\n' + action + '\n</code>')


class ManagedScriptedModel(ScriptedModel):
    def __init__(self, actions, manager_calls):
        super().__init__(actions)
        self.manager_calls = iter(manager_calls)
        self.requests = []
    def generate(self, messages, **kwargs):
        self.requests.append(str(messages))
        if kwargs.get('tools_to_call_from') is not None:
            name, args = next(self.manager_calls)
            call = ChatMessageToolCall(function=ChatMessageToolCallFunction(name=name, arguments=args),
                                       id=f'call_{len(self.requests)}', type='function')
            return ChatMessage(role=MessageRole.ASSISTANT, tool_calls=[call])
        return super().generate(messages, **kwargs)


@unittest.skipUnless(os.getenv('RUN_SANDBOX_TESTS') == '1', 'requires bubblewrap namespaces')
class IsolatedWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.business = self.root / 'business'
        self.business.mkdir()
        (self.business / 'sales.csv').write_text('item,amount\nA,12\nB,15\n')
        (self.root / 'calendar.csv').write_text('date\n')
        (self.root / 'secret').write_text('secret')
        self.worker = IsolatedExecutor(self.business, self.root / 'calendar.csv')
    def tearDown(self):
        self.worker.cleanup()
        self.tmp.cleanup()
    def test_tool_dataframes_persist(self):
        self.worker("df=read_table('F1')")
        output = self.worker("final_answer({'type':'number', 'answer':len(df)})")
        self.assertEqual(output.output['answer'], 2)

    def test_bare_inspection_results_reach_agent(self):
        self.assertIn('sales.csv', self.worker('list_files()').logs)
        self.assertIn('amount', self.worker("inspect_table('F1')").logs)

    def test_source_preview_runs_in_worker_and_contains_header(self):
        preview = source_preview(self.business, self.root / 'calendar.csv', 30)
        self.assertIn('sales.csv', preview)
        self.assertIn('amount', preview)
        self.assertNotIn('secret', preview)

    def test_full_read_reports_observed_categories_and_missing_values(self):
        (self.business / 'sales.csv').write_text('order,status\nA,complete\nA,complete\nB,cancelled\nC,\n')
        output = self.worker("df = read_table('F1')")
        self.assertIn('complete', output.logs)
        self.assertIn('cancelled', output.logs)
        self.assertIn("'missing': 1", output.logs)

    def test_first_read_and_answer_are_separated_without_losing_state(self):
        output = self.worker("df = read_table('F1')\nfinal_answer({'type':'number','answer':len(df)})")
        self.assertFalse(output.is_final_answer)
        self.assertIn('Review the full-read observations', output.logs)
        self.assertEqual(output.output, {'type': 'number', 'answer': 2})
        result = self.worker("final_answer({'type':'number','answer':len(df)})")
        self.assertEqual(result.output['answer'], 2)

    def test_unanswerable_requires_a_full_source_read(self):
        with self.assertRaisesRegex(ValueError, 'preview cannot establish'):
            self.worker("inspect_table('F1')\nfinal_answer({'type':'unanswerable','reason':'missing'})")
        self.worker("df = read_table('F1')")
        result = self.worker("final_answer({'type':'unanswerable','reason':'missing'})")
        self.assertEqual(result.output['type'], 'unanswerable')

    def test_near_miss_category_is_reported_instead_of_silent_zero(self):
        with self.assertRaisesRegex(ValueError, 'similar observed values'):
            self.worker("s = pd.Series(['Done', 'Already cancelled'])\nprint((s == 'cancelled').sum())")
        result = self.worker("s = pd.Series(['Done', 'Already cancelled'])\nprint((s == 'absent').sum())")
        self.assertIn('0', result.logs)
        result = self.worker("print((s == 'Already cancelled').sum())")
        self.assertIn('1', result.logs)

    def test_day_first_dates_are_not_silently_compared_as_text(self):
        with self.assertRaisesRegex(ValueError, 'parse the column'):
            self.worker("dates = pd.Series(['02/03/2025', '12/04/2025'])\nprint(dates >= '01/03/2025')")
        result = self.worker("dates = pd.to_datetime(pd.Series(['02/03/2025']), format='%d/%m/%Y')\nprint((dates >= '2025-03-01').sum())")
        self.assertIn('1', result.logs)

    def test_ambiguous_row_counts_require_explicit_entity_or_row_count(self):
        with self.assertRaisesRegex(ValueError, 'repeated values'):
            self.worker("df = pd.DataFrame({'order_id': ['A', 'A', 'B']})\nprint(len(df))")
        with self.assertRaisesRegex(ValueError, 'repeated values'):
            self.worker("df = pd.DataFrame({'order_id': ['A', 'A', 'B']})\nprint(df.shape[0])")
        result = self.worker("df = pd.DataFrame({'order_id': ['A', 'A', 'B']})\n"
                             "print(df['order_id'].nunique())\nprint(len(df.index))")
        self.assertIn('2', result.logs)
        self.assertIn('3', result.logs)
    def test_error_discards_mutated_state(self):
        self.worker("df=read_table('F1')")
        with self.assertRaisesRegex(ValueError, 'worker reset'):
            self.worker("df['bad']=1\nraise ValueError('failure')")
        self.assertIn('False', self.worker("print('df' in globals())").logs)

    def test_pandas_alias_is_available_after_worker_reset(self):
        with self.assertRaisesRegex(ValueError, 'worker reset'):
            self.worker("raise ValueError('synthetic')")
        self.assertIn('2025-03-02', self.worker("print(pd.Timestamp('2025-03-02').date())").logs)
    def test_error_keeps_prior_output_but_resets_state(self):
        with self.assertRaisesRegex(ValueError, 'known-column') as error:
            self.worker("print('known-column')\nvalue=1\nraise ValueError('failure')")
        self.assertIn('worker reset', str(error.exception))
        self.assertIn('False', self.worker("print('value' in globals())").logs)

    def test_non_json_final_answer_has_actionable_error(self):
        with self.assertRaisesRegex(ValueError, 'convert pandas objects'):
            self.worker("import pandas as pd\nfinal_answer({'type': 'number', 'answer': pd.Series([1])})")

    def test_decimal_final_answer_is_json_serializable(self):
        result = self.worker("values = parse_numbers(['12', '1.25'], '.', '')\n"
                             "final_answer({'type': 'list', 'answer': [values.iloc[0], values.iloc[1]]})")
        self.assertEqual(result.output, {'type': 'list', 'answer': [12, 1.25]})
        with self.assertRaisesRegex(ValueError, 'without loss'):
            self.worker("from decimal import Decimal\nfinal_answer({'type':'number', 'answer':Decimal('1.234567890123456789')})")

    def test_host_files_not_mounted_and_input_read_only(self):
        result = self.worker("from pathlib import Path\nprint(Path('/secret').exists())")
        self.assertIn('False', result.logs)
        with self.assertRaisesRegex(ValueError, 'Read-only'):
            self.worker("from pathlib import Path\nPath('/data/sales.csv').write_text('changed')")
        self.assertIn('amount', (self.business / 'sales.csv').read_text())
    def test_worker_cannot_reach_host_network(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            port = listener.getsockname()[1]
            code = ("import socket\ns=socket.socket()\ns.settimeout(1)\n"
                    f"print(s.connect_ex(('127.0.0.1', {port})) != 0)\ns.close()")
            self.assertIn('True', self.worker(code).logs)
    def test_codeagent_uses_actual_registered_tools(self):
        model = ScriptedModel(["df=read_table('F1')\nprint(len(df))", "final_answer({'type':'number','answer':len(df)})"])
        answer = answer_question(model, {'question':'Count?', 'as_of_date':'2026-01-01'}, self.business,
                                 self.root / 'calendar.csv', 'Count rows.')
        self.assertEqual(answer, {'type':'number','answer':2})

    def test_disagreement_hands_work_to_fresh_adjudicator(self):
        model = ManagedScriptedModel(["final_answer({'type':'number','answer':2})",
                                      "final_answer({'type':'number','answer':3})",
                                      "final_answer({'type':'number','answer':3})"],
                                     [('solver', {'task': 'Solve'}), ('reviewer', {'task': 'Review'}),
                                      ('adjudicator', {'task': 'Resolve'}),
                                      ('final_answer', {'answer': {'type': 'number', 'answer': 3}})])
        events = []
        answer = answer_with_verification(model, {'question': 'Synthetic question', 'as_of_date': '2026-01-01'},
                                          self.business, self.root / 'calendar.csv', 'Synthetic rules', emit=events.append)
        self.assertEqual(answer, {'type': 'number', 'answer': 3})
        self.assertTrue(any('prior_agent_work' in request and 'solver' in request for request in model.requests))
        self.assertTrue(any('prior_agent_work' in request and 'reviewer' in request for request in model.requests))
        self.assertEqual([e['phase'] for e in events if e['stage'] == 'memory_step'],
                         ['solver', 'manager', 'reviewer', 'manager', 'adjudicator', 'manager'])

    def test_manager_cannot_submit_unsupported_answer(self):
        model = ManagedScriptedModel(["final_answer({'type':'number','answer':2})",
                                      "final_answer({'type':'number','answer':3})"],
                                     [('solver', {'task': 'Solve'}), ('reviewer', {'task': 'Review'}),
                                      ('final_answer', {'answer': {'type': 'number', 'answer': 99}}),
                                      ('final_answer', {'answer': {'type': 'abstain'}})])
        events = []
        result = answer_with_verification(model, {'question': 'Synthetic', 'as_of_date': '2026-01-01'},
                                          self.business, self.root / 'calendar.csv', 'Rules', emit=events.append)
        self.assertEqual(result['type'], 'abstain')
        self.assertTrue(any('two managed agents' in str(e.get('error')) for e in events))

    def test_failed_solver_can_be_followed_by_reviewer_and_adjudicator(self):
        model = ManagedScriptedModel([RuntimeError('synthetic model failure'),
                                      "final_answer({'type':'number','answer':2})",
                                      "final_answer({'type':'number','answer':2})"],
                                     [('solver', {'task': 'Solve'}), ('reviewer', {'task': 'Review'}),
                                      ('adjudicator', {'task': 'Resolve'}),
                                      ('final_answer', {'answer': {'type': 'number', 'answer': 2}})])
        result = answer_with_verification(model, {'question': 'Synthetic', 'as_of_date': '2026-01-01'},
                                          self.business, self.root / 'calendar.csv', 'Rules')
        self.assertEqual(result, {'type': 'number', 'answer': 2})

    def test_memory_records_worker_error_and_next_step_recovers(self):
        events = []
        model = ScriptedModel(["df=read_table('F1')\nprint('inspected-column')\nraise ValueError('synthetic failure')",
                               "df=read_table('F1')\nfinal_answer({'type':'number','answer':len(df)})",
                               "final_answer({'type':'number','answer':len(df)})"])
        answer = answer_question(model, {'question': 'Count?', 'as_of_date': '2026-01-01'},
                                 self.business, self.root / 'calendar.csv', 'Count rows.', emit=events.append)
        steps = [event for event in events if event['stage'] == 'memory_step']
        self.assertEqual(answer, {'type': 'number', 'answer': 2})
        self.assertEqual(len(steps), 3)
        self.assertIn('synthetic failure', steps[0]['error']['message'])
        self.assertIn('worker reset', steps[0]['error']['message'])
        self.assertIn('synthetic failure', str(steps[1]['model_input_messages']))
        self.assertIn('inspected-column', str(steps[1]['model_input_messages']))

    def test_cli_saves_memory_in_question_log_without_reference_answers(self):
        split = self.root / 'split'
        business = split / 'data' / 'shop'
        business.mkdir(parents=True)
        (business / 'sales.csv').write_text('item,amount\nA,12\nB,15\n')
        (split / 'rulebook.md').write_text('Count rows')
        (split / 'calendar.csv').write_text('date\n')
        question = {'id': 'q1', 'business_id': 'shop', 'question': 'Count?', 'as_of_date': '2026-01-01'}
        (split / 'questions.jsonl').write_text(json.dumps(question) + '\n')
        (split / 'answers.jsonl').write_text('{"id":"q1","type":"string","answer":"reference-secret"}\n')
        output = self.root / 'output'
        args = parser().parse_args(['predict', '--split', str(split), '--output', str(output), '--model', 'fake'])
        model = ManagedScriptedModel(["df=read_table('F1')\nprint(len(df))", "final_answer({'type':'number','answer':len(df)})"] * 2,
                                     [('solver', {'task': 'Solve'}), ('reviewer', {'task': 'Review'}),
                                      ('final_answer', {'answer': {'type': 'number', 'answer': 2}})])
        with patch('tabular_agent.cli.create_model', return_value=model):
            self.assertEqual(run(args), 0)
        text = (output / 'logs' / '000000.jsonl').read_text()
        steps = [json.loads(line) for line in text.splitlines() if json.loads(line)['stage'] == 'memory_step']
        self.assertEqual([step['phase'] for step in steps], ['solver', 'solver', 'manager', 'reviewer', 'reviewer', 'manager'])
        self.assertIn('Count rows', json.dumps(steps[0]['model_input_messages']))
        self.assertNotIn('reference-secret', text)

    def test_worker_tool_calls_are_children_of_the_agent_step(self):
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        model = ScriptedModel(["df=read_table('F1')\nprint(len(df))", "final_answer({'type':'number','answer':len(df)})"])
        exporter = InMemorySpanExporter()
        events = []
        with tracing(True, exporter=exporter):
            answer = answer_question(model, {'id': 'synthetic', 'business_id': 'shop', 'question':'Count?',
                                              'as_of_date':'2026-01-01'}, self.business,
                                     self.root / 'calendar.csv', 'Count rows.', emit=events.append)
            spans = exporter.get_finished_spans()
        self.assertEqual(answer, {'type':'number','answer':2})
        tool = next(span for span in spans if span.name == 'read_table')
        step = next(span for span in spans if span.name == 'Step 1')
        root = next(span for span in spans if span.name == 'answer_question')
        self.assertEqual(tool.parent.span_id, step.context.span_id)
        self.assertEqual(tool.context.trace_id, root.context.trace_id)
        self.assertEqual(root.attributes['question.id'], 'synthetic')
        self.assertIn('F1', tool.attributes['input.value'])
        self.assertIn('DataFrame', tool.attributes['output.value'])
        self.assertEqual(len([event for event in events if event['stage'] == 'memory_step']), 2)

    def test_failed_worker_tool_is_reported_as_error(self):
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from opentelemetry.trace import StatusCode
        exporter = InMemorySpanExporter()
        with tracing(True, exporter=exporter):
            with self.assertRaisesRegex(ValueError, 'worker reset'):
                self.worker("read_table('missing.csv')")
            spans = exporter.get_finished_spans()
        tool = next(span for span in spans if span.name == 'read_table')
        self.assertEqual(tool.status.status_code, StatusCode.ERROR)
    def test_spreadsheet_database_and_explicit_number_parsing(self):
        import sqlite3
        import openpyxl
        book = openpyxl.Workbook()
        book.active.title = 'January'
        book.active.append(['amount'])
        book.active.append([12])
        book.create_sheet('February').append(['amount'])
        book['February'].append([15])
        book.save(self.business / 'sales.xlsx')
        with sqlite3.connect(self.business / 'orders.sqlite') as db:
            db.execute('CREATE TABLE orders (id INTEGER)')
            db.execute('INSERT INTO orders VALUES (1)')
        result = self.worker("print(len(read_workbook('sales.xlsx')))\n"
                             "print(len(read_table('orders.sqlite', table='orders')))\n"
                             "print(sum(parse_numbers(['1.200,5'], ',', '.')))")
        self.assertIn('2', result.logs)
        self.assertIn('1', result.logs)
        self.assertIn('1200.5', result.logs)
