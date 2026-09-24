"""Implementation of the ShivyC preprocessor.

This preprocessor handles the directives real C code (and library headers like
musl's) actually use:

* ``#include`` (quoted and angle-bracket headers)
* ``#define`` -- object-like and function-like macros, including ``#``
  (stringize), ``##`` (token paste), and variadic ``...`` / ``__VA_ARGS__``
* ``#undef``
* ``#if`` / ``#ifdef`` / ``#ifndef`` / ``#elif`` / ``#else`` / ``#endif`` with
  full integer constant-expression evaluation and the ``defined`` operator
* ``#error`` (reported) and ``#pragma`` / ``#line`` (ignored)

Macro expansion uses hide sets so a macro is never re-expanded inside its own
expansion, matching the C rule that prevents infinite recursion.

The implementation operates on the lexer's token stream. Tokens carry source
line numbers, which is what lets directives (which are line-oriented) be picked
out of the flat token list.
"""

import os
import sys

import shivyc.lexer as lexer
import shivyc.pack as pack
import shivyc.token_kinds as token_kinds
from shivyc.tokens import Token, parse_c_int
from shivyc.errors import error_collector, CompilerError, Position, Range


# Where lowered `.cpp` / `.cs` sources are staged. Same convention as the
# rpython include cache: a fixed root under /tmp, overridable for sandboxed
# builds.
CPP_CACHE_ROOT = os.environ.get("CRUST_CPP_CACHE", "/tmp/crust-cpp")


def _tool_script(basename, env_var):
    """Path to tools/<basename>.py, or "" if it cannot be found.

    Two layouts have to work. On the host, `__file__` is `.../shivyc/
    preproc.py`, so the repo root is two directories up. Self-hosted, py2c
    compiles `__file__` down to the bare string "preproc.py", so `abspath`
    resolves it against the current directory and only one level should be
    stripped. Rather than guess which world we are in, try both -- plus the
    working directory, and an explicit override for installs where the
    compiler does not sit next to its sources.
    """
    env = os.environ.get(env_var)
    if env:
        return env if os.path.exists(env) else ""
    here = os.path.dirname(os.path.abspath(__file__))
    roots = [os.path.dirname(here), here, os.getcwd()]
    for root in roots:
        cand = os.path.join(root, "tools", basename)
        if os.path.exists(cand):
            return cand
    return ""


def _cpprust_script():
    return _tool_script("cpprust.py", "CRUST_CPPRUST")


def _csrust_script():
    return _tool_script("csrust.py", "CRUST_CSRUST")


def _read_or(path, fallback):
    """The child's diagnostic, or `fallback` if it did not write one."""
    try:
        with open(path) as f:
            msg = f.read().strip()
        return msg or fallback
    except IOError:
        return fallback


