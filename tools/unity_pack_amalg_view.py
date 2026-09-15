#!/usr/bin/env python3
"""Build one C TU from packed engine/data + gles2_view for --target wasm.

Crust's wasm back end compiles a single translation unit. This stitches:

  engine_draw.h  (API)
  engine.c       (minus its duplicate EngineDraw typedef — header wins)
  data.c         (arrays only; structs already in engine.c)
  gles2_view.c   (with #include \"engine_draw.h\" removed)

    python3 tools/unity_pack_amalg_view.py /tmp/upack \\
        examples/unity_pack/gles2_view.c -o /tmp/upack/amalg.c
"""

from __future__ import annotations

import argparse
import os
import re
import sys


def strip_engine_draw_typedef(engine: str) -> str:
    """Drop the EngineDraw typedef block; keep engine_collect_draws."""
    return re.sub(
        r"/\* ---- draw list \(see engine_draw\.h\) ---- \*/\n"
        r"typedef struct EngineDraw \{.*?\n\} EngineDraw;\n\n",
        "/* ---- draw list (EngineDraw from engine_draw.h) ---- */\n",
        engine,
        count=1,
        flags=re.S,
    )


def data_arrays_only(data: str) -> str:
    """Keep Time_deltaTime and instance arrays; drop struct re-declarations."""
    m = re.search(r"^float Time_deltaTime", data, re.M)
    if not m:
        raise SystemExit("data.c: no Time_deltaTime")
    return (
        "/* data.c arrays only — structs live in engine.c */\n"
        + data[m.start():]
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("packdir", help="directory with engine.c / data.c / engine_draw.h")
    ap.add_argument("view", help="path to gles2_view.c")
    ap.add_argument("-o", required=True, help="output amalgam .c")
    args = ap.parse_args()

    pack = args.packdir
    header = open(os.path.join(pack, "engine_draw.h")).read()
    engine = strip_engine_draw_typedef(open(os.path.join(pack, "engine.c")).read())
    data = data_arrays_only(open(os.path.join(pack, "data.c")).read())
    view = open(args.view).read()
    view = view.replace('#include "engine_draw.h"',
                        "/* engine_draw.h already included above */")

    out = []
    out.append("/* amalgamated by tools/unity_pack_amalg_view.py — do not edit */\n")
    out.append(header)
    out.append("\n")
    out.append(engine)
    out.append("\n")
    out.append(data)
    out.append("\n")
    out.append(view)
    os.makedirs(os.path.dirname(os.path.abspath(args.o)) or ".", exist_ok=True)
    with open(args.o, "w") as f:
        f.write("".join(out))
    print("wrote", args.o, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
