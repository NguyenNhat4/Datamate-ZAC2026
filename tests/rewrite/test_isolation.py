"""Opt-in isolated execution tests using invented data."""
import os
import json
import socket
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from smolagents import Model
from smolagents.models import ChatMessage, MessageRole
from tabular_agent.agent import answer_question
from tabular_agent.cli import parser, run
from tabular_agent.executor import IsolatedExecutor
from tabular_agent.telemetry import tracing


class ScriptedModel(Model):
    def __init__(self, actions):
        super().__init__()
        self.actions = iter(actions)
    def generate(self, messages, **kwargs):
        return ChatMessage(role=MessageRole.ASSISTANT, content='Thought: calculate.\n<code>\n' + next(self.actions) + '\n</code>')


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
    def test_error_discards_mutated_state(self):
        self.worker("df=read_table('F1')")
        with self.assertRaisesRegex(ValueError, 'worker reset'):
            self.worker("df['bad']=1\nraise ValueError('failure')")
        self.assertIn('False', self.worker("print('df' in globals())").logs)
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

    def test_memory_records_worker_error_and_next_step_recovers(self):
        events = []
        model = ScriptedModel(["df=read_table('F1')\nraise ValueError('synthetic failure')",
                               "df=read_table('F1')\nfinal_answer({'type':'number','answer':len(df)})"])
        answer = answer_question(model, {'question': 'Count?', 'as_of_date': '2026-01-01'},
                                 self.business, self.root / 'calendar.csv', 'Count rows.', emit=events.append)
        steps = [event for event in events if event['stage'] == 'memory_step']
        self.assertEqual(answer, {'type': 'number', 'answer': 2})
        self.assertEqual(len(steps), 2)
        self.assertIn('synthetic failure', steps[0]['error']['message'])
        self.assertIn('worker reset', steps[0]['error']['message'])
        self.assertIn('synthetic failure', str(steps[1]['model_input_messages']))

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
        model = ScriptedModel(["df=read_table('F1')\nprint(len(df))", "final_answer({'type':'number','answer':len(df)})"])
        with patch('tabular_agent.cli.create_model', return_value=model):
            self.assertEqual(run(args), 0)
        text = (output / 'logs' / '000000.jsonl').read_text()
        steps = [json.loads(line) for line in text.splitlines() if json.loads(line)['stage'] == 'memory_step']
        self.assertEqual(len(steps), 2)
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
