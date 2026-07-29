"""rpyinc -- `#include "foo.py"`: rpython modules as C headers.

Crust already lets a C translation unit pull in a Rust module with an ordinary
`#include "vec2.rs"`.  This module does the same thing for the third language
the repo already knows how to compile: rpython.  `tools/py2c.py` lowers an
rpython module to C, so a `#include "foo.py"` can be answered by transpiling
the module and splicing the generated C in where the directive stood.

The hook is the same one Crust uses -- `preproc._do_include` -- so `-I`
directories, quoted vs angle-bracket lookup, nested includes and include
guards all keep working, and a single unit can mix all three languages:

    #include "vec2.rs"      /* Rust  */
    #include "stats.py"     /* rpython */
    int main(void) { ... }  /* C */

Transpiling is by far the slowest step in that pipeline, so results are cached
under /tmp keyed by a hash of the module text and of py2c itself.  A rebuild
that does not touch the .py file reuses the cached C and never imports py2c at
all, which is what makes `make test_fast_crust` fast enough to run in a loop.

Two post-processing rules, taken from `main.process_py_file`, decide what the
spliced text looks like:

  * A module that actually touches the transpiler runtime (lists, dicts,
    strings, objects) keeps its `#include "shivyc_rt.h"`.  The header is
    written into the same cache directory as the generated C, so the nested
    include resolves relative to it, and `shivyc_rt.c` is queued for linking.
  * A module that does not -- a pure numeric kernel -- gets the runtime
    include dropped and the handful of libc prototypes it needs prepended
    instead, so it compiles as plain C11 with nothing to link.
"""

import hashlib
import os
import re
import sys

CACHE_ROOT = os.environ.get("CRUST_RPY_CACHE", "/tmp/crust-rpy")

# shivyc_rt.c paths that a `#include "*.py"` in this run made necessary.
# main.process_c_file drains this after preprocessing, compiles each one and
# adds the object to the link line.  Kept module-level because the
# preprocessor has no access to the argument namespace.
_runtime_sources = []

# Cache-key -> generated C path, so a header included twice in one run (or by
# two different units) is not re-read from disk.
_memo = {}


class RpyIncludeError(Exception):
    """An rpython module could not be lowered to C."""


def take_runtime_sources():
    """Return and clear the shivyc_rt.c paths queued for linking."""
    global _runtime_sources
    out, _runtime_sources = _runtime_sources, []
    return out


def runtime_object(rt_c, args):
    """Path to the cached object for `rt_c`, or None if it must be built.

    shivyc_rt.c is ~48KB of C and takes an order of magnitude longer to
    compile than everything else in a small mixed-language unit, so building
    it once per *content and flag set* rather than once per invocation is
    what actually makes the second build fast. The .c already lives in a
    content-addressed directory, so only the code-generation flags need to go
    into the object's name.
    """
    obj = os.path.join(os.path.dirname(rt_c),
                       "shivyc_rt.%s.o" % _flag_stamp(args))
    if os.path.exists(obj) and os.path.getmtime(obj) >= os.path.getmtime(rt_c):
        return obj
    return None


def runtime_object_path(rt_c, args):
    """Where `runtime_object` will look, for storing a freshly built one."""
    return os.path.join(os.path.dirname(rt_c),
                        "shivyc_rt.%s.o" % _flag_stamp(args))


# Argument-namespace entries that cannot change the generated object: the
# input files, the output name, pure-reporting flags, and our own
# bookkeeping. Everything else goes into the stamp, so a cached object is
# only ever reused for an identical code-generation configuration.
_IRRELEVANT = {
    "files", "output_name", "_extra_objs", "_extensions",
    "no_cache", "show_reg_alloc_perf", "pdf", "print_call_graph",
    "print_eliminated_members", "emit_microslice", "thread_alloc_json",
}


def _flag_stamp(args):
    """A short hash of every argument that can change code generation."""
    items = []
    for key, value in sorted(vars(args).items()):
        if key in _IRRELEVANT or key.startswith("__"):
            continue
        items.append("%s=%r" % (key, value))
    return hashlib.sha256("\0".join(items).encode("utf-8")).hexdigest()[:12]


def _py2c():
    """Import tools/py2c.py, which is not a package module."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tools_dir = os.path.join(repo_root, "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    try:
        import py2c
    except Exception as e:                             # pragma: no cover
        raise RpyIncludeError("cannot import py2c: %s" % e)
    return py2c


def _py2c_stamp():
    """A cheap identity for the transpiler, so edits to it bust the cache."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(repo_root, "tools", "py2c.py")
    try:
        st = os.stat(path)
        return "%d:%d" % (st.st_mtime_ns, st.st_size)
    except OSError:                                    # pragma: no cover
        return "0:0"


def cache_key(text):
    """The cache key for an rpython module with the given source text."""
    h = hashlib.sha256()
    h.update(text.encode("utf-8"))
    h.update(b"\0")
    h.update(_py2c_stamp().encode("ascii"))
    h.update(b"\0v1")
    return h.hexdigest()[:32]


