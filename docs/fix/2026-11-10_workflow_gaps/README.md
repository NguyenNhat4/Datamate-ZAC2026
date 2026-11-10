# Review: what the fix workflow still lacks

Date: 10 November 2026
Status: review only; no code changed. Sections 4 and 5 propose additions; none is done except the three documents named in section 2.
Branch: `fix/sql-answering-accuracy` at `c732359` (answering and preparation code as of `5e1507b`).
Scope: the records in `docs/fix/`, the run folders, `scripts/`, `Makefile`, `predict.sh`, and how rounds are measured. No model was run.

Start with [the index](../README.md). The measurement commands are in [the runbook](../RUNBOOK.md).

## 1. Situation

**Goal (set 10 November).** Reach PA ≥ +0.80 on a sample of 100 questions the system was not tuned on, before running the whole public test.

- PA (penalized accuracy) = (correct − wrong) / questions. On 100 questions the target is correct − wrong ≥ 80, for example 85 / 5 / 10.
- Today (code `5e1507b`), the closed sample holdout48 is 37 / 6 / 5, PA +0.646 (77% correct). The gap is about 15 points of PA.
- A 100-question PA has a standard error of about 0.07 at today's mix and about 0.05 at the target mix. A reading of +0.80 therefore means roughly +0.70 to +0.90. Runs are deterministic, so two commits can still be compared question by question on the same sample. The total alone cannot show a change of two or three questions.

**Terms.**

- **Work set (open).** We read its failures and choose fixes from them. After that its score is optimistic, because the fixes were made for those questions.
- **Closed gate.** A sample whose questions, answers and logs nobody reads; only its total (correct, wrong, abstain, PA) is looked at. It is the pass/fail check before the public-test run. No fix was chosen by looking inside it, so its score is an honest estimate for questions the system was not tuned on. holdout48 has been used this way so far.

**What was reviewed.**

- The five fix records and both PROGRESS files.
- `question_sets.json`.
- The manifests of all fix-round runs (holdout runs by name and revision only).
- `scripts/`, `Makefile`, `predict.sh`, `tabular_agent/cli.py`, README and diagrams.
- The team agent's memory notes.
- The Claude Code transcript of the session that ran the 10 November rounds. It was read to recover the commands that session used; its scripts were kept only in a scratch folder that is now empty.

## 2. Summary

Priority 1 holds back the goal or the public run directly. "Done" marks what this review added: [the index](../README.md) and [the runbook](../RUNBOOK.md).

| # | Gap | Why it matters for the goal | Proposed addition | Size | Priority |
|---|---|---|---|---|---|
| 3.1 | The submission entrypoint does not run the system that is measured | A public or private run would not get the measured accuracy | `predict.sh` prepares `/data`, then answers with SQL and the benchmark settings; one full public dry run | M | 1 |
| 3.2 | The gate cannot see the public test's weakest businesses | +0.80 on the gate is compatible with about +0.4 on the public test | Status table of the 14 public businesses; abstentions per business from the dry run | S | 1 |
| 3.3 | Goal, gate rule and dates were not written down; no upload log | Each session re-derives them | Index with goal, gate and dates (done); upload log (table ready, empty) | S | 1 |
| 3.4 | No current failure list; backlog in four records; no table of question kinds | Fixes are chosen from the last one or two failures seen | Failure table per round; one backlog (done); question-kind table with counts | M | 2 |
| 3.5 | The measurement recipe was prose plus private notes | Slow start; easy mistakes (20-question cut, 4 steps, wrong bundles, silent abstentions) | Runbook (done); one script that runs a round | S | 1 |
| 3.6 | Every round rebuilds its checking scripts and loses them | Each round pays for them again | Recover the recurring ones into `scripts/` | M | 2 |
| 3.7 | Sample bookkeeping is incomplete | New samples cannot exclude all used ids; the closed sample wears out | Seed, rule and status per set; evidence ids per record; retirement rule; gate100 and work100 | S | 1 |
| 3.8 | The model is not chosen, but every fix is tuned to one 7B model | Prompt-level gains may not carry over | Decide the model before more prompt work | M | 2 |
| 3.9 | The internal scorer has never been checked | The internal PA may differ from the official one | Check `evaluate.py` against the matching rules | S | 3 |
| 3.10 | Stale documents and dead tests | They describe the old raw-file agent to a new session | Refresh README, diagrams, data-preparation; mark superseded plans (done in the index); remove dead tests | M | 3 |
| 3.11 | Lessons are spread over the records; records have no fixed shape | The same mistake costs a round again | Lessons and record checklist in the index (done) | S | done |

