# Tabular analysis agent

A `smolagents.CodeAgent` answers Vietnamese questions about CSV, Excel and SQLite files. It can use a local model server or the OpenAI API. The tool definitions and descriptions are together in [tabular_agent/tools.py](tabular_agent/tools.py); [tabular_agent/agent.py](tabular_agent/agent.py) passes them directly to `CodeAgent`.

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

5. Choose a **Business**, then select from its questions. To trace the run, check **Send traces to local Phoenix**. Click **Run selected questions**.
6. Expand the question under **Actions and observations** to inspect its code, results, and agent memory. If tracing is enabled, open [Phoenix](http://127.0.0.1:6006), select the `tabular-agent` project, and open the latest trace.

Press `Ctrl+C` in each terminal to stop the UI, local model server, and Phoenix. Runs, memory logs, and traces may contain business data and are stored locally in Git-ignored directories.

## Run locally

Requirements: Linux, working bubblewrap user namespaces, Python 3.12, `uv`, and an already provisioned local OpenAI-compatible model server. The agent does not download weights during inference.

```bash
make setup
make predict SPLIT=data/public_test ARGS="--model your-model --base-url http://127.0.0.1:8000/v1"
make bench SPLIT=data/train LIMIT=5 ARGS="--model your-model --base-url http://127.0.0.1:8000/v1"
```

## Switch to the OpenAI API

Add `OPENAI_API_KEY` to the Git-ignored `.env` file, or set it in your environment. Do not put the key in commands, logs, or chat. The environment variable takes precedence. The optional `OPENAI_MODEL` setting selects the default API model; otherwise the default is `gpt-5.4-mini`.

In `make ui`, select **OpenAI API** under **Model provider** and enter the model name. Select **Local server** to switch back. From the CLI:

```bash
make bench SPLIT=data/train LIMIT=5 ARGS="--provider openai --model gpt-5.4-mini"
```

The existing `--backend openai` option means a local OpenAI-compatible server; use `--provider openai` for the hosted API. OpenAI requests send the question, current rulebook, business README, generated code, and any observations printed from the data to OpenAI. API calls may incur charges. This provider requires network access and is not suitable for evaluation profiles that require offline inference. The API endpoint is fixed to `https://api.openai.com/v1`.

`make demo` creates an invented dataset and starts the existing provisioned local launcher. For the UI, follow the steps above; a separate local model server is needed only when **Local server** is selected.

Each run saves `questions.jsonl`, incrementally flushed `submission.jsonl`, and one action log per question under `logs/`. Benchmarks also save `score.json`. The scorer is an **internal approximation**, not the official evaluator. Existing run directories are never overwritten.

## Inspect agent memory

Smolagents keeps the task, actions, observations, and errors in its in-memory `AgentMemory` during each question. A step callback records the model-input messages and completed step in that question's local JSONL log. In `make ui`, expand a question under **Actions and observations** to inspect the messages, model output, errors, and token usage when the model reports it. This is the framework's memory representation before the model adapter formats the request.

Each question creates a fresh agent and worker; memory from a previous question is not reused. The recorded logs may contain full questions, rulebooks, business notes, and observations, so keep them local. The agent does not automatically summarize or prune its memory; the `--context` setting is not currently enforced as a token limit.

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
- `agent.py`: native `CodeAgent` construction and per-question task context.
- `memory.py`: records framework memory at each completed action for local inspection.
- `executor.py` and `worker.py`: a persistent bubblewrap worker with read-only input, no network, time and resource limits, and reset after errors.
- `telemetry.py`: optional OpenInference and local OpenTelemetry export; `phoenix_local.py` starts the local HTTP-only collector.
- `model.py`: local and OpenAI API `OpenAIModel` configurations.
- `contracts.py`, `evaluate.py`, and `cli.py`: typed outputs, internal scoring, and the runner.

The rewrite uses a task-specific system prompt and the framework's tool descriptions. It no longer includes the previous calculation-evidence validator, audit helpers, detailed dashboard, or trace format. Typed output validation and basic isolation tests do not prove answer accuracy, and real-model quality and final deployment compatibility remain to be evaluated.

## Checks

```bash
make check
make test
make test-sandbox
```

The new tests use invented data and a scripted model. The sandbox suite needs working bubblewrap namespaces. Older tests in the top-level `tests/` directory describe the retired implementation; the Makefile runs the rewrite suite in `tests/rewrite/`.
