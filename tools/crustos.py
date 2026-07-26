#!/usr/bin/env python3
"""crustos -- build a mini Redox with Crust, without cargo.

Redox is a cargo workspace of ~100 crates that pulls in a nightly toolchain,
build scripts, procedural macros and a package manager (the `redox` repo is
the *cookbook*; the OS itself lives in sibling repos). None of that is needed
to answer the question this tool exists to answer: **which Redox source files
can Crust actually compile today, and what is stopping the rest?**

So crustos ignores Cargo.toml entirely. It walks a source tree, finds `.rs`
files, runs each through the Crust front end, and reports what happened --
then compiles the ones that work.

    python3 tools/crustos.py survey ~/redox-kernel ~/redox-relibc
    python3 tools/crustos.py survey ~/redox-kernel --blockers
    python3 tools/crustos.py build  ~/redox-kernel -o build/crustos
    python3 tools/crustos.py stage  ~/redox-kernel -o /tmp/mini-redox

A note on honesty in the numbers. `crust.translate` passes a file through
unchanged when it finds no top-level Rust items in it -- which is the right
behaviour for a C file with no Rust in it, but would score a Redox file that
Crust understood *nothing* of as a success. So a file only counts as
translated here if Crust actually found and lowered at least one item, and the
report separates the two cases explicitly.
"""

import argparse
import collections
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import shivyc.crust as crust                                  # noqa: E402

# Directories that never hold OS source worth compiling.
SKIP_DIRS = {".git", "target", "tests", "test", "benches", "examples",
             "build", "node_modules", ".github"}

# Outcome buckets, worst to best.
EMPTY = "empty"        # no Rust items Crust recognizes -- nothing to do
FAILED = "failed"      # items found, translation raised
PARTIAL = "partial"    # translated, but Crust saw only some of the file
TRANSLATED = "translated"   # translated, and Crust saw essentially all of it


class Result:
    def __init__(self, path, outcome, items=0, lines=0, covered=0, error=None):
        self.path = path
        self.outcome = outcome
        self.items = items          # top-level Rust items Crust found
        self.lines = lines          # source lines
        self.covered = covered      # lines inside those items
        self.error = error

    @property
    def coverage(self):
        return (self.covered / self.lines) if self.lines else 0.0


def iter_sources(roots):
    """Yield every .rs file under `roots`, skipping vendored/build dirs."""
    for root in roots:
        if os.path.isfile(root) and root.endswith(".rs"):
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS and not d.startswith(".")]
            for name in sorted(filenames):
                if name.endswith(".rs"):
                    yield os.path.join(dirpath, name)


def classify(path):
    """Run one file through Crust and bucket the outcome."""
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
    except OSError as e:
        return Result(path, FAILED, error="unreadable: %s" % e)

    lines = src.count("\n") + 1
    try:
        spans = crust.find_rust_items(src, rust_file=True)
    except crust.CrustError as e:
        return Result(path, FAILED, lines=lines, error=str(e))

    if not spans:
        return Result(path, EMPTY, lines=lines)

    covered = sum(src.count("\n", s, e) for s, e, _ in spans)
    try:
        crust.translate(src, path=path)
    except crust.CrustError as e:
        return Result(path, FAILED, len(spans), lines, covered, str(e))
    except RecursionError:
        return Result(path, FAILED, len(spans), lines, covered,
                      "recursion limit in the parser")
    except Exception as e:                                    # pragma: no cover
        return Result(path, FAILED, len(spans), lines, covered,
                      "internal %s: %s" % (type(e).__name__, e))

    outcome = TRANSLATED if covered >= 0.6 * max(lines - 1, 1) else PARTIAL
    return Result(path, outcome, len(spans), lines, covered)


# Error-message normalization, so a histogram groups the same cause together.
_NORM = [
    (re.compile(r"^.*?line \d+: "), ""),
    (re.compile(r"`[^`]*`"), "`X`"),
    (re.compile(r"\d+"), "N"),
]


