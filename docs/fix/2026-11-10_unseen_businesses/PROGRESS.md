# Progress: unseen businesses

Last updated: 10 November 2026, after Step 0.
Branch: `fix/sql-answering-accuracy`. Last commit: `67d4d2c` (Step 1 code).

This file is the single place to see what is done, what is running and what comes next. It is updated after every sub-step. Each step ends with its own commit, so `git log` shows the same progress.

Status marks: `[x]` done, `[~]` started, `[ ]` not started.

## Where we are now

**Step 1, sub-step 1.4 (measuring).** Step 0 is done: the baselines at temperature 0 are in the results log, and two repeat runs were identical. Step 1 code is committed (`67d4d2c`), after an independent review that narrowed the rule; its measurement round is running. Step 2 code is written, reviewed and tested, not yet committed; all 44 businesses are being re-prepared with it.

## Starting point (commit `205dee8`, temperature 0.8)

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

- [x] **0.1 Send temperature 0.** `--temperature` option (default 0) in `tabular_agent/cli.py`, passed through `tabular_agent/model.py`, recorded in each run's `manifest.json`. Tests pass (the one old failure remains). Committed as `82ebdb6`.
- [x] **0.2 Freeze `holdout48`.** 48 questions from the 22 unseen businesses, none of them in any earlier set, chosen by a seeded hash. All frozen set ids are in `question_sets.json` in this folder.
- [x] **0.3 Check the server obeys temperature 0.** It does. One source of variation remained: the server's prompt cache made a reply depend on the request before it. Turning the cache off removes it but makes questions 2.5 times slower, so each set now runs on a freshly started server instead. Details in [README.md](README.md), decision 5.3.
- [x] **0.4 New baselines.** sample10, setA, setB, types16 and unseen48 at temperature 0, each on a fresh server. These replace the table above as the comparison point. setB and unseen48 were run a second time: identical replies at every model step.
- [x] **0.5 Holdout baseline.** holdout48: 25 / 16 / 7, PA +0.188. Only the total was read.
- [x] **0.6 Decision record.** [README.md](README.md): decisions 5.1 to 5.3 and results 6.1.
- [x] **0.7 Commit Step 0.** Code in `82ebdb6`; this record in the commit that follows Step 1's code.

## Step 1. Abstain when sales data is not rulebook-ready

Goal: stop confident wrong answers in businesses whose bundle has no `semantic.eligible_sales_lines`. Expected on unseen48: 11 wrong answers become abstentions (PA about +0.17 to +0.40).

- [x] **1.1 Replay the unseen48 logs.** Of the 14 questions in such bundles, the rule refuses a query in 9 of the 11 wrong answers and in no correct answer. The other 2 wrong answers are biz_110 (invoice headers only, Later item 4).
- [x] **1.2 Implement in `tabular_agent/sql_agent.py`.** In such a bundle, queries on raw order and sales-line tables are rejected with the reason and an instruction to abstain. Stock, price list, receipts and catalogue stay queryable. No development bundle is affected.
- [x] **1.3 Tests** on invented data (4 new tests).
- [ ] **1.4 Measure.** Re-run the five sets. The four development sets must not change. Run holdout48 and read its total only.
- [ ] **1.5 Record the decision and commit.**

## Step 2. Preparation fixes

Goal: publish sales relations for businesses that are blocked by rules that are too strict. Expected to unblock biz_105, 115, 116, 117, 118, 128 and public dev_004 (about 700 training questions).

- [x] **2.1 Fix A.** A sheet with only an order ID (for example debt terms) is not the shop's order table. Report a named issue instead of stopping silently.
- [x] **2.2 Fix B.** A delivery book is not a sales-line table when another line table records the sales.
- [x] **2.3 Fix C.** Map the status "Không giao được" to cancelled.
- [x] **2.4 Fix D.** Take the order date from non-return lines when the README says returns are negative-quantity lines.
- [x] **2.5 Small fixes.** Strip spaces around codes. Read "số ngày" columns as numbers.
- [x] **2.6 Tests** on invented data, one per fix (9 new tests; each fails on the old code).
- [~] **2.7 Re-prepare** all 30 training and 14 public businesses into new directories (about 25 minutes).
- [ ] **2.8 Check nothing broke.** The 8 development businesses' semantic relations must be identical to `artifacts/prepared-fix6`. Table of which businesses publish sales relations, before and after.
- [ ] **2.9 Measure.** Re-run the five sets. Run holdout48 and read its total only.
- [ ] **2.10 Record the decisions and commit.**

## Later (not started, not in this round)

3. Customer rankings that exclude walk-in buyers.
4. Revenue for businesses with invoice headers only (biz_110, dev_009).
5. Remaining public-test file layouts, largest first: dev_003 (129 questions), dev_014 (63), dev_007 (60), dev_013 (57), dev_012 (50), dev_011 (40).

## Results log

One row is added after every measurement.

| When | Code | Temperature | sample10 | setA | setB | types16 | unseen48 | holdout48 (total only) |
|---|---|---|---|---|---|---|---|---|
| 9–10 Nov | `205dee8` | 0.8 (server default) | +1.00 | +0.725 | +0.475 | +0.875 | +0.167 | not run |
| 10 Nov | `82ebdb6` (Step 0) | 0 | +1.00 | +0.725 | +0.600 | +1.00 | +0.167 | +0.188 (25 / 16 / 7) |
