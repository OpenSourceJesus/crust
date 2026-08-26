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
        self.status = "pending"
        self.detail = ""
        self.warnings = []      # (kind, count) of unlowered constructs
        self.binary = None

    @property
    def blockers(self):
        return sum(c for _, c in self.warnings)


UNLOWERED = re.compile(r"py2c: [^:]+:\d+: (.+?) is not lowered")


def transpile(name, workdir, verbose):
    """Run py2c; return (ok, cfile, warnings). Warnings are what py2c silently
    replaced with None -- the difference between a build and a correct one."""
    src = os.path.join(TOOLS, name)
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
    cfile = os.path.join(workdir, name[:-3] + ".c")
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


def compile_c(name, workdir, verbose):
    import glob
    binp = os.path.join(workdir, name[:-3])
    # mb_ffi.c is the ctypes/dlopen shim. Link it only for tools that actually
    # reach it: it defines symbols (mb_dom_*) that collide otherwise, which
    # showed up as a link failure on tools that had compiled perfectly well.
    cfile = os.path.join(workdir, name[:-3] + ".c")
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


def build_one(name, verbose):
    r = Result(name)
    if name in SKIP:
        r.status, r.detail = "skipped", SKIP[name]
        return r
    workdir = tempfile.mkdtemp(prefix="buildtools_")
    try:
        ok, cfile, warns = transpile(name, workdir, verbose)
        r.warnings = warns
        if not ok:
            r.status, r.detail = "transpile", cfile
            return r
        write_runtime(workdir)
        ok, binp = compile_c(name, workdir, verbose)
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
    if not names:
        sys.exit("buildtools: no tools matched")

    results = []
    for name in names:
        r = build_one(name, args.verbose)
        results.append(r)
        mark = {"verified": "ok  ", "unverified": "--  ", "skipped": "--  "}.get(
            r.status, "FAIL")
        print("  %s %-26s %-11s %s" % (mark, name, r.status, r.detail[:70]))
        if args.verbose and r.warnings:
            for kind, c in r.warnings[:6]:
                print("           %4d  %s" % (c, kind))

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
                shutil.copy(r.binary, os.path.join(args.prefix, r.name[:-3]))
                n += 1
        print("\ninstalled %d binaries into %s" % (n, args.prefix))

    bad = sum(1 for r in results
              if r.status in ("MISMATCH", "compile", "transpile"))
    return 1 if bad else 0


sys.exit(main())
