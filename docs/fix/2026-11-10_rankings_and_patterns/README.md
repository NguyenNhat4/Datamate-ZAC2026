# Decision record and plan: rankings, ties and missing query patterns

Date: 10 November 2026
Status: Steps 1 and 2 done. Live status is in [PROGRESS.md](PROGRESS.md).
Branch: `fix/sql-answering-accuracy`, starting from commit `8b3033b` (code `f746981`).
Scope: the SQL answering path (`tabular_agent/sql_agent.py`) and the period functions in the preparation layer (`tabular_agent/preparation/views.py`): one new function and the column checks that decide which functions are published.

This record follows [the unseen businesses record](../2026-11-10_unseen_businesses/README.md). Business and question ids appear only as evidence. No rule branches on an id, and no reference answer is stored in code.

Scores are penalized accuracy (PA = (correct − wrong) / questions) from the internal scorer, not the official one, on training questions, with the local 7B model (Qwen2.5-Coder-7B-Instruct, Q4_K_M) on the team laptop. Runs follow the previous record's method: temperature 0, a fresh model server per question set, and code from a git worktree pinned at the measured commit.

## 1. Situation

The previous round left these open items, found in its runs:

| Problem | Seen in | What happened |
|---|---|---|
| Tied query rows come back in arbitrary order | setB, biz_106 | A debt ranking where all 241 rows (customer names, one of them blank) tie at 0 returned different customers on each run of the same bundle, so one answer flipped between runs with no code change |
| Walk-in buyers ranked as customers | unseen48, biz_117 | "3 customers with the highest purchases last week" grouped by name and returned "kl", "Khách lẻ", "lẻ" |
| Days with revenue of at least X | unseen48, biz_118 | The model filtered single lines of 49 million or more instead of daily totals |
| Unsold products in a period | unseen48, biz_115 | The model looked for unsold products among products sold in the period |

The rulebook settles each one:

- **Ties.** "Two objects equal on the question's criterion are ordered by code ascending (by name when there is no code). A question about one object takes the first in that order."
- **Customers.** Orders without a customer code (walk-in, "khách lẻ") are not a customer: they are not counted and not ranked. "Customer who bought the most money" ranks by revenue.
- **Unsold products (hàng ế).** A product the shop sold before the period (in eligible orders, not cancelled or returned) with no eligible order in the period.

unseen48 was used to find these items, so its scores for them are optimistic. holdout48 stays closed: only totals are read.

## 2. Plan

### Step 1. Repeatable query results

- Answer with one DuckDB thread. With two threads the tied debt query above gave 3 different results in 30 runs on the same bundle; with one thread, 1 result in 30 runs. Replaying the 168 queries logged by setA, setB, types16 (baseline runs) and unseen48 (at `f746981`) took 3.2 s in total with one thread against 2.7 s with two (slowest 0.12 s either way), negligible next to about 7 s per question.

