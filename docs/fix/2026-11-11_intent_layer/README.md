# Fixed question kinds before the model, and three preparation fixes

Date: 11 November 2026
Status: code and checks done; the public score is not known (section 5).
Branch: `feat/intent-programs`, from `main` at `12d3703` (the merged `fix/sql-answering-accuracy`).
Scope: a new package `tabular_agent/intents/`, its wiring in `cli.py`, three scripts that need bundles but no model, three fixes in `preparation/`, tests, and documents. `sql_agent.py` and its prompts are unchanged.

Start from [the index](../README.md). The measuring commands are in [the runbook](../RUNBOOK.md), sections 12 and 4.

## 1. Situation

The target set for this round was stated as: **the public test with no wrong answer**. No design can guarantee that while the public answers are hidden, and the system was far from it:

| Measure | Value | Source |
|---|---|---|
| work100 (open; rare answer types over-represented), commit `37ee7e9` | 71 / 20 / 9, PA +0.510 | [gate and samples record](../2026-11-10_gate_and_work_samples/README.md) section 4 |
| holdout48 (closed), commit `5e1507b` | 37 / 6 / 5, PA +0.646 | [index](../README.md) |
| Public questions in businesses with no rulebook-ready sales lines | 420 of 1,000 | [index](../README.md) 3.2 of the workflow review |

Two facts shaped the approach:

1. **The training questions are templated.** The 2,852 training questions outside the two closed samples fall into about 40 recurring kinds under noisy typing: no accents (`dthu`, `bnhiu`), Telex residue (`thangs`, `ddown`, `toongr`, `dduwowcj`), abbreviations (`hqua`, `sp`, `ko`). The 7B model writes each query freely, so its errors spread thinly over all kinds, and a change to shared prompt text moves unrelated answers (index, lessons 1 to 3).
2. **A wrong answer costs −1 and an abstention 0** (lesson 6). A recurring kind that code answers from a reviewed query is right every time the kind is recognised, if the query applies the rulebook correctly. That can be checked against the 2,852 reference answers without a model.

## 2. Decisions

### 2.1 Answer recurring kinds with fixed queries, ahead of the model

**Decision.** `cli.py` first calls `answer_by_intent`. The kinds in `tabular_agent/intents/catalogue.py` each have a vocabulary and one query over the `semantic.*` relations that preparation publishes. A recognised question is answered without a model call and logged as an `intent` event. Everything else goes to the unchanged model path. `--no-intents` turns the layer off, so the model alone can still be measured.

**Why, in this situation.** The model path gets 71 of 100 right on work100 and its failures are one or two per kind (index, lessons 1 to 5); more prompt text did not converge. The kinds are what the reference answers show most often: revenue 475, comparison with an earlier period 172, average order 164, orders 151, cancellation rate 136, product quantity 127 and so on (section 3.1).

**Evidence before any model run.** Section 3.1: the kinds claim 2,351 of the 2,852 open training questions and every claim matches its reference answer. The replay calls no model.

**Rejected.**

- *The model fills a small form (kind, product, period) and code computes; a second reader votes.* Not built. The closed-vocabulary reader below already reaches 82.4% coverage at no model cost and at 0 wrong, so the vote would have bought little and would have added a model call and its variance to every question. It stays a later option for kinds outside the vocabulary.
- *More prompt patterns.* Lesson 3: a shared prompt edit moved 5 unrelated outcomes.

**Risk.** The kinds are tuned to the training phrasing. On the public split they claim far fewer questions (section 3.2); the model path still handles the rest, as before.

### 2.2 A question is recognised only when every word is explained

**Decision.** `text.py` repairs typing (accents, abbreviations, doubled letters and tone keys of Telex) and joins phrases (`doanh thu`, `bao nhieu`, `ti le`) into canonical words. A kind claims a question only if each word is one of the kind's cue words, common filler, a period, a number, or a name from the bundle's catalogue. One unexplained word (`trừ voucher`, `khách vip`, a name the bundle does not hold) sends the question to the model.

**Why.** It is the cheapest way to keep the claimed set precise: a qualifier that changes the meaning is, by construction, a word that no kind explains. A miss costs the model's attempt, not −1.

