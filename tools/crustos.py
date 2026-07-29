#!/usr/bin/env python3
"""crustos -- build a small OS from the parts of Redox that compile.

Redox is a cargo workspace of roughly a hundred crates on a nightly toolchain.
Crust compiles a subset of it, and this tool turns that subset into something
that actually builds and runs, by supplying the rest itself.

The split is the point:

  vendor/     whatever of the real Redox kernel Crust can compile today:
              memory-manager page tables and flags, the buddy and bump frame
              allocators, architecture constants, syscall numbers. Genuine
              Redox source, compiled by Crust, with no cargo involved.

  crustos/    the parts Crust cannot compile, written to be small rather than
              complete -- a scheme layer in rpython, and a frame allocator,
              scheduler and syscall dispatch in Rust. This is where the
              "smaller than Redox" claim lives, and it is honest only because
              it does far less.

Everything ends up as one binary. There is no bootloader and no bare-metal
target: CrustOS runs hosted, as an ordinary program. That makes it a working
model of the structure rather than an OS you can boot, and the distinction is
worth keeping in view -- see CRUSTOS.md.

    python3 tools/crustos.py fetch       # clone/update the Redox sources
    python3 tools/crustos.py survey      # what compiles, and what stops the rest
    python3 tools/crustos.py build       # upstream subset + crustos -> one binary
    python3 tools/crustos.py run         # build, then run it
    python3 tools/crustos.py clean
"""

import argparse
import collections
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import shivyc.crust as crust                                  # noqa: E402

VENDOR = os.environ.get("CRUSTOS_VENDOR", os.path.join(ROOT, "vendor"))
BUILD = os.environ.get("CRUSTOS_BUILD", os.path.join(ROOT, "build", "crustos"))
SHIM = os.path.join(ROOT, "crustos")

# The repositories that make up Redox proper. The `redox` repo itself is the
# *cookbook* -- a package build tool with 25 .rs files, none of them the OS --
# so these are the ones worth fetching.
REPOS = [
    ("kernel", "https://github.com/redox-os/kernel.git"),
    ("relibc", "https://github.com/redox-os/relibc.git"),
]

SKIP_DIRS = {".git", "target", "tests", "test", "benches", "examples",
             "build", "node_modules", ".github"}

EMPTY, FAILED, PARTIAL, TRANSLATED = "empty", "failed", "partial", "translated"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Result:
    def __init__(self, path, outcome, items=0, lines=0, covered=0, error=None):
        self.path = path
        self.outcome = outcome
        self.items = items
        self.lines = lines
        self.covered = covered
        self.error = error

    @property
    def coverage(self):
        return (self.covered / self.lines) if self.lines else 0.0


# -------------------------------------------------------------------------
# fetch
# -------------------------------------------------------------------------

def fetch(update):
    """Clone the Redox sources, or pull them if they are already here."""
    if shutil.which("git") is None:
        print("git is not available; clone these by hand into %s:" % VENDOR)
        for name, url in REPOS:
            print("  %s -> %s" % (url, os.path.join(VENDOR, name)))
        return 1
    os.makedirs(VENDOR, exist_ok=True)
    for name, url in REPOS:
        dest = os.path.join(VENDOR, name)
        if os.path.isdir(os.path.join(dest, ".git")):
            if not update:
                print("  have  %s" % dest)
                continue
            print("  pull  %s" % dest)
            proc = subprocess.run(["git", "-C", dest, "pull", "--ff-only"],
                                  capture_output=True, text=True)
        else:
            # Shallow: the history is of no use here and the full one is large.
            print("  clone %s" % url)
            proc = subprocess.run(["git", "clone", "--depth", "1", url, dest],
                                  capture_output=True, text=True)
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip().splitlines()
            print("  FAILED: %s" % (detail[-1] if detail else "?"))
            print("  (no network? clone by hand into %s)" % dest)
            return 1
    return 0


def sources(roots=None):
    """Every .rs file under the vendored checkouts, or under explicit roots."""
    roots = roots or [os.path.join(VENDOR, name) for name, _ in REPOS]
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


def require_sources(roots):
    got = list(sources(roots))
    if not got:
        print("no Redox sources found under %s" % VENDOR)
        print("run:  python3 tools/crustos.py fetch")
        return None
    return got


# -------------------------------------------------------------------------
# survey
# -------------------------------------------------------------------------

def classify(path):
    """Run one file through Crust and bucket the outcome.

    A file counts as translated only if Crust actually lowered an item.
    Passing unrecognized text through is right for a C file with no Rust in
    it, but it would score a Rust file Crust understood nothing of as a pass.
    """
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


