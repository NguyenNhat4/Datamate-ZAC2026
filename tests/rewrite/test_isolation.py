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
from tabular_agent.agent import answer_question, source_preview
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

    def test_as_of_date_is_available_after_worker_reset(self):
        worker = IsolatedExecutor(self.business, self.root / 'calendar.csv', as_of_date='2026-01-02')
        try:
            self.assertIn('2026-01-02', worker('print(as_of_date)').logs)
            with self.assertRaises(ValueError):
                worker("raise ValueError('reset')")
            self.assertIn('2026-01-02', worker('print(as_of_date)').logs)
        finally:
            worker.cleanup()

    def test_iso_date_variable_does_not_silently_mismatch_day_first_strings(self):
        code = ("dates=pd.Series(['18/10/2025','19/10/2025'])\n"
                "today='2025-10-18'\nprint(dates == today)")
        with self.assertRaisesRegex(ValueError, 'day/month/year text'):
            self.worker(code)

    def test_python_date_does_not_silently_mismatch_timestamps(self):
        code = ("dates=pd.to_datetime(pd.Series(['2025-10-18']))\n"
                "today=datetime.date(2025,10,18)\nprint(dates == today)")
        with self.assertRaisesRegex(ValueError, 'Date types do not match'):
            self.worker(code)

    def test_timestamp_does_not_silently_mismatch_python_date_series(self):
        code = ("dates=pd.to_datetime(pd.Series(['2025-10-18'])).dt.date\n"
                "today=pd.Timestamp('2025-10-18')\nprint(dates == today)")
        with self.assertRaisesRegex(ValueError, 'Date types do not match'):
            self.worker(code)

    def test_datetime_available_after_worker_reset(self):
        self.assertIn('2025-10-18', self.worker("print(datetime.datetime.strptime('2025-10-18', '%Y-%m-%d').date())").logs)
        with self.assertRaises(ValueError):
            self.worker("raise ValueError('reset')")
        self.assertIn('2025-10-18', self.worker("print(datetime.date(2025, 10, 18))").logs)

    def test_bare_inspection_results_reach_agent(self):
        self.assertIn('sales.csv', self.worker('list_files()').logs)
        self.assertIn('amount', self.worker("inspect_table('F1')").logs)

    def test_source_preview_runs_in_worker_and_contains_header(self):
        preview = source_preview(self.business, self.root / 'calendar.csv', 30)
        self.assertIn('sales.csv', preview)
        self.assertIn('amount', preview)
        self.assertNotIn('secret', preview)

    def test_extended_preview_can_show_nonfirst_header(self):
        (self.business / 'sales.csv').write_text('Report,\nitem,amount\nA,12\n')
        preview = source_preview(self.business, self.root / 'calendar.csv', 30, preview_rows=4)
        self.assertIn('first_rows_only', preview)
        self.assertIn('amount', preview)

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

    def test_table_cache_returns_independent_copies(self):
        self.worker("a=read_table('F1')\na.iloc[0, 0] = 'changed'\nb=read_table('F1')\nprint(b.iloc[0, 0])")
        output = self.worker("print(b.iloc[0, 0] != 'changed')")
        self.assertIn('True', output.logs)

    def test_order_count_warns_about_cancelled_status_in_worker(self):
        code = ("frame=pd.DataFrame({'Mã đơn hàng':['a','b'], "
                "'Trạng thái đơn hàng':['Đã hoàn thành','Đã hủy']})\n"
                "print(count_entities(frame, 'Mã đơn hàng'))")
        with self.assertRaisesRegex(ValueError, 'Order statuses include'):
            self.worker(code)

    def test_nunique_on_order_id_checks_selected_statuses(self):
        code = ("orders=pd.DataFrame({'Mã đơn hàng':['a','b'], "
                "'Trạng thái đơn hàng':['Đã hoàn thành','Đã hủy']})\n"
                "today_orders=orders.copy()\n"
                "print(today_orders['Mã đơn hàng'].nunique())")
        with self.assertRaisesRegex(ValueError, 'Order statuses include'):
            self.worker(code)
        good = ("orders=pd.DataFrame({'Mã đơn hàng':['a','b'], "
                "'Trạng thái đơn hàng':['Đã hoàn thành','Đã hủy']})\n"
                "selected=orders[orders['Trạng thái đơn hàng']=='Đã hoàn thành']\n"
                "print(selected['Mã đơn hàng'].nunique())")
        self.assertIn('1', self.worker(good).logs)

    def test_order_count_helper_in_worker_respects_eligibility(self):
        code = ("frame=pd.DataFrame({'order':['a','a','b'], 'date':['18/10/2025']*3, "
                "'status':['Đã hoàn thành','Đã hoàn thành','Đã hủy']})\n"
                "print(count_orders(frame, 'order', 'date', 'status', '2025-10-18', '2025-10-18', "
                "'%d/%m/%Y', True))")
        self.assertIn('distinct included=1', self.worker(code).logs)


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
        model = ScriptedModel(["df=read_table('F1')\nprint(len(df))", "final_answer({'type':'number','answer':len(df)})"])
        with patch('tabular_agent.cli.create_model', return_value=model):
            self.assertEqual(run(args), 0)
        text = (output / 'logs' / '000000.jsonl').read_text()
        steps = [json.loads(line) for line in text.splitlines() if json.loads(line)['stage'] == 'memory_step']
        self.assertEqual([step['step'] for step in steps], [1, 2])
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