**Evidence.** 0 wrong of 2,351 claims over 30 businesses. `tests/answering/test_intents.py` checks that an unexplained word, two periods where one is expected, and an empty question are not claimed.

**Rejected.** Matching by key phrase with free text around it: it claims `doanh thu tháng 3 của khách vip` as revenue.

**Risk.** Recall on unseen phrasing is limited by the vocabulary; precision on unseen phrasing is not measured by the replay, because the vocabulary was written from the same questions (gate100, section 3.3).

### 2.3 The rulebook's time rules, in one place

**Decision.** `periods.py` turns day, week, month, quarter, "tháng này", "tuần trước", "N ngày gần nhất", "từ đầu năm", date ranges and Tết into inclusive date ranges. Today is the question's `as_of_date`. A month or quarter without a year follows the rulebook (this year unless it starts after today). A comparison base depends on the kind of period: a whole month against the month before, "tháng này" against the same number of days of the month before, a quarter against the quarter before, and "cùng kỳ năm trước" against a year earlier. Tết is looked up in the bundle's `clean.calendar`: lunar 23/12 of the year before to lunar 10/1, inclusive.

**Why.** The prompt carried these rules as text and the 7B model sometimes mis-copied them (date hints record). Code does not.

**Evidence.** `tests/answering/test_intents.py::Periods` (including a leap-year month, the system date never used, an impossible date refused) and 172 revenue-change claims, 5 of them on Tết, all matching.

### 2.4 Names come from the bundle's own catalogue

**Decision.** `entities.py` finds products, product groups, units, sizes (`size M`) and customers by their accent-folded names, longest first, in the names the bundle publishes. A name that two products share once folded is never matched. A unit counts only after `số`, `bao nhiêu`, `mấy`, `theo` or `một`, and never `phần` of `phần trăm`.

**Why.** Questions copy names from the data. Nothing is guessed from similar spellings, so a name that is not in the bundle is not claimed.

### 2.5 "The data does not record it" is `unanswerable`, with a safeguard

**Decision.** An intent answers `unanswerable` when the asked period lies entirely outside the sales dates (rulebook section 7), and, for channel, payment method and customer questions, when the bundle has rulebook-ready orders **and** no source column even looks like the information (`Context.source_mentions`, which also checks the columns that preparation withheld).

**Evidence.** On the open training questions, a bundle without the column answered `unanswerable` for 4 of 4 channel and payment questions and 5 of 5 customer questions; a bundle with the column never did (the counts are in section 3.1). For all 4 bundles without a channel column, no source column had a channel-like name, except biz_124, which the safeguard leaves to the model.

**Rejected.** The same rule for debt: biz_106 and biz_109 have debt in their raw data but no published debt views, and their questions have numeric references. There the layer returns nothing.

**Risk.** A bundle where preparation failed to detect a column whose name looks unlike the hints would be answered `unanswerable` wrongly.

### 2.6 Rankings the public rulebook defines by name

**Decision.** From the public rulebook text alone, with no reference answer: the loyal customer (most days with a purchase), the big spender (highest average order among customers with at least 3 orders), the fickle customer (most cancelled orders), the favourite product (in the most orders), the busiest day (most orders) and the peak hour (whole hour of order time with the most orders, answered by its starting hour). Ties go to the lower code.

**Why.** These terms are new in the public rulebook, so the model has never been shown their definitions and training cannot test them.

**Not verified.** No reference exists. `tests/answering/test_intents.py::FixedQueries.test_rankings_defined_by_name_in_the_rulebook` checks each against a hand-derived result on invented data. The surface wording of the public questions is unknown, so how many they claim is unknown (section 3.2 shows 3 claims in total).

### 2.7 Product-level kinds wait where a dish and its topping share one name

**Decision.** In a bundle where product names join a dish and a topping with " + " (`Trà đào cam sả (M) + Trân châu đen (7.000đ)`), the kinds that depend on what a product is called or counts as do not answer: product revenue and quantity, group revenue and share, top products, unsold products, products in N orders, price-change effect, favourite product and size totals. Totals that do not depend on names (revenue, orders, averages, rankings of customers) are unaffected.

**Why.** The public rulebook makes the topping a product of its own: its quantity and money belong to the topping, not to the dish. Preparation does not split them yet. In dev_002 299 of 349 product names are composite, so product-level answers from those names would be wrong. `tests/answering/test_intents.py::ChannelsAndAbsentInformation` checks the guard.

