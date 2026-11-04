# Local Tabular Analysis Agent

One native `smolagents.CodeAgent` answers Vietnamese questions by reading business documentation, writing Python, and observing calculations in an isolated worker. A local model server supplies the language model. The runner owns deadlines, output validation, trace files, and submissions.

Start with [Understanding and debugging the agent](agent-walkthrough.md) for diagrams and a worked example. [project-overview.md](project-overview.md) defines the problem and reference evaluation profile; [smolagents-rewrite-plan.md](smolagents-rewrite-plan.md) records the design and remaining gates.

## Start with the invented example

With the already provisioned local model and llama.cpp runtime:

```bash
make demo
```

This creates three invented questions over two monthly Excel sheets and a CSV conversion table, starts the local 7B model, runs the native agent, scores against synthetic references in a separate process, and stops the server. It does not run the previous controller. Open the reported `trace.html`, choose a question, and read its expected-versus-actual summary and recommended next step. Expand the raw actions only when you need the code, observations, validation messages, or errors.

Requirements: Linux with working unprivileged user namespaces, `bubblewrap`, `make`, `uv`, Python 3.12, and provisioned weights/runtime. `make setup` installs the locked dependencies in `.venv-native`; installation may require internet. The earlier `.venv` is retained separately. Inference does not install packages or download models.

The launcher defaults to the provisioned Qwen2.5-Coder-7B-Instruct Q4_K_M weights, full GPU offload, a 16K context, Q8 key/value cache, and the workstation's `Vulkan1` device. This configuration loaded on the local 6 GB GPU during the synthetic demo. Override `MODEL_FILE`, `LLAMA_SERVER`, `LOCAL_DEVICE`, `LOCAL_PORT`, `LOCAL_CONTEXT`, `LOCAL_GPU_LAYERS`, or `LOCAL_CACHE_TYPE_K`/`V` as needed. These are workstation settings, not verified reference-hardware settings. The launcher records the actual weight digest.

## Commands

| Command | Purpose |
| --- | --- |
| `make setup` | Install Python 3.12 dependencies from `uv.lock`. |
| `make demo` | Run the provisioned model on the invented example. |
| `make bench-local LIMIT=5` | Start the local model and evaluate selected training questions with the new agent. |
| `make bench` | Evaluate against a local server that is already running. |
| `make predict SPLIT=data/public_test` | Write predictions without opening references. |
| `make doctor` | Check local model discovery; it does not generate an answer. |
| `make test` | Contract, evaluator, and scripted native-loop tests. |
| `make test-sandbox` | Also exercise actual isolation, readers, recovery, and output writes with invented data. |
| `make check` | Compile Python and check diff whitespace. |

For an existing local OpenAI-compatible server:

```bash
make bench ARGS="--backend openai --base-url http://127.0.0.1:8000/v1 --model your-served-model"
```

Ollama uses its compatible `/v1` endpoint: select `--backend ollama`. See `.env.example` for optional local settings. Environment variables override `.env`; explicit flags override both. External model endpoints are rejected. Redirects, environment proxies, SDK retries, and framework rate-limit retries are disabled. A loopback tunnel to an external service does not satisfy offline requirements.

Use `OUT=runs/new-name` to choose a new run directory. Existing outputs are never overwritten. `LIMIT=0` selects all questions. `ARGS` forwards CLI options, for example:

```bash
make bench-local LIMIT=20 ARGS="--partition validation --sample balanced"
```

Business partitions retain the existing deterministic membership (`--partition-seed 42`). Use development businesses for debugging, validation for comparisons, and untouched holdout businesses only after freezing choices. A benchmark opens references in a separate scoring process after inference. Synthetic and scripted tests establish mechanism behavior; they do not establish model accuracy.

## What to inspect when debugging

Each run writes:

| Artifact | What it tells you |
| --- | --- |
| `trace.html` | Click a question, then expand actions and observations in order. No web server or frontend build is needed. |
| `logs/000000.html` | Question, expected versus actual answer, observed problems, recommended next step, then expandable events. |
| `logs/000000.events.jsonl` | Events flushed during the question, including before interruption. |
| `logs/000000.json` | Completed question trace, final answer, calculation record, and elapsed time. |
| `submission.jsonl` | Only submission fields; each completed row is flushed and fsynced. |
| `questions.jsonl` | Exact selected question set, with association IDs. |
| `config.json` | Model, dependency versions, source/rulebook/question hashes, and launcher settings. |
| `report.json` | Completion status, timings, and benchmark metrics when references are available. |
| `score.json` | Separate scorer output for benchmark runs. |

Trace artifacts can contain business data and stay in Git-ignored local directories. Ctrl-C preserves completed answers and flushed events. There is no implicit resume; use a new output directory for another run. Missing predictions count as abstentions under the documented metric.

## Responsibility map

| Module | Responsibility |
| --- | --- |
| `agent.py` | Native CodeAgent instructions, completion guard, final checks, and step-limit hook. |
| `model.py` | Native OpenAIModel connected to a loopback server with bounded requests. |
| `data.py` | Business context and stable file aliases. |
| `executor.py` | Bubblewrap process, bounded JSON communication, worker generations, and cleanup. |
| `worker.py` | Persistent CPython execution inside the isolated process. |
| `tools.py`, `data_tools.py` | Inspections, full reads, explicit parsing, checked joins, and read diagnostics. |
| `contracts.py`, `evidence.py` | Strict submission shapes and calculation record checks. |
| `runner.py`, `cli.py` | Selection, configuration, deadlines, incremental output, and separate scoring. |
| `evaluate.py`, `splits.py` | Existing internal matcher and stable evaluation partitions. |
| `trace.py` | Local expandable HTML traces. |

