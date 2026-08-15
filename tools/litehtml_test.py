#!/usr/bin/env python3
"""litehtml_test -- drive tools/cpprust.py over the litehtml sources.

Two stages, either of which can fail:

  translate   tools/cpprust.py lowers one .cpp (and the headers it pulls
              in) to C. A refusal is a `CppError`, which is the whole
              point of this pass -- the message names the reason.
  compile     gcc -fsyntax-only on the lowered C. This is the stage that
              catches a lowering which *succeeded* and produced C that
              does not mean anything. ShivyCX is the real target, but gcc
              is much faster and rejects the same broken C, so it is the
              one to iterate against.

Usage:

    python3 tools/litehtml_test.py                    # everything
    python3 tools/litehtml_test.py el_div url         # just these
    python3 tools/litehtml_test.py --stage translate  # skip gcc
    python3 tools/litehtml_test.py -v el_div          # full diagnostic
    python3 tools/litehtml_test.py --groups           # failures by cause

The default report groups failures by their *message shape* rather than
listing them per file, because one refusal in a shared header fails every
file that includes it: eleven files reporting `delete ref` are one bug in
`context.h`, not eleven bugs. Grouping is what keeps that visible, and it
is what tells you which single fix buys the most files.

Translations are cached under .litehtml_cache/, keyed on the source, every
header that could be spliced, and the translator's own sources -- so
editing cpprust.py invalidates everything, which is the behaviour you want
while working on it. `--no-cache` forces a rerun.
"""

import argparse
import hashlib
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CPPRUST = os.path.join(HERE, "cpprust.py")

# The fork is normally cloned beside crust/. --litehtml overrides this.
DEFAULT_LITEHTML = os.path.join(os.path.dirname(ROOT), "juce_litehtml")


def _paths(base):
    """The include path litehtml needs, in the order gcc would see it.

    quickjs is the one that is easy to leave out and expensive to get
    wrong: context.h includes quickjs.h for `JS_NewClass`, and without
    that declaration `auto` has nothing to deduce from, so twenty-odd
    files fail with a diagnostic that looks like a translator gap and is
    really a missing -I.
    """
    lh = os.path.join(base, "juce_litehtml", "litehtml")
    qj = os.path.join(base, "juce_litehtml", "quickjs")
    return {
        "src": os.path.join(lh, "src"),
        "incdirs": [
            os.path.join(lh, "include"),
            os.path.join(lh, "include", "litehtml"),
            os.path.join(lh, "src"),
            qj,
        ],
    }


# `<cstdint>` and friends are the C headers under their C++ spellings.
# cpprust leaves angle includes alone, so they survive into the lowered C
# and gcc stops at the first one -- which hides everything behind it. The
# shim maps them back so the compile stage reports on the *lowering*.
# Mapping these properly belongs in cpprust (it already pulls in
# <stdbool.h> for `bool`); until then this keeps the harness useful.
_CHEADERS = {
    "cstdint": "stdint.h", "cstring": "string.h", "cstdlib": "stdlib.h",
    "cstdio": "stdio.h", "cstddef": "stddef.h", "cctype": "ctype.h",
    "cmath": "math.h", "cassert": "assert.h", "climits": "limits.h",
    "cwchar": "wchar.h", "cerrno": "errno.h", "ctime": "time.h",
}


def _make_shim(d):
    os.makedirs(d, exist_ok=True)
    for name, real in _CHEADERS.items():
        p = os.path.join(d, name)
        if not os.path.exists(p):
            with open(p, "w") as f:
                f.write("/* cpprust harness shim */\n#include <%s>\n" % real)
    return d


def _digest(files):
    h = hashlib.sha256()
    for p in sorted(files):
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except IOError:
            h.update(b"<missing>")
        h.update(p.encode("utf-8", "replace"))
    return h.hexdigest()


def _header_set(incdirs):
    out = []
    for d in incdirs:
        for dirpath, _, names in os.walk(d):
            for n in names:
                if n.endswith((".h", ".hpp", ".inc")):
                    out.append(os.path.join(dirpath, n))
    return out


