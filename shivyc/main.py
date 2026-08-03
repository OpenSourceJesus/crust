"""Main executable for ShivyC compiler."""

import argparse
import os
import pathlib
import platform
import subprocess
import sys

import shivyc.lexer as lexer
import shivyc.preproc as preproc

from shivyc.errors import error_collector, CompilerError
from shivyc.parser.parser import parse
from shivyc.il_gen import ILCode, SymbolTable, Context


def _concat_adjacent_strings(tokens):
    """Merge runs of adjacent string-literal tokens into single tokens.

    This implements C translation phase 6. It is especially important after
    macro expansion, where prefixes like `"[INFO] " fmt` become two adjacent
    string literals that must be treated as one.
    """
    import shivyc.token_kinds as token_kinds
    from shivyc.tokens import Token

    out = []
    i = 0
    n = len(tokens)
    while i < n:
        if tokens[i].kind == token_kinds.string:
            j = i + 1
            while j < n and tokens[j].kind == token_kinds.string:
                j += 1
            if j > i + 1:
                content = []
                for k in range(i, j):
                    chars = list(tokens[k].content)
                    # Drop each piece's trailing null; add a single one back.
                    if chars and chars[-1] == 0:
                        chars = chars[:-1]
                    content.extend(chars)
                content.append(0)
                rep = "".join(t.rep for t in tokens[i:j])
                r = tokens[i].r + tokens[j - 1].r
                out.append(Token(token_kinds.string, content, rep, r=r))
                i = j
                continue
        out.append(tokens[i])
        i += 1
    return out
from shivyc.asm_gen import ASMCode, ASMGen
from shivyc.targets import get_target


def main():
    """Run the main compiler script."""

    if sys.implementation.name != "shivyc":
        if platform.system() != "Linux":
            err = "only x86_64 Linux is supported"
            print(CompilerError(err))
            return 1

    if sys.implementation.name != "shivyc":
        arguments = get_arguments()
    else:
        arguments = get_arguments(
            [sys.argv[i] for i in range(len(sys.argv))])

    # When --musl is given, materialize the packaged musl headers and put their
    # include directories (and required defines) ahead of the user's, so that
    # #include resolves against musl instead of the host glibc. This keeps the
    # build self-contained: no musl tree needs to exist in the source checkout.
    include_dirs = list(getattr(arguments, "include_dirs", []))
    defines = list(getattr(arguments, "defines", []))
    if sys.implementation.name != "shivyc":
        if getattr(arguments, "use_musl", False):
            from shivyc import musl as _musl
            _tree = _musl.materialize(getattr(arguments, "musl_dir", None))
            defines = _tree.defines() + defines
            include_dirs = _tree.public_include_dirs() + include_dirs

    # Apply any -I include directories to the preprocessor.
    preproc.set_include_dirs(include_dirs)
    preproc.set_defines(defines)

    # Whether to alias long double to double (-f-long-double-as-double).
    if sys.implementation.name != "shivyc":
        import shivyc.ctypes as ctypes
        ctypes.long_double_as_double = getattr(
            arguments, "long_double_as_double", False)
        # 32-bit pointer compression (-f-pointer-compression). Drives
        # PointerCType.size, hence sizeof / struct layout / the size-driven
        # asm backend. It requires the low-4GiB image, so it implies -f-low-mem.
        ctypes.pointer_compression = getattr(
            arguments, "pointer_compression", False)
        if ctypes.pointer_compression:
            arguments.low_mem = True

    # Load a per-function register budget (thread partitioning) if supplied;
    # ASMGen consults arguments._thread_alloc to restrict allocation.
    arguments._thread_alloc = None
    if getattr(arguments, "thread_alloc_json", None):
        import json
        try:
            with open(arguments.thread_alloc_json) as fh:
                arguments._thread_alloc = json.load(fh)
        except OSError as e:
            print(CompilerError(f"cannot read thread budget: {e}"))
            return 1

    # Whole-program memory-safety analysis: detect use-after-free / double-free
    # (and optionally auto-free candidates) over all inputs, then exit.
    if getattr(arguments, "check_memory", False):
        import shivyc.memory_safety as memory_safety
        return memory_safety.run(arguments.files, arguments)

    # pdf_report is an optional, host-only feature (it shells out to LaTeX and
    # uses host stdlib). Guard it so the self-hosted compiler doesn't pull it
    # in: `sys.implementation.name` is 'shivyc' under the translator (folded to
    # a compile-time-false branch) but the real name under CPython.
    if sys.implementation.name != "shivyc":
        if getattr(arguments, "pdf", None):
            import shivyc.pdf_report as pdf_report
            return pdf_report.run(arguments.files, arguments, arguments.pdf)

    # Micro-slicing analysis: find pure, independent fragments and a slice plan
    # for productive spin-waiting, then exit.
    if getattr(arguments, "microslice", False):
        import shivyc.microslice as microslice
        return microslice.run(arguments.files, arguments)

    # Thread-partition switcher: analyze threads.left/right across the inputs
    # and emit a specialized context switcher, then exit. (Analysis + codegen
    # of the switcher only; the user's TUs are compiled normally otherwise.)
    if getattr(arguments, "emit_thread_switcher", None):
        import shivyc.thread_contracts as thread_contracts
        return thread_contracts.run(arguments.files, arguments)

    # Whole-program call-graph report: build the cross-TU call graph over all
    # input files and print it, then exit. (Analysis only; emits no objects.)
    if getattr(arguments, "print_call_graph", False):
        import shivyc.callgraph as callgraph
        graph, ok = callgraph.build_program_graph(arguments.files, arguments)
        error_collector.show()
        print(graph.summary())
        undef = graph.undefined_calls()
        externals = sorted({c for s in undef.values() for c in s})
        if externals:
            print("external/undefined callees: " + ", ".join(externals))
        return 0 if ok else 1

    # The metamorphic-reentrancy and -O4 near-scratch safety checks reason
    # about the call graph. Compiled per file, they would only see one TU and
    # miss recursion (or address-taking) that travels through another unit.
    # When either feature is active and there is more than one TU, build the
    # whole-program graph up front so those checks can close cross-TU cycles.
    arguments._wp_graph = None
    arguments._simd_pack_layout = None
    arguments._inline_bodies = None

    # Whole-program elimination of never-accessed struct members.
    import shivyc.member_elim as member_elim
    if sys.implementation.name != "shivyc":
        member_elim.enabled = getattr(arguments, "eliminate_unused_members", False)
    member_elim.install({})

    needs_graph = (getattr(arguments, "metamorphic", False)
                   or getattr(arguments, "opt_level", 0) >= 4
                   or getattr(arguments, "simd_pack_globals", False))
    c_files = [f for f in arguments.files if f.endswith(".c")]
    if needs_graph and len(c_files) > 1:
        import shivyc.callgraph as callgraph
        graph, _ = callgraph.build_program_graph(arguments.files, arguments)
        arguments._wp_graph = graph
        # Promote externally-linked flag globals into a single, consistent
        # xmm15 layout shared by every translation unit. This is only sound
        # with the whole-program view: the bit assignment must agree across
        # units, and a flag whose address escapes anywhere must be excluded.
        if getattr(arguments, "simd_pack_globals", False):
            arguments._simd_pack_layout = graph.simd_pack_layout()
        # At -O4, inline small pure leaf functions across TU boundaries: their
        # bodies were captured while building the graph, and a single unit
        # never has the body of a callee defined in another file.
        if getattr(arguments, "opt_level", 0) >= 4:
            arguments._inline_bodies = graph.inlinable
        # Building the graph runs its own front end; discard any diagnostics it
        # accumulated so the real compile below starts from a clean slate.
        error_collector.clear()

    # Whole-program unused-member analysis: parse + type every translation
    # unit with the collector active, then compute which struct members are
    # safe to drop. The per-file compile below consults the result.
    if member_elim.enabled and c_files:
        import shivyc.callgraph as callgraph
        member_elim.begin_collection()
        callgraph.build_program_graph(arguments.files, arguments)
        mapping = member_elim.finalize()
        member_elim.install(mapping)
        error_collector.clear()
        if getattr(arguments, "print_eliminated_members", False):
            for tag in sorted(mapping):
                members = ", ".join(sorted(mapping[tag]))
                print(f"eliminated from 'struct {tag}': {members}")

    objs = []
    arguments._extra_objs = []
    # The multi-source rpython co-compilation path imports py2c (the translator)
    # and uses os.path/sys.path/rpy_torch -- all host-only bootstrap machinery.
    # The self-hosted compiler only compiles C inputs, so guard the whole block;
    # under the translator the condition folds to false and it is dropped.
    if sys.implementation.name != "shivyc":
        py_files = [f for f in arguments.files if f.endswith(".py")]
        # Auto-bundle the rpy_torch mini-library when a source imports it, so a
        # single `shivyc.main model.py` still co-compiles the library it needs.
        if py_files:
            try:
                import os as _os
                import sys as _sys
                _td = _os.path.join(_os.path.dirname(_os.path.dirname(
                    _os.path.abspath(__file__))), "tools")
                if _td not in _sys.path:
                    _sys.path.insert(0, _td)
                import rpy_torch as _rpy_torch
                py_files = _rpy_torch.bundle(py_files)
            except Exception:
                pass
        if len(py_files) > 1:
            # Several rpython sources: translate them together as one unit so the
            # runtime is emitted and compiled exactly once (no duplicate-symbol
            # link errors) and cross-module calls between them resolve against a
            # single output directory -- the whole program in one translation
            # unit, no on-disk AST cache required.
            unit_objs = process_py_unit(py_files, arguments)
            if unit_objs is None:
                error_collector.show()
                return 1
            objs.extend(unit_objs)
            for file in arguments.files:
                if not file.endswith(".py"):
                    objs.append(process_file(file, arguments))
        else:
            for file in arguments.files:
                objs.append(process_file(file, arguments))
    else:
        for file in arguments.files:
            objs.append(process_file(file, arguments))
    objs.extend(arguments._extra_objs)

    error_collector.show()
    if any(not obj for obj in objs):
        return 1

    # -S: assembly already written by process_c_file; nothing to assemble/link.
    if getattr(arguments, "asm_only", False):
        return 0

    # -c: compile and assemble only, leaving the .o files; do not link.
    if getattr(arguments, "compile_only", False):
        if (arguments.output_name and len(arguments.output_name) == 1
                and len(objs) == 1 and objs[0] != arguments.output_name[0]):
            import shutil
            shutil.move(objs[0], arguments.output_name[0])
        return 0

    if True:
        # set the output ELF name
        out = "out"
        if arguments.output_name is not None and \
                len(arguments.output_name) == 1:
            # set the output ELF name
            out = arguments.output_name[0]
        writable_text = (getattr(arguments, "metamorphic", False)
                         or getattr(arguments, "opt_level", 0) >= 4)
        if not link_objs(out, objs, writable_text,
                         getattr(arguments, "low_mem", False),
                         libs=getattr(arguments, "libs", []),
                         lib_dirs=getattr(arguments, "lib_dirs", []),
                         export_dynamic=getattr(arguments, "export_dynamic",
                                                False)):
            err = "linker returned non-zero status"
            print(CompilerError(err))
            return 1
        return 0


