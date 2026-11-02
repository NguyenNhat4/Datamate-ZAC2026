# Solution Alternatives and Trade-offs

Status: proposal for review, not a confirmed architecture or model selection. Inspected on 30 October 2026. No inference benchmark has been run.

Implementation update: the user subsequently selected alternative 2, a bounded single-agent inspect/execute/observe loop. The local implementation, CLI and measurement commands are documented in `README.md`. The recommendation for alternative 3 below is retained as proposal history, not the implemented architecture. An experimental local 3B baseline has now been provisioned and measured; this is not a final model selection. See `README.md` for runtime details and measured limitations.

## Evidence and constraints

Sources: `project-overview.md`, both local split rulebooks, and representative training business READMEs covering a flat CSV, monthly Excel sheets, and joined CSV tables. A read-only Python count found 3,000 training questions across 30 businesses, with 1,884 number answers, 578 percentages, 193 lists, 145 dates, 88 strings, 70 unanswerable answers, 23 booleans, and 19 tables. Number and percentage answers account for 82.1%. Business files include 107 XLSX files, 20 CSV files, and one SQLite file. These are file counts, not row counts or workload measurements. No public-test questions or reference answers were used for this analysis.

The supplied reference profile specifies offline inference, at most 15B total inference parameters including auxiliary models, eligible publicly released weights before 1 October 2026, and a six-hour full run on two 32 GB GPUs. These are transcribed requirements, not independently verified current organizer guidance. General regulations and the separate Docker guideline remain unresolved dependencies.

Important local findings:

- The public rulebook differs from training. It adds customer loyalty by distinct purchase days, primary suppliers by receipt count, and industry-specific rules. Load the rulebook for the mounted split; never silently substitute training defaults.
- Revenue, debt, and inventory use different eligibility rules. Returned orders are excluded from revenue but may still contribute debt.
- Gross profit uses the cost recorded on each sales line. The general weighted-average costing description does not authorize reconstructing that cost from purchases.
- Forward filling must respect order/group boundaries. Duplicate removal depends on whether records belong to the same order or are independent ledger entries.
- An empty result does not prove that the answer is zero or unanswerable. Coverage and available fields matter.
- PA rewards correctness and penalizes wrong answers equally. Output type, rounding, list order, and table structure are part of correctness.

## Alternative 1: Schema-grounded text-to-SQL

Flow: deterministic file ingestion → schema and value profile → LLM generates SQL using the question, README, current rules and calendar → read-only execution → typed serialization.

Convert Excel sheets and CSVs into queryable tables while retaining original names, row provenance, order boundaries, and unit information. Expose only relevant schema and small value samples to the model; execute aggregations over all applicable rows. DuckDB is a candidate analytical engine, not a selected dependency. Required readers and extensions must be packaged before offline inference.

Strengths: compact execution path; SQL is inspectable; filters, joins, grouping and rankings are naturally expressed; limited repair calls make compute easier to bound. This is the preferred baseline.

Trade-offs: clean ingestion is substantial work. SQL that executes successfully can still join at the wrong grain, double count orders, or apply the wrong revenue definition. Irregular cells and stateful inventory reconstruction may be awkward. A schema mapping mistake can affect many questions.

Choose when: time is short, schemas can be reliably exposed, and the majority of supported questions reduce to relational operations. One syntax/execution repair is a proposed initial limit, to be tuned on validation.

## Alternative 2: Python execution agent

Flow: LLM inspects metadata and bounded samples → writes Python transformations and analysis → executes in a restricted worker → inspects results/errors → repairs within a fixed budget → typed serialization.

Provide narrow tools for reading sheets, querying SQLite, profiling columns and running code. Preserve read-only source mounts; use a separate scratch directory, no network, process timeouts, memory limits and bounded output. A simple `exec` wrapper is not adequate isolation.

Strengths: flexible for mixed formats, unusual sheet layouts, embedded item descriptions and sequential calculations. Can explore unfamiliar schemas instead of requiring every case to fit a predefined query vocabulary. Program-aided execution has a research precedent, but published results do not establish quality on this dataset.

Trade-offs: repeated inspection and repair consume tokens and time; generated code can silently misinterpret business rules; trajectories are harder to reproduce and debug. Flexibility increases the number of failure modes. An agent may repair a crash without repairing the calculation's meaning.

Choose when: irregular transformations dominate baseline failures and validation shows enough added correct answers to compensate for new wrong answers and runtime. Start with at most three execution attempts per question, including the first; this is a budget proposal, not a measured optimum.

## Alternative 3: Structured planner with deterministic execution and bounded fallback

Flow: shared ingestion and schema mapping → LLM produces a typed analysis plan with rule references → deterministic validation and SQL compilation for supported operations → bounded Python fallback for unsupported transformations → verification and answer/abstain decision.

The plan should contain the requested metric, source-field bindings, entity, time interval resolved against `as_of_date`, inclusion/exclusion rules, grouping, unit conversion, sorting, rounding and output type. Each rule binding should point to the current rulebook or explicit question instruction. Business IDs select data only; mappings derive from schemas and documentation.

