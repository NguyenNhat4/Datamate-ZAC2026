# Decision record and plan: businesses the system was not tuned on

Date: 10 November 2026
Status: Steps 0, 1 and 2 done (summary in section 6.4). Live status is in [PROGRESS.md](PROGRESS.md).
Branch: `fix/sql-answering-accuracy`, starting from commit `205dee8`.
Scope: the SQL answering path (`tabular_agent/sql_agent.py`), the preparation layer (`tabular_agent/preparation/`), and how runs are measured.

This record follows [the date hints and answer checks record](../2026-11-09_date_hints_and_answer_checks/README.md). Business and question ids appear only as evidence. No rule branches on an id, and no reference answer is stored in code.

## 1. Situation

Every earlier measurement used the same 8 training businesses, and their failures were read to choose fixes. Those scores are therefore optimistic. To get an honest estimate, the other 22 training businesses and the 14 public-test businesses were prepared for the first time, and one frozen sample of 48 questions (`unseen48`, 2–3 per unseen training business) was run once at commit `205dee8`.

Local 7B model (Qwen2.5-Coder-7B-Instruct, Q4_K_M) on a laptop GPU, internal scorer (not the official one), training questions. PA is penalized accuracy: (correct − wrong) / questions.

Terms used below. A bundle is the prepared database of one business. `semantic.eligible_sales_lines` is the bundle's view of sales lines after the rulebook's order rules (cancelled and returned orders separated). A bundle is "rulebook-ready" for sales when preparation could publish that view.

| Questions | Count | Correct / wrong / abstain | PA |
|---|---|---|---|
| unseen48, all | 48 | 24 / 16 / 8 | +0.167 |
| In bundles that publish `semantic.eligible_sales_lines` | 34 | 23 / 5 / 6 | +0.53 |
| In bundles that do not | 14 | 1 / 11 / 2 | −0.71 |

For comparison, the development sets scored +0.475 to +1.00 at the same commit.

**Where the gap comes from.** Where preparation publishes rulebook-ready sales relations, the unseen businesses score +0.53, at the bottom of the development range. Where it does not, the model reads the raw cleaned tables, applies no order eligibility, and gives a confident wrong number.

**How many questions are exposed.**

| Split | Businesses without `semantic.eligible_sales_lines` | Questions in them |
|---|---|---|
| Unseen training | 7 of 22 (biz_105, 110, 115, 116, 117, 118, 128) | 749 of 3,000 training questions |
| Public test | 8 of 14 (dev_003, 004, 007, 009, 011, 012, 013, 014) | 516 of 1,000 public questions (52%) |

Preparation never read questions or answers. Public-test questions were only counted per business.

## 2. How the causes were established

Four AI sub-agents diagnosed the preparation blockers and the 24 failed answers from the profiles, raw files, code and run logs. A second, independent sub-agent per group then tried to reproduce each diagnosis. Every diagnosis was confirmed except one: why biz_124's drug-list sheet is reported as an ambiguous layout could not be reproduced, so that cause is left open.

For the preparation causes, the agents re-ran the semantic step on scratch copies of bundles with the proposed rule changed, and compared the results with training references. This is evidence that the fixes compute the right figures. It is not a model run.

## 3. Causes

### 3.1 Preparation rules that are too strict

| Cause | What happens | Businesses |
|---|---|---|
| A. A sheet with only an order ID is taken as the order table | A debt-terms sheet lists only credit orders. Preparation treats it as the shop's order table, reports every other order's ID as missing from that table, then stops without naming a reason because the sheet has no status | biz_115, 116, 117, 118; dev_004 |
| A′. Same rule, second form | A dine-in table sheet counts as a second order table, so the order system looks ambiguous | biz_128 |
| B. A delivery book is taken as a sales-line table | It has an order ID, a product and a quantity, so it counts as a second candidate for sales lines | biz_115, 116, 117, 118; dev_004 |
| C. The status "Không giao được" is not recognised | Orders with it are "unknown", which blocks the sales relations. In the data it behaves as a cancellation label | biz_116, 118; dev_004 |
| D. Return lines carry the return date | The check "one date per order" fails for orders with a return line | biz_105, 117; dev_004 |
| Header-only invoices | The shop has no line table, so no sales lines are published; order revenue can come only from `semantic.eligible_orders` | biz_110; dev_009 |
| Smaller parsing causes | Codes padded with spaces; a day count parsed as a date; ambiguous thousands separators (not addressed in this round) | several |

