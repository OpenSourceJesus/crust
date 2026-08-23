"""drmdeep -- how much of DRM's generic layer can Crust actually compile?

The same question `tools/crustos.py survey` asks of the Redox kernel, asked of
[drm-kmod](https://github.com/freebsd/drm-kmod). The answer decides what a
`gpu:` scheme can be built from (see GPU.md).

The interesting part of DRM, for us, is not amdgpu or i915 -- those assume a
Linux kernel underneath and would need firmware, IOMMU and interrupt handling
we do not have. It is the generic layer: rectangle clipping, pixel formats,
VRAM allocators, mode timing math, EDID parsing. Those are self-contained
algorithms with only cosmetic ties to Linux, and `tools/drm_shim/` is the
~100 lines of headers that stand in for those ties.

    python3 tools/drmdeep.py fetch          # clone drm-kmod into vendor/
    python3 tools/drmdeep.py survey         # what compiles, and what stops the rest
    python3 tools/drmdeep.py survey -v      # name every file
    python3 tools/drmdeep.py blockers       # rank the failure causes
    python3 tools/drmdeep.py build FILE...  # compile specific files

Two compilers are used deliberately. gcc answers "is this file portable at
all", ShivyCX answers "can *our* toolchain build it". Reporting both keeps the
two failure modes apart: a file gcc also rejects is a shim gap, while a file
gcc takes and ShivyCX does not is a compiler gap worth fixing.
"""
import argparse
import collections
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.environ.get("DRM_VENDOR", os.path.join(ROOT, "vendor", "drm-kmod"))
SHIM = os.path.join(ROOT, "tools", "drm_shim")
BUILD = os.environ.get("DRM_BUILD", os.path.join(ROOT, "build", "drm"))
REPO_URL = "https://github.com/freebsd/drm-kmod.git"

# The generic layer. Driver directories (amd/, i915/, radeon/) are excluded on
# purpose: they are hardware-specific and depend on the whole kernel.
CORE_DIR = os.path.join("drivers", "gpu", "drm")

# Files worth trying first -- the ones GPU.md identifies as the portable slice.
PRIORITY = [
    "drm_rect.c", "drm_fourcc.c", "drm_displayid.c", "drm_dumb_buffers.c",
    "drm_blend.c", "drm_color_mgmt.c", "drm_mm.c", "drm_buddy.c",
    "drm_modes.c", "drm_edid.c",
]

CREATED = []      # headers autostub() generated during this run

OK = "ok"
FAIL_SHIM = "shim"          # gcc could not build it either
FAIL_SHIVYC = "shivyc"      # gcc built it, ShivyCX did not


class Result(object):
    def __init__(self, path, outcome, gcc_err="", shivyc_err="", syms=None):
        self.path = path
        self.outcome = outcome
        self.gcc_err = gcc_err
        self.shivyc_err = shivyc_err
        self.syms = syms or []


AUTO = os.path.join(SHIM, "auto")

MISSING_RE = re.compile(r"fatal error: ([^:]+): No such file")


def autostub(text):
    """Create a placeholder for the first missing header, if there is one.

    The hand-written shim covers the headers whose *contents* matter (list.h
    needs real list operations, bitfield.h real arithmetic). Beyond those there
    is a long tail of headers a freestanding build only needs to exist -- and
    generating them mechanically is both faster and more honest than writing
    fifty stubs by hand and implying they were considered.

    An empty stub cannot silently corrupt anything: if the file really did
    define something the code uses, the next compile fails on that identifier
    instead, which shows up in the survey as a named error.
    """
    m = MISSING_RE.search(re.sub(r"\x1b\[[0-9;]*m", "", text))
    if not m:
        return None
    header = m.group(1)
    if ".." in header or header.startswith("/"):
        return None
    # A placeholder for a header we hand-wrote is always a bug: it means the
    # real one is not reachable at the path the sources include it by, and
    # stubbing over it would replace (say) a real intrusive-list
    # implementation with an empty file and report that as progress. Say so
    # instead of doing it.
    if os.path.exists(os.path.join(SHIM, header)):
        raise SystemExit(
            "drmdeep: refusing to autostub <%s>: a hand-written shim header\n"
            "         exists at %s but was not found on the include path.\n"
            "         The shim is misplaced, not missing."
            % (header, os.path.join(SHIM, header)))
    path = os.path.join(AUTO, header)
    if os.path.exists(path):
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("/* auto-generated placeholder for <%s> (tools/drmdeep.py "
                "--autostub) */\n#include <linux/kernel.h>\n" % header)
    return header


def includes():
    return ["-I", SHIM, "-I", AUTO,
            "-I", os.path.join(VENDOR, "include"),
            "-I", os.path.join(VENDOR, "include", "uapi")]