## 3. Gaps in detail

### 3.1 The submission entrypoint does not run what is measured

**Done since this review.** `predict.sh` prepares the split and answers with SQL and the benchmark settings (`506d567`, [entrypoint record](../2026-11-10_submission_entrypoint/README.md)). The full public dry run is still to do. The evidence below describes the code before that commit.

**Missing.** `predict.sh` runs `cli predict --split /data --output /result` with no `--prepared` and no `--max-steps`. It therefore runs the old raw-file agent with at most 4 steps. Every score since 9 November comes from SQL answering (`agent_mode: sql` in every manifest) with `--prepared … --max-steps 6 --temperature 0`. `make predict` and the README examples still use port 8000; the benchmarks use 18234.

**Also missing:**

- **How preparation fits the run.** Bundles are prepared in advance into `artifacts/`, which Git ignores. The private test brings businesses that only appear at run time. `/data` is read-only, and preparation counts inside the 6-hour budget. No record says where the bundles go at run time or how long preparation takes per business.
- **A dry run of the full public test on the SQL path.** Its total time, abstentions per business, output validity and log fields have never been measured.

**Proposed.**

- `predict.sh` prepares the split into a writable folder, then predicts with `--prepared` and the benchmark settings.
- One full public-test dry run on the team laptop, recorded in a new decision record.
- Fixed settings also serve the profile's re-run rule (the organizers' re-run must be within 0.03 PA of the upload): temperature 0 and a fresh server.

### 3.2 The gate cannot see the public test's weakest businesses

**Evidence.** In the current public bundles (`artifacts/prepared-r2-public`), 7 of 14 businesses publish no `semantic.eligible_sales_lines`: dev_003, 007, 009, 011, 012, 013 and 014. They hold 420 of the 1,000 public questions, and their sales questions are refused by design and abstain. dev_006 (111 questions) is at the 16,384-token context: 12 of its questions are over it at the first step ([rankings record](../2026-11-10_rankings_and_patterns/README.md), section 5). Training has one business without rulebook-ready sales lines (biz_110, 40 questions).

**Illustration, not a measurement.** Suppose those 531 questions scored 0 and the other 469 scored +0.80: public PA would be about +0.38. If only the 420 scored 0, it would be about +0.46. Stock, purchase and catalogue questions in those businesses remain answerable, so the real figure is somewhere else. A gate drawn from training questions cannot show it either way.

**Proposed.**

- A status table of the 14 public businesses: questions, sales lines ready or not, blocker, prompt tokens at step 1, and abstentions in the dry run.
- Abstention counts come from the dry run's own submission, so no reference answer is needed.
- Report public readiness as a second number next to the gate.

### 3.3 Goal, gate rule and dates

**Evidence.**

- The target existed only in a conversation.
- The dates that bound the work are only in `project-overview.md`, transcribed from the PDF and not re-checked:
  - public test until 16 November, with 5 uploads per day;
  - complete solution due 16 November 23:59;
  - private data released 20 November.
- No upload log exists, although the public leaderboard is the only outside check of the work.

**Done.** [The index](../README.md) states the goal, the gate rule, what the gate does not cover, and the dates. It also holds an empty upload log.

### 3.4 Failures, backlog and question kinds

**Evidence.**

- At `5e1507b` the open sets hold 30 failures:

  | Set | Failures | Wrong | Abstain |
  |---|---|---|---|
  | setA | 7 | 4 | 3 |
  | setB | 12 | 4 | 8 |
  | unseen48 | 11 | 5 | 6 |

