# Project Overview

> Source: the user-provided eight-page PDF, converted on 29 October 2026. This document consolidates its problem statement, data contract, evaluation, technical requirements, and submission guidance. Website navigation and account details have been omitted. Requirements and dates below describe the supplied document; they have not been independently checked for subsequent updates. The separate general regulations and Docker guideline are referenced by the PDF but are not included in it.

## 1. Problem statement

Build an AI agent that answers natural-language Vietnamese questions directly from structured data in CSV, Excel, and SQLite files. Given raw business files and a question, the agent must return a correct answer, recognize when the data cannot support an answer, or choose to abstain.

Spreadsheets and simple databases are widely used by shop owners, teachers, clinics, HR teams, cooperatives, public offices, journalists, and company employees. These users have data but often cannot write queries. They want to ask everyday questions and receive correct answers, for example:

- “Tháng này bán chạy nhất mặt hàng gì?”
- “Tuần rồi bán được bao nhiêu ly trà sữa?”
- “Còn ai nợ tiền chưa trả?”
- “Thuốc nào sắp hết hạn trong kho?”

The supplied task focuses on Vietnamese small businesses and their varied, messy data. The underlying capability is general natural-language question answering over tabular data.

## 2. Inputs and outputs

### Inputs

| Input | Description |
| --- | --- |
| Business data package | One or more `.csv`, `.xlsx`, or `.sqlite` files, plus a business `README.md` |
| Question | Everyday Vietnamese, potentially abbreviated or without diacritics |
| `as_of_date` | Reference “today” for expressions such as “tháng này”, “tuần rồi”, and “cùng kỳ năm ngoái” |
| `rulebook.md` | Default business rules and vocabulary shared by the businesses in the current split |
| `calendar.csv` | Solar–lunar calendar shared by the businesses in the current split |

### Outputs

Return a final answer with one of these types:

`number`, `percent`, `string`, `date`, `boolean`, `list`, `table`, `unanswerable`, `abstain`.

- `unanswerable` means the data does not contain the information needed to answer.
- `abstain` means the agent chooses not to answer.

Example input, one line of `questions.jsonl`:

```json
{"id":"q_06de33fa21cc","business_id":"biz_118","question":"sp bán dc nhiều nhất tuần rồi là gì","as_of_date":"2026-03-04"}
```

Example output, one line of `submission.jsonl`:

```json
{"id":"q_06de33fa21cc","type":"string","answer":"Xi măng Hà Tiên PCB40 bao 50kg"}
```

## 3. Data

### Business coverage and scale

Business types include grocery stores, coffee and bubble-tea shops, restaurants, pharmacies, building-material stores, motorbike repair shops, and mother-and-baby stores. Private test includes business types absent from training and public test.

Each business covers 12–30 months of activity and ranges from a few hundred to approximately 300,000 rows. Seasonal effects include Tết, Mid-Autumn, public holidays, paydays, and weekends.

### Package structure

```text
<split>/
├── data/<business_id>/
│   ├── *.csv | *.xlsx | *.sqlite   # One or more data files
│   └── README.md                  # Business description and notes
├── rulebook.md                    # Default rules and vocabulary
├── calendar.csv                   # Solar–lunar calendar
├── questions.jsonl                # One question per line
└── answers.jsonl                  # Training only
```

A business `README.md` may contain a full data dictionary, a few informal notes, or almost no explanation.

Example question:

```json
{"id":"q_01234","business_id":"biz_017","question":"thang nay hang nao ban cham nhat, ton nhieu ma ban ko dc","as_of_date":"2026-08-27"}
```

Training `answers.jsonl` contains reference answers joined to questions by `id`. Each line follows the submission format and adds `alias`, listing accepted alternative name spellings present in the data and the item code. Unanswerable training references have `"answer": null`. Submissions do not need `alias`.

```json
{"id":"q_06de33fa21cc","type":"string","answer":"Xi măng Hà Tiên PCB40 bao 50kg","alias":["SP0001"]}
```

### Splits

| Split | Businesses | Approximate questions | Reference answers |
| --- | --- | --- | --- |
| Training | 30 | 3,000 | Available |
| Public test | 10–20 | 1,000 | Hidden; scored on the public leaderboard |
| Private test | 10–20 | 500 | Hidden; data released during the private phase; includes new business types |

### Business rules

Read and apply the current split's `rulebook.md`. Rules may differ between splits; a copy of the training rulebook must not replace the rulebook for the current split. Explicit question instructions override defaults.

The PDF gives these typical interpretations; the actual split rulebook determines the applicable defaults:

