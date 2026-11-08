"""Build and run the tabular CodeAgent."""
import json
from pathlib import Path

from smolagents import CodeAgent, ToolCallingAgent
from smolagents.default_tools import FinalAnswerTool
from smolagents.memory import ActionStep
from smolagents.agents import ToolOutput
from smolagents.models import ChatMessage, MessageRole
from smolagents.utils import AgentGenerationError, AgentParsingError

from .executor import IsolatedExecutor
from .tools import AGENT_TOOLS
from .contracts import validate_answer
from .memory import memory_callback
from .telemetry import question_span

INSTRUCTIONS = """Answer the question using only the business files, its README and the current split rulebook.
Use as_of_date for relative dates. Follow explicit question instructions before default rules.
Inspect the files first; read every relevant sheet and table, not just a preview.
Numbers and dates can be text in CSV or Excel: inspect formats and parse them before arithmetic.
For example, parse_numbers(df['amount'], decimal_separator='.', thousands_separator=',')
parses comma-grouped numbers; choose separators based on the actual data.
File aliases are assigned by list_files, not by the README order. For Excel, first use
inspect_table(name) to list sheets, then inspect_table(name, sheet='actual sheet').
Use keyword arguments for sheet and table; the file name is not a sheet name.
Use profile_columns(df, ['actual column']) to inspect actual status labels and missing values.
Parse dates using parse_dates(df['date column'], date_format='%d/%m/%Y') when the
README says dates are day/month/year. Adapt the format to the source.
Count distinct order IDs when each order spans multiple rows; use the rulebook's
eligibility criteria and actual status values, and handle blank repeated order fields.
For an order percentage, first restrict to the requested period; count distinct order IDs
matching the stated condition as numerator, and distinct order IDs in the appropriate
entire period as denominator. Use the observed status values, not an invented spelling.
Use distinct_percentage(filtered_df, id_column='actual ID column',
category_column='actual status column', selected_values=['observed status']) for this.
It computes the percentage over distinct IDs; round its result to the required precision.
Do not divide line counts when an order has multiple lines. Check that the status labels
used by the filter actually appear in the full data before interpreting a zero.
For revenue sum parsed line amounts rather than counting rows. Compare actual distinct
prices, not the number of matching rows, and apply the rulebook's price definition.
If a filter returns zero, check date coverage and observed values before accepting it.
Use /calendar.csv for lunar dates. Handle units, joins, eligibility and rounding carefully.
When a question asks for one price but matching records contain multiple prices, check
whether they agree; follow the rulebook and abstain if the data does not resolve the ambiguity.
Use final_answer with a single dictionary: {"type": "number", "answer": 10}, for example.
The supported types are number, percent, string, date, boolean, list, table, unanswerable,
and abstain. For tables, answer is {"columns": [...], "rows": [[...]]}.
Use unanswerable when the data cannot support the answer; abstain when you are uncertain.
After a worker error ALL variables and imports are cleared; fix the failed call, then
reload the data and recompute. Do not repeat unchanged code after the same error.
"""

MANAGED_INSTRUCTIONS = INSTRUCTIONS + """
Your framework-managed task requests a detailed report. Put that report inside the
`report` string field of the dictionary passed to final_answer, alongside the required
`type` and `answer` (or `reason`) fields. The report should contain the framework's
three requested sections and describe sources, filters and checks. For example:
final_answer({"type": "number", "answer": result, "report": "### 1. Task outcome (short version): ...\\n### 2. Task outcome (extremely detailed version): ...\\n### 3. Additional context (if relevant): ..."})
A schema preview contains only the first row, not the date coverage of the source.
Read relevant sources completely before judging whether a period has data.
"""


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