class Result(object):
    def __init__(self, name):
        self.name = name
        self.stage = "translate"
        self.ok = False
        self.message = ""
        self.cached = False
        self.clang = ""
        self.seconds = 0.0

    @property
    def summary(self):
        """First line of the diagnostic, trimmed."""
        msg = " ".join(self.message.split())
        return msg[:2000]


def _signature(msg):
    """A failure's shape, for grouping.

    Line numbers and the file's own name differ between files reporting
    the same underlying refusal, so they are dropped; what is left is the
    sentence the translator chose, which is one per cause.
    """
    words = []
    for w in " ".join(msg.split()).split(" "):
        if w.endswith(".cpp:") or w.endswith(".h:"):
            continue
        if w.rstrip(":").isdigit():
            continue
        words.append(w)
    return " ".join(words)[:160]


def translate(src, out, incdirs, cache_dir, use_cache, timeout, defines=(),
              clang=None):
    """Lower one .cpp to C. Returns (ok, message, cached)."""
    key = None
    if use_cache:
        deps = [src, CPPRUST, os.path.join(HERE, "cpp_auto.py")]
        deps += _header_set(incdirs)
        key = os.path.join(cache_dir,
                           _digest(deps + ["-D" + d for d in defines]
                                   + ["clang=%s" % clang]))
        if os.path.exists(key + ".ok"):
            with open(key + ".ok") as f:
                data = f.read()
            with open(out, "w") as f:
                f.write(data)
            note = ""
            if os.path.exists(key + ".clang"):
                with open(key + ".clang") as f:
                    note = f.read()
            return True, "", True, note
        if os.path.exists(key + ".fail"):
            with open(key + ".fail") as f:
                return False, f.read(), True, ""

    cmd = [sys.executable, CPPRUST, src, "-o", out]
    for d in incdirs:
        cmd += ["--incdir", d]
    for name in defines:
        cmd += ["-D", name]
    if clang is not None:
        cmd.append("--clang" if clang else "--no-clang")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timed out after %ds" % timeout, False, ""

    # cpprust writes its diagnostic into the -o file on failure, so the
    # file existing is not success -- the exit status is what says.
    ok = p.returncode == 0
    note = ""
    for line in (p.stderr or "").splitlines():
        if "clang answered" in line:
            note = line.split("clang answered", 1)[1].strip()
    msg = "" if ok else (p.stderr.strip() or p.stdout.strip())
    msg = msg.replace("cpprust: ", "", 1)

    if use_cache and key and note:
        with open(key + ".clang", "w") as f:
            f.write(note)
    if use_cache and key:
        if ok:
            with open(out) as f:
                body = f.read()
            with open(key + ".ok", "w") as f:
                f.write(body)
        else:
            with open(key + ".fail", "w") as f:
                f.write(msg)
    return ok, msg, False, note


def compile_c(path, incdirs, timeout, shim=None):
    """gcc -fsyntax-only over the lowered C. Returns (ok, message)."""
    cmd = ["gcc", "-fsyntax-only", "-std=c11", "-w"]
    for d in incdirs:
        cmd += ["-I", d]
    if shim:                       # after the real dirs, so it never wins
        cmd += ["-I", shim]
    cmd.append(path)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "gcc timed out after %ds" % timeout
    if p.returncode == 0:
        return True, ""
    # gcc repeats itself; the first few errors are the ones that matter,
    # the rest are usually the same missing declaration again.
    lines = [l for l in p.stderr.splitlines() if ": error:" in l]
    return False, "\n".join(lines[:6]) or p.stderr.strip()[:2000]


