# Settings of the local model server for benchmark runs and for predict.sh (docs/fix/RUNBOOK.md, section 1).
# Sourced from the repository root by serve-local.sh, bench-local.sh and predict.sh, so the server flags and the
# metadata recorded in each run's manifest come from one place. Each value can be overridden by its variable.
model_file="${MODEL_FILE:-$PWD/models/ollama/blobs/sha256-60e05f2100071479f596b964f89f510f057ce397ea22f2833a0cfe029bfc2463}"
server_bin="${LLAMA_SERVER:-$PWD/models/runtime/llama-b12015/llama-server}"
port="${LOCAL_PORT:-18234}"
device="${LOCAL_DEVICE:-Vulkan1}"
context="${LOCAL_CONTEXT:-16384}"
gpu_layers="${LOCAL_GPU_LAYERS:-all}"
alias="${LOCAL_MODEL_ALIAS:-local-coder-7b}"
cache_k="${LOCAL_CACHE_TYPE_K:-q8_0}"
cache_v="${LOCAL_CACHE_TYPE_V:-q8_0}"

# Stop when the port is taken: never send question data to an unknown listener.
refuse_busy_port() {
  .venv-native/bin/python - "$port" <<'PY'
import socket, sys
with socket.socket() as sock:
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', int(sys.argv[1])))
    except OSError as exc:
        raise SystemExit(f'Local port is unavailable: {exc}')
PY
}
