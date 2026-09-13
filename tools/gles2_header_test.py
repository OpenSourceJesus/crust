#!/usr/bin/env python3
"""Lock down Crust's GLES2 support.

Crust does not read /usr/include, so <GLES2/gl2.h> and <EGL/egl.h> in
shivyc/include are our own declarations of somebody else's ABI. Nothing about
that arrangement is self-checking: a constant with the wrong value or a
parameter of the wrong width compiles perfectly, links perfectly, and then
asks the driver to do something other than what the program said. There is no
error to see -- glClear with a wrong bit just clears the wrong buffer.

So this checks three separate things, because they fail in different ways:

  constants   Every GLES2/EGL constant we define is compared, by value, to
              the Khronos header -- when one is installed. Compared by
              *compiling* both rather than by reading the text, so a macro
              that expands differently than it looks is still caught.

  prototypes  Every entry point Khronos declares is declared by us with the
              same parameter types, and none of ours is missing or invented.

  render      The example is built twice -- once by gcc, once by ShivyCX,
              both against *our* headers -- and the two must produce
              byte-identical pixels. This is the only check that runs a line
              of GL: a header can pass the first two and still be wrong about
              something the compiler and the driver disagree on, such as a
              struct-by-value rule or an argument-promotion width.

The constant and prototype checks need the Khronos headers (libgles-dev);
the render check needs Mesa with a software rasteriser. Each skips with a
clear reason rather than silently passing when its prerequisite is absent,
because a check that quietly does nothing is worse than no check.

    python3 tools/gles2_header_test.py
    python3 tools/gles2_header_test.py --mutate   # prove it can fail
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OURS = os.path.join(ROOT, "shivyc", "include")
EXAMPLE = os.path.join(ROOT, "examples", "gles2", "triangle.c")

SYS_GL2 = "/usr/include/GLES2/gl2.h"
SYS_EGL = "/usr/include/EGL/egl.h"

# Macros that are plumbing rather than API values: they expand to calling
# conventions or to nothing, so there is no value to compare.
NOT_VALUES = {
    "GL_APICALL", "GL_APIENTRY", "GL_APIENTRYP", "GL_GLES_PROTOTYPES",
    "EGLAPI", "EGLAPIENTRY", "EGLAPIENTRYP", "EGL_CAST",
}


class Result:
    def __init__(self):
        self.failures = []
        self.skipped = []
        self.checked = 0

    def fail(self, msg):
        self.failures.append(msg)

    def skip(self, what, why):
        self.skipped.append("%s (%s)" % (what, why))


def defines_in(path):
    """Object-like macro names defined by a header, in file order."""
    out = []
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return out
    for m in re.finditer(r'^\s*#\s*define\s+((?:GL|EGL)_\w+)(?!\()\s+\S', text,
                         re.M):
        name = m.group(1)
        if name not in NOT_VALUES and name not in out:
            out.append(name)
    return out


def prototypes_in(path, kind):
    """Map entry-point name -> normalised (return type, [param types])."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return {}

    if kind == "gl":
        pat = r'^\s*GL_APICALL\s+(.+?)\s*GL_APIENTRY\s+(gl\w+)\s*\((.*?)\)\s*;'
    else:
        pat = r'^\s*EGLAPI\s+(.+?)\s*EGLAPIENTRY\s+(egl\w+)\s*\((.*?)\)\s*;'

    out = {}
    for m in re.finditer(pat, text, re.M | re.S):
        out[m.group(2)] = (norm_type(m.group(1)),
                           split_params(m.group(3)))
    return out


