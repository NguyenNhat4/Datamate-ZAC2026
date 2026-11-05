"""Synthetic UI/runner checks; no live model or benchmark quality claims."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tabular_agent.dashboard import ROOT, RUNS, RunJob, available_runs, read_live_rows, run_details, start_run
from tabular_agent.dashboard import model_summary, prompt_preview, execution_phase, run_budgets, DEFAULT_WEIGHT_DIGEST
from tabular_agent.demo import create_demo_split
from tabular_agent.runner import parser, run
from tabular_agent.splits import select_questions
from tabular_agent.trace import current_worker_observation, review_summary


class ExactSelection(unittest.TestCase):
    def test_ids_filter_cases_without_changing_question_content(self):
        questions = [{"id": str(i), "business_id": "shop", "question": "Example"} for i in range(4)]
        args = parser().parse_args(["bench", "--question-id", "3", "--question-id", "1"])
        selected = select_questions(questions, question_ids=args.question_id)
        self.assertEqual(selected, [questions[1], questions[3]])
        with self.assertRaisesRegex(ValueError, "Unknown question IDs"):
            select_questions(questions, question_ids=["missing"])


class Dashboard(unittest.TestCase):
    def setUp(self):
        RUNS.mkdir(exist_ok=True)
        self.source_temp = tempfile.TemporaryDirectory(prefix="demo-input-ui-test-", dir=RUNS)
        self.output_temp = tempfile.TemporaryDirectory(prefix="ui-test-", dir=RUNS)
        self.addCleanup(self.source_temp.cleanup)
        self.addCleanup(self.output_temp.cleanup)
        self.source = Path(self.source_temp.name)
        self.output = Path(self.output_temp.name)
        create_demo_split(self.source)
        (self.output / "logs").mkdir()
        self.questions = (self.source / "questions.jsonl").read_text()
        (self.output / "questions.jsonl").write_text(self.questions)
        self.write("config.json", {"arguments": {"split": str(self.source)}})

    def write(self, name, value):
        (self.output / name).write_text(json.dumps(value))

    def save_results(self):
        answers = [{"id": "example_top", "type": "string", "answer": "Trà đóng chai"},
                   {"id": "example_quantity", "type": "number", "answer": 121212},
                   {"id": "example_missing", "type": "abstain", "reason": "Question deadline exhausted"}]
        (self.output / "submission.jsonl").write_text("".join(json.dumps(a) + "\n" for a in answers))
        for i, answer in enumerate(answers):
            self.write(f"logs/{i:06d}.json", {"events": [{"stage": "code", "elapsed_seconds": 1,
                "code": "print('<script>alert(1)</script>')"}], "elapsed_seconds": 2, "final_answer": answer})
        self.write("score.json", {"questions": [{"id": a["id"], "outcome": outcome}
                   for a, outcome in zip(answers, ["correct", "wrong", "abstain"])]})
        self.write("report.json", {"status": "completed", "pa": 0})

    def test_saved_results_use_scorer_outcomes_and_show_expected_values(self):
        self.save_results()
        self.assertNotIn(self.source, available_runs())
        self.assertIn(self.output, available_runs())
        cases, report, _ = run_details(self.output)
        self.assertEqual([c["status"] for c in cases], ["PASS", "FAIL", "ABSTAIN"])
        self.assertEqual(cases[1]["expected"]["answer"], 27)
        self.assertEqual(cases[1]["review"]["status"], "Wrong answer")
        self.assertEqual(report["pa"], 0)

    def test_model_identity_uses_recorded_config_without_guessing_custom_weights(self):
        config = {"model": {"model": "served-alias", "context": 16384},
                  "arguments": {"weight_sha256": DEFAULT_WEIGHT_DIGEST}}
        self.assertIn("Qwen2.5-Coder-7B", model_summary(config)["name"])
        config["arguments"]["weight_sha256"] = "other"
        self.assertEqual(model_summary(config)["name"], "served-alias")
        self.assertEqual(model_summary(managed=False)["name"], "Discovered when the run starts")

    def test_prompt_preview_prefers_saved_snapshot_and_excludes_references(self):
        question = json.loads(self.questions.splitlines()[0])
        preview = prompt_preview(str(self.source), question)
        self.assertIn("sales.xlsx", str(preview["files"]))
        self.assertNotIn("answers.jsonl", str(preview["files"]))
        event = {"stage": "context", "prompt_context": {"question": "Recorded question",
                 "rulebook": "Recorded rules"}, "file_aliases": {"F1": "sales.xlsx"}}
        preview = prompt_preview(str(self.source), question, [event])
        self.assertEqual(preview["parts"]["rulebook"], "Recorded rules")
        self.assertIn("Recorded at inference", preview["source"])
        self.assertIn("older run saved excerpts only", preview["source"])

    def test_full_prompt_preserves_long_rules_and_recorded_budget_overrides(self):
        question = json.loads(self.questions.splitlines()[0])
        rules = "Business rule. " * 500 + "FINAL RULE"
        (self.source / "rulebook.md").write_text(rules)
        preview = prompt_preview(str(self.source), question)
        self.assertEqual(preview["parts"]["rulebook"], rules)
        event = {"stage": "context", "context_format": "full-v1",
                 "prompt_context": {"rulebook": rules}}
        self.assertEqual(prompt_preview(str(self.source), question, [event])["parts"]["rulebook"], rules)
        budgets = run_budgets({"arguments": {"max_steps": 3, "question_timeout": 45},
                               "model": {"context": 8192, "max_tokens": 1024}})
        self.assertEqual(budgets["Actions per question"], 3)
        self.assertEqual(budgets["Seconds per question"], 45)
        self.assertEqual(budgets["Context window (tokens)"], 8192)
        self.assertEqual(budgets["Output tokens per model call"], 1024)

    def test_live_flow_tracks_requests_code_checks_and_terminal_answer(self):
        case = {"trace": {"events": []}}
        for stage, phase in [("context", "model"), ("model_request", "model"),
                             ("code", "python"), ("validation", "checks")]:
            case["trace"]["events"] = [{"stage": stage}]
            self.assertEqual(execution_phase(case, running=True)[0], phase)
            self.assertTrue(execution_phase(case, running=True)[2])
        self.assertFalse(execution_phase(case, running=False)[2])
        case.update(answer={"type": "abstain"}, status="ABSTAIN")
        self.assertFalse(execution_phase(case, running=True)[2])
        self.assertIn("ABSTAIN", execution_phase(case, running=True)[1])

    def test_reset_discards_old_session_reads_and_diagnostics_in_summary(self):
        old = {"stage": "observation", "status": "ok", "reads": [{"source": "F1", "sheet": "old"}],
               "diagnostics": [{"kind": "filter", "label": "old check", "input_rows": 2, "output_rows": 1}]}
        events = [{"stage": "worker_start", "generation": 1}, old,
                  {"stage": "worker_reset", "generation": 1, "reason": "failed"}]
        self.assertIsNone(current_worker_observation(events))
        facts = review_summary({"type": "abstain"}, {"events": events})["facts"]
        self.assertTrue(any("must be recomputed" in fact for fact in facts))
        self.assertFalse(any("Full tables read" in fact for fact in facts))
        events.append({"stage": "worker_start", "generation": 2})
        self.assertIsNone(current_worker_observation(events))
        fresh = {"stage": "observation", "status": "ok", "reads": [{"source": "F2"}], "diagnostics": []}
        events.append(fresh)
        self.assertIs(current_worker_observation(events), fresh)
        facts = review_summary({"type": "abstain"}, {"events": events})["facts"]
        self.assertTrue(any("F2" in fact for fact in facts))
        self.assertFalse(any("F1" in fact for fact in facts))

    def test_streamlit_reset_does_not_display_stale_calculation_checks(self):
        from streamlit.testing.v1 import AppTest
        self.save_results()
        self.write("logs/000000.json", {"events": [
            {"stage": "worker_start", "elapsed_seconds": 0},
            {"stage": "observation", "status": "ok", "elapsed_seconds": 0.1,
             "reads": [{"source": "F1"}], "diagnostics": [{"kind": "filter", "label": "old check", "input_rows": 2, "output_rows": 1}]},
            {"stage": "observation", "status": "error", "elapsed_seconds": 0.2},
            {"stage": "worker_reset", "reason": "failed", "elapsed_seconds": 0.3}]})
        app = AppTest.from_file(str(ROOT / "streamlit_app.py"))
        app.session_state["demo_split"] = str(self.source)
        app.session_state["view_run"] = str(self.output)
        app.run(timeout=20)
        self.assertFalse(app.exception)
        self.assertFalse(any("Calculation checks" in item.label for item in app.expander))
        self.assertTrue(any("Reload sources and recompute" in item.value for item in app.warning))
        self.assertTrue(any("First recorded problem · step 3" in item.value for item in app.warning))

    def test_streamlit_full_context_messages_keep_the_end_of_long_text(self):
        from streamlit.testing.v1 import AppTest
        self.save_results()
        markdown = "# Business rules\n\n- Exclude cancelled orders.\n\n| Date | Format |\n| --- | --- |\n| Today | dd/mm/yyyy |"
        content = "New task:\n" + json.dumps({"question": "Full context details. " * 100 + "FULL MESSAGE END",
                                             "rulebook": markdown}, ensure_ascii=False)
        rules = "All business rules. " * 100 + "FINAL BUSINESS RULE"
        self.write("logs/000000.json", {"elapsed_seconds": 1, "events": [
            {"stage": "context", "context_format": "full-v1", "elapsed_seconds": 0,
             "prompt_context": {"rulebook": rules, "question": "Example", "as_of_date": "2026-03-04"}},
            {"stage": "model_request", "elapsed_seconds": 0.5, "prompt_format": "full-messages-v1",
             "input_tokens": 6728, "counting": "server chat template and tokenizer",
             "message_count": 1, "messages": [{"role": "user", "content": [
                 {"type": "text", "text": content}]}]},
            {"stage": "model_request", "elapsed_seconds": 0.8, "prompt_format": "full-messages-v1",
             "input_tokens": 7900, "counting": "server chat template and tokenizer",
             "message_count": 1, "messages": [{"role": "user", "content": content}]}]})
        app = AppTest.from_file(str(ROOT / "streamlit_app.py"))
        app.session_state["demo_split"] = str(self.source)
        app.session_state["view_run"] = str(self.output)
        app.run(timeout=20)
        self.assertFalse(app.exception)
        self.assertTrue(any(item.value == content for item in app.code))
        metric = next(item for item in app.metric if item.label == "First request input tokens")
        self.assertEqual(metric.value, "6,728")
        self.assertTrue(any("7,900 input tokens for this call" in item.value for item in app.caption))
        next(widget for widget in app.selectbox if widget.label == "Model call").select(0).run()
        self.assertFalse(app.exception)
        self.assertEqual(next(item for item in app.metric if item.label == "First request input tokens").value, "6,728")
        self.assertTrue(any(item.value == markdown for item in app.markdown))
        self.assertTrue(any(item.value == "Business rules" for item in app.subheader))
        self.assertTrue(any(item.label == "Preview" for item in app.tabs))
        self.assertTrue(any(item.label == "Raw text" for item in app.tabs))
        # Recorded messages are the sole prompt display; source ingredients
        # must not appear again as a second, confusing version of the prompt.
        self.assertFalse(any(item.value == rules for item in app.code))
        self.assertTrue(any(widget.label == "Model call" for widget in app.selectbox))
        self.assertFalse(any("Actual model requests" in item.label for item in app.expander))
        self.assertTrue(any("Budget per question: 8 actions" in item.value for item in app.caption))

    def test_partial_and_unscored_results_are_never_passes(self):
        path = self.output / "logs/000000.events.jsonl"
        path.write_text('{"stage":"code"}\n{"stage":')
        self.assertEqual(read_live_rows(path), [{"stage": "code"}])
        cases, _, _ = run_details(self.output)
        self.assertEqual([c["status"] for c in cases], ["RUNNING", "PENDING", "PENDING"])
        (self.output / "submission.jsonl").write_text('{"id":"example_top","type":"string","answer":"x"}\n')
        cases, _, _ = run_details(self.output)
        self.assertEqual(cases[0]["status"], "UNSCORED")
        self.write("report.json", {"status": "interrupted"})
        self.assertEqual(run_details(self.output)[0][1]["status"], "NOT RUN")

    def test_launch_passes_only_selected_ids_and_no_reference_answers(self):
        with patch("tabular_agent.dashboard.subprocess.Popen") as launch:
            job = start_run(self.source, ["example_quantity"], managed=False)
        self.addCleanup(job.console.unlink, missing_ok=True)
        command = launch.call_args.args[0]
        self.assertEqual(command.count("--question-id"), 1)
        self.assertIn("example_quantity", command)
        self.assertNotIn("answers.jsonl", " ".join(command))
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        with self.assertRaisesRegex(ValueError, "known question"):
            start_run(self.source, ["does-not-exist"])
        with self.assertRaisesRegex(ValueError, "loopback"):
            start_run(self.source, ["example_top"], managed=False, base_url="https://example.com/v1")

    def test_cancel_stops_a_real_subprocess(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                   start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        job = RunJob(process, self.output, self.output / "console", time.monotonic())
        job.cancel()
        self.assertIsNotNone(process.wait(timeout=3))

    def test_runner_executes_only_picked_case_and_scores_afterwards(self):
        output = self.output / "selected"
        visited = []

        class FixtureModel:
            def check(self):
                return {"model": "synthetic-ui-fixture"}

        class FixtureAgent:
            def __init__(self, *args, **kwargs):
                pass

            def solve(self, question, *args, **kwargs):
                visited.append(question["id"])
                return {"type": "abstain", "reason": "fixture"}, {"events": [], "elapsed_seconds": 0}

        args = parser().parse_args(["bench", "--split", str(self.source), "--output", str(output),
                                    "--question-id", "example_quantity", "--limit", "0"])
        self.assertEqual(run(args, model_factory=lambda _: FixtureModel(), agent_factory=FixtureAgent), 0)
        self.assertEqual(visited, ["example_quantity"])
        cases, report, _ = run_details(output)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["status"], "ABSTAIN")
        self.assertEqual(report["total"], 1)

    def test_streamlit_picker_saved_failure_and_filter(self):
        from streamlit.testing.v1 import AppTest
        self.save_results()
        app = AppTest.from_file(str(ROOT / "streamlit_app.py"))
        app.session_state["demo_split"] = str(self.source)
        app.session_state["view_run"] = str(self.output)
        app.run(timeout=20)
        self.assertFalse(app.exception)
        self.assertTrue(any("Qwen2.5-Coder" in item.value for item in app.text))
        self.assertTrue(any("Current input preview" in item.value for item in app.caption))
        self.assertTrue(any("Agent flow" in item.value for item in app.markdown))
        self.assertEqual(len(app.multiselect[0].options), 3)
        inspect = next(widget for widget in app.selectbox if widget.label == "Inspect question")
        inspect.select("example_quantity").run()
        self.assertFalse(app.exception)
        self.assertTrue(any("121212" in item.value for item in app.code))
        self.assertTrue(any("27" in item.value for item in app.code))
        self.assertTrue(any(item.value == "Wrong answer" for item in app.error))
        next(widget for widget in app.radio if widget.label == "Show results").set_value("Passed").run()
        self.assertFalse(app.exception)
        inspect = next(widget for widget in app.selectbox if widget.label == "Inspect question")
        self.assertEqual(len(inspect.options), 1)
        self.assertEqual(inspect.options[0], "Tuần rồi mặt hàng nào bán chạy nhất?")
        next(widget for widget in app.text_input if widget.label == "Search question text or ID").set_value("example_missing").run()
        self.assertEqual(len(app.multiselect[0].options), 1)
        self.assertFalse(app.exception)


if __name__ == "__main__":
    unittest.main()