Reusable primitives can cover date intervals, filters, sums, distinct counts, ratios, rankings and joins. The current rulebook supplies their meaning and parameters. Do not encode training semantics as immutable defaults. Unsupported or ambiguous rules must remain visible and route to controlled analysis or abstention.

Keep original records and provenance beside normalized views. Validate join cardinality, distinguish order and line grains, verify entity bindings and required columns, and check calendar coverage. Missing schema mappings are system uncertainty, not evidence that the source lacks data. Use decimal/fixed-point arithmetic and explicit rounding rules for exact outputs.

Strengths: makes common calculation errors detectable and reusable fixes possible; supports traceable answers; avoids spending multiple model calls on every question. A bounded fallback retains some flexibility for new schemas and industries.

Trade-offs: highest initial engineering cost; planner, mapping and compiler contracts must agree. A restrictive plan language can reduce coverage. Wrong shared mappings can create correlated failures. A second model pass agreeing with the first is weak evidence if both share the same faulty interpretation.

Choose when: the team can invest in the shared data/rule layer and validate components incrementally. This is the recommended target, conditional on measured PA improvements over alternative 1.

## Relative comparison

These are architectural expectations, not measured scores or latency claims.

| Dimension | 1: SQL | 2: Python agent | 3: Structured hybrid |
| --- | --- | --- | --- |
| Initial implementation | Lowest after shared ingestion | Moderate for a prototype; higher for reliable execution | Highest |
| Irregular transformations | Limited without preprocessing | Most flexible | Flexible through bounded fallback |
| Semantic checks | Must be added around SQL | Difficult across arbitrary programs | Explicit plan and reusable checks |
| Inference cost | Usually fewer calls | Variable, often more calls | Low on supported path; higher on fallback |
| New schemas and rules | Depends on schema grounding | Can inspect adaptively | Depends on mapping and plan expressiveness |
| Main failure | Executable but wrong query | Plausible but wrong program or retry loop | Incorrect shared abstraction or routing |
| Intended role | Baseline and fallback release option | Targeted experimental alternative | Recommended incremental target |

## Selective answering

For a calibrated probability `p` of exact correctness, answering has expected score `2p - 1`, versus zero for abstention. The mathematical threshold is `p > 0.5`; a model's self-reported confidence is not a calibrated probability. Select a practical gate using held-out training businesses, with separate reporting by answer type and question family.

Useful gate features include resolved schema mappings, rule coverage, execution success, join checks, time/unit checks and disagreement between independently constructed calculations. Agreement alone does not prove correctness. Return `unanswerable` only with source evidence of insufficient data; use `abstain` for unresolved interpretation, execution failures or unsupported operations.

If coverage is `c` and accuracy among answered questions is `a`, then `PA = c(2a - 1)`. For illustration only, 90% coverage at 75% accuracy gives 0.45 PA, while 75% coverage at 90% accuracy gives 0.60 PA. These are hypothetical figures, not forecasts.

## Evaluation and implementation sequence

1. Split training by business, keeping all questions for one business in the same fold. Also evaluate withheld industries and schema families where feasible. Keep training references out of inference context and held-out examples out of retrieval/fine-tuning.
2. Build an internal matcher for the documented normalization and typed formats. Track any ambiguous official matching behavior; do not claim official evaluator equivalence.
3. Build shared ingestion and alternative 1. Evaluate PA, coverage, answered accuracy, unanswerable confusion, invalid output rate, total runtime, per-question latency and peak memory. Categorize failures as ingestion, schema, rules, time, execution or serialization.
4. Add alternative 3 checks and supported plan operations only where the baseline exposes recurring errors. Test boundary dates, partial returns, grouped blanks, duplicate policy, units and rounding with synthetic fixtures in addition to held-out real training questions.
5. Compare alternative 2 on the same held-out folds. Add its fallback only if the final routed system improves PA under the total runtime budget. Compare equal compute budgets and include preprocessing/model loading.
6. Ablate rules, validation, fallback and abstention gates to identify which components help. Preserve a final holdout to reduce selection overfitting. Report per-business results as well as overall PA.
7. Freeze dependencies, model revision, quantization, seed and generation settings; verify a full offline run and repeatability on available hardware. Report separately whether the reference machine has been tested. Flush each completed answer so a timeout preserves completed work.

Start with one eligible code-capable language model reused for planning, repair and checking; compare a smaller dense model class around 7–8B against a larger one around 14B if hardware permits. Model class is a search range, not a model selection. Exact release date, licence, total parameters, revision, weight source and quantization must be verified before adoption. Avoid adding auxiliary models until a measured benefit justifies their parameter budget. Fine-tuning is a later experiment using training-only, execution-checked examples; the supplied final answers alone are not verified reasoning traces.

## External technical references

- [DuckDB data import documentation](https://www.duckdb.org/docs/current/data/overview): candidate execution and ingestion capabilities; no dataset performance claim.
- [PAL: Program-aided Language Models](https://arxiv.org/abs/2211.10435): precedent for delegating computation to executable programs; no claimed transfer of published scores.

## Verification boundary

Completed: read-only dataset metadata/count inspection, rulebook comparison and representative README inspection. No model calls, training, solution implementation, benchmark answers, commits or pushes were performed for this proposal. Current organizer runtime/packaging guidance and team hardware remain unverified.
