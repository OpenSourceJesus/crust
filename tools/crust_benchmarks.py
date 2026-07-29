#!/usr/bin/env python3
"""Dispatcher for Crust / ShivyCX / CrustOS benchmark suites.

The historical name ``tools/crust_benchmarks.py`` points here. Suites live under
``benchmarks/``; this entry point selects which harness to run.

    python3 tools/crust_benchmarks.py              # feature suite (default)
    python3 tools/crust_benchmarks.py features
    python3 tools/crust_benchmarks.py rpython
    python3 tools/crust_benchmarks.py minipy
    python3 tools/crust_benchmarks.py compile_speed
    python3 tools/crust_benchmarks.py all

Environment:
  CRUST_BENCH=features|rpython|minipy|compile_speed|all
"""
from __future__ import print_function

import os
import runpy
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(ROOT, "benchmarks")

SUITES = {
    "features": os.path.join(BENCH, "run_benchmarks.py"),
    "rpython": os.path.join(BENCH, "run_rpython_benchmarks.py"),
    "minipy": os.path.join(BENCH, "run_minipy_benchmarks.py"),
    "compile_speed": os.path.join(BENCH, "compile_speed", "bench_compile_speed.py"),
}


def _run(path):
    if not os.path.isfile(path):
        print("missing suite:", path, file=sys.stderr)
        return 1
    # Keep argv[0] as the suite script so relative paths inside it still work.
    sys.argv[0] = path
    runpy.run_path(path, run_name="__main__")
    return 0


def main(argv):
    name = (argv[0] if argv else os.environ.get("CRUST_BENCH", "features")).lower()
    if name in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if name == "all":
        rc = 0
        for key in ("features", "rpython", "minipy", "compile_speed"):
            print("\n######## crust_benchmarks: %s ########\n" % key)
            rc = _run(SUITES[key]) or rc
        return rc
    if name not in SUITES:
        print("unknown suite %r; choose: %s"
              % (name, ", ".join(sorted(SUITES) + ["all"])), file=sys.stderr)
        return 2
    # Drop the suite name so nested harnesses see their own argv.
    sys.argv = [SUITES[name]] + argv[1:]
    return _run(SUITES[name])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
