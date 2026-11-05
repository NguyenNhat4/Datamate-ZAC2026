"""Build and run the tabular CodeAgent."""
import json
from pathlib import Path

from smolagents import CodeAgent
from smolagents.default_tools import FinalAnswerTool

from .executor import IsolatedExecutor
from .tools import list_files, inspect_table, read_table, read_workbook, parse_numbers
from .contracts import validate_answer
from .memory import memory_callback
from .telemetry import question_span

INSTRUCTIONS = """Answer the question using only the business files, its README and the current split rulebook.
Use as_of_date for relative dates. Follow explicit question instructions before default rules.
Inspect the files first; read every relevant sheet and table, not just a preview.
CSV numbers are text: inspect formats and use parse_numbers before arithmetic.
Use /calendar.csv for lunar dates. Handle units, joins, eligibility and rounding carefully.
Use final_answer with a single dictionary: {"type": "number", "answer": 10}, for example.
The supported types are number, percent, string, date, boolean, list, table, unanswerable,
and abstain. For tables, answer is {"columns": [...], "rows": [[...]]}.
Use unanswerable when the data cannot support the answer; abstain when you are uncertain.
After a worker error its variables are cleared; reload the data and recompute.
"""


class TypedFinalAnswer(FinalAnswerTool):
    description = 'Return one dictionary with the answer type and answer or reason.'
    inputs = {'answer': {'type': 'object', 'description': 'Typed answer dictionary.'}}
    output_type = 'object'


def build_agent(model, business: Path, calendar: Path, *, max_steps=8, action_seconds=30, emit=None):
    executor = IsolatedExecutor(business, calendar, timeout=action_seconds, emit=emit)

    def check_answer(answer, memory, agent):
        validate_answer(answer)
        return True

    agent = CodeAgent(
        tools=[list_files, inspect_table, read_table, read_workbook, parse_numbers, TypedFinalAnswer()],
        model=model,
        executor=executor,
        instructions=INSTRUCTIONS,
        additional_authorized_imports=['pandas', 'numpy', 'datetime', 'decimal', 'math', 'statistics', 're'],
        final_answer_checks=[check_answer],
        step_callbacks=[memory_callback(emit)],
        max_steps=max_steps,
        verbosity_level=0,
    )
    return agent, executor


def answer_question(model, question: dict, business: Path, calendar: Path, rulebook: str,
                    *, max_steps=8, action_seconds=30, emit=None):
    readme = business / 'README.md'
    context = {
        'question': question['question'],
        'as_of_date': question['as_of_date'],
        'rulebook': rulebook,
        'business_readme': readme.read_text(encoding='utf-8') if readme.exists() else '',
    }
    agent, executor = build_agent(model, business, calendar, max_steps=max_steps,
                                  action_seconds=action_seconds, emit=emit)
    try:
        with question_span(question):
            return validate_answer(agent.run(json.dumps(context, ensure_ascii=False)))
    finally:
        executor.cleanup()