In scratch reruns, fixing A, B, C and D published sales relations for biz_105, 115, 116, 117, 118, 128 and dev_004. Seven public-test businesses stay blocked for other reasons, each with its own file layout.

### 3.2 Answer failures in unseen48

| Group | Wrong | Abstain |
|---|---|---|
| Bundle has no rulebook-ready sales relation (causes above) | 11 | 2 |
| Walk-in buyers ("Khách lẻ") ranked as a customer | 3 | – |
| A needed relation does not exist (purchase price, active ingredient, lot stock, price-change effect) | – | 5 |
| Wrong period or wrong query example used | 2 | – |
| "Unanswerable" could not be confirmed for data missing from the source | – | 1 |

## 4. Plan

The order follows what protects the most questions soonest. Step-by-step status, with sub-steps, is in [PROGRESS.md](PROGRESS.md). Each step ends with its own commit, a measurement, and an entry in section 5.

### Step 0. Measurement basis

- Send temperature 0 to the local server so two runs of the same code give the same answers.
- Re-run the five existing sets once at temperature 0. These become the baselines.
- Freeze `holdout48`: 48 more questions from the 22 unseen businesses, in no earlier set. Only its total score is read; its failures are never opened.

**Why first.** unseen48 has now been diagnosed, so it is a development set. Without a sample we never look inside, there is no honest number left. Without deterministic model replies (temperature 0), a change of one or two questions between runs cannot be told from noise.

### Step 1. Abstain when sales data is not rulebook-ready

In a bundle without `semantic.eligible_sales_lines`, queries on the raw order and sales-line tables are rejected with the reason and an instruction to abstain. Stock, price list, receipt and catalogue tables stay queryable.

**Why second.** It is small, and it applies to every unprepared business at once, including public-test businesses whose layouts we have not fixed. A wrong answer scores −1 and an abstention 0. On unseen48 the estimate is 11 wrong answers turned into abstentions, PA about +0.17 to +0.40.

**Check inside the step.** Replay the unseen48 logs to confirm the rule would not have blocked any correct answer.

### Step 2. Preparation fixes A, B, C and D

Plus two small fixes: strip spaces around codes, and read "số ngày" columns as numbers.

**Why third.** These turn abstentions into answers. They are expected to unblock 6 training businesses (about 700 training questions) and dev_004 (96 public questions).

**Check inside the step.** Re-prepare all 44 businesses into new directories. The 8 development businesses' semantic relations must be identical to `artifacts/prepared-fix6`.

### Later

3. Customer rankings that exclude walk-in buyers.
4. Revenue for businesses with invoice headers only: done for order revenue in Step 1 (`2eef9af`); questions that need sales lines still abstain there.
5. Remaining public-test file layouts, largest first: dev_003 (129 questions), dev_014 (63), dev_007 (60), dev_013 (57), dev_012 (50), dev_011 (40).

### Not planned in this round

Price-change effect, purchase prices, product attributes (active ingredient, prescription), lot-based stock, refusing a repeated failing query, and feedback for dates after the reference date. Each affects one or two questions in the samples.

### Found during this round, for a later round

- A price-only view for sales lines that are not rulebook-ready, so that price-on-a-date questions need not abstain (5.4).
- dev_007's status column "Tiến độ" is parsed as money (5.5).
- Query patterns for "days with revenue of at least X" (daily totals) and "products with no sales in a period" (from the catalogue) (6.3).
- Make query results repeatable when rows tie: for example, one DuckDB thread for answering, or a tie-breaking order added to ranked results (6.3). Until then, a single changed outcome between two runs may be noise.

## 5. Decisions

Entries are added as each step is done.

### 5.1 Send temperature 0 (Step 0; commit `82ebdb6`)

**Decision.** `bench` and `predict` take `--temperature`, default 0. It is sent with every request to the local server and recorded in each run's `manifest.json`.

**Why, in this situation.** No temperature was sent before, so the server used its own default sampling. In the previous round, one answer went from correct to wrong between two runs whose code change did not touch that question type; sampling was the likely cause, but with sampling on, this could not be confirmed. With gains of a few questions per step, that noise is as large as the effects being measured.