**Risk.** The model path has the same blind spot there. The split itself is a backlog item (section 6).

### 2.8 Five preparation fixes

Each is generic; none keys on a business id. All bundles were prepared again (generation `r4`) and compared with `r3` and `r2` by `scripts/compare-bundles.py` (section 3.4).

1. **"Thu hộ (COD)" is cash on delivery** (`views.is_cod`). The debt views excluded only the labels `cod`, `thu ho` and `giao hang thu tien`, so a bundle whose label is `Thu hộ (COD)` counted COD orders as customer debt. The replay found 10 wrong debt answers in biz_124 (total debt 2,749,773 against 948,973). After the fix every debt claim matches, and the comparison shows that biz_124's `debt_as_of` is the only thing that changed among 30 businesses.
2. **A README pattern for monthly files scopes every month's file** (`semantics._source_tokens`). dev_002's README defines its columns under `BanHang_T<tháng>_<năm>.xlsx`, but the files are `BanHang_T1_2025.xlsx` and so on, so no definition applied and `Tổng tiền` fell back to the label alias `order_total`. The README says it is `thành tiền sau giảm`, a line amount. With the fix dev_002 and dev_005 publish `line_amount`, hence revenue.
3. **A column whose README definition is a status stays text** (`parsers.infer_parser`). dev_007's `Tiến độ` ("trạng thái đơn") was typed as money because the label contains `tiền`; the status column was withheld, so the sales lines could not be made rulebook-ready.
4. **Header aliases of a sales sheet** (`semantics._ALIASES`): `Đơn số`, `Ngày HĐ`, `Hàng hóa`, `ĐV`, `Dòng`, `Trạng thái giao`, `SL (ĐV cơ bản)`. dev_003 names its columns this way and has no column dictionary.
5. **A single-sheet yearly file continues its sibling's sheet** (`pipeline._relation_base`). dev_003's `BanHang_TongHop_2025.xlsx` has five sheets (named after the sheet) and `BanHang_TongHop_2026.xlsx` has one (named after the file), so one list became two relations.

**Evidence.**

- Unit tests: `PaymentLabels`, `test_a_readme_pattern_for_monthly_files_scopes_every_month`, `test_common_vietnamese_headers_of_a_sales_sheet_get_their_roles`, `test_a_single_sheet_yearly_file_continues_the_sheet_of_its_sibling`.
- Training bundles, `r3` against `r4`: 29 of 30 businesses are identical in columns, roles, row counts, content hashes and table-function results. The one that differs is biz_124, whose stock file now publishes `product_name` (alias 4: `Hàng hóa`), which makes 2 more stock questions answerable and right.
- Public bundles, `r3` against `r4`: exactly dev_002, dev_003, dev_005 and dev_007 differ. dev_003's order states after the fix are 61,254 completed, 1,296 cancelled, 261 returned and 28 shipping, equal to the distinct orders of its raw files, and its data range ends on 21 August 2026, as its README states.
- `scripts/bundle-invariants.py` passes on all 44 bundles of `r4`.

**Rejected.** Adding `Nguồn` as a channel alias: it is a broad label, and without a README definition a wrong channel would be published confidently.

**Risk.** Status labels are mapped by the existing vocabulary and the "one unfamiliar label, one missing state" inference: dev_003's `Khách trả` becomes "returned" only because it is the one label left for the one state left.

### 2.9 An intent attempt closes the bundle before the model may open it

The first model run of this round (work100, `f70a679`) scored 83 / 0 / 17, with a median of 0.0 s per question. The 17 questions that reached the model all abstained with `InvalidInputException: Cannot change configuration option "lock_configuration"`: DuckDB shares one locked instance per file within a process, so the model path could not open a bundle that the intent layer still held. All 227 synthetic tests passed because the runner test mocks the model path. The fix opens the bundle for the attempt and closes it before returning (only the entity catalogue is cached), and `WithTheModelPath` fails without it. Lesson 13 in the index.

### 2.10 A kinds-only mode for the question "may the model guess?"

**Decision.** `--intents-only` answers what the kinds claim and abstains on everything else without asking the model. The default is unchanged: the model answers what the kinds leave.

