# Decision record: answering with SQL over prepared data

Date: 9 November 2026
Status: implemented on branch `fix/sql-answering-accuracy`; measured on team hardware only
Scope: the SQL answering path (`tabular_agent/sql_agent.py`) and the preparation rules and views it depends on (`tabular_agent/preparation/`).

This document records each decision, why it was made, what was rejected, and the evidence. Business identifiers and question counts below are evidence references only. No rule in the code branches on a business or question identifier, and no reference answer is stored in code.

## 1. Summary

The answering agent used to read raw files and clean them inside each question. It now can answer from a prepared DuckDB bundle whose relations already apply the rulebook.

Development measurements, local 7B model (Qwen2.5-Coder-7B-Instruct, Q4_K_M) on a laptop GPU, internal scorer:

| Run | Questions | Correct | Wrong | Abstain | PA | Median seconds |
|---|---|---|---|---|---|---|
| Raw files (before) | 10, seed 42 | 0 | 1 | 9 | −0.10 | 29.3 |
| SQL over first bundles | same 10 | 1 | 8 | 1 | −0.70 | 5.6 |
| SQL after sections 2.1–2.6 and 3.1–3.9 | same 10 | 10 | 0 | 0 | 1.00 | 5.3 |
| SQL after sections 2.1–2.6 and 3.1–3.9 | 39 other questions, same 8 businesses | 18 | 19 | 2 | −0.03 | 6.4 |
| SQL after all decisions (final) | same 10 | 10 | 0 | 0 | 1.00 | 5.5 |
| SQL after all decisions (final) | same 39 other questions | 19 | 12 | 8 | 0.18 | 6.6 |

The 10 questions were used to find problems, so their score is not an accuracy estimate. The 39 other questions are the better estimate for these eight businesses: their failures were read once, after the first measurement, to choose what to do next (sections 2.7–2.10 and 3.10), but no change was tuned to an individual question among them. These numbers say nothing about businesses that were not prepared, the public or private split, or the reference hardware.

Between the first and the final measurement on the 39 questions, no previously correct answer was lost: 18 stayed correct, 1 wrong answer became correct, and 6 wrong answers became abstentions.

## 2. Decisions in the answering path

### 2.1 Answer with SQL over a prepared bundle, behind a flag

**Decision.** `bench` and `predict` take `--prepared <directory>`. With it, the model writes `SELECT` statements against the bundle. Without it, the raw-file agent runs unchanged.

**Why.** On the 10 sample questions the raw-file agent abstained 9 times: 6 from repeated code errors and 3 because the request outgrew the 16,384-token context. One log showed it reading a CSV with the first data row as the header. Typed tables remove that whole class of failure, and a query is shorter than file-reading code, so requests stay small.

**Rejected.** Replacing the raw-file agent outright. The new path is not yet proven on enough questions, and a flag lets both run on the same questions.

**Evidence.** Median time per question fell from 29.3 s to 5.3 s. No context-size or file-parsing error occurred in any SQL run.

### 2.2 Check every query against an allowlist before running it

**Decision.** Each statement is parsed with DuckDB's own parser. It must be exactly one `SELECT`, and every relation and table function it names must be published in the bundle's schema. The connection is read-only, external file access is disabled, and configuration is locked.

**Why.** The bundle also holds private audit tables with raw rows, and DuckDB can read arbitrary files through table functions. A prompt instruction is not a control; the parser check is.

**Rejected.** Text matching on the SQL string, which is easy to bypass with quoting or comments.

**Evidence.** Tested rejections: a private audit table, `read_csv`, `duckdb_tables()`, `information_schema`, `DROP`, and two statements in one request.

### 2.3 Show the values of short label columns

**Decision.** For text columns with at most 12 distinct values, the prompt lists those values. Names, identifiers, notes, phone numbers and addresses are never listed.

**Why.** Status, unit and payment-method labels differ by business. Without them the model invented labels: in the first SQL run it filtered on a status label that the data does not contain.

