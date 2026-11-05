#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
model_file="${MODEL_FILE:-$PWD/models/ollama/blobs/sha256-60e05f2100071479f596b964f89f510f057ce397ea22f2833a0cfe029bfc2463}"
server_bin="${LLAMA_SERVER:-$PWD/models/runtime/llama-b12015/llama-server}"
[[ -f "$model_file" && -x "$server_bin" ]] || { echo 'Local model or llama-server is missing; see README.md.' >&2; exit 1; }
exec "$server_bin" -m "$model_file" --alias "${LOCAL_MODEL_ALIAS:-local-coder-7b}" \
  --host 127.0.0.1 --port "${LOCAL_PORT:-8000}" --device "${LOCAL_DEVICE:-Vulkan1}" \
  --n-gpu-layers "${LOCAL_GPU_LAYERS:-all}" --ctx-size "${LOCAL_CONTEXT:-16384}" \
  --parallel 1 --flash-attn on --cache-type-k "${LOCAL_CACHE_TYPE_K:-q8_0}" \
  --cache-type-v "${LOCAL_CACHE_TYPE_V:-q8_0}" --no-context-shift
