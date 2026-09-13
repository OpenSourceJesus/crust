#!/usr/bin/env python3
"""csrust — lower a C# subset file through cs2cpp + cpprust.

Same CLI shape as cpprust.py: one input, `-o` output, one exit status.
On failure the diagnostic is written to the output path (preproc protocol).

    python3 tools/csrust.py x.cs -o x.c
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.cpprust as cpprust  # noqa: E402
import tools.cs2cpp as cs2cpp  # noqa: E402


def translate(text, path="<cs>", **kw):
    """Normalize C# → C++ subset, then cpprust.translate."""
    cpp = cs2cpp.normalize(text, path=path)
    # Path kept as .cs so diagnostics name the user's file when possible.
    return cpprust.translate(cpp, path=path, **kw)


def main():
    args = list(sys.argv[1:])
    out_path = None
    owning = {}
    basedir = None
    incdirs = []
    clang = None
    rtti = False
    decls = []
    decls_out = None
    want_contracts = False
    want_mem_safe = False

    if "--emit-decls" in args:
        i = args.index("--emit-decls")
        if i + 1 >= len(args):
            sys.stderr.write("csrust: --emit-decls needs a path\n")
            return 2
        decls_out = args[i + 1]
        del args[i:i + 2]
    while "--decls" in args:
        i = args.index("--decls")
        if i + 1 >= len(args):
            sys.stderr.write("csrust: --decls needs a file\n")
            return 2
        decls.append(args[i + 1])
        del args[i:i + 2]
    if "--contracts" in args:
        want_contracts = True
        args.remove("--contracts")
    if "--mem-safe" in args:
        want_mem_safe = True
        args.remove("--mem-safe")
    if "--rtti" in args:
        rtti = True
        args.remove("--rtti")
    if "--clang" in args:
        clang = True
        args.remove("--clang")
    if "--no-clang" in args:
        clang = False
        args.remove("--no-clang")
    defines = []
    while "-D" in args:
        i = args.index("-D")
        if i + 1 >= len(args):
            sys.stderr.write("csrust: -D needs a name\n")
            return 2
        defines.append(args[i + 1].split("=")[0])
        del args[i:i + 2]
    while "--incdir" in args:
        i = args.index("--incdir")
        if i + 1 >= len(args):
            sys.stderr.write("csrust: --incdir needs a directory\n")
            return 2
        incdirs.append(args[i + 1])
        del args[i:i + 2]
    if "--basedir" in args:
        i = args.index("--basedir")
        if i + 1 >= len(args):
            sys.stderr.write("csrust: --basedir needs a directory\n")
            return 2
        basedir = args[i + 1]
        del args[i:i + 2]
    if "--owning" in args:
        i = args.index("--owning")
        if i + 1 >= len(args):
            sys.stderr.write("csrust: --owning needs a spec\n")
            return 2
        try:
            owning = cpprust._parse_owning(args[i + 1])
        except cpprust.CppError as e:
            sys.stderr.write("csrust: %s\n" % e.message)
            return 2
        del args[i:i + 2]
    if "-o" in args:
        i = args.index("-o")
        if i + 1 >= len(args):
            sys.stderr.write("csrust: -o needs a path\n")
            return 2
        out_path = args[i + 1]
        del args[i:i + 2]
    if len(args) != 1 or out_path is None:
        sys.stderr.write(
            "usage: csrust.py <source.cs> -o <out.c> "
            "[--owning Name:dropfn,..] [--basedir DIR] "
            "[--incdir DIR].. [-D NAME].. [--rtti] "
            "[--clang|--no-clang]\n")
        return 2

    src = args[0]
    try:
        with open(src) as f:
            text = f.read()
    except IOError as e:
        sys.stderr.write("csrust: cannot read %s: %s\n" % (src, e))
        return 2

    try:
        if basedir is None:
            basedir = os.path.dirname(os.path.abspath(src))
        result = translate(
            text, path=src, mem_safe=want_mem_safe, owning=owning,
            basedir=basedir, incdirs=incdirs, defines=defines, clang=clang,
            rtti=rtti, decls=decls, decls_out=decls_out,
            contracts=want_contracts)
    except (cs2cpp.CsError, cpprust.CppError) as e:
        msg = getattr(e, "message", None) or str(e)
        try:
            with open(out_path, "w") as f:
                f.write(msg)
        except IOError:
            pass
        sys.stderr.write("csrust: %s\n" % msg)
        return 1

    with open(out_path, "w") as f:
        f.write(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