- setA and setB failures were last described on 9 November, as a prose list ([date hints record](../2026-11-09_date_hints_and_answer_checks/README.md), section 4).
- The six unseen48 questions fixed on 10 November are described; the 11 that remain are not.
- Open items were spread over four records. They are now in one table in [the index](../README.md).
- **Questions carry no kind label.** `answers.jsonl` gives only the answer type: number 1,884, percent 578, list 193, date 145, string 88, unanswerable 70, boolean 23, table 19. Word-list detectors exist for five kinds only: `compares_periods`, `asks_shift`, `ranks_customers`, `counts_days_over_amount` and `asks_unsold` in `tabular_agent/sql_agent.py`.
- How many training questions a failure type affects was counted only once, for four kinds ([SQL record](../2026-11-09_sql_answering_accuracy/README.md) 3.11). Fixes are therefore chosen from the last one or two failures seen in a 48-question sample.

**Proposed.**

1. End every round with a failure table for the open sets: id, business, answer type, kind, outcome, one-line cause, backlog item.
2. Keep a question-kind table over the training questions outside the closed samples: kind, count, the relation or pattern that covers it, and its accuracy on the work set. Choose the next fix by questions affected × failure rate.

### 3.5 The measurement recipe

**Evidence.**

- Decision 5.3 of the [unseen businesses record](../2026-11-10_unseen_businesses/README.md) describes the method only in prose.
- The commands lived elsewhere: in scratch scripts of one session, now lost, and in the agent's private memory notes, which teammates and their agents cannot read.
- Traps that no document named:
  - `make bench` keeps only 20 questions unless `LIMIT=0`.
  - `--max-steps` defaults to 4; the SQL runs used 6.
  - `--prepared` takes one folder, and the bundles are split by business group (`-dev`, `-unseen`). A question whose business has no bundle in that folder is answered `abstain` without an error (`tabular_agent/cli.py:352`). A sample spread over all 30 businesses therefore needs one folder that holds all of them.
  - There is no flag to read question ids from a file.
  - A worktree needs `data`, `artifacts`, `runs`, `models` and `.venv-native` linked in and excluded from Git; otherwise the manifest reports a dirty tree.
- The results tables do not name run folders. A tag such as `r2s2b` can only be decoded from each `manifest.json`.

**Done.** [The runbook](../RUNBOOK.md): the commands, the traps, and an index of run tags.

**Still missing.** One script that runs a list of sets for a commit, each on a fresh server from a pinned worktree, and prints the results row.

### 3.6 Checking scripts are rebuilt every round and lost

**Evidence.** `scripts/` holds only `serve-local.sh` and `bench-local.sh`. The 10 November rounds wrote 20 helper files in the session's scratch folder, which is now empty.

| Helper | What it did |
|---|---|
| `run_bench.sh`, `run_bench2.sh`, `run_round.sh` | Run frozen sets, each on a fresh server, from a pinned worktree |
| `compare.py` | Per-question outcomes of two runs |
| `same_replies.py` | Model replies of two runs, compared step by step |
| `compare_bundles.py` | Two preparation outputs compared: data, issues, schema text |
| `replay_unready.py` | Logged queries replayed against a new refusal rule |
| `check.py` | Period functions checked against training references |
| `coverage.py` | Which rulebook-ready relations each bundle publishes |
| `dump_schema_text.py`, `prompt_text.py` | Schema text and a hash of the full first prompt per bundle (byte-identity checks) |
| `prompt_tokens.py`, `token_sides.sh` | First-prompt token counts per bundle |
| `kinds.py` | Question-kind detection over training questions |
| `temp_check.py`, `temp_check2.py`, `bench_nocache.py` | Server determinism checks |
| `apply.py`, `bmap.py`, `explore.py` | One-off helpers |

The checks against references for the 10 November patterns (71, 20, 33 questions) were scripts written by sub-agents and are lost too. An older `scripts/compare-runs.py` was deleted in `e959614` and targets the old run format.

**Where their text survives.** Only in the transcript of that session (id starting `67fffb96`, under `~/.claude/projects/` on the team laptop). That is a local file, and the tool may delete old transcripts.

**Proposed.** Recover the recurring helpers into `scripts/`, each with a small test on invented data, in this order:

1. round runner
2. run comparison (outcomes, then replies step by step)
3. failure table
4. sample draw
5. prompt identity and token count
6. bundle comparison
7. pattern check against references
8. log replay

