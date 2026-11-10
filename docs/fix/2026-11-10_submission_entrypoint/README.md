# Decision record: the submission entrypoint runs the measured system

Date: 10 November 2026
Status: code done in `506d567` and checked (section 3). The full public-test dry run was started and stopped on request. It is to be run with [the runbook](../RUNBOOK.md), section 11, and summarized with `scripts/dry-run-report.py`.
Branch: `fix/sql-answering-accuracy`, starting from `dcd3ab0` (answering and preparation code as of `5e1507b`).
Scope: `predict.sh`, the last event of each question's log (`tabular_agent/cli.py`), and the README section on the entrypoint. The answering and preparation code did not change.

This is the first addition proposed by [the workflow review](../2026-11-10_workflow_gaps/README.md) (3.1 and 3.2). Business ids appear only as evidence. The public test has no reference answers, so nothing below is an accuracy figure for it. Public questions and answers were not read: the dry run is summarized by counts per business and by abstention reasons that the code writes.

## 1. Situation

- **The entrypoint ran a different system.** `predict.sh` ran `cli predict` without `--prepared` and without `--max-steps`, that is, the old raw-file agent with at most 4 steps. Every score since 9 November comes from SQL answering over prepared bundles with 6 steps at temperature 0. That raw-file agent scored −0.10 on sample10, where SQL answering now scores +1.00 (SQL record, section 1).
- **Bundles existed only for known businesses.** They were prepared ahead of time into `artifacts/`. The private test brings new businesses that are released only at run time, and `/data` is read-only.
- **The logs lacked the final answer.** The log contract asks for each question's id, final answer and elapsed time (`project-overview.md`). Each question's log ended with the id, the elapsed time and the answer type, but not the answer itself, and the model's last reply can differ from the submitted answer after the code normalizes it.
- **The public test had never been run end to end on the SQL path.** Total time, abstentions per business and the validity of the output were unknown.

## 2. Decisions

### 2.1 `predict.sh` starts the server, prepares the data, and answers with SQL

**Decision.** In order, `predict.sh`:

1. refuses an occupied port, then starts the local model server through `scripts/serve-local.sh`, which holds the benchmark server settings;
2. prepares every business in `DATA_DIR` into bundles under `WORK_DIR` while the model loads;
3. waits for the server;
4. answers every question with `--prepared`, temperature 0, seed 42 and at most 6 steps, passing the weight digest and server settings to the manifest as `bench-local.sh` does;
5. stops the server on exit.

`BASE_URL` and `MODEL` select an already running server instead.

**Why, in this situation.** It is the only path with measured accuracy. The settings are the measured ones, and the server flags come from the same script the benchmarks use, so the two cannot drift apart.

**Rejected.**

- Calling `bench-local.sh`: it runs `bench`, which needs reference answers.
- Making `make predict` the entrypoint: the organizers run `predict.sh`.
- Preparing bundles ahead of time: impossible for the private test.

**Risk.** The server binary, the device (`Vulkan1`) and the weights are the team laptop's. A container for 2 × RTX 5090 needs a CUDA build of the server and its own device setting (`LOCAL_DEVICE`); that packaging is not done.

### 2.2 Prepare at run time; a blocked business abstains

**Decision.** Preparation runs inside `predict.sh`, at the same time as the model load. Its exit status 1 (some business blocked) does not stop the run: questions of a business without a bundle are answered `abstain` by the existing CLI rule.

**Why, in this situation.** A wrong answer costs −1 and an abstention 0. The raw-file agent, the only other path for a blocked business, scored −0.10 on sample10 (1 wrong, 9 abstentions), so abstaining is the better default. Preparation reads no questions or answers.

**Rejected.** Falling back to the raw-file agent for a blocked business.

### 2.3 Keep the whole run inside the time budget

**Decision.** Answering gets `--run-timeout` = `RUN_SECONDS` (default 21,600) − the time already spent − `STOP_MARGIN_SECONDS` (default 300).

**Why, in this situation.** The reference profile's 6 hours include preparation and model loading. Questions not answered when time runs out count as abstentions, but answers already written are scored; the CLI writes each answer as soon as it is made. Stopping a margin early keeps a run from being cut off mid-write.

### 2.4 The final answer in each question's log

**Decision.** The last event of each question's log (`question_timing`) also holds the final answer. Nothing else reads that event; `timings.json` is unchanged.

**Why, in this situation.** The log contract. In the smoke test (section 3), the answer in each log equals the row in `submission.jsonl`.

### 2.5 Hash the weights while preparing

**Decision.** The weight digest for the manifest is computed in the background during preparation.

**Why, in this situation.** In the first smoke test, hashing the 4.7 GB file before preparation took 48 s, and the whole three-question run took 78 s. With hashing alongside preparation, the run took 33 s (with the file already cached, so part of the gain is the cache).

## 3. Checks before the dry run

- **Smoke test, real model, training data.** A three-question split (the biz_104 questions of sample10, an open set) was copied into a scratch folder, with its references kept outside the split. `predict.sh` started the server, prepared the business, answered 3 of 3 correctly (internal scorer), wrote the logs, and stopped the server. The manifest shows SQL mode, temperature 0, 6 steps and the weight digest. The freshly prepared biz_104 bundle has the same `schema.json` as `prepared-r2-dev`.
- **Tests.** `tests/rewrite`: 84 run, all pass (32 skipped: sandbox tests). `tests/preparation`: 175 run, only the failure that predates this work (`test_compatible_partition_scales_and_missing_optional_field`).
- **Answering unchanged.** sample10 at `506d567`, from a pinned worktree on a fresh server with the [runbook](../RUNBOOK.md)'s section 4 commands: identical model replies at all 21 steps to `sample10-r2s2b`; 10 / 0 / 0. This is also the runbook's first full use.

## 4. Public-test dry run

**First attempt (`public-dry-e1`, stopped on request).** It ran from a worktree pinned at `506d567`.

| Phase | Result |
|---|---|
| Model loading | Done within the preparation time |
| Preparation of all 14 public businesses | 386 s, exit status 0: no business blocked. The same total as the earlier public preparation (388 s) |
| Answering | 49 of 1,000 questions, then stopped |
| Clean-up | `predict.sh` stopped the server itself and copied its phase times and preparation log into the run's logs |

Two things are already visible:

- **Run-time preparation reproduces the bundles.** All 14 bundles prepared at run time have the same bundle id and the same `schema.json` as `prepared-r2-public`.
- **dev_006 runs out of context.** It abstained on 6 of its first 8 questions: 4 requests over the 16,384-token context (16,385 to 16,879 tokens), and 2 replies cut off at the length limit because the prompt left too little room. This is backlog item B1. It is an early sign from 8 questions, not a rate.

The partial folders `runs/public-dry-e1` and `artifacts/prepared-dry-e1` (705 MB of bundles) are kept. A full run must use new folder names; reusing the work folder would reuse its bundles and hide the preparation time.

**Still to measure, with the full run:**

- total time;
- answered and abstained per business, and why;
- server errors, such as requests over the context size in dev_006;
- whether the bundles prepared at run time match `prepared-r2-public`;
- the projected time for about 500 private questions.

## 5. Not verified

- No public-test accuracy: the public test has no reference answers, and the submission was not uploaded.
- No container, no reference hardware, no CUDA build of the server. The time figures are the team laptop's (RTX 4050 Laptop GPU, 6 GB), with the model file already in the page cache.
- The private test brings business types absent from training and the public test; how many of them preparation blocks is unknown.
