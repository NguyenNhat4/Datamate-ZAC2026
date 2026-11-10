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

**Evidence before any model run.** Section 3.1: the kinds claim 2,299 of the 2,852 open training questions and every claim matches its reference answer. The replay calls no model.

**Rejected.**

- *The model fills a small form (kind, product, period) and code computes; a second reader votes.* Not built. The closed-vocabulary reader below already reaches 80.6% coverage at no model cost and at 0 wrong, so the vote would have bought little and would have added a model call and its variance to every question. It stays a later option for kinds outside the vocabulary.
- *More prompt patterns.* Lesson 3: a shared prompt edit moved 5 unrelated outcomes.

**Risk.** The kinds are tuned to the training phrasing. On the public split they claim far fewer questions (section 3.2); the model path still handles the rest, as before.

### 2.2 A question is recognised only when every word is explained

**Decision.** `text.py` repairs typing (accents, abbreviations, doubled letters and tone keys of Telex) and joins phrases (`doanh thu`, `bao nhieu`, `ti le`) into canonical words. A kind claims a question only if each word is one of the kind's cue words, common filler, a period, a number, or a name from the bundle's catalogue. One unexplained word (`trừ voucher`, `khách vip`, a name the bundle does not hold) sends the question to the model.

**Why.** It is the cheapest way to keep the claimed set precise: a qualifier that changes the meaning is, by construction, a word that no kind explains. A miss costs the model's attempt, not −1.

**Evidence.** 0 wrong of 2,299 claims over 30 businesses. `tests/answering/test_intents.py` checks that an unexplained word, two periods where one is expected, and an empty question are not claimed.

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

### 2.8 Three preparation fixes

Each is generic, none keys on a business id. All bundles were prepared again (generation `r4`) and compared with `r3` (section 3.4).

1. **"Thu hộ (COD)" is cash on delivery** (`views.is_cod`). The debt views excluded only the labels `cod`, `thu ho` and `giao hang thu tien`, so a bundle whose label is `Thu hộ (COD)` counted COD orders as customer debt. The replay found 10 wrong debt answers in biz_124 (total debt 2,749,773 against 948,973). After the fix all debt claims match.
2. **A README pattern for monthly files scopes every month's file** (`semantics._source_tokens`). dev_002's README defines its columns under `BanHang_T<tháng>_<năm>.xlsx`, but the files are `BanHang_T1_2025.xlsx` and so on, so no definition applied and `Tổng tiền` fell back to the label alias `order_total`. The README says it is `thành tiền sau giảm`, a line amount. With the fix dev_002 and dev_005 publish `line_amount`, hence revenue, for 128 public questions.
3. **A column whose README definition is a status stays text** (`parsers.infer_parser`). dev_007's `Tiến độ` ("trạng thái đơn") was typed as money because the label contains `tiền`, so the status column was withheld and the sales lines could not be made rulebook-ready.

**Evidence.** Unit tests: `PaymentLabels`, `test_a_readme_pattern_for_monthly_files_scopes_every_month`. `scripts/bundle-invariants.py` passes on all 44 bundles of `r4` (section 3.4).

### 2.9 An intent attempt closes the bundle before the model may open it

The first model run of this round (work100, `f70a679`) scored 83 / 0 / 17, with a median of 0.0 s per question. The 17 questions that reached the model all abstained with `InvalidInputException: Cannot change configuration option "lock_configuration"`: DuckDB shares one locked instance per file within a process, so the model path could not open a bundle that the intent layer still held. All 227 synthetic tests passed because the runner test mocks the model path. The fix opens the bundle for the attempt and closes it before returning (only the entity catalogue is cached), and `WithTheModelPath` fails without it. Lesson 13 in the index.

### 2.10 How the work was checked without a model

- `scripts/oracle-replay.py` runs the kinds over every training question except gate100 and holdout48 and compares with the references by the internal scorer.
- `scripts/bundle-invariants.py` compares line revenue with order revenue month by month, the order states with `counts_as_sale`, and looks for missing dates.
- `scripts/claim-coverage.py` counts the claims of any split per business. It reads no answer and prints no question, and it was the only way public questions were used: **no phrase, word or kind was added from the text of a public question**.

## 3. Results

The internal scorer is not the official one; all questions are training questions unless noted.

### 3.1 Oracle replay on the open training questions

Bundles `artifacts/prepared-<<BUNDLES>>-train`, code `<<CODE>>`. 2,852 questions (3,000 minus gate100 and holdout48).

| Measure | Value |
|---|---|
| Claimed by a kind | <<CLAIMED>> (<<CLAIMED_PCT>>%) |
| Right / wrong | <<RIGHT>> / <<WRONG>> |
| Left to the model | <<UNCLAIMED>> |

| Kind | Claimed | Right |
|---|---|---|
<<KIND_TABLE>>

Claims by reference type: <<TYPE_TABLE>>.

**Unclaimed, by crude kind** (not exclusive of vocabulary gaps):