### 3.7 Sample bookkeeping

**Evidence.**

- **`question_sets.json` holds only id lists.** It has 201 distinct ids in six sets (one id is in two sets), and no seed, selection rule or open/closed status.
- **Validation reads were not recorded.** Reference answers of more training questions were read to validate relations and patterns, and their ids were not kept:
  - 87 in the SQL record (3.11);
  - 108 in the unseen record (5.5);
  - 124 in the rankings record (3.2: 71 + 20 + 33).

  The kind detectors can regenerate the rankings-record questions; the others cannot be regenerated.
- **holdout48 has been read 7 times.** Its totals were read in runs `holdout48-t0`, `-s1`, `-s1b`, `-s2`, `-r2s1`, `-r2s2` and `-r2s2b`. Its businesses also supplied the validation questions of 10 November, so it now estimates new questions from known businesses (rankings record, section 4). No rule says when a closed sample is retired.
- **No unseen training business is left.** All 30 have now been prepared and studied. A new sample can only be "questions not used", not "businesses not seen".

**Proposed.**

1. Store each set with its seed, rule and status.
2. Give each record a list of evidence ids: questions whose reference answers were read outside the frozen sets.
3. Read a closed sample only as totals, once, after a round's final commit. Retire it after about 10 reads, or as soon as a choice between changes was made on its totals.
4. Draw gate100 and work100 (section 4).

### 3.8 The model is not chosen

**Evidence.**

- `AGENTS.md`: no model has been selected.
- Every SQL-path run used Qwen2.5-Coder-7B-Instruct at Q4_K_M with a 16,384 context on the team laptop (RTX 4050 Laptop GPU, 6 GB).
- The only comparison runs (`runs/model-comparison-*`, 31 October) tried a 3B and a 4B model on the old raw-file agent, 3 to 5 questions each: one wrong answer, all others abstained, and no record.
- The reference profile allows 15 billion inference parameters on 2 × RTX 5090 (64 GB) and 6 hours for about 500 questions, about 43 s per question.
- The records show that the 7B model moves unrelated answers on small shared prompt edits (rankings record 3.2), so prompt tuning is specific to this model.

**Proposed.**

- Decide which model the remaining rounds are tuned for before more prompt work.
- One larger model within the profile, run on work100 with the same code, would show whether a switch beats further prompt tuning.
- The laptop GPU cannot hold a 14B model at Q4 entirely, so that measurement needs CPU offload (slow) or other hardware.
- Record the weight source, licence, size, quantization and context (`AGENTS.md`).

### 3.9 The internal scorer

**Evidence.**

- Every record says that the internal scorer may differ from the official one.
- `tabular_agent/evaluate.py` labels itself `internal-not-official`.
- No record has compared its rules with the matching rules in `project-overview.md`.
- Public answers are not available locally, so an upload gives a public PA but cannot check the scorer.

**Proposed.** One pass over the rules against `evaluate.py`, with a test case for each rule: answer types, string normalization, number tolerance, percent rounding, list order, table columns and row sets, and aliases.

### 3.10 Stale documents and tests

**Done since this review.** The [cleanup record](../2026-11-10_cleanup_plan/README.md) carried this out and also removed the raw-file agent (`37ee7e9` to `b2657c4`). The evidence below describes the code before it.

**Evidence.**

- **`README.md`** still describes the raw-file agent:
  - line 9: preparation "does not change the current agent";
  - lines 48–49, 71 and 123: port 8000;
  - line 74: "Every UI and CLI run uses the single agent … at most four steps";
  - line 80: the planned 100-question validation is still outstanding.
- **`docs/diagrams/01`–`05`** describe only the raw-file agent (02:46: "at most four steps").
- **`docs/data-preparation.md:86–87`** shows one `artifacts/prepared` folder and `--max-steps 6` without temperature.
- **`docs/fix/2026-11-08_order_count_policy/`** is still "proposed", for the raw-file path that SQL answering replaced.
- **Tests:**
  - The top-level `tests/test_*.py` import modules that no longer exist: `dashboard`, `evidence`, `runner`, `splits`, `trace`, `runtime`, `data` and `helper_contracts`. `make test` does not run them.
  - `tests/preparation/test_safety.py:43` has failed since before 9 November, and nobody owns it.

