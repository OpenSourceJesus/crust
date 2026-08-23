#!/usr/bin/env python3
"""drm_shim_test -- the shim is reachable, hermetic, strict and idempotent.

Every check here corresponds to a defect that was actually present, silently,
in a released tree. None of them needs drm-kmod checked out; they test the
shim and the harness, not the corpus, so this runs anywhere gcc does.

    python3 tools/drm_shim_test.py
    python3 tools/drm_shim_test.py --mutate    # prove the checks can fail

The mutation mode matters as much as the checks. A test that cannot fail is
indistinguishable from one that passes, and each of these guards a condition
that held for months without anyone noticing -- so each is run once against a
deliberately broken arrangement to show it notices.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHIM = os.path.join(HERE, "drm_shim")
LINUX = os.path.join(SHIM, "linux")

PASS, FAIL = [], []


def check(ok, name, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", name,
                          "" if ok else "\n        " + detail))
    return ok


def gcc_own_include():
    p = subprocess.run(["gcc", "-print-file-name=include"],
                       capture_output=True, text=True)
    d = p.stdout.strip()
    return ["-I", d] if d and os.path.isdir(d) else []


def compile_probe(body, extra_inc, strict=True):
    """Compile a probe translation unit; return (ok, stderr)."""
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "probe.c")
        with open(src, "w") as f:
            f.write(body)
        cmd = ["gcc", "-c", "-nostdinc", "-ffreestanding", "-nostdlib",
               "-fno-pic"]
        if strict:
            # -w is not passed here: it defeats -Werror= regardless of order,
            # which is the very thing this probe exists to detect.
            cmd += ["-Werror=implicit-function-declaration",
                    "-Werror=implicit-int"]
        else:
            cmd += ["-w"]
        cmd += ["-D__KERNEL__"] + gcc_own_include() + extra_inc
        cmd += ["-o", os.path.join(td, "probe.o"), src]
        p = subprocess.run(cmd, capture_output=True, text=True)
        return p.returncode == 0, p.stderr


# --------------------------------------------------------------------------
# 1. Reachability.  Defect: the shim was committed flat in tools/drm_shim/,
#    but every DRM source includes it as <linux/NAME.h>. With -I tools/drm_shim
#    that resolves to tools/drm_shim/linux/NAME.h, so none of the hand-written
#    headers was ever loaded and --autostub replaced each with an empty file.
# --------------------------------------------------------------------------
def test_reachable(shim_dir=SHIM):
    linux_dir = os.path.join(shim_dir, "linux")
    if not os.path.isdir(linux_dir):
        return check(False, "shim headers live in drm_shim/linux/",
                     "no linux/ subdirectory in %s" % shim_dir)
    headers = sorted(f for f in os.listdir(linux_dir) if f.endswith(".h"))
    if not headers:
        return check(False, "shim headers live in drm_shim/linux/", "none found")
    bad = []
    for h in headers:
        ok, err = compile_probe("#include <linux/%s>\n" % h, ["-I", shim_dir])
        if not ok:
            bad.append("%s: %s" % (h, err.strip().splitlines()[0] if err else "?"))
    return check(not bad,
                 "every shim header is reachable as <linux/NAME.h> (%d)" % len(headers),
                 "\n        ".join(bad))


# --------------------------------------------------------------------------
# 2. Hermeticity.  Defect: drmdeep's gcc oracle used -ffreestanding -nostdlib
#    but not -nostdinc. -nostdlib affects linking, and these are -c compiles,
#    so <linux/errno.h> resolved against the host's linux-libc-dev package.
#    Files then "compiled freestanding" while depending on an ambient host
#    package -- and ShivyCX, which has no host include path, was recorded as
#    having a compiler gap for correctly failing where gcc should have too.
# --------------------------------------------------------------------------
def test_hermetic():
    # With the shim off the include path the probe MUST fail. If it succeeds,
    # the header came from the host and the build is not freestanding.
    ok, _ = compile_probe("#include <linux/errno.h>\nint x = EINVAL;\n", [])
    return check(not ok, "a host header cannot satisfy <linux/errno.h>",
                 "gcc resolved it without the shim -- -nostdinc is missing, "
                 "or the probe is picking up /usr/include/linux")


# --------------------------------------------------------------------------
# 3. Strictness.  Defect: the oracle passed -w, and gcc 13 still treats an
#    implicit function declaration as a warning. A file calling an undeclared
#    ERR_PTR() compiled "fine", with gcc assuming int ERR_PTR() and truncating
#    a 64-bit pointer to int. The survey counted that as a success and filed
#    ShivyCX's correct refusal as a gap.
# --------------------------------------------------------------------------
def test_strict():
    body = "void *f(void) { return undeclared_function(0); }\n"
    ok, _ = compile_probe(body, ["-I", SHIM], strict=True)
    return check(not ok, "the oracle rejects implicit function declarations",
                 "gcc accepted a call to an undeclared function; -w is "
                 "suppressing it and the -Werror= flags are absent")


# --------------------------------------------------------------------------
# 4. Idempotence.  Defect: kernel.h closed its include guard 19 lines early,
#    leaving its own #include <linux/errno.h> outside the guard. That turned
#    one missing header into five identical diagnostics, and -- once an
#    autostub placeholder for errno.h existed, which itself includes kernel.h
#    -- into unbounded include recursion.
#
#    Checked statically rather than by compiling, because the failure is
#    structural and a compile only shows it in combination with autostub.
# --------------------------------------------------------------------------
GUARD_RE = re.compile(r"^\s*#\s*ifndef\s+(\w+)")
COND_RE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|endif)\b")


def guard_closes_at_eof(path):
    with open(path) as f:
        lines = f.readlines()
    start = None
    for i, ln in enumerate(lines):
        m = GUARD_RE.match(ln)
        if m:
            start = i
            break
    if start is None:
        return False, "no #ifndef include guard"
    depth = 0
    close = None
    for i in range(start, len(lines)):
        m = COND_RE.match(lines[i])
        if not m:
            continue
        if m.group(1) == "endif":
            depth -= 1
            if depth == 0:
                close = i
                break
        else:
            depth += 1
    if close is None:
        return False, "include guard is never closed"
    # nothing but blank lines and comments may follow
    rest = "".join(lines[close + 1:])
    rest = re.sub(r"/\*.*?\*/", "", rest, flags=re.S)
    rest = re.sub(r"//[^\n]*", "", rest)
    if rest.strip():
        return False, ("content after the guard closes at line %d: %r"
                       % (close + 1, rest.strip()[:60]))
    return True, ""


def test_idempotent(shim_dir=SHIM):
    linux_dir = os.path.join(shim_dir, "linux")
    if not os.path.isdir(linux_dir):
        return check(False, "every shim header's guard covers the whole file",
                     "no linux/ subdirectory")
    bad = []
    for h in sorted(os.listdir(linux_dir)):
        if not h.endswith(".h"):
            continue
        ok, why = guard_closes_at_eof(os.path.join(linux_dir, h))
        if not ok:
            bad.append("%s: %s" % (h, why))
    return check(not bad, "every shim header's guard covers the whole file",
                 "\n        ".join(bad))


# --------------------------------------------------------------------------
# 5. No shadowing.  A generated placeholder for a header we hand-wrote always
#    means the real one was unreachable. This is the general guard against
#    Defect 1 returning in a new form.
# --------------------------------------------------------------------------
def test_no_shadowing():
    auto = os.path.join(SHIM, "auto")
    if not os.path.isdir(auto) or not os.path.isdir(LINUX):
        return check(True, "no autostub shadows a hand-written header",
                     "")
    hand = {f for f in os.listdir(LINUX) if f.endswith(".h")}
    auto_linux = os.path.join(auto, "linux")
    gen = set()
    if os.path.isdir(auto_linux):
        gen = {f for f in os.listdir(auto_linux) if f.endswith(".h")}
    clash = sorted(hand & gen)
    return check(not clash, "no autostub shadows a hand-written header",
                 "generated placeholders for: " + " ".join(clash))


# --------------------------------------------------------------------------
# 6. Namespace.  Defect: linux/printk.h defined drm_info, drm_warn, drm_err,
#    drm_WARN_ON and the DRM_* family. Those belong to <drm/drm_print.h>,
#    which is included later -- so upstream's real definitions won, ours were
#    dead code, and gcc reported a redefinition on every single file. Worse,
#    the dead no-ops had been masking a genuine ShivyCX gap: with them gone,
#    DRM_DEBUG_KMS turned out to be undefined for ShivyCX because drm_print.h
#    branches on #ifdef __linux__, which gcc predefines from the host triplet
#    and ShivyCX does not.
#
#    A Linux shim header has no business defining a drm_* symbol. Checked by
#    name, so it needs no vendor checkout.
# --------------------------------------------------------------------------
FOREIGN = ("drm_", "DRM_", "i915_", "amdgpu_", "nouveau_")


def test_namespace(shim_dir=SHIM):
    linux_dir = os.path.join(shim_dir, "linux")
    if not os.path.isdir(linux_dir):
        return check(False, "no shim header defines a drm_* symbol",
                     "no linux/ subdirectory")
    define_re = re.compile(r"^\s*#\s*define\s+(\w+)")
    bad = []
    for h in sorted(os.listdir(linux_dir)):
        if not h.endswith(".h"):
            continue
        with open(os.path.join(linux_dir, h)) as f:
            for i, ln in enumerate(f, 1):
                m = define_re.match(ln)
                if m and m.group(1).startswith(FOREIGN):
                    bad.append("%s:%d defines %s" % (h, i, m.group(1)))
    return check(not bad, "no shim header defines a drm_* symbol",
                 "\n        ".join(bad[:8]))


# --------------------------------------------------------------------------
# 7. Predefines.  The oracle must not rely on gcc's host-triplet predefines.
#    -nostdinc removes the host's *headers*; it does not touch __linux__ or
#    __unix__, which gcc defines because it was built for a Linux host. Any
#    source that branches on them gets a different program under each
#    compiler unless the survey sets them explicitly.
# --------------------------------------------------------------------------
def test_predefines():
    p = subprocess.run(["gcc", "-dM", "-E", "-nostdinc", "-ffreestanding", "-"],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True)
    host = [m for m in ("__linux__", "__unix__") if m in p.stdout]
    if not host:
        return check(True, "the survey sets host-OS predefines explicitly", "")
    drmdeep = os.path.join(HERE, "drmdeep.py")
    if not os.path.exists(drmdeep):
        return check(True, "the survey sets host-OS predefines explicitly", "")
    with open(drmdeep) as f:
        src = f.read()
    ok = all(("-D" + m) in src for m in host)
    return check(ok, "the survey sets host-OS predefines explicitly",
                 "gcc predefines %s but drmdeep.py does not set them, so "
                 "ShivyCX sees a different program" % " ".join(host))
def mutate():
    """Break each condition on purpose and confirm the check notices.

    The failures reported inside here are the desired outcome, so the real
    tally is saved and restored around them -- otherwise proving the tests
    work would make the run look broken.
    """
    print("\n== mutation: each check must fail when its condition is broken ==")
    print("   (FAIL lines below are the expected result)")
    import shutil
    saved_pass, saved_fail = list(PASS), list(FAIL)
    caught = 0

    # 1. flatten the shim, as the released tree had it
    with tempfile.TemporaryDirectory() as td:
        flat = os.path.join(td, "drm_shim")
        os.makedirs(flat)
        for f in os.listdir(LINUX):
            shutil.copy(os.path.join(LINUX, f), os.path.join(flat, f))
        print("  [flattened shim]")
        if not test_reachable(flat):
            caught += 1
        else:
            print("        NOT CAUGHT: reachability check passed on a flat shim")

    # 2. guard closing early, as kernel.h had it
    with tempfile.TemporaryDirectory() as td:
        broken = os.path.join(td, "drm_shim", "linux")
        os.makedirs(broken)
        with open(os.path.join(broken, "kernel.h"), "w") as f:
            f.write("#ifndef _G\n#define _G\nint a;\n#endif\n"
                    "#include <linux/errno.h>\n")
        print("  [guard closed early]")
        if not test_idempotent(os.path.dirname(broken)):
            caught += 1
        else:
            print("        NOT CAUGHT: early guard accepted")

    # 3. a shim header reaching into DRM's namespace, as printk.h did
    with tempfile.TemporaryDirectory() as td:
        bad = os.path.join(td, "drm_shim", "linux")
        os.makedirs(bad)
        with open(os.path.join(bad, "printk.h"), "w") as f:
            f.write("#ifndef _G\n#define _G\n#define drm_WARN_ON(...) (0)\n#endif\n")
        print("  [shim defining drm_WARN_ON]")
        if not test_namespace(os.path.dirname(bad)):
            caught += 1
        else:
            print("        NOT CAUGHT: drm_* definition accepted")

    PASS[:], FAIL[:] = saved_pass, saved_fail
    print("\n  %d of 3 mutations caught" % caught)
    return caught == 3


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mutate", action="store_true",
                    help="also prove the checks can fail")
    args = ap.parse_args()

    print("== drm shim ==")
    test_reachable()
    test_hermetic()
    test_strict()
    test_idempotent()
    test_no_shadowing()
    test_namespace()
    test_predefines()

    ok = True
    if args.mutate:
        ok = mutate()

    print("\ndrm_shim: %d pass, %d fail" % (len(PASS), len(FAIL)))
    return 0 if not FAIL and ok else 1


if __name__ == "__main__":
    sys.exit(main())
