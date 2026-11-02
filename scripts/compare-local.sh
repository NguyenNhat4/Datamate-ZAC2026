#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
model_3b="${MODEL_3B_FILE:-$PWD/models/ollama/blobs/sha256-4a188102020e9c9530b687fd6400f775c45e90a0d7baafe65bd0a36963fbb7ba}"
model_4b="${MODEL_4B_FILE:-$PWD/models/ollama/blobs/sha256-85e4a5b7b8ef0e48af0e8658f5aaab9c2324c76c1641493f4d1e25fce54b18b9}"
model_7b="${MODEL_7B_FILE:-$PWD/models/ollama/blobs/sha256-60e05f2100071479f596b964f89f510f057ce397ea22f2833a0cfe029bfc2463}"
IFS=, read -r -a variants <<< "${COMPARE_MODELS:-3b,4b,7b}"
((${#variants[@]} >= 2)) || { echo 'Select at least two models with COMPARE_MODELS.' >&2; exit 1; }
for variant in "${variants[@]}"; do
  case "$variant" in
    3b) model_file="$model_3b" ;;
    4b) model_file="$model_4b" ;;
    7b) model_file="$model_7b" ;;
    *) echo "Unknown model candidate: $variant" >&2; exit 1 ;;
  esac
  [[ -f "$model_file" ]] || { echo "Model $variant must be provisioned before comparison." >&2; exit 1; }
done
mkdir -p runs
comparison_dir=$(mktemp -d "$PWD/runs/model-comparison-XXXXXXXX")
printf 'Comparison directory: %s\n' "$comparison_dir"
cache_k="${COMPARE_CACHE_TYPE_K:-q8_0}"
cache_v="${COMPARE_CACHE_TYPE_V:-q8_0}"
run_dirs=()
for variant in "${variants[@]}"; do
  case "$variant" in
    3b) model_file="$model_3b"; model_alias=local-coder-3b; layers="${GPU_LAYERS_3B:-all}" ;;
    4b) model_file="$model_4b"; model_alias=local-instruct-4b; layers="${GPU_LAYERS_4B:-all}" ;;
    7b) model_file="$model_7b"; model_alias=local-coder-7b; layers="${GPU_LAYERS_7B:-20}" ;;
  esac
  run_dir="$comparison_dir/model-$variant"
  run_dirs+=("$run_dir")
  env MODEL_FILE="$model_file" LOCAL_MODEL_ALIAS="$model_alias" LOCAL_GPU_LAYERS="$layers" \
    LOCAL_CACHE_TYPE_K="$cache_k" LOCAL_CACHE_TYPE_V="$cache_v" \
    ./scripts/bench-local.sh --output "$run_dir" --partition validation --sample balanced "$@"
done
.venv/bin/python scripts/compare-runs.py "${run_dirs[@]}"