def _run_transpiler(script, filename, text, tag):
    """Lower a language-subset include out-of-process.

    Returns `(True, lowered_source)` or `(False, diagnostic)`.

    The script writes its output to a file, and on failure writes the
    diagnostic to that same file with a non-zero exit status. One file and one
    status is all the protocol needs, which matters for the self-hosted path
    below: it drives the child through `os.system`, where capturing a pipe is
    not available.
    """
    if not script:
        return False, ("cannot find tools/%s; set $CRUST_CPPRUST / "
                       "$CRUST_CSRUST to its path" % tag)

    try:
        os.makedirs(CPP_CACHE_ROOT)
    except OSError:
        pass
    stem = os.path.basename(filename)
    src = os.path.join(CPP_CACHE_ROOT, stem + ".in")
    out = os.path.join(CPP_CACHE_ROOT, stem + ".out.c")

    # Hand the child the *current* text rather than the path on disk: an
    # earlier stage may already have rewritten it, so the file on disk is not
    # necessarily what is being compiled.
    try:
        with open(src, "w") as f:
            f.write(text)
    except IOError as e:
        return False, "cannot stage %s: %s" % (src, e)
    if os.path.exists(out):
        os.remove(out)

    # Which Crust types own something? A C++/C# class may hold one by value,
    # and the child cannot see the unit being compiled to find out for itself.
    # Read from the module rather than threaded through every caller: the
    # Crust pass runs to completion before the preprocessor starts, so this is
    # a finished fact by the time it is read, not shared mutable state.
    import shivyc.crust as _crust
    owned = getattr(_crust, "OWNING_TYPES", None) or {}
    spec = ",".join("%s:%s" % (n, owned[n]) for n in sorted(owned))

    # Where the source really lives, not where it was staged: its own
    # `#include "x.h"` are relative to it, and the child expands those itself
    # so that a class declared in a header meets the definitions here.
    basedir = os.path.dirname(os.path.abspath(filename)) or "."

    if sys.implementation.name != "shivyc":
        import subprocess
        cmd = [sys.executable, script, src, "-o", out, "--basedir", basedir]
        for d in _extra_include_dirs:
            cmd += ["--incdir", d]
        if spec:
            cmd += ["--owning", spec]
        try:
            proc = subprocess.run(cmd,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        except OSError as e:
            return False, "cannot run %s: %s" % (script, e)
        if proc.returncode != 0:
            msg = _read_or(out, "")
            if not msg:
                msg = proc.stderr.decode("utf-8", "replace").strip()
            return False, msg or "translation failed"
    else:
        # Self-hosted: no subprocess module here, and a path with spaces
        # would break. Acceptable for the same reason it is in main.py.
        # stderr is dropped because the message is read back from `out`.
        extra = (" --owning " + spec) if spec else ""
        for d in _extra_include_dirs:
            extra += " --incdir " + d
        rc = os.system("python3 " + script + " " + src + " -o " + out +
                       " --basedir " + basedir + extra + " 2>/dev/null")
        if rc != 0:
            return False, _read_or(out, "translation failed")

    if not os.path.exists(out):
        return False, "produced no output"
    with open(out) as f:
        return True, f.read()


def _run_cpprust(filename, text):
    """Lower a `.cpp` include out-of-process."""
    return _run_transpiler(_cpprust_script(), filename, text, "cpprust.py")


def _run_csrust(filename, text):
    """Lower a `.cs` include out-of-process (cs2cpp → cpprust)."""
    return _run_transpiler(_csrust_script(), filename, text, "csrust.py")


def process(tokens, this_file, macros=None):
    """Process the given tokens and return the preprocessed token list."""
    if macros is None:
        macros = {}
        _seed_builtins(macros)
    return _Preprocessor(macros).run(tokens, this_file)


# GCC/C extension spellings that appear throughout library headers. These are
# "hint" constructs ShivyC does not act on, so stripping them (or mapping them
# to their plain-C equivalent) lets the headers parse. NOTE: this is
# deliberately limited to constructs that are safe to ignore. Semantically
# essential extensions -- inline `__asm__` and the `weak`/`alias` attributes --
# are NOT silently stripped here, because doing so would produce incorrect code
# rather than a parse. The type qualifiers
# `const`, `volatile`, `restrict`, and `_Atomic` are recognized natively by the
# parser (and ignored, which is sound for a single-threaded target), so only
# their GCC double-underscore spellings need mapping here. Thread-local storage
# (`_Thread_local`/`__thread`) is mapped to ordinary storage: with a single
# thread of execution there is exactly one instance, so this is sound here.
_BUILTIN_PRELUDE = r"""
#define __extension__
#define __restrict
#define __restrict__
#define __inline
#define __inline__
#define inline
#define _Noreturn
#define _Thread_local
#define __thread
#define __volatile__
#define __volatile
#define __asm__ asm
#define __asm asm
#define __signed__ signed
#define __const const
#define typeof __typeof__
#define __typeof __typeof__
#define __builtin_expect(x, c) (x)
#define _Static_assert(...)
#define static_assert(...)
"""

# ISO/IEC TS 18661-3 / C23 interchange & extended floating types, declared
# throughout glibc's <math.h>/<bits/floatn.h>. ShivyCX has no >64-bit float, so
# each spelling is mapped to its nearest native type. The 32/64-bit forms are
# exact; the wider forms (_Float64x/_Float128) are approximated as double, which
# is sound for bring-up because they appear only in pulled-in libm declarations,
# never in computation in the target sources.
_FLOATN_PRELUDE = r"""
#define _Float16 float
#define _Float32 float
#define _Float32x double
#define _Float64 double
#define _Float64x double
#define _Float128 double
#define _Float128x double
"""
_BUILTIN_PRELUDE += _FLOATN_PRELUDE

# GCC `__int128` / `unsigned __int128` 128-bit integers. ShivyCX has no
# 128-bit integer type, so these are approximated as 64-bit `long long` for
# bring-up. This is sound only because the target sources use them solely in
# pulled-in glibc-internal typedefs, never in 128-bit computation; real
# 128-bit arithmetic is a separate, unsupported feature.
_BUILTIN_PRELUDE += "#define __int128 long long\n"
_BUILTIN_PRELUDE += "#define __int128_t long long\n"
_BUILTIN_PRELUDE += "#define __uint128_t unsigned long long\n"

# A handful of GCC builtins used by glibc's byte-order inlines. Provided as
# portable shift/mask macros (the operands here are simple values, so the
# double-evaluation a macro implies is harmless).
_BUILTIN_PRELUDE += (
    "#define __builtin_bswap16(x) "
    "((unsigned short)((((unsigned short)(x) >> 8) & 0xff) "
    "| (((unsigned short)(x) & 0xff) << 8)))\n"
    "#define __builtin_bswap32(x) "
    "((unsigned int)((((unsigned int)(x) & 0xff000000u) >> 24) "
    "| (((unsigned int)(x) & 0x00ff0000u) >> 8) "
    "| (((unsigned int)(x) & 0x0000ff00u) << 8) "
    "| (((unsigned int)(x) & 0x000000ffu) << 24)))\n"
    "#define __builtin_bswap64(x) "
    "((unsigned long)("
    "(((unsigned long)(x) & 0xff00000000000000ul) >> 56) "
    "| (((unsigned long)(x) & 0x00ff000000000000ul) >> 40) "
    "| (((unsigned long)(x) & 0x0000ff0000000000ul) >> 24) "
    "| (((unsigned long)(x) & 0x000000ff00000000ul) >> 8) "
    "| (((unsigned long)(x) & 0x00000000ff000000ul) << 8) "
    "| (((unsigned long)(x) & 0x0000000000ff0000ul) << 24) "
    "| (((unsigned long)(x) & 0x000000000000ff00ul) << 40) "
    "| (((unsigned long)(x) & 0x00000000000000fful) << 56)))\n"
)

# GCC __atomic_* builtins. ShivyCX targets a single thread of execution, so each
# is implemented as the equivalent plain memory operation (the memory-order
# argument is irrelevant with one thread). The read-modify-write forms use the
# statement-expression + __auto_type extensions to evaluate the pointer once and
# return the documented value (old value for __atomic_fetch_*, new value for
# __atomic_*_fetch). This is sound for the single-threaded target and lets
# CPython's pyatomic_gcc.h compile unchanged.
_ATOMIC_PRELUDE = r"""
#define __atomic_load_n(p, m) (*(p))
#define __atomic_store_n(p, v, m) ((void)(*(p) = (v)))
#define __atomic_load(p, r, m) ((void)(*(r) = *(p)))
#define __atomic_store(p, v, m) ((void)(*(p) = *(v)))
#define __atomic_exchange_n(p, v, m) __extension__({ __auto_type _axp = (p); __auto_type _axo = *_axp; *_axp = (v); _axo; })
#define __atomic_exchange(p, v, r, m) ((void)(*(r) = __atomic_exchange_n((p), *(v), (m))))
#define __atomic_fetch_add(p, v, m) __extension__({ __auto_type _afp = (p); __auto_type _afo = *_afp; *_afp = *_afp + (v); _afo; })
#define __atomic_fetch_sub(p, v, m) __extension__({ __auto_type _afp = (p); __auto_type _afo = *_afp; *_afp = *_afp - (v); _afo; })
#define __atomic_fetch_and(p, v, m) __extension__({ __auto_type _afp = (p); __auto_type _afo = *_afp; *_afp = *_afp & (v); _afo; })
#define __atomic_fetch_or(p, v, m)  __extension__({ __auto_type _afp = (p); __auto_type _afo = *_afp; *_afp = *_afp | (v); _afo; })
#define __atomic_fetch_xor(p, v, m) __extension__({ __auto_type _afp = (p); __auto_type _afo = *_afp; *_afp = *_afp ^ (v); _afo; })
#define __atomic_add_fetch(p, v, m) __extension__({ __auto_type _afp = (p); *_afp = *_afp + (v); *_afp; })
#define __atomic_sub_fetch(p, v, m) __extension__({ __auto_type _afp = (p); *_afp = *_afp - (v); *_afp; })
#define __atomic_and_fetch(p, v, m) __extension__({ __auto_type _afp = (p); *_afp = *_afp & (v); *_afp; })
#define __atomic_or_fetch(p, v, m)  __extension__({ __auto_type _afp = (p); *_afp = *_afp | (v); *_afp; })
#define __atomic_xor_fetch(p, v, m) __extension__({ __auto_type _afp = (p); *_afp = *_afp ^ (v); *_afp; })
#define __atomic_compare_exchange_n(p, e, d, weak, sm, fm) __extension__({ __auto_type _cp = (p); __auto_type _ce = (e); (*_cp == *_ce) ? ((*_cp = (d)), 1) : ((*_ce = *_cp), 0); })
#define __atomic_compare_exchange(p, e, d, weak, sm, fm) __atomic_compare_exchange_n((p), (e), *(d), weak, sm, fm)
#define __atomic_thread_fence(m) ((void)0)
#define __atomic_signal_fence(m) ((void)0)
#define __atomic_test_and_set(p, m) __extension__({ __auto_type _tp = (p); __auto_type _to = *_tp; *_tp = 1; _to; })
#define __atomic_clear(p, m) ((void)(*(p) = 0))
"""
_BUILTIN_PRELUDE += _ATOMIC_PRELUDE

# Trivially-ignorable GCC hint builtins: alignment assumptions, prefetch, and
# unreachable markers carry no observable semantics for ShivyCX's code
# generation, so they reduce to their argument or a no-op.
_BUILTIN_PRELUDE += (
    "#define __builtin_assume_aligned(p, ...) (p)\n"
    "#define __builtin_prefetch(...) ((void)0)\n"
    "#define __builtin_unreachable() ((void)0)\n"
)

# A few more GCC builtins used by CPython/mimalloc. The infinities rely on the
# fact that a literal beyond the target type's range converts to IEEE infinity;
# the bit-count and overflow forms are exact, written with the statement-
# expression extension so each argument is evaluated once.
_BUILTIN_PRELUDE += (
    "#define __builtin_inff() (1e40f)\n"
    "#define __builtin_inf() (1e400)\n"
    "#define __builtin_huge_valf() (1e40f)\n"
    "#define __builtin_huge_val() (1e400)\n"
    "#define __builtin_nanf(s) (0.0f/0.0f)\n"
    "#define __builtin_clzl(x) __extension__({ unsigned long _clzv=(x); int _clzn=0; if(_clzv==0){_clzn=64;}else{while(!(_clzv & 0x8000000000000000UL)){_clzv<<=1;_clzn++;}} _clzn; })\n"
    "#define __builtin_clzll(x) __builtin_clzl(x)\n"
    "#define __builtin_clz(x)  __extension__({ unsigned int _clzv=(x); int _clzn=0; if(_clzv==0){_clzn=32;}else{while(!(_clzv & 0x80000000U)){_clzv<<=1;_clzn++;}} _clzn; })\n"
    "#define __builtin_ctzl(x) __extension__({ unsigned long _ctzv=(x); int _ctzn=0; if(_ctzv==0){_ctzn=64;}else{while(!(_ctzv & 1UL)){_ctzv>>=1;_ctzn++;}} _ctzn; })\n"
    "#define __builtin_ctzll(x) __builtin_ctzl(x)\n"
    "#define __builtin_ctz(x)  __extension__({ unsigned int _ctzv=(x); int _ctzn=0; if(_ctzv==0){_ctzn=32;}else{while(!(_ctzv & 1U)){_ctzv>>=1;_ctzn++;}} _ctzn; })\n"
    "#define __builtin_popcount(x)   __extension__({ unsigned int _pcv=(x); int _pcn=0; while(_pcv){ _pcn += (int)(_pcv & 1U); _pcv>>=1; } _pcn; })\n"
    "#define __builtin_popcountl(x)  __extension__({ unsigned long _pcv=(x); int _pcn=0; while(_pcv){ _pcn += (int)(_pcv & 1UL); _pcv>>=1; } _pcn; })\n"
    "#define __builtin_popcountll(x) __builtin_popcountl(x)\n"
    "#define __builtin_umull_overflow(a, b, res) __extension__({ unsigned long _moa=(a),_mob=(b); *(res)=_moa*_mob; (_moa!=0 && (*(res)/_moa)!=_mob); })\n"
    "#define __builtin_umulll_overflow(a, b, res) __builtin_umull_overflow(a, b, res)\n"
    "#define __builtin_mul_overflow(a, b, res) __extension__({ __auto_type _moa=(a),_mob=(b); *(res)=_moa*_mob; (_moa!=0 && (*(res)/_moa)!=_mob); })\n"
    "#define __builtin_add_overflow(a, b, res) __extension__({ __auto_type _aoa=(a),_aob=(b); *(res)=_aoa+_aob; (*(res) < _aoa); })\n"
)


def _seed_builtins(macros):
    """Populate `macros` with the GCC-compatibility prelude definitions."""
    pre = _Preprocessor(macros)
    pre.run(lexer.tokenize(_BUILTIN_PRELUDE, "<builtin>"), "<builtin>")
    if _cmdline_define_prelude:
        pre.run(lexer.tokenize(_cmdline_define_prelude, "<command-line>"),
                "<command-line>")


# Command-line ``-D`` definitions, as a synthetic ``#define`` prelude seeded
# after the builtins so they behave exactly like definitions at the top of
# every translation unit.
_cmdline_define_prelude = (
    "#define __SHIVYC__ 1\n"
    "#define __SIZE_TYPE__ unsigned long\n"
    "#define __PTRDIFF_TYPE__ long\n"
    "#define __WCHAR_TYPE__ int\n"
    "#define __INTPTR_TYPE__ long\n"
    "#define __UINTPTR_TYPE__ unsigned long\n"
    "#define __INT64_TYPE__ long\n"
    "#define __UINT64_TYPE__ unsigned long\n"
    "#define __INT32_TYPE__ int\n"
    "#define __UINT32_TYPE__ unsigned int\n"
    "#define __CHAR_BIT__ 8\n"
    "#define __builtin_va_list char *\n"
    "#define __builtin_va_start(ap, last) "
    "((ap) = (char *)__builtin_va_start_addr())\n"
    "#define __builtin_va_end(ap) ((void)((ap) = (char *)0))\n"
    "#define __builtin_va_copy(dst, src) ((dst) = (src))\n")


_target_name = "x86_64"


def set_target(name):
    """Record the target, so target-conditional macros can be predefined."""
    global _target_name
    _target_name = name


_target_os = ""


def set_os(os_name):
    """Record the target OS (canonical name from targets.normalize_os), so
    OS-conditional macros can be predefined. Call before set_defines."""
    global _target_os
    _target_os = os_name or ""


def _defines_name(defines, name):
    """True if the -D list defines `name` (as NAME or NAME=VALUE)."""
    for d in defines or []:
        if d == name or d.startswith(name + "="):
            return True
    return False


# The FreeBSD release series assumed when not compiling on FreeBSD itself.
FREEBSD_DEFAULT_MAJOR = 14


def _freebsd_major():
    """Major version of the FreeBSD host, or FREEBSD_DEFAULT_MAJOR when
    cross-compiling (or when the probe is unavailable, as in the self-hosted
    build, which folds this to the default)."""
    if sys.implementation.name != "shivyc":
        try:
            import platform
            if platform.system() == "FreeBSD":
                return int(platform.release().split(".")[0])
        except Exception:
            pass
    return FREEBSD_DEFAULT_MAJOR


def set_defines(defines: "list[str]"):
    """Record command-line ``-D`` macros (each ``NAME`` or ``NAME=VALUE``).

    The compiler always predefines ``__SHIVYC__`` so source (e.g. a musl built
    for this compiler) can detect it with ``#ifdef __SHIVYC__``.
    """
    global _cmdline_define_prelude
    # __STDC__ is required of every conforming implementation (C11 6.10.8.1).
    # Without it, headers that still carry pre-ANSI support -- the macOS
    # SDK's <sys/cdefs.h> -- take the K&R path and `#define const __const`.
    # __STDC_VERSION__ is deliberately left undefined for now: headers then
    # take their most conservative paths (no `restrict`, `_Noreturn`,
    # `_Static_assert`), and claiming C11 should wait until each is checked.
    lines = ["#define __SHIVYC__ 1",
             "#define __STDC__ 1",
             "#define __STDC_HOSTED__ 1",
             "#define __SIZE_TYPE__ unsigned long",
             "#define __PTRDIFF_TYPE__ long",
             "#define __WCHAR_TYPE__ int",
             "#define __INTPTR_TYPE__ long",
             "#define __UINTPTR_TYPE__ unsigned long",
             "#define __INT64_TYPE__ long",
             "#define __UINT64_TYPE__ unsigned long",
             "#define __INT32_TYPE__ int",
             "#define __UINT32_TYPE__ unsigned int",
             "#define __CHAR_BIT__ 8",
             "#define __builtin_va_list char *",
             "#define __builtin_va_start(ap, last) "
             "((ap) = (char *)__builtin_va_start_addr())",
             "#define __builtin_va_end(ap) ((void)((ap) = (char *)0))",
             "#define __builtin_va_copy(dst, src) ((dst) = (src))",
             # Feature-test operators (clang; GCC 10+; __has_include is C23).
             # Each answers honestly for this compiler: no clang language
             # features, attributes or builtins beyond what is predefined
             # here. Headers written for clang are built to take their
             # portable fallback when told "no" -- the macOS SDK drops its
             # nullability qualifiers and availability attributes that way.
             # Being macros, they also satisfy `#ifdef __has_feature` and
             # `defined(__has_feature)`. __has_include is a placeholder for
             # those two tests only: #if evaluates it by searching the
             # include path (see _has_include).
             "#define __has_feature(x) 0",
             "#define __has_extension(x) 0",
             "#define __has_attribute(x) 0",
             "#define __has_c_attribute(x) 0",
             "#define __has_cpp_attribute(x) 0",
             "#define __has_declspec_attribute(x) 0",
             "#define __has_builtin(x) 0",
             "#define __has_include(x) 0",
             "#define __has_include_next(x) 0"]
    if _target_name == "wasm":
        # Lets a header select the wasm spelling of an intrinsic, exactly as
        # it would under clang. shivyc/include/wasm_simd128.h uses it to pick
        # between the builtins and a portable scalar fallback.
        lines.append("#define __wasm__ 1")
        lines.append("#define __wasm32__ 1")
    if _target_os == "macos":
        # What clang predefines for arm64-apple-macos11 that portable code
        # and the SDK headers test for: the platform, the architecture, the
        # data model, and the deployment target (as the SDK's Availability
        # headers expect to find it, 11.0 -> 110000).
        lines.append("#define __APPLE__ 1")
        lines.append("#define __MACH__ 1")
        lines.append("#define __aarch64__ 1")
        lines.append("#define __arm64__ 1")
        lines.append("#define __LP64__ 1")
        lines.append("#define _LP64 1")
        lines.append("#define __LITTLE_ENDIAN__ 1")
        lines.append("#define __ENVIRONMENT_OS_VERSION_MIN_REQUIRED__ 110000")
        lines.append(
            "#define __ENVIRONMENT_MAC_OS_X_VERSION_MIN_REQUIRED__ 110000")
    if _target_os == "windows":
        # 64-bit Windows is LLP64: `long` is 4 bytes, so every 64-bit type
        # macro above has to be respelled `long long` (the same 8-byte type
        # as LP64's `long` -- Crust folds the two). Then what MinGW-w64 gcc
        # and MSVC predefine for x64 that portable code tests for. Neither
        # __GNUC__ nor _MSC_VER is claimed: Crust is neither compiler, and
        # headers that key on those expect extensions it does not have.
        # __MINGW64__ likewise stays undefined (Crust links msvcrt.dll
        # directly, with no MinGW runtime underneath).
        llp = {"__SIZE_TYPE__": "unsigned long long",
               "__PTRDIFF_TYPE__": "long long",
               "__INTPTR_TYPE__": "long long",
               "__UINTPTR_TYPE__": "unsigned long long",
               "__INT64_TYPE__": "long long",
               "__UINT64_TYPE__": "unsigned long long",
               "__WCHAR_TYPE__": "unsigned short"}
        i = 0
        while i < len(lines):
            parts = lines[i].split(" ", 2)
            if len(parts) == 3 and parts[1] in llp:
                lines[i] = "#define %s %s" % (parts[1], llp[parts[1]])
            i += 1
        lines.append("#define _WIN32 1")
        lines.append("#define _WIN64 1")
        lines.append("#define __x86_64__ 1")
        lines.append("#define __x86_64 1")
        lines.append("#define __amd64__ 1")
        lines.append("#define __amd64 1")
        lines.append("#define _M_X64 100")
        lines.append("#define _M_AMD64 100")
        lines.append("#define __LLP64__ 1")
        lines.append("#define __LITTLE_ENDIAN__ 1")
        lines.append("#define __SIZEOF_LONG__ 4")
        lines.append("#define __SIZEOF_LONG_LONG__ 8")
        lines.append("#define __SIZEOF_POINTER__ 8")
        lines.append("#define __SIZEOF_WCHAR_T__ 2")
        # The bit builtins above are written for LP64: the `l` forms hold
        # their operand in `unsigned long` but test 64-bit masks, and the
        # `ll` forms alias them. Under LLP64 that is a 32-bit variable
        # tested against bit 63 -- `__builtin_clzl(1)` never terminated. So
        # here `l` means 32 bits, as it does for MinGW gcc, and `ll` is
        # spelled out at 64.
        for nm in ("__builtin_clzl", "__builtin_ctzl", "__builtin_clzll",
                   "__builtin_ctzll", "__builtin_popcountll",
                   "__builtin_umulll_overflow"):
            lines.append("#undef " + nm)
        lines.append("#define __builtin_clzl(x) __builtin_clz(x)")
        lines.append("#define __builtin_ctzl(x) __builtin_ctz(x)")
        lines.append(
            "#define __builtin_clzll(x) __extension__({ unsigned long long "
            "_clzv=(x); int _clzn=0; if(_clzv==0){_clzn=64;}else{while(!("
            "_clzv & 0x8000000000000000ULL)){_clzv<<=1;_clzn++;}} _clzn; })")
        lines.append(
            "#define __builtin_ctzll(x) __extension__({ unsigned long long "
            "_ctzv=(x); int _ctzn=0; if(_ctzv==0){_ctzn=64;}else{while(!("
            "_ctzv & 1ULL)){_ctzv>>=1;_ctzn++;}} _ctzn; })")
        lines.append(
            "#define __builtin_popcountll(x) __extension__({ unsigned long "
            "long _pcv=(x); int _pcn=0; while(_pcv){ _pcn += (int)(_pcv & "
            "1ULL); _pcv>>=1; } _pcn; })")
        lines.append(
            "#define __builtin_umulll_overflow(a, b, res) __extension__({ "
            "unsigned long long _moa=(a),_mob=(b); *(res)=_moa*_mob; "
            "(_moa!=0 && (*(res)/_moa)!=_mob); })")
    if _target_os == "freebsd":
        # What FreeBSD's cc predefines for amd64 that portable code tests
        # for (checked against `cc -dM -E` on FreeBSD 15.1). __FreeBSD__ is
        # the major release: the host's own when compiling on FreeBSD, else
        # 14, the oldest series still supported; -D__FreeBSD__=N overrides.
        if not _defines_name(defines, "__FreeBSD__"):
            lines.append("#define __FreeBSD__ %d" % _freebsd_major())
        lines.append("#define __unix__ 1")
        lines.append("#define __unix 1")
        lines.append("#define __ELF__ 1")
        lines.append("#define __LP64__ 1")
        lines.append("#define _LP64 1")
        lines.append("#define __x86_64__ 1")
        lines.append("#define __x86_64 1")
        lines.append("#define __amd64__ 1")
        lines.append("#define __amd64 1")
    for d in defines or []:
        name, eq, val = d.partition("=")
        lines.append("#define %s %s" % (name, val if eq else "1"))
    _cmdline_define_prelude = "\n".join(lines) + ("\n" if lines else "")


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def spell(tok):
    """Return the source spelling of a token."""
    if tok.rep:
        return tok.rep
    if isinstance(tok.content, str):
        return tok.content
    return str(tok.kind)


def is_ident(tok):
    return tok.kind is token_kinds.identifier


def ident_name(tok):
    return tok.content if tok.kind is token_kinds.identifier else None


def is_punct(tok, s):
    return spell(tok) == s


def _relex(text, r):
    """Lex `text` into tokens, giving each the range `r` for diagnostics."""
    fname = r.start.file if r and r.start else "<pp>"
    toks = lexer.tokenize(text, fname)
    for t in toks:
        t.r = r
    return toks


def _num_token(value, r):
    """Build a number token for an integer value."""
    return Token(token_kinds.number, str(value), r=r)


def _coalesce(tokens):
    """Rejoin preprocessor-only multi-character tokens the lexer splits.

    ShivyC's lexer does not know the preprocessor tokens ``##`` (paste) or
    ``...`` (variadic), emitting them as separate ``#`` or ``.`` tokens. We
    rejoin them only when the pieces are immediately adjacent, so ``a ## b``
    pastes but ``a # # b`` does not, and ``...`` is recognized but ``. . .``
    is not.
    """
    out = []
    i = 0
    n = len(tokens)

    def adj(a, b):
        return a.r and b.r and b.r.start.col == a.r.end.col + 1

    while i < n:
        t = tokens[i]
        n1 = tokens[i + 1] if i + 1 < n else None
        n2 = tokens[i + 2] if i + 2 < n else None
        if (is_punct(t, ".") and n1 is not None and is_punct(n1, ".")
                and n2 is not None and is_punct(n2, ".")
                and adj(t, n1) and adj(n1, n2)):
            out.append(Token(token_kinds.dot, "...", rep="...", r=t.r))
            i += 3
        elif (is_punct(t, "#") and n1 is not None and is_punct(n1, "#")
                and adj(t, n1)):
            out.append(Token(token_kinds.pound, "##", rep="##", r=t.r))
            i += 2
        else:
            out.append(t)
            i += 1
    return out


def _string_token(text, r):
    """Build a string-literal token whose contents are `text`."""
    esc = text.replace("\\", "\\\\").replace('"', '\\"')
    toks = _relex('"' + esc + '"', r)
    return toks[0] if toks else Token(token_kinds.string, [0], rep='""', r=r)


def _retag(tok, r):
    """Return a copy of `tok` positioned at range `r`.

    Used to move a macro-body token to the invocation site. A copy rather than
    a mutation because the macro body is shared by every expansion of that
    macro -- mutating it would make the second call site inherit the first
    one's position.
    """
    if r is None or tok.r is r:
        return tok
    new = Token(tok.kind, tok.content, tok.rep, r)
    new.wide = tok.wide
    new.logical_line = tok.logical_line
    return new


class _PP:
    """A token paired with the set of macro names it may not be expanded by."""

    __slots__ = ("tok", "hide")

    def __init__(self, tok, hide=frozenset()):
        self.tok = tok
        self.hide = hide


class _Macro:
    def __init__(self, name, func_like, params, variadic, body):
        self.name = name
        self.func_like = func_like
        self.params = params          # list of parameter names
        self.variadic = variadic      # bool; if so, last logical arg is va
        self.body = body              # list of Token


# ---------------------------------------------------------------------------
# Preprocessor
# ---------------------------------------------------------------------------

class _Preprocessor:
    def __init__(self, macros):
        self.macros = macros
        # `#line` state. `shift` is added to every subsequent token's line
        # number in *this* file, so a pass that inserts text can hand the
        # line numbering back to the original source. An include runs its own
        # `_Preprocessor`, so the remap never leaks across files.
        self._line_shift = 0
        self._line_file = None

    def _remap(self, tok):
        """Apply the active `#line` remap to one token, in place."""
        if not self._line_shift and self._line_file is None:
            return tok
        r = getattr(tok, "r", None)
        if r is None or r.start is None:
            return tok

        def mv(pos):
            if pos is None:
                return None
            return Position(self._line_file or pos.file,
                            pos.line + self._line_shift,
                            pos.col, pos.full_line)

        tok.r = Range(mv(r.start), mv(r.end))
        return tok

    def _do_line(self, rest, directive):
        """`#line N ["file"]` -- renumber the lines that follow.

        The operand names the line the *next* physical line is to be called,
        which is what makes it a resync: a pass that inserted text above can
        state where the original source resumes, and every later diagnostic
        names the line the user actually wrote. Without this the only safe
        place to add code was onto an existing line, which is why the Crust
        prelude could never be put above a `#include`.
        """
        rest = [t for t in rest if t.kind is not token_kinds.pound]
        if not rest:
            return
        text = str(rest[0].content).strip().strip("'\"")
        if not text.isdigit():
            return                      # not a line number: ignore, as before
        want = int(text)
        here = directive.r.start.line if directive.r else 0
        self._line_shift = want - (here + 1)
        if len(rest) > 1 and rest[1].kind is token_kinds.string:
            # A string token's content is the NUL-terminated byte list the
            # lexer built, not a `str` -- rendering it directly puts
            # `[47, 116, ..]` where the file name belongs.
            name = rest[1].content
            if isinstance(name, (list, tuple)):
                name = bytes(b for b in name if b).decode("utf-8", "replace")
            elif isinstance(name, (bytes, bytearray)):
                name = bytes(name).rstrip(b"\x00").decode("utf-8", "replace")
            self._line_file = str(name).rstrip("\x00")

    def run(self, tokens, this_file):
        lines = self._group_lines(tokens)
        out = []
        pending = []          # active non-directive tokens awaiting expansion
        cond = []             # stack of condition frames

        def emitting():
            return all(f["active"] for f in cond)

        def flush():
            if pending:
                out.extend(self._expand_line(pending, this_file))
                pending.clear()

        for line in lines:
            if line and line[0].kind is token_kinds.pound:
                flush()
                self._directive(line, cond, out, this_file, emitting)
            elif emitting():
                pending.extend(self._remap(t) for t in line)
        flush()

        if cond:
            error_collector.add(CompilerError("unterminated #if", None))
        return out

    @staticmethod
    def _group_lines(tokens):
        """Group tokens into source lines using their start line numbers."""
        lines = []
        cur = []
        cur_line = None
        for t in tokens:
            if getattr(t, "logical_line", None) is not None:
                ln = t.logical_line
            else:
                ln = t.r.start.line if t.r and t.r.start else cur_line
            if cur and ln != cur_line:
                lines.append(cur)
                cur = []
            cur.append(t)
            cur_line = ln
        if cur:
            lines.append(cur)
        return lines

    # -- directives --------------------------------------------------------

    def _directive(self, line, cond, out, this_file, emitting):
        if len(line) < 2:
            return  # null directive '#'
        name = spell(line[1])  # 'if'/'else' are C keywords, not identifiers
        rest = line[2:]
        active = emitting()

        if name in ("if", "ifdef", "ifndef"):
            if not active:
                cond.append({"active": False, "taken": True,
                             "seen_else": False, "parent": False})
                return
            if name == "if":
                val = self._eval_cond(rest, line[0].r)
            else:
                want = (name == "ifdef")
                defined = bool(rest) and ident_name(rest[0]) in self.macros
                val = (defined == want)
            cond.append({"active": val, "taken": val,
                         "seen_else": False, "parent": True})

        elif name == "elif":
            if not cond:
                error_collector.add(CompilerError("#elif without #if",
                                                  line[0].r))
                return
            f = cond[-1]
            if f["seen_else"]:
                error_collector.add(CompilerError("#elif after #else",
                                                  line[0].r))
            if not f["parent"] or f["taken"]:
                f["active"] = False
            else:
                f["active"] = self._eval_cond(rest, line[0].r)
                f["taken"] = f["taken"] or f["active"]

        elif name == "else":
            if not cond:
                error_collector.add(CompilerError("#else without #if",
                                                  line[0].r))
                return
            f = cond[-1]
            f["seen_else"] = True
            f["active"] = f["parent"] and not f["taken"]
            f["taken"] = True

        elif name == "endif":
            if not cond:
                error_collector.add(CompilerError("#endif without #if",
                                                  line[0].r))
                return
            cond.pop()

        elif not active:
            return  # all remaining directives are inert when not emitting

        elif name == "define":
            self._do_define(rest, line[0].r)

        elif name == "undef":
            if rest and ident_name(rest[0]):
                self.macros.pop(ident_name(rest[0]), None)

        elif name == "include":
            self._do_include(rest, this_file, out)

        elif name == "include_next":
            # Used to fall through to "unknown directive" and be silently
            # dropped -- so the header it names was never read, and every
            # macro it defines was quietly undefined. Libc-style wrapper
            # headers and the macOS SDK's <arm/limits.h> depend on it.
            self._do_include(rest, this_file, out, True)

        elif name == "error":
            msg = " ".join(spell(t) for t in rest)
            error_collector.add(CompilerError("#error " + msg, line[0].r))

        elif name == "line":
            self._do_line(rest, line[0])

        elif name == "pragma" and rest and ident_name(rest[0]) == "pack":
            # Not ignored: it changes struct layout, and dropping it gave a
            # different layout from gcc's with nothing to say so. A directive
            # leaves no tokens behind, so it becomes a marker that
            # `shivyc.pack` reads in stream order, between the definitions
            # it applies to.
            out.extend(_relex(pack.PACK_MARKER, line[0].r))
            out.extend(rest[1:])

        elif name in ("pragma", "ident", "sccs", "warning"):
            pass  # ignored

        # Unknown directives are silently ignored (lenient).

    def _do_define(self, rest, r):
        rest = _coalesce(rest)  # rejoin ## and ... that the lexer split
        if not rest or not ident_name(rest[0]):
            error_collector.add(CompilerError("macro name missing", r))
            return
        name = ident_name(rest[0])

        # Function-like only if '(' immediately follows the name with no space.
        func_like = False
        params = []
        variadic = False
        body_start = 1

        if (len(rest) > 1 and is_punct(rest[1], "(")
                and rest[1].r and rest[0].r
                and rest[1].r.start.col == rest[0].r.end.col + 1):
            func_like = True
            i = 2
            while i < len(rest) and not is_punct(rest[i], ")"):
                if is_punct(rest[i], ","):
                    i += 1
                    continue
                if is_punct(rest[i], "..."):
                    variadic = True
                    params.append("__VA_ARGS__")
                elif ident_name(rest[i]):
                    params.append(ident_name(rest[i]))
                i += 1
            body_start = i + 1  # skip ')'

        body = rest[body_start:]
        self.macros[name] = _Macro(name, func_like, params, variadic, body)

    @staticmethod
    def _include_guard(lines):
        """If a file's grouped `lines` are wholly wrapped in an include
        guard -- first line `#ifndef G` or `#if !defined(G)`, its matching
        `#endif` the last line, and no `#else`/`#elif` at the top level --
        return G, else None. Only comments and whitespace (gone after
        lexing) may lie outside, so once G is defined, re-including the
        file provably contributes nothing."""
        if not lines:
            return None
        first = lines[0]
        if not first or first[0].kind is not token_kinds.pound:
            return None
        words = [spell(t) for t in first[1:]]
        guard = None
        if len(words) == 2 and words[0] == "ifndef":
            guard = words[1]
        elif (len(words) == 6 and words[0] == "if" and words[1] == "!"
              and words[2] == "defined" and words[3] == "("
              and words[5] == ")"):
            guard = words[4]
        elif (len(words) == 4 and words[0] == "if" and words[1] == "!"
              and words[2] == "defined"):
            guard = words[3]
        if guard is None or not guard:
            return None
        depth = 0
        k = 0
        n = len(lines)
        while k < n:
            line = lines[k]
            if line and line[0].kind is token_kinds.pound and len(line) > 1:
                d = spell(line[1])
                if d == "if" or d == "ifdef" or d == "ifndef":
                    depth += 1
                elif d == "endif":
                    depth -= 1
                    if depth == 0:
                        return guard if k == n - 1 else None
                elif depth == 1 and (d == "else" or d == "elif"
                                     or d == "elifdef" or d == "elifndef"):
                    return None
            k += 1
        return None

    def _do_include(self, rest, this_file, out, is_next=False):
        if not rest:
            return
        if rest[0].kind is token_kinds.include_file:
            header = rest[0].content
        else:
            # Computed include: expand macros then read the spelling. The
            # result must still name a header in "..." or <...> form; otherwise
            # it is not a valid include directive (e.g. `#include blah`).
            exp = self._expand_line(rest, this_file)
            header = "".join(spell(t) for t in exp).strip()
            if not ((len(header) >= 2 and header[0] == '"' and header[-1] == '"')
                    or (len(header) >= 2 and header[0] == "<"
                        and header[-1] == ">")):
                error_collector.add(CompilerError(
                    'expected "FILENAME" or <FILENAME> after include directive',
                    rest[0].r))
                return
        try:
            text, filename = read_file(header, this_file, is_next)
            # Multiple-include optimization (as GCC's): a header already seen
            # to be wholly include-guarded, unchanged, whose guard macro is
            # now defined, would expand to nothing -- skip lexing it again.
            # The macOS SDK's <sys/cdefs.h> is otherwise re-lexed on every
            # one of its many inclusions.
            plain = filename.endswith(".h")
            if plain:
                seen = _include_guards.get(filename)
                if seen is not None and seen[0] == text \
                        and seen[1] in self.macros:
                    return
            if filename.endswith(".rs"):
                # A Rust header: lower it to C before lexing. Crust preserves
                # line numbers, so diagnostics still point into the .rs file.
                import shivyc.crust as crust
                try:
                    text = crust.translate(text, path=filename)
                except crust.CrustError as e:
                    # `e.message`, not `%s % e`: py2c gives user classes no
                    # `__str__`, so formatting the exception itself yields
                    # `<obj 0x...>` and the diagnostic is lost in exactly the
                    # build where it is hardest to get at.
                    error_collector.add(CompilerError(
                        "crust: " + e.message, rest[0].r))
                    return
            elif (filename.endswith(".cpp") or filename.endswith(".cc")
                  or filename.endswith(".cxx")
                  or filename.endswith(".hpp")):
                # Spelled out rather than `endswith((...))`: py2c lowers the
                # tuple form to `str_endswith(s, AS_STR(<list>))`, handing a
                # list object where a `char *` suffix is expected. It never
                # matches, so self-hosted builds skipped C++ lowering
                # entirely and lexed the class body as C.
                #
                # A C++ subset module: lowered in place, like an `.rs`. Line
                # numbers are preserved where possible, but a class body is
                # rewritten into free functions, so a diagnostic inside a
                # method names the generated form.
                #
                # Run out-of-process rather than importing. `import
                # tools.cpprust` transpiles into a cross-module call to
                # `cpprust__translate`, undefined at link time because that
                # module is not in the self-hosted source set -- and it cannot
                # easily join it, since it leans on regex features py2c does
                # not lower. A subprocess leaves no symbol behind, so the
                # self-hosted compiler links clean and still lowers `.cpp`.
                ok, text = _run_cpprust(filename, text)
                if not ok:
                    error_collector.add(CompilerError(
                        "cpprust: %s" % text, rest[0].r))
                    return
            elif filename.endswith(".cs"):
                # C# subset (issue #25): cs2cpp → cpprust, same out-of-process
                # protocol as `.cpp`. Spelled as its own endswith for py2c.
                ok, text = _run_csrust(filename, text)
                if not ok:
                    error_collector.add(CompilerError(
                        "csrust: %s" % text, rest[0].r))
                    return
            elif filename.endswith(".py"):
                # An rpython module: transpile it with tools/py2c.py and lex
                # the generated C in its place. The result is cached under
                # /tmp, so only the first build of a given module pays for
                # the transpile. py2c does not preserve line numbers, so the
                # generated file is what later diagnostics name -- it is kept
                # on disk in the cache precisely so it can be read.
                import shivyc.rpyinc as rpyinc
                try:
                    text, filename = rpyinc.translate(filename, text)
                except rpyinc.RpyIncludeError as e:
                    error_collector.add(CompilerError(
                        "rpython include: " + e.message, rest[0].r))
                    return
            inc = lexer.tokenize(text, filename)
            if plain:
                g = self._include_guard(self._group_lines(inc))
                if g is not None:
                    _include_guards[filename] = (text, g)
            out.extend(process(inc, filename, self.macros))
        except IOError:
            error_collector.add(CompilerError(
                "unable to read included file", rest[0].r))

    # -- macro expansion ---------------------------------------------------

    def _expand_line(self, tokens, this_file):
        seq = [_PP(t) for t in tokens]
        seq = self._expand(seq)
        return [p.tok for p in seq]

    def _expand(self, seq, if_mode=False):
        seq = list(seq)
        out = []
        i = 0
        while i < len(seq):
            p = seq[i]
            nm = ident_name(p.tok)
            m = self.macros.get(nm) if nm else None

            # In #if, `defined` can also come out of a macro's expansion
            # (the macOS SDK's pthread.h: `#if !_PTHREAD_..._COMPAT()`, whose
            # body is `defined(SWIFT_CLASS_EXTRA) && ...`). C leaves that
            # undefined, but GCC and clang evaluate it, and headers rely on
            # it. Pass the operand through unexpanded so _eval_cond can
            # resolve it afterwards, as GCC does.
            if if_mode and nm == "defined":
                out.append(p)
                i += 1
                if i < len(seq) and is_punct(seq[i].tok, "("):
                    k = i
                    while k < len(seq) and not is_punct(seq[k].tok, ")"):
                        k += 1
                    out.extend(seq[i:k + 1])
                    i = k + 1
                elif i < len(seq):
                    out.append(seq[i])
                    i += 1
                continue

            # Dynamic predefined macros: __LINE__ expands to the line number of
            # the token, __FILE__ to the source file name. They are not stored
            # in self.macros because their value depends on position; a user
            # #define of the same name (if any) takes precedence.
            if m is None and nm in ("__LINE__", "__FILE__"):
                pos = p.tok.r.start
                if nm == "__LINE__":
                    out.append(_PP(Token(token_kinds.number, str(pos.line),
                                         r=p.tok.r)))
                else:
                    chars = [ord(c) for c in pos.file] + [0]
                    out.append(_PP(Token(token_kinds.string, chars,
                                         rep='"%s"' % pos.file, r=p.tok.r)))
                i += 1
                continue

            if m is None or nm in p.hide:
                out.append(p)
                i += 1
                continue

            if not m.func_like:
                repl = self._subst(m, [], p.hide | {nm}, p.tok.r)
                seq[i:i + 1] = repl
                continue

            # Function-like: needs a '(' next.
            j = i + 1
            if j < len(seq) and is_punct(seq[j].tok, "("):
                args, endj = self._gather_args(seq, j, m)
                if args is None:
                    out.append(p)
                    i += 1
                    continue
                hs = (p.hide & seq[endj].hide) | {nm}
                repl = self._subst(m, args, hs, p.tok.r)
                seq[i:endj + 1] = repl
                continue
            else:
                out.append(p)
                i += 1
        return out

    @staticmethod
    def _gather_args(seq, lparen_idx, m):
        """Collect call arguments. Returns (args, rparen_index) or (None, _).

        Arguments are collected as _PP objects so their hide sets are preserved
        (per the C preprocessing algorithm, an argument is expanded as if it
        formed the rest of the file, carrying the hide sets it had at the call
        site; losing them causes a blue-painted self-referential macro inside an
        already-expanded argument to be re-expanded incorrectly)."""
        args = []
        cur = []
        depth = 0
        i = lparen_idx
        while i < len(seq):
            pp = seq[i]
            t = pp.tok
            if is_punct(t, "("):
                depth += 1
                if depth > 1:
                    cur.append(pp)
            elif is_punct(t, ")"):
                depth -= 1
                if depth == 0:
                    args.append(cur)
                    break
                cur.append(pp)
            elif is_punct(t, ",") and depth == 1 and not (
                    m.variadic and len(args) >= len(m.params) - 1):
                args.append(cur)
                cur = []
            else:
                cur.append(pp)
            i += 1
        else:
            return None, i  # unbalanced

        # FOO() with a single empty arg but macro takes no params -> no args.
        if len(args) == 1 and not args[0] and not m.params:
            args = []
        # Pad missing args (e.g. empty variadic) with empty lists.
        while len(args) < len(m.params):
            args.append([])
        return args, i

    def _subst(self, m, args, hide, r):
        """Substitute params into the macro body; handle # and ##.

        Operates on _PP objects so each token keeps its own hide set; the new
        hide set `hide` (HS') is UNIONED onto every result token at the end
        (Prosser's hsadd). Argument tokens thus retain the hide sets accumulated
        during their own expansion -- in particular a self-referential macro
        that was blue-painted while expanding an argument stays painted, instead
        of being wrongly re-expanded when the argument is reused."""
        argmap = {name: args[i] for i, name in enumerate(m.params)
                  if i < len(args)}

        # Pass 1: substitute params and stringize, leaving ## markers.
        items = []  # each: ("tok", _PP) or ("paste",) marker
        body = m.body
        i = 0
        n = len(body)
        expanded_cache = {}

        def expanded(name):
            if name not in expanded_cache:
                # argmap values are already _PP (from _gather_args); expand
                # them preserving hide sets.
                raw = list(argmap.get(name, []))
                expanded_cache[name] = self._expand(raw)
            return expanded_cache[name]

        while i < n:
            t = body[i]
            nm = ident_name(t)
            nxt = body[i + 1] if i + 1 < n else None
            nxt_nm = ident_name(nxt) if nxt else None

            if is_punct(t, "#") and nxt_nm in argmap:
                text = " ".join(spell(x.tok) for x in argmap.get(nxt_nm, []))
                items.append(("tok", _PP(_string_token(text, r))))
                i += 2
                continue

            if is_punct(t, "##"):
                # GNU extension: `, ## __VA_ARGS__` deletes the preceding comma
                # when the variadic argument is empty. (When it is non-empty,
                # the normal paste below relexes `,x` back into `,` `x`, which
                # is the desired juxtaposition, so only the empty case is
                # special.)
                if (m.variadic and nxt_nm == m.params[-1]
                        and not argmap.get(nxt_nm)):
                    if (items and items[-1][0] == "tok"
                            and is_punct(items[-1][1].tok, ",")):
                        items.pop()
                    i += 2
                    continue
                items.append(("paste",))
                i += 1
                continue

            adjacent_paste = (is_punct(nxt, "##") if nxt else False) or \
                             (bool(items) and items[-1] == ("paste",))

            if nm in argmap:
                if adjacent_paste:
                    sub = argmap.get(nm, [])      # raw _PP, for pasting
                else:
                    sub = expanded(nm)            # fully expanded _PP
                for x in sub:
                    items.append(("tok", x))
                i += 1
                continue

            # C99 6.10.8: __LINE__ and __FILE__ denote the position of the
            # *invocation*, not of the macro definition. A body token carries
            # the definition's range, so without retagging, every diagnostic
            # macro defined in a header reports that header's own line -- e.g.
            # `#define CHECK(p) report(__FILE__, __LINE__)` in a .h blamed the
            # .h for every call site in every .c.
            #
            # The retag also covers body tokens that name another macro, so
            # the call site propagates through nesting: with `#define A()
            # __LINE__` and `#define B() A()`, the `A` inside B's body would
            # otherwise hand A's expansion B's definition range.
            if nm in ("__LINE__", "__FILE__") or (nm and nm in self.macros):
                t = _retag(t, r)
            items.append(("tok", _PP(t)))         # body token: fresh hide
            i += 1

        # Pass 2: resolve ## pastes.
        result = []  # list of _PP
        k = 0
        while k < len(items):
            it = items[k]
            if it[0] == "paste":
                right = None
                if k + 1 < len(items) and items[k + 1][0] == "tok":
                    right = items[k + 1][1]
                    k += 1
                if result and right is not None:
                    left = result.pop()
                    pasted = _relex(spell(left.tok) + spell(right.tok), r)
                    result.extend(_PP(t) for t in pasted)
                elif right is not None:
                    result.append(right)
                # else: paste with empty operand -> drop
            else:
                result.append(it[1])
            k += 1

        # hsadd: union HS' onto every result token, preserving each token's
        # own accumulated hide set.
        return [_PP(p.tok, p.hide | hide) for p in result]

    # -- #if constant-expression evaluation --------------------------------

    def _has_include(self, tokens, i, r):
        """Evaluate `__has_include(...)` whose name token is tokens[i].
        Returns (value, index after the closing paren). The operand is a
        "quoted" or <angled> header name; outside #include the lexer splits
        the angled form into pieces, so it is rejoined here and resolved by
        the same search #include uses."""
        j = i + 1
        if j >= len(tokens) or not is_punct(tokens[j], "("):
            return 0, i + 1
        j += 1
        header = ""
        if j < len(tokens) and is_punct(tokens[j], "<"):
            j += 1
            while j < len(tokens) and not is_punct(tokens[j], ">"):
                header = header + spell(tokens[j])
                j += 1
            header = "<" + header + ">"
            j += 1                                    # past '>'
        elif j < len(tokens):
            header = spell(tokens[j]).strip()
            j += 1
        while j < len(tokens) and not is_punct(tokens[j], ")"):
            j += 1
        j += 1                                        # past ')'
        if len(header) < 3:
            return 0, j
        this_file = r.start.file if r and r.start else ""
        is_next = ident_name(tokens[i]) == "__has_include_next"
        try:
            read_file(header, this_file or "", is_next)
        except IOError:
            return 0, j
        return 1, j

    def _eval_cond(self, tokens, r):
        # 1) Resolve `defined X` / `defined(X)` and `__has_include(...)`
        #    before macro expansion (a header name must not be expanded).
        resolved = self._resolve_defined(tokens, r)

        # 2) Macro-expand the remainder, then resolve any `defined` that the
        #    expansion itself produced.
        expanded = [p.tok for p in self._expand([_PP(t) for t in resolved],
                                                True)]
        expanded = self._resolve_defined(expanded, r)

        return self._eval_expanded(expanded, r)

    def _resolve_defined(self, tokens, r):
        """Replace `defined X`, `defined(X)` and `__has_include(...)` in
        `tokens` with 0/1 number tokens."""
        resolved = []
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if ident_name(t) == "__has_include" \
                    or ident_name(t) == "__has_include_next":
                val, i = self._has_include(tokens, i, r)
                resolved.append(_num_token(val, t.r))
                continue
            if ident_name(t) == "defined":
                j = i + 1
                paren = j < len(tokens) and is_punct(tokens[j], "(")
                if paren:
                    j += 1
                name = ident_name(tokens[j]) if j < len(tokens) else None
                val = 1 if (name and name in self.macros) else 0
                resolved.append(_num_token(val, t.r))
                i = j + (2 if paren else 1)
                continue
            resolved.append(t)
            i += 1
        return resolved

    def _eval_expanded(self, expanded, r):
        # 3) Map remaining identifiers/keywords to 0; keep numbers/operators.
        spells = []
        for t in expanded:
            if (t.kind is token_kinds.number
                    or t.kind is token_kinds.char_string):
                spells.append(spell(t))
            elif is_ident(t) or t.kind in token_kinds.keyword_kinds:
                spells.append("0")
            else:
                spells.append(spell(t))

        try:
            val = _ConstExpr(spells).parse()
        except _PPExprError as e:
            error_collector.add(CompilerError(
                "invalid #if expression: " + str(e), r))
            return False
        return val != 0


class _PPExprError(Exception):
    pass


class _ConstExpr:
    """Recursive-descent evaluator for #if integer constant expressions."""

    # Binary operators by precedence (low to high).
    _LEVELS = [
        {"||"}, {"&&"}, {"|"}, {"^"}, {"&"},
        {"==", "!="}, {"<", "<=", ">", ">="}, {"<<", ">>"},
        {"+", "-"}, {"*", "/", "%"},
    ]

    def __init__(self, spells):
        self.toks = spells
        self.i = 0

    def parse(self):
        if not self.toks:
            raise _PPExprError("empty expression")
        v = self._ternary()
        if self.i != len(self.toks):
            raise _PPExprError("trailing tokens")
        return v

    def _peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _eat(self, s=None):
        t = self._peek()
        if t is None:
            raise _PPExprError("unexpected end")
        if s is not None and t != s:
            raise _PPExprError("expected " + s)
        self.i += 1
        return t

    def _ternary(self):
        c = self._binary(0)
        if self._peek() == "?":
            self._eat("?")
            a = self._ternary()
            self._eat(":")
            b = self._ternary()
            return a if c != 0 else b
        return c

    def _binary(self, level):
        if level >= len(self._LEVELS):
            return self._unary()
        left = self._binary(level + 1)
        while self._peek() in self._LEVELS[level]:
            op = self._eat()
            right = self._binary(level + 1)
            left = self._apply(op, left, right)
        return left

    @staticmethod
    def _apply(op, a, b):
        if op == "||":
            return 1 if (a != 0 or b != 0) else 0
        if op == "&&":
            return 1 if (a != 0 and b != 0) else 0
        if op == "|":
            return a | b
        if op == "^":
            return a ^ b
        if op == "&":
            return a & b
        if op == "==":
            return 1 if a == b else 0
        if op == "!=":
            return 1 if a != b else 0
        if op == "<":
            return 1 if a < b else 0
        if op == "<=":
            return 1 if a <= b else 0
        if op == ">":
            return 1 if a > b else 0
        if op == ">=":
            return 1 if a >= b else 0
        if op == "<<":
            return a << b
        if op == ">>":
            return a >> b
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            if b == 0:
                raise _PPExprError("division by zero")
            return -(-a // b) if (a < 0) ^ (b < 0) else a // b
        if op == "%":
            if b == 0:
                raise _PPExprError("modulo by zero")
            q = -(-a // b) if (a < 0) ^ (b < 0) else a // b
            return a - b * q
        raise _PPExprError("bad operator " + op)

    def _unary(self):
        t = self._peek()
        if t == "+":
            self._eat()
            return self._unary()
        if t == "-":
            self._eat()
            return -self._unary()
        if t == "!":
            self._eat()
            return 0 if self._unary() != 0 else 1
        if t == "~":
            self._eat()
            return ~self._unary()
        return self._primary()

    def _primary(self):
        t = self._eat()
        if t == "(":
            v = self._ternary()
            self._eat(")")
            return v
        try:
            return parse_c_int(t)
        except (ValueError, IndexError):
            raise _PPExprError("bad token " + repr(t))


# ---------------------------------------------------------------------------
# Include file resolution
# ---------------------------------------------------------------------------

_extra_include_dirs = []

# filename -> (text, guard macro) for headers found to be wholly
# include-guarded; see _Preprocessor._include_guard. The text is kept so a
# file that changes between compiles in one process is never skipped wrongly.
_include_guards = {}


def set_include_dirs(dirs):
    """Set additional `-I` include directories searched by read_file."""
    global _extra_include_dirs
    _extra_include_dirs = list(dirs or [])


# On a Windows host paths also separate with '\\' -- `__file__` is
# `C:\\...\\shivyc\\preproc.py` -- and a lookup that only splits on '/' finds
# no directory at all, so the bundled headers resolved only when the compiler
# happened to run from the repo root. Elsewhere a backslash is an ordinary
# filename character, so it counts as a separator on Windows alone. Shaped as
# a bare implementation test so the self-hosted build folds it to False.
_BACKSLASH_SEP = False
if sys.implementation.name != "shivyc":
    _BACKSLASH_SEP = os.sep == "\\"


def _dirname(p):
    """Directory portion of a '/'-separated path (own impl, no os/pathlib so it
    transpiles to C). On Windows, '\\' separates too."""
    i = len(p) - 1
    while i >= 0 and p[i] != '/' and not (_BACKSLASH_SEP and p[i] == '\\'):
        i = i - 1
    if i < 0:
        return "."
    return p[:i]


def _pjoin(a, b):
    """Join two path components with a single '/'."""
    if a == "":
        return b
    if a[len(a) - 1] == '/':
        return a + b
    return a + "/" + b


def _try_read(path):
    """Return the contents of `path`, or None if it cannot be opened."""
    try:
        f = open(path, "r")
    except Exception:
        return None
    if f is None:          # transpiled C: fopen returns NULL on failure
        return None
    data = f.read()
    f.close()
    return data


def _bundled_include_dir():
    """Directory holding ShivyC's fallback headers.

    `_dirname(__file__)` is right on the host and empty in a self-hosted
    build, where py2c compiles `__file__` down to the bare string
    "preproc.py". The bundled headers then resolved against the working
    directory: `<stdbool.h>` was found when the compiler happened to run from
    the repo root and not otherwise, so an rpython include -- whose generated
    runtime needs it -- failed depending on where you stood. Try the module's
    own directory first, then the same two fallbacks the other self-hosted
    path lookups use.
    """
    env = os.environ.get("CRUST_INCLUDE")
    if env:
        return env
    here = _dirname(os.path.abspath(__file__))
    for cand in (_pjoin(here, "include"),
                 _pjoin(_pjoin(os.getcwd(), "shivyc"), "include"),
                 _pjoin(os.getcwd(), "include")):
        if os.path.isdir(cand):
            return cand
    return _pjoin(here, "include")


def _found_in_dir(this_file, dirs):
    """Index in `dirs` of the search directory `this_file` was found in (the
    first whose path is a prefix of it), or -1 if it came from none -- e.g.
    the main source file, or a quoted include beside it."""
    k = 0
    while k < len(dirs):
        d = dirs[k]
        if d and this_file.startswith(_pjoin(d, "")):
            return k
        k += 1
    return -1


def read_file(include_file, this_file, is_next=False):
    """Read the text of the given include file.

    include_file - the header name, including opening and closing quotes or
    angle brackets.
    this_file - location of the current file being preprocessed. used for
    locating quoted headers.
    is_next - `#include_next` / `__has_include_next`: resume the search in
    the directories *after* the one `this_file` was found in, so a wrapper
    header can reach the header it wraps. As in GCC and clang, a file not
    found through the search path makes it an ordinary include.
    """
    name = include_file[1:-1]
    bundled = _pjoin(_bundled_include_dir(), name)
    candidates = []
    if is_next:
        dirs = list(_extra_include_dirs) + [_bundled_include_dir()]
        k = _found_in_dir(this_file, dirs)
        if k >= 0:
            k += 1
            while k < len(dirs):
                candidates.append(_pjoin(dirs[k], name))
                k += 1
            for path in candidates:
                data = _try_read(path)
                if data is not None:
                    return data, path
            raise IOError(f"could not find include file {include_file}")
    if include_file[0] == '"':
        # Quoted: the including file's directory, then -I dirs, then ShivyC's
        # bundled fallback headers.
        candidates.append(_pjoin(_dirname(this_file), name))
        for d in _extra_include_dirs:
            candidates.append(_pjoin(d, name))
        candidates.append(bundled)
    else:
        # Angle-bracket: -I dirs first, so a real libc's headers (provided via
        # -I, e.g. musl) take precedence over ShivyC's bundled fallback stubs.
        for d in _extra_include_dirs:
            candidates.append(_pjoin(d, name))
        candidates.append(bundled)

    for path in candidates:
        data = _try_read(path)
        if data is not None:
            return data, path

    raise IOError(f"could not find include file {include_file}")