**Rejected.** Running each set several times and averaging: it multiplies run time by the number of repeats and still leaves noise.

**Risk.** A deterministic model can repeat the same failing reply until the step limit. The accuracy at temperature 0 may also differ from the accuracy at the old setting, in either direction. The new baselines measure that before any other change.

**Result.** On the development sets accuracy did not drop: setB went from +0.475 to +0.600 and types16 from +0.875 to +1.00; sample10, setA and unseen48 kept the same totals (section 6). Median time per question was 6.2–7.5 s, against 5.4–6.2 s in the temperature-0.8 runs; those runs also differed in server state, so the change is not attributed to temperature alone.

### 5.2 Keep one sample closed (Step 0)

**Decision.** `holdout48` is run after each step, and only its totals are read (correct, wrong, abstain, and the timing summary). Frozen ids for every set are in [question_sets.json](question_sets.json). It runs after each step, not once at the end, so that a step that hurts unseen businesses is caught before the next step builds on it.

**Why, in this situation.** Each sample that was diagnosed stopped being an estimate: first setA and setB, then unseen48.

**Risk.** As an estimate of accuracy on unseen businesses, a 48-question PA has a standard error of about 0.11–0.13. The holdout shows direction and rough size, not a precise score. Every read, even of totals only, slowly makes it less independent (section 7).

### 5.3 Start each set on a fresh server, from a pinned copy of the code (Step 0)

**What was found.** The server obeys temperature 0, but one dependence remained. With its prompt cache on, a reply can depend on the request processed just before it. In a check, the same invented question was sent right after itself and right after a different question; the two replies were identical for about the first 540 characters and then differed. With the cache off, replies never varied, but each sample10 question took about 2.5 to 3 times as long (median 17 s instead of about 6 s), which pushes multi-step questions toward the 60-second limit.

**Decision.** Keep the cache on, as the system will run, and start every question set on a freshly started server, so each set begins from the same server state. Each set also runs from a separate git worktree checked out at the commit being measured. A set imports the code when it starts, so an edit made in the main folder during a long run would otherwise leak into it.

**Evidence.** Two full runs of sample10 gave identical replies at all 21 model steps, with the cache on and with it off. On fresh servers, setB and unseen48 were each run twice: identical replies at all 92 and all 121 model steps, and identical outcomes.

**Rejected.** Cache off: about 2.5 to 3 times slower and not how the system will run. Restarting the server before every question: adds a model load to every question and is not how the system runs either. Several runs per set and averaging: costs time and still leaves noise.

**Risk.** Within a set a reply can still depend on the question before it. The order is fixed, so runs repeat, but a change to one question's conversation can shift the next question's reply. Time limits (60 s per question, 15 s per query) can still change an outcome when the machine is loaded. Found later (6.3): a query that sorts on tied values and keeps the first rows gets those rows in arbitrary order from DuckDB, even on the same bundle, so a reply can change between runs.

### 5.4 Refuse sales tables that are not rulebook-ready (Step 1; commits `67d4d2c`, `2eef9af`)

**Decision.** When a bundle publishes no `semantic.eligible_sales_lines`, two kinds of cleaned table are listed by name only, and a query on one is refused with the reason and an instruction to abstain:

- order, order-line and sale-recording tables that record a sale fact or amount (status, order date or time, line amount, unit price, order amounts);
- unclassified tables that carry a sales amount or an order ID and no purchase-receipt column.

Where the bundle has rulebook-ready orders (`semantic.eligible_orders`, as in biz_110 and dev_009), the text points order counts, cancellations and revenue to them. Revenue there is `SUM(order_goods_amount)` over eligible orders and is shown first among the query patterns. Only questions that need individual sales lines are asked to abstain. Order, order-line and sale-recording tables without sale facts (delivery books, credit-terms sheets, a dine-in sheet) stay queryable, with a note that they cannot give revenue, order counts or quantities sold.

**Why, in this situation.** In unseen48, answers from such bundles were 1 correct and 11 wrong (−0.71): the model summed raw tables that apply no order eligibility. A wrong answer scores −1 and an abstention 0. The rule acts only where preparation failed, so it also covers 5 of the 7 public-test layouts that are not fixed yet; dev_012 and dev_013 hold no table the rule recognises as sales.

**Evidence before any run.** Replaying the logged queries:

