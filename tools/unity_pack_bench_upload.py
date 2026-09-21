#!/usr/bin/env python3
"""Microbench: AoS gather vs SoA contiguous position upload.

Packs MiniScene twice (default AoS and --soa), links a tiny host that
calls engine_upload_positions many times, and prints ns/call.

    python3 tools/unity_pack_bench_upload.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = os.path.join(ROOT, "examples", "unity_pack", "MiniScene")
_CC = shutil.which("gcc") or shutil.which("cc")

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


def build_and_run(soa: bool) -> str:
    if not _CC:
        raise SystemExit("need gcc/cc")
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
        [_CC, "-O3", "-c", "-o", os.path.join(d, "engine.o"),
         os.path.join(d, "engine.c")])
    subprocess.check_call(
        [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
         os.path.join(d, "data.c")])
    exe = os.path.join(d, "bench")
    subprocess.check_call(
        [_CC, "-O3", "-o", exe, host,
         os.path.join(d, "engine.o"), os.path.join(d, "data.o"),
         "-I", d, "-lm"])
    out = subprocess.check_output([exe], text=True)
    return out.strip()


def main() -> int:
    print("AoS ", build_and_run(False))
    print("SoA ", build_and_run(True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
