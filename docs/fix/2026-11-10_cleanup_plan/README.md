# Cleanup: remove the retired code path and refresh outdated documents

Date: 10 November 2026
Status: done. Steps 1–5 are commits `37ee7e9`, `a40c07d`, `a19da8b`, `b2657c4` and the commit that adds section 6. Results and deviations from the plan are in section 6.
Branch: `fix/sql-answering-accuracy`, starting at `155f323`.
Scope: documents, tests, Makefile and scripts, and removing the raw-file CodeAgent path. The prompts and answer logic of `sql_agent.py` and of `preparation/` do not change.

This plan carries out and extends item 3.10 of [the workflow review](../2026-11-10_workflow_gaps/README.md). Start from [the index](../README.md).

## 1. Situation

Every score since 9 November and the submission entrypoint (`predict.sh`, `506d567`) use **SQL over prepared DuckDB bundles** (`agent_mode: sql`). The older **raw-file CodeAgent** path is still in the code, and most documents still describe it as the current system.

### 1.1 Two answering paths in `cli.py`

| Path | When it runs | Code |
|---|---|---|
| SQL over bundles (measured) | `--prepared` given: `predict.sh`, the runbook commands, the UI (on by default) | `sql_agent.py`, `preparation/` |
| Raw-file CodeAgent (retired) | `--prepared` missing: `make bench`, `make predict`, `make demo`, README examples, UI with the box unchecked | `agent.py`, `executor.py`, `worker.py`, `tools.py`, `memory.py`, about 1,760 lines |

So `make predict` and `make bench` run the retired path, and they default to 4 steps and (for `bench`) 20 questions. These are runbook traps 1, 2 and 4.

### 1.2 Tests (run on 10 November at `155f323`, synthetic data only)

| Suite | Run by `make test` | Result |
|---|---|---|
| `tests/rewrite/` | yes | 84 run, OK, 32 skipped (bubblewrap tests need `make test-sandbox`) |
| `tests/preparation/` | yes | 175 run, **1 failure**: `test_safety.py:53`, `test_compatible_partition_scales_and_missing_optional_field`, `2 != 1` for `COUNT(customer_name)`. Failing since `d8d0352` |
| `tests/test_*.py` (6 files) and `tests/native_support.py` | no | **all 6 fail**. Five import modules that no longer exist (`dashboard`, `runner`, `splits`, `trace`, `runtime`, `evidence`, `data`, `helper_contracts`, `data_tools`, `NativeAgent`). `test_streamlit_results.py` imports, then fails (`KeyError: 'Total'`; expected message missing) |

`tests/rewrite/` mostly tests the retired path: `test_isolation`, `test_fast_path`, `test_memory` and parts of `test_rewrite`.

### 1.3 Outdated documents

| File | What is outdated |
|---|---|
| `README.md` | Line 3: "A single `smolagents.CodeAgent` answers…". Line 9: preparation "does not change the current agent". Lines 48–49, 87, 139: port 8000, no `--prepared`. Line 90: "Every UI and CLI run uses the single agent … at most four steps". Lines 92–96: 8 November evidence from one raw-file question. Lines 98–124 (memory, worker, helpers) and 146–156 (structure) describe the raw-file path only; `sql_agent.py`, `preparation/`, `predict.sh` and `scripts/` are missing from the structure |
| `docs/diagrams/01`–`05` and `README.md` there | All describe the CodeAgent, preview worker and bubblewrap worker; 02 says "at most four steps". None shows preparation, bundles, schema prompt, query checks or answer checks |
| `docs/data-preparation.md:84–90` | One `artifacts/prepared` folder; `--max-steps 6` without `--temperature 0` or `--limit 0`; no pointer to the runbook |
| `docs/fix/README.md` | "The submission entrypoint, which does not yet run the measured system" (done in `506d567`). "gate100 has not been drawn yet" becomes outdated once the gate record lands (in progress, untracked) |
| `docs/fix/RUNBOOK.md` trap 4 | Stays true until step 3 below is done; update it then |
| `docs/fix/2026-11-08_order_count_policy/README.md` | Header still says "proposed"; the index marks it superseded but the record itself does not |
| `docs/fix/2026-11-10_workflow_gaps/README.md` 3.1 | Describes `predict.sh` before `506d567`; needs a "done" note |
| `.env.example` | `BASE_URL` on port 8000, `MODEL=your-served-model` |
| `pyproject.toml` | Description: "native code actions and isolated execution" |
| `Makefile` help | "Run the new agent", "Rewrite contract and scripted agent checks" |
| `scripts/serve-local.sh` | Port default 8000; the Makefile and `predict.sh` override it to 18234. `bench-local.sh` repeats the server flags instead of calling `serve-local.sh` |

