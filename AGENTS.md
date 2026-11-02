# Coding Agent Instructions

## Scope and reading order

This repository develops a **Vietnamese tabular data analysis assistant**.

Read:

1. `project-overview.md` for the central problem statement, data contract, evaluation requirements, and submission guidance transcribed from the supplied PDF.
2. Dataset `rulebook.md`, `README.md`, and runtime instructions when data is available.

When working against an evaluation profile, its current official requirements take precedence over an outdated local summary. Record discrepancies and update the relevant documentation. Do not invent unverified rules.

## Working with the team

- **Do not use skills unless explicitly requested by the user.**
- Write documentation in English and communicate in English unless the user requests another language. Preserve technical field names, APIs, paths, and data types. Vietnamese question/answer strings may remain as target-language data examples.
- Work only within the assigned scope. Documentation does not authorize implementation automatically.
- Inspect Git status and relevant files before editing. Preserve unrelated work by teammates.
- Distinguish product goals, evaluation requirements, confirmed team decisions, and proposals. No model or framework has been selected yet.
- Prefer small changes that can be evaluated. Update the interface documentation when changing boundaries between modules.
- Report what changed, how it was checked, the results, and anything unverified. Do not claim scores, speed, reproducibility, or offline operation without actual execution evidence.

## Naming

- Do not add specific project or competition names to content or file/directory names.

## Implementation principles

- Identify the applicable evaluation profile before enforcing limits. Dates, parameter budgets, hardware, and runtime limits in a reference profile are not automatically general project requirements.
- For profiles requiring offline inference, do not call external services or download dependencies or weights during inference. Any service integration must match the assigned task and data usage permissions.
- Respect model licences and the applicable profile's limits; count auxiliary models and MoE parameters as required by that profile.
- Do not train on public/private test data or collect answers to test questions. Do not hard-code answers or branch answer logic on question `id` or `business_id`. Use those identifiers only for data routing and result association.
- Use training data and appropriately licensed sources for training/validation. Do not include a question's reference answer in its own inference context.
- Read the rulebook for the current split. Resolve relative time against `as_of_date`, not the system date.
- Keep source data read-only. The reference profile uses `/data` for input, `/result/submission.jsonl` for output, and `/result/logs/` for logs; follow a separately documented contract where applicable.
- Distinguish `unanswerable` (the data cannot support an answer) from `abstain` (the system chooses not to answer).
- Do not commit datasets, weights, Docker archives, logs containing real data, large generated results, or secrets.

## Evaluation and reporting

- The local runner is documented in `README.md`: `make bench`, `make predict`, `make test`, and `make test-sandbox`. Benchmarks require a provisioned local model server; scripted tests do not establish model quality.
- Use the applicable profile's metrics and matching rules. Implement PA according to the reference profile when relevant. Distinguish internal evaluators from official evaluators.
- Run checks appropriate to logic changes and state whether they use synthetic data, training/validation data, or benchmark data. Mock-model checks do not establish real-model quality.
- Distinguish measurements on team hardware from profile reference limits. Do not claim compliance on unverified hardware.
- Once a model is selected, document its weight source, release date, licence, parameter count, version, and quantization. Record configurations and seeds for reproducibility.

## Git operations

- Do not create commits, push to a remote, or submit an entry unless explicitly requested in the current task.
- Reset commits only when explicitly requested. Preserve working files by default; do not use a hard reset, delete work, or force-push without explicit authorization for those actions.
