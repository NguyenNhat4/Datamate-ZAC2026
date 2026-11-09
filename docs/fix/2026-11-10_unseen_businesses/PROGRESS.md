# Progress: unseen businesses

Last updated: 10 November 2026, after Step 2.
Branch: `fix/sql-answering-accuracy`. Last code commit: `f746981` (Step 2). Nothing is pushed.

This file is the single place to see what is done, what is running and what comes next. It is updated after every sub-step. Each step ends with its own commit, so `git log` shows the same progress.

Status marks: `[x]` done, `[~]` started, `[ ]` not started.

## Where we are now

**Steps 0, 1 and 2 are done.** On the closed sample (holdout48) PA went from +0.188 to +0.583 and on unseen48 from +0.167 to +0.417. The development sets did not change, apart from one setB question flipped by tied query rows. Next: the items under "Later" and the new items in the decision record's plan section ("Found during this round").

## Starting point (commit `205dee8`, temperature 0.8; development sets from the `*-fix7` runs on the changes that became `205dee8`)

| Set | Questions | Correct / wrong / abstain | PA |
|---|---|---|---|
| sample10 | 10 | 10 / 0 / 0 | +1.00 |
| setA | 40 | 33 / 4 / 3 | +0.725 |
| setB | 40 | 26 / 7 / 7 | +0.475 |
| types16 | 16 | 15 / 1 / 0 | +0.875 |
| unseen48 | 48 | 24 / 16 / 8 | +0.167 |

The first four sets come from the 8 development businesses. unseen48 comes from the 22 other training businesses.

## Step 0. Measurement basis

Goal: make runs comparable, and keep one sample we never look inside.

- [x] **0.1 Send temperature 0.** `--temperature` option (default 0) in `tabular_agent/cli.py`, passed through `tabular_agent/model.py`, recorded in each run's `manifest.json`. Tests pass except one failure that predates this round (`test_safety.PublicationSafety.test_compatible_partition_scales_and_missing_optional_field`). Committed as `82ebdb6`.
- [x] **0.2 Freeze `holdout48`.** 48 questions from the 22 unseen businesses, none of them in any earlier set, chosen by a seeded hash. All frozen set ids are in `question_sets.json` in this folder.
- [x] **0.3 Check the server obeys temperature 0.** It does. One source of variation remained: the server's prompt cache made a reply depend on the request before it. Turning the cache off removes it but makes questions 2.5 times slower, so each set now runs on a freshly started server instead. Details in [README.md](README.md), decision 5.3.
- [x] **0.4 New baselines.** sample10, setA, setB, types16 and unseen48 at temperature 0, each on a fresh server. These replace the table above as the comparison point. setB and unseen48 were run a second time: identical replies at every model step.
- [x] **0.5 Holdout baseline.** holdout48: 25 / 16 / 7, PA +0.188. Only the total was read.
- [x] **0.6 Decision record.** [README.md](README.md): decisions 5.1 to 5.3 and results 6.1.
- [x] **0.7 Commit Step 0.** Code in `82ebdb6`; this record in the commit that follows Step 1's code.

## Step 1. Abstain when sales data is not rulebook-ready

Goal: stop confident wrong answers in businesses whose bundle has no `semantic.eligible_sales_lines`. Expected on unseen48: 11 wrong answers become abstentions (PA about +0.17 to +0.40).

- [x] **1.1 Replay the unseen48 logs.** Of the 14 questions in such bundles, the rule refuses a query in 9 of the 11 wrong answers and in no correct answer at `205dee8`. In the temperature-0 baseline it refuses 7 wrong answers and 1 correct one (decision 5.4). The other 2 wrong answers are biz_110 revenue questions, fixed by `2eef9af`.
- [x] **1.2 Implement in `tabular_agent/sql_agent.py`.** In such a bundle, queries on raw order and sales-line tables are rejected with the reason and an instruction to abstain. Stock, price list, receipts and catalogue stay queryable. No development bundle is affected.
- [x] **1.3 Tests** on invented data (5 new tests).
- [x] **1.4 Measure.** Development sets: identical replies at every model step. unseen48 +0.167 → +0.292 at `67d4d2c`; the revenue answers of biz_110 still used `order_total`, so the revenue query was moved into the query patterns (`2eef9af`): +0.375. holdout48 total: +0.188 → +0.417 → +0.458.
- [x] **1.5 Record the decision and commit.** Code in `67d4d2c` and `2eef9af`; decision 5.4 and results 6.2 in [README.md](README.md). An independent review narrowed the rule before it was measured (delivery books, credit terms and dine-in sheets stay queryable; rulebook-ready orders answer order questions).

