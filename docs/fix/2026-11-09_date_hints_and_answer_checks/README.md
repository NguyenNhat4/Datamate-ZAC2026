# Decision record: date hints, reply parsing and percent checks

Date: 9 November 2026
Status: implemented on branch `fix/sql-answering-accuracy` on top of commit `56b991a`; measured on team hardware only
Scope: the answering loop and prompt in `tabular_agent/sql_agent.py`. The prepared bundles did not change; every run below uses `artifacts/prepared-fix6`.

This record follows [the SQL answering record](../2026-11-09_sql_answering_accuracy/README.md). Question ids appear only as evidence references. No rule branches on a question or business id, and no reference answer is stored in code.

## 1. Situation

Commit `56b991a` added four period functions and several prompt aids. Measured with the local 7B model on 8 training businesses (internal scorer, seed 42), it gave:

| Set | Baseline (correct / wrong / abstain, PA) | `56b991a` (runs `*-fix6`) |
|---|---|---|
| sample10 | 10 / 0 / 0, +1.00 | 10 / 0 / 0, +1.00 |
| setA (40) | 20 / 12 / 8, +0.20 | 26 / 8 / 6, +0.45 |
| setB (40) | 17 / 14 / 9, +0.075 | 21 / 9 / 10, +0.30 |
| types16 (16 questions of the four new question types) | none | 12 / 3 / 1, +0.56 |

PA is penalized accuracy: (correct − wrong) / questions.

The overall gain hid **8 regressions**: questions the baseline answered correctly and `56b991a` did not. Before changing anything we needed to know whether they came from the new functions, from the other changes in that commit, or from noise.

**How the causes were established.** Every regression and every failure among the four new question types (19 questions) was diagnosed from the run logs. Four agents read the logs and re-ran the model's SQL on the bundle. A second, independent agent per batch then tried to refute each diagnosis by reproducing it; 18 of 19 diagnoses were upheld, and the one refuted was corrected with the refuter's evidence. Re-running each baseline query on the new bundles gave the same values, so the bundle rebuild caused none of the regressions.

**What the diagnosis found**, by number of questions:

| Cause | Questions | Introduced by |
|---|---|---|
| The date hint for a named month or quarter always added "the month (quarter) before it is …". On questions that compare nothing, the model used that earlier range as the asked period (for example, Q1 for "Quý 2 năm 2025"). | 6, plus 2 where it contributed | Date hints added before the period functions |
| A percent result below 1 in absolute value was copied multiplied by 100 (−0.35 answered as −35); the answer check then abstained. | 2 | Model behaviour |
| A reply holding both a query and an answer: the parser read only the last block, so the query never ran and the model looped to an abstention. | 2 | Parser, from the start |
| The percent-change query example drew a plain "doanh thu tuần trước" question into the comparison function, which returned a percentage. | 1 | Period functions |
| The shift query example showed only revenue, so a question about the number of orders per shift was answered with revenue. | 1 | Period functions |
| Other model errors: a wrong group-name literal, a repeated failing query, top 2 for "top 3", a debt total for a customer count, an invented product name. | 7 | Model behaviour or earlier prompt changes |

The model samples at temperature 0.8, and the seed differs from the older baselines, so a single flip can be noise. The causes above were accepted only where the log shows the mechanism (for example, the exact wrong date range copied from the hint).

## 2. Decisions

### 2.1 Print the earlier period only when the question asks for a comparison with it

**Decision.** For a named month or quarter, the date hint gives only that period. It adds "the month before it" (or "the same month a year earlier" for "cùng kỳ năm trước") only when both hold:

- the question names an earlier period (`_EARLIER`: tháng trước / trc, liền trước / trc, trước đó, cùng kỳ, kỳ trước, năm trước / ngoái, tăng trưởng, and the "last month" phrases), and
- it names exactly one month or quarter. With two named periods the question names its own base.

**Why, in this situation.** This was the largest single cause: 6 wrong answers in 2 runs, and the hint was the only source of the wrong range. The earlier range is needed only for a comparison, and a comparison with the previous period always says so in words. The training questions show the rule separates the cases cleanly:

