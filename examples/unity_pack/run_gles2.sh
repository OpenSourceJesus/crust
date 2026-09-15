#!/usr/bin/env bash
# Pack MiniScene and display it via surfaceless GLES2.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${OUT:-$ROOT/build/unity_gles2}"
SCENE="${SCENE:-$ROOT/examples/unity_pack/MiniScene}"
VIEW="$ROOT/examples/unity_pack/gles2_view.c"
CC="${CC:-gcc}"

mkdir -p "$OUT"
python3 "$ROOT/tools/unity_pack.py" "$SCENE" -o "$OUT"
"$CC" -O3 -c -o "$OUT/engine.o" "$OUT/engine.c"
"$CC" -O0 -c -o "$OUT/data.o" "$OUT/data.c"
"$CC" -O2 -o "$OUT/view" "$VIEW" "$OUT/engine.o" "$OUT/data.o" \
    -I "$OUT" -lEGL -lGLESv2 -lm

echo "== native surfaceless =="
EGL_PLATFORM=surfaceless LIBGL_ALWAYS_SOFTWARE="${LIBGL_ALWAYS_SOFTWARE:-}" \
    "$OUT/view" ${1:+"$1"}