**Why first.** It changes no answer logic and no prompt text, and without it a single changed answer between two runs cannot be told from noise, which every later measurement depends on. (Adding the rulebook's tie rule to the instructions was considered for Step 2 and dropped; see 3.2.)

### Step 2. Missing rankings and query patterns

- Customers who bought the most money: rank `semantic.eligible_orders` by revenue per `customer_key` (walk-in orders have no key), ties by key.
- (Dropped, see 3.2.) The rulebook's tie rule in the model instructions: it would add text to every prompt; the ranking patterns break ties by key instead.
- Days with revenue of at least X: group eligible sales lines by day and count the days whose total reaches X.
- Unsold products in a period, by the rulebook definition above.

**Check inside the step.** Before any model run, each new query is checked against the reference answers of all training questions of the same type outside holdout48, so the evidence does not rest on the unseen48 examples that revealed the problems.

## 3. Decisions

### 3.1 Answer with one DuckDB thread (Step 1; commit `c509cba`)

**Decision.** The answering connection uses one DuckDB thread instead of two.

**Why, in this situation.** With parallel execution, rows that tie in `ORDER BY … LIMIT` come back in a different order on each run, even on the same bundle. In the previous round this alone changed one setB answer between runs. The rulebook orders ties by code, but a query the model writes does not always say so; one thread at least makes the result the same every run.

**Evidence.** Same tied query, 30 runs on one bundle: 3 different results with two threads, 1 with one. 168 logged queries: 3.2 s in total against 2.7 s, slowest 0.12 s either way.

**Rejected.** Rewriting the model's SQL to add a tie-breaking order: the query text would have to be parsed and changed, which risks breaking valid queries. Adding the rulebook's tie rule to the instructions: it changes the prompt of every question (see 3.2).

**Risk.** Ties are now repeatable but still follow the bundle's stored row order, not always the rulebook's code order, when the model's own query has no tie-break. A re-prepared bundle can store rows in a different order.

### 3.2 Rankings and counts that were missing (Step 2; commits `a77671d`, `5e1507b`)

| Question type | Decision | Why, in this situation | Evidence before any model run |
|---|---|---|---|
| Customers who bought the most money | Query pattern on `semantic.eligible_orders`: group by `customer_key` (walk-in orders have none and are never ranked), order by revenue, ties by key | The model grouped by name and ranked walk-in labels ("kl", "Khách lẻ", "lẻ") as customers; the rulebook says walk-in orders are not a customer | 71 of 71 training questions of this type (holdout48 excluded) return the reference customers in order; 63 with the exact reference spelling, the other 8 with an accepted spelling (alias). The name is picked with `MIN(customer_name)`, as in the existing "most orders" pattern; `MAX` would give 68 exact spellings, and both pass with accepted spellings |
| Number of days whose revenue reaches an amount | Query pattern: daily totals of eligible sales lines with `HAVING` | The model filtered single lines against the amount | 20 of 20; counting days over all orders, cancelled and returned included, would be wrong in 14 of them. All 20 use "trở lên" (≥); the pattern's > for "trên / vượt" and < for "dưới" are untested, and for "dưới" days without any eligible sale would be left out. The example amount in the pattern (49,000,000) is the one in the diagnosed unseen48 question, so that question's gain is especially optimistic |
| Unsold products (hàng ế) | Function `semantic.unsold_products(first_day, last_day)` in preparation, with a query pattern: products sold in counted orders before the period and not in it, service-fee lines excluded | The model looked among products sold in the period; the rulebook defines the term by sales before the period | 33 of 33; reading the raw line table instead would be far off because only the rulebook-ready view merges spelling variants of product names |

The three checks were made by independent agents on every training question of each type outside holdout48, with hand-written queries on the prepared bundles. They found no question of these types in biz_110 (invoice headers only), and none asks for a list of unsold products.

**Shown only for their kind of question.** Each pattern, and the unsold-products function, appears only when the question is of that kind, detected from the question's words (like the comparison and shift patterns). Every validated question of each type is detected (71, 20, 33). Of the 2,952 training questions outside holdout48, the customer detector also fires on 38 others: 27 about customers by number of orders, 7 unsold-product questions and 4 unrelated ones; these get the customers-by-money pattern as extra text. The days and unsold detectors fire on no other question. The word lists were written from these training questions plus common spelling variants, so their recall on new phrasings is unknown.

**What the review changed.** An independent review (2 reviewers; each finding checked by a second agent) confirmed four findings. Three changed the code:

1. **Prompt size (high).** The first version added 379 to 496 tokens to every prompt (496 on dev_006). dev_006, the public-test business with the longest prompt, already had 12 of its 111 questions over the 16,384-token context the local server is started with (`LOCAL_CONTEXT` in `scripts/serve-local.sh`) at the first step and at most about 220 tokens of headroom for the rest, so the first version would have put all 111 over the limit. Gating by question kind fixed this; the rulebook's tie rule was also kept out of the instructions (the ranking patterns break ties by code where the ranked rows carry one and by name otherwise, and the rulebook in every prompt states the rule). Measured with the model's tokenizer on every bundle, with one invented question per kind: a question of another kind gets the same first prompt as before (byte-identical after `5e1507b`); for a question of the new kinds it grows by at most 174 tokens. These counts leave out the date-phrase block (34 to 45 tokens for these questions). With it, dev_006 has about 190 tokens of headroom for a question of another kind and only about 5 to 60 for the new kinds, so questions of the new kinds there will almost certainly fail on context size (section 5).
2. **Out-of-stock questions (low).** The first unsold-products text said "count the rows for mấy mặt hàng", which also matches "how many products are out of stock". The text now names unsold products only, and is not shown for other questions.
3. **Function published only with line amounts (low).** The period functions were all skipped when sales lines have no amounts. Each function now checks only the columns it uses, so dev_002 and dev_005 get the unsold-products function.

The fourth, unsold-product lists in name order, is left as a risk (below). Four other findings were rejected after reproduction: they occur only in invented data (order dates with a time of day other than midnight, products without code or name, fee lines with a rate in the name, off-catalogue spelling variants; the last two surveyed on training bundles only).

**Prompts stay byte-identical for questions no new detector fires on.** The first commit also shortened the title of the existing "customer with the most orders" pattern, to save 8 tokens. That title is in most prompts (32 of 44 bundles), and the 7B model's replies changed on 25 unrelated questions; five changed outcome (three better, two worse: for example, an afternoon-shift revenue question was answered with total revenue). Commit `5e1507b` restores the title. Checked on all 44 bundles with five questions of other kinds (four invented, plus the text of the setA afternoon-shift question): the full first prompt (instructions, schema text, allowed relations, date phrases) is byte-identical to before this round.

**Rejected.** A question classifier model: it would add a model call to every question, and the word-based detectors catch every validated question (in-sample: they were written from those questions). Products not sold in the period taken from the catalogue: not the rulebook's definition.

**Risk.**

- Lists of unsold products come out in name order where the sales lines carry only names but the catalogue has codes (9 bundles: 7 training, plus dev_001 and dev_005 in the public test); the rulebook orders such a list by code. No training question asks for this list.
- The detectors are word lists. A question phrased in a way none of the training questions use would not see the new pattern; it then gets the same prompt as before this round.

## 4. Results

Measurements are logged in [PROGRESS.md](PROGRESS.md) as they are made. Each set ran on a fresh model server from a worktree pinned at the measured commit; every manifest reports a clean tree. The development and unseen bundles in `prepared-r2-*` were prepared before `a77671d` was committed; the committed code differs from that version only in the column checks for sales lines without amounts, which no training bundle has.

| Set | Start (`f746981`) | Step 1 (`c509cba`) | Step 2, first commit (`a77671d`) | Step 2, final (`5e1507b`) |
|---|---|---|---|---|
| sample10 | +1.00 | +1.00 | +1.00 | +1.00 |
| setA | +0.725 | +0.725 | +0.675 (32 / 5 / 3) | +0.725 |
| setB | +0.575 (28 / 5 / 7) | +0.600 (28 / 4 / 8) | +0.650 (29 / 3 / 8) | +0.600 (28 / 4 / 8) |
| types16 | +1.00 | +1.00 | +1.00 | +1.00 |
| unseen48 (diagnosed) | +0.417 (31 / 11 / 6) | +0.417 (31 / 11 / 6) | +0.646 (36 / 5 / 7) | +0.667 (37 / 5 / 6) |
| holdout48 (closed, totals only) | +0.583 (35 / 7 / 6) | +0.583 (35 / 7 / 6) | +0.604 (36 / 7 / 5) | +0.646 (37 / 6 / 5) |

**Step 1.** No answer changed except the one setB question that tied rows had flipped in the previous round: all four development sets now give the same model reply at every step of every question as the previous round's temperature-0 baseline (runs `*-t0`, commit `82ebdb6`), and unseen48 is identical at every step to the start of this round.

**Step 2, first commit.** In unseen48, five of the six questions this step aimed at went from wrong to correct: three customer rankings, the day count and the unsold count; the fourth customer ranking (biz_108) got the new pattern but stayed wrong: it answered a list of two names where one was asked. After `5e1507b` restored the old title, which says "use SUM(revenue) for the customer who bought the most money", it was answered correctly; the restore therefore also changed one targeted answer. The shortened pattern title (3.2) also changed the model's replies on 25 unrelated questions and the submitted answer on 16; five of those changed outcome: in unseen48 a morning-shift order count went from wrong to correct and a product-quantity question from correct to abstain (the model typed a garbled product name); in setA an afternoon-shift revenue question went from correct to wrong; in setB one wrong answer became an abstention and one abstention became correct. holdout48 gained one question.

**Step 2, final.** Only questions of the three new kinds changed. All four development sets are identical to Step 1 at every model step. In unseen48 exactly six questions changed, all from wrong to correct: four customer rankings by money, the day count and the unsold count; the other 42 are identical to Step 1 at every step. unseen48 was used to find these problems, so its gain is optimistic. In holdout48 correct answers rose from 35 to 37 and wrong ones fell from 7 to 6 (+0.063). That is well within the standard error of a 48-question PA (about 0.11 to 0.13). Runs are deterministic, so the change on these 48 questions comes from the code. holdout48's businesses also supplied the questions used to write and check the new queries, so it now estimates accuracy on new questions from known businesses, not on unseen businesses. Median time per question was 6.4–7.4 s.

### Summary of the round

| Set | Start of round | End of round |
|---|---|---|
| sample10, setA, setB, types16 | +1.00, +0.725, +0.575, +1.00 | +1.00, +0.725, +0.600, +1.00 |
| unseen48 (diagnosed) | +0.417 | +0.667 |
| holdout48 (closed) | +0.583 | +0.646 |

## 5. Not verified

- dev_006's prompts already reach the 16,384-token context before this round: at the first step 12 of its 111 questions are over the limit (and are recorded as abstain), and the other 99 have at most about 220 tokens of headroom (median about 200). That leaves little room for the model's query, its result and the answer at later steps; how many overflow there has not been measured. This needs a structural cut of the prompt (for example, not listing clean tables that semantic relations already cover).
- One review agent's script read the question texts of dev_006 (a public-test business) and output only prompt token counts. The detector word lists were written from training questions outside holdout48 plus common spelling variants, not from public-test text.
- No public-test answers exist, so the new patterns are not measured on the public test.

## 6. Next

1. **Prompt size of the largest bundles.** dev_006 (111 public-test questions) already has 12 questions over the 16,384-token context at the first step and the rest within about 220 tokens of it; later steps add queries and results. A structural cut is needed, for example not listing clean tables whose content the semantic relations already cover. Measure the token count per bundle with the model's tokenizer before and after.
2. **Order of unsold-product lists** by catalogue code where sales lines carry only names (9 bundles).
3. Items left from the previous record: a price-only view for sales lines that are not rulebook-ready, dev_007's status column, and the remaining public-test layouts.