_NORM = [(re.compile(r"^.*?line \d+: "), ""), (re.compile(r"`[^`]*`"), "`X`"),
         (re.compile(r"\d+"), "N")]


def normalize(msg):
    for pat, rep in _NORM:
        msg = pat.sub(rep, msg)
    return msg.strip()


def survey(results, show_blockers, top, verify):
    total = len(results)
    buckets = collections.Counter(r.outcome for r in results)
    print("Redox source survey -- %d .rs files\n" % total)
    for label, key in [("translated (Crust saw most of the file)", TRANSLATED),
                       ("partial    (some items lowered)", PARTIAL),
                       ("failed     (items found, translation errored)",
                        FAILED),
                       ("empty      (no Rust items Crust recognizes)", EMPTY)]:
        n = buckets[key]
        print("  %-46s %4d  %5.1f%%"
              % (label, n, 100.0 * n / total if total else 0))
    usable = [r for r in results if r.outcome in (TRANSLATED, PARTIAL)]
    print("\n  top-level Rust items parsed: %d"
          % sum(r.items for r in results))
    print("  files with at least one lowered item: %d" % len(usable))

    if verify:
        objs, causes = compile_objects(usable, os.path.join(BUILD, "verify"))
        print("\nverified by compiling:")
        print("  %d of %d produce an object  (%.1f%% of all %d files)"
              % (len(objs), len(usable),
                 100.0 * len(objs) / total if total else 0, total))
        if causes:
            print("\n  what stops the rest:")
            for msg, n in causes.most_common(10):
                print("    %3d  %s" % (n, msg))

    if not show_blockers:
        return
    failed = [r for r in results if r.outcome == FAILED and r.error]
    print("\nmost frequent messages (of %d failing files):" % len(failed))
    for msg, n in collections.Counter(
            normalize(r.error) for r in failed).most_common(top):
        print("  %4d  %s" % (n, msg[:96]))


# -------------------------------------------------------------------------
# build
# -------------------------------------------------------------------------

def diagnostic(proc):
    """The real message from a failed compile, not the caret line."""
    text = _ANSI.sub("", (proc.stderr or "") + (proc.stdout or ""))
    for line in text.splitlines():
        if "error:" in line.lower():
            tail = line.split("error:", 1)[-1].strip()
            return (tail or line.strip())[:90]
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1].strip()[:90] if lines else "?"


def compile_one(path, obj):
    os.makedirs(os.path.dirname(obj), exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "shivyc.main", "-c", path, "-o", obj],
        cwd=ROOT, capture_output=True, text=True)
    return (proc.returncode == 0 and os.path.exists(obj)), proc


def compile_objects(results, out_dir, verbose=False):
    """Compile each translatable file; return (objects, failure causes)."""
    objs, causes = [], collections.Counter()
    for i, r in enumerate(results):
        obj = os.path.join(out_dir, "u%03d_%s.o"
                           % (i, os.path.basename(r.path)[:-3]))
        ok, proc = compile_one(r.path, obj)
        if ok:
            objs.append(obj)
            if verbose:
                print("  ok    %s" % os.path.relpath(r.path, VENDOR))
        else:
            causes[diagnostic(proc)] += 1
            if verbose:
                print("  skip  %s: %s" % (os.path.relpath(r.path, VENDOR),
                                          diagnostic(proc)))
    return objs, causes


def symbols(obj):
    """(defined, undefined) global symbol names in an object file."""
    proc = subprocess.run(["nm", "-g", obj], capture_output=True, text=True)
    if proc.returncode != 0:
        return set(), set()
    defined, undef = set(), set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-2] == "U":
            undef.add(parts[-1])
        elif len(parts) >= 3 and parts[-2] in "TDBRWVGSA":
            defined.add(parts[-1])
    return defined, undef


