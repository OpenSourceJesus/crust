#!/usr/bin/env python3
"""csrust -- translate a C# subset source to C.

    python3 tools/csrust.py counter.cs -o counter.c

Two halves. `tools/cs2cpp.py` rewrites the C# into the C++ subset;
`tools/cpprust.py` lowers that to C. The split is argued in CSHARP.md: C#
overlaps this particular C++ subset in exactly the passes that are already
written -- `var` is `auto`, `foreach` is a range-`for`, `interface` is a
pure-abstract base, and C# generics are strictly weaker than the templates
the monomorphiser already handles.

The command line is `cpprust.py`'s, option for option, and so is the
protocol: **one output file and one exit status**, with the diagnostic
written to the output path on failure. That is not tidiness. It is what
lets `shivyc/preproc.py` drive this as a subprocess from the self-hosted
compiler, where there is no `subprocess` module and no pipe to capture --
only `os.system` and a return code.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.cpprust as cpprust                              # noqa: E402
import tools.cs2cpp as cs2cpp                                # noqa: E402


def translate(text, path="<cs>", owning=None, basedir=None, incdirs=(),
              defines=(), clang=None, rtti=False, decls=(), decls_out=None,
              contracts=False, mem_safe=False, coost=None):
    """C# source in, C out. Raises CsError or CppError.

    `coost`: a coost checkout (crust edition) -- turns on `string`, lowered to
    its fastring; see cs2cpp_strings.py. `"auto"` finds one the way unity_pack
    does ($COOST_ROOT, or a `coost` directory beside this repository).

    `clang` defaults to False here rather than None. The fallback in
    `cpp_auto` answers an `auto` it cannot read by compiling the *original
    file* with `clang++` -- and the original file is C#, which clang will
    not parse. Consulting it could only ever fail slowly, so it is off
    unless a caller insists.
    """
    root = find_coost(coost) if coost else None
    cpp = cs2cpp.translate(text.lstrip("\ufeff"), path=path, coost=bool(root))
    if root:
        incdirs = list(incdirs) + [os.path.join(root, "include"), root]
    c = cpprust.translate(
        cpp, path=path, owning=owning, basedir=basedir, incdirs=incdirs,
        defines=defines, clang=False if clang is None else clang,
        rtti=rtti, decls=decls, decls_out=decls_out,
        contracts=contracts, mem_safe=mem_safe, any_order=True, bodies_last=True)
    return _finish(c)


def _finish(c):
    """What every csrust output gets, API and command line alike."""
    # C# `null` is lowered to NULL, which C defines only in <stddef.h>
    c = cpprust._sub_code(r"(?<![\w])NULL(?![\w])", lambda m: "((void *)0)", c)
    return _with_wrapv(c)


def translate_unit(sources, **kw):
    """Several C# files as one translation unit. `sources` is [(path, text)].

    Every file sees every class, so a call into another file, a base class
    declared elsewhere, or a `ref`/`out` argument to another file's method
    all resolve (cs2cpp_unit.py). An error names the file and line the author
    wrote, not a line of the joined text.
    """
    import tools.cs2cpp_unit as cs2cpp_unit
    unit = cs2cpp_unit.Unit(sources)
    try:
        return translate(unit.text, path=cs2cpp_unit.NAME, **kw)
    except cs2cpp.CsError as e:
        raise cs2cpp.CsError(unit.remap(e.message))


def find_coost(coost):
    """The crust edition of coost: *coost*, $COOST_ROOT, or `../coost`."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cands = [] if coost == "auto" else [coost]
    cands += [os.environ.get("COOST_ROOT"), os.path.join(os.path.dirname(here), "coost")]
    for c in cands:
        h = os.path.join(c or "", "include", "co", "fastring.h")
        if c and os.path.isfile(h):
            with open(h) as f:
                if "assign_cstr" in f.read():
                    return os.path.abspath(c)
    raise cs2cpp.CsError(
        "`--coost` needs the crust edition of coost (it has fastring::assign_cstr): "
        "pass its path, set COOST_ROOT, or clone crustos/coost beside crust")


