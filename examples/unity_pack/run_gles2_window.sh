#!/usr/bin/env bash
# Pack MiniScene and show it in a real GLFW / OpenGL ES window.
#
#     ./examples/unity_pack/run_gles2_window.sh
#     ./examples/unity_pack/run_gles2_window.sh --soa
#     SCENE=/path/to/project ./examples/unity_pack/run_gles2_window.sh
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${OUT:-$ROOT/build/unity_gles2_window}"
SCENE="${SCENE:-$ROOT/examples/unity_pack/MiniScene}"
VIEW="$ROOT/examples/unity_pack/gles2_window.c"
CC="${CC:-gcc}"
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

if ! pkg-config --exists glfw3; then
  echo "need glfw3 (pkg-config glfw3)" >&2
  exit 1
fi

mkdir -p "$OUT"
echo "== packing $SCENE → $OUT =="
PYTHONUNBUFFERED=1 python3 -u "$ROOT/tools/unity_pack.py" "$SCENE" -o "$OUT" "${SOA[@]}"
echo "== compiling engine.c (-O3) ($(wc -c < "$OUT/engine.c") bytes) =="
"$CC" -O3 -c -o "$OUT/engine.o" "$OUT/engine.c"
echo "== compiling data.c (-O0) ($(wc -c < "$OUT/data.c") bytes) =="
"$CC" -O0 -c -o "$OUT/data.o" "$OUT/data.c"
echo "== linking window =="
"$CC" -O2 -o "$OUT/window" "$VIEW" "$OUT/engine.o" "$OUT/data.o" \
    -I "$OUT" $(pkg-config --cflags --libs glfw3) -lGLESv2 -lm

echo "== GLFW window${SOA[*]:+ (SoA)} (Escape/Q to quit) =="
exec "$OUT/window"