def fetch(update):
    if not os.path.isdir(os.path.join(VENDOR, ".git")):
        os.makedirs(os.path.dirname(VENDOR), exist_ok=True)
        print("  clone %s" % REPO_URL)
        p = subprocess.run(["git", "clone", "--depth", "1", REPO_URL, VENDOR],
                           capture_output=True, text=True)
    elif update:
        print("  pull  %s" % VENDOR)
        p = subprocess.run(["git", "-C", VENDOR, "pull", "--ff-only"],
                           capture_output=True, text=True)
    else:
        print("  have  %s" % VENDOR)
        return 0
    if p.returncode != 0:
        tail = (p.stderr or "").strip().splitlines()
        print("  FAILED: %s" % (tail[-1] if tail else "?"))
        return 1
    return 0


def sources():
    d = os.path.join(VENDOR, CORE_DIR)
    if not os.path.isdir(d):
        return []
    names = sorted(f for f in os.listdir(d) if f.endswith(".c"))
    # priority files first, so a truncated run still says something useful
    head = [n for n in PRIORITY if n in names]
    tail = [n for n in names if n not in PRIORITY]
    return [os.path.join(d, n) for n in head + tail]


def first_error(text):
    """The first real error line, trimmed to something readable."""
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    for line in plain.splitlines():
        if "error:" in line:
            msg = line.split("error:", 1)[1].strip()
            return msg[:90]
    for line in plain.splitlines():
        if line.strip():
            return line.strip()[:90]
    return "?"


def gcc_own_include():
    """gcc's own freestanding headers (stddef.h, stdarg.h).

    -nostdinc removes these along with the host's, but they are part of the
    compiler rather than the distribution: a freestanding target is entitled
    to them. Handing them back explicitly keeps the difference between "the
    compiler provides it" and "the host distribution happened to provide it".
    """
    p = subprocess.run(["gcc", "-print-file-name=include"],
                       capture_output=True, text=True)
    d = p.stdout.strip()
    return ["-I", d] if d and os.path.isdir(d) else []


def build_gcc(path, obj):
    # -nostdinc is load-bearing. Without it, -ffreestanding and -nostdlib do
    # NOT remove the host include path: -nostdlib affects linking, and these
    # are -c compiles. #include <linux/errno.h> then resolves against
    # /usr/include/linux/errno.h from the distribution's linux-libc-dev, so a
    # file can appear to build freestanding while depending on an ambient host
    # package -- and ShivyCX, which has no such path, gets filed as having a
    # compiler gap for correctly failing where gcc should have failed too.
    #
    # The two -Werror= flags are load-bearing for the same reason. gcc 13
    # still treats an implicit function declaration as a warning, so a file
    # calling an undeclared ERR_PTR() compiles "fine" -- with gcc assuming
    # int ERR_PTR(), truncating a 64-bit pointer to int and emitting wrong
    # code. ShivyCX rejects it, correctly, and without these flags the survey
    # records that correctness as a ShivyCX gap.
    #
    # -w is deliberately NOT passed. It defeats -Werror= regardless of the
    # order the two appear in, which would leave the strictness silently
    # inert. Warnings are ignored instead: first_error() reads "error:" lines
    # and the verdict comes from the exit status.
    cmd = (["gcc", "-c", "-nostdinc", "-ffreestanding", "-nostdlib",
            "-fno-pic", "-Werror=implicit-function-declaration",
            "-Werror=implicit-int", "-D__KERNEL__"] + gcc_own_include()
           + includes() + ["-o", obj, path])
    p = subprocess.run(cmd, capture_output=True, text=True)
    return (p.returncode == 0 and os.path.exists(obj), p)


def build_shivyc(path, obj):
    env = dict(os.environ)
    env["SHIVYC_RASM"] = "1"
    env.pop("SHIVYC_RLINK", None)
    cmd = ([sys.executable, "-m", "shivyc.main", "-c", path, "-o", obj,
            "-D__KERNEL__"] + includes())
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       env=env)
    return (os.path.exists(obj), p)


def defined_symbols(obj):
    p = subprocess.run(["nm", "-g", obj], capture_output=True, text=True)
    out = []
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] in ("T", "D", "B", "R"):
            out.append(parts[2])
    return out


def classify(path, auto=False):
    os.makedirs(BUILD, exist_ok=True)
    base = os.path.basename(path)[:-2]
    gobj = os.path.join(BUILD, base + ".gcc.o")
    sobj = os.path.join(BUILD, base + ".o")
    for f in (gobj, sobj):
        if os.path.exists(f):
            os.remove(f)

    gok, gp = build_gcc(path, gobj)
    if not gok and auto:
        # generate placeholders for missing headers until something else stops
        for _ in range(60):
            made = autostub(gp.stderr + gp.stdout)
            if made is None:
                break
            CREATED.append(made)
            gok, gp = build_gcc(path, gobj)
            if gok:
                break
    if not gok:
        return Result(path, FAIL_SHIM,
                      gcc_err=first_error(gp.stderr + gp.stdout))
    sok, sp = build_shivyc(path, sobj)
    if not sok:
        return Result(path, FAIL_SHIVYC,
                      shivyc_err=first_error(sp.stderr + sp.stdout))
    return Result(path, OK, syms=defined_symbols(sobj))


