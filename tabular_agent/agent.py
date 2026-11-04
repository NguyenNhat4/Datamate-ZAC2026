"""Native CodeAgent loop with three small application hooks.

1. Reject incomplete model output before any worker dispatch.
2. Validate final answers against the current worker session.
3. Stop at the step limit without the framework's extra fallback generation.
"""
from dataclasses import asdict
from importlib import resources
import json
from pathlib import Path
import time

from smolagents import CodeAgent
from smolagents.default_tools import FinalAnswerTool
from smolagents.agents import ToolCall
from smolagents.memory import ActionStep
from smolagents.utils import AgentParsingError
import yaml

from .data import BusinessContext
from .executor import IsolatedExecutor
from .runtime import Deadline, QuestionStopped, enforce_deadline

INSTRUCTIONS = """You answer Vietnamese questions using the current business's files.
Use the full current rulebook, business README, question, and AS_OF_DATE. Explicit
question instructions override defaults. IDs are routing metadata, not answer rules.

You write native Python actions, observe their output, and repair within the budget.
Start with a concise calculation outline. Do not invent facts or infer missing data
from a preview. Resolve relative time using AS_OF_DATE. Read all relevant monthly
sheets. Preserve sheet/year provenance. Do not confuse line counts with order counts.
Use the appropriate eligibility rules for the requested metric, explicit join keys,
and product-specific units. Check dates, coverage, join expansion, and rounding.
Before arithmetic, identify the actual input columns needed for this metric.
Gross profit needs sales amounts and recorded sales-line cost under the rulebook;
quantities and unit conversion factors cannot stand in for either monetary input.
Missing required information calls for a complete absence diagnostic, not a proxy.
For last week, compute the Monday of AS_OF_DATE's week, subtract seven days for
the start, and subtract one day from that Monday for the end. Use all relevant sheets.

Available inside your persistent isolated Python session:
- inspect_table(name, sheet=None, table=None): bounded raw preview or sheet/table list.
- read_table(name, sheet=None, table=None, header_row=None, delimiter=None,
             encoding='utf-8-sig', numeric_columns=None, decimal_separator=None,
             thousands_separator=None): full selected table as a pandas DataFrame.
  name may be a file alias such as F1. header_row is zero-based. Choose the header
  explicitly after inspection when ambiguous. pandas itself uses header=.
- read_workbook(name, **read_options): read ALL XLSX sheets, preserving __sheet.
  Use this for monthly-sheet workbooks when relevant coverage can span sheets.
  Parse missing years using each row's __sheet provenance.
- CSV numbers remain TEXT unless explicitly parsed. Before multiplying, summing,
  or joining on numeric measures, use numeric_columns=['actual column', ...]
  with explicit decimal_separator and thousands_separator, or parse_numbers.
  For example, text '12' * 3 repeats text; it does not calculate 36. Parse both
  quantities and conversion factors. Keep identifier columns unchanged.
- table_profile(name, date_column=None, dayfirst=None, year=None, **read_options):
  full row/null counts and optional date coverage, without business filters.
- parse_numbers(values, decimal_separator='.', thousands_separator=','): explicit
  locale parsing with Decimal. Native spreadsheet numbers keep their numeric value.
- parse_dates(values, dayfirst=True, year=None): explicit date parsing; provide the
  sheet's year for dates without a year.
- checked_join(left, right, on=..., relationship='many_to_one', require_match=True).
- split_units(quantity, factor). Decimal with ROUND_HALF_UP for required rounding.
- DATA_DIR, FILES (alias to absolute source path), CALENDAR_PATH, AS_OF_DATE.

Use read_table for source calculations so the runner records full reads. A preview
cannot establish absence. Print small diagnostics; keep DataFrames in the worker.
Variables persist after successful actions. After an execution error, ALL variables
and earlier candidate answers are discarded. Reload the sources and recompute.

Call final_answer with ONE dict containing a typed answer and calculation record:
{"type":"number", "answer":30, "calculation":{
 "sources":["F1"], "required_columns":{"F1":["amount"]},
 "metric":"net revenue", "period":None,
 "grain":"sales line", "unit":"VND", "rules":["current revenue definition"],
 "row_count":2, "checks":{"cancelled_orders_excluded":True}}}
For a dated nonempty result, period is {"start":"YYYY-MM-DD","end":"YYYY-MM-DD"}
and calculation also contains date_min/date_max of the selected rows, within that
period. Provide actual diagnostics, not unsupported claims of correctness.
required_columns lists the actual columns necessary for the requested metric in
each source, including monetary inputs for money calculations. Check their presence
before computing. Do not list a substitute column with a different business meaning.
Supported types: number, percent, string, date, boolean, list, table, unanswerable,
abstain. Numbers must be finite JSON numbers; booleans cannot be used as numbers.
Percent values use percentage units. Dates use YYYY-MM-DD or YYYY-MM. Preserve
Vietnamese diacritics. Tables are {"columns":[...],"rows":[[...],...]}; lists follow
the requested order. Apply current rulebook money/percentage rounding.
For unanswerable, fully inspect relevant sources and provide a calculation with
search_complete=True and missing="specific unavailable information". Failed code,
ambiguous parsing, or uncertainty is abstain, not unanswerable. You may always use
final_answer({"type":"abstain","reason":"specific reason"}) without calculation.
"""