- At `205dee8`, the rule refuses a query in 9 of the 11 wrong answers and in no correct answer.
- In the temperature-0 baseline, it refuses 7 wrong answers and 1 correct one. The correct one counted products with no sales in a week of biz_115, from raw lines that ignore eligibility; it matched the reference although the query applies no rulebook rule.
- The remaining 2 wrong answers are biz_110 revenue questions answered with `SUM(order_total)`. `SUM(order_goods_amount)` gives both reference figures exactly.
- No development bundle is affected. The schema text, rendered for all 44 bundles by the baseline code and by the new code, differs in exactly 13 bundles. These are the 15 bundles without rulebook-ready sales lines, minus dev_012 and dev_013, which hold no sales table.

**What the review changed.** An independent review (3 reviewers; each finding checked by a second agent who tried to refute it) confirmed three problems in the first version:

1. It refused delivery books and credit-terms sheets, which hold no sale amounts. Delivery books answer the rulebook's delivery-round questions exactly: 13 of 13 training references matched when queried directly. The rule was narrowed to tables that record a sale fact or amount.
2. In biz_110 and dev_009, the text told the model to abstain on revenue and order counts that `semantic.eligible_orders` answers. The text now depends on whether rulebook-ready orders exist.
3. A price on a past date could be mistaken for the catalogue's current price or cost. The text now says it is not.

One confirmed finding was left unfixed: a query that defines a WITH-subquery with the same short name as a refused table, and reads that table by the short name inside it, is not refused. No logged query has this form.

After the first measurement, the biz_110 revenue answers were still wrong: the model ignored the revenue query given in the new section. Commit `2eef9af` moved that query into the query patterns.

**Rejected.** Detecting sales questions and abstaining without the model would need a question classifier, and the same bundles still answer stock and purchase questions. A warning without a refusal was also rejected: the model already ignored "use semantic relations first".

**Risk.**

- Questions about the selling price on a past date abstain in these bundles. The rulebook takes that price from all sales lines, cancelled and returned ones included, so the raw table could answer them. A price-only view is left for later.
- The one correct answer above (biz_115, products with no sales in a week) was lost at Step 1. After Step 2 it is answered wrong from the new relations (6.3), so against the baseline it went from correct to wrong.
- After Step 2, only biz_110 among the training businesses is still affected, so the training sets can no longer measure this rule well. In the public test the rule still refuses tables in 5 businesses (dev_003, 007, 009, 011, 014; 313 of 1,000 questions), and no answers exist to measure them.

### 5.5 Preparation fixes (Step 2; commit `f746981`)

A to D are the causes in 3.1. D′ extends D, S replaces silent stops with named issues, and XS1 and XS2 are the two small fixes. Fixes A to D were first tried by the diagnosis agents on scratch copies. All fixes were then reviewed independently (3 reviewers; each finding checked by a second agent). For 108 training questions in the six newly published businesses (none from holdout48), a hand-written query on the new semantic views reproduced the reference answer every time; the checks under C and D below are part of these.

| Fix | Decision | Why, in this situation | Evidence | Rejected |
|---|---|---|---|---|
| A | An order-keyed table is the shop's order table, and a foreign-key parent, only if it records an order fact (status, order date or order time) | A credit-terms sheet lists only credit orders. Taken as the order table, it made every other order an orphan key, and nothing was published, silently | 6 fact-less order-keyed tables dropped, each a credit-terms or dine-in sheet per its README; every real order table kept | Choosing the largest order table: a credit sheet can be the only order-keyed table |
| B | A line table that records no sale fact is dropped as a sales-line candidate only when another line table records the sales | A delivery book (order, product, quantity) counted as a second sales-line table, so the order system looked ambiguous | 5 delivery books dropped; no business has only fact-less line tables | The first version dropped fact-less line tables always; that would lose sales lines for a shop whose only line table has no prices |
| C | "Không giao được" is cancelled | It was unknown, which blocked eligibility | Where it is the only unfamiliar label (biz_107, biz_124), preparation already mapped it to cancelled, because a single unfamiliar label takes the only rulebook state that has no label; in biz_116 and biz_118 it replaces "hủy" after a cut-over date; all 22 training checks there match | Leaving it unknown |
| D | When the business's README records a return as a negative-quantity line, the order date, the one-date-per-order check, and the order-level facts carried from lines (including the order time) are read from sale lines only | Return lines carry the return date and time, so every order with a return failed the check, and the order time (hence the shift) was dropped | Affects exactly biz_105, 117, dev_004; 36 of 36 checks match; dating returned orders by their return line would get 3 of them wrong | Dropping return lines entirely: they are the evidence that the order is returned |
| D′ | "giờ đặt đơn" describes the order time | The README wording for the time column in the same three businesses was not recognised | Exactly 4 of 1,210 README column descriptions change | — |
| S | A missing or unparsable order status is reported by name (`ORDER_STATUS_MISSING`, `ORDER_STATUS_BLOCKED`) instead of stopping silently | Silent stops hid causes A and B | dev_007 now reports its unparsable status column | — |
| XS1 | Spaces around codes are dropped | Padded product and payment codes in dev_010 (and dev_003) matched nothing | dev_010: 6,871 and 3,204 orphan keys become 0, and debt views are published | — |
| XS2 | A label with "số ngày" or "(ngày)" is a number, not a date | Credit days were parsed as dates and failed | Matches exactly 5 column labels in the 44 businesses; in biz_115 the due date minus the order date equals the day count for 2,304 of 2,304 rows | — |