def survey(verbose, limit, only_priority, auto=False):
    paths = sources()
    if not paths:
        print("no drm-kmod sources under %s" % VENDOR)
        print("run:  python3 tools/drmdeep.py fetch")
        return 1
    if only_priority:
        paths = [p for p in paths if os.path.basename(p) in PRIORITY]
    if limit:
        paths = paths[:limit]

    results = []
    for p in paths:
        r = classify(p, auto)
        results.append(r)
        if verbose or r.outcome == OK:
            name = os.path.basename(r.path)
            if r.outcome == OK:
                print("  ok    %-26s %d symbols" % (name, len(r.syms)))
            elif r.outcome == FAIL_SHIVYC:
                print("  SHIVYC %-25s %s" % (name, r.shivyc_err))
            else:
                print("  shim  %-26s %s" % (name, r.gcc_err))

    ok = [r for r in results if r.outcome == OK]
    shim = [r for r in results if r.outcome == FAIL_SHIM]
    shivyc = [r for r in results if r.outcome == FAIL_SHIVYC]
    lines = 0
    for r in ok:
        with open(r.path) as f:
            lines += sum(1 for _ in f)

    if CREATED:
        print("\n  generated %d placeholder headers: %s"
              % (len(CREATED), " ".join(sorted(set(CREATED))[:8])))
    print("\n== drm generic layer, through the Crust toolchain ==")
    print("  %d of %d files compile        (%d lines, %d symbols)"
          % (len(ok), len(results), lines, sum(len(r.syms) for r in ok)))
    print("  %d need more shim             (gcc rejects them too)" % len(shim))
    print("  %d are ShivyCX gaps           (gcc accepts them)" % len(shivyc))
    if shivyc:
        print("\n  ShivyCX gaps are the actionable ones:")
        for r in shivyc[:10]:
            print("    %-26s %s" % (os.path.basename(r.path), r.shivyc_err))
    return 0


def blockers(top):
    paths = sources()
    if not paths:
        print("no sources; run `fetch` first")
        return 1
    causes = collections.Counter()
    kinds = collections.Counter()
    for p in paths:
        r = classify(p)
        kinds[r.outcome] += 1
        if r.outcome == FAIL_SHIM:
            causes["[shim]   " + normalise(r.gcc_err)] += 1
        elif r.outcome == FAIL_SHIVYC:
            causes["[shivyc] " + normalise(r.shivyc_err)] += 1
    print("\n== what stops the rest ==")
    for msg, n in causes.most_common(top):
        print("  %3d  %s" % (n, msg))
    print("\n  (%d compile, %d shim gaps, %d ShivyCX gaps)"
          % (kinds[OK], kinds[FAIL_SHIM], kinds[FAIL_SHIVYC]))
    return 0


def normalise(msg):
    """Collapse the identifier out of a message so causes group together."""
    m = re.sub(r"'[^']*'", "'X'", msg)
    m = re.sub(r"\d+", "N", m)
    return m[:70]


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")

    f = sub.add_parser("fetch", help="clone drm-kmod into vendor/")
    f.add_argument("--update", action="store_true")

    s = sub.add_parser("survey", help="what compiles")
    s.add_argument("-v", "--verbose", action="store_true")
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--priority", action="store_true",
                   help="only the portable slice named in GPU.md")
    s.add_argument("--autostub", action="store_true",
                   help="generate placeholders for missing headers")

    b = sub.add_parser("blockers", help="rank the failure causes")
    b.add_argument("--top", type=int, default=12)

    bl = sub.add_parser("build", help="compile specific files")
    bl.add_argument("files", nargs="+")

    args = ap.parse_args(argv[1:])
    if args.cmd == "fetch":
        return fetch(args.update)
    if args.cmd == "blockers":
        return blockers(args.top)
    if args.cmd == "build":
        rc = 0
        for name in args.files:
            path = name
            if not os.path.exists(path):
                path = os.path.join(VENDOR, CORE_DIR, name)
            r = classify(path)
            if r.outcome == OK:
                print("  ok    %-26s %s" % (os.path.basename(path),
                                            " ".join(r.syms[:6])))
            else:
                print("  FAIL  %-26s %s" % (os.path.basename(path),
                                            r.gcc_err or r.shivyc_err))
                rc = 1
        return rc
    return survey(getattr(args, "verbose", False),
                  getattr(args, "limit", 0),
                  getattr(args, "priority", False),
                  getattr(args, "autostub", False))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
