# Fix rounds: start here

This folder holds one decision record per round of fixes. Each record covers its plan, its decisions with their reasons, and its measurements. Read this page first: it holds the current goal, where the scores stand, the open backlog, and the lessons so far. Then read the records you need. The exact commands for measuring a commit are in [RUNBOOK.md](RUNBOOK.md).

Last updated: 11 November 2026, branch `feat/intent-programs` (code `<<CODE>>`).

## Goal now

**11 November: the public test with no wrong answer.** That was the target stated for the 11 November round. It cannot be guaranteed while the public answers are hidden, and it is not reached: see [the intent record](2026-11-11_intent_layer/README.md), section 5. Recurring question kinds now go through fixed queries before the model; they explain 80.6% of the open training questions with no wrong answer, but only <<PUBLIC_PCT>>% of the public questions, and 5 of the 7 public businesses without sales lines still have none. The numbers below are the earlier PA target, which still decides when to spend public uploads.

**Target:** PA ≥ +0.80 on gate100, a closed sample of 100 training questions, before running the whole public test. The target was set on 10 November.

- **PA** (penalized accuracy) = (correct − wrong) / questions. On 100 questions the target means correct − wrong ≥ 80, for example 85 / 5 / 10.
- **Precision.** The standard error is about 0.05 to 0.07, so a reading of +0.80 means roughly +0.70 to +0.90.
- **gate100 has not been drawn yet.** The proposed design is in [the workflow review](2026-11-10_workflow_gaps/README.md), section 4. Until then, holdout48 is the closed sample. At `5e1507b` it scores +0.646.
- **What the gate does not cover.** Report these next to the gate score:
  - The public businesses without rulebook-ready sales lines: 7 businesses, 420 of the 1,000 public questions.
  - dev_006: 111 questions at the prompt-size limit.
  - The full public dry run through `predict.sh`, which has not been completed yet (B6).

  See the workflow review, 3.1 and 3.2.

**Dates.** These come from `project-overview.md` and were not re-checked against the current official rules.

| What | When |
|---|---|
| Public test uploads | until 16 November, 5 per day |
| Complete solution package | 16 November, 23:59 |
| Private test data released | 20 November, 09:00 |
| Private results upload | 23 November, 23:59 |

**Upload log.** Add one row per public upload.

| Date | Commit | Bundles | Public PA | Internal gate PA at that commit | Note |
|---|---|---|---|---|---|
| | | | | | |

## Where we stand

Code `5e1507b`, bundles `artifacts/prepared-r2-*`, local 7B model on the team laptop, internal scorer (not the official one), training questions. Each cell gives correct / wrong / abstain.

| Set | Status | Result | PA | Run folder |
|---|---|---|---|---|
| sample10 | open | 10 / 0 / 0 | +1.00 | `runs/sample10-r2s2b` |
| setA | open | 33 / 4 / 3 | +0.725 | `runs/setA-r2s2b` |
| setB | open | 28 / 4 / 8 | +0.600 | `runs/setB-r2s2b` |
| types16 | open | 16 / 0 / 0 | +1.00 | `runs/types16-r2s2b` |
| unseen48 | open | 37 / 5 / 6 | +0.667 | `runs/unseen48-r2s2b` |
| holdout48 | closed, totals only | 37 / 6 / 5 | +0.646 | `runs/holdout48-r2s2b` |

## Since 11 November: no-model checks

| Check | Result | Command |
|---|---|---|
| Fixed kinds against the 2,852 open training references | <<CLAIMED>> claimed (<<CLAIMED_PCT>>%), <<WRONG>> wrong | `scripts/oracle-replay.py --prepared artifacts/prepared-r4-train` |
| Claims on the 1,000 public questions | <<PUBLIC_CLAIMED>> (<<PUBLIC_PCT>>%) | `scripts/claim-coverage.py --split data/public_test --prepared artifacts/prepared-r4-public` |
| Bundle invariants, 44 bundles | <<INVARIANTS>> | `scripts/bundle-invariants.py` |

## Question sets

The frozen ids are in [question_sets.json](2026-11-10_unseen_businesses/question_sets.json): 201 distinct ids.

| Set | Questions | Businesses | Status | Note |
|---|---|---|---|---|
| sample10 | 10 | development | open | The first sample; used to find problems since 9 November |
| setA, setB | 40 each | development, 5 per business | open | Diagnosed on 9 November |
| types16 | 16 | development | open | Questions of the four period-function types |
| unseen48 | 48 | the 22 other training businesses | open | Diagnosed on 10 November |
| holdout48 | 48 | the same 22 | closed | Totals only, read 7 times. Its businesses also supplied validation questions on 10 November, so it now estimates new questions from known businesses |
| gate100, work100 | 100 each | all 30 | proposed | Workflow review, section 4 |

