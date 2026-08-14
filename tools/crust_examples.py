#!/usr/bin/env python3
"""Compile and run every examples/crust program, checking what it produces.

The Crust examples fall into two families. The `.c` ones print to stdout and
exit 0; the all-Rust ones carry their result in the exit status, so a subset
of the language can be exercised without depending on printf at all. This
runner checks whichever the example uses -- and both where both are
meaningful -- so a silent miscompile that still exits 0 is caught.

    python3 tools/crust_examples.py            # every example
    python3 tools/crust_examples.py --fast     # the quick subset
    python3 tools/crust_examples.py --keep     # leave binaries in place

Used by `make test_crust` and `make test_fast_crust`.
"""

import argparse
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, "examples", "crust")

# (source, expected exit status, expected stdout or None to ignore, fast?)
#
# `fast` marks the examples that make test_fast_crust runs: enough to cover
# each front end (C, Rust, rpython, C++) and the include paths between them,
# without recompiling the whole directory.
EXAMPLES = [
    ("mixed.c", 0,
     "gcd(1071, 462) = 21\n"
     "classify(-7)   = -1\n"
     "sum_to(100)    = 5050\n"
     "dot(u, v)      = 32\n", True),

    # C++ destructor freeing a Crust Vec — automatic Drop at block exit.
    ("raii.c", 42,
     "sum      = 30\n"
     "len      = 5\n"
     "released = 1\n", True),

    # A C++ class *owning* Crust values by value rather than borrowing a
    # pointer: the prelude sits above the include (with a `#line` resync), so
    # `Vec_int` and `Res` are complete in the class body, and Crust hands over
    # which types own something so the implicit destructor frees them. Also
    # covers transitive Rust field glue and an owning value moved across a
    # call boundary.
    ("ownmember.c", 42,
     "  Res_drop tag=7\n"
     "total    = 12\n"
     "count    = 3\n"
     "built    = 4\n"
     "moved    = 6\n", True),

    # C++11 surface syntax -- `auto`, range-`for`, namespaces, and the
    # smart pointers -- all resolved before the lowering runs, so what
    # reaches ShivyCX is the same plain C every other example produces.
    ("cpp11.c", 42,
     "sum      = 60\n"
     "owned    = 421\n"
     "released = 2\n"
     "combined = 481\n", True),

    # C++ single inheritance and virtual dispatch, with Rust reducing the
    # results — base-as-first-member, so upcasting is a pointer cast.
    ("dispatch.c", 42,
     "area         = 9 24\n"
     "via base     = 9 24\n"
     "describe     = 10 25\n"
     "scaled(10)   = 90 240\n"
     "inherited    = 1\n"
     "total        = 34\n", True),

    ("shapes.c", 0,
     "centroid  = (2, 3)\n"
     "len2      = 13\n"
     "dot(p1,p2)= 0\n", True),

    ("stats.c", 0,
     "sample: n=6 total=30 max=9 mean=5\n", False),

    ("polyglot.c", 0,
     "buckets   = 4\n"
     "spread    = 100\n"
     "bucket[1] = 2\n"
     "  n=12  bar= 9\n"
     "  n=30  bar=24\n"
     "  n= 6  bar= 4\n"
     "  n= 2  bar= 1\n", True),

    # Three languages building one thing: Rust geometry (mingine.rs), rpython
    # level rules (mingine.py) and C framebuffer code (mingine.c), all in a
    # single translation unit. The scene itself goes to stderr; stdout carries
    # the deterministic summary, so this is a golden test as well as a demo.
    ("baremetalgames/helloworld.c", 0,
     "ball  = 56,44  foe = 34,37\n"
     "score = 594\n"
     "pixels= 2347ee01\n", True),

    ("tally.c", 0,
     "labels = b0,b1,b2,b3\n"
     "widest = 3\n"
     "len2   = 25\n", True),

    ("twoway.c", 0,
     "primes    = 10\n"
     "sum       = 129\n"
     "dropped   = 1\n"
     "odd count = 9\n"
     "odd sum   = 127\n", True),

    ("small_os.c", 0,
     "schemes    = 4\n"
     "routed     = 4 of 5 accepted\n"
     "path depth = 8\n"
     "listing    = tcp -> /80 (depth 1)\n"
     "frames     = 5 claimed, 251 free\n"
     "runnable   = 2 of 3\n"
     "switches   = 6\n"
     "ticks(a,c) = 12 3\n"
     "read(0,7)  = 7\n"
     "close(0)   = 0\n"
     "badfd      = -1\n", True),

    ("mini_os.c", 0,
     "levels    = 3\n"
     "admitted  = 6\n"
     "switches  = 6\n"
     "demand    = 60\n"
     "task0     = pid 1 prio 1 ticks 20\n", True),

    ("derive.rs", 0,
     "clone    = 42\n"
     "eq(a, b) = 1\n"
     "default  = 0 0\n"
     "debug    = Point { x: 40, y: 2 }\n"
     "override = 40\n"
     "zeroed   = 0\n", True),

    ("enums.rs", 0,
     "quit   = 0\n"
     "move   = 307\n"
     "write  = 42\n"
     "color  = 42\n"
     "just_x = 5\n"
     "leaf   = 42\n"
     "empty  = 7\n", True),

    ("tail.rs", 0,
     "divmod   = (9, 2)\n"
     "stats    = (1, 9, 5.33333)\n"
     "boxed    = 42\n"
     "keywords = 42\n"
     "closures = 42 42 7\n", True),

    ("macros.rs", 0,
     "n=6 f=1.5 s=crust\n"
     "hex=6 braces={} percent=100%\n"
     "square(6)    = 36\n"
     "pick()       = 0\n"
     "pick(7)      = 7\n"
     "pick(3, 9)   = 9\n"
     "twice(add)   = 42\n"
     "cfg!         = false\n", True),

    ("traits.rs", 0,
     "disk: bytes=1048576 kib=1024\n"
     "ram: bytes=1048576 kib=1024\n"
     "disk summary = 1025\n"
     "ram  summary = 1026\n"
     "report(disk) = 1049601\n"
     "report(ram)  = 1049602\n", True),

    ("kernel.rs", 0,
     "tasks     = 3\n"
     "switches  = 12\n"
     "task0 pid = 1 (ready)\n"
     "weight(2) = 2\n"
     "boxed     = 42\n", True),

    ("generic.rs", 0,
     "Pair<i32>.sum     = 42\n"
     "Pair<i32>.largest = 40\n"
     "Pair<f64>.sum     = 3.75\n"
     "max(3, 9)         = 9\n"
     "max(2.5, 1.5)     = 2.5\n"
     "id::<i32>(7)      = 7\n", True),

    ("iter.rs", 0,
     "sum         = 80\n"
     "max         = 23C\n"
     "spread      = 12\n"
     "running_max = 23\n", True),

    # All-Rust examples that report through their exit status.
    ("fib.rs", 159, None, False),
    ("lookup.rs", 62, None, False),
    ("parse.rs", 42, None, True),
    ("tokens.rs", 242, None, False),
]