def normalize(msg):
    for pat, rep in _NORM:
        msg = pat.sub(rep, msg)
    return msg.strip()


# Blockers worth naming as features rather than as parse errors. Each entry is
# (label, test) where test looks at the raw error and the source.
FEATURES = [
    ("generics / turbofish `<T>`",
     lambda e, s: "found '<'" in e or "found '>'" in e),
    ("`unsafe { }` blocks",
     lambda e, s: "'unsafe'" in e),
    ("`impl Trait for Type`",
     lambda e, s: "found 'for'" in e),
    ("data-carrying enum variants",
     lambda e, s: "data-carrying enum" in e),
    ("macros (`name!`)",
     lambda e, s: "found '!'" in e or "'#'" in e),
    ("paths / `::` in type or pattern position",
     lambda e, s: "found '::'" in e),
    ("closures / tuples `(..)`",
     lambda e, s: "found '('" in e or "a type, found '('" in e),
    ("range or `..` patterns",
     lambda e, s: "found '..'" in e),
    ("method resolution (trait methods)",
     lambda e, s: "no method" in e or "cannot infer the type of the receiver"
     in e),
]


def survey(results, show_blockers, top, show_files):
    buckets = collections.Counter(r.outcome for r in results)
    total = len(results)
    items = sum(r.items for r in results)
    print("Redox source survey -- %d .rs files" % total)
    print()
    for label, key in [("translated (Crust saw most of the file)", TRANSLATED),
                       ("partial    (some items lowered, rest passed through)",
                        PARTIAL),
                       ("failed     (items found, translation errored)",
                        FAILED),
                       ("empty      (no Rust items Crust recognizes)", EMPTY)]:
        n = buckets[key]
        print("  %-52s %4d  %5.1f%%" % (label, n, 100.0 * n / total if total
                                        else 0))
    print()
    print("  top-level Rust items Crust parsed: %d" % items)
    usable = [r for r in results if r.outcome in (TRANSLATED, PARTIAL)]
    print("  files with at least one lowered item: %d" % len(usable))

    if show_files and usable:
        print("\ncompilable files:")
        for r in sorted(usable, key=lambda r: -r.items)[:top]:
            print("  %3d items  %5.0f%%  %s"
                  % (r.items, 100 * r.coverage, os.path.relpath(r.path)))

    if not show_blockers:
        return
    failed = [r for r in results if r.outcome == FAILED and r.error]

    print("\nblocking features, by files affected (of %d failing files):"
          % len(failed))
    counts = collections.Counter()
    for r in failed:
        for label, test in FEATURES:
            try:
                if test(r.error, None):
                    counts[label] += 1
                    break
            except Exception:                                 # pragma: no cover
                pass
        else:
            counts["other"] += 1
    for label, n in counts.most_common():
        print("  %4d  %5.1f%%  %s" % (n, 100.0 * n / len(failed) if failed
                                      else 0, label))

    print("\nraw messages, most frequent %d:" % top)
    raw = collections.Counter(normalize(r.error) for r in failed)
    for msg, n in raw.most_common(top):
        print("  %4d  %s" % (n, msg[:96]))


def verify(results, out_dir):
    """Compile the translatable files and report how many really build.

    Translating is not compiling, and the gap between them is large: text
    Crust does not recognize is passed through byte-for-byte, so a construct
    with no C meaning survives translation intact and only fails later, in
    the C front end. `use core::mem;` did exactly that for a long time --
    invisible to any measurement that stops at `translate()`.

    This is the number that answers "how much of Redox can Crust compile",
    so it is worth the extra minute it costs.
    """
    import tempfile
    out_dir = out_dir or tempfile.mkdtemp(prefix="crustos_verify_")
    os.makedirs(out_dir, exist_ok=True)
    usable = [r for r in results
              if r.outcome in (TRANSLATED, PARTIAL)]
    ok, causes = 0, collections.Counter()
    for r in usable:
        obj = os.path.join(out_dir, os.path.basename(r.path) + ".o")
        proc = subprocess.run(
            [sys.executable, "-m", "shivyc.main", "-c", r.path, "-o", obj],
            cwd=ROOT, capture_output=True, text=True)
        if proc.returncode == 0 and os.path.exists(obj):
            ok += 1
        else:
            causes[_diagnostic(proc)] += 1
    print("\nverified by compiling:")
    print("  %d of %d translatable files produce an object  (%.1f%% of all "
          "%d files)" % (ok, len(usable),
                         100.0 * ok / len(results) if results else 0,
                         len(results)))
    if causes:
        print("\n  what stops the rest:")
        for msg, n in causes.most_common(10):
            print("    %3d  %s" % (n, msg))


