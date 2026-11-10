# Plan: enforce the order-count eligibility policy

Date: 8 November 2026  
Status: superseded on 9 November and not implemented. SQL answering over prepared bundles replaced the raw-file agent this plan was for; order eligibility is now applied in the prepared `semantic` relations ([SQL answering record](../2026-11-09_sql_answering_accuracy/README.md)). Kept as a record only.  
Scope: prevent the single agent from accepting an order count that contradicts the current question and split rulebook.

## 1. Main issue

The model selected the wrong business meaning for an ordinary order-count question. It counted cancelled orders even though the current rulebook excludes them. The runtime accepted that choice because eligibility is a model-controlled boolean and final validation checks answer format rather than calculation policy.

This is an interface and validation gap as well as a model mistake. Correct instructions were available, the data was read, and the helper performed the requested calculation. None of those facts prevented an incorrect policy from producing an accepted answer.

The proposed solution keeps one CodeAgent and the isolated worker. It introduces a narrowly supported, application-owned order-count policy, enforces it when the tool executes, and checks a structured calculation record before accepting a simple order-count answer. The model continues to discover source files and column mappings, but cannot authorize a policy exception merely by setting a flag.

## 2. Evidence from the observed run

The inspected local run is `runs/ui-20261108-224452-708851/`. Its ignored artifacts remain local; do not copy the full trace or business records into this documentation directory.

| Item | Observed value |
| --- | --- |
| Question | `check gium hnay cua hang co bao nhiu don` |
| Question association | `q_0139627bccae`, business `biz_104` |
| Reference date | `2025-10-18` |
| Code revision recorded in manifest | `293d7121d8bdb8678b3081d117e12317044c7548`, clean checkout |
| Provider and model alias | Local, `local-coder-7b` |
| Returned answer | `number: 111` |
| Training reference | `number: 107` |
| Independent source check | 107 completed orders and 4 cancelled orders on the selected date |
| Elapsed time | Approximately 25.23 seconds |
| Model calls / retries | 2 / 0 |
| Internal outcome | 0 correct, 1 wrong, 0 abstain; PA = -1 for this single question |

The training rulebook's **Số đơn** entry says to count only orders that are neither cancelled nor returned. The public-test rulebook inspected locally contains the same default. This observation does not authorize assuming that every future split has identical rules.

The question does not explicitly request cancelled orders or override the default. Its identifiers are evidence references only. Implementation must never branch on these identifiers or embed the reference answer.

### What the two actions did

1. The first action read the relevant order sheet and called `count_orders` with the correct date range and source date format, but passed `exclude_cancelled_and_returned=False`.
2. The helper reported completed and cancelled statuses, zero excluded order IDs, and 111 distinct included orders.
3. The worker deferred the first final answer so the agent could review the full-read observations.
4. The second action submitted the literal value 111 without recomputing under the required policy. The answer passed format validation.

Both the system instructions and the task's `calculation_instruction` already told the model to enable exclusions for normal order counts. Missing instructions are not the demonstrated cause. Two model calls and a review step did not establish semantic correctness.

## 3. Why it happened

### A. Business interpretation remained an unrestricted model choice

The model treated an ordinary count as a request for every status. It supplied `False` despite the rulebook and explicit instructions. There was no application-owned policy against which to check that argument.

### B. The helper validates execution inputs, not authorization

In [tools.py](../../../tabular_agent/tools.py), `count_orders` requires a boolean, parses dates, filters the period, and counts distinct IDs. When exclusions are disabled, it deliberately excludes no order IDs. The helper cannot currently tell whether that setting is justified by the question or rulebook.

Changing every call to always exclude cancelled orders would also be incorrect. Explicit all-status counts, cancelled-only counts, and cancellation-rate denominators have different semantics.

### C. Review was advisory

In [worker.py](../../../tabular_agent/worker.py), `observed_final_answer` can require another action after the first full source read. It does not compare the selected policy with the applicable business rule. The model can repeat the same candidate in its next action.

### D. Final validation had no calculation contract

In [agent.py](../../../tabular_agent/agent.py), `check_answer` delegates to `validate_answer`. The dictionary containing 111 is a valid numeric answer. The hook receives no mandatory, structured record proving which eligibility policy produced that number.

### E. Existing tests permit both policies in isolation

[test_fast_path.py](../../../tests/rewrite/test_fast_path.py) checks that `True` excludes invalid orders and `False` includes them. Those tests verify helper arithmetic. They do not establish whether an ordinary question is allowed to select `False`.

### What is not established by this trace

