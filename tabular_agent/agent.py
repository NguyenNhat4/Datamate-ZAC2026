"""Build and run the tabular CodeAgent."""
import json
from pathlib import Path

from smolagents import CodeAgent
from smolagents.default_tools import FinalAnswerTool
from smolagents.models import ChatMessage, MessageRole
from smolagents.utils import AgentGenerationError, AgentParsingError

from .executor import IsolatedExecutor
from .tools import AGENT_TOOLS
from .contracts import validate_answer
from .memory import memory_callback
from .telemetry import question_span

SINGLE_INSTRUCTIONS = """Answer using the business files, README, and current split rulebook.
Resolve relative time using as_of_date; explicit question instructions override defaults.
The variable as_of_date is provided to Python as an ISO date string. Parse source dates before comparison.
Previews are incomplete and can have titles above headers; select the actual zero-based header_row.
Read all relevant sheets or tables completely. Inspect actual columns and status values before filtering.
Parse dates and numbers according to the source format. Filter the requested period and check coverage.
For day/month/year text, use parse_dates(column, date_format='%d/%m/%Y'); compare with pd.Timestamp(as_of_date), never raw strings. Handle missing dates explicitly.
Count distinct entity IDs rather than rows; validate joins, units, eligibility, and rounding against the rulebook.
For order counts, apply the rulebook's cancellation and return exclusions using observed statuses, then count distinct order IDs.
For a count of orders over a period, use count_orders with the actual ID, order-date and status columns, explicit ISO start/end dates, the source date format, and the exclusion policy required by the rulebook. For today, both dates are as_of_date. Do not substitute row counts.
For a normal count of valid orders, set exclude_cancelled_and_returned=True. Use False only when the question or current rulebook explicitly requires including cancelled and returned orders.
Example for an order count today (replace example file and column names and date format with the actual source metadata):
orders = read_table('F1', sheet='Orders')
count = count_orders(orders, 'order_id', 'order_date', 'status', as_of_date, as_of_date, '%d/%m/%Y', True)
final_answer({'type': 'number', 'answer': count})
If count_entities flags order statuses, filter the correct eligible statuses; allow flagged statuses only if the question or rulebook requires them.
Use date_window, filter_categories, count_entities, checked_join and round_decimal when applicable.
Use /calendar.csv for lunar dates. If a zero or a missing value is ambiguous, investigate or abstain.
Return final_answer with one dictionary, for example final_answer({'type': 'number', 'answer': 10}), or unanswerable/abstain and reason.
Use unanswerable only when the data cannot support an answer; abstain when uncertain.
After an error the worker is reset: reload data and fix the cause; never repeat failed code unchanged.
"""


class TypedFinalAnswer(FinalAnswerTool):
    description = 'Return one dictionary with the answer type and answer or reason.'
    inputs = {'answer': {'type': 'object', 'description': 'Typed answer dictionary.'}}
    output_type = 'object'


class TabularCodeAgent(CodeAgent):
    def provide_final_answer(self, task):
        # The agent has exhausted its execution budget; an ungrounded extra model
        # call cannot execute code and can exceed the model's context window.
        """Return an abstention when the agent exhausts its execution steps.

        Args:
            task: Framework task text; unused because no extra model call is made.

        Returns:
            Assistant ChatMessage containing a typed abstention and its reason.
        """
        return ChatMessage(role=MessageRole.ASSISTANT,
                           content={'type': 'abstain', 'reason': 'Unable to obtain a validated answer within the step limit.'})


class RepairLimitReached(BaseException):
    """Stop after the single-agent repair budget is exhausted."""


class SingleTabularAgent(TabularCodeAgent):
    def _step_stream(self, memory_step):
        """Stream one agent step and make truncated generations repairable.

        Args:
            memory_step: Framework action-memory entry for the current step.

        Yields:
            Events produced by the parent CodeAgent step implementation.

        Raises:
            AgentParsingError: A generation failed because its response was truncated.
            AgentGenerationError: Another model-generation failure occurred.
        """
        try:
            yield from super()._step_stream(memory_step)
        except AgentGenerationError as error:
            cause = error.__cause__
            if isinstance(cause, ValueError) and str(cause).startswith("Incomplete model response ('length')"):
                raise AgentParsingError('The previous response was truncated and not executed. Produce a concise action.', self.logger) from error
            raise


def source_preview(business, calendar, timeout, *, preview_rows=1):
    """Collect a bounded source preview in a temporary isolated worker.

    Args:
        business: Path to the business directory mounted read-only as /data.
        calendar: Path to the split calendar mounted as /calendar.csv.
        timeout: Maximum seconds to wait for each worker response.
        preview_rows: Rows to display per preview; values above one are capped
            at eight. Inspections themselves read at most eight sample rows.

    Returns:
        At most 6,000 characters of logs describing up to eight files and two
        sheets or tables per file. This is not a complete source read.

    Raises:
        RuntimeError: The isolated worker cannot start or exits unexpectedly.
        TimeoutError: The worker does not respond before the timeout.
        ValueError: Execution or the worker protocol fails.

    Notes:
        Always cleans up the preview worker, including after a failure.
    """
    executor = IsolatedExecutor(business, calendar, timeout=timeout)
    code = '''
files = list_files()
print('Files:', files[:8])
def summary(info):
    """Bound preview rows while retaining file metadata.

    Args:
        info: Metadata dictionary returned by inspect_table.

    Returns:
        New dictionary with rows replaced by first_row_only, containing at most
        one row. The enclosing preview builder can expand that bound to eight.
    """
    out = {key: value for key, value in info.items() if key not in ('rows',)}
    if 'rows' in info:
        out['first_row_only'] = info['rows'][:1]
    return out
for item in files[:8]:
    name = item['alias']
    try:
        info = inspect_table(name)
        print('File', name, 'metadata:', summary(info))
        if 'sheets' in info:
            for sheet in info['sheets'][:2]:
                print('File', name, 'sheet', sheet, 'preview:', summary(inspect_table(name, sheet=sheet)))
        elif 'tables' in info:
            for table in info['tables'][:2]:
                print('File', name, 'table', table, 'preview:', summary(inspect_table(name, table=table)))
    except Exception as error:
        print('Preview failed for', name, type(error).__name__, str(error))
'''
    if preview_rows > 1:
        code = code.replace("out['first_row_only'] = info['rows'][:1]",
                            f"out['first_rows_only'] = info['rows'][:{min(preview_rows, 8)}]")
    try:
        return executor(code).logs[:6000]
    finally:
        executor.cleanup()


