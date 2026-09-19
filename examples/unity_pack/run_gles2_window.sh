#!/usr/bin/env bash
# Pack a Unity project and run the GLFW / OpenGL ES player that unity_pack builds.
#
#     ./examples/unity_pack/run_gles2_window.sh
#     ./examples/unity_pack/run_gles2_window.sh --soa
#     SCENE=/path/to/project ./examples/unity_pack/run_gles2_window.sh
#
# Default output: $TMPDIR/<project folder>/<productName>
# Override the directory with OUT=... (binary name stays productName).
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCENE="${SCENE:-$ROOT/examples/unity_pack/MiniScene}"
SOA=()

for arg in "$@"; do
  case "$arg" in
    --soa) SOA=(--soa) ;;
    --soa-vec4) SOA=(--soa-vec4) ;;
    -h|--help)
      echo "usage: $0 [--soa | --soa-vec4]"
      echo "  SCENE=...  Unity project (default: MiniScene)"
      echo "  OUT=...    pack directory (default: \$TMPDIR/<project folder>)"
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

SCENE="$(cd "$SCENE" && pwd)"
# Resolve out dir + product binary the same way unity_pack.py does.
eval "$(
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 - <<PY
import os, sys
sys.path.insert(0, "$ROOT")
import tools.unity_pack as up
root = "$SCENE"
outdir = os.environ.get("OUT") or up.default_pack_dir(root)
_c, product = up.player_identity(root)
exe = up.exe_filename(product)
print("OUT=%s" % repr(outdir))
print("EXE=%s" % repr(os.path.join(outdir, exe)))
PY
)"

echo "== packing $SCENE → $OUT =="
PYTHONUNBUFFERED=1 PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -u "$ROOT/tools/unity_pack.py" "$SCENE" -o "$OUT" "${SOA[@]}"

if [[ ! -x "$EXE" ]]; then
  echo "unity_pack did not produce executable: $EXE" >&2
  exit 1
fi

echo "== GLFW window${SOA[*]:+ (SoA)}: $EXE (Escape/Q to quit) =="
exec "$EXE"
