#!/usr/bin/env bash
# Pack MiniScene and show it in a real GLFW / OpenGL ES window.
#
#     ./examples/unity_pack/run_gles2_window.sh
#     ./examples/unity_pack/run_gles2_window.sh --soa
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
    -h|--help)
      echo "usage: $0 [--soa]"
      exit 0
      ;;
    *)
      echo "unknown option: $arg (try --soa)" >&2
      exit 2
      ;;
  esac
done

if ! pkg-config --exists glfw3; then
  echo "need glfw3 (pkg-config glfw3)" >&2
  exit 1
fi

mkdir -p "$OUT"
python3 "$ROOT/tools/unity_pack.py" "$SCENE" -o "$OUT" "${SOA[@]}"
"$CC" -O3 -c -o "$OUT/engine.o" "$OUT/engine.c"
"$CC" -O0 -c -o "$OUT/data.o" "$OUT/data.c"
"$CC" -O2 -o "$OUT/window" "$VIEW" "$OUT/engine.o" "$OUT/data.o" \
    -I "$OUT" $(pkg-config --cflags --libs glfw3) -lGLESv2 -lm

echo "== GLFW window${SOA[*]:+ (SoA)} (Escape/Q to quit) =="
exec "$OUT/window"
