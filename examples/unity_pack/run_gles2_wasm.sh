#!/usr/bin/env bash
# Pack MiniScene and display it under --target wasm + soft GLES host.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${OUT:-$ROOT/build/unity_gles2_wasm}"
SCENE="${SCENE:-$ROOT/examples/unity_pack/MiniScene}"
VIEW="$ROOT/examples/unity_pack/gles2_view.c"

mkdir -p "$OUT"
python3 "$ROOT/tools/unity_pack.py" "$SCENE" -o "$OUT"
python3 "$ROOT/tools/unity_pack_amalg_view.py" "$OUT" "$VIEW" -o "$OUT/amalg.c"
python3 -m shivyc.main --target wasm "$OUT/amalg.c" -o "$OUT/view.wasm"

echo "== wasm soft GLES =="
node "$ROOT/tools/gles2_wasm_run.js" "$OUT/view.wasm"
