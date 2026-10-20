#!/usr/bin/env bash
# Build a Unity project with CCSharp (tools/unity/unity_pack.py) and run it in the GLFW / OpenGL ES window host.
#
#     ./examples/unity_pack/run_gles2_window.sh
#     PROJECT=/path/to/project ./examples/unity_pack/run_gles2_window.sh
#     UNITY_PACK_GLES2=1 ./examples/unity_pack/run_gles2_window.sh    # ES 2.0 host instead of ES 3.1
#
# Default output: $TMPDIR/<project folder>/player; override the directory with OUT=...
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PROJECT="${PROJECT:-$ROOT/examples/unity_pack/MiniScene}"

case "${1:-}" in
  "") ;;
  -h|--help)
    echo "usage: $0"
    echo "  PROJECT=...  Unity project (default: MiniScene)"
    echo "  OUT=...      build directory (default: \$TMPDIR/<project folder>)"
    exit 0
    ;;
  *)
    echo "unknown option: $1" >&2
    exit 2
    ;;
esac

if ! pkg-config --exists glfw3; then
  echo "need glfw3 (pkg-config glfw3)" >&2
  exit 1
fi

PROJECT="$(cd "$PROJECT" && pwd)"
OUT="${OUT:-${TMPDIR:-/tmp}/$(basename "$PROJECT")}"
HOST="$ROOT/examples/unity_pack/gles3_window.c"
[[ -n "${UNITY_PACK_GLES2:-}" ]] && HOST="$ROOT/examples/unity_pack/gles2_window.c"

echo "== building $PROJECT → $OUT =="
PYTHONUNBUFFERED=1 PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -u "$ROOT/tools/unity/unity_pack.py" "$PROJECT" -o "$OUT"

# Box2D-Packed, when the project has physics: unity_pack_ccs writes the link arguments, relative to $OUT
PHYS=()
[[ -f "$OUT/physics/link.txt" ]] && read -ra PHYS < "$OUT/physics/link.txt"
# DotNetAnywhere, when scripts (or the engine) run managed: the headers and runtime library; game.managed.dll and corlib.dll sit beside the player
DNA=()
[[ -f "$OUT/dna/link.txt" ]] && read -ra DNA < "$OUT/dna/link.txt"
cd "$OUT"
# shellcheck disable=SC2046
"${CC:-gcc}" -O2 -w -o player "$HOST" engine.c -I . "${PHYS[@]}" "${DNA[@]}" $(pkg-config --cflags --libs glfw3) -lGLESv2 -lm

echo "== GLFW window: $OUT/player (Application.Quit or window close) =="
exec ./player
