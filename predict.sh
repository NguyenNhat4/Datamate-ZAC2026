#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# Dependencies and local model must already be provisioned. Never downloads at inference.
exec .venv/bin/python -m tabular_agent.cli predict --split "${DATA_DIR:-/data}" --output "${RESULT_DIR:-/result}" "$@"