**Why.** The target "no wrong answer" and the PA metric pull in different directions on the unclaimed questions. A wrong model answer costs −1 and an abstention 0, so abstaining is better wherever the model is wrong more often than right. On the 15 questions of work100 that reached the model, it got 4 right, 7 wrong and abstained on 4 (section 3.3); that is a small and hard residual, not an estimate for the public split, where the unclaimed questions include ordinary kinds in unfamiliar wording. Only a public upload or a dry run can say which mode scores higher there.

**Not decided here.** Which mode to submit. It is a team decision; the upload log in the index records each upload with its mode.

### 2.11 How the work was checked without a model

- `scripts/oracle-replay.py` runs the kinds over every training question except gate100 and holdout48 and compares with the references by the internal scorer.
- `scripts/bundle-invariants.py` compares line revenue with order revenue month by month, the order states with `counts_as_sale`, and looks for missing dates.
- `scripts/claim-coverage.py` counts the claims of any split per business. It reads no answer and prints no question, and it was the only way public questions were used: **no phrase, word or kind was added from the text of a public question**.

## 3. Results

The internal scorer is not the official one; all questions are training questions unless noted.

### 3.1 Oracle replay on the open training questions

Bundles `artifacts/prepared-r4-train`, code `98e5e46`. 2,852 questions (3,000 minus gate100 and holdout48).

| Measure | Value |
|---|---|
| Claimed by a kind | 2351 (82.4%) |
| Right / wrong | 2351 / 0 |
| Left to the model | 501 |

| Kind | Claimed | Right |
|---|---|---|
| `revenue` | 475 | 475 |
| `revenue_change` | 172 | 172 |
| `average_order_value` | 164 | 164 |
| `orders` | 151 | 151 |
| `cancellation_rate` | 136 | 136 |
| `product_quantity` | 127 | 127 |
| `best_day` | 120 | 120 |
| `product_revenue` | 100 | 100 |
| `cancelled_orders` | 95 | 95 |
| `top_products` | 83 | 83 |
| `group_share` | 77 | 77 |
| `price_change_effect` | 76 | 76 |
| `top_customers_by_money` | 68 | 68 |
| `group_revenue` | 48 | 48 |
| `shift_total` | 47 | 47 |
| `size_total` | 43 | 43 |
| `channel_revenue` | 27 | 27 |
| `stock_units` | 25 | 25 |
| `debt_collected` | 24 | 24 |
| `shift_comparison` | 23 | 23 |
| `monthly_revenue` | 22 | 22 |
| `revenue_trend` | 22 | 22 |
| `orders_over_amount` | 22 | 22 |
| `payment_revenue` | 22 | 22 |
| `unit_total` | 21 | 21 |
| `days_over_amount` | 19 | 19 |
| `absent_attribute` | 18 | 18 |
| `products_in_orders` | 18 | 18 |
| `unsold_products` | 16 | 16 |
| `best_month` | 15 | 15 |
| `top_customer_by_orders` | 14 | 14 |
| `customers_with_min_orders` | 14 | 14 |
| `customers_min_orders_share` | 13 | 13 |
| `top_debtors` | 12 | 12 |
| `debtor_count` | 9 | 9 |
| `total_debt` | 7 | 7 |
| `customer_debt_flow` | 6 | 6 |

Claims by reference type: number 1428, percent 507, list 144, date 133, string 63, unanswerable 42, boolean 22, table 12.

**Unclaimed, by crude kind** (not exclusive of vocabulary gaps):

| Kind of question | Questions | Why it is not claimed |
|---|---|---|
| Selling price on a date, average import price | 108 | Needs the price history and unit conversion; both rules were removed from the public rulebook |
| Pharmacy attributes (prescription, active ingredient, lots, expiry) | 98 | The drug list has no semantic roles; each pharmacy names its columns differently |
| Debt by age or by customer, in bundles that publish no debt views | 54 | No published relation (biz_106, biz_109) |
| Stock value, out-of-stock items, stock by lot | 51 | No published relation; a bundle without stock data answers `unanswerable` for 10 of 27 such questions and a number for 17, with no rule that separates them |
| Purchases (receipts, suppliers) | 49 | Receipt layouts differ per business |
| Customer group (`khách nhóm sỉ`) | 40 | The group column has no role |
| Staff, deliveries, profit and cost | 44 | No published relation |
| Price change with no date in the question | 6 | Needs the price history to find the date |
| Other | 51 | Vocabulary gaps, and `unanswerable` kinds the data cannot show