# Identifiers that only exist if the module leans on the transpiler runtime.
# Same test main.process_py_file uses, so an included module and a
# command-line one are classified identically.
_USES_RT = re.compile(
    r"\b(obj_[a-z]|OBJ_[A-Z]|aalloc|afree|pystr|subscript|truthy|"
    r"list_of|make_closure|pyconcat)")

_LIBC_PROTOS = [
    ("malloc", "void *malloc(unsigned long);"),
    ("free", "void free(void *);"),
    ("realloc", "void *realloc(void *, unsigned long);"),
    ("printf", "int printf(const char *, ...);"),
    ("puts", "int puts(const char *);"),
    ("atoi", "int atoi(const char *);"),
    ("strlen", "unsigned long strlen(const char *);"),
    ("strcmp", "int strcmp(const char *, const char *);"),
    ("abort", "void abort(void);"),
    ("exit", "void exit(int);"),
]


def _prelude_for(code):
    """libc/libm prototypes a runtime-free module still needs.

    ShivyCX's C11-subset front end has no system headers, and dropping
    `shivyc_rt.h` takes the declarations it did supply with it.  Re-supply
    only what the generated code actually names, so the spliced text stays
    small and declares nothing it does not use.
    """
    import shivyc.main as main
    prelude = []
    # A module that avoids the transpiler runtime loses `shivyc_rt.h`, and
    # with it `<stdbool.h>`. py2c still writes `bool` for a Python truth
    # value, so the typedef has to come from somewhere -- a pure numeric
    # kernel that happens to use a flag was otherwise rejected with
    # "expected ';' after 'bool'".
    if main._has_word(code, "bool"):
        prelude.append("typedef _Bool bool;")
        prelude.append("#define true 1")
        prelude.append("#define false 0")
    for sym, proto in _LIBC_PROTOS:
        if main._has_word(code, sym):
            prelude.append(proto)
    prelude.extend(main._libm_protos(code))
    return prelude


def _postprocess(code):
    """Turn py2c's module output into text that can be spliced into a unit."""
    if _USES_RT.search(code):
        return code, True
    code = re.sub(r'#include "shivyc_rt\.h"\n', "", code)
    prelude = _prelude_for(code)
    if prelude:
        code = "\n".join(prelude) + "\n" + code
    return code, False


def translate(path, text=None):
    """Lower the rpython module at `path` to C; return (c_text, c_path).

    `c_path` is the cached generated file.  The caller lexes the text under
    that name so a nested `#include "shivyc_rt.h"` resolves next to it.
    """
    if text is None:
        with open(path, encoding="utf-8") as f:
            text = f.read()

    key = cache_key(text)
    stem = os.path.splitext(os.path.basename(path))[0]
    out_dir = os.path.join(CACHE_ROOT, key)
    out_c = os.path.join(out_dir, stem + ".c")
    rt_c = os.path.join(out_dir, "shivyc_rt.c")
    stamp = os.path.join(out_dir, ".crust-ok")

    if key in _memo:
        cached_c, needs_rt = _memo[key]
        if needs_rt and rt_c not in _runtime_sources:
            _runtime_sources.append(rt_c)
        with open(cached_c, encoding="utf-8") as f:
            return f.read(), cached_c

    if os.path.exists(stamp) and os.path.exists(out_c):
        # Warm cache: skip py2c entirely.  The stamp records whether the
        # module needed the runtime, so that decision is cached too.
        with open(stamp, encoding="utf-8") as f:
            needs_rt = f.read().strip() == "rt"
        with open(out_c, encoding="utf-8") as f:
            code = f.read()
        _memo[key] = (out_c, needs_rt)
        if needs_rt and rt_c not in _runtime_sources:
            _runtime_sources.append(rt_c)
        return code, out_c

    py2c = _py2c()
    os.makedirs(out_dir, exist_ok=True)
    try:
        produced, err = py2c.transpile_file(path, out_dir)
    except Exception as e:
        raise RpyIncludeError("py2c failed on '%s': %s" % (path, e))
    if err or not produced:
        raise RpyIncludeError(
            "py2c could not translate '%s': %s" % (path, err or "no output"))

    with open(produced, encoding="utf-8") as f:
        code = f.read()
    code, needs_rt = _postprocess(code)

    if needs_rt:
        # The generated C keeps `#include "shivyc_rt.h"`; put the header (and
        # the matching .c) beside it so the nested include resolves and the
        # runtime can be linked.
        py2c.write_runtime(out_dir)

    if produced != out_c and os.path.exists(produced):
        os.replace(produced, out_c)
    with open(out_c, "w", encoding="utf-8") as f:
        f.write(code)
    with open(stamp, "w", encoding="utf-8") as f:
        f.write("rt" if needs_rt else "pure")

    _memo[key] = (out_c, needs_rt)
    if needs_rt and rt_c not in _runtime_sources:
        _runtime_sources.append(rt_c)
    return code, out_c
