#!/usr/bin/env python3
"""Microbench: AoS gather vs SoA contiguous position upload.

Packs MiniScene twice (default AoS and --soa), links a tiny host that
calls engine_upload_positions many times, and prints ns/call.

Runs under gcc and clang when both are installed (label cc=...).

    python3 tools/unity_pack_bench_upload.py
    python3 tools/unity_pack_bench_upload.py --cc clang
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = os.path.join(ROOT, "examples", "unity_pack", "MiniScene")

HOST = r"""
#include "engine_draw.h"
#include <stdio.h>
#include <stdlib.h>

#ifndef NITER
#define NITER 200000
#endif

int main(void) {
    int n = engine_position_floats();
    float *buf = (float *)malloc(sizeof(float) * (size_t)(n + 8));
    int i, got;
    if (!buf) return 2;
    /* warm */
    for (i = 0; i < 1000; i++)
        engine_upload_positions(buf, n);
    clock_t t0 = clock();
    for (i = 0; i < NITER; i++)
        got = engine_upload_positions(buf, n);
    clock_t t1 = clock();
    double secs = (double)(t1 - t0) / (double)CLOCKS_PER_SEC;
    printf("floats=%d iters=%d sec=%.6f ns_per=%.2f got=%d\n",
           n, NITER, secs, secs * 1e9 / (double)NITER, got);
    free(buf);
    return got == n ? 0 : 1;
}
"""


def _host_ccs(restrict: str | None = None):
    """[(name, path), ...] — gcc and clang when present; else cc."""
    out = []
    for name in ("gcc", "clang"):
        if restrict and name != restrict:
            continue
        path = shutil.which(name)
        if path:
            out.append((name, path))
    if not out and not restrict:
        cc = shutil.which("cc")
        if cc:
            out.append(("cc", cc))
    return out


def build_and_run(soa: bool, cc_name: str, cc_path: str) -> str:
    d = tempfile.mkdtemp(prefix="upack-bench-")
    cmd = [sys.executable, os.path.join(ROOT, "tools", "unity_pack.py"),
           PROJECT, "-o", d]
    if soa:
        cmd.append("--soa")
    subprocess.check_call(cmd)
    host = os.path.join(d, "host.c")
    with open(host, "w") as f:
        f.write("#include <time.h>\n")
        f.write(HOST)
    subprocess.check_call(
        [cc_path, "-O3", "-fno-math-errno", "-c", "-o",
         os.path.join(d, "engine.o"),
         os.path.join(d, "engine.c")])
    subprocess.check_call(
        [cc_path, "-O0", "-c", "-o", os.path.join(d, "data.o"),
         os.path.join(d, "data.c")])
    exe = os.path.join(d, "bench")
    subprocess.check_call(
        [cc_path, "-O3", "-fno-math-errno", "-o", exe, host,
         os.path.join(d, "engine.o"), os.path.join(d, "data.o"),
         "-I", d, "-lm"])
    out = subprocess.check_output([exe], text=True).strip()
    return "cc=%s %s" % (cc_name, out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cc", choices=("gcc", "clang"), default=None,
                    help="restrict to one compiler (default: all found)")
    args = ap.parse_args()
    ccs = _host_ccs(args.cc)
    if not ccs:
        if args.cc:
            raise SystemExit("need %s on PATH" % args.cc)
        raise SystemExit("need gcc, clang, or cc")
    for name, path in ccs:
        print("AoS ", build_and_run(False, name, path))
        print("SoA ", build_and_run(True, name, path))
    if not shutil.which("clang") and args.cc is None:
        print("clang skipped: not on PATH", file=sys.stderr)
    if not shutil.which("gcc") and args.cc is None and shutil.which("clang"):
        print("gcc skipped: not on PATH", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