def run_one(src, want_rc, want_out, out_dir, verbose):
    """Compile and run one example; return (ok, message)."""
    path = os.path.join(EX, src)
    if not os.path.exists(path):
        return False, "missing source %s" % path
    binary = os.path.join(out_dir, os.path.basename(src) + ".bin")

    build = subprocess.run(
        [sys.executable, "-m", "shivyc.main", path, "-o", binary],
        cwd=ROOT, capture_output=True, text=True)
    if build.returncode != 0 or not os.path.exists(binary):
        detail = (build.stderr or build.stdout or "").strip().splitlines()
        return False, "compile failed: %s" % (detail[-1] if detail else "?")

    try:
        run = subprocess.run([binary], capture_output=True, text=True,
                             timeout=30)
    except subprocess.TimeoutExpired:
        return False, "timed out"

    if run.returncode != want_rc:
        return False, "exit %d, expected %d" % (run.returncode, want_rc)
    if want_out is not None and run.stdout != want_out:
        if verbose:
            return False, "output mismatch\n--- expected ---\n%s--- got ---\n%s" \
                % (want_out, run.stdout)
        return False, "output mismatch (use -v to see it)"
    return True, "exit %d" % run.returncode


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fast", action="store_true",
                    help="run only the quick subset")
    ap.add_argument("--keep", action="store_true",
                    help="keep the built binaries")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="show full output on mismatch")
    args = ap.parse_args(argv)

    chosen = [e for e in EXAMPLES if e[3] or not args.fast]
    out_dir = (os.path.join(ROOT, "build", "crust") if args.keep
               else tempfile.mkdtemp(prefix="crust_ex_"))
    os.makedirs(out_dir, exist_ok=True)

    failures = 0
    for src, want_rc, want_out, _ in chosen:
        ok, msg = run_one(src, want_rc, want_out, out_dir, args.verbose)
        if ok:
            print("  ok    %-14s (%s)" % (src, msg))
        else:
            print("  FAIL  %-14s %s" % (src, msg))
            failures += 1

    label = "test_fast_crust" if args.fast else "test_crust"
    if failures:
        print("%s: FAIL (%d/%d)" % (label, failures, len(chosen)))
        return 1
    print("%s: PASS (%d examples)" % (label, len(chosen)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
