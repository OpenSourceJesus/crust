#!/usr/bin/env python3
"""Check shivyc/include/GLES3/gl31.h against Khronos's GLES3/gl31.h.

Like GLES2/gl2.h, our gl31.h is a declaration of somebody else's ABI, and a
wrong constant or parameter type compiles, links and then asks the driver
for something else. So one translation unit includes *Khronos's* header and
then states ours: each of our constants must equal theirs
(`_Static_assert`), and each of our prototypes is redeclared -- gcc rejects
a redeclaration whose types differ.

Khronos's headers come from libgles-dev (/usr/include) or from
$CRUST_KHRONOS_INCLUDE (a directory with GLES3/, KHR/ -- fetched from
KhronosGroup/OpenGL-Registry and EGL-Registry). Without them this skips,
saying so.

    python3 tools/gles3_header_test.py
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OURS = os.path.join(ROOT, "shivyc", "include", "GLES3", "gl31.h")


def khronos_dir():
    env = os.environ.get("CRUST_KHRONOS_INCLUDE")
    for d in ([env] if env else []) + ["/usr/include"]:
        if d and os.path.isfile(os.path.join(d, "GLES3", "gl31.h")):
            return d
    return None


def ours():
    text = open(OURS).read()
    consts = re.findall(r"(?m)^#define\s+(GL_\w+)\s+(0x[0-9A-Fa-f]+|\d+)\s*$",
                        text)
    protos = re.findall(r"(?ms)^(void|GLuint|GLint|GLenum|GLboolean)\s+"
                        r"(gl\w+)\s*\((.*?)\);", text)
    return consts, protos


def check(kdir):
    consts, protos = ours()
    lines = ["#include <GLES3/gl31.h>"]
    for name, value in consts:
        lines.append('_Static_assert((%s) == (%s), "%s differs from Khronos");'
                     % (name, value, name))
    for ret, name, params in protos:
        lines.append("%s %s(%s);" % (ret, name, " ".join(params.split())))
    src = "\n".join(lines) + "\n"
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "check.c")
        open(path, "w").write(src)
        r = subprocess.run(["gcc", "-fsyntax-only", "-std=c11", "-I", kdir,
                            path], capture_output=True, text=True)
    return r.returncode, r.stderr, len(consts), len(protos)


def main():
    kdir = khronos_dir()
    if kdir is None:
        print("SKIP: no Khronos GLES3/gl31.h (install libgles-dev, or set "
              "CRUST_KHRONOS_INCLUDE)")
        return 0
    rc, err, nc, np_ = check(kdir)
    if rc:
        print(err)
        print("FAIL: shivyc/include/GLES3/gl31.h disagrees with Khronos")
        return 1
    print("OK: %d constants, %d prototypes match Khronos (%s)" % (nc, np_, kdir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