## 2. Decisions proposed

### 2.1 Remove the raw-file CodeAgent path

**Proposal.** Delete `agent.py`, `executor.py`, `worker.py`, `tools.py`, `memory.py` and `demo.py`, the raw-file branch of `cli.py` (source preview, preview cache, worker timing fields), the UI's "uncheck for the raw-file agent" option, `make demo` and `make test-sandbox`, and the tests that only cover them. `--prepared` becomes required for `bench` and `predict`.

**Why.** Nothing measured or submitted uses it. It is about a third of the package, it makes `make predict` run a system nobody measures, and it is what the documents describe to a new session. Git keeps it (last commit with it: the one before the removal).

**Rejected.**

- *Keep it and label it legacy.* The traps stay, and every document needs two descriptions.
- *Keep `tools.py` helpers for future use.* Nothing in the SQL path imports them; lesson 5 puts deterministic rules in SQL relations and code checks instead.

**Kept.** `model.py` (the SQL path uses its `OpenAIModel` subclasses and `smolagents.ChatMessage`), `contracts.py`, `evaluate.py`, `telemetry.py` and `phoenix_local.py` (`sql_agent.py` uses `question_span`). Dependencies do not change in this step; `smolagents` is still imported.

**Risk.** `cli.py` is on the measured path. The SQL branch's call and arguments must stay exactly as they are. Check: re-run sample10 from a pinned worktree and compare every model reply with `runs/sample10-r2s2b` (the runbook's `e1` check matched all 21 steps).

**Needs the team's agreement** before step 4.

### 2.2 Delete the dead top-level tests; repair the UI test

Delete `tests/test_dashboard.py`, `test_helper_contracts.py`, `test_helper_feedback.py`, `test_native_isolation.py`, `test_native_unit.py` and `tests/native_support.py`. They test modules removed before 9 November and cannot be repaired without them. Fix `tests/test_streamlit_results.py` against the current UI and move it into a suite that `make test` runs.

### 2.3 Investigate the old `test_safety` failure before changing it

`COUNT(customer_name)` is 2 where the test expects 1. The likely cause is the grouped-rows rule (blank order-level cells inherit the first row of their group), added after the test was written. Decide from the rulebook whether 2 is correct. Then fix the test with that reason, or fix preparation. Do not mark it as an expected failure without a cause. If preparation changes, it needs a new bundle generation and the comparison of runbook section 2.

### 2.4 Rewrite the documents for the SQL path, not around it

Describe one system: `predict.sh` → preparation → bundles → SQL answering → typed answer. Point to the runbook for measurement settings and to the index for scores, instead of copying numbers into the README.

## 3. Steps

Each step is one commit. Steps 1–3 do not touch answering code and can be done at any time. Step 4 needs agreement (2.1) and should not start in the middle of a measurement round.

**0. Before starting.** Let the gate100 work (`docs/fix/2026-11-10_gate_and_work_samples/`, `scripts/draw-samples.py`, `question_sets.json`) be committed by whoever owns it. Do not edit those files or the index's "Goal now" section in this plan.

**1. Documents that are wrong today** (documents only)

- `docs/fix/README.md`: remove "does not yet run the measured system"; add this plan to the records table.
- `2026-11-08_order_count_policy/README.md`: status "superseded by SQL answering (9 November); not implemented".
- `2026-11-10_workflow_gaps/README.md` 3.1 and 3.10: add done/in-progress notes with commits.
- `docs/data-preparation.md:84–90`: use the benchmark settings and per-group folders, and link runbook sections 1, 2 and 4.
- `.env.example`: port 18234, `MODEL=local-coder-7b`.

**2. Tests** (no runtime change)