class ManagedTabularAgent(TabularCodeAgent):
    """A native managed agent whose generated code always runs in its own isolated worker."""
    def __init__(self, *args, task_context, records, phase, **kwargs):
        self.task_context = task_context
        self.records = records
        self.phase = phase
        self.trace = []
        super().__init__(*args, **kwargs)

    def __call__(self, task: str, additional_args=None, **kwargs):
        if additional_args:
            raise ValueError('Managed agents do not accept additional variables; include instructions in task.')
        if self.phase == 'reviewer' and 'solver' not in self.records:
            raise ValueError('Call the solver before the reviewer.')
        if self.phase == 'adjudicator' and not {'solver', 'reviewer'} <= self.records.keys():
            raise ValueError('Call the solver and reviewer before the adjudicator.')
        context = dict(self.task_context)
        context['delegation'] = task
        prior = {name: item for name, item in self.records.items() if name != self.phase}
        if prior:
            context['prior_agent_work'] = prior
        self.trace.clear()
        try:
            return super().__call__(json.dumps(context, ensure_ascii=False), **kwargs)
        finally:
            self.python_executor.cleanup()

    def run(self, *args, **kwargs):
        try:
            result = super().run(*args, **kwargs)
            answer = validate_answer(result)
        except AgentGenerationError as error:
            cause = error
            while cause is not None:
                if isinstance(cause, TimeoutError):
                    raise
                cause = cause.__cause__
            answer = {'type': 'abstain', 'reason': 'Managed agent could not generate a complete action.'}
            result = answer
        report = result.get('report', '') if isinstance(result, dict) else ''
        self.records[self.phase] = {'answer': answer,
                                    'report': report[:1500] if isinstance(report, str) else '',
                                    'trace': json.dumps(self.trace, ensure_ascii=False, default=str)[-4000:]}
        return answer


class TabularManager(ToolCallingAgent):
    def __init__(self, *args, records, **kwargs):
        self.records = records
        super().__init__(*args, **kwargs)

    def provide_final_answer(self, task):
        return ChatMessage(role=MessageRole.ASSISTANT,
                           content={'type': 'abstain', 'reason': 'Manager reached its step limit.'})

    def _step_stream(self, memory_step):
        try:
            yield from super()._step_stream(memory_step)
        except AgentGenerationError as error:
            cause = error.__cause__
            if isinstance(cause, ValueError) and str(cause).startswith("Incomplete model response ('length')"):
                raise AgentParsingError('The previous response was truncated and not executed. '
                                        'Make one concise managed-agent tool call.', self.logger) from error
            raise

    def process_tool_calls(self, chat_message, memory_step):
        # Managed agents share one model and results store. Sequential calls also
        # make review ordering and worker cleanup deterministic.
        if len(chat_message.tool_calls or []) != 1:
            from smolagents.utils import AgentExecutionError
            raise AgentExecutionError('Call exactly one managed agent or final_answer per step.', self.logger)
        for output in super().process_tool_calls(chat_message, memory_step):
            if isinstance(output, ToolOutput) and output.tool_call.name in self.managed_agents:
                answers = [item['answer'] for item in self.records.values()]
                for candidate in answers:
                    if candidate['type'] in ('abstain', 'unanswerable'):
                        continue
                    if sum(other == candidate for other in answers) >= 2:
                        output.output = candidate
                        output.is_final_answer = True
                        break
            yield output