- **Development businesses (8):** biz_104, 106, 107, 111, 112, 120, 121, 127.
- **The other 22 training businesses:** biz_101, 102, 103, 105, 108, 109, 110, 113, 114, 115, 116, 117, 118, 119, 122, 123, 124, 125, 126, 128, 129, 130.
- **Bundle folders.** Each folder holds one group: `-dev`, `-unseen` or `-public`. A question whose business is not in the `--prepared` folder abstains silently ([RUNBOOK.md](RUNBOOK.md), traps).

## Records, in reading order

| Date | Record | What it settled | Code | Status |
|---|---|---|---|---|
| 8 Nov | [Order-count policy](2026-11-08_order_count_policy/README.md) | A plan to enforce order eligibility in the raw-file agent | none | Superseded: SQL answering over prepared bundles replaced that path; not implemented |
| 9 Nov | [SQL answering over prepared data](2026-11-09_sql_answering_accuracy/README.md) | SQL over DuckDB bundles; the query guard; rulebook-ready relations; status mapping; period functions | `3667d62`, `c7e0883`, `88657bc`, `56b991a` | Done |
| 9 Nov | [Date hints and answer checks](2026-11-09_date_hints_and_answer_checks/README.md) | Earlier-period hints only for comparisons; replies holding both a query and an answer; percent checks; examples per question kind | `205dee8` | Done |
| 10 Nov | [Businesses the system was not tuned on](2026-11-10_unseen_businesses/README.md), with [PROGRESS](2026-11-10_unseen_businesses/PROGRESS.md) | Measurement basis (temperature 0, fresh server, pinned worktree); the frozen sets and holdout48; refusing sales tables that are not rulebook-ready; preparation fixes A to D | `82ebdb6`, `67d4d2c`, `2eef9af`, `f746981` | Done |
| 10 Nov | [Rankings, ties and query patterns](2026-11-10_rankings_and_patterns/README.md), with [PROGRESS](2026-11-10_rankings_and_patterns/PROGRESS.md) | One DuckDB thread; customers by money; days over an amount; unsold products; byte-identical prompts for other kinds | `c509cba`, `a77671d`, `5e1507b` | Done |
| 10 Nov | [Workflow review](2026-11-10_workflow_gaps/README.md) | What the workflow lacks for the 100-question goal; proposed samples and additions | none | Review; additions proposed |
| 10 Nov | [Submission entrypoint](2026-11-10_submission_entrypoint/README.md) | `predict.sh` prepares the data at run time and answers with SQL and the benchmark settings, within the 6-hour budget; each question's log holds its final answer | `506d567` | Code done and checked; the full public dry run is still to do (runbook section 11) |
| 11 Nov | [Fixed question kinds before the model](2026-11-11_intent_layer/README.md) | A deterministic layer for recurring kinds, checked against the training references with no model; five preparation fixes (cash-on-delivery label, README patterns for monthly files, documented status columns, header aliases, yearly files of one series); the bundle left open that made every model-routed question abstain | `<<CODE>>` | Done; the public score is not known |
| 10 Nov | [Cleanup of the retired path and outdated documents](2026-11-10_cleanup_plan/README.md) | The raw-file CodeAgent path, dead tests and outdated documents removed; `make bench`/`predict` and the CLI defaults use the measured settings; server settings in one file; answers unchanged (replay of five sets, live sample10) | `37ee7e9`, `a40c07d`, `a19da8b`, `b2657c4` | Done |

## Terms

- **Bundle.** The prepared DuckDB database of one business, made by `cli prepare`.
- **Rulebook-ready.** A bundle publishes `semantic.eligible_sales_lines`, the sales lines after the rulebook's order rules.
- **Open set (work set).** A set whose failures are read and used to choose fixes. After that its score is optimistic.
- **Closed sample (gate).** A set whose questions, answers and logs nobody reads; only its totals are. Its score is the honest estimate.
- **Internal scorer.** `tabular_agent/evaluate.py`, labelled `internal-not-official`; it has not been checked against the official rules.

## Open backlog

Gathered on 10 November from the four records with plans. "Affects" gives the question counts the records know of. Mark items done here, and in the record that does them.

### Public-test coverage