def linkable_subset(objs, provided):
    """Pick the objects that can actually be linked together.

    Two things make the full set unlinkable, and both are consequences of
    compiling files that were never meant to be one program:

      * Redox ships one implementation per architecture, and Crust flattens
        paths, so `aarch64` and `riscv64` versions of the same function end
        up with the same symbol. The first is kept and later duplicates
        dropped -- an arbitrary but consistent choice, and the alternative is
        linking nothing.

      * A file may reference a symbol from a part of Redox that does not
        compile. Those objects are dropped too, rather than left to fail the
        link with a message that names the symbol but not the file.

    Returns (kept, dropped) where `dropped` maps object -> reason.
    """
    if shutil.which("nm") is None:
        return objs, {}
    info = {o: symbols(o) for o in objs}
    seen = set(provided)
    for _defined, _u in info.values():
        pass
    dropped = {}
    candidates = list(objs)
    # Iterate to a fixed point. Dropping an object removes the symbols it
    # defined, which can leave a *previously kept* object with an undefined
    # reference -- that is how `KernelMapper_lock` survived the filter and
    # then failed the link. One pass is not enough; each pass can only remove
    # objects, so this terminates.
    while True:
        seen = set(provided)
        available = set(provided)
        for o in candidates:
            available |= info[o][0]
        kept, removed = [], []
        for o in candidates:
            defined, undef = info[o]
            clash = defined & seen
            if clash:
                dropped[o] = "duplicate symbol %s" % sorted(clash)[0]
                removed.append(o)
                continue
            missing = {u for u in undef if u not in available}
            if missing:
                dropped[o] = "needs %s" % sorted(missing)[0]
                removed.append(o)
                continue
            seen |= defined
            kept.append(o)
        if not removed:
            return kept, dropped
        candidates = kept


def build(roots, verbose, upstream_only):
    """Compile the upstream subset and the CrustOS shim into one binary."""
    paths = require_sources(roots)
    if paths is None:
        return 1

    print("== upstream: the Redox files Crust understands ==")
    results = [classify(p) for p in paths]
    usable = [r for r in results if r.outcome in (TRANSLATED, PARTIAL)]
    objs, causes = compile_objects(usable, os.path.join(BUILD, "upstream"),
                                   verbose)
    print("   %d of %d translatable files became objects"
          % (len(objs), len(usable)))
    if causes and verbose:
        for msg, n in causes.most_common(5):
            print("     %3d  %s" % (n, msg))
    if upstream_only:
        return 0

    # libc symbols the final link supplies anyway.
    provided = {"printf", "malloc", "free", "realloc", "memcpy", "memset",
                "abort", "exit", "strlen", "puts", "fprintf", "stderr"}
    objs, dropped = linkable_subset(objs, provided)
    if dropped:
        print("   %d dropped so the set links (%d kept)"
              % (len(dropped), len(objs)))
        if verbose:
            for o, why in sorted(dropped.items())[:10]:
                print("     %-38s %s" % (os.path.basename(o), why))

    print("\n== crustos: the parts we supply ourselves ==")
    main_src = os.path.join(SHIM, "kernel.c")
    if not os.path.exists(main_src):
        print("   missing %s" % main_src)
        return 1
    binary = os.path.join(BUILD, "crustos")
    os.makedirs(BUILD, exist_ok=True)
    # Upstream objects go on the link line. Most contribute layout constants
    # and helpers rather than entry points, which is what a kernel takes from
    # a memory manager anyway.
    # Inputs first, then `-o`: the argument parser binds a trailing option
    # before the positional list, so objects placed after `-o` are rejected.
    cmd = ([sys.executable, "-m", "shivyc.main", main_src] + objs
           + ["-o", binary])
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(binary):
        print("   FAILED: %s" % diagnostic(proc))
        return 1
    print("   linked %s  (%d upstream objects)" % (binary, len(objs)))
    return 0


def run(roots, verbose):
    rc = build(roots, verbose, False)
    if rc != 0:
        return rc
    print("\n== running ==")
    proc = subprocess.run([os.path.join(BUILD, "crustos")],
                          capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc.returncode


def clean():
    if os.path.isdir(BUILD):
        shutil.rmtree(BUILD)
        print("  removed %s" % BUILD)
    return 0


def main(argv):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode",
                    choices=("fetch", "survey", "build", "run", "clean"))
    ap.add_argument("roots", nargs="*",
                    help="source trees to use instead of the vendored ones")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--update", action="store_true",
                    help="fetch: pull even if the checkout already exists")
    ap.add_argument("--verify", action="store_true",
                    help="survey: also compile, and report how many succeed")
    ap.add_argument("--blockers", action="store_true")
    ap.add_argument("--upstream-only", action="store_true",
                    help="build: stop after the upstream objects")
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args(argv)

    if args.mode == "fetch":
        return fetch(args.update)
    if args.mode == "clean":
        return clean()

    roots = args.roots or None
    if args.mode == "survey":
        paths = require_sources(roots)
        if paths is None:
            return 1
        survey([classify(p) for p in paths], args.blockers, args.top,
               args.verify)
        return 0
    if args.mode == "build":
        return build(roots, args.verbose, args.upstream_only)
    return run(roots, args.verbose)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