**Trade-off.** The original plan said the schema carries no value lists. This is a deliberate, narrow deviation: the values are computed at question time, are not stored in `schema.json`, and exclude customer and product lists.

### 2.4 Give the model dates, coverage and query patterns computed by code

**Decision.** The prompt carries:

- explicit inclusive date ranges for the rulebook's relative periods (today, yesterday, this week, last week, this month, last month in both senses, quarter and year to date, last 7 and 30 days), computed from `as_of_date`;
- the first and last order date in the data;
- one query pattern for each rulebook-ready relation that this bundle actually has;
- a short glossary of common chat abbreviations (`hqua`, `hnay`, `dthu`, `bn`, …).

**Why.** Each item answers an observed error. The model summed every day up to yesterday instead of yesterday alone, and it missed `hqua` as "yesterday". Date arithmetic and the week and month rules are deterministic, so code should do them. Showing only patterns for relations that exist keeps the prompt relevant to the business.

**Rejected.** Letting the model compute dates in SQL from the reference date. It is error-prone and risks use of the system date.

### 2.5 Match names with one accent-insensitive function

**Decision.** Each connection defines `name_like(column, 'text')`, which compares accent-stripped, lower-cased text. The instructions tell the model to use it for any product or customer named in a question.

**Why.** Questions are often typed without accents, and data spellings vary. The model had written an accent-stripped column compared with an accented pattern, which can never match, and then concluded the data did not exist.

### 2.6 Normalize the final answer's number format in code

**Decision.** Numeric text in a number, percent, list or table answer becomes a JSON number. Integral floats become integers. Percentages are rounded half-up to two decimals. Text with a leading zero is treated as a code and left alone.

**Why.** A correct stock answer was returned as `["13", "2"]` and scored wrong, because the type and format must match exactly. Format rules that do not depend on the question belong in code.

### 2.7 Accept `unanswerable` only when code can confirm it

**Decision.** An `unanswerable` answer is accepted only when the asked period lies entirely outside the data's sales dates. The period comes from the answer's own `"period"` field, or else from the date literals of the last query. Otherwise the model gets one specific correction and one more attempt; a second unconfirmed `unanswerable` is returned as an abstention.

**Why.** On the 39 other questions the model answered `unanswerable` 11 times and 10 of those were wrong. In each wrong case a query had matched nothing because of its own filters (a name compared with `=`, the system date, a column that does not exist), and the model then concluded the data did not exist. A wrong answer scores −1 and an abstention 0, so an unconfirmed "no data" claim is a bad bet. The rulebook limits `unanswerable` to a period entirely outside the data or information the business never records; the first is checkable, so code checks it.

**Trade-off.** "The business never records this" cannot be confirmed by code, so those cases now end as abstentions (0) instead of a possible +1. No such case was answered correctly before the change, so nothing measured was lost. Reference answers of type `unanswerable` are 70 of 3,000 training questions.

**Evidence.** `unanswerable` answers on the 39 questions fell from 11 to 1, and that one is correct. Wrong answers fell from 19 to 12.

### 2.8 Refuse queries that use the system date

**Decision.** A query that references `CURRENT_DATE`, `now()`, `today()` or similar is rejected with a pointer to the Dates block.

**Why.** Relative time must be resolved against `as_of_date`, never the system date. The model used `CURRENT_DATE` in a "this month" question; the query ran, matched nothing, and produced a wrong `unanswerable`. A rejected query gives the model a reason to correct itself, which a silent empty result does not.

### 2.9 Several numbers are a list; a table needs a named object

**Decision.** A table body written beside `"type"` is moved under `"answer"`. A one-row table whose cells are all numbers becomes a list. Invalid answers get feedback that shows the valid shapes.

**Why.** For a stock question the model had the right two numbers and returned them five times as a malformed table until the step limit. The rulebook says several numbers asked together are a list in the order asked, and a table has one named object per row with the name first. A one-row, all-number table therefore has no valid reading as a table.

