#!/usr/bin/env bash
# Pack MiniScene and display it via surfaceless GLES2.
#
#     ./examples/unity_pack/run_gles2.sh
#     ./examples/unity_pack/run_gles2.sh --soa
#     ./examples/unity_pack/run_gles2.sh out.ppm
#     ./examples/unity_pack/run_gles2.sh --soa out.ppm
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${OUT:-$ROOT/build/unity_gles2}"
SCENE="${SCENE:-$ROOT/examples/unity_pack/MiniScene}"
VIEW="$ROOT/examples/unity_pack/gles2_view.c"
CC="${CC:-gcc}"
SOA=()
PPM=""

for arg in "$@"; do
  case "$arg" in
    --soa) SOA=(--soa); OUT="${OUT}_soa" ;;
    --soa-vec4) SOA=(--soa-vec4); OUT="${OUT}_soa_vec4" ;;
    -h|--help)
      echo "usage: $0 [--soa | --soa-vec4] [out.ppm]"
      exit 0
      ;;
    -*)
      echo "unknown option: $arg" >&2
      exit 2
      ;;
    *)
      PPM="$arg"
      ;;
  esac
done

mkdir -p "$OUT"
echo "== packing $SCENE → $OUT =="
PYTHONUNBUFFERED=1 python3 -u "$ROOT/tools/unity_pack.py" "$SCENE" -o "$OUT" "${SOA[@]}"
echo "== compiling engine.c (-O3) ($(wc -c < "$OUT/engine.c") bytes) =="
"$CC" -O3 -c -o "$OUT/engine.o" "$OUT/engine.c"
echo "== compiling data.c (-O0) ($(wc -c < "$OUT/data.c") bytes) =="
"$CC" -O0 -c -o "$OUT/data.o" "$OUT/data.c"
echo "== linking view =="
"$CC" -O2 -o "$OUT/view" "$VIEW" "$OUT/engine.o" "$OUT/data.o" \
    -I "$OUT" -lEGL -lGLESv2 -lm

echo "== native surfaceless${SOA[*]:+ (SoA)} =="
EGL_PLATFORM=surfaceless LIBGL_ALWAYS_SOFTWARE="${LIBGL_ALWAYS_SOFTWARE:-}" \
    "$OUT/view" ${PPM:+"$PPM"}
