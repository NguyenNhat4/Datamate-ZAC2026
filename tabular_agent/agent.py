"""Build and run the tabular CodeAgent."""
import importlib.resources
import json
from pathlib import Path

import yaml

from smolagents import CodeAgent
from smolagents.default_tools import FinalAnswerTool
from smolagents.models import ChatMessage, MessageRole

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
When a question asks for one price but matching records contain multiple prices, check
whether they agree; follow the rulebook and abstain if the data does not resolve the ambiguity.
Use final_answer with a single dictionary: {"type": "number", "answer": 10}, for example.
The supported types are number, percent, string, date, boolean, list, table, unanswerable,
and abstain. For tables, answer is {"columns": [...], "rows": [[...]]}.
Use unanswerable when the data cannot support the answer; abstain when you are uncertain.
After a worker error its variables are cleared; reload the data and recompute.
"""


SYSTEM_PROMPT = """You solve the user's tabular-data question by writing Python.
At each step give a brief Thought and Python code between {{code_block_opening_tag}} and {{code_block_closing_tag}}.
Print observations from intermediate steps. Use only the tools and imports listed below.
Python state persists between successful steps. If a worker error resets state, reload data and recompute.
Return the final result by calling final_answer with the typed dictionary described in the instructions.
Do not copy a task, date, calculation or assumption from an example: solve the actual question.

Available tools:
{{code_block_opening_tag}}
{% for tool in tools.values() %}
{{ tool.to_code_prompt() }}
{% endfor %}
{{code_block_closing_tag}}
Allowed imports: {{authorized_imports}}
{{custom_instructions}}
"""


def prompt_templates():
    # Keep the framework's auxiliary templates, but remove its unrelated example tasks.
    templates = yaml.safe_load(importlib.resources.files('smolagents.prompts').joinpath('code_agent.yaml').read_text())
    templates['system_prompt'] = SYSTEM_PROMPT
    return templates


class TypedFinalAnswer(FinalAnswerTool):
    description = 'Return one dictionary with the answer type and answer or reason.'
    inputs = {'answer': {'type': 'object', 'description': 'Typed answer dictionary.'}}
    output_type = 'object'


class TabularCodeAgent(CodeAgent):
    def provide_final_answer(self, task):
        # The agent has exhausted its execution budget; an ungrounded extra model
        # call cannot execute code and can exceed the model's context window.
        return ChatMessage(role=MessageRole.ASSISTANT,
                           content={'type': 'abstain', 'reason': 'Unable to obtain a validated answer within the step limit.'})


def build_agent(model, business: Path, calendar: Path, *, max_steps=8, action_seconds=30, emit=None):
    executor = IsolatedExecutor(business, calendar, timeout=action_seconds, emit=emit)

    def check_answer(answer, memory, agent):
        validate_answer(answer)
        return True

    agent = TabularCodeAgent(
        tools=[list_files, inspect_table, read_table, read_workbook, parse_numbers, TypedFinalAnswer()],
        model=model,
        prompt_templates=prompt_templates(),
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
