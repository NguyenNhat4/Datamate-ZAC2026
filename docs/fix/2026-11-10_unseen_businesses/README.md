# Decision record and plan: businesses the system was not tuned on

Date: 10 November 2026
Status: Step 0 done; Steps 1 and 2 in progress. Live status is in [PROGRESS.md](PROGRESS.md).
Branch: `fix/sql-answering-accuracy`, starting from commit `205dee8`.
Scope: the SQL answering path (`tabular_agent/sql_agent.py`), the preparation layer (`tabular_agent/preparation/`), and how runs are measured.

This record follows [the date hints and answer checks record](../2026-11-09_date_hints_and_answer_checks/README.md). Business and question ids appear only as evidence. No rule branches on an id, and no reference answer is stored in code.

## 1. Situation

Every earlier measurement used the same 8 training businesses, and their failures were read to choose fixes. Those scores are therefore optimistic. To get an honest estimate, the other 22 training businesses and the 14 public-test businesses were prepared for the first time, and one frozen sample of 48 questions (`unseen48`, 2–3 per unseen training business) was run once at commit `205dee8`.

Local 7B model (Qwen2.5-Coder-7B-Instruct, Q4_K_M) on a laptop GPU, internal scorer, training questions. PA is penalized accuracy: (correct − wrong) / questions.

| Questions | Count | Correct / wrong / abstain | PA |
|---|---|---|---|
| unseen48, all | 48 | 24 / 16 / 8 | +0.167 |
| In bundles that publish `semantic.eligible_sales_lines` | 34 | 23 / 5 / 6 | +0.53 |
| In bundles that do not | 14 | 1 / 11 / 2 | −0.71 |

For comparison, the development sets scored +0.475 to +1.00 at the same commit.

**Where the gap comes from.** Where preparation publishes rulebook-ready sales relations, the unseen businesses score close to the development sets. Where it does not, the model reads the raw cleaned tables, applies no order eligibility, and gives a confident wrong number.

**How many questions are exposed.**

| Split | Businesses without `semantic.eligible_sales_lines` | Questions in them |
|---|---|---|
| Unseen training | 7 of 22 (biz_105, 110, 115, 116, 117, 118, 128) | 749 of 3,000 training questions |
| Public test | 8 of 14 (dev_003, 004, 007, 009, 011, 012, 013, 014) | 516 of 1,000 public questions (52%) |

Preparation never read questions or answers. Public-test questions were only counted per business.

## 2. How the causes were established

Four agents diagnosed the preparation blockers and the 24 failed answers from the profiles, raw files, code and run logs. A second, independent agent per group then tried to reproduce each diagnosis. Every diagnosis was upheld except one mechanism (the drug-list header in biz_124), which is left unresolved.

For the preparation causes, the agents re-ran the semantic step on scratch copies of bundles with the proposed rule changed, and compared the results with training references. This is evidence that the fixes compute the right figures. It is not a model run.

## 3. Causes

### 3.1 Preparation rules that are too strict

| Cause | What happens | Businesses |
|---|---|---|
| A. A sheet with only an order ID is taken as the order table | A debt-terms sheet lists only credit orders. Preparation treats it as the shop's order table, raises orphan-key issues for every other order, then stops without naming a reason because the sheet has no status | biz_115, 116, 117, 118; dev_004 |
| A′. Same rule, second form | A dine-in table sheet counts as a second order table, so the order system looks ambiguous | biz_128 |
| B. A delivery book is taken as a sales-line table | It has an order ID, a product and a quantity, so it counts as a second candidate for sales lines | biz_115, 116, 117, 118; dev_004 |
| C. The status "Không giao được" is not recognised | Orders with it are "unknown", which blocks the sales relations. In the data it behaves as a cancellation label | biz_116, 118; dev_004 |
| D. Return lines carry the return date | The check "one date per order" fails for orders with a return line | biz_105, 117; dev_004 |
| Header-only invoices | The shop has no line table, so no revenue is published | biz_110; dev_009 |
| Smaller parsing causes | Zero-padded codes; a day count parsed as a date; ambiguous thousands separators | several |

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

**Why first.** unseen48 has now been diagnosed, so it is a development set. Without a sample we never look inside, there is no honest number left. Without fixed sampling, a change of one or two questions between runs cannot be told from noise.

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
4. Revenue for businesses with invoice headers only.
5. Remaining public-test file layouts, largest first: dev_003 (129 questions), dev_014 (63), dev_007 (60), dev_013 (57), dev_012 (50), dev_011 (40).

### Not planned in this round

Price-change effect, purchase prices, product attributes (active ingredient, prescription), lot-based stock, refusing a repeated failing query, and feedback for dates after the reference date. Each affects one or two questions in the samples.

## 5. Decisions

