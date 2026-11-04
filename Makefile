.DEFAULT_GOAL := help
PYTHON := .venv-native/bin/python
UV_CACHE_DIR := $(CURDIR)/.cache/uv
export UV_CACHE_DIR
SPLIT ?= data/train
LIMIT ?= 20
SEED ?= 42
OUT ?=
ARGS ?=

.PHONY: help setup bench bench-local demo predict doctor test test-sandbox check
help:
	@echo 'make demo                         Run the new agent on invented cross-month sales data'
	@echo 'make bench-local LIMIT=5           Start the provisioned local model and benchmark'
	@echo 'make bench                        Benchmark against an already running local server'
	@echo 'make predict SPLIT=data/public_test  Predict without reading reference answers'
	@echo 'make test                         Contract and scripted native-loop checks'
	@echo 'make test-sandbox                 Real isolated worker checks with invented data'
	@echo 'BASE_URL=http://127.0.0.1:8000/v1 MODEL=name OUT=runs/name ARGS="..."'

.venv-native/.ready: pyproject.toml uv.lock
	env UV_PROJECT_ENVIRONMENT=.venv-native uv sync --locked --python 3.12
	@touch $@
setup: .venv-native/.ready
bench: setup
	$(PYTHON) -m tabular_agent.cli bench --split "$(SPLIT)" --limit "$(LIMIT)" --seed "$(SEED)" $(if $(OUT),--output "$(OUT)",) $(ARGS)
bench-local: setup
	./scripts/bench-local.sh --split "$(SPLIT)" --limit "$(LIMIT)" --seed "$(SEED)" $(if $(OUT),--output "$(OUT)",) $(ARGS)
demo: setup
	$(PYTHON) -m tabular_agent.demo --run
predict: setup
	$(PYTHON) -m tabular_agent.cli predict --split "$(SPLIT)" --limit 0 --seed "$(SEED)" $(if $(OUT),--output "$(OUT)",) $(ARGS)
doctor: setup
	$(PYTHON) -m tabular_agent.cli doctor $(ARGS)
test: setup
	$(PYTHON) -m unittest discover -s tests -v
test-sandbox: setup
	env RUN_SANDBOX_TESTS=1 $(PYTHON) -m unittest discover -s tests -v
check:
	$(PYTHON) -m compileall -q tabular_agent tests
	git diff --check
