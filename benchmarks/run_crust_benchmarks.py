#!/usr/bin/env python3
"""Run the Crust (C+Rust) language benchmarks against gcc.

Each benchmark in `examples/crust/bench` is written in the Rust subset and
exercises one part of the lowering -- static trait dispatch, monomorphised
generics, tagged-union enums, array and slice traffic. That separation is the
point: a regression in, say, tag dispatch shows up as one number rather than
being averaged into a single score.

The comparison is done on the *same C*. Crust translates the `.rs` to C, and
that C is then compiled by ShivyCX and by gcc at three optimisation levels, so
the difference measured is code generation and nothing else -- not the Rust
front end, which both sides share.

Writes `benchmarks/results/crust_results.json`; `plot_crust.py` renders it.
"""

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BENCH_DIR = os.path.join(ROOT, "examples", "crust", "bench")
RESULTS_DIR = os.path.join(HERE, "results")
WORK = "/tmp/crust_bench"

# Best of N, to take the minimum rather than an average: the minimum is the
# run least disturbed by whatever else the machine was doing.
REPEATS = 3

DESCRIPTIONS = {
    "traits": "Static trait dispatch: three impls of one trait, resolved at "
              "monomorphisation. Measures whether a trait call really costs a "
              "direct call.",
    "generics": "A monomorphised `Ring<T>` instantiated at two element types. "
                "Measures whether an instantiation costs what a hand-written "
                "version would.",
    "enums": "Data-carrying enums dispatched by `match` -- the tagged-union "
             "lowering. Measures the tag switch and the payload reads.",
    "memory": "Array and slice traffic: bounds-free indexing, slice "
              "iteration, a struct-of-arrays walk. Measures address "
              "generation more than arithmetic.",
    "bench_rmm": "Redox's own shapes: `PageFlags<A>` generic over an "
                 "architecture trait with associated consts, a bitmap frame "
                 "allocator, a page-table walk.",
}


def discover():
    if not os.path.isdir(BENCH_DIR):
        return []
    return sorted(f for f in os.listdir(BENCH_DIR) if f.endswith(".rs"))


def translate(src, out_c):
    """Lower a Crust source to C, so both compilers see the same input."""
    sys.path.insert(0, ROOT)
    import shivyc.crust as crust
    with open(src) as f:
        text = f.read()
    with open(out_c, "w") as f:
        f.write(crust.translate(text, path=src))
    return out_c


def time_binary(path):
    best = None
    for _ in range(REPEATS):
        t0 = time.perf_counter()
        subprocess.run([path], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    return best


def build_shivyc(cpath, out):
    p = subprocess.run([sys.executable, "-m", "shivyc.main", cpath, "-o", out],
                       cwd=ROOT, capture_output=True)
    return out if p.returncode == 0 and os.path.exists(out) else None


def build_gcc(cpath, out, level):
    p = subprocess.run(["gcc", level, "-x", "c", cpath, "-o", out],
                       capture_output=True)
    return out if p.returncode == 0 and os.path.exists(out) else None


def run_one(name):
    src = os.path.join(BENCH_DIR, name + ".rs")
    cpath = os.path.join(WORK, name + ".c")
    translate(src, cpath)

    rec = {"benchmark": name,
           "description": DESCRIPTIONS.get(name, ""),
           "configs": []}
    targets = [("ShivyCX", lambda o: build_shivyc(cpath, o))]
    for lvl in ("-O0", "-O2", "-O3"):
        targets.append(("gcc " + lvl,
                        (lambda l: lambda o: build_gcc(cpath, o, l))(lvl)))

    for label, builder in targets:
        out = os.path.join(WORK, "%s_%s" % (name, label.replace(" ", "")))
        binary = builder(out)
        if binary is None:
            rec["configs"].append({"name": label, "ok": False})
            continue
        rec["configs"].append({
            "name": label,
            "ok": True,
            "time_s": time_binary(binary),
            "size_bytes": os.path.getsize(binary),
        })
    return rec


def main():
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    names = [f[:-3] for f in discover()]
    if not names:
        print("no benchmarks found in %s" % BENCH_DIR)
        return 1

    results = []
    for name in names:
        print("Running crust benchmark: %s" % name)
        try:
            rec = run_one(name)
        except Exception as e:
            # One benchmark failing costs that benchmark, not the suite.
            print("  SKIP %s: %s: %s" % (name, type(e).__name__, e))
            continue
        results.append(rec)
        base = None
        for c in rec["configs"]:
            if not c.get("ok"):
                print("  %-10s build failed" % c["name"])
                continue
            if c["name"] == "ShivyCX":
                base = c["time_s"]
            rel = ""
            if base and c["time_s"]:
                rel = "  (%.1fx ShivyCX)" % (c["time_s"] / base)
            print("  %-10s %7.3fs%s" % (c["name"], c["time_s"], rel))

    out = os.path.join(RESULTS_DIR, "crust_results.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=1)
    print("\nWrote %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