def build_agent(model, business: Path, calendar: Path, *, max_steps=4, action_seconds=30, emit=None, limit_repairs=True,
                as_of_date=None):
    """Construct a fresh single agent and its isolated Python executor.

    Args:
        model: Configured smolagents model used for generation.
        business: Business data directory to mount read-only.
        calendar: Current split's calendar file to mount read-only.
        max_steps: Maximum number of agent execution steps.
        action_seconds: Timeout in seconds for each worker response.
        emit: Optional callback accepting event dictionaries for local logs.
        limit_repairs: Reject repeated failed code and stop after a second
            failed step when true.
        as_of_date: Optional ISO reference date restored in each worker action.

    Returns:
        A (SingleTabularAgent, IsolatedExecutor) pair. The caller must clean
        up the executor after using the agent.

    Raises:
        RuntimeError: bubblewrap is unavailable.
        ValueError: A supplied reference date is invalid.
    """
    executor = IsolatedExecutor(business, calendar, timeout=action_seconds, emit=emit, reject_repeated=limit_repairs,
                                as_of_date=as_of_date)

    def check_answer(answer, memory, agent):
        """Validate the final answer for the framework's acceptance hook.

        Args:
            answer: Candidate typed answer dictionary.
            memory: Framework memory supplied by the hook; unused.
            agent: Calling agent supplied by the hook; unused.

        Returns:
            True if the answer satisfies the output contract.

        Raises:
            ValueError: The answer's type or value is invalid.
        """
        validate_answer(answer)
        return True

    failures = [0]
    record = memory_callback(emit)
    def on_step(step, agent):
        """Record a completed step and enforce this agent's repair budget.

        Args:
            step: Framework action step, including its optional error.
            agent: Agent whose memory and step are being recorded.

        Returns:
            None. Emits a memory event and increments the captured failure count.

        Raises:
            RepairLimitReached: Repair limiting is enabled and a second step fails.
        """
        record(step, agent)
        if limit_repairs and step.error is not None:
            failures[0] += 1
            if failures[0] > 1:
                raise RepairLimitReached()

    agent = SingleTabularAgent(
        tools=[*AGENT_TOOLS, TypedFinalAnswer()],
        model=model,
        executor=executor,
        instructions=SINGLE_INSTRUCTIONS,
        additional_authorized_imports=['pandas', 'numpy', 'datetime', 'decimal', 'math', 'statistics', 're'],
        final_answer_checks=[check_answer],
        step_callbacks=[on_step],
        max_steps=max_steps,
        verbosity_level=0,
    )
    return agent, executor


def answer_question(model, question: dict, business: Path, calendar: Path, rulebook: str,
                    *, max_steps=4, action_seconds=30, emit=None, source_summary=None):
    """Solve one question using fresh agent memory and an isolated worker.

    Args:
        model: Configured model used by the agent.
        question: Mapping containing question text and ISO as_of_date; id and
            business_id are used by optional tracing for association.
        business: Directory containing this business's source files and README.
        calendar: Calendar file for the current split.
        rulebook: Current split's business rules as text.
        max_steps: Maximum number of agent execution steps.
        action_seconds: Timeout in seconds for each worker response.
        emit: Optional callback receiving code, observation, timing and memory
            event dictionaries, which can contain business data.
        source_summary: Optional bounded preview text to include in context.

    Returns:
        Validated dictionary containing type and answer, or type and reason.
        Exhausting the repair budget returns a typed abstention.

    Raises:
        ValueError: The final answer or configured inputs are invalid.
        OSError: Business instructions cannot be read.

    Notes:
        Other model or worker failures propagate to the caller. The executor
        is always cleaned up; reference answers are not read here.
    """
    readme = business / 'README.md'
    context = {
        'rulebook': rulebook,
        'business_readme': readme.read_text(encoding='utf-8') if readme.exists() else '',
        'calculation_instruction': ('For an order count, call count_orders with the actual file columns, '
                                    'correct date range and source date format. Under the current rulebook, '
                                    'set exclude_cancelled_and_returned=True unless the question or rulebook '
                                    'explicitly asks to include those statuses. Use its result in a typed final_answer.'),
    }
    if source_summary:
        context['source_preview'] = source_summary
    context.update({'as_of_date': question['as_of_date'], 'question': question['question']})
    agent, executor = build_agent(model, business, calendar, max_steps=max_steps,
                                  action_seconds=action_seconds, emit=emit, limit_repairs=True,
                                  as_of_date=question['as_of_date'])
    try:
        with question_span(question):
            try:
                return validate_answer(agent.run(json.dumps(context, ensure_ascii=False)))
            except RepairLimitReached:
                return {'type': 'abstain', 'reason': 'Repair budget exhausted.'}
    finally:
        executor.cleanup()
