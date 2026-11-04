import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import unittest
import uuid

from tabular_agent.data import BusinessContext
from tabular_agent.demo import create_demo_split
from tabular_agent.executor import IsolatedExecutor
from tabular_agent.agent import NativeAgent
from tabular_agent.runner import parser, run
from tabular_agent.runtime import Deadline, QuestionStopped
from native_support import ScriptedModel, calculation, response


@unittest.skipUnless(os.environ.get("RUN_SANDBOX_TESTS") == "1", "opt-in real Linux isolation checks")
class WorkerIsolation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.business = self.root / "business"
        self.business.mkdir()
        (self.business / "sales.csv").write_text("item,amount\nA,100\n", encoding="utf-8-sig")
        (self.root / "calendar.csv").write_text("date\n")
        (self.root / "answers.jsonl").write_text("synthetic-reference-secret")
        self.worker = self.new_worker()

    def new_worker(self, seconds=15):
        return IsolatedExecutor(BusinessContext.load(self.business, self.root / "calendar.csv"),
                                "2026-03-04", Deadline.after(seconds), action_seconds=5)

    def tearDown(self):
        self.worker.cleanup()
        self.temp.cleanup()

    def test_dataframe_persists_after_success_and_bom_header_is_clean(self):
        self.worker("df = read_table('F1', header_row=0)\nprint(list(df.columns))")
        result = self.worker("print(int(df.amount.astype(int).sum()))")
        self.assertIn("100", result.logs)
        self.assertEqual(self.worker.generation, 1)

    def test_mutation_exception_discards_state_and_prevents_double_conversion(self):
        self.worker("df = read_table('F1', header_row=0)\ndf.amount = df.amount.astype(int)")
        with self.assertRaisesRegex(ValueError, "Worker reset"):
            self.worker("df.amount *= 2\nraise ValueError('after mutation')")
        result = self.worker("assert 'df' not in globals()\ndf = read_table('F1', header_row=0)\nprint(int(df.amount.astype(int).sum()) * 2)")
        self.assertIn("200", result.logs)
        self.assertNotIn("400", result.logs)
        self.assertEqual(self.worker.generation, 2)

    def test_candidate_from_old_generation_is_rejected(self):
        candidate = {"type": "number", "answer": 100, "calculation": calculation(unit="VND")}
        result = self.worker("df = read_table('F1', header_row=0)\nfinal_answer(" + repr(candidate) + ")")
        self.assertEqual(self.worker.validate_final(result.output)["answer"], 100)
        with self.assertRaises(ValueError):
            self.worker("raise ValueError('reset')")
        with self.assertRaisesRegex(ValueError, "current worker"):
            self.worker.validate_final(candidate)
        fresh = self.worker("final_answer(" + repr(candidate) + ")")
        with self.assertRaisesRegex(ValueError, "Read every declared source"):
            self.worker.validate_final(fresh.output)

    def test_next_question_has_no_previous_variables(self):
        self.worker("private_value = 123")
        self.worker.cleanup()
        other = self.new_worker()
        try:
            self.assertIn("False", other("print('private_value' in globals())").logs)
        finally:
            other.cleanup()

    def test_inputs_are_read_only_and_references_are_not_mounted(self):
        before = (self.business / "sales.csv").read_bytes()
        with self.assertRaisesRegex(ValueError, "Read-only"):
            self.worker("from pathlib import Path\nPath('/data/sales.csv').write_text('changed')")
        result = self.worker("from pathlib import Path\nprint(Path('/answers.jsonl').exists())\nprint(Path('/data/../answers.jsonl').exists())\nprint(Path('/home/nhatnm/.env').exists())")
        self.assertEqual(result.logs.count("False"), 3)
        self.assertEqual((self.business / "sales.csv").read_bytes(), before)

    def test_host_network_is_unreachable(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            result = self.worker(
                "import socket\ns = socket.socket()\ns.settimeout(0.5)\n"
                f"try:\n    s.connect(('127.0.0.1', {port}))\n"
                "except OSError:\n    print('blocked')\nelse:\n    raise AssertionError('host network exposed')\nfinally:\n    s.close()"
            )
        self.assertIn("blocked", result.logs)

    def test_timeout_kills_worker_and_its_child(self):
        marker = "worker-child-" + uuid.uuid4().hex
        self.worker("import subprocess, sys\nchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', " + repr(marker) + "])")
        processes = subprocess.check_output(["ps", "-eo", "args"], text=True)
        self.assertIn(marker, processes)
        self.worker.action_seconds = 0.1
        with self.assertRaisesRegex(QuestionStopped, "timed out"):
            self.worker("while True: pass")
        self.assertIsNone(self.worker.process)
        self.assertNotIn(marker, subprocess.check_output(["ps", "-eo", "args"], text=True))

    def test_invalid_protocol_ends_question_and_cleans_up(self):
        with self.assertRaisesRegex(QuestionStopped, "Invalid worker protocol"):
            self.worker("import os\nos.write(3, b'not-json\\n')")
        self.assertIsNone(self.worker.process)

    def test_file_size_and_process_limits_are_active(self):
        result = self.worker("import resource\nprint(resource.getrlimit(resource.RLIMIT_FSIZE))\nprint(resource.getrlimit(resource.RLIMIT_NOFILE))")
        self.assertIn("1048576", result.logs)
        self.assertIn("64", result.logs)
        with self.assertRaises(ValueError):
            self.worker("with open('/tmp/oversized', 'wb') as output:\n    output.write(b'x' * (2 * 1024**2))")

    def test_sqlite_reader_is_read_only(self):
        with sqlite3.connect(self.business / "stock.sqlite") as database:
            database.execute("CREATE TABLE stock(product TEXT, quantity INTEGER)")
            database.execute("INSERT INTO stock VALUES ('A', 7)")
        self.worker.cleanup()
        self.worker = self.new_worker()
        result = self.worker("stock = read_table('stock.sqlite', table='stock')\nprint(int(stock.quantity.sum()))")
        self.assertIn("7", result.logs)

    def test_explicit_numeric_read_preserves_identifiers_and_prevents_repetition(self):
        (self.business / "factors.csv").write_text('code;factor;price\n0012;12;1.234,50\n')
        self.worker.cleanup()
        self.worker = self.new_worker()
        result = self.worker("df = read_table('factors.csv', header_row=0, numeric_columns=['factor','price'], decimal_separator=',', thousands_separator='.')\nassert df.code.iloc[0] == '0012'\nassert df.factor.iloc[0] * 3 == 36\nprint(df.price.iloc[0])")
        self.assertIn("1234.50", result.logs)
        with self.assertRaisesRegex(ValueError, "explicit decimal_separator"):
            self.worker("read_table('factors.csv', header_row=0, numeric_columns=['factor'])")


COMPUTE_QUANTITY = """import pandas as pd
sales = pd.concat([read_table('F2', sheet=sheet, header_row=0) for sheet in ['2026-02', '2026-03']], ignore_index=True)
sales['date'] = parse_dates(sales['date'], dayfirst=False)
selected = sales[sales['date'].between('2026-02-23', '2026-03-01') & (sales['status'] == 'completed')].copy()
conversions = read_table('F1', header_row=0)
factors = conversions.set_index('product')['factor'].astype(int)
selected['bottles'] = selected['quantity'].astype(int) * selected['product'].map(factors).where(selected['unit'] == 'box', 1)
totals = selected.groupby('product')['bottles'].sum().sort_values(ascending=False)
print(totals.to_dict())
"""


@unittest.skipUnless(os.environ.get("RUN_SANDBOX_TESTS") == "1", "opt-in real Linux isolation checks")
class NativeEndToEnd(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.split = self.root / "inputs"
        self.business = create_demo_split(self.split)
        self.record = calculation(
            sources=["F1", "F2"], metric="best selling by base quantity", row_count=2,
            required_columns={"F1": ["factor"], "F2": ["quantity", "date", "status"]},
            period={"start": "2026-02-23", "end": "2026-03-01"},
            date_min="2026-02-24", date_max="2026-03-01",
            checks={"returned_excluded": True, "cancelled_excluded": True, "sheets_read": 2},
        )

    def tearDown(self):
        self.temp.cleanup()

    def solve(self, responses, **options):
        model = ScriptedModel(responses)
        question = json.loads((self.split / "questions.jsonl").read_text().splitlines()[0])
        result = NativeAgent(model, question_seconds=20, **options).solve(
            question, self.business, self.split / "calendar.csv", (self.split / "rulebook.md").read_text())
        return *result, model

    def test_cross_month_units_statuses_and_typed_answer(self):
        final = "final_answer({'type':'string','answer':str(totals.index[0]),'calculation':" + repr(self.record) + "})"
        answer, trace, model = self.solve([response(COMPUTE_QUANTITY), response(final)])
        self.assertEqual(answer, {"type": "string", "answer": "Trà đóng chai"})
        self.assertEqual(trace["python_executions"], 2)
        self.assertNotIn("example_top", model.messages[0])
        self.assertNotIn("answers.jsonl", model.messages[0])
        self.assertIn("27", str(trace["events"]))

    def test_invalid_final_can_be_repaired_without_losing_successful_state(self):
        invalid = COMPUTE_QUANTITY + "\nfinal_answer({'type':'number','answer':True,'calculation':" + repr(self.record) + "})"
        repaired = "final_answer({'type':'number','answer':int(totals.iloc[0]),'calculation':" + repr(self.record) + "})"
        answer, trace, _ = self.solve([response(invalid), response(repaired)])
        self.assertEqual(answer, {"type": "number", "answer": 27})
        self.assertEqual(sum(event["stage"] == "worker_start" for event in trace["events"]), 1)
        self.assertTrue(any(event["stage"] == "validation" and not event["accepted"] for event in trace["events"]))

    def test_truncation_preserves_worker_state_and_dispatches_nothing(self):
        final = "final_answer({'type':'number','answer':int(totals.iloc[0]),'calculation':" + repr(self.record) + "})"
        answer, trace, model = self.solve([
            response(COMPUTE_QUANTITY), response("totals *= 100", "length"), response(final)])
        self.assertEqual(answer["answer"], 27)
        self.assertEqual(trace["python_executions"], 2)
        self.assertEqual(model.calls, 3)

    def test_native_loop_reloads_after_mutation_error(self):
        failed = "df = read_table('F1', header_row=0)\ndf.factor = df.factor.astype(int) * 2\nraise ValueError('partial mutation')"
        repaired = ("assert 'df' not in globals()\n" + COMPUTE_QUANTITY
                    + "\nfinal_answer({'type':'number','answer':int(totals.iloc[0]),'calculation':" + repr(self.record) + "})")
        answer, trace, model = self.solve([response(failed), response(repaired)])
        self.assertEqual(answer, {"type": "number", "answer": 27})
        self.assertEqual(model.calls, 2)
        self.assertTrue(any(event["stage"] == "worker_reset" for event in trace["events"]))
        self.assertEqual([event["generation"] for event in trace["events"] if event["stage"] == "code"], [1, 2])

    def test_workbook_helper_reads_both_months_and_parses_conversion_factors(self):
        code = """import pandas as pd
sales = read_workbook('F2', header_row=0, numeric_columns=['quantity'], decimal_separator='.', thousands_separator='')
assert set(sales['__sheet']) == {'2026-02','2026-03'}
conversions = read_table('F1', header_row=0, numeric_columns=['factor'], decimal_separator='.', thousands_separator='')
selected = sales[sales.status == 'completed'].copy()
factors = conversions.set_index('product').factor
selected['bottles'] = selected.quantity * selected['product'].map(factors).where(selected.unit == 'box', 1)
"""
        final = "final_answer({'type':'number','answer':int(selected.bottles.sum()),'calculation':" + repr(self.record) + "})"
        answer, _, _ = self.solve([response(code + final)])
        self.assertEqual(answer, {"type": "number", "answer": 27})

    def test_missing_cost_columns_reject_profit_and_allow_absence_diagnostic(self):
        record = calculation(sources=["F2"], metric="profit", unit="VND",
                             required_columns={"F2": ["amount", "cost"]})
        invalid = "sales = read_workbook('F2', header_row=0)\nfinal_answer({'type':'number','answer':18,'calculation':" + repr(record) + "})"
        record.update(search_complete=True, missing="No sales amount or cost fields are recorded")
        fixed = "final_answer({'type':'unanswerable','reason':'Missing monetary inputs','calculation':" + repr(record) + "})"
        answer, trace, _ = self.solve([response(invalid), response(fixed)])
        self.assertEqual(answer["type"], "unanswerable")
        self.assertTrue(any(event["stage"] == "validation" and not event["accepted"] for event in trace["events"]))

    def test_incremental_writes_survive_interruption_and_trace_pages_exist(self):
        output = self.root / "output"
        model = ScriptedModel([response("final_answer({'type':'abstain','reason':'fixture'})")])

        class InterruptSecondQuestion(NativeAgent):
            count = 0

            def solve(self, *args, **kwargs):
                self.count += 1
                if self.count == 2:
                    raise KeyboardInterrupt()
                return super().solve(*args, **kwargs)

        args = parser().parse_args(["predict", "--split", str(self.split), "--output", str(output)])
        code = run(args, model_factory=lambda args: model, agent_factory=InterruptSecondQuestion)
        self.assertEqual(code, 130)
        self.assertEqual(len((output / "submission.jsonl").read_text().splitlines()), 1)
        self.assertTrue((output / "logs/000000.html").exists())
        self.assertTrue((output / "trace.html").exists())
        self.assertEqual(json.loads((output / "report.json").read_text())["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
