.DEFAULT_GOAL := help
PYTHON := .venv-native/bin/python
UV_CACHE_DIR := $(CURDIR)/.cache/uv
export UV_CACHE_DIR
SPLIT ?= data/train
LIMIT ?= 0
SEED ?= 42
OUT ?=
ARGS ?=
# Server and answering settings of the frozen-set benchmark runs (docs/fix/RUNBOOK.md, section 1):
# port 18234, SQL over prepared bundles, temperature 0, six steps (the CLI defaults).
LOCAL_PORT ?= 18234
export LOCAL_PORT
# bench and predict: the one folder of prepared bundles that holds the split's businesses.
BUNDLES ?=
# ui: folders searched, in order, for the selected business's bundle.
PREPARED ?= artifacts/prepared-r2-dev artifacts/prepared-r2-unseen artifacts/prepared-r2-public
need_bundles = $(if $(BUNDLES),,$(error Set BUNDLES to one folder of prepared bundles, for example BUNDLES=artifacts/prepared-r2-dev))

.PHONY: help setup ui serve serve-local telemetry bench bench-local demo predict doctor profile prepare test test-sandbox check
help:
	@echo 'make setup                           Install the locked dependencies into .venv-native'
	@echo 'make serve                           Start the local model server (benchmark settings, port 18234)'
	@echo 'make ui                              Pick questions, run them and inspect queries and answers'
	@echo 'make prepare SPLIT=data/train        Publish validated DuckDB bundles (OUT=folder, ARGS="--business id")'
	@echo 'make profile                         Inspect source data without questions or answers'
	@echo 'make bench BUNDLES=folder LIMIT=20   Benchmark against the running server; LIMIT=0 (default) runs all'
	@echo 'make bench-local BUNDLES=folder      Same, on a freshly started server that stops afterwards'
	@echo 'make predict SPLIT=data/public_test BUNDLES=folder   Predict without reading reference answers'
	@echo 'make telemetry                       Start local Phoenix for trace inspection'
	@echo 'make test                            Synthetic checks; no model is called'
	@echo 'make check                           Compile all modules and check whitespace'
	@echo 'Submission entrypoint: ./predict.sh (README.md). Measuring a commit: docs/fix/RUNBOOK.md.'

.venv-native/.ready: pyproject.toml uv.lock
	env UV_PROJECT_ENVIRONMENT=.venv-native uv sync --locked --python 3.12
	@touch $@
setup: .venv-native/.ready
ui: setup
	env UI_BASE_URL=http://127.0.0.1:$(LOCAL_PORT)/v1 UI_PREPARED="$(PREPARED)" \
	$(PYTHON) -m streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
serve serve-local:
	./scripts/serve-local.sh
telemetry: setup
	mkdir -p .cache/phoenix
	env PHOENIX_WORKING_DIR=$(CURDIR)/.cache/phoenix $(PYTHON) -m tabular_agent.phoenix_local serve
bench: setup
	$(need_bundles)$(PYTHON) -m tabular_agent.cli bench --split "$(SPLIT)" --prepared "$(BUNDLES)" --limit "$(LIMIT)" --seed "$(SEED)" $(if $(OUT),--output "$(OUT)",) $(ARGS)
bench-local: setup
	$(need_bundles)./scripts/bench-local.sh --split "$(SPLIT)" --prepared "$(BUNDLES)" --limit "$(LIMIT)" --seed "$(SEED)" $(if $(OUT),--output "$(OUT)",) $(ARGS)
demo: setup
	$(PYTHON) -m tabular_agent.demo --run
predict: setup
	$(need_bundles)$(PYTHON) -m tabular_agent.cli predict --split "$(SPLIT)" --prepared "$(BUNDLES)" --limit 0 --seed "$(SEED)" $(if $(OUT),--output "$(OUT)",) $(ARGS)
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