- **100 questions** name a period and use an earlier-period word together with a comparison verb. 99 of their references are percentages, so they are comparisons.
- **205 questions** use only a comparison verb. They are trends ("tăng liên tục"), comparisons of two named periods, or counts ("số" folds to "so"). None needs an earlier range.
- **1,639 questions** have neither. They are single-period questions.

**Rejected.**
- Keeping the line but marking it "only for a comparison". The 7B model copies date ranges from the hint block without reading the qualifier; the logs show it did exactly that.
- Using any comparison verb as the trigger. Shift comparisons ("ca chiều so với ca sáng") and trends would get an earlier range they do not use.

**Evidence.** On the training questions, all 39 period-change questions from the earlier function check still get their earlier range and still match the reference through `period_comparison`. The review (section 3) found two gaps in the first version, and both were fixed:

- 12 questions written "tháng liền trc" lost their range.
- 10 year-over-year questions naming both months got a wrong "month before".

**Risk.** A comparison written with an earlier-period word not in the list loses the earlier range. The model must then build the base dates itself, as it did before the date hints existed.

### 2.2 Run the query when a reply holds both a query and an answer

**Decision.** When a reply holds a SQL block and a JSON answer, the SQL runs and the model is told its answer was not read. The exception is when the SQL repeats the last query whose result the model has already seen. Then the answer is read and checked against that result.

**Why, in this situation.** The parser kept only the last block, so such a reply never ran its query. The model then repeated the same reply until the step limit. This caused 2 abstentions in the `56b991a` runs and 5 in the setB baseline. An answer written before the query result exists cannot be grounded. Running the query gives the model the result it needs for the next step.

**Rejected.** Reading the answer and ignoring the query, the old behaviour: the answer is unchecked. Rejecting the whole reply: it wastes a step without giving the model a result.

**Evidence.** The review replayed all 428 logged SQL-mode question runs. All 125 mixed replies occurred before any successful query, which is the case this decision handles. The exception for a repeated query covers the reviewer's constructed case, which was not observed in the logs, in which a model restates its query together with a correct answer.

### 2.3 Correct a percentage that was multiplied by 100 twice

**Decision.** Some result cells are already percentages: a column named `*percent*`, or every numeric cell of a query that multiplies by 100. If a percent answer equals 100 times one of those cells, the answer becomes that cell, rounded half-up to two decimals.

**Why, in this situation.** Two percent-change answers had the right result on screen (−0.35 and 0.74) and copied them as −35 and 74. The answer check correctly refused them, and after the second refusal the loop abstained. The cell is a percentage by construction: the published functions name their percentage columns `*_percent`, and a query that multiplies by 100 produces percentages. So the slip can be corrected without guessing.

**Rejected.**
- Accepting any answer that is 100 times any cell. A fraction cell multiplied by 100 is often right, but nothing shows which unit the model meant.
- Only adding feedback. The model had already ignored the feedback twice.

**Evidence and risk.** The review constructed two cases that the rule mishandles:

- a fraction that the model itself aliases `*_percent`;
- a `* 100 … / 100` rounding expression.

Neither appears in the logs. Across 395 logged successful queries, the model gave its own column a percent-like name 22 times, and multiplied by 100 every time. If the model starts writing either form, the rule would turn a right answer wrong. The suggested fix is to accept only published `*_percent` columns.

### 2.4 Show query examples only for the question type they serve; separate orders from revenue

**Decision.**
- **Comparison examples.** The examples for `period_comparison` are shown only when the question compares periods (`compares_periods`). The group name "giảm đau" and "giảm giá" do not count as the verb "giảm".
- **Shift examples.** The examples for `shift_summary` are shown only for questions about ca sáng / ca chiều.
- **Separate measures.** Revenue and order count get separate examples for both functions.

**Why, in this situation.** Each example is a strong pull on a 7B model. A plain "doanh thu tuần trước" question was answered through the comparison example. An order-count shift question was answered with the revenue example, because it was the only shift example. Hiding examples that do not fit the question removes the pull. The function itself stays listed in the schema, so a question the classifier misses can still use it.

