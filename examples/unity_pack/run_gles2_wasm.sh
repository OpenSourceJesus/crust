#!/usr/bin/env bash
# Pack MiniScene and display it under --target wasm + soft GLES host.
#
#     ./examples/unity_pack/run_gles2_wasm.sh
#     ./examples/unity_pack/run_gles2_wasm.sh --soa
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${OUT:-$ROOT/build/unity_gles2_wasm}"
PROJECT="${PROJECT:-$ROOT/examples/unity_pack/MiniScene}"
VIEW="$ROOT/examples/unity_pack/gles2_view.c"
SOA=()

for arg in "$@"; do
  case "$arg" in
    --soa) SOA=(--soa); OUT="${OUT}_soa" ;;
    --soa-vec4) SOA=(--soa-vec4); OUT="${OUT}_soa_vec4" ;;
    -h|--help)
      echo "usage: $0 [--soa | --soa-vec4]"
      exit 0
      ;;
    *)
      echo "unknown option: $arg (try --soa or --soa-vec4)" >&2
      exit 2
      ;;
  esac
done

mkdir -p "$OUT"
echo "== packing $PROJECT → $OUT =="
PYTHONUNBUFFERED=1 python3 -u "$ROOT/tools/unity_pack.py" "$PROJECT" -o "$OUT" "${SOA[@]}"
echo "== amalgamating view =="
python3 "$ROOT/tools/unity_pack_amalg_view.py" "$OUT" "$VIEW" -o "$OUT/amalg.c"
echo "== compiling wasm =="
python3 -m shivyc.main --target wasm "$OUT/amalg.c" -o "$OUT/view.wasm"

echo "== wasm soft GLES${SOA[*]:+ (SoA)} =="
node "$ROOT/tools/gles2_wasm_run.js" "$OUT/view.wasm"