- It does not show a wrong reference date, row-count duplication, a worker exception, or a retry failure.
- It does not prove that model size, context length, or prompt length caused this mistake. The recorded input-token total spans two calls and must not be interpreted as one request's context size.
- It does not establish broader accuracy or latency. One corrected example would remain a development regression, not a benchmark result.

## 4. Proposed behavior and boundaries

For supported ordinary order counts, policy selection becomes an application responsibility derived from the original question and the current split rulebook. The model selects the source and actual columns, then invokes the policy-bound tool.

| Request and rule evidence | Required behavior |
| --- | --- |
| Ordinary order count; applicable default excludes cancelled and returned orders | Count distinct eligible order IDs and exclude an entire order if it has a cancelled or returned line |
| Explicit count including cancelled and returned orders | Permit an all-status count only when the resolver recognizes that override |
| Explicit cancelled-only or returned-only count | Select that status scope and count distinct IDs; do not apply the ordinary valid-order default |
| Cancellation percentage | Treat numerator and denominator as separate calculations; do not apply the simple-count final-answer check to a percentage |
| Unrecognized or conflicting order-count policy | Do not invent an exception; report unresolved policy and abstain if it cannot be resolved within the supported path |
| Question outside the supported order-count family | Preserve the general agent path; do not force revenue, debt, inventory, or repair-visit questions through this policy |

The first implementation should support simple order counts with a single numeric result. Comparisons, grouped tables, and multiple counts need separate calculation records before they can receive the same final-answer guarantee.

### Policy resolution must be explicit about its limits

Add a small pure resolver, proposed as `tabular_agent/order_policy.py`, that receives question text and the current rulebook. It must not use reference answers, question IDs, business IDs, model-generated code, or the system date to choose a policy.

Use conservative, tested recognition for supported order-count wording and rule clauses. Normalize accents, case, and abbreviations for recognition while retaining the original evidence text. Handle negation explicitly: a request to exclude cancellations is not an all-status override. A phrase such as “all orders” alone must not automatically override a rulebook that defines which orders count.

Return one of `resolved`, `unresolved`, or `not_applicable`, plus a policy record when resolved. Distinguish an ordinary count from cancelled-only, returned-only, and explicitly all-status requests. Verify that any cited rulebook excerpt is present in the actual input and that the supported clause establishes the claimed rule. Merely attaching a quote or a model-written justification is insufficient.

For an unfamiliar rulebook or ambiguous wording, return `unresolved` rather than assume the training default. This is deliberately a bounded resolver, not a claim that string matching understands arbitrary Vietnamese business rules. Its supported and unsupported cases must be documented and measured.

## 5. Proposed contracts and data flow

The following fields describe proposed internal contracts, not existing APIs.

### A. Application-owned policy record

Create the record before the agent executes code:

```json
{
  "status": "resolved",
  "intent": "simple_order_count",
  "status_scope": "eligible_orders",
  "authority": "split_rulebook",
  "rule_excerpt": "<exact applicable source excerpt>",
  "question_override_excerpt": null,
  "policy_version": 1
}
```

Supported status scopes initially include `eligible_orders`, `all_orders`, `cancelled_only`, and `returned_only`. The exact rule content must determine eligible statuses; an industry-specific exception must not be silently replaced with a universal policy.

Resolve dates through the existing `as_of_date` contract. For the initial “today” regression, the application can bind both boundaries to that date. Do not claim application-verified date ranges for other periods until those periods have resolver coverage.

### B. Bind the policy into the executor and worker

Pass the same record from `answer_question` through `build_agent` to `IsolatedExecutor`, then to the worker through application-generated protocol metadata. Keep it separate from editable Python variables. Restore it after worker restart, just as the reference date is restored.

The bounded source-preview worker remains policy-free because it only inspects sources. Standalone worker tests that do not construct a question policy remain explicit low-level tests; they must not be mistaken for protected answer runs.

### C. Enforce policy at the tool boundary

Expose a policy-bound `count_orders` to generated code. The preferred final interface lets the model supply the table, source columns, and applicable period, while the application supplies status eligibility. The model should not need to choose a raw exclusion boolean for an ordinary count.

During a short compatibility transition, accept the old boolean only when it agrees with the bound policy. Reject a contradictory `False` before treating its result as valid. Do not silently change the requested argument and leave a misleading trace. Remove the obsolete argument from the agent-facing description after callers and tests are migrated.

Keep the underlying stateless counting primitive available internally for supported scopes. Update the tool registry, worker bindings, and `send_tools` validation together so the tool description matches what actually executes in isolation.

For the observed ordinary-count case, feedback should explain that the selected policy requires excluding cancellations and returns, and instruct the agent to reload and recompute through the bound helper. Existing worker-error reset behavior must still be respected.