def ours_prototypes(path, prefix):
    """Same, for our headers, which use no GL_APICALL/EGLAPI decoration."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return {}
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)
    out = {}
    pat = (r'^\s*((?:const\s+)?\w[\w\s*]*?)\s*\**\s*(' + prefix +
           r'\w+)\s*\((.*?)\)\s*;')
    for m in re.finditer(pat, text, re.M | re.S):
        ret = m.group(1)
        # Put any pointer stars back on the return type.
        star = text[m.start(1):m.start(2)].count("*")
        out[m.group(2)] = (norm_type(ret + " *" * star),
                           split_params(m.group(3)))
    return out


def norm_type(t):
    """Collapse spelling differences that do not change the type."""
    t = re.sub(r'\s+', ' ', t).strip()
    t = t.replace("* ", "*").replace(" *", "*")
    # Khronos writes `const GLchar *const*`; whitespace aside these agree.
    t = re.sub(r'\bconst\b\s*', 'const ', t)
    return re.sub(r'\s+', ' ', t).strip()


def split_params(s):
    """Parameter *types*, with names dropped -- names are documentation."""
    s = re.sub(r'\s+', ' ', s).strip()
    if s in ("", "void"):
        return []
    out = []
    for p in s.split(","):
        p = p.strip()
        # Drop a trailing identifier that is not part of the type.
        p = re.sub(r'\b\w+\s*(\[\s*\])?$', lambda m: (m.group(1) or ""), p, 1) \
            if re.search(r'\b[A-Za-z_]\w*\s*(\[\s*\])?$', p) else p
        out.append(norm_type(p))
    return out


def cc_values(header_line, names, extra_inc, cc="gcc"):
    """Compile a probe that prints each macro's value; return name -> value.

    Values are printed by the compiler rather than parsed out of the header,
    so `(GLenum)0x1F00` and `0x1F00` compare equal and a macro defined in
    terms of another is resolved.
    """
    src = ["#include <stdio.h>", header_line, "int main(void){"]
    for n in names:
        src.append('  printf("%s=%lld\\n", "{0}", (long long)({0}));'
                   .format(n))
    src.append("  return 0; }")

    with tempfile.TemporaryDirectory() as td:
        c = os.path.join(td, "probe.c")
        exe = os.path.join(td, "probe")
        open(c, "w").write("\n".join(src) + "\n")
        cmd = [cc, c, "-o", exe, "-w"]
        for d in extra_inc:
            cmd += ["-I", d]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return None, r.stderr.strip()[:400]
        r = subprocess.run([exe], capture_output=True, text=True)
        vals = {}
        for line in r.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                vals[k] = int(v)
        return vals, None


def check_values(res, ours_header, sys_header, include_line, label):
    if not os.path.exists(sys_header):
        res.skip("%s constants" % label,
                 "%s not installed" % sys_header)
        return

    ours_names = set(defines_in(ours_header))
    sys_names = set(defines_in(sys_header))
    shared = sorted(ours_names & sys_names)
    if not shared:
        res.fail("%s: no constants in common with %s -- "
                 "is the parse working?" % (label, sys_header))
        return

    ours_vals, err = cc_values(include_line, shared, [OURS])
    if ours_vals is None:
        res.fail("%s: our header will not compile under gcc: %s" % (label, err))
        return
    sys_vals, err = cc_values(include_line, shared, [])
    if sys_vals is None:
        res.skip("%s constants" % label, "system header probe failed: %s" % err)
        return

    for n in shared:
        res.checked += 1
        if n not in ours_vals or n not in sys_vals:
            continue
        if ours_vals[n] != sys_vals[n]:
            res.fail("%s: %s is 0x%X for us, 0x%X upstream"
                     % (label, n, ours_vals[n], sys_vals[n]))

    # Anything upstream defines that we do not is a gap, not an error --
    # report it as information so the header's coverage is visible.
    missing = sorted(sys_names - ours_names)
    if missing:
        print("    note: %d upstream %s constants not declared: %s%s"
              % (len(missing), label, ", ".join(missing[:6]),
                 " ..." if len(missing) > 6 else ""))


def check_prototypes(res, ours_header, sys_header, kind, prefix, label):
    if not os.path.exists(sys_header):
        res.skip("%s prototypes" % label, "%s not installed" % sys_header)
        return

    upstream = prototypes_in(sys_header, kind)
    mine = ours_prototypes(ours_header, prefix)
    if not upstream:
        res.fail("%s: parsed no prototypes from %s" % (label, sys_header))
        return
    if not mine:
        res.fail("%s: parsed no prototypes from %s" % (label, ours_header))
        return

    for name, (ret, params) in sorted(upstream.items()):
        if name not in mine:
            # Only gl2.h is meant to be exhaustive; egl.h is a stated subset.
            if kind == "gl":
                res.fail("%s: %s is not declared" % (label, name))
            continue
        res.checked += 1
        my_ret, my_params = mine[name]
        if my_ret != ret:
            res.fail("%s: %s returns %s for us, %s upstream"
                     % (label, name, my_ret, ret))
        if my_params != params:
            res.fail("%s: %s takes (%s) for us, (%s) upstream"
                     % (label, name, ", ".join(my_params), ", ".join(params)))

    invented = sorted(set(mine) - set(upstream))
    if invented:
        res.fail("%s: declares entry points upstream does not have: %s"
                 % (label, ", ".join(invented)))


def check_self_contained(res):
    """Our headers must not reach for a system header.

    The whole premise is that Crust reads only its own include directory. A
    stray <KHR/khrplatform.h> would compile here, because gcc can find it,
    and fail under ShivyCX, which cannot.
    """
    for rel in ("GLES2/gl2.h", "EGL/egl.h"):
        path = os.path.join(OURS, rel)
        if not os.path.exists(path):
            res.fail("missing header: %s" % rel)
            continue
        text = open(path, encoding="utf-8").read()
        for m in re.finditer(r'^\s*#\s*include\s*<([^>]+)>', text, re.M):
            inc = m.group(1)
            res.checked += 1
            if not os.path.exists(os.path.join(OURS, inc)):
                res.fail("%s includes <%s>, which is not one of ours"
                         % (rel, inc))


def run_render(res, mutate_flag=None):
    """Build the example with gcc and with ShivyCX; compare the pixels."""
    if not os.path.exists(EXAMPLE):
        res.skip("render difftest", "example not present")
        return
    if not shutil.which("gcc"):
        res.skip("render difftest", "no gcc for the oracle")
        return

    env = dict(os.environ)
    env["EGL_PLATFORM"] = "surfaceless"
    env["LIBGL_ALWAYS_SOFTWARE"] = "1"

    with tempfile.TemporaryDirectory() as td:
        gcc_exe = os.path.join(td, "gcc_tri")
        our_exe = os.path.join(td, "our_tri")
        gcc_ppm = os.path.join(td, "gcc.ppm")
        our_ppm = os.path.join(td, "our.ppm")

        r = subprocess.run(["gcc", EXAMPLE, "-o", gcc_exe, "-I", OURS,
                            "-w", "-lEGL", "-lGLESv2"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            res.fail("gcc could not build the example against our headers: %s"
                     % r.stderr.strip()[:400])
            return

        cmd = [sys.executable, "crust", EXAMPLE, "-o", our_exe,
               "-lEGL", "-lGLESv2"]
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        if not os.path.exists(our_exe):
            res.fail("ShivyCX could not build the example: %s"
                     % (r.stderr or r.stdout).strip()[:400])
            return

        rg = subprocess.run([gcc_exe, gcc_ppm], capture_output=True, env=env)
        if rg.returncode != 0:
            res.skip("render difftest",
                     "no working GLES2 driver: %s"
                     % rg.stderr.decode(errors="replace").strip()[:200])
            return
        ro = subprocess.run([our_exe, our_ppm], capture_output=True, env=env)
        if ro.returncode != 0:
            res.fail("the ShivyCX build failed at run time: %s"
                     % ro.stderr.decode(errors="replace").strip()[:300])
            return

        res.checked += 1
        a = open(gcc_ppm, "rb").read()
        b = open(our_ppm, "rb").read()
        if mutate_flag == "render":
            b = b[:-1] + bytes([b[-1] ^ 0xFF])
        if a != b:
            diff = sum(1 for x, y in zip(a, b) if x != y)
            res.fail("render differs from the gcc oracle in %d of %d bytes"
                     % (diff, len(a)))
            return

        # A blank image would compare equal and prove nothing.
        body = a.split(b"255\n", 1)[-1]
        if len(set(body)) < 3:
            res.fail("both builds rendered a blank image -- "
                     "the comparison is vacuous")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mutate", action="store_true",
                    help="inject known defects and confirm each is caught")
    args = ap.parse_args()

    if args.mutate:
        return run_mutations()

    res = Result()
    print("GLES2 header and render checks")

    check_self_contained(res)
    check_values(res, os.path.join(OURS, "GLES2", "gl2.h"), SYS_GL2,
                 "#include <GLES2/gl2.h>", "gl2.h")
    check_values(res, os.path.join(OURS, "EGL", "egl.h"), SYS_EGL,
                 "#include <EGL/egl.h>", "egl.h")
    check_prototypes(res, os.path.join(OURS, "GLES2", "gl2.h"), SYS_GL2,
                     "gl", "gl", "gl2.h")
    check_prototypes(res, os.path.join(OURS, "EGL", "egl.h"), SYS_EGL,
                     "egl", "egl", "egl.h")
    run_render(res)

    print()
    for s in res.skipped:
        print("  skip  %s" % s)
    for f in res.failures:
        print("  FAIL  %s" % f)
    print("\n  %d checks, %d failures, %d skipped"
          % (res.checked, len(res.failures), len(res.skipped)))
    return 1 if res.failures else 0


def run_mutations():
    """A test that cannot fail is not a test. Break things on purpose."""
    print("mutation testing\n")
    gl2 = os.path.join(OURS, "GLES2", "gl2.h")
    original = open(gl2, encoding="utf-8").read()
    passed = 0
    total = 0

    mutations = [
        # Keep the name, change the value -- renaming it would only prove the
        # coverage note fires, which is not the check under test.
        ("a constant with the wrong value",
         lambda t: re.sub(r'(#define GL_TRIANGLES\s+)0x0004', r'\g<1>0x0005',
                          t)),
        ("a constant defined via another macro, wrongly",
         lambda t: re.sub(r'(#define GL_COLOR_BUFFER_BIT\s+)0x00004000',
                          r'\g<1>(0x00004000 | 1)', t)),
        ("a missing entry point",
         lambda t: t.replace("void glDrawArrays(GLenum mode, GLint first, "
                             "GLsizei count);", "")),
        ("a parameter of the wrong type",
         lambda t: t.replace("void glClear(GLbitfield mask);",
                             "void glClear(GLint mask);")),
        ("a header reaching outside our include dir",
         lambda t: t.replace("#ifndef _GLES2_GL2_H",
                             "#ifndef _GLES2_GL2_H\n#include <KHR/khrplatform.h>")),
    ]

    try:
        for name, mutate in mutations:
            total += 1
            broken = mutate(original)
            if broken == original:
                print("  ????  %-46s mutation did not apply" % name)
                continue
            open(gl2, "w", encoding="utf-8").write(broken)
            res = Result()
            check_self_contained(res)
            check_values(res, gl2, SYS_GL2, "#include <GLES2/gl2.h>", "gl2.h")
            check_prototypes(res, gl2, SYS_GL2, "gl", "gl", "gl2.h")
            if res.failures:
                passed += 1
                print("  caught  %-44s %s" % (name, res.failures[0][:60]))
            else:
                print("  MISSED  %s" % name)
    finally:
        open(gl2, "w", encoding="utf-8").write(original)

    print("\n  %d of %d mutations caught" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