### 3.2 Claims on the public split

`scripts/claim-coverage.py --split data/public_test`, bundles `r4-public`:

| Business | Questions | Claimed | of which `unanswerable` | To the model |
|---|---|---|---|---|
| dev_001 | 87 | 25 | 2 | 62 |
| dev_002 | 41 | 5 | 0 | 36 |
| dev_003 | 129 | 23 | 0 | 106 |
| dev_004 | 96 | 30 | 2 | 66 |
| dev_005 | 87 | 15 | 1 | 72 |
| dev_006 | 111 | 18 | 1 | 93 |
| dev_007 | 60 | 16 | 0 | 44 |
| dev_008 | 99 | 19 | 0 | 80 |
| dev_009 | 21 | 10 | 0 | 11 |
| dev_010 | 59 | 16 | 0 | 43 |
| dev_011 | 40 | 0 | 0 | 40 |
| dev_012 | 50 | 0 | 0 | 50 |
| dev_013 | 57 | 1 | 1 | 56 |
| dev_014 | 63 | 1 | 1 | 62 |
| **all** | 1000 | 179 | 8 | 821 |

Claims by kind: revenue 43, orders 14, revenue_change 12, top_products 11, product_revenue 8, absent_attribute 8, average_order_value 8, cancelled_orders 6, days_over_amount 6, best_day 5, customers_with_min_orders 4, shift_comparison 4, orders_over_amount 4, cancellation_rate 4, group_revenue 3, customers_min_orders_share 3, monthly_revenue 3, best_month 3, channel_revenue 3, top_customer_by_orders 2, products_in_orders 2, unit_total 2, group_share 2, revenue_trend 2, shift_total 2, product_quantity 2, payment_revenue 2, unsold_products 2, top_customers_by_money 2, peak_hour 1, price_change_effect 1, customer_debt_flow 1, big_spender 1, busiest_day 1, top_debtors 1, total_debt 1.

**This is the main finding of the round.** On training the kinds claim 82.4% of the questions; on the public split they claim 17.9%. Three reasons are visible without reading a public question:

- 231 of the 1,000 public questions are in businesses with no rulebook-ready sales lines: dev_011 and dev_014 (phone shops), dev_012 (repair shop), dev_013 (mother and baby), and dev_009, which has invoice headers only. Their layouts are new industries and notebook-style ledgers.
- The public rulebook defines kinds that training lacks, and the questions about them are phrased in ways the training vocabulary does not cover.
- Dish-and-topping names (dev_002) switch the product-level kinds off (decision 2.7).

The public questions were not read to widen the vocabulary (section 2.11). Widening it from them would make the layer fit the public test rather than the task.

### 3.3 Model runs

Local model `local-coder-7b` (Qwen2.5-Coder-7B-Instruct, Q4_K_M), temperature 0, 6 steps, a fresh server per set, a worktree pinned at the measured commit with a clean tree ([runbook](../RUNBOOK.md) sections 3 and 4). Bundles `artifacts/prepared-r4-train`.

| Set | Mode | Commit | Correct / wrong / abstain | PA | Median, p90 per question | Run folder |
|---|---|---|---|---|---|---|
| work100 (open) | baseline, model alone, bundles `r2` | `37ee7e9` | 71 / 20 / 9 | +0.510 | 7.3 s, 13.7 s | `runs/work100-r3s0` |
| work100 (open) | first run with the kinds: **invalid** (decision 2.9) | `f70a679` | 83 / 0 / 17 | +0.830 | 0.0 s, 0.3 s | `runs/work100-r4i1` |
| work100 (open) | model alone | `83dca1a` | 72 / 18 / 10 | +0.540 | 7.2 s, 14.2 s | `runs/work100-r4m` |
| work100 (open) | kinds, then model | `83dca1a` | 89 / 7 / 4 | +0.820 | 0.1 s, 7.6 s | `runs/work100-r4i2` |
| work100 (open) | kinds, then model | `9b8ad23` | 91 / 5 / 4 | +0.860 | 0.1 s, 7.1 s | `runs/work100-r4i3` |
| gate100 (closed) | kinds, then model (default) | `9b8ad23` | 85 / 10 / 5 | +0.750 | 0.1 s, 8.7 s | `runs/gate100-r4i3` |
| gate100 (closed) | kinds only (`--intents-only`) | `9b8ad23` | 82 / 0 / 18 | +0.820 | 0.1 s, 0.4 s | `runs/gate100-r4k` |