### 2.10 Resolve the date phrases found in each question

**Decision.** Besides the general Dates block, the prompt lists each relative date phrase that occurs in the question ("bữa qua", "tuần rồi", "7 ngày gần nhất", …) with its explicit date or range. Phrases are matched on accent-folded words.

**Why.** After an unrelated instruction edit, the model read "bữa qua" (yesterday) as a seven-day range, although the Dates block listed it. For a fixed prompt the model's output is the same on every run, so this was prompt sensitivity, not chance: any edit can flip borderline questions. Stating the phrase's meaning next to the question makes the answer depend less on the rest of the prompt. Relative dates occur in about 20% of training questions.

## 3. Decisions in the preparation layer

### 3.1 Map status labels to the rulebook's four states; infer one label by elimination

**Decision.** Labels map to `completed`, `shipping`, `cancelled` or `returned` by an exact, accent-folded vocabulary. Qualified or negated labels never match. If exactly one label of a source is unfamiliar and exactly one state has no label, that label is assigned that state. Any other unfamiliar label still blocks the sales relations.

**Why.** The previous vocabulary knew few labels, and one unfamiliar label blocked every sales relation: sales relations were published for 1 of the 8 sample businesses. The rulebook states that every order has exactly one of four states and that different names for the same thing are the same thing, so mapping is licensed by the rules. Elimination follows from "exactly one of four".

**Rejected.** Substring matching ("contains huỷ"), which would read "không huỷ" as cancelled. Also rejected: publishing with unknown orders set aside, which would silently drop sales.

**Evidence.** After the change, sales relations are published for 8 of 8 sample businesses. A scan of the status columns of order tables in all 30 training businesses found one label outside the vocabulary; elimination resolved it in the sample business that has it, and that business's reference revenue then matched.

**Risk.** Elimination can be wrong if a source uses two labels for one state and none for another. It is recorded as an issue (`STATUS_INFERRED_BY_ELIMINATION`) so it can be reviewed.

### 3.2 Inherit order-level cells on continuation rows

**Decision.** When the rulebook's blank-cell rule is present, a row with no order ID belongs to the group started by the last row that has one. Only columns that are blank on every such row inherit the group's first-row value. Rows before the first order ID stay blank.

**Why.** In one sample business 38,878 of 82,920 line rows had no order ID, date or status. A date filter dropped them, and the quarterly revenue came out at about half the reference. The rulebook says these blanks mean "same as the first row of the group", not missing.

**Why the narrow column rule.** Filling every blank cell could overwrite a line-level value that is truly empty, such as a discount. A column that is blank on every continuation row is structurally an order-level column.

**Evidence.** After the change the reference quarterly revenue matched exactly.

### 3.3 Accept a weak header when independent evidence supports it

**Decision.** A first-row header with no recognised header words is accepted when either the README data dictionary documents at least half of its labels (and at least two), or its cells are text sitting over columns that parse as numbers or dates. Otherwise the table stays blocked.

**Why.** Product and order lists with correct but unfamiliar headers were blocked, which withheld product base units and one business's whole order table. Both forms of evidence are the ones the handoff plan itself named: labels matching the README, and a type shift between the header and the records.

**Rejected.** Adding more words to the header vocabulary. It does not generalize: recognised column labels fell from 76% on training to 53% on public-test files in the earlier audit.

**Risk.** The type-shift rule could accept a first data row as a header if that row happens to be all text over numeric columns. It applies only when no row has stronger evidence.

### 3.4 Corroborate serial dates by their written-out neighbours

**Decision.** In a CSV or SQLite column that mixes written dates with Excel serial numbers, an epoch is accepted when the written dates never decrease down the file, every serial value falls between its nearest written neighbours, and exactly one epoch satisfies this.

**Why.** One order table had 16,594 serial values among 66,469 dates and no second date column to compare against, so the whole date column was withheld and no sales relation could be dated. The previous rule required a separate timestamp field.

