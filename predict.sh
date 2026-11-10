#!/usr/bin/env bash
# Submission entrypoint. Answers every question in $DATA_DIR the way the benchmark runs measure the
# system (docs/fix/RUNBOOK.md): prepare each business's files into a DuckDB bundle, start the local
# model server, then answer with SQL over the bundles at temperature 0 and at most six steps.
# Dependencies, weights and the server binary must already be provisioned; nothing is downloaded.
# $DATA_DIR is only read. Answers go to $RESULT_DIR/submission.jsonl and logs to $RESULT_DIR/logs/.
set -euo pipefail
cd "$(dirname "$0")"
data_dir="${DATA_DIR:-/data}"
result_dir="${RESULT_DIR:-/result}"
work_dir="${WORK_DIR:-${TMPDIR:-/tmp}/tabular-agent-$$}"   # prepared bundles; writable, outside data and results
budget="${RUN_SECONDS:-21600}"             # whole run, including preparation and model loading
margin="${STOP_MARGIN_SECONDS:-300}"       # answering stops this long before the budget ends
model_file="${MODEL_FILE:-$PWD/models/ollama/blobs/sha256-60e05f2100071479f596b964f89f510f057ce397ea22f2833a0cfe029bfc2463}"
port="${LOCAL_PORT:-18234}"
alias="${LOCAL_MODEL_ALIAS:-local-coder-7b}"
python=.venv-native/bin/python
mkdir -p "$work_dir"
note() { printf '%s %6ss %s\n' "$(date -u +%FT%TZ)" "$SECONDS" "$*" | tee -a "$work_dir/pipeline.log"; }

server_pid=
digest_pid=
cleanup() {
  if [[ -n "$digest_pid" ]]; then kill "$digest_pid" 2>/dev/null || true; fi
  if [[ -n "$server_pid" ]]; then kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true; fi
  if [[ -d "$result_dir/logs" ]]; then cp "$work_dir/pipeline.log" "$work_dir/prepare.log" "$result_dir/logs/" 2>/dev/null || true; fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

metadata=()
if [[ -n "${BASE_URL:-}" ]]; then
  base_url="$BASE_URL"                     # an already running OpenAI-compatible server
  note "using the server at $base_url"
else
  # Refuse an occupied port: never send question data to an unknown listener.
  "$python" - "$port" <<'PY'
import socket, sys
with socket.socket() as sock:
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', int(sys.argv[1])))
    except OSError as exc:
        raise SystemExit(f'Local port is unavailable: {exc}')
PY
  # Same server settings as the benchmark runs; scripts/serve-local.sh reads the LOCAL_* variables.
  LOCAL_PORT="$port" ./scripts/serve-local.sh > "$work_dir/llama-server.log" 2>&1 &
  server_pid=$!
  base_url="http://127.0.0.1:$port/v1"
  note "model server starting (pid $server_pid)"
  # The weight digest is recorded in the run manifest; hashing runs alongside preparation.
  sha256sum -- "$model_file" > "$work_dir/weights.sha256" &
  digest_pid=$!
fi

# Prepare while the model loads. Exit status 1 means some business was blocked: its questions abstain.
note "preparation started"
status=0
"$python" -m tabular_agent.cli prepare --split "$data_dir" --output "$work_dir/prepared" > "$work_dir/prepare.log" 2>&1 || status=$?
note "preparation finished with exit status $status"

if [[ -n "$server_pid" ]]; then
  for ((i = 0; ; i++)); do
    if ! kill -0 "$server_pid" 2>/dev/null; then cat "$work_dir/llama-server.log" >&2; exit 1; fi
    if "$python" -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=1)' \
         "http://127.0.0.1:$port/health" 2>/dev/null; then break; fi
    if ((i >= ${SERVER_WAIT_SECONDS:-600})); then echo "Model server did not become ready; see $work_dir/llama-server.log" >&2; exit 1; fi
    sleep 1
  done
  note "model server ready"
  wait "$digest_pid"
  metadata=(--weight-sha256 "$(cut -d ' ' -f 1 "$work_dir/weights.sha256")" --context "${LOCAL_CONTEXT:-16384}"
            --gpu-layers "${LOCAL_GPU_LAYERS:-all}" --cache-type-k "${LOCAL_CACHE_TYPE_K:-q8_0}"
            --cache-type-v "${LOCAL_CACHE_TYPE_V:-q8_0}")
fi

remaining=$((budget - SECONDS - margin))
if ((remaining <= 0)); then echo 'No time left for answering' >&2; exit 1; fi
note "answering started; stops after ${remaining}s"
"$python" -m tabular_agent.cli predict --split "$data_dir" --output "$result_dir" --prepared "$work_dir/prepared" \
  --base-url "$base_url" --model "${MODEL:-$alias}" --temperature 0 --seed 42 --max-steps 6 \
  --run-timeout "$remaining" "${metadata[@]}" "$@"
note "answering finished"