**Reading them.**

- **work100 is open, and the kinds were written from the open questions**, work100's among them, so its +0.860 says what the kinds do on questions they were built from, not what they do on new ones. Of its 100 questions at `9b8ad23`, 87 were answered by a kind (all 87 right) and 13 went to the model (4 right, 5 wrong, 4 abstained). The model alone scored +0.540 on the same bundles, against +0.510 before the preparation fixes.
- **gate100 is the honest number for new questions of known businesses.** Its kinds-only mode answers 82 questions, all right, and abstains on 18: precision 100% and coverage 82%, in line with the 82.4% of the open pool. That is a +0.820 PA with no wrong answer, above the +0.80 target. The default mode asks the model about the other 18 and gets 3 right, 10 wrong and 5 abstentions, so it scores +0.750, below the target. On this evidence the model's answers on what the kinds leave are net negative, on the open set as well (work100: 4 right, 5 wrong).
- **Neither mode is a public result.** The gate does not see the public split, where the kinds claim 17.9% (section 3.2), nor businesses without rulebook-ready sales lines, nor the context limit of dev_006 (index, 3.2 of the workflow review). On the public split the model may answer ordinary kinds in unfamiliar wording better than it answers the hard residue of training, so the better mode there is unknown. The upload log in the index should record the mode of each upload; uploading `./predict.sh` and `./predict.sh --intents-only` once each would show which is better.
- **The gate was read twice, at `9b8ad23`**, once per mode. A later commit (`98e5e46`) adds two wordings to the vocabulary (28 more open training claims, none wrong); the gate was not read again.

**Failure table, work100 at `9b8ad23` (open set; all 9 reached the model).**

| Business | Answer type | Kind | Outcome | Cause | Backlog |
|---|---|---|---|---|---|
| biz_124 | string | product with the highest gross profit ("lời nhất") | wrong | no gross-profit relation | B26 |
| biz_109 | number | customers who still owe | wrong (251 against 111) | no debt views in this partial bundle | B27 |
| biz_125 | number | selling price on a date | abstain | no price relation | B20 |
| biz_118 | number | deliveries in a month ("đợt giao") | wrong | no delivery relation | B26 |
| biz_105 | percent | price change of a topping, date given | abstain | the question's wording was not in the vocabulary; fixed at `98e5e46` | none |
| biz_120 | number | revenue by active ingredient | wrong | drug list has no roles | B21 |
| biz_109 | list | one customer's new debt and repayments | abstain | no debt views | B27 |
| biz_108 | number | revenue of a customer group | abstain | customer group has no role | B22 |
| biz_130 | unanswerable | stock of a product | wrong (answered 9) | the bundle holds no stock data; no rule separates this from the 17 stock questions with a number | B28 |

### 3.4 Bundles and tests

| Check | Result |
|---|---|
| `compare-bundles.py` `r2` → `r3`, 30 training bundles | 1 changed: biz_124's `debt_as_of` at the last date, 40 rows → 25 (the cash-on-delivery label). The other 29 are identical in columns, roles, rows, content hashes and function results |
| `compare-bundles.py` `r3` → `r4`, 30 training bundles | 1 changed: biz_124's stock file gained `product_name` (header alias). The other 29 are identical |
| `compare-bundles.py` `r3` → `r4`, 14 public bundles | 4 changed: dev_002, dev_003, dev_005, dev_007 (fixes 2.8.2 to 2.8.5). The other 10 are identical |
| `bundle-invariants.py`, `r4` | 44 bundles, 0 problems; 4 public bundles have no rulebook-ready sales relation (dev_011 to dev_014) |
| Independent recomputation of the six rankings of decision 2.6 in Python over the rulebook-ready orders and lines of the 30 training bundles | loyal customer 393 cases, fickle customer 393, big spender 393, busiest day 424, favourite product 411, peak hour 161, no mismatch. This checks the SQL, not the reading of the rulebook |
| `make test` | `tests/answering` 62 tests OK; `tests/preparation` 180 tests OK with 1 expected failure (B17) |
| `make check` | compiles; no whitespace error |