class TypedFinalAnswer(FinalAnswerTool):
    description = "Return a typed answer dictionary with a calculation record. Never return a bare string or number."
    inputs = {"answer": {"type": "object", "description": "Dictionary with type, answer/reason, and calculation; see the required schema."}}
    output_type = "object"


def prompt_templates():
    """Keep native framework memory/repair templates, with a focused system prompt."""
    templates = yaml.safe_load(resources.files("smolagents.prompts").joinpath("code_agent.yaml").read_text())
    templates["system_prompt"] = """You are a careful tabular data analyst.
{{custom_instructions}}

For each action, write a short Thought and Python inside {{code_block_opening_tag}}
and {{code_block_closing_tag}}. Observe the result before choosing the next action.
Import libraries before using them. Helpers listed above already exist in the worker.
Only these additional imports are supported by the installed runtime: {{authorized_imports}}.

Final tool:
{% for tool in tools.values() %}
{{tool.name}}: {{tool.description}}
{% endfor %}
Use Python values None, True, False in code. The runner converts the result to JSON.
Compute the answer from the files; do not put entire tables in your response.
"""
    return templates


def completion_status(message) -> str | None:
    raw = getattr(message, "raw", None)
    if isinstance(raw, dict):
        choices = raw.get("choices", [])
        return choices[0].get("finish_reason") if choices else None
    choices = getattr(raw, "choices", None)
    return getattr(choices[0], "finish_reason", None) if choices else None


class GuardedCodeAgent(CodeAgent):
    def _step_stream(self, memory_step):
        # Native CodeAgent yields the tool call before invoking its executor.
        # This is the dispatch boundary: keep generation and repair in its loop.
        for event in super()._step_stream(memory_step):
            if isinstance(event, ToolCall):
                status = completion_status(memory_step.model_output_message)
                if status != "stop":
                    raise AgentParsingError(
                        f"Incomplete model response ({status!r}); no code ran. "
                        "Produce a shorter complete Python action.", self.logger,
                    )
            yield event

    def _handle_max_steps_reached(self, task):
        raise QuestionStopped("Step budget exhausted; fallback generation is disabled")


class NativeAgent:
    def __init__(self, model, *, max_steps=8, question_seconds=180, action_seconds=30,
                 executor_factory=IsolatedExecutor):
        self.model = model
        self.max_steps = max_steps
        self.question_seconds = question_seconds
        self.action_seconds = action_seconds
        self.executor_factory = executor_factory

    def solve(self, question, business: Path, calendar: Path, rulebook: str, emit=None):
        started = time.monotonic()
        deadline = Deadline.after(self.question_seconds)
        events = []
        executor = None
        candidate = None
        final = {"type": "abstain", "reason": "No accepted answer"}

        def record(event):
            event = {"elapsed_seconds": round(time.monotonic() - started, 3), **event}
            events.append(event)
            if emit is not None:
                emit(event)

        def final_check(answer, memory, agent):
            try:
                executor.validate_final(answer)
            except (ValueError, TypeError) as error:
                record({"stage": "validation", "accepted": False, "reason": str(error)})
                raise
            record({"stage": "validation", "accepted": True})
            return True

        def step_finished(step, agent):
            if not isinstance(step, ActionStep):
                return
            record({
                "stage": "model_step", "step": step.step_number,
                "finish_reason": completion_status(step.model_output_message),
                "response": step.model_output, "code": step.code_action,
                "observation": step.observations, "error": str(step.error) if step.error else None,
                "usage": asdict(step.token_usage) if step.token_usage else None,
            })

        try:
            with enforce_deadline(deadline):
                context = BusinessContext.load(business, calendar)
                executor = self.executor_factory(context, question["as_of_date"], deadline,
                                                 self.action_seconds, emit=record)
                manifest = executor.manifest()
                self.model.deadline = deadline
                task = json.dumps({
                    "question": question["question"], "as_of_date": question["as_of_date"],
                    "rulebook": rulebook, "business_readme": context.readme,
                    "file_aliases": context.aliases, "manifest": manifest,
                }, ensure_ascii=False, allow_nan=False)
                record({"stage": "context", "file_aliases": context.aliases, "manifest": manifest})
                agent = GuardedCodeAgent(
                    tools=[TypedFinalAnswer()], model=self.model, executor=executor, instructions=INSTRUCTIONS,
                    prompt_templates=prompt_templates(),
                    max_steps=self.max_steps, planning_interval=None, stream_outputs=False,
                    additional_authorized_imports=["pandas", "numpy", "datetime", "decimal", "sqlite3",
                                                   "math", "json", "re", "collections", "statistics"],
                    final_answer_checks=[final_check], step_callbacks=[step_finished], verbosity_level=0,
                )
                candidate = agent.run(task)
                # Revalidate at the application boundary before writing a submission.
                final = executor.validate_final(candidate)
        except QuestionStopped as error:
            final = {"type": "abstain", "reason": str(error)}
            record({"stage": "stop", "reason": str(error)})
        except Exception as error:
            # The native framework wraps generation errors; record the actual class.
            final = {"type": "abstain", "reason": f"{type(error).__name__}: {error}"}
            record({"stage": "error", "reason": final["reason"]})
        finally:
            if executor is not None:
                executor.cleanup()
        return final, {
            "events": events, "calculation": candidate.get("calculation") if isinstance(candidate, dict) else None,
            "python_executions": executor.dispatches if executor else 0,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