**Why it is safe.** The 1900 and 1904 epochs differ by four years. In a chronologically ordered column only one can sit between its neighbours. If the column is not ordered, or both or neither epoch fits, nothing is chosen and the column stays withheld.

### 3.5 Name relations by sheet when a workbook holds different lists

**Decision.** A workbook whose sheets have different names publishes one relation per sheet, named after the sheet. Period suffixes on file and sheet names (`_1`, `_T1_2025`, `_Thang01_2025`, `01.2025`) are dropped so parts of one table share a name and are assembled.

**Why.** Every sheet of a multi-list workbook was named after the file, so seven different lists shared one name. One blocked sheet then blocked all seven, and the rest got hash-suffixed names the model could not interpret.

### 3.6 Publish relations that already apply the rulebook

**Decision.** The sales relations carry the rule, not just the data:

- `semantic.orders` has `order_state`, `counts_as_sale` and the order's `revenue` (sum of its line amounts).
- `semantic.eligible_sales_lines` carries `order_date` on every line, also when the date lives in a separate order table.
- A book with one sale per row and no order IDs gets `semantic.eligible_sales_lines` from its own row status.
- `semantic.product_quantities` carries the product's `base_unit` from a verified catalogue.

**Why.** With clean but rule-free tables the model made rule mistakes in 8 of 10 questions: it forgot to exclude cancelled and returned orders, counted rows instead of orders, and averaged per line instead of per order. With these relations each of those questions is a one-line query. The rules are deterministic and stated in the rulebook, so code should apply them once.

**Evidence.** A one-line query against these relations returns the reference answer for all 10 sample questions.

### 3.7 Debt as a table function and single-date relations

**Decision.** `semantic.debt_as_of(DATE)` returns the orders still owing at the end of a date. `semantic.debt_repayments` and `semantic.debt_added` cover repayments and new debt of credit orders. Each relation exposes exactly one date column that decides the period.

**Why a function.** Outstanding debt depends on a date chosen by the question, so a fixed view cannot hold it. The rulebook's definition has six conditions (unpaid part of the order total, payments up to the date, not cancelled, returned orders included, cash-on-delivery and marketplace orders excluded, only positive balances). The model summed all payments instead.

**Why one date column.** With both the order date and the payment date exposed, the model filtered repayments by order date. Removing the wrong column removed the error.

**Evidence.** Total debt and four further repayment references that were not in the sample match exactly, as does a count of owing customers.

**Limits.** Published only when the order table has an order total and a payment method, exactly one payment table links to every order, and a credit label is recognised. Otherwise nothing is published.

### 3.8 Stock in large units, valid for one date only

**Decision.** `semantic.stock_snapshot` splits end-of-data stock into whole large units and loose base units, and carries `stock_date`.

**Why.** The rulebook defines stock in large units as a two-number list. The README states that the stock report is the stock at the end of the last day with data, so it answers a question only for that date. Exposing the date lets the model check it; historical stock is not reconstructed.

### 3.9 New column roles

**Decision.** Added roles for payment method, sales channel, remaining amount, product group and employee, recognised from README descriptions and common labels.

**Why.** The debt rules need the payment method and channel, and these columns previously had no role, so no rule could depend on them.

### 3.10 Use the day of the order timestamp when no order date is recorded

**Decision.** When an order table has an order timestamp but no order date column, `semantic.orders` and the sales lines carry `order_date` as the day of that timestamp.

**Why.** The rulebook assigns an order to a period by the day it was placed. One sample business records only a timestamp. Its relations then had no `order_date`, so the query patterns did not apply, the data range was unknown (which made the check in 2.7 impossible there), and the model compared the timestamp with whole dates, which drops every sale after midnight on the last day of a range.

### 3.11 Period functions for the question types that failed most

**Decision.** Four table functions take an inclusive date range and return the finished figures, so each of these question types is one call:

