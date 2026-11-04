"""Human-readable local reports; reference answers are used only after inference."""
import argparse
from html import escape
import json
from pathlib import Path

from .contracts import read_jsonl

STYLE = """body{font:16px/1.5 system-ui;max-width:1000px;margin:32px auto;padding:0 20px}
details,section{border:1px solid #ccd2d9;border-radius:8px;margin:16px 0;padding:16px}
summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f5f7;padding:12px}
small{color:#555}a{color:#1455a0}table{border-collapse:collapse;width:100%}
th,td{text-align:left;vertical-align:top;padding:10px;border-bottom:1px solid #ddd}
.bad{border-left:5px solid #ad2525}.good{border-left:5px solid #287441}.neutral{border-left:5px solid #627080}
"""

STAGE_NAMES = {
    "worker_start": "Started isolated worker", "context": "Loaded file descriptions",
    "code": "Executed Python", "observation": "Worker result", "worker_reset": "Discarded failed computation",
    "validation": "Checked final answer", "model_step": "Model step", "stop": "Stopped", "error": "Application error",
}


def page(title, content):
    return (f'<!doctype html><html lang="en"><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{escape(title)}</title><style>{STYLE}</style>'
            f'<h1>{escape(title)}</h1>{content}</html>')


def answer_text(answer):
    if answer is None:
        return "No reference value is attached to this report."
    kind = answer["type"]
    if kind == "unanswerable":
        return "unanswerable — the available data cannot support an answer"
    if kind == "abstain":
        return "abstain — " + answer.get("reason", "the agent chose not to answer")
    return f'{json.dumps(answer.get("answer"), ensure_ascii=False)} ({kind})'


def review_summary(answer, trace, expected=None, outcome=None):
    """Describe observed facts; do not invent a root cause from an error label."""
    if outcome == "correct":
        status, tone = "Matches the reference", "good"
        decision = "Inspect the sources and calculation before trusting this approach on other questions. One match does not prove correct reasoning."
    elif outcome == "wrong":
        status, tone = "Wrong answer", "bad"
        decision = "Fix the data selection and calculation before accepting this result."
        if expected and expected["type"] == "unanswerable":
            decision = "Check which inputs the metric requires. Use unanswerable when a complete source inspection establishes that required information is missing."
    elif answer["type"] == "abstain":
        status, tone = "The agent abstained", "neutral"
        decision = "Read the stop reason, then address the specific parsing, validation, or budget problem before retrying."
    else:
        status, tone = "Correctness not evaluated", "neutral"
        decision = "Review the input selection and calculation. Successful execution and valid formatting do not establish a correct answer."
    events = trace.get("events", [])
    resets = sum(event["stage"] == "worker_reset" for event in events)
    rejections = sum(event["stage"] == "validation" and not event.get("accepted") for event in events)
    facts = []
    if resets:
        facts.append(f"The worker restarted {resets} time(s) after execution errors; computed variables were discarded.")
    if rejections:
        facts.append(f"The answer checks rejected {rejections} candidate(s) before the final result.")
    latest_reads = next((event.get("reads", []) for event in reversed(events)
                         if event["stage"] == "observation" and event.get("status") == "ok"), [])
    sources = sorted({str(read["source"]) + (" / " + str(read["sheet"]) if read.get("sheet") else "")
                      for read in latest_reads})
    if sources:
        facts.append("Full tables read in the final worker session: " + "; ".join(sources) + ".")
    context = next((event for event in events if event["stage"] == "context"), {})
    for item in context.get("manifest", {}).get("files", []):
        if item.get("sheets"):
            opened = {read.get("sheet") for read in latest_reads if read["source"] == item["alias"]}
            if len(opened) < len(item["sheets"]):
                facts.append(f'{item["alias"]}: read {len(opened)} of {len(item["sheets"])} listed sheets. Check whether unread sheets overlap the requested period.')
    return {"status": status, "tone": tone, "decision": decision, "facts": facts}


def write_question_trace(path: Path, question_id: str, answer: dict, trace: dict,
                         question=None, expected=None, outcome=None):
    review = review_summary(answer, trace, expected, outcome)
    question = question or {}
    content = '<p><a href="../trace.html">All questions</a></p>'
    if question.get("question"):
        content += f'<p>Reference date: {escape(question["as_of_date"])}</p>'
    content += (f'<section class="{review["tone"]}"><h2>{escape(review["status"])}</h2>'
                f'<table><tr><th>Expected</th><td>{escape(answer_text(expected))}</td></tr>'
                f'<tr><th>Agent returned</th><td>{escape(answer_text(answer))}</td></tr></table>'
                f'<h3>What to do next</h3><p>{escape(review["decision"])}</p></section>')
    if review["facts"]:
        content += '<h2>What the trace shows</h2><ul>' + ''.join(f'<li>{escape(fact)}</li>' for fact in review["facts"]) + '</ul>'
    content += '<h2>Inspect the details</h2><p>Expand an action to see the code, result, or rejection reason. Observed errors are clues, not proof of the root cause.</p>'
    for index, event in enumerate(trace.get("events", []), 1):
        title = f'{index}. {STAGE_NAMES.get(event["stage"], event["stage"])} — {event["elapsed_seconds"]:.3f}s'
        content += (f'<details><summary>{escape(title)}</summary>'
                    f'<pre>{escape(json.dumps(event, ensure_ascii=False, indent=2))}</pre></details>')
    path.write_text(page(question.get("question") or question_id, content), encoding="utf-8")


def write_trace_index(output: Path, predictions: list[dict], questions=None, outcomes=None):
    questions, outcomes = questions or {}, outcomes or {}
    links = []
    for index, row in enumerate(predictions):
        question = questions.get(row["id"], {}).get("question", row["id"])
        outcome = outcomes.get(row["id"], "not scored")
        links.append(f'<li><a href="logs/{index:06d}.html">{escape(question)}</a> — {escape(outcome)}</li>')
    content = '<p>Choose a question to compare the expected answer, the agent\'s result, observed problems, and the recommended next step.</p><ol>' + ''.join(links) + '</ol>'
    (output / "trace.html").write_text(page("Agent run review", content), encoding="utf-8")


def render_run(output: Path, references=None):
    """Refresh HTML without changing saved predictions, logs, or scores."""
    questions = {item["id"]: item for item in read_jsonl(output / "questions.jsonl")}
    predictions = read_jsonl(output / "submission.jsonl")
    expected = {item["id"]: item for item in read_jsonl(references)} if references else {}
    score_path = output / "score.json"
    outcomes = {}
    if score_path.exists():
        outcomes = {item["id"]: item["outcome"] for item in json.loads(score_path.read_text())["questions"]}
    for index, answer in enumerate(predictions):
        trace = json.loads((output / "logs" / f"{index:06d}.json").read_text())
        write_question_trace(output / "logs" / f"{index:06d}.html", answer["id"], answer, trace,
                             questions.get(answer["id"]), expected.get(answer["id"]), outcomes.get(answer["id"]))
    write_trace_index(output, predictions, questions, outcomes)


def main():
    parser = argparse.ArgumentParser(description="Render a saved run as readable HTML; no inference is performed")
    parser.add_argument("run", type=Path)
    parser.add_argument("--references", type=Path, help="Optional training/synthetic references, used only for this post-inference report")
    args = parser.parse_args()
    render_run(args.run, args.references)
    print(args.run / "trace.html")


if __name__ == "__main__":
    main()