Explicit exception requests receive their scope from the resolver, not a second model-controlled bypass flag. If an exception is unsupported, report that limitation rather than fabricate authorization.

### D. Return an always-on calculation record

After a successful protected helper call, the worker records at least:

- Operation and policy version/scope.
- Source columns, inclusive period, and date format.
- Observed statuses, excluded distinct-order count, and included distinct-order count.
- Computed result and a calculation sequence identifier within the question.
- Whether a policy mismatch or execution failure invalidated the calculation.

Send bounded structured records to the executor with normal responses, even when optional telemetry is disabled. Do not parse free-text observations or depend on Phoenix to validate an answer. Keep detailed records in ignored local logs; the public submission schema stays unchanged.

The record describes the calculation that actually ran. It must not be supplied as an arbitrary dictionary by generated code. This protects against accidental model mistakes; it is not a new security guarantee against malicious Python introspection inside the worker.

### E. Check protected final answers

For a resolved simple order-count question, the final-answer hook requires a successful current-question calculation record using the bound policy. Require the numeric final value to equal the recorded count and reject a conflicting or stale candidate. For supported application-resolved periods, require the recorded range to agree as well.

Invalidate records after worker reset or any policy violation. A model must not obtain 111 from a rejected call and submit it as a literal in the next action. A caught policy error must still invalidate that action's calculation eligibility; catching an exception in generated code must not turn the rejected result into accepted evidence.

Allow an explicit `abstain` without a successful record. A missing or uncertain calculation is not evidence for `unanswerable`. Preserve the existing full-source-read requirement for that answer type.

This final check also prevents a simple protected count from bypassing the policy-bound helper with direct `nunique()` followed by a numeric answer. It does not establish correct file selection, join grain, or source completeness for arbitrary calculations. Those remain separate validation work.

### Intended flow

```text
Original question + current split rulebook + as_of_date
    -> bounded policy resolver
    -> application-owned policy in agent context and executor metadata
    -> model selects source and columns
    -> worker checks and applies policy-bound count
    -> structured calculation record
    -> final hook checks policy, record validity, and final value
    -> typed answer or bounded repair / abstention
```

## 6. Why this should work

| Proposed change | Failure it addresses | Limit |
| --- | --- | --- |
| Resolve policy from original inputs | The model invented an all-status interpretation | Only supported intents and rule clauses can be resolved confidently |
| Bind eligibility into the execution interface | A model-controlled `False` disabled the required exclusions | Does not select the correct file or column by itself |
| Record actual helper execution | Free-text review did not constrain acceptance | A record is evidence of the operation, not proof of complete business semantics |
| Compare the final number with a valid record | The second action repeated a literal without correction | Applies initially to simple scalar counts |
| Preserve explicit exception scopes | Always excluding cancellations would break legitimate requests | Complex percentages and grouped outputs need their own contracts |
| Retain bounded repair and abstention | A rejected policy could otherwise loop or become an unjustified answer | Safety improvements must be assessed together with coverage and PA |

For this specific trace, the wrong boolean would be rejected, or would no longer exist in the model-facing interface. A successful ordinary count would exclude the four cancelled orders and produce 107 from the source data. The final hook would accept only the value supported by the protected calculation. This is an expected result to verify during implementation, not a claim that it has already happened.

## 7. Ordered implementation tasks

| Task | Main files | Completion criterion |
| --- | --- | --- |
| 1. Capture failure with invented data | New focused tests under `tests/rewrite/` | A scripted model reproduces the wrong flag and repeated literal; the new acceptance test fails on current code |
| 2. Define and test the bounded resolver | Proposed `order_policy.py` and resolver tests | Normal counts, explicit exceptions, negation, changed rules and ambiguous wording produce the expected policy states |
| 3. Propagate application policy | `agent.py`, `executor.py`, `worker.py` | The same policy reaches the tool and survives worker restart without depending on model-supplied variables |
| 4. Bind the tool and emit calculation records | `tools.py`, `worker.py`, `executor.py` | Contradictory eligibility is rejected; supported explicit exceptions execute correctly; records exist without tracing |
| 5. Enforce final-answer acceptance | `agent.py` and focused integration tests | Wrong literals, missing records, caught policy failures and stale records cannot finalize a protected count |
| 6. Align tool descriptions and architecture docs | `README.md`, `docs/diagrams/02-question-flow.md`, `docs/diagrams/03-components.md`, `docs/diagrams/05-interactions-to-answer.md` | Document the actual implemented boundary, supported cases and remaining limitations |
| 7. Verify the reported regression with the local model | New ignored run directory | Save the resulting answer, policy/calculation events, timings and score using the same question and model configuration |
| 8. Measure generalization | Frozen training-derived development/validation samples | Report paired counts, PA, coverage and latency; inspect regressions in explicit exceptions before accepting the fix |