def _with_wrapv(c):
    """`cs2cpp.WRAPV_PRAGMA` on the first line that is not a directive, so
    no line moves and no `#include` is broken."""
    lines = c.split("\n")
    for k, line in enumerate(lines):
        if not line.lstrip().startswith("#"):
            lines[k] = cs2cpp.WRAPV_PRAGMA + line
            break
    return "\n".join(lines)


def main():
    args = list(sys.argv[1:])
    out_path = None
    owning = {}
    basedir = None
    incdirs = []
    rtti = False
    decls = []
    decls_out = None
    emit_cpp = None
    contracts = False
    mem_safe = False

    if "--emit-cpp" in args:
        i = args.index("--emit-cpp")
        if i + 1 >= len(args):
            sys.stderr.write("csrust: --emit-cpp needs a path\n")
            return 2
        emit_cpp = args[i + 1]
        del args[i:i + 2]
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
    coost = None
    if "--coost" in args:
        i = args.index("--coost")
        if i + 1 >= len(args):
            sys.stderr.write("csrust: --coost needs a path (or `auto`)\n")
            return 2
        coost = args[i + 1]
        del args[i:i + 2]
    if "--contracts" in args:
        contracts = True
        args.remove("--contracts")
    if "--mem-safe" in args:
        mem_safe = True
        args.remove("--mem-safe")
    if "--rtti" in args:
        rtti = True
        args.remove("--rtti")
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
    if len(args) < 1 or out_path is None:
        sys.stderr.write("usage: csrust.py <source.cs>.. -o <out.c> "
                         "[--owning Name:dropfn,..] [--basedir DIR] "
                         "[--incdir DIR].. [-D NAME].. [--rtti] "
                         "[--emit-cpp PATH] [--coost PATH|auto]\n")
        return 2

    src = args[0]
    unit = None
    try:
        sources = []
        for p in args:
            with open(p, encoding="utf-8-sig") as f:   # no byte-order mark in the text
                sources.append((p, f.read()))
    except IOError as e:
        sys.stderr.write("csrust: cannot read %s: %s\n" % (e.filename or src, e))
        return 2
    text = sources[0][1]
    if len(sources) > 1:
        import tools.cs2cpp_unit as cs2cpp_unit
        try:
            unit = cs2cpp_unit.Unit(sources)
        except cs2cpp.CsError as e:
            sys.stderr.write("csrust: %s\n" % e.message)
            return 1
        text, src = unit.text, cs2cpp_unit.NAME

    if basedir is None:
        basedir = os.path.dirname(os.path.abspath(args[0]))

    def fail(msg):
        # The message goes where the output would have gone; the caller
        # reads it back and reports it against the `#include` line.
        try:
            with open(out_path, "w") as f:
                f.write(msg)
        except IOError:
            pass
        sys.stderr.write("csrust: %s\n" % msg)
        return 1

    try:
        root = find_coost(coost) if coost else None
        cpp = cs2cpp.translate(text, path=src, coost=bool(root))
        if root:
            incdirs = list(incdirs) + [os.path.join(root, "include"), root]
    except cs2cpp.CsError as e:
        return fail(unit.remap(e.message) if unit else e.message)

    # Written before the C++ half runs, so it is on disk to read when that
    # half is what failed -- which is the case this option exists for.
    if emit_cpp:
        with open(emit_cpp, "w") as f:
            f.write(cpp)

    try:
        result = cpprust.translate(
            cpp, path=src, owning=owning, basedir=basedir, incdirs=incdirs,
            defines=defines, clang=False, rtti=rtti, decls=decls,
            decls_out=decls_out, contracts=contracts, mem_safe=mem_safe,
            any_order=True, bodies_last=True)
    except cpprust.CppError as e:
        # A C++ diagnostic reaching a C# author names a construct they did
        # not write. That is a gap in `_check_refusals`, not a user error,
        # and saying so is the difference between a bug report and an hour
        # spent looking for a `class` they never wrote.
        return fail(
            "%s\n"
            "  (This is the C++ half of the translation reporting against "
            "generated source. Every C# construct outside the subset is "
            "supposed to be refused before that point, so reaching here is "
            "a cs2cpp bug -- please report it. `--emit-cpp PATH` writes the "
            "generated C++ for inspection.)" % e.message)

    with open(out_path, "w") as f:
        f.write(_finish(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
