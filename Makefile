.DEFAULT_GOAL := help
PYTHON := .venv-native/bin/python
UV_CACHE_DIR := $(CURDIR)/.cache/uv
export UV_CACHE_DIR
SPLIT ?= data/train
LIMIT ?= 20
SEED ?= 42
OUT ?=
ARGS ?=

.PHONY: help setup ui serve-local telemetry bench bench-local demo predict doctor profile prepare test test-sandbox check
help:
	@echo 'make ui                           Open the local question picker and visual test results'
	@echo 'make serve-local                  Start the local model server for the UI'
	@echo 'make telemetry                    Start local Phoenix for agent trace inspection'
	@echo 'make demo                         Run the new agent on invented cross-month sales data'
	@echo 'make bench-local LIMIT=5           Start the provisioned local model and benchmark'
	@echo 'make bench                        Benchmark against an already running local server'
	@echo 'make predict SPLIT=data/public_test  Predict without reading reference answers'
	@echo 'make profile                      Fully inspect source data without questions or answers'
	@echo 'make prepare                      Publish validated DuckDB data and schema bundles'
	@echo 'make test                         Rewrite contract and scripted agent checks'
	@echo 'make test-sandbox                 Also check the isolated worker with invented data'
	@echo 'BASE_URL=http://127.0.0.1:8000/v1 MODEL=name OUT=runs/name ARGS="..."'

.venv-native/.ready: pyproject.toml uv.lock
	env UV_PROJECT_ENVIRONMENT=.venv-native uv sync --locked --python 3.12
	@touch $@
setup: .venv-native/.ready
ui: setup
	$(PYTHON) -m streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
serve-local:
	./scripts/serve-local.sh
telemetry: setup
	mkdir -p .cache/phoenix
	env PHOENIX_WORKING_DIR=$(CURDIR)/.cache/phoenix $(PYTHON) -m tabular_agent.phoenix_local serve
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
profile: setup
	$(PYTHON) -m tabular_agent.cli profile --split "$(SPLIT)" $(if $(OUT),--output "$(OUT)",) $(ARGS)
prepare: setup
	$(PYTHON) -m tabular_agent.cli prepare --split "$(SPLIT)" $(if $(OUT),--output "$(OUT)",) $(ARGS)
test: setup
	$(PYTHON) -m unittest discover -s tests/rewrite -v
	$(PYTHON) -m unittest discover -s tests/preparation -v
test-sandbox: setup
	env RUN_SANDBOX_TESTS=1 $(PYTHON) -m unittest discover -s tests/rewrite -v
	$(PYTHON) -m unittest discover -s tests/preparation -v
check:
	$(PYTHON) -m compileall -q tabular_agent tests streamlit_app.py
	git diff --check
