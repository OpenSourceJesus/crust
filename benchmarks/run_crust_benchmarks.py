#!/usr/bin/env python3
"""Run the Crust (C+Rust) language benchmarks against gcc.

Each benchmark in `examples/crust/bench` exercises one part of the lowering --
static trait dispatch, monomorphised generics, tagged-union enums, array and
slice traffic on the Rust side; method calls, virtual dispatch and scope-exit
destruction on the C++ side. That separation is the point: a regression in,
say, tag dispatch shows up as one number rather than being averaged into a
single score.

Both front ends are covered. A `.rs` benchmark is lowered by `shivyc.crust`
and a `.cpp` one by `tools.cpprust`; from there they are the same thing, since
both front ends emit plain C over struct pointers. `cpp_methods` and
`cpp_dispatch` are a matched pair -- identical arithmetic and iteration count,
one reached directly and one through a vtable -- so the gap between them is
what dynamic dispatch costs.

The comparison is done on the *same C*. The front end translates the source to
C, and that C is then compiled by ShivyCX and by gcc at three optimisation
levels, so the difference measured is code generation and nothing else -- not
the front end, which both sides share.

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
    "cpp_methods": "C++ non-virtual method calls and member access. Measures "
                   "whether a method really costs a direct call -- the "
                   "baseline half of the pair with cpp_dispatch.",
    "cpp_dispatch": "C++ virtual dispatch through a base pointer, over two "
                    "derived types so the target cannot be devirtualised. "
                    "Measures the vtable indirection plus the thunk that "
                    "keeps the generated table free of function-pointer "
                    "casts.",
    "cpp_raii": "C++ construction and destruction at scope exit, including "
                "the `continue`, `break` and early-`return` paths. Measures "
                "whether automatic Drop costs what hand-written calls would.",
}

# Which front end lowers which source.
FRONT_ENDS = {".rs": "crust", ".cpp": "cpprust"}


def discover():
    """Benchmark sources, as (name, extension) pairs."""
    if not os.path.isdir(BENCH_DIR):
        return []
    found = []
    for f in sorted(os.listdir(BENCH_DIR)):
        stem, ext = os.path.splitext(f)
        if ext in FRONT_ENDS:
            found.append((stem, ext))
    return found


def translate(src, ext, out_c):
    """Lower a benchmark source to C, so both compilers see the same input."""
    sys.path.insert(0, ROOT)
    with open(src) as f:
        text = f.read()
    if FRONT_ENDS[ext] == "cpprust":
        import tools.cpprust as front
    else:
        import shivyc.crust as front
    with open(out_c, "w") as f:
        f.write(front.translate(text, path=src))
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


def run_one(name, ext):
    src = os.path.join(BENCH_DIR, name + ext)
    cpath = os.path.join(WORK, name + ".c")
    translate(src, ext, cpath)

    rec = {"benchmark": name,
           "language": "C++" if ext == ".cpp" else "Rust",
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
    names = discover()
    if not names:
        print("no benchmarks found in %s" % BENCH_DIR)
        return 1

    results = []
    for name, ext in names:
        print("Running crust benchmark: %s (%s)"
              % (name, "C++" if ext == ".cpp" else "Rust"))
        try:
            rec = run_one(name, ext)
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
