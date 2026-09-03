#!/usr/bin/env python3
"""rpy_census -- what stops an RPython program from lowering to C that compiles.

`py2c.py` transpiles happily and reports problems as it goes, but the two
kinds of problem it reports read very differently once a program is large
enough to have hundreds of them:

  advisory   "list 'decls' looks like list[str]; annotate it ..."
             costs a reader nothing if ignored -- the program is correct
             either way, only slower.
  substituted"re.sub() is not lowered; substituted None"
             changes what the program *does*. These are the dangerous ones.

And neither of those is the same as the third kind, which py2c cannot see at
all: C that gcc then refuses. `tools/cpprust.py` transpiles with zero errors
from py2c and produces C with seventy-six of them.

This runs all three passes and prints one census, so a change can be measured
rather than argued about:

    python3 tools/rpy_census.py tools/cpprust.py
    python3 tools/rpy_census.py tools/cpprust.py --errors   # just the count

The count is the point. It is what makes "fixed the re.sub coercion" a claim
with a number attached instead of an assertion, and what a commit can move.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PY2C = os.path.join(HERE, "py2c.py")


def transpile(src, out_dir):
    """Run py2c over `src`. Returns (ok, stderr, generated_c_path)."""
    p = subprocess.run([sys.executable, PY2C, src, "--out", out_dir],
                       capture_output=True, text=True)
    base = os.path.basename(src)[:-3] + ".c"
    gen = os.path.join(out_dir, base)
    return p.returncode == 0 and os.path.exists(gen), p.stderr, gen


def compile_errors(gen):
    """gcc's complaints about the generated C, as raw text."""
    if not os.path.exists(gen):
        return ""
    # LC_ALL=C so gcc quotes with 'straight' rather than typographic quotes:
    # the grouping below keys on them, and a locale that swaps them silently
    # turned every shape into its own group.
    env = dict(os.environ, LC_ALL="C")
    p = subprocess.run(["gcc", "-w", "-fsyntax-only", gen],
                       capture_output=True, text=True, env=env)
    return p.stderr


def _norm(msg):
    """An error's shape, for grouping: identifiers and numbers dropped."""
    msg = re.sub(r"'[^']*'", "'X'", msg)
    return re.sub(r"\d+", "N", msg)


def census(stderr, cerr):
    """(substituted, advisory, errors_by_shape, functions) from the raw text."""
    subs, adv = [], []
    for line in stderr.splitlines():
        if line.startswith("py2c:") and "is not lowered" in line:
            subs.append(line)
        elif line.startswith("rpython:"):
            adv.append(line)
    shapes = {}
    for m in re.finditer(r"error: (.*)", cerr):
        shapes[_norm(m.group(1))] = shapes.get(_norm(m.group(1)), 0) + 1
    funcs = set(re.findall(r"In function '([^']+)'", cerr))
    return subs, adv, shapes, funcs


def _kind(line):
    """The construct a substitution warning is about, for grouping."""
    m = re.search(r": ([\w.]+\(\)) is not lowered", line)
    return m.group(1) if m else line


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("source", help="the .py to lower")
    ap.add_argument("--errors", action="store_true",
                    help="print only the gcc error count (for scripting)")
    ap.add_argument("--out", help="keep the generated C in this directory")
    args = ap.parse_args(argv)

    tmp = args.out or tempfile.mkdtemp(prefix="rpy_census_")
    os.makedirs(tmp, exist_ok=True)
    ok, stderr, gen = transpile(args.source, tmp)
    cerr = compile_errors(gen)
    subs, adv, shapes, funcs = census(stderr, cerr)
    total = sum(shapes.values())

    if args.errors:
        print(total)
        return 0 if total == 0 else 1

    name = os.path.basename(args.source)
    print("%s -- %s" % (name, "transpiled" if ok else "TRANSPILE FAILED"))
    print()
    print("  gcc errors in the generated C : %d  (in %d functions)"
          % (total, len(funcs)))
    print("  calls substituted with None   : %d" % len(subs))
    print("  container advisories          : %d" % len(adv))

    if shapes:
        print("\n  error shapes, worst first -- one rule may cover many:")
        for shape, n in sorted(shapes.items(), key=lambda kv: -kv[1]):
            print("    %4d  %s" % (n, shape[:96]))

    if subs:
        print("\n  substituted with None -- these change what the program does:")
        kinds = {}
        for line in subs:
            kinds.setdefault(_kind(line), []).append(line)
        for kind, lines in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
            where = ", ".join(
                re.sub(r"^.*?:(\d+):.*$", r"\1", ln) for ln in lines[:6])
            more = "" if len(lines) <= 6 else ", ..."
            print("    %4d  %-18s lines %s%s"
                  % (len(lines), kind, where, more))

    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