**Not changed, found by the review.** dev_007's status column "Tiến độ" is parsed as money because "tiến" folds to "tien". Fixing it alone would still leave one order with no status there. This belongs to the public-test layouts (Later 5).

**Check that nothing broke.** All 44 businesses were re-prepared into new directories and compared with the earlier bundles: every relation's columns, row count and content hash, the issues, and the schema text the model sees.

| Group | Identical | Changed as intended |
|---|---|---|
| 8 development | 7 | biz_107: one informational issue gone (C maps the label directly); data and schema text identical |
| 22 unseen training | 15 | biz_105, 115, 116, 117, 118, 128 now publish sales relations; biz_124: same informational issue gone |
| 14 public test | 9 | dev_004 now publishes sales relations; dev_010 codes fixed; dev_003 payment codes trimmed; dev_014 day column numeric; dev_007 issue named |

| Split | Businesses without rulebook-ready sales lines, before → after | Questions in them, before → after |
|---|---|---|
| Training (30) | 7 → 1 (biz_110, which has rulebook-ready orders) | 749 → 40 of 3,000 |
| Public test (14) | 8 → 7 | 516 → 420 of 1,000 |

## 6. Results

Measurements are logged in [PROGRESS.md](PROGRESS.md) as they are made. Local 7B model on the team laptop (RTX 4050 Laptop GPU), internal scorer, training questions.

### 6.1 Baselines at temperature 0 (commit `82ebdb6`)

| Set | Correct / wrong / abstain | PA | At temperature 0.8 (`205dee8`)¹ | Median / p90 seconds per question |
|---|---|---|---|---|
| sample10 | 10 / 0 / 0 | +1.00 | +1.00 | 6.2 / 9.8 |
| setA | 33 / 4 / 3 | +0.725 | +0.725 | 6.2 / 9.4 |
| setB | 28 / 4 / 8 | +0.600 | +0.475 | 7.2 / 9.7 |
| types16 | 16 / 0 / 0 | +1.00 | +0.875 | 7.5 / 11.4 |
| unseen48 | 24 / 16 / 8 | +0.167 | +0.167 | 7.5 / 14.9 |
| holdout48 (total only) | 25 / 16 / 7 | +0.188 | not run | 6.9 / 13.4 |

¹ unseen48 ran on a clean `205dee8`. The four development sets come from the `*-fix7` runs: `56b991a` plus the uncommitted changes that became `205dee8`.

sample10 and setA ran from the main folder, before the pinned worktree was in use. For sample10 the folder held only the untracked documents. For setA it also held the uncommitted Step 1 change, which has no effect on these 8 businesses: none of their bundles has a refused table, so the prompt and the checks are unchanged. Every other run reports a clean tree at `82ebdb6`.

Under the same totals, 9 unseen48 questions changed outcome between 0.8 and 0 (4 better, 5 worse), which shows how large the earlier sampling noise was.

### 6.2 Step 1: refuse sales tables that are not rulebook-ready