- Delete the six files of 2.2; fix and move `test_streamlit_results.py`.
- Resolve `test_safety.py:53` as in 2.3.
- Result wanted: `make test` passes with no failures, and no test file is left that no target runs.

**3. Makefile and scripts match the measured settings** (no answering change)

- `make bench` and `make predict` pass `--prepared`, `--max-steps 6` and `--temperature 0` to the CLI; `make bench` defaults to `LIMIT=0`. Consider `make predict` calling `predict.sh`.
- CLI defaults: `--max-steps 6`, so a forgotten flag gives the measured setting.
- `scripts/serve-local.sh` defaults to port 18234; `bench-local.sh` starts the server through `serve-local.sh`, so the server flags live in one file.
- Update the Makefile help text and runbook traps 1, 2 and 4.
- Check: runbook section 4 on sample10 gives the same manifest fields as `runs/sample10-r2s2b` (except revision).

**4. Remove the raw-file path** (after agreement)

- As in 2.1. Also remove the `worker_start_seconds`, `file_read_seconds`, `cache_hits` and preview fields from the measurements, and `bubblewrap` from the README requirements.
- Rename `tests/rewrite/` to a name for what it now tests (for example `tests/answering/`), keeping the tests of `cli.py`, `contracts.py`, `evaluate.py`, `model.py`, telemetry and UI providers.
- Check:
  - `make check` and `make test` pass.
  - sample10 from a pinned worktree: every model reply identical to `runs/sample10-r2s2b`.
  - `predict.sh` on a small split: it starts, prepares, answers and writes `submission.jsonl` and logs.
  - `grep -rn 'agent\.py\|executor\|worker\|CodeAgent\|source_preview' --include=*.md --include=*.py .` (outside `docs/fix/` history) finds nothing that describes current behaviour.

**5. README and diagrams** (documents only, after step 4 so they describe the final code)

- `README.md`: what the system is (SQL over bundles); quick start (`make setup`, `make prepare`, `make serve`, `make ui`); `predict.sh`; measurement → runbook; scores → index; OpenAI provider (development only, not offline); structure listing `sql_agent.py`, `preparation/`, `scripts/`; checks. Remove sections 98–124 and the 8 November evidence.
- Diagrams: replace 01–05 with three: system overview (`predict.sh`, preparation, server, answering, outputs); one question (schema prompt, date block, model, `check_query`, `run_query`, answer checks, abstain cases); runs and scoring (kept, with the manifest fields of runbook section 1).
- `pyproject.toml` description.
- Update the index's records table and backlog.

## 4. What is not in this plan

- Any change to prompts, query patterns, relations or answer checks.
- `project-overview.md` (transcribed from the source PDF).
- Removing `arize-phoenix` or `smolagents` from the dependencies. Possible later; `model.py` and telemetry would need replacing first.

## 5. Not verified

Sections 1 to 4 are the plan as written before the work. What was checked, and what still is not, is in section 6.

## 6. Results

### 6.1 What each commit did

| Step | Commit | Done |
|---|---|---|
| 1 | `37ee7e9` | This plan; the index no longer says the entrypoint runs another system; the 8 November plan marked superseded; notes in the workflow review 3.1 and 3.10; the preparation doc no longer calls the SQL executor future work |
| 2 | `a40c07d` | Six dead test files and their scripted model deleted; the UI results test rewritten for the current run view and moved into `make test`; `test_safety` split (2.3, below) |
| 3 | `a19da8b` | CLI defaults: six steps, all selected questions, the server on `LOCAL_PORT` (18234) with `LOCAL_MODEL_ALIAS`. `make bench`, `bench-local` and `predict` take `BUNDLES=` (one folder) and answer with SQL. `scripts/local-model.sh` holds the server settings for `serve-local.sh`, `bench-local.sh` and `predict.sh`; `serve-local.sh` defaults to port 18234. `--backend` removed (6.2). Runbook traps 1, 2 and 4 and the preparation doc updated |
| 4 | `b2657c4` | `agent.py`, `executor.py`, `worker.py`, `tools.py`, `memory.py`, `demo.py` and their tests deleted (2,701 lines removed in the commit). `--prepared` required for `bench` and `predict`; `--exec-timeout`, `make demo` and `make test-sandbox` removed; the always-zero worker timing fields and `retries` dropped from `timings.json`; the manifest also records the DuckDB version. The UI always answers with SQL and shows the final answer of each question. `tests/rewrite/` renamed `tests/answering/` |
| 5 | this commit | README, diagrams (five pages became three: overview, one question, runs and scoring), `AGENTS.md` runner line, preparation doc test line, this section, the index and the runbook's tag table |