**Proposed.**

- Refresh the README's run and structure sections, and diagrams 01–05, for the SQL path.
- Mark the 8 November plan as superseded (done in the index).
- Delete or rewrite the dead tests.
- Fix the old failure, or mark it as expected with a reason.

### 3.11 Lessons and the shape of a record

**Evidence.** Rules that each cost a round to learn are spread over five records. Examples: show a pattern only for its question kind; keep other prompts byte-identical; replay logs before adding a refusal; check a query against the references of its type before a model run. The records also differ in what they keep: run folder names, the failure table at the end of a round, and ids used as evidence are each missing from some of them.

**Done.** [The index](../README.md) has a lessons list and a checklist for each round's record.

## 4. Suggested samples

**Pool.** The 2,799 training questions in no earlier set.

**Order matters.** gate100 is drawn first, from ids and business ids only. work100 is then drawn from the rest, using answer types. Drawing in this order keeps the gate's composition from depending on anything read from its answers.

### gate100 (closed)

- Seeded draw spread over all 30 businesses: 100 questions split in proportion to each business's remaining questions (largest remainder), with at least 2 per business.
- No question text, answer or answer type is read to choose it. Only totals are ever read, once per round, after the round's final commit.
- It estimates PA on new questions from known businesses at the current code. It does not estimate the public test (3.2).
- **Validation kinds.** Some gate questions will be of kinds whose references were read to validate a relation or pattern (change, shift, top products, group share, customer rankings, day counts, unsold products). Excluding them would bias the mix, so keep them. If needed, report the total without them, separated by the kind detectors without anyone reading the questions.

### work100 (open)

- Drawn after gate100, from the rest, with the same spread by business.
- Then some questions are swapped so that rare answer types have enough cases to learn from:
  - at least 6 each of list, date, string and unanswerable;
  - at least 4 each of boolean and table (natural rates give 0 to 6 per 100);
  - at least 6 questions from biz_110, the only training business like the public businesses without sales lines.
- Its PA is not an estimate. It is for reading failures and comparing commits question by question.

### Both samples

- Both span all 30 training businesses, so they need one bundle folder that holds all 30. Either prepare the training split into one folder and compare it with `prepared-r2-dev` and `-unseen` before use, or link both folders' bundles and copy their `run-*.json` reports into one folder. Otherwise questions from the missing group abstain silently (3.5).
- Store both id lists in `question_sets.json` with the seed and the rule.
- holdout48 stays closed, keeping its trend line, until it is retired.

## 5. Proposed additions, in order

| Order | Addition | Gaps | Size |
|---|---|---|---|
| 1 | `predict.sh` on the SQL path, with preparation of `/data`; full public dry run; time and abstentions per business recorded | 3.1, 3.2 | M |
| 2 | Draw gate100 and work100; one bundle folder for all 30 training businesses; baselines of both at the current commit | 3.5, 3.7, 4 | S |
| 3 | Recover the round runner, the run comparison and the failure table into `scripts/` | 3.5, 3.6 | M |
| 4 | Model decision, before more prompt-specific work | 3.8 | M |
| 5 | Question-kind table and work100 failure table; choose fixes by size | 3.4 | M |
| 6 | Public layouts and the dev_006 prompt size (backlog in the index) | 3.2 | L |
| 7 | Scorer rule check | 3.9 | S |
| 8 | README, diagrams, data-preparation guide and dead tests | 3.10 | M |

## 6. Not verified

- The runbook's commands come from that session's commands and the run manifests. The following were tested:
  - the worktree set-up and clean-up, on a test worktree at `5e1507b`;
  - the id list and flags, which select all of sample10;
  - the outcome comparison (`unseen48-r2s1` against `unseen48-r2s2b` gives the six changes in the rankings record);
  - the totals-only report.

  The model server and `bench-local.sh` were not run.
- The public-test figure in 3.2 is arithmetic on question counts, not a measurement.
- The lost helper scripts are listed from the transcript and were not re-run.
- The dates come from `project-overview.md` (the PDF converted on 29 October) and were not re-checked against current official rules.