| Set | Baseline (`82ebdb6`) | `67d4d2c` | `2eef9af` (revenue pattern) |
|---|---|---|---|
| sample10, setA, setB, types16 | +1.00, +0.725, +0.600, +1.00 | identical replies at every model step | not rerun: no development bundle's schema text changes |
| unseen48 | 24 / 16 / 8, +0.167 | 23 / 9 / 16, +0.292 | 25 / 7 / 16, +0.375 |
| holdout48 (total only) | 25 / 16 / 7, +0.188 | 26 / 6 / 16, +0.417 | 27 / 5 / 16, +0.458 |

In unseen48 the outcomes changed exactly as the replay predicted. At `67d4d2c`, 7 wrong answers became abstentions, and so did the one correct answer from a raw table. At `2eef9af`, the 2 biz_110 revenue answers became correct. Median time per question was 6.2–6.8 s (p90 13–15 s on the unseen sets).

### 6.3 Step 2: preparation fixes (commit `f746981`, bundles re-prepared)

| Set | Before (`2eef9af`) | `f746981` |
|---|---|---|
| sample10, setA, setB, types16 | +1.00, +0.725, +0.600, +1.00 | sample10, setA, types16: identical replies at every model step. setB: 28 / 5 / 7, +0.575 (one question; see below) |
| unseen48 | 25 / 7 / 16, +0.375 | 31 / 11 / 6, +0.417 |
| holdout48 (total only) | 27 / 5 / 16, +0.458 | 35 / 7 / 6, +0.583 |

The one setB change is not caused by Step 2. The development bundles hold identical data and give the model identical schema text. But one query, in biz_106, ranks customers by debt with `ORDER BY … LIMIT 3`, and all 241 customers tie at 0. DuckDB returns tied rows in arbitrary order: run twice on the same bundle, the query returned different customers each time. The model then answered with the new rows (wrong) where it had abstained before.

In unseen48, the 10 questions of the six newly published businesses that abstained at `2eef9af` are now answered: 6 correct and 4 wrong. At `2eef9af`, 7 of them had a query refused by Step 1, 1 abstained by following the new text, and 2 abstained for other reasons. Nine of the 10 now query rulebook-ready relations; the price-change question read the raw sales table, which is now allowed. Against the temperature-0 baseline, 8 unseen48 questions are better and 2 worse (the price-change question, abstain → wrong; products with no sales in a week, correct → wrong). The 4 wrong answers have causes outside this round:

- A ranking of top customers counted walk-in buyers (Later 3).
- "Days with revenue of at least 49 million" filtered single lines instead of daily totals.
- The effect of a price change is not covered by any relation.
- "Products with no sales in a week" looked for them among sold products instead of in the catalogue.

### 6.4 Summary of this round

| Set | Start (`205dee8`, temperature 0.8; note ¹ in 6.1) | Baseline (`82ebdb6`, temperature 0) | End (`f746981`) |
|---|---|---|---|
| sample10, setA, setB, types16 | +1.00, +0.725, +0.475, +0.875 | +1.00, +0.725, +0.600, +1.00 | +1.00, +0.725, +0.575, +1.00 |
| unseen48 (diagnosed) | +0.167 | +0.167 (24 / 16 / 8) | +0.417 (31 / 11 / 6) |
| holdout48 (closed) | not run | +0.188 (25 / 16 / 7) | +0.583 (35 / 7 / 6) |

On the closed sample, wrong answers fell from 16 to 7 and correct answers rose from 25 to 35: correct minus wrong rose by 19 (PA +0.188 → +0.583), and at least 10 questions changed outcome. Repeat runs are identical, so this is not run-to-run noise. As an estimate for unseen businesses, a 48-question PA has a standard error of about 0.11–0.13, so the direction is clear and the size approximate.

## 7. Not verified

- No public-test answers exist. Public-test progress is measured only as preparation coverage (8 → 7 businesses without rulebook-ready sales lines). The Step 1 rule's effect on the 5 public businesses where it refuses a table (313 questions) is not measured.
- The unclassified-table part of the Step 1 rule (tables of grain "unknown" with a sales amount or an order ID and no purchase-receipt column) applies only to public-test bundles (dev_003, dev_011, dev_014), so no training measurement covers it.
- holdout48 has now been run after each step (four totals). Only totals were read, but choosing among changes by its totals would slowly turn it into a development set; no change in this round was chosen that way.
- No measurement on the reference hardware or inside an offline container. The internal scorer may differ from the official one.
