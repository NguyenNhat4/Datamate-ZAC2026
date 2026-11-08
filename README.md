# Tabular analysis agent

A single `smolagents.CodeAgent` answers Vietnamese tabular questions by writing Python that runs in an isolated worker. It uses the framework's original prompt templates with task-specific instructions and can use a local model server or the OpenAI API. Tool definitions are in [tabular_agent/tools.py](tabular_agent/tools.py); [tabular_agent/agent.py](tabular_agent/agent.py) builds the agent.

For the data contract, answer types and reference evaluation profile, see [project-overview.md](project-overview.md). The current split's `rulebook.md`, each business's `README.md`, and `as_of_date` are provided to the agent. Reference answers are read only after a benchmark's inference has finished.

## Run questions in the UI

1. Install the dependencies once:

   ```bash
   make setup
   ```

2. To inspect traces in Phoenix, start it in a terminal and leave it running. This step is optional:

   ```bash
   make telemetry
   ```

3. In a terminal (a second one if Phoenix is running), start the test UI:

   ```bash
   make ui
   ```

4. Open [the test UI](http://127.0.0.1:8501). Choose a dataset directory, such as `data/train`, and select a **Model provider**:

   - **OpenAI API**: add `OPENAI_API_KEY` to your local `.env` file or environment, then choose an OpenAI model. The API is billed and receives the selected question and relevant business context.
   - **Local server**: run `make serve-local` in a separate terminal and leave it running. This uses the locally provisioned model and defaults to `local-coder-7b` at `http://127.0.0.1:8000/v1`. Loading can take a while. Use **Check local connection** when it is ready. You may instead use another OpenAI-compatible local server.

5. Choose a **Business**, select questions, and click **Run selected questions**. To trace the run, check **Send traces to local Phoenix** first.
6. After the run finishes, inspect each result and its elapsed time, model calls, and retries. Expand the question under **Actions and observations** to see generated code, observations, errors, and agent memory. If tracing is enabled, open [Phoenix](http://127.0.0.1:6006), select the `tabular-agent` project, and open the latest trace.

Press `Ctrl+C` in each terminal to stop the UI, local model server, and Phoenix. Runs, memory logs, and traces may contain business data and are stored locally in Git-ignored directories.

## Run locally

Requirements: Linux, working bubblewrap user namespaces, Python 3.12, `uv`, and an already provisioned local OpenAI-compatible model server. The agent does not download weights during inference.

```bash
make setup
make predict SPLIT=data/public_test ARGS="--model your-model --base-url http://127.0.0.1:8000/v1"
make bench SPLIT=data/train LIMIT=5 ARGS="--model your-model --base-url http://127.0.0.1:8000/v1"
```

## Switch to the OpenAI API

Add `OPENAI_API_KEY` to the Git-ignored `.env` file, or set it in your environment. Do not put the key in commands, logs, or chat. The environment variable takes precedence. The optional `OPENAI_MODEL` setting selects the default API model; otherwise the default is `gpt-6-luna`.

In `make ui`, select **OpenAI API** under **Model provider** and enter the model name. Select **Local server** to switch back. From the CLI:

```bash
make bench SPLIT=data/train LIMIT=5 ARGS="--provider openai --model gpt-5.4-mini"
```

The existing `--backend openai` option means a local OpenAI-compatible server; use `--provider openai` for the hosted API. OpenAI requests send the question, current rulebook, business README, generated code, and any observations printed from the data to OpenAI. API calls may incur charges. This provider requires network access and is not suitable for evaluation profiles that require offline inference. The API endpoint is fixed to `https://api.openai.com/v1`.

`make demo` creates an invented dataset and starts the existing provisioned local launcher. For the UI, follow the steps above; a separate local model server is needed only when **Local server** is selected.

Each run saves `questions.jsonl`, incrementally flushed `submission.jsonl`, `manifest.json`, `timings.json`, `timing-summary.json`, and one action log per question under `logs/`. Benchmarks also save `score.json`, including coverage and answered accuracy. The scorer is an **internal approximation**, not the official evaluator. Existing run directories are never overwritten. File-read timing is included in execution timing; it must not be added again. Manifest fields for weights and server settings are user-supplied, not independently verified.

To evaluate the single agent on a balanced validation sample, run:

```bash
make bench LIMIT=100 ARGS="--model local-coder-7b --base-url http://127.0.0.1:8000/v1 --partition validation --sample balanced"
```

Every UI and CLI run uses the single agent; no mode selection is required. The agent uses at most four steps, one repair, and a 60-second question deadline (or a smaller configured limit). It caches bounded source previews for unchanged business files during the run; a preview is not evidence of full data coverage. The single-agent architecture is intended to reduce model calls and latency; speed and accuracy are still being evaluated. The within-10-second goal has **not been achieved or validated**. For comparisons, freeze the selected question list and use the same model, settings and questions. Timing summaries use nearest-rank percentiles and distinguish answers from abstentions.

### Current real-model evidence

On 8 November 2026, one local training-question run using the `local-coder-7b` alias returned **111**, while the training reference was **107**. It took about **20.6 seconds**. The model called `count_orders` with `exclude_cancelled_and_returned=False`, despite the applicable training rulebook excluding cancelled and returned orders for a normal order count. The helper followed the requested setting; it cannot determine whether that setting correctly reflects the question. An independent calculation found four cancelled orders in that period.

This is one development example, not a representative benchmark or a model ranking. The current safeguards can catch some date and status mistakes, but they do not ensure the model uses the correct eligibility policy or prevent every incorrect answer. The current model alias alone does not verify its weight source or hardware configuration. The planned 100-question business-disjoint validation and repeated latency measurements remain outstanding.

## Inspect agent memory

Smolagents keeps the task, actions, observations, and errors in its in-memory `AgentMemory` during each question. A step callback records the model-input messages and completed step in that question's local JSONL log. In `make ui`, expand a question under **Actions and observations** to inspect the messages, model output, errors, and token usage when the model reports it. This is the framework's memory representation before the model adapter formats the request.

Each question creates a fresh agent and worker; memory from a previous question is not reused. The recorded logs may contain full questions, rulebooks, business notes, and observations, so keep them local. The agent does not automatically summarize or prune its memory; the `--context` setting is not currently enforced as a token limit.

Before solving, the runner gathers a bounded file and schema preview in a separate read-only isolated worker. It includes at most eight files and two sheets or tables per file. It includes up to four preview rows per inspected sheet or table to help identify headers below a title. The preview worker is then cleaned up; its variables and table cache are separate from the question execution worker. It is not a complete source read or evidence of date coverage. Previewed business rows may appear in model context and local logs.

Bare `list_files()` and `inspect_table(...)` calls display their results in the worker observations. Other intermediate values should be printed explicitly. Excel workbooks with one sheet can be read without a sheet argument; workbooks with multiple sheets require a selection.

`profile_columns` summarizes observed values and missing entries in selected DataFrame columns; `parse_dates` uses an explicit date format. A fresh worker provides pandas as `pd` and the `datetime` module. `as_of_date` is also supplied as an ISO date string; it is restored if the worker restarts. Computed variables are lost after an execution error.

Full table reads report a bounded list of observed values for low-cardinality text columns to help identify categorical labels. These observations can contain business data and are included in the local logs and model context.

`distinct_percentage` computes a percentage over distinct IDs in a selected period and checks for conflicting or missing categories. If an agent reads its first table and attempts to answer in the same action, the worker defers the answer for a separate step so the agent can review the read observations; this is not a semantic correctness guarantee.

The worker also exposes `date_window`, `filter_categories`, `count_entities`, `count_orders`, `checked_join`, and `round_decimal`. `count_orders` takes an explicit date format, period, and cancellation/return policy, and excludes an entire order when any selected line has an excluded status. These helpers require explicit inputs; the agent must still apply the current split's rulebook. Repeated table reads in one worker use an in-memory cache and return copies. An execution error discards the worker and its cache.

For recognizable order identifiers and status columns, `count_entities` flags rows with cancellation or return labels. The worker also checks certain direct `nunique()` calls against matching selected DataFrames. The agent must filter the relevant statuses or explicitly allow them when the question or rulebook requires them. These heuristics do not validate arbitrary generated code or prove the result correct.

An `unanswerable` conclusion requires at least one complete source read; an agent that cannot establish absence can abstain. A complete read alone does not prove that all relevant sources were inspected.

The worker flags some near-miss categorical equality filters, incompatible comparisons between ISO and day-first dates or Python dates and pandas timestamps, and day-first date strings ordered as text. A near-miss can be a legitimate empty result; after inspecting the data, the agent can explicitly use `Series.eq` to retain exact-match behavior. These checks cover only some generated expressions and do not prove a calculation is correct.

When generated code uses `len(frame)` or `frame.shape[0]` on a DataFrame with repeated values in a column named like an identifier, the worker asks the agent to choose between a distinct entity count and an explicit row count (`len(frame.index)`). This heuristic can flag valid row counts and does not cover every way of counting rows.

This uses the step callbacks described in the [smolagents memory guide](https://huggingface.co/docs/smolagents/v1.26.0/en/tutorials/memory). The saved step view is for inspection; it is not a resumable agent session.

## Inspect runs with OpenTelemetry

The optional local setup follows the [smolagents run-inspection guide](https://huggingface.co/docs/smolagents/v1.26.0/en/tutorials/inspect_runs): OpenInference instruments the agent and sends OpenTelemetry spans to Phoenix.

Start Phoenix in one terminal:

```bash
make telemetry
```

Open [Phoenix](http://127.0.0.1:6006) and select the `tabular-agent` project. In another terminal, enable tracing for a run:

```bash
make bench LIMIT=5 ARGS="--model your-model --base-url http://127.0.0.1:8000/v1 --telemetry"
```

For `make ui`, select **Send traces to local Phoenix** before starting a run. Phoenix must be running first. Agent runs, steps, local model calls, and worker-observed tool calls appear in the trace; DataFrames stay in the worker and are represented by their shape. A nested helper called internally by another tool is not a separate tool span.

Tracing is off by default and exports only to `127.0.0.1:6006`. It can record questions, rulebooks, model messages, code, observations, and tool arguments or results, which may contain business data. Phoenix stores traces locally under the Git-ignored `.cache/phoenix` directory. The local launcher disables Phoenix's gRPC listener; the HTTP endpoint remains available on localhost. The collector is optional and does not need to run for normal inference.

## Structure

- `tools.py`: `@tool` functions, descriptions, and the available tool list.
- `agent.py`: single native `CodeAgent` construction and per-question task context.
- `memory.py`: records framework memory at each completed action for local inspection.
- `executor.py` and `worker.py`: a persistent bubblewrap worker with read-only input, no network, time and resource limits, and reset after errors.
- `telemetry.py`: optional OpenInference and local OpenTelemetry export; `phoenix_local.py` starts the local HTTP-only collector.
- `model.py`: local and OpenAI API `OpenAIModel` configurations.
- `contracts.py`, `evaluate.py`, and `cli.py`: typed outputs, internal scoring, and the runner.

The agent uses the framework's original system prompt, task-specific instructions, and the framework's tool descriptions. Typed output validation and isolation tests do not prove answer accuracy, and real-model quality and final deployment compatibility remain to be evaluated.

## Checks

```bash
make check
make test
make test-sandbox
```

The new tests use invented data and a scripted model. The sandbox suite needs working bubblewrap namespaces. Older tests in the top-level `tests/` directory describe the retired implementation; the Makefile runs the rewrite suite in `tests/rewrite/`.
