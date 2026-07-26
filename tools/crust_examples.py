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
# each front end (C, Rust, rpython) and the include paths between them,
# without recompiling the whole directory.
EXAMPLES = [
    ("mixed.c", 0,
     "gcd(1071, 462) = 21\n"
     "classify(-7)   = -1\n"
     "sum_to(100)    = 5050\n"
     "dot(u, v)      = 32\n", True),

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

    ("tally.c", 0,
     "labels = b0,b1,b2,b3\n"
     "widest = 3\n"
     "len2   = 25\n", True),

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