Entries are added as each step is done.

### 5.1 Send temperature 0 (Step 0; commit `82ebdb6`)

**Decision.** `bench` and `predict` take `--temperature`, default 0. It is sent with every request to the local server and recorded in each run's `manifest.json`.

**Why, in this situation.** No temperature was sent before, so the server used its own default sampling. Between two runs of the previous round, one answer changed from correct to wrong with no cause in the code. With gains of a few questions per step, that noise is as large as the effects being measured.

**Rejected.** Running each set several times and averaging: it multiplies run time by the number of repeats and still leaves noise.

**Risk.** A deterministic model can repeat the same failing reply until the step limit. The accuracy at temperature 0 may also differ from the accuracy at the old setting, in either direction. The new baselines measure that before any other change.

**Result.** On the development sets accuracy did not drop: setB went from +0.475 to +0.600 and types16 from +0.875 to +1.00; sample10, setA and unseen48 kept the same totals (section 6). Median time per question is unchanged (about 6–7.5 s).

### 5.2 Keep one sample closed (Step 0)

**Decision.** `holdout48` is run after each step, and only its totals are read. Frozen ids for every set are in [question_sets.json](question_sets.json).

**Why, in this situation.** Each sample that was diagnosed stopped being an estimate: first setA and setB, then unseen48.

**Risk.** With 48 questions, a difference of about 3 questions is within what chance can produce. The holdout shows direction and rough size, not a precise score.

### 5.3 Start each set on a fresh server, from a pinned copy of the code (Step 0)

**What was found.** The server obeys temperature 0, but one dependence remained. With its prompt cache on, a reply can depend on the request processed just before it. In a check with an invented question, the reply was one text after the same question and another after a different one; the two diverged after about 540 characters. With the cache off, replies never varied, but each sample10 question took 2.5 times as long (median 17 s instead of 6–7 s), which pushes multi-step questions toward the 60-second limit.

**Decision.** Keep the cache on, as the system will run, and start every question set on a freshly started server, so each set begins from the same server state. Each set also runs from a separate git worktree checked out at the commit being measured. A set imports the code when it starts, so an edit made in the main folder during a long run would otherwise leak into it.

**Evidence.** Two full runs of sample10 gave identical replies at all 21 model steps, with the cache on and with it off. On fresh servers, setB and unseen48 were each run twice: identical replies at all 92 and all 121 model steps, and identical outcomes.

**Rejected.** Cache off: 2.5 times slower and not how the system will run. Several runs per set and averaging: costs time and still leaves noise.

**Risk.** Within a set a reply can still depend on the question before it. The order is fixed, so runs repeat, but a change to one question's conversation can shift the next question's reply. Time limits (60 s per question, 15 s per query) can still change an outcome when the machine is loaded.

## 6. Results

Measurements are logged in [PROGRESS.md](PROGRESS.md) as they are made. Local 7B model on the team laptop (RTX 4050 Laptop GPU), internal scorer, training questions.

### 6.1 Baselines at temperature 0 (commit `82ebdb6`)

| Set | Correct / wrong / abstain | PA | At temperature 0.8 (`205dee8`) | Median / p90 seconds per question |
|---|---|---|---|---|
| sample10 | 10 / 0 / 0 | +1.00 | +1.00 | 6.2 / 9.8 |
| setA | 33 / 4 / 3 | +0.725 | +0.725 | 6.2 / 9.4 |
| setB | 28 / 4 / 8 | +0.600 | +0.475 | 7.2 / 9.7 |
| types16 | 16 / 0 / 0 | +1.00 | +0.875 | 7.5 / 11.4 |
| unseen48 | 24 / 16 / 8 | +0.167 | +0.167 | 7.5 / 14.9 |
| holdout48 (total only) | 25 / 16 / 7 | +0.188 | not run | 6.9 / 13.4 |

sample10 and setA ran from the main folder, before the pinned worktree was in use. For sample10 the folder held only the untracked documents. For setA it also held the uncommitted Step 1 change, which has no effect on these 8 businesses: none of their bundles has a refused table, so the prompt and the checks are unchanged. Every other run reports a clean tree at `82ebdb6`.

Under the same totals, 9 unseen48 questions changed outcome between 0.8 and 0 (4 better, 5 worse), which shows how large the earlier sampling noise was.

## 7. Not verified

- The plan's own review was stopped before it finished. Its three questions are handled as checks inside the steps: what the abstain rule would wrongly block, whether the preparation fixes change the development bundles, and how much the holdout can detect.
- The preparation fixes have only been tried on scratch copies by the diagnosis agents, not implemented.
- No public-test answers exist, so public-test progress can only be measured as preparation coverage.
- No measurement on the reference hardware or inside an offline container. The internal scorer may differ from the official one.