### 6.2 Deviations from the plan, with reasons

- **`--backend` removed, and local runs no longer read `MODEL` or `BASE_URL`** (step 3, not planned). Importing `smolagents` calls `load_dotenv()`, which copies the local `.env` into the environment. On the team laptop `.env` holds `BACKEND=ollama` and `MODEL=gpt-4o-mini`, so a run without `--base-url` went to port 11434, where nothing listens, and a run without `--model` would have recorded an OpenAI model name in its manifest. The measured runs always passed both flags, so no recorded result is affected. Local defaults now come only from the `LOCAL_*` variables that the scripts also use. A test covers it. `predict.sh` still honours `BASE_URL` and `MODEL` from the shell, as documented.
- **`test_safety` (2.3).** The test never passed: `parse_value` keeps text exactly as written, so a blank text cell is published as `''`, not NULL. In the r2 bundles 50 `clean` text columns hold such cells and no `semantic` relation does. Changing it changes the bundles, so it was not fixed here: the partition-scaling half of the test now passes on its own, and the blank-text half is an expected failure that points to backlog B17.
- **`normalize_final_json` kept** (step 4). `LocalModel.generate`, which the SQL path uses, still rewrites a reply ending in `Final answer: ```json …``` ` into a CodeAgent `final_answer(...)` action. The SQL path cannot read that and asks the model to reply again, which costs a step. It fired once in the recorded SQL runs (`unseen48-205dee8`, at temperature 0.8). Removing it changes answering, so it is backlog B18.
- **`make bench` defaults to `LIMIT=0` and needs `BUNDLES`.** A bare `make bench` now stops with a message instead of sampling 20 questions over the raw files.

### 6.3 Checks

- **Replay of recorded replies (no model server).** A replay model returned the recorded model replies of `runs/<set>-r2s2b` through `cli.run` and recorded every prompt it received. The unchanged code (`155f323`) reproduced each recorded submission exactly, and two replays of it were identical. After steps 3 and 4, all five open frozen sets (sample10, setA, setB, types16, unseen48: 154 questions, 345 model calls) gave **byte-identical prompts, logs, submissions and scores**. The comparison ignored timing fields, and the fields step 4 removed, which were zero in every baseline row. holdout48 was not replayed: it is closed. The replay scripts are not kept in the repository.
- **Live run.** sample10 at `b2657c4`, run as in runbook sections 3 and 4 through the new `bench-local.sh` (tag `c1`, `runs/sample10-c1`): 21 of 21 model replies identical to `runs/sample10-r2s2b`, the same submission, 10 / 0 / 0, PA +1.00, median 6.2 s. The manifest shows `dirty: false`, `agent_mode: sql`, temperature 0, 6 steps, port 18234 and the recorded weight digest.
- **Server arguments.** With a stand-in `llama-server` that records its arguments, `serve-local.sh`, `bench-local.sh` and `predict.sh` passed identical arguments before (`155f323`) and after step 3.
- **Entrypoint.** Old and new `predict.sh`, against a stand-in server on a one-business synthetic split, both exited 0 with the same submission and question log. Their manifests differ only in the paths, the revision and the added DuckDB version.
- **Tests.** `make test`: `tests/answering` 36 run, OK; `tests/preparation` 176 run, OK with one expected failure (B17). `make check` passes. `uv sync --locked` passes after the `pyproject.toml` description change.

### 6.4 Not verified

- The full public dry run (backlog B6) has still not been run; the entrypoint check above used a stand-in server and one invented business.
- Whether anyone used `make demo` or the raw-file UI option. Both are gone; the code is in Git history before `b2657c4`.
- Real-model equality was checked live on sample10 only. The other four sets were checked by replay, which shows the prompts are identical; with temperature 0 and a fresh server, identical prompts gave identical replies on every earlier repeat (runbook section 9, `e1`).
