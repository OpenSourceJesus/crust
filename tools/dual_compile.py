#!/usr/bin/env python3
"""Compile one Crust translation unit to a native binary and a wasm module.

    python3 tools/dual_compile.py examples/wireproto/codec.c \\
        -I examples/wireproto -o /tmp/wireproto/codec

Writes:
    /tmp/wireproto/codec       -- native executable
    /tmp/wireproto/codec.wasm  -- wasm module (same IL, --target wasm)

Issue #15: compiling the backend and the frontend from the same sources is
what makes a POD struct layout a shared binary protocol. This driver is the
one-shot form of that; it does not invent a second ABI.
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _compile(sources, includes, out_path, target=None):
    cmd = [sys.executable, "-m", "shivyc.main"]
    if target:
        cmd.extend(["--target", target])
    for inc in includes:
        cmd.extend(["-I", inc])
    cmd.extend(sources)
    cmd.extend(["-o", out_path])
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stdout)
        sys.stderr.write(p.stderr)
        raise SystemExit(
            "dual_compile: %s failed (%d)"
            % (target or "native", p.returncode))
    return out_path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sources", nargs="+", help="C/Crust sources (one TU)")
    ap.add_argument("-I", dest="includes", action="append", default=[],
                    help="include directory (repeatable)")
    ap.add_argument("-o", "--output", required=True,
                    help="native output path; .wasm is written beside it")
    args = ap.parse_args(argv)

    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    native = os.path.abspath(args.output)
    wasm = native + ".wasm"
    # If the user passed foo.wasm as -o by mistake, still land next to it.
    if native.endswith(".wasm"):
        wasm = native
        native = native[:-5]

    sources = [os.path.abspath(s) for s in args.sources]
    includes = [os.path.abspath(i) for i in args.includes]

    # Wasm back end emits one finished module per TU and has no linker here.
    if len(sources) != 1:
        raise SystemExit(
            "dual_compile: pass exactly one translation unit "
            "(wasm cannot link multiple .c files)")

    print("native ->", _compile(sources, includes, native))
    print("wasm   ->", _compile(sources, includes, wasm, target="wasm"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