def run_one(name, src, cfg, args):
    r = Result(name)
    t0 = time.time()
    out = os.path.join(cfg["outdir"], name + ".c")
    ok, msg, cached, note = translate(src, out, cfg["incdirs"], cfg["cache"],
                                      not args.no_cache, args.timeout,
                                      cfg["defines"], cfg["clang"])
    r.clang = note
    r.cached = cached
    if not ok:
        r.stage, r.message = "translate", msg
        r.seconds = time.time() - t0
        return r
    if args.stage == "translate":
        r.stage, r.ok = "translate", True
        r.seconds = time.time() - t0
        return r
    ok, msg = compile_c(out, cfg["incdirs"], args.timeout,
                        None if args.no_shim else cfg["shim"])
    r.stage = "compile"
    r.ok = ok
    r.message = msg
    r.seconds = time.time() - t0
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Drive cpprust.py over litehtml, then gcc the result.")
    ap.add_argument("only", nargs="*",
                    help="substrings of filenames to test (default: all)")
    ap.add_argument("--litehtml", default=DEFAULT_LITEHTML,
                    help="path to the juce_litehtml checkout")
    ap.add_argument("--stage", choices=["translate", "compile"],
                    default="compile", help="stop after this stage")
    ap.add_argument("--outdir", default=os.path.join(ROOT, ".litehtml_out"))
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("-D", "--define", action="append", default=[],
                    help="preprocessor name to define (default: "
                         "LITEHTML_UTF8, as juce_litehtml.h does)")
    ap.add_argument("--clang", dest="clang", action="store_true",
                    default=None, help="require the clang `auto` fallback")
    ap.add_argument("--no-clang", dest="clang", action="store_false",
                    help="forbid it, to see which `auto`s are genuinely "
                         "written out rather than answered by a compiler")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--no-shim", action="store_true",
                    help="do not map <cstdint> and friends to their C "
                         "names; shows how far gcc gets unaided")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print each failure's full diagnostic")
    ap.add_argument("--groups", action="store_true",
                    help="group failures by cause, worst first")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="only the final tally")
    args = ap.parse_args(argv)

    paths = _paths(args.litehtml)
    if not os.path.isdir(paths["src"]):
        sys.stderr.write(
            "no litehtml sources at %s\n"
            "clone https://github.com/crustos/juce_litehtml.git beside "
            "crust/, or pass --litehtml\n" % paths["src"])
        return 2

    cfg = {
        "incdirs": [d for d in paths["incdirs"] if os.path.isdir(d)],
        "outdir": args.outdir,
        "cache": os.path.join(ROOT, ".litehtml_cache"),
        "shim": _make_shim(os.path.join(ROOT, ".litehtml_out", "_shim")),
        # juce_litehtml.h defines this before including litehtml, so a
        # translation that does not is reading the wrong half of
        # os_types.h -- `tstring` would be `std::wstring`.
        "defines": list(args.define) or ["LITEHTML_UTF8"],
        "clang": args.clang,
    }
    os.makedirs(cfg["outdir"], exist_ok=True)
    os.makedirs(cfg["cache"], exist_ok=True)

    names = sorted(n for n in os.listdir(paths["src"]) if n.endswith(".cpp"))
    if args.only:
        names = [n for n in names
                 if any(k in n for k in args.only)]
        if not names:
            sys.stderr.write("nothing matches %s\n" % " ".join(args.only))
            return 2

    results = []
    t0 = time.time()
    for n in names:
        r = run_one(n, os.path.join(paths["src"], n), cfg, args)
        results.append(r)
        if not args.quiet:
            mark = "ok  " if r.ok else ("TRANS" if r.stage == "translate"
                                        else "GCC  ")
            note = " (cached)" if r.cached else " %.0fs" % r.seconds
            line = "%-6s %-24s%s" % (mark, n, "" if r.ok else note)
            if r.clang:
                line += "\n       clang answered %s" % r.clang[:110]
            if not r.ok and not args.verbose:
                line += "\n       " + r.summary[:150]
            print(line)
            if not r.ok and args.verbose:
                print("       " + r.message.replace("\n", "\n       "))
            sys.stdout.flush()

    good = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    leaning = [r for r in results if r.clang]
    if leaning:
        print("\n%d file(s) needed the clang `auto` fallback -- without "
              "clang these report. Re-run with --no-clang to see them."
              % len(leaning))
    print("\n%d/%d ok, %d translate-fail, %d gcc-fail  (%.0fs)" % (
        len(good), len(results),
        len([r for r in bad if r.stage == "translate"]),
        len([r for r in bad if r.stage == "compile"]),
        time.time() - t0))

    if args.groups and bad:
        groups = {}
        for r in bad:
            groups.setdefault(_signature(r.summary), []).append(r.name)
        print("\nfailures by cause, worst first "
              "-- one fix at the top may buy several files:\n")
        for sig, files in sorted(groups.items(),
                                 key=lambda kv: -len(kv[1])):
            print("  [%d] %s" % (len(files), sig))
            print("      %s\n" % " ".join(sorted(files)))

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
