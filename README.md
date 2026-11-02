# Local Tabular Analysis Agent

A single ReAct-style agent profiles data, plans a calculation, executes Python, checks reported evidence, recomputes higher-risk results, and reviews before answering. It remains one model-driven agent with isolated Python execution.

## Quick start

For this workstation, the experimental local baseline uses `qwen2.5-coder:3b` (Q4_K_M) and a standalone Vulkan `llama.cpp` server. After the model and runtime have been provisioned into `models/`, run:

```bash
make bench-local LIMIT=5
```

This starts the local server for the run, selects `Vulkan1` (the tested NVIDIA device on this host), measures the requested sample, and stops the server. Set `LOCAL_DEVICE`, `LOCAL_PORT`, `LOCAL_CONTEXT`, `LOCAL_GPU_LAYERS`, `LOCAL_CACHE_TYPE_K`, `LOCAL_CACHE_TYPE_V`, `MODEL_FILE`, or `LOCAL_MODEL_ALIAS` if needed. This route does not require the system Ollama installation. `LIMIT=0` runs the full split; the default limit is 20. Run logs and model files can contain business data and are ignored by Git.

The default experimental weights are [Qwen2.5-Coder-3B-Instruct](https://huggingface.co/Qwen/Qwen2.5-Coder-3B-Instruct), released in the [November 2024 model family](https://qwenlm.github.io/blog/qwen2.5-coder-family/). The [Ollama variant](https://ollama.com/library/qwen2.5-coder:3b) reports 3.09B parameters, Q4_K_M quantization and the Qwen Research License, which is limited to non-commercial research/evaluation. Its suitability for an actual competition submission or other use is not established. No auxiliary model is used. The portable runtime is [llama.cpp b12015](https://github.com/ggml-org/llama.cpp/releases/tag/b12015); model version/digest and run settings are recorded per run where the backend exposes them.

An additional candidate is [Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507), created on 5 August 2025. Its [Ollama Q4_K_M variant](https://ollama.com/library/qwen3:4b-instruct-2507-q4_K_M) reports 4.02B parameters and Apache 2.0 licensing. Its registry model blob has SHA-256 `85e4a5b7b8ef0e48af0e8658f5aaab9c2324c76c1641493f4d1e25fce54b18b9`; the downloaded 2,497,280,480-byte file was verified against this checksum. It is an experimental candidate, not a demonstrated accuracy winner.

A second experimental candidate is [Qwen2.5-Coder-7B-Instruct](https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct), released in the November 2024 model family. The [7B Q4_K_M variant](https://ollama.com/library/qwen2.5-coder:7b-instruct-q4_K_M) reports 7.62B parameters and Apache 2.0 licensing. The downloaded 4,683,074,048-byte model blob was verified as SHA-256 `60e05f2100071479f596b964f89f510f057ce397ea22f2833a0cfe029bfc2463`. It is a candidate, not a demonstrated accuracy winner or a complete submission eligibility certification.

Downloaded Ollama model blob SHA-256: `4a188102020e9c9530b687fd6400f775c45e90a0d7baafe65bd0a36963fbb7ba`. The runtime archive SHA-256 is `bb4c5600ceac4c97fbf2c65ff7b945a178ecff657e87dbd00ab8ea763bd85e59`. The local launcher defaults to context 32768, one slot, all GPU layers and flash attention; the 4B target uses Q8 key/value cache to reduce VRAM use, while the 7B target defaults to 20 GPU layers; inference uses seed 42 and temperature zero by default. OpenAI-compatible model discovery exposes the alias but not necessarily the weight hash, so keep this record alongside `config.json`.

Requirements: Linux with working unprivileged user namespaces and `bubblewrap`, `make`, `uv`, system Python 3.14 (the tested interpreter), and a running **local** model server. The pinned dependency set was resolved on Python 3.14/Linux; other Python versions are unverified. Install system prerequisites separately if missing (`sudo apt-get install bubblewrap make`). Python dependency installation is automated by `make`; model weights are not downloaded automatically.

With Ollama already serving exactly one installed model:

```bash
make bench
```

This installs/syncs the locked Python dependencies once, selects 20 training questions with seed 42, checks the execution sandbox and model discovery, runs inference, then computes internal PA. If several models are installed, select one:

```bash
make bench MODEL=your-installed-model LIMIT=20
```

If Ollama is installed but stopped, run `ollama serve` in another terminal first. Use `ollama list` to find installed model names. The workstation launcher uses the experimental weights described above. Other installations must provision weights separately before benchmarking.

For an existing local OpenAI-compatible inference server:

```bash
make bench BACKEND=openai BASE_URL=http://127.0.0.1:8000/v1 MODEL=your-served-model
```

The server must implement model discovery and Chat Completions with JSON-object output. Only localhost endpoints are accepted. HTTP redirects and environment proxies are disabled. Optional authentication uses `LOCAL_MODEL_API_KEY` from the environment, never CLI arguments or committed files. A local tunnel to an external service does not satisfy offline inference requirements.

## Commands

| Command | Purpose |
| --- | --- |
| `make help` | Short command reference |
| `make setup` | Install locked Python dependencies; may need internet |
| `make doctor` | Verify sandbox/imports and local model discovery; does not generate an answer |
| `make bench LIMIT=0` | Evaluate all training questions |
| `make bench-4b LIMIT=5` | Benchmark the provisioned 4B instruction candidate |
| `make bench-7b LIMIT=5` | Benchmark the provisioned 7B candidate with partial GPU offload |
| `make bench-dev LIMIT=20` | Balanced sample from development businesses |
| `make bench-validation LIMIT=20` | Balanced sample from validation businesses |
| `make compare-local LIMIT=20` | Compare the provisioned 3B, 4B and 7B models on the same validation sample |
| `make calibrate RUN=runs/name POLICY=runs/policy.json` | Fit an abstention policy from an adequately sized validation run |
| `make bench LIMIT=50 SEED=123` | Deterministic sample for development measurement |
| `make bench ARGS="--business biz_101,biz_103"` | Filter businesses before sampling |
| `make predict SPLIT=data/public_test` | Predict all questions without reading references |
| `make bench OUT=runs/experiment-01` | Choose a new/empty run directory |
| `make test` | Dependency-free unit/scripted-agent tests |
| `make test-sandbox` | Real sandbox and synthetic end-to-end tests; no LLM |
| `make check` | Compile Python and check diff whitespace |

Run directories are never overwritten. There is no implicit resume: preserve partial output and start a new directory for a rerun. Progress prints one line per completed question. Ctrl-C preserves completed answers and writes a partial report; missing predictions count as abstentions.

A full invocation without Make, after setup:

```bash
.venv/bin/python -m tabular_agent.cli bench \
  --split data/train --limit 20 --seed 42 \
  --backend ollama --model your-installed-model \
  --max-executions 5 --max-steps 12 --question-timeout 240
```

Defaults: up to 12 model turns, 2,048 generated tokens per turn, 5 Python execution attempts, 40 seconds per worker, 120 seconds per model HTTP request, 240 seconds per question, and 21,600 seconds per run. The worker also has a 30-second CPU limit. Remaining question/run time shortens subsequent calls. Startup/preflight time is included in wall-clock reporting; server startup before the CLI is not. Serial execution is deliberate for the first baseline; no throughput on reference hardware is claimed.

`ARGS` forwards additional CLI flags, for example `ARGS="--schema-mode compact --verification review"`. For `bench-local`, set `LOCAL_CONTEXT` to control both the llama.cpp server and the recorded client context; an existing OpenAI-compatible server controls its own context limit. Temperature is zero and the seed is recorded, but backend determinism is not guaranteed.

## Outputs

Each run writes:

- `submission.jsonl`: completed predictions, flushed and fsynced after each question.
- `report.json`: local wall time, latency summaries, completion status; for `bench`, PA, coverage, answered accuracy, invalid count and per-type/business outcomes.
- `logs/000000.json`, etc.: question ID, generated actions/code, observations, final answer and elapsed time.
- `questions.jsonl`: the exact selected question set, needed to rescore partial runs.
- `config.json`: model/backend configuration, installed library versions, rulebook, source and selected-question hashes, and model metadata. The local launcher computes and records the actual weight SHA-256 and GPU layer setting.

Artifacts can contain real business data and are Git-ignored. They remain on the local filesystem. References are opened by the scorer **after inference**, never injected into model context or mounted in execution workers. The numerical score in synthetic tests is only a fixture assertion, not a benchmark result.

Rescore against training references:

```bash
.venv/bin/python -m tabular_agent.cli score \
  --questions runs/experiment-01/questions.jsonl \
  --predictions runs/experiment-01/submission.jsonl \
  --references data/train/answers.jsonl
```

## Agent interfaces

Flow: **schema discovery → calculation plan → isolated Python → evidence checks → optional recomputation → review or repair**. There is one model and one ReAct loop. The full current rulebook, business README, question, `as_of_date` and bounded schema profile are provided to the model. Question and business IDs stay in the host for association and routing. Files receive stable `F1`, `F2`, ... aliases for each business, so the model does not need to retype filenames with damaged encodings.

The `plan` action declares metric, period (ISO dates or null), source files, aggregation grain, filters, unit and risk. The host rejects missing fields, reversed dates and nonexistent source files. A computation must set a typed `result` and an `evidence` object containing `sources`, `row_count` and `checks`; dated nonempty results also include `date_min` and `date_max`. The host checks reported dates are within the planned period, declared sources agree with the plan, and zero matching dated rows report coverage. Evidence is generated by the same code and is **not independent proof** of the rows or rules used.

Example actions on a synthetic sales file:

```json
{"action":"plan","plan":{"metric":"total quantity","period":null,"sources":["F1"],"grain":"item","filters":[],"unit":"pieces","risk":"normal"}}
{"action":"python","code":"df=read_table('F1', header_row=0)\nresult={'type':'number','answer':sum(int(x) for x in df['quantity'])}\nevidence={'sources':['F1'],'row_count':len(df),'checks':{'unit':'pieces'}}"}
{"action":"review","verdict":"accept","findings":"Checked the requested measure and units."}
```

Available sandbox helpers are `read_table` (CSV/XLSX/SQLite; explicit header/sheet/table options), `parse_numbers` (explicit separators and Decimal), `parse_dates` (explicit day-first setting and missing-year handling), `checked_join` (explicit cardinality and unmatched keys), and `split_units`. They do not infer business rules, forward-fill rows, or silently resolve ambiguity. Excel profiles include header candidates; SQLite profiles include declared keys and columns. Shared names across files are hints, not proven joins. Every Python call starts fresh; `DATA_DIR`, `CALENDAR_PATH`, `AS_OF_DATE` and `FILES` (alias-to-exact-path mapping) are provided. Inspect and plan actions, `read_table`, and evidence can refer to aliases. Generated Python runs from private scratch containing alias and original-name links to the read-only source; direct calls such as `pd.read_csv("F1")` also work. If an original filename collides with an alias, use its absolute `/data` path. For diagnostics without `result`, set the Python action's `purpose` to `inspect`.

With the default `--verification high-risk`, plans marked high-risk and plans that specify dates, multiple sources or money units require a different second program whose typed result matches, followed by a model review. `--verification all` requests recomputation for all substantive answers; `review` skips it for ablation. The host checks that the two program texts differ and their typed results match; it cannot prove the algorithms are independent or even that the reported evidence is accurate. The same model writes both programs, so agreement and a self-review can share the same mistake. An explicit `unanswerable` requires a successful full-data diagnostic reporting sources, searched row count, what is missing, checks and `search_complete=true`; a preview or failed execution is insufficient. The diagnostic evidence remains a model-generated claim and does not itself prove that the scan was complete.

One repeated tool action produces feedback without rerunning it; a further identical repetition stops the loop. Missing results, invalid evidence, wrong period bounds, failed joins, inconsistent recomputation and malformed output produce bounded repair feedback. An unsupported missing-data claim receives a repair opportunity before repeated claims lead to abstention. Python observations include bounded dataframe/series shapes and column names to help identify contradictory empty-data claims; a dated calculation with an empty dataframe receives a prompt to check date and entity filters. These diagnostics do not prove that the filters or calculation are correct. On a column `KeyError`, the agent also supplies the exact previously inspected columns and candidate zero-based header offset for planned sources, and identifies runtime dataframes where the missing column is visible; these are suggestions, not proof of the correct schema. It also explains pandas' `header` argument when the helper's `header_row` argument is mistakenly used with pandas. CSV inspection strips a UTF-8 byte-order mark consistently with the provided reader. `--schema-mode compact` reduces examples but retains the full rulebook; the default is `full`.

## Business-disjoint evaluation

The training questions can be assigned to `dev`, `validation` or `holdout` using a deterministic SHA-256 bucket of business ID and `--partition-seed` (default 42). IDs are used for evaluation grouping only, never answer logic. With the currently installed training set and seed 42, there are 18 development businesses, 6 validation businesses and 6 holdout businesses. Previously inspected sample businesses fall in development. `--sample balanced` selects across businesses without looking at answers. `--partition all` preserves the original general-purpose runner behavior.

Use validation for model and prompt comparisons; do not score or tune against holdout until the configuration is frozen. The shared training reference file was previously read for scorer self-comparison, but holdout-specific scores have not been used for tuning. Changing the partition seed changes membership and can leak a previously inspected business into a new holdout. `compare-local` requires the selected local weights, runs sequentially and checks that selected-question and rulebook hashes match before reporting PA, coverage, answered accuracy and time. The local scorer opens training references only after inference. The comparison defaults to three models and Q8 key/value cache for all models; `COMPARE_MODELS=4b,7b` selects a pair. Q8 cache is an accuracy/memory trade-off and can change model output. The cache configuration is recorded and must match across compared runs. Neither this partition nor the scorer establishes official test accuracy. Reports group non-exclusive diagnostic signals by outcome; these are triage hints, not automatic proof of semantic causes.

## Validation-based abstention calibration

After collecting a completed, uncalibrated validation run from this repository’s `data/train` split with at least 20 questions and 10 substantive answers, fit a policy with `make calibrate RUN=runs/validation-run POLICY=runs/policy.json`. The fitter compares four fixed policies using validation PA and breaks ties by coverage. It records aggregate counts, not reference answer values or question IDs. A policy is tied to the recorded model-weight SHA-256, source digest, cache configuration, GPU layers and inference settings; incompatible runs reject it.

Apply a compatible policy with `ARGS="--abstention-policy runs/policy.json"` on a later run. Calibration cannot create correct answers; it can only change some answers to abstentions. Policy selection scores on the same validation data are optimistic, especially for small or low-coverage samples. Evaluate the frozen policy separately on holdout before drawing conclusions. Public/private test answers must never be used for fitting.

## Isolation and limitations

Execution requires bubblewrap; there is no unsafe host `exec` fallback. Workers see only the system runtime, project Python environment, trusted worker script, current business directory, calendar and private scratch directory. They do not see the repository, home credentials or split references. Input mounts are read-only and network/PID namespaces are separate. Output is bounded and symlink outputs are rejected before host reads. The worker limits per-process address space to 2 GiB, file size to 1 MiB and open file descriptors to 64. This is Linux process isolation, not a VM; it depends on host kernel support and does not claim resistance to kernel vulnerabilities.

Exact business semantics remain model-dependent: code can run successfully while using the wrong join, period, eligibility rule or rounding convention. Format validation is not semantic verification. Model-generated observations must not be treated as proof of correctness.

## Internal evaluation contract

PA is `(correct - wrong) / selected_questions`; missing answers count as abstain. Invalid typed answers count as wrong. The internal matcher covers strict types, NFC/lowercase/punctuation/whitespace normalization without removing Vietnamese diacritics, aliases, ordered lists, and unordered tables with name-based column mapping or positional fallback.

Known assumptions requiring comparison with the official evaluator: absolute numeric tolerance `1e-8`, decimal half-up rounding for percent matching, full-column-name matching before positional fallback, and row alias interpretation (first/name column). Do not claim official score equivalence. Duplicate prediction IDs and unknown IDs are rejected rather than guessed. A seeded training sample is a development measurement, not evidence of generalization; use held-out businesses/industries for model selection.

## Offline submission boundary

`predict.sh` runs the already provisioned CLI without installing or downloading anything. Defaults: input `/data`, outputs `/result/submission.jsonl` and `/result/logs/`. `/result` must be empty. Override `DATA_DIR`, `RESULT_DIR`, `MODEL`, `BACKEND`, `BASE_URL` as needed. The local model server must already be started within the offline deployment environment.

This repository currently supplies a local runner, not a verified competition Docker image. The separate official base-image/packaging guidance remains unavailable. Before submission, package dependencies and weights, start the inference backend inside the offline environment, test nested sandbox availability, and verify the complete run on the required hardware. No remote APIs are supported.

Before selecting a model, record weight source, immutable revision, release date, licence, total parameters including auxiliary models, quantization, and decoding configuration. Runtime discovery metadata is not a licence/compliance certification. The reference profile's 15B limit and release cutoff must be checked for the selected model.

## Technical references

- [Ollama API](https://github.com/ollama/ollama/blob/main/docs/api.md): local JSON chat transport.
- [Bubblewrap](https://github.com/containers/bubblewrap): Linux namespace execution boundary.

## Verification record — 30 October 2026

- `make test-sandbox`: all 22 tests passed, including a synthetic CLI benchmark with a scripted model, actual CSV/XLSX/SQLite reads, read-only mounts, blocked network access, timeouts and symlink-output rejection.
- `make check` and `bash -n predict.sh`: passed.
- All 3,000 training reference records passed matcher self-comparison. This checks accepted reference shapes, not official scorer equivalence or model accuracy.
- `make doctor`: sandbox passed; model discovery failed because no Ollama server was listening at the default endpoint. A later run used the separately provisioned llama.cpp server.

Two real-model runs of the same seeded five-question training sample were performed on a laptop RTX 4050 (6 GB VRAM), using the internal scorer. Before fixing relative data paths: PA -0.20, 0 correct, 1 wrong, 4 abstain, 361.6 seconds. After that fix: PA 0.00, 0 correct, 0 wrong, 5 abstain, 357.0 seconds. Client times exclude server startup. The second run provides no answered accuracy because no questions were answered. One question reached the 240-second limit in both runs. These are development measurements on a tiny training sample, not an official score, generalization estimate, reference-hardware runtime or reproducibility result.

## ReAct optimization record — 31 October 2026

The loop now receives cached schema discovery, rejects repeated identical tool actions, detects truncated generation, and permits `final` to accept a validated Python result without copying it. Numeric NumPy results remain numeric. Defaults changed from 10 turns / 4,096 output tokens / 240 seconds per question to 6 turns / 2,048 tokens / 120 seconds. This trades exploration budget for bounded latency; difficult questions may abstain sooner.

Validation: 21 unit/scripted tests passed after the final execution-failure guard. All 8 real sandbox integration tests passed for schema discovery and numeric serialization, including the synthetic end-to-end CLI test. These tests do not measure real-model quality.

The first optimization run (`runs/20261031-025807-535732`) used the same five training questions, seed 42 and local 3B model as the earlier baseline: 140.008 seconds, 0 correct, 2 wrong, 3 abstain, PA -0.40. One incorrect answer classified a Python failure as missing data; the final guard addresses that procedural error. Another executed successfully but counted catalogue rows instead of calculating inventory, demonstrating that execution and format checks cannot establish semantic correctness. No reference values were injected into inference or used as answer rules.

Final implementation run (`runs/20261031-030049-020653`): 104.289 seconds, median 20.727 seconds/question, maximum 26.676 seconds; 0 correct, 1 wrong, 4 abstain, 0 invalid, PA -0.20, coverage 20%. The earlier 356.959-second baseline answered no questions (PA 0); this run is faster but does not improve quality. Both use the same five-question training sample on the laptop RTX 4050 and the internal evaluator. Runtime controls are verified; this 3B baseline remains inadequate for accurate analysis. These are individual development runs, not a controlled repeated timing study or an official score.

## Accuracy development record — 31 October 2026

The 7B model loaded with 20–24 layers on the laptop GPU, while full offload with a 32K F16 cache exceeded available VRAM. A one-question development run reached the reference numeric value but abstained because it reported the evidence date in an unambiguous slash format. The evidence validator now accepts unambiguous slash dates; ambiguous dates remain rejected. This observation is not a measured post-fix score.

A five-question, business-balanced training-validation comparison with identical source and Q8 cache settings produced: 3B, 0 correct / 0 wrong / 5 abstain (PA 0.00); 4B, 0 correct / 1 wrong / 4 abstain (PA -0.20). These are development measurements using the internal scorer, not an official or representative accuracy estimate. The 4B wrong answer inferred missing information from a small preview; the controller now requires a successful full-data diagnostic reporting absence before an `unanswerable` result. Any later benchmark must use the updated source; the earlier comparison is not a final-code result.

After adding the absence-evidence guard, a matched five-question training-validation run using 3B and 4B, Q8 KV cache and the same source returned 0 correct, 0 wrong and 5 abstentions for both models (PA 0; coverage 0). The small sample does not establish overall model quality. After that run, a parser bug affecting native numeric values under comma-decimal conventions was fixed and checked in sandbox tests; the five-question run predates that parser fix.

After the preceding benchmarks, exact filename aliases and native-number parsing for mixed locale formats were added and verified with synthetic sandbox tests. Earlier model scores predate these changes.

A matched three-question validation run after file aliases and numeric parsing returned 0 correct, 0 wrong and 3 abstentions for both 3B and 4B (PA 0; coverage 0). In the first case the 4B model opened the previously inaccessible file but did not apply the inspected header row. Schema-based repair feedback was added afterward and is covered by synthetic tests; the three-question scores predate that feedback.

After adding header feedback, bounded dataframe-shape diagnostics, and consistent CSV BOM handling, a 4B run on the same three validation questions again returned 0 correct, 0 wrong and 3 abstentions (PA 0; coverage 0; 149.797 seconds of client time, excluding prior server startup). It found matching rows in one case but still claimed the data was absent, and dropped required columns during a join in another. These cases have been repeatedly inspected during development and are not an independent evaluation. At that point 52 tests passed with the real Linux sandbox enabled, including synthetic regressions for row-count feedback and CSV inspection. Passing these tests does not establish model accuracy.

On the same three questions and agent source, the 7B model also returned 0 correct, 0 wrong and 3 abstentions (PA 0; coverage 0; 297.234 seconds of client time, excluding prior server startup). Both runs used Q8 key/value cache and a 150-second per-question limit; the 7B run used 24 GPU layers while 4B ran all layers on GPU. The sample cannot establish a model ranking. After those runs, additional repair feedback was added for using the helper's `header_row` argument with pandas and for columns found in an input dataframe but missing after a join. The reported runs predate that feedback.

After adding a dated empty-selection diagnostic, a 7B rerun of one previously inspected validation question still abstained (PA 0; coverage 0; 70.326 seconds of client time, excluding prior server startup). The agent received the diagnostic but chose to abstain rather than repair its date filter. The final source passed 55 tests with the real Linux sandbox enabled. Neither these tests nor the one-question regression establish a general accuracy gain. Calibration remains unfitted because there are too few answered validation cases; the holdout release gate remains untested.
