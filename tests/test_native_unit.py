import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx
from smolagents import OpenAIModel

from smolagents.local_python_executor import CodeOutput

from tabular_agent.contracts import validate_answer
from tabular_agent.evidence import validate_candidate
from tabular_agent.model import LocalCodeModel, local_endpoint
from tabular_agent.agent import NativeAgent
from tabular_agent.runtime import Deadline, QuestionStopped, enforce_deadline
from tabular_agent.trace import write_question_trace
from native_support import ScriptedModel, calculation, response


class RecordingExecutor:
    instances = []

    def __init__(self, *args, **kwargs):
        self.dispatches = 0
        self.closed = False
        self.instances.append(self)

    def manifest(self):
        return {"files": []}

    def send_variables(self, variables):
        pass

    def send_tools(self, tools):
        pass

    def __call__(self, code):
        self.dispatches += 1
        return CodeOutput({"type": "abstain", "reason": "scripted"}, "", True)

    def validate_final(self, answer):
        return validate_answer(answer)

    def cleanup(self):
        self.closed = True


class NativeLoopGuards(unittest.TestCase):
    def solve(self, responses, max_steps=3):
        model = ScriptedModel(responses)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            calendar = root / "calendar.csv"
            calendar.write_text("date\n")
            agent = NativeAgent(model, max_steps=max_steps, executor_factory=RecordingExecutor)
            answer, trace = agent.solve({"question": "A question", "as_of_date": "2026-03-04"}, root, calendar, "rules")
        return answer, trace, model, RecordingExecutor.instances[-1]

    def test_truncated_final_never_dispatches_and_complete_retry_runs(self):
        answer, trace, model, executor = self.solve([
            response("final_answer({'type':'number','answer':999})", "length"),
            response("final_answer({'type':'abstain'})"),
        ])
        self.assertEqual(answer["type"], "abstain")
        self.assertEqual(executor.dispatches, 1)
        self.assertEqual(model.calls, 2)
        self.assertIn("no code ran", str(trace["events"]))
        self.assertTrue(executor.closed)

    def test_repeated_truncation_stops_without_fallback_generation(self):
        answer, _, model, executor = self.solve([response("x = 2", "length")] * 2, max_steps=2)
        self.assertEqual(executor.dispatches, 0)
        self.assertEqual(model.calls, 2)
        self.assertIn("Step budget", answer["reason"])

    def test_unknown_completion_status_is_rejected(self):
        _, _, _, executor = self.solve([response("x = 2", None)], max_steps=1)
        self.assertEqual(executor.dispatches, 0)

    def test_deadline_interrupts_active_work_and_restores_handler(self):
        started = time.monotonic()
        with self.assertRaises(QuestionStopped):
            with enforce_deadline(Deadline.after(0.05)):
                time.sleep(1)
        self.assertLess(time.monotonic() - started, 0.5)


class EvidenceContracts(unittest.TestCase):
    def setUp(self):
        self.reads = [{"source": "F1", "full": True, "rows": 1}]

    def test_strict_answer_types_and_source_evidence(self):
        for kind, value in [("number", 27), ("percent", -12.4), ("string", "Trà"),
                            ("boolean", True), ("date", "2026-03"), ("list", ["A", 2]),
                            ("table", {"columns": ["x"], "rows": [[1]]})]:
            candidate = {"type": kind, "answer": value, "calculation": calculation()}
            self.assertEqual(validate_candidate(candidate, self.reads)["answer"], value)
        for value in (True, "27", float("nan")):
            with self.assertRaises(ValueError):
                validate_candidate({"type": "number", "answer": value, "calculation": calculation()}, self.reads)
        with self.assertRaises(ValueError):
            validate_candidate({"type": "number", "answer": 27, "calculation": calculation()}, [])

    def test_unanswerable_requires_fresh_complete_absence_diagnostic(self):
        candidate = {"type": "unanswerable", "calculation": calculation()}
        with self.assertRaises(ValueError):
            validate_candidate(candidate, self.reads)
        candidate["calculation"].update(search_complete=True, missing="No costs column")
        self.assertEqual(validate_candidate(candidate, self.reads)["type"], "unanswerable")

    def test_absence_diagnostic_may_report_coverage_outside_requested_period(self):
        candidate = {"type": "unanswerable", "calculation": calculation(
            period={"start": "2020-01-01", "end": "2020-01-31"},
            date_min="2026-02-24", date_max="2026-03-01",
            search_complete=True, missing="The requested period is wholly outside source coverage")}
        self.assertEqual(validate_candidate(candidate, self.reads)["type"], "unanswerable")

    def test_period_and_selected_coverage_are_checked(self):
        candidate = {"type": "number", "answer": 27, "calculation": calculation(
            period={"start": "2026-02-23", "end": "2026-03-01"},
            date_min="2026-02-24", date_max="2026-03-01")}
        self.assertEqual(validate_candidate(candidate, self.reads)["answer"], 27)
        candidate["calculation"]["date_max"] = "2026-03-04"
        with self.assertRaises(ValueError):
            validate_candidate(candidate, self.reads)
        candidate["calculation"]["row_count"] = True
        with self.assertRaises(ValueError):
            validate_candidate(candidate, self.reads)

    def test_loopback_endpoints_only(self):
        self.assertEqual(local_endpoint("http://127.0.0.1:8000/v1/"), "http://127.0.0.1:8000/v1")
        for url in ("https://example.com/v1", "http://127.0.0.1.evil/v1", "http://key@localhost/v1", "http://localhost/v1?key=secret"):
            with self.assertRaises(ValueError):
                local_endpoint(url)

    def test_trace_pages_escape_generated_content(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "trace.html"
            write_question_trace(path, "example", {"type": "abstain"}, {
                "events": [{"stage": "code", "elapsed_seconds": 0.1, "code": "<script>alert(1)</script>"}]})
            text = path.read_text()
            self.assertIn("<details>", text)
            self.assertIn("&lt;script&gt;", text)
            self.assertNotIn("<script>", text)


class ContextCounting(unittest.TestCase):
    def test_server_token_count_allows_repairs_that_byte_count_would_reject(self):
        model = LocalCodeModel("fixture", "http://127.0.0.1:8000/v1", context=2048, max_tokens=100)
        replies = [
            httpx.Response(200, json={"prompt": "formatted chat"}, request=httpx.Request("POST", "http://localhost/apply-template")),
            httpx.Response(200, json={"tokens": list(range(12))}, request=httpx.Request("POST", "http://localhost/tokenize")),
        ]
        try:
            with patch.object(model.http_client, "post", side_effect=replies), patch.object(OpenAIModel, "generate", return_value=response("pass")) as generate:
                model.generate([{"role": "user", "content": "long Vietnamese context " * 500}])
            generate.assert_called_once()
            self.assertEqual(model.context_counting, "server chat template and tokenizer")
        finally:
            model.client.close()

    def test_token_overflow_stops_before_generation(self):
        model = LocalCodeModel("fixture", "http://127.0.0.1:8000/v1", context=2048, max_tokens=100)
        try:
            with patch.object(model, "count_input_tokens", return_value=2000), patch.object(OpenAIModel, "generate") as generate:
                with self.assertRaises(QuestionStopped):
                    model.generate([{"role": "user", "content": "question"}])
            generate.assert_not_called()
        finally:
            model.client.close()


if __name__ == "__main__":
    unittest.main()