## Step 2. Preparation fixes

Goal: publish sales relations for businesses that are blocked by rules that are too strict. Expected to unblock biz_105, 115, 116, 117, 118, 128 and public dev_004 (about 700 training questions).

- [x] **2.1 Fix A.** A sheet with only an order ID (for example debt terms) is not the shop's order table. Report a named issue instead of stopping silently.
- [x] **2.2 Fix B.** A delivery book is not a sales-line table when another line table records the sales. (Narrowed during the work: the first version dropped every line table without sale facts.)
- [x] **2.3 Fix C.** Map the status "Không giao được" to cancelled.
- [x] **2.4 Fix D.** Take the order date from non-return lines when the business's README says returns are negative-quantity lines. After review, also the order time (so the shift is published), and "giờ đặt đơn" is read as the order time.
- [x] **2.5 Small fixes.** Strip spaces around codes. Read "số ngày" columns as numbers. Name an unparsable status column instead of stopping silently.
- [x] **2.6 Tests** on invented data (11 new tests; 10 fail on the old code, and one checks that a line table without sale facts is still used when it is the only one).
- [x] **2.7 Re-prepare** all 30 training and 14 public businesses into `artifacts/prepared-step2-dev`, `-unseen` and `-public`.
- [x] **2.8 Check nothing broke.** Development bundles: 7 identical, biz_107 differs only by one informational issue (data and schema text identical). Businesses without rulebook-ready sales lines: training 7 → 1 (749 → 40 questions), public 8 → 7 (516 → 420 questions). None lost them.
- [x] **2.9 Measure.** unseen48 +0.375 → +0.417; holdout48 total +0.458 → +0.583. Development sets: sample10, setA and types16 identical at every model step; setB lost one question because a query on tied values returned other rows (arbitrary DuckDB order), not because of Step 2.
- [x] **2.10 Record the decisions and commit.** Code in `f746981`; decision 5.5 and results 6.3 in [README.md](README.md).

## Later (not started, not in this round)

3. Customer rankings that exclude walk-in buyers.
4. Revenue for businesses with invoice headers only (biz_110, dev_009): order revenue done in Step 1 (`2eef9af`); questions that need sales lines still abstain.
5. Remaining public-test file layouts, largest first: dev_003 (129 questions), dev_014 (63), dev_007 (60), dev_013 (57), dev_012 (50), dev_011 (40).

## Results log

One row is added after every measurement. Scores are PA from the internal scorer (not the official one) on training questions, with the local 7B model on the team laptop.

| When | Code | Temperature | sample10 | setA | setB | types16 | unseen48 | holdout48 (total only) |
|---|---|---|---|---|---|---|---|---|
| 9–10 Nov | `205dee8` | 0.8 (server default) | +1.00 | +0.725 | +0.475 | +0.875 | +0.167 | not run |
| 10 Nov | `82ebdb6` (Step 0) | 0 | +1.00 | +0.725 | +0.600 | +1.00 | +0.167 | +0.188 (25 / 16 / 7) |
| 10 Nov | `67d4d2c` (Step 1) | 0 | +1.00 | +0.725 | +0.600 | +1.00 | +0.292 | +0.417 (26 / 6 / 16) |
| 10 Nov | `2eef9af` (Step 1, revenue pattern) | 0 | not rerun | not rerun | not rerun | not rerun | +0.375 | +0.458 (27 / 5 / 16) |
| 10 Nov | `f746981` (Step 2, re-prepared bundles) | 0 | +1.00 | +0.725 | +0.575 (one question changed by tied query rows, not by Step 2) | +1.00 | +0.417 | +0.583 (35 / 7 / 6) |