def process_file(file, args):
    """Process single file into object file and return the object file name."""
    if file[-2:] == ".c":
        return process_c_file(file, args)
    elif file[-3:] == ".py":
        return process_py_file(file, args)
    elif file[-3:] == ".rs":
        return process_c_file(file, args)
    elif file[-2:] == ".o":
        return file
    else:
        err = f"unknown file type: '{file}'"
        error_collector.add(CompilerError(err))
        return None


def process_py_unit(py_files, args):
    """Translate several rpython `.py` sources as one translation unit.

    Every source is transpiled into a single shared output directory, so the
    py2c runtime (shivyc_rt.c) is emitted and compiled exactly once and the
    modules link together without duplicate-symbol errors. Cross-module
    references between the sources resolve against that shared output. Returns
    the list of object files (runtime first), or None on any failure.
    """
    import os
    import sys
    import tempfile

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tools_dir = os.path.join(repo_root, "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    try:
        import py2c
    except Exception as e:                       # pragma: no cover
        error_collector.add(CompilerError(f"cannot import py2c: {e}"))
        return None

    out_dir = tempfile.mkdtemp(prefix="shivyc_py2c_unit_")
    mp_bridge = any("python-stdlib" in os.path.abspath(p) for p in py_files)
    try:
        py2c.set_local_module_dirs(py_files)
        py2c.write_runtime(out_dir, mp_bridge=mp_bridge)
    except Exception as e:                       # pragma: no cover
        error_collector.add(CompilerError(f"py2c runtime emit failed: {e}"))
        return None

    generated = []
    for f in py_files:
        try:
            out_c, err = py2c.transpile_file(f, out_dir)
        except Exception as e:
            error_collector.add(CompilerError(f"py2c failed on '{f}': {e}"))
            return None
        if err or not out_c:
            error_collector.add(
                CompilerError(f"py2c could not translate '{f}': {err}"))
            return None
        generated.append(out_c)

    # The shared runtime backs every module in the unit; compile it once.
    objs = []
    rt_c = os.path.join(out_dir, "shivyc_rt.c")
    if os.path.exists(rt_c):
        rt_obj = process_c_file(rt_c, args)
        if not rt_obj:
            return None
        objs.append(rt_obj)
    for out_c in generated:
        # shivyc_rt.h doesn't pull in <math.h>; re-supply any libm prototypes
        # this module references so bare exp/sqrt/... resolve (see _libm_protos).
        code = open(out_c, encoding="utf-8").read()
        mprotos = _libm_protos(code)
        if mprotos:
            with open(out_c, "w", encoding="utf-8") as f:
                f.write("\n".join(mprotos) + "\n" + code)
        obj = process_c_file(out_c, args)
        if not obj:
            return None
        objs.append(obj)
    return objs


def _is_word_char(c):
    return c == "_" or ("0" <= c <= "9") or ("a" <= c <= "z") \
        or ("A" <= c <= "Z")


def _has_word(text, word):
    """True if `word` occurs in `text` as a standalone identifier (the
    \\bword\\b test, without regex)."""
    wlen = len(word)
    tlen = len(text)
    start = 0
    while True:
        idx = text.find(word, start)
        if idx < 0:
            return False
        before_ok = (idx == 0) or (not _is_word_char(text[idx - 1]))
        after = idx + wlen
        after_ok = (after >= tlen) or (not _is_word_char(text[after]))
        if before_ok and after_ok:
            return True
        start = idx + 1


def _libm_protos(code):
    """libm prototypes for the math functions actually referenced in `code`.

    ShivyCX's C11-subset front end has no system headers, and shivyc_rt.h does
    not pull in <math.h>, so a bare `exp`/`sqrt`/... call is otherwise an
    undeclared identifier. We re-supply just the prototypes that are used, with
    the standard signatures (so they are compatible whether or not the runtime
    header is present)."""
    protos = []
    if _has_word(code, "sqrt"):
        protos.append("double sqrt(double);")
    for fn in ("cbrt", "exp", "exp2", "expm1", "log", "log2", "log10",
               "log1p", "sin", "cos", "tan", "asin", "acos", "atan",
               "sinh", "cosh", "tanh", "asinh", "acosh", "atanh", "fabs",
               "floor", "ceil", "round", "trunc"):
        if _has_word(code, fn):
            protos.append("double %s(double);" % fn)
    for fn in ("pow", "fmod", "fmax", "fmin", "atan2", "hypot", "copysign"):
        if _has_word(code, fn):
            protos.append("double %s(double, double);" % fn)
    # single-precision variants (expf/sqrtf/...) for f32 kernels
    for fn in ("sqrtf", "cbrtf", "expf", "exp2f", "expm1f", "logf", "log2f",
               "log10f", "log1pf", "sinf", "cosf", "tanf", "asinf", "acosf",
               "atanf", "sinhf", "coshf", "tanhf", "asinhf", "acoshf",
               "atanhf", "fabsf", "floorf", "ceilf", "roundf", "truncf"):
        if _has_word(code, fn):
            protos.append("float %s(float);" % fn)
    for fn in ("powf", "fmodf", "fmaxf", "fminf", "atan2f", "hypotf",
               "copysignf"):
        if _has_word(code, fn):
            protos.append("float %s(float, float);" % fn)
    return protos


def process_py_file(file, args):
    """Transpile an rpython `.py` to C with tools/py2c.py, then compile it.

    This lets ShivyCX consume rpython sources directly --
    `shivyc.main kernels.py harness.c -o run` -- so the numpy examples no longer
    need a hand-written .c copy or a transpile-then-compile shell script. Any
    runtime support code py2c needs (shivyc_rt.c) is generated here and queued
    for linking; pure kernels (the SIMD examples) reference no runtime, so the
    runtime include is dropped and the kernel C compiles on its own.
    """
    if sys.implementation.name != "shivyc":
        import os
        import re
        import tempfile

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tools_dir = os.path.join(repo_root, "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        try:
            import py2c
        except Exception as e:                       # pragma: no cover
            error_collector.add(CompilerError(f"cannot import py2c: {e}"))
            return None

        out_dir = tempfile.mkdtemp(prefix="shivyc_py2c_")
        try:
            out_c, err = py2c.transpile_file(file, out_dir)
        except Exception as e:
            error_collector.add(CompilerError(f"py2c failed on '{file}': {e}"))
            return None
        if err or not out_c:
            error_collector.add(
                CompilerError(f"py2c could not translate '{file}': {err}"))
            return None

        code = open(out_c, encoding="utf-8").read()
        uses_rt = re.search(
            r"\b(obj_[a-z]|OBJ_[A-Z]|aalloc|afree|pystr|subscript|truthy|"
            r"list_of|make_closure|pyconcat)", code)
        if uses_rt:
            # Module needs the runtime: emit and queue shivyc_rt.c for linking.
            py2c.write_runtime(out_dir)
            rt_c = os.path.join(out_dir, "shivyc_rt.c")
            if os.path.exists(rt_c):
                rt_obj = process_c_file(rt_c, args)
                if rt_obj:
                    getattr(args, "_extra_objs", []).append(rt_obj)
            # shivyc_rt.h supplies the libc prototypes (malloc/printf/...) but not
            # <math.h>; re-supply any libm prototypes the kernel actually uses so
            # bare exp/sqrt/... resolve in functions that also touch the runtime.
            mprotos = _libm_protos(code)
            if mprotos:
                code = "\n".join(mprotos) + "\n" + code
                with open(out_c, "w", encoding="utf-8") as f:
                    f.write(code)
        else:
            # Pure kernel/program: drop the (unused) runtime include so the
            # C11-subset front end compiles it directly, but re-supply the handful
            # of libc prototypes the dropped header would have provided.
            code = re.sub(r'#include "shivyc_rt\.h"\n', "", code)
            prelude = []
            if re.search(r"\bmalloc\b", code):
                prelude.append("void *malloc(unsigned long);")
            if re.search(r"\bfree\b", code):
                prelude.append("void free(void *);")
            if re.search(r"\brealloc\b", code):
                prelude.append("void *realloc(void *, unsigned long);")
            if re.search(r"\bprintf\b", code):
                prelude.append("int printf(const char *, ...);")
            prelude.extend(_libm_protos(code))
            if re.search(r"\batoi\b", code):
                prelude.append("int atoi(const char *);")
            for sym, proto in [
                    ("fopen", "void *fopen(const char *, const char *);"),
                    ("fputs", "int fputs(const char *, void *);"),
                    ("fgets", "char *fgets(char *, int, void *);"),
                    ("fwrite", "unsigned long fwrite(const void *, unsigned long, unsigned long, void *);"),
                    ("fclose", "int fclose(void *);"),
                    ("system", "int system(const char *);"),
                    ("puts", "int puts(const char *);"),
                    ("strlen", "unsigned long strlen(const char *);"),
                    ("strcmp", "int strcmp(const char *, const char *);"),
                    ("fork", "int fork(void);"),
                    ("_exit", "void _exit(int);"),
                    ("waitpid", "int waitpid(int, int *, int);")]:
                if re.search(r"\b" + sym + r"\b", code):
                    prelude.append(proto)
            for stream in ("stdin", "stdout", "stderr"):
                if re.search(r"\b" + stream + r"\b", code):
                    prelude.append("extern void *" + stream + ";")
            if prelude:
                code = "\n".join(prelude) + "\n" + code
            with open(out_c, "w", encoding="utf-8") as f:
                f.write(code)

        return process_c_file(out_c, args)
    return None

def process_c_file(file, args):
    """Compile a C file into an object file and return the object file name."""
    code = read_file(file)
    if not error_collector.ok():
        return None

    # Crust front end: expand `#include "*.rs"` and lower any top-level
    # Rust-syntax items to C before the C lexer sees the source.
    import shivyc.crust as crust
    try:
        code = crust.translate(code, path=file)
    except crust.CrustError as e:
        # `.message`, not `%s % e`: py2c gives user classes no `__str__`, so
        # formatting the exception renders as `<obj 0x...>` and the diagnostic
        # is lost in the self-hosted build, where it is needed most.
        error_collector.add(CompilerError("crust: " + e.message))
        return None

    # Language-extension pre-pass: recognize __stackless__/__metamorphic__
    # specifiers and assert-style contracts, strip them to plain C, and record
    # the metadata for later passes.
    import shivyc.extensions as extensions
    try:
        code, ext_info = extensions.preprocess_extensions(code)
    except extensions.ExtensionError as e:
        error_collector.add(CompilerError(str(e)))
        return None
    args._extensions = ext_info

    token_list = lexer.tokenize(code, file)
    if not error_collector.ok():
        return None

    token_list = preproc.process(token_list, file)
    if not error_collector.ok():
        return None

    # An `#include "foo.py"` expanded above may have pulled in a module that
    # leans on the py2c runtime. Compile shivyc_rt.c now and add it to the
    # link line, the same way a `.py` named on the command line does.
    import shivyc.rpyinc as rpyinc
    for rt_c in rpyinc.take_runtime_sources():
        if not os.path.exists(rt_c):
            continue
        rt_obj = rpyinc.runtime_object(rt_c, args)
        if rt_obj is None:
            saved = args._extensions if hasattr(args, "_extensions") else None
            built = process_c_file(rt_c, args)
            if saved is not None:
                args._extensions = saved
            if not built:
                return None
            rt_obj = rpyinc.runtime_object_path(rt_c, args)
            try:
                import shutil
                shutil.copyfile(built, rt_obj)
            except OSError:                            # pragma: no cover
                rt_obj = built
        getattr(args, "_extra_objs", []).append(rt_obj)

    # Any `unrecognized` token that survived preprocessing is in live code
    # (dead #if branches and #error message text were dropped/consumed by the
    # preprocessor). Report it now with the original lexical diagnostic.
    import shivyc.token_kinds as _tk
    for tok in token_list:
        if tok.kind == _tk.unrecognized:
            error_collector.add(CompilerError(
                "unrecognized token at '%s'" % tok.content, tok.r))
    if not error_collector.ok():
        return None

    # Extract GCC alias/weak attributes (and strip other attributes) at the
    # token level; the recorded aliases become .weak/.set directives below.
    import shivyc.weak_alias as weak_alias
    token_list, aliases = weak_alias.extract_aliases(token_list)

    # Translation phase 6: concatenate adjacent string literals
    # (e.g. `"[INFO] " fmt` after macro expansion becomes one literal).
    token_list = _concat_adjacent_strings(token_list)

    # If parse() can salvage the input into a parse tree, it may emit an
    # ast_root even when there are errors saved to the error_collector. In this
    # case, we still want to continue the compiler stages.
    #
    # Parsing depends only on the token stream, so consult the on-disk AST
    # cache (keyed by a hash of these tokens) before parsing from scratch.
    import shivyc.cache as cache
    use_cache = not getattr(args, "no_cache", False)
    cache_key = cache.token_key(token_list) if use_cache else None
    ast_root = cache.load_ast(cache_key) if cache_key else None
    if ast_root is None:
        ast_root = parse(token_list)
        if cache_key is not None and ast_root is not None \
                and error_collector.ok():
            cache.store_ast(cache_key, ast_root)
    if not ast_root:
        return None

    il_code = ILCode()
    symbol_table = SymbolTable()
    import shivyc.contracts as contracts
    contracts.reset_unit()
    contracts.install_contracts(getattr(args._extensions, "contracts", None))
    ast_root.make_il(il_code, symbol_table, Context())
    if not error_collector.ok():
        return None

    # Reject 80-bit long double, but only where it actually reaches the
    # generated program. Unused `static inline` long double helpers (musl's
    # headers define several) are removed by dead-function elimination first,
    # so merely including such a header does not fail; a long double object or
    # computation that survives into a real function is rejected.
    if il_code.long_double_taint:
        import shivyc.dce as dce
        from shivyc.tree.general_nodes import LONG_DOUBLE_MSG
        dce.eliminate_dead_functions(il_code, symbol_table)
        for fn, rng in il_code.long_double_taint.items():
            if fn in il_code.commands:
                error_collector.add(CompilerError(LONG_DOUBLE_MSG, rng))
        if not error_collector.ok():
            return None

    ext_info = getattr(args, "_extensions", None)

    # Auto-free: insert free() for provably-local, non-escaping, never-freed
    # allocations so the programmer may omit it. Runs before the optimization
    # passes so the inserted frees are optimized like any other call.
    if getattr(args, "auto_free", False):
        import shivyc.memory_safety as memory_safety
        try:
            n = memory_safety.insert_auto_frees(il_code, symbol_table, args)
            if n and not getattr(args, "quiet", False):
                print(f"auto-free: inserted {n} free(s) for non-escaping "
                      f"allocations in {file}")
        except Exception:
            pass  # never let the analysis break a normal compile

    # Cross-TU inlining runs first, before any optimization pass: splicing a
    # small pure leaf (whose body was captured from the whole-program graph)
    # into its direct call sites removes the call, so the later passes (tail
    # calls, near-scratch, recursion checks) see the simplified, call-free
    # code. Doing it after tail-call lowering would be wrong -- a `return f(x)`
    # is turned into a tail jump that drops the Return the inlined body needs.
    inline_bodies = getattr(args, "_inline_bodies", None)
    if inline_bodies:
        import shivyc.inline as inline
        import shivyc.stackless as _stk
        for fn in il_code.commands:
            cmds = _stk._apply_direct_calls(il_code.commands[fn], symbol_table)
            cmds, _ = inline.inline_calls(cmds, inline_bodies, il_code)
            il_code.commands[fn] = cmds
        # Inlining often leaves a static helper with no remaining callers.
        # Drop any internal-linkage function that is now unreachable.
        import shivyc.dce as dce
        dce.eliminate_dead_functions(il_code, symbol_table)

    # Fold statically-known calls -- AddrOf(f) followed by Call(p) -- into a
    # direct `call f` for every function, at every optimization level. Without
    # this each call materializes the callee's address into a register, and the
    # indirect-call path can allocate that register on top of an argument's
    # source (e.g. `mov rsi, rax` after `mov rax, <callee>` clobbered the value
    # rsi was meant to receive). The fold is idempotent -- already-direct calls
    # are skipped -- so it is safe even though the inline pass above and the
    # stackless pass below may also apply it.
    import shivyc.stackless as _stk_direct
    for _fn in il_code.commands:
        il_code.commands[_fn] = _stk_direct._apply_direct_calls(
            il_code.commands[_fn], symbol_table)

    # Metamorphic returns (advanced/experimental): functions marked
    # __metamorphic__ return via a self-modified slot in a writable, executable
    # section instead of the stack. Only active when -fmetamorphic is passed.
    metamorphic_funcs = set()
    if getattr(args, "metamorphic", False) and ext_info:
        metamorphic_funcs = {name for name in ext_info.attrs
                             if ext_info.has_attr(name, "metamorphic")}
    il_code.metamorphic_funcs = metamorphic_funcs

    # Stackless lowering applies whole-program (-fstackless-calls / -O4) or
    # per-function via the __stackless__ specifier.
    stackless_attr_funcs = set()
    if ext_info:
        stackless_attr_funcs = {
            name for name in ext_info.attrs
            if ext_info.has_attr(name, "stackless")}

    whole_program = (getattr(args, "stackless_calls", False)
                     or getattr(args, "opt_level", 0) >= 4)

    if whole_program or stackless_attr_funcs or metamorphic_funcs:
        import shivyc.stackless as stackless
        if whole_program:
            enabled = None
        else:
            # Even without whole-program stackless, metamorphic calls need
            # their target names resolved (direct-call folding).
            enabled = stackless_attr_funcs | metamorphic_funcs
        # A call to a metamorphic function returns to its call site, so it must
        # never be turned into a tail jump (which would drop the return).
        stackless.optimize(il_code, symbol_table, enabled,
                            no_tail=metamorphic_funcs)

        # Metamorphic call sites need their target name resolved even in callers
        # the stackless pass did not otherwise optimize.
        if metamorphic_funcs:
            for fn in il_code.commands:
                il_code.commands[fn] = stackless._apply_direct_calls(
                    il_code.commands[fn], symbol_table)

            # A metamorphic function uses a single static return slot, so it
            # cannot be safely re-entered. Refuse if any is reachable from
            # itself through the (direct) call graph, rather than emit code
            # that would corrupt the return slot at run time.
            import shivyc.il_cmds.control as _control
            edges = {}
            for fn, cmds in il_code.commands.items():
                edges[fn] = {c.direct_name for c in cmds
                             if isinstance(c, _control.Call) and c.direct_name}
            # Close the graph across translation units: for functions defined
            # in *other* units, fold in the whole-program edges so recursion
            # that travels through another TU is detected. This TU's own edges
            # are kept as computed locally (they reflect tail-call lowering),
            # so a single-file build is unaffected.
            wp = getattr(args, "_wp_graph", None)
            if wp is not None:
                for fn, callees in wp.edges.items():
                    if fn not in il_code.commands:
                        edges.setdefault(fn, set()).update(callees)
            for m in metamorphic_funcs:
                seen, stack = set(), list(edges.get(m, ()))
                while stack:
                    cur = stack.pop()
                    if cur == m:
                        err = (f"metamorphic function '{m}' may be re-entered "
                               f"(recursion); not supported")
                        error_collector.add(CompilerError(err))
                        return None
                    if cur in seen:
                        continue
                    seen.add(cur)
                    stack.extend(edges.get(cur, ()))

    # Contract-driven SIMD: prove array-length contracts across the call graph
    # and, where proven, license a fallback-free SIMD reduction.
    if ext_info and ext_info.contracts:
        import shivyc.simd_contracts as simd_contracts
        proven, reports = simd_contracts.analyze(
            il_code, symbol_table, ext_info)
        for report in reports:
            print(report)
        il_code.simd_proven = proven

    # Argument packing (-f-pack-args): a non-standard calling convention that
    # bit-packs small integer parameters into shared argument registers,
    # reducing register pressure and stack spills (most visibly across deeply
    # nested calls). Direct calls are resolved first so each statically-known
    # callee can be named; the pass then rewrites packable callee prologues and
    # annotates their direct call sites.
    if getattr(args, "pack_args", False):
        import shivyc.stackless as _stk
        import shivyc.pack_args as _pack_args
        for fn in il_code.commands:
            il_code.commands[fn] = _stk._apply_direct_calls(
                il_code.commands[fn], symbol_table)
        _pack_args.optimize(il_code, symbol_table)

    # Loop register-promotion for packed globals (-fsimd-pack-globals): hoist
    # the xmm15 decompress/recompress of packed globals out of loops, caching
    # the live value in a GP register across the loop body.
    if getattr(args, "simd_pack_globals", False):
        import shivyc.simd_pack_promote as _promote
        _promote.optimize(il_code, symbol_table)

    # -O4 near-function scratch: a non-reentrant function can hold its locals
    # and register spills in a static per-function buffer instead of the stack,
    # cutting stack pressure (and, for leaf functions, the frame entirely). It
    # is only safe for functions that cannot be active twice at once, so we
    # require: not reachable from itself through the (direct) call graph, and
    # not address-taken (which could allow indirect re-entry).
    if getattr(args, "opt_level", 0) >= 4:
        import shivyc.il_cmds.control as _ctrl
        import shivyc.il_cmds.value as _val
        names_by_val = {v: n for v, n in symbol_table.names.items()
                        if getattr(v, "ctype", None) is not None
                        and v.ctype.is_function()}
        edges = {}
        addr_taken = set()
        for fn, cmds in il_code.commands.items():
            e = set()
            for c in cmds:
                if isinstance(c, _ctrl.Call) and c.direct_name:
                    e.add(c.direct_name)
                if isinstance(c, _val.AddrOf) and c.var in names_by_val:
                    addr_taken.add(names_by_val[c.var])
            edges[fn] = e

        # Close the graph across translation units (see the metamorphic check
        # above): add edges from functions defined in other units, and treat a
        # function whose address is taken in *any* unit as address-taken. For a
        # single-file build the whole-program graph equals this TU, so neither
        # addition changes anything.
        wp = getattr(args, "_wp_graph", None)
        if wp is not None:
            for fn, callees in wp.edges.items():
                if fn not in il_code.commands:
                    edges.setdefault(fn, set()).update(callees)
            addr_taken |= wp.addr_taken

        # A function defined somewhere we can analyze has known call edges; a
        # function we cannot see (declared-only in this TU, or external to the
        # whole program) is "unknown" and might call back into us. In a single
        # TU, only this unit's functions are known, so a call to a function
        # defined in another file is unknown; with the whole-program graph,
        # every function defined anywhere in the program becomes known. This is
        # what lets whole-program analysis grant near-scratch that a sound
        # single-TU analysis must refuse.
        known = set(il_code.commands)
        if wp is not None:
            known |= wp.defined

        # Functions with internal (static) linkage cannot be named by code
        # outside their own translation unit, so no unknown external can
        # re-enter them by name; only a cycle through known functions can.
        internal_funcs = {
            symbol_table.names[v]
            for v, lk in symbol_table.linkage_type.items()
            if lk == symbol_table.INTERNAL and v in symbol_table.names
            and getattr(v, "ctype", None) is not None and v.ctype.is_function()}

        def _eligible(fn):
            # Walk fn's transitive callees. fn can be re-entered (so it is not
            # eligible) if either:
            #   (a) there is a cycle back to fn through known functions, or
            #   (b) fn's closure reaches an unknown external that could name
            #       fn -- conservatively, any unknown external when fn has
            #       external linkage.
            seen, stack = set(), list(edges.get(fn, ()))
            hits_unknown = False
            while stack:
                cur = stack.pop()
                if cur == fn:
                    return False                   # (a) recursion
                if cur in seen:
                    continue
                seen.add(cur)
                if cur not in known:
                    hits_unknown = True            # external: do not expand
                    continue
                stack.extend(edges.get(cur, ()))
            if hits_unknown and fn not in internal_funcs:
                return False                       # (b) external may re-enter
            return True

        il_code.near_scratch_funcs = {
            fn for fn in il_code.commands
            if fn not in addr_taken and _eligible(fn)}

    asm_code = ASMCode(get_target(getattr(args, "target", "x86_64")))
    try:
        ASMGen(il_code, symbol_table, asm_code, args).make_asm()
    except CompilerError as e:
        # Code generation can find a limitation the front end could not --
        # a struct whose size has no register width, say. Collecting it here
        # turns it into an ordinary diagnostic instead of an uncaught
        # exception with a Python traceback.
        error_collector.add(e)
        return None
    if not error_collector.ok():
        return None

    # Emit recorded weak aliases as assembler directives.
    for alias_name, target, is_weak in aliases:
        if is_weak:
            asm_code.add_weak(alias_name)
        asm_code.add_alias(alias_name, target)

    asm_source = asm_code.full_code()
    if not error_collector.ok():
        return None

    asm_file = file[:-2] + ".s"
    obj_file = file[:-2] + ".o"

    write_asm(asm_source, asm_file)
    if not error_collector.ok():
        return None

    # -S: stop after emitting assembly. If a single -o name was given, publish
    # the .s there (gcc-style); otherwise leave it next to the source.
    if getattr(args, "asm_only", False):
        out_names = getattr(args, "output_name", None)
        if out_names and len(out_names) == 1 and out_names[0] != asm_file:
            import shutil
            shutil.copyfile(asm_file, out_names[0])
            return out_names[0]
        return asm_file

    assemble(asm_file, obj_file)
    if not error_collector.ok():
        return None

    return obj_file


class Arguments:
    """Concrete, rpython-friendly replacement for the argparse Namespace:
    every option is a real field with a fixed type, so attribute access
    compiles to a struct member read instead of a dynamic getattr."""

    def __init__(self):
        self.files = []
        self.show_reg_alloc_perf = False
        self.simd_pack_globals = False
        self.stackless_calls = False
        self.pack_args = False
        self.metamorphic = False
        self.compile_only = False
        self.asm_only = False
        self.eliminate_unused_members = False
        self.print_eliminated_members = False
        self.long_double_as_double = False
        self.low_mem = False
        self.pointer_compression = False
        self.opt_level = 0
        self.target = "x86_64"
        self.output_name = None
        self.include_dirs = []
        self.defines = []
        self.use_musl = False
        self.musl_dir = None
        self.print_call_graph = False
        self.emit_thread_switcher = None
        self.thread_alloc_json = None
        self.no_cache = False
        self.check_memory = False
        self.auto_free = False
        self.no_peephole = False
        self.microslice = False
        self.slice_budget = None
        self.emit_microslice = None
        self.pdf = None
        self._thread_alloc = None
        self._extra_objs = []
        self._wp_graph = None
        self._simd_pack_layout = None
        self._inline_bodies = None


def _parse_args_selfhost(argv):
    """Minimal command-line parser for the self-hosted build (no argparse).
    Handles the flags the compiler itself needs; unknown flags are ignored
    and everything else is treated as an input file."""
    args = Arguments()
    i = 1
    n = len(argv)
    while i < n:
        a = argv[i]
        if a == '-c':
            args.compile_only = True
        elif a == '-S':
            args.asm_only = True
        elif a == '-o':
            i += 1
            if i < n:
                args.output_name = [argv[i]]
        elif a == '-O':
            i += 1
            if i < n:
                args.opt_level = int(argv[i])
        elif len(a) > 2 and a[0] == '-' and a[1] == 'O':
            args.opt_level = int(a[2:])
        elif a == '-I':
            i += 1
            if i < n:
                args.include_dirs.append(argv[i])
        elif len(a) > 2 and a[0] == '-' and a[1] == 'I':
            args.include_dirs.append(a[2:])
        elif a == '-D':
            i += 1
            if i < n:
                args.defines.append(argv[i])
        elif len(a) > 2 and a[0] == '-' and a[1] == 'D':
            args.defines.append(a[2:])
        elif a == '--musl':
            args.use_musl = True
        elif a == '--musl-dir':
            i += 1
            if i < n:
                args.musl_dir = argv[i]
        elif a == '--target':
            i += 1
            if i < n:
                args.target = argv[i]
        elif a == '--no-cache':
            args.no_cache = True
        elif a == '--no-peephole':
            args.no_peephole = True
        elif a == '-fsimd-pack-globals':
            args.simd_pack_globals = True
        elif a == '-fstackless-calls':
            args.stackless_calls = True
        elif a == '-f-pack-args':
            args.pack_args = True
        elif a == '-fmetamorphic':
            args.metamorphic = True
        elif a == '-f-eliminate-unused-members':
            args.eliminate_unused_members = True
        elif a == '-f-long-double-as-double':
            args.long_double_as_double = True
        elif a == '-f-low-mem':
            args.low_mem = True
        elif a == '-f-pointer-compression':
            args.pointer_compression = True
            args.low_mem = True   # 32-bit pointers require the low-4GiB image
        elif len(a) > 0 and a[0] == '-':
            pass
        else:
            args.files.append(a)
        i += 1
    return args


def get_arguments(argv=None):
    """Get the command-line arguments.

    This function sets up the argument parser. Returns a tuple containing
    an object storing the argument values and a list of the file names
    provided on command line.
    """
    if sys.implementation.name != "shivyc":
        desc = """Compile, assemble, and link C files. Option flags starting
        with `-z` are primarily for debugging or diagnostic purposes."""
        parser = argparse.ArgumentParser(
            prog='ShivyC',
            description=desc,
            usage="shivyc [-h] [options] files...")

        # Files to compile
        parser.add_argument("files", metavar="files", nargs="+")

        # Boolean flag for whether to print register allocator performance info
        parser.add_argument("-z-reg-alloc-perf",
                            help="display register allocator performance info",
                            dest="show_reg_alloc_perf", action="store_true")

        # Pack small (1-8 bit) static global flags into the last SIMD register
        # (xmm15) for zero-latency reads in hot / interrupt routines.
        parser.add_argument("-fsimd-pack-globals",
                            help="pack small global flags into xmm15",
                            dest="simd_pack_globals", action="store_true")

        # Lower deeply-nested calls with direct calls, tail-call jumps, and
        # frame-pointer omission to cut call overhead.
        parser.add_argument("-fstackless-calls",
                            help="direct calls, tail-call jmps, frameless funcs",
                            dest="stackless_calls", action="store_true")

        # Argument packing: a non-standard calling convention that bit-packs several
        # small integer parameters into as few argument registers as possible.
        # Applied to statically-known (direct) calls of qualifying functions only.
        parser.add_argument("-f-pack-args",
                            help="pack small integer args into shared registers",
                            dest="pack_args", action="store_true")

        # Advanced/experimental: metamorphic returns. Requires a writable text
        # segment (the linker is told to make it writable). The return address is
        # patched into the callee's code by the caller, so no return address is
        # pushed. Enable per-function with the __metamorphic__ specifier.
        parser.add_argument("-fmetamorphic",
                            help="enable metamorphic returns (writable .text)",
                            dest="metamorphic", action="store_true")

        parser.add_argument("-c",
                            help="compile and assemble to .o, but do not link",
                            dest="compile_only", action="store_true")

        parser.add_argument("-S",
                            help="emit assembly (.s) and stop; do not assemble or "
                                 "link",
                            dest="asm_only", action="store_true")

        parser.add_argument(
            "-f-eliminate-unused-members",
            help="whole-program: remove struct members never accessed in any "
                 "translation unit (only when provably safe)",
            dest="eliminate_unused_members", action="store_true")

        parser.add_argument(
            "--print-eliminated-members",
            help="report struct members removed by -f-eliminate-unused-members",
            dest="print_eliminated_members", action="store_true")

        parser.add_argument(
            "-f-long-double-as-double",
            help="treat 'long double' as 64-bit double (with a warning); this "
                 "compiler never supports 80-bit floats",
            dest="long_double_as_double", action="store_true")

        parser.add_argument(
            "-f-low-mem",
            help="link the output as a non-PIE executable based in the low "
                 "32-bit address range (text segment at 0x400000). Every "
                 "static address then fits in 32 bits, so a 64-bit register "
                 "holding such a pointer is zero-extended for free -- the "
                 "groundwork for a future -f-pointer-compression that shrinks "
                 "in-struct pointers to 32 bits",
            dest="low_mem", action="store_true")

        parser.add_argument(
            "-f-pointer-compression",
            help="compile data/object pointers as 4 bytes instead of 8 "
                 "(V8/x32-style). Implies -f-low-mem: the image is based in "
                 "the low 4 GiB so every address fits in 32 bits and is "
                 "recovered by zero-extension. Shrinks pointer-dense data and "
                 "struct pointer fields. NOTE: addresses handed to the program "
                 "must lie in the low 4 GiB (globals/.text and the compression "
                 "arena do; see limitations re: stack addresses and 64-bit "
                 "libc calls)",
            dest="pointer_compression", action="store_true")

        # Optimization level. -O4 is aggressive and, like -fmetamorphic, depends on
        # a writable text segment; it turns on whole-program stackless lowering and
        # near-function scratch storage to reduce stack pressure.
        parser.add_argument("-O", type=int, default=0,
                            help="optimization level (0-4); 4 needs writable .text",
                            dest="opt_level")
        # Back-end architecture. x86-64 is the original, fully-supported target;
        # arm64/aarch64 is the in-progress cross target (see shivyc/targets).
        parser.add_argument("--target", dest="target", default="x86_64",
                            help="back-end architecture: x86_64 (default), arm64")
        # Generate binary file with file name
        parser.add_argument(
            "-o",
            nargs=1,
            metavar="file",
            help="place output into <file>",
            dest="output_name")

        # Additional directories searched for #include files.
        parser.add_argument("-I", metavar="dir", dest="include_dirs",
                            action="append", default=[],
                            help="add a directory to the include search path")

        # Predefine a macro (NAME or NAME=VALUE), like the C compiler's -D.
        parser.add_argument("-D", metavar="name[=value]", dest="defines",
                            action="append", default=[],
                            help="predefine a preprocessor macro")

        # Link against a library, like the C compiler's -l (e.g. -lwayland-client
        # -lm). Passed through to ld. -lc and -lm are always linked.
        parser.add_argument("-l", metavar="lib", dest="libs",
                            action="append", default=[],
                            help="link against the named library")

        # Add a directory to the library search path, like -L. Passed to ld.
        parser.add_argument("-L", metavar="dir", dest="lib_dirs",
                            action="append", default=[],
                            help="add a directory to the library search path")

        # Export the executable's global symbols into the dynamic symbol table
        # (like the C compiler's -rdynamic / --export-dynamic), so a library the
        # program dlopen()s at run time can resolve back into it. minibrowser
        # needs this: its JIT'd page-code .so files call host DOM helpers.
        parser.add_argument("-rdynamic", "--export-dynamic",
                            dest="export_dynamic", action="store_true",
                            help="add all symbols to the dynamic symbol table")

        # Compile against the packaged musl libc (bypassing glibc). Materializes
        # musl's headers to a temp dir and prepends their include paths + defines,
        # so user code resolves #include against musl. The needed musl .c sources
        # can then be extracted/compiled on demand (see shivyc.musl).
        parser.add_argument("--musl", dest="use_musl", action="store_true",
                            help="compile against the packaged musl libc, not glibc")
        parser.add_argument("--musl-dir", dest="musl_dir", default=None,
                            help="where to materialize musl headers/sources "
                                 "(default: a temp directory)")

        # Build and print the whole-program (cross-TU) call graph, then exit.
        parser.add_argument("--print-call-graph", dest="print_call_graph",
                            action="store_true",
                            help="print the cross-translation-unit call graph")

        # Register-partitioned threads (bare-metal).
        parser.add_argument("--emit-thread-switcher", dest="emit_thread_switcher",
                            metavar="OUT.s", default=None,
                            help="analyze threads.left/right declarations across "
                                 "the inputs and write a specialized context "
                                 "switcher to OUT.s, then exit")
        parser.add_argument("--thread-alloc-json", dest="thread_alloc_json",
                            metavar="FILE", default=None,
                            help="constrain each function's register pool to the "
                                 "{func: [reg,...]} budget in FILE (used by the "
                                 "thread partitioner to make footprints disjoint)")

        # Disable the on-disk AST parse cache.
        parser.add_argument("--no-cache", dest="no_cache", action="store_true",
                            help="disable the on-disk parsed-AST cache")

        # Whole-program memory-safety analysis.
        parser.add_argument("--check-memory", dest="check_memory",
                            action="store_true",
                            help="run the whole-program use-after-free / double-free "
                                 "analysis over the inputs and exit")
        parser.add_argument("--auto-free", dest="auto_free", action="store_true",
                            help="with --check-memory, also report (and where safe, "
                                 "insert) automatic frees for non-escaping allocations")

        parser.add_argument("--no-peephole", dest="no_peephole",
                            action="store_true",
                            help="disable the IL peephole optimizer "
                                 "(compare-and-branch fusion, arithmetic identities)")

        # Micro-slicing: productive spin-waiting analysis.
        parser.add_argument("--microslice", dest="microslice", action="store_true",
                            help="analyze the inputs for pure, independent fragments "
                                 "and print a slice plan for productive spinning")
        parser.add_argument("--slice-budget", dest="slice_budget", type=int,
                            metavar="N", default=None,
                            help="per-slice cost budget for --microslice (default 64)")
        parser.add_argument("--emit-microslice", dest="emit_microslice",
                            metavar="OUT.c", default=None,
                            help="with --microslice, write a work-injected acquire "
                                 "scaffold for the hottest fragment to OUT.c")

        parser.add_argument("--pdf", dest="pdf", nargs="?", const="/tmp",
                            default=None, metavar="DIR",
                            help="generate a LaTeX/PDF build report (overview, "
                                 "per-module sections, safety findings in red, a "
                                 "TikZ call graph, and the run output in an "
                                 "appendix). Output directory defaults to /tmp.")

        return parser.parse_args()

    return _parse_args_selfhost(argv)


def read_file(file):
    """Return the contents of the given file."""
    try:
        with open(file) as c_file:
            return c_file.read()
    except IOError:
        descrip = f"could not read file: '{file}'"
        error_collector.add(CompilerError(descrip))


def write_asm(asm_source, asm_filename):
    """Save the given assembly source to disk at asm_filename.

    asm_source (str) - Full assembly source code.
    asm_filename (str) - Filename to which to save the generated assembly.

    """
    try:
        with open(asm_filename, "w") as s_file:
            s_file.write(asm_source)
    except IOError:
        descrip = f"could not write output file '{asm_filename}'"
        error_collector.add(CompilerError(descrip))


def assemble(asm_name, obj_name):
    """Assemble the given assembly file into an object file.

    With SHIVYC_RASM set, use the self-hosted rasm assembler (written in the
    restricted-Python dialect) instead of the external GNU assembler, taking a
    step toward a fully self-contained toolchain.
    """
    import os
    if os.environ.get("SHIVYC_RASM"):
        try:
            _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            _rlib = os.path.join(_root, "tools", "rpy_lib")
            if _rlib not in sys.path:
                sys.path.insert(0, _rlib)
            import rasm_obj
            with open(asm_name) as _f:
                _elf = rasm_obj.assemble_to_elf(_f.read())
            with open(obj_name, "wb") as _o:
                _o.write(bytes(_elf))
            return True
        except Exception as _e:
            error_collector.add(CompilerError("rasm assembler failed: %s" % _e))
            return False
    if sys.implementation.name != 'shivyc':
        try:
            subprocess.check_call(["as", "-o", obj_name, asm_name])
            return True
        except subprocess.CalledProcessError:
            err = "assembler returned non-zero status"
            error_collector.add(CompilerError(err))
            return False
    else:
        # Simpler self-hosted path (uses os.system, already supported by the
        # translator). Breaks on paths with spaces, which is acceptable here.
        os.system("as -o %s %s" % (obj_name, asm_name))
        return True


def link_objs(binary_name, obj_names, writable_text=False, low_mem=False,
              libs=None, lib_dirs=None, export_dynamic=False):
    """Assemble the given object files into a binary.

    `libs` / `lib_dirs` add `-l<name>` / `-L<dir>` to the ld command (like the C
    compiler's -l/-L); -lc and -lm are always linked. `export_dynamic` adds
    `--export-dynamic` (the -rdynamic flag) so a run-time-dlopen'd library can
    resolve symbols back into this executable -- minibrowser's JIT'd page code
    calls host DOM helpers this way.

    When `writable_text` is set (for -fmetamorphic / -O4), the linker is asked
    to emit a writable, non-page-aligned text segment via `-N` (OMAGIC), so
    self-modifying metamorphic-return code can patch instruction bytes at run
    time. This is intentionally unsafe and opt-in.

    When `low_mem` is set (-f-low-mem), the binary is linked as a non-PIE
    executable based in the low 32-bit address range (text segment at
    0x400000, the classic ET_EXEC base -- well above the kernel's
    vm.mmap_min_addr guard, so it is not rejected). With the whole image below
    4 GiB, every static code/data address fits in 32 bits; on x86-64 a write to
    a 32-bit register zero-extends its 64-bit counterpart, so such a pointer
    needs no base-register add to reconstruct. This is the groundwork for a
    later -f-pointer-compression (32-bit in-struct pointers); the heap side of
    that would additionally map the arena with MAP_32BIT.
    """
    import os

    if os.environ.get("SHIVYC_RLINK"):
        # Self-hosted path: link with our own rlink instead of GNU ld. The
        # result is a static, freestanding ET_EXEC -- no crt files, no dynamic
        # loader, no libc -- so the runtime comes from tools/rpy_lib/rcrt.s
        # (assembled by rasm) rather than from the system. Combined with
        # SHIVYC_RASM this makes the whole compile->assemble->link chain ours.
        try:
            _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            _rlib = os.path.join(_root, "tools", "rpy_lib")
            if _rlib not in sys.path:
                sys.path.insert(0, _rlib)
            import rasm_obj as _rasm_obj
            import rlink as _rlink

            _ln = _rlink.Linker()
            _ln.entry_name = os.environ.get("SHIVYC_ENTRY", "_start")
            if os.environ.get("SHIVYC_BASE"):
                _ln.base = int(os.environ["SHIVYC_BASE"], 0)

            # the freestanding runtime, assembled on the fly by rasm
            if not os.environ.get("SHIVYC_NO_RCRT"):
                _crt_s = os.path.join(_rlib, "rcrt.s")
                with open(_crt_s) as _f:
                    _crt = _rasm_obj.assemble_to_elf(_f.read())
                _ln.add_object(_crt_s, list(_crt))
                # ...and the C half of the runtime (printf, malloc, getenv,
                # syscall wrappers). It is compiled by this same compiler, so
                # it is built once and cached next to the build tree rather
                # than recompiled on every link.
                _libc_c = os.path.join(_rlib, "rlibc.c")
                if os.path.exists(_libc_c):
                    _cache = os.environ.get(
                        "SHIVYC_RLIBC_OBJ",
                        os.path.join(_rlib, "build", "rlibc.o"))
                    _stale = (not os.path.exists(_cache)
                              or os.path.getmtime(_cache)
                              < os.path.getmtime(_libc_c))
                    if _stale:
                        os.makedirs(os.path.dirname(_cache), exist_ok=True)
                        _env = dict(os.environ)
                        _env["SHIVYC_RASM"] = "1"
                        _env.pop("SHIVYC_RLINK", None)
                        _env["PYTHONPATH"] = _root
                        _r = subprocess.run(
                            [sys.executable, "-m", "shivyc.main", "-c",
                             _libc_c, "-o", _cache],
                            cwd=_root, env=_env, capture_output=True)
                        if not os.path.exists(_cache):
                            raise CompilerError(
                                "could not build the rlink C runtime: %s"
                                % _r.stderr.decode()[:200])
                    with open(_cache, "rb") as _f:
                        _ln.add_object(_cache, list(_f.read()))

            for _o in obj_names:
                with open(_o, "rb") as _f:
                    _data = list(_f.read())
                if _rlink._is_archive(_data):
                    _ln.add_archive(_o, _data)
                else:
                    _ln.add_object(_o, _data)
            for _lib in (libs or []):
                _found = None
                for _d in (lib_dirs or []):
                    _cand = os.path.join(_d, "lib" + _lib + ".a")
                    if os.path.exists(_cand):
                        _found = _cand
                        break
                if _found:
                    with open(_found, "rb") as _f:
                        _ln.add_archive(_found, list(_f.read()))

            _image = _ln.link()
            with open(binary_name, "wb") as _f:
                _f.write(bytes(bytearray(_image)))
            os.chmod(binary_name, 0o755)
            for _w in _ln.warnings:
                sys.stderr.write("rlink: warning: %s\n" % _w)
            return True
        except Exception as _e:
            error_collector.add(CompilerError("rlink failed: %s" % _e))
            return False

    crtnum = find_crtnum()
    if not crtnum:
        return False
    crti = find_library_or_err("crti.o")
    if not crti:
        return False
    linux_so = find_library_or_err("ld-linux-x86-64.so.2")
    if not linux_so:
        return False
    crtn = find_library_or_err("crtn.o")
    if not crtn:
        return False

    cmd = ["ld"]
    # Writable text for metamorphic returns is arranged via the .text
    # section's "awx" flag (set in asm_gen), which is compatible with the
    # glibc crt startup; the older -N/OMAGIC route is not.
    if low_mem:
        # Force a fixed, low load address (non-PIE ET_EXEC). -no-pie keeps ld
        # from emitting a position-independent ET_DYN that the loader would
        # place at a random high address.
        cmd += ["-no-pie", "-Ttext-segment=0x400000"]
    if export_dynamic:
        cmd += ["--export-dynamic"]
    for d in (lib_dirs or []):
        cmd += ["-L" + d]
    cmd += ["-dynamic-linker", linux_so, crtnum, crti]
    cmd = cmd + obj_names
    cmd += ["-lc", "-lm"]
    for lib in (libs or []):
        cmd += ["-l" + lib]
    cmd += [crtn, "-o", binary_name]

    if sys.implementation.name != 'shivyc':
        try:
            subprocess.check_call(cmd)
            return True
        except subprocess.CalledProcessError:
            return False
    else:
        os.system(" ".join(cmd))
        return True


def find_crtnum():
    """Search for the crt0, crt1, or crt2.o files on the system.

    If one is found, return its path. Else, add an error to the
    error_collector and return None.
    """
    for file in ["crt2.o", "crt1.o", "crt0.o"]:
        crt = find_library(file)
        if crt: return crt

    err = "could not find crt0.o, crt1.o, or crt2.o for linking"
    error_collector.add(CompilerError(err))
    return None


def find_library_or_err(file):
    """Search the given library file and return path if found.

    If not found, add an error to the error collector and return None.
    """
    path = find_library(file)
    if not path:
        err = f"could not find {file}"
        error_collector.add(CompilerError(err))
        return None
    else:
        return path


def find_library(file):
    """Search the given library file by searching in common directories.

    If found, returns the path. Otherwise, returns None.
    """
    import os
    search_paths = ["/usr/local/lib/x86_64-linux-gnu",
                    "/lib/x86_64-linux-gnu",
                    "/usr/lib/x86_64-linux-gnu",
                    "/usr/local/lib64",
                    "/lib64",
                    "/usr/lib64",
                    "/usr/local/lib",
                    "/lib",
                    "/usr/lib",
                    "/usr/x86_64-linux-gnu/lib64",
                    "/usr/x86_64-linux-gnu/lib"]

    for path in search_paths:
        full = os.path.join(path, file)
        if os.path.exists(full):
            return full
    return None


if __name__ == "__main__":
    # Machine-generated C (e.g. py2c's output for the minipy interpreter) nests
    # expressions deeply, which the recursive-descent parser follows frame for
    # frame. Raise the Python recursion limit, and run on a large-stack thread
    # so the deeper limit can't overflow the C stack (a mere setrecursionlimit
    # would segfault instead of erroring). Only used for the shivyc.main CLI;
    # the transpiled/self-hosted compiler has its own native stack.
    if sys.implementation.name != "shivyc":
        import threading
        sys.setrecursionlimit(200000)
        threading.stack_size(1024 * 1024 * 1024)
        _result = []
        _t = threading.Thread(target=lambda: _result.append(main()))
        _t.start()
        _t.join()
        sys.exit(_result[0] if _result else 1)
    sys.exit(main())