def stage(results, out_dir):
    """Copy every file with a lowered item into `out_dir`, flattened."""
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    for r in results:
        if r.outcome not in (TRANSLATED, PARTIAL):
            continue
        dest = os.path.join(out_dir, os.path.basename(r.path))
        base, ext = os.path.splitext(dest)
        k = 1
        while os.path.exists(dest):
            dest = "%s_%d%s" % (base, k, ext)
            k += 1
        with open(r.path, encoding="utf-8", errors="replace") as f:
            src = f.read()
        with open(dest, "w", encoding="utf-8") as f:
            f.write(src)
        n += 1
    print("staged %d files into %s" % (n, out_dir))
    return n


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _diagnostic(proc):
    """Pull the real compiler message out of a failed run.

    The last line of the output is usually caret/underline decoration, so
    taking it verbatim produces a histogram of dashes rather than of causes.
    Prefer the first line that actually carries a diagnostic.
    """
    text = _ANSI.sub("", (proc.stderr or "") + (proc.stdout or ""))
    for line in text.splitlines():
        if "error:" in line.lower():
            tail = line.split("error:", 1)[-1].strip()
            return (tail or line.strip())[:90]
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1].strip()[:90] if lines else "?"


def build(results, out_dir, keep_going):
    """Compile every translatable file to an object with ShivyCX."""
    os.makedirs(out_dir, exist_ok=True)
    ok = failed = 0
    for r in results:
        if r.outcome not in (TRANSLATED, PARTIAL):
            continue
        obj = os.path.join(out_dir, os.path.basename(r.path) + ".o")
        proc = subprocess.run(
            [sys.executable, "-m", "shivyc.main", "-c", r.path, "-o", obj],
            cwd=ROOT, capture_output=True, text=True)
        if proc.returncode == 0 and os.path.exists(obj):
            ok += 1
            print("  ok    %s" % os.path.relpath(r.path))
        else:
            failed += 1
            print("  FAIL  %s: %s" % (os.path.relpath(r.path),
                                      _diagnostic(proc)))
            if not keep_going:
                break
    print("\nbuild: %d objects, %d failures" % (ok, failed))
    return 0 if failed == 0 else 1


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("survey", "build", "stage"))
    ap.add_argument("roots", nargs="+", help="Redox source trees or .rs files")
    ap.add_argument("-o", "--out", default="build/crustos",
                    help="output directory for build/stage")
    ap.add_argument("--verify", action="store_true",
                    help="also compile each translatable file, and report "
                         "how many actually produce an object")
    ap.add_argument("--blockers", action="store_true",
                    help="show what is stopping the failing files")
    ap.add_argument("--files", action="store_true",
                    help="list the files that do translate")
    ap.add_argument("--top", type=int, default=20,
                    help="how many entries to show in each ranking")
    ap.add_argument("-k", "--keep-going", action="store_true",
                    help="keep building after a failure")
    args = ap.parse_args(argv)

    sources = list(iter_sources(args.roots))
    if not sources:
        print("no .rs files found under: %s" % ", ".join(args.roots))
        return 1
    results = [classify(p) for p in sources]

    if args.mode == "survey":
        survey(results, args.blockers, args.top, args.files)
        if args.verify:
            verify(results, args.out)
        return 0
    if args.mode == "stage":
        stage(results, args.out)
        return 0
    return build(results, args.out, args.keep_going)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
