#!/usr/bin/env bash
# Pack MiniScene and show it in a real GLFW / OpenGL ES window.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${OUT:-$ROOT/build/unity_gles2_window}"
SCENE="${SCENE:-$ROOT/examples/unity_pack/MiniScene}"
VIEW="$ROOT/examples/unity_pack/gles2_window.c"
CC="${CC:-gcc}"

if ! pkg-config --exists glfw3; then
  echo "need glfw3 (pkg-config glfw3)" >&2
  exit 1
fi

mkdir -p "$OUT"
python3 "$ROOT/tools/unity_pack.py" "$SCENE" -o "$OUT"
"$CC" -O3 -c -o "$OUT/engine.o" "$OUT/engine.c"
"$CC" -O0 -c -o "$OUT/data.o" "$OUT/data.c"
"$CC" -O2 -o "$OUT/window" "$VIEW" "$OUT/engine.o" "$OUT/data.o" \
    -I "$OUT" $(pkg-config --cflags --libs glfw3) -lGLESv2 -lm

echo "== GLFW window (Escape/Q to quit) =="
exec "$OUT/window"