- `semantic.group_revenue_share(first_day, last_day)`: each product group's revenue and `share_percent` of the whole shop's revenue. The denominator includes lines without a group and service-fee lines, as the rulebook says.
- `semantic.period_comparison(first_day, last_day, base_first_day, base_last_day)`: revenue and order count of the asked period against a base period, the difference (asked − base) and the percent change. An empty period gives NULL, not 0.
- `semantic.shift_summary(first_day, last_day)`: revenue and order count of the morning and afternoon shifts, with each shift's percent difference from the morning shift, which the rulebook names as the base.
- `semantic.top_products(first_day, last_day)`: products ranked by base quantity, ties by code (by name without codes). When the rulebook says service-fee lines are not products, they are left out.

Percentages are computed in SQL and rounded half-up to two decimals. Each function is published only when the sales relations carry the columns it needs. In the answering path, these functions' query patterns replace the multi-step patterns for the same question types. A new `name_is(column, 'text')` macro matches a group name exactly, ignoring accents, case and spacing, because the rulebook keeps similarly named groups apart (`trà` is not `trà sữa`). `date_phrases` now resolves "cùng kỳ năm trước / năm ngoái" beside a named month or quarter as the same months a year earlier, and it reads the Telex typo `thangs` as `tháng`.

**Why.** These question types were the most common failures in section 6. Each needed two or three aggregates plus arithmetic, and the model got one of the steps wrong. For example, it filtered on a column that does not exist, ignored the shift filter, returned a table for a top-k list, or used the wrong base. In the training questions, the four types make up about 15% of all questions (by keyword count: 261 change, 88 top, 77 shift and 21 group share, of 3,000; the change count includes some that use another base, such as price-change effects).

**Rejected.** Fixed views precomputed per month: questions use arbitrary ranges (last week, month to date, Tết). Group matching with `name_like` (substring): it would also match `trà sữa` for `trà`.

**Evidence (development check, not a model run).** On copies of the `prepared-fix5` bundles, a script resolved each question's period with `date_phrases`, called the matching function, and compared the result with the training reference. All of these questions from the 8 prepared businesses matched: 39 of 39 change, 16 of 16 shift, 19 of 19 top products and 13 of 13 product-group share. The checker's own keyword rules picked out these questions. It also picked out five more "tỉ trọng" questions that ask about a channel's or a customer group's share, which is a different type and not covered. Before the date-phrase change, the three "same period last year" questions did not match. This shows that the functions compute the right figures. It does not show that the model will call them correctly.

## 4. How this was checked

- Scripted tests: `make test` (invented data only). New tests cover the status mapping, relation names, header evidence, group inheritance, serial-date corroboration, the sales, debt and stock relations, the query guard, the answer format rules, date phrases, and the answering loop with a scripted model (`tests/preparation/test_business_views.py`, `tests/preparation/test_sql_agent.py`). The preparation suite has one failure that predates this work (`test_compatible_partition_scales_and_missing_optional_field`).
- Training data, development use: the 10 sample questions and 39 other questions, as in section 1. Reference answers were read only by the scorer after inference, and by hand-written checks of the relations.
- Query guard: tested with the rejected statements listed in 2.2.

## 5. Not verified

- The new rules have not been run on the other 22 training businesses or on any public-test data file.
- No measurement on the reference hardware or inside an offline container.
- The internal scorer may differ from the official one.
- Only one model was measured. Earlier analysis found the model, not the data, to be the largest factor in accuracy.

## 6. Known remaining failures (39 other questions, final measurement)

12 wrong and 8 abstained. Of the abstentions, 6 were the model's own choice after failed queries and 2 hit the step limit.

- Most failures are question types with no rulebook-ready relation yet: share of revenue by product group, change against the previous period, morning and afternoon shift, top products, customers by number of orders, price-change effect. Section 3.11 adds functions for the first four; they have not been measured with the model yet.
- A few are format slips, such as a correct percentage returned with type `number`.
- The model is sensitive to prompt wording (section 2.10). Any prompt change needs both question sets re-measured.