| Kind of question | Questions | Why it is not claimed |
|---|---|---|
| Selling price on a date, average import price | 108 | Needs the price history and unit conversion; both rules were removed from the public rulebook |
| Pharmacy attributes (prescription, active ingredient, lots, expiry) | 98 | The drug list has no semantic roles; each pharmacy names its columns differently |
| Stock value, out-of-stock items, stock by lot | 63 | No published relation |
| Debt by age, in bundles that publish no debt views | 54 | No published relation |
| Purchases (receipts, suppliers) | 49 | Receipt layouts differ per business |
| Customer group (`khách nhóm sỉ`) | 40 | The group column has no role |
| Price change with no date in the question | 32 | Needs the price history to find the date |
| Staff, deliveries, profit and cost | 48 | No published relation |
| Other | 61 | Vocabulary gaps and `unanswerable` kinds the data cannot show |

### 3.2 Claims on the public split

`scripts/claim-coverage.py --split data/public_test`, bundles `<<BUNDLES>>-public`:

<<PUBLIC_TABLE>>

**This is the main finding of the round.** On training the kinds claim 80.6% of the questions; on the public split they claim <<PUBLIC_PCT>>%. Three reasons are visible without reading a public question:

- <<PUBLIC_NO_SALES>> of the 1,000 public questions are in businesses with no rulebook-ready sales relation (dev_003, 011, 012, 013, 014, and the lines of 009). Their layouts are new industries (phone shop, repair shop, mother-and-baby shop) and notebook-style ledgers.
- The public rulebook defines kinds that training lacks, and the questions about them are phrased in ways the training vocabulary does not cover.
- Dish-and-topping names (dev_002) switch the product-level kinds off (decision 2.7).

The public questions were not read to widen the vocabulary (section 2.10). Widening it from them would make the layer fit the public test rather than the task.

### 3.3 Model runs

<<MODEL_RUNS>>

### 3.4 Bundles and tests

<<BUNDLE_CHECKS>>

## 4. Closed samples and evidence

- **gate100** <<GATE_READ>>
- **holdout48**: not run this round. Its totals have been read 7 times (index).
- **Evidence ids.** The reference answers of every open training question were used by the replay. Nothing about gate100 or holdout48 was loaded: `oracle-replay.py` removes their ids before reading answers, and the vocabulary was built from open questions only.

## 5. Where this leaves the target

"No wrong answer on the public test" is not reached, and nothing here shows it can be. What the round adds:

1. A layer that answers the questions it recognises with no model variance, whose precision can be re-measured at any time without a model (`oracle-replay.py`).
2. A way to count, for any split and without reading answers, how much of it the layer covers (`claim-coverage.py`) and whether the bundles agree with themselves (`bundle-invariants.py`).
3. A measured gap: training phrasing is covered at 80.6%, public at <<PUBLIC_PCT>>%. The 100% target depends on that gap, on the six public businesses with no sales relation, and on the model path's accuracy for everything left.

Recommended next steps, in order of expected effect on the public score (all unverified until a public upload or a dry run):

1. **Publish sales relations for the public businesses that still have none** (backlog B2): dev_003 needs its two yearly files read as one relation, header aliases (`Đơn số`, `Ngày HĐ`, `Hàng hóa`, `ĐV`) and a decision on the status label `Khách trả`; dev_011 and dev_014 (phone shop) need the ledger layout read with their rulebook rules; dev_012 (repair shop) and dev_013 (mother and baby) need their own kinds. Check each with `bundle-invariants.py` before use.
2. **Split dish and topping in preparation** (decision 2.7), so that dev_002's product-level kinds, and the model's, can work.
3. **Implement the remaining public-rulebook kinds from their definitions**, each with a second independent computation and a hand-derived test: average per weekday, main supplier, hardest-working staff.
4. **Run the full public dry run** (backlog B6) and read it with `dry-run-report.py`, to see abstentions and the time budget with the layer in place.
5. Only then spend public uploads; with every question answered, wrong answers = (1 − PA) × N / 2 exactly, which tells how far the system is from zero.

## 6. Backlog changes and not verified

- **B2** (public layouts with no sales relation): dev_002, dev_005 and dev_007 now publish one (fixes 2.8.2 and 2.8.3). Still without: dev_003, 011, 012, 013, 014, and dev_009 (headers only).
- **B3** (dev_007 `Tiến độ` parsed as money): done, fix 2.8.3.
- **New:** B19 split dish and topping in preparation (2.7); B20 selling price on a date and the price history as a semantic relation; B21 pharmacy attributes (prescription, active ingredient, lots) from the drug list; B22 customer group role; B23 receipts and suppliers as a semantic relation; B24 the remaining public-rulebook kinds (2.6, section 5.3); B25 dev_003's two yearly files as one relation.
- **Not verified:** the public score of anything here; precision on questions phrased unlike the training ones; the rankings of decision 2.6 against references; the `unanswerable` rule of decision 2.5 on bundles whose columns preparation did not detect; that the internal scorer agrees with the official one (backlog, workflow review 3.9); the full public dry run and the 6-hour budget with this layer; any claim of offline operation beyond what the entrypoint record shows.