**Rejected.** Writing longer warnings in the example titles. The model copies examples, not prose.

### 2.5 Refuse a percentage once when the question asks for an amount or a count

**Decision.** If the question does not ask for a percentage or a rate (`asks_percent`) and the answer is a percentage, the answer is refused once with "answer the amount or count it asks for". A second percent answer is accepted.

`asks_percent` now also recognises:
- Telex-typed cue words ("phaanf trăm", "tir leej");
- a `%` sign that does not follow a number ("maays %"). A `%` after a number, as in "10%", is part of a product name.

**Why, in this situation.** The question about last week's revenue was answered with a percentage, and the answer check passed it because the number was in the result. A percent answer to a question that asks for an amount is wrong by type. On training data, `asks_percent` now finds all 578 percent questions and flags no other question. So the refusal almost never fires on a real percent question, and the second attempt is accepted in case it does.

**Risk.** The review showed that a percent question typed in a form `asks_percent` still misses would be refused once, and the model might then answer with an amount. The Telex-tolerant cue words close the forms seen in training and the reviewer's examples. Others may exist in unseen data.

### 2.6 Not done in this round, and why

- **Temperature 0.** It would make runs comparable, but it changes every answer at once, so the effect of these fixes could not be measured against the `56b991a` runs. It should be its own measured change.
- **Smaller fixes for single regressions:**
  - a pattern for the number of customers who still owe;
  - limiting the named-product example to questions that name a product;
  - a check that a top-k list has k items;
  - a refusal of an identical failing query.

  Each touches one or two questions in these samples. They are listed in the earlier diagnosis and are left for a later round, so that this round's results stay attributable.
- **"N ngày kể từ / trước đó" windows** (the price-change effect). These need their own date resolution and a quantity comparison, not the revenue comparison function.

## 3. Review of the change

Before the benchmark, three reviewers examined the diff from different angles:

- the answer loop's control flow;
- the question classifiers, measured on all 3,000 training questions;
- whether the percent rules can turn a right answer into a wrong one.

A skeptic then tried to reproduce each finding. Of 12 findings, 3 were upheld and fixed:

| Upheld finding | Fix |
|---|---|
| "tháng liền trc" was not an earlier-period word, so 12 month-over-month questions lost their range | The earlier-period words now include the "trc" forms and every "last month" phrase |
| Year-over-year questions naming both months got a wrong "month before" | The earlier range needs exactly one named period; "cùng tháng / quý năm trước" is recognised |
| The percent refusal could turn a correct answer wrong when the percent word is Telex-typed | `asks_percent` tolerates Telex forms and a bare `%` |

The other 9 findings had scripted reproductions but a precondition that never occurs in the logged model output: a fraction aliased `*percent*`, a `* 100 / 100` expression, two query blocks in one reply, or a percent answer to a non-percent question. Two of them were cheap and were taken anyway: "giảm đau" no longer counts as "giảm", and a repeated query with an answer is read (section 2.2).

## 4. Results

The same questions, bundles (`prepared-fix6`), model and seed as the `*-fix6` runs. Only the answering code changed (runs `*-fix7`). Each cell gives correct / wrong / abstain, then PA.

| Set | Baseline | `56b991a` (`*-fix6`) | These fixes (`*-fix7`) |
|---|---|---|---|
| sample10 | 10 / 0 / 0, +1.00 | 10 / 0 / 0, +1.00 | 10 / 0 / 0, +1.00 |
| setA (40) | 20 / 12 / 8, +0.20 | 26 / 8 / 6, +0.45 | **33 / 4 / 3, +0.725** |
| setB (40) | 17 / 14 / 9, +0.075 | 21 / 9 / 10, +0.30 | **26 / 7 / 7, +0.475** |
| types16 (16) | none | 12 / 3 / 1, +0.56 | **15 / 1 / 0, +0.875** |
| All 106 | | 69 / 20 / 17, +0.46 | **84 / 12 / 10, +0.68** |