| # | Item | Affects | Source |
|---|---|---|---|
| B1 | Prompt size of the largest bundles. In dev_006, 12 questions are over the 16,384-token context at step 1, and the rest have about 220 tokens of headroom. Needs a structural cut, for example not listing clean tables that semantic relations cover | dev_006, 111 public | Rankings 5, 6.1 |
| B2 | Public layouts that still publish no sales lines. **Done on 11 November** for dev_002 and dev_005 (README pattern for monthly files), dev_007 (documented status column) and dev_003 (header aliases, yearly files of one series). Still none: dev_014 and dev_011 (phone-shop ledgers), dev_012 (repair-shop ledger), dev_013 (two-row header, non-product lines, no status column), and dev_009 (invoice headers only) | 63 + 40 + 50 + 57 = 210 public, and 21 (dev_009) | Intent record 2.8, 6 |
| B3 | ~~dev_007's status column "Tiến độ" is parsed as money~~ done on 11 November | dev_007, 60 public | Intent record 2.8 |
| B4 | Shops with invoice headers only: questions that need sales lines still abstain (order revenue is done) | biz_110 (40 training), dev_009 (21 public) | Unseen 4 (Later 4) |
| B5 | A price-only view for sales lines that are not rulebook-ready, so that price-on-a-date questions need not abstain | not counted | Unseen 4, 5.4 |
| B6 | The submission entrypoint and a full public dry run. The entrypoint is done (`506d567`). The dry run is to do: a first attempt stopped after 49 questions; see [the entrypoint record](2026-11-10_submission_entrypoint/README.md) | all public | Workflow review 3.1 |

### Answering

| # | Item | Affects | Source |
|---|---|---|---|
| B7 | Question kinds with no relation or pattern: customers by number of orders, debt by customer, the number of customers who still owe, payment-method revenue as a list, channel and prescription shares, size or base-unit totals across all items, trends ("tăng liên tục"). Not re-checked since 9 November | 1–2 each in the samples | Date hints 4, 2.6 |
| B8 | Product attributes (active ingredient, prescription), purchase prices, lot-based stock, and the price-change effect ("N ngày kể từ") | 1–2 each | Unseen 4 (not planned); date hints 2.6 |
| B9 | Answer checks: a top-k list has k items; refuse an identical failing query; give feedback for dates after the reference date | 1–2 each | Date hints 2.6; unseen 4 |
| B10 | Show the named-product example only when the question names a product | 1–2 | Date hints 2.6 |
| B11 | Percent rescaling: accept only published `*_percent` columns | none observed | Date hints 2.3 |
| B12 | Unsold-product lists come out in name order where the rulebook wants code order | 9 bundles; no training question asks | Rankings 3.2, 6.2 |
| B13 | Day-count pattern: the ">" and "<" forms are untested; the detectors' recall on new phrasings is unknown | unknown | Rankings 3.2 |
| B14 | A WITH-subquery with the same short name as a refused table is not refused | none observed | Unseen 5.4 |

### Preparation

| # | Item | Affects | Source |
|---|---|---|---|
| B15 | biz_124's drug-list sheet is reported as an ambiguous layout; the cause was not reproduced | biz_124 | Unseen 2 |
| B16 | Thousands separators that could be read either way | several | Unseen 3.1 |
| B17 | A blank text cell is published as `''`, not NULL, so `COUNT` and `GROUP BY` on that column count it. In the r2 bundles: 50 `clean` text columns, none in `semantic`. Fixing it needs a new bundle generation and a measurement; `test_safety.py` keeps it as an expected failure | queries over `clean` text columns | Cleanup 2.3 |
| B18 | `LocalModel.generate` (`model.py`) still rewrites a reply ending in `Final answer: ```json …``` ` into a CodeAgent `final_answer(...)` action, which the SQL path cannot read, so the model is asked again and loses a step. Seen once in the recorded SQL runs (`unseen48-205dee8`, temperature 0.8). Removing it changes answering: measure it | rare | Cleanup 6.2 |

| B19 | Dish and topping in one product name ("Trà đào (M) + Trân châu (7.000đ)"): preparation should make the topping a product of its own (quantity from the number before the name, money in the brackets, the dish keeps the rest). Until then the product-level kinds do not answer in such a bundle | dev_002: 299 of 349 product names; 41 public | Intent record 2.7 |
| B20 | Selling price on a date and the price history as a semantic relation (also "price change" without a date in the question) | 108 + 32 open training; not in the public rulebook | Intent record 3.1 |
| B21 | Pharmacy attributes (prescription, active ingredient, lots, expiry) as a semantic relation built from the drug list | 98 open training; dev_005 | Intent record 3.1 |
| B22 | A customer-group role (`khách nhóm sỉ`, `thân thiết`) | 40 open training | Intent record 3.1 |
| B23 | Receipts and suppliers as a semantic relation (purchase total, main supplier by receipt count) | 49 open training; public rulebook adds the main supplier | Intent record 3.1 |
| B24 | Public-rulebook kinds still without a query: average per weekday, main supplier, hardest-working staff, and the industry rules (mother-and-baby, repair shop, phone shop). Each needs a second independent computation and a hand-derived test | public only | Intent record 5 |
| B25 | Publish a rulebook-ready relation for the phone-shop, repair-shop and mother-and-baby layouts, checked with `scripts/bundle-invariants.py` | 210 public | Intent record 5 |

