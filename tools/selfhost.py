#!/usr/bin/env python3
"""Build, test, and benchmark ShivyCX self-hosting.

This drives the self-host pipeline end to end: it transpiles ShivyCX's own
Python source to C with ``tools/py2c.py``, compiles the modules with gcc, and
links runnable test/benchmark executables.

Two link backends produce a runnable exe:

  * ``host``     -- plain gcc + glibc. Used for pure-C modules (e.g. tokens)
                    whose only otherwise-undefined symbol is ``main``.
  * ``objcore``  -- additionally links micropython's objcore core objects, for
                    code that uses the dynamic bridge (``mp_call_import`` ...).
                    This is the path for genuinely-dynamic modules.

``--musl`` compiles the transpiled C against the *packaged* musl headers
(``shivyc/musl``) instead of glibc, validating that ShivyCX's own output is
glibc-independent. (Producing a fully static musl-libc exe -- building the
~1500 musl sources into a libc.a with crt/syscall startup -- is the next step;
see ``musl_link_libc`` below.)

All C support code (test harnesses and benchmarks) is kept inline as
triple-quoted strings in this file and written to ``/tmp`` before the compile
steps, so this script is the single source for the self-host build glue.

Usage:
    python3 tools/selfhost.py list
    python3 tools/selfhost.py test [NAME ...]      # default: all host tests
    python3 tools/selfhost.py bench [NAME ...]
    python3 tools/selfhost.py coverage [--musl]    # transpile+compile all mods
Options:
    --musl        compile transpiled C against packaged musl headers
    --objcore     allow objcore-backed targets (needs the objcore build)
    --keep        keep the scratch build dir and print its path
    -q/--quiet    less chatter
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY2C = os.path.join(REPO, "tools", "py2c.py")

# objcore (micropython) locations -- the proven link recipe from
# examples/link_objcore.sh.
OBJCORE = "/home/claude/micropython/ports/objcore"
MP_TOP = "/home/claude/micropython"

# ==========================================================================
# Inlined C support code (written to /tmp before compiling)
# ==========================================================================

# End-to-end correctness harness for the transpiled Token class. Declares a
# struct matching the generated layout, builds Tokens, and checks fields.
TOKEN_DEMO_C = r'''
#include "shivyc_rt.h"
#include <stdio.h>
#include <string.h>
typedef struct Range Range;                 /* opaque (errors.Range) */
typedef struct Token {                       /* matches generated tokens.c */
    Obj _hdr; obj r; obj content; obj kind; char* rep; bool wide;
    obj logical_line;
} Token;
Token* Token_new(obj kind, obj content, char* rep, obj r);

int main(void) {
    /* `content` is polymorphic (a string for identifiers, a char-code list for
       string literals), so it is an `obj`, not a char*. Pass and read it boxed. */
    Token* a = Token_new(OBJ_STR("identifier"), OBJ_STR("myvar"), "", OBJ_NONE);
    printf("token.content = %s (expect myvar)\n", AS_STR(a->content));
    /* Empty content is falsy, so __init__ falls back to str(self.kind). */
    Token* b = Token_new(OBJ_STR("kw_int"), OBJ_STR(""), "", OBJ_NONE);
    printf("default content = %s (expect kw_int)\n", AS_STR(b->content));
    printf("wide=%d logical_line_is_none=%d\n",
           (int)a->wide, a->logical_line.tag == T_NONE);
    int ok = !strcmp(AS_STR(a->content), "myvar")
             && !strcmp(AS_STR(b->content), "kw_int")
             && !a->wide && a->logical_line.tag == T_NONE;
    printf("%s\n", ok ? "ALL CHECKS PASS" : "FAIL");
    return ok ? 0 : 1;
}
'''

# Throughput benchmark: how fast does the transpiled Token constructor run
# (arena alloc + __init__) versus wall time.
TOKEN_BENCH_C = r'''
#define _POSIX_C_SOURCE 199309L   /* clock_gettime / struct timespec */
#include "shivyc_rt.h"
#include <stdio.h>
#include <time.h>
typedef struct Range Range;
typedef struct Token {
    Obj _hdr; Range* r; obj content; obj kind; char* rep; bool wide;
    obj logical_line;
} Token;
Token* Token_new(obj kind, obj content, char* rep, obj r);

int main(void) {
    const long N = 2000000;
    volatile obj sink = OBJ_NONE;
    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    for (long i = 0; i < N; i++) {
        Token* t = Token_new(OBJ_STR("identifier"), OBJ_STR("myvar"), "", OBJ_NONE);
        sink = t->content;
        if ((i & 0xffff) == 0) arena_reset();   /* keep the arena bounded */
    }
    clock_gettime(CLOCK_MONOTONIC, &t1);
    (void)sink;
    double ms = (t1.tv_sec - t0.tv_sec) * 1e3 + (t1.tv_nsec - t0.tv_nsec) / 1e6;
    printf("Token_new x %ld: %.1f ms  (%.1f M/s)\n", N, ms, N / ms / 1e3);
    return 0;
}
'''

# objcore bridge harness: init micropython, call a builtin through the
# ShivyCX->mp bridge, and supply the port stubs the core needs.
BRIDGE_DEMO_C = r'''
#include <stdio.h>
#include "py/builtin.h"
#include "py/gc.h"
#include "py/lexer.h"
#include "py/mperrno.h"
#include "py/runtime.h"
#include "py/mphal.h"
#include "shivyc_rt.h"
#include "mp_stdlib_bridge.h"

static char *stack_top;
static char heap[MICROPY_HEAP_SIZE];

int main(void) {
    int stack_dummy; stack_top = (char*)&stack_dummy;
    gc_init(heap, heap + sizeof(heap));
    mp_init();
    nlr_buf_t nlr;
    int ok = 0;
    if (nlr_push(&nlr) == 0) {
        obj r = mp_call_import("builtins", "abs", 1, OBJ_INT(-5));
        printf("abs(-5) via objcore bridge = %ld (expect 5)\n", AS_INT(r));
        ok = (AS_INT(r) == 5);
        nlr_pop();
    } else {
        printf("micropython raised during bridge call\n");
    }
    mp_deinit();
    printf("%s\n", ok ? "ALL CHECKS PASS" : "FAIL");
    return ok ? 0 : 1;
}
/* port stubs (from objcore main.c) */
void gc_collect(void){ void*d; gc_collect_start();
    gc_collect_root(&d,((mp_uint_t)stack_top-(mp_uint_t)&d)/sizeof(mp_uint_t));
    gc_collect_end(); }
mp_lexer_t *mp_lexer_new_from_file(qstr f){(void)f; mp_raise_OSError(MP_ENOENT);}
mp_import_stat_t mp_import_stat(const char *p){(void)p; return MP_IMPORT_STAT_NO_EXIST;}
void nlr_jump_fail(void *v){(void)v; for(;;){}}
void mp_hal_stdout_tx_strn_cooked(const char *s, size_t n){ fwrite(s,1,n,stdout); }
/* float disabled in this objcore config; bridge refs are dead code here */
mp_obj_t mp_obj_new_float(double v){(void)v; return mp_const_none;}
double mp_obj_get_float(mp_obj_t o){(void)o; return 0;}
const mp_obj_type_t mp_type_float;
'''

# Cross-module harness: il_cmds.base's _is_imm runs isinstance(spot,
# LiteralSpot) where LiteralSpot is defined in the *spots* module -- so this
# links two transpiled modules and exercises a cross-module isinstance over the
# generated vtable/TypeInfo.
ILBASE_DEMO_C = r'''
#include "shivyc_rt.h"
#include <stdio.h>
#include <string.h>
typedef struct LiteralSpot LiteralSpot;
typedef struct RegSpot RegSpot;
LiteralSpot* LiteralSpot_new(obj value);
RegSpot*     RegSpot_new(char* name);
char*        LiteralSpot_asm_str(Obj* self_, int size);
obj          ILCommand__is_imm(Obj* self_, obj spot);   /* il_cmds.base */
void         spots_init(void);

int main(void) {
    spots_init();                              /* cross-module: spots */
    LiteralSpot* lit = LiteralSpot_new(OBJ_INT(5));
    RegSpot*     reg = RegSpot_new("rax");
    int is_lit = truthy(ILCommand__is_imm((Obj*)0, OBJ_OBJ(lit)));
    int is_reg = truthy(ILCommand__is_imm((Obj*)0, OBJ_OBJ(reg)));
    printf("_is_imm(LiteralSpot) = %d (expect 1)\n", is_lit);
    printf("_is_imm(RegSpot)     = %d (expect 0)\n", is_reg);
    char* a = LiteralSpot_asm_str((Obj*)lit, 4);
    printf("LiteralSpot.asm_str = %s\n", a);
    int ok = is_lit && !is_reg && a && strchr(a, '5');
    printf("%s\n", ok ? "ALL CHECKS PASS" : "FAIL");
    return ok ? 0 : 1;
}
'''

# Three-module closure: weak_alias._ident reads a tokens.Token's fields and
# compares its kind against the token_kinds.identifier singleton. Relies on
# list.sort(key=lambda) and imported-class default args working in
# token_kinds' module init.
WA_DEMO_C = r'''
#include "shivyc_rt.h"
#include <stdio.h>
#include <string.h>
typedef struct Range Range;
typedef struct TokenKind TokenKind;
typedef struct Token { Obj _hdr; Range* r; obj content; obj kind;
                       char* rep; bool wide; obj logical_line; } Token;
extern TokenKind* identifier;            /* token_kinds singleton */
void   token_kinds_init(void);
Token* Token_new(obj kind, obj content, char* rep, obj r);
obj    _ident(Token* t);                 /* weak_alias */

int main(void) {
    token_kinds_init();
    Token* id = Token_new(OBJ_OBJ(identifier), OBJ_STR("myalias"), "", OBJ_NONE);
    obj a = _ident(id);
    printf("_ident(identifier 'myalias') = %s (expect myalias)\n", pystr(a));
    Token* other = Token_new(OBJ_INT(0), OBJ_STR("nope"), "", OBJ_NONE);
    obj b = _ident(other);
    printf("_ident(non-identifier) is_none = %d (expect 1)\n", b.tag == T_NONE);
    int ok = a.tag == T_STR && !strcmp(AS_STR(a), "myalias") && b.tag == T_NONE;
    printf("%s\n", ok ? "ALL CHECKS PASS" : "FAIL");
    return ok ? 0 : 1;
}
'''

# ==========================================================================
# Each target self-hosts one ShivyCX module end to end: transpile the listed
# .py module(s), compile, link a harness, run, and check the output.

TARGETS = {
    "tokens": {
        "modules": ["shivyc/tokens.py"],
        "harness": ("token_demo.c", TOKEN_DEMO_C),
        "backend": "host",
        "expect": "ALL CHECKS PASS",
        "desc": "Token class: arena alloc + __init__ + str(kind) default",
    },
    "bridge": {
        "modules": [],            # bridge runtime only; no ShivyCX module
        "harness": ("bridge_demo.c", BRIDGE_DEMO_C),
        "backend": "objcore",
        "expect": "ALL CHECKS PASS",
        "desc": "dynamic call (abs) routed through the objcore mp bridge",
    },
    "ilbase": {
        "modules": ["shivyc/il_cmds/base.py", "shivyc/spots.py"],
        "harness": ("ilbase_demo.c", ILBASE_DEMO_C),
        "backend": "host",
        "expect": "ALL CHECKS PASS",
        "desc": "cross-module: il_cmds.base._is_imm isinstance(spot, "
                "spots.LiteralSpot) over 2 linked modules",
    },
    "weak_alias": {
        "modules": ["shivyc/weak_alias.py", "shivyc/token_kinds.py",
                    "shivyc/tokens.py"],
        "harness": ("wa_demo.c", WA_DEMO_C),
        "backend": "host",
        "expect": "ALL CHECKS PASS",
        "desc": "3-module closure: weak_alias._ident vs token_kinds.identifier "
                "(exercises list.sort(key=) in token_kinds init)",
    },
}

BENCHES = {
    "tokens": {
        "modules": ["shivyc/tokens.py"],
        "harness": ("token_bench.c", TOKEN_BENCH_C),
        "backend": "host",
        "desc": "Token_new throughput (arena alloc + __init__)",
    },
}

# All ShivyCX source globs used for the coverage report.
COVERAGE_GLOBS = ["shivyc/*.py", "shivyc/tree/*.py",
                  "shivyc/parser/*.py", "shivyc/il_cmds/*.py",
                  "shivyc/targets/*.py"]


# ==========================================================================
# Helpers
# ==========================================================================
def log(quiet, *a):
    if not quiet:
        print(*a)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def forward_py2c_notes(r):
    """Re-emit py2c's substitution warnings from a captured transpile.

    A transpile can succeed and still have replaced something it could not
    lower with a stand-in value -- `None` for an unsupported call, an empty
    dict for `vars()`. Those lines are the only advance notice that the
    native build will behave differently from the host, and `run` captures
    stderr, so without this they are discarded in exactly the build where
    they matter. Three self-hosting bugs were tracked down through segfaults
    and bisection that these lines would have named outright.
    """
    notes = [ln for ln in (getattr(r, "stderr", "") or "").splitlines()
             if ln.startswith("py2c: ")]
    if not notes:
        return
    for ln in notes:
        sys.stderr.write(ln + "\n")
    sys.stderr.write(
        "py2c: %d construct(s) replaced by a stand-in value; the self-hosted "
        "build will not match the host on those lines.\n" % len(notes))


def transpile(modules, out_dir, mp_bridge=False):
    """Transpile module(s) to C in out_dir. Always writes the runtime; with
    mp_bridge also writes the objcore bridge."""
    if modules:
        paths = [os.path.join(REPO, m) for m in modules]
        r = run([sys.executable, PY2C, *paths, "--out", out_dir], cwd=REPO)
        if r.returncode != 0:
            raise RuntimeError("transpile failed:\n" + r.stderr)
        forward_py2c_notes(r)
    # py2c writes shivyc_rt.{c,h}; for the bridge backend we also need
    # mp_stdlib_bridge.{c,h}, which write_runtime emits when mp_bridge=True.
    sys.path.insert(0, os.path.join(REPO, "tools"))
    import py2c
    py2c.write_runtime(out_dir, mp_bridge=mp_bridge)


def musl_cflags():
    """-nostdinc plus the packaged musl include dirs and defines."""
    sys.path.insert(0, REPO)
    from shivyc import musl
    tree = musl.materialize()
    flags = ["-nostdinc"]
    flags += tree.public_cflags()
    return flags


def compile_c(src, obj, inc_dirs=(), extra=(), use_musl=False):
    cmd = ["gcc", "-c", src, "-o", obj, "-std=c99", "-O2"]
    for d in inc_dirs:
        cmd += ["-I", d]
    if use_musl:
        cmd += musl_cflags()
    cmd += list(extra)
    r = run(cmd)
    return r


def objcore_core_objects():
    import glob
    objs = glob.glob(os.path.join(OBJCORE, "build/py", "*.o"))
    objs += glob.glob(os.path.join(OBJCORE, "build/shared", "**", "*.o"),
                      recursive=True)
    # exclude the port's own entry objects -- the harness supplies them
    return [o for o in objs
            if os.path.basename(o) not in ("main.o", "hal.o", "script.o")]


def link(objs, exe, backend):
    cmd = ["gcc", "-no-pie", *objs, "-o", exe]
    if backend == "objcore":
        cmd[2:2] = objcore_core_objects()
    r = run(cmd)
    return r


def musl_link_libc():
    """Placeholder for the fully-static musl-libc backend.

    The packaged musl tree (~1500 .c sources) can be compiled into a libc.a
    with crt startup and linked -nostdlib to produce a glibc-free static exe.
    That build (correct source set, syscall/crt asm, generated headers) is the
    next self-host milestone; today --musl validates header compatibility.
    """
    raise NotImplementedError("musl static libc backend: see docstring")


# ==========================================================================
# Build / test / bench
# ==========================================================================
def build_target(name, spec, build_root, use_musl=False, quiet=False):
    """Transpile -> compile -> link one target. Returns (exe_path or None,
    message)."""
    backend = spec["backend"]
    work = os.path.join(build_root, name)
    os.makedirs(work, exist_ok=True)
    transpile(spec["modules"], work, mp_bridge=(backend == "objcore"))

    # write the inline harness
    hname, hsrc = spec["harness"]
    hpath = os.path.join(work, hname)
    with open(hpath, "w") as f:
        f.write(hsrc)

    inc = [work]
    if backend == "objcore":
        inc += [OBJCORE, MP_TOP, os.path.join(OBJCORE, "build")]

    objs = []
    # compile the harness
    o = os.path.join(work, "harness.o")
    r = compile_c(hpath, o, inc_dirs=inc)
    if r.returncode != 0:
        return None, "harness compile failed:\n" + r.stderr
    objs.append(o)

    # compile each transpiled module (against musl headers if requested)
    for m in spec["modules"]:
        base = os.path.splitext(os.path.basename(m))[0]
        # py2c names files by dotted module path; find the .c it produced
        cfile = _find_module_c(work, base)
        oo = os.path.join(work, base + ".o")
        r = compile_c(cfile, oo, inc_dirs=[work], use_musl=use_musl)
        if r.returncode != 0:
            return None, "module %s compile failed:\n%s" % (base, r.stderr)
        objs.append(oo)

    # runtime
    rt_o = os.path.join(work, "shivyc_rt.o")
    r = compile_c(os.path.join(work, "shivyc_rt.c"), rt_o, inc_dirs=[work])
    if r.returncode != 0:
        return None, "runtime compile failed:\n" + r.stderr
    objs.append(rt_o)

    # bridge runtime (objcore backend)
    if backend == "objcore":
        br_o = os.path.join(work, "mp_stdlib_bridge.o")
        r = compile_c(os.path.join(work, "mp_stdlib_bridge.c"), br_o,
                      inc_dirs=inc)
        if r.returncode != 0:
            return None, "bridge compile failed:\n" + r.stderr
        objs.append(br_o)

    exe = os.path.join(work, name)
    r = link(objs, exe, backend)
    if r.returncode != 0:
        return None, "link failed:\n" + r.stderr
    return exe, "ok"


def _find_module_c(work, base):
    """py2c emits one .c per module, named by dotted path (e.g.
    shivyc.tree.memory_exprs.c) or short name (tokens.c). Find it by suffix."""
    cands = [f for f in os.listdir(work)
             if f.endswith(".c") and f not in
             ("shivyc_rt.c", "mp_stdlib_bridge.c")
             and (f == base + ".c" or f.endswith("." + base + ".c"))]
    if not cands:
        raise RuntimeError("no transpiled .c for module %r in %s" % (base, work))
    return os.path.join(work, cands[0])


def cmd_test(names, build_root, args):
    targets = names or [n for n, s in TARGETS.items()
                        if s["backend"] == "host" or args.objcore]
    rc = 0
    for name in targets:
        spec = TARGETS.get(name)
        if not spec:
            print("  ?  %s: unknown target" % name); rc = 1; continue
        if spec["backend"] == "objcore" and not args.objcore:
            print("  -  %s: skipped (objcore backend; pass --objcore)" % name)
            continue
        if spec["backend"] == "objcore" and not os.path.isdir(
                os.path.join(OBJCORE, "build", "py")):
            print("  -  %s: skipped (objcore not built)" % name); continue
        try:
            exe, msg = build_target(name, spec, build_root,
                                    use_musl=args.musl, quiet=args.quiet)
        except Exception as e:
            print("  X  %s: %s" % (name, e)); rc = 1; continue
        if exe is None:
            print("  X  %s: %s" % (name, msg.splitlines()[0])); rc = 1
            log(args.quiet, "     " + "\n     ".join(msg.splitlines()[1:6]))
            continue
        out = run([exe])
        passed = spec["expect"] in out.stdout
        print("  %s  %s -- %s" % ("OK" if passed else "X ", name, spec["desc"]))
        for ln in out.stdout.strip().splitlines():
            log(args.quiet, "       " + ln)
        if not passed:
            rc = 1
    return rc


def cmd_bench(names, build_root, args):
    targets = names or list(BENCHES)
    for name in targets:
        spec = BENCHES.get(name)
        if not spec:
            print("  ?  %s: unknown bench" % name); continue
        try:
            exe, msg = build_target(name, spec, build_root,
                                    use_musl=args.musl, quiet=args.quiet)
        except Exception as e:
            print("  X  %s: %s" % (name, e)); continue
        if exe is None:
            print("  X  %s: %s" % (name, msg.splitlines()[0])); continue
        out = run([exe])
        print("  %s: %s" % (name, out.stdout.strip()))


def cmd_coverage(build_root, args):
    """Transpile every ShivyCX module, compile each, report how many compile
    (against glibc, or musl headers with --musl)."""
    import glob
    out = os.path.join(build_root, "coverage")
    os.makedirs(out, exist_ok=True)
    mods = []
    for g in COVERAGE_GLOBS:
        mods += glob.glob(os.path.join(REPO, g))
    r = run([sys.executable, PY2C, *mods, "--out", out], cwd=REPO)
    forward_py2c_notes(r)
    if r.returncode != 0:
        print("transpile failed:\n" + r.stderr); return 1
    sys.path.insert(0, os.path.join(REPO, "tools"))
    import py2c as _p
    _p.write_runtime(out)
    rt = os.path.join(out, "shivyc_rt.o")
    compile_c(os.path.join(out, "shivyc_rt.c"), rt, inc_dirs=[out])
    cfiles = [f for f in os.listdir(out)
              if f.endswith(".c") and f != "shivyc_rt.c"]
    ok = 0
    fails = []
    for c in sorted(cfiles):
        oo = os.path.join(out, c[:-2] + ".o")
        res = compile_c(os.path.join(out, c), oo, inc_dirs=[out],
                        use_musl=args.musl)
        if res.returncode == 0:
            ok += 1
        else:
            n = res.stderr.count("error:")
            fails.append((n, c))
    mode = "musl headers" if args.musl else "glibc"
    print("self-host compile coverage (%s): %d/%d modules"
          % (mode, ok, len(cfiles)))
    if fails and not args.quiet:
        print("remaining (errors, module):")
        for n, c in sorted(fails)[:15]:
            print("  %2d  %s" % (n, c[:-2]))
    return 0


def cmd_link(build_root, args):
    """Fast self-host LINK check (gcc, not ShivyCX).

    Transpiles every module as ONE translation unit, gcc-compiles each, then
    links them together to surface cross-module *linker* errors -- undefined
    references and multiple definitions -- which is what most self-host bugs
    reduce to. Much faster than driving each module through the ShivyCX backend
    (use that, or `make rpython`, for codegen correctness)."""
    import glob
    out = os.path.join(build_root, "link")
    os.makedirs(out, exist_ok=True)
    mods = []
    for g in COVERAGE_GLOBS:
        mods += glob.glob(os.path.join(REPO, g))
    r = run([sys.executable, PY2C, *mods, "--out", out], cwd=REPO)
    forward_py2c_notes(r)
    if r.returncode != 0:
        print("transpile failed:\n" + r.stderr)
        return 1
    sys.path.insert(0, os.path.join(REPO, "tools"))
    import py2c as _p
    _p.write_runtime(out)

    # Compile the runtime and every module (-O0 for speed; we only need
    # symbols, not optimized code).
    objs = []
    compile_fails = []
    rt_o = os.path.join(out, "shivyc_rt.o")
    compile_c(os.path.join(out, "shivyc_rt.c"), rt_o, inc_dirs=[out],
              extra=["-O0"])
    objs.append(rt_o)
    cfiles = sorted(f for f in os.listdir(out)
                    if f.endswith(".c") and f != "shivyc_rt.c")
    for c in cfiles:
        oo = os.path.join(out, c[:-2] + ".o")
        res = compile_c(os.path.join(out, c), oo, inc_dirs=[out], extra=["-O0"])
        if res.returncode == 0:
            objs.append(oo)
        else:
            compile_fails.append(c[:-2])

    # (1) Partial link to surface multiple-definition clashes between modules.
    combined = os.path.join(out, "combined.o")
    lr = run(["ld", "-r", "-o", combined] + objs)
    dup = sorted({ln.split("`")[1].split("'")[0]
                  for ln in lr.stderr.splitlines()
                  if "multiple definition" in ln and "`" in ln})

    # (2) Final link (allowing dups) to surface undefined references. A tiny
    # stub provides main() so a missing entry point isn't reported as an error.
    stub_c = os.path.join(out, "_linkstub.c")
    with open(stub_c, "w") as f:
        f.write("int main(void){return 0;}\n")
    stub_o = os.path.join(out, "_linkstub.o")
    compile_c(stub_c, stub_o, extra=["-O0"])
    exe = os.path.join(out, "_all")
    fl = run(["gcc"] + objs + [stub_o, "-o", exe, "-lm",
              "-Wl,--allow-multiple-definition"])
    undef = sorted({ln.split("`")[1].split("'")[0]
                    for ln in fl.stderr.splitlines()
                    if "undefined reference" in ln and "`" in ln
                    and "`main'" not in ln})

    print("self-host link check (gcc): %d/%d modules compiled, "
          "%d dup symbols, %d undefined refs"
          % (len(objs) - 1, len(cfiles), len(dup), len(undef)))
    if not args.quiet:
        if compile_fails:
            print("  compile fails (%d): %s"
                  % (len(compile_fails), ", ".join(compile_fails[:20])))
        for d in dup[:20]:
            print("  DUP    " + d)
        for u in undef[:25]:
            print("  UNDEF  " + u)
    return 0


def _compiler_init_order(out):
    """A valid module-init order for the whole compiler. Import shivyc.main
    under the host to get a dependency-respecting order of module names, map
    each to its emitted .c, and read that file's actual `<slug>_init` symbol
    (rather than guessing the slug). Lazily-imported modules are appended."""
    import re as _re
    sys.path.insert(0, REPO)
    import importlib
    importlib.import_module("shivyc.main")
    eager = [m for m in sys.modules if m.startswith("shivyc")]

    def cbase(mod):
        if mod == "shivyc":
            return "__init__"
        rest = mod[len("shivyc."):]
        return mod if "." in rest else rest

    def init_sym(cpath):
        m = _re.search(r"^void ([A-Za-z0-9_]+_init)\(void\) \{",
                       open(cpath).read(), _re.M)
        return m.group(1) if m else None

    cfiles = sorted(f for f in os.listdir(out)
                    if f.endswith(".c") and f != "shivyc_rt.c"
                    and f != "_entry.c")
    base_to_file = {f[:-2]: f for f in cfiles}
    order, seen = [], set()
    for m in eager:
        f = base_to_file.get(cbase(m))
        if f and f not in seen:
            order.append(f); seen.add(f)
    for f in cfiles:                           # lazily-imported remainder
        if f not in seen:
            order.append(f); seen.add(f)
    syms = []
    for f in order:
        s = init_sym(os.path.join(out, f))
        if s:
            syms.append(s)
    return syms


def cmd_compiler(build_root, args):
    """Build the whole self-hosted compiler as a native binary.

    Transpiles all modules (no bridge), compiles them, generates a C entry
    point that runs every module's import-time init and then the Python
    `main(argc, argv)`, and links a single executable. The emitted `main.c`
    defines the Python entry as `obj main(...)`, which clashes with C's entry,
    so its symbol is renamed at build time (main.py is left untouched -- the
    host tests still call shivyc.main.main())."""
    import glob
    out = args.build_dir or os.path.join(build_root, "compiler")
    os.makedirs(out, exist_ok=True)
    mods = []
    for g in COVERAGE_GLOBS:
        mods += glob.glob(os.path.join(REPO, g))
    r = run([sys.executable, PY2C, *mods, "--out", out], cwd=REPO)
    forward_py2c_notes(r)
    if r.returncode != 0:
        print("transpile failed:\n" + r.stderr); return 1
    sys.path.insert(0, os.path.join(REPO, "tools"))
    import py2c as _p
    _p.write_runtime(out)

    # Rename the Python entry `main` -> `shivyc_pymain` in the emitted main.c
    # so it doesn't clash with the C `int main` we generate below.
    mainc = os.path.join(out, "main.c")
    src = open(mainc).read().replace(
        "obj main(int argc, char** argv)",
        "obj shivyc_pymain(int argc, char** argv)")
    open(mainc, "w").write(src)

    inits = _compiler_init_order(out)
    entry = ['#include "shivyc_rt.h"', ""]
    entry += ["void %s(void);" % s for s in inits]
    entry.append("obj shivyc_pymain(int argc, char** argv);")
    # Link stub for the optional internal ELF assembler. main.py reaches
    # rasm_obj.assemble_to_elf only under SHIVYC_RASM -- a runtime-only path that
    # dynamically imports tools/rpy_lib/rasm_obj via sys.path. That module isn't
    # part of the native compiler, so the path cannot run in the bootstrapped
    # binary; define the symbol so the (never-taken) reference resolves.
    entry.append("obj assemble_to_elf(char* _s);")
    entry.append(
        "obj assemble_to_elf(char* _s) { (void)_s; "
        "fputs(\"SHIVYC_RASM is unavailable in the bootstrap compiler\\n\", "
        "stderr); abort(); }")
    # Same story for the internal ELF linker (rlink): main.py imports
    # tools/rpy_lib/rlink only under SHIVYC_RLINK, a runtime-only path that is
    # not part of the native compiler. Stub the symbols the transpiled call
    # sites reference (`_rlink.Linker()` -> Linker_new, `_rlink._is_archive`)
    # so the never-taken references resolve at link time.
    entry.append("obj Linker_new(void);")
    entry.append("obj _is_archive(obj _d);")
    entry.append(
        "obj Linker_new(void) { "
        "fputs(\"SHIVYC_RLINK is unavailable in the bootstrap compiler\\n\", "
        "stderr); abort(); }")
    entry.append(
        "obj _is_archive(obj _d) { (void)_d; "
        "fputs(\"SHIVYC_RLINK is unavailable in the bootstrap compiler\\n\", "
        "stderr); abort(); }")
    entry.append("int main(int argc, char** argv) {")
    entry += ["    %s();" % s for s in inits]
    entry.append("    obj rc = shivyc_pymain(argc, argv);")
    entry.append("    return (rc.tag == T_INT || rc.tag == T_BOOL) "
                 "? (int)rc.u.i : 0;")
    entry.append("}")
    with open(os.path.join(out, "_entry.c"), "w") as f:
        f.write("\n".join(entry) + "\n")

    objs, fails = [], []
    for c in sorted(f for f in os.listdir(out) if f.endswith(".c")):
        oo = os.path.join(out, c[:-2] + ".o")
        res = compile_c(os.path.join(out, c), oo, inc_dirs=[out], extra=["-O0"])
        (objs.append(oo) if res.returncode == 0 else fails.append(c[:-2]))
    if fails:
        print("compile fails: %s" % ", ".join(fails[:10])); return 1
    exe = os.path.join(out, "shivyc_native")
    fl = run(["gcc"] + objs + ["-o", exe, "-lm",
              "-Wl,--allow-multiple-definition"])
    if fl.returncode != 0:
        print("link failed:\n" + fl.stderr[:1500]); return 1

    # Copy the bundled fallback headers next to the binary. The transpiled
    # preproc resolves <stddef.h> etc. relative to its module path, which under
    # self-host reduces to "include/<name>" off the current directory -- so the
    # headers must sit at <out>/include for the native compiler to find them
    # when it preprocesses a real C input.
    import shutil
    inc_src = os.path.join(REPO, "shivyc", "include")
    if os.path.isdir(inc_src):
        shutil.copytree(inc_src, os.path.join(out, "include"),
                        dirs_exist_ok=True)

    print("built native self-host compiler: %s (%d modules linked)"
          % (exe, len(objs) - 2))
    return 0


# Small C programs exercising the features the self-hosted native compiler
# currently supports, each with a known exit code. `bootstrap` compiles and runs
# each with the freshly built native binary to validate it end to end.
SMOKE_PROGRAMS = [
    ("const",      "int main(void){ return 42; }", 42),
    ("arith",      "int main(void){ return 3*4 + 10/2 - 1; }", 16),
    ("if_else",    "int main(void){ int x=5; if(x>3) return 7; else return 2; }", 7),
    ("while",      "int main(void){ int i=0,s=0; while(i<5){s+=i;i++;} return s; }", 10),
    ("for",        "int main(void){ int s=0; for(int i=1;i<=4;i++) s+=i; return s; }", 10),
    ("recursion",  "int f(int n){return n<=1?1:n*f(n-1);} int main(void){return f(5);}", 120),
    ("pointer",    "int main(void){ int x=9; int*p=&x; return *p; }", 9),
    ("array",      "int main(void){ int a[3]; a[0]=1;a[1]=2;a[2]=3; return a[0]+a[1]+a[2]; }", 6),
    ("ptr_arith",  "int main(void){ int a[3]; a[0]=10;a[1]=20; int*p=a; return *(p+1); }", 20),
    ("string_idx", 'int main(void){ char*s="ABC"; return s[1]; }', 66),
]


def _native_smoke(exe, quiet=False):
    """Compile+run each smoke program with `exe`; return (passed, total)."""
    passed = 0
    for name, src, want in SMOKE_PROGRAMS:
        d = tempfile.mkdtemp(prefix="shivyc-smoke-")
        cp = os.path.join(d, "p.c")
        op = os.path.join(d, "p.out")
        open(cp, "w").write(src)
        run([exe, cp, "-o", op])
        if os.path.exists(op):
            got = subprocess.run([op]).returncode
            ok = (got == want)
        else:
            got, ok = "compile-error", False
        log(quiet, "  %-4s %-11s -> %s (want %s)"
            % ("ok" if ok else "FAIL", name, got, want))
        passed += 1 if ok else 0
        shutil.rmtree(d, ignore_errors=True)
    return passed, len(SMOKE_PROGRAMS)


# Larger single-file feature-regression programs that pin native-codegen bugs
# fixed while bootstrapping the self-host compiler. Unlike SMOKE_PROGRAMS, these
# are validated against gcc (the C oracle) rather than a hardcoded exit code, so
# they stay robust as the programs grow. Add a new file here whenever a
# self-hosting bug is fixed (and a matching case to the file).
NATIVE_REGRESSION_FILES = [
    os.path.join(REPO, "tests", "fast", "selfhost_regressions.c"),
]


def _native_regressions(exe, quiet=False):
    """Compile+run each regression file with `exe` and with gcc; require the two
    exit codes to agree. Returns (passed, total)."""
    passed = 0
    for src in NATIVE_REGRESSION_FILES:
        name = os.path.basename(src)
        d = tempfile.mkdtemp(prefix="shivyc-reg-")
        cp = os.path.join(d, "p.c")
        shutil.copyfile(src, cp)
        gbin = os.path.join(d, "p.gcc")
        rg = run(["gcc", "-std=c99", "-O0", cp, "-o", gbin])
        want = subprocess.run([gbin]).returncode if rg.returncode == 0 else None
        nbin = os.path.join(d, "p.native")
        run([exe, cp, "-o", nbin])
        got = (subprocess.run([nbin]).returncode
               if os.path.exists(nbin) else "compile-error")
        ok = (want is not None and got == want)
        log(quiet, "  %-4s %-24s -> %s (gcc %s)"
            % ("ok" if ok else "FAIL", name, got, want))
        passed += 1 if ok else 0
        shutil.rmtree(d, ignore_errors=True)
    return passed, len(NATIVE_REGRESSION_FILES)


def cmd_bootstrap(build_root, args):
    """Stage 1: build the native self-hosted compiler (py2c -> gcc), smoke-test
    it, then benchmark its compile speed against gcc."""
    out = args.build_dir or os.path.join(build_root, "bootstrap")
    args.build_dir = out
    print("== bootstrap stage 1: building the native compiler ==")
    if cmd_compiler(build_root, args) != 0:
        return 1
    exe = os.path.join(out, "shivyc_native")

    print("\n== smoke test (native binary) ==")
    p, t = _native_smoke(exe, args.quiet)
    print("smoke: %d/%d passed" % (p, t))
    if p != t:
        print("bootstrap: native binary failed the smoke test")
        return 1

    print("\n== feature regressions (native vs gcc) ==")
    rp, rt = _native_regressions(exe, args.quiet)
    print("regressions: %d/%d passed" % (rp, rt))
    if rp != rt:
        print("bootstrap: native binary failed a feature regression")
        return 1

    print("\n== compile-speed benchmark: native shivyc vs gcc ==")
    bench = os.path.join(REPO, "benchmarks", "compile_speed",
                         "bench_compile_speed.py")
    env = dict(os.environ, SHIVYC=exe)
    subprocess.run([sys.executable, bench, "-n", "3"], cwd=REPO, env=env)

    print("\nbootstrap stage 1 complete -> %s" % exe)
    print("next: `make bootstrap2` (self-compile) then `make install`")
    return 0


def cmd_bootstrap2(build_root, args):
    """Stage 2: feed the compiler's own generated C back through the stage-1
    native binary, producing the final `shivycx`.

    A full stage-2 self-compile is the bootstrap milestone we are working
    toward; until the native compiler accepts every C construct it emits for its
    own source, this reports how many of the generated modules it can already
    compile (a concrete progress gauge) and the first blocker. When all modules
    self-compile it links `shivycx` and runs the full test suite against it."""
    out = args.build_dir or os.path.join(build_root, "bootstrap")
    exe = os.path.join(out, "shivyc_native")
    if not os.path.exists(exe):
        print("no stage-1 compiler at %s -- run `make bootstrap` first" % exe)
        return 1
    cfiles = sorted(f for f in os.listdir(out)
                    if f.endswith(".c") and not f.endswith(".s2.c"))
    print("== bootstrap stage 2: self-compiling %d generated modules =="
          % len(cfiles))
    objs, fails, first_err = [], [], None
    for c in cfiles:
        oo = os.path.join(out, c[:-2] + ".s2.o")
        if os.path.exists(oo):
            os.remove(oo)
        r = run([exe, os.path.join(out, c), "-c", "-o", oo], cwd=out)
        if os.path.exists(oo):
            objs.append(oo)
        else:
            fails.append(c)
            if first_err is None:
                msg = (r.stdout + r.stderr).strip().splitlines()
                first_err = (c, msg[0] if msg else "(no diagnostic)")
    print("self-compiled %d/%d modules" % (len(objs), len(cfiles)))
    if fails:
        print("\n%d module(s) the native compiler cannot yet compile."
              % len(fails))
        if first_err:
            print("first blocker: %s\n  %s" % first_err)
        print("\nThe native compiler does not yet accept all of its own "
              "generated C\n(the current bootstrap frontier -- see "
              "tools/SELFHOST_STATUS.md).")
        return 1

    shivycx = os.path.join(out, "shivycx")
    fl = run(["gcc"] + objs + ["-o", shivycx, "-lm",
              "-Wl,--allow-multiple-definition"])
    if fl.returncode != 0:
        print("stage-2 link failed:\n" + fl.stderr[:1500])
        return 1
    print("built final self-compiled compiler -> %s" % shivycx)

    print("\n== full test suite against shivycx ==")
    bindir = os.path.join(REPO, "bin")
    os.makedirs(bindir, exist_ok=True)
    shim = os.path.join(bindir, "shivyc")
    open(shim, "w").write('#!/bin/sh\nexec %s "$@"\n' % shivycx)
    os.chmod(shim, 0o755)
    env = dict(os.environ, PATH=bindir + ":" + os.environ.get("PATH", ""))
    r = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q"],
                       cwd=REPO, env=env)
    return r.returncode


def main():
    ap = argparse.ArgumentParser(description="ShivyCX self-host build/test")
    ap.add_argument("cmd", choices=["list", "test", "bench", "coverage",
                                    "link", "compiler", "bootstrap",
                                    "bootstrap2"])
    ap.add_argument("names", nargs="*", help="target name(s)")
    ap.add_argument("--musl", action="store_true",
                    help="compile transpiled C against packaged musl headers")
    ap.add_argument("--objcore", action="store_true",
                    help="include objcore-backed targets")
    ap.add_argument("--keep", action="store_true",
                    help="keep the scratch build dir")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("--build-dir", default=None,
                    help="build into this dir (kept) instead of a temp dir")
    args = ap.parse_args()

    if args.cmd == "list":
        print("self-host test targets:")
        for n, s in TARGETS.items():
            print("  %-8s [%s]  %s" % (n, s["backend"], s["desc"]))
        print("benchmarks:")
        for n, s in BENCHES.items():
            print("  %-8s [%s]  %s" % (n, s["backend"], s["desc"]))
        return 0

    if args.build_dir:
        build_root = args.build_dir
        os.makedirs(build_root, exist_ok=True)
        args.keep = True
    else:
        build_root = tempfile.mkdtemp(prefix="shivyc-selfhost-")
    try:
        if args.cmd == "test":
            rc = cmd_test(args.names, build_root, args)
        elif args.cmd == "bench":
            rc = cmd_bench(args.names, build_root, args) or 0
        elif args.cmd == "coverage":
            rc = cmd_coverage(build_root, args)
        elif args.cmd == "link":
            rc = cmd_link(build_root, args)
        elif args.cmd == "compiler":
            rc = cmd_compiler(build_root, args)
        elif args.cmd == "bootstrap":
            rc = cmd_bootstrap(build_root, args)
        elif args.cmd == "bootstrap2":
            rc = cmd_bootstrap2(build_root, args)
    finally:
        if args.keep:
            print("build dir:", build_root)
        else:
            shutil.rmtree(build_root, ignore_errors=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
