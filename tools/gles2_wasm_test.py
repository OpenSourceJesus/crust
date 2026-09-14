#!/usr/bin/env python3
"""Compile examples/gles2/triangle.c for wasm and run it under the soft host.

Checks that the module validates, exits 0, and prints a non-blank RGB
triangle (R, G and B cells present). Pixel-identical match to Mesa is not
required: the host is a minimal software rasteriser, not the system driver.

    python3 tools/gles2_wasm_test.py
"""

import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXAMPLE = os.path.join(ROOT, "examples", "gles2", "triangle.c")
RUNNER = os.path.join(HERE, "gles2_wasm_run.js")


def main():
    if not os.path.isfile(EXAMPLE):
        print("FAIL missing", EXAMPLE)
        return 1
    if not os.path.isfile(RUNNER):
        print("FAIL missing", RUNNER)
        return 1

    with tempfile.TemporaryDirectory(prefix="gles2_wasm_") as tmp:
        wasm = os.path.join(tmp, "triangle.wasm")
        r = subprocess.run(
            [sys.executable, "-m", "shivyc.main", "--target", "wasm",
             EXAMPLE, "-o", wasm],
            cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0:
            print("FAIL compile\n", r.stdout, r.stderr)
            return 1

        r = subprocess.run(
            ["node", "-e",
             'WebAssembly.compile(require("fs").readFileSync(process.argv[1]))'
             '.then(()=>console.log("ok"))'
             '.catch(e=>{console.error(""+e); process.exit(1)})',
             wasm],
            capture_output=True, text=True)
        if r.returncode != 0:
            print("FAIL validate\n", r.stdout, r.stderr)
            return 1

        r = subprocess.run(["node", RUNNER, wasm],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("FAIL run rc=%d\n%s\n%s" % (r.returncode, r.stdout, r.stderr))
            return 1

        out = r.stdout
        if "EGL " not in out:
            print("FAIL missing EGL banner\n", out)
            return 1
        # ASCII art: at least one R, G, B cell from the Gouraud corners.
        art = "\n".join(line for line in out.splitlines()
                        if re.fullmatch(r"[.RGB]+", line or ""))
        if not art:
            print("FAIL no ASCII art\n", out)
            return 1
        for ch in "RGB":
            if ch not in art:
                print("FAIL ASCII missing %r\n%s" % (ch, art))
                return 1
        # Refuse a blank frame: every cell '.' proves nothing.
        if set(art) <= {".", "\n"}:
            print("FAIL blank frame\n", art)
            return 1

    print("ok  gles2 wasm triangle (%d ASCII lines)" % art.count("\n"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
