# Decision record: the 100-question gate and work samples

Date: 10 November 2026
Status: samples drawn and frozen. work100 baseline done (section 4). gate100 not run yet: held back on request.
Branch: `fix/sql-answering-accuracy` at `155f323` (answering code as of `5e1507b`).
Scope: two new frozen question sets, the script that draws them (`scripts/draw-samples.py`), and one bundle folder that holds all 30 training businesses. No answering or preparation code changed.

This is the second addition proposed by [the workflow review](../2026-11-10_workflow_gaps/README.md) (section 4 and 3.7). Business ids appear only as evidence and in the sample design; no answer logic uses them.

## 1. Situation

The goal is PA ≥ +0.80 on 100 questions the system was not tuned on, before running the whole public test ([index](../README.md)). Until now, holdout48 was the only closed sample, and it is wearing out:

- Its totals have been read 7 times.
- Its businesses supplied the questions used to validate the 10 November patterns.
- A 48-question PA has a standard error of about 0.11 to 0.13, too coarse for a +0.80 target.

All 30 training businesses have now been prepared and studied. A new sample can therefore only consist of questions not used before; no business with reference answers is unseen any more.

## 2. Decisions

### 2.1 Two samples of 100: a closed gate and an open work set

**Decision.**

- **gate100 (closed).** Only its totals are ever read: once per round, after the round's final commit. It decides when the system is ready for the full public run.
- **work100 (open).** Its failures are read and fixes are chosen from them.

**Why, in this situation.** A sample whose failures are read stops being an estimate, as setA, setB and unseen48 did. The team chose to keep both a sample to learn from and one honest number (10 November).

**Rejected.**

- One open sample: its final score would be optimistic.
- One closed sample, with fixes taken from the diagnosed sets: those sets are almost exhausted (sample10 and types16 score +1.00).

### 2.2 How gate100 is drawn

**Decision.**

- The pool is the 2,799 training questions in no earlier set (3,000 − 201 frozen ids).
- Each business gets a share of 100 in proportion to its remaining questions, with at least 2. Shares are settled by largest remainder, with ties broken by business id.
- Within a business, questions are taken in the order of a seeded SHA-256 hash of their id (seed `samples-2026-11-10`).
- Only question ids and business ids are read: no question text, no answer, no answer type.

**Result.** 100 questions over all 30 businesses, 2 to 4 each.

**Why, in this situation.** Drawing without answer types keeps the gate's composition independent of anything a fix might target. The proportional spread makes it estimate the training distribution of new questions from known businesses.

**Validation reads not excluded.** Some gate questions will be of kinds whose references were read to validate a relation or pattern, and the ids of those reads were not recorded (workflow review 3.7). Excluding those kinds would bias the mix, so they stay in. If needed, the kind detectors can separate them later without anyone reading the questions.

### 2.3 How work100 is drawn

**Decision.** work100 is drawn after gate100, from the questions left, with the same spread rule and its own hash order. Then questions are swapped so that every type of answer has enough cases to learn from:

- at least 4 tables, 4 booleans, 6 unanswerable, 6 strings, 6 dates and 6 lists;
- at least 6 questions from biz_110, the one training business whose bundle has rulebook-ready orders but no rulebook-ready sales lines, like 7 of the 14 public businesses.

Each swap takes out a `number` question: of the same business for a type swap when there is one, otherwise of the business with the most questions.

**Result.** 16 swaps. 30 businesses, 2 to 6 each.

| Answer type | Natural draw | work100 |
|---|---|---|
| number | 60 | 47 |
| percent | 18 | 18 |
| date | 7 | 8 |
| list | 7 | 7 |
| string | 6 | 6 |
| unanswerable | 1 | 6 |
| boolean | 0 | 4 |
| table | 1 | 4 |

**Why, in this situation.** A natural draw gives 0 or 1 boolean, table and unanswerable questions, too few to see a failure pattern. Their share of training is small (23, 19 and 70 of 3,000), but each is a type the format checks strictly. work100's PA is therefore not an estimate of anything. It is for reading failures and for comparing commits question by question.

### 2.4 One bundle folder for all 30 training businesses

**Decision.** `artifacts/prepared-r2-train` holds the training split prepared in one run with the committed preparation code, the same code that made `prepared-r2-public`.

**Why, in this situation.** Both samples span the development and unseen groups, but `--prepared` takes one folder. A question whose business is missing from that folder is answered `abstain` with no error.

**Check.** All 30 businesses have the same `schema.json` (relations, columns, row counts, roles) as in `prepared-r2-dev` or `prepared-r2-unseen`. Statuses are as before: 22 ready, 8 partial. Preparation took 811 s in total. The bundle ids differ because those two folders were prepared before the last preparation-code commit (rankings record, section 4).

### 2.5 Reading rules

- **gate100.** Read only its totals (correct, wrong, abstain, PA, timing), with the runbook's section 6 command. Read it once per round, after the round's final commit, and count each read in [the index](../README.md). Never choose between two changes by its totals. Retire it after about 10 reads, or as soon as a choice was made by its totals.
- **holdout48.** It no longer decides anything. It stays closed and may be run alongside the gate to keep its trend line, but each run is another read.
- **work100.** Open: read its failures and compare commits on it question by question (runbook section 5).

## 3. How to reproduce

```bash
.venv-native/bin/python scripts/draw-samples.py            # prints the draw; the same every time
.venv-native/bin/python scripts/draw-samples.py --write    # adds gate100 and work100 to question_sets.json (refuses to change them)
```

## 4. Baselines

**work100** (run `runs/work100-r3s0`).

- **Setup.** Commit `37ee7e9`, whose answering code is unchanged since `5e1507b`. Fresh server, pinned worktree, bundles `prepared-r2-train`, clean manifest. No question abstained for a missing bundle.
- **Result.** 71 / 20 / 9 (correct / wrong / abstain), PA +0.510. Median 7.3 s and p90 13.7 s per question.

| Answer type | Questions | Correct / wrong / abstain |
|---|---|---|
| number | 47 | 39 / 5 / 3 |
| percent | 18 | 12 / 2 / 4 |
| date | 8 | 4 / 4 / 0 |
| list | 7 | 4 / 2 / 1 |
| string | 6 | 5 / 1 / 0 |
| unanswerable | 6 | 2 / 3 / 1 |
| table | 4 | 3 / 1 / 0 |
| boolean | 4 | 2 / 2 / 0 |

By business group:

- the 8 development businesses: 16 / 6 / 2;
- the other 22: 55 / 14 / 7;
- biz_110: 4 / 2 / 0.

work100 over-represents the rare types, which score lower, so its PA is not comparable with the gate's. On its 47 `number` questions it scores +0.72.

**gate100.** Not run yet; held back on request on 10 November. Run it with the runbook's section 4 commands (bundles `prepared-r2-train`) and read it with section 6 only.

## 5. Not verified

- A 100-question PA has a standard error of about 0.05 to 0.07: a gate reading of +0.80 means roughly +0.70 to +0.90.
- gate100 estimates new questions from the 30 training businesses. It does not estimate the public or private test, which have other businesses; see the workflow review, 3.2.
