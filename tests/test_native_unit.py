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
from tabular_agent.trace import review_summary, write_question_trace
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
        self.reads = [{"source": "F1", "full": True, "rows": 1, "columns": ["amount"]}]

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
        candidate["calculation"]["date_max"] = "2026-03-01"
        candidate["calculation"]["row_count"] = True
        with self.assertRaises(ValueError):
            validate_candidate(candidate, self.reads)

    def test_missing_prerequisites_cannot_support_a_substantive_answer(self):
        candidate = {"type": "number", "answer": 18, "calculation": calculation(
            metric="profit", required_columns={"F1": ["amount", "cost"]})}
        with self.assertRaisesRegex(ValueError, "Required columns absent"):
            validate_candidate(candidate, self.reads)
        candidate["calculation"]["required_columns"] = {"F1": ["amount"]}
        candidate["calculation"]["missing"] = "No cost information"
        with self.assertRaisesRegex(ValueError, "Required information is missing"):
            validate_candidate(candidate, self.reads)

    def test_every_declared_source_needs_an_input_check(self):
        candidate = {"type": "number", "answer": 18, "calculation": calculation(required_columns={})}
        with self.assertRaisesRegex(ValueError, "Declare required_columns"):
            validate_candidate(candidate, self.reads)

    def test_zero_requires_worker_diagnostics_when_source_has_rows(self):
        candidate = {"type": "number", "answer": 0, "calculation": calculation()}
        with self.assertRaisesRegex(ValueError, "profile_columns and audit_filter"):
            validate_candidate(candidate, self.reads)
        diagnostics = [{"kind": "profile"}, {"kind": "filter", "input_rows": 1, "output_rows": 0}]
        self.assertEqual(validate_candidate(candidate, self.reads, diagnostics)["answer"], 0)
        self.assertEqual(validate_candidate(candidate, [{**self.reads[0], "rows": 0}])["answer"], 0)
        candidate["calculation"]["period"] = {"start": "2025-10-18", "end": "2025-10-18"}
        candidate["calculation"]["row_count"] = 0
        with self.assertRaisesRegex(ValueError, "audit_date_filter"):
            validate_candidate(candidate, self.reads, diagnostics)
        diagnostics.append({"kind": "date_filter", "start": "2025-10-18", "end": "2025-10-18"})
        self.assertEqual(validate_candidate(candidate, self.reads, diagnostics)["answer"], 0)

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

    def test_report_has_actionable_expected_vs_actual_summary(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "trace.html"
            write_question_trace(path, "example", {"type": "number", "answer": 18}, {"events": []},
                                 question={"question": "Lãi gộp?", "as_of_date": "2026-03-04"},
                                 expected={"type": "unanswerable"}, outcome="wrong")
            text = path.read_text()
            self.assertIn("Wrong answer", text)
            self.assertIn("Lãi gộp?", text)
            self.assertIn("Expected", text)
            self.assertIn("18 (number)", text)
            self.assertIn("What to do next", text)
            self.assertIn("cannot support an answer", text)

    def test_unscored_report_does_not_claim_correctness(self):
        review = review_summary({"type": "number", "answer": 27}, {"events": []})
        self.assertEqual(review["status"], "Correctness not evaluated")


class ContextCounting(unittest.TestCase):
    def test_server_token_count_allows_repairs_that_byte_count_would_reject(self):
        model = LocalCodeModel("fixture", "http://127.0.0.1:8000/v1", context=2048, max_tokens=100)
        events = []
        model.emit = events.append
        replies = [
            httpx.Response(200, json={"prompt": "formatted chat"}, request=httpx.Request("POST", "http://localhost/apply-template")),
            httpx.Response(200, json={"tokens": list(range(12))}, request=httpx.Request("POST", "http://localhost/tokenize")),
        ]
        try:
            with patch.object(model.http_client, "post", side_effect=replies), patch.object(OpenAIModel, "generate", return_value=response("pass")) as generate:
                model.generate([{"role": "user", "content": "long Vietnamese context " * 500}])
            generate.assert_called_once()
            self.assertEqual(model.context_counting, "server chat template and tokenizer")
            self.assertEqual([e["stage"] for e in events], ["model_request", "model_response"])
            self.assertEqual(events[0]["input_tokens"], 12)
            self.assertEqual(events[0]["prompt_format"], "full-messages-v1")
            self.assertIn("long Vietnamese context " * 500, str(events[0]["messages"]))
            self.assertNotIn("api_key", str(events))
        finally:
            model.client.close()

    def test_full_request_record_keeps_all_messages_and_their_endings(self):
        model = LocalCodeModel("fixture", "http://127.0.0.1:8000/v1", context=32768, max_tokens=100)
        messages = [{"role": "user" if i % 2 == 0 else "assistant",
                     "content": f"Message {i} " + "detail " * 100 + f"END-{i}"} for i in range(12)]
        events = []
        model.emit = events.append
        try:
            with patch.object(model, "count_input_tokens", return_value=1000), patch.object(
                    OpenAIModel, "generate", return_value=response("pass")):
                model.generate(messages)
            recorded = events[0]["messages"]
            self.assertEqual(len(recorded), 12)
            for i in range(12):
                self.assertIn(f"END-{i}", str(recorded[i]))
            messages[0]["content"] = "changed afterwards"
            self.assertIn("END-0", str(recorded[0]))
        finally:
            model.client.close()

    def test_token_overflow_stops_before_generation(self):
        model = LocalCodeModel("fixture", "http://127.0.0.1:8000/v1", context=2048, max_tokens=100)
        events = []
        model.emit = events.append
        try:
            with patch.object(model, "count_input_tokens", return_value=2000), patch.object(OpenAIModel, "generate") as generate:
                with self.assertRaises(QuestionStopped):
                    model.generate([{"role": "user", "content": "question"}])
            generate.assert_not_called()
            self.assertEqual(events, [])
        finally:
            model.client.close()


if __name__ == "__main__":
    unittest.main()
