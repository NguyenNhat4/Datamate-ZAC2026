#!/usr/bin/env bash
# One benchmark set on a freshly started local model server (docs/fix/RUNBOOK.md, section 4): start the server
# through serve-local.sh, wait until it is ready, run `cli bench` with the server and weight metadata, and stop the
# server on exit. Arguments are passed to `cli bench`.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/local-model.sh
[[ -f "$model_file" && -x "$server_bin" ]] || { echo "Local model or llama-server is missing; see README.md." >&2; exit 1; }
weight_digest=$(sha256sum -- "$model_file" | cut -d ' ' -f 1)
refuse_busy_port
mkdir -p .cache
server_log="$PWD/.cache/llama-server-$$.log"
./scripts/serve-local.sh >"$server_log" 2>&1 &
server_pid=$!
cleanup() { kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true; }
trap cleanup EXIT
trap 'exit 130' INT TERM
printf 'Starting local model; server log: %s\n' "$server_log"
for ((i=0; i<120; i++)); do
  if ! kill -0 "$server_pid" 2>/dev/null; then
    cat "$server_log" >&2
    exit 1
  fi
  if curl --silent --fail --max-time 1 "http://127.0.0.1:$port/health" >/dev/null; then
    break
  fi
  if ((i == 119)); then
    echo "Model server did not become ready; inspect $server_log" >&2
    exit 1
  fi
  sleep 1
done
.venv-native/bin/python -m tabular_agent.cli bench --base-url "http://127.0.0.1:$port/v1" --model "$alias" \
  --context "$context" --weight-sha256 "$weight_digest" --gpu-layers "$gpu_layers" \
  --cache-type-k "$cache_k" --cache-type-v "$cache_v" "$@"