| Concept | Typical interpretation |
| --- | --- |
| Revenue | Sum of line amounts after line-level discounts; do not deduct order-level vouchers; exclude VAT, delivery fees, cancelled orders, and orders with any returned item |
| “Bán chạy” | By quantity |
| Week | Monday through Sunday |
| “Tháng này” | The month containing `as_of_date`, up to and including that date |
| Default cost | Weighted-average purchase cost by lot |
| Tết window | Lunar month 12, day 23 through lunar month 1, day 10 |

### Data download links supplied in the PDF

- [Training data](https://dl-challenge.zalo.ai/2026/datamate_dkewxftdzo/train.zip)
- [Public test data](https://dl-challenge.zalo.ai/2026/datamate_dkewxftdzo/public_test.zip)

These links are transcribed from the PDF; the archives have not been downloaded or inspected as part of this conversion.

## 4. Evaluation

### Local dataset inspection note — 30 October 2026

The local training and public-test packages are now present and their rulebooks have been inspected. The earlier download note describes only the original PDF conversion. A read-only metadata count confirmed 3,000 training questions across 30 businesses; this is not an inference benchmark.

The split rulebooks provide more specific rules than the PDF summary above. In particular, gross profit uses cost recorded on each sales line; do not reconstruct it from purchase lots based only on the general weighted-average-cost description. Money is rounded to whole currency units under the split rulebooks, while percentages and other averages generally use two decimal places unless otherwise specified.

The public-test rulebook also adds definitions absent from training, including customer loyalty by distinct purchase days and primary suppliers by receipt count, plus industry-specific rules. Always load the current split's rules. These observations do not independently verify the latest general regulations or Docker guideline.

### Per-question scoring

Each question is scored correct, wrong, or abstain. An answer is correct only when its `type` equals the reference type and its value satisfies the matching rule. There is no partial credit.

An answer of type `abstain`, or a missing answer, is scored as abstain. Predicting `unanswerable` is correct only when the reference is also `unanswerable`. Returning a substantive answer for an unanswerable question is wrong.

| Type | Answer content | Matching rule |
| --- | --- | --- |
| `number` | Numeric value, such as VND amounts, counts, or quantities | Exact match; reference values are integers or rounded to two decimal places |
| `percent` | Percentage units, e.g. `-12.45` | Exact match after rounding to two decimal places; the sign must be correct |
| `string` | Name, such as a product, customer, branch, or weekday | Normalize the prediction and each accepted reference form, then compare for exact equality |
| `date` | ISO date or month | Exact match: `yyyy-mm-dd` for dates or `yyyy-mm` for months |
| `boolean` | JSON `true` or `false` | Exact match |
| `list` | Ordered names or numbers | Compare position by position in the order requested by the question, or the rulebook default; use string or number matching for elements |
| `table` | Object with `columns` and `rows` | Match columns by normalized name, falling back to position if names do not match; compare rows as a set; use string matching for name cells and number matching for numeric cells |
| `unanswerable` | Data lacks the information needed | Correct if and only if the reference is `unanswerable` |
| `abstain` | Agent chooses not to answer | Scored as abstain regardless of the reference |

### String normalization

Apply in order:

1. Unicode NFC normalization.
2. Lowercase.
3. Replace every character that is not a letter, digit, or whitespace with a space.
4. Remove leading and trailing whitespace and collapse whitespace runs to one space.

Do not remove Vietnamese diacritics. Accepted reference forms include every spelling of the name occurring in that business's data and its code. Examples after normalization: `sting dâu 330ml`, `sp0042`.

### Ranking questions

- Use the criterion stated in the question, or the rulebook default when omitted.
- Rank in descending order of that criterion over valid orders only.
- Break equal values by ascending code.
- Questions with a reference tie at the `k` boundary, or at rank one for superlative questions, are excluded from test sets.
- If fewer than `k` candidates exist, the reference includes all candidates.

### Penalized Accuracy

The leaderboard metric is Penalized Accuracy (PA):

```text
s_i = +1  if answer i is correct
s_i =  0  if answer i is abstain or missing
s_i = -1  if answer i is wrong

PA = (1 / N) × sum(s_i)
   = (correct answers - wrong answers) / total questions
```

`N` includes all questions, including abstentions. All questions carry equal weight.

Ties in the final ranking are broken by lower total inference time in the organizers' re-run, then by earlier submission.

## 5. Submission and runtime contract

### Result file

Both rounds use `submission.jsonl`, containing one JSON object per line:

```jsonl
{"id":"q_01234","type":"string","answer":"Sting dâu 330ml"}
{"id":"q_01235","type":"number","answer":48250000}
{"id":"q_01236","type":"percent","answer":-12.45}
{"id":"q_01237","type":"date","answer":"2026-08-15"}
{"id":"q_01238","type":"boolean","answer":true}
{"id":"q_01239","type":"list","answer":["Cà phê sữa","Bạc xỉu","Trà đào"]}
{"id":"q_01240","type":"table","answer":{"columns":["khach_hang","so_tien_no"],"rows":[["Chị Lan",3200000],["Anh Tú",1500000]]}}
{"id":"q_01241","type":"unanswerable","reason":"Dữ liệu không có giá vốn nên không tính được lợi nhuận"}
{"id":"q_01242","type":"abstain"}
```

The `reason` field is optional. Missing questions count as abstain.

### Docker package

The image must contain training and inference source code, model weights, a README, and `predict.sh`. The complete-solution submission guidance also lists data among package contents. Follow the separate Docker guideline for packaging details; it was not included in the supplied PDF.

| Path | Purpose |
| --- | --- |
| `/data/` | Evaluator-mounted test data, read-only |
| `/result/submission.jsonl` | Prediction output |
| `/result/logs/` | Per-question logs |

Logs may use any format and must contain at least the question id, final answer, and elapsed time. Generated code, execution output, and other supporting evidence may also be logged. The organizers use logs to investigate uploaded-result and re-run differences.

### Evaluation rounds

1. **Public test:** develop the system, run it on public test, and upload `submission.jsonl`. Public leaderboard scores are a development reference.
2. **Final solution submission:** submit and freeze the Docker image before the solution deadline. The organizers mount test data at `/data` and collect `/result/submission.jsonl`.
3. **Private test:** run the frozen solution on the released private data and upload results. The organizers' re-run of the submitted image determines the final ranking.

## 6. Technical requirements

These requirements are transcribed from the supplied task specification.

| Requirement | Details |
| --- | --- |
| Eligibility | Teams and eligibility follow the organizer's general regulations, which are not included in the PDF |
| Models | Weights must have been publicly released before **1 October 2026** under a licence permitting use; fine-tuning is allowed |
| Parameter budget | Total inference parameters across language models, embedding models, rerankers, and classifiers must be **at most 15 billion**, whether loaded together or sequentially; count all MoE parameters |
| Quantization and declaration | Any quantization is allowed; declare all models in the solution README |
| Offline inference | No external services, commercial LLM APIs, downloads, or network access; the Docker image runs with networking disabled |
| Automated answers | No manual answer intervention, hard-coded answers, or answer logic keyed on question or business identifiers |
| Code execution | Agent-generated code execution inside the container is expected but optional; input data is read-only and temporary files must be written elsewhere |
| Permitted data | Provided training data, self-synthesized data, and publicly available datasets and models may be used |
| Test restrictions | Do not train on public/private test data or collect answers to test questions by any means |
| Reference hardware | **2 × NVIDIA RTX 5090**, 32 GB VRAM each, 64 GB total; **512 GB RAM** |
| Runtime | Process the entire private test within **6 hours** wall-clock time on the reference machine, including model loading |
| GPU compatibility | Blackwell requires **CUDA ≥ 12.8** and matching PyTorch/vLLM builds; use the base image specified by the separate Docker guideline |

### Reproducibility and incomplete runs

The organizers' private-test Docker re-run PA is the final score. Uploaded private-test JSONL establishes the provisional leaderboard and which teams are re-run.

A solution is considered reproducible when:

```text
abs(PA_uploaded - PA_rerun) ≤ 0.03
```

If the difference exceeds `0.03`, the organizers examine logs, may run the image once more, and may request additional information. An unexplained difference leads to disqualification.

If the re-run exceeds the time limit or terminates early, only answers already written to `/result/submission.jsonl` are scored; remaining questions count as abstain.

## 7. Schedule and submission guidance

The PDF describes the **2026** evaluation. Times below are **GMT+7**.

| Milestone | Date/time | Guidance |
| --- | --- | --- |
| Public test | 26 October–16 November | Upload JSONL through the submission portal; up to five submissions per day, with quota reset at the end of each day; scores and ranks appear on the public leaderboard |
| Complete solution deadline | **16 November, 23:59** | Submit the complete Docker package through the solution submission form; provide a checksum and a public Google Drive download link for the large package |
| Solution submission quota | By the solution deadline | Up to two submissions; only the last is evaluated |
| Private test release | **20 November, 09:00** | Run the submitted, frozen solution |
| Private results deadline | **23 November, 23:59** | Upload JSONL through the submission portal; private leaderboard is displayed in submission-time order |
| Top two announcement | 2 December | Organizers verify results, review source code, and check for cheating |

The PDF's data section defers daily quotas to general regulations; its submission guidance explicitly states five public submissions per day. The current general regulations and separate Docker guideline remain external dependencies for confirming submission details.
