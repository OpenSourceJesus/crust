#!/usr/bin/env python3
"""buildtools.py -- lower the scripts in tools/ to C and install what works.

Lives at the top level rather than in tools/ on purpose: everything in tools/
is a candidate for lowering, and this is not -- it is a build/test runner whose
cost is dominated by the compilers it invokes, so a C version would not be
meaningfully faster and would only complicate the bootstrap.

Each tool goes through four stages:

    transpile -> compile -> verify -> install

`verify` is the gate that matters. A tool that transpiles and compiles is not
necessarily correct: py2c substitutes None for calls it cannot lower and warns
rather than failing, so a binary can build cleanly and then behave differently
from the script it came from. Nothing is installed until its native output
matches the Python original on a real invocation, so the default status for a
tool with no smoke test is "unverified", never "installed".

    python3 buildtools.py                 # build + verify everything, table
    python3 buildtools.py --only cpprust  # one tool (repeatable)
    python3 buildtools.py --report        # what is blocking translation, ranked
    python3 buildtools.py --install       # copy verified binaries to PREFIX
    python3 buildtools.py -v              # show compiler output on failure

Installed binaries go to /tmp/crusted/usr/bin by default (--prefix to change).
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(ROOT, "tools")
PY2C = os.path.join(TOOLS, "py2c.py")
DEFAULT_PREFIX = "/tmp/crusted/usr/bin"

# Tools that are deliberately not candidates, with the reason. Keeping this
# explicit means "not attempted" never gets confused with "failed".
SKIP = {
    "py2c.py": "the transpiler itself: complex, and worth keeping flexible",
    "__init__.py": "package marker",
    "buildtools.py": "this script",
}

# How to exercise a tool so its native build can be compared against the
# script. `args` is formatted with {fixture} and {out}; a tool whose run
# produces a file sets `output_file` so the file is diffed instead of stdout.
SMOKE = {
    "cpprust.py": {
        "fixture": "examples/crust/owned.cpp",
        "args": ["{fixture}", "-o", "{out}"],
        "output_file": True,
    },
    "cpp2rust.py": {
        "fixture": "examples/crust/owned.cpp",
        "args": ["{fixture}"],
        "output_file": False,
    },
}


def tool_scripts(only):
    out = []
    for name in sorted(os.listdir(TOOLS)):
        if not name.endswith(".py"):
            continue
        if only and not any(o in name for o in only):
            continue
        out.append(name)
    return out


class Result:
    def __init__(self, name):
        self.name = name
        # What the binary is called in PREFIX. For a tools/<x>.py script that is
        # <x>; targets like minipy are already named for the binary, so stripping
        # a ".py" that isn't there would install it as "min".
        self.install_name = name[:-3] if name.endswith(".py") else name
        self.status = "pending"
        self.detail = ""
        self.warnings = []      # (kind, count) of unlowered constructs
        self.binary = None

    @property
    def blockers(self):
        return sum(c for _, c in self.warnings)


UNLOWERED = re.compile(r"py2c: [^:]+:\d+: (.+?) is not lowered")


def transpile(src, stem, workdir, verbose):
    """Run py2c; return (ok, cfile, warnings). Warnings are what py2c silently
    replaced with None -- the difference between a build and a correct one.

    `src` is the script to lower and `stem` the basename py2c will give the C
    file. They are passed separately because not every target lives at
    tools/<stem>.py: minipy's interpreter is tools/minipy/interp.py but is
    installed under its package name."""
    p = subprocess.run([sys.executable, PY2C, src, "--out", workdir],
                       capture_output=True, text=True)
    blob = p.stdout + p.stderr
    counts = {}
    for m in UNLOWERED.finditer(blob):
        kind = m.group(1).strip()
        # "re.sub()" and "expression of type Yield" both appear; normalise the
        # long form so the report groups them.
        kind = re.sub(r"^expression of type (\w+).*", r"\1 expression", kind)
        counts[kind] = counts.get(kind, 0) + 1
    warnings = sorted(counts.items(), key=lambda kv: -kv[1])
    cfile = os.path.join(workdir, stem + ".c")
    if p.returncode != 0 or not os.path.isfile(cfile):
        return False, blob if verbose else _last_error(blob), warnings
    return True, cfile, warnings


def _last_error(blob):
    lines = [l for l in blob.splitlines()
             if l.strip() and not l.startswith("rpython:")
             and "is not lowered" not in l]
    return lines[-1][:160] if lines else "transpile failed"


def write_runtime(workdir):
    subprocess.run(
        [sys.executable, "-c",
         "import sys;sys.path.insert(0,%r);import py2c;py2c.write_runtime(%r)"
         % (TOOLS, workdir)], capture_output=True, text=True)


def compile_c(stem, workdir, verbose):
    import glob
    binp = os.path.join(workdir, stem)
    # mb_ffi.c is the ctypes/dlopen shim. Link it only for tools that actually
    # reach it: it defines symbols (mb_dom_*) that collide otherwise, which
    # showed up as a link failure on tools that had compiled perfectly well.
    cfile = os.path.join(workdir, stem + ".c")
    body = open(cfile).read() if os.path.isfile(cfile) else ""
    mbffi = os.path.join(TOOLS, "rpy_lib", "mb_ffi.c")
    if "mb_ffi" in body or "mb_dlopen" in body:
        if os.path.isfile(mbffi):
            shutil.copy(mbffi, workdir)
    csrc = sorted(glob.glob(os.path.join(workdir, "*.c")))
    cc = subprocess.run(["gcc", "-std=c99", "-O2", "-w", "-I", workdir]
                        + csrc + ["-o", binp, "-lm"],
                        capture_output=True, text=True)
    if cc.returncode != 0 or not os.path.isfile(binp):
        errs = [l for l in cc.stderr.splitlines() if " error:" in l]
        detail = cc.stderr if verbose else (errs[0][:160] if errs else "link failed")
        return False, detail
    return True, binp


def verify(name, binp, workdir, verbose):
    """Compare native output against the Python script on a real invocation."""
    spec = SMOKE.get(name)
    if not spec:
        return "unverified", "no smoke test"
    fixture = os.path.join(ROOT, spec["fixture"])
    if not os.path.isfile(fixture):
        return "unverified", "fixture missing: " + spec["fixture"]

    def run(cmd, tag):
        out = os.path.join(workdir, tag + ".out")
        argv = [a.format(fixture=fixture, out=out) for a in spec["args"]]
        p = subprocess.run(cmd + argv, capture_output=True, text=True,
                           timeout=120, cwd=ROOT)
        if spec["output_file"]:
            body = open(out).read() if os.path.isfile(out) else "<no output file>"
        else:
            body = p.stdout
        return p.returncode, body

    try:
        prc, pout = run([sys.executable, os.path.join(TOOLS, name)], "py")
        nrc, nout = run([binp], "native")
    except subprocess.TimeoutExpired:
        return "MISMATCH", "timed out"
    if prc != nrc:
        return "MISMATCH", "exit %d (python) vs %d (native)" % (prc, nrc)
    if pout != nout:
        n = sum(1 for a, b in zip(pout.splitlines(), nout.splitlines()) if a != b)
        return "MISMATCH", ("output differs (%d lines, %d vs %d bytes)"
                            % (n, len(pout), len(nout)))
    return "verified", "%d bytes match" % len(pout)


# --------------------------------------------------------------------------- #
# minipy                                                                       #
# --------------------------------------------------------------------------- #
# minipy is not a tools/*.py script, so it does not come out of tool_scripts():
# the thing that gets lowered is tools/minipy/interp.py, one file inside a
# package whose other half (compiler.py) stays on CPython for now. It is built
# here anyway because it is the same four-stage pipeline and, unlike the other
# tools, it is the one binary the self-hosting story depends on.
#
# The verify gate is stricter than the SMOKE table's single fixture. minipy is a
# Python implementation, so "does it work" is a suite question, not a one-shot
# diff: every tools/minipy/test_*.py is run under CPython and under the native
# interpreter and the outputs must match byte for byte. That is the same 3-way
# check `make testminipy` performs, folded into the install gate so a regressed
# interpreter cannot reach PREFIX.
MINIPY_SRC = os.path.join(TOOLS, "minipy", "interp.py")
MINIPY_TESTS = os.path.join(TOOLS, "minipy", "test_*.py")

# Compile a guest script to minipy bytecode. This runs under CPython by
# necessity: compiler.py does not yet compile under minipy itself (it uses one
# construct the v0 code generator rejects), so the front end cannot be lowered
# alongside the interpreter. When that lands, this shell-out disappears and
# minipy stops needing CPython at all.
_COMPILE = ("import sys, json; sys.path.insert(0, %r)\n"
            "from minipy import compiler\n"
            "json.dump(compiler.compile_file(%r), open(%r, 'w'))\n")


def _minipy_bytecode(script, out):
    p = subprocess.run([sys.executable, "-c", _COMPILE % (TOOLS, script, out)],
                       capture_output=True, text=True, cwd=ROOT)
    if p.returncode != 0 or not os.path.isfile(out):
        last = [l for l in (p.stdout + p.stderr).splitlines() if l.strip()]
        return False, (last[-1][:120] if last else "compile_file failed")
    return True, ""


def verify_minipy(binp, workdir, verbose):
    """Run the minipy test suite under CPython and under the native binary and
    require identical output. Returns (status, detail)."""
    import glob
    tests = sorted(glob.glob(MINIPY_TESTS))
    if not tests:
        return "unverified", "no tests found in tools/minipy"
    failures = []
    for t in tests:
        rel = os.path.relpath(t, ROOT)
        bc = os.path.join(workdir, os.path.basename(t)[:-3] + ".json")
        ok, err = _minipy_bytecode(t, bc)
        if not ok:
            failures.append("%s: %s" % (os.path.basename(t), err))
            continue
        try:
            cp = subprocess.run([sys.executable, t], capture_output=True,
                                text=True, timeout=300, cwd=ROOT)
            nat = subprocess.run([binp, bc], capture_output=True,
                                 text=True, timeout=300, cwd=ROOT)
        except subprocess.TimeoutExpired:
            failures.append("%s: timed out" % os.path.basename(t))
            continue
        if cp.stdout != nat.stdout or cp.returncode != nat.returncode:
            failures.append("%s: output differs" % os.path.basename(t))
    if failures:
        detail = "%d/%d tests failed" % (len(failures), len(tests))
        if verbose:
            detail += ": " + "; ".join(failures[:6])
        return "MISMATCH", detail
    return "verified", "%d/%d tests match CPython" % (len(tests), len(tests))


def build_minipy(verbose):
    r = Result("minipy")
    if not os.path.isfile(MINIPY_SRC):
        r.status, r.detail = "skipped", "tools/minipy/interp.py missing"
        return r
    workdir = tempfile.mkdtemp(prefix="buildtools_minipy_")
    try:
        ok, cfile, warns = transpile(MINIPY_SRC, "interp", workdir, verbose)
        r.warnings = warns
        if not ok:
            r.status, r.detail = "transpile", cfile
            return r
        write_runtime(workdir)
        ok, binp = compile_c("interp", workdir, verbose)
        if not ok:
            r.status, r.detail = "compile", binp
            return r
        status, detail = verify_minipy(binp, workdir, verbose)
        r.status, r.detail = status, detail
        if status == "verified":
            keep = os.path.join(tempfile.gettempdir(), "buildtools_bin")
            os.makedirs(keep, exist_ok=True)
            r.binary = os.path.join(keep, "minipy")
            shutil.copy(binp, r.binary)
        return r
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def build_one(name, verbose):
    r = Result(name)
    if name in SKIP:
        r.status, r.detail = "skipped", SKIP[name]
        return r
    workdir = tempfile.mkdtemp(prefix="buildtools_")
    try:
        ok, cfile, warns = transpile(os.path.join(TOOLS, name), name[:-3],
                                     workdir, verbose)
        r.warnings = warns
        if not ok:
            r.status, r.detail = "transpile", cfile
            return r
        write_runtime(workdir)
        ok, binp = compile_c(name[:-3], workdir, verbose)
        if not ok:
            r.status, r.detail = "compile", binp
            return r
        status, detail = verify(name, binp, workdir, verbose)
        r.status, r.detail = status, detail
        if status == "verified":
            keep = os.path.join(tempfile.gettempdir(), "buildtools_bin")
            os.makedirs(keep, exist_ok=True)
            r.binary = os.path.join(keep, name[:-3])
            shutil.copy(binp, r.binary)
        return r
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def report(results):
    counts = {}
    per_tool = {}
    for r in results:
        for kind, c in r.warnings:
            counts[kind] = counts.get(kind, 0) + c
            per_tool.setdefault(kind, set()).add(r.name)
    print("\nWhat is blocking translation, ranked:\n")
    print("  %-34s %6s %6s" % ("construct", "uses", "tools"))
    for kind, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        print("  %-34s %6d %6d" % (kind[:34], c, len(per_tool[kind])))
    if not counts:
        print("  (nothing)")


STATUS_ORDER = ["verified", "unverified", "MISMATCH", "compile", "transpile",
                "skipped"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", default=[],
                    help="substring of a tool name; repeatable")
    ap.add_argument("--install", action="store_true",
                    help="copy verified binaries into --prefix")
    ap.add_argument("--prefix", default=DEFAULT_PREFIX)
    ap.add_argument("--report", action="store_true",
                    help="rank the constructs blocking translation")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    names = tool_scripts(args.only)
    want_minipy = not args.only or any(o in "minipy" for o in args.only)
    if not names and not want_minipy:
        sys.exit("buildtools: no tools matched")

    def record(r):
        results.append(r)
        mark = {"verified": "ok  ", "unverified": "--  ", "skipped": "--  "}.get(
            r.status, "FAIL")
        print("  %s %-26s %-11s %s" % (mark, r.name, r.status, r.detail[:70]))
        if args.verbose and r.warnings:
            for kind, c in r.warnings[:6]:
                print("           %4d  %s" % (c, kind))

    results = []
    for name in names:
        record(build_one(name, args.verbose))
    # minipy last: it is the slowest target (the interpreter is ~3.7k lines and
    # verification runs the whole suite twice), so the cheap tools report first.
    if want_minipy:
        record(build_minipy(args.verbose))

    tally = {}
    for r in results:
        tally[r.status] = tally.get(r.status, 0) + 1
    print("\n" + "  ".join("%s=%d" % (s, tally[s])
                           for s in STATUS_ORDER if s in tally))

    if args.report:
        report(results)

    if args.install:
        os.makedirs(args.prefix, exist_ok=True)
        n = 0
        for r in results:
            if r.status == "verified" and r.binary:
                shutil.copy(r.binary, os.path.join(args.prefix, r.install_name))
                n += 1
        print("\ninstalled %d binaries into %s" % (n, args.prefix))

    bad = sum(1 for r in results
              if r.status in ("MISMATCH", "compile", "transpile"))
    return 1 if bad else 0


sys.exit(main())
