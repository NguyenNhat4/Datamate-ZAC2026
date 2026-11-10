# Tabular analysis assistant

Answers Vietnamese questions about a business's tabular files (CSV, Excel, SQLite) with a typed answer, in three stages:

1. **Preparation** (`make prepare`, [tabular_agent/preparation/](tabular_agent/preparation/)) reads every file of each business, without reading questions or answers. For each business it publishes a DuckDB **bundle**: clean tables, rulebook-ready `semantic` relations (orders and sales lines with the rulebook's order rules applied), and a documented schema. See [data preparation](docs/data-preparation.md).
2. **Fixed question kinds** ([tabular_agent/intents/](tabular_agent/intents/)) answer the recurring kinds of question without the model: revenue, orders, cancellations, comparisons with an earlier period, shifts, rankings of products and customers, debt, stock and more. A question is recognised only when every one of its words is explained by the kind's vocabulary, a period or a number; the answer comes from a fixed query over the bundle's rulebook-ready relations. Anything not recognised goes to stage 3. The kinds are checked against the training reference answers with `scripts/oracle-replay.py`, which calls no model; see [the intent record](docs/fix/2026-11-11_intent_layer/README.md).
3. **Model answering** ([tabular_agent/sql_agent.py](tabular_agent/sql_agent.py)) gives a local model the split's rulebook, the business README, the bundle's schema, explicit dates derived from `as_of_date`, and the question. The model sends one read-only `SELECT` at a time, sees the result, and ends with a typed JSON answer. Code checks every query and every answer; see [one question](docs/diagrams/02-question-flow.md).

The data contract, answer types and the reference evaluation profile are in [project-overview.md](project-overview.md). The current goal, scores, backlog and lessons are in [the fix index](docs/fix/README.md). How to measure a commit is in [the runbook](docs/fix/RUNBOOK.md).

## Setup

Requirements: Linux, Python 3.12, `uv`, and the local model weights and `llama-server` binary already provisioned under `models/` (the paths are in [scripts/local-model.sh](scripts/local-model.sh)). Nothing is downloaded during inference.

```bash
make setup      # installs the locked dependencies into .venv-native
make help       # lists the targets
```

The benchmark runs use the alias `local-coder-7b` (weights and server settings in runbook section 1). The model choice is still open ([workflow review](docs/fix/2026-11-10_workflow_gaps/README.md), 3.8).

## Try questions in the UI

1. In one terminal, start the model server and leave it running. It loads the model with the benchmark settings on port 18234:

   ```bash
   make serve
   ```

2. In a second terminal, start the UI and open <http://127.0.0.1:8501>:

   ```bash
   make ui
   ```

3. Choose a dataset directory such as `data/train`, a business and some questions, then **Run selected questions**. The UI answers with SQL over the business's bundle, taken from the first of `artifacts/prepared-r2-dev`, `-unseen` and `-public` that holds it (override with `PREPARED="dir ..."`), at temperature 0 with at most six steps. Prepare the bundles first with `make prepare`.
4. Each result shows the answer, time and model calls. Expand a question under **Actions and observations** to see each model reply, query and result, and the final answer. On a split with `answers.jsonl` the run is a benchmark and also shows the internal score.

Press `Ctrl+C` in each terminal to stop. Stop the model server when you are done so the GPU idles. Runs are saved under `runs/`; they hold business data and stay out of Git.

## Command line

```bash
make prepare SPLIT=data/train OUT=artifacts/prepared-example ARGS="--business biz_104"
make bench BUNDLES=artifacts/prepared-example LIMIT=20        # against the server started by make serve
make bench-local BUNDLES=artifacts/prepared-example LIMIT=20  # starts a fresh server and stops it afterwards
make predict SPLIT=data/public_test BUNDLES=artifacts/prepared-r2-public
```

- `BUNDLES` is one folder written by `make prepare`. A question whose business has no bundle there is answered `abstain` without an error, so check that the folder holds every business of the split.
- The defaults are the benchmark settings: temperature 0, at most six steps, 60 seconds per question, all selected questions (`LIMIT=0`), the server on `LOCAL_PORT` (18234) with the model `LOCAL_MODEL_ALIAS` (`local-coder-7b`). Pass other CLI options with `ARGS="..."`, for example `ARGS="--question-id q_... --question-id q_..."`.
- Recurring question kinds are answered by fixed queries before the model is asked (`--no-intents` turns this off, to measure the model alone). A run's log shows an `intent` event for each such question; the manifest records `"intents": true`.
- Each run writes `questions.jsonl`, `submission.jsonl` (flushed after every question), `manifest.json` (commit, settings, dependency versions), `timings.json`, `timing-summary.json`, and one log per question under `logs/`. A log holds the stages `schema`, `memory_step` (model reply), `sql`, `observation` (result or rejection) and `question_timing` (id, final answer, seconds). A benchmark also writes `score.json`.
- Reference answers are read only by `bench`, after all questions are answered. The scorer (`tabular_agent/evaluate.py`) is an **internal approximation**, not the official evaluator.
- `make bench` against a long-running server is quicker to start, but the server's prompt cache makes replies depend on earlier requests. For comparable numbers, use the runbook: one fresh server per set, from a worktree pinned at the measured commit.

## Submission entrypoint

`predict.sh` runs the system the benchmarks measure, end to end:

1. It starts the local model server with the benchmark settings (`scripts/serve-local.sh`, port `LOCAL_PORT`, default 18234).
2. While the model loads, it prepares every business in `DATA_DIR` (default `/data`, only read) into DuckDB bundles under `WORK_DIR` (default a new folder under `/tmp`).
3. It answers every question with SQL over those bundles, at temperature 0 with at most six steps.

Answers go to `RESULT_DIR/submission.jsonl` (default `/result`); `RESULT_DIR` must be empty or absent. Logs go to `RESULT_DIR/logs/`: one file per question, whose last event holds the id, final answer and elapsed seconds, plus `pipeline.log` (phase times) and `prepare.log`.

The whole run is kept within `RUN_SECONDS` (default 21,600, the reference profile's six hours, including preparation and model loading). Answering stops `STOP_MARGIN_SECONDS` (default 300) before that, keeping every answer already written. A business that preparation blocks gets `abstain` for its questions. Set `BASE_URL` (and `MODEL`) to use an already running server instead of starting one. The server binary, device (`LOCAL_DEVICE`, default `Vulkan1`) and weights are those of this machine; a container for other hardware must provide its own.

```bash
DATA_DIR=data/public_test RESULT_DIR=runs/public-dry WORK_DIR=$PWD/artifacts/prepared-dry ./predict.sh
```

The dry-run procedure and how to read its result without looking at answers are in runbook section 11.

## OpenAI API (development only)

`--provider openai` answers with a hosted model instead of the local server, for example `make bench BUNDLES=... LIMIT=5 ARGS="--provider openai --model gpt-5.4-mini"`, or **OpenAI API** in the UI. Put `OPENAI_API_KEY` in the Git-ignored `.env` file or the environment, never in commands or logs. `OPENAI_MODEL` sets the default model (otherwise `gpt-6-luna`). The API receives the question, rulebook, business README, bundle schema, the generated queries and their results; calls are billed and need network access, so this provider does not fit an offline evaluation profile.

Importing `smolagents` loads `.env` into the environment. Local runs therefore read only the `LOCAL_*` settings, never generic names such as `MODEL` or `BASE_URL` that `.env` may hold for other tools.

## Traces (optional)

`make telemetry` starts a local Phoenix collector on <http://127.0.0.1:6006>. Add `ARGS="--telemetry"` to a run, or check **Send traces to local Phoenix** in the UI, to export one span per question and its model calls to the `tabular-agent` project. Tracing is off by default, exports only to `127.0.0.1:6006`, and stores traces under the Git-ignored `.cache/phoenix`. Traces hold questions, prompts and results, so they contain business data.

## Code layout

| Path | Role |
|---|---|
| `tabular_agent/preparation/` | Source readers, value parsers, column semantics, rulebook-ready views and the preparation pipeline; optional local-model help for ambiguous layouts |
| `tabular_agent/intents/` | Fixed question kinds: `text.py` (typing residue, abbreviations), `periods.py` (the rulebook's time rules, Tết from the lunar calendar), `entities.py` (product, group, unit and customer names of a bundle), `catalogue.py` (the kinds and their queries), `context.py` (read-only bundle access) |
| `tabular_agent/sql_agent.py` | Prompt, query checks (one allowlisted `SELECT`, no system date, unready sales tables refused) and answer checks |
| `tabular_agent/cli.py` | `prepare`, `profile`, `bench`, `predict`, `score` and `doctor` |
| `tabular_agent/model.py` | Local and OpenAI clients |
| `tabular_agent/contracts.py`, `evaluate.py` | Answer types and the internal scorer |
| `tabular_agent/telemetry.py`, `phoenix_local.py` | Optional tracing |
| `streamlit_app.py` | The UI |
| `predict.sh`, `scripts/` | Submission entrypoint; model server settings (`local-model.sh`), server and benchmark launchers, dry-run report; `oracle-replay.py` (intents against training references, no model), `claim-coverage.py` (counts of claimed questions of any split, no answers read), `bundle-invariants.py` (consistency of prepared bundles), `draw-samples.py` |
| `tests/answering/`, `tests/preparation/` | Synthetic checks |

Answer types are `number`, `percent`, `string`, `date`, `boolean`, `list`, `table`, `unanswerable` and `abstain`. `unanswerable` means the data cannot support an answer; `abstain` means the system chooses not to answer.

## Checks

```bash
make check   # compiles every module and checks whitespace
make test    # tests/answering and tests/preparation
```

Checks that need prepared bundles but no model (they print counts only; see the [runbook](docs/fix/RUNBOOK.md), section 12):

```bash
.venv-native/bin/python scripts/oracle-replay.py --prepared artifacts/prepared-r4-train      # open training questions against their references
.venv-native/bin/python scripts/bundle-invariants.py artifacts/prepared-r4-public            # lines vs orders, states, dates
.venv-native/bin/python scripts/claim-coverage.py --split data/public_test --prepared artifacts/prepared-r4-public
```

The tests use invented data and no model, so they check mechanisms, not answer quality. One preparation test is an expected failure on purpose (backlog B17 in the fix index).