def source_preview(business, calendar, timeout):
    """Collect a bounded schema preview inside the same read-only worker sandbox."""
    executor = IsolatedExecutor(business, calendar, timeout=timeout)
    code = '''
files = list_files()
print('Files:', files[:8])
def summary(info):
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
    try:
        return executor(code).logs[:6000]
    finally:
        executor.cleanup()


def build_agent(model, business: Path, calendar: Path, *, max_steps=8, action_seconds=30, emit=None):
    executor = IsolatedExecutor(business, calendar, timeout=action_seconds, emit=emit)

    def check_answer(answer, memory, agent):
        validate_answer(answer)
        return True

    agent = TabularCodeAgent(
        tools=[*AGENT_TOOLS, TypedFinalAnswer()],
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
                    *, max_steps=8, action_seconds=30, emit=None, role='solver', prior_work=None):
    readme = business / 'README.md'
    context = {
        'rulebook': rulebook,
        'business_readme': readme.read_text(encoding='utf-8') if readme.exists() else '',
    }
    if role == 'reviewer':
        context['instruction'] = ('Review the solver work as potentially incorrect. Independently inspect the source '
                                  'data and recalculate; check dates, filters, grain, units, and rulebook. '
                                  'Return your own typed answer, not an assessment.')
    elif role == 'adjudicator':
        context['instruction'] = ('The solver and reviewer disagree or one abstained. Inspect the source data and '
                                  'independently recompute the answer. Do not assume either candidate is correct; '
                                  'return your own typed answer or abstain.')
    if prior_work is not None:
        context['prior_agent_work'] = prior_work
    context.update({'as_of_date': question['as_of_date'], 'question': question['question']})
    agent, executor = build_agent(model, business, calendar, max_steps=max_steps,
                                  action_seconds=action_seconds, emit=emit)
    try:
        with question_span(question):
            return validate_answer(agent.run(json.dumps(context, ensure_ascii=False)))
    finally:
        executor.cleanup()


def answer_with_verification(model, question, business, calendar, rulebook, *, max_steps=8,
                             action_seconds=30, emit=None):
    """Use smolagents managed-agent delegation with isolated CodeAgent workers."""
    records = {}
    readme = business / 'README.md'
    task_context = {'rulebook': rulebook, 'business_readme': readme.read_text(encoding='utf-8') if readme.exists() else '',
                    'as_of_date': question['as_of_date'], 'question': question['question']}
    try:
        task_context['source_preview'] = source_preview(business, calendar, action_seconds)
    except (ValueError, RuntimeError, TimeoutError, OSError) as error:
        task_context['source_preview'] = f'Preview unavailable: {type(error).__name__}: {error}'
    if emit:
        emit({'stage': 'source_preview', 'text': task_context['source_preview']})
    agents = []
    descriptions = {
        'solver': 'Solve the question from the business data and rulebook, returning a typed answer.',
        'reviewer': 'Review the solver result and recalculate from the source data; return your own typed answer.',
        'adjudicator': 'Investigate disagreements between the solver and reviewer and recompute a typed answer.',
    }
    for phase, description in descriptions.items():
        executor = IsolatedExecutor(business, calendar, timeout=action_seconds,
                                    emit=(lambda event, phase=phase: emit({**event, 'phase': phase})) if emit else None)
        agent = ManagedTabularAgent(tools=[*AGENT_TOOLS, TypedFinalAnswer()], model=model, executor=executor,
                                    instructions=MANAGED_INSTRUCTIONS, additional_authorized_imports=['pandas', 'numpy', 'datetime', 'decimal', 'math', 'statistics', 're'],
                                    max_steps=max_steps, verbosity_level=0, name=phase, description=description,
                                    task_context=task_context, records=records, phase=phase)
        def make_record(target, role):
            def record(step, agent):
                callback_events = []
                memory_callback(callback_events.append)(step, agent)
                event = callback_events[0]
                target.trace.append({key: event.get(key) for key in ('code_action', 'observations', 'error')})
                if emit:
                    emit({**event, 'phase': role})
            return record
        agent.step_callbacks.register(ActionStep, make_record(agent, phase))
        agents.append(agent)

    def check_manager(answer, memory, agent):
        validated = validate_answer(answer)
        if validated['type'] == 'abstain':
            return True
        if not {'solver', 'reviewer'} <= records.keys():
            raise ValueError('Delegate to solver and reviewer before answering.')
        agreeing = sum(1 for item in records.values() if item['answer']['type'] == validated['type']
                       and (validated['type'] == 'unanswerable' or item['answer'].get('answer') == validated.get('answer')))
        if agreeing < 2:
            raise ValueError('At least two managed agents must agree with the final answer; delegate to adjudicator or abstain.')
        return True

    manager = TabularManager(tools=[TypedFinalAnswer()], managed_agents=agents, model=model, records=records,
                             instructions=('Delegate the task to solver, then reviewer. If they disagree, ask the adjudicator. '
                                           'Use final_answer with the typed answer only when two agents agree; otherwise abstain. '
                                           'All agents have the question, as_of_date, business README and rulebook.'),
                             final_answer_checks=[check_manager], max_steps=max_steps, max_tool_threads=1, verbosity_level=0,
                             step_callbacks=[memory_callback((lambda event: emit({**event, 'phase': 'manager'})) if emit else None)])
    try:
        with question_span(question):
            return validate_answer(manager.run(json.dumps({'question': question['question'], 'as_of_date': question['as_of_date']}, ensure_ascii=False)))
    finally:
        for agent in agents:
            agent.python_executor.cleanup()
