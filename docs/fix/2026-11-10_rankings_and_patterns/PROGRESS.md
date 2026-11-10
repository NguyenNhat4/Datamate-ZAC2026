# Progress: rankings, ties and missing query patterns

Last updated: 10 November 2026, at the end of the round.
Branch: `fix/sql-answering-accuracy`. Starting commit: `8b3033b` (code `f746981`). Nothing is pushed.

This file shows what is done, what is running and what comes next. It is updated after every sub-step. Plan and reasons: [README.md](README.md).

Status marks: `[x]` done, `[~]` started, `[ ]` not started.

## Where we are now

**Round done.** Steps 1 and 2 are committed (`c509cba`; `a77671d`, `5e1507b`). holdout48 +0.583 → +0.646, unseen48 +0.417 → +0.667; setB +0.575 → +0.600 (Step 1, the tied debt query), the other development sets unchanged. The model server is off. Next: [README.md](README.md) section 6, starting with the prompt size of the largest bundles.

## Starting point (`f746981`, temperature 0, from the previous round)

| Set | Correct / wrong / abstain | PA |
|---|---|---|
| sample10 | 10 / 0 / 0 | +1.00 |
| setA | 33 / 4 / 3 | +0.725 |
| setB | 28 / 5 / 7 | +0.575 |
| types16 | 16 / 0 / 0 | +1.00 |
| unseen48 | 31 / 11 / 6 | +0.417 |
| holdout48 (total only) | 35 / 7 / 6 | +0.583 |

Scores are PA from the internal scorer (not the official one) on training questions, with the local 7B model on the team laptop.

## Step 1. Repeatable query results

- [x] **1.1 One DuckDB thread for answering**, so tied rows come back in the same order every run.
- [x] **1.2 Tests** on invented data (1 new test; it fails on the old code).
- [x] **1.3 Measure** the six sets. unseen48 and holdout48 unchanged (unseen48 gives the same model reply at every step of every question). The four development sets give the same replies as the previous round's temperature-0 baseline (runs `*-t0`), including the setB question that tied rows had flipped (setB back to +0.600).
- [x] **1.4 Record and commit.** Code in `c509cba`; decision 3.1 in [README.md](README.md).

The tie rule in the model instructions moved to Step 2: any change to the instructions shifts replies on every question, so Step 1 keeps only the thread change and its measurement shows only that.

## Step 2. Missing rankings and query patterns

- [x] **2.1 Check each new query on training questions** of the same type (not holdout48) against the reference answers, before any model run: customers by money 71 of 71, days over an amount 20 of 20, unsold products 33 of 33.
- [x] **2.2 Customers who bought the most money**: walk-in orders excluded, ties by customer (query pattern). The rulebook's tie rule was kept out of the instructions (it would change every prompt); the ranking pattern breaks ties by key.
- [x] **2.3 Days with revenue of at least X**: daily totals, not single lines (query pattern).
- [x] **2.4 Unsold products in a period (hàng ế)**, by the rulebook: a preparation function `semantic.unsold_products(first_day, last_day)` and a query pattern.
- [x] **2.5 Tests** on invented data.
- [x] **2.6 Re-prepare and check nothing else changed.** In `artifacts/prepared-r2-*`, compared with `artifacts/prepared-step2-*`: 36 bundles differ only by the added function; the other 8 (no rulebook-ready sales lines) are identical. The public test was re-prepared once more after the last fix, so dev_002 and dev_005, whose lines have no amounts, also get the function; that fix changes nothing for bundles with line amounts, which every training bundle with sales lines has (biz_110 has none and gets no period function either way).
- [x] **2.7 Measure** the six sets at `a77671d`. Five of the six targeted unseen48 questions became correct (the biz_108 customer ranking stayed wrong). A shortened pattern title shared by most prompts also changed the submitted answer of 16 unrelated questions, 5 of them in outcome.
- [x] **2.7b Restore that title** (`5e1507b`) so prompts of other questions are byte-identical (checked on all 44 bundles), and measure again: development sets identical to Step 1 at every model step; in unseen48 exactly the six targeted questions changed, all to correct; holdout48 +0.646.
- [x] **2.8 Record and commit.** Decisions 3.1 and 3.2 and results in [README.md](README.md). The model server is off.

## Results log

One row is added after every measurement.

| When | Code | sample10 | setA | setB | types16 | unseen48 | holdout48 (total only) |
|---|---|---|---|---|---|---|---|
| 10 Nov | `f746981` (start) | +1.00 | +0.725 | +0.575 | +1.00 | +0.417 | +0.583 (35 / 7 / 6) |
| 10 Nov | `c509cba` (Step 1) | +1.00 | +0.725 | +0.600 | +1.00 | +0.417 | +0.583 (35 / 7 / 6) |
| 10 Nov | `a77671d` (Step 2, first commit) | +1.00 | +0.675 | +0.650 | +1.00 | +0.646 | +0.604 (36 / 7 / 5) |
| 10 Nov | `5e1507b` (Step 2, final) | +1.00 | +0.725 | +0.600 | +1.00 | +0.667 | +0.646 (37 / 6 / 5) |