The agent receives the current rulebook, business README, question, `as_of_date`, file map, and bounded previews. It can inspect or read additional sheets. Full source tables stay in the worker. Association IDs are not inserted into the question prompt. Business semantics are selected by the model; readers do not silently select business rules.

Generated code calls `final_answer({...})` with a supported answer type and an internal `calculation` record. The record declares sources, required input columns per source, metric, period, grain, unit, rules, selected row count, and checks. Substantive answers are rejected when declared columns are absent from the read schemas or required information is explicitly marked missing. Dated nonempty substantive calculations also report selected date bounds. Only source aliases recorded by full `read_table` calls in the current worker session may support a substantive answer. `unanswerable` requires a complete absence diagnostic and a description of missing information; its observed source coverage can lie outside the requested period. `abstain` requires no calculation. These checks detect some mistakes; evidence written by the model is not independent proof of semantic correctness.

CSV fields remain strings by default. Request numeric parsing explicitly, for example `read_table("F1", numeric_columns=["factor"], decimal_separator=".", thousands_separator=",")`. Identifier columns remain unchanged. `read_workbook("F2", header_row=0)` reads every XLSX sheet and adds `__sheet` provenance; inspect differing sheet layouts before choosing this helper.

To refresh an existing report without rerunning inference:

```bash
.venv-native/bin/python -m tabular_agent.trace runs/your-run --references path/to/training-or-synthetic-answers.jsonl
```

Use the same references as the saved scoring run. Omit `--references` for prediction-only reports; they do not establish correctness. Reference answers are attached only in the separate post-inference report process.

## Failure behavior and limits

Variables and DataFrames persist between successful actions within one question. A truncated response never reaches the worker. An execution error kills the worker, discards computed variables and pending answers, and tells the native loop to reload and recompute. Timeout, crash, broken protocol, or question deadline ends the question with `abstain`. Every question starts with fresh state. Reaching the step limit produces abstention without an extra fallback generation.

Defaults: 8 actions, 2,048 generated tokens per call, 30 seconds per code action, 120 seconds per model request, 180 seconds per question, and 21,600 seconds per CLI run. Retries share those budgets. The worker also has a 2 GiB address-space limit, a 1 MiB file-size limit, 64 file descriptors, and bounded JSON/log output. It mounts only the current business, calendar, trusted helpers, runtime, and private scratch. Inputs are read-only; network and PID namespaces are separate. There is no host execution fallback.

Context is checked using the local server's chat template and tokenizer when those endpoints are available, via `/apply-template` and `/tokenize` in the [pinned server API](https://github.com/ggml-org/llama.cpp/blob/b12015/tools/server/README.md). Other compatible servers use a conservative byte allowance, recorded in `config.json`, which can abstain earlier than necessary. Oversized prompts are rejected instead of silently dropping rules or history. The local launcher disables context shifting. Model-server startup precedes the CLI's measured time; this launcher does not establish the reference six-hour end-to-end requirement.

## Verification boundary

On 3 November 2026, `make test-sandbox` passed all 36 checks, including actual bubblewrap execution and synthetic native-agent recovery. `make check` and shell syntax checks passed. At that checkpoint, the retained scorer, answer contracts, parser helpers, and partitions matched their source-snapshot digests. The following fixes extend the table reader with opt-in numeric parsing.

A real local 7B run on the three invented demo questions completed in 138.124 seconds of CLI time, excluding server startup: 1 correct, 2 wrong, 0 abstentions, 0 invalid outputs; internal PA −0.3333. The quantity answer was `121212.0` instead of `27`, and the agent returned a numeric profit despite absent cost data. The matching product answer also read only one monthly sheet, so it does not demonstrate correct cross-month reasoning. This live semantic acceptance gate remains unmet. The traces show that execution/format checks can pass while the calculation is wrong. These observations are from invented examples, not a representative benchmark or a comparison against the previous controller.

The completed demo artifacts are in `runs/demo-output-20261103-172651-636584/`, including `trace.html`, predictions, configuration, and the scoring report. Artifacts remain Git-ignored.

The previous source, untracked source files, tests, locks, and documentation were captured in a local Git-ignored snapshot before replacement. The user requested starting the new implementation without running an old-agent baseline; no new old-agent benchmark was performed. Source data and weights remain outside Git.

The scorer and explicit parsing helpers were retained as independent components. The internal scorer is not an official evaluator. Documented matching assumptions remain: numeric tolerance `1e-8`, decimal half-up percent rounding, full-column-name matching before positional fallback, and first/name-column alias interpretation. Scores across changed evaluators are not automatically comparable.

A complete offline Docker package, nested sandbox compatibility in the evaluator image, representative business-level accuracy, reference-hardware throughput, and repeatability remain unverified. Record the exact model revision, source, licence, release date, parameter count, quantization, and runtime build before selecting a submission configuration. The offline reference profile and separate packaging guidance still require confirmation.

On 4 November 2026, all 43 updated checks passed with `make test-sandbox`, including real-worker checks for explicit numeric parsing, unchanged identifiers, declared missing-column rejection, and reading both monthly sheets. `make check` passed. Report content and escaping were checked automatically; visual browser inspection was blocked by the local-file URL policy. These checks do not establish real-model accuracy.

The follow-up live run with the same local model completed in 381.735 seconds of CLI time: 0 correct, 1 wrong, 2 abstentions, 0 invalid outputs (internal PA −0.3333). The quantity result remained `121212.0` instead of `27`; the other questions exhausted their step budgets after execution or calculation-record errors. The added helpers and column checks did not resolve model reliability. Its readable reports are in `runs/demo-output-20261104-165856-950463/`. This is another three-question synthetic observation, not evidence of improved benchmark accuracy.
