#!/usr/bin/env bash
# Start the local model server with the benchmark settings of scripts/local-model.sh; runs until stopped.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/local-model.sh
[[ -f "$model_file" && -x "$server_bin" ]] || { echo 'Local model or llama-server is missing; see README.md.' >&2; exit 1; }
exec "$server_bin" -m "$model_file" --alias "$alias" --host 127.0.0.1 --port "$port" --device "$device" \
  --n-gpu-layers "$gpu_layers" --ctx-size "$context" --parallel 1 --flash-attn on \
  --cache-type-k "$cache_k" --cache-type-v "$cache_v" --no-context-shift