### Measurement and workflow

The additions are listed in order in [the workflow review](2026-11-10_workflow_gaps/README.md), section 5: samples, round runner and run comparison, failure table, question-kind table, model decision, scorer check, and stale documents.

## Lessons so far

1. **The 7B model copies examples, not prose.** Put a fix into a query pattern, not a warning. (Date hints 2.4; unseen 5.4, `2eef9af`.)
2. **Show a pattern only for its question kind.** An example that fits another kind pulls the model toward it. (Date hints 2.4; rankings 3.2.)
3. **Any edit to text that every prompt shares moves unrelated answers.** One pattern-title change moved 5 outcomes. Keep the prompts of other kinds byte-identical, and check this on all 44 bundles. (Rankings 3.2.)
4. **Count prompt tokens with the model's tokenizer before adding text.** The largest bundles are at the context limit. (Rankings 3.2.)
5. **Where a rule is deterministic, apply it in code, not in the prompt.** Examples: dates, order eligibility, number format, confirming `unanswerable`. (SQL 2.4, 2.6, 2.7, 3.6.)
6. **A wrong answer costs −1 and an abstention 0.** Where the data is not rulebook-ready, refuse the query rather than warn. (Unseen 5.4.)
7. **Before any model run, check a new relation or pattern against the reference answers** of every training question of its type outside the closed sample. Record those ids. (Rankings 2.1; unseen 5.5.)
8. **Before adding a refusal or a check, replay the logged queries** to see which correct answers it would hit. (Unseen 5.4.)
9. **Have a second agent try to reproduce each diagnosis and each review finding.** Some fail: 18 of 19 diagnoses were upheld on 9 November. (Date hints 1, 3; unseen 2.)
10. **Make one change per measurement.** Two changes in one step cannot be told apart. (Date hints 2.6; rankings Step 1.)
11. **Runs repeat only with all four conditions:** temperature 0, a fresh server per set, a worktree pinned at the commit, and one DuckDB thread. Compare runs question by question, not by totals. (Unseen 5.1, 5.3; rankings 3.1.)
12. **Compare re-prepared bundles with the previous generation before measuring:** columns, rows, hashes, issues and schema text. (Unseen 5.5.) `scripts/compare-bundles.py` now does it, including the result of every table function; it showed that the cash-on-delivery fix changed one of 30 training businesses, and that the header aliases changed one (biz_124's stock names).
13. **Run the whole measured path once, with nothing mocked, before trusting a new layer.** The first model run of the intent layer abstained on every question that reached the model, while all synthetic tests passed: two components held the same DuckDB file. (Intent record 2.9.)
14. **A rule reader written from a set of questions says little about new questions.** The kinds claim 80.7% of the training questions they were written from and <<PUBLIC_PCT>>% of the public questions. Measure coverage on a split you did not write from, by counts only, and never widen the vocabulary from its text. (Intent record 3.2.)
15. **Check a deterministic layer against the references before any model run**, and fix a wrong claim at its cause. The replay found the cash-on-delivery label in the bundle, not in the parser. (Intent record 2.8.)

## What each round's record should contain

- **Header:** date, status, branch, starting commit, scope.
- **Situation:** the numbers, the sets used, and which sets have been diagnosed.
- **For each decision:**
  - the decision;
  - why it fits this situation;
  - evidence gathered before any model run;
  - what was rejected;
  - the risk.
- **Results table:** the commit and the run folder for each cell, for example `setA-r2s2b`.
- **Per-question changes:** against the previous run, for the open sets (better, worse, and why).
- **Failure table** at the end of the round, for the open sets: id, business, answer type, kind, outcome, cause, backlog item.
- **Evidence ids:** questions whose reference answers were read outside the frozen sets.
- **Closed-sample reads:** how many so far.
- **Not verified.**
- **Updates:** PROGRESS.md after every sub-step; this page's "Where we stand" and backlog at the end of the round.