**By question type**, over all 106 questions (`*-fix6` → `*-fix7`, correct / wrong / abstain):

| Question type | `*-fix6` | `*-fix7` |
|---|---|---|
| Change against an earlier period | 5 / 2 / 2 | 8 / 1 / 0 |
| Shift | 4 / 3 / 0 | 7 / 0 / 0 |
| Product-group share | 4 / 0 / 3 | 5 / 0 / 2 |
| Top products | 7 / 1 / 0 | 7 / 1 / 0 |
| Other | 49 / 14 / 12 | 57 / 10 / 8 |

**Regressions.**
- **Against the baseline**, regressions fell from 8 to 3:
  - The customer-debt count (2.6).
  - The "every item, counted in cups" question (2.6).
  - A three-month trend question ("tăng liên tục"), which has no query example.
- **Between `*-fix6` and `*-fix7`**, one answer went from correct to wrong:
  - Question: "số ly size M … tính mọi loại đồ uống".
  - What happened: the same date range in both runs, but this time the model filtered on one product name instead of all size-M items.
  - Cause: this is the invented-product-filter failure left for a later round (2.6), not an effect of these fixes. With temperature 0.8, either outcome can occur.

**What each decision did in these runs** (counted from the `*-fix7` logs):

- **2.3 (percent copied ×100):** rescaled 4 answers. All 4 now match the reference: 74 → 0.74, −40 → −0.4, 79 → 0.79 and −35 → −0.35.
- **2.2 (reply with both blocks):** fired in 5 questions. In each, the query ran instead of the reply being ignored.
- **2.4 (examples per question type):** the comparison examples were shown in 10 of 106 prompts, against all 106 before.
- **2.5 (one-time percent refusal):** never fired. No percent answer was given to an amount question.
- **2.1 (earlier period):** all 6 questions where the earlier range had been the primary cause are now correct: Q2 2025 and Q2 2026 revenue, the September average per order, the December morning shift, the May afternoon shift, and a group share for a month outside the data, now answered unanswerable.
- **2.2 and 2.4:** the other 3 diagnosed questions are now correct too: the average per order (2.2), last week's revenue (2.4) and the order-count shift comparison (2.4).

**Timing.** Median 5.4–6.2 s and 90th percentile 8.4–10.6 s per set. 87.5–95% of questions finish within 10 seconds, on team hardware.

**Remaining failures.** These are mostly question types without a rulebook-ready relation or example:
- size or base-unit totals across all items;
- channel and prescription shares;
- customers by number of orders;
- debt by customer;
- payment-method revenue as a list;
- price-change effects;
- trends.

The model is still sensitive to prompt wording and sampling, so each further change needs the same four sets re-run.

## 5. How this was checked

- **Scripted tests** (`make test`, invented data only):
  - date hints with and without comparison words, one and two named periods, and shift comparisons;
  - the comparison classifier;
  - query examples shown per question;
  - a reply with both blocks, and a repeated query with an answer;
  - the percent rescaling, with and without evidence that the cell is a percentage;
  - the one-time percent refusal;
  - Telex percent cues.

  Rewrite: 83 pass. Preparation: 155 run, and the only failure is the one that predates this work (`test_compatible_partition_scales_and_missing_optional_field`).
- **Training data, development use:** the classifiers were measured on all 3,000 training questions. The function reference check, which takes its periods from the date hints, still matches every period-change, shift, top-product and product-group-share question in the 8 prepared businesses.
- **Benchmark:** local 7B model (Qwen2.5-Coder-7B-Instruct, Q4_K_M, llama-server, default sampling) on a laptop GPU, internal scorer, training questions only.

## 6. Not verified

- **Test splits:** no public- or private-test data was used. Only 8 of the 30 training businesses are prepared.
- **Sampling noise:** the model samples at temperature 0.8, and each set ran once. A difference of one or two questions between runs can be noise.
- **Hardware and scoring:** no measurement on the reference hardware or inside an offline container. The internal scorer may differ from the official one.