Tasks 1–6 should land as a focused implementation before expanding into broader business-rule infrastructure. No additional agent, external service, or framework replacement is needed for this proposal.

## 8. Validation plan

### Synthetic policy and arithmetic cases

Use invented order IDs and dates, not copied business rows. Include multiple lines per order so tests distinguish distinct-order counts from line counts.

- Ordinary count with completed, in-progress, cancelled, fully returned, and partially returned orders. Exclude the entire affected order when the rule requires it; retain in-progress orders when eligible.
- Explicit all-status, cancelled-only and returned-only requests. Verify the selected status scope rather than merely checking that a boolean can be toggled.
- Negated exceptions such as “do not count cancelled orders”; vague “all orders” wording must not automatically disable defaults.
- Rulebook absence, changed defaults, conflicting industry rules and unfamiliar phrasing. Unresolved policy must be visible rather than silently mapped to the training rule.
- Missing/ambiguous status labels, incomplete grouped cells, inconsistent dates and missing IDs. Preserve existing source-validation failures rather than hiding them with policy selection.
- Today, neighboring dates, future records and another `as_of_date`. The result must follow the question's reference date.
- Revenue, debt, product quantity, repair visits and cancellation percentages. Confirm that simple-count policy enforcement does not hijack unrelated calculations.

### Scripted agent and isolated worker cases

1. Wrong policy, then corrected computation: the invalid result cannot finalize; the corrected distinct count can.
2. Wrong policy, then literal repetition: the final check rejects the unsupported value.
3. Direct raw count bypass: a protected simple count without a valid helper record cannot finalize.
4. Caught policy exception: the action cannot create an accepted calculation record.
5. Worker reset: policy is restored, calculated variables and old records are discarded, and sources must be reloaded.
6. Multiple calculations: contradictory records cannot be silently overwritten to approve a different result.
7. Telemetry off: execution records and acceptance checks still work.
8. Repair budget exhausted: return `abstain` within the existing limits, without accepting the rejected candidate.
9. Explicit exception: a valid all-status request remains answerable; a cancellation-rate denominator is not forced into the ordinary-count scope.
10. Fresh question: neither policy nor calculation evidence leaks from the previous question.

Run `make check`, `make test`, and `make test-sandbox` in an environment with working bubblewrap namespaces. The documented Makefile gate covers `tests/rewrite/`; do not describe it as proof that every retired top-level test passes. Synthetic and scripted tests verify mechanisms, not real-model quality.

### Real-model verification

First rerun the reported training question with the same model alias, seed, question list and execution limits, recording the new code revision and available model provenance. Keep the reference answer out of inference context and score only after generation. A correct answer here establishes that the observed development regression was fixed for that run.

Then compare baseline and candidate on a frozen development sample covering ordinary and exception order counts, multiple businesses, and unrelated questions. Use identical model/server settings and question IDs for association only. Measure policy-resolution coverage, correct/wrong/abstain counts, PA, answered accuracy, model calls and latency. Baseline artifacts may require reproducing the old revision in an isolated checkout; do not reset the shared working tree.

Use business-disjoint validation for the final candidate and preserve the untouched holdout. Do not tune using public/private reference answers. If a baseline is unavailable, report candidate results without claiming a measured improvement.

## 9. Acceptance criteria and remaining risks

- The reported question returns the independently checked count through a valid calculation record, not a stored answer.
- Scripted reproduction cannot finalize the wrong policy or its repeated literal.
- Explicit supported exceptions remain correct, and unsupported policies are reported transparently.
- Policy enforcement and calculation records operate without optional tracing and remain consistent after reset.
- Unrelated answer paths and the primary scripted/isolation checks retain their documented behavior.
- Paired validation shows whether the change improves PA and what coverage or latency cost it introduces. Set any numerical release target before examining validation results; do not invent an official passing score.

The largest design risk is overconfident policy recognition. A bounded resolver must expose uncertainty, and an implementation that merely adds regexes without exception/negation tests is incomplete. Another risk is a model switching to direct Python after helper rejection; the protected final-answer contract addresses that specific bypass but does not validate arbitrary generated programs.

If a candidate only turns the reported wrong answer into abstention, it has reduced that error but has not completed the requested accuracy fix. If synthetic checks pass but real-model verification does not, report that boundary and continue diagnosing within this scope.

No runtime changes, model reruns, or accuracy improvements are established by this document. The implementation tasks above remain proposed.