Tests added: 25 in `tests/answering/test_intents.py` (text repair, periods, every kind family on an invented bundle, the model path opening a bundle after an attempt, the topping guard, absent information), 1 in `tests/answering/test_runner.py` (`--intents-only`), 5 in `tests/preparation/` (cash-on-delivery labels, README patterns for monthly files, header aliases, yearly files, documented status columns).

## 4. Closed samples and evidence

- **gate100**: read twice on 11 November at `9b8ad23`, once in each mode (section 3.3). Totals only: its questions, answers and logs were not opened, and an earlier run of the gate at `52b54b4`, made before two code changes, was deleted unread.
- **holdout48**: not run this round. Its totals have been read 7 times (index).
- **Evidence ids.** The reference answers of every open training question were used by the replay. Nothing about gate100 or holdout48 was loaded: `oracle-replay.py` removes their ids before reading answers, and the vocabulary was built from open questions only.

## 5. Where this leaves the target

"No wrong answer on the public test" is not reached, and nothing here shows it can be. What the round adds:

1. A layer that answers the questions it recognises with no model variance, whose precision can be re-measured at any time without a model (`oracle-replay.py`).
2. A way to count, for any split and without reading answers, how much of it the layer covers (`claim-coverage.py`) and whether the bundles agree with themselves (`bundle-invariants.py`).
3. A measured gap: training phrasing is covered at 82.4%, public at 17.9%. The 100% target depends on that gap, on the six public businesses with no sales relation, and on the model path's accuracy for everything left.

Recommended next steps, in order of expected effect on the public score (all unverified until a public upload or a dry run):

1. **Publish sales relations for the public businesses that still have none** (backlog B2): dev_003 needs its two yearly files read as one relation, header aliases (`Đơn số`, `Ngày HĐ`, `Hàng hóa`, `ĐV`) and a decision on the status label `Khách trả`; dev_011 and dev_014 (phone shop) need the ledger layout read with their rulebook rules; dev_012 (repair shop) and dev_013 (mother and baby) need their own kinds. Check each with `bundle-invariants.py` before use.
2. **Split dish and topping in preparation** (decision 2.7), so that dev_002's product-level kinds, and the model's, can work.
3. **Implement the remaining public-rulebook kinds from their definitions**, each with a second independent computation and a hand-derived test: average per weekday, main supplier, hardest-working staff.
4. **Run the full public dry run** (backlog B6) and read it with `dry-run-report.py`, to see abstentions and the time budget with the layer in place.
5. Only then spend public uploads; with every question answered, wrong answers = (1 − PA) × N / 2 exactly, which tells how far the system is from zero.

## 6. Backlog changes and not verified

- **B2** (public layouts with no sales relation): dev_002, dev_005 and dev_007 now publish one (fixes 2.8.2 and 2.8.3). Still without: dev_003, 011, 012, 013, 014, and dev_009 (headers only).
- **B3** (dev_007 `Tiến độ` parsed as money): done, fix 2.8.3.
- **New:** B19 split dish and topping in preparation (2.7); B20 selling price on a date and the price history as a semantic relation; B21 pharmacy attributes (prescription, active ingredient, lots) from the drug list; B22 customer group role; B23 receipts and suppliers as a semantic relation; B24 the remaining public-rulebook kinds (2.6, section 5.3); B25 a rulebook-ready relation for the phone-shop, repair-shop and mother-and-baby layouts; B26 gross profit, cost and deliveries as semantic relations; B27 debt views for the partial bundles biz_106 and biz_109; B28 stock for bundles that publish no stock snapshot (when is the answer `unanswerable`?).
- **Not verified:** the public score of anything here; precision on questions phrased unlike the training ones; the rankings of decision 2.6 against references; the `unanswerable` rule of decision 2.5 on bundles whose columns preparation did not detect; that the internal scorer agrees with the official one (backlog, workflow review 3.9); the full public dry run and the 6-hour budget with this layer; any claim of offline operation beyond what the entrypoint record shows.
