"""Local, dependency-free trace pages with expandable code and observations."""
from html import escape
import json
from pathlib import Path

STYLE = """body{font:16px/1.5 system-ui;max-width:1000px;margin:32px auto;padding:0 20px}
details{border:1px solid #ccc;border-radius:6px;margin:10px 0;padding:10px}
summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f5f7;padding:12px}
small{color:#555}a{color:#1455a0}"""


def page(title, content):
    return (f'<!doctype html><html lang="en"><meta charset="utf-8">'
            f'<title>{escape(title)}</title><style>{STYLE}</style>'
            f'<h1>{escape(title)}</h1>{content}</html>')


def write_question_trace(path: Path, question_id: str, answer: dict, trace: dict):
    content = f'<p><a href="../trace.html">All questions</a></p><pre>{escape(json.dumps(answer, ensure_ascii=False, indent=2))}</pre>'
    for index, event in enumerate(trace["events"], 1):
        title = f'{index}. {event["stage"]} — {event["elapsed_seconds"]:.3f}s'
        content += (f'<details><summary>{escape(title)}</summary>'
                    f'<pre>{escape(json.dumps(event, ensure_ascii=False, indent=2))}</pre></details>')
    path.write_text(page(question_id, content), encoding="utf-8")


def write_trace_index(output: Path, predictions: list[dict]):
    links = [f'<li><a href="logs/{index:06d}.html">{escape(row["id"])}</a>: {escape(row["type"])}</li>'
             for index, row in enumerate(predictions)]
    (output / "trace.html").write_text(page("Agent trace", "<ol>" + "".join(links) + "</ol>"), encoding="utf-8")
