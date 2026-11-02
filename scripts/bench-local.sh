#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
model_file="${MODEL_FILE:-$PWD/models/ollama/blobs/sha256-4a188102020e9c9530b687fd6400f775c45e90a0d7baafe65bd0a36963fbb7ba}"
server_bin="${LLAMA_SERVER:-$PWD/models/runtime/llama-b12015/llama-server}"
port="${LOCAL_PORT:-18234}"
device="${LOCAL_DEVICE:-Vulkan1}"
context="${LOCAL_CONTEXT:-32768}"
gpu_layers="${LOCAL_GPU_LAYERS:-all}"
alias="${LOCAL_MODEL_ALIAS:-local-coder-3b}"
cache_k="${LOCAL_CACHE_TYPE_K:-f16}"
cache_v="${LOCAL_CACHE_TYPE_V:-f16}"
[[ -f "$model_file" && -x "$server_bin" ]] || { echo "Local model or llama-server is missing; see README.md." >&2; exit 1; }
weight_digest=$(sha256sum -- "$model_file" | cut -d ' ' -f 1)
# Fail if the port is occupied; never send question data to an unknown listener.
.venv/bin/python - "$port" <<'PY'
import socket, sys
with socket.socket() as sock:
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', int(sys.argv[1])))
    except OSError as exc:
        raise SystemExit(f'Local port is unavailable: {exc}')
PY
mkdir -p .cache
server_log="$PWD/.cache/llama-server-$$.log"
"$server_bin" -m "$model_file" --alias "$alias" --host 127.0.0.1 --port "$port" \
  --device "$device" --n-gpu-layers "$gpu_layers" --ctx-size "$context" --parallel 1 --flash-attn on \
  --cache-type-k "$cache_k" --cache-type-v "$cache_v" >"$server_log" 2>&1 &
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
.venv/bin/python -m tabular_agent.cli bench --backend openai --base-url "http://127.0.0.1:$port/v1" --model "$alias" \
  --context "$context" --weight-sha256 "$weight_digest" --gpu-layers "$gpu_layers" \
  --cache-type-k "$cache_k" --cache-type-v "$cache_v" "$@"
