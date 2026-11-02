.DEFAULT_GOAL := help
PYTHON := .venv/bin/python
UV_CACHE_DIR := $(CURDIR)/.cache/uv
export UV_CACHE_DIR
BACKEND ?= ollama
MODEL ?=
BASE_URL ?=
SPLIT ?= data/train
LIMIT ?= 20
SEED ?= 42
OUT ?=
ARGS ?=
RUN ?=
POLICY ?= runs/abstention-policy.json
MODEL_7B_FILE ?= $(CURDIR)/models/ollama/blobs/sha256-60e05f2100071479f596b964f89f510f057ce397ea22f2833a0cfe029bfc2463
MODEL_4B_FILE ?= $(CURDIR)/models/ollama/blobs/sha256-85e4a5b7b8ef0e48af0e8658f5aaab9c2324c76c1641493f4d1e25fce54b18b9
GPU_LAYERS_7B ?= 20
export BACKEND MODEL BASE_URL

.PHONY: help setup bench bench-local bench-4b bench-7b bench-dev bench-validation compare-local calibrate predict doctor test test-sandbox check
help:
	@echo 'make bench [MODEL=name] [LIMIT=20]  Install dependencies and measure on training data'
	@echo 'make bench-local LIMIT=5           Run the installed local model and benchmark'
	@echo 'make bench-4b LIMIT=5             Benchmark the installed 4B instruction model'
	@echo 'make bench-7b LIMIT=5             Benchmark the installed 7B model with partial GPU offload'
	@echo 'make bench-validation LIMIT=20     Measure a business-disjoint validation sample'
	@echo 'make compare-local LIMIT=20        Compare provisioned models on the same validation sample'
	@echo 'make calibrate RUN=runs/name       Fit a policy from an adequately sized scored validation run'
	@echo 'make bench LIMIT=0                 Measure all training questions'
	@echo 'make predict SPLIT=data/public_test  Predict all questions without references'
	@echo 'make doctor                       Check isolated Python and local model discovery'
	@echo 'make test                         Unit and scripted-agent tests (no model)'
	@echo 'make test-sandbox                 Real Linux isolation/CSV/XLSX/SQLite tests'
	@echo 'BACKEND=ollama|openai BASE_URL=http://127.0.0.1:port[/v1] MODEL=name OUT=runs/name ARGS="..."'

.venv/.ready: requirements.lock
	uv venv --python /usr/bin/python3 --allow-existing .venv
	uv pip sync --python $(PYTHON) --require-hashes requirements.lock
	@touch $@
setup: .venv/.ready
bench: setup
	$(PYTHON) -m tabular_agent.cli bench --split "$(SPLIT)" --limit "$(LIMIT)" --seed "$(SEED)" $(if $(OUT),--output "$(OUT)",) $(ARGS)
bench-local: setup
	./scripts/bench-local.sh --split "$(SPLIT)" --limit "$(LIMIT)" --seed "$(SEED)" $(if $(OUT),--output "$(OUT)",) $(ARGS)
bench-4b: setup
	env MODEL_FILE="$(MODEL_4B_FILE)" LOCAL_MODEL_ALIAS=local-instruct-4b LOCAL_GPU_LAYERS=all \
	  LOCAL_CACHE_TYPE_K=q8_0 LOCAL_CACHE_TYPE_V=q8_0 \
	  ./scripts/bench-local.sh --split "$(SPLIT)" --limit "$(LIMIT)" --seed "$(SEED)" $(if $(OUT),--output "$(OUT)",) $(ARGS)
bench-7b: setup
	env MODEL_FILE="$(MODEL_7B_FILE)" LOCAL_MODEL_ALIAS=local-coder-7b LOCAL_GPU_LAYERS="$(GPU_LAYERS_7B)" \
	  ./scripts/bench-local.sh --split "$(SPLIT)" --limit "$(LIMIT)" --seed "$(SEED)" $(if $(OUT),--output "$(OUT)",) $(ARGS)
bench-dev: setup
	./scripts/bench-local.sh --split "$(SPLIT)" --limit "$(LIMIT)" --seed "$(SEED)" --partition dev --sample balanced $(if $(OUT),--output "$(OUT)",) $(ARGS)
bench-validation: setup
	./scripts/bench-local.sh --split "$(SPLIT)" --limit "$(LIMIT)" --seed "$(SEED)" --partition validation --sample balanced $(if $(OUT),--output "$(OUT)",) $(ARGS)
compare-local: setup
	./scripts/compare-local.sh --split "$(SPLIT)" --limit "$(LIMIT)" --seed "$(SEED)" $(ARGS)
calibrate: setup
	@test -n "$(RUN)" || { echo 'Set RUN to a completed validation run directory' >&2; exit 1; }
	$(PYTHON) -m tabular_agent.cli calibrate --run "$(RUN)" --output "$(POLICY)"
predict: setup
	$(PYTHON) -m tabular_agent.cli predict --split "$(SPLIT)" --limit 0 --seed "$(SEED)" $(if $(OUT),--output "$(OUT)",) $(ARGS)
doctor: setup
	$(PYTHON) -m tabular_agent.cli doctor $(ARGS)
test:
	python3 -m unittest discover -s tests -v
test-sandbox: setup
	RUN_SANDBOX_TESTS=1 $(PYTHON) -m unittest discover -s tests -v
check:
	python3 -m compileall -q tabular_agent tests
	git diff --check
