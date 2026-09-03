#!/usr/bin/env python3
"""py2c.py -- a source-to-source transpiler from the ShivyCX compiler's Python
source into C, specialized to the ShivyCX code base (not general Python).

See the NAMING CONVENTIONS section below: where a type is not annotated and not
easily recovered, the transpiler guesses it from the variable's name. This is
sound here because ShivyCX evolves slowly and follows consistent conventions.

OBJECT MODEL (why this can be smaller/faster than a PyObject*-based approach)
---------------------------------------------------------------------------
The codebase has no __getattr__/__setattr__, no metaclasses, no eval/exec, no
runtime class creation, and a fixed per-class attribute set. So we do NOT need
CPython's dict-based attributes or slot dispatch. Instead:

  Tier 0  concrete C types from inference (int, char*, struct*) -- no `obj`.
  Tier 1  each class -> a real C struct whose first member is an `Obj` header
          carrying a `TypeInfo*` (name + base chain + vtable). This gives:
            * isinstance  -> walk the base chain (isinstance_of)
            * virtual call -> one indirect call through a vtable slot
            * attributes  -> compile-time struct offsets (no dict)
          Subclass structs repeat inherited fields *first*, so a common prefix
          lays out identically across a family (header at 0, then base fields),
          making base<->derived pointer casts valid.
  Tier 2  a tagged `obj` word, used only where a value is genuinely dynamic
          (e.g. MemSpot.base which the source documents as `str | Spot`).

Memory: the whole compile is one arena (aalloc); freed in one shot. No
refcounting, no GC -- a batch compiler can do this; CPython cannot.

The C runtime (shivyc_rt.h / shivyc_rt.c) is embedded in this file as raw
strings and written to the output directory at transpile time, so a generated
module compiles with just `cc module.c shivyc_rt.c`.


NAMING CONVENTIONS (the contract for ShivyCX contributors)
----------------------------------------------------------
  * int   : index, idx, i, j, k, n, n1, n2, count, size, offset, chunk, num,
            length, len, pos, position, line, lineno, col, column, start, end,
            depth, level, width, height, amount, total, addr, address, byte,
            bytes, bits, rbp_offset, spot_size; or a name ending in
            _size/_offset/_count/_index/_len/_num/_idx.
  * char* : name, text, s, string, msg, message, filename, fname, func_name,
            tag, rep, content, spelling, label, identifier, prog, code,
            asm_code, asm_str, text_repr, mangled, suffix, prefix; or a name
            ending in _str.
  * bool  : names starting is_/has_/can_/should_/was_/use_/allow_, or exactly
            defined/ok/found/done/wide/signed/unsigned/const/volatile/valid/
            empty/present/enabled/success.
  * self  : pointer to the enclosing class' struct.
  * A Capitalized annotation / 'ForwardRef' (e.g. Spot, 'Spot') -> `Spot*`.
  Everything else falls back to the generic `obj`. A `: type` annotation always
  overrides the name-based guess.

Usage:
    python3 py2c.py                 # transpile every ../shivyc/*.py -> /tmp/*.c
    python3 py2c.py spots.py        # transpile the given file(s) -> /tmp
    python3 py2c.py --out DIR ...   # choose a different output directory
    python3 py2c.py --conventions   # print the naming-convention rules
    python3 py2c.py --stdlib-dir DIR --out DIR
                                    # batch-transpile python-stdlib to C
"""
import ast
import os
import re
import sys


# ==========================================================================
# python-stdlib module index (micropython-lib layout)
# ==========================================================================

_STDLIB_INDEX_CACHE: dict[str, dict[str, str]] = {}


def py_modname_from_path(path, stdlib_root):
    """Dotted Python module name for a file under python-stdlib, or None."""
    if not stdlib_root:
        return None
    ap = os.path.abspath(path)
    for mod, p in build_stdlib_index(stdlib_root).items():
        if os.path.abspath(p) == ap:
            return mod
    return None


def build_stdlib_index(stdlib_root):
    """Map dotted Python module names to .py source paths."""
    stdlib_root = os.fspath(stdlib_root)
    if stdlib_root in _STDLIB_INDEX_CACHE:
        return _STDLIB_INDEX_CACHE[stdlib_root]
    index = {}
    for pkg_name in sorted(os.listdir(stdlib_root)):
        pkg_dir = os.path.join(stdlib_root, pkg_name)
        if not os.path.isdir(pkg_dir):
            continue
        manifest = os.path.join(pkg_dir, "manifest.py")
        if not os.path.isfile(manifest):
            continue
        package = None
        module_files = []
        with open(manifest, encoding="utf-8") as _mfh:
            manifest_text = _mfh.read()
        for line in manifest_text.splitlines():
            line = line.split("#")[0].strip()
            m = re.match(r'package\("([^"]+)"\)', line)
            if m:
                package = m.group(1)
            m = re.match(r'module\("([^"]+)"\)', line)
            if m:
                module_files.append(m.group(1))
        if package:
            for walk_root, _dirs, filenames in os.walk(pkg_dir):
                for fn in sorted(filenames):
                    if not fn.endswith(".py") or fn == "manifest.py":
                        continue
                    py = os.path.join(walk_root, fn)
                    rel = os.path.relpath(py, pkg_dir)
                    rel_parts = rel.split(os.sep)
                    if not rel_parts or rel_parts[0] != package:
                        continue
                    if fn == "__init__.py":
                        mod = ".".join(rel_parts[:-1])
                    else:
                        rel_parts[-1] = os.path.splitext(rel_parts[-1])[0]
                        mod = ".".join(rel_parts)
                    index[mod] = py
        for mf in module_files:
            path = os.path.join(pkg_dir, mf)
            if os.path.isfile(path):
                index[os.path.splitext(os.path.basename(mf))[0]] = path
    _STDLIB_INDEX_CACHE[stdlib_root] = index
    return index


MICROPYTHON_TOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STDLIB = os.path.join(MICROPYTHON_TOP, "lib", "micropython-lib", "python-stdlib")


# ==========================================================================
# Embedded C runtime (written to the output dir at transpile time)
# ==========================================================================

RUNTIME_H = r'''#ifndef SHIVYC_RT_H
#define SHIVYC_RT_H
/* ShivyCX transpiler runtime -- generated by tools/py2c.py */
#include <stdbool.h>
#include <stddef.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <stdint.h>
#include <setjmp.h>
#include <math.h>

typedef char* str;

/* ---- Tier 2: generic dynamic value (used only where the type is open) ---- */
typedef struct Obj Obj;
enum { T_NONE, T_INT, T_BOOL, T_STR, T_OBJ, T_LIST, T_DICT, T_FUNC, T_FLOAT, T_SET };
typedef struct { unsigned char tag; union { long i; str s; Obj* o; double d; } u; } obj;

#define OBJ_NONE    ((obj){T_NONE,{0}})
#define OBJ_INT(x)  ((obj){T_INT,{.i=(long)(x)}})
#define OBJ_BOOL(x) ((obj){T_BOOL,{.i=(long)(x)}})
#define OBJ_STR(x)  ((obj){T_STR,{.s=(str)(x)}})
#define OBJ_OBJ(x)  ((obj){T_OBJ,{.o=(Obj*)(x)}})
#define OBJ_FLOAT(x) ((obj){T_FLOAT,{.d=(double)(x)}})
#define IS_OBJ(v)   ((v).tag==T_OBJ)
#define IS_NONE(v)  ((v).tag==T_NONE)
#define IS_FLOAT(v) ((v).tag==T_FLOAT)
#define AS_OBJ(v)   ((v).u.o)
#define AS_INT(v)   ((v).u.i)
#define AS_STR(v)   ((v).u.s)
#define AS_FLOAT(v) ((v).u.d)

/* ---- Tier 1: object header -- first member of every transpiled struct ---- */
/* `type` points at a per-module TypeInfo; kept void* here so this header is
   module-agnostic. Each module casts it to its own TypeInfo. The base chain
   used by isinstance lives at a fixed offset, so isinstance_of works on any
   module's TypeInfo as long as {name; base;} are the first two members. */
struct Obj { const void* type; };

/* ---- Pointer compression (V8-style flat 32-bit in-struct pointers) --------
   With -f-pointer-compression the program is linked non-PIE at a low load
   address (-f-low-mem), so EVERY object address -- arena objects and .rodata
   string/TypeInfo pointers alike -- fits in the low 4 GiB. A class-pointer
   struct field can then be stored as a 32-bit `cptr` (the low 32 address bits)
   and recovered by zero-extension, halving 8-byte pointer fields to 4 bytes.
   PACK truncates, UNPACK<T> zero-extends back to a real pointer. When the flag
   is off, `cptr` is a plain pointer and PACK/UNPACK are identities, so the
   default build is byte-for-byte unchanged. */
#ifdef SHIVYC_PCOMPRESS
typedef uint32_t cptr;
#define PTR_PACK(p)       ((cptr)(uintptr_t)(const void*)(p))
#define PTR_UNPACK(T, c)  ((T)(uintptr_t)(c))
#else
typedef void* cptr;
#define PTR_PACK(p)       ((void*)(p))
#define PTR_UNPACK(T, c)  ((T)(c))
#endif

/* Per-type field table for bridge-free dynamic attribute access. Each entry is
   a field name -> byte offset within the struct + a 1-char storage code:
     i=int  l=long  b=bool  d=double  f=float  s=char*  o=obj(16B)  p=Obj*
   The array is terminated by a {NULL,...} sentinel. rt_getattr/rt_setattr walk
   it (plus the base chain) instead of routing through the micropython core. */
typedef struct FieldDesc {
    const char* name;
    long off;
    char tc;
} FieldDesc;

typedef struct TypeInfoHdr {
    const char* name;
    const struct TypeInfoHdr* base;
    const FieldDesc* fields;
    obj (*tostr)(Obj*);
    bool (*eq)(Obj*, obj);
    obj (*addfn)(Obj*, obj);  /* user __add__, or NULL */
    unsigned long objsize;   /* sizeof the concrete struct; for shallow copy */
} TypeInfoHdr;

static inline bool isinstance_of(Obj* o, const void* t) {
    const TypeInfoHdr* want = (const TypeInfoHdr*)t;
    const TypeInfoHdr* k = o ? (const TypeInfoHdr*)o->type : NULL;
    for (; k; k = k->base) if (k == want) return true;
    return false;
}
/* isinstance on an obj-word: true only if it boxes a matching Obj* */
#define OBJ_ISINST(v, t) (IS_OBJ(v) && isinstance_of((v).u.o, (t)))

/* fetch a transpiled value's TypeInfo (cast to the module's TypeInfo type) */
#define TYPEINFO(T, o) ((const T*)((Obj*)(o))->type)

/* Bridge-free dynamic attribute access on an object-model instance. Walks the
   receiver type's field table (and base chain); a declared field is read/
   written directly at its offset. rt_getattr returns `dflt` for an absent
   field; rt_setattr is a no-op for an absent one. No micropython core. */
obj  rt_getattr(obj recv, const char* name, obj dflt);
void rt_setattr(obj recv, const char* name, obj val);

/* ---- first-class functions: a callable obj (T_FUNC) is a closure: a uniform
   function pointer plus a captured-environment obj. All transpiled functions
   used as values get a trampoline of this shape that unpacks the arg list. -- */
typedef obj (*ClosureFn)(obj env, obj args);   /* args is a T_LIST of arguments */
typedef struct Closure { Obj _hdr; ClosureFn fn; obj env; } Closure;
obj  make_closure(ClosureFn fn, obj env);
obj  call_closure(obj f, obj args);
obj  identity__tramp(obj env, obj args);
obj  call_obj(obj f, int n, ...);              /* convenience: builds arg list  */
obj  call_obj_a(obj f, obj* a, int n);         /* varargs-free call_obj         */

/* ---- arena: bump-allocate; free the whole compile at once (no refcount) -- */
void* aalloc(size_t n);
long pymod(long a, long b);
long pyfdiv(long a, long b);
void  afree(void* p, size_t n);   /* manual reclaim for Python `del` (see .c) */
obj   arena_mark(void);           /* snapshot arena bump pointer (scratch scope) */
void  arena_release(obj mark);    /* free everything bump-allocated since mark   */
void  arena_reset(void);

/* micropython-lib manifest.py packaging metadata (no runtime effect in C) */
void metadata(void);
void module(const char* name);
void require(const char* name);
void package(const char* name);

/* ---- small python-ish helpers ---- */
str  pystr(obj v);                 /* str(x)                                  */
str  pyrepr(obj v);                 /* repr() — quotes strings inside containers */
str  pyfmt(int n, const char* fmt, ...); /* f-strings: "{}..." + obj args     */
str  pyfmt_a(const char* fmt, obj* args, int n);  /* varargs-free f-strings    */
str  pyconcat(str a, str b);       /* "x" + y                                 */
bool truthy(obj v);                /* Python truthiness of a Tier-2 value     */
double as_dbl(obj v);              /* obj -> double (int/bool widen, else .d) */

/* ---- lists: growable obj vector, represented as a tagged obj (T_LIST) ----- */
typedef struct { obj* data; int len; int cap; } List;
obj  list_new(void);
obj  list_of(int n, ...);          /* [a, b, c] literal                       */
obj  list_from(obj* a, int n);     /* varargs-free list build (stack array)    */
obj  list_copy(obj src);           /* copy a (possibly stack-backed) list to heap */
obj  set_from(obj* a, int n);      /* like list_from, de-duplicated (sets)     */
obj  varg_list(int n, va_list ap); /* collect C varargs (obj) into a list     */
void list_append(obj lst, obj v);
void list_insert(obj lst, long i, obj v);
void list_extend(obj lst, obj it);      /* append all elements of it           */
obj  list_get(obj lst, long i);    /* supports negative indices               */
void list_set(obj lst, long i, obj v);
long pylen(obj v);                 /* len(list) or len(str)                   */
void list_free(obj lst);           /* reclaim a dead list (see .c)            */
obj  index_obj(obj container, long i);  /* container[i] for list/str          */
bool obj_eq(obj a, obj b);         /* == on Tier-2 values                     */
bool pycontains(obj container, obj v);  /* v in container                     */
bool in_str(str needle, const str* hay, int n);  /* x in ("a","b",...)        */
void pyprint(obj v);               /* print(x)                                */
void pyprint_a(obj* a, int n);     /* print(x, y, ...) -- space separated      */

/* exceptions: try/except lowers to setjmp on g_exc_jmp; raise -> rt_raise. */
extern jmp_buf g_exc_jmp[];
extern int     g_exc_sp;
extern obj     g_exc_val;
obj  rt_exc_value(void);
void rt_raise(obj e);

/* ---- dicts: insertion-ordered obj->obj map, tagged obj (T_DICT) ----------- */
typedef struct { obj key; obj val; } DEnt;
typedef struct { DEnt* e; int len; int cap; } Dict;
obj  dict_new(void);
obj  dict_of(int n, ...);          /* {k1:v1, k2:v2, ...} (2n varargs)         */
obj  dict_of_a(obj* a, int n);     /* varargs-free dict_of (a = n key,val pairs) */
obj  list_pair(obj a, obj b);      /* 2-element list, no varargs               */
void dict_set(obj d, obj k, obj v);
obj  dict_get(obj d, obj k, obj dflt);
bool dict_contains(obj d, obj k);
obj  dict_pop(obj d, obj k, obj dflt);
obj  dict_setdefault(obj d, obj k, obj dflt);
void dict_update(obj d, obj other);
obj  pycopy(obj c);                /* shallow copy of a dict / list / set      */
obj  dict_from(obj src);           /* dict(x): copy a dict, build from pairs   */
obj  dict_keys(obj d);
obj  dict_values(obj d);
obj  dict_items(obj d);            /* list of [k, v] pairs                     */

obj  subscript(obj container, obj key);       /* container[key] (list/dict/str) */
void subscript_set(obj container, obj key, obj v);  /* container[key] = v       */

/* ---- arithmetic / comparison on Tier-2 values ---- */
obj  obj_add(obj a, obj b);        /* +  (int, str concat, list concat)       */
obj  obj_sub(obj a, obj b);
obj  obj_mul(obj a, obj b);        /* *  (int, str/list repeat)               */
obj  obj_fdiv(obj a, obj b);       /* // */
obj  obj_mod(obj a, obj b);
str  str_mod(const char* fmt, obj* args, int n);  /* `fmt % args` formatting */
obj  obj_neg(obj a);               /* unary -  */
obj  obj_invert(obj a);            /* unary ~  */
obj  obj_bin(char op, obj a, obj b);  /* &|^ and << >>                        */
long ipow(long base, long exp);       /* integer ** (exp >= 0)                */
obj  obj_pow(obj a, obj b);           /* Tier-2 **                            */
long obj_cmp(obj a, obj b);        /* <0,0,>0 for < <= > >=                   */
obj  pyrange(long lo, long hi, long step);

/* ---- string methods (operate on char*) ---- */
str str_mod_spread(str fmt, obj args);
bool str_startswith(str s, str p);
bool str_endswith(str s, str p);
bool str_startswith_at(str s, str p, long start);
bool str_endswith_at(str s, str p, long start);
str  str_strip(str s, int mode);   /* 0 both, 1 left, 2 right                 */
obj  str_split(str s, str sep);    /* sep NULL -> split on whitespace runs    */
obj  str_partition(str s, str sep);/* (before, sep_or_'', after) as a 3-list  */
obj  str_splitlines(str s);
obj  str_rsplit(str s, str sep, long maxsplit);
str  str_replace(str s, str a, str b);
long str_find(str s, str sub, bool last);
long str_find_from(str s, str sub, bool last, long start);
long str_find_range(str s, str sub, bool last, long start, long end);
long str_count(str s, str sub, long start, long end);
bool str_isdigit(str s);
bool str_isalpha(str s);
bool str_isspace(str s);
bool str_isalnum(str s);
str  str_lower(str s);
str  str_upper(str s);
str  pyjoin(str sep, obj it);

/* ---- common builtins ---- */
obj  pyenumerate(obj it, long start);   /* list of [i, x]                     */
obj  pyzip(obj a, obj b);               /* list of [x, y] (shortest)          */
obj  pyzip3(obj a, obj b, obj c);       /* list of [x, y, z] (shortest)       */
obj  pyzip4(obj a, obj b, obj c, obj d); /* list of [w, x, y, z] (shortest)   */
obj  pysorted(obj it);                  /* natural-order sort (obj_cmp)        */
void list_sort(obj lst);                /* in-place natural sort               */
obj  pymax(obj it, obj dflt, bool has_dflt);
obj  pymin(obj it, obj dflt, bool has_dflt);
obj  pysum(obj it, obj start);
obj  pyreversed(obj it);
obj  pyset(obj it);                     /* dedup iterable into a set (T_SET)   */
obj  set_union(obj a, obj b);           /* a | b   (all return a fresh T_SET)  */
obj  set_inter(obj a, obj b);           /* a & b                               */
obj  set_diff(obj a, obj b);            /* a - b                               */
obj  set_symdiff(obj a, obj b);         /* a ^ b                               */
#define PY_SLICE_END 0x7fffffffffffffffL
obj  py_slice(obj seq, long lo, long hi);  /* str or list slice, Python rules  */
obj  py_slice_step(obj seq, long lo, long hi, long step, int hl, int hh);
str  char_at(str s, long i);            /* s[i] on a string -> 1-char string   */
void set_add(obj s, obj v);             /* add to a list-set if absent         */
void pyclear(obj c);                    /* list/set/dict .clear() (empty in place) */
void list_remove(obj lst, obj v);       /* remove first matching element       */
obj  list_index(obj lst, obj v);        /* position of first match (else abort) */
long list_count(obj lst, obj v);        /* count of elements equal to v          */
obj  list_reverse(obj lst);             /* in-place reverse, returns None         */
obj  list_pop(obj lst);                 /* remove & return last element        */
obj  float_to_bits(obj val, long size); /* IEEE-754 float -> int bit pattern   */
obj  float_fromhex(str s);              /* float.fromhex: strtod (hex floats)  */
obj  pyfloat(obj v);                    /* float(x): str/int/float -> double    */
void list_assign_slice(obj dst, obj src); /* dst[:] = src (replace contents)   */
void list_set_slice(obj dst, long lo, long hi, obj src); /* dst[lo:hi] = src   */
obj  obj_augop(obj a, char op, obj b);  /* x op= y for obj (arith / set ops)   */
void del_item(obj c, obj k);            /* del c[k] for dict (key) or list (i) */
obj  pylist(obj it);                    /* shallow copy iterable into a list   */
long pyord(obj c);
str  pychr(long i);
long pyint(obj v);
long py_int_base(str s, long base);     /* int(s, base) incl 0x/0b/0o prefix   */
long pyabs(long x);

/* Dynamic-dispatch bridge: emitted in stdlib mode for setattr/getattr and
   other genuinely dynamic operations. Defined by the micropython core
   (mp_stdlib_bridge) in a full stdlib link; declared here so a module that
   falls back to one of them still compiles on its own. */
obj  mp_call_import(const char* mod, const char* attr, int n, ...);
obj  mp_call_method(obj recv, const char* attr, int n, ...);
obj  mp_call_obj(obj fun, obj args, obj kwargs);
bool mp_hasattr(obj recv, const char* attr);
obj  mp_getattr(obj recv, const char* attr, obj dflt);
obj  mp_getattr_obj(obj recv, obj attr, obj dflt);
obj    rpy_eval(const char* src);       /* eval(s) -> boxed result            */
long   rpy_eval_int(const char* src);   /* x: int   = eval(s)                 */
double rpy_eval_float(const char* src); /* x: float = eval(s)                 */
bool   rpy_eval_bool(const char* src);  /* x: bool  = eval(s)                 */
str    rpy_eval_str(const char* src);   /* x: str   = eval(s)                 */
void   rpy_exec(const char* src);       /* exec(s): run statements            */

#endif /* SHIVYC_RT_H */
'''

MP_BRIDGE_H = r'''#ifndef MP_STDLIB_BRIDGE_H
#define MP_STDLIB_BRIDGE_H
#include "shivyc_rt.h"
obj mp_call_import(const char* mod, const char* attr, int n, ...);
obj mp_call_method(obj recv, const char* attr, int n, ...);
obj mp_call_obj(obj fun, obj args, obj kwargs);
bool mp_hasattr(obj recv, const char* attr);
obj mp_getattr(obj recv, const char* attr, obj dflt);
obj mp_getattr_obj(obj recv, obj attr, obj dflt);
obj    rpy_eval(const char* src);       /* eval(s) -> boxed result            */
long   rpy_eval_int(const char* src);   /* x: int   = eval(s)                 */
double rpy_eval_float(const char* src); /* x: float = eval(s)                 */
bool   rpy_eval_bool(const char* src);  /* x: bool  = eval(s)                 */
str    rpy_eval_str(const char* src);   /* x: str   = eval(s)                 */
void   rpy_exec(const char* src);       /* exec(s): run statements            */
#endif
'''

MP_BRIDGE_C = r'''#include "mpconfigstdlib.h"
#include "mp_stdlib_bridge.h"
#include <stdarg.h>
#include <string.h>
#include "py/runtime.h"
#include "py/obj.h"
#include "py/objstr.h"
#include "py/objlist.h"
#include "py/qstr.h"
#include "py/compile.h"
#include "py/lexer.h"
#include "py/gc.h"
#include "py/stackctrl.h"

static mp_obj_t obj_to_mp(obj v) {
    switch (v.tag) {
        case T_NONE: return mp_const_none;
        case T_INT: return mp_obj_new_int(v.u.i);
        case T_BOOL: return mp_obj_new_bool((bool)v.u.i);
        case T_STR: return mp_obj_new_str(v.u.s, v.u.s ? strlen(v.u.s) : 0);
        case T_FLOAT: return mp_obj_new_float(v.u.d);
        case T_SET:
        case T_LIST: {
            List* l = (List*)v.u.o;
            mp_obj_t items[l->len];
            for (int i = 0; i < l->len; i++) items[i] = obj_to_mp(l->data[i]);
            return mp_obj_new_list(l->len, items);
        }
        default: return mp_const_none;
    }
}

static obj mp_to_obj(mp_obj_t v) {
    if (v == mp_const_none) return OBJ_NONE;
    if (v == mp_const_true) return OBJ_BOOL(1);
    if (v == mp_const_false) return OBJ_BOOL(0);
    if (mp_obj_is_integer(v)) return OBJ_INT(mp_obj_get_int(v));
    if (mp_obj_is_str(v)) {
        size_t len; const char* s = mp_obj_str_get_data(v, &len);
        char* out = aalloc(len + 1);
        memcpy(out, s, len); out[len] = 0;
        return OBJ_STR(out);
    }
    if (mp_obj_is_float(v)) return OBJ_FLOAT(mp_obj_get_float(v));
    return OBJ_OBJ((Obj*)v);
}

obj mp_call_import(const char* mod, const char* attr, int n, ...) {
    qstr qm = qstr_from_str(mod);
    mp_obj_t module = mp_import_name(qm, MP_OBJ_NEW_SMALL_INT(0), MP_OBJ_NEW_SMALL_INT(0));
    qstr qa = qstr_from_str(attr);
    mp_obj_t fun = mp_load_attr(module, qa);
    mp_obj_t args[n];
    va_list ap; va_start(ap, n);
    for (int i = 0; i < n; i++) args[i] = obj_to_mp(va_arg(ap, obj));
    va_end(ap);
    return mp_to_obj(mp_call_function_n_kw(fun, n, 0, args));
}

obj mp_call_method(obj recv, const char* attr, int n, ...) {
    mp_obj_t dest[4];
    mp_obj_t o = obj_to_mp(recv);
    qstr qa = qstr_from_str(attr);
    mp_load_method(o, qa, dest);
    mp_obj_t args[n];
    va_list ap; va_start(ap, n);
    for (int i = 0; i < n; i++) args[i] = obj_to_mp(va_arg(ap, obj));
    va_end(ap);
    return mp_to_obj(mp_call_method_n_kw(n, 0, args));
}

obj mp_call_obj(obj fun, obj args, obj kwargs) {
    mp_obj_t f = obj_to_mp(fun);
    List* l = (List*)args.u.o;
    size_t n = l ? (size_t)l->len : 0;
    Dict* kd = (kwargs.tag == T_DICT) ? (Dict*)kwargs.u.o : NULL;
    size_t nkw = kd ? (size_t)kd->len : 0;
    mp_obj_t call_args[n + 2 * nkw];
    for (size_t i = 0; i < n; i++) {
        call_args[i] = obj_to_mp(l->data[i]);
    }
    for (size_t i = 0; i < nkw; i++) {
        call_args[n + 2 * i] = obj_to_mp(kd->e[i].key);
        call_args[n + 2 * i + 1] = obj_to_mp(kd->e[i].val);
    }
    return mp_to_obj(mp_call_function_n_kw(f, n, nkw, call_args));
}

bool mp_hasattr(obj recv, const char* attr) {
    mp_obj_t o = obj_to_mp(recv);
    qstr qa = qstr_from_str(attr);
    mp_obj_t dest[2];
    mp_load_method_protected(o, qa, dest, false);
    return dest[0] != MP_OBJ_NULL;
}

obj mp_getattr(obj recv, const char* attr, obj dflt) {
    mp_obj_t o = obj_to_mp(recv);
    qstr qa = qstr_from_str(attr);
    mp_obj_t dest[2];
    mp_load_method_protected(o, qa, dest, false);
    if (dest[0] == MP_OBJ_NULL) return dflt;
    return mp_to_obj(dest[0]);
}

obj mp_getattr_obj(obj recv, obj attr, obj dflt) {
    mp_obj_t o = obj_to_mp(recv);
    mp_obj_t a = obj_to_mp(attr);
    size_t len; const char* s = mp_obj_str_get_data(a, &len);
    char buf[256];
    if (len >= sizeof buf) len = sizeof buf - 1;
    memcpy(buf, s, len); buf[len] = 0;
    return mp_getattr(recv, buf, dflt);
}

/* ---- eval(): evaluate a Python expression string through the MicroPython core.
   A standalone (non-stdlib) program has no other mp_init, so initialize the
   interpreter lazily on first use. mp_to_obj boxes the result for untyped eval;
   the typed variants pull the value straight out of the result mp_obj_t (a
   small int is just a shifted field), so `x: int = eval(...)` needs no obj. */
static int rpy_mp_ready = 0;
static char rpy_mp_heap[192 * 1024];

void rpy_mp_ensure(void) {
    if (rpy_mp_ready) return;
    rpy_mp_ready = 1;
    mp_stack_ctrl_init();
    mp_stack_set_limit(0x10000);
    gc_init(rpy_mp_heap, rpy_mp_heap + sizeof(rpy_mp_heap));
    mp_init();
}

static mp_obj_t rpy_mp_eval_raw(const char* src) {
    rpy_mp_ensure();
    nlr_buf_t nlr;
    if (nlr_push(&nlr) == 0) {
        mp_lexer_t* lex = mp_lexer_new_from_str_len(
            MP_QSTR__lt_string_gt_, src, strlen(src), 0);
        qstr src_name = lex->source_name;
        mp_parse_tree_t pt = mp_parse(lex, MP_PARSE_EVAL_INPUT);
        mp_obj_t fun = mp_compile(&pt, src_name, false);
        mp_obj_t res = mp_call_function_0(fun);
        nlr_pop();
        return res;
    }
    /* Uncaught exception in the evaluated expression: surface as None. */
    return mp_const_none;
}

obj rpy_eval(const char* src) { return mp_to_obj(rpy_mp_eval_raw(src)); }

long rpy_eval_int(const char* src) {
    return mp_obj_get_int(rpy_mp_eval_raw(src));
}

double rpy_eval_float(const char* src) {
    return mp_obj_get_float(rpy_mp_eval_raw(src));
}

bool rpy_eval_bool(const char* src) {
    return mp_obj_is_true(rpy_mp_eval_raw(src));
}

str rpy_eval_str(const char* src) {
    mp_obj_t r = rpy_mp_eval_raw(src);
    size_t len; const char* s = mp_obj_str_get_data(r, &len);
    char* out = aalloc(len + 1);
    memcpy(out, s, len); out[len] = 0;
    return out;
}

/* exec(): run a block of Python *statements* (FILE input), e.g. a whole script.
   Unlike eval there is no value; an uncaught exception is printed like CPython
   would and execution of the caller continues. */
void rpy_exec(const char* src) {
    rpy_mp_ensure();
    nlr_buf_t nlr;
    if (nlr_push(&nlr) == 0) {
        mp_lexer_t* lex = mp_lexer_new_from_str_len(
            MP_QSTR__lt_string_gt_, src, strlen(src), 0);
        qstr src_name = lex->source_name;
        mp_parse_tree_t pt = mp_parse(lex, MP_PARSE_FILE_INPUT);
        mp_obj_t fun = mp_compile(&pt, src_name, false);
        mp_call_function_0(fun);
        nlr_pop();
    } else {
        mp_obj_print_exception(&mp_plat_print, MP_OBJ_FROM_PTR(nlr.ret_val));
    }
}
'''

# Non-bridge implementations of the dynamic-dispatch functions: when the
# micropython core is NOT linked (a plain self-host / glibc build), genuinely
# dynamic operations resolve against the ShivyCX object model itself
# (rt_getattr/rt_setattr/call_obj). Appended to shivyc_rt.c by write_runtime
# when mp_bridge is false. Semantics match the bridge for ShivyCX objects:
# getattr returns a declared field or the default; setattr writes a declared
# field; a callable obj is invoked positionally.
MP_NOBRIDGE_C = r'''
/* --- dynamic-dispatch (no micropython bridge) --------------------------- */
obj mp_getattr(obj recv, const char* attr, obj dflt) {
    return rt_getattr(recv, attr, dflt);
}

obj mp_getattr_obj(obj recv, obj attr, obj dflt) {
    return rt_getattr(recv, AS_STR(attr), dflt);
}

bool mp_hasattr(obj recv, const char* attr) {
    /* present iff rt_getattr returns something other than our private miss
       sentinel (two distinct sentinels disambiguate a real None field). */
    obj miss = {T_OBJ, {0}};
    obj got = rt_getattr(recv, attr, miss);
    return !(got.tag == T_OBJ && got.u.o == 0);
}

obj mp_call_obj(obj fun, obj args, obj kwargs) {
    (void)kwargs;                 /* self-host dynamic calls are positional */
    List* l = (args.tag == T_LIST || args.tag == T_SET) ? (List*)args.u.o : 0;
    int n = l ? (int)l->len : 0;
    return call_obj_a(fun, l ? l->data : (obj*)0, n);
}

obj mp_call_method(obj recv, const char* attr, int n, ...) {
    /* x.attr(...) on a dynamic obj: fetch the (function) attribute, then call
       it positionally. */
    obj fun = rt_getattr(recv, attr, OBJ_NONE);
    obj av[16];
    if (n > 16) n = 16;
    va_list ap; va_start(ap, n);
    for (int i = 0; i < n; i++) av[i] = va_arg(ap, obj);
    va_end(ap);
    return call_obj_a(fun, av, n);
}

obj mp_call_import(const char* mod, const char* attr, int n, ...) {
    /* The only dynamic builtins call ShivyCX emits without the bridge is
       setattr(obj, name, value); route it to the object model. */
    va_list ap; va_start(ap, n);
    obj r = OBJ_NONE;
    if (strcmp(mod, "builtins") == 0 && strcmp(attr, "setattr") == 0 && n == 3) {
        obj recv = va_arg(ap, obj);
        obj name = va_arg(ap, obj);
        obj val  = va_arg(ap, obj);
        rt_setattr(recv, AS_STR(name), val);
    } else if (strcmp(mod, "builtins") == 0 && strcmp(attr, "getattr") == 0
               && n >= 2) {
        obj recv = va_arg(ap, obj);
        obj name = va_arg(ap, obj);
        obj dflt = (n >= 3) ? va_arg(ap, obj) : OBJ_NONE;
        r = rt_getattr(recv, AS_STR(name), dflt);
    }
    va_end(ap);
    return r;
}
'''

# Self-contained native evaluator for eval() of Python *expressions*, used in
# place of the MicroPython core (nothing external to link). minipy's compiler
# and parser are Python, so a transpiled C binary cannot compile arbitrary
# source at runtime; what it can do cheaply -- and what the overwhelmingly common
# eval() pattern needs (e.g. eval(f"{a}+{b}")) -- is evaluate a literal numeric
# expression. This recursive-descent evaluator covers int/float literals,
# + - * / // % ** with Python's floor/mod semantics, unary +/-, parentheses, and
# the six comparisons, boxing the result into the ShivyCX object model. Anything
# outside that grammar (names, calls, containers) needs the minipy frontend
# compiled to C and is future work.
MINIPY_EVAL_C = r'''/* ---- minipy eval: native expression evaluator (no MicroPython) ---- */
#include <math.h>
typedef struct { int isf; long i; double d; } mpe_val;
static double mpe_tod(mpe_val v) { return v.isf ? v.d : (double)v.i; }
static mpe_val mpe_int(long x)  { mpe_val v; v.isf=0; v.i=x; v.d=0; return v; }
static mpe_val mpe_flt(double x){ mpe_val v; v.isf=1; v.i=0; v.d=x; return v; }

typedef struct { const char* s; } mpe_p;
static void mpe_ws(mpe_p* p){ while(*p->s==' '||*p->s=='\t') p->s++; }
static mpe_val mpe_cmp(mpe_p* p);

static mpe_val mpe_atom(mpe_p* p){
    mpe_ws(p);
    if(*p->s=='('){ p->s++; mpe_val v=mpe_cmp(p); mpe_ws(p); if(*p->s==')') p->s++; return v; }
    const char* st=p->s; int isf=0;
    while(*p->s>='0'&&*p->s<='9') p->s++;
    if(*p->s=='.'){ isf=1; p->s++; while(*p->s>='0'&&*p->s<='9') p->s++; }
    if(*p->s=='e'||*p->s=='E'){ isf=1; p->s++; if(*p->s=='+'||*p->s=='-') p->s++;
        while(*p->s>='0'&&*p->s<='9') p->s++; }
    if(isf) return mpe_flt(strtod(st,0));
    return mpe_int(strtol(st,0,10));
}
static mpe_val mpe_unary(mpe_p* p){
    mpe_ws(p);
    if(*p->s=='-'){ p->s++; mpe_val v=mpe_unary(p); return v.isf?mpe_flt(-v.d):mpe_int(-v.i); }
    if(*p->s=='+'){ p->s++; return mpe_unary(p); }
    return mpe_atom(p);
}
static mpe_val mpe_power(mpe_p* p){
    mpe_val b=mpe_unary(p); mpe_ws(p);
    if(p->s[0]=='*'&&p->s[1]=='*'){ p->s+=2; mpe_val e=mpe_power(p);
        double r=pow(mpe_tod(b),mpe_tod(e));
        if(!b.isf&&!e.isf&&e.i>=0) return mpe_int((long)(r+0.5));
        return mpe_flt(r); }
    return b;
}
static mpe_val mpe_term(mpe_p* p){
    mpe_val a=mpe_power(p);
    for(;;){ mpe_ws(p); char c=*p->s;
        if(c=='*'&&p->s[1]!='*'){ p->s++; mpe_val b=mpe_power(p);
            a = (a.isf||b.isf) ? mpe_flt(mpe_tod(a)*mpe_tod(b)) : mpe_int(a.i*b.i); }
        else if(c=='/'&&p->s[1]=='/'){ p->s+=2; mpe_val b=mpe_power(p);
            if(a.isf||b.isf){ a=mpe_flt(floor(mpe_tod(a)/mpe_tod(b))); }
            else { long q=a.i/b.i; if((a.i%b.i!=0)&&((a.i<0)!=(b.i<0))) q--; a=mpe_int(q); } }
        else if(c=='/'){ p->s++; mpe_val b=mpe_power(p); a=mpe_flt(mpe_tod(a)/mpe_tod(b)); }
        else if(c=='%'){ p->s++; mpe_val b=mpe_power(p);
            if(a.isf||b.isf){ double m=fmod(mpe_tod(a),mpe_tod(b));
                if(m!=0&&((m<0)!=(mpe_tod(b)<0))) m+=mpe_tod(b); a=mpe_flt(m); }
            else { long m=a.i%b.i; if(m!=0&&((m<0)!=(b.i<0))) m+=b.i; a=mpe_int(m); } }
        else break; }
    return a;
}
static mpe_val mpe_add(mpe_p* p){
    mpe_val a=mpe_term(p);
    for(;;){ mpe_ws(p); char c=*p->s;
        if(c=='+'){ p->s++; mpe_val b=mpe_term(p);
            a=(a.isf||b.isf)?mpe_flt(mpe_tod(a)+mpe_tod(b)):mpe_int(a.i+b.i); }
        else if(c=='-'){ p->s++; mpe_val b=mpe_term(p);
            a=(a.isf||b.isf)?mpe_flt(mpe_tod(a)-mpe_tod(b)):mpe_int(a.i-b.i); }
        else break; }
    return a;
}
static mpe_val mpe_cmp(mpe_p* p){
    mpe_val a=mpe_add(p); mpe_ws(p); const char* s=p->s; int op=0;
    if(s[0]=='='&&s[1]=='='){op=1;p->s+=2;} else if(s[0]=='!'&&s[1]=='='){op=2;p->s+=2;}
    else if(s[0]=='<'&&s[1]=='='){op=3;p->s+=2;} else if(s[0]=='>'&&s[1]=='='){op=4;p->s+=2;}
    else if(s[0]=='<'){op=5;p->s++;} else if(s[0]=='>'){op=6;p->s++;}
    if(op){ mpe_val b=mpe_add(p); double x=mpe_tod(a),y=mpe_tod(b); long r=0;
        if(op==1)r=(x==y);else if(op==2)r=(x!=y);else if(op==3)r=(x<=y);
        else if(op==4)r=(x>=y);else if(op==5)r=(x<y);else r=(x>y);
        return mpe_int(r); }
    return a;
}
static mpe_val mpe_run(const char* src){ mpe_p p; p.s=src; return mpe_cmp(&p); }

obj    rpy_eval(const char* src){ mpe_val v=mpe_run(src); return v.isf?OBJ_FLOAT(v.d):OBJ_INT(v.i); }
long   rpy_eval_int(const char* src){ mpe_val v=mpe_run(src); return v.isf?(long)v.d:v.i; }
double rpy_eval_float(const char* src){ return mpe_tod(mpe_run(src)); }
bool   rpy_eval_bool(const char* src){ mpe_val v=mpe_run(src); return v.isf?(v.d!=0.0):(v.i!=0); }
str    rpy_eval_str(const char* src){ mpe_val v=mpe_run(src); char* b=aalloc(40);
    if(v.isf) sprintf(b,"%g",v.d); else sprintf(b,"%ld",v.i); return b; }
void   rpy_exec(const char* src){ (void)src; /* statements need the minipy frontend in C */ }
'''

RUNTIME_C = r'''/* ShivyCX transpiler runtime -- generated by tools/py2c.py */
#include <ctype.h>
#include "shivyc_rt.h"

/* libc numeric parsers (declared explicitly so the C11-subset front end can
   compile this runtime without <stdlib.h>). */
double strtod(const char *, char **);
long   strtol(const char *, char **, int);

/* One big region for the whole compilation. The boxed object model bump-
 * allocates aggressively (no GC; the whole arena is freed at once per compile),
 * so a self-hosted compile of a few hundred lines needs hundreds of MB. Default
 * to 1 GiB; it lives in BSS, so pages are demand-zeroed and only the touched
 * portion is ever committed. Override the order at build time, e.g.
 * -DSHIVYC_ARENA_LOG2=31 for 2 GiB. */
#ifndef SHIVYC_ARENA_LOG2
#define SHIVYC_ARENA_LOG2 30
#endif
static char   g_arena[1ull << SHIVYC_ARENA_LOG2];
static size_t g_ap = 0;

/* Manual reclaim for Python `del`. A `del x` on a heap object becomes
 * afree(x, sizeof *x): the block is pushed onto a per-size free list and reused
 * by the next aalloc of that size. This keeps the fast bump allocator while
 * letting ShivyCX hand back large intermediate structures mid-compile (no
 * refcounting, no GC). arena_reset() drops the bump pointer and every free
 * list at once. afree ignores any pointer not allocated here (string literals,
 * stack, NULL), so `del` on a borrowed value is harmless. */
#define ARENA_ALIGN   16u
#define ARENA_BUCKETS 4096u   /* sizes 16..65536 bytes are pooled for reuse */
static void* g_free[ARENA_BUCKETS];


/* Interned one-character strings. Indexing a string yields a fresh `str`, and
 * `for ch in text` does that once per character -- which on a self-hosted
 * compile was 67 million 16-byte allocations, 99% of the arena and the reason
 * it ran out. There are only 256 of them, so they are built once and shared.
 * Never freed, which is correct: they are immutable and always live. */
static char g_chars[256][2];
static int  g_chars_ready = 0;
static char* _char_str(char ch) {
    if (!g_chars_ready) {
        for (int k = 0; k < 256; k++) { g_chars[k][0] = (char)k; g_chars[k][1] = 0; }
        g_chars_ready = 1;
    }
    return g_chars[(unsigned char)ch];
}

/* Python's `%` and `//` floor; C's truncate. They differ only when exactly one
   operand is negative, and then by one divisor. */
long pymod(long a, long b) {
    if (b == 0) return 0;
    long r = a % b;
    if (r != 0 && ((r < 0) != (b < 0))) r += b;
    return r;
}
long pyfdiv(long a, long b) {
    if (b == 0) return 0;
    long q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0))) q -= 1;
    return q;
}

void* aalloc(size_t n) {
    size_t a = (n + (ARENA_ALIGN - 1u)) & ~(size_t)(ARENA_ALIGN - 1u);
    if (a == 0) a = ARENA_ALIGN;
    size_t bucket = a / ARENA_ALIGN;
    if (bucket < ARENA_BUCKETS && g_free[bucket]) {  /* reuse a del'd block */
        void* p = g_free[bucket];
        g_free[bucket] = *(void**)p;
        return p;
    }
    if (g_ap + a > sizeof g_arena) { fprintf(stderr, "arena exhausted\n"); abort(); }
    void* p = &g_arena[g_ap];
    g_ap += a;
    return p;
}

void afree(void* p, size_t n) {
    if (!p) return;
    char* cp = (char*)p;
    if (cp < g_arena || cp >= g_arena + sizeof g_arena) return;  /* not ours */
    size_t a = (n + (ARENA_ALIGN - 1u)) & ~(size_t)(ARENA_ALIGN - 1u);
    if (a == 0) a = ARENA_ALIGN;
    size_t bucket = a / ARENA_ALIGN;
    if (bucket == 0 || bucket >= ARENA_BUCKETS) return;  /* too big to pool */
    *(void**)p = g_free[bucket];
    g_free[bucket] = p;
}

void arena_reset(void) { g_ap = 0; memset(g_free, 0, sizeof g_free); }

/* Scratch scope: arena_mark() snapshots the bump pointer; arena_release(mark)
 * rewinds it, reclaiming everything bump-allocated since the mark in one O(1)
 * step. This is the classic region discipline -- it is sound ONLY when nothing
 * allocated after the mark escapes the scope, and when no afree() ran in the
 * scope (an afree would push a block above the mark onto a free list, which the
 * rewind cannot account for). The lexer uses it around match_symbol_kind_at,
 * which allocates only short-lived boxed characters for comparison and returns
 * a pre-existing token-kind, so both conditions hold. */
obj arena_mark(void) { return OBJ_INT((long)g_ap); }
void arena_release(obj mark) {
    size_t m = (size_t)AS_INT(mark);
    if (m <= g_ap) g_ap = m;
}

/* ---- exceptions: setjmp/longjmp. An uncaught raise prints the exception and
 * exits (the native compiler reports the first error and stops; the Python
 * build keeps the full error_collector). try/except pushes a frame; raise
 * longjmps to the innermost frame, or prints+exits if none is active. ------- */
#define EXC_STACK_MAX 2048
jmp_buf g_exc_jmp[EXC_STACK_MAX];
int     g_exc_sp = 0;
obj     g_exc_val = {T_NONE, {0}};
obj  rt_exc_value(void) { return g_exc_val; }
void rt_raise(obj e) {
    g_exc_val = e;
    if (g_exc_sp > 0) longjmp(g_exc_jmp[g_exc_sp - 1], 1);
    pyprint(e);
    exit(1);
}

void metadata(void) {}
void module(const char* name) { (void)name; }
void require(const char* name) { (void)name; }
void package(const char* name) { (void)name; }

/* ---- first-class function support ---- */
static const TypeInfoHdr CLOSURE_TYPE = { "function", NULL };
/* A closure with no captured environment (env is None) is fully determined by
 * its function pointer, so it is a singleton: every `make_closure(&f, None)`
 * yields an equivalent object. Referencing a bare function as a value is
 * common in hot paths (sort keys, dispatch tables, method values), so without
 * caching the compiler re-allocates hundreds of thousands of identical
 * closures per compile. Cache them, allocated with libc malloc so they live
 * outside the arena -- arena_reset and the scratch-scope arena_release must
 * never reclaim a cached object (that would dangle the cache entry). */
#define CLOSURE_CACHE_MAX 1024
static ClosureFn g_cc_fn[CLOSURE_CACHE_MAX];
static obj       g_cc_obj[CLOSURE_CACHE_MAX];
static int       g_cc_n;
obj make_closure(ClosureFn fn, obj env) {
    if (IS_NONE(env)) {
        for (int i = 0; i < g_cc_n; i++)
            if (g_cc_fn[i] == fn) return g_cc_obj[i];
        if (g_cc_n < CLOSURE_CACHE_MAX) {
            Closure* c = (Closure*)malloc(sizeof(Closure));  /* permanent singleton */
            c->_hdr.type = &CLOSURE_TYPE;
            c->fn = fn; c->env = env;
            obj r = (obj){T_FUNC, {.o = (Obj*)c}};
            g_cc_fn[g_cc_n] = fn; g_cc_obj[g_cc_n] = r; g_cc_n++;
            return r;
        }
        /* cache full (improbable): fall through to an arena allocation */
    }
    Closure* c = (Closure*)aalloc(sizeof(Closure));
    c->_hdr.type = &CLOSURE_TYPE;
    c->fn = fn; c->env = env;
    return (obj){T_FUNC, {.o = (Obj*)c}};
}
obj call_closure(obj f, obj args) {
    Closure* c = (Closure*)f.u.o;
    return c->fn(c->env, args);
}
obj identity__tramp(obj env, obj args) {
    (void)env;
    return pylen(args) ? index_obj(args, 0) : OBJ_NONE;
}
obj call_obj(obj f, int n, ...) {
    obj args = list_new();
    va_list ap; va_start(ap, n);
    for (int i = 0; i < n; i++) list_append(args, va_arg(ap, obj));
    va_end(ap);
    return call_closure(f, args);
}
obj call_obj_a(obj f, obj* a, int n) {    /* varargs-free call_obj */
    /* The caller already has the arguments in a stack array `a`. Wrap it in a
     * stack-allocated List header rather than copying into a fresh heap list:
     * a closure call is by far the most common dynamic operation, and the heap
     * arg list was pure transient churn. `args` is only read to unpack the
     * parameters (and is valid for the duration of this call), so this is safe
     * for every closure target -- EXCEPT one that stores its *args, which is
     * why the vararg trampolines copy `args` to the heap before keeping it. */
    Closure* c = (Closure*)f.u.o;
    List tmp; tmp.data = a; tmp.len = n; tmp.cap = n;
    obj args; args.tag = T_LIST; args.u.o = (Obj*)&tmp;
    return c->fn(c->env, args);
}
/* Copy a (possibly stack-backed) list into a fresh heap list. Used by vararg
 * trampolines, whose *args parameter may outlive the call. */
obj list_copy(obj src) {
    List* s = (List*)src.u.o;
    obj r = list_new();
    for (int i = 0; i < s->len; i++) list_append(r, s->data[i]);
    return r;
}


obj float_to_bits(obj val, long size) {
    /* IEEE-754 reinterpret of a float initializer to its integer bit pattern,
       for an assembler .int/.quad directive. Integer initializers (the common
       case) pass through unchanged. */
    if (val.tag != T_FLOAT) return val;
    if (size == 4) {
        float f = (float)val.u.d;
        unsigned int u;
        memcpy(&u, &f, sizeof u);
        return OBJ_INT((long)u);
    }
    double d = val.u.d;
    unsigned long u;
    memcpy(&u, &d, sizeof u);
    return OBJ_INT((long)u);
}
obj float_fromhex(str s) {
    /* float.fromhex: C99 strtod parses hex-float syntax ("0x1.8p3") directly. */
    return OBJ_FLOAT(strtod(s, NULL));
}
obj pyfloat(obj v) {
    /* float(x): parse a string, or widen an int/bool; floats pass through. */
    if (v.tag == T_STR) return OBJ_FLOAT(strtod(v.u.s, NULL));
    if (v.tag == T_FLOAT) return v;
    return OBJ_FLOAT((double)((v.tag == T_INT || v.tag == T_BOOL) ? v.u.i : 0));
}

/* CPython-compatible repr for a double.
 *
 * `%g` was used here, which is six significant digits: 3.141592653589793
 * printed as 3.14159, and 1.0000000000000002 printed as 1. For a compiler that
 * has to emit float constants into generated code, that is a value change, not
 * a formatting difference -- and it is exactly what would break a byte-identical
 * self-hosted compile.
 *
 * CPython's repr is the shortest decimal string that reads back as the same
 * double, laid out fixed or exponential according to its exponent. Both halves
 * matter: shortest-round-trip alone renders 15000000000.0 as "1.5e+10", where
 * CPython gives "15000000000.0".
 *
 * Step 1: find the smallest precision, 1 through 17, whose %e rendering parses
 *         back to the identical bit pattern. 17 always suffices for a double.
 * Step 2: if the decimal exponent is in [-4, 16), reformat as fixed with just
 *         enough places (at least one, so an integral value keeps its ".0");
 *         otherwise the %e string is already in CPython's form.
 */
str fmt_double(double d) {
    char* b;
    char tmp[64];
    int prec;
    int exp;
    int decimals;
    const char* p;

    if (d != d) return "nan";
    if (d > 1.7976931348623157e308) return "inf";
    if (d < -1.7976931348623157e308) return "-inf";

    for (prec = 1; prec < 17; prec++) {
        sprintf(tmp, "%.*e", prec - 1, d);
        if (strtod(tmp, NULL) == d) break;
    }
    sprintf(tmp, "%.*e", prec - 1, d);

    exp = 0;
    for (p = tmp; *p; p++) {
        if (*p == 'e') { exp = (int)strtol(p + 1, NULL, 10); break; }
    }

    b = aalloc(64);
    if (exp >= -4 && exp < 16) {
        decimals = prec - 1 - exp;
        if (decimals < 1) decimals = 1;
        sprintf(b, "%.*f", decimals, d);
        return b;
    }
    sprintf(b, "%s", tmp);
    return b;
}

str pystr(obj v) {
    char* b;
    switch (v.tag) {
        case T_INT:  b = aalloc(24); sprintf(b, "%ld", v.u.i); return b;
        case T_FLOAT: return fmt_double(v.u.d);
        case T_BOOL: return v.u.i ? "True" : "False";
        case T_STR:  return v.u.s ? v.u.s : "";
        case T_NONE: return "None";
        case T_SET: {
            List* l = (List*)v.u.o;
            if (l->len == 0) return "set()";
            size_t cap = 3;
            for (int i = 0; i < l->len; i++) cap += strlen(pyrepr(l->data[i])) + 2;
            b = aalloc(cap); char* p = b; *p++ = '{';
            for (int i = 0; i < l->len; i++) {
                if (i) { *p++ = ','; *p++ = ' '; }
                str s = pyrepr(l->data[i]); size_t n = strlen(s);
                memcpy(p, s, n); p += n;
            }
            *p++ = '}'; *p = 0; return b;
        }
        case T_LIST: {
            List* l = (List*)v.u.o;
            size_t cap = 3;
            for (int i = 0; i < l->len; i++) cap += strlen(pyrepr(l->data[i])) + 2;
            b = aalloc(cap); char* p = b; *p++ = '[';
            for (int i = 0; i < l->len; i++) {
                if (i) { *p++ = ','; *p++ = ' '; }
                str s = pyrepr(l->data[i]); size_t n = strlen(s);
                memcpy(p, s, n); p += n;
            }
            *p++ = ']'; *p = 0; return b;
        }
        case T_DICT: {
            Dict* d = (Dict*)v.u.o;
            size_t cap = 3;
            for (int i = 0; i < d->len; i++)
                cap += strlen(pyrepr(d->e[i].key)) + strlen(pyrepr(d->e[i].val)) + 4;
            b = aalloc(cap); char* p = b; *p++ = '{';
            for (int i = 0; i < d->len; i++) {
                if (i) { *p++ = ','; *p++ = ' '; }
                str k = pyrepr(d->e[i].key); size_t kn = strlen(k);
                memcpy(p, k, kn); p += kn; *p++ = ':'; *p++ = ' ';
                str vv = pyrepr(d->e[i].val); size_t vn = strlen(vv);
                memcpy(p, vv, vn); p += vn;
            }
            *p++ = '}'; *p = 0; return b;
        }
        default:
            if (v.u.o) {
                const TypeInfoHdr* _h = (const TypeInfoHdr*)v.u.o->type;
                if (_h && _h->tostr) {
                    obj _s = _h->tostr(v.u.o);
                    if (_s.tag == T_STR) return _s.u.s ? _s.u.s : "";
                    return pystr(_s);
                }
            }
            b = aalloc(24); sprintf(b, "<obj %p>", (void*)v.u.o); return b;
    }
}

str pyrepr(obj v) {
    if (v.tag == T_STR) {
        size_t n = v.u.s ? strlen(v.u.s) : 0;
        char* b = aalloc(n + 3);
        b[0] = '\''; if (v.u.s) memcpy(b + 1, v.u.s, n);
        b[n + 1] = '\''; b[n + 2] = 0; return b;
    }
    return pystr(v);
}

str pyfmt(int n, const char* fmt, ...) {
    (void)n;
    va_list ap; va_start(ap, fmt);
    char* out = aalloc(strlen(fmt) + 256);
    char* p = out; const char* f = fmt;
    while (*f) {
        if (f[0] == '{' && f[1] == '}') {
            obj a = va_arg(ap, obj);
            str s = pystr(a);
            size_t l = strlen(s);
            memcpy(p, s, l); p += l;
            f += 2;
        } else {
            *p++ = *f++;
        }
    }
    *p = 0;
    va_end(ap);
    return out;
}
str pyfmt_a(const char* fmt, obj* args, int n) {   /* varargs-free pyfmt; also
                                          sizes the buffer to the args */
    size_t cap = strlen(fmt) + 1;
    for (int i = 0; i < n; i++) { str s = pystr(args[i]); cap += s ? strlen(s) : 0; }
    char* out = aalloc(cap + 1);
    char* p = out; const char* f = fmt; int ai = 0;
    while (*f) {
        if (f[0] == '{' && f[1] == '}') {
            str s = (ai < n) ? pystr(args[ai++]) : ""; if (!s) s = "";
            size_t l = strlen(s); memcpy(p, s, l); p += l; f += 2;
        } else {
            *p++ = *f++;
        }
    }
    *p = 0;
    return out;
}

str pyconcat(str a, str b) {
    size_t la = a ? strlen(a) : 0, lb = b ? strlen(b) : 0;
    char* out = aalloc(la + lb + 1);
    if (a) memcpy(out, a, la);
    if (b) memcpy(out + la, b, lb);
    out[la + lb] = 0;
    return out;
}

bool in_str(str needle, const str* hay, int n) {
    for (int i = 0; i < n; i++) if (!strcmp(needle, hay[i])) return true;
    return false;
}

bool truthy(obj v) {
    switch (v.tag) {
        case T_NONE: return false;
        case T_INT:
        case T_BOOL: return v.u.i != 0;
        case T_STR:  return v.u.s && v.u.s[0];
        case T_SET:
        case T_LIST: return ((List*)v.u.o)->len != 0;
        case T_DICT: return ((Dict*)v.u.o)->len != 0;
        default:     return v.u.o != NULL;
    }
}

/* ---- lists ---- */
obj list_new(void) {
    List* l = aalloc(sizeof *l);
    l->len = 0; l->cap = 4;
    l->data = aalloc(sizeof(obj) * l->cap);
    obj r; r.tag = T_LIST; r.u.o = (Obj*)l; return r;
}
/* Return a dead list's backing array and struct to the arena free list.
   list_append already afree()s the *old* backing when it grows, so growth
   does not leak -- but a whole list that becomes garbage was never reclaimed,
   which made peak memory scale with total allocations rather than with the
   live set. Tag-checked so it is a no-op on anything that is not a list, and
   afree itself ignores pointers that did not come from the arena. */
void list_free(obj lst) {
    if (lst.tag != T_LIST || !lst.u.o) return;
    List* l = (List*)lst.u.o;
    if (l->data && l->cap) afree(l->data, sizeof(obj) * l->cap);
    l->data = NULL; l->len = 0; l->cap = 0;
    afree(l, sizeof *l);
}
void list_append(obj lst, obj v) {
    List* l = (List*)lst.u.o;
    if (l->len == l->cap) {
        int nc = l->cap * 2;
        obj* nd = aalloc(sizeof(obj) * nc);
        memcpy(nd, l->data, sizeof(obj) * l->len);
        afree(l->data, sizeof(obj) * l->cap);  /* old backing is now dead */
        l->data = nd; l->cap = nc;
    }
    l->data[l->len++] = v;
}
void list_insert(obj lst, long i, obj v) {
    List* l = (List*)lst.u.o;
    if (i < 0) i += l->len;
    if (i < 0) i = 0;
    if (i > l->len) i = l->len;
    if (l->len == l->cap) {
        int nc = l->cap ? l->cap * 2 : 4;
        obj* nd = aalloc(sizeof(obj) * nc);
        memcpy(nd, l->data, sizeof(obj) * l->len);
        if (l->cap) afree(l->data, sizeof(obj) * l->cap);  /* old backing dead */
        l->data = nd; l->cap = nc;
    }
    for (long j = l->len; j > i; j--)
        l->data[j] = l->data[j - 1];
    l->data[i] = v;
    l->len++;
}
void list_extend(obj lst, obj it) {
    long n = pylen(it);
    for (long i = 0; i < n; i++) list_append(lst, index_obj(it, i));
}
void list_assign_slice(obj dst, obj src) {
    /* dst[:] = src  -- replace all of dst's contents with src's, in place.
       Snapshot src first so the operation is safe even if src aliases dst. */
    long n = pylen(src);
    obj* tmp = (obj*)aalloc((n ? n : 1) * sizeof(obj));
    for (long i = 0; i < n; i++) tmp[i] = index_obj(src, i);
    ((List*)dst.u.o)->len = 0;
    for (long i = 0; i < n; i++) list_append(dst, tmp[i]);
}
void list_set_slice(obj dst, long lo, long hi, obj src) {
    /* dst[lo:hi] = src  -- splice src's elements in place of dst[lo:hi],
       resizing as needed. Snapshots src and the surviving tail first so the
       operation is safe even if src aliases dst. */
    List* d = (List*)dst.u.o;
    long n = d->len;
    if (lo < 0) lo += n;
    if (hi < 0) hi += n;
    if (lo < 0) lo = 0;
    if (lo > n) lo = n;
    if (hi < lo) hi = lo;
    if (hi > n) hi = n;
    long m = pylen(src), tail = n - hi;
    obj* sp = (obj*)aalloc((m ? m : 1) * sizeof(obj));
    for (long i = 0; i < m; i++) sp[i] = index_obj(src, i);
    obj* tl = (obj*)aalloc((tail ? tail : 1) * sizeof(obj));
    for (long i = 0; i < tail; i++) tl[i] = d->data[hi + i];
    d->len = lo;                              /* keep head [0:lo] */
    for (long i = 0; i < m; i++) list_append(dst, sp[i]);
    for (long i = 0; i < tail; i++) list_append(dst, tl[i]);
}
obj list_of(int n, ...) {
    obj r = list_new();
    va_list ap; va_start(ap, n);
    for (int i = 0; i < n; i++) list_append(r, va_arg(ap, obj));
    va_end(ap);
    return r;
}
obj list_from(obj* a, int n) {     /* like list_of, but no varargs (a 16-byte
                                      obj passed through `...` mis-lowers on some
                                      backends); the caller fills a stack array */
    obj r = list_new();
    for (int i = 0; i < n; i++) list_append(r, a[i]);
    return r;
}
obj set_from(obj* a, int n) {      /* set literal: list_from, minus duplicates */
    obj r = list_new();
    for (int i = 0; i < n; i++) {
        List* l = (List*)r.u.o;
        bool seen = false;
        for (int j = 0; j < l->len; j++)
            if (obj_eq(l->data[j], a[i])) { seen = true; break; }
        if (!seen) list_append(r, a[i]);
    }
    r.tag = T_SET;
    return r;
}
obj varg_list(int n, va_list ap) {
    obj r = list_new();
    for (int i = 0; i < n; i++) list_append(r, va_arg(ap, obj));
    return r;
}
obj list_get(obj lst, long i) {
    List* l = (List*)lst.u.o;
    if (i < 0) i += l->len;
    if (i < 0 || i >= l->len) return OBJ_NONE;
    return l->data[i];
}
void list_set(obj lst, long i, obj v) {
    List* l = (List*)lst.u.o;
    if (i < 0) i += l->len;
    if (i >= 0 && i < l->len) l->data[i] = v;
}
long pylen(obj v) {
    if (v.tag == T_LIST || v.tag == T_SET) return ((List*)v.u.o)->len;
    if (v.tag == T_DICT) return ((Dict*)v.u.o)->len;
    if (v.tag == T_STR)  return v.u.s ? (long)strlen(v.u.s) : 0;
    return 0;
}
obj index_obj(obj container, long i) {
    if (container.tag == T_LIST || container.tag == T_SET) return list_get(container, i);
    if (container.tag == T_DICT) {   /* iterating a dict yields its keys */
        Dict* d = (Dict*)container.u.o;
        if (i < 0) i += d->len;
        if (i < 0 || i >= d->len) return OBJ_NONE;
        return d->e[i].key;
    }
    if (container.tag == T_STR) {
        long n = (long)strlen(container.u.s);
        if (i < 0) i += n;
        if (i < 0 || i >= n) return OBJ_NONE;
        return OBJ_STR(_char_str(container.u.s[i]));
    }
    return OBJ_NONE;
}
bool obj_eq(obj a, obj b) {
    if (a.tag != b.tag) {
        if ((a.tag == T_INT && b.tag == T_BOOL) ||
            (a.tag == T_BOOL && b.tag == T_INT)) return a.u.i == b.u.i;
        return false;
    }
    switch (a.tag) {
        case T_NONE: return true;
        case T_INT:
        case T_BOOL: return a.u.i == b.u.i;
        case T_STR:  return strcmp(a.u.s, b.u.s) == 0;
        case T_LIST: {                /* ordered: same length, elementwise equal
                                         (also covers tuples, which are lists) */
            List* la = (List*)a.u.o; List* lb = (List*)b.u.o;
            if (la == lb) return true;
            if (la->len != lb->len) return false;
            for (int i = 0; i < la->len; i++)
                if (!obj_eq(la->data[i], lb->data[i])) return false;
            return true;
        }
        case T_SET: {                 /* order-independent: same elements */
            List* la = (List*)a.u.o; List* lb = (List*)b.u.o;
            if (la->len != lb->len) return false;
            for (int i = 0; i < la->len; i++) {
                bool found = false;
                for (int j = 0; j < lb->len; j++)
                    if (obj_eq(la->data[i], lb->data[j])) { found = true; break; }
                if (!found) return false;
            }
            return true;
        }
        case T_DICT: {                /* order-independent: same key->value map */
            Dict* da = (Dict*)a.u.o; Dict* db = (Dict*)b.u.o;
            if (da->len != db->len) return false;
            for (int i = 0; i < da->len; i++) {
                bool found = false;
                for (int j = 0; j < db->len; j++)
                    if (obj_eq(da->e[i].key, db->e[j].key)) {
                        if (!obj_eq(da->e[i].val, db->e[j].val)) return false;
                        found = true; break;
                    }
                if (!found) return false;
            }
            return true;
        }
        default: {
            /* Honor a user-defined __eq__ (populated into the type's `eq`
               slot); otherwise fall back to pointer identity. Without this,
               classes whose semantics rely on value equality (e.g. Spot in the
               register allocator) would compare unequal across distinct but
               equivalent instances, since dicts/sets/`in` all route through
               obj_eq. */
            if (a.u.o && b.u.o) {
                const TypeInfoHdr* _t = (const TypeInfoHdr*)((Obj*)a.u.o)->type;
                if (_t && _t->eq) return _t->eq((Obj*)a.u.o, b);
            }
            return a.u.o == b.u.o;
        }
    }
}
bool pycontains(obj container, obj v) {
    if (container.tag == T_LIST || container.tag == T_SET) {
        List* l = (List*)container.u.o;
        for (int i = 0; i < l->len; i++) if (obj_eq(l->data[i], v)) return true;
        return false;
    }
    if (container.tag == T_DICT) return dict_contains(container, v);
    if (container.tag == T_STR && v.tag == T_STR)
        return strstr(container.u.s, v.u.s) != NULL;
    return false;
}
void pyprint(obj v) { printf("%s\n", pystr(v)); }
/* print with several arguments: CPython joins them with a single space and
   ends with a newline. Array-based (like pyfmt_a) rather than varargs so the
   self-hosted front end can compile it. */
void pyprint_a(obj* a, int n) {
    for (int i = 0; i < n; i++) {
        if (i) putchar(' ');
        printf("%s", pystr(a[i]));
    }
    putchar('\n');
}

/* ---- dicts ---- */
obj dict_new(void) {
    Dict* d = aalloc(sizeof *d);
    d->len = 0; d->cap = 8; d->e = aalloc(sizeof(DEnt) * d->cap);
    obj r; r.tag = T_DICT; r.u.o = (Obj*)d; return r;
}
static int dict_find(Dict* d, obj k) {
    for (int i = 0; i < d->len; i++) if (obj_eq(d->e[i].key, k)) return i;
    return -1;
}
void dict_set(obj dd, obj k, obj v) {
    Dict* d = (Dict*)dd.u.o;
    int i = dict_find(d, k);
    if (i >= 0) { d->e[i].val = v; return; }
    if (d->len == d->cap) {
        int nc = d->cap * 2; DEnt* ne = aalloc(sizeof(DEnt) * nc);
        memcpy(ne, d->e, sizeof(DEnt) * d->len);
        afree(d->e, sizeof(DEnt) * d->cap);  /* old backing is now dead */
        d->e = ne; d->cap = nc;
    }
    d->e[d->len].key = k; d->e[d->len].val = v; d->len++;
}
obj dict_of(int n, ...) {
    obj r = dict_new();
    va_list ap; va_start(ap, n);
    for (int i = 0; i < n; i++) { obj k = va_arg(ap, obj); obj v = va_arg(ap, obj); dict_set(r, k, v); }
    va_end(ap); return r;
}
obj dict_of_a(obj* a, int n) {            /* varargs-free dict_of; a holds n
                                             key,value pairs back to back */
    obj r = dict_new();
    for (int i = 0; i < n; i++) dict_set(r, a[2 * i], a[2 * i + 1]);
    return r;
}
obj list_pair(obj a, obj b) {             /* 2-element list without varargs */
    obj r = list_new(); list_append(r, a); list_append(r, b); return r;
}
obj dict_get(obj dd, obj k, obj dflt) {
    Dict* d = (Dict*)dd.u.o; int i = dict_find(d, k);
    return i >= 0 ? d->e[i].val : dflt;
}
bool dict_contains(obj dd, obj k) { return dict_find((Dict*)dd.u.o, k) >= 0; }
obj dict_pop(obj dd, obj k, obj dflt) {
    Dict* d = (Dict*)dd.u.o; int i = dict_find(d, k);
    if (i < 0) return dflt;
    obj v = d->e[i].val;
    for (int j = i; j < d->len - 1; j++) d->e[j] = d->e[j + 1];
    d->len--; return v;
}
obj dict_setdefault(obj dd, obj k, obj dflt) {
    Dict* d = (Dict*)dd.u.o; int i = dict_find(d, k);
    if (i >= 0) return d->e[i].val;
    dict_set(dd, k, dflt); return dflt;
}
void dict_update(obj dd, obj other) {
    if (other.tag != T_DICT) return;
    Dict* o = (Dict*)other.u.o;
    for (int i = 0; i < o->len; i++) dict_set(dd, o->e[i].key, o->e[i].val);
}
/* `dict(x)`: a dict copies, an iterable of pairs builds. `dict(zip(a, b))`
   is the case that matters -- treating it as a shallow copy leaves a *list*
   of pairs behind, and every subsequent `k in d` is then false. */
obj dict_from(obj src) {
    if (src.tag == T_DICT) return pycopy(src);
    if (src.tag == T_LIST || src.tag == T_SET) {
        obj d = dict_new();
        List* l = (List*)src.u.o;
        if (!l) return d;
        for (long i = 0; i < l->len; i++) {
            obj pair = l->data[i];
            if (pair.tag != T_LIST || !pair.u.o) continue;
            List* pl = (List*)pair.u.o;
            if (pl->len < 2) continue;
            dict_set(d, pl->data[0], pl->data[1]);
        }
        return d;
    }
    return pycopy(src);
}
obj pycopy(obj c) {                 /* shallow copy: dict / list / set */
    if (c.tag == T_DICT) { obj r = dict_new(); dict_update(r, c); return r; }
    if (c.tag == T_LIST || c.tag == T_SET) {
        obj r = list_new(); List* l = (List*)c.u.o;
        for (int i = 0; i < l->len; i++) list_append(r, l->data[i]);
        r.tag = c.tag;             /* preserve set-ness */
        return r;
    }
    return c;
}
obj dict_keys(obj dd) {
    Dict* d = (Dict*)dd.u.o; obj r = list_new();
    for (int i = 0; i < d->len; i++) list_append(r, d->e[i].key);
    return r;
}
obj dict_values(obj dd) {
    Dict* d = (Dict*)dd.u.o; obj r = list_new();
    for (int i = 0; i < d->len; i++) list_append(r, d->e[i].val);
    return r;
}
obj dict_items(obj dd) {
    Dict* d = (Dict*)dd.u.o; obj r = list_new();
    for (int i = 0; i < d->len; i++)
        list_append(r, list_pair(d->e[i].key, d->e[i].val));
    return r;
}
obj subscript(obj container, obj key) {
    if (container.tag == T_DICT) return dict_get(container, key, OBJ_NONE);
    if (container.tag == T_LIST) return list_get(container, AS_INT(key));
    if (container.tag == T_STR)  return index_obj(container, AS_INT(key));
    return OBJ_NONE;
}
void subscript_set(obj container, obj key, obj v) {
    if (container.tag == T_DICT) { dict_set(container, key, v); return; }
    if (container.tag == T_LIST) { list_set(container, AS_INT(key), v); return; }
}

/* ---- arithmetic / comparison on Tier-2 values ---- */
static long as_num(obj v) { return (v.tag == T_INT || v.tag == T_BOOL) ? v.u.i : 0; }
double as_dbl(obj v) { return v.tag == T_FLOAT ? v.u.d : (double)as_num(v); }

obj obj_add(obj a, obj b) {
    /* Honor a user-defined __add__ (populated into the type's `addfn` slot).
       Without this a `Position + 1` fell through to the numeric path below,
       which reinterprets the object pointer as an integer via as_num and
       returns a T_INT that crashes when the caller casts it back. */
    if (a.tag == T_OBJ && a.u.o) {
        const TypeInfoHdr* _t = (const TypeInfoHdr*)((Obj*)a.u.o)->type;
        if (_t && _t->addfn) return _t->addfn((Obj*)a.u.o, b);
    }
    if (a.tag == T_STR && b.tag == T_STR) return OBJ_STR(pyconcat(a.u.s, b.u.s));
    if (a.tag == T_LIST && b.tag == T_LIST) {
        obj r = list_new();
        List* la = (List*)a.u.o; List* lb = (List*)b.u.o;
        for (int i = 0; i < la->len; i++) list_append(r, la->data[i]);
        for (int i = 0; i < lb->len; i++) list_append(r, lb->data[i]);
        return r;
    }
    if (a.tag == T_FLOAT || b.tag == T_FLOAT)
        return OBJ_FLOAT(as_dbl(a) + as_dbl(b));
    return OBJ_INT(as_num(a) + as_num(b));
}
obj obj_sub(obj a, obj b) {
    if (a.tag == T_SET && b.tag == T_SET) return set_diff(a, b);
    if (a.tag == T_FLOAT || b.tag == T_FLOAT)
        return OBJ_FLOAT(as_dbl(a) - as_dbl(b));
    return OBJ_INT(as_num(a) - as_num(b));
}
obj obj_mul(obj a, obj b) {
    if (a.tag == T_STR && b.tag == T_INT) {
        long n = b.u.i; size_t l = strlen(a.u.s);
        char* o = aalloc(l * (n < 0 ? 0 : n) + 1); o[0] = 0;
        for (long i = 0; i < n; i++) memcpy(o + i * l, a.u.s, l);
        o[l * (n < 0 ? 0 : n)] = 0; return OBJ_STR(o);
    }
    if ((a.tag == T_LIST && b.tag == T_INT) ||
        (b.tag == T_LIST && a.tag == T_INT)) {     /* list repeat: [x]*n / n*[x] */
        obj lst = a.tag == T_LIST ? a : b;
        long n = (a.tag == T_LIST ? b : a).u.i;
        obj r = list_new(); List* l = (List*)lst.u.o;
        for (long k = 0; k < n; k++)
            for (int i = 0; i < l->len; i++) list_append(r, l->data[i]);
        return r;
    }
    if (a.tag == T_FLOAT || b.tag == T_FLOAT)
        return OBJ_FLOAT(as_dbl(a) * as_dbl(b));
    return OBJ_INT(as_num(a) * as_num(b));
}
obj obj_fdiv(obj a, obj b) {
    if (a.tag == T_FLOAT || b.tag == T_FLOAT) {
        double d = as_dbl(b); return OBJ_FLOAT(d != 0.0 ? as_dbl(a) / d : 0.0);
    }
    long d = as_num(b); return OBJ_INT(d ? as_num(a) / d : 0); }
obj obj_mod(obj a, obj b) { long d = as_num(b); return OBJ_INT(d ? as_num(a) % d : 0); }
str str_mod(const char* fmt, obj* args, int n) {
    /* Python `fmt % args` (printf-style). args already collected into an array
       by the caller (a tuple right-hand side spreads; anything else is one
       arg), so there are no varargs and no tuple/list ambiguity. */
    size_t cap = strlen(fmt) + 1;
    int specs = 0;
    for (const char* t = fmt; *t; t++) if (*t == '%') specs++;
    cap += (size_t)specs * 512;
    for (int i = 0; i < n; i++)
        if (args[i].tag == T_STR && args[i].u.s) cap += strlen(args[i].u.s);
    char* out = aalloc(cap + 64);
    size_t len = 0;
    int ai = 0;
    const char* f = fmt;
    while (*f) {
        if (*f != '%') { out[len++] = *f++; continue; }
        f++;                                    /* past '%' */
        if (*f == '%') { out[len++] = '%'; f++; continue; }
        char spec[64]; int sl = 0; spec[sl++] = '%';
        while (*f && !strchr("diouxXeEfFgGcsr", *f) && sl < 58)
            spec[sl++] = *f++;
        char conv = *f ? *f++ : 's';
        obj a = (ai < n) ? args[ai++] : OBJ_NONE;
        if (conv == 's' || conv == 'r') {       /* plain string copy (no flags) */
            str s = pystr(a); if (!s) s = "";
            size_t l = strlen(s); memcpy(out + len, s, l); len += l;
        } else {
            char buf[512];
            if (conv == 'f' || conv == 'F' || conv == 'e' || conv == 'E' ||
                conv == 'g' || conv == 'G') {
                spec[sl++] = conv; spec[sl] = 0;
                double d = a.tag == T_FLOAT ? a.u.d : (double)as_num(a);
                snprintf(buf, sizeof buf, spec, d);
            } else {                            /* d i o u x X c -> long */
                spec[sl++] = 'l'; spec[sl++] = conv; spec[sl] = 0;
                snprintf(buf, sizeof buf, spec, as_num(a));
            }
            size_t l = strlen(buf); memcpy(out + len, buf, l); len += l;
        }
    }
    out[len] = 0;
    return out;
}
obj obj_neg(obj a) { if (a.tag == T_FLOAT) return OBJ_FLOAT(-a.u.d); return OBJ_INT(-as_num(a)); }
obj obj_invert(obj a) { return OBJ_INT(~as_num(a)); }
long ipow(long base, long exp) {
    long r = 1;
    while (exp > 0) { if (exp & 1) r *= base; base *= base; exp >>= 1; }
    return r;
}
obj obj_pow(obj a, obj b) { return OBJ_INT(ipow(as_num(a), as_num(b))); }
obj obj_bin(char op, obj a, obj b) {
    if (a.tag == T_SET && b.tag == T_SET) {      /* set algebra, not bitwise */
        if (op == '|') return set_union(a, b);
        if (op == '&') return set_inter(a, b);
        if (op == '^') return set_symdiff(a, b);
    }
    if (a.tag == T_DICT && b.tag == T_DICT && op == '|') {   /* dict merge */
        obj r = dict_new(); dict_update(r, a); dict_update(r, b); return r;
    }
    long x = as_num(a), y = as_num(b), r = 0;
    switch (op) {
        case '&': r = x & y; break; case '|': r = x | y; break;
        case '^': r = x ^ y; break; case 'l': r = x << y; break;
        case 'r': r = x >> y; break;
    }
    return OBJ_INT(r);
}
long obj_cmp(obj a, obj b) {
    if (a.tag == T_STR && b.tag == T_STR) return strcmp(a.u.s, b.u.s);
    long x = as_num(a), y = as_num(b);
    return x < y ? -1 : (x > y ? 1 : 0);
}
obj pyrange(long lo, long hi, long step) {
    obj r = list_new();
    if (step > 0) for (long i = lo; i < hi; i += step) list_append(r, OBJ_INT(i));
    else if (step < 0) for (long i = lo; i > hi; i += step) list_append(r, OBJ_INT(i));
    return r;
}

/* ---- string methods ---- */
/* `fmt % args` where `args` is a function's collected *args: hand the list's
   elements to str_mod rather than the list itself. */
str str_mod_spread(str fmt, obj args) {
    if (args.tag != T_LIST || !args.u.o) return str_mod(fmt, &args, 1);
    List* l = (List*)args.u.o;
    return str_mod(fmt, l->data, l->len);
}
bool str_startswith(str s, str p) {
    size_t ls = strlen(s), lp = strlen(p);
    return lp <= ls && memcmp(s, p, lp) == 0;
}
bool str_endswith(str s, str p) {
    size_t ls = strlen(s), lp = strlen(p);
    return lp <= ls && memcmp(s + ls - lp, p, lp) == 0;
}
/* `s.startswith(p, i)` / `s.endswith(p, i)`: test from an offset rather than
   from the head of the string. A negative index counts from the end, as in
   Python; an out-of-range one simply cannot match. */
static size_t _str_clamp(str s, long start) {
    size_t ls = strlen(s);
    if (start < 0) {
        start += (long)ls;
        if (start < 0) start = 0;
    }
    return (size_t)start > ls ? ls : (size_t)start;
}
bool str_startswith_at(str s, str p, long start) {
    size_t off = _str_clamp(s, start);
    return str_startswith(s + off, p);
}
bool str_endswith_at(str s, str p, long start) {
    size_t off = _str_clamp(s, start);
    return str_endswith(s + off, p);
}
str str_strip(str s, int mode) {
    size_t n = strlen(s); size_t a = 0, b = n;
    if (mode != 2) while (a < b && isspace((unsigned char)s[a])) a++;
    if (mode != 1) while (b > a && isspace((unsigned char)s[b - 1])) b--;
    char* o = aalloc(b - a + 1); memcpy(o, s + a, b - a); o[b - a] = 0; return o;
}
obj str_split(str s, str sep) {
    obj r = list_new();
    if (!sep || !sep[0]) {                 /* whitespace */
        size_t i = 0, n = strlen(s);
        while (i < n) {
            while (i < n && isspace((unsigned char)s[i])) i++;
            size_t j = i;
            while (j < n && !isspace((unsigned char)s[j])) j++;
            if (j > i) { char* o = aalloc(j - i + 1); memcpy(o, s + i, j - i); o[j - i] = 0; list_append(r, OBJ_STR(o)); }
            i = j;
        }
        return r;
    }
    size_t sl = strlen(sep); const char* p = s; const char* q;
    while ((q = strstr(p, sep)) != NULL) {
        size_t len = q - p; char* o = aalloc(len + 1); memcpy(o, p, len); o[len] = 0;
        list_append(r, OBJ_STR(o)); p = q + sl;
    }
    list_append(r, OBJ_STR((str)p));
    return r;
}
/* `s.rsplit(sep, maxsplit)`: split from the right, at most `maxsplit` times,
   so the *leftmost* piece keeps whatever separators are left over. A NULL or
   empty `sep` splits on runs of whitespace, as `str_split` does. `maxsplit`
   below zero means no limit. The pieces are appended left-to-right, matching
   what Python returns. */
obj str_rsplit(str s, str sep, long maxsplit) {
    obj r = list_new(), tmp = list_new();
    long n = (long)strlen(s), i, j, sl, k, cnt = 0;
    if (!sep || !sep[0]) {
        i = n;
        while (i > 0 && (maxsplit < 0 || cnt < maxsplit)) {
            while (i > 0 && isspace((unsigned char)s[i - 1])) i--;
            if (i == 0) break;
            j = i;
            while (i > 0 && !isspace((unsigned char)s[i - 1])) i--;
            { char* o = aalloc((size_t)(j - i) + 1);
              memcpy(o, s + i, (size_t)(j - i)); o[j - i] = 0;
              list_append(tmp, OBJ_STR(o)); cnt++; }
        }
        /* whatever is left, with trailing whitespace trimmed as Python does */
        while (i > 0 && isspace((unsigned char)s[i - 1])) i--;
        if (i > 0) { char* o = aalloc((size_t)i + 1);
                     memcpy(o, s, (size_t)i); o[i] = 0;
                     list_append(r, OBJ_STR(o)); }
    } else {
        sl = (long)strlen(sep);
        i = n;
        while (i >= sl && (maxsplit < 0 || cnt < maxsplit)) {
            long hit = -1;
            for (j = i - sl; j >= 0; j--)
                if (memcmp(s + j, sep, (size_t)sl) == 0) { hit = j; break; }
            if (hit < 0) break;
            { char* o = aalloc((size_t)(i - hit - sl) + 1);
              memcpy(o, s + hit + sl, (size_t)(i - hit - sl));
              o[i - hit - sl] = 0;
              list_append(tmp, OBJ_STR(o)); cnt++; }
            i = hit;
        }
        { char* o = aalloc((size_t)i + 1);
          memcpy(o, s, (size_t)i); o[i] = 0;
          list_append(r, OBJ_STR(o)); }
    }
    for (k = pylen(tmp) - 1; k >= 0; k--) list_append(r, index_obj(tmp, k));
    return r;
}
obj str_partition(str s, str sep) {
    /* str.partition: (head, sep, tail) at the first occurrence of sep, else
       (s, "", "").  Returned as a 3-element list for tuple unpacking. */
    obj r = list_new();
    const char* q = (sep && sep[0]) ? strstr(s, sep) : NULL;
    if (!q) {
        list_append(r, OBJ_STR(s));
        list_append(r, OBJ_STR(""));
        list_append(r, OBJ_STR(""));
        return r;
    }
    size_t hl = q - s;
    char* head = aalloc(hl + 1); memcpy(head, s, hl); head[hl] = 0;
    list_append(r, OBJ_STR(head));
    list_append(r, OBJ_STR(sep));
    list_append(r, OBJ_STR((str)(q + strlen(sep))));
    return r;
}
obj str_splitlines(str s) {
    obj r = list_new(); const char* p = s; const char* start = s;
    for (; *p; p++) if (*p == '\n') {
        size_t len = p - start; char* o = aalloc(len + 1); memcpy(o, start, len); o[len] = 0;
        list_append(r, OBJ_STR(o)); start = p + 1;
    }
    if (*start) { list_append(r, OBJ_STR((str)start)); }
    return r;
}
str str_replace(str s, str a, str b) {
    size_t la = strlen(a); if (!la) return s;
    size_t lb = strlen(b), ls = strlen(s); int cnt = 0;
    for (const char* p = s; (p = strstr(p, a)); p += la) cnt++;
    char* o = aalloc(ls + (lb > la ? (lb - la) : 0) * cnt + 1); char* w = o; const char* p = s; const char* q;
    while ((q = strstr(p, a))) { memcpy(w, p, q - p); w += q - p; memcpy(w, b, lb); w += lb; p = q + la; }
    strcpy(w, p); return o;
}
/* `s.count(sub[, start[, end]])`: non-overlapping occurrences in s[start:end).
 * `end < 0` means to the end of the string. An empty `sub` counts the gaps,
 * as Python does, which is len(window)+1 rather than an infinite loop. */
long str_count(str s, str sub, long start, long end) {
    long n = (long)strlen(s), ls = (long)strlen(sub), c = 0, i;
    if (start < 0) start += n;
    if (start < 0) start = 0;
    if (end < 0 || end > n) end = n;
    if (start > end) return 0;
    if (ls == 0) return end - start + 1;
    for (i = start; i + ls <= end; ) {
        if (memcmp(s + i, sub, (size_t)ls) == 0) { c++; i += ls; }
        else i++;
    }
    return c;
}
long str_find_from(str s, str sub, bool last, long start) {
    long n = (long)strlen(s);
    if (start < 0) start += n;
    if (start < 0) start = 0;
    if (start > n) return -1;
    const char* base = s + start;
    const char* hit = NULL;
    if (!last) { const char* q = strstr(base, sub); return q ? (long)(q - s) : -1; }
    for (const char* p = base; (p = strstr(p, sub)); p++) hit = p;
    return hit ? (long)(hit - s) : -1;
}
/* `s.find(sub, start, end)` / `s.rfind(...)`: search only `s[start:end]`.
   Dropping the end bound made `rfind("\n", 0, start)` -- find the start of
   the line containing `start` -- search the *whole* string, so it reported a
   newline from later in the file. Every top-level Rust item after a
   preprocessor directive then looked like it was on a `#` line and was
   skipped, and a `.cpp` include followed by inline Rust compiled as neither. */
long str_find_range(str s, str sub, bool last, long start, long end) {
    long n = (long)strlen(s);
    if (start < 0) start += n;
    if (start < 0) start = 0;
    if (end < 0) end += n;
    if (end > n) end = n;
    if (end < start) return -1;
    long span = end - start;
    char* tmp = (char*)aalloc((size_t)span + 1);
    for (long i = 0; i < span; i++) tmp[i] = s[start + i];
    tmp[span] = 0;
    long r = str_find_from(tmp, sub, last, 0);
    return r < 0 ? -1 : r + start;
}
long str_find(str s, str sub, bool last) { return str_find_from(s, sub, last, 0); }
bool str_isdigit(str s) { if (!*s) return false; for (; *s; s++) if (!isdigit((unsigned char)*s)) return false; return true; }
bool str_isalpha(str s) { if (!*s) return false; for (; *s; s++) if (!isalpha((unsigned char)*s)) return false; return true; }
bool str_isspace(str s) { if (!*s) return false; for (; *s; s++) if (!isspace((unsigned char)*s)) return false; return true; }
bool str_isalnum(str s) { if (!*s) return false; for (; *s; s++) if (!isalnum((unsigned char)*s)) return false; return true; }
str str_lower(str s) { size_t n = strlen(s); char* o = aalloc(n + 1); for (size_t i = 0; i < n; i++) o[i] = tolower((unsigned char)s[i]); o[n] = 0; return o; }
str str_upper(str s) { size_t n = strlen(s); char* o = aalloc(n + 1); for (size_t i = 0; i < n; i++) o[i] = toupper((unsigned char)s[i]); o[n] = 0; return o; }
str pyjoin(str sep, obj it) {
    long n = pylen(it); if (n <= 0) return "";
    size_t sl = strlen(sep), tot = 0;
    str* parts = aalloc(sizeof(str) * n);
    for (long i = 0; i < n; i++) { parts[i] = pystr(index_obj(it, i)); tot += strlen(parts[i]); }
    char* o = aalloc(tot + sl * (n - 1) + 1); char* w = o;
    for (long i = 0; i < n; i++) { if (i) { memcpy(w, sep, sl); w += sl; } size_t l = strlen(parts[i]); memcpy(w, parts[i], l); w += l; }
    *w = 0; return o;
}

/* ---- builtins ---- */
obj pyenumerate(obj it, long start) {
    obj r = list_new(); long n = pylen(it);
    for (long i = 0; i < n; i++) list_append(r, list_pair(OBJ_INT(start + i), index_obj(it, i)));
    return r;
}
obj pyzip(obj a, obj b) {
    obj r = list_new();
    long n = pylen(a), m = pylen(b); if (m < n) n = m;
    for (long i = 0; i < n; i++)
        list_append(r, list_pair(index_obj(a, i), index_obj(b, i)));
    return r;
}
obj pyzip3(obj a, obj b, obj c) {
    obj r = list_new();
    long n = pylen(a), m = pylen(b), k = pylen(c);
    if (m < n) n = m; if (k < n) n = k;
    for (long i = 0; i < n; i++) {
        obj t = list_new();
        list_append(t, index_obj(a, i));
        list_append(t, index_obj(b, i));
        list_append(t, index_obj(c, i));
        list_append(r, t);
    }
    return r;
}
obj pyzip4(obj a, obj b, obj c, obj d) {
    obj r = list_new();
    long n = pylen(a), m = pylen(b), k = pylen(c), j = pylen(d);
    if (m < n) n = m; if (k < n) n = k; if (j < n) n = j;
    for (long i = 0; i < n; i++) {
        obj t = list_new();
        list_append(t, index_obj(a, i));
        list_append(t, index_obj(b, i));
        list_append(t, index_obj(c, i));
        list_append(t, index_obj(d, i));
        list_append(r, t);
    }
    return r;
}
static int cmp_obj_qsort(const void* a, const void* b) {
    long c = obj_cmp(*(const obj*)a, *(const obj*)b);
    return c < 0 ? -1 : (c > 0 ? 1 : 0);
}
void list_sort(obj lst) {
    if (lst.tag != T_LIST) return;
    List* l = (List*)lst.u.o;
    qsort(l->data, l->len, sizeof(obj), cmp_obj_qsort);
}
obj pysorted(obj it) {
    obj r = pylist(it); list_sort(r); return r;
}
obj pymax(obj it, obj dflt, bool has_dflt) {
    long n = pylen(it); if (n == 0) return has_dflt ? dflt : OBJ_NONE;
    obj best = index_obj(it, 0);
    for (long i = 1; i < n; i++) { obj v = index_obj(it, i); if (obj_cmp(v, best) > 0) best = v; }
    return best;
}
obj pymin(obj it, obj dflt, bool has_dflt) {
    long n = pylen(it); if (n == 0) return has_dflt ? dflt : OBJ_NONE;
    obj best = index_obj(it, 0);
    for (long i = 1; i < n; i++) { obj v = index_obj(it, i); if (obj_cmp(v, best) < 0) best = v; }
    return best;
}
obj pysum(obj it, obj start) {
    obj acc = start; long n = pylen(it);
    for (long i = 0; i < n; i++) acc = obj_add(acc, index_obj(it, i));
    return acc;
}
obj pyreversed(obj it) {
    obj r = list_new(); long n = pylen(it);
    for (long i = n - 1; i >= 0; i--) list_append(r, index_obj(it, i));
    return r;
}
obj pylist(obj it) {
    obj r = list_new(); long n = pylen(it);
    for (long i = 0; i < n; i++) list_append(r, index_obj(it, i));
    return r;
}
obj pyset(obj it) {
    obj r = list_new(); long n = pylen(it);
    for (long i = 0; i < n; i++) { obj v = index_obj(it, i); if (!pycontains(r, v)) list_append(r, v); }
    r.tag = T_SET;
    return r;
}
void set_add(obj s, obj v) { if (!pycontains(s, v)) list_append(s, v); }
/* ---- set algebra: all return a fresh T_SET ---- */
obj set_union(obj a, obj b) {
    obj r = list_new(); r.tag = T_SET;
    long n = pylen(a);
    for (long i = 0; i < n; i++) set_add(r, index_obj(a, i));
    n = pylen(b);
    for (long i = 0; i < n; i++) set_add(r, index_obj(b, i));
    return r;
}
obj set_inter(obj a, obj b) {
    obj r = list_new(); r.tag = T_SET;
    long n = pylen(a);
    for (long i = 0; i < n; i++) {
        obj v = index_obj(a, i);
        if (pycontains(b, v) && !pycontains(r, v)) list_append(r, v);
    }
    return r;
}
obj set_diff(obj a, obj b) {
    obj r = list_new(); r.tag = T_SET;
    long n = pylen(a);
    for (long i = 0; i < n; i++) {
        obj v = index_obj(a, i);
        if (!pycontains(b, v) && !pycontains(r, v)) list_append(r, v);
    }
    return r;
}
obj set_symdiff(obj a, obj b) {     /* (a - b) U (b - a) */
    obj r = set_diff(a, b);
    long n = pylen(b);
    for (long i = 0; i < n; i++) {
        obj v = index_obj(b, i);
        if (!pycontains(a, v)) set_add(r, v);
    }
    return r;
}
void pyclear(obj c) {
    if (c.tag == T_LIST || c.tag == T_SET) ((List*)c.u.o)->len = 0;
    else if (c.tag == T_DICT) ((Dict*)c.u.o)->len = 0;
}
void list_remove(obj lst, obj v) {
    if (lst.tag != T_LIST && lst.tag != T_SET) return;
    List* l = (List*)lst.u.o;
    for (long i = 0; i < l->len; i++) {
        if (obj_eq(l->data[i], v)) {
            for (long j = i; j + 1 < l->len; j++) l->data[j] = l->data[j + 1];
            l->len--;
            return;
        }
    }
}
obj list_pop(obj lst) {
    if (lst.tag != T_LIST && lst.tag != T_SET) return OBJ_NONE;
    List* l = (List*)lst.u.o;
    if (l->len == 0) return OBJ_NONE;
    return l->data[--l->len];
}
obj list_index(obj lst, obj v) {
    if (lst.tag == T_LIST) {
        List* l = (List*)lst.u.o;
        for (long i = 0; i < l->len; i++)
            if (obj_eq(l->data[i], v)) return OBJ_INT(i);
    }
    fprintf(stderr, "list.index: value not found\n"); abort();
}
long list_count(obj lst, obj v) {  /* number of elements equal to v */
    long c = 0;
    if (lst.tag == T_LIST) {
        List* l = (List*)lst.u.o;
        for (long i = 0; i < l->len; i++) if (obj_eq(l->data[i], v)) c++;
    }
    return c;
}
obj list_reverse(obj lst) {        /* in-place reverse; returns None */
    if (lst.tag == T_LIST) {
        List* l = (List*)lst.u.o;
        for (long i = 0, j = l->len - 1; i < j; i++, j--) {
            obj t = l->data[i]; l->data[i] = l->data[j]; l->data[j] = t;
        }
    }
    return OBJ_NONE;
}
obj obj_augop(obj a, char op, obj b) {
    if (op == '+') return obj_add(a, b);
    if (op == '-') return obj_sub(a, b);   /* set diff when both sets, else num */
    /* |, &, ^ dispatch to set algebra inside obj_bin when both are sets. */
    return obj_bin(op, a, b);
}
void del_item(obj c, obj k) {
    if (c.tag == T_DICT) { dict_pop(c, k, OBJ_NONE); return; }
    if (c.tag == T_LIST) {
        List* l = (List*)c.u.o;
        long i = as_num(k);
        if (i < 0) i += l->len;
        if (i >= 0 && i < l->len) {
            for (long j = i; j + 1 < l->len; j++) l->data[j] = l->data[j + 1];
            l->len--;
        }
    }
}

str char_at(str s, long i) {
    /* Interned rather than freshly allocated. `for ch in text` lowers to this,
     * and a self-hosted compile made 67 million of these 16-byte allocations
     * -- 99% of the arena, and the reason it ran out. The result is a
     * one-character immutable string; there are only 256 of them. */
    long n = (long)strlen(s);
    if (i < 0) i += n;
    return _char_str((i >= 0 && i < n) ? s[i] : 0);
}
static long clamp_index(long i, long n) {
    if (i < 0) i += n;
    if (i < 0) i = 0;
    if (i > n) i = n;
    return i;
}
obj py_slice(obj seq, long lo, long hi) {
    if (seq.tag == T_STR) {
        const char* s = seq.u.s ? seq.u.s : "";
        long n = (long)strlen(s);
        long a = clamp_index(lo, n), b = (hi == PY_SLICE_END) ? n
                                          : clamp_index(hi, n);
        if (b < a) b = a;
        char* o = aalloc(b - a + 1);
        memcpy(o, s + a, b - a); o[b - a] = 0;
        return OBJ_STR(o);
    }
    long n = pylen(seq);
    long a = clamp_index(lo, n), b = (hi == PY_SLICE_END) ? n
                                      : clamp_index(hi, n);
    obj r = list_new();
    for (long i = a; i < b; i++) list_append(r, index_obj(seq, i));
    return r;
}
obj py_slice_step(obj seq, long lo, long hi, long step, int has_lo, int has_hi) {
    /* Full Python slice with step (incl. negative step, e.g. [::-1]). */
    if (step == 0) step = 1;
    int is_str = (seq.tag == T_STR);
    long n = is_str ? (long)strlen(seq.u.s ? seq.u.s : "") : pylen(seq);
    if (!has_lo) lo = step > 0 ? 0 : n - 1;
    else { if (lo < 0) lo += n;
           if (step > 0) { if (lo < 0) lo = 0; if (lo > n) lo = n; }
           else { if (lo < -1) lo = -1; if (lo > n - 1) lo = n - 1; } }
    if (!has_hi) hi = step > 0 ? n : -1;
    else { if (hi < 0) hi += n;
           if (step > 0) { if (hi < 0) hi = 0; if (hi > n) hi = n; }
           else { if (hi < -1) hi = -1; if (hi > n - 1) hi = n - 1; } }
    if (is_str) {
        const char* s = seq.u.s ? seq.u.s : "";
        char* o = aalloc(n + 1); long k = 0;
        for (long i = lo; step > 0 ? i < hi : i > hi; i += step) o[k++] = s[i];
        o[k] = 0; return OBJ_STR(o);
    }
    obj r = list_new();
    for (long i = lo; step > 0 ? i < hi : i > hi; i += step)
        list_append(r, index_obj(seq, i));
    return r;
}
long pyord(obj c) { return (c.tag == T_STR && c.u.s) ? (unsigned char)c.u.s[0] : (long)as_num(c); }
str pychr(long i) { char* o = aalloc(2); o[0] = (char)i; o[1] = 0; return o; }
long pyint(obj v) {
    if (v.tag == T_INT || v.tag == T_BOOL) return v.u.i;
    if (v.tag == T_FLOAT) return (long)v.u.d;
    if (v.tag == T_STR) return strtol(v.u.s, NULL, 0);
    return 0;
}
long pyabs(long x) { return x < 0 ? -x : x; }

/* Bridge-free dynamic attribute access via the per-type FieldDesc tables. */
obj rt_getattr(obj recv, const char* name, obj dflt) {
    if (recv.tag != T_OBJ || !recv.u.o) return dflt;
    const TypeInfoHdr* ti = (const TypeInfoHdr*)recv.u.o->type;
    char* p = (char*)recv.u.o;
    for (; ti; ti = ti->base) {
        const FieldDesc* f = ti->fields;
        for (; f && f->name; f++) {
            if (strcmp(f->name, name) != 0) continue;
            char* a = p + f->off;
            switch (f->tc) {
                case 'i': return OBJ_INT(*(int*)a);
                case 'l': return OBJ_INT(*(long*)a);
                case 'b': return OBJ_BOOL(*(unsigned char*)a);
                case 'd': return OBJ_FLOAT(*(double*)a);
                case 'f': return OBJ_FLOAT(*(float*)a);
                case 's': return OBJ_STR(*(str*)a);
                case 'o': return *(obj*)a;
                case 'p': { void* _pp = *(void**)a;
                            return _pp ? OBJ_OBJ(_pp) : OBJ_NONE; }
                default:  return dflt;
            }
        }
    }
    return dflt;
}

void rt_setattr(obj recv, const char* name, obj val) {
    if (recv.tag != T_OBJ || !recv.u.o) return;
    const TypeInfoHdr* ti = (const TypeInfoHdr*)recv.u.o->type;
    char* p = (char*)recv.u.o;
    for (; ti; ti = ti->base) {
        const FieldDesc* f = ti->fields;
        for (; f && f->name; f++) {
            if (strcmp(f->name, name) != 0) continue;
            char* a = p + f->off;
            switch (f->tc) {
                case 'i': *(int*)a = (int)AS_INT(val); return;
                case 'l': *(long*)a = AS_INT(val); return;
                case 'b': *(unsigned char*)a = (unsigned char)(AS_INT(val) != 0); return;
                case 'd': *(double*)a = AS_FLOAT(val); return;
                case 'f': *(float*)a = (float)AS_FLOAT(val); return;
                case 's': *(str*)a = AS_STR(val); return;
                case 'o': *(obj*)a = val; return;
                case 'p': *(void**)a = AS_OBJ(val); return;
                default:  return;
            }
        }
    }
}
long py_int_base(str s, long base) {
    const char* p = s ? s : "";
    while (isspace((unsigned char)*p)) p++;
    int neg = 0;
    if (*p == '+' || *p == '-') { neg = (*p == '-'); p++; }
    if (base == 16 && p[0] == '0' && (p[1] == 'x' || p[1] == 'X')) p += 2;
    else if (base == 2 && p[0] == '0' && (p[1] == 'b' || p[1] == 'B')) p += 2;
    else if (base == 8 && p[0] == '0' && (p[1] == 'o' || p[1] == 'O')) p += 2;
    long v = strtol(p, NULL, (int)(base == 0 ? 0 : base));
    return neg ? -v : v;
}
'''


# ==========================================================================
# Type inference from naming conventions
# ==========================================================================

INT_NAMES = {
    "index", "idx", "i", "j", "k", "n", "n1", "n2", "count", "size",
    "offset", "chunk", "num", "length", "len", "pos", "position", "line",
    "lineno", "col", "column", "start", "end", "depth", "level", "width",
    "height", "amount", "total", "addr", "address", "byte", "bytes", "bits",
    "rbp_offset", "spot_size",
    # rpython numeric-loop conventions
    "limit", "terms", "iters", "iterations", "steps", "seed", "rows", "cols",
    "rank", "dim", "dims", "stride", "nrows", "ncols", "iter", "reps",
}
INT_SUFFIXES = ("size", "offset", "count", "index", "len", "num", "idx")

# Integer C types, narrowest first: a conditional with two integer branches
# unifies to the widest of them. Shared by `ex_IfExp` (which emits the cast)
# and `value_ctype` (which reports the result), because those two disagreeing
# is exactly what leaves a raw scalar where an obj is expected.
_IFEXP_INTS = ("bool", "char", "short", "int", "long")

# Float-by-name: numeric scalars that are conventionally real-valued. Kept
# narrow and domain-flavoured (stats / signals / geometry) so it does not
# collide with the integer-heavy names above.
FLOAT_NAMES = {
    "ratio", "rate", "pct", "percent", "scale", "factor", "mean", "avg",
    "average", "alpha", "beta", "gamma", "theta", "freq", "frequency",
    "prob", "probability", "weight", "epsilon", "eps", "tolerance", "tol",
    "magnitude", "amplitude", "phase", "angle", "radius",
}
FLOAT_SUFFIXES = ("ratio", "rate", "pct", "scale", "factor", "freq", "prob")

STR_NAMES = {
    "name", "text", "s", "string", "msg", "message", "filename", "fname",
    "func_name", "tag", "rep", "content", "spelling", "label", "identifier",
    "prog", "code", "asm_str", "text_repr", "mangled", "suffix",
    "prefix",
}
STR_SUFFIXES = ("str",)

BOOL_NAMES = {
    "defined", "ok", "found", "done", "wide", "signed", "unsigned", "const",
    "volatile", "valid", "empty", "present", "enabled", "success",
}
BOOL_PREFIXES = ("is_", "has_", "can_", "should_", "was_", "use_", "allow_")

OBJ = "obj"

# C scalar types whose pointer form is a real array (native indexing), as
# opposed to a class pointer (Foo*) or the boxed `obj`.
_SCALAR_CTYPES = {"int", "double", "float", "char", "long", "short",
                  "bool", "unsigned", "unsigned char"}

# ctypes scalar markers -> C types, for the FFI bridge (see rpy_ctypes.py).
_CTYPES_TYPEMAP = {
    "c_void": "void", "c_bool": "bool", "c_char": "char",
    "c_byte": "signed char", "c_ubyte": "unsigned char",
    "c_short": "short", "c_ushort": "unsigned short",
    "c_int": "int", "c_uint": "unsigned",
    "c_long": "long", "c_ulong": "unsigned long",
    "c_size_t": "long", "c_ssize_t": "long",
    "c_float": "float", "c_double": "double",
    "c_char_p": "char*", "c_void_p": "void*",
}


def _tlist_name(et):
    """C struct name for a typed list of element ctype `et` (a scalar)."""
    return "_tlist_" + et.replace(" ", "_").replace("*", "p")


def _mangle_ct(ct):
    return ct.replace(" ", "_").replace("*", "p")


def ann_dict_kv(ann):
    """(key_ctype, val_ctype) for a `dict[K, V]`/`Dict[K, V]` annotation, or
    None if `ann` is not a typed-dict annotation."""
    try:
        text = ast.unparse(ann).strip().strip("'\"")
    except Exception:
        return None
    for pfx in ("dict[", "Dict["):
        if text.startswith(pfx) and text.endswith("]"):
            inner = text[len(pfx):-1]
            depth = 0
            for i, c in enumerate(inner):
                if c == "[":
                    depth += 1
                elif c == "]":
                    depth -= 1
                elif c == "," and depth == 0:
                    k = ann_text_to_ctype(inner[:i].strip())
                    v = ann_text_to_ctype(inner[i + 1:].strip())
                    if k and v:
                        return (k, v)
                    return None
    return None


def _tdict_name(kct, vct):
    return "_tdict_%s_%s" % (_mangle_ct(kct), _mangle_ct(vct))


def _tdict_prelude(kct, vct):
    """A small, runtime-free dict for scalar/string keys and scalar values:
    parallel key/value arrays with linear probe. String keys compare by
    strcmp; everything else by ==. O(n) lookup, fine for the small dicts
    rpython programs use; no tagged obj, no hashing runtime."""
    td = _tdict_name(kct, vct)
    keyeq = ("(strcmp(d->keys[i], k) == 0)" if kct == "char*"
             else "(d->keys[i] == k)")
    return (
        "typedef struct %s { %s* keys; %s* vals; long len; long cap; } %s;\n"
        "static %s* %s_new(long cap) {\n"
        "    %s* d = malloc(sizeof *d);\n"
        "    d->len = 0; d->cap = cap > 0 ? cap : 4;\n"
        "    d->keys = malloc((unsigned long)d->cap * sizeof(%s));\n"
        "    d->vals = malloc((unsigned long)d->cap * sizeof(%s));\n"
        "    return d;\n"
        "}\n"
        "static long %s_find(%s* d, %s k) {\n"
        "    for (long i = 0; i < d->len; i++) if %s return i;\n"
        "    return -1;\n"
        "}\n"
        "static void %s_set(%s* d, %s k, %s v) {\n"
        "    long i = %s_find(d, k);\n"
        "    if (i >= 0) { d->vals[i] = v; return; }\n"
        "    if (d->len >= d->cap) {\n"
        "        d->cap = d->cap * 2;\n"
        "        d->keys = realloc(d->keys, (unsigned long)d->cap * sizeof(%s));\n"
        "        d->vals = realloc(d->vals, (unsigned long)d->cap * sizeof(%s));\n"
        "    }\n"
        "    d->keys[d->len] = k; d->vals[d->len] = v; d->len++;\n"
        "}\n"
        "static %s %s_get(%s* d, %s k) {\n"
        "    long i = %s_find(d, k);\n"
        "    return i >= 0 ? d->vals[i] : (%s)0;\n"
        "}\n"
        "static int %s_has(%s* d, %s k) { return %s_find(d, k) >= 0; }\n"
        % (td, kct, vct, td,
           td, td, td, kct, vct,
           td, td, kct, keyeq,
           td, td, kct, vct, td, kct, vct,
           vct, td, td, kct, td, vct,
           td, td, kct, td))


def _tlist_prelude(et):
    """A small, runtime-free growable-array type for a scalar element `et`:
    `{ et* data; long len, cap; }` with new/push helpers (get/len are inline
    field accesses). Used to lower rpython `list[T]` for scalar T."""
    tl = _tlist_name(et)
    return (
        "typedef struct %s { %s* data; long len; long cap; } %s;\n"
        "static %s* %s_new(long cap) {\n"
        "    %s* l = malloc(sizeof *l);\n"
        "    l->len = 0; l->cap = cap > 0 ? cap : 4;\n"
        "    l->data = malloc((unsigned long)l->cap * sizeof(%s));\n"
        "    return l;\n"
        "}\n"
        "static void %s_push(%s* l, %s v) {\n"
        "    if (l->len >= l->cap) {\n"
        "        l->cap = l->cap ? l->cap * 2 : 4;\n"
        "        l->data = realloc(l->data, (unsigned long)l->cap * sizeof(%s));\n"
        "    }\n"
        "    l->data[l->len++] = v;\n"
        "}\n"
        "static %s %s_pop(%s* l) {\n"
        "    return l->data[--l->len];\n"
        "}\n"
        # Counterpart to _new. A typed list is libc-malloc'd, not arena-
        # allocated, so `del` on one must actually free it -- without this the
        # prelude could only ever grow a program's memory, and a caller that
        # built a typed list per iteration (a collector's mark vector, say)
        # leaked it every time while the arena itself stayed flat.
        "static void %s_free(%s* l) {\n"
        "    if (!l) return;\n"
        "    free(l->data); l->data = 0; l->len = 0; l->cap = 0;\n"
        "    free(l);\n"
        "}\n"
        % (tl, et, tl, tl, tl, tl, et, tl, tl, et, et, et, tl, tl, tl, tl))


KNOWN_CLASSES = {}      # name -> ClassInfo
VTABLE_METHODS = set()  # method names that are virtual somewhere in the module
EMIT_DECLS = False       # --decls: also write <module>.decls.json
_XMOD_CACHE = {}        # dotted module name -> imported-symbol registry

# Directories to search when resolving a co-compiled local module by its bare
# (non-package) name, e.g. `import helper` / `from helper import f` where
# `helper.py` is one of the sources given on the command line. Populated by the
# CLI / multi-file driver from the input files' directories so that several
# .py files compiled together form one translation unit with direct calls
# between them (instead of dynamic mp_call_import).
_LOCAL_MODULE_DIRS = []

# Module names (both the shivyc-package dotted form and the bare basename) of the
# sources in the CURRENT compilation unit -- i.e. the files handed to py2c in one
# invocation, which are the modules whose object files will actually be linked
# together. Used to decide whether an ambiguous-method definer can be emitted as
# a runtime type branch: referencing the TypeInfo of a class from a module that
# is NOT in this unit (e.g. rasm_obj, imported only by the stubbed SHIVYC_RASM
# path) would be an undefined-symbol link error, so such definers are skipped.
_COMPILED_MODULES = set()


_BUILD_FILES = []


def set_build_files(paths):
    """Remember the .py files being transpiled together.

    `ambiguous_function_names` only walks `shivyc/`, which is right for the
    compiler's own package and blind to everything else: two *other* modules
    compiled into one program -- `tools/cpprust.py` and the
    `tools/cpp_auto.py` it imports -- each defined `_match` and `_split_top`,
    and the link failed on the duplicate symbols. Names defined in more than
    one file of the actual build are ambiguous too, whatever directory they
    sit in.
    """
    global _BUILD_FILES
    _BUILD_FILES = [p for p in paths if p.endswith(".py")]


def ambiguous_function_names_in_build():
    """Top-level function names defined by more than one file of this build."""
    seen, dup = {}, set()
    for p in _BUILD_FILES:
        try:
            t = ast.parse(open(p, encoding="utf-8").read())
        except Exception:
            continue
        mod = os.path.basename(p)[:-3]
        for n in t.body:
            if isinstance(n, ast.FunctionDef):
                if seen.get(n.name, mod) != mod:
                    dup.add(n.name)
                seen.setdefault(n.name, mod)
    return dup


def set_compiled_modules(paths):
    """Record the module names of the compilation unit's input files."""
    global _COMPILED_MODULES
    mods = set()
    for p in paths:
        ap = os.path.abspath(p)
        if not ap.endswith(".py"):
            continue
        parts = ap[:-3].split(os.sep)
        mods.add(parts[-1])                      # bare basename form
        if "shivyc" in parts:                    # dotted package form
            mods.add(".".join(parts[parts.index("shivyc"):]))
    _COMPILED_MODULES = mods


def set_local_module_dirs(paths):
    """Register the directories of the input sources for local-module lookup."""
    global _LOCAL_MODULE_DIRS
    seen = []
    for p in paths:
        d = os.path.dirname(os.path.abspath(p)) if not os.path.isdir(p) \
            else os.path.abspath(p)
        if d and d not in seen:
            seen.append(d)
    _LOCAL_MODULE_DIRS = seen


def ann_text_to_ctype(text):
    text = text.strip().strip("'\"")
    # bit-field annotation `T(N)` (e.g. `int(3)`): a value of scalar type T that
    # is stored packed into N bits. The value/expression type is simply T; the
    # width N is recorded per-class by discover_bitfields and consumed only at
    # struct emission, so reads and writes of the field behave as ordinary T.
    mbf = re.match(r"^(\w[\w ]*?)\s*\(\s*\d+\s*\)$", text)
    if mbf is not None:
        return ann_text_to_ctype(mbf.group(1))
    simple = {"int": "int", "bool": "bool", "None": "void", "str": "char*",
              "float": "double", "bytes": "char*"}
    # numpy-style dtype aliases -> real C scalar types. f32 is a true 32-bit
    # float (single precision), so f32 kernels vectorize to mulps/addps.
    dtypes = {"f32": "float", "f64": "double", "i8": "char", "i16": "short",
              "i32": "int", "i64": "long", "u8": "unsigned char",
              "u32": "unsigned"}
    if text in simple:
        return simple[text]
    if text in dtypes:
        return dtypes[text]
    head = text.split("[", 1)[0].strip()
    if head in ("List", "list", "Dict", "dict", "Set", "set", "Tuple",
                "tuple", "Optional", "Any", "object"):
        return OBJ

    def _scalar(base):
        base = base.strip()
        if base in dtypes:
            return dtypes[base]
        if base in simple and simple[base] != "void":
            return simple[base]
        if base in ("char", "long", "short", "unsigned", "signed",
                    "int", "float", "double"):
            return base
        return None

    # a bare C scalar name (`long`, `double`, `short`, `unsigned`, ...) used
    # directly as an annotation -- not just inside an array/pointer form. The
    # top-level `simple`/`dtypes` maps cover int/float/bool/str; route the rest
    # through `_scalar` so e.g. `age: "long"` / `x: "double"` get a real C type
    # instead of falling through to a boxed obj.
    if "[" not in text and not text.endswith("*"):
        s = _scalar(text)
        if s:
            return s

    # fixed-size array  T[N] / f32[N] -> pointer to element (count via
    # ann_array_size, used to infer SIMD-divisibility contracts).
    m = re.match(r"^([A-Za-z_][\w ]*?)\s*\[\s*\d+\s*\]$", text)
    if m:
        s = _scalar(m.group(1))
        if s:
            return s + "*"
    # pointer-to-scalar arrays, e.g. "int*", "float*", "f32*", "double*" -- the
    # rpython way to take a real C array (numpy-style) rather than a boxed list,
    # so `a[i]` lowers to native indexing and the loop can be vectorized.
    if text.endswith("*"):
        s = _scalar(text[:-1])
        if s:
            return s + "*"
        if text[:-1].strip() in ("void",):
            return "void*"
    if text and (text[0].isupper() or text[0] == "_") and text.isidentifier():
        return text + "*"
    return None


def ann_array_size(ann):
    """Element count N of a fixed-size array annotation `T[N]` / `f32[N]`, else
    None. Lets py2c infer SIMD-divisibility contracts with no user assert."""
    if ann is None:
        return None
    try:
        text = ann.strip().strip("'\"") if isinstance(ann, str) \
            else ast.unparse(ann).strip().strip("'\"")
    except Exception:
        return None
    m = re.match(r"^[A-Za-z_][\w ]*?\s*\[\s*(\d+)\s*\]$", text)
    return int(m.group(1)) if m else None


def ann_elem_ctype(ann):
    """Element C type of a `List[T]`/`list[T]` annotation, e.g. List[ILValue]
    -> 'ILValue*'. None if `ann` is not a typed-list annotation. Used to give a
    concrete type to the loop variable of `for x in <annotated list>` (and to
    subscripts of it) so member access on the elements resolves."""
    if ann is None:
        return None
    try:
        text = ast.unparse(ann).strip().strip("'\"")
    except Exception:
        return None
    for pfx in ("List[", "list["):
        if text.startswith(pfx) and text.endswith("]"):
            return ann_text_to_ctype(text[len(pfx):-1])
    return None


def ann_to_ctype(ann):
    if ann is None:
        return None
    try:
        return ann_text_to_ctype(ast.unparse(ann))
    except Exception:
        return None


def infer_from_name(name):
    if name == "self":
        return None
    low = name.lower()
    if low in INT_NAMES:
        return "int"
    if low in STR_NAMES:
        return "char*"
    if low in BOOL_NAMES:
        return "bool"
    if low in FLOAT_NAMES:
        return "double"
    if low.startswith(BOOL_PREFIXES):
        return "bool"
    tail = low.rsplit("_", 1)[-1]
    if tail in INT_SUFFIXES and not name.startswith("_"):
        return "int"
    if tail in FLOAT_SUFFIXES and not name.startswith("_"):
        return "double"
    if tail in STR_SUFFIXES:
        return "char*"
    return None


def infer_type(name, ann=None):
    return ann_to_ctype(ann) or infer_from_name(name) or OBJ


# Signed C integer types, narrowest to widest. When a local is assigned values
# of two different integer types (e.g. `pc` gets both an `int` literal and a
# `long` from `v.iv`), the wider type holds both -- so the local can stay an
# unboxed integer instead of falling back to a boxed `obj`.
_SIGNED_INT_RANK = {"bool": 0, "char": 1, "short": 2, "int": 3, "long": 4}


def _int_widen(a, b):
    """The integer type wide enough to hold both `a` and `b`, or None if either
    is not a signed integer type. Used to reconcile a local's inferred type
    across assignments without boxing."""
    if a in _SIGNED_INT_RANK and b in _SIGNED_INT_RANK:
        return a if _SIGNED_INT_RANK[a] >= _SIGNED_INT_RANK[b] else b
    return None


def _const_numeric_ctype(node):
    """ctype ("int"/"double") of a numeric literal class-constant value, or
    None. bool excluded (bool is an int subclass); str/None/containers return
    None and keep their existing inference. A leading unary +/- is allowed."""
    n = node
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)) \
            and isinstance(n.operand, ast.Constant):
        n = n.operand
    if isinstance(n, ast.Constant):
        v = n.value
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return "int"
        if isinstance(v, float):
            return "double"
    return None


def _class_attr_is_mutated(classnode, attr):
    """True if `attr` is ever an assignment target (`x.attr = ...` /
    `x.attr += ...`) inside `classnode`. A mutated class attribute is a real
    storage slot (e.g. a `_counter += 1` class counter), not an inlinable
    constant, so its field must not be narrowed to a literal scalar type."""
    for sub in ast.walk(classnode):
        tgts = []
        if isinstance(sub, ast.Assign):
            tgts = sub.targets
        elif isinstance(sub, (ast.AugAssign, ast.AnnAssign)):
            tgts = [sub.target]
        for t in tgts:
            for tt in (t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t]):
                if isinstance(tt, ast.Attribute) and tt.attr == attr:
                    return True
    return False


def _attr_mutated_via_classname(classnode, attr):
    """True if `attr` is assigned through the class's OWN name inside the class
    (e.g. `ASMCode.label_num += 1` inside class ASMCode). Such an attribute is a
    shared class-level counter that needs real global storage: treating it as a
    per-instance scalar default would inline its initial literal at every read
    and turn the mutation into a no-op (`OBJ_INT(0) = OBJ_INT(0)+1`)."""
    cls_name = getattr(classnode, "name", None)
    if not cls_name:
        return False
    for sub in ast.walk(classnode):
        tgts = []
        if isinstance(sub, ast.Assign):
            tgts = sub.targets
        elif isinstance(sub, (ast.AugAssign, ast.AnnAssign)):
            tgts = [sub.target]
        for t in tgts:
            if isinstance(t, ast.Attribute) and t.attr == attr \
                    and isinstance(t.value, ast.Name) and t.value.id == cls_name:
                return True
    return False


def optional_param_names(fn):
    """Params with a literal `None` default and no annotation are Optional,
    hence must be boxed as `obj` (None has to be representable)."""
    names = set()
    defaults = fn.args.defaults
    if defaults:
        for arg, default in zip(fn.args.args[-len(defaults):], defaults):
            if arg.annotation is None and isinstance(default, ast.Constant) \
                    and default.value is None:
                names.add(arg.arg)
    return names


def _param_used_as_container(fn, name):
    """True if `name` is iterated, membership-tested, subscripted, or mutated as
    a collection inside `fn` -- strong evidence it isn't the scalar its name
    suggests (e.g. a set parameter called `defined`)."""
    for n in ast.walk(fn):
        if isinstance(n, ast.For) and isinstance(n.iter, ast.Name) \
                and n.iter.id == name:
            return True
        if isinstance(n, ast.Compare):
            for op, cmp in zip(n.ops, n.comparators):
                if isinstance(op, (ast.In, ast.NotIn)) and \
                        isinstance(cmp, ast.Name) and cmp.id == name:
                    return True
        if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) \
                and n.value.id == name:
            return True
        if isinstance(n, ast.comprehension) and isinstance(n.iter, ast.Name) \
                and n.iter.id == name:
            return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and isinstance(n.func.value, ast.Name) \
                and n.func.value.id == name and n.func.attr in (
                    "append", "add", "update", "extend", "pop", "remove",
                    "keys", "values", "items", "get", "discard"):
            return True
    return False


def _param_used_as_object(fn, name):
    """True if `name` is accessed via an attribute that isn't a string or
    container method (e.g. `identifier.content`) -- evidence it's an object,
    not the scalar string/int its name suggests."""
    KNOWN = {"strip", "lstrip", "rstrip", "upper", "lower", "replace", "split",
             "partition",
             "splitlines", "rsplit", "startswith", "endswith", "isdigit",
             "isalpha",
             "isspace", "isalnum", "find", "rfind", "rindex", "index",
             "join", "encode", "decode",
             "format", "count", "index", "append", "add", "update", "extend",
             "pop", "remove", "keys", "values", "items", "get", "discard",
             "sort", "copy", "setdefault"}
    for n in ast.walk(fn):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                and n.value.id == name and n.attr not in KNOWN:
            return True
    return False


def _param_used_in_isinstance(fn, name):
    """True if `name` is the subject of isinstance(...)."""
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and \
                n.func.id == "isinstance" and n.args and \
                isinstance(n.args[0], ast.Name) and n.args[0].id == name:
            return True
    return False


def _param_assigned_from_call(fn, name, call_names):
    """True if `name` is assigned from a call like input()."""
    for n in ast.walk(fn):
        if not isinstance(n, ast.Assign):
            continue
        for t in n.targets:
            if isinstance(t, ast.Name) and t.id == name and \
                    isinstance(n.value, ast.Call):
                f = n.value.func
                if isinstance(f, ast.Name) and f.id in call_names:
                    return True
                if isinstance(f, ast.Attribute) and f.attr in call_names:
                    return True
    return False


def _param_used_in_str_compare(fn, name):
    """True if `name` is equality-compared (==/!=) against a string literal --
    a strong hint it is a str. Ordering comparisons (<, <=, >, >=) are numeric
    and must NOT trigger this, or a plain `i < n` would wrongly force `n` to
    obj and defeat the int-by-name inference."""
    for n in ast.walk(fn):
        if not isinstance(n, ast.Compare):
            continue
        if not all(isinstance(op, (ast.Eq, ast.NotEq)) for op in n.ops):
            continue
        sides = [n.left] + list(n.comparators)
        if not any(isinstance(s, ast.Name) and s.id == name for s in sides):
            continue
        if any(isinstance(s, ast.Constant) and isinstance(s.value, str)
               for s in sides):
            return True
    return False


def arg_ctype(fn, arg):
    if arg.annotation is not None:
        return ann_to_ctype(arg.annotation) or OBJ
    if arg.arg in optional_param_names(fn):
        return OBJ
    if _param_used_in_isinstance(fn, arg.arg):
        return OBJ
    if _param_assigned_from_call(fn, arg.arg, {"input", "raw_input"}):
        return OBJ
    if _param_used_in_str_compare(fn, arg.arg):
        return OBJ
    guess = infer_from_name(arg.arg)
    if guess in ("bool", "int") and _param_used_as_container(fn, arg.arg):
        return OBJ                  # usage contradicts the scalar name guess
    if guess in ("bool", "int", "char*") and _param_used_as_object(fn, arg.arg):
        return OBJ                  # attribute access -> it's an object
    return guess or OBJ


# ==========================================================================
# Class model
# ==========================================================================

def _contains_call(node):
    """True if evaluating `node` could have a side effect.

    Used to decide whether a short-circuit operand is safe to name twice in
    the generated ternary. Calls are the case that matters; a name, attribute
    or comparison can be repeated harmlessly.
    """
    for n in ast.walk(node):
        if isinstance(n, (ast.Call, ast.Await, ast.Yield, ast.NamedExpr)):
            return True
    return False


def _truth_of(var, ctype):
    """A truth test for an already-evaluated temporary of type `ctype`."""
    if ctype == OBJ:
        return "truthy(%s)" % var
    if ctype == "char*":
        # Python string truthiness: empty is false, not merely non-NULL.
        return "(%s && *%s)" % (var, var)
    return "(%s)" % var


class Py2CUnsupported(Exception):
    """A construct py2c cannot lower correctly.

    Raised rather than warned. The alternative -- emitting the closest
    approximation and carrying on -- is how `s.startswith(p, i)` came to
    compile into a test against position zero: correct-looking C that
    silently answered the wrong question, and only in self-hosted builds.
    """


class ClassInfo:
    def __init__(self, node):
        self.node = node
        self.name = node.name
        self.csym = node.name       # C base symbol (module-qualified if the
                                    # class name collides across modules)
        self.base = None
        self.base_name = None
        self.methods = {}
        self.static_methods = set()  # names decorated @staticmethod (no self)
        self.classmethod_methods = set()  # @classmethod (cls as first arg)
        self.property_methods = set()  # @property: direct call, not vtable slot
        self.own_fields = []
        self.bitfields = {}         # fieldname -> bit width N, from a `T(N)`
                                    # annotation; emitted as a C bit field
        self.const_dicts = {}
        self.union_fields = []      # fields that share storage (anonymous C
                                    # union); declared via `_c_union_ = (...)`
        self.class_statics = {}     # class-level obj constants (lists / dicts
                                    # the const_dict fast-path can't specialize)
        self.class_attrs = {}       # class-level scalar defaults (instance flds)
        # Accept both `class X(Base)` and `class X(mod.Base)` (an imported
        # base referenced through its module alias): the bare base name is what
        # the hierarchy and POD logic key on.
        self.base_attr = None       # module alias when base is `mod.Base`
        # Secondary (multiple-inheritance) bases. The first base is the layout
        # base (its fields form the struct prefix); any further bases are
        # recorded here so their methods can still resolve to direct calls.
        # This is sound for stateless mixin bases (whose methods don't read
        # instance fields through the secondary-base layout), which is the MI
        # pattern used in the tree node hierarchy.
        self.extra_base_names = []
        self.extra_bases = []
        for b in node.bases:
            if isinstance(b, ast.Name):
                bn, battr = b.id, None
            elif isinstance(b, ast.Attribute):
                bn = b.attr
                battr = b.value.id if isinstance(b.value, ast.Name) else None
            else:
                continue
            if self.base_name is None:
                self.base_name = bn
                self.base_attr = battr
            else:
                self.extra_base_names.append(bn)

    def root(self):
        c = self
        while c.base:
            c = c.base
        return c

    def full_fields(self):
        out = []
        if self.base:
            out.extend(self.base.full_fields())
        seen = {f[0] for f in out}
        for f in self.own_fields:
            if f[0] not in seen:
                out.append(f)
                seen.add(f[0])
        return out

    def field_ctype(self, cn):
        for n, t in self.full_fields():
            if n == cn:
                return t
        return None

    def find_method_owner(self, mname):
        c = self
        while c:
            if mname in c.methods:
                return c
            c = c.base
        return None

    def find_method_owner_mi(self, mname):
        """Like find_method_owner, but also searches secondary (MI) bases.

        Walks the primary base chain first; if the method is not found, it
        searches each class on that chain's secondary bases (and their own
        primary chains). Used to resolve a method inherited from a non-primary
        base to a direct call instead of a broken closure-field call.
        """
        owner = self.find_method_owner(mname)
        if owner:
            return owner
        c = self
        while c:
            for b in c.extra_bases:
                o = b.find_method_owner(mname)
                if o:
                    return o
            c = c.base
        return None


def _const_value(node):
    """Python constant value of `node` (int/str/bool, incl. negatives), else
    None."""
    if isinstance(node, ast.Constant) and isinstance(node.value,
                                                     (int, str, bool, bytes)):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) \
            and isinstance(node.operand, ast.Constant) \
            and isinstance(node.operand.value, (int, float)):
        return -node.operand.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id == "const" and len(node.args) == 1:
        return _const_value(node.args[0])
    return None


def _assigned_names(fn):
    """Names truly *bound* (rebound) anywhere in fn. A subscript/attribute
    target (a[i]=..., a.x=...) mutates an existing object rather than binding a
    new local, so the base name is NOT counted -- it stays a free variable."""
    out = set()

    def bind_target(t):
        if isinstance(t, ast.Name):
            out.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                bind_target(e)
        elif isinstance(t, ast.Starred):
            bind_target(t.value)
        # Subscript / Attribute targets intentionally ignored (mutation)

    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                bind_target(t)
        elif isinstance(n, ast.AnnAssign):
            bind_target(n.target)
        elif isinstance(n, (ast.AugAssign,)):
            if isinstance(n.target, ast.Name):
                out.add(n.target.id)
        elif isinstance(n, ast.For):
            bind_target(n.target)
        elif isinstance(n, ast.comprehension):
            bind_target(n.target)
        elif isinstance(n, ast.withitem) and n.optional_vars is not None:
            bind_target(n.optional_vars)
    return out


def _cellify_nonlocals(fn):
    """Box variables that nested functions rebind via `nonlocal` into
    one-element list cells, so the ordinary lift can handle them.

    Captures are passed by value, so a nested `nonlocal x; x += 1` could not be
    lowered at all -- the def was dropped and every call to it became an
    undefined reference. Rewriting `x` to `x__cell[0]` throughout (and the
    binding site to `x__cell = [init]`) turns the rebind into a mutation of a
    shared list, which already has reference semantics when captured. After
    this runs no `nonlocal` remains, so the normal lifting path applies.

    Only names with an unambiguous binding site in `fn` (a parameter, or a
    top-level `x = ...`) are converted; anything else is left alone so the
    caller still reports it as unsupported.
    """
    declared = set()
    for sub in fn.body:
        if isinstance(sub, ast.FunctionDef):
            for n in ast.walk(sub):
                if isinstance(n, ast.Nonlocal):
                    declared.update(n.names)
    if not declared:
        return set()

    params = {a.arg for a in fn.args.args}
    converted = set()
    for x in sorted(declared):
        cell = x + "__cell"
        init_idx = None
        if x not in params:
            for i, st in enumerate(fn.body):
                if isinstance(st, ast.Assign) and len(st.targets) == 1 \
                        and isinstance(st.targets[0], ast.Name) \
                        and st.targets[0].id == x:
                    init_idx = i
                    break
            if init_idx is None:
                continue                # no clear binding site; leave unsupported

        class _Cellify(ast.NodeTransformer):
            def visit_FunctionDef(self, node):
                # A nested function that binds `x` without declaring it
                # nonlocal has its own local `x`; do not touch its body.
                decl = any(isinstance(n, ast.Nonlocal) and x in n.names
                           for n in ast.walk(node))
                if not decl and x in _assigned_names(node):
                    return node
                node.body = [s for s in (self.visit(t) for t in node.body)
                             if s is not None]
                return node

            def visit_Nonlocal(self, node):
                names = [n for n in node.names if n != x]
                return ast.Nonlocal(names=names) if names else None

            def visit_Name(self, node):
                if node.id != x:
                    return node
                return ast.Subscript(
                    value=ast.Name(id=cell, ctx=ast.Load()),
                    slice=ast.Constant(value=0), ctx=node.ctx)

        # Rewrite fn's body directly: the guard above is for *nested* defs, and
        # applying it to fn itself would skip the whole function (fn binds `x`
        # and of course does not declare it nonlocal).
        _t = _Cellify()
        fn.body = [s for s in (_t.visit(st) for st in fn.body)
                   if s is not None]
        if x in params:
            fn.body.insert(0, ast.Assign(
                targets=[ast.Name(id=cell, ctx=ast.Store())],
                value=ast.List(elts=[ast.Name(id=x, ctx=ast.Load())],
                               ctx=ast.Load())))
        else:
            old = fn.body[init_idx]     # now `cell[0] = <value>`
            fn.body[init_idx] = ast.Assign(
                targets=[ast.Name(id=cell, ctx=ast.Store())],
                value=ast.List(elts=[old.value], ctx=ast.Load()))
        converted.add(x)
    ast.fix_missing_locations(fn)
    return converted


def _reads_self_fields(fn):
    """True if `fn` reads an attribute off its first parameter (self). Used to
    reject unsound multiple-inheritance vtable dispatch: a stateless mixin is
    safe to resolve through, one that touches instance state is not."""
    if not fn.args.args:
        return False
    selfname = fn.args.args[0].arg
    for n in ast.walk(fn):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                and n.value.id == selfname:
            return True
    return False


def _free_vars(sub, enclosing_names):
    """Enclosing locals that `sub` reads but does not itself bind/param."""
    params = {a.arg for a in sub.args.args}
    bound = params | _assigned_names(sub)
    used = {n.id for n in ast.walk(sub)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return [nm for nm in sorted(used & enclosing_names) if nm not in bound]


class _CallRewriter(ast.NodeTransformer):
    """Rewrite calls to a lifted nested function: rename + prepend captures."""
    def __init__(self, name_map):
        self.name_map = name_map        # original name -> (mangled, captures)

    def visit_Call(self, node):
        self.generic_visit(node)
        f = node.func
        if isinstance(f, ast.Name) and f.id in self.name_map:
            mangled, captures = self.name_map[f.id]
            # `g(*seq)` on a lifted function does not lower: the captures are
            # prepended and the star is left as one more argument, so the call
            # arrives short. It usually fails at the C compiler, but it only
            # *usually* does -- if the arities happen to line up it is a wrong
            # call that compiles. Say so rather than emit it silently.
            if any(isinstance(a, ast.Starred) for a in node.args):
                sys.stderr.write(
                    "py2c: line %s: `%s(*args)` on a nested function is not "
                    "lowered -- its captured values are prepended, so the "
                    "starred call arrives short. Spell the arguments out.\n"
                    % (getattr(node, "lineno", "?"), f.id))
            node.func = ast.Name(id=mangled, ctx=ast.Load())
            node.args = [ast.Name(id=c, ctx=ast.Load()) for c in captures] \
                + node.args
        return node


class _ValueUseReplacer(ast.NodeTransformer):
    """Replace a *value* use of a lifted nested function (the bare name, not a
    call — those are already rewritten) with a closure-construction marker, so
    `callback(emitting)` builds a real closure carrying the captured env."""
    def __init__(self, vmap):
        self.vmap = vmap                # orig name -> (mangled, captures)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) and node.id in self.vmap:
            mangled, captures = self.vmap[node.id]
            return ast.Call(
                func=ast.Name(id="__closure_env__", ctx=ast.Load()),
                args=[ast.Constant(value=mangled)] +
                     [ast.Name(id=c, ctx=ast.Load()) for c in captures],
                keywords=[])
        return node


class _NameRenamer(ast.NodeTransformer):
    """Rename references to a single local name within an expression."""
    def __init__(self, old, new):
        self.old, self.new = old, new

    def visit_Name(self, node):
        if node.id == self.old:
            return ast.copy_location(ast.Name(id=self.new, ctx=node.ctx), node)
        return node


def rewrite_module_lambdas(tree):
    """Module-level `name = lambda ...` -> `def name(...): return ...`."""
    new_body = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                and isinstance(stmt.targets[0], ast.Name) \
                and isinstance(stmt.value, ast.Lambda):
            lam = stmt.value
            name = stmt.targets[0].id
            fn = ast.FunctionDef(name=name, args=lam.args,
                                 body=[ast.Return(value=lam.body)],
                                 decorator_list=[], returns=None)
            new_body.append(ast.copy_location(fn, stmt))
        else:
            new_body.append(stmt)
    tree.body = new_body
    ast.fix_missing_locations(tree)
    return tree


def rewrite_class_lambdas(tree):
    """A class-level `name = lambda self_, *args: body` is, in Python, just a
    method (a function stored as a class attribute binds as one). Rewrite each
    such assignment into a real method def so it participates in normal method
    dispatch -- including polymorphic override across subclasses."""
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        new_body = []
        for stmt in cls.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                    and isinstance(stmt.targets[0], ast.Name) \
                    and isinstance(stmt.value, ast.Lambda):
                lam = stmt.value
                name = stmt.targets[0].id
                body = lam.body
                if lam.args.args and lam.args.args[0].arg != "self":
                    # the first parameter is the (conventionally `_`) self slot
                    body = _NameRenamer(lam.args.args[0].arg, "self").visit(body)
                    lam.args.args[0] = ast.arg(arg="self")
                fn = ast.FunctionDef(name=name, args=lam.args,
                                     body=[ast.Return(value=body)],
                                     decorator_list=[], returns=None)
                new_body.append(ast.copy_location(fn, stmt))
            else:
                new_body.append(stmt)
        cls.body = new_body
    ast.fix_missing_locations(tree)
    return tree


def _is_dunder_main_guard(node):
    """True for `if __name__ == "__main__":` -- the script-entry idiom. The C
    program has its own entry point, so this block is skipped when transpiling."""
    return (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
            and len(node.test.ops) == 1
            and isinstance(node.test.ops[0], ast.Eq)
            and len(node.test.comparators) == 1
            and isinstance(node.test.comparators[0], ast.Constant)
            and node.test.comparators[0].value == "__main__")


def lift_nested_functions(tree):
    """Closure-convert one level of nested functions to file scope.

    Each nested def is moved to module scope with a mangled name and its
    captured enclosing locals prepended as parameters; all calls (including
    recursive ones) are rewritten to pass those captures. Functions that
    rebind a captured variable via `nonlocal` are left in place (unsupported).
    """
    lifted = []
    specs = {}

    def process(fn, prefix, cls=None):
        _cellify_nonlocals(fn)
        # Annotations of the enclosing function's params and annotated locals,
        # so a captured local keeps its real (possibly unboxed) type when it
        # becomes a parameter of the lifted function.
        _encl_ann = {}
        for a in fn.args.args:
            if a.annotation is not None:
                _encl_ann[a.arg] = a.annotation
        for st in ast.walk(fn):
            if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) \
                    and st.annotation is not None:
                _encl_ann.setdefault(st.target.id, st.annotation)
        encl = {a.arg for a in fn.args.args} | _assigned_names(fn)
        if fn.args.vararg:
            encl.add(fn.args.vararg.arg)
        if fn.args.kwarg:
            encl.add(fn.args.kwarg.arg)
        nested = [s for s in fn.body if isinstance(s, ast.FunctionDef)]
        if not nested:
            return
        name_map = {}
        orig_defaults = {}              # sub.name -> original-param default nodes
        for sub in nested:
            if any(isinstance(n, ast.Nonlocal) for n in ast.walk(sub)):
                # Rebinds an enclosing local: not liftable (captures are passed
                # by value, so the write would not be visible to the parent).
                # Say so -- otherwise the def is silently dropped and every
                # call to it becomes an undefined reference at link time.
                sys.stderr.write(
                    "py2c: line %s: nested function '%s' uses `nonlocal`; it is "
                    "not lowered and calls to it will not link. Pass the value "
                    "in and return the update, or use a one-element list as a "
                    "mutable cell.\n" % (getattr(sub, "lineno", "?"), sub.name))
                continue                # rebinds enclosing var; can't lift
            captures = _free_vars(sub, encl)
            mangled = "%s__%s" % (prefix, sub.name)
            name_map[sub.name] = (mangled, captures)
            np = len(sub.args.args)
            nd = len(sub.args.defaults)
            orig_defaults[sub.name] = [None] * (np - nd) + list(sub.args.defaults)
        if not name_map:
            return
        # Captures are transitive. `_free_vars` only sees names a function
        # reads *directly*, but a sibling call is rewritten to pass that
        # sibling's captures as arguments -- so the caller must capture them
        # too. Without this, `def alloc(): ... taken(f) ...` (where only
        # `taken` reads `words`) lifted to `alloc(void)` yet emitted
        # `taken(words, f)`, and `words` was undeclared in that scope.
        #
        # *Mentioning* a sibling is enough; it need not be called. Handing one
        # on as a value -- `_emit_class(cls, names, cinfo, tsub, ...)` in
        # `cpprust.translate` -- lowers to a `make_closure` carrying that
        # sibling's captured values, emitted right here in the caller's body.
        # Walking only `ast.Call` missed those, so `translate__emit_one`
        # built a closure over `tnames` and `wanted` while taking neither as
        # a parameter: eleven undeclared identifiers across four functions.
        # Iterate to a fixed point so chains of any depth converge.
        changed = True
        while changed:
            changed = False
            for sub in nested:
                if sub.name not in name_map:
                    continue
                mangled, caps = name_map[sub.name]
                inherited = set(caps)
                for n in ast.walk(sub):
                    if isinstance(n, ast.Name) and \
                            isinstance(n.ctx, ast.Load) and \
                            n.id in name_map and n.id != sub.name:
                        inherited |= set(name_map[n.id][1])
                merged = sorted(inherited)
                if merged != caps:
                    name_map[sub.name] = (mangled, merged)
                    changed = True
        rewriter = _CallRewriter(name_map)
        # drop the nested defs from the parent body, rewrite remaining calls
        fn.body = [s for s in fn.body if not (isinstance(s, ast.FunctionDef)
                                              and s.name in name_map)]
        rewriter.visit(fn)
        my_subs = []
        for sub in nested:
            if sub.name not in name_map:
                continue
            mangled, captures = name_map[sub.name]
            real_defs = orig_defaults[sub.name]
            sub.name = mangled
            # captured locals default to Tier-2 obj (their real type coerces in
            # at the call site); a name-based guess like defined->bool is wrong.
            # `self` keeps its class so member/static access still resolves.
            bound = {a.arg for a in sub.args.args}
            if sub.args.vararg:
                bound.add(sub.args.vararg.arg)
            if sub.args.kwarg:
                bound.add(sub.args.kwarg.arg)
            bound.update(a.arg for a in sub.args.kwonlyargs)
            cap_args = []
            for c in captures:
                cap_name = c
                if cap_name in bound:
                    cap_name = c + "__cap"
                    _NameRenamer(c, cap_name).visit(sub)
                # Reuse the enclosing declaration's annotation when there is
                # one. Defaulting everything to `object` boxes an unboxed
                # container (`words: "list[int]"` -> _tlist_int*) as a bare
                # T_OBJ, and the generic subscript/subscript_set runtime path
                # then misreads it -- writes silently fail to land.
                ann = _encl_ann.get(c)
                if ann is None:
                    ann = ast.Name(id=(cls if (c == "self" and cls)
                                       else "object"), ctx=ast.Load())
                cap_args.append(ast.arg(arg=cap_name, annotation=ann))
            sub.args.args = cap_args + sub.args.args
            rewriter.visit(sub)         # rewrite recursive/sibling calls
            sub.decorator_list = []
            lifted.append(sub)
            my_subs.append(sub)
            # a value use of this fn (passed as an argument, stored, returned)
            # needs a real closure; register a spec so a trampoline is emitted.
            specs[mangled] = (sub, len(captures), real_defs)
            process(sub, mangled, cls)  # handle deeper nesting
        # any remaining bare references to a lifted name are value uses
        repl = _ValueUseReplacer(dict(name_map))
        repl.visit(fn)
        for sub in my_subs:
            repl.visit(sub)

    for top in list(tree.body):
        if isinstance(top, ast.FunctionDef):
            process(top, top.name)
        elif isinstance(top, ast.ClassDef):
            for item in top.body:
                if isinstance(item, ast.FunctionDef):
                    process(item, "%s_%s" % (top.name, item.name), top.name)
    tree.body = lifted + tree.body
    ast.fix_missing_locations(tree)
    return specs


def convert_block_closures(tree):
    """Closure-convert nested functions the call-rewriting lift cannot handle:
    those defined inside a block (for/while/if/with/try) and used as a
    first-class value (e.g. asm_gen's `get_reg`, defined per-command inside a
    loop and handed to `command.make_asm`).

    Each such function is moved to file scope with its captured enclosing
    locals prepended as parameters; the original `def f(...)` is replaced by
    `f = __closure_env__("mangled", cap0, cap1, ...)`, which the emitter lowers
    to a make_closure carrying the captured values. The classic capture-via-
    default idiom (`_i=i`) is honoured: such params are folded into the
    captured environment (their value is the default expression at def time),
    while params with constant/no defaults (`pref=None`) stay caller-supplied.

    Returns {mangled: (lifted_node, n_caps, real_default_nodes)}.
    """
    specs = {}
    lifted = []
    used = {f.name for f in tree.body if isinstance(f, ast.FunctionDef)}

    def process(fn, prefix, cls):
        toplevel = {id(s) for s in fn.body}   # handled by the other lift
        encl = {a.arg for a in fn.args.args} | _assigned_names(fn)
        # A block-nested `def f` becomes a local `f = __closure_env__(...)`, so a
        # sibling closure that references `f` must be able to capture it. Such
        # def names are not picked up by _assigned_names, so add them here.
        for n in ast.walk(fn):
            if isinstance(n, ast.FunctionDef) and n is not fn:
                encl.add(n.name)
        if fn.args.vararg:
            encl.add(fn.args.vararg.arg)
        if fn.args.kwarg:
            encl.add(fn.args.kwarg.arg)

        class T(ast.NodeTransformer):
            def visit_FunctionDef(self, sub):
                if sub is fn or id(sub) in toplevel:
                    self.generic_visit(sub)
                    return sub
                if any(isinstance(n, ast.Nonlocal) for n in ast.walk(sub)):
                    return sub                 # rebinds enclosing var; skip
                return self.closure(sub)

            def closure(self, sub):
                params = sub.args.args
                defaults = sub.args.defaults
                dmap = {len(params) - len(defaults) + k: d
                        for k, d in enumerate(defaults)}
                defcap, real, real_defs = [], [], []
                for idx, p in enumerate(params):
                    d = dmap.get(idx)
                    if d is not None and not isinstance(d, ast.Constant):
                        defcap.append((p.arg, d))      # `_i=i` -> capture
                    else:
                        real.append(p)
                        real_defs.append(d)
                # free vars read in the BODY (not in default expressions)
                bound = {a.arg for a in params} | _assigned_names(sub)
                body_used = set()
                for st in sub.body:
                    for n in ast.walk(st):
                        if isinstance(n, ast.Name) and \
                                isinstance(n.ctx, ast.Load):
                            body_used.add(n.id)
                fv = [nm for nm in sorted(body_used & encl) if nm not in bound]
                cap_names = list(fv) + [nm for nm, _ in defcap]
                cap_vals = [ast.Name(id=nm, ctx=ast.Load()) for nm in fv] + \
                    [d for _, d in defcap]
                n_caps = len(cap_names)
                orig_name = sub.name
                mangled = "%s__%s" % (prefix, orig_name)
                base, k = mangled, 2
                while mangled in used:    # avoid colliding with the call-rewrite
                    mangled = "%s__%d" % (base, k)   # lift or a same-named sibling
                    k += 1
                used.add(mangled)
                new_args = []
                for nm in cap_names:
                    ann = ast.Name(id=(cls if (nm == "self" and cls)
                                       else "object"), ctx=ast.Load())
                    new_args.append(ast.arg(arg=nm, annotation=ann))
                sub.name = mangled
                sub.args.args = new_args + real
                sub.args.defaults = []
                sub.args.kwonlyargs = []
                sub.args.kw_defaults = []
                sub.decorator_list = []
                self.generic_visit(sub)        # handle any deeper nesting
                lifted.append(sub)
                specs[mangled] = (sub, n_caps, real_defs)
                marker = ast.Call(
                    func=ast.Name(id="__closure_env__", ctx=ast.Load()),
                    args=[ast.Constant(value=mangled)] + cap_vals,
                    keywords=[])
                return ast.Assign(
                    targets=[ast.Name(id=orig_name, ctx=ast.Store())],
                    value=marker)

        T().visit(fn)

    for top in list(tree.body):
        if isinstance(top, ast.FunctionDef):
            process(top, top.name, None)
        elif isinstance(top, ast.ClassDef):
            for item in top.body:
                if isinstance(item, ast.FunctionDef):
                    process(item, "%s_%s" % (top.name, item.name), top.name)
    tree.body = lifted + tree.body
    ast.fix_missing_locations(tree)
    return specs


def _const_dict_specializable(dnode):
    """Whether emit_const_dict has a fast-path for this dict literal
    (str -> list[str], or int -> str). Others become obj class-statics."""
    keys, vals = dnode.keys, dnode.values
    if not keys:
        return False
    str_keys = all(isinstance(k, ast.Constant) and isinstance(k.value, str)
                   for k in keys)
    int_keys = all(isinstance(k, ast.Constant) and isinstance(k.value, int)
                   for k in keys)
    list_vals = all(isinstance(v, ast.List) for v in vals)
    str_vals = all(isinstance(v, ast.Constant) and isinstance(v.value, str)
                   for v in vals)
    return (str_keys and list_vals) or (int_keys and str_vals)


def collect_classes(tree):
    classes = {}
    order = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            ci = ClassInfo(node)
            classes[ci.name] = ci
            order.append(ci)
    for ci in order:
        if ci.base_name in classes:
            ci.base = classes[ci.base_name]
        for bn in ci.extra_base_names:
            if bn in classes:
                ci.extra_bases.append(classes[bn])
    for ci in order:
        for item in ci.node.body:
            if isinstance(item, ast.FunctionDef):
                ci.methods[item.name] = item
                if any(isinstance(d, ast.Name) and d.id == "staticmethod"
                       for d in item.decorator_list):
                    ci.static_methods.add(item.name)
                if any(isinstance(d, ast.Name) and d.id == "classmethod"
                       for d in item.decorator_list):
                    ci.classmethod_methods.add(item.name)
                if any(isinstance(d, ast.Name) and d.id == "property"
                       for d in item.decorator_list):
                    ci.property_methods.add(item.name)
            elif isinstance(item, ast.Assign) and len(item.targets) == 1 \
                    and isinstance(item.targets[0], ast.Name):
                nm, val = item.targets[0].id, item.value
                if nm == "_c_union_" and isinstance(val, ast.Tuple) and all(
                        isinstance(e, ast.Constant) and isinstance(e.value, str)
                        for e in val.elts):
                    # `_c_union_ = ("iv", "dv", "sv")`: these fields share one
                    # storage slot (anonymous C union). Mutually-exclusive by a
                    # discriminant the program manages (e.g. a tag field), this
                    # shrinks the POD toward the 16-byte tagged-union model.
                    ci.union_fields = [e.value for e in val.elts]
                    continue
                if isinstance(val, ast.Dict) and _const_dict_specializable(val):
                    ci.const_dicts[nm] = val
                elif isinstance(val, (ast.Dict, ast.List, ast.Set, ast.Tuple)):
                    ci.class_statics[nm] = val
                elif isinstance(val, ast.Name) and val.id in ci.methods:
                    pass  # method alias, e.g. __rmul__ = __mul__
                elif nm.startswith("__") and nm.endswith("__"):
                    pass  # dunder method names are not instance fields
                elif _attr_mutated_via_classname(ci.node, nm):
                    # A scalar mutated through the class name (e.g.
                    # `ASMCode.label_num += 1`) is a shared class counter, not a
                    # per-instance default: give it real global storage instead
                    # of inlining the initial literal at every read.
                    ci.class_statics[nm] = val
                else:
                    # class-level scalar (e.g. `comm = False`, `name = "add"`,
                    # `FInst_s = None`): a per-class default, possibly overridden
                    # in subclasses -> becomes an instance field set in the ctor
                    ci.class_attrs[nm] = val
        ci.own_fields = discover_fields(ci.node, set(ci.methods))
        ci.bitfields = discover_bitfields(ci.node)
        for nm in ci.class_attrs:           # ensure they get a struct slot
            # A class constant assigned a numeric literal (e.g. `DEFINED = 3`)
            # is typed from its VALUE, not a name heuristic: otherwise an int
            # enum whose name collides with BOOL_NAMES ("defined") is mistyped
            # bool and 3 is stored as true(1). int keeps integer enums
            # comparable/maxable as integers (def_state bookkeeping). Only do
            # this when the attribute is never mutated, so a mutable class
            # counter (`_counter += 1`) stays a real slot, not an inlined
            # literal.
            lit_ct = _const_numeric_ctype(ci.class_attrs[nm])
            if lit_ct is not None and not _class_attr_is_mutated(ci.node, nm):
                ci.own_fields = [(f, lit_ct if f == nm else t)
                                 for (f, t) in ci.own_fields]
                if not any(f == nm for f, _ in ci.own_fields):
                    ci.own_fields.append((nm, lit_ct))
            elif not any(f == nm for f, _ in ci.own_fields):
                ci.own_fields.append((nm, OBJ))
        for nm in ci.class_statics:         # class statics accessed as self.X
            if not any(f == nm for f, _ in ci.own_fields):
                ci.own_fields.append((nm, OBJ))
    vt = set()
    for ci in order:
        root = ci.root()
        for mname in ci.methods:
            if mname == "__init__" or (mname.startswith("__") and
                                       mname.endswith("__")):
                continue
            if mname in ci.static_methods:   # @staticmethod: not virtual
                continue
            if mname in ci.property_methods:  # @property: not virtual
                continue
            if mname in root.methods:
                vt.add(mname)
    discover_fields_from_ctor_locals(tree, classes)
    return classes, order, vt


def _ctor_class_name(val, classes):
    """Class name from `Cls()` / `Cls(...)` if `Cls` is a known local class."""
    if not isinstance(val, ast.Call):
        return None
    f = val.func
    if isinstance(f, ast.Name) and f.id in classes:
        return f.id
    return None


def _ann_class_name(ann, classes):
    """Local class named by a param annotation (`x: Cls` or `x: "Cls"`)."""
    if isinstance(ann, ast.Name) and ann.id in classes:
        return ann.id
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str) \
            and ann.value in classes:
        return ann.value
    return None


def discover_fields_from_ctor_locals(tree, classes):
    """Discover instance fields written through a *typed* receiver other than
    `self` -- `var.attr = ...` / `setattr(var, "attr", ...)` where `var`'s class
    is known from a `var = Cls()` binding or a parameter annotation. These
    cross-class dynamic attributes otherwise have no struct slot, so rt_setattr
    would silently drop the write; giving them a slot makes the write persist.

    A discovered field is typed `obj` by default, but when *every* write to it
    assigns a direct constructor result of one local class -- `recv.layout =
    Layout()` -- the field takes that concrete `Layout*` type, so it can be
    used as a typed pointer rather than a boxed obj. Any disagreement or any
    non-constructor value falls back to `obj` (which holds anything safely)."""

    # (clsname, attr) -> set of candidate ctypes; `None` marks an
    # un-inferable (non-constructor) write, which forces the obj fallback.
    candidates = {}

    def note(clsname, attr, ctype):
        ci = classes.get(clsname)
        if ci is None or any(f == attr for f, _ in ci.own_fields):
            return
        candidates.setdefault((clsname, attr), set()).add(ctype)

    def rhs_ctype(val, locals_types):
        """Concrete `Cls*` type of an assigned value, or None if not a direct
        local-class constructor / a local bound to one."""
        cls = _ctor_class_name(val, classes)
        if cls is None and isinstance(val, ast.Name) and val.id in locals_types:
            cls = locals_types[val.id]
        return (classes[cls].csym + "*") if cls else None

    def scan_scope(body, seed=None):
        locals_types = dict(seed) if seed else {}
        # Collect the nodes of *this* lexical scope, treating a nested
        # def/lambda as an opaque boundary: its body begins a new scope and is
        # scanned separately (below). Flattening scopes together -- as a plain
        # ast.walk does -- lets a `var = Cls()` binding in one function be
        # attributed to a same-named local in a sibling function, inventing
        # bogus fields on unrelated classes.
        scope_nodes = []
        nested = []
        stack = list(body)
        while stack:
            node = stack.pop()
            scope_nodes.append(node)
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.Lambda)):
                    nested.append(child)
                else:
                    stack.append(child)
        for sub in scope_nodes:
            if isinstance(sub, ast.Assign) and len(sub.targets) == 1 and \
                    isinstance(sub.targets[0], ast.Name):
                cls = _ctor_class_name(sub.value, classes)
                if cls:
                    locals_types[sub.targets[0].id] = cls
        for sub in scope_nodes:
            if isinstance(sub, ast.Assign):
                for tgt in sub.targets:
                    if isinstance(tgt, ast.Attribute) and \
                            isinstance(tgt.value, ast.Name) and \
                            tgt.value.id != "self" and \
                            tgt.value.id in locals_types:
                        note(locals_types[tgt.value.id], tgt.attr,
                             rhs_ctype(sub.value, locals_types))
            elif isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                    and sub.func.id == "setattr" and len(sub.args) == 3 \
                    and isinstance(sub.args[0], ast.Name) \
                    and sub.args[0].id != "self" \
                    and sub.args[0].id in locals_types \
                    and isinstance(sub.args[1], ast.Constant) \
                    and isinstance(sub.args[1].value, str):
                note(locals_types[sub.args[0].id], sub.args[1].value,
                     rhs_ctype(sub.args[2], locals_types))
        # Recurse into nested scopes. A nested function closes over the
        # enclosing scope's ctor-typed locals (and module globals), except for
        # any name one of its own parameters rebinds; annotated parameters then
        # contribute their own class. This keeps closures/globals working while
        # never leaking a binding sideways into a sibling scope.
        for fn in nested:
            if isinstance(fn, ast.Lambda):
                continue          # lambda bodies contain no assignments to scan
            child = dict(locals_types)
            _a = fn.args
            for _arg in (list(_a.posonlyargs) + list(_a.args) +
                         list(_a.kwonlyargs)):
                child.pop(_arg.arg, None)
            if _a.vararg:
                child.pop(_a.vararg.arg, None)
            if _a.kwarg:
                child.pop(_a.kwarg.arg, None)
            child.update(params_seed(fn))
            scan_scope(fn.body, child)

    def params_seed(fn):
        seed = {}
        for a in (list(fn.args.posonlyargs) + list(fn.args.args) +
                  list(fn.args.kwonlyargs)):
            cls = _ann_class_name(a.annotation, classes) if a.annotation else None
            if cls:
                seed[a.arg] = cls
        return seed

    scan_scope(tree.body)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            scan_scope(node.body, params_seed(node))
    for ci in classes.values():
        for fn in ci.methods.values():
            scan_scope(fn.body, params_seed(fn))

    for (clsname, attr), types in candidates.items():
        ci = classes[clsname]
        if any(f == attr for f, _ in ci.own_fields):
            continue
        ctype = next(iter(types)) if (None not in types and len(types) == 1) \
            else OBJ
        ci.own_fields.append((attr, ctype))


def discover_bitfields(classnode):
    """Map fieldname -> bit width N for `self.x : T(N) = ...` annotations (and
    class-level `x: T(N)`). The field's value type is the plain scalar T (see
    ann_text_to_ctype); only the struct declaration needs the width."""
    widths = {}

    def _record(name, ann):
        if ann is None:
            return
        try:
            txt = ast.unparse(ann)
        except Exception:
            return
        m = re.match(r"^\w[\w ]*?\(\s*(\d+)\s*\)$", txt.strip().strip("'\""))
        if m is not None:
            widths[name] = int(m.group(1))

    for item in classnode.body:
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            _record(item.target.id, item.annotation)
    for item in classnode.body:
        if not isinstance(item, ast.FunctionDef):
            continue
        for sub in ast.walk(item):
            if isinstance(sub, ast.AnnAssign) \
                    and isinstance(sub.target, ast.Attribute) \
                    and isinstance(sub.target.value, ast.Name) \
                    and sub.target.value.id == "self":
                _record(sub.target.attr, sub.annotation)
    return widths


def discover_fields(classnode, method_names=None):
    fields = []
    seen = set()
    method_names = method_names or set()

    def add(name, ctype):
        if name not in seen:
            seen.add(name)
            fields.append((name, ctype))

    for item in classnode.body:
        if isinstance(item, ast.AnnAssign) and isinstance(item.target,
                                                          ast.Name) \
                and not isinstance(item.value, ast.Dict):
            add(item.target.id, infer_type(item.target.id, item.annotation))
    for item in classnode.body:
        if not isinstance(item, ast.FunctionDef):
            continue
        opt = optional_param_names(item)
        param_ann = {a.arg: a.annotation
                     for a in item.args.args if a.annotation is not None}
        for sub in ast.walk(item):
            targets, ann = [], None
            if isinstance(sub, ast.Assign):
                targets = sub.targets
            elif isinstance(sub, ast.AnnAssign):
                targets, ann = [sub.target], sub.annotation
            elif isinstance(sub, ast.Attribute) and \
                    isinstance(sub.value, ast.Name) and sub.value.id == "self":
                # reads of self.attr in methods (e.g. abstract base expects
                # subclass-provided _iv, block_size, digest_size)
                if sub.attr not in method_names:
                    add(sub.attr, infer_type(sub.attr, None))
            else:
                continue
            for tgt in targets:
                # `self.a, self.b = ...` -> the target is a Tuple/List of
                # Attributes; flatten so each self.<attr> is discovered.
                subtgts = (tgt.elts if isinstance(tgt, (ast.Tuple, ast.List))
                           else [tgt])
                for st in subtgts:
                    if isinstance(st, ast.Attribute) and \
                            isinstance(st.value, ast.Name) and \
                            st.value.id == "self":
                        # self.x = <optional param>  ->  obj
                        if ann is None and isinstance(sub, ast.Assign) and \
                                isinstance(sub.value, ast.Name) and \
                                sub.value.id in opt:
                            add(st.attr, OBJ)
                        # self.x = <annotated param>  ->  the param's type
                        elif ann is None and isinstance(sub, ast.Assign) and \
                                isinstance(sub.value, ast.Name) and \
                                sub.value.id in param_ann:
                            add(st.attr, infer_type(st.attr,
                                                    param_ann[sub.value.id]))
                        # self.x = None  ->  a None-initialised field is
                        # nullable and may later hold an object (possibly only
                        # in another module), so a name/scalar heuristic that
                        # would call it int/double/bool/char* is unsafe: type it
                        # obj, which can hold None and any value.
                        elif ann is None and isinstance(sub, ast.Assign) and \
                                isinstance(sub.value, ast.Constant) and \
                                sub.value.value is None and \
                                infer_type(st.attr, None) in (
                                    "int", "double", "bool", "char*"):
                            add(st.attr, OBJ)
                        else:
                            add(st.attr, infer_type(st.attr, ann))
    return fields


# ==========================================================================
# Transpiler
# ==========================================================================

class Unsupported(Exception):
    pass


C_KEYWORDS = {"int", "char", "short", "long", "float", "double", "void",
              "struct", "union", "enum", "const", "register", "static",
              "return", "if", "else", "while", "for", "switch", "default",
              "auto", "extern", "signed", "unsigned", "volatile", "goto",
              # runtime type/identifier names that must not be shadowed
              "obj", "Obj", "List", "Dict", "TypeInfo"}

# Method names that collide with per-class `TypeInfo <Class>_type` symbols.
METHOD_TYPEINFO_COLLISION = frozenset({"type"})

# Runtime API symbols that Python stdlib modules may reuse as function names.
RUNTIME_API_NAMES = {
    "make_closure", "call_closure", "subscript", "subscript_set",
    "dict_get", "dict_set", "dict_new", "list_new", "list_of", "truthy",
    "obj_add", "obj_neg", "aalloc", "identity__tramp", "call_obj",
}

# Module-level helper functions whose Python body is not transpilable (they use
# the stdlib `struct` module) but which the runtime provides directly. Their
# `def` is skipped and calls are lowered to the runtime function.
RUNTIME_INTRINSICS = {"_float_to_bits", "arena_mark", "arena_release"}

JSON_PRELUDE = r"""/* ---- rpython typed-json support: a single-pass cursor lexer. Each helper
   takes a `const char* p` and returns the position after the value it read,
   writing the parsed value through an out-pointer. Generated per-class decoders
   (rpy.json.generate_decoder) dispatch keys with strcmp and write struct slots
   directly -- no dict, no boxing, no Python object_hook callback. ---- */
static const char* _json_ws(const char* p) {
    while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
    return p;
}
static const char* _json_long(const char* p, long* out) {
    char* e; *out = strtol(p, &e, 10); return e;
}
static const char* _json_double(const char* p, double* out) {
    char* e; *out = strtod(p, &e); return e;
}
static const char* _json_bool(const char* p, int* out) {
    if (*p == 't') { *out = 1; return p + 4; }   /* true  */
    if (*p == 'f') { *out = 0; return p + 5; }   /* false */
    if (*p == 'n') { *out = 0; return p + 4; }   /* null  */
    return p;
}
/* decode one JSON escape char (after the backslash); \uXXXX is not decoded in
   this subset and is passed through as its final char. */
static char _json_esc(char e) {
    switch (e) {
        case 'n': return '\n'; case 't': return '\t'; case 'r': return '\r';
        case 'b': return '\b'; case 'f': return '\f';
        default:  return e;   /* '"' '\\' '/' and anything else: literal */
    }
}
/* parse a quoted string, allocating a NUL-terminated C string into *out. */
static const char* _json_str(const char* p, char** out) {
    if (*p != '"') { *out = ""; return p; }
    p++;
    int len = 0; const char* q = p;
    while (*q && *q != '"') { if (*q == '\\' && q[1]) q++; len++; q++; }
    char* buf = (char*)malloc((unsigned long)len + 1);
    int i = 0;
    while (*p && *p != '"') {
        char c = *p;
        if (c == '\\' && p[1]) { p++; c = _json_esc(*p); }
        buf[i++] = c; p++;
    }
    buf[i] = 0;
    if (*p == '"') p++;
    *out = buf;
    return p;
}
/* parse a quoted string into a fixed buffer (used for object keys; no alloc). */
static const char* _json_key(const char* p, char* buf, int cap) {
    int i = 0;
    if (*p != '"') { buf[0] = 0; return p; }
    p++;
    while (*p && *p != '"') {
        char c = *p;
        if (c == '\\' && p[1]) { p++; c = _json_esc(*p); }
        if (i < cap - 1) buf[i++] = c;
        p++;
    }
    buf[i] = 0;
    if (*p == '"') p++;
    return p;
}
static const char* _json_skip_str(const char* p) {
    p++;
    while (*p && *p != '"') { if (*p == '\\' && p[1]) p++; p++; }
    if (*p == '"') p++;
    return p;
}
/* skip any value (used for keys the target struct does not declare). */
static const char* _json_skip(const char* p) {
    p = _json_ws(p);
    if (*p == '"') return _json_skip_str(p);
    if (*p == '{' || *p == '[') {
        char open = *p, close = (open == '{') ? '}' : ']';
        int depth = 1; p++;
        while (*p && depth) {
            if (*p == '"') { p = _json_skip_str(p); continue; }
            if (*p == open) depth++;
            else if (*p == close) depth--;
            p++;
        }
        return p;
    }
    while (*p && *p != ',' && *p != '}' && *p != ']'
           && *p != ' ' && *p != '\t' && *p != '\n' && *p != '\r') p++;
    return p;
}
"""



SOCKET_PRELUDE = r"""/* ---- rpython socket support: BSD sockets, fds are opaque int ---- */
int socket(int, int, int);
int connect(int, void*, unsigned int);
int bind(int, void*, unsigned int);
int listen(int, int);
int accept(int, void*, void*);
long send(int, void*, unsigned long, int);
long recv(int, void*, unsigned long, int);
int setsockopt(int, int, int, void*, unsigned int);
int close(int);
unsigned short htons(unsigned short);
unsigned int inet_addr(const char*);
int fork(void);
void _exit(int);
struct __py_sin { unsigned short fam; unsigned short port;
                  unsigned int addr; unsigned char zero[8]; };
static int __py_sock_connect(int fd, char* host, int port) {
    struct __py_sin a; int i = 0;
    a.fam = 2; a.port = htons((unsigned short)port); a.addr = inet_addr(host);
    while (i < 8) { a.zero[i] = 0; i = i + 1; }
    return connect(fd, &a, 16);
}
static int __py_sock_bind(int fd, char* host, int port) {
    struct __py_sin a; int i = 0;
    a.fam = 2; a.port = htons((unsigned short)port);
    a.addr = (host[0] == 0) ? 0 : inet_addr(host);
    while (i < 8) { a.zero[i] = 0; i = i + 1; }
    return bind(fd, &a, 16);
}
"""

_SOCK_CONSTS = {"AF_INET": "2", "AF_INET6": "10", "AF_UNIX": "1",
                "SOCK_STREAM": "1", "SOCK_DGRAM": "2",
                "SOL_SOCKET": "1", "SO_REUSEADDR": "2"}

# libm unary/binary functions: numeric ufuncs that return double. Recognised so
# `exp(x) + sin(x)` stays native double arithmetic instead of boxing.
MATH_FUNCS = {
    "sqrt", "cbrt", "exp", "exp2", "expm1", "log", "log2", "log10", "log1p",
    "sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh",
    "asinh", "acosh", "atanh", "fabs", "floor", "ceil", "round", "trunc",
    "pow", "fmod", "fmax", "fmin", "atan2", "hypot", "copysign",
}

# Bare names routed through mp_call_import("builtins", ...) in stdlib mode.
STDLIB_BUILTINS = {
    "open", "next", "globals", "locals", "type", "__import__", "dir", "round",
    "iter", "callable", "hex", "oct", "bin", "hash", "id", "input", "eval",
    "exec", "compile", "format", "help", "memoryview", "bytearray", "bytes",
    "super", "property", "staticmethod", "classmethod", "object",
    "map", "filter", "zip", "enumerate", "reversed", "sorted", "sum", "min",
    "max", "any", "all", "print", "len", "range", "list", "dict", "set",
    "divmod", "issubclass", "setattr", "tuple", "hasattr", "getattr",
    "int", "float", "bool", "str", "Ellipsis",
    "complex",
    "frozenset",
}

# Exception / warning types resolved from the builtins module.
EXCEPTION_NAMES = {
    "BaseException", "Exception", "StopIteration", "StopAsyncIteration",
    "GeneratorExit", "SystemExit", "KeyboardInterrupt",
    "ArithmeticError", "AssertionError", "AttributeError", "BufferError",
    "EOFError", "ImportError", "LookupError", "MemoryError", "NameError",
    "OSError", "ReferenceError", "RuntimeError", "SyntaxError",
    "SystemError", "TypeError", "ValueError", "Warning", "UserWarning",
    "DeprecationWarning", "PendingDeprecationWarning", "RuntimeWarning",
    "FutureWarning", "ImportWarning", "UnicodeWarning", "BytesWarning",
    "ResourceWarning", "BlockingIOError", "BrokenPipeError", "ChildProcessError",
    "ConnectionError", "ConnectionAbortedError", "ConnectionRefusedError",
    "ConnectionResetError", "FileExistsError", "FileNotFoundError",
    "InterruptedError", "IsADirectoryError", "NotADirectoryError",
    "PermissionError", "ProcessLookupError", "TimeoutError",
    "IndexError", "KeyError", "ModuleNotFoundError", "NotImplementedError",
    "OverflowError", "RecursionError", "UnicodeError", "UnicodeDecodeError",
    "UnicodeEncodeError", "UnicodeTranslateError", "ZeroDivisionError",
    "EnvironmentError", "IOError", "WindowsError", "FloatingPointError",
    "IndentationError", "TabError", "UnboundLocalError",
}

TYPEINFO_RESERVED = {"name", "base"}

# stdio/unistd macros that collide with common Python module-level names.
C_STDIO_MACRO_NAMES = frozenset({
    "SEEK_SET", "SEEK_CUR", "SEEK_END", "EOF",
})


def cname(name):
    return name + "_" if name in C_KEYWORDS or name in RUNTIME_API_NAMES else name


def _fdef_name(f):
    # Sort key for FunctionDef / object-with-.name lists. A named module
    # function rather than a lambda so the source parses under rast (and mirrors
    # how the translator itself rewrites lambdas to defs).
    return f.name


def method_cname(mname):
    return mname + "_" if mname in METHOD_TYPEINFO_COLLISION else mname


def vslot_name(mname):
    n = cname(mname)
    return "vt_" + n if n in TYPEINFO_RESERVED else n


def c_mod_slug(name):
    return name.replace(".", "_").replace("-", "_")


_AMBIG_CACHE = {}


def ambiguous_class_names(base_dir):
    """Class names defined in more than one shivyc module. Such names cannot use
    a bare C symbol (it would collide at link time, e.g. tree.Mult vs the il_cmd
    Mult), so they are module-qualified. Computed once over the whole package so
    that every separately-compiled module agrees, even ones that import neither
    side of a collision."""
    ckey = (base_dir, tuple(sorted(_LOCAL_MODULE_DIRS)))
    if ckey in _AMBIG_CACHE:
        return _AMBIG_CACHE[ckey]
    name2mods = {}
    pkg = os.path.join(base_dir or ".", "shivyc")
    pkg_abs = os.path.abspath(pkg)
    for dp, _dirs, fns in sorted(os.walk(pkg)):
        if "musl" in dp.split(os.sep):
            continue
        for fn in sorted(fns):
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dp, fn)
            mod = os.path.relpath(p, base_dir or ".")[:-3].replace(os.sep, ".")
            try:
                t = ast.parse(open(p, encoding="utf-8").read())
            except Exception:
                continue
            for n in t.body:
                if isinstance(n, ast.ClassDef):
                    name2mods.setdefault(n.name, set()).add(mod)
    # Also scan the input program's own module directories (set via
    # set_local_module_dirs) so same-named classes in *any* multi-file rpython
    # program are detected -- not just the shivyc package. Dirs already inside
    # the shivyc package are skipped: the walk above covers them under their
    # dotted module name, and re-adding them under their basename would make a
    # genuinely unique class look ambiguous.
    for d in _LOCAL_MODULE_DIRS:
        da = os.path.abspath(d)
        if not os.path.isdir(d) or da == pkg_abs \
                or da.startswith(pkg_abs + os.sep):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py"):
                continue
            try:
                t = ast.parse(open(os.path.join(d, fn),
                                   encoding="utf-8").read())
            except Exception:
                continue
            for n in t.body:
                if isinstance(n, ast.ClassDef):
                    name2mods.setdefault(n.name, set()).add(fn[:-3])
    amb = {n for n, mods in name2mods.items() if len(mods) > 1}
    _AMBIG_CACHE[ckey] = amb
    return amb


_PROJECT_METHOD_CACHE = {}


def project_method_owners(base_dir):
    """method name -> set of (modname, classname) over the whole shivyc package
    (plus any local module dirs). Lets a method called on an untyped/dynamic obj
    receiver resolve to its defining class even when that class is not imported
    by the calling module -- but only when it is the SOLE definer of the name
    (the caller enforces that), so the cast to that class is sound. Computed and
    cached once, like ambiguous_class_names."""
    ckey = (base_dir, tuple(sorted(_LOCAL_MODULE_DIRS)))
    if ckey in _PROJECT_METHOD_CACHE:
        return _PROJECT_METHOD_CACHE[ckey]
    owners = {}
    pkg = os.path.join(base_dir or ".", "shivyc")
    pkg_abs = os.path.abspath(pkg)

    def scan(path, modname):
        try:
            if path.endswith(".decls.json"):
                # A C++ module, present as its class digest. Rendered
                # through the same stub the import path uses and harvested
                # by the same ClassDef walk below -- one rendering, every
                # consumer. Without this the hierarchy scan cannot see a
                # C++ base at all, so a py2c subclass becomes its own
                # hierarchy root and the canonical vtable shrinks to its
                # own methods, dropping the inherited slots. (Caught as
                # `.take` missing from a derived TypeInfo and a segfault
                # through the NULL slot.)
                import json as _json
                with open(path, encoding="utf-8") as f:
                    stub = _stub_from_decls(_json.load(f), modname, path)
                if stub is None:
                    return
                t = ast.parse(stub)
            else:
                t = ast.parse(open(path, encoding="utf-8").read())
        except Exception:
            return
        for n in t.body:
            if not isinstance(n, ast.ClassDef):
                continue
            for item in n.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and item.name != "__init__":
                    owners.setdefault(item.name, set()).add((modname, n.name))

    for dp, _dirs, fns in os.walk(pkg):
        if "musl" in dp.split(os.sep):
            continue
        for fn in fns:
            if fn.endswith(".decls.json"):
                p = os.path.join(dp, fn)
                mod = os.path.relpath(p, base_dir or ".")
                mod = mod[:-len(".decls.json")].replace(os.sep, ".")
                scan(p, mod)
                continue
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dp, fn)
            mod = os.path.relpath(p, base_dir or ".")[:-3].replace(os.sep, ".")
            scan(p, mod)
    for d in _LOCAL_MODULE_DIRS:
        da = os.path.abspath(d)
        if not os.path.isdir(d) or da == pkg_abs \
                or da.startswith(pkg_abs + os.sep):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".py"):
                scan(os.path.join(d, fn), fn[:-3])
            elif fn.endswith(".decls.json"):
                scan(os.path.join(d, fn), fn[:-len(".decls.json")])
    _PROJECT_METHOD_CACHE[ckey] = owners
    return owners


_PROJECT_HIER_CACHE = {}


def project_class_hierarchy(base_dir):
    """Global class map for cross-module vtable consistency, computed once over
    the whole package (+ local dirs) and cached.

    Returns (classes, byname):
      classes : (mod, name) -> {"base": base_name|None, "fns": name->FunctionDef}
      byname  : name -> [mods]   (to resolve a base reference to its module)

    Keyed by (mod, name) because class names are NOT globally unique (e.g.
    general_nodes.Root extends Node while decl_nodes.Root extends DeclNode); a
    flat by-name map would mis-root one of them and break the canon."""
    ckey = (base_dir, tuple(sorted(_LOCAL_MODULE_DIRS)))
    if ckey in _PROJECT_HIER_CACHE:
        return _PROJECT_HIER_CACHE[ckey]
    classes, byname = {}, {}

    def base_of(n):
        for b in n.bases:
            if isinstance(b, ast.Name):
                return b.id
            if isinstance(b, ast.Attribute):
                return b.attr
        return None

    def scan(path, modname):
        try:
            if path.endswith(".decls.json"):
                # A C++ module, present as its class digest, rendered
                # through the same stub the import path uses -- without it
                # the hierarchy cannot see a C++ base, a py2c subclass
                # becomes its own root, and the canonical vtable shrinks
                # to its own methods, dropping the inherited slots.
                import json as _json
                with open(path, encoding="utf-8") as f:
                    stub = _stub_from_decls(_json.load(f), modname, path)
                if stub is None:
                    return
                t = ast.parse(stub)
            else:
                t = ast.parse(open(path, encoding="utf-8").read())
        except Exception:
            return
        for n in t.body:
            if not isinstance(n, ast.ClassDef):
                continue
            fns = {it.name: it for it in n.body
                   if isinstance(it, ast.FunctionDef)}
            classes[(modname, n.name)] = {"base": base_of(n), "fns": fns}
            byname.setdefault(n.name, [])
            if modname not in byname[n.name]:
                byname[n.name].append(modname)

    pkg = os.path.join(base_dir or ".", "shivyc")
    pkg_abs = os.path.abspath(pkg)
    for dp, _dirs, fns in sorted(os.walk(pkg)):
        if "musl" in dp.split(os.sep):
            continue
        for fn in sorted(fns):
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dp, fn)
            mod = os.path.relpath(p, base_dir or ".")[:-3].replace(os.sep, ".")
            scan(p, mod)
    for d in _LOCAL_MODULE_DIRS:
        da = os.path.abspath(d)
        if not os.path.isdir(d) or da == pkg_abs \
                or da.startswith(pkg_abs + os.sep):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".py"):
                scan(os.path.join(d, fn), fn[:-3])
            elif fn.endswith(".decls.json"):
                scan(os.path.join(d, fn), fn[:-len(".decls.json")])
    _PROJECT_HIER_CACHE[ckey] = (classes, byname)
    return classes, byname


def _hier_resolve(classes, byname, mod, name):
    """Resolve class `name` as referenced from module `mod` to a (mod,name) key:
    prefer the same module, else a globally-unique definition, else None."""
    if (mod, name) in classes:
        return (mod, name)
    mods = byname.get(name)
    if mods and len(mods) == 1:
        return (mods[0], name)
    return None


_HIER_ROOT_MEMO = {}


def hier_root_key(classes, byname, mod, name):
    """(mod,name) key of the hierarchy root for `name` referenced from `mod`."""
    memo = _HIER_ROOT_MEMO.get(id(classes))
    if memo is None:
        memo = _HIER_ROOT_MEMO[id(classes)] = {}
    mk = (mod, name)
    if mk in memo:
        return memo[mk]
    cur = _hier_resolve(classes, byname, mod, name)
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        base = classes[cur]["base"]
        if not base:
            break
        nxt = _hier_resolve(classes, byname, cur[0], base)
        if nxt is None:
            break               # base unresolved/ambiguous: cur is the root
        cur = nxt
    memo[mk] = cur
    return cur


def _hier_skip_method(m, fn):
    if m == "__init__" or (m.startswith("__") and m.endswith("__")):
        return True
    return any(isinstance(d, ast.Name) and d.id in ("staticmethod", "property")
               for d in fn.decorator_list)


def hier_members(classes, byname, root_key):
    return [k for k in classes
            if hier_root_key(classes, byname, k[0], k[1]) == root_key]


def _fn_arity(fn):
    return len(fn.args.posonlyargs) + len(fn.args.args)   # includes self


def _slot_paddable(fns):
    """True if every definition of a method can be uniformly called through one
    widest slot: the widest fn's params beyond the NARROWEST definition's arity
    must all have defaults (so a narrow call pads with them). This admits real
    optional-arg overrides (Node.make_il vs Compound.make_il(no_scope=False))
    but rejects same-named helpers with incompatible shapes (arith
    _check_type(left,right) vs unary _check_type(expr)), which must stay
    statically dispatched."""
    widest = max(fns, key=_fn_arity)
    if widest.args.vararg:
        return True                       # vararg slots route via mp_call
    nparams = _fn_arity(widest)           # includes self
    ndef = len(widest.args.defaults)
    first_defaulted = nparams - ndef      # index of first param with a default
    lo = min(_fn_arity(f) for f in fns)   # narrowest call arity (incl. self)
    return lo >= first_defaulted


def _is_true_const(node):
    """True if `node` is the literal `True` (a `while True:` test)."""
    return isinstance(node, ast.Constant) and node.value is True


def _stmt_has_own_break(s):
    """True if statement `s` contains a `break` that targets the loop directly
    enclosing it -- i.e. not descending into a nested loop (whose break belongs
    to that nested loop)."""
    if isinstance(s, ast.Break):
        return True
    if isinstance(s, (ast.For, ast.While, ast.AsyncFor)):
        return False
    if isinstance(s, ast.If):
        return any(_stmt_has_own_break(x) for x in s.body) or \
            any(_stmt_has_own_break(x) for x in s.orelse)
    if isinstance(s, ast.With):
        return any(_stmt_has_own_break(x) for x in s.body)
    if isinstance(s, ast.Try):
        return (any(_stmt_has_own_break(x) for x in s.body) or
                any(_stmt_has_own_break(x) for h in s.handlers
                    for x in h.body) or
                any(_stmt_has_own_break(x) for x in s.orelse) or
                any(_stmt_has_own_break(x) for x in s.finalbody))
    return False


def _stmts_always_exit(stmts):
    """True if executing `stmts` always leaves via return/raise (never falls off
    the end). Conservative: only returns True when certain, so an uncertain
    function gets a (harmless if unreachable) default return appended."""
    return bool(stmts) and _stmt_always_exits(stmts[-1])


def _stmt_always_exits(s):
    if isinstance(s, (ast.Return, ast.Raise)):
        return True
    if isinstance(s, ast.If):
        if not s.orelse:
            return False
        return _stmts_always_exit(s.body) and _stmts_always_exit(s.orelse)
    if isinstance(s, ast.While):
        # `while True:` with no break of its own never falls through.
        return _is_true_const(s.test) and not any(
            _stmt_has_own_break(x) for x in s.body)
    if isinstance(s, ast.With):
        return _stmts_always_exit(s.body)
    if isinstance(s, ast.Try):
        if s.finalbody and _stmts_always_exit(s.finalbody):
            return True
        main = s.orelse if s.orelse else s.body
        return _stmts_always_exit(main) and \
            all(_stmts_always_exit(h.body) for h in s.handlers)
    return False


def hier_canon_key(classes, byname, root_key):
    """Canonical vtable method names for a hierarchy: every method that is
    polymorphically dispatched. That is the root's own interface PLUS any method
    overridden across the hierarchy (defined in >= 2 member classes) -- the
    latter catches virtuals introduced by an intermediate class (e.g. ExprNode's
    `lvalue`). A method is admitted only if its definitions share one paddable
    slot signature; same-named private helpers with incompatible shapes stay
    statically dispatched. A hierarchy with no subclasses needs no vtable."""
    members = hier_members(classes, byname, root_key)
    if len(members) < 2:
        return set()
    defs = {}
    for k in members:
        for m, fn in classes[k]["fns"].items():
            if _hier_skip_method(m, fn):
                continue
            defs.setdefault(m, []).append(fn)
    root_fns = classes.get(root_key, {}).get("fns", {})
    canon = set()
    for m, fns in defs.items():
        in_root = m in root_fns and not _hier_skip_method(m, root_fns[m])
        if not (in_root or len(fns) >= 2):
            continue
        if _slot_paddable(fns):
            canon.add(m)
    return canon


def hier_widest_fn(classes, byname, root_key, mname):
    """Across every class whose root is `root_key`, the FunctionDef for `mname`
    with the most positional parameters (the slot must take the widest arity)."""
    best, bestn = None, -1
    for key in hier_members(classes, byname, root_key):
        fn = classes[key]["fns"].get(mname)
        if fn is None:
            continue
        n = len(fn.args.posonlyargs) + len(fn.args.args)
        if n > bestn:
            bestn, best = n, fn
    return best


def module_external_canon(base_dir, modname, local_names):
    """The canonical vtable (canon_method_set, root_key) for `modname`'s
    classes. Works whether the hierarchy root is defined in this module (the
    root-defining module must lay out the SAME union as its subclass modules) or
    imported. Returns None when the module's virtual classes don't share a
    single hierarchy (so there is no one layout to pin)."""
    classes, byname = project_class_hierarchy(base_dir)
    roots = {}
    for nm in local_names:
        rk = hier_root_key(classes, byname, modname, nm)
        if rk is None:
            continue
        canon = hier_canon_key(classes, byname, rk)
        if canon:
            roots[rk] = canon
    if len(roots) != 1:
        return None
    rk, canon = next(iter(roots.items()))
    return canon, rk


def pod_csyms(tree, order, pod_enabled, extra_subclassed=None):
    """Csyms of classes in `order` (parsed from `tree`) that qualify for the
    POD lowering (bare struct, no Obj header / vtable). Mirrors the core of
    Transpiler._compute_pod_set, but as a free function so an *importing*
    module can replicate a defining module's POD decision and keep struct
    layout and dispatch in agreement across the module boundary.

    `extra_subclassed` is a set of class names that are subclassed *elsewhere*
    in the project; those are never POD even if this file shows no subclass."""
    if not pod_enabled:
        return set()
    has_subclass = set(extra_subclassed or ())
    for ci in order:
        if getattr(ci, "base_name", None):
            has_subclass.add(ci.base_name)
    class_names = {ci.name for ci in order}
    value_used = set()
    for parent in ast.walk(tree):
        for _f, child in ast.iter_fields(parent):
            kids = child if isinstance(child, list) else [child]
            for c in kids:
                if isinstance(c, ast.Name) and c.id in class_names:
                    if isinstance(parent, ast.Call) and parent.func is c:
                        continue            # construction X(...) is fine
                    value_used.add(c.id)
    pod = set()
    for ci in order:
        if (not getattr(ci, "base_name", None)
                and ci.name not in has_subclass
                and ci.name not in value_used
                and not getattr(ci, "const_dicts", None)
                and not getattr(ci, "class_statics", None)
                and "__add__" not in getattr(ci, "methods", {})
                and not getattr(ci, "class_attrs", None)):
            pod.add(ci.csym)
    return pod


def class_csym(name, modname, ambiguous):
    """C base symbol for class `name` defined in `modname`: bare when unique
    across the package, else module-qualified so collisions get distinct
    symbols. The qualifier uses the module's LAST path component (as func_csym
    does), so a defining module and its importers agree on the symbol whether
    the module is named bare (`asm_cmds`, a top-level self-transpile) or
    package-qualified (`shivyc.asm_cmds`, how siblings import it)."""
    if name in ambiguous and modname:
        return "%s__%s" % (c_mod_slug(modname.split(".")[-1]), name)
    return name


_AMBIG_FUNC_CACHE = {}


def ambiguous_function_names(base_dir):
    """Top-level function names defined in more than one shivyc module. Like
    ambiguous class names, such functions are cross-module API (e.g. every
    optimizer pass exports `optimize`), so a bare C symbol would collide at
    link time; they get module-qualified instead."""
    if base_dir in _AMBIG_FUNC_CACHE:
        return _AMBIG_FUNC_CACHE[base_dir]
    name2mods = {}
    pkg = os.path.join(base_dir or ".", "shivyc")
    for dp, _dirs, fns in os.walk(pkg):
        if "musl" in dp.split(os.sep):
            continue
        for fn in fns:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dp, fn)
            mod = os.path.relpath(p, base_dir or ".")[:-3].replace(os.sep, ".")
            try:
                t = ast.parse(open(p, encoding="utf-8").read())
            except Exception:
                continue
            for n in t.body:
                if isinstance(n, ast.FunctionDef):
                    name2mods.setdefault(n.name, set()).add(mod)
    amb = {n for n, mods in name2mods.items() if len(mods) > 1}
    _AMBIG_FUNC_CACHE[base_dir] = amb
    return amb


def func_csym(name, modname, ambiguous_funcs):
    """C symbol for module-level function `name` defined in `modname`: bare when
    unique across the package, else module-qualified to avoid link collisions
    (e.g. peephole.optimize vs stackless.optimize). The qualifier uses the
    module's final component so the definition and every cross-module call agree
    regardless of whether the module was named by basename or dotted path."""
    if name in ambiguous_funcs and modname:
        slug = c_mod_slug(modname.split(".")[-1])
        return "%s__%s" % (slug, cname(name))
    return cname(name)


# ==========================================================================
# Translation-time regex: a small `re` subset compiled straight to C.
#
# Each static pattern from `re.compile("...")` (or `re.search("...", s)`) is
# parsed here and lowered to a specialized C matcher function, so the dynamic
# `re` engine is never needed. A compiled pattern is represented at runtime as
# an integer id (OBJ_INT); `pat.search(text)` dispatches by id and returns a
# match as an obj LIST of captured strings ([whole, g1, g2, ...]) -- which is
# truthy when matched (None when not), so `if m:` and `m.group(n)` (->
# index_obj) work with no new runtime types.
#
# Supported subset (anything else returns None -> caller falls back, so we
# never emit a silently-wrong matcher): literals and escaped literals, the
# anchors ^ and $, the classes \d \w \s \D \W \S and `.`, character classes
# [...] (with ranges and a leading-^ negation), capturing groups (...), and
# AT MOST ONE quantifier (+ * ?) in the whole pattern. Rejected: alternation
# |, non-capturing/named groups (?...), backreferences, {m,n}, \b, lookaround,
# and more than one quantifier.
# ==========================================================================

REGEX_HELPER = r"""/* ---- rpython translation-time regex: support helpers ---- */
static obj _re_slice(char* t, long a, long b) {
    if (a < 0 || b < a) return OBJ_NONE;
    long n = b - a;
    char* s = (char*)aalloc((size_t)n + 1);
    for (long i = 0; i < n; i++) s[i] = t[a + i];
    s[n] = 0;
    return OBJ_STR(s);
}

/* Span accessors over the match layout [g0..gn, s0,e0, ..., sn,en]: the group
 * count is 3*(ng+1)/3, so the spans begin at pylen/3. */
static long _re_span(obj m, long g, int which) {
    long n3 = pylen(m) / 3;
    long at = n3 + 2 * g + which;
    if (g < 0 || at >= pylen(m)) return -1;
    return AS_INT(index_obj(m, at));
}

/* re.escape: backslash every character that is not [A-Za-z0-9_]. CPython
 * leaves those alone and escapes the rest, which is what callers building a
 * pattern out of an identifier rely on. Pure string work, no engine needed. */
static obj _re_escape(char* t) {
    if (!t) return OBJ_NONE;
    long n = (long)strlen(t), i, k = 0;
    char* s = (char*)aalloc((size_t)n * 2 + 1);
    for (i = 0; i < n; i++) {
        unsigned char c = (unsigned char)t[i];
        int plain = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
                 || (c >= '0' && c <= '9') || c == '_';
        if (!plain) s[k++] = '\\';
        s[k++] = (char)c;
    }
    s[k] = 0;
    return OBJ_STR(s);
}
"""


SUBPROC_PRELUDE = r"""/* ---- rpython subprocess.run / tempfile.mkdtemp shim ---- */
#include <unistd.h>
#include <sys/wait.h>
#include <poll.h>
#include <fcntl.h>

/* Grow-on-demand byte buffer for a child's output. */
typedef struct { char* p; long n, cap; } _sp_buf;

static void _sp_push(_sp_buf* b, const char* src, long n) {
    if (b->n + n + 1 > b->cap) {
        long want = (b->cap ? b->cap * 2 : 4096);
        while (want < b->n + n + 1) want *= 2;
        char* np = (char*)aalloc((size_t)want);
        for (long i = 0; i < b->n; i++) np[i] = b->p[i];
        b->p = np; b->cap = want;
    }
    for (long i = 0; i < n; i++) b->p[b->n + i] = src[i];
    b->n += n;
    b->p[b->n] = 0;
}

/* subprocess.run(argv, capture_output=, text=, cwd=).
 * Returns a LIST [returncode, stdout, stderr] -- the same trick the regex
 * matcher uses, so .returncode/.stdout/.stderr lower to a plain index and no
 * new runtime type is needed. */
static obj _sp_run(obj argv, char* cwd, int capture) {
    long n = pylen(argv), i;
    char** av = (char**)aalloc((size_t)(n + 1) * sizeof(char*));
    int op[2], ep[2];
    pid_t pid;
    _sp_buf ob, eb;
    obj res;
    int status = 0;

    ob.p = 0; ob.n = 0; ob.cap = 0;
    eb.p = 0; eb.n = 0; eb.cap = 0;
    for (i = 0; i < n; i++) av[i] = AS_STR(index_obj(argv, i));
    av[n] = 0;
    if (n == 0) return OBJ_NONE;

    if (capture) {
        if (pipe(op) != 0) return OBJ_NONE;
        if (pipe(ep) != 0) { close(op[0]); close(op[1]); return OBJ_NONE; }
    }
    pid = fork();
    if (pid < 0) {
        if (capture) { close(op[0]); close(op[1]); close(ep[0]); close(ep[1]); }
        return OBJ_NONE;
    }
    if (pid == 0) {
        if (cwd && cwd[0]) { if (chdir(cwd) != 0) _exit(127); }
        if (capture) {
            dup2(op[1], 1); dup2(ep[1], 2);
            close(op[0]); close(op[1]); close(ep[0]); close(ep[1]);
        }
        execvp(av[0], av);
        _exit(127);
    }
    if (capture) {
        struct pollfd pf[2];
        int open_n = 2;
        char tmp[4096];
        close(op[1]); close(ep[1]);
        pf[0].fd = op[0]; pf[0].events = POLLIN;
        pf[1].fd = ep[0]; pf[1].events = POLLIN;
        /* Both pipes are drained together. Reading one to EOF first would
         * deadlock as soon as the child filled the other. */
        while (open_n > 0) {
            int k;
            if (poll(pf, 2, -1) < 0) break;
            for (k = 0; k < 2; k++) {
                if (pf[k].fd < 0) continue;
                if (pf[k].revents & (POLLIN | POLLHUP)) {
                    long got = (long)read(pf[k].fd, tmp, sizeof tmp);
                    if (got > 0) _sp_push(k == 0 ? &ob : &eb, tmp, got);
                    else { close(pf[k].fd); pf[k].fd = -1; open_n--; }
                }
            }
        }
    }
    while (waitpid(pid, &status, 0) < 0) { }

    res = list_new();
    list_append(res, OBJ_INT(WIFEXITED(status) ? WEXITSTATUS(status)
                                               : 128 + WTERMSIG(status)));
    list_append(res, OBJ_STR(ob.p ? ob.p : ""));
    list_append(res, OBJ_STR(eb.p ? eb.p : ""));
    return res;
}

/* tempfile.mkdtemp(prefix=) -- a real mkdtemp under $TMPDIR (default /tmp). */
static char* _tf_mkdtemp(char* prefix) {
    const char* base = getenv("TMPDIR");
    char* tmpl;
    long bl, pl;
    if (!base || !base[0]) base = "/tmp";
    if (!prefix) prefix = "tmp";
    bl = (long)strlen(base);
    pl = (long)strlen(prefix);
    tmpl = (char*)aalloc((size_t)(bl + pl + 10));
    { long i, k = 0;
      for (i = 0; i < bl; i++) tmpl[k++] = base[i];
      if (k == 0 || tmpl[k-1] != '/') tmpl[k++] = '/';
      for (i = 0; i < pl; i++) tmpl[k++] = prefix[i];
      for (i = 0; i < 6; i++) tmpl[k++] = 'X';
      tmpl[k] = 0; }
    if (!mkdtemp(tmpl)) return "";
    return tmpl;
}
"""


OS_SYS_PRELUDE = r"""/* ---- rpython os.path / sys shim ---- */
#include <unistd.h>
#include <sys/stat.h>
static char* _ospath_dirname(char* p) {
    long n = (long)strlen(p), i;
    for (i = n - 1; i >= 0; i--) if (p[i] == '/') break;
    if (i < 0) return "";
    if (i == 0) return "/";
    char* s = (char*)aalloc((size_t)i + 1);
    for (long k = 0; k < i; k++) s[k] = p[k];
    s[i] = 0; return s;
}
static char* _ospath_basename(char* p) {
    long n = (long)strlen(p), i;
    for (i = n - 1; i >= 0; i--) if (p[i] == '/') break;
    return p + i + 1;
}
static char* _ospath_join(char* a, char* b) {
    if (b[0] == '/') return b;
    long la = (long)strlen(a); if (la == 0) return b;
    long lb = (long)strlen(b); int sep = (a[la - 1] != '/');
    char* s = (char*)aalloc((size_t)la + lb + 2); long k = 0;
    for (long i = 0; i < la; i++) s[k++] = a[i];
    if (sep) s[k++] = '/';
    for (long i = 0; i < lb; i++) s[k++] = b[i];
    s[k] = 0; return s;
}
/* os.path.normpath: collapse '.', '..' and repeated slashes, textually --
   the same lexical rule CPython uses, with no filesystem lookup, so a
   symlink is not resolved (that is realpath's job, not this one). A leading
   '/' is kept; an empty result is '.'. */
static char* _ospath_normpath(char* p) {
    long n = (long)strlen(p), i = 0, k = 0, nseg = 0, j;
    int absolute = (n > 0 && p[0] == '/');
    char* out;
    long* starts; long* lens;
    if (n == 0) return (char*)".";
    starts = (long*)aalloc(sizeof(long) * (size_t)(n + 1));
    lens = (long*)aalloc(sizeof(long) * (size_t)(n + 1));
    while (i < n) {
        long b;
        while (i < n && p[i] == '/') i++;
        b = i;
        while (i < n && p[i] != '/') i++;
        if (i == b) break;
        if (i - b == 1 && p[b] == '.') continue;
        if (i - b == 2 && p[b] == '.' && p[b + 1] == '.') {
            /* '..' pops a segment, except past the root or past leading
               '..' in a relative path, which have nothing to pop. */
            if (nseg > 0 && !(lens[nseg - 1] == 2 &&
                              p[starts[nseg - 1]] == '.' &&
                              p[starts[nseg - 1] + 1] == '.')) {
                nseg--; continue;
            }
            if (absolute) continue;
        }
        starts[nseg] = b; lens[nseg] = i - b; nseg++;
    }
    out = (char*)aalloc((size_t)n + 2);
    if (absolute) out[k++] = '/';
    for (j = 0; j < nseg; j++) {
        long q;
        if (j) out[k++] = '/';
        for (q = 0; q < lens[j]; q++) out[k++] = p[starts[j] + q];
    }
    out[k] = 0;
    if (k == 0) { out[0] = '.'; out[1] = 0; }
    return out;
}
/* os.path.realpath: resolve symlinks through libc. libc's realpath(3)
   requires the path to exist and returns NULL otherwise, where CPython's
   falls back to a purely lexical answer -- so that is what happens here
   too, via normpath(abspath(p)). Without the fallback a non-existent path
   would come back NULL and be read as a string. */
static char* _ospath_abspath(char* p);
static char* _ospath_normpath(char* p);
static char* _ospath_realpath(char* p) {
    char _b[4096];
    if (realpath(p, _b)) {
        size_t n = strlen(_b);
        char* s = (char*)aalloc(n + 1);
        for (size_t i = 0; i <= n; i++) s[i] = _b[i];
        return s;
    }
    return _ospath_normpath(_ospath_abspath(p));
}
/* os.getcwd(): arena-allocated so the result outlives the call, unlike a
   static buffer that the next call would overwrite. */
static char* _os_getcwd(void) {
    char _b[4096];
    if (!getcwd(_b, sizeof _b)) return (char*)".";
    size_t n = strlen(_b);
    char* s = (char*)aalloc(n + 1);
    for (size_t i = 0; i <= n; i++) s[i] = _b[i];
    return s;
}
static char* _ospath_abspath(char* p) {
    if (p[0] == '/') return p;
    char _cwd[4096]; if (!getcwd(_cwd, sizeof _cwd)) return p;
    return _ospath_join(_cwd, p);
}
static int _ospath_exists(char* p) { return access(p, 0) == 0; }
/* os.path.isfile / isdir: `access` cannot tell them apart, so this stats. */
static int _ospath_isfile(char* p) {
    struct stat _st;
    return stat(p, &_st) == 0 && S_ISREG(_st.st_mode);
}
static int _ospath_isdir(char* p) {
    struct stat _st;
    return stat(p, &_st) == 0 && S_ISDIR(_st.st_mode);
}
/* SHA-256, for `hashlib.sha256(x).hexdigest()`. Implemented rather than
   stubbed because the one caller in this codebase uses it for *cache keys*:
   a constant digest would silently make every module share one cache entry,
   which is worse than a compile error. FIPS 180-4, tested against known
   digests. */
static const unsigned int _sha_k[64] = {
0x428a2f98u,0x71374491u,0xb5c0fbcfu,0xe9b5dba5u,0x3956c25bu,0x59f111f1u,
0x923f82a4u,0xab1c5ed5u,0xd807aa98u,0x12835b01u,0x243185beu,0x550c7dc3u,
0x72be5d74u,0x80deb1feu,0x9bdc06a7u,0xc19bf174u,0xe49b69c1u,0xefbe4786u,
0x0fc19dc6u,0x240ca1ccu,0x2de92c6fu,0x4a7484aau,0x5cb0a9dcu,0x76f988dau,
0x983e5152u,0xa831c66du,0xb00327c8u,0xbf597fc7u,0xc6e00bf3u,0xd5a79147u,
0x06ca6351u,0x14292967u,0x27b70a85u,0x2e1b2138u,0x4d2c6dfcu,0x53380d13u,
0x650a7354u,0x766a0abbu,0x81c2c92eu,0x92722c85u,0xa2bfe8a1u,0xa81a664bu,
0xc24b8b70u,0xc76c51a3u,0xd192e819u,0xd6990624u,0xf40e3585u,0x106aa070u,
0x19a4c116u,0x1e376c08u,0x2748774cu,0x34b0bcb5u,0x391c0cb3u,0x4ed8aa4au,
0x5b9cca4fu,0x682e6ff3u,0x748f82eeu,0x78a5636fu,0x84c87814u,0x8cc70208u,
0x90befffau,0xa4506cebu,0xbef9a3f7u,0xc67178f2u};
#define _SHR(x,n) ((x) >> (n))
#define _ROR(x,n) (((x) >> (n)) | ((x) << (32 - (n))))
static void _sha256_block(unsigned int* h, const unsigned char* p) {
    unsigned int w[64], a,b,c,d,e,f,g,hh,t1,t2; int i;
    for (i = 0; i < 16; i++)
        w[i] = ((unsigned int)p[i*4] << 24) | ((unsigned int)p[i*4+1] << 16)
             | ((unsigned int)p[i*4+2] << 8) | (unsigned int)p[i*4+3];
    for (i = 16; i < 64; i++) {
        unsigned int s0 = _ROR(w[i-15],7) ^ _ROR(w[i-15],18) ^ _SHR(w[i-15],3);
        unsigned int s1 = _ROR(w[i-2],17) ^ _ROR(w[i-2],19) ^ _SHR(w[i-2],10);
        w[i] = w[i-16] + s0 + w[i-7] + s1;
    }
    a=h[0];b=h[1];c=h[2];d=h[3];e=h[4];f=h[5];g=h[6];hh=h[7];
    for (i = 0; i < 64; i++) {
        unsigned int S1 = _ROR(e,6) ^ _ROR(e,11) ^ _ROR(e,25);
        unsigned int ch = (e & f) ^ ((~e) & g);
        unsigned int S0 = _ROR(a,2) ^ _ROR(a,13) ^ _ROR(a,22);
        unsigned int mj = (a & b) ^ (a & c) ^ (b & c);
        t1 = hh + S1 + ch + _sha_k[i] + w[i];
        t2 = S0 + mj;
        hh=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
    }
    h[0]+=a;h[1]+=b;h[2]+=c;h[3]+=d;h[4]+=e;h[5]+=f;h[6]+=g;h[7]+=hh;
}
static char* _sha256_hex(char* msg) {
    unsigned int h[8] = {0x6a09e667u,0xbb67ae85u,0x3c6ef372u,0xa54ff53au,
                         0x510e527fu,0x9b05688cu,0x1f83d9abu,0x5be0cd19u};
    unsigned long n = msg ? (unsigned long)strlen(msg) : 0UL;
    unsigned long i;
    unsigned char blk[64];
    for (i = 0; i + 64 <= n; i += 64)
        _sha256_block(h, (const unsigned char*)msg + i);
    unsigned long rem = n - i;
    for (unsigned long j = 0; j < rem; j++) blk[j] = (unsigned char)msg[i+j];
    blk[rem] = 0x80;
    if (rem >= 56) {
        for (unsigned long j = rem + 1; j < 64; j++) blk[j] = 0;
        _sha256_block(h, blk);
        for (unsigned long j = 0; j < 56; j++) blk[j] = 0;
    } else {
        for (unsigned long j = rem + 1; j < 56; j++) blk[j] = 0;
    }
    unsigned long bits = n * 8UL;
    for (int j = 0; j < 8; j++)
        blk[56 + j] = (unsigned char)((bits >> (56 - 8*j)) & 0xFFUL);
    _sha256_block(h, blk);
    char* out = malloc(65);
    for (int j = 0; j < 8; j++) {
        static const char* hx = "0123456789abcdef";
        for (int k = 0; k < 8; k++)
            out[j*8+k] = hx[(h[j] >> (28 - 4*k)) & 0xFu];
    }
    out[64] = 0;
    return out;
}
/* os.path.getmtime: seconds since the epoch, or -1 if the path is gone. The
   real one raises OSError; returning -1 keeps the common "is A newer than B"
   comparison working, since a missing file compares older than anything. */
/* os.path.splitext: returns (root, ext) as a two-element list so `[0]` and
   `[1]` both work. The extension keeps its leading dot, as Python's does. */
static obj _ospath_splitext(char* p) {
    long n = p ? (long)strlen(p) : 0;
    long dot = -1, i;
    for (i = n - 1; i >= 0; i--) {
        if (p[i] == '/') break;
        if (p[i] == '.' && dot < 0) dot = i;
    }
    /* A leading dot is part of the name, not an extension: ".bashrc". */
    long sl = -1;
    for (i = n - 1; i >= 0; i--) { if (p[i] == '/') { sl = i; break; } }
    if (dot <= sl + 1) dot = -1;
    char* root; char* ext;
    if (dot < 0) {
        root = malloc((unsigned long)n + 1);
        memcpy(root, p, (unsigned long)n); root[n] = 0;
        ext = malloc(1); ext[0] = 0;
    } else {
        root = malloc((unsigned long)dot + 1);
        memcpy(root, p, (unsigned long)dot); root[dot] = 0;
        ext = malloc((unsigned long)(n - dot) + 1);
        memcpy(ext, p + dot, (unsigned long)(n - dot)); ext[n - dot] = 0;
    }
    obj l = list_new();
    list_append(l, OBJ_STR(root));
    list_append(l, OBJ_STR(ext));
    return l;
}
static long _ospath_getmtime(char* p) {
    struct stat _st;
    if (stat(p, &_st) != 0) return -1;
    return (long)_st.st_mtime;
}
static obj _os_makedirs(char* p) {
    char b[4096]; long n = (long)strlen(p); if (n >= 4096) return OBJ_NONE;
    for (long i = 0; i <= n; i++) {
        b[i] = p[i];
        if ((p[i] == '/' || p[i] == 0) && i > 0) {
            char c = b[i]; b[i] = 0; mkdir(b, 0777); b[i] = c;
        }
    }
    return OBJ_NONE;
}
static obj _os_unlink(char* p) { unlink(p); return OBJ_NONE; }
static obj _sys_path_get(void) {
    static obj _sp; static int _init = 0;
    if (!_init) { _sp = list_new(); _init = 1; }
    return _sp;
}
"""


STRUCT_PRELUDE = r"""/* ---- rpython struct.pack/unpack subset: <f <d <I <Q ----
   A packed value is represented as an obj holding the raw little-endian bit
   pattern (OBJ_INT); pack does any float->bits conversion, unpack reads it back
   per the format. Covers exactly the IEEE-754 reinterpretation asm_gen needs. */
static obj _struct_pack(char* fmt, double val) {
    char c = (fmt[0] == '<' || fmt[0] == '>' || fmt[0] == '=') ? fmt[1] : fmt[0];
    if (c == 'f') { union { float f; unsigned int u; } x; x.f = (float)val;
                    return OBJ_INT((long)x.u); }
    if (c == 'd') { union { double d; unsigned long long u; } x; x.d = val;
                    return OBJ_INT((long)x.u); }
    if (c == 'I' || c == 'L') return OBJ_INT((long)(unsigned int)val);
    if (c == 'Q') return OBJ_INT((long)(unsigned long long)val);
    return OBJ_INT((long)val);
}
static obj _struct_unpack(char* fmt, obj packed) {
    char c = (fmt[0] == '<' || fmt[0] == '>' || fmt[0] == '=') ? fmt[1] : fmt[0];
    long bits = AS_INT(packed);
    obj r = list_new();
    if (c == 'I' || c == 'L') list_append(r, OBJ_INT((long)(unsigned int)bits));
    else if (c == 'Q') list_append(r, OBJ_INT(bits));
    else if (c == 'f') { union { unsigned int u; float f; } x;
                         x.u = (unsigned int)bits; list_append(r, OBJ_FLOAT(x.f)); }
    else if (c == 'd') { union { unsigned long long u; double d; } x;
                         x.u = (unsigned long long)bits; list_append(r, OBJ_FLOAT(x.d)); }
    else list_append(r, OBJ_INT(bits));
    return r;
}
"""


def c_char_literal(ch):
    """A C char constant for a single character."""
    if ch == "\\":
        return "'\\\\'"
    if ch == "'":
        return "'\\''"
    if ch == "\n":
        return "'\\n'"
    if ch == "\t":
        return "'\\t'"
    if ch == "\r":
        return "'\\r'"
    o = ord(ch)
    if 32 <= o < 127:
        return "'%s'" % ch
    return "%d" % o


def _re_class_test(atom, var):
    """C boolean expr testing whether char `var` matches a single atom."""
    t = atom["type"]
    if t == "lit":
        return "%s == %s" % (var, c_char_literal(atom["ch"]))
    if t == "digit":
        return "(%s >= '0' && %s <= '9')" % (var, var)
    if t == "ndigit":
        return "!(%s >= '0' && %s <= '9')" % (var, var)
    if t == "word":
        return ("((%s>='a'&&%s<='z')||(%s>='A'&&%s<='Z')||"
                "(%s>='0'&&%s<='9')||%s=='_')" % ((var,) * 7))
    if t == "nword":
        return ("!((%s>='a'&&%s<='z')||(%s>='A'&&%s<='Z')||"
                "(%s>='0'&&%s<='9')||%s=='_')" % ((var,) * 7))
    if t == "space":
        return ("(%s==' '||%s=='\\t'||%s=='\\n'||%s=='\\r'||"
                "%s=='\\f'||%s=='\\v')" % ((var,) * 6))
    if t == "nspace":
        return ("!(%s==' '||%s=='\\t'||%s=='\\n'||%s=='\\r'||"
                "%s=='\\f'||%s=='\\v')" % ((var,) * 6))
    if t == "dot":
        return "%s != '\\n'" % var
    if t == "class":
        neg, items = atom["cls"]
        terms = []
        for it in items:
            if isinstance(it, tuple):
                terms.append("(%s >= %s && %s <= %s)" % (
                    var, c_char_literal(it[0]), var, c_char_literal(it[1])))
            else:
                terms.append("%s == %s" % (var, c_char_literal(it)))
        inner = " || ".join(terms) if terms else "0"
        return "!(%s)" % inner if neg else "(%s)" % inner
    return "0"


def regex_parse(pattern):
    """Parse `pattern` into the supported subset, or return None.

    Returns dict(start_anchor, end_anchor, ngroups, atoms) where each atom is
    dict(type, ch?/cls?, quant, gopen, gclose). Rejects anything outside the
    documented subset by returning None.
    """
    i, n = 0, len(pattern)
    start_anchor = False
    end_anchor = False
    if n and pattern[0] == "^":
        start_anchor = True
        i = 1
    atoms = []
    ngroups = 0
    open_stack = []          # group indices currently open
    pending_open = []        # opens to attach to the next atom
    nquant = 0

    while i < n:
        c = pattern[i]
        if c == "$" and i == n - 1:
            end_anchor = True
            i += 1
            break
        if c == "|":
            return None                      # alternation unsupported
        if c == "(":
            if pattern[i:i + 2] == "(?":
                return None                  # non-capturing / named / lookaround
            ngroups += 1
            open_stack.append(ngroups)
            pending_open.append(ngroups)
            i += 1
            continue
        if c == ")":
            if not open_stack or not atoms:
                return None
            g = open_stack.pop()
            atoms[-1].setdefault("gclose", []).append(g)
            i += 1
            continue
        if c == "{":
            return None                      # counted repetition unsupported
        # build one atom
        atom = {"quant": "", "gopen": [], "gclose": []}
        if c == "\\":
            if i + 1 >= n:
                return None
            e = pattern[i + 1]
            mapping = {"d": "digit", "D": "ndigit", "w": "word", "W": "nword",
                       "s": "space", "S": "nspace"}
            if e in mapping:
                atom["type"] = mapping[e]
            elif e == "b" or e.isdigit():
                return None                  # word boundary / backref unsupported
            else:
                atom["type"] = "lit"
                atom["ch"] = e               # escaped literal (\. \( \\ ...)
            i += 2
        elif c == ".":
            atom["type"] = "dot"
            i += 1
        elif c == "[":
            j = i + 1
            neg = False
            if j < n and pattern[j] == "^":
                neg = True
                j += 1
            items = []
            if j < n and pattern[j] == "]":      # literal ] as first member
                items.append("]")
                j += 1
            while j < n and pattern[j] != "]":
                ch = pattern[j]
                if ch == "\\" and j + 1 < n:
                    ch = pattern[j + 1]
                    j += 1
                if j + 2 < n and pattern[j + 1] == "-" and pattern[j + 2] != "]":
                    items.append((ch, pattern[j + 2]))
                    j += 3
                else:
                    items.append(ch)
                    j += 1
            if j >= n:
                return None                  # unterminated class
            atom["type"] = "class"
            atom["cls"] = (neg, items)
            i = j + 1
        else:
            atom["type"] = "lit"
            atom["ch"] = c
            i += 1
        # optional quantifier
        if i < n and pattern[i] in "+*?":
            atom["quant"] = pattern[i]
            nquant += 1
            i += 1
        atom["gopen"] = list(pending_open)
        pending_open.clear()
        atoms.append(atom)

    if open_stack or pending_open:
        return None                          # unbalanced groups
    if i < n:
        return None                          # leftover (e.g. a stray $ midway)
    if nquant > 1:
        return None                          # at most one quantifier supported
    return {"start_anchor": start_anchor, "end_anchor": end_anchor,
            "ngroups": ngroups, "atoms": atoms}


def _re_emit_build(pid, ng, indent):
    """Build the match value: the captured strings, then their spans.

    Layout is [g0..gn, s0,e0, s1,e1, ..., sn,en] -- length always 3*(ng+1), so
    a consumer recovers the group count from the length alone. Keeping the
    strings first means `.group(i)` stays a plain index into the list, while
    `.start(i)`/`.end(i)` (130 call sites in cpprust.py alone) become an index
    at len/3 + 2i. Appending rather than restructuring is what makes this
    backwards compatible with every existing consumer.
    """
    parts = ["%s{ obj _m = list_new();" % indent]
    parts.append("%s  list_append(_m, _re_slice(_t, _g0s, _g0e));" % indent)
    for k in range(1, ng + 1):
        parts.append("%s  list_append(_m, _re_slice(_t, _g%ds, _g%de));"
                     % (indent, k, k))
    parts.append("%s  list_append(_m, OBJ_INT(_g0s));" % indent)
    parts.append("%s  list_append(_m, OBJ_INT(_g0e));" % indent)
    for k in range(1, ng + 1):
        parts.append("%s  list_append(_m, OBJ_INT(_g%ds));" % (indent, k))
        parts.append("%s  list_append(_m, OBJ_INT(_g%de));" % (indent, k))
    parts.append("%s  return _m; }" % indent)
    return "\n".join(parts)


def regex_emit_c(pid, parsed):
    """Emit `static obj _re_p<pid>(char* _t, int _anchored)` for `parsed`."""
    atoms = parsed["atoms"]
    ng = parsed["ngroups"]
    out = []
    a = out.append
    a("static obj _re_p%d(char* _t, int _anchored) {" % pid)
    a("    if (!_t) return OBJ_NONE;")
    a("    long _L = (long)strlen(_t);")
    gvars = ["_g0s", "_g0e"] + sum(
        ([("_g%ds" % k), ("_g%de" % k)] for k in range(1, ng + 1)), [])
    a("    long %s;" % ", ".join(gvars))
    a("    for (long _s = 0; _s <= _L; _s++) {")
    a("        long p = _s; _g0s = _s;")
    for k in range(1, ng + 1):
        a("        _g%ds = -1; _g%de = -1;" % (k, k))
    fail = "_fail%d" % pid

    qidx = next((idx for idx, at in enumerate(atoms) if at["quant"]), None)

    def emit_opens(at, indent):
        for g in at.get("gopen", []):
            a("%s_g%ds = p;" % (indent, g))

    def emit_closes(at, indent):
        for g in at.get("gclose", []):
            a("%s_g%de = p;" % (indent, g))

    def emit_fixed(at, indent, failgoto):
        emit_opens(at, indent)
        a("%sif (p >= _L || !(%s)) goto %s;" % (
            indent, _re_class_test(at, "_t[p]"), failgoto))
        a("%sp++;" % indent)
        emit_closes(at, indent)

    if qidx is None:
        for at in atoms:
            emit_fixed(at, "        ", fail)
        if parsed["end_anchor"]:
            a("        if (p != _L) goto %s;" % fail)
        a("        _g0e = p;")
        a(_re_emit_build(pid, ng, "        "))
        a("      %s:;" % fail)
    else:
        for at in atoms[:qidx]:
            emit_fixed(at, "        ", fail)
        qat = atoms[qidx]
        emit_opens(qat, "        ")
        a("        { long _qs = p;")
        a("          while (p < _L && (%s)) p++;" % _re_class_test(qat, "_t[p]"))
        q = qat["quant"]
        if q == "?":
            a("          long _qmax = (p > _qs + 1) ? _qs + 1 : p;")
        else:
            a("          long _qmax = p;")
        qmin = "_qs + 1" if q == "+" else "_qs"
        a("          for (long _q = _qmax; _q >= %s; _q--) {" % qmin)
        a("            p = _q;")
        for g in qat.get("gclose", []):
            a("            _g%de = p;" % g)
        bt = "_bt%d" % pid
        for at in atoms[qidx + 1:]:
            emit_fixed(at, "            ", bt)
        if parsed["end_anchor"]:
            a("            if (p != _L) goto %s;" % bt)
        a("            _g0e = p;")
        a(_re_emit_build(pid, ng, "            "))
        a("          %s:;" % bt)
        a("          } }")
        a("        goto %s;" % fail)
        a("      %s:;" % fail)

    a("        if (_anchored) break;")
    if parsed["start_anchor"]:
        a("        break;")                 # ^ anchors the search to position 0
    a("    }")
    a("    return OBJ_NONE;")
    a("}")
    return "\n".join(out)


class Transpiler:
    def __init__(self, modname, base_dir=None, stdlib_root=None,
                 py_modname=None, pod_classes=True):
        self.modname = modname
        self.cmod = c_mod_slug(modname)   # C-safe form (e.g. for _init)
        self.py_modname = py_modname      # dotted module name (stdlib only)
        self.base_dir = base_dir    # repo dir containing the shivyc/ package
        self.stdlib_root = os.fspath(stdlib_root) if stdlib_root else None
        self._pod_enabled = pod_classes   # POD class lowering (rpython only)
        self._typed_lists = set()         # scalar element ctypes needing a list type
        self._typed_dicts = set()         # (key_ct, val_ct) needing a dict type
        self._tdict_by_name = {}          # _tdict_K_V -> (key_ct, val_ct)
        self.stdlib_index = build_stdlib_index(self.stdlib_root) \
            if self.stdlib_root else {}
        self.lines = []
        self.cur_class = None
        self.modules = set()
        self._regex_ids = {}        # pattern string -> matcher id (per module)
        self._regex_parsed = {}     # id -> parsed pattern struct (tier 1)
        self._regex_vm = {}         # id -> pattern string (tier 2: crust_re VM)
        self._regex_dyn = False     # a runtime-valued pattern needs the VM
        self._regex_used = False    # module does regex at all (see _prescan)
        self._regex_escape = False  # re.escape used (needs no engine)
        self._subproc_used = False  # subprocess.run / tempfile.mkdtemp shim
        self._subproc_vars = set()  # names bound to a subprocess.run result
        self._regex_dyn_vars = set()  # names bound to re.compile(<runtime expr>)
        self._regex_var_pat = {}    # name -> pattern text, for constant compiles
        self._regex_vars = set()    # names bound to re.compile(const) (a matcher id)
        self._regex_match_vars = set()  # names bound to a .search()/.match() result
        self._regex_list_vars = set()   # names bound to a list of re.compile(const)
        self._ossys_used = False    # os.path/sys shim referenced
        self._struct_used = False    # struct.pack/unpack shim referenced
        self._json_used = False     # typed-json lexer prelude referenced
        self._json_decoders = []    # ClassInfos needing a generated decoder
        self._json_hook_class = {}  # var name -> ClassInfo for a hoisted decoder
        self.import_alias = {}      # alias -> full dotted module name
        self.from_imports = {}      # imported name -> full dotted module name
        self.star_import_mods = []  # modules imported via `from X import *`
        self.mod_global_types = {}  # module global name -> ctype
        self.used_imports = set()   # (modname, name) actually referenced
        self.mod_const_types = {}
        self.func_nodes = {}
        self.singletons = []
        self.singleton_names = {}   # var -> ClassName (module-level instances)
        self.str_sets = {}          # module-level set/list of string literals
        self.func_returns = {}      # top-level function name -> return ctype
        self.func_values_needed = set()  # functions used as first-class values
        self.class_values_needed = set()  # classes used as constructor values
        self.func_params = {}       # top-level function name -> [param ctypes]
        self.scope = {}             # local/param name -> ctype (per function)
        self.narrowed = {}          # name -> ctype, active in an isinstance block
        self.hoisted = set()        # locals declared at function top
        self.cur_ret = OBJ          # current function's return ctype
        self.loop_n = 0             # unique-id counter for generated loops
        self.exc_n = 0              # unique-id counter for try/except frames
        self.cm_n = 0               # unique-id counter for inlined contextmanagers
        self.try_stack = []         # open try/loop scopes for return/break/continue cleanup
        self.indent = 0

    def emit(self, line=""):
        self.lines.append(("    " * self.indent + line) if line else "")

    def _fs(self):
        """File-local linkage (unused; stdlib uses module-qualified symbols)."""
        return ""

    def _msym(self, name):
        """Module-qualified C symbol for stdlib shared-library linkage."""
        if self.stdlib_root:
            return "%s__%s" % (self.cmod, cname(name))
        return cname(name)

    def fnsym(self, name):
        """C symbol for a module-level function."""
        if self.stdlib_root and name in self.func_nodes:
            return "%s__%s" % (self.cmod, cname(name))
        if name in self.ambiguous_funcs and name in self.func_nodes:
            # Defined in several modules under the same name (cross-module API
            # like `optimize`): module-qualify to avoid a link-time collision.
            return func_csym(name, self.modname, self.ambiguous_funcs)
        return cname(name)

    def pname(self, name):
        """C parameter name: avoid typedef / keyword / imported-global collisions."""
        if name in C_KEYWORDS or name in RUNTIME_API_NAMES \
                or name in self.class_typedef_names \
                or name in getattr(self, "_shadow_names", ()):
            return name + "_"
        return name

    def lid(self, name):
        """C identifier for a local/param in the current function."""
        if name in self.scope:
            return self.pname(name)
        return cname(name)

    # ---- driver ----------------------------------------------------------

    # ---- object-model report (--show-object-model) ----------------------
    _SCALAR_CTYPES = {"int", "long", "short", "char", "char*", "str",
                      "bool", "double", "float"}

    def _objmodel_kind(self, ctype):
        """Return ('obj'|'POD', field-tag-code) for a field/var C type."""
        code = self._field_tc(ctype)
        return ("obj" if code in ("o", "p") else "POD"), code

    def object_model_report(self, tree):
        """A human-readable table of how every class field and function
        parameter was typed: as a plain C scalar/struct (POD, copied by value)
        or as the boxed object model (arena-allocated, Obj header + vtable).

        This makes the translator's type-inference decisions visible, which is
        the quickest way to spot a value that was meant to be an object but got
        inferred as a scalar (or vice versa) before it crashes at runtime."""
        L = []
        order = list(self.class_order)
        pod = [ci for ci in order if ci.csym in self._pod_set]
        obj = [ci for ci in order if ci.csym not in self._pod_set]
        L.append("=" * 72)
        L.append("OBJECT MODEL  --  %s" % (self.src_name or self.modname))
        L.append("  POD     plain C struct/scalar: no header, copied by value")
        L.append("  object  boxed: arena-allocated, Obj header + per-class vtable")
        L.append("  tag     i/l int  b bool  d/f float  s char*  o obj  p ptr")
        L.append("-" * 72)
        L.append("classes: %d total  (%d POD struct, %d object-model)"
                 % (len(order), len(pod), len(obj)))
        for ci in order:
            is_pod = ci.csym in self._pod_set
            base = (" : %s" % ci.base_name) if ci.base_name else ""
            L.append("")
            L.append("  class %s%s   [%s]"
                     % (ci.name, base, "POD struct" if is_pod else "object-model"))
            ff = ci.full_fields()
            if not ff:
                L.append("      (no fields)")
            for fn, ft in ff:
                kind, code = self._objmodel_kind(ft)
                L.append("      %-22s %-10s %s   %s" % (fn, ft, code, kind))
        funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        if funcs:
            L.append("")
            L.append("-" * 72)
            L.append("functions: parameter typing  (name:ctype [kind/source])")
            for fn in sorted(funcs, key=_fdef_name):
                params = []
                for a in (list(getattr(fn.args, "posonlyargs", [])) +
                          list(fn.args.args)):
                    if a.arg == "self":
                        continue
                    t = (ann_to_ctype(a.annotation)
                         or infer_from_name(a.arg) or OBJ)
                    kind, _ = self._objmodel_kind(t)
                    src = ("ann" if a.annotation is not None
                           else ("name" if infer_from_name(a.arg) else "obj"))
                    params.append("%s:%s[%s/%s]" % (a.arg, t, kind, src))
                if params:
                    L.append("  %-24s %s" % (fn.name, "  ".join(params)))
        nfields = sum(len(ci.full_fields()) for ci in order)
        nobj = sum(1 for ci in order for _, ft in ci.full_fields()
                   if self._field_tc(ft) in ("o", "p"))
        # REVIEW: every field/param given a narrow C scalar SOLELY because its
        # name matched the int/str/bool/float heuristic. These are exactly the
        # spots where an actual object value would be silently truncated to a
        # C scalar -- the dominant self-hosting bug class. Legitimate scalars
        # show up here too; the point is to make every name-based guess visible
        # so a human can confirm each one really is POD.
        review = []
        for ci in order:
            for fn, ft in ci.full_fields():
                if ft in self._SCALAR_CTYPES and infer_from_name(fn) == ft:
                    review.append("field  %s.%s -> %s" % (ci.name, fn, ft))
        for fn in sorted((n for n in ast.walk(tree)
                          if isinstance(n, ast.FunctionDef)),
                         key=_fdef_name):
            for a in (list(getattr(fn.args, "posonlyargs", [])) +
                      list(fn.args.args)):
                if a.arg == "self" or a.annotation is not None:
                    continue
                g = infer_from_name(a.arg)
                if g in self._SCALAR_CTYPES:
                    review.append("param  %s(%s) -> %s" % (fn.name, a.arg, g))
        L.append("")
        L.append("-" * 72)
        if review:
            L.append("REVIEW: %d typing(s) inferred from a name (would truncate "
                     "an object):" % len(review))
            for r in review:
                L.append("    %s   annotate as \"object\"/\"ILValue\" if it "
                         "holds one" % r)
        else:
            L.append("REVIEW: no scalar typings were guessed from names")
        L.append("")
        L.append("summary: %d field(s)  (%d object/ptr, %d POD scalar)"
                 % (nfields, nobj, nfields - nobj))
        L.append("=" * 72)
        return "\n".join(L)

    def object_model_warnings(self, tree):
        """High-precision diagnostics for the 'object stored in a scalar' bug
        class that dominated self-hosting: a field given a narrow C scalar type
        that is nonetheless assigned an object-typed constructor parameter, so
        the 64-bit object is silently truncated to int/char*. Returns a list of
        warning strings (empty when nothing looks risky)."""
        warns = []
        for ci in self.class_order:
            init = ci.methods.get("__init__")
            if init is None:
                continue
            ptypes = {}
            for a in (list(getattr(init.args, "posonlyargs", [])) +
                      list(init.args.args)):
                if a.arg == "self":
                    continue
                ptypes[a.arg] = (ann_to_ctype(a.annotation)
                                 or infer_from_name(a.arg) or OBJ)
            ftypes = dict(ci.full_fields())
            for st in ast.walk(init):
                if not (isinstance(st, ast.Assign)
                        and isinstance(st.value, ast.Name)
                        and st.value.id in ptypes
                        and ptypes[st.value.id] == OBJ):
                    continue
                for tgt in st.targets:
                    if (isinstance(tgt, ast.Attribute)
                            and isinstance(tgt.value, ast.Name)
                            and tgt.value.id == "self"
                            and ftypes.get(tgt.attr) in self._SCALAR_CTYPES
                            and infer_from_name(tgt.attr) == ftypes.get(tgt.attr)):
                        # Only when the field's scalar type was itself guessed
                        # from its name (not an explicit `x: str` annotation,
                        # which is the author declaring intent): an obj param
                        # stored into a name-guessed scalar field truncates.
                        warns.append(
                            "%s.%s: C `%s` field (typed from its name) is "
                            "assigned object-typed parameter `%s` -- the object "
                            "is truncated. Annotate the field as `%s: \"object\"`."
                            % (ci.name, tgt.attr, ftypes[tgt.attr],
                               st.value.id, tgt.attr))
        return warns

    def _project_uses_copy_module(self):
        """True if any project module uses the stdlib `copy` module via
        copy.copy() or `from copy import copy`. Cached; purely advisory."""
        cached = getattr(self, "_copy_mod_used_cache", None)
        if cached is not None:
            return cached
        used = False
        base = self.base_dir
        if base and os.path.isdir(base):
            for root, _dirs, fnames in os.walk(base):
                for fn in fnames:
                    if not fn.endswith(".py"):
                        continue
                    try:
                        src = open(os.path.join(root, fn),
                                   encoding="utf-8").read()
                    except Exception:
                        continue
                    if "copy.copy(" in src or "from copy import copy" in src:
                        used = True
                        break
                if used:
                    break
        self._copy_mod_used_cache = used
        return used

    def copy_shadow_warnings(self, tree):
        """rpython style advisory. A user method named `copy` collides with the
        stdlib `copy` module (copy.copy()) and the builtin container `.copy()`:
        at an untyped-obj `.copy()` call site the transpiler cannot always tell
        the user method from a list/dict/set shallow copy, so when a project
        both uses copy.copy() and defines `copy` methods the dispatch is only
        partially supported. Returns a warning for any class in THIS module that
        defines `copy` under that condition (rename it, e.g. copy_node /
        copy_code / copy_state). Empty when the pattern is absent."""
        warns = []
        if not self._project_uses_copy_module():
            return warns
        for ci in self.class_order:
            if "copy" not in ci.methods:
                continue
            warns.append(
                "%s defines a method named `copy`, but this project also uses "
                "the stdlib `copy` module / copy.copy(); `.copy()` on an "
                "untyped obj is then ambiguous with a container shallow copy. "
                "Rename the method (e.g. copy_node / copy_code / copy_state) "
                "for full rpython support." % ci.name)
        return warns

    _MATCH_METHODS = ("group", "start", "end", "groups")

    def _prescan_regex_use(self, tree):
        """Does this module use `re` anywhere? Set before anything is emitted.

        `.group()` on a match lowers only when the module is known to do
        regex, and that was decided by flags set as a *side effect* of
        lowering a regex call. So a helper defined above the first `re.` call
        in the file -- which is where a `re.sub` callback naturally lives --
        was emitted before the flag was set, and `m.group(0)` came out as a
        bare `group(m, 0)` that C defaulted to returning int.

        Deliberately a separate flag from `_regex_dyn`: that one also pulls
        in the VM bridge, and a module whose patterns all fit the specialized
        matchers should still link no engine.
        """
        if "re" not in getattr(self, "modules", set()) and \
                not any(isinstance(n, ast.Import) and
                        any(a.name == "re" for a in n.names)
                        for n in ast.walk(tree)):
            return
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and \
                    n.attr in self._MATCH_METHODS:
                self._regex_used = True
                return

    def run(self, tree):
        global KNOWN_CLASSES, VTABLE_METHODS
        self._prescan_regex_use(tree)
        rewrite_class_lambdas(tree)
        self._rewrite_thread_decorators(tree)
        if self.stdlib_root:
            rewrite_module_lambdas(tree)
        self.closure_specs = lift_nested_functions(tree)
        self.closure_specs.update(convert_block_closures(tree))
        self.closure_values_needed = set()
        self.classes, self.class_order, vt = collect_classes(tree)
        self.ambiguous = ambiguous_class_names(self.base_dir)
        self.ambiguous_funcs = set(ambiguous_function_names(self.base_dir))
        self.ambiguous_funcs |= ambiguous_function_names_in_build()
        for ci in self.class_order:     # qualify local colliding class symbols
            ci.csym = class_csym(ci.name, self.modname, self.ambiguous)
        self.class_typedef_names = {ci.csym for ci in self.class_order}
        KNOWN_CLASSES = self.classes
        VTABLE_METHODS = vt
        self._compute_pod_set(tree)
        self.build_owner_maps()
        self.collect_imports(tree)
        # json pre-pass: record every `name = rpy.json.generate_decoder(Cls)`
        # binding up front, so a `json.loads(s, object_hook=name)` emitted before
        # the binding is processed (or during type inference) already types the
        # result as Cls* and lowers to the specialized decoder.
        for _n in ast.walk(tree):
            if isinstance(_n, ast.Assign) and len(_n.targets) == 1 \
                    and isinstance(_n.targets[0], ast.Name):
                _gci = self._decoder_gen_class(_n.value)
                if _gci is not None:
                    self._json_hook_class[_n.targets[0].id] = _gci
        # regex pre-pass: intern every static re.compile/search/match pattern
        # up front, so a function body emitted before the module-init (where a
        # module-global `_RE = re.compile(...)` lives) already knows the
        # feature is active and can lower `.search`/`.group` to the matcher.
        for _n in ast.walk(tree):
            if isinstance(_n, ast.Call) and isinstance(_n.func, ast.Attribute) \
                    and isinstance(_n.func.value, ast.Name) \
                    and _n.func.value.id == "re" \
                    and _n.func.attr in ("compile", "search", "match") \
                    and _n.args and isinstance(_n.args[0], ast.Constant) \
                    and isinstance(_n.args[0].value, str):
                self._re_intern(_n.args[0].value)
        # Same pre-pass for runtime-valued patterns: the match-object lowering
        # is gated on the regex feature being active, and a module whose only
        # regex use is dynamic interns no pattern at all, so the flag has to be
        # decided here rather than when the call is finally emitted.
        for _n in ast.walk(tree):
            if isinstance(_n, ast.Call) and isinstance(_n.func, ast.Attribute) \
                    and isinstance(_n.func.value, ast.Name) \
                    and _n.func.value.id == "re" \
                    and _n.func.attr in ("search", "match") \
                    and len(_n.args) == 2 \
                    and not (isinstance(_n.args[0], ast.Constant)
                             and isinstance(_n.args[0].value, str)):
                self._regex_dyn = True
        # Names bound to a subprocess.run(...) result, so `r.returncode` and
        # friends lower to an index. Same shape as the regex match-var pass
        # below, and for the same reason: the field access has to know what
        # kind of value it is reading.
        for _n in ast.walk(tree):
            if isinstance(_n, ast.Assign) and len(_n.targets) == 1 \
                    and isinstance(_n.targets[0], ast.Name) \
                    and isinstance(_n.value, ast.Call) \
                    and isinstance(_n.value.func, ast.Attribute) \
                    and _n.value.func.attr == "run" \
                    and isinstance(_n.value.func.value, ast.Name) \
                    and _n.value.func.value.id == "subprocess":
                self._subproc_vars.add(_n.targets[0].id)
        # Track names bound to a compiled pattern (`X = re.compile(const)`) and
        # to a match result (`m = X.search(...)` / `m = re.search(const, ...)`).
        # Their `.search`/`.match`/`.group` calls must lower to the generated
        # matcher even though newer regex modules (minire/test_minire) also
        # define classes with those method names -- otherwise the compiler's own
        # regex use links against a regex module that isn't in the bootstrap.
        def _is_re_compile(v):
            return isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) \
                and isinstance(v.func.value, ast.Name) \
                and v.func.value.id == "re" and v.func.attr == "compile"
        for _n in ast.walk(tree):
            if isinstance(_n, ast.Assign) and len(_n.targets) == 1 and \
                    isinstance(_n.targets[0], ast.Name) and _is_re_compile(_n.value):
                # A constant pattern interns to an integer matcher id; a
                # runtime one compiles to nothing at all -- the "compiled
                # object" is just the pattern string, and _cre_dyn_get's cache
                # makes reusing it as cheap as an id would have been.
                _a = _n.value.args
                if _a and isinstance(_a[0], ast.Constant) \
                        and isinstance(_a[0].value, str):
                    self._regex_vars.add(_n.targets[0].id)
                    # Remember the text as well as the id. Plain .search()/
                    # .match() keep the specialized matcher, but findall/sub/
                    # finditer and the 3-arg search have no specialized form
                    # and need the pattern to reach the bridge.
                    self._regex_var_pat[_n.targets[0].id] = _a[0].value
                else:
                    self._regex_dyn = True
                    self._regex_dyn_vars.add(_n.targets[0].id)
        for _n in ast.walk(tree):
            if isinstance(_n, ast.Assign) and len(_n.targets) == 1 and \
                    isinstance(_n.targets[0], ast.Name) and \
                    isinstance(_n.value, ast.Call) and \
                    isinstance(_n.value.func, ast.Attribute) and \
                    _n.value.func.attr in ("search", "match") and \
                    isinstance(_n.value.func.value, ast.Name) and \
                    (_n.value.func.value.id in self._regex_vars or
                     _n.value.func.value.id in self._regex_dyn_vars or
                     _n.value.func.value.id == "re"):
                self._regex_match_vars.add(_n.targets[0].id)
        # a list literal of compiled patterns (`_HOT_RE = [re.compile(...), ...]`)
        # and the loop/comprehension variable that iterates it -- so
        # `p.search(...)` for `p in _HOT_RE` also lowers to the matcher.
        for _n in ast.walk(tree):
            if isinstance(_n, ast.Assign) and len(_n.targets) == 1 and \
                    isinstance(_n.targets[0], ast.Name) and \
                    isinstance(_n.value, ast.List) and _n.value.elts and \
                    all(_is_re_compile(e) for e in _n.value.elts):
                self._regex_list_vars.add(_n.targets[0].id)
        # `for m in ....finditer(...)` binds a match to the loop variable; it is
        # an ast.For target, not an Assign, so the match-var pass above misses
        # it and .start()/.group() on m would not be typed.
        for _n in ast.walk(tree):
            _it2 = _tg2 = None
            if isinstance(_n, (ast.For, ast.comprehension)):
                _it2, _tg2 = _n.iter, _n.target
            if isinstance(_it2, ast.Call) \
                    and isinstance(_it2.func, ast.Attribute) \
                    and _it2.func.attr == "finditer" \
                    and isinstance(_tg2, ast.Name):
                self._regex_match_vars.add(_tg2.id)
        for _n in ast.walk(tree):
            _it = _tgt = None
            if isinstance(_n, ast.comprehension):
                _it, _tgt = _n.iter, _n.target
            elif isinstance(_n, ast.For):
                _it, _tgt = _n.iter, _n.target
            if isinstance(_it, ast.Name) and _it.id in self._regex_list_vars \
                    and isinstance(_tgt, ast.Name):
                self._regex_vars.add(_tgt.id)
        self._scan_ctypes(tree)
        # cross-module class registry: clsname -> (ClassInfo, modname)
        self.xclasses = {}
        # names of imported module-level singletons/globals: a local of the
        # same name would shadow the bare extern in C (and break an inlined
        # default arg that references the global, e.g. token_kinds.open_paren).
        self._shadow_names = set()
        # Sorted: `setdefault` below means the *first* module visited claims a
        # class name. Iterating a set of module names let that winner vary per
        # process, so a class defined in two modules bound differently between
        # builds -- lowering a field read to a struct offset in one build and a
        # generic `mp_getattr` in the next. Same source, two programs.
        for modname in sorted(set(self.import_alias.values()) |
                              set(self.from_imports.values())):
            reg = self.load_xmod(modname)
            if reg:
                self._shadow_names |= set(reg.get("singletons", {})) | \
                    set(reg.get("globals", {}))
                for cn, ci in reg["classes"].items():
                    ci.csym = class_csym(cn, modname, self.ambiguous)
                    self.xclasses.setdefault(cn, (ci, modname))
        # transitively pull in imported base classes (e.g. a subclass module
        # imports ILCommand from il_cmds.base) so the hierarchy root is known
        for _ in range(8):           # fixpoint; depth is tiny in practice
            added = False
            for cn, (ci, mod) in list(self.xclasses.items()):
                bn = ci.base_name
                if not bn or bn in self.xclasses:
                    continue
                src = self.load_xmod(mod)["imports"].get(bn)
                if not src:
                    continue
                breg = self.load_xmod(src)
                if breg and bn in breg["classes"]:
                    self.xclasses.setdefault(bn, (breg["classes"][bn], src))
                    added = True
            if not added:
                break
        self.used_xmethods = {}     # (clsname, method) -> return ctype
        self.used_xmethods_csym = {}  # (csym, method) -> ret: same, but keyed
                                    # by exact csym for *ambiguous* classes whose
                                    # bare name can't pick the right same-named
                                    # class at extern-emission time.
        self.xstructs_needed = set()  # imported classes whose fields are read
        self.xshadow_td = {}        # csym -> ClassInfo: forward typedef for an
        self.xshadow_body = {}      # ambiguous same-named class that is *not*
        self.xshadow_type = {}      # the bare-keyed registry entry (a "shadow")
        self._io_used = set()       # libc I/O symbols referenced (fopen, ...)
        self._sock_used = set()     # socket symbols referenced (-> prelude)
        self.xvt_needed = set()     # imported modules needing a VT struct emitted
        self.xtype_externs = set()  # imported TypeInfo singletons (Cls_type)
        self.xvtable_impls = set()  # (clsname, method) imported vtable slot impls
        self._tostr_externs = set()  # csyms of imported classes whose __str__ we reference
        self._eq_externs = set()     # csyms of imported classes whose __eq__ we reference
        self._add_externs = set()    # csyms of imported classes whose __add__ we reference
        self.xconstdict_externs = set()  # (clsname, dict) imported const-dict fns
        self.xclass_module = {}     # imported class name -> its module
        for cn, (ci, mod) in self.xclasses.items():
            self.xclass_module[cn] = mod
        self.xmethod_owners = {}    # method name -> [imported ClassInfo] (sep.
                                    # from method_owners so runtime-helper guards
                                    # are unaffected)
        for cn, (ci, _m) in self.xclasses.items():
            if cn in self.classes:
                continue
            for fn, _ in ci.own_fields:
                self.field_owners.setdefault(fn, []).append(ci)
            for m in ci.methods:
                if m != "__init__":
                    self.xmethod_owners.setdefault(m, []).append(ci)
        self.link_cross_module_hierarchy(vt)
        # cross-module-hierarchy dispatch: imported roots whose hierarchy spans
        # modules. Any of the root's interface methods can be dispatched through
        # the root module's (canonical) vtable layout.
        self.hierarchy_method = {}   # method -> root module (for xvcall)
        for cn, (ci, mod) in self.xclasses.items():
            if ci.base is not None:          # not a root
                continue
            subs = [c for c, (c2, _m) in self.xclasses.items()
                    if c2 is not ci and c2.root() is ci]
            if subs:
                for m in ci.methods:
                    if not (m.startswith("__") and m.endswith("__")):
                        self.hierarchy_method.setdefault(m, mod)
            else:
                # An imported polymorphic root whose subclasses live in modules
                # this one never imports -- e.g. asm_gen calls the ILCommand
                # interface (inputs/outputs/targets/...) on IL commands it
                # receives but never constructs. The root's own module emits the
                # canonical vtable, so dispatch its virtual methods through that.
                reg = self.load_xmod(mod)
                if reg:
                    for m in reg["vt"]:
                        self.hierarchy_method.setdefault(m, mod)
        self.collect_module_globals(tree)

        self.prelude()
        # forward typedefs so the TypeInfo vtable can mention class pointers
        for ci in self.class_order:
            self.emit("typedef struct %s %s;" % (ci.csym, ci.csym))
        # imported classes used as local field types need a typedef too (and
        # their struct body, via xstructs_needed, for member access). This is
        # transitive: an imported field type (ILValue) may itself have imported
        # field types (CType) -- and those may live in a module this one never
        # imported, so we resolve and load them on demand.
        imported_field_types = set()
        # work items: (ClassInfo, its_module_name)
        work = [(ci, self.modname) for ci in self.class_order]
        seen_cls = set()
        while work:
            ci, ci_mod = work.pop()
            if ci.name in seen_cls:
                continue
            seen_cls.add(ci.name)
            for _fn, ft in ci.full_fields():
                if not ft.endswith("*"):
                    continue
                base = ft[:-1]
                if base in self.classes or base == ci.name:
                    continue
                if base not in self.xclasses:
                    # resolve `base`'s defining module via ci's imports, then
                    # register the WHOLE module's classes (so sibling subclasses
                    # are available for downcasts / polymorphic dispatch).
                    reg = self.load_xmod(ci_mod) if ci_mod else None
                    src = reg["imports"].get(base) if reg else None
                    breg = self.load_xmod(src) if src else None
                    if breg and base in breg["classes"]:
                        for bn, bci in breg["classes"].items():
                            self.xclasses.setdefault(bn, (bci, src))
                            self.xclass_module.setdefault(bn, src)
                    else:
                        continue
                if base not in imported_field_types:
                    imported_field_types.add(base)
                    bci, bmod = self.xclasses[base]
                    work.append((bci, bmod))
        # virtual-return typing: a method whose annotated return is a leaf
        # class may have its (obj-ABI) result recovered as a typed pointer at a
        # call site in this module (see ex_Call), so ensure that class's struct
        # is declared. Scan reachable method return annotations up front, since
        # struct typedefs are emitted here, before any method body.
        for _ci in list(self.classes.values()) + \
                [c for (c, _m) in self.xclasses.values()]:
            for _fn in _ci.methods.values():
                _rt = ann_to_ctype(_fn.returns) if _fn.returns else None
                if _rt and self._is_class_ptr(_rt):
                    _cn = _rt[:-1]
                    if _cn not in self.classes and _cn in self.xclasses \
                            and self._class_is_leaf(_cn):
                        imported_field_types.add(_cn)
        for c in sorted(imported_field_types):
            self.emit("typedef struct %s %s;" % (c, c))
            self.xstructs_needed.add(c)
        if self.class_order:
            self.emit()
        non_pod = [ci for ci in self.class_order
                   if ci.csym not in self._pod_set]

        if non_pod:
            self.emit_typeinfo_struct()
            for ci in non_pod:
                self.emit("extern const TypeInfo %s_type;" % ci.csym)
            self.emit()
        for ci in self.class_order:
            self.emit_struct(ci)
        self.extern_idx = len(self.lines)   # cross-module externs go here
        self.emit_forward_decls(tree)
        for ci in self.class_order:
            self.emit_class_impl(ci)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                continue
            if _is_dunder_main_guard(node):
                continue            # script-entry guard; C entry is separate
            self.toplevel(node)
        self.emit_module_init()
        self.emit_trampolines()
        # insert cross-module extern declarations at the reserved point
        externs = self.build_externs()
        tramps = ["static obj %s__tramp(obj, obj);" % self.fnsym(fn)
                  for fn in sorted(self.func_values_needed)]
        tramps += ["static obj %s__tramp(obj, obj);" % cname(m)
                   for m in sorted(self.closure_values_needed)]
        def _tramp_sym(c):
            cci = self.classes.get(c) or (self.xclasses[c][0]
                                          if c in self.xclasses else None) \
                or self._ci_by_csym(c)
            return cci.csym if cci is not None else c
        tramps += ["static obj %s__ctortramp(obj, obj);" % s
                   for s in sorted({_tramp_sym(cls) for cls in
                                    (set(self.class_values_needed) |
                                     {ci.csym for ci in self.class_order})
                                    - self._pod_set})]
        self.lines[self.extern_idx:self.extern_idx] = externs + tramps
        if self._typed_lists or self._typed_dicts:
            pre = []
            for et in sorted(self._typed_lists):
                pre.extend(_tlist_prelude(et).splitlines())
            for (kct, vct) in sorted(self._typed_dicts):
                pre.extend(_tdict_prelude(kct, vct).splitlines())
            self.lines[self.extern_idx:self.extern_idx] = pre
        if self._sock_used:
            self.lines[self.extern_idx:self.extern_idx] = \
                SOCKET_PRELUDE.splitlines()
        if self._subproc_used:
            self.lines[self.extern_idx:self.extern_idx] = \
                SUBPROC_PRELUDE.splitlines()
        if self._ossys_used:
            self.lines[self.extern_idx:self.extern_idx] = \
                OS_SYS_PRELUDE.splitlines()
        if self._struct_used:
            self.lines[self.extern_idx:self.extern_idx] = \
                STRUCT_PRELUDE.splitlines()
        if self._json_used:
            # Generate decoder *definitions* first: a nested class-pointer field
            # appends its class to _json_decoders during generation, and the
            # index-based loop picks those up too (transitive closure). Then emit
            # the lexer prelude + forward declarations (both the cursor `_at`
            # form and the public wrapper) for every class discovered.
            tail = []
            i = 0
            while i < len(self._json_decoders):
                tail.extend(self._json_decoder_fn(self._json_decoders[i]))
                i += 1
            head = JSON_PRELUDE.splitlines()
            for ci in self._json_decoders:
                head.append("static %s* _json_decode_%s_at(const char**);"
                            % (ci.csym, ci.csym))
                head.append("static %s* _json_decode_%s(const char*);"
                            % (ci.csym, ci.csym))
            self.lines[self.extern_idx:self.extern_idx] = head
            self.lines.extend(tail)
        if self._regex_ids or self._regex_dyn or self._regex_escape:
            pre = REGEX_HELPER.splitlines()
            if self._regex_vm or self._regex_dyn:
                pre.extend(self._regex_vm_prelude())
            for pid in sorted(self._regex_parsed):
                pre.extend(regex_emit_c(pid, self._regex_parsed[pid]).splitlines())
            disp = ["static obj _re_search(long id, char* t, int anc) {"]
            for pid in sorted(self._regex_parsed):
                disp.append("    if (id == %d) return _re_p%d(t, anc);"
                            % (pid, pid))
            for pid in sorted(self._regex_vm):
                disp.append("    if (id == %d) return _cre_run(%d, t, anc);"
                            % (pid, self._regex_vm_slot[pid]))
            disp.append("    return OBJ_NONE;")
            disp.append("}")
            pre.extend(disp)
            self.lines[self.extern_idx:self.extern_idx] = pre
        return "\n".join(self.lines) + "\n"

    def struct_body_lines(self, ci):
        """Tag definition `struct C { ... };` for an imported class whose
        fields are accessed (the typedef is forward-declared separately)."""
        out = ["struct %s {" % ci.csym]
        if not getattr(ci, "pod", False):
            out.append("    Obj _hdr;")  # non-POD: boxed obj header for vtable
        ff = ci.full_fields()
        if not ff:
            out.append("    char _empty;")
        bf = getattr(ci, "bitfields", {})
        for fn, ft in ff:
            if fn in bf:
                out.append("    unsigned int %s : %d;"
                           % (self.fnsym(fn), bf[fn]))
            else:
                # Same emit-time qualification: a field typed by a class
                # arrives as a bare `Class*`.
                out.append("    %s %s;"
                           % (self._qualify_class_ctype(ft), self.fnsym(fn)))
        out.append("};")
        return out

    def _load_xclass_anywhere(self, cls):
        """Best-effort: register imported class `cls` by searching the modules
        reachable from this one's imports (transitively). Used when a concrete
        pointer type (e.g. CType* inferred from a `.ctype` access) names a class
        whose defining module was never directly imported here."""
        if cls in self.xclasses or cls in self.classes:
            return cls in self.xclasses
        # Sorted and consumed front-to-back: this is a *first match wins*
        # search, so traversal order picks the winner when a class name is
        # defined in more than one reachable module.
        roots = sorted(set(self.import_alias.values())
                       | set(self.from_imports.values()))
        seen, work = set(), list(roots)
        while work:
            mod = work.pop(0)
            if mod in seen:
                continue
            seen.add(mod)
            reg = self.load_xmod(mod)
            if not reg:
                continue
            if cls in reg["classes"]:
                src = mod
                breg = reg
                for bn, bci in breg["classes"].items():
                    self.xclasses.setdefault(bn, (bci, src))
                    self.xclass_module.setdefault(bn, src)
                return True
            work += sorted(reg["imports"].values())
        # Fallback: the class's defining module is not reachable through this
        # module's import graph (the graph is directional -- e.g. il_cmds never
        # imports asm_gen, though asm_gen imports il_cmds). Resolve it through
        # the project-wide class scan: if exactly one project module defines
        # `cls`, load that module so a `mod.Class` annotation can still type the
        # param (letting an ambiguous method like `add` dispatch through the
        # receiver's real class vtable).
        try:
            _classes_h, byname_h = project_class_hierarchy(self.base_dir)
        except Exception:
            byname_h = {}
        mods = byname_h.get(cls)
        if mods and len(mods) == 1:
            src = mods[0]
            reg = self.load_xmod(src)
            if reg and cls in reg["classes"]:
                for bn, bci in reg["classes"].items():
                    self.xclasses.setdefault(bn, (bci, src))
                    self.xclass_module.setdefault(bn, src)
                return True
        return False

    def _load_missing_xclass(self, base, from_mod):
        """Ensure imported class `base` is registered in xclasses, loading its
        whole defining module (resolved via `from_mod`'s imports) on demand.
        Returns True if `base` is available afterwards."""
        if base in self.classes:
            return False
        if base in self.xclasses:
            return True
        reg = self.load_xmod(from_mod) if from_mod else None
        src = reg["imports"].get(base) if reg else None
        breg = self.load_xmod(src) if src else None
        if breg and base in breg["classes"]:
            for bn, bci in breg["classes"].items():
                self.xclasses.setdefault(bn, (bci, src))
                self.xclass_module.setdefault(bn, src)
            return True
        return False

    def build_externs(self):
        classes, funcs, singles, globs = set(), {}, {}, {}
        for (mod, name) in sorted(self.used_imports):
            kind, info = self.resolve_import(name, mod)
            if kind == "class" and (name not in self.classes
                                    or name in self.xclasses):
                # An imported class is externed via its module-qualified csym,
                # so it must be emitted even when a local class shares the bare
                # name (e.g. a tree node `Return` plus il_cmds.control.Return).
                classes.add(name)
                # A re-export (`pkg.Name` -> defining submodule) qualifies the
                # csym by the *defining* module; keep the xclasses registry in
                # step so the extern's csym matches the (already qualified) call
                # site rather than a stale bare entry.
                if name not in self.classes and info is not None and \
                        self.xclasses.get(name, (None,))[0] is not info:
                    self.xclasses[name] = (info, getattr(info, "defmod", mod))
            elif kind == "func" and name not in self.func_params:
                _pcts = []
                try:
                    _pcts = [self._imported_arg_ctype(info, a) or OBJ
                             for a in info.args.args]
                except Exception:
                    _pcts = []
                funcs[func_csym(name, mod, self.ambiguous_funcs)] = \
                    (self._imported_ret_ctype(info), _pcts)
            elif kind == "singleton":
                singles[name] = info
                if info not in self.classes:
                    classes.add(info)
            elif kind == "global":
                globs[name] = info
        for (cls, meth) in self.used_xmethods:
            if cls not in self.classes:
                classes.add(cls)
        needed = {c for c in self.xstructs_needed
                  if c in self.xclasses and c not in self.classes}
        classes |= needed
        # transitive field-type dependencies of accessed struct bodies. A body
        # like ILValue's names CType*, which may live in a module this one never
        # imported -- load it on demand so its forward typedef can be emitted.
        dep_work = list(needed)
        dep_seen = set()
        while dep_work:
            c = dep_work.pop()
            if c in dep_seen or c not in self.xclasses:
                continue
            dep_seen.add(c)
            ci, cmod = self.xclasses[c]
            for _, ft in ci.full_fields():
                if not ft.endswith("*"):
                    continue
                base = ft[:-1]
                if base in self.classes:
                    continue
                if base not in self.xclasses and \
                        not self._load_missing_xclass(base, cmod):
                    continue
                classes.add(base)
                dep_work.append(base)
        vt_fwd = set()              # classes named only in VT slot signatures
        for mod in self.xvt_needed:
            reg = self.load_xmod(mod)
            for m in reg["vt"]:
                ret, params = self.ximported_method_sig(mod, m)
                for ct in [ret] + params:
                    base = ct.rstrip("*")
                    if base in self.xclasses and base not in self.classes:
                        vt_fwd.add(base)
        # field-type dependencies of *shadow* struct bodies (an ambiguous
        # same-named class whose body we emit by exact csym): their pointer
        # fields may name classes not otherwise referenced here.
        for ci in list(self.xshadow_body.values()):
            cmod = getattr(ci, "defmod", None)
            for _, ft in ci.full_fields():
                if not ft.endswith("*"):
                    continue
                base = ft[:-1]
                if base in self.classes or base in self.xclasses:
                    if base in self.xclasses and base not in self.classes:
                        classes.add(base)
                    continue
                if cmod and self._load_missing_xclass(base, cmod):
                    classes.add(base)
        if not (classes or funcs or singles or globs or self.used_xmethods or
                self.xvt_needed or self.xtype_externs or self.xvtable_impls or
                self.xconstdict_externs or self.xshadow_td or
                self._tostr_externs or self._eq_externs or self._add_externs):
            return []
        out = ["/* ---- cross-module imports (extern declarations) ---- */"]
        emitted_td = set()
        for c in sorted(classes | (vt_fwd - classes)):  # forward typedefs first
            cs = self.xcsym(c)
            emitted_td.add(cs)
            out.append("typedef struct %s %s;" % (cs, cs))
        for cs in sorted(self.xshadow_td):     # shadows of ambiguous names
            if cs not in emitted_td:
                emitted_td.add(cs)
                out.append("typedef struct %s %s;" % (cs, cs))
        emitted_body = set()
        for c in sorted(needed):            # full layout for accessed classes
            ci = self.xclasses[c][0]
            emitted_body.add(ci.csym)
            out += self.struct_body_lines(ci)
        for cs in sorted(self.xshadow_body):
            if cs not in emitted_body:
                emitted_body.add(cs)
                out += self.struct_body_lines(self.xshadow_body[cs])
        emitted_type = set()
        for cs in sorted(self.xshadow_type):   # TypeInfo for shadow isinstance
            emitted_type.add(cs)
            out.append("extern const TypeInfoHdr %s_type;" % cs)
        for cs in sorted(self.xshadow_td):     # ctor for a constructed shadow
            out.append("extern %s* %s_new();" % (cs, cs))
        for c in sorted(classes):
            # a class that is also a cross-module hierarchy base gets a full
            # `extern const TypeInfo c_type;` below; emitting a TypeInfoHdr one
            # here too would conflict, so emit only the constructor for it.
            cs = self.xcsym(c)
            if c not in self.xtype_externs and cs not in emitted_type:
                emitted_type.add(cs)
                out.append("extern const TypeInfoHdr %s_type;" % cs)
            out.append("extern %s* %s_new();" % (cs, cs))
        for n in sorted(funcs):
            ret, pcts = funcs[n]
            # Emit a full prototype (not K&R `name()`): without parameter types
            # the C compiler applies default argument promotions at the call
            # site, so a `float` parameter would receive a promoted `double` and
            # read garbage. A real prototype makes the compiler coerce each arg.
            plist = ", ".join(pcts) if pcts else "void"
            out.append("extern %s %s(%s);" % (ret, n, plist))
        for n in sorted(singles):
            out.append("extern %s* %s;" % (singles[n], cname(n)))
        for n in sorted(globs):
            out.append("extern %s %s;" % (globs[n], cname(n)))
        for (cls, meth) in sorted(self.used_xmethods):
            ci = self.classes.get(cls) or (self.xclasses[cls][0]
                                           if cls in self.xclasses else None)
            fn = ci.methods.get(meth) if ci else None
            if fn is not None and meth == "__init__":
                ip = self._init_param_list(fn, skip_self=True)
                plist = ", ".join(["%s* self" % self.xcsym(cls)] + ip)
                out.append("extern void %s_%s(%s);" % (self.xcsym(cls), meth,
                                                       plist))
            else:
                out.append("extern %s %s_%s();" % (
                    self.used_xmethods[(cls, meth)], self.xcsym(cls), meth))
        for (cs, meth) in sorted(self.used_xmethods_csym):  # ambiguous classes
            out.append("extern %s %s_%s();" % (
                self.used_xmethods_csym[(cs, meth)], cs, meth))
        for (cls, meth) in sorted(self.xvtable_impls):  # imported vtable slots
            if (cls, meth) not in self.used_xmethods:
                out.append("extern %s %s_%s();" % (
                    self.ximported_method_sig(self.xclass_module.get(cls), meth)[0]
                    if self.xclass_module.get(cls) else OBJ,
                    self.xcsym(cls), meth))
        for cls in sorted(self.xtype_externs):          # imported TypeInfo
            out.append("extern const TypeInfo %s_type;" % self.xcsym(cls))
        for cs in sorted(self._tostr_externs):           # imported __str__ impls
            out.append("extern obj %s___str__(Obj*);" % cs)
        for cs in sorted(self._eq_externs):              # imported __eq__ impls
            out.append("extern bool %s___eq__(Obj*, obj);" % cs)
        for cs in sorted(self._add_externs):             # imported __add__ impls
            out.append("extern obj %s___add__(Obj*, obj);" % cs)
        for (cls, d) in sorted(self.xconstdict_externs):  # imported const-dicts
            out.append("extern str %s_%s();" % (self.xcsym(cls), d))
            out.append("extern obj %s_%s_items();" % (self.xcsym(cls), d))
            out.append("extern obj %s_%s_keys();" % (self.xcsym(cls), d))
            out.append("extern obj %s_%s_values();" % (self.xcsym(cls), d))
        for mod in sorted(self.xvt_needed):     # replicated TypeInfo layouts
            reg = self.load_xmod(mod)
            vt = self.vt_struct_name(mod)
            out.append("typedef struct %s {" % vt)
            out.append("    const char* name;")
            out.append("    const struct %s* base;" % vt)
            out.append("    const FieldDesc* fields;")
            out.append("    obj (*tostr)(Obj*);")
            out.append("    bool (*eq)(Obj*, obj);")
            out.append("    obj (*addfn)(Obj*, obj);")
            out.append("    unsigned long objsize;")
            for m in sorted(reg["vt"]):
                ret, params = self.ximported_method_sig(mod, m)
                out.append("    %s (*%s)(%s);" % (
                    ret, vslot_name(m), ", ".join(["Obj*"] + params)))
            out.append("} %s;" % vt)
        out.append("")
        return out

    def emit_forward_decls(self, tree):
        """Prototypes for top-level functions, plus file-scope declarations for
        module-level globals (initialized later in <module>_init)."""
        protos = []
        for ci in self.class_order:         # method + constructor prototypes
            if "__init__" not in ci.methods:
                ni = self._nearest_init(ci)  # inherit-only ctor still gets _new
                if ni is not None:
                    _, fn = ni
                    if fn.args.vararg:
                        vn = fn.args.vararg.arg
                        cargs = "int _n_%s, ..." % vn
                    else:
                        cargs = ", ".join(self.param_list(fn, skip_self=True) +
                                          self._kwonly_param_list(fn)) or "void"
                    protos.append("%s* %s_new(%s);" % (ci.csym, ci.csym, cargs))
                else:
                    protos.append("%s* %s_new(void);" % (ci.csym, ci.csym))
            for mname, fn in ci.methods.items():
                if mname == "__str__" and mname not in ci.static_methods:
                    protos.append("obj %s___str__(Obj* self_);" % ci.csym)
                    continue
                if mname == "__eq__" and mname not in ci.static_methods:
                    protos.append("bool %s___eq__(Obj* self_, obj);" % ci.csym)
                    continue
                if mname.startswith("__") and mname.endswith("__") \
                        and mname not in ("__init__", "__enter__", "__exit__"):
                    continue
                ret = self._c_ret(fn)
                if mname in ci.static_methods:   # @staticmethod: no self
                    sp = self.param_list(fn, skip_self=False)
                    protos.append("%s %s_%s(%s);" % (
                        ret, ci.csym, method_cname(mname),
                        ", ".join(sp) if sp else "void"))
                    continue
                params = self.param_list(fn, skip_self=True)
                if mname == "__init__":
                    init_params = self._init_param_list(fn, skip_self=True)
                    plist = ", ".join(["%s* self" % ci.csym] + init_params)
                    protos.append("void %s___init__(%s);" % (ci.csym, plist))
                    if fn.args.vararg:
                        vn = fn.args.vararg.arg
                        cargs = "int _n_%s, ..." % vn
                    else:
                        cargs = ", ".join(self.param_list(fn, skip_self=True) +
                                          self._kwonly_param_list(fn)) or "void"
                    protos.append("%s* %s_new(%s);" % (ci.csym, ci.csym, cargs))
                elif mname in VTABLE_METHODS and \
                        ci.csym not in self._pod_set:
                    vparams = self._vtable_c_param_list(fn)
                    protos.append("%s %s_%s(Obj* self_%s);" % (
                        ret, ci.csym, method_cname(mname),
                        (", " + ", ".join(vparams)) if vparams else ""))
                else:
                    plist = ", ".join(["%s* self" % ci.csym] + params)
                    protos.append("%s %s_%s(%s);" % (ret, ci.csym, method_cname(mname), plist))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and \
                    node.name not in RUNTIME_INTRINSICS and \
                    self.func_nodes.get(node.name) is node:
                protos.append(self.func_signature(node) + ";")
        if protos:
            self.emit("/* forward declarations */")
            for p in protos:
                self.emit(p)
            # main calls this to run module-level global initialization; it is
            # defined below, so it needs a prototype here.
            self.emit("void %s_init(void);" % self.cmod)
            self.emit()
        _ctp = self.ctypes_externs() if hasattr(self, "ctypes_externs") else []
        if _ctp:
            self.emit("/* ctypes FFI: dynamic lookups resolved to C externs */")
            for p in _ctp:
                self.emit(p)
            self.emit()
        if self.mod_globals:
            self.emit("/* module-level globals (init in %s_init) */"
                      % self.modname)
            for name, ctype, kind, val in self.mod_globals:
                # `VOID: CType` emitted `CType* VOID;` while the struct was
                # `crust__CType`; every later field access on one then failed
                # as "member in something not a structure".
                ctype = self._qualify_class_ctype(ctype)
                # A leading underscore marks a module-private global (Python
                # convention); give it `static` linkage so identically-named
                # privates in different modules (e.g. a compiled-regex `_NAME_RE`)
                # don't collide at link time.
                stor = "static " if name.startswith("_") else ""
                if kind == "const":     # literal: define with initializer here
                    if name in C_STDIO_MACRO_NAMES:
                        self.emit("#undef %s" % name)
                    self.emit("%s%s %s = %s;" % (stor, ctype, self._msym(name),
                                                 self.expr(val)))
                else:                   # complex: declare now, init in _init()
                    self.emit("%s%s %s;" % (stor, ctype, self._msym(name)))
            self.emit()
        statics = [(ci, nm) for ci in self.class_order
                   for nm in ci.class_statics]
        if statics:
            self.emit("/* class-level statics (obj; init in %s_init) */"
                      % self.modname)
            for ci, nm in statics:
                self.emit("obj %s_%s;" % (ci.csym, cname(nm)))
            self.emit()

    def _qualify_class_ctype(self, ctype):
        """Map a bare `Class*` C type onto that class's emitted struct name.

        The annotation resolver deliberately keeps a class reference as a bare
        `Class*` so ClassInfo lookup keeps working, and expects emit-time csym
        qualification. Function signatures did that; module globals and struct
        fields did not.
        """
        if not ctype or not ctype.endswith("*"):
            return ctype
        bare = ctype[:-1].strip()
        ci = self.classes.get(bare)
        if ci is None:
            # An *imported* class arrives the same way: `asm_gen` has a field
            # typed `CType`, which lives in `ctypes` and is emitted as
            # `ctypes__CType`.
            x = self.xclasses.get(bare)
            if x is not None:
                ci = x[0] if isinstance(x, tuple) else x
        if ci is not None and getattr(ci, "csym", None):
            return ci.csym + "*"
        return ctype

    def func_signature(self, node):
        ret = self._ret_ctype(node.returns)
        if self._uses_argv(node):
            return "%s %s(int argc, char** argv)" % (ret, self.fnsym(node.name))
        params = self.param_list(node, skip_self=False)
        plist = ", ".join(params) if params else "void"
        return "%s %s(%s)" % (ret, self.fnsym(node.name), plist)

    def resolve_import_module(self, node):
        """Absolute dotted module name for an ImportFrom node."""
        if node.level == 0:
            return node.module or ""
        if not self.py_modname:
            return node.module or ""
        parts = self.py_modname.split(".")
        base = parts[:max(0, len(parts) - node.level)]
        if node.module:
            base.extend(node.module.split("."))
        return ".".join(base)

    def _walk_live(self, node):
        """Like ast.walk, but for an `if` whose test the translator can fold
        (`sys.implementation.name == 'shivyc'`), descend only the live branch.
        This keeps a dead `else: import torch` from shadowing the alias bound in
        the taken branch (`import rpy_torch as torch`).

        Returns an eager pre-order list rather than a generator: minipy keeps its
        bytecode simple and deliberately has no `yield`, so the few generators in
        py2c are written as list-builders to stay self-hostable."""
        out = [node]
        if isinstance(node, ast.If):
            cond = self._static_cond(node.test)
            if cond is True:
                for c in node.body:
                    out.extend(self._walk_live(c))
                return out
            if cond is False:
                for c in node.orelse:
                    out.extend(self._walk_live(c))
                return out
        for c in ast.iter_child_nodes(node):
            out.extend(self._walk_live(c))
        return out

    def collect_imports(self, tree):
        for node in self._walk_live(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    alias = (a.asname or a.name).split(".")[0]
                    self.modules.add(alias)
                    self.import_alias[alias] = a.name
            elif isinstance(node, ast.ImportFrom):
                abs_mod = self.resolve_import_module(node)
                for a in node.names:
                    if a.name == "*":
                        if abs_mod:
                            self.star_import_mods.append(abs_mod)
                            reg = self.load_xmod(abs_mod)
                            if reg:
                                for n in reg["funcs"]:
                                    self.from_imports.setdefault(n, abs_mod)
                                for n in reg["classes"]:
                                    self.from_imports.setdefault(n, abs_mod)
                                for n in reg["globals"]:
                                    self.from_imports.setdefault(n, abs_mod)
                                for n in reg["consts"]:
                                    self.from_imports.setdefault(n, abs_mod)
                    else:
                        self.from_imports[a.asname or a.name] = abs_mod

    def _scan_ctypes(self, tree):
        """FFI bridge: track `ctypes.CDLL` handles and `lib.symbol` lookups as
        compile-time constants so `lib.symbol(args)` lowers to a direct C call
        `symbol(args)` with a real `extern` prototype (see rpy_ctypes.py).

        Populates:
          self.ctypes_libs  {handle var -> .so name}
          self.ctypes_funcs {symbol -> {"restype": ct, "argtypes": [ct]|None}}
          self.ctypes_bind  {local var -> symbol}   (func = lib.symbol)
          self.ctypes_used  set of symbols actually called (drives externs)
        """
        ctypes_mods = {a for a, m in self.import_alias.items()
                       if m in ("ctypes", "rpy_ctypes")}
        self.ctypes_mods = ctypes_mods
        self.ctypes_libs = {}
        self.ctypes_funcs = {}
        self.ctypes_bind = {}
        self.ctypes_used = set()
        self.ctypes_skip = set()        # ids of statements that emit no C
        if not ctypes_mods:
            return

        def is_ct_attr(n):
            return (isinstance(n, ast.Attribute)
                    and isinstance(n.value, ast.Name)
                    and n.value.id in ctypes_mods)

        def ctype_of(n):
            if is_ct_attr(n):
                return _CTYPES_TYPEMAP.get(n.attr)
            return None

        def sym_of(n):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                    and n.value.id in self.ctypes_libs:
                return n.attr
            if isinstance(n, ast.Name) and n.id in self.ctypes_bind:
                return self.ctypes_bind[n.id]
            return None

        def rec_for(sym):
            return self.ctypes_funcs.setdefault(
                sym, {"restype": "int", "argtypes": None})

        for node in self._walk_live(tree):
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
                continue
            tgt, val = node.targets[0], node.value
            # lib = ctypes.CDLL("name.so")
            if isinstance(tgt, ast.Name) and isinstance(val, ast.Call) \
                    and is_ct_attr(val.func) and val.func.attr == "CDLL":
                if val.args and isinstance(val.args[0], ast.Constant):
                    self.ctypes_libs[tgt.id] = val.args[0].value
                self.ctypes_skip.add(id(node))
                continue
            # func = lib.symbol
            if isinstance(tgt, ast.Name):
                s = sym_of(val)
                if s is not None:
                    self.ctypes_bind[tgt.id] = s
                    rec_for(s)
                    self.ctypes_skip.add(id(node))
                    continue
            # lib.symbol.restype = ctypes.c_double / func.argtypes = [...]
            if isinstance(tgt, ast.Attribute) \
                    and tgt.attr in ("restype", "argtypes"):
                s = sym_of(tgt.value)
                if s is not None:
                    rec = rec_for(s)
                    if tgt.attr == "restype":
                        rec["restype"] = ctype_of(val) or "int"
                    elif isinstance(val, (ast.List, ast.Tuple)):
                        rec["argtypes"] = [ctype_of(e) or "int"
                                           for e in val.elts]
                    self.ctypes_skip.add(id(node))

        # second pass: record which externs are actually called, so the prelude
        # can emit a prototype for each (the call sites are now resolvable).
        for node in self._walk_live(tree):
            if isinstance(node, ast.Call):
                s = self.ctypes_call_symbol(node)
                if s is not None:
                    self.ctypes_used.add(s)

    def ctypes_externs(self):
        """`extern <ret> sym(<args>);` lines for every called FFI symbol."""
        lines = []
        for sym in sorted(self.ctypes_used):
            rec = self.ctypes_funcs.get(sym, {"restype": "int",
                                              "argtypes": None})
            ret = rec.get("restype") or "int"
            ats = rec.get("argtypes")
            plist = ", ".join(ats) if ats else "void"
            lines.append("extern %s %s(%s);" % (ret, sym, plist))
        return lines

    def ctypes_call_symbol(self, node):
        """If `node` is a Call to a tracked ctypes extern, return its C symbol,
        else None. Handles both `lib.symbol(args)` and a bound `func(args)`."""
        if not getattr(self, "ctypes_mods", None):
            return None
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                and f.value.id in self.ctypes_libs:
            return f.attr
        if isinstance(f, ast.Name) and f.id in self.ctypes_bind:
            return self.ctypes_bind[f.id]
        return None

    def _emit_ctypes_call(self, node, sym):
        """Lower a tracked FFI call to a direct C call `symbol(args)`, coercing
        each argument to its declared `argtypes` C type."""
        self.ctypes_used.add(sym)
        rec = self.ctypes_funcs.get(sym, {"restype": "int", "argtypes": None})
        argtypes = rec.get("argtypes")
        args = []
        for i, a in enumerate(node.args):
            ax = self.expr(a)
            if argtypes and i < len(argtypes):
                ax = self.coerce_to(argtypes[i], a, ax)
            args.append(ax)
        return "%s(%s)" % (sym, ", ".join(args))

    def ccls(self, name):
        """C base symbol for a class referenced by `name` in local context:
        the local class if one exists, else an imported class, else the bare
        name. (Explicitly cross-module sites use the xclass's .csym directly.)"""
        ci = self.classes.get(name)
        if ci is not None:
            return ci.csym
        ent = self.xclasses.get(name)
        if ent is not None:
            return ent[0].csym
        return class_csym(name, None, self.ambiguous)

    def ann_ctype(self, ann):
        """Annotation -> C type, extending ann_to_ctype with dotted
        `module.Class` hints (e.g. `il_gen.ILValue`, `decl_nodes.Root`).

        The dotted form is resolved to the referenced class's pointer type as a
        *bare* `Class*` so the usual ClassInfo lookup and emit-time csym
        qualification both keep working. Ambiguous class names are only typed
        when the dotted module is the local one; an ambiguous name imported from
        another module is declined (left as obj) because a bare `Class*` would
        bind to the wrong same-named class."""
        if ann is None:
            return None
        # rpython typed list of a scalar element (`list[int]`, `list[float]`,
        # ...) -> a runtime-free unboxed growable array. Checked before
        # ann_to_ctype, which would otherwise collapse `list[T]` to obj.
        # Self-host's `List[ILValue]` etc. have non-scalar elements and stay obj.
        if getattr(self, "_pod_enabled", False):
            _el = ann_elem_ctype(ann)
            if _el is not None and _el in _SCALAR_CTYPES:
                self._typed_lists.add(_el)
                return _tlist_name(_el) + "*"
            _kv = ann_dict_kv(ann)
            if _kv is not None and _kv[1] in _SCALAR_CTYPES and \
                    (_kv[0] in _SCALAR_CTYPES or _kv[0] == "char*"):
                self._typed_dicts.add(_kv)
                self._tdict_by_name[_tdict_name(_kv[0], _kv[1])] = _kv
                return _tdict_name(_kv[0], _kv[1]) + "*"
        base = ann_to_ctype(ann)
        if base is not None:
            return base
        # A bare POD class annotation (`"Body"` / `"Body*"`) resolves to a plain
        # struct pointer: POD classes are passed by pointer, not via the boxed
        # `obj` ABI, so a function can take a `Body*` directly.
        try:
            _text = ast.unparse(ann).strip().strip("'\"")
        except Exception:
            _text = None
        if _text:
            _bare = _text[:-1] if _text.endswith("*") else _text
            _ci = self.classes.get(_bare)
            if _ci is not None and _ci.csym in getattr(self, "_pod_set", set()):
                return _ci.csym + "*"
        try:
            text = ast.unparse(ann).strip().strip("'\"")
        except Exception:
            return None
        if "[" in text or "." not in text:
            return None
        alias, _, cls = text.rpartition(".")
        if not (cls and (cls[0].isupper() or cls[0] == "_")
                and cls.isidentifier()):
            return None
        if cls not in self.classes and cls not in self.xclasses:
            # The class is referenced via a module import (e.g. `il_gen.ILCode`
            # from `import ... as il_gen`), so it isn't in xclasses yet. Load it
            # from a reachable import so the annotation can type the param (which
            # lets a method call on it dispatch through the RIGHT class's vtable,
            # not an unrelated module that defines a same-named method).
            self._load_xclass_anywhere(cls)
        if cls not in self.classes and cls not in self.xclasses:
            return None                  # not a class whose fields we can resolve
        if cls in self.ambiguous:
            # bare `Class*` resolves locally; only safe if that is what the
            # dotted module actually names.
            if self.import_alias.get(alias) != self.modname:
                return None
        return cls + "*"

    def arg_ctype_q(self, fn, arg):
        """arg_ctype, but honoring dotted `module.Class` parameter annotations."""
        if arg.annotation is not None:
            t = self.ann_ctype(arg.annotation)
            if t is not None:
                return t
        return arg_ctype(fn, arg)

    def _imported_arg_ctype(self, fn, arg):
        """Parameter C type for a function imported from another module.

        Like `arg_ctype`, but first resolves rpython typed-list / typed-dict
        annotations (`list[float]`, `dict[str, int]`, ...) to their unboxed
        runtime-free pointer types -- exactly as `ann_ctype` does for local
        params. Without this, the module-level `arg_ctype` runs the annotation
        through `ann_text_to_ctype`, which collapses every `list[...]`/`dict[...]`
        to the boxed `obj`. That made an imported function's extern prototype and
        every call site disagree with the callee's *definition* (which is typed
        locally and so really takes a `_tlist_T*`): the caller boxed the typed
        list into a 16-byte `obj` and passed it where a pointer was expected, so
        the callee dereferenced a tagged union as a struct -> segfault.

        Registering the typed list/dict here also ensures the importing module
        emits the matching struct prelude, so the two translation units share an
        identical layout. Any annotation that is not a scalar typed container
        falls through to the unchanged `arg_ctype`, so no other case is affected.
        """
        ann = arg.annotation
        if ann is not None and getattr(self, "_pod_enabled", False):
            _el = ann_elem_ctype(ann)
            if _el is not None and _el in _SCALAR_CTYPES:
                self._typed_lists.add(_el)
                return _tlist_name(_el) + "*"
            _kv = ann_dict_kv(ann)
            if _kv is not None and _kv[1] in _SCALAR_CTYPES and \
                    (_kv[0] in _SCALAR_CTYPES or _kv[0] == "char*"):
                self._typed_dicts.add(_kv)
                self._tdict_by_name[_tdict_name(_kv[0], _kv[1])] = _kv
                return _tdict_name(_kv[0], _kv[1]) + "*"
        return arg_ctype(fn, arg)

    def _imported_ret_ctype(self, info):
        """Return C type for a function imported from another module.

        The mirror of `_imported_arg_ctype` for the *return* annotation: an
        imported function declared `-> "list[float]"` really returns a
        `_tlist_double*` (its definition is typed locally), but `ann_to_ctype`
        would report the boxed `obj`. That made the extern prototype and the
        call-result type disagree with the definition -- the caller stored a
        16-byte `obj` where the callee returned a pointer (ABI mismatch), then
        unboxed garbage and dereferenced it -> segfault. Resolve scalar typed
        containers here (registering them so the prelude is emitted); everything
        else falls through to the unchanged `ann_to_ctype(...) or OBJ`.
        """
        ret = getattr(info, "returns", None)
        if ret is not None and getattr(self, "_pod_enabled", False):
            _el = ann_elem_ctype(ret)
            if _el is not None and _el in _SCALAR_CTYPES:
                self._typed_lists.add(_el)
                return _tlist_name(_el) + "*"
            _kv = ann_dict_kv(ret)
            if _kv is not None and _kv[1] in _SCALAR_CTYPES and \
                    (_kv[0] in _SCALAR_CTYPES or _kv[0] == "char*"):
                self._typed_dicts.add(_kv)
                self._tdict_by_name[_tdict_name(_kv[0], _kv[1])] = _kv
                return _tdict_name(_kv[0], _kv[1]) + "*"
        return ann_to_ctype(ret) or OBJ

    def _is_class_ptr(self, ct):
        """True if `ct` is a pointer to a (local or imported) class struct."""
        return bool(ct) and ct.endswith("*") and ct != OBJ \
            and (ct[:-1] in self.classes or ct[:-1] in self.xclasses)

    def _typed_container_ret(self, returns):
        """If a return annotation is a scalar typed list/dict (`list[float]`,
        `dict[str,int]`, ...), return its unboxed pointer ctype (registering it
        so the prelude is emitted); else None. Shared by `_c_ret` (the ABI
        return) and `_logical_ret` (the typing return) so a method that returns
        such a container is seen identically at its definition and every call
        site -- otherwise the definition returns a `_tlist_T*` while callers
        believe it returns `obj`, and the boxed/pointer mismatch corrupts the
        value (the method analog of `_imported_ret_ctype`)."""
        if returns is None or not getattr(self, "_pod_enabled", False):
            return None
        _el = ann_elem_ctype(returns)
        if _el is not None and _el in _SCALAR_CTYPES:
            self._typed_lists.add(_el)
            return _tlist_name(_el) + "*"
        _kv = ann_dict_kv(returns)
        if _kv is not None and _kv[1] in _SCALAR_CTYPES and \
                (_kv[0] in _SCALAR_CTYPES or _kv[0] == "char*"):
            self._typed_dicts.add(_kv)
            self._tdict_by_name[_tdict_name(_kv[0], _kv[1])] = _kv
            return _tdict_name(_kv[0], _kv[1]) + "*"
        return None

    def _logical_ret(self, fn):
        """Logical (typing) return type of a method's call result. A class
        return annotation is exposed to callers only when the class is a leaf
        (no subclasses) -- a non-leaf base could be any subclass at runtime, so
        statically typing the result to the base would make subclass-field
        access unsound; those stay obj. Scalars/char* pass through unchanged."""
        _tc = self._typed_container_ret(fn.returns) if fn is not None else None
        if _tc is not None:
            return _tc
        rt = (ann_to_ctype(fn.returns) or OBJ) if fn is not None else OBJ
        if rt and rt != OBJ and rt.endswith("*") and rt[0].isupper():
            cls = rt[:-1]
            if cls not in self.classes and cls not in self.xclasses:
                self._load_xclass_anywhere(cls)
            if (cls in self.classes or cls in self.xclasses) \
                    and self._class_is_leaf(cls):
                return rt
            return OBJ          # non-leaf or unresolvable here
        return rt               # scalars, char*

    def _c_ret(self, fn):
        """C/ABI return type of a method: a class-pointer return is emitted as
        `obj` so every dispatch path (direct, vtable, cross-module hierarchy)
        shares one uniform ABI -- even when the class is not imported in this
        module. The typed pointer is recovered at the call site via
        `_logical_ret` + an AS_OBJ cast (see ex_Call)."""
        _tc = self._typed_container_ret(fn.returns) if fn is not None else None
        if _tc is not None:
            return _tc
        rt = (ann_to_ctype(fn.returns) or OBJ) if fn is not None else OBJ
        if rt and rt != OBJ and rt.endswith("*") and rt[0].isupper():
            return OBJ
        return rt

    _CONTAINER_METHODS = {
        "strip", "lstrip", "rstrip", "upper", "lower", "replace", "split",
        "partition", "splitlines", "rsplit", "startswith", "endswith",
        "isdigit",
        "isalpha", "isspace", "isalnum", "find", "rfind", "rindex", "index",
        "join", "encode",
        "decode", "format", "count", "index", "append", "add", "update",
        "extend", "pop", "remove", "keys", "values", "items", "get", "discard",
        "sort", "copy", "setdefault", "insert", "clear", "reverse", "next"}

    def _exclusive_vt_module(self, attr):
        """Imported module whose canonical vtable provides `attr` when `attr` is
        defined in *exactly one* reachable module's class hierarchy and nowhere
        else (not locally, not in another module). Such a method is
        unambiguously that hierarchy's -- e.g. the CType predicates
        is_void/is_integral/is_pointer/... live only in `ctypes` -- so any call
        to it can dispatch through that module's vtable, correct for a non-leaf
        base and uniform across typed-pointer and bare-obj receivers. Cached."""
        # Lazy per-instance cache. Using getattr/setattr (not
        # `self.__dict__.setdefault`) keeps this working under self-hosting:
        # minipy instances don't expose a live `__dict__` mapping.
        cache = getattr(self, "_excl_vt_cache", None)
        if cache is None:
            cache = {}
            self._excl_vt_cache = cache
        if attr in cache:
            return cache[attr]
        res = None
        # never hijack builtin / container method names (pop, get, append, ...)
        # which collide with list/dict/str operations on unrelated receivers.
        if attr in self._CONTAINER_METHODS:
            cache[attr] = None
            return None
        if not any(attr in ci.methods for ci in self.classes.values()):
            roots = set(self.import_alias.values()) | \
                set(self.from_imports.values())
            seen, work, defining, vt_has = set(), list(roots), set(), set()
            while work:
                mod = work.pop()
                if mod in seen:
                    continue
                seen.add(mod)
                reg = self.load_xmod(mod)
                if not reg:
                    continue
                if any(attr in ci.methods
                       for ci in reg["classes"].values()):
                    defining.add(mod)
                if attr in reg["vt"]:
                    vt_has.add(mod)
                work += list(reg["imports"].values())
            if len(defining) == 1:
                m = next(iter(defining))
                if m in vt_has:
                    res = m
        cache[attr] = res
        return res

    def _ximported_logical_ret(self, mod, mname):
        """Logical (leaf-class) return type of imported method `mname` defined
        in `mod`, used to type cross-module-dispatched call results whose slot
        ABI is obj. Returns obj when absent/non-leaf/non-class."""
        reg = self.load_xmod(mod)
        if reg:
            for ci in reg["classes"].values():
                fn = ci.methods.get(mname)
                if fn is not None:
                    return self._logical_ret(fn)
        return OBJ

    def ctype_csym(self, ft):
        """Rewrite a C type so a pointer to an ambiguous class uses that class's
        qualified symbol (e.g. 'Mult*' -> 'shivyc_..._Mult*')."""
        if ft == "FILE*":           # rpython file handle -> opaque void*
            return "void*"
        if ft == "sockfd":          # rpython socket handle -> int fd
            return "int"
        if ft.endswith("*") and ft[:-1] in self.ambiguous:
            return self.ccls(ft[:-1]) + "*"
        return ft

    def xcsym(self, name):
        """C base symbol for an *imported* class name (the cross-module one,
        even when a local class shares the name)."""
        ent = self.xclasses.get(name)
        if ent is not None:
            return ent[0].csym
        return class_csym(name, self.xclass_module.get(name), self.ambiguous)

    def _ref_xclass(self, ci, body=False, typeinfo=False):
        """Register an imported class referenced by its *exact* csym so its
        forward typedef (plus struct body / TypeInfo extern, as requested) is
        always emitted -- even when the bare-name registry entry is a
        *different*, same-named class, or flips between two such classes by
        load order (the ambiguous-collision case). Emission dedups by csym, so
        registering the common (non-ambiguous) case here is harmless."""
        if ci is None or ci.name in self.classes:
            return
        self.xshadow_td[ci.csym] = ci
        if body:
            self.xshadow_body[ci.csym] = ci
        if typeinfo:
            self.xshadow_type[ci.csym] = ci

    def _find_decls_digest(self, modname):
        """Path to `<modname>.decls.json` beside the inputs, or None. Same
        search roots as `_find_local_module`, and looked for only after it
        fails: a real Python module always wins over a digest of the same
        name, because the module *is* its own best description."""
        rel = os.path.join(*modname.split("."))
        dirs = []
        if self.base_dir:
            dirs.append(self.base_dir)
        for d in _LOCAL_MODULE_DIRS:
            if d not in dirs:
                dirs.append(d)
        for d in dirs:
            cand = os.path.join(d, rel + ".decls.json")
            if os.path.isfile(cand):
                return cand
        return None

    def _find_local_module(self, modname):
        """Path to a co-compiled local module `modname`, or None.

        Searches the transpiled file's own directory plus every input
        directory registered via set_local_module_dirs(), trying both
        `modname.py` and `modname/__init__.py` (dotted names map to a path)."""
        rel = os.path.join(*modname.split("."))
        dirs = []
        if self.base_dir:
            dirs.append(self.base_dir)
        for d in _LOCAL_MODULE_DIRS:
            if d not in dirs:
                dirs.append(d)
        for d in dirs:
            for cand in (os.path.join(d, rel + ".py"),
                         os.path.join(d, rel, "__init__.py")):
                if os.path.isfile(cand):
                    return cand
        return None

    def load_xmod(self, modname):
        """Parse an imported shivyc module and register its public symbols."""
        cache = _XMOD_CACHE
        if modname in cache:
            return cache[modname]
        cache[modname] = None       # guard against import cycles
        reg = {"classes": {}, "funcs": {}, "singletons": {}, "vt": set(),
               "order": [], "imports": {}, "consts": {}, "globals": {}}
        path = None
        if self.base_dir and modname.startswith("shivyc"):
            path = os.path.join(self.base_dir, *modname.split(".")) + ".py"
            if not os.path.exists(path):
                # a package (e.g. `shivyc.tree`) lives in its __init__.py, which
                # re-exports the submodules' public classes.
                pkg_init = os.path.join(self.base_dir, *modname.split("."),
                                        "__init__.py")
                if os.path.exists(pkg_init):
                    path = pkg_init
        elif modname in self.stdlib_index:
            path = self.stdlib_index[modname]
        else:
            # A bare co-compiled module given on the command line (resolve its
            # name against the input directories): treat it as local so calls
            # into it become direct C calls, not dynamic mp_call_import.
            path = self._find_local_module(modname)
        src = None
        from_digest = False
        if path:
            src = open(path, encoding="utf-8").read()
        else:
            # No Python module by that name -- is there a C++ one? A digest
            # (`cpprust --emit-decls`) beside the inputs is rendered as the
            # Python a matching module would have been written as, and the
            # ordinary path below parses it. So `import pool_cpp` resolves,
            # `class MyPool(pool_cpp.Pool)` lays out over the C++ struct,
            # and calls lower to the symbols the digest published.
            dpath = self._find_decls_digest(modname)
            if dpath:
                import json as _json
                try:
                    with open(dpath, encoding="utf-8") as f:
                        digest = _json.load(f)
                except (IOError, ValueError) as e:
                    raise RuntimeError("%s: unreadable class digest (%s)"
                                       % (dpath, e))
                src = _stub_from_decls(digest, modname, dpath)
                if src is not None:
                    path = dpath
                    from_digest = True
        if src is not None:
            try:
                t = ast.parse(src)
                classes, order, vt = collect_classes(t)
                amb = ambiguous_class_names(self.base_dir)
                for cn, ci in classes.items():
                    ci.csym = class_csym(cn, modname, amb)
                    ci.defmod = modname     # module that actually defines it
                # Replicate this module's POD decision so a POD class's methods
                # are not advertised as virtual: a POD class has no vtable, so
                # importers must dispatch its methods directly (and not read a
                # nonexistent Obj `type` header). Keep a method in vt only if
                # some *non-POD* class in the module defines it.
                # For a digest-backed module the POD question is not a
                # heuristic: cpprust already decided which classes carry a
                # vtable, and the stub only renders those (see
                # _stub_from_decls). The classifier would call the base POD
                # anyway -- nothing subclasses it *inside the stub file*,
                # which is the only file it can see -- and a POD base means
                # the importer drops its slots, leaving a NULL in the
                # derived table where `take` should be. (Caught as a
                # segfault through exactly that slot.)
                pods = pod_csyms(t, order,
                                 self._pod_enabled and not from_digest,
                                 self._project_subclassed())
                for ci in order:
                    ci.pod = ci.csym in pods
                non_pod_methods = set()
                for ci in order:
                    if ci.csym not in pods:
                        non_pod_methods.update(ci.methods)
                vt = {m for m in vt if m in non_pod_methods}
                # Cross-module layout: if these classes extend a root defined in
                # ANOTHER module, the defining module pinned the vtable to that
                # root's interface (canon). Replicate it so the VT_<mod> struct
                # built here matches the emitted TypeInfo slot-for-slot. canon
                # is used as-is (NOT filtered by this module's own methods: a
                # root method like make_il_raw may be defined only in the
                # external root, yet still occupies a slot here).
                ext = module_external_canon(self.base_dir, modname,
                                            set(classes))
                if ext is not None:
                    vt = set(ext[0])
                reg["classes"] = classes
                reg["order"] = order
                reg["vt"] = vt
                for n in t.body:        # module-level constant globals
                    if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                            and isinstance(n.targets[0], ast.Name):
                        val = _const_value(n.value)
                        if val is not None:
                            reg["consts"][n.targets[0].id] = val
                for n in t.body:        # module-level annotated globals
                    # `tokens: "list | None" = None` and similar annotated
                    # module state are referenced cross-module as `alias.name`;
                    # register them so the read resolves to the bare exported
                    # symbol (with a matching extern) instead of a bogus
                    # `alias_name` identifier.
                    if isinstance(n, ast.AnnAssign) \
                            and isinstance(n.target, ast.Name) \
                            and n.target.id not in reg["consts"]:
                        reg["globals"].setdefault(
                            n.target.id, ann_to_ctype(n.annotation) or OBJ)
                imp_src = list(t.body)
                for n in t.body:        # descend into `if TYPE_CHECKING:`
                    if isinstance(n, ast.If) and isinstance(n.test, ast.Name) \
                            and n.test.id == "TYPE_CHECKING":
                        imp_src += list(n.body)
                for n in imp_src:        # name -> defining module (for base res.)
                    if isinstance(n, ast.ImportFrom) and n.module:
                        for a in n.names:
                            reg["imports"][a.asname or a.name] = n.module
                    elif isinstance(n, ast.Import):
                        for a in n.names:
                            reg["imports"][a.asname or a.name] = a.name
                for n in t.body:
                    if isinstance(n, ast.FunctionDef):
                        reg["funcs"][n.name] = n
                    elif isinstance(n, ast.Assign) and len(n.targets) == 1 \
                            and isinstance(n.targets[0], ast.Name) \
                            and isinstance(n.value, ast.Call):
                        f = n.value.func
                        cls = None
                        if isinstance(f, ast.Name):
                            if f.id in classes or (f.id[:1].isupper()):
                                cls = f.id
                        elif isinstance(f, ast.Attribute) and f.attr[:1].isupper():
                            cls = f.attr
                        if cls:
                            reg["singletons"][n.targets[0].id] = cls
                    elif isinstance(n, ast.Assign) and len(n.targets) == 1 \
                            and isinstance(n.targets[0], ast.Name) \
                            and isinstance(n.value, (ast.List, ast.Dict,
                                ast.Set, ast.Tuple, ast.BinOp, ast.ListComp,
                                ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                        # module-level obj global (e.g. xmm_arg_regs = [...] or
                        # registers = caller_saved + callee_saved), emitted
                        # unprefixed in its module as `obj <name>;`
                        reg["globals"][n.targets[0].id] = OBJ
                    elif isinstance(n, ast.Assign) and len(n.targets) == 1 \
                            and isinstance(n.targets[0], ast.Name) \
                            and isinstance(n.value, ast.Constant) \
                            and n.value.value is None \
                            and n.targets[0].id not in reg["consts"]:
                        # a mutable module global initialised to None (e.g.
                        # `current_function = None`); it is emitted as a real
                        # `obj <name>;` in its module and referenced elsewhere
                        # as `alias.name`, so register it for the extern.
                        reg["globals"].setdefault(n.targets[0].id, OBJ)
            except (OSError, SyntaxError):
                pass
        cache[modname] = reg
        return reg

    def resolve_import(self, name, modname, _seen=None):
        """('class'|'func'|'singleton'|None, info) for `name` in `modname`."""
        reg = self.load_xmod(modname)
        if not reg:
            return (None, None)
        if name in reg["classes"]:
            return ("class", reg["classes"][name])
        if name in reg["funcs"]:
            return ("func", reg["funcs"][name])
        if name in reg["singletons"]:
            return ("singleton", reg["singletons"][name])
        if name in reg["globals"]:
            return ("global", reg["globals"][name])
        if name in reg["consts"]:
            return ("const", reg["consts"][name])
        # Re-export through a package __init__ (`from .submod import Name`):
        # follow the alias to the module that actually defines the symbol, so
        # `pkg.Name` resolves even when `Name` is only re-exported by `pkg`.
        src = reg.get("imports", {}).get(name)
        if src and src != modname:
            seen = _seen if _seen is not None else set()
            if modname not in seen:
                seen.add(modname)
                return self.resolve_import(name, src, seen)
        return (None, None)

    def xref(self, name, modname):
        """Resolve an imported name; record it for extern emission."""
        kind, info = self.resolve_import(name, modname)
        if kind:
            self.used_imports.add((modname, name))
        return kind, info

    def build_owner_maps(self):
        """Which class declares each attribute/method. Because attributes are a
        fixed per-class set in this codebase, a `.attr` read off an untyped obj
        usually identifies the element's type uniquely -> a real struct offset."""
        self.field_owners = {}
        self.method_owners = {}
        for ci in self.class_order:
            for fn, _ in ci.own_fields:
                self.field_owners.setdefault(fn, []).append(ci)
            for m in ci.methods:
                if m == "__init__":
                    continue
                self.method_owners.setdefault(m, []).append(ci)

    def link_cross_module_hierarchy(self, local_vt):
        """When local classes extend an *imported* base, link the chain so
        find_method_owner/root() walk across the module boundary, and adopt the
        hierarchy root's full virtual interface as the canonical vtable layout
        (so every module in the hierarchy emits a byte-identical TypeInfo)."""
        global VTABLE_METHODS
        self.vt_root = None          # imported root ClassInfo of the hierarchy
        self.vt_root_mod = None
        for ci in self.class_order:  # link a local class's imported base
            if ci.base is None and ci.base_name in self.xclasses \
                    and ci.base_name not in self.classes:
                ci.base = self.xclasses[ci.base_name][0]
            # link any imported secondary (MI) bases not resolved locally
            for bn in ci.extra_base_names:
                if not any(b.name == bn for b in ci.extra_bases) \
                        and bn in self.xclasses:
                    ci.extra_bases.append(self.xclasses[bn][0])
        for cn, (ci, _m) in self.xclasses.items():   # transitive imported links
            if ci.base is None and ci.base_name in self.xclasses:
                ci.base = self.xclasses[ci.base_name][0]
        roots = {}                   # external roots of local classes
        for ci in self.class_order:
            r = ci.root()
            if r is not ci and r.name in self.xclasses \
                    and r.name not in self.classes:
                roots[r.name] = r
        if len(roots) == 1:          # record the imported root for method_proto
            rname, r = next(iter(roots.items()))
            self.vt_root = r
            self.vt_root_mod = self.xclass_module.get(rname)
        # Canonical vtable layout, computed from the WHOLE cross-module
        # hierarchy (root interface + every overridden method) so each module --
        # the root-defining one included -- emits a byte-identical TypeInfo and
        # importers replicate it slot-for-slot. None => this module's virtual
        # classes don't form a single pinned hierarchy; keep the per-module vt
        # (e.g. a standalone class dispatched virtually from elsewhere).
        ext = module_external_canon(self.base_dir, self.modname,
                                    {ci.name for ci in self.class_order})
        self._vt_root_key = ext[1] if ext is not None else None
        if ext is not None:
            VTABLE_METHODS = set(ext[0])

    def is_ancestor(self, a, b):
        c = b
        while c:
            if c is a:
                return True
            c = c.base
        return False

    def _resolve_owner(self, owners):
        if not owners:
            return None
        if len(owners) == 1:
            return owners[0]
        # if one owner is a base of all others, casting to it is offset-safe
        for cand in owners:
            if all(self.is_ancestor(cand, o) for o in owners):
                return cand
        return None

    def resolve_attr_owner(self, attr):
        return self._resolve_owner(self.field_owners.get(attr, []))

    def resolve_method_owner(self, attr):
        return self._resolve_owner(self.method_owners.get(attr, []))

    def resolve_xmethod_owner(self, attr):
        """Imported class declaring `attr`, only if it is the SOLE definer.

        Multiple definers means the method is overridden (polymorphic); calling
        any one statically would dispatch to the wrong override at runtime, and
        cross-module vtables aren't available, so we decline rather than
        miscompile."""
        if attr in self.method_owners:     # a local method of the same name wins
            return None
        if attr in self.hierarchy_method:  # virtual via a canonical cross-module
            return None                    # vtable; never bind to one (base) impl
        owners = self.xmethod_owners.get(attr, [])
        return owners[0] if len(owners) == 1 else None

    def resolve_project_xmethod(self, attr):
        """Last-resort resolution for a method called on an untyped/dynamic obj
        receiver whose class is not imported here: consult the project-wide
        scan. Returns (ClassInfo, is_static) or None.

        Binds in two sound cases:
        - `attr` has a SINGLE definer across the package (so the receiver, if it
          has that method, must be that class -- the cast is sound); or
        - `attr` has one @staticmethod definer and every *other* definer is a
          trivial forwarder `return X.attr(...)` to it. A static method ignores
          the receiver, and the forwarders return the same value, so calling the
          static directly is correct regardless of the receiver's exact class.

        The chosen class is loaded as an xclass on demand (struct, return type,
        extern decl emitted), exactly as an explicit import would."""
        if attr in self.method_owners or attr in self.xmethod_owners \
                or attr in self.hierarchy_method:
            return None
        if not self.base_dir and not _LOCAL_MODULE_DIRS:
            return None
        defs = project_method_owners(self.base_dir).get(attr, set())
        # ignore a definition in the current module (handled by other paths)
        defs = {(m, c) for (m, c) in defs if m != self.py_modname}
        if not defs:
            return None
        if len(defs) == 1:
            modname, classname = next(iter(defs))
            kind, info = self.xref(classname, modname)
            if kind == "class" and info is not None and attr in info.methods:
                return (info, attr in info.static_methods)
            return None
        return self._resolve_static_forwarded(attr, defs)

    def _resolve_static_forwarded(self, attr, defs):
        """Multiple definers: bind only if exactly one is a @staticmethod and
        every other is a trivial forwarder to a same-named method. Returns
        (static_ClassInfo, True) or None."""
        static_ci = None
        others = []
        for modname, classname in defs:
            kind, info = self.xref(classname, modname)
            if kind != "class" or info is None or attr not in info.methods:
                return None
            if attr in info.static_methods:
                if static_ci is not None:
                    return None                # >1 static definer: ambiguous
                static_ci = info
            else:
                others.append(info.methods[attr])
        if static_ci is None:
            return None
        for m in others:
            if not self._is_forwarder_to(m, attr):
                return None
        return (static_ci, True)

    @staticmethod
    def _is_forwarder_to(fn, attr):
        """True if `fn`'s body is just `return <expr>.attr(...)` (ignoring a
        docstring and inline imports) -- i.e. it delegates to a same-named
        method, so it yields the same value as the method it forwards to."""
        body = [s for s in fn.body
                if not (isinstance(s, ast.Expr)
                        and isinstance(s.value, ast.Constant))
                and not isinstance(s, (ast.Import, ast.ImportFrom))]
        if len(body) != 1 or not isinstance(body[0], ast.Return):
            return False
        val = body[0].value
        return isinstance(val, ast.Call) \
            and isinstance(val.func, ast.Attribute) and val.func.attr == attr

    @staticmethod
    def vt_struct_name(modname):
        return "VT_" + modname.replace(".", "_")

    def _class_is_leaf(self, clsname):
        """True if no known class (local or imported) derives from clsname."""
        for ci in self.classes.values():
            if ci.base_name == clsname:
                return False
        for cn, (ci, _m) in self.xclasses.items():
            if ci.base_name == clsname:
                return False
        return True

    def resolve_xvirtual(self, attr):
        """A polymorphic imported method `attr` is dispatchable via a
        cross-module vtable iff all its definers live in ONE module and it is
        virtual there. Returns that module name, else None."""
        if attr in self.method_owners or attr in VTABLE_METHODS:
            return None
        owners = self.xmethod_owners.get(attr, [])
        if len(owners) < 2:                # single/zero -> handled elsewhere
            return None
        mods = {self.xclass_module.get(o.name) for o in owners}
        if len(mods) != 1:
            return None
        mod = next(iter(mods))
        reg = self.load_xmod(mod)
        return mod if reg and attr in reg["vt"] else None

    def resolve_project_xvirtual(self, attr):
        """Canonical vtable module for a polymorphic method `attr` whose
        hierarchy is NOT dispatchable via the imported paths (its owners span
        modules, or its hierarchy root isn't imported here) -- e.g.
        general_nodes calls `expr.lvalue()` / `._lvalue()` on expr nodes it
        neither constructs nor imports. Runs only after resolve_xmethod_owner
        and resolve_xvirtual have both declined, so it is safe to consult the
        project-wide scan: resolve the hierarchy root, and dispatch a received
        instance through the root module's canonical vtable exactly as an
        importing module would. Returns that module, or None."""
        if attr in self.method_owners or attr in VTABLE_METHODS:
            return None
        if attr in self.hierarchy_method:
            return None                 # the imported-hierarchy path handles it
        defs = project_method_owners(self.base_dir).get(attr, set())
        if len(defs) < 2:               # single definer -> not polymorphic
            return None
        classes, _byname = project_class_hierarchy(self.base_dir)
        # Resolve the canonical vtable via module_external_canon -- the same
        # routine that decides each module's emitted vtable layout -- so the
        # dispatch pins to the exact root the defining modules were compiled
        # against. For each defining module, prefer its FULL class set (which
        # yields the complete hierarchy canon), but a module that hosts several
        # unrelated hierarchies makes that ambiguous (returns None): fall back to
        # just this method's definer classes there. Modules that still can't pin
        # the slot are skipped; every module that CAN must agree on one root.
        canon_mods = set()
        skipped = False
        for dm in set(m for m, _c in defs):
            allcls = set(c for (m, c) in classes if m == dm)
            ext = module_external_canon(self.base_dir, dm, allcls)
            if ext is None or attr not in ext[0]:
                dcls = set(c for (m, c) in defs if m == dm)
                ext = module_external_canon(self.base_dir, dm, dcls)
            if ext is None or attr not in ext[0]:
                skipped = True          # this definer can't pin the slot
                continue                # can't pin here; another module may
            canon_mods.add(ext[1][0])   # ext[1] is the (module, class) root key
        # If any defining module couldn't pin a canonical slot, `attr` is NOT a
        # single coherent cross-module vtable method -- it is merely an ambiguous
        # name shared by unrelated classes (e.g. ILCode.add, which has no
        # subclasses, vs ErrorCollector.add). Committing to the one module that
        # happened to pin would cast every receiver through that module's slot
        # layout and read the wrong offset for the others. Decline so the caller
        # falls through to runtime type dispatch.
        if skipped or len(canon_mods) != 1:
            return None
        mod = next(iter(canon_mods))
        return mod if self.load_xmod(mod) else None

    def ximported_method_sig(self, mod, mname):
        """(ret_ctype, [param_ctypes]) for method `mname` in imported `mod`,
        replicating that module's emitted slot signature."""
        reg = self.load_xmod(mod)
        ext = module_external_canon(self.base_dir, mod, set(reg["classes"]))
        if ext is not None:
            cands = self._hier_method_fns(ext[1], mname)
            if cands:
                ret, params, _ = self._proto_from_fns(cands)
                return ret, params
        cands = [ci.methods[mname] for ci in reg["order"]
                 if mname in ci.methods]
        if not cands:
            return OBJ, []
        ret, params, _ = self._proto_from_fns(cands)
        return ret, params

    def ximported_method_fn(self, mod, mname):
        """The widest FunctionDef for `mname` in imported `mod` (used to recover
        default-argument values for vtable calls). Must match the slot's widest
        signature so a narrower call site pads the missing args with the widest
        implementation's defaults."""
        reg = self.load_xmod(mod)
        ext = module_external_canon(self.base_dir, mod, set(reg["classes"]))
        cands = []
        if ext is not None:
            cands = self._hier_method_fns(ext[1], mname)
        if not cands:
            cands = [ci.methods[mname] for ci in reg["order"]
                     if mname in ci.methods]
        best, bestn = None, -1
        for fn in cands:
            n = len(fn.args.posonlyargs) + len(fn.args.args)
            if n > bestn:
                bestn, best = n, fn
        return best

    def _box_expr_to_obj(self, expr, ct):
        """Box a rendered C expression of ctype `ct` into a tagged `obj`."""
        if ct == OBJ:
            return expr
        if ct == "int":
            return "OBJ_INT(%s)" % expr
        if ct == "bool":
            return "OBJ_BOOL(%s)" % expr
        if ct == "char*":
            return "OBJ_STR(%s)" % expr
        if ct in ("double", "float"):
            return "OBJ_FLOAT(%s)" % expr
        if isinstance(ct, str) and ct.endswith("*"):
            return "OBJ_OBJ(%s)" % expr
        return expr

    def _obj_ambiguous_dispatch(self, recv_node, mname, arg_nodes):
        """Dispatch `recv.mname(...)` on an untyped obj receiver when `mname` is
        defined in two or more UNRELATED, NON-polymorphic classes (each a root
        with no project-wide subclasses), e.g. ILCode.add / ErrorCollector.add /
        ASMCode.add.

        A single representative-vtable cast is wrong here: each class lives in
        its own module and the shared method name sits at a DIFFERENT vtable
        slot offset per module, so casting (say) an ILCode through the errors
        vtable reads a bogus slot and calls a null/incorrect pointer. Instead we
        emit a runtime type-switch keyed on the receiver's actual TypeInfo and
        call each class's own method directly. Returns a C expression, or None
        if the pattern does not apply (leaving existing resolution untouched)."""
        if not self.base_dir and not _LOCAL_MODULE_DIRS:
            return None
        defs = project_method_owners(self.base_dir).get(mname, set())
        if len(defs) < 2:
            return None
        try:
            classes_h, _byname = project_class_hierarchy(self.base_dir)
        except Exception:
            return None
        # any class named as a base => it has subclasses (exact-type match would
        # miss instances of those subclasses, so the pattern is unsafe)
        has_sub = {info.get("base") for info in classes_h.values()
                   if info.get("base")}
        definers = []
        for (modname, classname) in sorted(defs):
            if _COMPILED_MODULES and modname not in _COMPILED_MODULES:
                # Definer lives outside this compilation unit (e.g. rasm_obj,
                # dragged in only by main's stubbed `import rasm_obj`). Its
                # instances cannot occur as receivers here, and referencing its
                # TypeInfo would be an undefined-symbol link error -- omit its
                # branch rather than declining the whole (otherwise correct and
                # deterministic) type switch.
                continue
            info = classes_h.get((modname, classname))
            base = info.get("base") if info else None
            if info is None:
                return None                      # unknown: bail
            if base is not None and base != "object":
                return None                      # real subclass: exact-type
                                                 # match would miss it -> bail
            if classname in has_sub:
                return None                      # polymorphic: bail
            kind, ci = self.xref(classname, modname)
            if kind != "class" or ci is None:
                # Not locally imported (e.g. general_nodes never imports the
                # ErrorCollector class nor asm_gen at all): resolve the definer
                # project-wide so its TypeInfo + method can still be externed
                # into the runtime chain. Every in-unit definer must become a
                # branch -- a receiver whose type is the omitted one would
                # otherwise fall through and hit the wrong slot.
                reg = self.load_xmod(modname)
                ci = reg["classes"].get(classname) if reg else None
            if ci is None or mname not in ci.methods:
                return None
            if mname in getattr(ci, "static_methods", ()):
                return None                      # static: receiver type irrelevant
            definers.append(ci)
        if len(definers) < 2:
            return None
        branches = []
        for ci in definers:
            m = ci.methods[mname]
            pct = [arg_ctype(m, a) for a in m.args.args[1:]]
            cargs = self.coerce_args(pct, arg_nodes, self.defaults_for(m, True))
            ret = self._c_ret(m)
            # Ambiguous class names (e.g. minire.Pattern and test_minire.Pattern
            # both `Pattern`) must be externed per module-qualified csym; keying
            # by bare name collides and would leave one call site undeclared
            # (gcc then assumes int -> obj/int type errors).
            if ci.name in self.ambiguous and ci.name not in self.classes:
                self.used_xmethods_csym[(ci.csym, mname)] = ret
            else:
                self.used_xmethods[(ci.name, mname)] = ret
            self._ref_xclass(ci, body=True, typeinfo=True)
            call = "%s_%s((Obj*)AS_OBJ(_d)%s)" % (
                ci.csym, mname, (", " + ", ".join(cargs)) if cargs else "")
            if ret == "void":
                body = "%s; _dv = OBJ_NONE;" % call
            else:
                body = "_dv = %s;" % self._box_expr_to_obj(call, ret)
            branches.append(
                "if (_dt == (const TypeInfoHdr*)&%s_type) { %s }"
                % (ci.csym, body))
        chain = " else ".join(branches)
        # genuine set receivers reach here too (an untyped `.add` site that is
        # sometimes a class method and sometimes set.add): a real set/list is
        # tagged T_SET/T_LIST (not T_OBJ), so none of the type branches match;
        # fall back to the builtin set add in that case.
        tail = ""
        if mname == "add" and len(arg_nodes) == 1:
            tail = " else if (_d.tag == T_SET || _d.tag == T_LIST) { " \
                   "set_add(_d, %s); }" % self.wrap_obj(arg_nodes[0])
        elif mname == "copy" and not arg_nodes:
            # `copy` is also dict/list/set.copy(): a genuine container receiver
            # matches none of the class TypeInfo branches, so fall back to the
            # builtin shallow copy instead of returning None (or, worse, calling
            # a null `copy` vtable slot the container does not have).
            tail = " else { _dv = pycopy(_d); }"
        return ("({ obj _d = %s; const TypeInfoHdr* _dt = "
                "(_d.tag == T_OBJ && _d.u.o) ? "
                "(const TypeInfoHdr*)_d.u.o->type : (const TypeInfoHdr*)0; "
                "obj _dv = OBJ_NONE; %s%s _dv; })" % (self.wrap_obj(recv_node),
                                                      chain, tail))

    def xvcall(self, mod, recv_node, mname, arg_nodes):
        """Cross-module virtual call: index the defining module's TypeInfo
        layout (replicated locally as a VT struct) through the object header."""
        fn = self.ximported_method_fn(mod, mname)
        if self.stdlib_root:
            return self._mp_method_call_args(recv_node, mname, arg_nodes, fn)
        if self._method_has_varargs(fn) or \
                len(arg_nodes) > len(self.ximported_method_sig(mod, mname)[1]):
            return self._mp_method_call_args(recv_node, mname, arg_nodes, fn)
        self.xvt_needed.add(mod)
        xo = self.vtable_recv(recv_node)
        ret, pct = self.ximported_method_sig(mod, mname)
        fn = self.ximported_method_fn(mod, mname)
        defs = self.defaults_for(fn, True) if fn else None
        cargs = self.coerce_args(pct, arg_nodes, defs)
        vt = self.vt_struct_name(mod)
        return "((const %s*)(%s)->type)->%s(%s)" % (
            vt, xo, vslot_name(mname), ", ".join([xo] + cargs))

    def ctor_class(self, call):
        """If `call` constructs a known class (local/imported/alias), return its
        name (marking the import used); else None."""
        if not isinstance(call, ast.Call):
            return None
        f = call.func
        if isinstance(f, ast.Name):
            if f.id in self.classes:
                return f.id
            if f.id in self.from_imports:
                kind, _ = self.xref(f.id, self.from_imports[f.id])
                if kind == "class":
                    return f.id
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                and f.value.id in self.import_alias:
            kind, _ = self.xref(f.attr, self.import_alias[f.value.id])
            if kind == "class":
                return f.attr
        return None

    def _register_mod_global(self, name, ctype, kind, val):
        """First binding -> file-scope decl; later bindings -> module init."""
        if name in self.mod_global_names:
            self.mod_init_stmts.append(ast.Assign(
                targets=[ast.Name(id=name, ctx=ast.Store())], value=val))
            return
        if kind not in ("const", "singleton") and \
                ctype not in ("char*", "int", "bool"):
            ctype = OBJ
        self.mod_globals.append((name, ctype, kind, val))
        self.mod_global_names.add(name)
        self.mod_global_types[name] = ctype

    def _register_assign_global(self, name, val):
        """Register one name from a module-level assignment."""
        if isinstance(val, (ast.Set, ast.List)) and val.elts and \
                all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                    for e in val.elts):
            self.str_sets[name] = [e.value for e in val.elts]
        cls = self.ctor_class(val)
        if cls:
            self.singleton_names[name] = cls
            self._register_mod_global(name, cls + "*", "singleton", val)
        elif isinstance(val, ast.Name) and val.id in self.func_nodes:
            self.func_values_needed.add(val.id)
            self._register_mod_global(name, OBJ, "expr", val)
        elif isinstance(val, ast.Name):
            self._register_mod_global(name, OBJ, "expr", val)
        elif isinstance(val, ast.Attribute):
            self._register_mod_global(name, OBJ, "expr", val)
        elif isinstance(val, ast.Constant) and val.value is None:
            # `X = None` at module scope: a nullable obj global (the common
            # lazy-init / singleton sentinel). Registering it as a real global
            # ensures a `global X; X = ...` inside a function stores into the
            # module global instead of a shadowing local.
            self._register_mod_global(name, OBJ, "expr", val)
        elif isinstance(val, (ast.List, ast.Dict, ast.Set, ast.Tuple,
                              ast.BinOp, ast.ListComp, ast.DictComp,
                              ast.SetComp, ast.Call)):
            if isinstance(val, ast.Tuple) and val.elts and \
                    all(isinstance(e, ast.Name) for e in val.elts):
                tnames = [e.id for e in val.elts]
                if all(t in STDLIB_BUILTINS or t in self.BUILTIN_TYPE_TAGS
                       for t in tnames):
                    self.tuple_type_globals[name] = tnames
            ct = self.value_ctype(val) or OBJ
            if ct not in ("char*", "int", "bool"):
                ct = OBJ
            self._register_mod_global(name, ct, "expr", val)
        elif isinstance(val, ast.Constant) and isinstance(val.value, (str, bytes)):
            self._register_mod_global(name, OBJ, "expr", val)
        else:
            ct = self.value_ctype(val)
            if ct and ct != OBJ:
                self.mod_const_types[name] = ct
                if isinstance(val, ast.Constant):
                    kind = "const" if name not in self.func_nodes else "expr"
                    self._register_mod_global(name, ct, kind, val)
                elif isinstance(val, ast.Name):
                    self._register_mod_global(name, ct, "expr", val)

    def collect_module_globals(self, tree):
        # mod_globals: ordered [(name, ctype, kind, value_node)] needing a
        # file-scope declaration + deferred init in <module>_init().
        self.mod_globals = []
        self.mod_global_names = set()
        self.tuple_type_globals = {}  # name -> [type names] for isinstance(x, T)
        self.mod_const_types = {}   # module-level constant name -> ctype
        self.mod_init_stmts = []
        for node in tree.body:
            if isinstance(node, ast.Try):
                for stmt in node.body:
                    if isinstance(stmt, ast.ImportFrom):
                        for alias in stmt.names:
                            if alias.name == "*":
                                continue
                            nm = alias.asname or alias.name.split(".")[0]
                            if nm not in self.mod_global_names:
                                self._register_mod_global(nm, OBJ, "expr",
                                                          ast.Constant(value=None))
                    elif isinstance(stmt, ast.Import):
                        for alias in stmt.names:
                            nm = alias.asname or alias.name.split(".")[0]
                            if nm not in self.mod_global_names:
                                self._register_mod_global(nm, OBJ, "expr",
                                                          ast.Constant(value=None))
                for handler in node.handlers:
                    for stmt in handler.body:
                        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                                and isinstance(stmt.targets[0], ast.Name) \
                                and isinstance(stmt.value, ast.Constant) \
                                and stmt.value.value is None:
                            nm = stmt.targets[0].id
                            if nm not in self.mod_global_names:
                                self._register_mod_global(nm, OBJ, "expr",
                                                          stmt.value)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                self.func_returns[node.name] = \
                    self._ret_ctype(node.returns)
                self.func_params[node.name] = [self.arg_ctype_q(node, a)
                                               for a in node.args.args]
                self.func_nodes[node.name] = node
            if not isinstance(node, ast.Assign):
                continue
            if id(node) in getattr(self, "ctypes_skip", ()):
                continue                    # ctypes config: no C declaration
            val = node.value
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            for i, tgt in enumerate(node.targets):
                if not isinstance(tgt, ast.Name):
                    continue
                if i == 0:
                    self._register_assign_global(tgt.id, val)
                else:
                    self._register_mod_global(tgt.id, OBJ, "expr",
                                              ast.Name(id=names[0]))
        # Module-level annotated assignments (`x: T = ...` / `x: T`) are emitted
        # by toplevel() via st_AnnAssign, but must also be recorded as globals so
        # a `global x; x = ...` inside a function stores into the module global
        # instead of creating a shadowing local. Type-only registration (we do
        # NOT append to mod_globals) avoids emitting a second file-scope decl.
        for node in tree.body:
            if not isinstance(node, ast.AnnAssign) \
                    or not isinstance(node.target, ast.Name):
                continue
            nm = node.target.id
            if nm in self.mod_global_names:
                continue
            try:
                ct = self._local_ann_ctype(nm, node.annotation)
            except Exception:
                ct = OBJ
            # Match st_AnnAssign(toplevel=True): a class-pointer/base annotation
            # initialized to None lowers to obj (nullable, dynamic dispatch).
            if isinstance(node.value, ast.Constant) and node.value.value is None \
                    and isinstance(ct, str) and ct.endswith("*"):
                ct = OBJ
            self.mod_global_names.add(nm)
            self.mod_global_types[nm] = ct
        # module-level statements that aren't declarations (attribute/subscript
        # assignments, aug-assignments, bare calls) run at import time, so they
        # are deferred into <module>_init() rather than emitted at file scope.
        for node in tree.body:
            if id(node) in getattr(self, "ctypes_skip", ()):
                continue                    # ctypes config: compile-time only
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and not isinstance(node.targets[0], ast.Name):
                self.mod_init_stmts.append(node)
            elif isinstance(node, ast.AugAssign):
                self.mod_init_stmts.append(node)
            elif isinstance(node, ast.Expr):
                self.mod_init_stmts.append(node)
            elif isinstance(node, (ast.If, ast.For, ast.While, ast.Try, ast.With)):
                if _is_dunder_main_guard(node):
                    continue        # script-entry guard; C has its own entry
                self.mod_init_stmts.append(node)

    def prelude(self):
        bar = "/* " + "=" * 66 + " */"
        self.emit(bar)
        if self.stdlib_root:
            self.emit("/*  Transpiled from python-stdlib/%s by tools/py2c.py */"
                      % self.modname)
        else:
            self.emit("/*  Transpiled from shivyc/%s.py by tools/py2c.py        */"
                      % self.modname)
        self.emit("/*  Object model: arena + per-class vtable (see py2c.py).  */")
        self.emit(bar)
        self.emit('#include "shivyc_rt.h"')
        if self.stdlib_root:
            self.emit('#include "mp_stdlib_bridge.h"')
        self.emit()

    def _shallow_copy(self, node):
        """copy.copy(x) on a class instance -> a fresh arena node bit-copied
        from the source (shallow copy), keeping x's type. Returns None when the
        argument's type isn't a known struct pointer (caller falls back)."""
        if len(node.args) != 1:
            return None
        ct = self.value_ctype(node.args[0])
        if ct and ct.endswith("*") and ct not in ("char*", "void*", OBJ):
            struct = ct[:-1].strip()
            e = self.expr(node.args[0])
            # Copy the *runtime* object: `self` may be statically the base
            # class but dynamically a larger subclass, so sizeof(static type)
            # would truncate it (dropping subclass fields). The concrete size
            # is recorded on each TypeInfo as objsize.
            return ("({ %s _src = (%s); "
                    "const TypeInfoHdr* _ti = (const TypeInfoHdr*)"
                    "((Obj*)_src)->type; "
                    "unsigned long _sz = _ti ? _ti->objsize : sizeof(%s); "
                    "%s _cp = aalloc(_sz); memcpy(_cp, _src, _sz); _cp; })"
                    % (ct, e, struct, ct))
        return None

    def _mp_import_call(self, mod, attr, node):
        # copy.copy(x) on a class instance is a shallow struct copy, so a
        # self-hosted ShivyCX needs no dynamic `copy` module.
        if mod == "copy" and attr == "copy":
            cc = self._shallow_copy(node)
            if cc:
                return cc
        nargs = len(node.args)
        wrapped = [self.wrap_obj(a) for a in node.args]
        sm, sa = c_string(mod), c_string(attr)
        if nargs == 0:
            return "mp_call_import(%s, %s, 0)" % (sm, sa)
        if nargs == 1:
            return "mp_call_import(%s, %s, 1, %s)" % (sm, sa, wrapped[0])
        if nargs == 2:
            return "mp_call_import(%s, %s, 2, %s, %s)" % (
                sm, sa, wrapped[0], wrapped[1])
        return "mp_call_import(%s, %s, %d, %s)" % (
            sm, sa, nargs, ", ".join(wrapped))

    def _mp_method_call(self, recv, attr, node):
        nargs = len(node.args)
        wrapped = [self.wrap_obj(a) for a in node.args]
        r = self.wrap_obj(recv) if not isinstance(recv, str) else recv
        sa = c_string(attr)
        if nargs == 0:
            return "mp_call_method(%s, %s, 0)" % (r, sa)
        if nargs == 1:
            return "mp_call_method(%s, %s, 1, %s)" % (r, sa, wrapped[0])
        if nargs == 2:
            return "mp_call_method(%s, %s, 2, %s, %s)" % (
                r, sa, wrapped[0], wrapped[1])
        return "mp_call_method(%s, %s, %d, %s)" % (
            r, sa, nargs, ", ".join(wrapped))

    def emit_typeinfo_struct(self):
        self.emit("/* Per-module type descriptor: header + vtable slots. */")
        self.emit("typedef struct TypeInfo {")
        self.indent += 1
        self.emit("const char* name;")
        self.emit("const struct TypeInfo* base;")
        self.emit("const FieldDesc* fields;")
        self.emit("obj (*tostr)(Obj*);")
        self.emit("bool (*eq)(Obj*, obj);")
        self.emit("obj (*addfn)(Obj*, obj);")
        self.emit("unsigned long objsize;")
        for m in sorted(VTABLE_METHODS):
            self.emit(self.vslot_signature(m) + ";")
        self.indent -= 1
        self.emit("} TypeInfo;")
        self.emit()
        self.emit("#define TYPE(o) (TYPEINFO(TypeInfo, (o)))")
        self.emit()

    def vslot_signature(self, mname):
        ret, params, _ = self.method_proto(mname)
        return "%s (*%s)(%s)" % (ret, vslot_name(mname), ", ".join(["Obj*"] + params))

    def _proto_from_fns(self, cands):
        """Uniform vtable slot proto (ret, params) from candidate FunctionDefs:
        widest positional arity, with kwonly/vararg/kwarg padding unioned over
        all candidates. `fndef` is the widest fn (used for param names)."""
        ret, params, fndef, n_kwonly = OBJ, [], None, 0
        has_vararg = has_kwarg = False
        for fn in cands:
            p = self._method_proto_params(fn)
            if len(p) > len(params) or fndef is None:
                params, fndef, ret = list(p), fn, self._c_ret(fn)
            n_kwonly = max(n_kwonly, len(fn.args.kwonlyargs))
            has_vararg = has_vararg or bool(fn.args.vararg)
            has_kwarg = has_kwarg or bool(fn.args.kwarg)
        params = list(params) + [OBJ] * n_kwonly
        if has_vararg:
            params.append(OBJ)
        if has_kwarg:
            params.append(OBJ)
        return ret, params, fndef

    def _hier_method_fns(self, root_key, mname):
        """Every FunctionDef for `mname` across the hierarchy rooted at
        `root_key` (the global source of truth for a slot's signature)."""
        classes_h, byname_h = project_class_hierarchy(self.base_dir)
        out = []
        for key in hier_members(classes_h, byname_h, root_key):
            fn = classes_h[key]["fns"].get(mname)
            if fn is not None:
                out.append(fn)
        return out

    def method_proto(self, mname):
        # A pinned cross-module hierarchy: the slot signature is the WIDEST
        # across the whole hierarchy, identical in every module, so all emitted
        # TypeInfos and replicated VT structs agree.
        rk = getattr(self, "_vt_root_key", None)
        if rk is not None:
            cands = self._hier_method_fns(rk, mname)
            if cands:
                return self._proto_from_fns(cands)
        cands = [ci.methods[mname] for ci in self.class_order
                 if mname in ci.methods]
        return self._proto_from_fns(cands)

    def _canon_vtable_param_names(self, fn, n):
        """Parameter names for the first `n` vtable positional slots of `fn`."""
        pos = fn.args.args[1:]
        out = []
        for i in range(n):
            if i < len(pos):
                out.append(self.pname(pos[i].arg))
            else:
                out.append("_vtpad%d" % i)
        return out

    def _method_proto_params(self, fn):
        """Positional params only (vtable slots share a uniform positional ABI).

        A class-pointer param (e.g. `NodeGraph*` from a bare `g: "NodeGraph"`
        annotation) is narrowed to the boxed `obj` ABI -- matching what the impl
        signature (_vtable_c_param_list) and enter_scope already do -- so the
        slot doesn't declare `NodeGraph*` while the impl takes `obj` (which made
        a caller pass an 8-byte pointer where the callee reads a 16-byte obj).
        The test is purely lexical (uppercase/underscore-led base), NOT a
        self.classes/xclasses membership check, so the slot signature is
        identical in every module and cross-module vtable layouts stay in sync.
        """
        out = []
        for a in fn.args.args[1:]:
            ct = arg_ctype(fn, a)
            if fn.name in VTABLE_METHODS and ct.endswith("*") and ct != OBJ \
                    and (ct[0].isupper() or ct[0] == "_"):
                ct = OBJ
            out.append(ct)
        return out

    def _vtable_kwonly_union(self, mname):
        n = 0
        for ci in self.class_order:
            fn = ci.methods.get(mname)
            if fn:
                n = max(n, len(fn.args.kwonlyargs))
        return n

    def _vtable_vararg_union(self, mname):
        for ci in self.class_order:
            fn = ci.methods.get(mname)
            if fn and fn.args.vararg:
                return True
        return False

    def _vtable_kwarg_union(self, mname):
        for ci in self.class_order:
            fn = ci.methods.get(mname)
            if fn and fn.args.kwarg:
                return fn.args.kwarg.arg
        return None

    def _vtable_c_param_list(self, fn):
        _, canon, _ = self.method_proto(fn.name)
        n_kw_union = self._vtable_kwonly_union(fn.name)
        n_vararg = 1 if self._vtable_vararg_union(fn.name) else 0
        n_kwarg = 1 if self._vtable_kwarg_union(fn.name) else 0
        npos_canon = len(canon) - n_kw_union - n_vararg - n_kwarg
        npos = max(npos_canon, max(0, len(fn.args.args) - 1))
        names = self._canon_vtable_param_names(fn, npos)
        parts = []
        for i in range(npos):
            if i < len(fn.args.args) - 1:
                arg = fn.args.args[i + 1]
                ct = self.arg_ctype_q(fn, arg)
                if fn.name in VTABLE_METHODS and self._is_class_ptr(ct):
                    ct = OBJ
                parts.append("%s %s" % (self.ctype_csym(ct), self.pname(arg.arg)))
            elif i < npos_canon:
                parts.append("%s %s" % (self.ctype_csym(canon[i]), cname(names[i])))
            else:
                parts.append("obj %s" % cname(names[i]))
        for a in fn.args.kwonlyargs:
            parts.append("obj %s" % self.pname(a.arg))
        for j in range(len(fn.args.kwonlyargs), n_kw_union):
            parts.append("obj _vtkw%d" % j)
        if fn.args.vararg:
            parts.append("obj %s" % self.pname(fn.args.vararg.arg))
        elif n_vararg:
            parts.append("obj _vtvarargs")
        if fn.args.kwarg:
            parts.append("obj %s" % self.pname(fn.args.kwarg.arg))
        elif n_kwarg:
            parts.append("obj _vtkwargs")
        return parts

    # ---- struct emission -------------------------------------------------

    def _project_subclassed_cached(self):
        """Cached _project_subclassed(): a global 'is this class a base anywhere'
        test. Local _class_is_leaf only sees this module's own and imported
        classes, so a root like Node (subclassed only in sibling modules) looks
        leaf here; this guards against devirtualizing such a class."""
        c = self.__dict__.get("_proj_subclassed_cache")
        if c is None:
            c = self._project_subclassed()
            self.__dict__["_proj_subclassed_cache"] = c
        return c

    def _project_subclassed(self):
        """Set of bare class names that are used as a base anywhere in the
        project (so they must not be POD; see _compute_pod_set)."""
        names = set()
        try:
            _pc, _bn = project_class_hierarchy(self.base_dir)
            for _key, _info in _pc.items():
                b = _info.get("base")
                if b:
                    names.add(b)
        except Exception:
            pass
        return names

    def _compute_pod_set(self, tree):
        """Decide which classes get the POD lowering (a bare struct passed by
        pointer, with malloc and no Obj header / vtable / runtime). A class is
        POD only if it has no base, no subclass, no class-level statics/attrs,
        and is never used as a value (isinstance target, passed/stored, or used
        as a base) -- anything but a direct constructor call `X(...)`. Computed
        early (before func/param ctypes) so a `Body*` annotation resolves to a
        plain struct pointer rather than the boxed obj ABI."""
        self._has_subclass = set()
        for ci in self.class_order:
            if getattr(ci, "base_name", None):
                self._has_subclass.add(ci.base_name)
        # Cross-module subclasses also disqualify POD: if *any* module in the
        # project extends this class, it must carry the Obj header so struct
        # layout and vtable dispatch agree across the module boundary (a class
        # may be POD in its own file yet subclassed elsewhere). See RPYTHON.md:
        # "the POD decision is propagated ... so layout and dispatch agree".
        self._has_subclass |= self._project_subclassed()
        class_names = {ci.name for ci in self.class_order}
        value_used = set()
        for parent in ast.walk(tree):
            for _f, child in ast.iter_fields(parent):
                kids = child if isinstance(child, list) else [child]
                for c in kids:
                    if isinstance(c, ast.Name) and c.id in class_names:
                        if isinstance(parent, ast.Call) and parent.func is c:
                            continue            # construction X(...) is fine
                        value_used.add(c.id)
        self._pod_set = set()
        for ci in self.class_order:
            if (self._pod_enabled
                    and not getattr(ci, "base_name", None)
                    and ci.name not in self._has_subclass
                    and ci.name not in value_used
                    and not getattr(ci, "const_dicts", None)
                    and not getattr(ci, "class_statics", None)
                    # __add__ is dispatched at runtime through the TypeInfo
                    # `addfn` slot, which a POD class does not have.
                    and "__add__" not in getattr(ci, "methods", {})
                    and not self._resolved_class_attrs(ci)):
                self._pod_set.add(ci.csym)
        # method AST nodes belonging to POD classes: their class-pointer params
        # must stay typed pointers (POD methods are direct, non-virtual calls).
        self._pod_method_nodes = set()
        for ci in self.class_order:
            if ci.csym in self._pod_set:
                for fn in ci.methods.values():
                    self._pod_method_nodes.add(id(fn))

    _FIELD_TC = {"int": "i", "long": "l", "short": "i", "char": "i",
                 "bool": "b", "double": "d", "float": "f", "char*": "s",
                 "str": "s", "obj": "o"}

    def _field_tc(self, ctype):
        """1-char storage code for a field's C type (see FieldDesc in runtime)."""
        if ctype in self._FIELD_TC:
            return self._FIELD_TC[ctype]
        return "p" if ctype.endswith("*") else "o"

    def emit_struct(self, ci):
        bn = (" : " + ci.base_name) if ci.base_name else ""
        self.emit("/* class %s%s */" % (ci.name, bn))
        self.emit("typedef struct %s {" % ci.csym)
        self.indent += 1
        if ci.csym not in self._pod_set:
            self.emit("Obj _hdr;")
        ff = ci.full_fields()
        if not ff:
            self.emit("char _empty;")
        uset = set(getattr(ci, "union_fields", []))
        bf = getattr(ci, "bitfields", {})
        emitted_union = False
        for fn, ft in ff:
            if fn in uset:
                if not emitted_union:    # emit the whole anonymous union once,
                    self.emit("union {")  # at the position of its first member
                    self.indent += 1
                    for ufn, uft in ff:
                        if ufn in uset:
                            self.emit("%s %s;" % (self.ctype_csym(uft),
                                                  self.fnsym(ufn)))
                    self.indent -= 1
                    self.emit("};")
                    emitted_union = True
                continue
            if fn in bf:                 # `T(N)` annotation -> C bit field
                self.emit("unsigned int %s : %d;" % (self.fnsym(fn), bf[fn]))
                continue
            self.emit("%s %s;" % (self.ctype_csym(ft), self.fnsym(fn)))
        self.indent -= 1
        self.emit("} %s;" % ci.csym)
        # Per-type field table for bridge-free rt_getattr/rt_setattr. Only
        # object-model classes (those carrying a TypeInfo) get one; POD structs
        # have no type pointer to reach it from.
        if ci.csym not in self._pod_set:
            self.emit_field_table(ci)
        self.emit()

    def emit_field_table(self, ci):
        bf = getattr(ci, "bitfields", {})   # offsetof is illegal on a bit field,
        rows = ["{ %s, offsetof(%s, %s), '%s' }" % (  # so such fields are not
            c_string(fn), ci.csym, self.fnsym(fn), self._field_tc(ft))  # reflectable
            for fn, ft in ci.full_fields() if fn not in bf]
        rows.append("{ NULL, 0, 0 }")
        self.emit("static const FieldDesc %s__fields[] = { %s };" % (
            ci.csym, ", ".join(rows)))

    # ---- class implementation -------------------------------------------

    def emit_class_impl(self, ci):
        prev = self.cur_class
        self.cur_class = ci
        for dname, dnode in ci.const_dicts.items():
            self.emit_const_dict(ci, dname, dnode)
        if "__init__" in ci.methods:
            self.emit_constructor(ci, ci.methods["__init__"])
        else:
            ni = self._nearest_init(ci)     # inherit __init__ but get own _new
            if ni is not None:
                owner, fn = ni
                self.emit_inherited_constructor(ci, owner, fn)
            else:
                self.emit_default_constructor(ci)
        for mname, fn in ci.methods.items():
            if mname == "__init__":
                continue
            if mname == "__str__" and mname not in ci.static_methods:
                self.emit_tostr_method(ci, fn)
                continue
            if mname == "__eq__" and mname not in ci.static_methods:
                self.emit_eq_method(ci, fn)
                continue
            if mname == "__add__" and mname not in ci.static_methods:
                self.emit_add_method(ci, fn)
                continue
            if mname.startswith("__") and mname.endswith("__"):
                if mname not in ("__enter__", "__exit__"):
                    # Dropping it silently is how `Position.__add__` went
                    # missing: `pos + 1` then fell through to numeric addition,
                    # reinterpreted the pointer as an integer, and crashed on
                    # the next dereference. Say so on stderr too -- a comment
                    # buried in generated C is not a diagnostic.
                    sys.stderr.write(
                        "py2c: %s.%s is not lowered; uses of the corresponding "
                        "operator will fall back to the generic path and may "
                        "produce wrong results\n" % (ci.name, mname))
                    self.emit("/* %s.%s: dunder not lowered in this pass */"
                              % (ci.name, mname))
                    self.emit()
                    continue
            self.emit_method(ci, fn, virtual=(mname in VTABLE_METHODS
                                              and ci.csym not in self._pod_set))
        self.emit_vtable(ci)
        self.cur_class = prev

    def emit_const_dict(self, ci, dname, dnode):
        keys, vals = dnode.keys, dnode.values
        all_str_keys = all(isinstance(k, ast.Constant) and
                           isinstance(k.value, str) for k in keys)
        all_int_keys = all(isinstance(k, ast.Constant) and
                           isinstance(k.value, int) for k in keys)
        list_vals = all(isinstance(v, ast.List) for v in vals)
        const_str_vals = all(isinstance(v, ast.Constant) and
                             isinstance(v.value, str) for v in vals)
        if all_str_keys and list_vals:
            self.emit("/* const dict %s.%s : str -> list[str] */" %
                      (ci.name, dname))
            self.emit("str %s_%s(str key, int i) {" % (ci.name, dname))
            self.indent += 1
            for k, v in zip(keys, vals):
                items = ", ".join(c_string(e.value) for e in v.elts
                                  if isinstance(e, ast.Constant))
                self.emit('if (!strcmp(key, %s)) { static const char* _r[] = {%s}; return (str)_r[i]; }'
                          % (c_string(k.value), items))
            self.emit('return (str)"";')
            self.indent -= 1
            self.emit("}")
            # Iteration helpers: the lookup function above only serves the
            # `D[key][i]` fast path. `.items()/.keys()/.values()` need the data
            # as real containers, so build them from the same compile-time
            # entries (works cross-module via an extern call into this module).
            self.emit("obj %s_%s_items(void) {" % (ci.name, dname))
            self.indent += 1
            self.emit("obj _r = list_new();")
            for k, v in zip(keys, vals):
                strs = [c_string(e.value) for e in v.elts
                        if isinstance(e, ast.Constant)]
                self.emit("{ obj _v = list_new();%s obj _p = list_new(); "
                          "list_append(_p, OBJ_STR(%s)); list_append(_p, _v); "
                          "list_append(_r, _p); }"
                          % ("".join(" list_append(_v, OBJ_STR(%s));" % s
                                     for s in strs), c_string(k.value)))
            self.emit("return _r;")
            self.indent -= 1
            self.emit("}")
            self.emit("obj %s_%s_keys(void) {" % (ci.name, dname))
            self.indent += 1
            self.emit("obj _r = list_new();")
            for k in keys:
                self.emit("list_append(_r, OBJ_STR(%s));" % c_string(k.value))
            self.emit("return _r;")
            self.indent -= 1
            self.emit("}")
            self.emit("obj %s_%s_values(void) {" % (ci.name, dname))
            self.indent += 1
            self.emit("obj _r = list_new();")
            for v in vals:
                strs = [c_string(e.value) for e in v.elts
                        if isinstance(e, ast.Constant)]
                self.emit("{ obj _v = list_new();%s list_append(_r, _v); }"
                          % "".join(" list_append(_v, OBJ_STR(%s));" % s
                                    for s in strs))
            self.emit("return _r;")
            self.indent -= 1
            self.emit("}")
            self.emit()
            return
        if all_int_keys and const_str_vals:
            self.emit("/* const dict %s.%s : int -> str */" % (ci.name, dname))
            self.emit("str %s_%s_get(long key, str dflt) {" %
                      (ci.name, dname))
            self.indent += 1
            for k, v in zip(keys, vals):
                self.emit("if (key == %d) return %s;" %
                          (k.value, c_string(v.value)))
            self.emit("return dflt;")
            self.indent -= 1
            self.emit("}")
            self.emit()
            return
        self.emit("/* const dict %s.%s: shape not lowered */" %
                  (ci.name, dname))
        self.emit()

    def _resolved_class_attrs(self, ci):
        """attr -> value AST node for `ci`, walking root->leaf so a subclass
        override wins over an inherited default."""
        chain = []
        c = ci
        while c:
            chain.append(c)
            c = c.base
        res = {}
        for c in reversed(chain):
            for k, v in c.class_attrs.items():
                res[k] = v
        return res

    def _resolve_class_default(self, ci, attr, seen=None):
        """Resolve a class-level attribute's default value, following bare-Name
        references to sibling class attributes (so `all_registers =
        alloc_registers` resolves through to alloc_registers's own default).
        Returns an AST value node, or None if `attr` has no class default."""
        seen = seen if seen is not None else set()
        attrs = self._resolved_class_attrs(ci)
        val = attrs.get(attr)
        if isinstance(val, ast.Name) and val.id in attrs and val.id not in seen:
            seen.add(val.id)
            return self._resolve_class_default(ci, val.id, seen)
        return val

    def _lookup_imported_const(self, name, ci=None):
        """Resolve a bare Name to a C literal from an imported/base module."""
        mods = set(self.from_imports.values()) | set(self.import_alias.values())
        if ci:
            c = ci
            while c:
                bn = c.name
                if bn in self.xclasses:
                    mods.add(self.xclasses[bn][1])
                c = c.base
        for mod in mods:
            if not mod:
                continue
            reg = self.load_xmod(mod)
            if reg and name in reg.get("consts", {}):
                return self.const_literal(reg["consts"][name])
        return None

    def _expr_class_default(self, ci, dflt, ft):
        """Emit a class-attribute default value, resolving cross-module consts."""
        if isinstance(dflt, ast.Name) and dflt.id not in self.scope \
                and dflt.id not in self.mod_global_names:
            lit = self._lookup_imported_const(dflt.id, ci)
            if lit is not None:
                if ft == OBJ:
                    if lit in ("0", "1") and lit == "1":
                        return "OBJ_BOOL(true)"
                    if lit in ("0", "1"):
                        return "OBJ_BOOL(false)"
                    if lit.isdigit() or (lit.startswith("-") and lit[1:].isdigit()):
                        return "OBJ_INT(%s)" % lit
                    return "OBJ_STR(%s)" % lit
                return lit
        s = self.expr(dflt)
        if ft and ft != OBJ:
            return self.coerce_to(ft, dflt, s)
        return self.wrap_obj(dflt) if ft == OBJ or not ft else s

    def emit_class_attr_init(self, ci):
        """Set the instance fields backing class-level scalar attributes to this
        class's most-derived value (polymorphic class data made per-instance)."""
        attrs = self._resolved_class_attrs(ci)
        if not attrs:
            return
        prev = self.cur_class
        self.cur_class = ci
        for nm, val in sorted(attrs.items()):
            # a default that names a sibling class attr resolves to that
            # sibling's own default value, not a (nonexistent) bare local.
            dflt = self._resolve_class_default(ci, nm)
            ft = ci.field_ctype(nm)
            sval = self._expr_class_default(ci, dflt, ft)
            self.emit("self->%s = %s;" % (cname(nm), sval))
        self.cur_class = prev

    def emit_class_static_instance_init(self, ci):
        """Copy class-level list/dict statics into instance fields when the
        struct declares a slot (Python: self._iv finds the class variable)."""
        for nm in ci.class_statics:
            if any(fn == nm for fn, _ in ci.full_fields()):
                self.emit("self->%s = %s_%s;" % (
                    cname(nm), ci.csym, cname(nm)))

    def _nearest_init(self, ci):
        """(owner, __init__ fn) for the nearest class in ci's chain that defines
        __init__, or None."""
        c = ci
        while c:
            if "__init__" in c.methods:
                return c, c.methods["__init__"]
            c = c.base
        return None

    def _project_nearest_init(self, ci):
        """Nearest `__init__` FunctionDef walking ci's base chain through the
        project-wide class scan. Needed for the ctor trampoline of an *imported*
        class whose `__init__` is inherited from a base that was never loaded as
        an xclass in this module (so _nearest_init's local walk stops short and
        the trampoline would call Class_new() with no args -> garbage fields)."""
        if ci is None:
            return None
        classes_h, byname_h = project_class_hierarchy(self.base_dir)
        mod = (self.xclass_module.get(ci.name)
               or (self.xclasses[ci.name][1] if ci.name in self.xclasses
                   else None)
               or self.modname)
        key = _hier_resolve(classes_h, byname_h, mod, ci.name)
        seen = set()
        while key and key not in seen:
            seen.add(key)
            entry = classes_h.get(key)
            if entry is None:
                return None
            fn = entry["fns"].get("__init__")
            if fn is not None:
                return fn
            bn = entry["base"]
            if not bn:
                return None
            key = _hier_resolve(classes_h, byname_h, key[0], bn)
        return None

    def emit_constructor(self, ci, fn):
        self.enter_scope(fn, skip_self=True)
        init_params = self._init_param_list(fn, skip_self=True)
        init_sig = ", ".join(["%s* self" % ci.csym] + init_params)
        argnames = [self.pname(a.arg) for a in fn.args.args[1:]]
        if fn.args.vararg:
            argnames.append(self.pname(fn.args.vararg.arg))
        if fn.args.kwarg:
            argnames.append(self.pname(fn.args.kwarg.arg))
        for a in fn.args.kwonlyargs:
            argnames.append(self.pname(a.arg))
        self.emit("void %s___init__(%s) {" % (ci.csym, init_sig))
        self.indent += 1
        self.cur_ret = "void"
        self.emit_hoisted_body(fn.body)
        self.indent -= 1
        self.emit("}")
        self.emit()
        if fn.args.vararg:
            vn = fn.args.vararg.arg
            plist = "int _n_%s, ..." % vn
        else:
            plist = ", ".join(self.param_list(fn, skip_self=True) +
                              self._kwonly_param_list(fn)) or "void"
        self.emit("%s* %s_new(%s) {" % (ci.csym, ci.csym, plist))
        self.indent += 1
        if ci.csym in self._pod_set:
            # Arena, not libc malloc. A POD is a bare struct with no Obj header,
            # so it is usually small -- minipy's V is 16 bytes -- and glibc
            # rounds a 16-byte request up to a 32-byte chunk plus bookkeeping,
            # so half the memory was allocator overhead. The arena hands out
            # exactly the aligned size with no header, and `del` already lowers
            # to afree(), which previously silently ignored malloc'd pointers
            # and leaked them. Non-POD classes have always used aalloc; this
            # just makes the two paths agree.
            self.emit("%s* self = aalloc(sizeof *self);" % ci.csym)
        else:
            self.emit("%s* self = aalloc(sizeof *self);" % ci.csym)
            self.emit("((Obj*)self)->type = &%s_type;" % ci.csym)
        if fn.args.vararg:
            vn = fn.args.vararg.arg
            self.emit("va_list _ap; va_start(_ap, _n_%s);" % vn)
            self.emit("obj %s = varg_list(_n_%s, _ap);" % (cname(vn), vn))
            self.emit("va_end(_ap);")
        # Class-level attributes (constants like AT/GOT/AFTER and list/dict
        # statics) exist before any __init__ runs in Python, and __init__ may
        # read them (e.g. ParserError.__init__ dispatches on self.AT/AFTER), so
        # initialize them BEFORE calling __init__. Doing it after left those
        # fields uninitialized during __init__, which read garbage.
        self.emit_class_attr_init(ci)
        self.emit_class_static_instance_init(ci)
        self.emit("%s___init__(%s);" % (ci.csym,
                                        ", ".join(["self"] + argnames)))
        self.emit("return self;")
        self.indent -= 1
        self.emit("}")
        self.emit()

    def emit_default_constructor(self, ci):
        """Classes with no __init__ still need a _new for ctor calls."""
        self.emit("%s* %s_new(void) {" % (ci.csym, ci.csym))
        self.indent += 1
        if ci.csym in self._pod_set:
            self.emit("%s* self = aalloc(sizeof *self);" % ci.csym)   # see above
        else:
            self.emit("%s* self = aalloc(sizeof *self);" % ci.csym)
            self.emit("((Obj*)self)->type = &%s_type;" % ci.csym)
        self.emit_class_attr_init(ci)
        self.emit_class_static_instance_init(ci)
        self.emit("return self;")
        self.indent -= 1
        self.emit("}")
        self.emit()

    def emit_inherited_constructor(self, ci, owner, fn):
        """A concrete class with no own __init__ still needs its own _new so it
        sets its own type pointer and its own class-attr values; it delegates
        construction to the nearest inherited __init__."""
        if fn.args.vararg:
            vn = fn.args.vararg.arg
            plist = "int _n_%s, ..." % vn
        else:
            plist = ", ".join(self.param_list(fn, skip_self=True) +
                              self._kwonly_param_list(fn)) or "void"
        argnames = [self.pname(a.arg) for a in fn.args.args[1:]]
        if fn.args.kwarg:
            argnames.append(self.pname(fn.args.kwarg.arg))
        for a in fn.args.kwonlyargs:
            argnames.append(self.pname(a.arg))
        self.emit("%s* %s_new(%s) {" % (ci.csym, ci.csym, plist))
        self.indent += 1
        self.emit("%s* self = aalloc(sizeof *self);" % ci.csym)
        self.emit("((Obj*)self)->type = &%s_type;" % ci.csym)
        if owner.name not in self.classes:
            self.xstructs_needed.add(owner.name)
            if "__init__" in owner.methods:
                self.used_xmethods[(owner.name, "__init__")] = "void"
        self.emit_class_attr_init(ci)
        self.emit_class_static_instance_init(ci)
        self.emit("%s___init__((%s*)self%s);" % (
            owner.csym, owner.csym,
            (", " + ", ".join(argnames)) if argnames else ""))
        self.emit("return self;")
        self.indent -= 1
        self.emit("}")
        self.emit()

    def emit_method(self, ci, fn, virtual):
        static = fn.name in ci.static_methods
        classmethod = fn.name in getattr(ci, "classmethod_methods", set())
        self.enter_scope(fn, skip_self=not static)
        ret = self._c_ret(fn)
        self.cur_ret = ret
        params = self.param_list(fn, skip_self=not static)
        if static:                          # @staticmethod: no receiver at all
            plist = ", ".join(params) if params else "void"
            self.emit("%s %s_%s(%s) {" % (ret, ci.csym, method_cname(fn.name), plist))
            self.indent += 1
        elif virtual:
            vparams = self._vtable_c_param_list(fn)
            self.emit("%s %s_%s(Obj* self_%s) {" % (
                ret, ci.csym, method_cname(fn.name),
                (", " + ", ".join(vparams)) if vparams else ""))
            self.indent += 1
            _, canon, _ = self.method_proto(fn.name)
            n_kw_union = self._vtable_kwonly_union(fn.name)
            n_vararg = 1 if self._vtable_vararg_union(fn.name) else 0
            n_kwarg = 1 if self._vtable_kwarg_union(fn.name) else 0
            npos_canon = len(canon) - n_kw_union - n_vararg - n_kwarg
            npos = max(npos_canon, max(0, len(fn.args.args) - 1))
            pad_names = self._canon_vtable_param_names(fn, npos)
            if classmethod:
                self.emit("(void)self_;")
                cls_nm = fn.args.args[0].arg
                self.emit("obj %s = make_closure(&%s__ctortramp, OBJ_NONE);" % (
                    cname(cls_nm), ci.csym))
                self.scope[cls_nm] = OBJ
            else:
                self.emit("%s* self = (%s*)self_;" % (ci.csym, ci.csym))
                self.emit("(void)self;")
            for i in range(max(0, len(fn.args.args) - 1), npos):
                self.emit("(void)%s;" % cname(pad_names[i]))
            for j in range(len(fn.args.kwonlyargs), n_kw_union):
                self.emit("(void)_vtkw%d;" % j)
            if n_vararg and not fn.args.vararg:
                self.emit("(void)_vtvarargs;")
            if n_kwarg and not fn.args.kwarg:
                self.emit("(void)_vtkwargs;")
            if fn.args.kwarg:
                self.scope[fn.args.kwarg.arg] = OBJ
        else:
            plist = ["%s* self" % ci.csym] + params
            self.emit("%s %s_%s(%s) {" % (ret, ci.csym, method_cname(fn.name),
                                          ", ".join(plist)))
            self.indent += 1
        if fn.args.vararg:
            self._note_vararg(fn.args.vararg.arg)
            if virtual:
                self.scope[fn.args.vararg.arg] = OBJ
            else:
                self._emit_vararg_setup(fn)
        self.emit_hoisted_body(fn.body)
        self.indent -= 1
        self.emit("}")
        self.emit()

    def emit_tostr_method(self, ci, fn):
        """Lower `__str__` to a standalone `obj <Class>___str__(Obj*)` so that
        pystr can dispatch it through the TypeInfo `tostr` slot (str()/print() of
        an object). __str__ is otherwise an unlowered dunder."""
        self.enter_scope(fn, skip_self=True)
        self.cur_ret = OBJ
        self.emit("obj %s___str__(Obj* self_) {" % ci.csym)
        self.indent += 1
        self.emit("%s* self = (%s*)self_;" % (ci.csym, ci.csym))
        self.emit("(void)self;")
        self.emit_hoisted_body(fn.body)
        self.indent -= 1
        self.emit("}")
        self.emit()

    def emit_eq_method(self, ci, fn):
        """Lower `__eq__` to a standalone `bool <Class>___eq__(Obj*, obj)` so
        obj_eq can dispatch value-equality through the TypeInfo `eq` slot. Like
        __str__, __eq__ is otherwise an unlowered dunder; without this, `==`,
        `in`, dict/set keys on such objects fall back to pointer identity."""
        other = fn.args.args[1].arg if len(fn.args.args) > 1 else "other"
        self.enter_scope(fn, skip_self=True)
        self.cur_ret = "bool"
        self.emit("bool %s___eq__(Obj* self_, obj %s) {" % (ci.csym, cname(other)))
        self.indent += 1
        self.emit("%s* self = (%s*)self_;" % (ci.csym, ci.csym))
        self.emit("(void)self;")
        self.emit_hoisted_body(fn.body)
        self.indent -= 1
        self.emit("}")
        self.emit()

    def emit_add_method(self, ci, fn):
        """Lower `__add__` to a standalone `obj <Class>___add__(Obj*, obj)` so
        obj_add can dispatch through the TypeInfo `addfn` slot. Otherwise it is
        an unlowered dunder and `a + b` on such objects falls through to
        numeric addition, which reinterprets the pointer as an integer and
        hands back a value that crashes on the next dereference."""
        other = fn.args.args[1].arg if len(fn.args.args) > 1 else "other"
        self.enter_scope(fn, skip_self=True)
        self.cur_ret = "obj"
        self.emit("obj %s___add__(Obj* self_, obj %s) {"
                  % (ci.csym, cname(other)))
        self.indent += 1
        self.emit("%s* self = (%s*)self_;" % (ci.csym, ci.csym))
        self.emit("(void)self;")
        self.emit_hoisted_body(fn.body)
        self.indent -= 1
        self.emit("}")
        self.emit()

    def _vtable_slot_fn(self, m, owner):
        """Function pointer for a TypeInfo's `m` slot. Normally owner.csym_<m>,
        but when that impl declares MORE positional params than the (canonical,
        narrower) slot -- an override with extra defaulted args, e.g.
        Compound.make_il's `no_scope=False` -- the raw 4-arg function can't sit
        in a 3-arg slot (virtual calls would leave the extra arg uninitialized
        and read stack garbage). Emit a thunk matching the slot that forwards to
        the impl, supplying the missing args' defaults, and use it instead.
        Direct calls that pass the extra arg still call the impl directly."""
        base = "%s_%s" % (owner.csym, method_cname(m))
        impl = owner.methods.get(m)
        if impl is None or owner.name not in self.classes:
            return base                     # imported impl: keep the extern name
        _, slot_pct, _ = self.method_proto(m)
        pos = impl.args.args[1:]
        if len(pos) <= len(slot_pct):
            return base
        # Only forward through a thunk when the impl's leading params match the
        # slot's types (a genuine extra-optional-arg override). If they differ
        # -- `m` is ambiguous across UNRELATED classes and the canonical slot
        # belongs to a sibling with different param types (e.g. DeclInfo.
        # get_storage's `int defined` vs Declaration's) -- a thunk would pass
        # mismatched types; leave the raw impl in the slot (it is only ever
        # reached by direct dispatch anyway).
        impl_narrowed = self._method_proto_params(impl)
        if impl_narrowed[:len(slot_pct)] != list(slot_pct):
            return base
        ad = "%s__vtad" % base
        emitted = self.__dict__.setdefault("_emitted_vtads", set())
        if ad not in emitted:
            emitted.add(ad)
            ret = self._c_ret(impl)
            names = self._canon_vtable_param_names(impl, len(slot_pct))
            params = ["Obj* self"]
            for i in range(len(slot_pct)):
                params.append("%s %s" % (self.ctype_csym(slot_pct[i]),
                                         cname(names[i])))
            callargs = ["self"] + [cname(n) for n in names]
            defs = self.defaults_for(impl, True)
            for i in range(len(slot_pct), len(pos)):
                dnode = defs[i] if i < len(defs) else None
                pct = self.arg_ctype_q(impl, pos[i])
                if isinstance(dnode, ast.Constant):
                    callargs.append(self.coerce_to(pct, dnode, self.expr(dnode)))
                else:
                    callargs.append("OBJ_NONE")
            self.emit("%s %s(%s) {" % (ret, ad, ", ".join(params)))
            self.emit("    return %s(%s);" % (base, ", ".join(callargs)))
            self.emit("}")
        return ad

    def emit_vtable(self, ci):
        if ci.csym in self._pod_set:
            return                          # POD class: no TypeInfo / vtable
        # Only a base that is a known class in this module gets a type pointer;
        # external/builtin bases (e.g. Exception) become NULL.
        base = "NULL"
        if ci.base:
            base = "&%s_type" % ci.base.csym
            if ci.base.name not in self.classes:        # imported base type
                self.xtype_externs.add(ci.base.name)
        slots = []
        for m in sorted(VTABLE_METHODS):
            # MI-aware: a method inherited from a *secondary* base (e.g.
            # `class Cast(Declaration, _RExprNode)` inheriting lvalue/make_il
            # from _RExprNode) must still fill its vtable slot. Using the
            # primary-chain-only lookup here emitted NULL, so any virtual call
            # through that slot jumped to address 0.
            owner = ci.find_method_owner_mi(m)
            # Resolving through a *secondary* base is only sound for stateless
            # mixins: only the primary base's fields form the struct prefix, so
            # a secondary-base method that reads `self.<field>` would index a
            # layout the object does not have. Flag it rather than miscompile.
            if owner and owner is not ci.find_method_owner(m):
                impl = owner.methods.get(m)
                if impl is not None and _reads_self_fields(impl):
                    sys.stderr.write(
                        "py2c: %s inherits %s() from secondary base %s, which "
                        "reads self fields; only the primary base's fields are "
                        "in the struct prefix, so this dispatch is unsound\n"
                        % (ci.name, m, owner.name))
            if owner and owner.name not in self.classes:  # imported impl
                self.xvtable_impls.add((owner.name, m))
            slots.append(".%s = %s" % (vslot_name(m), self._vtable_slot_fn(m, owner)
                                       if owner else "NULL"))
        str_owner = ci.find_method_owner("__str__")
        if str_owner and "__str__" not in str_owner.static_methods:
            tostr = "%s___str__" % str_owner.csym
            if str_owner.name not in self.classes:      # imported __str__ impl
                self._tostr_externs.add(str_owner.csym)
        else:
            tostr = "NULL"
        eq_owner = ci.find_method_owner("__eq__")
        if eq_owner and "__eq__" not in eq_owner.static_methods:
            eqfn = "%s___eq__" % eq_owner.csym
            if eq_owner.name not in self.classes:       # imported __eq__ impl
                self._eq_externs.add(eq_owner.csym)
        else:
            eqfn = "NULL"
        add_owner = ci.find_method_owner_mi("__add__")
        if add_owner and "__add__" not in add_owner.static_methods:
            addfn = "%s___add__" % add_owner.csym
            if add_owner.name not in self.classes:      # imported __add__ impl
                self._add_externs.add(add_owner.csym)
        else:
            addfn = "NULL"
        init = ", ".join([".name = %s" % c_string(ci.name),
                          ".base = (const struct TypeInfo*)%s" % base,
                          ".fields = %s__fields" % ci.csym,
                          ".tostr = %s" % tostr,
                          ".eq = %s" % eqfn,
                          ".addfn = %s" % addfn,
                          ".objsize = sizeof(%s)" % ci.csym] + slots)
        self.emit("const TypeInfo %s_type = { %s };" % (ci.csym, init))
        self.emit()

    def _emit_vararg_setup(self, fn):
        """Materialize *args into a list local at function entry."""
        if not fn.args.vararg:
            return
        vn = fn.args.vararg.arg
        cn = cname(vn)
        self.scope[vn] = OBJ
        if fn.name in getattr(self, "closure_specs", {}):
            return
        self.hoisted.add(vn)
        self.emit("va_list _ap_%s; va_start(_ap_%s, _n_%s);" % (vn, vn, vn))
        self.emit("obj %s = varg_list(_n_%s, _ap_%s);" % (cn, vn, vn))
        self.emit("va_end(_ap_%s);" % vn)

    def param_list(self, fn, skip_self):
        params = []
        args = fn.args.args[1:] if skip_self else fn.args.args
        for arg in args:
            ct = self.arg_ctype_q(fn, arg)
            if fn.name in VTABLE_METHODS and self._is_class_ptr(ct) \
                    and id(fn) not in self._pod_method_nodes:
                ct = OBJ
            params.append("%s %s" % (self.ctype_csym(ct), self.pname(arg.arg)))
        if fn.args.kwarg:
            params.append("obj %s" % cname(fn.args.kwarg.arg))
        if fn.args.vararg:
            if fn.name in getattr(self, "closure_specs", {}):
                params.append("obj %s" % cname(fn.args.vararg.arg))
            else:
                params.append("int _n_%s" % fn.args.vararg.arg)
                params.append("...")
        return params

    def _init_param_list(self, fn, skip_self):
        """__init__ parameter list: *vararg becomes a single obj list param."""
        params = []
        args = fn.args.args[1:] if skip_self else fn.args.args
        for arg in args:
            ct = self.arg_ctype_q(fn, arg)
            if fn.name in VTABLE_METHODS and self._is_class_ptr(ct) \
                    and id(fn) not in self._pod_method_nodes:
                ct = OBJ
            params.append("%s %s" % (self.ctype_csym(ct), self.pname(arg.arg)))
        if fn.args.kwarg:
            params.append("obj %s" % cname(fn.args.kwarg.arg))
        if fn.args.vararg:
            params.append("obj %s" % cname(fn.args.vararg.arg))
        params.extend(self._kwonly_param_list(fn))
        return params

    def _kwonly_param_list(self, fn):
        return ["obj %s" % self.pname(a.arg) for a in fn.args.kwonlyargs]

    def enter_scope(self, fn, skip_self):
        self.scope = {}
        self.hoisted = set()
        self.narrowed = {}          # name -> ctype, active in an isinstance block
        self.elem_types = {}        # list var -> element ctype (from List[T])
        self.array_sizes = {}       # array var -> fixed element count N (T[N])
        args = fn.args.args[1:] if skip_self else fn.args.args
        for arg in args:
            ct = self.arg_ctype_q(fn, arg)
            sz = ann_array_size(arg.annotation)
            if sz:
                self.array_sizes[arg.arg] = sz
            if fn.name in VTABLE_METHODS and self._is_class_ptr(ct) \
                    and id(fn) not in self._pod_method_nodes:
                # boxed `obj` param (vtable ABI) with a known concrete class:
                # keep it obj, but narrow so member/method access resolves to
                # the typed struct (unwrapping with AS_OBJ at each use).
                self.scope[arg.arg] = OBJ
                self.narrowed[arg.arg] = ct
                cls = ct[:-1]
                if cls in self.xclasses and cls not in self.classes:
                    self.xstructs_needed.add(cls)   # typed casts need its struct
            else:
                self.scope[arg.arg] = ct
            et = ann_elem_ctype(arg.annotation)
            if et:
                self.elem_types[arg.arg] = et
        if fn.args.vararg:
            self.scope[fn.args.vararg.arg] = OBJ
            self._note_vararg(fn.args.vararg.arg)
        if fn.args.kwarg:
            self.scope[fn.args.kwarg.arg] = OBJ
        ko = fn.args.kwonlyargs
        kd = fn.args.kw_defaults
        for i, arg in enumerate(ko):
            di = i - (len(ko) - len(kd))
            dflt = kd[di] if di >= 0 else None
            ct = self.arg_ctype_q(fn, arg)
            if dflt is not None and ct in ("int", "bool", "char*") and \
                    self.value_ctype(dflt) == OBJ:
                ct = OBJ
            self.scope[arg.arg] = ct or OBJ

    def iter_elem_ctype(self, node):
        """Element ctype of an iterable expression when known from a List[T]
        annotation (a bare list Name, or self.<field> declared List[T])."""
        if isinstance(node, ast.BoolOp):           # `xs or []` defaulting idiom
            for v in node.values:
                et = self.iter_elem_ctype(v)
                if et:
                    return et
            return None
        if isinstance(node, ast.Name):
            return self.elem_types.get(node.id)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)\
                and node.value.id == "self" and self.cur_class:
            return getattr(self.cur_class, "field_elem_types", {}).get(node.attr)
        if isinstance(node, ast.Call):
            # `for x in f(...)` where f is annotated `-> list[T]`: x has type T.
            # Resolves both local functions and imported `mod.f` calls.
            f, fn = node.func, None
            if isinstance(f, ast.Name) and f.id in self.func_nodes:
                fn = self.func_nodes[f.id]
            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                    and f.value.id in self.import_alias:
                kind, info = self.resolve_import(f.attr,
                                                 self.import_alias[f.value.id])
                if kind == "func":
                    fn = info
            if fn is not None and getattr(fn, "returns", None) is not None:
                return ann_elem_ctype(fn.returns)
        return None

    def _float_locals(self, body):
        """Locals that are real-valued, found by fixpoint: a local is double if
        some assignment to it is a float literal, a `/` true-division, a
        float()/known float-returning call, or arithmetic that involves another
        float local -- and no assignment is clearly non-numeric. This breaks the
        single-pass chicken-and-egg where `acc = acc + ...` looked like obj
        because `acc` was not yet known to be double."""
        assigns = {}
        for stmt in body:
            for sub in ast.walk(stmt):
                tgt = None
                if isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            assigns.setdefault(t.id, []).append(sub.value)
                elif isinstance(sub, ast.AugAssign) and \
                        isinstance(sub.target, ast.Name):
                    assigns.setdefault(sub.target.id, []).append(sub.value)

        def non_numeric(node):
            if isinstance(node, ast.Constant):
                return not isinstance(node.value, (int, float, bool))
            if isinstance(node, (ast.List, ast.Dict, ast.Set,
                                 ast.Tuple, ast.ListComp, ast.DictComp,
                                 ast.SetComp, ast.JoinedStr)):
                return True
            return False

        floats = set()

        def is_float(node):
            if isinstance(node, ast.Constant):
                return isinstance(node.value, float)
            if isinstance(node, ast.Name):
                return node.id in floats
            if isinstance(node, ast.BinOp):
                if isinstance(node.op, ast.Div):     # true division -> float
                    return True
                return is_float(node.left) or is_float(node.right)
            if isinstance(node, ast.UnaryOp):
                return is_float(node.operand)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                return node.func.id in MATH_FUNCS or node.func.id == "float"
            if isinstance(node, ast.Call) and isinstance(node.func,
                                                         ast.Attribute):
                return node.func.attr in MATH_FUNCS    # math.sin / np.exp
            return False

        changed = True
        while changed:
            changed = False
            for name, vals in assigns.items():
                if name in floats or name in self.scope:
                    continue
                if any(non_numeric(v) for v in vals):
                    continue                 # ever holds a non-number -> not double
                if any(is_float(v) for v in vals):
                    floats.add(name)
                    changed = True
        return floats

    def hoist_locals(self, body):
        """Find all assigned locals and their types, to declare at function top
        (Python has function scope; C blocks would otherwise lose them)."""
        order = []
        types = {}
        self._hoisting = types          # let value_ctype see earlier inferences
        float_locals = self._float_locals(body)
        # Names declared `global` in this function are NOT locals: a write to
        # one must store into the module global, never a shadowing local decl.
        gnames = set()
        for stmt in body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Global):
                    gnames.update(sub.names)

        def consider(name, ctype):
            if name in self.scope or name in gnames:
                return
            if name in float_locals:            # pinned real-valued
                if name not in types:
                    order.append(name)
                types[name] = "double"
                return
            if name not in types:
                order.append(name)
                types[name] = ctype
            elif ctype == OBJ or types[name] == OBJ:
                types[name] = OBJ
            elif types[name] != ctype:
                w = _int_widen(types[name], ctype)   # int+long -> long, etc.,
                types[name] = w if w else OBJ        # rather than boxing to obj

        # pre-scan typed-list annotations so a `for x in <list>` below can give
        # x the element type even when the list is a local declared earlier.
        for stmt in body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.AnnAssign) and \
                        isinstance(sub.target, ast.Name):
                    et = ann_elem_ctype(sub.annotation)
                    if et:
                        self.elem_types[sub.target.id] = et
                elif isinstance(sub, ast.Assign):
                    # `xs = f(...)` where f is annotated `-> list[T]`: a later
                    # `for x in xs` can then give x the element type T.
                    et = self.iter_elem_ctype(sub.value)
                    if et:
                        for t in sub.targets:
                            if isinstance(t, ast.Name):
                                self.elem_types[t.id] = et

        for stmt in body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            if t.id in getattr(self, "ctypes_bind", ()):
                                continue    # FFI binding: not a real local
                            if isinstance(sub.value, ast.Name) and \
                                    sub.value.id in STDLIB_BUILTINS:
                                ct = OBJ
                            else:
                                ct = self.value_ctype(sub.value) or \
                                    infer_from_name(t.id) or OBJ
                            consider(t.id, ct)
                        elif isinstance(t, (ast.Tuple, ast.List)):
                            for el in t.elts:
                                if isinstance(el, ast.Name) and el.id != "_":
                                    consider(el.id, OBJ)
                elif isinstance(sub, ast.AnnAssign) and \
                        isinstance(sub.target, ast.Name):
                    _et = ann_elem_ctype(sub.annotation)
                    if not _et:
                        _kv = ann_dict_kv(sub.annotation)
                        if _kv:
                            _et = _kv[0]      # iterating a dict yields keys
                    if _et:
                        self.elem_types.setdefault(sub.target.id, _et)
                    consider(sub.target.id,
                             self._local_ann_ctype(sub.target.id,
                                                    sub.annotation))
                elif isinstance(sub, ast.For):
                    if isinstance(sub.target, ast.Name):
                        is_range = isinstance(sub.iter, ast.Call) and \
                            isinstance(sub.iter.func, ast.Name) and \
                            sub.iter.func.id == "range"
                        et = None if is_range else self.iter_elem_ctype(sub.iter)
                        consider(sub.target.id,
                                 "int" if is_range else (et or OBJ))
                    elif isinstance(sub.target, (ast.Tuple, ast.List)):
                        for el in sub.target.elts:
                            if isinstance(el, ast.Name) and el.id != "_":
                                consider(el.id, OBJ)
        result = [(n, types[n]) for n in order]
        self._hoisting = None
        return result

    def emit_hoisted_body(self, body):
        for name, ct in self.hoist_locals(body):
            self.scope[name] = ct
            self.hoisted.add(name)
            self.emit("%s %s;" % (self.ctype_csym(ct), self.pname(name)))
        self.emit_body(body)
        # A Python function that can fall off the end implicitly returns None.
        # Emit the matching default return so a non-void C function never leaves
        # an uninitialised (garbage, usually truthy) value in the return
        # register -- which otherwise makes callers that test the result loop
        # forever (e.g. `while _coalesce_once(g): ...` on an empty graph).
        ret = getattr(self, "cur_ret", OBJ)
        if ret and ret != "void" and not _stmts_always_exit(body):
            if ret in (OBJ, "obj"):
                self.emit("return OBJ_NONE;")
            elif ret.endswith("*"):
                self.emit("return NULL;")
            else:
                self.emit("return 0;")

    # ---- top level (non-class) -------------------------------------------

    def toplevel(self, node):
        if getattr(self, "mod_init_stmts", None) and node in self.mod_init_stmts:
            return                          # emitted inside <module>_init()
        if id(node) in getattr(self, "ctypes_skip", ()):
            return                          # ctypes config: compile-time only
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            doc = node.value.value.strip().splitlines()
            if doc:
                self.emit("/* " + doc[0] + " */")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            self.emit("/* " + self.src1(node) + " */")
        elif isinstance(node, ast.FunctionDef):
            if node.name not in RUNTIME_INTRINSICS:
                if self.func_nodes.get(node.name) is not node:
                    self.emit("/* %s: superseded by later definition */"
                              % node.name)
                else:
                    self.func_def(node)
                    self.emit()
        elif isinstance(node, ast.Assign):
            self.toplevel_assign(node)
        elif isinstance(node, ast.AnnAssign):
            for ln in self.st_AnnAssign(node, toplevel=True):
                self.emit(ln)
        elif isinstance(node, ast.AugAssign):
            for ln in self.stmt(node):
                self.emit(ln)
        else:
            self.emit("/* top-level: " + self.src1(node) + " */")

    def toplevel_assign(self, node):
        if all(isinstance(t, ast.Name) and t.id in self.mod_global_names
               for t in node.targets):
            for t in node.targets:
                self.emit("/* module global %s -- initialized in %s_init() */"
                          % (t.id, self.modname))
            return
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in self.mod_global_names:
            self.emit("/* module global %s -- initialized in %s_init() */"
                      % (node.targets[0].id, self.modname))
            return
        for ln in self.assign(node, toplevel=True):
            self.emit(ln)

    def _coerce_obj_to(self, expr, ct):
        """Coerce a Tier-2 obj expression `expr` to C type `ct`."""
        if ct == "int":
            return "AS_INT(%s)" % expr
        if ct == "bool":
            return "truthy(%s)" % expr
        if ct == "char*":
            return "AS_STR(%s)" % expr
        if ct.endswith("*") and ct != OBJ:
            cls = ct[:-1]
            lci = self.classes.get(cls)
            if lci is not None and getattr(lci, "csym", cls) != cls:
                ct = lci.csym + "*"              # local class: module csym
            elif cls in self.xclasses and cls not in self.classes:
                self.xstructs_needed.add(cls)   # cast needs its forward struct
                ct = self.xcsym(cls) + "*"       # qualify csym (matches decl)
            return "(%s)AS_OBJ(%s)" % (ct, expr)
        return expr

    def _ctortramp_new_args(self, init):
        """Build a ctor-trampoline argument list with defaults and **kwargs."""
        pct = [self.arg_ctype_q(init, a) for a in init.args.args[1:]]
        pos_defs = self.defaults_for(init, True)
        n_pos = len(pct)
        out = []
        for i, ct in enumerate(pct):
            dflt = pos_defs[i] if i < len(pos_defs) else None
            if dflt is not None:
                raw = "(pylen(args) > %d ? index_obj(args, %d) : %s)" % (
                    i, i, self.wrap_obj(dflt))
            else:
                raw = "(pylen(args) > %d ? index_obj(args, %d) : OBJ_NONE)" % (
                    i, i)
            if ct in ("int", "long", "short", "char", "unsigned",
                      "unsigned char", "signed"):
                raw = "AS_INT(%s)" % raw
            elif ct == "bool":
                raw = "truthy(%s)" % raw
            elif ct in ("double", "float"):
                raw = "as_dbl(%s)" % raw
            elif ct == "char*":
                raw = "AS_STR(%s)" % raw
            elif ct.endswith("*") and ct != OBJ:
                raw = "(%s)AS_OBJ(%s)" % (ct, raw)
            out.append(raw)
        ko = init.args.kwonlyargs
        kd = init.args.kw_defaults
        for j, _a in enumerate(ko):
            di = j - (len(ko) - len(kd))
            dflt = self.wrap_obj(kd[di]) if di >= 0 else "OBJ_NONE"
            idx = n_pos + j
            out.append("(pylen(args) > %d ? index_obj(args, %d) : %s)" % (
                idx, idx, dflt))
        if init.args.kwarg:
            idx = n_pos + len(ko)
            out.append("(pylen(args) > %d ? index_obj(args, %d) : dict_new())" % (
                idx, idx))
        return out

    def _emit_ctortramp(self, cls, ci, init):
        """Emit static obj Class__ctortramp(obj env, obj args)."""
        # The trampoline symbol must match make_closure(&<csym>__ctortramp) and
        # the emitted ctor extern; for an ambiguous class the bare name and the
        # module-qualified csym differ, so always key off the resolved csym.
        sym = ci.csym if ci is not None else cls
        if init is None:
            self.emit("static obj %s__ctortramp(obj env, obj args) {" % sym)
            self.indent += 1
            self.emit("(void)env; (void)args;")
            self.emit("return OBJ_OBJ(%s_new());" % sym)
            self.indent -= 1
            self.emit("}")
            self.emit()
            return
        if init.args.vararg and len(init.args.args) == 1:
            prev = self.cur_class
            self.cur_class = ci
            self.emit("static obj %s__ctortramp(obj env, obj args) {" % sym)
            self.indent += 1
            self.emit("(void)env;")
            self.emit("%s* self = aalloc(sizeof *self);" % ci.csym)
            self.emit("((Obj*)self)->type = &%s_type;" % ci.csym)
            self.emit("%s___init__(self, list_copy(args));" % ci.csym)
            self.emit_class_attr_init(ci)
            self.emit_class_static_instance_init(ci)
            self.emit("return OBJ_OBJ(self);")
            self.indent -= 1
            self.emit("}")
            self.emit()
            self.cur_class = prev
            return
        nargs = self._ctortramp_new_args(init)
        self.emit("static obj %s__ctortramp(obj env, obj args) {" % sym)
        self.indent += 1
        self.emit("(void)env; (void)args;")
        self.emit("return OBJ_OBJ(%s_new(%s));" % (ci.csym, ", ".join(nargs)))
        self.indent -= 1
        self.emit("}")
        self.emit()

    def emit_trampolines(self):
        """Uniform-signature wrappers for functions used as first-class values:
        unpack the arg list, coerce to the real parameter types, call, and box
        the result back to a Tier-2 obj."""
        # closure-converted nested functions: captures come from `env`, the
        # caller-supplied params from `args` (filling defaults when short).
        for mangled in sorted(self.closure_values_needed):
            node, n_caps, real_defs = self.closure_specs[mangled]
            ret = self._ret_ctype(node.returns)
            parts = []
            for i in range(n_caps):
                pct_i = arg_ctype(node, node.args.args[i])
                parts.append(self._coerce_obj_to("index_obj(env, %d)" % i,
                                                 pct_i))
            for j in range(n_caps, len(node.args.args)):
                param = node.args.args[j]
                k = j - n_caps
                d = real_defs[k] if k < len(real_defs) else None
                pct_j = arg_ctype(node, param)
                if d is None:
                    raw = "index_obj(args, %d)" % k
                else:
                    dflt = self.wrap_obj(d)
                    raw = "(pylen(args) > %d ? index_obj(args, %d) : %s)" % (
                        k, k, dflt)
                parts.append(self._coerce_obj_to(raw, pct_j))
            if node.args.kwarg:
                parts.append("dict_new()")
            if node.args.vararg:
                parts.append("list_copy(args)")
            call = "%s(%s)" % (self.fnsym(mangled), ", ".join(parts))
            self.emit("static obj %s__tramp(obj env, obj args) {" %
                      cname(mangled))
            self.indent += 1
            self.emit("(void)env; (void)args;")
            if ret == "void":
                self.emit("%s; return OBJ_NONE;" % call)
            elif ret == "int":
                self.emit("return OBJ_INT(%s);" % call)
            elif ret == "bool":
                self.emit("return OBJ_BOOL(%s);" % call)
            elif ret == "char*":
                self.emit("return OBJ_STR(%s);" % call)
            elif ret.endswith("*") and ret != OBJ:
                self.emit("return OBJ_OBJ(%s);" % call)
            else:
                self.emit("return %s;" % call)
            self.indent -= 1
            self.emit("}")
            self.emit()
        for fn in sorted(self.func_values_needed):
            node = self.func_nodes[fn]
            pct = self.func_params.get(fn, [])
            ret = self.func_returns.get(fn, OBJ)
            args = []
            for i, ct in enumerate(pct):
                a = "index_obj(args, %d)" % i
                if ct == "int":
                    a = "AS_INT(%s)" % a
                elif ct == "bool":
                    a = "truthy(%s)" % a
                elif ct == "char*":
                    a = "AS_STR(%s)" % a
                elif ct.endswith("*") and ct != OBJ:
                    a = "(%s)AS_OBJ(%s)" % (ct, a)
                args.append(a)
            call = "%s(%s)" % (self.fnsym(fn), ", ".join(args))
            self.emit("static obj %s__tramp(obj env, obj args) {" % self.fnsym(fn))
            self.indent += 1
            self.emit("(void)env; (void)args;")
            if ret == "void":
                self.emit("%s; return OBJ_NONE;" % call)
            elif ret == OBJ:
                self.emit("return %s;" % call)
            elif ret == "int":
                self.emit("return OBJ_INT(%s);" % call)
            elif ret == "bool":
                self.emit("return OBJ_BOOL(%s);" % call)
            elif ret == "char*":
                self.emit("return OBJ_STR(%s);" % call)
            elif ret.endswith("*"):
                self.emit("return OBJ_OBJ(%s);" % call)
            else:
                self.emit("return %s;" % call)
            self.indent -= 1
            self.emit("}")
            self.emit()
        for cls in sorted((set(self.class_values_needed) |
                           {ci.csym for ci in self.class_order})
                          - self._pod_set):
            ci = self.classes.get(cls) or (self.xclasses[cls][0]
                                           if cls in self.xclasses else None)
            if ci is None:
                # `cls` may be a csym (mangled, module-qualified name) rather
                # than a short class name, e.g. for an ambiguous class. Without
                # this, ci stays None, init looks empty, and the trampoline
                # calls Class_new() with no args -> "too few arguments".
                ci = next((c for c in self.classes.values()
                           if c.csym == cls), None) \
                    or next((c for c, _ in self.xclasses.values()
                             if c.csym == cls), None)
            if ci is None:
                # An imported class used only as a first-class value (e.g. the
                # parser stores node classes like `Plus` in an op table) was
                # never loaded as an xclass here. Load it project-wide so its
                # (possibly inherited) __init__ resolves -- otherwise the
                # trampoline calls Class_new() with no args -> garbage fields.
                self._load_xclass_anywhere(cls)
                ci = self.xclasses[cls][0] if cls in self.xclasses else None
            ni = self._nearest_init(ci) if ci else None
            init = ni[1] if ni else (ci.methods.get("__init__") if ci else None)
            if init is None and ci is not None:
                init = self._project_nearest_init(ci)
            self._emit_ctortramp(cls, ci, init)

    def emit_module_init(self):
        self.emit("/* Initialize module-level globals (Python import-time). */")
        self.emit("void %s_init(void) {" % (self.cmod))
        self.indent += 1
        # Idempotent: the multi-module driver emits an _entry.c that calls every
        # module's init before main, while a single translation unit calls its
        # own from the top of main. The guard makes both paths safe.
        self.emit("static int _init_done = 0;")
        self.emit("if (_init_done) return;")
        self.emit("_init_done = 1;")
        if not self.mod_globals:
            self.emit("/* none */")
        for name, ctype, kind, val in self.mod_globals:
            if kind == "const":             # already defined at file scope
                continue
            if kind == "singleton":
                cls = ctype[:-1]
                if cls in self.classes:
                    init = self.classes[cls].methods.get("__init__")
                    defs = self.defaults_for(init, True) if init else None
                    args = self.coerce_args(
                        self.init_param_ctypes(self.classes[cls]), val.args,
                        defs)
                else:                       # imported class
                    xinfo = self.xclasses[cls][0] if cls in self.xclasses \
                        else None
                    init = xinfo.methods.get("__init__") if xinfo else None
                    if init:                # pad omitted args with __init__ defaults
                        pct = [arg_ctype(init, a)
                               for a in init.args.args[1:]]
                        defs = self.defaults_for(init, True)
                        args = self.coerce_args(pct, val.args, defs)
                    else:
                        args = [self.expr(a) for a in val.args]
                self.emit("%s = %s_new(%s);" % (self._msym(name), self.ccls(cls),
                                                ", ".join(args)))
            else:
                lit = _const_value(val)
                if ctype == OBJ and isinstance(lit, (str, bytes)):
                    s = lit.decode("latin1") if isinstance(lit, bytes) else lit
                    self.emit("%s = OBJ_STR(%s);" % (self._msym(name), c_string(s)))
                else:
                    self.emit("%s = %s;" % (self._msym(name),
                        self.coerce_to(ctype, val, self.expr(val))))
        for ci in self.class_order:         # class-level statics
            prev = self.cur_class
            self.cur_class = ci
            for nm, val in ci.class_statics.items():
                self.emit("%s_%s = %s;" % (ci.csym, cname(nm),
                                           self.coerce_to(OBJ, val, self.expr(val))))
            self.cur_class = prev
        for stmt in getattr(self, "mod_init_stmts", []):  # deferred top-level
            for ln in self.stmt(stmt):
                self.emit(ln)
        self.indent -= 1
        self.emit("}")

    def _thread_decorator_info(self, dec):
        """If `dec` is @rpy.threads.left(core=N) / .right(core=N), return
        (side, core); else None."""
        if not isinstance(dec, ast.Call):
            return None
        f = dec.func
        if not (isinstance(f, ast.Attribute) and f.attr in ("left", "right")):
            return None
        v = f.value
        if not (isinstance(v, ast.Attribute) and v.attr == "threads"
                and isinstance(v.value, ast.Name) and v.value.id == "rpy"):
            return None
        core = 0
        for kw in dec.keywords:
            if kw.arg == "core" and isinstance(kw.value, ast.Constant):
                core = int(kw.value.value)
        return f.attr, core

    def _is_start_new_thread(self, node):
        """`rpy.threads.start_new_thread(fn, ...)` -> the started fn's name,
        else None."""
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "start_new_thread"):
            return None
        v = node.func.value
        if isinstance(v, ast.Attribute) and v.attr == "threads" \
                and isinstance(v.value, ast.Name) and v.value.id == "rpy":
            if node.args and isinstance(node.args[0], ast.Name):
                return node.args[0].id
        return None

    def _rewrite_thread_decorators(self, tree):
        """Recognize ShivyCX bare-metal thread declarations written as rpython
        decorators. Records `@rpy.threads.left/right(core=N)` on each function
        (stripping the decorator so the function translates as an ordinary void
        function), and -- when the program drives them from a `__main__` guard
        rather than an explicit `main` -- synthesizes a `main` whose body is that
        guard with each `rpy.threads.start_new_thread(fn)` turned into `fn()`.
        The recorded contracts are emitted in main's header by _auto_contracts.
        """
        self._thread_contracts = {}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.decorator_list:
                keep = []
                for dec in node.decorator_list:
                    info = self._thread_decorator_info(dec)
                    if info is not None:
                        self._thread_contracts[node.name] = info
                    else:
                        keep.append(dec)
                node.decorator_list = keep
        if not self._thread_contracts:
            return
        if any(isinstance(n, ast.FunctionDef) and n.name == "main"
               for n in tree.body):
            return
        guard = None
        for n in tree.body:
            if _is_dunder_main_guard(n):
                guard = n
                break
        main_body = []
        if guard is not None:
            for st in guard.body:
                fn = self._is_start_new_thread(st.value) \
                    if isinstance(st, ast.Expr) else None
                if fn is not None:
                    main_body.append(ast.Expr(value=ast.Call(
                        func=ast.Name(id=fn, ctx=ast.Load()),
                        args=[], keywords=[])))
                else:
                    main_body.append(st)
        main_body.append(ast.Return(value=ast.Constant(value=0)))
        main_fn = ast.FunctionDef(
            name="main",
            args=ast.arguments(posonlyargs=[], args=[], vararg=None,
                               kwonlyargs=[], kw_defaults=[], kwarg=None,
                               defaults=[]),
            body=main_body, decorator_list=[],
            returns=ast.Constant(value="int"))
        if guard is not None:
            tree.body = [n for n in tree.body if n is not guard]
        tree.body.append(main_fn)
        ast.fix_missing_locations(tree)

    def _extract_contracts(self, node):
        """Pull leading `assert len(arr) ...` statements off the body and render
        them as ShivyCX contract clauses (which sit between the parameter list
        and `{` and let the compiler prove SIMD-divisible / bounded lengths and
        emit a vectorized loop). Returns (clauses, remaining_body). Asserts that
        are not array-length contracts are left in the body as ordinary asserts.
        """
        clauses = []
        body = list(node.body)
        # A leading docstring (an expression statement holding a string) sits
        # before any contract asserts; skip it so a documented kernel still has
        # its `assert len(...)` clauses lifted into the contract region.
        if body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]
        while body and isinstance(body[0], ast.Assert):
            test = body[0].test
            clause = self._contract_clause(test)
            if clause is None:
                break
            clauses.append(clause)
            body.pop(0)
        return clauses, body

    def _contract_clause(self, test):
        def is_len(n):
            return (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "len" and len(n.args) == 1
                    and isinstance(n.args[0], ast.Name))
        def lenexpr(n):
            return "len(%s)" % cname(n.args[0].id)
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            return None
        op = test.ops[0]
        left, right = test.left, test.comparators[0]
        # len(arr) % K == 0  ->  assert not len(arr) % K
        if isinstance(op, ast.Eq) and isinstance(left, ast.BinOp) \
                and isinstance(left.op, ast.Mod) and is_len(left.left) \
                and isinstance(left.right, ast.Constant) \
                and isinstance(right, ast.Constant) and right.value == 0:
            return "assert not %s %% %d" % (lenexpr(left.left), left.right.value)
        # len(arr) >= N  /  len(arr) <= N
        if is_len(left) and isinstance(right, ast.Constant) \
                and isinstance(op, (ast.GtE, ast.LtE)):
            sym = ">=" if isinstance(op, ast.GtE) else "<="
            return "assert %s %s %d" % (lenexpr(left), sym, right.value)
        return None

    def _auto_contracts(self, node):
        """Infer SIMD-divisibility contracts from fixed-size array parameters
        (`x: "f32[256]"`) so the user need not write any assert. A 128-bit SSE
        register holds 16/sizeof(elem) lanes; if the fixed element count is a
        multiple of that lane count, emit the divisibility + minimum-length
        contracts that license ShivyCX's vectorized kernel."""
        bytesz = {"char": 1, "bool": 1, "short": 2, "int": 4, "unsigned": 4,
                  "float": 4, "long": 8, "double": 8, "unsigned char": 1}
        clauses = []
        for a in node.args.args:
            if a.annotation is None:
                continue
            ct = ann_to_ctype(a.annotation)
            size = ann_array_size(a.annotation)
            if size is None or not ct or not ct.endswith("*") \
                    or ct[:-1] not in _SCALAR_CTYPES:
                continue
            lanes = 16 // bytesz.get(ct[:-1], 4)
            if lanes >= 2 and size % lanes == 0:
                nm = cname(a.arg)
                clauses.append("assert not len(%s) %% %d" % (nm, lanes))
                clauses.append("assert len(%s) >= %d" % (nm, lanes))
        # Register-partitioned bare-metal threads: emit `assert FN in
        # threads.SIDE(core=N)` in main's header for each decorated worker.
        if node.name == "main" and getattr(self, "_thread_contracts", None):
            for fn, (side, core) in self._thread_contracts.items():
                clauses.append("assert %s in threads.%s(core=%d)"
                               % (self.fnsym(fn), side, core))
        return clauses

    def _uses_argv(self, node):
        """True if this is `main` and its body reads `sys.argv` -- then main
        takes (int argc, char** argv) instead of (void), so a runtime command
        line argument can drive the program (and defeat constant folding)."""
        if node.name != "main":
            return False
        for sub in ast.walk(node):
            if self._is_sys_argv(sub):
                return True
        return False

    def _is_sys_argv(self, node):
        return (isinstance(node, ast.Attribute) and node.attr == "argv"
                and isinstance(node.value, ast.Name) and node.value.id == "sys")

    # The translator identifies itself as this implementation so that source
    # can guard host-CPython-only code with `if sys.implementation.name !=
    # 'shivyc':` -- the dead branch is skipped at translation time and never
    # reaches the C backend (see _static_cond / st_If).
    IMPL_NAME = "shivyc"

    def _is_sys_impl_name(self, node):
        return (isinstance(node, ast.Attribute) and node.attr == "name"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "implementation"
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "sys")

    def _static_cond(self, test):
        """Compile-time truth value of a guard the translator can fold, or None.

        Currently folds `sys.implementation.name` compared (==/!=) against a
        string literal, in either operand order."""
        if isinstance(test, ast.Compare) and len(test.ops) == 1 \
                and len(test.comparators) == 1:
            left, right = test.left, test.comparators[0]
            impl = other = None
            if self._is_sys_impl_name(left):
                impl, other = left, right
            elif self._is_sys_impl_name(right):
                impl, other = right, left
            if impl is not None and isinstance(other, ast.Constant) \
                    and isinstance(other.value, str):
                eq = (other.value == self.IMPL_NAME)
                if isinstance(test.ops[0], ast.Eq):
                    return eq
                if isinstance(test.ops[0], ast.NotEq):
                    return not eq
        return None

    def func_def(self, node):
        self.enter_scope(node, skip_self=False)
        try:
            self._promote_containers(node)
        except Exception:
            pass        # promotion is opt-in and best-effort; never break codegen
        try:
            self._analyze_untyped_containers(node)
        except Exception:
            pass        # advisory only: never let analysis break compilation
        ret = self._ret_ctype(node.returns)
        self.cur_ret = ret
        if self._uses_argv(node):
            self.scope["argc"] = "int"
            self.scope["argv"] = "char**"
        params = self.param_list(node, skip_self=False)
        plist = ", ".join(params) if params else "void"
        clauses, body = self._extract_contracts(node)
        for c in self._auto_contracts(node):       # from fixed-size arrays
            if c not in clauses:
                clauses.append(c)
        sig = self.func_signature(node).rstrip(";")
        if clauses:
            # ShivyCX reads these contract clauses (between the parameter list
            # and `{`) to prove SIMD-divisibility and vectorize the loop. They
            # are not valid C, so guard them for gcc/clang with __SHIVYC__,
            # which ShivyCX's preprocessor always predefines.
            self.emit(sig)
            self.emit("#ifdef __SHIVYC__")
            for c in clauses:
                self.emit(c)
            self.emit("#endif")
            self.emit("{")
        else:
            self.emit("%s {" % sig)
        self.indent += 1
        self._emit_vararg_setup(node)
        # Python runs module-level code at import time. A single translation
        # unit has no _entry.c to do that, so main runs its own module init
        # first -- otherwise module globals stay zeroed and, e.g., a global
        # list reads back as empty. The init is idempotent.
        if node.name == "main" and self.mod_globals:
            self.emit("%s_init();" % self.cmod)
        self.emit_hoisted_body(body)
        self.indent -= 1
        self.emit("}")

    # ---- untyped-container inference + rpython-rule warnings -------------

    def _warn_unsupported(self, line, what, subst, hint=""):
        """Report a construct that was replaced by a stand-in value.

        These are the dangerous ones. An advisory about container typing costs
        a reader nothing if ignored; a substitution changes what the program
        *does*, and until now the only trace was a comment in the generated C
        that nobody reads. Three separate self-hosting bugs were found the
        hard way -- through a segfault and a bisect -- when a line of stderr
        would have named the cause outright. `os.getcwd()` became `None`,
        `os.path.join` was handed it, and the compiler died with no diagnostic
        at all.

        Deduplicated per (file, line, construct): a call in a loop body is one
        problem, not one per iteration.
        """
        if os.environ.get("PY2C_NO_UNSUPPORTED_WARN"):
            return
        seen = getattr(self, "_unsupported_seen", None)
        if seen is None:
            seen = self._unsupported_seen = set()
        src = getattr(self, "src_name", "?")
        key = (src, line, what)
        if key in seen:
            return
        seen.add(key)
        msg = "%s is not lowered; substituted %s" % (what, subst)
        if hint:
            msg += " -- " + hint
        sys.stderr.write("py2c: %s:%s: %s\n" % (src, line, msg))

    def _warn(self, line, msg):
        """Emit an rpython advisory to stderr (never affects generated code)."""
        if getattr(self, "_no_warn", False) or \
                os.environ.get("PY2C_NO_CONTAINER_WARN"):
            return
        where = "%s:%s" % (getattr(self, "src_name", "?"), line)
        sys.stderr.write("rpython: %s: %s\n" % (where, msg))

    def _empty_container_kind(self, node):
        """'dict'/'list'/'set' if node is an empty container constructor."""
        if isinstance(node, ast.Dict) and not node.keys:
            return "dict"
        if isinstance(node, ast.List) and not node.elts:
            return "list"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "dict" and not node.args:
                return "dict"
            if node.func.id == "list" and not node.args:
                return "list"
            if node.func.id in ("set", "frozenset") and not node.args:
                return "set"
        return None

    def _lit_type(self, node):
        """Coarse element-type inference for a value expression: one of
        'int'/'float'/'bool'/'str'/'None'/'obj' ('obj' == unknown/boxed)."""
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool):
                return "bool"
            if isinstance(v, int):
                return "int"
            if isinstance(v, float):
                return "float"
            if isinstance(v, str):
                return "str"
            if v is None:
                return "None"
            return "obj"
        if isinstance(node, ast.Name):
            t = getattr(self, "_infer_locals", {}).get(node.id)
            if t:
                return t
            ct = self.scope.get(node.id)
            return {"int": "int", "long": "int", "bool": "bool",
                    "double": "float", "float": "float",
                    "char*": "str"}.get(ct, "obj")
        if isinstance(node, (ast.Compare, ast.BoolOp)):
            return "bool"
        if isinstance(node, ast.BinOp):
            lt, rt = self._lit_type(node.left), self._lit_type(node.right)
            if "str" in (lt, rt):
                return "str"
            if "float" in (lt, rt):
                return "float"
            if lt == "int" and rt == "int":
                return "int"
            return "obj"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            fn = node.func.id
            if fn in ("int", "len", "ord", "abs", "hash"):
                return "int"
            if fn in ("str", "chr", "repr", "input", "bytes"):
                return "str"
            if fn == "float":
                return "float"
            if fn == "bool":
                return "bool"
            if fn in self.classes or fn in self.xclasses:
                return "obj"      # a class instance
        if isinstance(node, ast.Call) and \
                isinstance(node.func, ast.Attribute) and \
                node.func.attr == "get" and len(node.args) == 2:
            return self._lit_type(node.args[1])     # d.get(k, default)
        return "obj"

    @staticmethod
    def _ann_for(kind, key, val):
        """Suggested rpython annotation string for an inferred container."""
        if kind == "dict":
            return "dict[%s, %s]" % (key, val)
        return "%s[%s]" % (kind, val)

    def _analyze_untyped_containers(self, fndef):
        """Infer key/value (element) types of *unannotated* empty containers
        from their use, and warn so the user can adapt to the rpython rules.
        Purely advisory: the generated code is unchanged (boxed containers)."""
        annotated = set()
        for n in ast.walk(fndef):
            if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                annotated.add(n.target.id)
        decls = {}     # name -> (kind, lineno)
        for stmt in fndef.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                    and isinstance(stmt.targets[0], ast.Name):
                kind = self._empty_container_kind(stmt.value)
                if kind and stmt.targets[0].id not in annotated:
                    decls.setdefault(stmt.targets[0].id, (kind, stmt.lineno))
        if not decls:
            return
        # Lightweight local typing: for-loop variables over range()/strings/
        # homogeneous literals, and simple scalar assignments. Used to infer
        # element types from loop bodies (e.g. `for i in range(n): xs.append(i)`).
        locals_t = {}
        for n in ast.walk(fndef):
            if isinstance(n, ast.For) and isinstance(n.target, ast.Name):
                et = self._iter_elem_type(n.iter)
                if et != "obj":
                    locals_t[n.target.id] = et
        self._infer_locals = locals_t
        for n in ast.walk(fndef):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and \
                    isinstance(n.targets[0], ast.Name):
                t = self._lit_type(n.value)
                if t not in ("obj", "None"):
                    locals_t.setdefault(n.targets[0].id, t)
        info = {nm: {"kind": k, "line": ln, "keys": set(), "vals": set()}
                for nm, (k, ln) in decls.items()}
        for n in ast.walk(fndef):
            if isinstance(n, ast.Assign):
                for tgt in n.targets:
                    if isinstance(tgt, ast.Subscript) and \
                            isinstance(tgt.value, ast.Name) and \
                            tgt.value.id in info:
                        d = info[tgt.value.id]
                        if d["kind"] == "dict":
                            d["keys"].add(self._lit_type(tgt.slice))
                        d["vals"].add(self._lit_type(n.value))
            elif isinstance(n, ast.Call) and \
                    isinstance(n.func, ast.Attribute) and \
                    isinstance(n.func.value, ast.Name) and \
                    n.func.value.id in info and n.args:
                d = info[n.func.value.id]
                if n.func.attr in ("append", "add"):
                    d["vals"].add(self._lit_type(n.args[0]))
        for nm, d in info.items():
            self._warn_container(nm, d)
        self._infer_locals = {}

    # ---- auto-promotion of cleanly-inferred containers (opt-in) ---------

    _SCALARS = {"int", "float", "bool", "str"}

    def _promote_containers(self, fndef):
        """When PY2C_PROMOTE_CONTAINERS is set, rewrite an unannotated empty
        list/dict whose element/key/value types infer to a single scalar AND
        whose every use is supported by the unboxed typed form, into an
        annotated `name: "list[int]"`-style assignment -- so the existing typed
        path lowers it to an unboxed array. Conservative by construction: any
        escape (return, pass as arg, alias, store-in-container), unsupported
        method, slice, or negative index leaves the container boxed."""
        if not os.environ.get("PY2C_PROMOTE_CONTAINERS"):
            return
        annotated = {n.target.id for n in ast.walk(fndef)
                     if isinstance(n, ast.AnnAssign)
                     and isinstance(n.target, ast.Name)}
        decls = {}            # name -> (kind, assign_stmt)
        for stmt in fndef.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                    and isinstance(stmt.targets[0], ast.Name):
                kind = self._empty_container_kind(stmt.value)
                if kind in ("list", "dict") and \
                        stmt.targets[0].id not in annotated:
                    decls.setdefault(stmt.targets[0].id, (kind, stmt))
        if not decls:
            return
        # one assignment only (a reassignment may change the representation)
        assigns = {}
        for n in ast.walk(fndef):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id in decls:
                        assigns[t.id] = assigns.get(t.id, 0) + 1
        locals_t = {}
        for n in ast.walk(fndef):
            if isinstance(n, ast.For) and isinstance(n.target, ast.Name):
                et = self._iter_elem_type(n.iter)
                if et != "obj":
                    locals_t[n.target.id] = et
        self._infer_locals = locals_t
        info = {nm: {"kind": k, "keys": set(), "vals": set()}
                for nm, (k, _) in decls.items()}
        parent = {}
        for n in ast.walk(fndef):
            for c in ast.iter_child_nodes(n):
                parent[c] = n
        safe = {nm: True for nm in decls}
        for n in ast.walk(fndef):
            if isinstance(n, ast.Assign):
                for tgt in n.targets:
                    if isinstance(tgt, ast.Subscript) and \
                            isinstance(tgt.value, ast.Name) and \
                            tgt.value.id in info:
                        d = info[tgt.value.id]
                        if d["kind"] == "dict":
                            d["keys"].add(self._lit_type(tgt.slice))
                        vt = self._promo_val_type(tgt.value.id, n.value)
                        if vt:
                            d["vals"].add(vt)
            elif isinstance(n, ast.Call) and \
                    isinstance(n.func, ast.Attribute) and \
                    isinstance(n.func.value, ast.Name) and \
                    n.func.value.id in info and n.args:
                if n.func.attr == "append":
                    avt = self._promo_val_type(n.func.value.id, n.args[0])
                    if avt:
                        info[n.func.value.id]["vals"].add(avt)
        for n in ast.walk(fndef):
            if isinstance(n, ast.Name) and n.id in decls:
                if not self._safe_container_use(n, parent, decls[n.id][0]):
                    safe[n.id] = False
        self._infer_locals = {}
        for nm, (kind, stmt) in decls.items():
            if not safe.get(nm) or assigns.get(nm, 0) != 1:
                continue
            d = info[nm]
            vals = d["vals"] - {"None"}
            if len(vals) != 1:
                continue
            vt = next(iter(vals))
            if vt not in self._SCALARS:
                continue
            if kind == "dict":
                keys = d["keys"] - {"None"}
                if len(keys) != 1:
                    continue
                kt = next(iter(keys))
                if kt not in self._SCALARS:
                    continue
                ann = "dict[%s, %s]" % (kt, vt)
            else:
                ann = "list[%s]" % vt
            self._rewrite_to_annassign(fndef, stmt, ann)
            self._warn(stmt.lineno, "promoted %s '%s' to unboxed %s." % (
                kind, nm, ann))

    def _promo_val_type(self, nm, node):
        """Scalar type of a value assigned into container `nm`, treating a
        self-reference `nm[...]` as transparent so the counter idiom
        `d[k] = d[k] + 1` infers from the rest of the expression."""
        if isinstance(node, ast.Subscript) and \
                isinstance(node.value, ast.Name) and node.value.id == nm:
            return None                              # self-ref: no new info
        if isinstance(node, ast.BinOp):
            cand = [t for t in (self._promo_val_type(nm, node.left),
                                self._promo_val_type(nm, node.right)) if t]
            if not cand:
                return "obj"
            if "str" in cand:
                return "str"
            if "float" in cand:
                return "float"
            return "int" if all(t == "int" for t in cand) else "obj"
        return self._lit_type(node)

    def _safe_container_use(self, nm_node, parent, kind):
        """True if this occurrence of the container name is an operation the
        unboxed typed form supports (so promotion preserves behavior)."""
        p = parent.get(nm_node)
        if p is None:
            return False
        if isinstance(p, ast.Assign) and nm_node in p.targets:
            return True                              # `nm = ...`
        if isinstance(p, ast.Subscript) and p.value is nm_node:
            sl = p.slice
            if isinstance(sl, ast.Slice):
                return False                         # no typed slicing
            if isinstance(sl, ast.UnaryOp) and isinstance(sl.op, ast.USub):
                return False                         # no negative index
            if isinstance(sl, ast.Constant) and \
                    isinstance(sl.value, int) and sl.value < 0:
                return False
            return True
        if isinstance(p, ast.Attribute) and p.value is nm_node:
            gp = parent.get(p)
            methods = {"append"} if kind == "list" else set()
            return isinstance(gp, ast.Call) and gp.func is p and \
                p.attr in methods
        if isinstance(p, ast.For) and p.iter is nm_node:
            return True
        if isinstance(p, ast.Compare) and nm_node in p.comparators and \
                any(isinstance(o, (ast.In, ast.NotIn)) for o in p.ops):
            return True
        if isinstance(p, ast.Call) and isinstance(p.func, ast.Name) and \
                p.func.id == "len" and nm_node in p.args:
            return True
        return False

    def _rewrite_to_annassign(self, fndef, assign_stmt, ann):
        new = ast.AnnAssign(target=assign_stmt.targets[0],
                            annotation=ast.Constant(value=ann),
                            value=assign_stmt.value, simple=1)
        ast.copy_location(new, assign_stmt)
        ast.fix_missing_locations(new)
        for i, s in enumerate(fndef.body):
            if s is assign_stmt:
                fndef.body[i] = new
                return

    def _iter_elem_type(self, node):
        """Coarse element type of an iterable used in `for x in <node>`."""
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "range":
            return "int"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return "str"
        if isinstance(node, (ast.List, ast.Set, ast.Tuple)) and node.elts:
            ts = {self._lit_type(e) for e in node.elts}
            ts.discard("None")
            if len(ts) == 1:
                return next(iter(ts))
        return "obj"

    def _warn_container(self, nm, d):
        kind, line = d["kind"], d["line"]
        vals = d["vals"] - {"None"}        # None alone doesn't pin a type
        keys = d.get("keys", set()) - {"None"}
        had_none = "None" in d["vals"]
        if not vals:
            vt = "obj" if had_none else None
        elif len(vals) == 1:
            vt = next(iter(vals))
        else:
            vt = "MIXED"
        kt = None
        if kind == "dict":
            if not keys:
                kt = None
            elif len(keys) == 1:
                kt = next(iter(keys))
            else:
                kt = "MIXED"

        if vt is None and kt is None:
            self._warn(line, "untyped %s '%s' has no observed use; it stays a "
                       "boxed container. Annotate it (e.g. %s: \"%s\") for the "
                       "unboxed fast path." % (
                           kind, nm, nm, self._ann_for(kind, "str", "int")))
            return
        if vt == "MIXED" or kt == "MIXED":
            parts = []
            if kt == "MIXED":
                parts.append("key types " + ", ".join(sorted(keys)))
            if vt == "MIXED":
                parts.append("value types " + ", ".join(sorted(vals)))
            self._warn(line, "%s '%s' mixes %s; rpython containers should be "
                       "homogeneous, so it stays boxed (obj). Use one element "
                       "type, or keep it boxed intentionally." % (
                           kind, nm, " and ".join(parts)))
            return
        vt = vt or "obj"
        if kind == "dict":
            kt = kt or "obj"
            ann = self._ann_for("dict", kt, vt)
        else:
            ann = self._ann_for(kind, None, vt)
        extra = " (value is None/object)" if had_none and vt == "obj" else ""
        if vt == "obj" or (kind == "dict" and kt == "obj"):
            self._warn(line, "%s '%s' looks like %s%s and stays boxed; that is "
                       "fine, but a scalar element type would compile to an "
                       "unboxed %s." % (kind, nm, ann, extra, kind))
        else:
            self._warn(line, "%s '%s' looks like %s; annotate it as %s: \"%s\" "
                       "to get the unboxed fast path." % (
                           kind, nm, ann, nm, ann))

    def emit_body(self, body):
        if not body:
            self.emit("/* pass */")
            return
        for stmt in body:
            for ln in self.stmt(stmt):
                self.emit(ln)

    # ---- statements ------------------------------------------------------

    def stmt(self, node):
        if id(node) in getattr(self, "ctypes_skip", ()):
            return []                       # ctypes config: compile-time only
        m = getattr(self, "st_" + type(node).__name__, None)
        if m is None:
            return ["/* stmt %s: %s */" % (type(node).__name__,
                                           self.src1(node))]
        try:
            return m(node)
        except Unsupported:
            raise
        except Exception as e:
            if self.stdlib_root:
                raise Unsupported(str(e)) from e
            return ["/* transpile-error (%s): %s */" % (e, self.src1(node))]

    def st_Expr(self, node):
        v = node.value
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            first = v.value.strip().splitlines()
            return ["/* " + first[0] + " */"] if first else []
        return [self.expr(v) + ";"]

    def st_Assign(self, node):
        return self.assign(node)

    # ---- NumPy-style elementwise array-expression fusion ----------------
    # A whole-array store `out[:] = <elementwise expr over arrays>` is lowered
    # to ONE C loop with no intermediate array temporaries (operator fusion +
    # allocation elision, in the spirit of Codon's core-numpy-fusion pass).
    # ShivyCX then vectorizes the resulting scalar loop.
    _FUSE_SCALAR_CTS = {"double", "float", "int", "long", "short", "char",
                        "unsigned char", "unsigned", "signed char",
                        "unsigned int"}
    _FUSE_BINOP = {ast.Add: ("+", 1), ast.Sub: ("-", 1), ast.Mult: ("*", 1),
                   ast.Div: ("/", 8), ast.Mod: ("%", 8), ast.BitAnd: ("&", 1),
                   ast.BitOr: ("|", 1), ast.BitXor: ("^", 1)}
    _FUSE_CMP = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
                 ast.Eq: "==", ast.NotEq: "!="}
    _FUSE_UFUNCS = {"sqrt": ("sqrt", 2), "exp": ("exp", 5), "log": ("log", 5),
                    "log2": ("log2", 5), "log10": ("log10", 5),
                    "exp2": ("exp2", 5), "cbrt": ("cbrt", 5),
                    "sin": ("sin", 10), "cos": ("cos", 10), "tan": ("tan", 10),
                    "sinh": ("sinh", 10), "cosh": ("cosh", 10),
                    "tanh": ("tanh", 10), "fabs": ("fabs", 1),
                    "floor": ("floor", 1), "ceil": ("ceil", 1)}

    def _array_elem_ct(self, node):
        """Element C type if `node` is a Name bound to a native scalar array
        (`double*`/`float*`/`int*`/...), else None."""
        if isinstance(node, ast.Name):
            ct = self.scope.get(node.id)
            if isinstance(ct, str) and ct.endswith("*") \
                    and ct[:-1] in self._FUSE_SCALAR_CTS:
                return ct[:-1]
        return None

    def _fuse_analyze(self, node):
        """Validate an elementwise array expression. Returns
        (is_array, cost, [array_names]) or None if not purely elementwise."""
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return None
            return (False, 0, []) if isinstance(node.value, (int, float)) \
                else None
        if isinstance(node, ast.Name):
            if self._array_elem_ct(node):
                return (True, 0, [node.id])
            ct = self.scope.get(node.id)
            if ct in ("int", "long", "double", "float", "short", "char",
                      "unsigned", "bool"):
                return (False, 0, [])
            return None
        if isinstance(node, ast.UnaryOp) and \
                isinstance(node.op, (ast.USub, ast.UAdd)):
            return self._fuse_analyze(node.operand)
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Pow):
                lb = self._fuse_analyze(node.left)
                rb = self._fuse_analyze(node.right)
                if lb is None or rb is None:
                    return None
                sq = isinstance(node.right, ast.Constant) and \
                    node.right.value == 2
                return (lb[0] or rb[0], lb[1] + rb[1] + (1 if sq else 8),
                        lb[2] + rb[2])
            opc = self._FUSE_BINOP.get(type(node.op))
            if opc is None:
                return None
            lb = self._fuse_analyze(node.left)
            rb = self._fuse_analyze(node.right)
            if lb is None or rb is None:
                return None
            return (lb[0] or rb[0], lb[1] + rb[1] + opc[1], lb[2] + rb[2])
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            if type(node.ops[0]) not in self._FUSE_CMP:
                return None
            lb = self._fuse_analyze(node.left)
            rb = self._fuse_analyze(node.comparators[0])
            if lb is None or rb is None:
                return None
            return (lb[0] or rb[0], lb[1] + rb[1] + 1, lb[2] + rb[2])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in self._FUSE_UFUNCS \
                and len(node.args) == 1 and not node.keywords:
            a = self._fuse_analyze(node.args[0])
            if a is None:
                return None
            return (a[0], a[1] + self._FUSE_UFUNCS[node.func.id][1], a[2])
        return None

    def _fuse_render(self, node, idx, dtype=None):
        f32 = (dtype == "float")
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, float):
                return repr(float(v)) + ("f" if f32 else "")
            return str(int(v))
        if isinstance(node, ast.Name):
            if self._array_elem_ct(node):
                return "%s[%s]" % (self.lid(node.id), idx)
            return self.lid(node.id)
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub):
                return "(-(%s))" % self._fuse_render(node.operand, idx, dtype)
            return self._fuse_render(node.operand, idx, dtype)
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Pow):
                base = self._fuse_render(node.left, idx, dtype)
                e = node.right
                if isinstance(e, ast.Constant) and isinstance(e.value, int) \
                        and 1 <= e.value <= 4:
                    return "(" + "*".join("(%s)" % base
                                          for _ in range(e.value)) + ")"
                powfn = "powf" if f32 else "pow"
                return "%s(%s, %s)" % (powfn, base,
                                       self._fuse_render(e, idx, dtype))
            cop = self._FUSE_BINOP[type(node.op)][0]
            return "(%s %s %s)" % (self._fuse_render(node.left, idx, dtype),
                                   cop,
                                   self._fuse_render(node.right, idx, dtype))
        if isinstance(node, ast.Compare):
            cop = self._FUSE_CMP[type(node.ops[0])]
            return "(%s %s %s)" % (self._fuse_render(node.left, idx, dtype),
                                   cop,
                                   self._fuse_render(node.comparators[0], idx,
                                                     dtype))
        if isinstance(node, ast.Call):
            fn = self._FUSE_UFUNCS[node.func.id][0]
            if f32:
                fn = fn + "f"           # single-precision libm (expf/sqrtf/...)
            return "%s(%s)" % (fn, self._fuse_render(node.args[0], idx, dtype))
        return "0"

    def _try_fused_array_store(self, node):
        if len(node.targets) != 1:
            return None
        tgt = node.targets[0]
        if not (isinstance(tgt, ast.Subscript)
                and isinstance(tgt.value, ast.Name)
                and isinstance(tgt.slice, ast.Slice)
                and tgt.slice.step is None and tgt.slice.lower is None):
            return None
        elem = self._array_elem_ct(tgt.value)
        if not elem:
            return None
        info = self._fuse_analyze(node.value)
        if info is None:
            return None
        is_arr, cost, names = info
        if tgt.slice.upper is not None:
            n = self.coerce_to("long", tgt.slice.upper,
                               self.expr(tgt.slice.upper))
        else:
            sz = self.array_sizes.get(tgt.value.id)
            if sz is None:
                return None
            n = str(sz)
        self.loop_n += 1
        iv = "_fi%d" % self.loop_n
        body = self._fuse_render(node.value, iv, elem)
        if os.environ.get("PY2C_NPFUSE_VERBOSE"):
            kind = "array" if is_arr else "scalar-fill"
            sys.stderr.write(
                "npfuse: %s[:] := %s expr over {%s} [cost=%d] -> 1 pass, "
                "0 temporaries\n" % (tgt.value.id, kind,
                                     ", ".join(sorted(set(names))) or "-",
                                     cost))
        return ["for (long %s = 0; %s < %s; %s++) %s[%s] = (%s)(%s);" % (
            iv, iv, n, iv, self.lid(tgt.value.id), iv, elem, body)]

    def _subscript_container_is_obj(self, node):
        if self.is_obj_word(node) or self.value_ctype(node) == OBJ:
            return True
        if isinstance(node, (ast.Call, ast.Attribute, ast.Subscript)):
            return True
        return False

    def assign(self, node, toplevel=False):
        # `hook = rpy.json.generate_decoder(Cls)` binds a decoder to a name so a
        # hot loop can reuse it (`json.loads(s, object_hook=hook)`). The decoder
        # has no runtime existence in compiled code -- the json.loads call is
        # specialized directly -- so record the name->class mapping and emit
        # nothing for the binding itself.
        _gci = self._decoder_gen_class(node.value)
        if _gci is not None and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            self._json_hook_class[node.targets[0].id] = _gci
            return ["/* %s = json decoder for %s (elided) */"
                    % (node.targets[0].id, _gci.name)]
        fused = self._try_fused_array_store(node)
        if fused is not None:
            return fused
        rhs = self.expr(node.value)
        lines = []
        for tgt in node.targets:
            if isinstance(tgt, (ast.Tuple, ast.List)):
                # a, b = expr  ->  bind each element from the materialized rhs
                self.loop_n += 1
                tmp = "_u%d" % self.loop_n
                lines.append("obj %s = %s;" % (tmp, rhs))
                for i, el in enumerate(tgt.elts):
                    if isinstance(el, ast.Name) and el.id == "_":
                        continue
                    src = "index_obj(%s, %d)" % (tmp, i)
                    if isinstance(el, ast.Name):
                        if el.id not in self.scope and el.id not in self.hoisted:
                            self.scope[el.id] = OBJ
                            lines.append("obj %s = %s;" % (self.lid(el.id), src))
                        else:
                            t = self.scope.get(el.id, OBJ)
                            lines.append("%s = %s;" % (self.lid(el.id),
                                                       self.unwrap_obj(t, src)))
                    elif isinstance(el, ast.Subscript):
                        lines.append("subscript_set(%s, %s, %s);" % (
                            self.expr(el.value), self.wrap_obj(el.slice), src))
                    elif isinstance(el, (ast.Tuple, ast.List)):
                        # A *nested* target: `(a, b), c = ...`. Falling through
                        # to the generic branch below asked `expr()` for a
                        # tuple as an lvalue, which emitted the element names
                        # without ever declaring them -- gcc then reported
                        # every one of them undeclared. `bind_target` already
                        # recurses through Tuple/List, so hand it the element.
                        lines += self.bind_target(el, src)
                    elif isinstance(el, ast.Attribute):
                        # Unpack the i-th element into this attribute. Must use
                        # `src` (index_obj(tmp, i)), NOT the whole RHS node:
                        # passing node.value here assigned the entire tuple to
                        # every attribute target (e.g. `self.a, self.b = x, y`
                        # set both a and b to the list [x, y]).
                        if self._attr_is_property(el) or \
                                self._attr_assign_needs_setattr(el):
                            lines.append(
                                'mp_call_import("builtins", "setattr", 3, '
                                '%s, %s, %s);' % (
                                    self.wrap_obj(el.value),
                                    "OBJ_STR(%s)" % c_string(el.attr), src))
                        else:
                            t = self.target_ctype(el) or OBJ
                            lines.append("%s = %s;" % (
                                self.expr(el), self.unwrap_obj(t, src)))
                    else:
                        t = self.target_ctype(el) or OBJ
                        lines.append("%s = %s;" % (self.expr(el),
                                                   self.unwrap_obj(t, src)))
                continue
            if isinstance(tgt, ast.Subscript):
                # dst[:] = src  -- replace the list's contents in place.
                if isinstance(tgt.slice, ast.Slice) and \
                        tgt.slice.lower is None and tgt.slice.upper is None \
                        and tgt.slice.step is None:
                    vct = self.value_ctype(tgt.value)
                    if vct == "char*":
                        lines.append("strcpy(%s, AS_STR(%s));" % (
                            self.expr(tgt.value), self.wrap_obj(node.value)))
                    else:
                        lines.append("list_assign_slice(%s, %s);" % (
                            self.expr(tgt.value), self.wrap_obj(node.value)))
                    continue
                # dst[lo:hi] = src  -- splice (step must be absent)
                if isinstance(tgt.slice, ast.Slice) and tgt.slice.step is None:
                    lo = self.coerce_to("int", tgt.slice.lower,
                                        self.expr(tgt.slice.lower)) \
                        if tgt.slice.lower is not None else "0"
                    hi = self.coerce_to("int", tgt.slice.upper,
                                        self.expr(tgt.slice.upper)) \
                        if tgt.slice.upper is not None else "pylen(%s)" % \
                        self.expr(tgt.value)
                    lines.append("list_set_slice(%s, %s, %s, %s);" % (
                        self.expr(tgt.value), lo, hi, self.wrap_obj(node.value)))
                    continue
                # rpython typed dict: d[k] = v -> _tdict_K_V_set(d, k, v)
                tdct = self._typed_dict_ct(tgt.value)
                if tdct is not None and not isinstance(tgt.slice, ast.Slice):
                    name = tdct[:-1]
                    kct, vct = self._tdict_by_name[name]
                    lines.append("%s_set(%s, %s, %s);" % (
                        name, self.expr(tgt.value),
                        self.coerce_to(kct, tgt.slice, self.expr(tgt.slice)),
                        self.coerce_to(vct, node.value,
                                       self.expr(node.value))))
                    continue
                if self._subscript_container_is_obj(tgt.value) or \
                        isinstance(tgt.value, ast.Call):
                    lines.append("subscript_set(%s, %s, %s);" % (
                        self.expr(tgt.value), self.wrap_obj(tgt.slice),
                        self.wrap_obj(node.value)))
                elif self.value_ctype(tgt.value) == "char*" and \
                        not isinstance(tgt.slice, ast.Slice):
                    # mutable byte buffer (e.g. malloc'd char*): store the byte
                    # directly rather than through the string read-accessor.
                    lines.append("%s[%s] = %s;" % (
                        self.expr(tgt.value), self.as_long(tgt.slice), rhs))
                else:
                    lines.append("%s = %s;" % (self.expr(tgt), rhs))
                continue
            if isinstance(tgt, ast.Name):
                if tgt.id in self.mod_global_types and tgt.id not in self.scope:
                    lines.append("%s = %s;" % (
                        self._msym(tgt.id),
                        self.coerce_to(self.mod_global_types[tgt.id],
                                       node.value, rhs)))
                    continue
                # already declared in this scope?  ->  plain reassignment
                if tgt.id in self.scope and not toplevel:
                    lines.append("%s = %s;" % (
                        self.lid(tgt.id),
                        self.coerce_to(self.scope[tgt.id], node.value, rhs)))
                    continue
                # the value's actual C type wins over the name guess
                vct = self.value_ctype(node.value)
                ctype = vct or infer_from_name(tgt.id) or OBJ
                if vct == OBJ and infer_from_name(tgt.id) in ("char*", "int", "bool"):
                    ctype = OBJ
                if not toplevel:
                    self.scope[tgt.id] = ctype
                if toplevel and ctype == OBJ and rhs == "OBJ_NONE":
                    # A file-scope `obj` is zero-initialized and T_NONE == 0, so
                    # an omitted initializer already yields OBJ_NONE. Emit it
                    # without the OBJ_NONE compound literal, which the ShivyCX C
                    # front end rejects as a static-storage initializer (gcc
                    # accepts it; this keeps the generated C self-hostable).
                    lines.append("%s %s;" % (ctype, cname(tgt.id)))
                else:
                    lines.append("%s %s = %s;" % (ctype, cname(tgt.id), rhs))
            else:
                if isinstance(tgt, ast.Attribute):
                    lines.append(self._emit_attr_assign(tgt, node.value))
                else:
                    lines.append("%s = %s;" % (
                        self.expr(tgt),
                        self.coerce_to(self.target_ctype(tgt), node.value, rhs)))
        return lines

    def wrap_for_assign(self, value_node, rendered):
        if self.value_ctype(value_node) in ("int", "bool", "char*"):
            return self.wrap_obj(value_node)
        return rendered

    def _attr_assign_needs_setattr(self, tgt):
        """Attribute store that cannot lower to a struct field offset."""
        if not isinstance(tgt, ast.Attribute):
            return False
        # `module_alias.attr = v` is a store to that module's global, which the
        # attribute-read path already resolves (e.g. p.cur_func_name reads as
        # the module global). Let the write resolve the same way rather than
        # falling back to dynamic setattr; the module global is declared in the
        # source, so this stays pure C.
        if isinstance(tgt.value, ast.Name) and \
                (tgt.value.id in self.import_alias or
                 tgt.value.id in self.modules):
            return False
        if isinstance(tgt.value, ast.Name) and tgt.value.id == "self" \
                and self.cur_class:
            if self.cur_class.field_ctype(tgt.attr) is not None:
                return False
        bt = self.value_ctype(tgt.value)
        if bt and bt.endswith("*") and bt != OBJ:
            if self._class_has_field(bt[:-1], tgt.attr):
                return False
            return True
        if self.is_obj_word(tgt.value) or bt == OBJ or \
                isinstance(tgt.value, ast.Call):
            # An untyped (obj) base whose attribute uniquely identifies one
            # class is a real struct field, not a dynamic attribute: let it
            # lower to a field store (the lvalue path casts via the unique
            # owner), exactly as attribute *reads* already do. Only an
            # attribute owned by no known class needs dynamic setattr.
            if self.resolve_attr_owner(tgt.attr) is not None:
                return False
            return True
        return False

    def _attr_is_property(self, tgt):
        if not isinstance(tgt, ast.Attribute):
            return False
        bt = self.value_ctype(tgt.value)
        if not (bt and bt.endswith("*") and bt != OBJ):
            return False
        cls = bt[:-1]
        ci = self.classes.get(cls) or \
            (self.xclasses[cls][0] if cls in self.xclasses else None)
        return ci is not None and tgt.attr in ci.property_methods

    def _emit_attr_assign(self, tgt, value_node):
        val = self.wrap_obj(value_node)
        if self._attr_is_property(tgt) or self._attr_assign_needs_setattr(tgt):
            # mp_call_import reads each variadic argument as a 16-byte `obj`
            # (va_arg(ap, obj)), so the attribute name must be passed as an
            # OBJ_STR, not a bare `const char*` -- otherwise the 8-byte pointer
            # is misread as the front half of an obj and the rest is pulled
            # from the next argument, yielding a garbage name (AS_STR -> NULL,
            # crashing rt_setattr's strcmp).
            return 'mp_call_import("builtins", "setattr", 3, %s, %s, %s);' % (
                self.wrap_obj(tgt.value),
                "OBJ_STR(%s)" % c_string(tgt.attr), val)
        t = self.target_ctype(tgt) or OBJ
        raw = self.expr(value_node)
        return "%s = %s;" % (self.expr(tgt), self.coerce_to(t, value_node, raw))

    def value_ctype(self, node):
        """Best-effort C type of an expression, when determinable."""
        # `x.count(y)` lowers to `list_count`, which returns `long`. Without
        # this the target is inferred `obj` and the assignment is rejected --
        # "incompatible types when assigning to type 'obj' from type 'long
        # int'". The helper takes a boxed receiver, so this is only about the
        # result type. `index` is deliberately excluded: `list_index` returns
        # `obj`, and typing it `long` traded one error for another.
        if isinstance(node, ast.Call) and len(node.args) == 1 \
                and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "count" \
                and node.func.attr not in self.method_owners:
            return "long"
        if isinstance(node, ast.Call):
            _jm = self._match_json_decode(node)
            if _jm is not None:
                return _jm[0].csym + "*"
            _sym = self.ctypes_call_symbol(node)
            if _sym is not None:
                return self.ctypes_funcs.get(
                    _sym, {}).get("restype", "int")
        if isinstance(node, ast.Call) and len(node.args) == 1:
            f = node.func
            is_copy = (
                (isinstance(f, ast.Attribute) and f.attr == "copy"
                 and isinstance(f.value, ast.Name) and f.value.id == "copy")
                or (isinstance(f, ast.Name) and f.id == "copy"
                    and self.from_imports.get("copy") == "copy"))
            if is_copy:
                return self.value_ctype(node.args[0])  # shallow copy keeps type
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "__closure_env__":
            return OBJ                  # make_closure(...) yields a Tier-2 obj
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "next":
            # next(it[, default]) is always emitted as an obj statement-expr
            # (`({ obj _nx = ...; ... : OBJ_NONE; })`), so its static type must
            # be obj too -- otherwise a name-typed-int target like
            # `chunk = next(...)` is declared `int` and the obj RHS won't assign.
            return OBJ
        # Methods on a dynamically compiled pattern: search/match/finditer and
        # findall all yield obj, sub yields a C string.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) \
                and (node.func.value.id in self._regex_dyn_vars
                     or node.func.value.id in self._regex_var_pat):
            if node.func.attr in ("search", "match", "finditer", "findall"):
                return OBJ
            if node.func.attr == "sub":
                return "char*"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "re" \
                and node.func.attr in ("finditer", "findall"):
            return OBJ
        # `re.sub(...)` at module level lowers to `_cre_sub`, which returns a
        # C string -- the same type the `pat.sub` method form above reports.
        # Without this the call reads as obj, so an assignment into an obj
        # local emitted the raw `char*` with no `OBJ_STR` around it and the
        # generated C did not compile. Sixteen of `tools/cpprust.py`'s
        # seventy-six errors were this one rule. `re.escape` is *not*
        # listed: `_re_escape` returns obj, and claiming char* here would
        # trade this bug for its mirror image.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "re" \
                and node.func.attr == "sub":
            return "char*"
        # `.start()`/`.end()` on a match are byte offsets, so they are long,
        # not obj -- checked before the obj group below.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and (self._regex_ids or self._regex_dyn or self._regex_used) \
                and node.func.attr in ("start", "end") \
                and node.func.attr not in self.method_owners \
                and node.func.attr not in self.xmethod_owners:
            # Same conservative guard as .group() below: a receiver known to
            # hold a match takes it outright, anything else only when no real
            # class defines .start/.end. Requiring a tracked receiver was too
            # narrow -- a match reached through a call or an element still
            # yields an offset, and typing it obj made `m.end() - 1` lower to
            # obj arithmetic on a long.
            return "long"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and (self._regex_ids or self._regex_dyn or self._regex_used) \
                and node.func.attr in ("search", "match", "group") \
                and node.func.attr not in self.method_owners \
                and node.func.attr not in self.xmethod_owners:
            # translation-time regex: `.search`/`.match` return a match list or
            # None, `.group` returns a captured string -- all obj. Without this
            # they default to int and a boolean/assign context mis-handles the
            # obj the matcher actually returns.
            return OBJ
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            _f = node.func
            if isinstance(_f.value, ast.Attribute) and \
                    isinstance(_f.value.value, ast.Name) and \
                    _f.value.value.id == "os" and _f.value.attr == "path":
                if _f.attr in ("dirname", "basename", "abspath", "join",
                               "normpath", "realpath"):
                    return "char*"       # returns a C string
                if _f.attr in ("exists", "isfile", "isdir"):
                    return "int"
            if isinstance(_f.value, ast.Name) and _f.value.id == "os" and \
                    _f.attr == "getcwd":
                return "char*"           # returns a C string, like abspath
            if isinstance(_f.value, ast.Name) and _f.value.id == "tempfile" and \
                    _f.attr == "mkdtemp":
                return "char*"           # a path, same as abspath
            if isinstance(_f.value, ast.Name) and _f.value.id == "subprocess" and \
                    _f.attr == "run":
                return OBJ               # LIST [returncode, stdout, stderr]
            if isinstance(_f.value, ast.Name) and _f.value.id == "os" and \
                    _f.attr in ("makedirs", "unlink", "remove"):
                return OBJ               # returns OBJ_NONE
            if isinstance(_f.value, ast.Name) and _f.value.id == "struct" and \
                    _f.attr in ("pack", "unpack"):
                return OBJ               # packed bytes / unpacked list
        t = self.static_type(node)
        if t:
            return t
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Add) and (self.looks_str(node.left) or
                                                 self.looks_str(node.right)):
                return "char*"
            if isinstance(node.op, ast.Mod) and self.looks_str(node.left):
                return "char*"          # `fmt % args` -> formatted string
            lt = self.value_ctype(node.left)
            rt = self.value_ctype(node.right)
            numeric = ("int", "bool", "double", "float", "long",
                       "short", "unsigned", "char", "unsigned char")
            if lt in numeric and rt in numeric:
                if isinstance(node.op, ast.Div):
                    return "double"          # Python `/` is always float
                if "double" in (lt, rt):
                    return "double"
                if "float" in (lt, rt):
                    return "float"
                if isinstance(node.op, ast.Pow):
                    return "long"            # ipow() returns a 64-bit long
                if "long" in (lt, rt):       # long widens int/short/char
                    return "long"
                return "int"
            return OBJ  # obj arithmetic yields a Tier-2 obj
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp,
                             ast.GeneratorExp)):
            return OBJ
        if isinstance(node, ast.IfExp):
            bt = self.value_ctype(node.body)
            ot = self.value_ctype(node.orelse)
            if bt == ot:
                return bt
            # Must agree with `ex_IfExp`, which unifies two integer branches
            # to the wider integer rather than boxing them. Reporting obj here
            # while it emits a `long` is the disagreement that leaves a raw
            # scalar where an obj was expected.
            if bt in _IFEXP_INTS and ot in _IFEXP_INTS:
                return _IFEXP_INTS[max(_IFEXP_INTS.index(bt),
                                       _IFEXP_INTS.index(ot))]
            return OBJ
        if isinstance(node, ast.BoolOp):
            types = [self.value_ctype(v) for v in node.values]
            return types[0] if len(set(types)) == 1 and \
                types[0] in ("int", "bool", "char*") else OBJ
        if isinstance(node, ast.Subscript):
            if self._is_sys_argv(node.value):
                # One argument is a C string; a *slice* of them is a list.
                # Reporting char* for both wrapped the list in `OBJ_STR`.
                return OBJ if isinstance(node.slice, ast.Slice) else "char*"
            if isinstance(node.slice, ast.Slice):
                return OBJ
            # Typed list/dict element type, resolved via static_type so it works
            # mid-hoist (before the receiver is in scope): an unannotated
            # `x = xs[i]` then infers the element type instead of boxing to obj.
            _st = self.static_type(node.value)
            if _st and _st.endswith("*"):
                if _st.startswith("_tlist_"):
                    return _st[len("_tlist_"):-1]
                if _st.startswith("_tdict_"):
                    _kv = self._tdict_by_name.get(_st[:-1])
                    if _kv:
                        return _kv[1]
                if _st[:-1] in _SCALAR_CTYPES and _st != "char*":
                    return _st[:-1]     # a[i] of a numeric scalar pointer (mid-hoist)
            if isinstance(node.value, ast.Subscript):
                inner = node.value
                if isinstance(inner.value, ast.Attribute) and \
                        self.const_dict_owner(inner.value) is not None:
                    return "char*"
            if self.is_obj_word(node.value) or \
                    self.value_ctype(node.value) == OBJ:
                return OBJ
            if self.value_ctype(node.value) == "char*":
                return "char*"
            vc = self.value_ctype(node.value)
            if vc and vc.startswith("_tlist_") and vc.endswith("*"):
                return vc[len("_tlist_"):-1]   # xs[i] of a typed list -> elem
            if vc and vc.startswith("_tdict_") and vc.endswith("*"):
                kv = self._tdict_by_name.get(vc[:-1])
                if kv:
                    return kv[1]               # d[k] of a typed dict -> value
            if vc and vc.endswith("*") and vc[:-1] in _SCALAR_CTYPES:
                return vc[:-1]          # a[i] of a scalar pointer -> element type
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.Not):
                return "bool"
            if isinstance(node.op, ast.USub):
                lt = self.value_ctype(node.operand)
                if self.is_obj_word(node.operand) or lt == OBJ:
                    return OBJ
                if lt and lt.endswith("*") and lt not in ("char*", OBJ):
                    return OBJ
            return self.value_ctype(node.operand)
        if isinstance(node, ast.IfExp):
            return self.value_ctype(node.body) or self.value_ctype(node.orelse)
        if isinstance(node, ast.Subscript) and \
                isinstance(node.value, ast.Subscript):
            inner = node.value
            if isinstance(inner.value, ast.Attribute) and \
                    isinstance(inner.value.value, ast.Name) and \
                    inner.value.value.id == "self" and self.cur_class and \
                    inner.value.attr in self.cur_class.const_dicts:
                return "char*"
        if self._std_stream(node) is not None:
            return "FILE*"
        if isinstance(node, ast.Attribute) and \
                isinstance(node.value, ast.Name) and node.value.id == "socket" \
                and node.attr in _SOCK_CONSTS:
            return "int"
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in MATH_FUNCS:
                return "double"
            if isinstance(f, ast.Attribute) and f.attr in MATH_FUNCS and \
                    isinstance(f.value, ast.Name) and \
                    f.value.id in ("math", "np", "numpy"):
                return "double"
            if isinstance(f, ast.Attribute):
                if isinstance(f.value, ast.Name) and f.value.id == "socket" \
                        and f.attr == "socket":
                    return "sockfd"
                if isinstance(f.value, ast.Name) and f.value.id == "os" \
                        and f.attr == "fork":
                    return "int"
                if self.value_ctype(f.value) == "sockfd":
                    if f.attr == "accept":
                        return "sockfd"
                    if f.attr in ("connect", "bind", "listen", "send", "recv",
                                  "setsockopt", "close"):
                        return "int"
                if isinstance(f.value, ast.Name) and f.value.id == "os" \
                        and f.attr == "system":
                    return "int"
                # `subprocess.check_call` lowers to `system`, so it has the
                # same `int` result. Without this the target is inferred `obj`
                # and the assignment is rejected.
                if isinstance(f.value, ast.Name) \
                        and f.value.id == "subprocess" \
                        and f.attr in ("check_call", "call"):
                    return "int"
                if self.value_ctype(f.value) == "FILE*":
                    if f.attr in ("read", "readline"):
                        return "char*"
                    if f.attr == "write":
                        return "int"
                    if f.attr == "close":
                        return "int"
            if isinstance(f, ast.Name):
                if f.id == "open":
                    return "FILE*"
                if f.id == "input":
                    return "char*"
                if f.id == "isinstance":
                    return "bool"
                if f.id in ("any", "all"):
                    return "bool"
                if f.id in ("chr", "repr"):
                    return "char*"
                if f.id in ("str", "bytes"):
                    if self.stdlib_root and len(node.args) != 1:
                        return OBJ
                    return "char*"
                if f.id == "getattr" and len(node.args) >= 2 and \
                        isinstance(node.args[1], ast.Constant) and \
                        isinstance(node.args[1].value, str):
                    ci = self._dyn_struct_ci(node.args[0])
                    if ci is not None and \
                            ci.field_ctype(node.args[1].value) is not None:
                        return ci.field_ctype(node.args[1].value)
                    owner = self.resolve_attr_owner(node.args[1].value)
                    return owner.field_ctype(node.args[1].value) if owner \
                        else OBJ
                if f.id in ("len", "ord", "int", "abs", "const"):
                    return "int" if f.id != "const" else (
                        self.value_ctype(node.args[0]) if node.args else "int")
                if f.id == "bool":
                    return "bool"
                if f.id == "float":
                    # float(x) lowers to a plain `(double)x` cast when x is
                    # already a concrete numeric (see ex_Call's fast path), and
                    # only to a Tier-2 `pyfloat(...)` obj otherwise. Mirror that
                    # here so a coercion of the result doesn't wrap a real double
                    # in pyfloat (which expects an obj) -> a C type error.
                    if node.args:
                        _at = self.value_ctype(node.args[0])
                        if _at in ("int", "bool", "double", "float", "long",
                                   "short", "char"):
                            return "double"
                    return OBJ          # pyfloat(...) yields a Tier-2 obj
                if f.id in ("range", "sorted", "list", "dict", "set",
                            "reversed", "enumerate", "max", "min", "sum",
                            "zip", "map", "filter", "vars"):
                    return OBJ
                if f.id in self.mod_global_types:
                    return self.mod_global_types[f.id]
                if f.id in self.classes:
                    return f.id + "*"
                if f.id in self.func_returns:
                    return self.func_returns[f.id]
                # calling an obj-typed local/param (first-class function) -> obj
                if f.id in self.scope and self.scope[f.id] == OBJ \
                        and f.id not in self.func_params \
                        and f.id not in self.classes:
                    return OBJ
                if f.id in self.from_imports:
                    kind, info = self.resolve_import(f.id,
                                                     self.from_imports[f.id])
                    if kind == "class":
                        cs = getattr(info, "csym", None)
                        if cs and f.id in self.ambiguous:
                            return cs + "*"
                        return cname(f.id) + "*"
                    if kind == "func":
                        return self._imported_ret_ctype(info)
            if isinstance(f, ast.Attribute):
                # float.fromhex("0x..") -> a Tier-2 float obj
                if f.attr == "fromhex" and isinstance(f.value, ast.Name) \
                        and f.value.id == "float":
                    return OBJ
                # a method dispatched through an imported module's vtable (see
                # ex_Call's _exclusive_vt_module): report its logical return --
                # obj/scalar for predicates (matching the obj slot so boolean
                # lowering wraps truthy), or a leaf class pointer for class
                # returns (so ex_Call recovers the typed pointer via AS_OBJ).
                if not (isinstance(f.value, ast.Name) and (
                        f.value.id in self.import_alias
                        or f.value.id in self.modules
                        or f.value.id in self.classes
                        or f.value.id in self.xclasses)):
                    _xm = self._exclusive_vt_module(f.attr)
                    if _xm is not None:
                        return self._ximported_logical_ret(_xm, f.attr)
                # isinstance-narrowed receiver: report the concrete method's
                # return type, matching ex_Call's narrowing dispatch.
                if isinstance(f.value, ast.Name) and \
                        f.value.id in self.narrowed:
                    cls = self.narrowed[f.value.id][:-1]
                    ci = self.classes.get(cls) or (self.xclasses[cls][0]
                                                   if cls in self.xclasses
                                                   else None) \
                        or self._ci_by_csym(cls)
                    if ci is not None and self._class_is_leaf(cls):
                        owner = ci.find_method_owner(f.attr)
                        if owner is None and f.attr in ci.methods:
                            owner = ci
                        if owner is not None and f.attr in owner.methods:
                            m = owner.methods[f.attr]
                            return self._logical_ret(m)
                if isinstance(f.value, ast.Name) and \
                        f.value.id in self.import_alias:
                    kind, info = self.resolve_import(
                        f.attr, self.import_alias[f.value.id])
                    if kind == "class":
                        cs = getattr(info, "csym", None)
                        if cs and f.attr in self.ambiguous:
                            return cs + "*"
                        return cname(f.attr) + "*"
                    if kind == "func":
                        return self._imported_ret_ctype(info)
                    if kind == "global":
                        return OBJ
                    if self.stdlib_root:
                        return OBJ
                if isinstance(f.value, ast.Name) and \
                        f.value.id in self.modules and self.stdlib_root:
                    return OBJ
                if f.attr == "get" and isinstance(f.value, ast.Attribute) \
                        and isinstance(f.value.value, ast.Name) \
                        and f.value.value.id == "self" and self.cur_class \
                        and f.value.attr in self.cur_class.const_dicts:
                    return "char*"
                if f.attr in VTABLE_METHODS:
                    return self._logical_ret(self.method_proto(f.attr)[2])
                # A concrete class instance whose class actually defines this
                # method must use the method's real return type, never the
                # string/dict-method *name* heuristics below: a user method
                # named `get`/`split`/`pop`/... (local or imported) would
                # otherwise be mistyped, so a scalar/typed-list return is lost.
                _rbt = self.value_ctype(f.value)
                if _rbt and _rbt.endswith("*") and _rbt != OBJ \
                        and not _rbt.startswith("_tlist_"):
                    _rcls = _rbt[:-1]
                    _rci = self.classes.get(_rcls) or \
                        (self.xclasses[_rcls][0]
                         if _rcls in self.xclasses else None)
                    if _rci is not None:
                        _rowner = _rci.find_method_owner(f.attr)
                        if _rowner and _rowner.methods.get(f.attr):
                            return self._logical_ret(_rowner.methods[f.attr])
                # `xs.pop()` on an unboxed typed list yields its element scalar.
                if f.attr == "pop" and not node.args:
                    _tl = self._typed_list_ct(f.value)
                    if _tl is not None:
                        return _tl[len("_tlist_"):-1]
                if f.attr not in self.method_owners:
                    if f.attr in ("startswith", "endswith", "isdigit",
                                  "isalpha", "isspace", "isalnum"):
                        return "bool"
                    if f.attr in ("strip", "lstrip", "rstrip", "replace",
                                  "lower", "upper", "encode", "join"):
                        return "char*"
                    if f.attr in ("split", "splitlines", "keys", "values",
                                  "items", "get", "pop", "setdefault"):
                        return OBJ
                    if f.attr in ("find", "rfind"):
                        return "int"
                # method call on a concrete class instance (local or imported)
                bt = self.value_ctype(f.value)
                if bt and bt.endswith("*") and bt != OBJ:
                    cls = bt[:-1]
                    ci = self.classes.get(cls) or \
                        (self.xclasses[cls][0] if cls in self.xclasses else None)
                    if ci:
                        owner = ci.find_method_owner(f.attr)
                        if owner:
                            m = owner.methods.get(f.attr)
                            return self._logical_ret(m) if m else OBJ
                # method call on an untyped obj -> unique local/imported owner
                if self.is_obj_word(f.value) or bt == OBJ:
                    # a cross-module-hierarchy-dispatched method (e.g. make_il)
                    # is resolved through its hierarchy root's canonical return,
                    # not an arbitrary (unannotated) local/imported override, so
                    # the typed result matches the xvcall dispatch.
                    if f.attr in self.hierarchy_method:
                        return self._ximported_logical_ret(
                            self.hierarchy_method[f.attr], f.attr)
                    owner = self.resolve_method_owner(f.attr) or \
                        self.resolve_xmethod_owner(f.attr)
                    if owner:
                        m = owner.methods.get(f.attr)
                        return self._logical_ret(m) if m else OBJ
                    xmod = self.resolve_xvirtual(f.attr)
                    if xmod:
                        return self._ximported_logical_ret(xmod, f.attr)
                    if f.attr in self.hierarchy_method:
                        return self._ximported_logical_ret(
                            self.hierarchy_method[f.attr], f.attr)
            # calling a *complex* obj-valued expression (closure/ctor returned
            # by another call, a subscript, etc.) yields obj; Name/Attribute
            # funcs are already handled above and must not fall through here
            if not isinstance(f, (ast.Name, ast.Attribute)) and \
                    (self.value_ctype(f) == OBJ or self.is_obj_word(f)):
                return OBJ
        return self.guess_from_value(node)

    def _is_eval_call(self, node):
        """True for a bare `eval("...")` (one positional arg, no keywords)."""
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "eval" and len(node.args) == 1
                and not node.keywords)

    def _eval_typed(self, ctype, srcnode):
        """The typed bridge call for `<ctype> x = eval(src)`, or None if `ctype`
        is not a scalar the MicroPython bridge can extract directly (then the
        caller falls back to the boxed `rpy_eval`)."""
        src = self.as_str(srcnode)
        if ctype in ("double", "float"):
            return "rpy_eval_float(%s)" % src
        if ctype in ("char*", "str"):
            return "rpy_eval_str(%s)" % src
        if ctype == "bool":
            return "rpy_eval_bool(%s)" % src
        if ctype in ("int", "long", "short", "char"):
            return "rpy_eval_int(%s)" % src
        return None

    def st_AnnAssign(self, node, toplevel=False):
        ctype = self._local_ann_ctype(
            getattr(node.target, "id", "x"), node.annotation)
        # `self.x: T = ...` is a *field* initializer: the storage is the struct
        # member, whose ctype was fixed at class discovery. List/dict fields are
        # currently kept as the boxed `obj`, but the annotation re-resolves here
        # to an unboxed `_tlist_T*` / `_tdict_*`; assigning that to an `obj`
        # member is a C type error. Honor the field's declared type so the
        # initializer is built boxed to match the member (a correct, if boxed,
        # field). Drop this once fields carry unboxed typed-container storage.
        if isinstance(node.target, ast.Attribute) and \
                isinstance(node.target.value, ast.Name) and \
                node.target.value.id == "self" and self.cur_class is not None:
            fct = self.cur_class.field_ctype(node.target.attr)
            if fct and fct != ctype:
                ctype = fct
        if isinstance(node.target, ast.Name):
            et = ann_elem_ctype(node.annotation)
            if not et:
                kv = ann_dict_kv(node.annotation)
                if kv:
                    et = kv[0]                # iterating a dict yields keys
            if et:
                self.elem_types[node.target.id] = et
            sz = ann_array_size(node.annotation)
            if sz:
                self.array_sizes[node.target.id] = sz
        tgt = self.expr(node.target)
        # A module-scope class-pointer global initialized to None (e.g.
        # `_v: "V" = None`) is stored as the boxed `obj`, matching how class
        # values flow everywhere else and how the global-type registry records
        # it (collect_module_globals coerces the same `*`+None case to obj).
        # This keeps the file-scope declaration consistent with every
        # `global x; x = ...` store and read, and obj zero-init is a valid
        # static (.bss) initializer (T_NONE == 0), whereas
        # `V* x = (V*)AS_OBJ(OBJ_NONE)` is not a constant expression. Enables
        # runtime-initialized typed globals (value caches, global lookup tables).
        if toplevel and isinstance(node.target, ast.Name) \
                and isinstance(node.value, ast.Constant) \
                and node.value.value is None \
                and isinstance(ctype, str) and ctype.endswith("*") \
                and ctype[:-1] in self.classes:
            ctype = OBJ
        if node.value is None:
            return ["%s %s;" % (ctype, tgt)]
        # A module-scope `obj` initialized to None is zero-initialized and
        # T_NONE == 0, so drop the OBJ_NONE compound literal, which the ShivyCX
        # C front end rejects as a static-storage initializer (gcc accepts it).
        if toplevel and ctype == OBJ and isinstance(node.value, ast.Constant) \
                and node.value.value is None:
            return ["%s %s;" % (ctype, tgt)]
        already = isinstance(node.target, ast.Name) and \
            node.target.id in self.scope
        decl = "" if (already or not isinstance(node.target, ast.Name)) \
            else (ctype + " ")
        rhs = self.expr(node.value)
        # Typed eval: `x: int = eval("...")` pulls the value straight out of the
        # MicroPython result object (no obj boxing) when the annotation is a
        # scalar the bridge has a typed extractor for.
        if isinstance(ctype, str) and self._is_eval_call(node.value):
            typed = self._eval_typed(ctype, node.value.args[0])
            if typed is not None:
                self._uses_eval = True
                return ["%s%s = %s;" % (decl, tgt, typed)]
        # rpython typed list: `xs: "list[int]" = [..]` builds an unboxed array.
        if isinstance(ctype, str) and ctype.startswith("_tlist_") and \
                isinstance(node.value, ast.List):
            rhs = self._typed_list_literal(ctype, node.value)
        if isinstance(ctype, str) and ctype.startswith("_tdict_") and \
                isinstance(node.value, ast.Dict):
            rhs = self._typed_dict_literal(ctype, node.value)
        # A class instance assigned to an obj-typed target (e.g. a non-leaf
        # base annotation `base: "Shape*"`, which is typed obj for sound
        # dynamic dispatch) must be boxed into the tagged union.
        vt = self.value_ctype(node.value)
        if ctype == OBJ and vt and vt != OBJ and vt.endswith("*") \
                and vt[:-1] in self.classes:
            rhs = self.wrap_obj(node.value)
        # An obj-valued RHS assigned to a concrete scalar annotation needs an
        # unbox (e.g. `start_index: int = max(...)`, where max() is a Tier-2
        # obj): coerce so the declared C type and the value agree.
        elif isinstance(ctype, str) and \
                ctype in ("int", "long", "bool", "double", "char*") and \
                (vt == OBJ or self.is_obj_word(node.value)):
            rhs = self.coerce_to(ctype, node.value, rhs)
        # An obj-valued RHS assigned to a concrete class-pointer annotation is a
        # checked downcast (e.g. `node_typed: "Node" = node`, where `node` came
        # back from a closure as a Tier-2 obj). Insert the unbox so `.attr`
        # stores become real struct-field writes instead of dynamic setattr.
        elif isinstance(ctype, str) and ctype.endswith("*") and ctype != OBJ \
                and (ctype[:-1] in self.classes or ctype[:-1] in self.xclasses) \
                and (vt == OBJ or self.is_obj_word(node.value)):
            rhs = self.coerce_to(ctype, node.value, rhs)
        return ["%s%s = %s;" % (decl, tgt, rhs)]

    AUG_OP_CHAR = {ast.Add: '+', ast.Sub: '-', ast.Mult: '*', ast.Div: '/',
                   ast.FloorDiv: '/', ast.Mod: '%', ast.BitOr: '|',
                   ast.BitAnd: '&', ast.BitXor: '^', ast.LShift: '<',
                   ast.RShift: '>'}

    def st_AugAssign(self, node):
        # `c[i] += v` on an obj container: read-modify-write via subscript_set,
        # since `subscript(...)` is an rvalue and cannot be assigned to.
        if isinstance(node.target, ast.Subscript) and \
                (self.is_obj_word(node.target.value) or
                 self.value_ctype(node.target.value) == OBJ or
                 isinstance(node.target.value, ast.Call)):
            cont = self.expr(node.target.value)
            idx = self.wrap_obj(node.target.slice)
            ch = self.AUG_OP_CHAR.get(type(node.op), '+')
            cur = "subscript(%s, %s)" % (cont, idx)
            return ["subscript_set(%s, %s, obj_augop(%s, '%c', %s));" % (
                cont, idx, cur, ch, self.wrap_obj(node.value))]
        tgt = self.expr(node.target)
        tt = self.target_ctype(node.target) or self.value_ctype(node.target)
        if tt == OBJ or self.is_obj_word(node.target):
            ch = self.AUG_OP_CHAR.get(type(node.op), '+')
            return ["%s = obj_augop(%s, '%c', %s);" % (
                tgt, tgt, ch, self.wrap_obj(node.value))]
        if tt and tt.endswith("*") and tt[:-1] in self.classes:
            ch = self.AUG_OP_CHAR.get(type(node.op), '+')
            return ["%s = (%s)AS_OBJ(obj_augop(OBJ_OBJ(%s), '%c', %s));" % (
                tgt, tt, tgt, ch, self.wrap_obj(node.value))]
        if isinstance(node.op, ast.Add) and tt == "char*":
            return ["%s = pyconcat(%s, %s);" % (tgt, tgt,
                                                self.as_str(node.value))]
        return ["%s %s= %s;" % (tgt, self.binop_sym(node.op),
                                self.coerce_to(tt, node.value,
                                               self.expr(node.value)))]

    def _try_finallys(self, stop_at_loop):
        """Finally bodies (innermost-first) and the g_exc_sp restore target for
        an early exit (return / break / continue) that escapes open try blocks.
        Returns (list_of_c_lines, restore_target_or_None)."""
        out, restore = [], None
        scan = []
        for e in reversed(self.try_stack):
            if e["kind"] == "loop":
                if stop_at_loop:
                    restore = e["esp"]
                    break
                else:
                    continue
            scan.append(e)              # a try, innermost-first
        for e in scan:
            if e["finally"]:
                out += self.suite(e["finally"])
            if not stop_at_loop:
                restore = e["fr"]       # for return: pop to outermost try's frame
        if not stop_at_loop and scan:
            restore = scan[-1]["fr"]    # outermost open try
        return out, restore

    def st_Return(self, node):
        pre, restore = self._try_finallys(stop_at_loop=False)
        ret = getattr(self, "cur_ret", OBJ)
        if node.value is None:
            r = "return OBJ_NONE;" if ret in (OBJ, "obj") else "return;"
            tail = (["g_exc_sp = %s;" % restore] if restore is not None else [])
            return pre + tail + [r]
        val = self.coerce_to(ret, node.value, self.expr(node.value))
        if restore is None and not pre:
            return ["return %s;" % val]
        # Inside a try: evaluate the return expression while the handler frames
        # are still active (so a raise from within it is caught here), THEN run
        # finallys, pop frames, and return the stashed value.
        self.exc_n += 1
        tmp = "_rv%d" % self.exc_n
        out = ["%s %s = %s;" % (ret, tmp, val)]
        out += pre
        if restore is not None:
            out.append("g_exc_sp = %s;" % restore)
        out.append("return %s;" % tmp)
        return out

    def st_Assert(self, node):
        test = self.bool_expr(node.test)
        if node.msg:
            msg = self.wrap_obj(node.msg)
            return ['if (!(%s)) { fprintf(stderr, "AssertionError: %%s\\n", AS_STR(%s)); abort(); }' % (test, msg)]
        return ['if (!(%s)) { fprintf(stderr, "AssertionError\\n"); abort(); }' % test]

    def st_Pass(self, node):
        return ["/* pass */"]

    def st_Break(self, node):
        pre, restore = self._try_finallys(stop_at_loop=True)
        tail = (["g_exc_sp = %s;" % restore] if restore is not None else [])
        # A break means the loop did NOT run to completion, so its else-suite
        # must be skipped: clear the nearest enclosing loop's for/while-else
        # flag (if any) before leaving.
        fe = None
        for e in reversed(self.try_stack):
            if e["kind"] == "loop":
                fe = e.get("fe")
                break
        if fe:
            tail = ["%s = 0;" % fe] + tail
        return pre + tail + ["break;"]

    def st_Continue(self, node):
        pre, restore = self._try_finallys(stop_at_loop=True)
        tail = (["g_exc_sp = %s;" % restore] if restore is not None else [])
        return pre + tail + ["continue;"]

    def st_Global(self, node):
        return ["/* global %s */" % ", ".join(node.names)]

    def st_Delete(self, node):
        out = []
        for t in node.targets:
            # `del xs[a:b]` is `xs[a:b] = []`, and the helper for that already
            # exists. It used to fall through to the no-op comment below and
            # silently do nothing: `tools/cpprust.py` strips a consumed option
            # with `del args[i:i + 2]`, so every flag stayed in the list and
            # the program printed its own usage instead of running.
            if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Slice) \
                    and t.slice.step is None:
                lo = self.as_long(t.slice.lower) \
                    if t.slice.lower is not None else "0"
                hi = self.as_long(t.slice.upper) \
                    if t.slice.upper is not None else \
                    "pylen(%s)" % self.expr(t.value)
                out.append("list_set_slice(%s, %s, %s, list_new());"
                           % (self.expr(t.value), lo, hi))
                continue
            if isinstance(t, ast.Subscript) and not isinstance(t.slice,
                                                               ast.Slice):
                out.append("del_item(%s, %s);" % (self.wrap_obj(t.value),
                                                  self.wrap_obj(t.slice)))
                continue
            # `del obj` on a heap object (a class instance / struct pointer)
            # hands its storage back to the arena's free list so a later
            # allocation of the same size reuses it -- manual memory management
            # in the ShivyCX source, no refcounting. Borrowed/scalar values
            # (char* strings, obj, int, ...) keep the bulk-reclaim no-op;
            # afree itself also ignores anything not on the arena.
            ct = None
            if isinstance(t, (ast.Name, ast.Attribute)):
                try:
                    ct = self.value_ctype(t)
                except Exception:
                    ct = None
            # `del p` on a heap pointer reclaims its storage. A non-POD class
            # instance lives in the runtime arena (aalloc) and is returned to
            # the arena free list with afree; a POD instance or a typed array
            # is libc-malloc'd, so it is released with free(). Borrowed/scalar
            # values (char*, void*, obj, int, ...) keep the bulk-reclaim no-op.
            # A typed list owns two allocations -- the struct and its backing
            # array -- so it must be released through its own helper. The
            # generic pointer branch below would emit a bare free(l), which
            # frees the struct and leaks l->data; this has to be tested first.
            if ct and ct.startswith("_tlist_") and ct.endswith("*"):
                out.append("%s_free(%s);" % (ct[:-1], self.expr(t)))
                continue
            if ct and ct.endswith("*") and ct not in ("char*", "void*"):
                expr = self.expr(t)
                cls = ct[:-1]
                ci = self.classes.get(cls)
                if ci is not None:
                    # Every class instance -- POD or not -- comes from aalloc,
                    # so every one goes back through afree. This used to be
                    # split, with POD instances libc-freed to match their
                    # libc-malloc; now that PODs are arena-allocated too, a
                    # free() here hands the allocator a pointer into the arena
                    # ("free(): invalid pointer", immediately, on the first
                    # `del` of a POD).
                    out.append("afree(%s, sizeof(*(%s)));" % (expr, expr))
                else:
                    # Not a class: a typed array or other libc-malloc'd buffer.
                    out.append("free(%s);" % expr)
                continue
            # `del` on an obj-typed value: if it holds a list, hand it back to
            # the arena. list_free tag-checks, so this stays a no-op for any
            # other obj (int, str, dict, None) and keeps the bulk-reclaim
            # meaning of `del` for those. Same manual-ownership contract as the
            # pointer case above -- `del` asserts the value is dead.
            if ct == OBJ and isinstance(t, (ast.Name, ast.Attribute)):
                out.append("list_free(%s);" % self.expr(t))
                continue
            # no-op (arena reclaims in bulk); use source text, never the
            # generated C, so inner /* */ can't break the comment
            try:
                txt = ast.unparse(t)
            except Exception:
                txt = "?"
            # A `del` of a *container element* that reaches here changes what
            # the program does, and the comment made that invisible. Reclaim
            # of a plain local is genuinely a no-op here and stays quiet.
            if isinstance(t, ast.Subscript):
                self._warn_unsupported(
                    getattr(node, "lineno", 0), "del %s" % txt, "nothing",
                    "the element is not removed")
            out.append("/* del %s */" % txt.replace("*/", "* /"))
        return out

    def st_Import(self, node):
        return ["/* " + self.src1(node) + " */"]

    def st_ImportFrom(self, node):
        return ["/* " + self.src1(node) + " */"]

    def _exc_class_ci(self, nm):
        if nm in self.classes:
            return self.classes[nm]
        if nm in self.xclasses:
            return self.xclasses[nm][0]
        return None

    def _exc_ctor_total(self, ci):
        """Positional arg count of a class's (possibly inherited) __init__,
        excluding self; 0 if none is defined in the transpiled chain."""
        while ci is not None:
            init = ci.methods.get("__init__")
            if init is not None:
                return len(init.args.args) - 1
            ci = ci.base
        return 0

    @staticmethod
    def _looks_exception_name(nm):
        """True if `nm` reads as an exception class rather than a function.

        The distinction matters at a `raise`: an exception class is
        constructed and its message carried, while anything else has to be
        called and its result raised.
        """
        return nm.endswith("Error") or nm.endswith("Exception") or \
            nm in ("StopIteration", "KeyboardInterrupt", "SystemExit",
                   "GeneratorExit", "StopAsyncIteration")

    def st_Raise(self, node):
        if node.exc is None:
            return ["rt_raise(g_exc_val);"]   # bare re-raise
        exc = node.exc
        if isinstance(exc, ast.Call):
            f = exc.func
            nm = (f.id if isinstance(f, ast.Name)
                  else f.attr if isinstance(f, ast.Attribute) else None)
            ci = self._exc_class_ci(nm) if nm else None
            if ci is not None:
                # A real transpiled exception class: construct an instance so
                # `except <Class>` (isinstance) matches. Trim surplus args to
                # the constructor's arity (e.g. _PPExprError("msg") has a 0-arg
                # ctor inherited from Exception).
                total = self._exc_ctor_total(ci)
                if not exc.keywords and len(exc.args) > total:
                    exc = ast.Call(func=f, args=exc.args[:total], keywords=[])
                    ast.copy_location(exc, node.exc)
                return ["rt_raise(%s);" % self.wrap_obj(exc)]
            # `raise make_error(...)` -- a *factory* returning an exception,
            # not an exception class. Evaluate the call and raise its result.
            # Without this it fell into the builtin-exception heuristic below,
            # which raises the call's first *argument*: crust's
            # `raise fail(e, start)` became `rt_raise(code)`, so a parse error
            # arrived at every handler as the raw source string, matched no
            # `except CrustError`, and the compiler printed the program it had
            # been asked to compile instead of the diagnostic.
            if nm is not None and not self._looks_exception_name(nm) \
                    and (nm in self.scope
                         or nm in getattr(self, "func_nodes", ())):
                return ["rt_raise(%s);" % self.wrap_obj(exc)]
            # builtin / unknown exception (NotImplementedError, ValueError, ...):
            # carry its message (or name) as a printable obj. Uncaught -> print
            # and exit; a catch-all handler still catches it.
            if exc.args:
                a0 = exc.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                    return ["rt_raise(OBJ_STR(%s));" % c_string(a0.value)]
                return ["rt_raise(%s);" % self.wrap_obj(a0)]
            return ["rt_raise(OBJ_STR(%s));" % c_string(nm or "Exception")]
        if isinstance(exc, ast.Name) and exc.id not in self.scope \
                and self._exc_class_ci(exc.id) is None:
            # bare builtin exception name, e.g. `raise NotImplementedError`
            return ["rt_raise(OBJ_STR(%s));" % c_string(exc.id)]
        return ["rt_raise(%s);" % self.wrap_obj(exc)]

    def _exc_match_cond(self, htype, ev="g_exc_val"):
        """C condition matching the in-flight exception against an except
        clause's type. None means 'catch all' (bare except, Exception, or a
        builtin/unknown type -- the native compiler is first-error-and-exit so
        precise builtin matching is unnecessary).

        `ev` names the value to test. Callers pass the handler's own snapshot
        rather than `g_exc_val`, which is a single global and does not survive
        anything the handler runs.
        """
        if htype is None:
            return None
        if isinstance(htype, ast.Tuple):
            sub = [self._exc_match_cond(e, ev) for e in htype.elts]
            if any(c is None for c in sub):
                return None
            return " || ".join("(%s)" % c for c in sub)
        csym = self._isinstance_class(htype) \
            if isinstance(htype, (ast.Name, ast.Attribute)) else None
        if csym is None:
            return None
        return ("(IS_OBJ(%s) && isinstance_of(AS_OBJ(%s), "
                "(const void*)&%s_type))" % (ev, ev, csym))

    def _isinstance_class(self, ref):
        """Resolve an isinstance() 2nd-arg class reference to a known class
        *csym* (local or imported), or None for tuples / unknown types. Uses
        the same resolution as the isinstance check itself so the narrowed cast
        targets the same same-named class the check tested against."""
        if not isinstance(ref, (ast.Name, ast.Attribute)):
            return None                 # tuple-of-types: ambiguous, don't narrow
        ci = self._resolve_class_ref(ref)
        if ci is not None:
            return ci.csym
        n = ref.id if isinstance(ref, ast.Name) else ref.attr
        if n in self.classes or n in self.xclasses:
            return n
        return None

    def _narrowings(self, test):
        """[(name, 'Cls*')] implied true by `test`: a bare isinstance(name,Cls)
        or an `and`-chain of them. Negation / `or` yield nothing."""
        out = []
        conds = test.values if isinstance(test, ast.BoolOp) and \
            isinstance(test.op, ast.And) else [test]
        for c in conds:
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) \
                    and c.func.id == "isinstance" and len(c.args) == 2 \
                    and isinstance(c.args[0], ast.Name):
                cls = self._isinstance_class(c.args[1])
                if cls:
                    out.append((c.args[0].id, cls + "*"))
        return out

    def st_If(self, node):
        # `if TYPE_CHECKING:` guards type-only imports; emit nothing for it
        if isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            return []
        # Translation-time guard (e.g. `sys.implementation.name != 'shivyc'`):
        # transpile only the live branch so host-only code in the dead branch
        # is never lowered to C.
        sc = self._static_cond(node.test)
        if sc is True:
            return self.suite(node.body)
        if sc is False:
            return self.suite(node.orelse) if node.orelse else []
        lines = ["if (%s) {" % self.bool_expr(node.test)]
        narrows = self._narrowings(node.test)
        saved = {n: self.narrowed.get(n) for n, _ in narrows}
        for n, ct in narrows:
            self.narrowed[n] = ct
        lines += self.indent_lines(self.suite(node.body))
        for n in saved:                 # restore: narrowing is block-scoped
            if saved[n] is None:
                self.narrowed.pop(n, None)
            else:
                self.narrowed[n] = saved[n]
        if node.orelse:
            if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
                inner = self.st_If(node.orelse[0])
                inner[0] = "} else " + inner[0]
                return lines + inner
            lines.append("} else {")
            lines += self.indent_lines(self.suite(node.orelse))
        lines.append("}")
        return lines

    def _body_has_try(self, body):
        # A loop needs its exception-frame marker (so break/continue inside an
        # enclosed try can restore g_exc_sp) when the body opens a try frame.
        # That includes a `with` statement: a @contextmanager `with` is inlined
        # into a try/except, and the generic `with` is lowered with a try frame
        # too -- so either form can leave an open frame a break/continue must
        # pop. Matching ast.Try alone missed `with log_error(): ... continue`,
        # which leaked g_exc_sp every iteration and corrupted the handler stack.
        for n in body:
            for sub in ast.walk(n):
                if isinstance(sub, (ast.Try, ast.With)):
                    return True
        return False

    def _loop_wrap(self, node, which):
        """Run a loop body codegen (`which` is "while" or "for") with a loop
        marker on the try-stack so break/continue inside an enclosed try restore
        the exception-frame pointer to the loop's level."""
        esp = None
        pre = []
        if self._body_has_try(node.body):
            self.exc_n += 1
            esp = "_lesp%d" % self.exc_n
            pre = ["int %s = g_exc_sp;" % esp]
        # for/while ... else: the else-suite runs iff the loop finishes WITHOUT
        # a break. Lower it with a flag that starts 1 and is cleared by any
        # break targeting this loop (see st_Break); after the loop, run the
        # else-suite when the flag is still set. Previously the orelse was
        # dropped entirely, which silently broke `for x in xs: if ...: break
        # else: return` patterns -- e.g. the parser's parse_series fell through
        # to parse another base instead of returning when no separator matched.
        fe = None
        if node.orelse:
            self.exc_n += 1
            fe = "_fe%d" % self.exc_n
            pre = pre + ["int %s = 1;" % fe]
        self.try_stack.append({"kind": "loop", "esp": esp, "fe": fe})
        try:
            if which == "while":
                lines = self._st_While_impl(node)
            else:
                lines = self._st_For_impl(node)
        finally:
            self.try_stack.pop()
        if fe:
            lines = lines + ["if (%s) {" % fe] \
                + self.indent_lines(self.suite(node.orelse)) + ["}"]
        return pre + lines

    def st_While(self, node):
        return self._loop_wrap(node, "while")

    def _st_While_impl(self, node):
        lines = ["while (%s) {" % self.bool_expr(node.test)]
        lines += self.indent_lines(self.suite(node.body))
        lines.append("}")
        return lines

    def st_For(self, node):
        return self._loop_wrap(node, "for")

    def _st_For_impl(self, node):
        it, tgt = node.iter, node.target
        if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) \
                and it.func.id == "range" and isinstance(tgt, ast.Name):
            v = cname(tgt.id)
            a = [self.coerce_to("int", x, self.expr(x)) for x in it.args]
            if len(a) == 1:
                lo, hi, stp = "0", a[0], "1"
            elif len(a) == 2:
                lo, hi, stp = a[0], a[1], "1"
            else:
                lo, hi, stp = a[0], a[1], a[2]
            # range() with a negative step counts DOWN, so the C continuation
            # test must be `>` (not `<`): `for i in range(n, -1, -1)` visits
            # n, n-1, ..., 0. Emitting `<` unconditionally made every such
            # descending loop fall through without running (e.g. the parser's
            # _find_pair_backward returned its start index instead of scanning
            # back to the matching paren). Pick the test from the step's sign:
            # a compile-time-constant step decides it directly; a runtime step
            # is tested per-iteration so either direction works.
            stepc = _const_value(it.args[2]) if len(it.args) >= 3 else 1
            if stepc is not None:
                cont = "%s %s %s" % (v, ">" if stepc < 0 else "<", hi)
            else:
                cont = "(%s) < 0 ? %s > %s : %s < %s" % (stp, v, hi, v, hi)
            decl = "" if tgt.id in self.hoisted else "int "
            if tgt.id in self.hoisted and self.scope.get(tgt.id) == OBJ:
                # The loop variable is also used as a boxed obj elsewhere (e.g.
                # reused across a range loop and an enumerate, or as a subscript
                # passed to obj helpers), so it is declared `obj`. Drive the
                # loop with a hidden int counter and box each value in, instead
                # of assigning an int straight into the obj variable.
                self.loop_n += 1
                ii = "_ri%d" % self.loop_n
                if stepc is not None:
                    icont = "%s %s %s" % (ii, ">" if stepc < 0 else "<", hi)
                else:
                    icont = "(%s) < 0 ? %s > %s : %s < %s" % (stp, ii, hi,
                                                              ii, hi)
                lines = ["for (long %s = %s; %s; %s += %s) {" %
                         (ii, lo, icont, ii, stp)]
                lines.append("    %s = OBJ_INT(%s);" % (v, ii))
                lines += self.indent_lines(self.suite(node.body))
                lines.append("}")
                return lines
            lines = ["for (%s%s = %s; %s; %s += %s) {" %
                     (decl, v, lo, cont, v, stp)]
            lines += self.indent_lines(self.suite(node.body))
            lines.append("}")
            return lines
        # `for i, x in enumerate(SRC[, start]):` -- iterate SRC directly with a
        # counter instead of materializing pyenumerate's list of [i, x] pairs.
        # This is a hot path (e.g. the lexer calls it per character), and the
        # arena never frees the temporary list, so eliminating it matters a lot.
        if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) \
                and it.func.id == "enumerate" and isinstance(tgt, ast.Tuple) \
                and len(tgt.elts) == 2 and len(it.args) in (1, 2):
            self.loop_n += 1
            srcv = "_en%d" % self.loop_n
            idx = "_k%d" % self.loop_n
            start = (self.coerce_to("int", it.args[1], self.expr(it.args[1]))
                     if len(it.args) > 1 else "0")
            lines = ["/* for %s in %s: */" % (self.src1(tgt), self.src1(it))]
            lines.append("{ obj %s = %s;" % (srcv, self.wrap_obj(it.args[0])))
            lines.append("  for (long %s = 0; %s < pylen(%s); %s++) {" %
                         (idx, idx, srcv, idx))
            binds = self.bind_target(tgt.elts[0],
                                     "OBJ_INT(%s + %s)" % (start, idx))
            binds += self.bind_target(tgt.elts[1],
                                      "index_obj(%s, %s)" % (srcv, idx))
            for b in binds:
                lines.append("    " + b)
            lines += self.indent_lines(self.indent_lines(self.suite(node.body)))
            lines.append("  }")
            lines.append("}")
            return lines
        lines = ["/* for %s in %s: */" % (self.src1(tgt), self.src1(it))]
        tlct = self._typed_list_ct(it)
        if tlct is not None and isinstance(tgt, ast.Name):
            et = tlct[len("_tlist_"):-1]
            self.loop_n += 1
            itv = "_it%d" % self.loop_n
            idx = "_k%d" % self.loop_n
            saved = self.scope.get(tgt.id)
            self.scope[tgt.id] = et
            decl = "" if tgt.id in self.hoisted else (et + " ")
            lines.append("{ %s %s = %s;" % (tlct, itv, self.expr(it)))
            lines.append("  for (long %s = 0; %s < %s->len; %s++) {" %
                         (idx, idx, itv, idx))
            lines.append("    %s%s = %s->data[%s];" %
                         (decl, self.lid(tgt.id), itv, idx))
            lines += self.indent_lines(self.indent_lines(self.suite(node.body)))
            lines.append("  }")
            lines.append("}")
            if saved is None:
                self.scope.pop(tgt.id, None)
            else:
                self.scope[tgt.id] = saved
            return lines
        # rpython typed dict: `for k in d` iterates the keys.
        tdct = self._typed_dict_ct(it)
        if tdct is not None and isinstance(tgt, ast.Name):
            kct = self._tdict_by_name[tdct[:-1]][0]
            self.loop_n += 1
            itv = "_it%d" % self.loop_n
            idx = "_k%d" % self.loop_n
            saved = self.scope.get(tgt.id)
            self.scope[tgt.id] = kct
            decl = "" if tgt.id in self.hoisted else (kct + " ")
            lines.append("{ %s %s = %s;" % (tdct, itv, self.expr(it)))
            lines.append("  for (long %s = 0; %s < %s->len; %s++) {" %
                         (idx, idx, itv, idx))
            lines.append("    %s%s = %s->keys[%s];" %
                         (decl, self.lid(tgt.id), itv, idx))
            lines += self.indent_lines(self.indent_lines(self.suite(node.body)))
            lines.append("  }")
            lines.append("}")
            if saved is None:
                self.scope.pop(tgt.id, None)
            else:
                self.scope[tgt.id] = saved
            return lines
        if isinstance(tgt, (ast.Name, ast.Tuple, ast.List)):
            self.loop_n += 1
            itv = "_it%d" % self.loop_n
            idx = "_k%d" % self.loop_n
            if isinstance(tgt, ast.Name) and tgt.id != "_" \
                    and tgt.id not in self.hoisted:
                lines.append("obj %s;" % self.lid(tgt.id))
                self.scope[tgt.id] = OBJ
                self.hoisted.add(tgt.id)
            lines.append("{ obj %s = %s;" % (itv, self.wrap_obj(it)))
            lines.append("  for (long %s = 0; %s < pylen(%s); %s++) {" %
                         (idx, idx, itv, idx))
            binds = self.bind_target(tgt, "index_obj(%s, %s)" % (itv, idx))
            for b in binds:
                lines.append("    " + b)
            lines += self.indent_lines(self.indent_lines(self.suite(node.body)))
            lines.append("  }")
            lines.append("}")
            return lines
        lines.append("FOR_EACH(%s, %s) {" % (self.src1(tgt), self.expr(it)))
        lines += self.indent_lines(self.suite(node.body))
        lines.append("}")
        return lines

    def bind_target(self, target, src, force_decl=False):
        """C statements binding `target` (Name or Tuple/List) from obj `src`."""
        out = []
        if isinstance(target, ast.Name):
            if target.id == "_":
                return ["(void)(%s);" % src]
            fresh = force_decl or target.id not in self.hoisted
            if fresh:
                # a freshly declared `obj name` shadows any outer binding of the
                # same name; record its type as obj so value_ctype is correct
                self.scope[target.id] = OBJ
            elif target.id not in self.scope:
                self.scope[target.id] = OBJ
            decl = "obj " if fresh else ""
            rhs = src
            declared = self.scope.get(target.id)
            if not fresh and declared and declared != OBJ:
                # `src` is an obj element; coerce to the target's hoisted C type
                if declared == "char*":
                    rhs = "AS_STR(%s)" % src
                elif declared == "int":
                    rhs = "AS_INT(%s)" % src
                elif declared == "bool":
                    rhs = "truthy(%s)" % src
                elif declared.endswith("*"):
                    rhs = "(%s)AS_OBJ(%s)" % (declared, src)
            out.append("%s%s = %s;" % (decl, cname(target.id), rhs))
        elif isinstance(target, (ast.Tuple, ast.List)):
            self.loop_n += 1
            tmp = "_e%d" % self.loop_n
            out.append("obj %s = %s;" % (tmp, src))
            for i, el in enumerate(target.elts):
                out += self.bind_target(el, "index_obj(%s, %d)" % (tmp, i),
                                        force_decl=force_decl)
        return out

    def lower_comp(self, node, kind):
        """Lower a comprehension/genexpr to a GCC statement-expression loop.
        kind is 'list' (list/set/genexpr) or 'dict'."""
        saved = dict(self.scope)
        self.loop_n += 1
        acc = "_acc%d" % self.loop_n
        init = "dict_new()" if kind == "dict" else "list_new()"
        gens = node.generators

        def build(i):
            if i == len(gens):
                if kind == "dict":
                    return "dict_set(%s, %s, %s);" % (
                        acc, self.wrap_obj(node.key), self.wrap_obj(node.value))
                if kind == "set":     # de-duplicate as we go
                    return "set_add(%s, %s);" % (acc, self.wrap_obj(node.elt))
                return "list_append(%s, %s);" % (acc, self.wrap_obj(node.elt))
            g = gens[i]
            self.loop_n += 1
            it = "_it%d" % self.loop_n
            idx = "_k%d" % self.loop_n
            binds = self.bind_target(g.target, "index_obj(%s, %s)" % (it, idx),
                                     force_decl=True)
            guard_open, guard_close = "", ""
            if g.ifs:
                conds = " && ".join("(%s)" % self.bool_expr(c) for c in g.ifs)
                guard_open, guard_close = "if (%s) { " % conds, " }"
            inner = build(i + 1)
            body = " ".join(binds) + " " + guard_open + inner + guard_close
            return "{ obj %s = %s; for (long %s = 0; %s < pylen(%s); %s++) " \
                   "{ %s } }" % (it, self.wrap_obj(g.iter), idx, idx, it, idx,
                                 body)

        body = build(0)
        self.scope = saved
        if kind == "set":
            return "({ obj %s = %s; %s %s.tag = T_SET; %s; })" % (
                acc, init, body, acc, acc)
        return "({ obj %s = %s; %s %s; })" % (acc, init, body, acc)

    def st_Try(self, node):
        self.exc_n += 1
        fr = "_ef%d" % self.exc_n
        st = "_es%d" % self.exc_n
        hd = "_eh%d" % self.exc_n
        ev = "_ev%d" % self.exc_n

        def fin():
            return self.suite(node.finalbody) if node.finalbody else []

        lines = ["{ int %s = g_exc_sp++;" % fr]
        lines.append("  int %s = setjmp(g_exc_jmp[%s]);" % (st, fr))
        lines.append("  if (%s == 0) {" % st)
        # --- normal path: run the body with this try on the cleanup stack ---
        self.try_stack.append({"kind": "try", "fr": fr,
                               "finally": node.finalbody})
        body_lines = self.suite(node.body)
        self.try_stack.pop()
        lines += self.indent_lines(self.indent_lines(body_lines))
        lines.append("    g_exc_sp = %s;" % fr)
        lines += self.indent_lines(self.indent_lines(fin()))
        # --- exception path: dispatch handlers, run finally, re-raise if unhandled
        lines.append("  } else {")
        lines.append("    g_exc_sp = %s;" % fr)
        # Snapshot the in-flight exception. `g_exc_val` is one global slot, so
        # anything the handler runs that raises and catches internally
        # overwrites it -- and the re-raise below would then propagate that
        # unrelated value. That is not hypothetical: a CrustError unwinding
        # through here was replaced by the bare string from a
        # `raise ValueError("empty")` two frames down, so every outer
        # `except CrustError` declined it and the compiler printed the raw
        # source instead of a diagnostic.
        lines.append("    obj %s = g_exc_val;" % ev)
        lines.append("    int %s = 0;" % hd)
        chain = []
        for i, h in enumerate(node.handlers):
            cond = self._exc_match_cond(h.type, ev)
            binds = []
            if h.name:
                if h.name not in self.scope:
                    self.scope[h.name] = OBJ
                binds.append("obj %s = %s;" % (cname(h.name), ev))
            hbody = binds + self.suite(h.body) + ["%s = 1;" % hd]
            if cond is None:
                opener = "{" if i == 0 else "else {"
                chain.append(opener)
                chain += self.indent_lines(hbody)
                chain.append("}")
                break
            kw = "if" if i == 0 else "else if"
            chain.append("%s (%s) {" % (kw, cond))
            chain += self.indent_lines(hbody)
            chain.append("}")
        lines += self.indent_lines(self.indent_lines(chain))
        lines += self.indent_lines(self.indent_lines(fin()))
        lines.append("    if (!%s) rt_raise(%s);" % (hd, ev))
        lines.append("  }")
        lines.append("}")
        return lines

    def _is_contextmanager(self, fn):
        for d in getattr(fn, "decorator_list", []):
            nm = d.attr if isinstance(d, ast.Attribute) else getattr(d, "id", None)
            if nm == "contextmanager":
                return True
        return False

    @staticmethod
    def _stmt_is_yield(s):
        return isinstance(s, ast.Expr) and isinstance(s.value,
                                                      (ast.Yield, ast.YieldFrom))

    @staticmethod
    def _is_docstring(s):
        return isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) \
            and isinstance(s.value.value, str)

    def _inline_ctxmgr(self, fn, with_body, defining_mod=None):
        """Inline a single-`yield` @contextmanager generator: the code before
        the yield runs first, the with-body replaces the yield, and the
        surrounding try/except/finally (if any) wraps the with-body. The
        generator's own locals are renamed per site to avoid collisions, and
        its module globals are rewritten so they resolve in the using module."""
        import copy
        self.cm_n += 1
        # names the generator declares `global` are NOT locals -- they refer to
        # the *defining* module's globals.
        gnames = set()
        for s in ast.walk(fn):
            if isinstance(s, ast.Global):
                gnames.update(s.names)
        locals_ = {a.arg for a in fn.args.args}
        for s in ast.walk(fn):
            if isinstance(s, ast.Name) and isinstance(s.ctx, ast.Store) \
                    and s.id not in gnames:
                locals_.add(s.id)
            if isinstance(s, ast.ExceptHandler) and s.name and s.name not in gnames:
                locals_.add(s.name)
        rename = {n: "_cm%d_%s" % (self.cm_n, n) for n in locals_}
        # how the *current* module reaches the generator's module globals
        alias = None
        if defining_mod and defining_mod != getattr(self, "modname", None):
            alias = next((a for a, m in self.import_alias.items()
                          if m == defining_mod), None)
            if alias is None:
                for g in gnames:
                    self.from_imports.setdefault(g, defining_mod)
            # Free names the generator uses from its own module (imported
            # singletons/globals/funcs, e.g. report_err's error_collector) must
            # resolve -- and get an extern -- in the using module too.
            reg = self.load_xmod(defining_mod) or {}
            mimp = reg.get("imports", {})
            mown = (set(reg.get("singletons", {})) | set(reg.get("globals", {}))
                    | set(reg.get("funcs", {})) | set(reg.get("classes", {})))
            for s in ast.walk(fn):
                if isinstance(s, ast.Name) and isinstance(s.ctx, ast.Load):
                    n = s.id
                    if n in rename or n in gnames:
                        continue
                    if n in mimp:
                        self.from_imports.setdefault(n, mimp[n])
                    elif n in mown:
                        self.from_imports.setdefault(n, defining_mod)

        class R(ast.NodeTransformer):
            def visit_Name(self, n):
                if n.id in rename:
                    n.id = rename[n.id]
                elif alias and n.id in gnames:
                    a = ast.Attribute(value=ast.Name(id=alias, ctx=ast.Load()),
                                      attr=n.id, ctx=n.ctx)
                    return ast.copy_location(a, n)
                return n

            def visit_ExceptHandler(self, h):
                self.generic_visit(h)
                if h.name in rename:
                    h.name = rename[h.name]
                return h

        out = []
        for s in fn.body:
            if isinstance(s, ast.Global) or self._is_docstring(s):
                continue
            if self._stmt_is_yield(s):
                out.extend(copy.deepcopy(with_body))
            elif isinstance(s, ast.Try) and any(self._stmt_is_yield(b)
                                                for b in s.body):
                t = R().visit(copy.deepcopy(s))
                nb = []
                for b in t.body:
                    if self._stmt_is_yield(b):
                        nb.extend(copy.deepcopy(with_body))
                    else:
                        nb.append(b)
                t.body = nb
                out.append(t)
            else:
                out.append(R().visit(copy.deepcopy(s)))
        return out

    def _resolve_ctxmgr_fn(self, name):
        """Find a no-arg @contextmanager function `name`, returning (fn, mod)
        where mod is the defining module (None if local). Handles imports
        (e.g. the parser's log_error, defined in shivyc.parser.utils)."""
        fn = self.func_nodes.get(name)
        mod = None
        if fn is None and name in self.from_imports:
            mod = self.from_imports[name]
            reg = self.load_xmod(mod)
            if reg:
                fn = reg.get("funcs", {}).get(name)
        if fn is not None and self._is_contextmanager(fn):
            return fn, mod
        return None, None

    def st_With(self, node):
        # Inline a no-arg @contextmanager call (e.g. the parser's `log_error`)
        # into the equivalent try/except/finally so backtracking works.
        if len(node.items) == 1 and node.items[0].optional_vars is None:
            ce = node.items[0].context_expr
            if isinstance(ce, ast.Call) and isinstance(ce.func, ast.Name) \
                    and not ce.args and not ce.keywords:
                fn, mod = self._resolve_ctxmgr_fn(ce.func.id)
                if fn is not None:
                    return self.suite(self._inline_ctxmgr(fn, node.body, mod))
        items = ", ".join(self.src1(i.context_expr) for i in node.items)
        lines = ["/* with %s */ {" % items]
        binds = []
        file_closes = []
        for it in node.items:
            tv = it.optional_vars
            if isinstance(tv, ast.Name):
                ct = self.value_ctype(it.context_expr) or OBJ
                rhs = self.expr(it.context_expr)   # before tv enters scope
                self.scope[tv.id] = ct             # body sees the real type
                binds.append("%s %s = %s;" % (
                    self.ctype_csym(ct), self.lid(tv.id), rhs))
                # `with open(...) as f:` must flush+close the file on block exit
                # (a libc FILE* otherwise buffers until process exit, so a
                # subprocess reading the file mid-run sees it empty/partial).
                if ct == "FILE*":
                    self._io_used.add("fclose")
                    file_closes.append("if (%s) fclose(%s);" % (
                        self.lid(tv.id), self.lid(tv.id)))
            else:
                binds.append("%s;" % self.expr(it.context_expr))
        lines += self.indent_lines(binds)
        lines += self.indent_lines(self.suite(node.body))
        lines += self.indent_lines(file_closes)
        lines.append("}")
        return lines

    def st_FunctionDef(self, node):
        return ["/* nested function %s not lifted */" % node.name]

    def suite(self, body):
        out = []
        for s in body:
            out += self.stmt(s)
        return out

    def indent_lines(self, lines):
        return ["    " + ln for ln in lines]

    # ---- expressions -----------------------------------------------------

    def expr(self, node):
        # An expression that cannot be lowered becomes `OBJ_NONE`. That is
        # deliberate -- it keeps a build going when the failure is in code the
        # native compiler never runs -- but it *deletes* whatever the
        # expression did, and the only trace was a comment in the generated C.
        # A rejected `make_il(..., no_scope=True)` removed the call that emits
        # every function body, and the result was a compiler that built
        # cleanly and returned 0 from every program. Warn on both paths: the
        # cost of a stray line on stderr is nothing against that.
        m = getattr(self, "ex_" + type(node).__name__, None)
        if m is None:
            self._warn_unsupported(
                getattr(node, "lineno", 0),
                "expression of type %s" % type(node).__name__, "None",
                "the expression is dropped, not evaluated")
            return "/* %s: %s */ OBJ_NONE" % (type(node).__name__,
                                              self.src1(node))
        try:
            return m(node)
        except Unsupported:
            raise
        except Py2CUnsupported:
            raise
        except Exception as e:
            if self.stdlib_root:
                raise Unsupported(str(e)) from e
            self._warn_unsupported(
                getattr(node, "lineno", 0), "expression `%s`" % self.src1(node),
                "None", "lowering failed: %s" % e)
            return "/* expr-error %s */ OBJ_NONE" % e

    def ex_Name(self, node):
        if node.id == "__file__" and node.id not in self.scope:
            # No meaningful runtime path in the transpiled binary; emit the
            # module's source name so code that only inspects/joins it compiles.
            return "OBJ_STR(%s)" % c_string(getattr(self, "modname", "module")
                                            + ".py")
        if self.stdlib_root and node.id == "__name__" and node.id not in self.scope:
            return "OBJ_STR(%s)" % c_string(self.modname)
        if self.stdlib_root and node.id in EXCEPTION_NAMES and \
                node.id not in self.scope:
            return "mp_getattr(mp_call_import(\"builtins\", \"\", 0), %s, OBJ_NONE)" % (
                c_string(node.id))
        # a top-level function used as a *value* (not called) becomes a closure
        if node.id in self.func_nodes and node.id not in self.scope:
            self.func_values_needed.add(node.id)
            return "make_closure(&%s__tramp, OBJ_NONE)" % self.fnsym(node.id)
        # a class used as a *value* becomes a constructor closure
        if node.id in self.classes and node.id not in self.scope:
            self.class_values_needed.add(self.classes[node.id].csym)
            return "make_closure(&%s__ctortramp, OBJ_NONE)" % \
                self.classes[node.id].csym
        # an imported module global / singleton referenced by bare name: make
        # sure it gets an extern declaration emitted by build_externs
        if node.id in self.from_imports and node.id not in self.scope:
            mod = self.from_imports[node.id]
            kind, info = self.xref(node.id, mod)
            if kind == "const":
                # a module-level constant (e.g. an error-message string): inline
                if self.stdlib_root and isinstance(info, bool):
                    return "OBJ_BOOL(%s)" % ("true" if info else "false")
                if self.stdlib_root and isinstance(info, int):
                    return "OBJ_INT(%d)" % info
                if self.stdlib_root and isinstance(info, str):
                    return "OBJ_STR(%s)" % c_string(info)
                return self.const_literal(info)
            if kind in ("singleton", "func", "class"):
                self.used_imports.add((mod, node.id))
                if kind == "class":
                    self.class_values_needed.add(info.csym)
                    return "make_closure(&%s__ctortramp, OBJ_NONE)" % info.csym
                if kind == "func" and self.stdlib_root:
                    return "mp_call_import(%s, %s, 0)" % (
                        c_string(mod), c_string(node.id))
            if kind == "global" and self.stdlib_root:
                return "mp_call_import(%s, %s, 0)" % (
                    c_string(mod), c_string(node.id))
            if self.stdlib_root:
                return "mp_call_import(%s, %s, 0)" % (
                    c_string(mod), c_string(node.id))
        if self.stdlib_root and node.id in STDLIB_BUILTINS and \
                node.id not in self.scope:
            return "mp_call_import(\"builtins\", %s, 0)" % c_string(node.id)
        if self.stdlib_root and node.id in self.import_alias and \
                node.id not in self.scope:
            return "mp_call_import(%s, %s, 0)" % (
                c_string(self.import_alias[node.id]), c_string(""))
        if (node.id in self.modules or node.id in self.import_alias) and \
                node.id not in self.scope:
            # A module referenced as a first-class value (e.g. passed as an
            # argument: `self._rv_binop(cmd, math_cmds, size)`). A module has no
            # runtime representation in the generated C -- every `mod.Attr` use
            # is resolved statically at its own site -- so any parameter that
            # receives it is dead. Emit a placeholder instead of the undeclared
            # module identifier.
            return "OBJ_NONE"
        if node.id == "self" and node.id not in self.scope:
            return "self"
        if node.id in self.mod_global_names and node.id not in self.scope:
            return self._msym(node.id)
        if node.id not in self.scope and self.star_import_mods and \
                self.stdlib_root and node.id not in self.from_imports:
            mod = self.star_import_mods[-1]
            return "mp_call_import(%s, %s, 0)" % (c_string(mod), c_string(node.id))
        if self.stdlib_root and node.id not in self.scope:
            for mod in set(self.from_imports.values()) | \
                    set(self.import_alias.values()) | \
                    set(self.star_import_mods):
                reg = self.load_xmod(mod)
                if not reg:
                    continue
                if node.id in reg.get("globals", {}) or \
                        node.id in reg.get("consts", {}):
                    return "mp_call_import(%s, %s, 0)" % (
                        c_string(mod), c_string(node.id))
            for _cls, (_ci, mod) in self.xclasses.items():
                reg = self.load_xmod(mod)
                if reg and node.id in reg.get("globals", {}):
                    return "mp_call_import(%s, %s, 0)" % (
                        c_string(mod), c_string(node.id))
        if node.id in self.scope:
            return self.lid(node.id)
        return cname(node.id)

    def const_literal(self, v):
        """Render a Python constant (int/str/bool) as a C literal."""
        if v is True:
            return "1"
        if v is False:
            return "0"
        if isinstance(v, int):
            # -(2**63) can't be written as a literal (the 2**63 operand
            # overflows a signed long before negation); build it instead.
            if v == -9223372036854775808:
                return "(-9223372036854775807LL - 1)"
            # values above INT64_MAX need an unsigned-long-long literal.
            if v > 9223372036854775807:
                return "%dULL" % v
            return str(v)
        if isinstance(v, str):
            return c_string(v)
        return "OBJ_NONE"

    def ex_Constant(self, node):
        v = node.value
        if v is None:
            return "OBJ_NONE"
        if v is True:
            return "true"
        if v is False:
            return "false"
        if isinstance(v, str):
            return c_string(v)
        if isinstance(v, bytes):
            return c_string(v.decode("latin-1"))
        if isinstance(v, float):
            return repr(v)
        if isinstance(v, int):
            # ShivyCX's lexer parses a bare decimal literal as a signed long
            # long; values above LLONG_MAX need an explicit unsigned suffix so
            # they fit (e.g. ULONG_MAX = 18446744073709551615).
            if 9223372036854775807 < v <= 18446744073709551615:
                return "%dULL" % v
            return str(v)
        return str(v)

    def _ci_by_csym(self, csym):
        """Find the ClassInfo (local or imported) whose C symbol is exactly
        `csym`. The xclasses registry is keyed by *bare* name, which is
        ambiguous when two modules share a class name; the csym (then
        module-qualified) identifies the intended one precisely. The ambiguous
        "loser" (not the bare-keyed entry) lives only in the shadow maps."""
        for ci in self.classes.values():
            if ci.csym == csym:
                return ci
        for ci, _mod in self.xclasses.values():
            if ci.csym == csym:
                return ci
        for m in (self.xshadow_body, self.xshadow_td, self.xshadow_type):
            if csym in m:
                return m[csym]
        return None

    def _class_has_field(self, cls, field):
        """True if class `cls` (local or imported), including bases, declares
        `field` as a real struct member."""
        ci = self.classes.get(cls) or (self.xclasses[cls][0]
                                       if cls in self.xclasses else None) \
            or self._ci_by_csym(cls)
        if ci is None:
            return False
        try:
            return any(fn == field for fn, _ in ci.full_fields())
        except Exception:
            return any(fn == field for fn, _ in ci.own_fields)

    def _field_owner_subclass(self, base_cls, attr):
        """If `attr` is not declared on `base_cls` but is an own field of
        exactly one of its (transitive) subclasses, return that subclass name.
        Used to faithfully downcast a base pointer to the concrete type the
        Python code assumes (e.g. CType* whose `.signed` lives on IntegerCType).
        """
        pool = list(self.classes.values()) + \
            [ci for ci, _m in self.xclasses.values()]
        cands = set()
        for ci in pool:
            if ci.name == base_cls:
                continue
            c, is_sub = ci, False
            while c is not None:
                if c.name == base_cls:
                    is_sub = True
                    break
                c = c.base
            if is_sub and any(fn == attr for fn, _ in ci.own_fields):
                cands.add(ci.name)
        return next(iter(cands)) if len(cands) == 1 else None

    def _class_ptr_expr(self, node, cls):
        """Render `node` as a `cls*`. A Tier-2 obj (e.g. an isinstance-narrowed
        variable) must be unwrapped with AS_OBJ; a real pointer casts directly."""
        cls = self.ccls(cls)            # qualify ambiguous class names
        e = self.expr(node)
        if self.is_obj_word(node):
            return "(%s*)AS_OBJ(%s)" % (cls, e)
        return "(%s*)(%s)" % (cls, e)

    def _std_stream(self, node):
        """`sys.stdin/stdout/stderr` -> the libc stream name, else None."""
        if isinstance(node, ast.Attribute) and \
                isinstance(node.value, ast.Name) and node.value.id == "sys" \
                and node.attr in ("stdin", "stdout", "stderr"):
            return node.attr
        return None

    _SUBPROC_FIELDS = {"returncode": 0, "stdout": 1, "stderr": 2}

    def ex_Attribute(self, node):
        # A subprocess.run result is a LIST [returncode, stdout, stderr]; its
        # three fields are a plain index. Checked before _std_stream, which
        # would otherwise claim `r.stdout` as the process-wide stream.
        if isinstance(node.value, ast.Name) \
                and node.value.id in self._subproc_vars \
                and node.attr in self._SUBPROC_FIELDS:
            return "index_obj(%s, %d)" % (self.expr(node.value),
                                          self._SUBPROC_FIELDS[node.attr])
        stream = self._std_stream(node)
        if stream is not None:
            self._io_used.add(stream)
            return stream
        if isinstance(node.value, ast.Name) and node.value.id == "sys":
            if node.attr == "path":
                self._ossys_used = True
                return "_sys_path_get()"
            if node.attr == "executable":
                return 'OBJ_STR("python3")'
        if self._is_sys_impl_name(node):
            return 'OBJ_STR("%s")' % self.IMPL_NAME
        if isinstance(node.value, ast.Name) and node.value.id == "socket" \
                and node.attr in _SOCK_CONSTS:
            return _SOCK_CONSTS[node.attr]
        if node.attr == "__name__" and isinstance(node.value, ast.Attribute) \
                and node.value.attr == "__class__":
            return "TYPE(%s)->name" % self.obj_ptr(node.value.value)
        # type(x).__name__  ->  TYPE(x)->name
        if node.attr == "__name__" and isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Name) \
                and node.value.func.id == "type" and node.value.args:
            return "TYPE(%s)->name" % self.obj_ptr(node.value.args[0])
        # type(self).<class-attr>  ->  the attribute's class-level *default*
        # (used to reset an instance field to the original, e.g. restoring the
        # full register list). Resolved by following sibling-name defaults.
        if isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Name) \
                and node.value.func.id == "type" and len(node.value.args) == 1 \
                and isinstance(node.value.args[0], ast.Name) \
                and node.value.args[0].id == "self" and self.cur_class:
            dflt = self._resolve_class_default(self.cur_class, node.attr)
            if dflt is not None:
                return self.wrap_obj(dflt)
        if node.attr == "buffer" and self.stdlib_root and \
                (self.is_obj_word(node.value) or
                 self.value_ctype(node.value) == OBJ):
            return "mp_getattr(%s, %s, OBJ_NONE)" % (
                self.wrap_obj(node.value), c_string(node.attr))
        if self.stdlib_root and isinstance(node.value, ast.Attribute):
            bt = self.value_ctype(node.value)
            if not (bt and bt.endswith("*") and bt != OBJ and
                    self._class_has_field(bt[:-1], node.attr)):
                return "mp_getattr(%s, %s, OBJ_NONE)" % (
                    self.wrap_obj(node.value), c_string(node.attr))
        if isinstance(node.value, ast.Call) and self.stdlib_root:
            return "mp_getattr(%s, %s, OBJ_NONE)" % (
                self.wrap_obj(node.value), c_string(node.attr))
        if isinstance(node.value, ast.Name):
            base = node.value.id
            # `ClassName.CONST` where CONST is a class-level scalar constant
            # (e.g. `ParserError.AFTER`): resolve to the literal rather than
            # instantiating the class and reading a struct member. Works for a
            # local class, a from-imported class, or a module-aliased one.
            if base not in self.scope:
                _cci = self.classes.get(base)
                if _cci is None and base in self.from_imports:
                    _ck, _ci = self.xref(base, self.from_imports[base])
                    _cci = _ci if _ck == "class" else None
                if _cci is None and base in self.import_alias:
                    _ck, _ci = self.xref(node.attr, self.import_alias[base])
                    # handled by the class-as-value path below; skip here
                    _cci = None
                if _cci is not None:
                    _ca = getattr(_cci, "class_attrs", {})
                    _cv = _ca.get(node.attr)
                    if isinstance(_cv, ast.Constant) and \
                            isinstance(_cv.value, (int, bool, str, float)):
                        return self.wrap_obj(_cv)
            if base in self.import_alias:
                modname = self.import_alias[base]
                consts = self.load_xmod(modname).get("consts", {})
                if node.attr in consts:
                    return self.const_literal(consts[node.attr])
                kind, info = self.xref(node.attr, modname)
                if kind == "func" and self.stdlib_root:
                    return "mp_call_import(%s, %s, 0)" % (
                        c_string(modname), c_string(node.attr))
                if kind == "global" and self.stdlib_root:
                    return "mp_call_import(%s, %s, 0)" % (
                        c_string(modname), c_string(node.attr))
                if kind in ("singleton", "func", "global"):
                    return cname(node.attr)      # bare exported symbol
                if kind == "class":
                    self.class_values_needed.add(info.csym)
                    return "make_closure(&%s__ctortramp, OBJ_NONE)" % info.csym
                if base in self.modules:
                    mod = self.import_alias.get(base, base)
                    if self.stdlib_root:
                        return "mp_getattr(mp_call_import(%s, %s, 0), %s, OBJ_NONE)" % (
                            c_string(mod), c_string(""), c_string(node.attr))
                    return "%s_%s" % (base, node.attr)
            if base in self.modules:
                mod = self.import_alias.get(base, base)
                if self.stdlib_root:
                    return "mp_getattr(mp_call_import(%s, %s, 0), %s, OBJ_NONE)" % (
                        c_string(mod), c_string(""), c_string(node.attr))
                return "%s_%s" % (base, node.attr)
            if base == "self":
                if self.cur_class:
                    owner = self.static_owner(self.cur_class, node.attr)
                    if owner:
                        return "%s_%s" % (owner.csym, cname(node.attr))
                    if node.attr in self.cur_class.methods:
                        ff = [f for f, _ in self.cur_class.full_fields()]
                        if node.attr not in ff:
                            if node.attr in self.cur_class.property_methods or \
                                    node.attr not in VTABLE_METHODS:
                                return "%s_%s(self)" % (self.cur_class.csym,
                                                        method_cname(node.attr))
                            if self.stdlib_root:
                                return "mp_getattr(%s, %s, OBJ_NONE)" % (
                                    "OBJ_OBJ(self)", c_string(node.attr))
                            return self.vcall(ast.Name(id="self", ctx=node.ctx),
                                              node.attr, [])
                    return "self->%s" % cname(node.attr)
                # cur_class is None (e.g. a lifted nested function): `self` is a
                # typed param, so fall through to the concrete-pointer path.
            # Class.STATIC (local or imported)
            ci = self.classes.get(base) or (self.xclasses[base][0]
                                            if base in self.xclasses else None)
            if ci is not None:
                owner = self.static_owner(ci, node.attr)
                if owner:
                    return "%s_%s" % (owner.csym, cname(node.attr))
            # isinstance-narrowed variable: access the field through its proven
            # concrete class (only when that class really declares the field).
            if base in self.narrowed:
                cls = self.narrowed[base][:-1]
                if self._class_has_field(cls, node.attr):
                    if cls in self.xclasses and cls not in self.classes:
                        self.xstructs_needed.add(cls)
                    return "(%s)->%s" % (self._class_ptr_expr(node.value, cls),
                                         cname(node.attr))
        # access through a concrete class pointer:  p.attr -> (p)->attr
        bt = self.value_ctype(node.value)
        if bt and bt.endswith("*") and bt != OBJ:
            cls = bt[:-1]
            sci = self.classes.get(cls) or (self.xclasses[cls][0]
                                            if cls in self.xclasses else None)
            if self.stdlib_root and sci is not None and \
                    cls in self.xclasses and cls not in self.classes and \
                    not self._class_has_field(cls, node.attr) and \
                    node.attr not in getattr(sci, "static_methods", set()):
                return "mp_getattr(%s, %s, OBJ_NONE)" % (
                    self.wrap_obj(node.value), c_string(node.attr))
            if sci is not None:
                owner = self.static_owner(sci, node.attr)
                if owner:                       # class static via a typed ptr
                    return "%s_%s" % (owner.csym, cname(node.attr))
                if node.attr in sci.methods:
                    ff = [f for f, _ in sci.full_fields()]
                    if node.attr not in ff:
                        recv = self.expr(node.value)
                        if node.attr in sci.property_methods or \
                                node.attr not in VTABLE_METHODS:
                            return "%s_%s(%s)" % (sci.csym, node.attr, recv)
                        return self.vcall(node.value, node.attr, [])
            if cls not in self.xclasses and cls not in self.classes:
                self._load_xclass_anywhere(cls)
            if cls in self.xclasses:
                self.xstructs_needed.add(cls)
            if sci is not None and not self._class_has_field(cls, node.attr):
                sub = self._field_owner_subclass(cls, node.attr)
                if sub:
                    if sub in self.xclasses and sub not in self.classes:
                        self.xstructs_needed.add(sub)
                    return "((%s*)(%s))->%s" % (
                        self.ccls(sub), self.expr(node.value), cname(node.attr))
            return "(%s)->%s" % (self.expr(node.value), cname(node.attr))
        # reading an attribute off a Tier-2 obj: resolve the element's class by
        # which class declares this attribute, then offset into its struct.
        if self.is_obj_word(node.value) or self.value_ctype(node.value) == OBJ:
            owner = self.resolve_attr_owner(node.attr)
            if owner:
                if owner.name in self.xclasses and \
                        owner.name not in self.classes:
                    self.xstructs_needed.add(owner.name)
                self._ref_xclass(owner, body=True)
                return "((%s*)AS_OBJ(%s))->%s" % (
                    owner.csym, self.expr(node.value), cname(node.attr))
            # No statically-resolvable owner: do a real runtime attribute
            # lookup. (Previously this discarded the access as OBJ_NONE, which
            # silently produced wrong values; mp_getattr is declared in the
            # runtime header and defined by the stdlib bridge.)
            return "mp_getattr(%s, %s, OBJ_NONE)" % (
                self.wrap_obj(node.value), c_string(node.attr))
        # Receiver type is unknown or a stale scalar (e.g. the ambiguous field
        # `self.count`, value_ctyped int in some classes but emitted obj here):
        # a raw `x.attr` is only valid C when x is a real struct-by-value class.
        # Otherwise fall back to a runtime attribute lookup on the actual expr,
        # which is always valid (the emitted receiver is an obj at runtime).
        if bt in self.classes or bt in self.xclasses:
            return "%s.%s" % (self.expr(node.value), cname(node.attr))
        return "mp_getattr(%s, %s, OBJ_NONE)" % (
            self.expr(node.value), c_string(node.attr))

    def ex_Call(self, node):
        s = self._ex_call_inner(node)
        # virtual-return typing: an instance method's annotated leaf-class
        # return is emitted with an obj ABI (see _c_ret); recover the typed
        # pointer here so chained member/method access on the result resolves.
        # Module/alias/class-qualified calls and constructors return real
        # pointers already and are skipped.
        f = node.func
        if isinstance(f, ast.Attribute) and not self.ctor_class(node):
            fv = f.value
            qualified = isinstance(fv, ast.Name) and (
                fv.id in self.import_alias or fv.id in self.modules
                or fv.id in self.classes or fv.id in self.xclasses)
            if not qualified:
                rt = self.value_ctype(node)
                if self._is_class_ptr(rt):
                    cls = rt[:-1]
                    if cls in self.xclasses and cls not in self.classes:
                        self.xstructs_needed.add(cls)
                    return "(%s)AS_OBJ(%s)" % (rt, s)
        return s

    def _lower_vararg_local_call(self, fn, fndef, node):
        """Lower a call to a module-local function with *args / **kwargs."""
        n_reg = len(fndef.args.args)
        pos = node.args[:n_reg]
        var = node.args[n_reg:]
        kw = self._lower_call_kwargs(node)
        defs = self.defaults_for(fndef, False)
        creg = self.coerce_args(self.func_params[fn][:n_reg], pos, defs)
        wvar = [self.wrap_obj(a) for a in var]
        parts = list(creg)
        # The kwargs dict only if the callee actually declares `**kwargs`:
        # `param_list` emits that slot under the same condition, so passing it
        # unconditionally handed `def probe(*texts)` -- signature
        # `(int _n_texts, ...)` -- a leading `dict_new()` that the count then
        # sat behind, and gcc rejected the call.
        if fndef.args.kwarg:
            parts.append(kw)
        parts += [str(len(wvar))] + wvar
        return "%s(%s)" % (self.fnsym(fn), ", ".join(parts))

    def _lower_starred_local_call(self, fn, fndef, node):
        """Expand `f(*seq, ...)` when `f` is a module-local function."""
        if not node.args or not isinstance(node.args[0], ast.Starred):
            return None
        star_val = node.args[0].value
        rest = node.args[1:]
        nparams = len(fndef.args.args)
        nkw = len([k for k in node.keywords if k.arg])
        npos = max(0, nparams - len(rest) - nkw)
        if npos < 0:
            return None
        star_expr = self.expr(star_val)
        unpacked = ["subscript(%s, OBJ_INT(%d))" % (star_expr, i)
                    for i in range(npos)]
        merged = list(unpacked) + list(rest)
        if node.keywords:
            merged = self._merge_keyword_args(fndef, merged, node.keywords)
        defs = self.defaults_for(fndef, False)
        cargs = self.coerce_args(self.func_params[fn], merged, defs)
        return "%s(%s)" % (self.fnsym(fn), ", ".join(cargs))

    def _sort_with_key(self, func, node):
        """Lower ``LIST.sort(key=lambda P: BODY[, reverse=R])`` to an inline
        decorate-sort-undecorate.

        The lambda's single parameter is typed from the list's element type so
        attribute access in BODY resolves to real struct fields; each element's
        key is computed once, boxed to obj, and compared with obj_cmp (which
        handles int and str keys). Emitted as a GCC statement-expression that
        evaluates to OBJ_NONE (sort is in-place). Returns None for unsupported
        key forms, so the caller falls back to a natural list_sort.
        """
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        key = kw.get("key")
        if not isinstance(key, ast.Lambda) or len(key.args.args) != 1:
            return None
        param = key.args.args[0].arg
        etype = self.iter_elem_ctype(func.value) or OBJ
        # Bind the lambda parameter in scope while emitting the key body, so
        # `param.attr` resolves against the element type.
        had = param in self.scope
        saved = self.scope.get(param)
        self.scope[param] = etype
        try:
            keyobj = self.wrap_obj(key.body)
        finally:
            if had:
                self.scope[param] = saved
            else:
                self.scope.pop(param, None)
        if etype != OBJ and etype.endswith("*"):
            bind = "%s %s = (%s)AS_OBJ(_e);" % (etype, param, etype)
        else:
            bind = "obj %s = _e; (void)%s;" % (param, param)
        rev = kw.get("reverse")
        cmp_sign = "<" if (isinstance(rev, ast.Constant) and
                           rev.value is True) else ">"
        lst = self.expr(func.value)
        return (
            "({ obj _lst = %s; long _n = pylen(_lst); "
            "obj* _ks = aalloc((size_t)_n*sizeof(obj)); "
            "obj* _es = aalloc((size_t)_n*sizeof(obj)); "
            "for (long _i=0;_i<_n;_i++){ obj _e=index_obj(_lst,_i); _es[_i]=_e; "
            "%s _ks[_i]=%s; } "
            "for (long _i=1;_i<_n;_i++){ obj _k=_ks[_i],_ev=_es[_i]; "
            "long _j=_i-1; while(_j>=0 && obj_cmp(_ks[_j],_k) %s 0){ "
            "_ks[_j+1]=_ks[_j]; _es[_j+1]=_es[_j]; _j--; } "
            "_ks[_j+1]=_k; _es[_j+1]=_ev; } "
            "for (long _i=0;_i<_n;_i++) list_set(_lst,_i,_es[_i]); OBJ_NONE; })"
            % (lst, bind, keyobj, cmp_sign))

    def _coerce_str_arg(self, node, i):
        # char* coercion of the i-th call argument (replaces a local lambda so
        # the source parses under rast).
        return self.coerce_to("char*", node.args[i], self.expr(node.args[i]))

    def _io_call(self, node):
        """Lower simple I/O to C stdio (all handles are opaque void*):
            open(path, mode)        -> fopen
            f.write(s)              -> fputs
            f.read()/f.readline()   -> fgets into a fresh buffer (line)
            f.close()               -> fclose
            input()                 -> read a line from stdin (newline stripped)
            os.system(cmd)          -> system
        """
        f = node.func
        if isinstance(f, ast.Attribute):
            recv = f.value
            if isinstance(recv, ast.Name) and recv.id == "os" \
                    and f.attr == "system" and node.args:
                self._io_used.add("system")
                return "system(%s)" % self.as_str(node.args[0])
            # `subprocess.check_call([...])` / `call` over a *literal*
            # argv list becomes a shell command. This exists so a transpiled
            # module can shell out to a tool it cannot import -- the
            # self-hosted compiler invoking `py2c.py` for an
            # `#include "x.py"`, rather than needing py2c compiled into it.
            #
            # `run` is deliberately excluded: it returns a CompletedProcess
            # whose `.stdout` and `.returncode` callers read, so lowering it to
            # `system()` and typing it `int` broke `thread_contracts`, which
            # does exactly that.
            #
            # Only a list literal is accepted, and only of literals or names:
            # building the command line from a runtime list would need
            # quoting that this cannot do safely, and a silently mis-quoted
            # shell command is a worse failure than a compile error.
            if isinstance(recv, ast.Name) and recv.id == "subprocess" \
                    and f.attr in ("check_call", "call") and node.args:
                argv = node.args[0]
                if isinstance(argv, ast.List) and argv.elts:
                    self._io_used.add("system")
                    # Space-joined into one shell command. Only a list literal
                    # is accepted: building argv from a runtime list would need
                    # quoting this cannot do safely, and a silently mis-quoted
                    # shell command is a worse failure than a compile error.
                    # `pyconcat` takes and returns `str` (a `char*`), so no
                    # boxing: the joins stay at C-string level throughout.
                    cmd = self.as_str(argv.elts[0])
                    for el in argv.elts[1:]:
                        cmd = 'pyconcat(pyconcat(%s, " "), %s)' % (
                            cmd, self.as_str(el))
                    return "system(%s)" % cmd
            # os.path.<fn>(...) -> string / libc shims (see OS_SYS_PRELUDE)
            if isinstance(recv, ast.Attribute) and \
                    isinstance(recv.value, ast.Name) and \
                    recv.value.id == "os" and recv.attr == "path" and node.args:
                if f.attr == "dirname":
                    self._ossys_used = True
                    return "_ospath_dirname(%s)" % self._coerce_str_arg(node, 0)
                if f.attr == "basename":
                    self._ossys_used = True
                    return "_ospath_basename(%s)" % self._coerce_str_arg(node, 0)
                if f.attr == "abspath":
                    self._ossys_used = True
                    return "_ospath_abspath(%s)" % self._coerce_str_arg(node, 0)
                if f.attr == "normpath":
                    self._ossys_used = True
                    return "_ospath_normpath(%s)" % self._coerce_str_arg(node, 0)
                if f.attr == "realpath":
                    self._ossys_used = True
                    return "_ospath_realpath(%s)" % self._coerce_str_arg(node, 0)
                if f.attr == "exists":
                    self._ossys_used = True
                    return "_ospath_exists(%s)" % self._coerce_str_arg(node, 0)
                if f.attr in ("isfile", "isdir"):
                    self._ossys_used = True
                    return "_ospath_%s(%s)" % (f.attr,
                                               self._coerce_str_arg(node, 0))
                if f.attr == "splitext":
                    self._ossys_used = True
                    return ("_ospath_splitext(%s)"
                            % self._coerce_str_arg(node, 0))
                if f.attr == "getmtime":
                    # Unsupported before this: the emitted call treated
                    # `os.path` as a receiver object that does not exist, so
                    # the module failed with "'os_path' undeclared".
                    self._ossys_used = True
                    return ("_ospath_getmtime(%s)"
                            % self._coerce_str_arg(node, 0))
                if f.attr == "join" and len(node.args) >= 2:
                    self._ossys_used = True
                    e = "_ospath_join(%s, %s)" % (self._coerce_str_arg(node, 0), self._coerce_str_arg(node, 1))
                    for i in range(2, len(node.args)):
                        e = "_ospath_join(%s, %s)" % (e, self._coerce_str_arg(node, i))
                    return e
            # os.environ.get(key[, default]) -> getenv, boxed as an obj
            # (there is no live environ dict in the C runtime). Both self-host
            # call sites pass a string key; a missing var yields the default
            # (OBJ_NONE when omitted), matching dict.get semantics.
            if isinstance(recv, ast.Attribute) and \
                    isinstance(recv.value, ast.Name) and \
                    recv.value.id == "os" and recv.attr == "environ" and \
                    f.attr == "get" and node.args:
                key = self._coerce_str_arg(node, 0)
                dflt = (self.wrap_obj(node.args[1]) if len(node.args) > 1
                        else "OBJ_NONE")
                return ("({ char* _ge = getenv(%s); "
                        "_ge ? OBJ_STR(_ge) : %s; })" % (key, dflt))
            # `os.getcwd()` takes no arguments, so it has to be matched
            # before the `node.args` guards below -- without this it fell
            # through to the generic unsupported path, which substitutes
            # `OBJ_NONE` and leaves a comment in the C. Passing that None to
            # `os.path.join` segfaulted the self-hosted compiler.
            if isinstance(recv, ast.Name) and recv.id == "os" \
                    and f.attr == "getcwd" and not node.args:
                self._ossys_used = True
                return "_os_getcwd()"
            # a few os.* filesystem ops
            if isinstance(recv, ast.Name) and recv.id == "os" and node.args:
                if f.attr == "makedirs":
                    self._ossys_used = True
                    return "_os_makedirs(%s)" % self.coerce_to(
                        "char*", node.args[0], self.expr(node.args[0]))
                if f.attr in ("unlink", "remove"):
                    self._ossys_used = True
                    return "_os_unlink(%s)" % self.coerce_to(
                        "char*", node.args[0], self.expr(node.args[0]))
            if self.value_ctype(recv) == "FILE*":
                fe = self.expr(recv)
                if f.attr == "write" and node.args:
                    if len(node.args) >= 2:          # write(buf, n) -> fwrite
                        self._io_used.add("fwrite")
                        return "fwrite(%s, 1, %s, %s)" % (
                            self.expr(node.args[0]),
                            self.as_long(node.args[1]), fe)
                    self._io_used.add("fputs")
                    return "fputs(%s, %s)" % (self.as_str(node.args[0]), fe)
                if f.attr == "close":
                    self._io_used.add("fclose")
                    return "fclose(%s)" % fe
                if f.attr == "readline":
                    self._io_used.update(("malloc", "fgets"))
                    return ("({ char* _b = malloc(4096); "
                            "if (!fgets(_b, 4096, %s)) _b[0] = 0; _b; })" % fe)
                if f.attr == "read":
                    # Read the ENTIRE stream (not a single line). A growing
                    # fread loop works on non-seekable streams too, and unlike
                    # fgets it does not stop at the first newline -- the latter
                    # silently truncated multi-line files to their first line.
                    self._io_used.update(("malloc", "realloc", "fread"))
                    return ("({ FILE* _f = (FILE*)(%s); "
                            "size_t _cap = 65536, _len = 0; "
                            "char* _b = (char*)malloc(_cap); "
                            "if (_f) { size_t _r; "
                            "while ((_r = fread(_b + _len, 1, _cap - _len, _f)) "
                            "> 0) { _len += _r; if (_len == _cap) { "
                            "_cap *= 2; _b = (char*)realloc(_b, _cap); } } } "
                            "_b[_len] = 0; _b; })" % fe)
        if isinstance(f, ast.Name):
            if f.id == "open" and node.args:
                self._io_used.add("fopen")
                mode = self.as_str(node.args[1]) if len(node.args) > 1 \
                    else "\"r\""
                return "fopen(%s, %s)" % (self.as_str(node.args[0]), mode)
            if f.id == "input":
                self._io_used.update(("malloc", "fgets", "stdin"))
                return ("({ char* _b = malloc(4096); "
                        "if (!fgets(_b, 4096, stdin)) _b[0] = 0; "
                        "long _n = 0; while (_b[_n]) _n++; "
                        "if (_n && _b[_n-1] == '\\n') _b[_n-1] = 0; _b; })")
        return None

    def _addr_pair(self, node):
        """A Python (host, port) address tuple -> (host_expr, port_expr)."""
        if isinstance(node, ast.Tuple) and len(node.elts) == 2:
            return self.as_str(node.elts[0]), self.as_long(node.elts[1])
        return self.as_str(node), "0"

    def _sock_call(self, node):
        """Lower socket / fork operations to BSD sockets C (fds are int):
            socket.socket(af, ty)   -> socket(af, ty, 0)
            s.bind((host, port))    -> __py_sock_bind     s.listen(n) -> listen
            s.connect((host, port)) -> __py_sock_connect   s.accept()  -> accept
            s.send(data[, n])       -> send                s.recv(b,n) -> recv
            s.setsockopt(l, o, v)   -> setsockopt          s.close()   -> close
            os.fork() -> fork()     os._exit(n) -> _exit(n)
        """
        f = node.func
        if not isinstance(f, ast.Attribute):
            return None
        recv = f.value
        if isinstance(recv, ast.Name) and recv.id == "socket" \
                and f.attr == "socket":
            self._sock_used.add("socket")
            af = self.as_long(node.args[0]) if node.args else "2"
            ty = self.as_long(node.args[1]) if len(node.args) > 1 else "1"
            return "socket(%s, %s, 0)" % (af, ty)
        if isinstance(recv, ast.Name) and recv.id == "os":
            if f.attr == "fork":
                self._io_used.add("fork")
                return "fork()"
            if f.attr == "_exit":
                self._io_used.add("_exit")
                n = self.as_long(node.args[0]) if node.args else "0"
                return "_exit(%s)" % n
        if self.value_ctype(recv) != "sockfd":
            return None
        fd = self.expr(recv)
        a = f.attr
        if a == "connect":
            self._sock_used.add("connect")
            host, port = self._addr_pair(node.args[0])
            return "__py_sock_connect(%s, %s, %s)" % (fd, host, port)
        if a == "bind":
            self._sock_used.add("bind")
            host, port = self._addr_pair(node.args[0])
            return "__py_sock_bind(%s, %s, %s)" % (fd, host, port)
        if a == "listen":
            self._sock_used.add("listen")
            n = self.as_long(node.args[0]) if node.args else "1"
            return "listen(%s, %s)" % (fd, n)
        if a == "accept":
            self._sock_used.add("accept")
            return "accept(%s, 0, 0)" % fd
        if a == "send":
            self._sock_used.add("send")
            data = self.as_str(node.args[0])
            if len(node.args) > 1:
                ln = self.as_long(node.args[1])
            else:
                self._io_used.add("strlen")
                ln = "strlen(%s)" % data
            return "send(%s, %s, %s, 0)" % (fd, data, ln)
        if a == "recv":
            self._sock_used.add("recv")
            buf = self.expr(node.args[0])
            ln = self.as_long(node.args[1])
            return "recv(%s, %s, %s, 0)" % (fd, buf, ln)
        if a == "setsockopt":
            self._sock_used.add("setsockopt")
            return ("({ int _v = %s; setsockopt(%s, %s, %s, &_v, 4); })"
                    % (self.as_long(node.args[2]), fd,
                       self.as_long(node.args[0]), self.as_long(node.args[1])))
        if a == "close":
            self._sock_used.add("close")
            return "close(%s)" % fd
        return None

    def _ex_call_inner(self, node):
        sym = self.ctypes_call_symbol(node)
        if sym is not None:
            return self._emit_ctypes_call(node, sym)
        _jd = self._json_decode_call(node)
        if _jd is not None:
            return _jd
        f0 = node.func
        # Unchecked direct list indexing, opt-in via _lget/_lset. For call sites
        # where the index is provably valid and non-negative (e.g. the bytecode
        # interpreter's register/pc accesses), this skips subscript()'s container
        # dispatch and list_get()'s negative/bounds handling -- a raw array load.
        if isinstance(f0, ast.Name) and f0.id in ("_lget", "_lset") \
                and f0.id not in self.scope:
            if f0.id == "_lget" and len(node.args) == 2:
                return "(((List*)AS_OBJ(%s))->data[%s])" % (
                    self.expr(node.args[0]), self.as_long(node.args[1]))
            if f0.id == "_lset" and len(node.args) == 3:
                return "(((List*)AS_OBJ(%s))->data[%s] = %s)" % (
                    self.expr(node.args[0]), self.as_long(node.args[1]),
                    self.wrap_obj(node.args[2]))
        if isinstance(f0, ast.Attribute) and f0.attr in MATH_FUNCS and \
                isinstance(f0.value, ast.Name) and \
                f0.value.id in ("math", "np", "numpy"):
            return "%s(%s)" % (f0.attr,
                               ", ".join(self.coerce_to("double", a,
                                                        self.expr(a))
                                         for a in node.args))
        # bare `round(x)` / `floor(x)` / ... : C math funcs take double, so the
        # argument (often an obj from true division) must be coerced. value_ctype
        # already reports double for these, so the result wraps correctly in
        # context. Guarded so a local of the same name isn't hijacked.
        if isinstance(f0, ast.Name) and f0.id in MATH_FUNCS and \
                f0.id not in self.scope and f0.id not in self.classes:
            return "%s(%s)" % (f0.id,
                               ", ".join(self.coerce_to("double", a,
                                                        self.expr(a))
                                         for a in node.args))
        io = self._io_call(node)
        if io is not None:
            return io
        sk = self._sock_call(node)
        if sk is not None:
            return sk
        func = node.func
        argstrs = [self.expr(a) for a in node.args]

        if isinstance(func, ast.Lambda):
            clo = self.expr(func)
            wargs = [self.wrap_obj(a) for a in node.args]
            return self._emit_call_obj(clo, wargs)

        if isinstance(func, ast.Call) and isinstance(func.func, ast.Name) and \
                func.func.id == "type" and len(func.args) == 1:
            recv = func.args[0]
            rt = self.value_ctype(recv)
            if isinstance(recv, ast.Name) and recv.id == "self" and self.cur_class:
                ci = self.cur_class
            elif rt and rt.endswith("*") and rt != OBJ:
                cls = rt[:-1]
                ci = self.classes.get(cls) or (self.xclasses[cls][0]
                                               if cls in self.xclasses else None)
            else:
                ci = None
            if ci is not None:
                nargs = [self.wrap_obj(a) for a in node.args]
                if nargs:
                    return "OBJ_OBJ(%s_new(%s))" % (ci.csym, ", ".join(nargs))
                init = ci.methods.get("__init__")
                nparams = len(init.args.args) - 1 if init else 0
                if nparams or (init and init.args.kwarg):
                    cargs = self._pad_ctor_kwargs(init, [])
                    return "OBJ_OBJ(%s_new(%s))" % (ci.csym, ", ".join(cargs))
                return "OBJ_OBJ(%s_new())" % ci.csym

        if isinstance(func, ast.Call):
            inner = self.expr(func)
            args = [self.wrap_obj(a) for a in node.args]
            if not self.stdlib_root:        # rpython runtime: call_obj varargs
                return self._emit_call_obj(inner, args)
            if args:
                return "mp_call_obj(%s, list_of(%d, %s), dict_new())" % (
                    inner, len(args), ", ".join(args))
            return "mp_call_obj(%s, list_new(), dict_new())" % inner

        if isinstance(func, ast.Name):
            fn = func.id
            if fn == "__closure_env__":
                # closure-converted nested function used as a value: bundle the
                # captured values into an env list and build the closure.
                mangled = node.args[0].value
                self.closure_values_needed.add(mangled)
                caps = node.args[1:]
                env = self._list_literal(
                    [self.wrap_obj(c) for c in caps])
                return "make_closure(&%s__tramp, %s)" % (cname(mangled), env)
            if fn == "const" and node.args:
                return self.expr(node.args[0])
            if fn == "isinstance" and len(node.args) == 2:
                return self.lower_isinstance(node.args[0], node.args[1])
            if fn == "_float_to_bits" and len(node.args) == 2:
                size = node.args[1]
                sz = self.expr(size) if self.value_ctype(size) in \
                    ("int", "bool") else "AS_INT(%s)" % self.wrap_obj(size)
                return "float_to_bits(%s, %s)" % (
                    self.wrap_obj(node.args[0]), sz)
            if fn in ("str", "bytes"):
                if len(node.args) == 1:
                    return "pystr(%s)" % self.wrap_obj(node.args[0])
                if self.stdlib_root and node.args:
                    return self._mp_import_call("builtins", "str", node)
            if fn == "len" and len(node.args) == 1:
                if self._is_sys_argv(node.args[0]):
                    return "argc"
                if self._typed_list_ct(node.args[0]) is not None or \
                        self._typed_dict_ct(node.args[0]) is not None:
                    return "%s->len" % self.expr(node.args[0])
                if self.value_ctype(node.args[0]) == "char*":
                    self._io_used.add("strlen")
                    return "strlen(%s)" % self.expr(node.args[0])
                return "pylen(%s)" % self.wrap_obj(node.args[0])
            if fn == "print":
                if len(node.args) == 1 and \
                        self.value_ctype(node.args[0]) == "char*":
                    self._io_used.add("puts")
                    return "puts(%s)" % self.expr(node.args[0])
                if len(node.args) > 1:
                    # CPython prints every argument, space separated. Emitting
                    # only args[0] silently dropped the rest.
                    exprs = [self.wrap_obj(a) for a in node.args]
                    self._list_tmp = getattr(self, "_list_tmp", 0) + 1
                    v = "_pp%d" % self._list_tmp
                    stores = " ".join("%s[%d] = %s;" % (v, i, e)
                                      for i, e in enumerate(exprs))
                    return "({ obj %s[%d]; %s pyprint_a(%s, %d); })" % (
                        v, len(exprs), stores, v, len(exprs))
                if node.args:
                    return "pyprint(%s)" % self.wrap_obj(node.args[0])
                return 'pyprint(OBJ_STR(""))'
            if fn in ("any", "all") and len(node.args) == 1:
                return self.lower_any_all(fn, node.args[0])
            if fn == "bool" and len(node.args) == 1:
                # Python bool() yields exactly 0 or 1 as a *value* (not just a
                # truthy-in-context expression), so normalize.
                return "(%s ? 1 : 0)" % self.bool_expr(node.args[0])
            if fn == "range":
                a = [self.coerce_to("int", x, self.expr(x))
                     for x in node.args]
                if len(a) == 1:
                    return "pyrange(0, %s, 1)" % a[0]
                if len(a) == 2:
                    return "pyrange(%s, %s, 1)" % (a[0], a[1])
                if len(a) == 3:
                    return "pyrange(%s, %s, %s)" % (a[0], a[1], a[2])
            if fn == "zip" and len(node.args) == 2:
                return "pyzip(%s, %s)" % (self.wrap_obj(node.args[0]),
                                          self.wrap_obj(node.args[1]))
            if fn == "zip" and len(node.args) == 3:
                return "pyzip3(%s, %s, %s)" % tuple(
                    self.wrap_obj(a) for a in node.args)
            if fn == "zip" and len(node.args) == 4:
                return "pyzip4(%s, %s, %s, %s)" % tuple(
                    self.wrap_obj(a) for a in node.args)
            if fn == "enumerate" and node.args:
                start = self.expr(node.args[1]) if len(node.args) > 1 else "0"
                return "pyenumerate(%s, %s)" % (self.wrap_obj(node.args[0]),
                                                start)
            if fn == "sorted" and node.args:
                return "pysorted(%s)" % self.wrap_obj(node.args[0])
            if fn == "reversed" and node.args:
                return "pyreversed(%s)" % self.wrap_obj(node.args[0])
            if fn in ("max", "min"):
                # A single unboxed typed-list argument (`min(xs)` where xs is a
                # `list[float]`) cannot go through pymin/pymax: those iterate a
                # boxed list, but a typed list is a raw `_tlist_T` struct. Reduce
                # it with a direct C loop over ->data[], boxing the scalar result
                # so the value stays a Tier-2 obj (matching value_ctype).
                if len(node.args) == 1 and not node.keywords:
                    tlct = self._typed_list_ct(node.args[0])
                    if tlct:
                        return self._typed_list_minmax(fn, tlct, node.args[0])
                kw = {k.arg: k.value for k in node.keywords}
                if "default" in kw:
                    dflt, has = self.wrap_obj(kw["default"]), "true"
                else:
                    dflt, has = "OBJ_NONE", "false"
                if len(node.args) == 1:
                    it = self.wrap_obj(node.args[0])
                else:
                    it = self._list_literal(
                        [self.wrap_obj(x) for x in node.args])
                return "py%s(%s, %s, %s)" % (fn, it, dflt, has)
            if fn == "sum" and node.args:
                start = self.wrap_obj(node.args[1]) if len(node.args) > 1 \
                    else "OBJ_INT(0)"
                return "pysum(%s, %s)" % (self.wrap_obj(node.args[0]), start)
            if fn in ("set", "frozenset"):
                return "pyset(%s)" % self.wrap_obj(node.args[0]) if node.args \
                    else "({ obj _es = list_new(); _es.tag = T_SET; _es; })"
            if fn == "list":
                return "pylist(%s)" % self.wrap_obj(node.args[0]) if node.args \
                    else "list_new()"
            # A tuple is a list in this runtime -- `tuple` maps to T_LIST in
            # the type table already -- so `tuple(xs)` is the same materialise
            # as `list(xs)`. It had no lowering at all, so py2c emitted a bare
            # `tuple(...)` that C defaulted to returning int; three of
            # `tools/cpprust.py`'s errors were that, including a `return
            # tuple(names)` from an obj-returning function.
            if fn == "tuple":
                return "pylist(%s)" % self.wrap_obj(node.args[0]) if node.args \
                    else "list_new()"
            if fn == "dict":
                if not node.args:
                    return "dict_new()"
                if self.stdlib_root:
                    return self._mp_import_call("builtins", "dict", node)
                # dict(x): a dict is copied, an iterable of pairs is
                # built into one. This used to be a bare `pycopy`, which is
                # right for `dict(other_dict)` and wrong for
                # `dict(zip(params, args))` -- that left a *list* of pairs, so
                # crust's type-parameter map answered False to every lookup
                # and generic instantiation substituted nothing, reporting
                # `cannot instantiate Vec over T`.
                return "dict_from(%s)" % self.wrap_obj(node.args[0])
            if fn == "ord" and node.args:
                a0 = node.args[0]
                # ord(s[i]) on a char* -> direct byte read, no per-char string
                # allocation. Makes char-scanning kernels (lexers, parsers)
                # compile to tight C.
                if isinstance(a0, ast.Subscript) and \
                        not isinstance(a0.slice, ast.Slice) and \
                        self.value_ctype(a0.value) == "char*":
                    return "((long)(unsigned char)%s[%s])" % (
                        self.expr(a0.value), self.as_long(a0.slice))
                return "pyord(%s)" % self.wrap_obj(node.args[0])
            if fn == "chr" and node.args:
                return "pychr(%s)" % self.as_long(node.args[0])
            if fn == "int":
                if len(node.args) == 2:
                    return "py_int_base(%s, %s)" % (self.as_str(node.args[0]),
                                                    self.expr(node.args[1]))
                if node.args:
                    vt = self.value_ctype(node.args[0])
                    if vt in ("int", "bool", "double", "float", "long",
                              "short", "char"):
                        return "((long)%s)" % self.expr(node.args[0])
                    if vt == "char*":
                        return "atoi(%s)" % self.expr(node.args[0])
                    return "pyint(%s)" % self.wrap_obj(node.args[0])
                return "0"
            if fn == "abs" and node.args:
                return "pyabs(%s)" % self.as_long(node.args[0])
            # `eval(expr, {"__builtins__": {}}, {})` -- the sandboxing spelling.
            # Lowered only when both environments are *literal* dicts binding
            # no names, because then they provably cannot affect the result:
            # with nothing bound, an expression either references no name (and
            # evaluates the same either way) or fails either way. Anything with
            # a real environment still warns rather than silently losing it.
            if fn == "eval" and len(node.args) == 3 and not node.keywords \
                    and self._is_nameless_env(node.args[1]) \
                    and self._is_nameless_env(node.args[2]):
                self._uses_eval = True
                return "rpy_eval(%s)" % self.as_str(node.args[0])
            if fn == "eval" and len(node.args) == 1 and not node.keywords:
                # Untyped eval: the MicroPython result is boxed into an obj.
                # `x: T = eval(...)` is special-cased in st_AnnAssign to pull the
                # typed value straight out, with no boxing.
                self._uses_eval = True
                return "rpy_eval(%s)" % self.as_str(node.args[0])
            if fn == "exec" and len(node.args) == 1 and not node.keywords:
                # exec(s): run Python statements through the MicroPython core.
                # Returns nothing -- emitted as a statement.
                self._uses_eval = True
                return "rpy_exec(%s)" % self.as_str(node.args[0])
            if fn == "divmod" and len(node.args) == 2:
                # (a // b, a % b) as a 2-tuple; uses the same C division as the
                # `//`/`%` operators so divmod stays consistent with them.
                return ("({ long _a = %s, _b = %s; obj _dm[2]; "
                        "_dm[0] = OBJ_INT(_b ? _a / _b : 0); "
                        "_dm[1] = OBJ_INT(_b ? _a %% _b : 0); "
                        "list_from(_dm, 2); })" % (self.as_long(node.args[0]),
                                                   self.as_long(node.args[1])))
            if fn == "iter" and len(node.args) == 1:
                # Iterables are materialized lists in this runtime, so iter() is
                # the identity (the list is directly indexable).
                return self.wrap_obj(node.args[0])
            if fn == "id" and len(node.args) == 1:
                # Object identity: the heap pointer as an integer (matches
                # CPython's id() being the address). Used for set-based dedup.
                return "OBJ_INT((long)AS_OBJ(%s))" % \
                    self.wrap_obj(node.args[0])
            if fn == "next" and node.args:
                # The iterable is materialized (generator exprs are lowered to
                # list comprehensions), so next() is the first element, or the
                # default (2-arg form) when empty.
                it = self.wrap_obj(node.args[0])
                dflt = self.wrap_obj(node.args[1]) if len(node.args) > 1 \
                    else "OBJ_NONE"
                return ("({ obj _nx = %s; pylen(_nx) > 0 ? "
                        "index_obj(_nx, 0) : %s; })" % (it, dflt))
            if fn == "bool":
                if not node.args:
                    return "0"
                vt = self.value_ctype(node.args[0])
                if vt in ("int", "bool", "long", "short", "char",
                          "double", "float"):
                    return "(%s != 0)" % self.expr(node.args[0])
                # obj / char* / other: Python truthiness, yielding a C bool.
                return "truthy(%s)" % self.wrap_obj(node.args[0])
            if fn == "float" and node.args:
                vt = self.value_ctype(node.args[0])
                if vt in ("int", "bool", "double", "float", "long",
                          "short", "char"):
                    return "((double)%s)" % self.expr(node.args[0])
                return "pyfloat(%s)" % self.wrap_obj(node.args[0])
            if fn == "repr" and node.args:
                return "pyrepr(%s)" % self.wrap_obj(node.args[0])
            if fn == "vars":
                self._warn_unsupported(
                    node.lineno, "vars()", "an empty dict",
                    "attribute introspection has no lowering")
                return "dict_new() /* vars() unsupported */"
            if fn == "hasattr" and len(node.args) == 2 and \
                    isinstance(node.args[1], ast.Constant) and \
                    isinstance(node.args[1].value, str):
                # Structural approximation (yields a C bool): an object "has" an
                # attribute iff it is an instance of the class that declares it.
                # Dynamically-set attributes (no declared owner) read as absent.
                owner = self.resolve_attr_owner(node.args[1].value)
                if owner:
                    return self.lower_isinstance(
                        node.args[0], ast.Name(id=owner.name))
                if self.stdlib_root:
                    return "mp_hasattr(%s, %s)" % (
                        self.wrap_obj(node.args[0]),
                        c_string(node.args[1].value))
                self._warn_unsupported(
                    node.lineno, "hasattr() on a dynamic attribute", "False",
                    "the attribute has no declared owner to check against")
                return "0 /* hasattr: dynamic attr, unsupported */"
            if fn == "getattr" and len(node.args) >= 2 and \
                    not (isinstance(node.args[1], ast.Constant) and
                         isinstance(node.args[1].value, str)):
                # runtime key on a statically-typed struct -> compiled switch
                ci = self._dyn_struct_ci(node.args[0])
                if ci is not None:
                    dflt = self.wrap_obj(node.args[2]) if len(node.args) > 2 \
                        else "OBJ_NONE"
                    return self._emit_dynget(node.args[0], ci,
                                             node.args[1], dflt)
            if fn == "setattr" and len(node.args) == 3:
                ci = self._dyn_struct_ci(node.args[0])
                if ci is not None and not (
                        isinstance(node.args[1], ast.Constant) and
                        isinstance(node.args[1].value, str) and
                        ci.field_ctype(node.args[1].value) is None):
                    return self._emit_dynset(node.args[0], ci,
                                             node.args[1], node.args[2])
                if ci is None and not self._is_module_ref(node.args[0]):
                    # object-model receiver: bridge-free runtime setattr
                    return "({ rt_setattr(%s, %s, %s); OBJ_NONE; })" % (
                        self.wrap_obj(node.args[0]),
                        self._key_charp(node.args[1]),
                        self.wrap_obj(node.args[2]))
            if fn == "getattr" and len(node.args) >= 2 and \
                    isinstance(node.args[1], ast.Constant) and \
                    isinstance(node.args[1].value, str):
                # const key on a statically-typed struct with a declared field:
                # a direct, unboxed member read (normal coercion boxes it on
                # demand, matching value_ctype). Absent fields fall through.
                ci = self._dyn_struct_ci(node.args[0])
                if ci is not None and \
                        ci.field_ctype(node.args[1].value) is not None:
                    return "(%s)->%s" % (
                        self._class_ptr_expr(node.args[0], ci.name),
                        cname(node.args[1].value))
            if fn == "hasattr" and len(node.args) == 2 and self.stdlib_root \
                    and isinstance(node.args[1], ast.Constant) \
                    and isinstance(node.args[1].value, str):
                return "mp_hasattr(%s, %s)" % (
                    self.wrap_obj(node.args[0]), c_string(node.args[1].value))
            if fn == "getattr" and len(node.args) >= 2 and \
                    isinstance(node.args[1], ast.Constant) and \
                    isinstance(node.args[1].value, str):
                attr = node.args[1].value
                dflt = self.wrap_obj(node.args[2]) if len(node.args) > 2 \
                    else "OBJ_NONE"
                owner = self.resolve_attr_owner(attr)
                if owner:
                    if owner.name in self.xclasses and \
                            owner.name not in self.classes:
                        self.xstructs_needed.add(owner.name)
                    return "((%s*)AS_OBJ(%s))->%s" % (
                        owner.csym, self.wrap_obj(node.args[0]), cname(attr))
                if self.stdlib_root:
                    return "mp_getattr(%s, %s, %s)" % (
                        self.wrap_obj(node.args[0]), c_string(attr), dflt)
                if self._is_module_ref(node.args[0]):
                    return dflt
                return "rt_getattr(%s, %s, %s)" % (
                    self.wrap_obj(node.args[0]), c_string(attr), dflt)
            if fn == "getattr" and len(node.args) >= 2:
                if self.stdlib_root and isinstance(node.args[1], ast.Constant) \
                        and isinstance(node.args[1].value, str):
                    dflt = self.wrap_obj(node.args[2]) if len(node.args) > 2 \
                        else "OBJ_NONE"
                    return "mp_getattr(%s, %s, %s)" % (
                        self.wrap_obj(node.args[0]),
                        c_string(node.args[1].value), dflt)
                if self.stdlib_root:
                    dflt = self.wrap_obj(node.args[2]) if len(node.args) > 2 \
                        else "OBJ_NONE"
                    return "mp_getattr_obj(%s, %s, %s)" % (
                        self.wrap_obj(node.args[0]),
                        self.wrap_obj(node.args[1]), dflt)
                dflt = self.wrap_obj(node.args[2]) if len(node.args) > 2 \
                    else "OBJ_NONE"
                if self._is_module_ref(node.args[0]):
                    return dflt
                return "rt_getattr(%s, %s, %s)" % (
                    self.wrap_obj(node.args[0]),
                    self._key_charp(node.args[1]), dflt)
            if fn in self.classes:
                ci = self.classes[fn]
                init = ci.methods.get("__init__")
                if init and init.args.vararg:
                    n_regular = len(init.args.args) - 1
                    regular_args = node.args[:n_regular]
                    var_args = node.args[n_regular:]
                    defs = self.defaults_for(init, True)
                    cargs = self.coerce_args(
                        self.init_param_ctypes(ci), regular_args, defs)
                    wrapped = [self.wrap_obj(a) for a in var_args]
                    if not wrapped:
                        return "%s_new(0)" % ci.csym
                    return "%s_new(%d, %s)" % (
                        ci.csym, len(wrapped), ", ".join(wrapped))
                defs = self.defaults_for(init, True) if init else None
                merged = self._merge_keyword_args(init, node.args,
                                                  node.keywords, True) \
                    if init and node.keywords else node.args
                cargs = self.coerce_args(
                    self.init_param_ctypes(ci), merged, defs)
                cargs = self._pad_ctor_kwargs(init, cargs)
                return "%s_new(%s)" % (ci.csym, ", ".join(cargs))
            if fn in self.func_params:
                fndef = self.func_nodes[fn]
                if fndef.args.vararg:
                    return self._lower_vararg_local_call(fn, fndef, node)
                if any(isinstance(a, ast.Starred) for a in node.args):
                    starcall = self._lower_starred_local_call(fn, fndef, node)
                    if starcall:
                        return starcall
                merged = self._merge_keyword_args(fndef, node.args,
                                                  node.keywords) \
                    if node.keywords else node.args
                defs = self.defaults_for(fndef, False)
                cargs = self.coerce_args(self.func_params[fn], merged, defs)
                return "%s(%s)" % (self.fnsym(fn), ", ".join(cargs))
            if fn in self.from_imports:
                kind, info = self.xref(fn, self.from_imports[fn])
                if kind == "class":
                    self._ref_xclass(info, body=True)
                    init = self._resolve_init(info)
                    pct = [arg_ctype(init, a) for a in init.args.args[1:]] \
                        if init else []
                    defs = self.defaults_for(init, True) if init else None
                    merged = self._merge_keyword_args(init, node.args,
                                                      node.keywords, True) \
                        if init and node.keywords else node.args
                    cargs = self.coerce_args(pct, merged, defs)
                    cargs = self._pad_ctor_kwargs(init, cargs)
                    return "%s_new(%s)" % (info.csym, ", ".join(cargs))
                if kind == "func":
                    pct = [self._imported_arg_ctype(info, a)
                           for a in info.args.args]
                    defs = self.defaults_for(info, False)
                    merged = self._merge_keyword_args(info, node.args,
                                                      node.keywords) \
                        if node.keywords else node.args
                    cargs = self.coerce_args(pct, merged, defs)
                    return "%s(%s)" % (
                        func_csym(fn, self.from_imports[fn],
                                  self.ambiguous_funcs),
                        ", ".join(cargs))
                if self.stdlib_root:
                    return self._mp_import_call(self.from_imports[fn], fn, node)
            if fn in self.mod_global_types and self.mod_global_types[fn] == OBJ \
                    and fn not in self.scope:
                args = [self.wrap_obj(a) for a in node.args]
                return self._emit_call_obj(self._msym(fn), args)
            if self.stdlib_root and fn in STDLIB_BUILTINS:
                return self._mp_import_call("builtins", fn, node)
            if fn in self.from_imports and fn not in self.scope \
                    and fn not in self.func_params and fn not in self.func_nodes:
                return self._mp_import_call(self.from_imports[fn], fn, node)
            if fn not in self.scope and fn not in self.from_imports \
                    and self.star_import_mods and self.stdlib_root \
                    and fn not in self.func_nodes and fn not in self.classes:
                return self._mp_import_call(self.star_import_mods[-1], fn, node)
            if fn in ("metadata", "module", "require", "package"):
                args = [self.expr(a) for a in node.args]
                if fn == "metadata":
                    return "metadata()"
                return "%s(%s)" % (fn, ", ".join(args))
            if self.stdlib_root and fn in EXCEPTION_NAMES:
                return "mp_getattr(mp_call_import(\"builtins\", \"\", 0), %s, OBJ_NONE)" % (
                    c_string(fn))
            # calling an obj-typed local/param: a first-class function value
            if fn in self.scope and self.scope[fn] == OBJ \
                    and fn not in self.func_params and fn not in self.classes:
                varcall = self._lower_varcall(self.fnsym(fn), node)
                if varcall:
                    return varcall
                args = [self.wrap_obj(a) for a in node.args]
                return self._emit_call_obj(self.fnsym(fn), args)

        if isinstance(func, ast.Attribute) and is_super_call(func.value):
            base = self.cur_class.base if self.cur_class else None
            if base is None:
                # base is external/builtin (e.g. Exception): nothing to chain to
                return "(void)0"
            bname = base.name
            if func.attr == "__init__":
                owner = base.find_method_owner("__init__")
                if owner is None:
                    # super().__init__() resolves to object.__init__ (no class
                    # in the base chain defines one): a no-op. Emitting
                    # Base___init__ here would be an undeclared/dangling call.
                    return "(void)0"
                init = owner.methods.get("__init__")
                pct = self.init_param_ctypes(owner)
                defs = self.defaults_for(init, True) if init else None
                cargs = self.coerce_args(pct, node.args, defs)
                if owner.name not in self.classes:  # imported base: extern decls
                    self.xstructs_needed.add(owner.name)
                    self.used_xmethods[(owner.name, "__init__")] = "void"
                return "%s___init__((%s*)self%s)" % (
                    owner.csym, owner.csym,
                    (", " + ", ".join(cargs)) if cargs else "")
            if func.attr in VTABLE_METHODS:
                return "%s_%s((Obj*)self%s)" % (
                    base.csym, func.attr,
                    (", " + ", ".join(argstrs)) if argstrs else "")
            if bname not in self.classes:       # imported base: extern decl
                self.xstructs_needed.add(bname)
                self.used_xmethods.setdefault(
                    (bname, func.attr),
                    self._c_ret(base.methods.get(func.attr)))
            return "%s_%s((%s*)self%s)" % (
                base.csym, func.attr, base.csym,
                (", " + ", ".join(argstrs)) if argstrs else "")

        # `hashlib.sha256(X).hexdigest()` -- the one-shot form. Matched before
        # the generic attribute-call path below, which lowers the inner
        # `sha256(...)` to OBJ_NONE and then emits an undeclared
        # `hexdigest(...)` returning `int`, breaking any subsequent slice.
        #
        # Implemented rather than stubbed because the caller in `rpyinc` uses
        # the digest as a *cache key*: a constant would silently make every
        # module share one cache entry. The incremental form
        # (`h = sha256(); h.update(..)`) is still unsupported.
        if isinstance(func, ast.Attribute) and func.attr == "hexdigest" \
                and not node.args \
                and isinstance(func.value, ast.Call) \
                and isinstance(func.value.func, ast.Attribute) \
                and func.value.func.attr == "sha256" \
                and isinstance(func.value.func.value, ast.Name) \
                and func.value.func.value.id == "hashlib" \
                and len(func.value.args) == 1:
            self._ossys_used = True
            inner = func.value.args[0]
            # `.encode("utf-8")` is a no-op here: the runtime hashes the bytes
            # of a C string either way.
            while isinstance(inner, ast.Call) \
                    and isinstance(inner.func, ast.Attribute) \
                    and inner.func.attr == "encode":
                inner = inner.func.value
            # Boxed: `hexdigest()` is a `str`, and the common use is a slice
            # (`[:12]`), whose helper takes an `obj`.
            return "OBJ_STR(_sha256_hex(%s))" % self.coerce_to(
                "char*", inner, self.expr(inner))

        if isinstance(func, ast.Attribute):
            # A receiver known to hold a compiled pattern (`_RE = re.compile(...)`)
            # or a match result must lower `.search`/`.match`/`.group` to the
            # generated translation-time matcher, taking priority over any regex
            # module (minire/test_minire) whose Pattern/Match classes share those
            # method names -- those modules aren't part of the bootstrap link.
            if (self._regex_ids or self._regex_dyn) and isinstance(func.value, ast.Name) and (
                    func.value.id in self._regex_vars or
                    func.value.id in self._regex_match_vars):
                if func.attr in ("search", "match") and len(node.args) == 1:
                    anc = "1" if func.attr == "match" else "0"
                    txt = self.coerce_to("char*", node.args[0],
                                         self.expr(node.args[0]))
                    return "_re_search(AS_INT(%s), %s, %s)" % (
                        self.wrap_obj(func.value), txt, anc)
                if func.attr == "group":
                    if not node.args:
                        gi = "0"
                    elif isinstance(node.args[0], ast.Constant) and \
                            isinstance(node.args[0].value, int):
                        gi = str(node.args[0].value)
                    else:
                        gi = self.coerce_to("int", node.args[0],
                                            self.expr(node.args[0]))
                    return "index_obj(%s, %s)" % (self.wrap_obj(func.value), gi)
            # float.fromhex("0x1.8p3") -- C's strtod parses hex float literals.
            if func.attr == "fromhex" and isinstance(func.value, ast.Name) \
                    and func.value.id == "float" and node.args:
                return "float_fromhex(%s)" % self.as_str(node.args[0])
            # const-dict .get() (e.g. self.size_map.get(...)) takes priority
            if func.attr == "get" and isinstance(func.value, ast.Attribute) \
                    and isinstance(func.value.value, ast.Name) \
                    and func.value.value.id == "self" and self.cur_class \
                    and func.value.attr in self.cur_class.const_dicts:
                d = func.value.attr
                k = argstrs[0]
                dflt = argstrs[1] if len(argstrs) > 1 else '""'
                return "%s_%s_get(%s, %s)" % (self.cur_class.name, d, k, dflt)
            # a method defined in exactly one imported hierarchy (e.g. the
            # CType predicates is_void/is_integral/...) dispatches through that
            # module's canonical vtable -- correct for a non-leaf base and
            # uniform whether the receiver is a typed pointer or a bare obj.
            xm = self._exclusive_vt_module(func.attr)
            if xm is not None and not self.ctor_class(node):
                fv = func.value
                if not (isinstance(fv, ast.Name) and (
                        fv.id in self.import_alias or fv.id in self.modules
                        or fv.id in self.classes or fv.id in self.xclasses)):
                    return self.xvcall(xm, func.value, func.attr, node.args)
            # method call on a concrete class instance (local or imported) is
            # resolved before the generic list/dict/str heuristics so that an
            # instance method named append/add/get/pop/etc. wins.
            rcv = self.method_on_instance(func, node)
            if rcv is not None:
                return rcv
            # list methods on an obj/list receiver
            if func.attr == "append" and len(node.args) == 1:
                tlct = self._typed_list_ct(func.value)
                if tlct is not None:
                    return "%s_push(%s, %s)" % (
                        tlct[:-1], self.expr(func.value),
                        self.coerce_to(tlct[len("_tlist_"):-1], node.args[0],
                                       self.expr(node.args[0])))
                return "list_append(%s, %s)" % (self.expr(func.value),
                                                self.wrap_obj(node.args[0]))
            if func.attr == "pop" and len(node.args) == 0:
                # `xs.pop()` on an unboxed typed list removes and returns the
                # last element directly (the boxed list_pop would reinterpret
                # the _tlist_T struct as a boxed list and corrupt both the value
                # and the length). value_ctype reports the element scalar so the
                # result types like a subscript read.
                tlct = self._typed_list_ct(func.value)
                if tlct is not None:
                    return "%s_pop(%s)" % (tlct[:-1], self.expr(func.value))
            if func.attr == "extend" and len(node.args) == 1 and \
                    func.attr not in self.method_owners:
                return "list_extend(%s, %s)" % (self.wrap_obj(func.value),
                                                self.wrap_obj(node.args[0]))
            if func.attr == "add" and len(node.args) == 1 and \
                    func.attr not in self.method_owners and \
                    func.attr not in self.xmethod_owners:
                # `add` is also ILCode/ASMCode/ErrorCollector.add; when the
                # receiver is one of those classes (passed as an untyped obj,
                # e.g. asm_code in a make_asm vtable body) a flat set_add reads
                # the wrong object layout. Dispatch on the receiver's real type;
                # the helper falls back to set_add for genuine set receivers.
                amb = self._obj_ambiguous_dispatch(
                    func.value, func.attr, node.args)
                if amb is not None:
                    return amb
                return "set_add(%s, %s)" % (self.wrap_obj(func.value),
                                            self.wrap_obj(node.args[0]))
            if func.attr == "clear" and not node.args and \
                    not self._recv_class_owns(func.value, "clear"):
                return "pyclear(%s)" % self.wrap_obj(func.value)
            if func.attr == "copy" and not node.args and \
                    not (isinstance(func.value, ast.Name) and
                         (func.value.id in self.import_alias or
                          func.value.id == "copy")):
                # `copy` is both a container builtin (dict/list/set.copy) and a
                # user method (ILCode/NodeGraph/State.copy). When the receiver is
                # an untyped obj we cannot tell them apart statically, so dispatch
                # on the receiver's real TypeInfo: each defining class calls its
                # own copy, and a genuine container falls back to pycopy via the
                # helper's tail. Only when that runtime switch does not apply (no
                # ambiguity / single-module host build) do we fall back to the
                # flat pycopy, and then only if `copy` is not a user method here.
                amb = self._obj_ambiguous_dispatch(
                    func.value, func.attr, node.args)
                if amb is not None:
                    return amb
                if func.attr not in self.method_owners and \
                        func.attr not in self.xmethod_owners:
                    # dict/list/set .copy() -> shallow copy (runtime-dispatched).
                    return "pycopy(%s)" % self.wrap_obj(func.value)
            if func.attr == "remove" and len(node.args) == 1 and \
                    func.attr not in self.method_owners:
                return "list_remove(%s, %s)" % (self.wrap_obj(func.value),
                                                self.wrap_obj(node.args[0]))
            if func.attr == "discard" and len(node.args) == 1 and \
                    func.attr not in self.method_owners and \
                    func.attr not in self.xmethod_owners:
                # set.discard: remove if present (list_remove is already a
                # no-op when the value is absent, which is discard's contract).
                return "list_remove(%s, %s)" % (self.wrap_obj(func.value),
                                                self.wrap_obj(node.args[0]))
            if func.attr == "pop" and not node.args and \
                    not self._local_method_accepts_argc("pop", 0):
                return "list_pop(%s)" % self.wrap_obj(func.value)
            if func.attr == "index" and len(node.args) == 1 and \
                    func.attr not in self.method_owners:
                return "list_index(%s, %s)" % (self.wrap_obj(func.value),
                                               self.wrap_obj(node.args[0]))
            # `s.count(sub, start[, end])` -- a string count over a window.
            # Only the 2- and 3-argument forms: with one argument the receiver
            # could equally be a list, and that already lowers below. The
            # windowed spelling is unambiguous, and had no lowering at all --
            # py2c emitted a bare `count(...)` that C defaulted to returning
            # int. `tools/cpprust.py` counts newlines that way to turn an
            # offset into a line number.
            if func.attr == "count" and 2 <= len(node.args) <= 3 and \
                    func.attr not in self.method_owners:
                return "str_count(%s, %s, %s, %s)" % (
                    self.coerce_to("char*", func.value, self.expr(func.value)),
                    self.coerce_to("char*", node.args[0],
                                   self.expr(node.args[0])),
                    self.coerce_to("int", node.args[1],
                                   self.expr(node.args[1])),
                    self._re_endpos(node, 2))
            if func.attr == "count" and len(node.args) == 1 and \
                    func.attr not in self.method_owners:
                return "list_count(%s, %s)" % (self.wrap_obj(func.value),
                                               self.wrap_obj(node.args[0]))
            if func.attr == "sort":
                ks = self._sort_with_key(func, node)
                if ks is not None:
                    return ks
                return "list_sort(%s)" % self.expr(func.value)
            if func.attr == "reverse" and not node.args and \
                    func.attr not in self.method_owners:
                return "list_reverse(%s)" % self.wrap_obj(func.value)
            # string methods (guard against user methods of the same name)
            if func.attr in self.STR_METHODS and \
                    func.attr not in self.method_owners:
                r = self.lower_str_method(func, node)
                if r is not None:
                    return r
            # `.items()/.keys()/.values()` on a specialized const dict: call the
            # owning module's generated helper (the dict has no runtime object).
            if func.attr in ("items", "keys", "values") and not node.args \
                    and isinstance(func.value, ast.Attribute):
                cd_owner = self.const_dict_owner(func.value)
                if cd_owner is not None:
                    d = func.value.attr
                    dnode = cd_owner.const_dicts.get(d)
                    if dnode is not None and all(
                            isinstance(k, ast.Constant)
                            and isinstance(k.value, str) for k in dnode.keys) \
                            and all(isinstance(v, ast.List)
                                    for v in dnode.values):
                        return "%s_%s_%s()" % (cd_owner.name, d, func.attr)
            # dict methods on an obj receiver
            if func.attr == "items" and not node.args:
                return "dict_items(%s)" % self.wrap_obj(func.value)
            if func.attr == "keys" and not node.args:
                return "dict_keys(%s)" % self.wrap_obj(func.value)
            if func.attr == "values" and not node.args:
                return "dict_values(%s)" % self.wrap_obj(func.value)
            if func.attr == "update" and len(node.args) == 1:
                return "dict_update(%s, %s)" % (self.wrap_obj(func.value),
                                                self.wrap_obj(node.args[0]))
            if func.attr == "setdefault":
                k = self.wrap_obj(node.args[0])
                d = self.wrap_obj(node.args[1]) if len(node.args) > 1 \
                    else "OBJ_NONE"
                return "dict_setdefault(%s, %s, %s)" % (
                    self.wrap_obj(func.value), k, d)
            if func.attr == "pop" and node.args:
                k = self.wrap_obj(node.args[0])
                d = self.wrap_obj(node.args[1]) if len(node.args) > 1 \
                    else "OBJ_NONE"
                return "dict_pop(%s, %s, %s)" % (self.wrap_obj(func.value), k, d)
            if func.attr == "get" and node.args:
                k = self.wrap_obj(node.args[0])
                d = self.wrap_obj(node.args[1]) if len(node.args) > 1 \
                    else "OBJ_NONE"
                return "dict_get(%s, %s, %s)" % (self.wrap_obj(func.value), k, d)
            if isinstance(func.value, ast.Name) and \
                    func.value.id in self.import_alias:
                modname = self.import_alias[func.value.id]
                kind, info = self.xref(func.attr, modname)
                if kind == "class":
                    self._ref_xclass(info, body=True)
                    init = self._resolve_init(info)
                    pct = [arg_ctype(init, a) for a in init.args.args[1:]] \
                        if init else []
                    defs = self.defaults_for(init, True) if init else None
                    merged = self._merge_keyword_args(init, node.args,
                                                      node.keywords, True) \
                        if init and node.keywords else node.args
                    cargs = self.coerce_args(pct, merged, defs)
                    cargs = self._pad_ctor_kwargs(init, cargs)
                    return "%s_new(%s)" % (info.csym, ", ".join(cargs))
                if kind == "func":
                    pct = [self._imported_arg_ctype(info, a)
                           for a in info.args.args]
                    defs = self.defaults_for(info, False)
                    merged = self._merge_keyword_args(info, node.args,
                                                      node.keywords) \
                        if node.keywords else node.args
                    cargs = self.coerce_args(pct, merged, defs)
                    return "%s(%s)" % (
                        func_csym(func.attr, modname, self.ambiguous_funcs),
                        ", ".join(cargs))
                if self.stdlib_root:
                    return self._mp_import_call(modname, func.attr, node)
                if not modname.startswith("shivyc"):
                    if modname == "copy" and func.attr == "copy":
                        cc = self._shallow_copy(node)
                        if cc:
                            return cc
                    # translation-time regex: re.compile / re.search / re.match
                    # with a constant pattern lower to a generated C matcher.
                    if modname == "re" and func.attr in (
                            "compile", "search", "match") and node.args and \
                            isinstance(node.args[0], ast.Constant) and \
                            isinstance(node.args[0].value, str):
                        pid = self._re_intern(node.args[0].value)
                        if pid is not None:
                            if func.attr == "compile":
                                return "OBJ_INT(%d)" % pid
                            anc = "1" if func.attr == "match" else "0"
                            txt = self.coerce_to("char*", node.args[1],
                                                 self.expr(node.args[1]))
                            return "_re_search(%d, %s, %s)" % (pid, txt, anc)
                    # A pattern only known at runtime -- an interpreter passing
                    # a guest script's pattern through, say. Compiled on first
                    # use and cached, so a loop over the same pattern pays once.
                    if modname == "re" and func.attr in ("search", "match") \
                            and len(node.args) == 2:
                        self._regex_dyn = True
                        anc = "1" if func.attr == "match" else "0"
                        pat = self.coerce_to("char*", node.args[0],
                                             self.expr(node.args[0]))
                        txt = self.coerce_to("char*", node.args[1],
                                             self.expr(node.args[1]))
                        return "_cre_dyn(%s, %s, %s)" % (pat, txt, anc)
                    # subprocess.run(argv, capture_output=, text=, cwd=).
                    # Only those keywords are lowered: check= would need to
                    # raise, and env=/stdout=/stderr= change the plumbing, so a
                    # call using them keeps warning rather than having the
                    # option silently dropped.
                    if modname == "subprocess" and func.attr == "run" \
                            and len(node.args) == 1:
                        kw = {k.arg: k.value for k in node.keywords if k.arg}
                        if all(k in ("capture_output", "text", "cwd") for k in kw) \
                                and not any(k.arg is None for k in node.keywords):
                            cap = "1" if self._const_true(kw.get("capture_output")) else "0"
                            cwd = ("(char*)0" if "cwd" not in kw else
                                   self.coerce_to("char*", kw["cwd"],
                                                  self.expr(kw["cwd"])))
                            self._subproc_used = True
                            return "_sp_run(%s, %s, %s)" % (
                                self.wrap_obj(node.args[0]), cwd, cap)
                    if modname == "tempfile" and func.attr == "mkdtemp" \
                            and not node.args:
                        kw = {k.arg: k.value for k in node.keywords if k.arg}
                        if all(k == "prefix" for k in kw):
                            self._subproc_used = True
                            pre = ('OBJ_STR("tmp")' if "prefix" not in kw
                                   else self.expr(kw["prefix"]))
                            pre = self.coerce_to("char*", kw.get("prefix"), pre) \
                                if "prefix" in kw else '"tmp"'
                            return "_tf_mkdtemp(%s)" % pre
                    if modname == "re" and func.attr == "compile" \
                            and len(node.args) == 1:
                        self._regex_dyn = True
                        return self.wrap_obj(node.args[0])
                    # re.escape needs no engine at all -- it is pure string
                    # work, so it is lowered even in a program with no other
                    # regex use.
                    if modname == "re" and func.attr == "escape" \
                            and len(node.args) == 1:
                        self._regex_escape = True
                        return "_re_escape(%s)" % self.coerce_to(
                            "char*", node.args[0], self.expr(node.args[0]))
                    if modname == "re" and func.attr == "finditer" \
                            and len(node.args) == 2:
                        self._regex_dyn = True
                        return "_cre_finditer(%s, %s)" % (
                            self.coerce_to("char*", node.args[0],
                                           self.expr(node.args[0])),
                            self.coerce_to("char*", node.args[1],
                                           self.expr(node.args[1])))
                    if modname == "re" and func.attr == "findall" \
                            and len(node.args) == 2:
                        self._regex_dyn = True
                        return "_cre_findall(%s, %s)" % (
                            self.coerce_to("char*", node.args[0],
                                           self.expr(node.args[0])),
                            self.coerce_to("char*", node.args[1],
                                           self.expr(node.args[1])))
                    # Only a literal-string replacement is lowered. A callable
                    # repl would need the match object passed back into guest
                    # code, so it is left to warn rather than be silently
                    # mishandled.
                    if modname == "re" and func.attr == "sub" \
                            and len(node.args) == 3 \
                            and self._is_str_repl(node.args[1]):
                        self._regex_dyn = True
                        return "_cre_sub(%s, %s, %s)" % (
                            self.coerce_to("char*", node.args[0],
                                           self.expr(node.args[0])),
                            self.coerce_to("char*", node.args[1],
                                           self.expr(node.args[1])),
                            self.coerce_to("char*", node.args[2],
                                           self.expr(node.args[2])))
                    # A replacement reached through a value: a function taking
                    # the match, or a string this pass cannot prove is one.
                    # `_cre_sub_any` reads the tag and does the right thing,
                    # which is better than deciding here -- both spellings
                    # reach this point and only the runtime knows which.
                    if modname == "re" and func.attr == "sub" \
                            and len(node.args) == 3:
                        self._regex_dyn = True
                        return "_cre_sub_any(%s, %s, %s)" % (
                            self.coerce_to("char*", node.args[0],
                                           self.expr(node.args[0])),
                            self.wrap_obj(node.args[1]),
                            self.coerce_to("char*", node.args[2],
                                           self.expr(node.args[2])))
                    # struct.pack/unpack subset (see STRUCT_PRELUDE)
                    if modname == "struct" and func.attr in ("pack", "unpack") \
                            and len(node.args) == 2:
                        self._struct_used = True
                        fmt = self.coerce_to("char*", node.args[0],
                                             self.expr(node.args[0]))
                        if func.attr == "pack":
                            val = self.coerce_to("double", node.args[1],
                                                 self.expr(node.args[1]))
                            return "_struct_pack(%s, %s)" % (fmt, val)
                        return "_struct_unpack(%s, %s)" % (
                            fmt, self.wrap_obj(node.args[1]))
                    for a in node.args:
                        self.expr(a)
                    self._warn_unsupported(
                        node.lineno, "%s.%s()" % (func.value.id, func.attr),
                        "None",
                        "guard the call for the self-hosted build, or the "
                        "None will reach whatever consumes the result")
                    return "OBJ_NONE /* %s.%s(...) unsupported */" % (
                        func.value.id, func.attr)
                if func.value.id in self.modules:
                    mod = self.import_alias.get(func.value.id, func.value.id)
                    if self.stdlib_root:
                        return self._mp_import_call(mod, func.attr, node)
                    return "%s_%s(%s)" % (func.value.id, func.attr,
                                          ", ".join(argstrs))
            # a method name that collides between a local class method and an
            # imported polymorphic hierarchy (e.g. `make_asm`: ASMCode defines
            # a 0-arg make_asm, while ILCommand's is make_asm(spotmap, ...)).
            # When the call's arity cannot fit any local method, it must be the
            # hierarchy method -> dispatch through the cross-module vtable.
            if func.attr in self.hierarchy_method and \
                    not self._local_method_accepts_argc(func.attr,
                                                         len(node.args)):
                # ambiguous name across unrelated non-polymorphic classes
                # (e.g. ILCode.add): the per-module vtable slot offsets differ,
                # so dispatch on the receiver's real type rather than casting
                # through one module's (wrong) vtable.
                amb = self._obj_ambiguous_dispatch(
                    func.value, func.attr, node.args)
                if amb is not None:
                    return amb
                _nmod = self._narrowed_recv_vt_module(func)
                if _nmod is not None:
                    return self.xvcall(_nmod, func.value, func.attr, node.args)
                return self.xvcall(self.hierarchy_method[func.attr],
                                   func.value, func.attr, node.args)
            # A static method invoked on the class itself
            # (`ASMCode.get_label()`) is a direct call, never a vtable dispatch
            # -- even when that method name is virtual on some *other* class.
            if isinstance(func.value, ast.Name) and \
                    func.value.id not in self.scope:
                _scls = func.value.id
                _sci = self.classes.get(_scls) or \
                    (self.xclasses[_scls][0] if _scls in self.xclasses else None)
                if _sci is not None and \
                        func.attr in getattr(_sci, "static_methods", set()):
                    m = _sci.methods.get(func.attr)
                    cargs = self.coerce_args(
                        [arg_ctype(m, a) for a in m.args.args] if m else [],
                        node.args)
                    if _scls not in self.classes:    # imported: needs an extern
                        self.used_xmethods[(_sci.name, func.attr)] = \
                            self._c_ret(m) if m else OBJ
                    return "%s_%s(%s)" % (_sci.csym, func.attr,
                                          ", ".join(cargs))
            # Explicit base-class init by class name: `Node.__init__(self)` ->
            # Node___init__((Node*)self). (super().__init__() is handled
            # separately; this is the spelled-out form.)
            if isinstance(func.value, ast.Name) and \
                    func.value.id not in self.scope and \
                    func.attr == "__init__" and node.args:
                _bcls = func.value.id
                _bci = self.classes.get(_bcls) or \
                    (self.xclasses[_bcls][0] if _bcls in self.xclasses else None)
                if _bci is not None:
                    owner = _bci.find_method_owner("__init__")
                    recv0 = node.args[0]
                    if owner is None:        # object.__init__: nothing to do
                        return "(void)(%s)" % \
                            self._class_ptr_expr(recv0, _bci.csym)
                    rest = self.coerce_args(
                        self.init_param_ctypes(owner), node.args[1:],
                        self.defaults_for(owner.methods.get("__init__"), True))
                    if owner.name not in self.classes:
                        self.xstructs_needed.add(owner.name)
                        self.used_xmethods[(owner.name, "__init__")] = "void"
                    return "%s___init__(%s%s)" % (
                        owner.csym, self._class_ptr_expr(recv0, owner.csym),
                        (", " + ", ".join(rest)) if rest else "")
            if func.attr in VTABLE_METHODS:
                return self.vcall(func.value, func.attr,
                                  self._call_args_with_kw(node, func.attr, func.value))
            # concrete class. Safe to devirtualize only for a leaf class, so no
            # subclass can override the method at runtime.
            if isinstance(func.value, ast.Name) and \
                    func.value.id in self.narrowed:
                cls = self.narrowed[func.value.id][:-1]
                ci = self.classes.get(cls) or (self.xclasses[cls][0]
                                               if cls in self.xclasses else None) \
                    or self._ci_by_csym(cls)
                if ci is not None and self._class_is_leaf(cls):
                    owner = ci.find_method_owner(func.attr)
                    if owner is None and func.attr in ci.methods:
                        owner = ci
                    if owner is not None and func.attr in owner.methods:
                        m = owner.methods[func.attr]
                        if owner.name in self.xclasses and \
                                owner.name not in self.classes:
                            self.used_xmethods[(owner.name, func.attr)] = \
                                self._c_ret(m)
                        return self._format_direct_method_call(
                            owner, m, func.value, node.args)
            # non-virtual method on a concrete class pointer
            bt = self.value_ctype(func.value)
            if bt and bt.endswith("*") and bt != OBJ and bt[:-1] in self.classes:
                ci = self.classes[bt[:-1]]
                owner = ci.find_method_owner_mi(func.attr)
                if owner:
                    m = owner.methods.get(func.attr)
                    if m is not None:
                        return self._format_direct_method_call(
                            owner, m, func.value, node.args)
            # method on a concrete *imported* class pointer (e.g. narrowed by
            # isinstance). Safe to devirtualize only when the static class is a
            # leaf, so no subclass can override the method at runtime.
            if bt and bt.endswith("*") and bt != OBJ and bt[:-1] in self.xclasses \
                    and self._class_is_leaf(bt[:-1]):
                cls = bt[:-1]
                ci = self.xclasses[cls][0]
                m = ci.methods.get(func.attr)
                if m is not None:
                    ret = self._c_ret(m)
                    self.used_xmethods[(cls, func.attr)] = ret
                    pct = [arg_ctype(m, a) for a in m.args.args[1:]]
                    cargs = self.coerce_args(pct, node.args)
                    return "%s_%s(%s%s)" % (
                        cls, func.attr,
                        self._class_ptr_expr(func.value, cls),
                        (", " + ", ".join(cargs)) if cargs else "")
            # imported class pointer that is NOT a leaf (e.g. `node.expr`
            # narrowed by isinstance to CompoundLiteral, which has subclasses):
            # a runtime override is possible, so dispatch through the hierarchy's
            # canonical vtable rather than devirtualizing or, worse, falling
            # through to a bare undeclared call.
            if bt and bt.endswith("*") and bt != OBJ and bt[:-1] in self.xclasses:
                pxmod = self.resolve_project_xvirtual(func.attr)
                if pxmod:
                    return self.xvcall(pxmod, func.value, func.attr, node.args)
            if isinstance(func.value, ast.Name) and \
                    func.value.id in self.classes:
                return "%s_%s(%s)" % (func.value.id, func.attr,
                                      ", ".join(argstrs))
            # method call on an untyped obj: resolve a unique non-vtable owner
            if self.is_obj_word(func.value) or \
                    self.value_ctype(func.value) == OBJ:
                owner = self.resolve_method_owner(func.attr)
                if owner:
                    m = owner.methods.get(func.attr)
                    pct = [arg_ctype(m, a) for a in m.args.args[1:]] \
                        if m else []
                    cargs = self.coerce_args(pct, node.args)
                    return "%s_%s((%s*)AS_OBJ(%s)%s)" % (
                        owner.csym, func.attr, owner.csym,
                        self.expr(func.value),
                        (", " + ", ".join(cargs)) if cargs else "")
                xowner = self.resolve_xmethod_owner(func.attr)
                if xowner:
                    m = xowner.methods.get(func.attr)
                    ret = self._c_ret(m) if m else OBJ
                    self.used_xmethods[(xowner.name, func.attr)] = ret
                    args = [self.expr(a) for a in node.args]
                    return "%s_%s((%s*)AS_OBJ(%s)%s)" % (
                        xowner.csym, func.attr, xowner.csym,
                        self.expr(func.value),
                        (", " + ", ".join(args)) if args else "")
                xmod = self.resolve_xvirtual(func.attr)
                if xmod:
                    return self.xvcall(xmod, func.value, func.attr, node.args)
                # a polymorphic method whose hierarchy this module never imports
                # (e.g. general_nodes calling expr.lvalue()); dispatch through
                # the hierarchy root's canonical vtable, resolved project-wide.
                pxmod = self.resolve_project_xvirtual(func.attr)
                if pxmod:
                    return self.xvcall(pxmod, func.value, func.attr, node.args)
                # ambiguous name across unrelated non-polymorphic classes
                # (e.g. ILCode.add vs ErrorCollector.add): a single vtable cast
                # would read the wrong per-module slot offset, so dispatch on
                # the receiver's real type.
                amb = self._obj_ambiguous_dispatch(
                    func.value, func.attr, node.args)
                if amb is not None:
                    return amb
                # method belonging to an imported hierarchy whose root spans
                # modules: dispatch through the root module's canonical vtable
                if func.attr in self.hierarchy_method:
                    return self.xvcall(self.hierarchy_method[func.attr],
                                       func.value, func.attr, node.args)
                # last resort: a method whose sole project-wide definer is a
                # class not imported here (e.g. layout.slot_for_spot where
                # layout came from getattr), or a @staticmethod with forwarders.
                presult = self.resolve_project_xmethod(func.attr)
                if presult is not None:
                    powner, is_static = presult
                    m = powner.methods.get(func.attr)
                    ret = self._c_ret(m) if m else OBJ
                    self.used_xmethods[(powner.name, func.attr)] = ret
                    args = [self.expr(a) for a in node.args]
                    if is_static:        # @staticmethod: receiver discarded
                        return "%s_%s(%s)" % (
                            powner.csym, func.attr, ", ".join(args))
                    return "%s_%s((%s*)AS_OBJ(%s)%s)" % (
                        powner.csym, func.attr, powner.csym,
                        self.expr(func.value),
                        (", " + ", ".join(args)) if args else "")
            if func.attr == "insert" and len(node.args) == 2 and \
                    func.attr not in self.method_owners:
                lo = self.coerce_to("int", node.args[0], self.expr(node.args[0]))
                return "list_insert(%s, %s, %s)" % (
                    self.wrap_obj(func.value), lo, self.wrap_obj(node.args[1]))
            if isinstance(func.value, ast.Name) and \
                    func.value.id in self.from_imports:
                sym = func.value.id
                if func.attr == sym:
                    kind, info = self.xref(sym, self.from_imports[sym])
                    if kind == "class":
                        self._ref_xclass(info, body=True)
                        init = info.methods.get("__init__")
                        pct = [arg_ctype(init, a) for a in init.args.args[1:]] \
                            if init else []
                        defs = self.defaults_for(init, True) if init else None
                        merged = self._merge_keyword_args(init, node.args,
                                                          node.keywords, True) \
                            if init and node.keywords else node.args
                        cargs = self.coerce_args(pct, merged, defs)
                        cargs = self._pad_ctor_kwargs(init, cargs)
                        return "%s_new(%s)" % (info.csym, ", ".join(cargs))
            # `self.attr(...)` where `attr` is a class-valued attribute (an
            # instance field holding a constructor/closure, e.g. the
            # polymorphic `default_il_cmd = math_cmds.Add` idiom) is a call of
            # the stored closure, not a method named `attr`.
            recv_ct = self.value_ctype(func.value)
            if isinstance(recv_ct, str) and recv_ct.endswith("*") \
                    and recv_ct != OBJ:
                rci = self.classes.get(recv_ct[:-1])
                if rci is not None \
                        and rci.find_method_owner(func.attr) is None \
                        and any(f == func.attr for f, _ in rci.own_fields):
                    fld = self.expr(func)
                    wargs = [self.wrap_obj(a) for a in node.args]
                    return self._emit_call_obj(fld, wargs)
            # translation-time regex methods on a compiled-pattern obj (an
            # OBJ_INT id) or a match obj (a list). Active only when at least one
            # static pattern compiled. A receiver KNOWN to hold a compiled
            # pattern or a match result takes this path unconditionally (so the
            # compiler's own `_RE.search(...).group(...)` never binds to a regex
            # module's Pattern/Match methods); any other receiver keeps the
            # conservative guard so a real ShivyCX `.search`/`.group` isn't shadowed.
            _re_recv = isinstance(func.value, ast.Name) and (
                func.value.id in self._regex_vars or
                func.value.id in self._regex_dyn_vars or
                func.value.id in self._regex_match_vars)
            # A dynamically compiled pattern IS its pattern string, so its
            # methods go straight to the dynamic bridge rather than through
            # the id dispatch.
            # A constant-pattern var keeps the specialized matcher for plain
            # search/match, but everything else routes through the bridge with
            # the pattern text recovered from the pre-pass.
            if isinstance(func.value, ast.Name) \
                    and func.value.id in self._regex_var_pat:
                _pt = c_string(self._regex_var_pat[func.value.id])
                _a0 = (self.coerce_to("char*", node.args[0],
                                      self.expr(node.args[0]))
                       if node.args else None)
                # `pat.search(t, pos)` and `pat.search(t, pos, endpos)`.
                # The endpos form is what lets a caller window a scan without
                # slicing the subject, which is the only way a lookbehind at
                # `pos` can still see what precedes it.
                if func.attr in ("search", "match") and \
                        2 <= len(node.args) <= 3:
                    self._regex_dyn = True
                    return "_cre_at(%s, %s, %s, %s, %s)" % (
                        _pt, _a0,
                        self.coerce_to("int", node.args[1],
                                       self.expr(node.args[1])),
                        self._re_endpos(node, 2),
                        "1" if func.attr == "match" else "0")
                if func.attr == "finditer" and 1 <= len(node.args) <= 3:
                    self._regex_dyn = True
                    if len(node.args) == 1:
                        return "_cre_finditer(%s, %s)" % (_pt, _a0)
                    return "_cre_finditer_at(%s, %s, %s, %s)" % (
                        _pt, _a0,
                        self.coerce_to("int", node.args[1],
                                       self.expr(node.args[1])),
                        self._re_endpos(node, 2))
                if func.attr == "findall" and len(node.args) == 1:
                    self._regex_dyn = True
                    return "_cre_findall(%s, %s)" % (_pt, _a0)
                if func.attr == "sub" and len(node.args) == 2 \
                        and self._is_str_repl(node.args[0]):
                    self._regex_dyn = True
                    return "_cre_sub(%s, %s, %s)" % (
                        _pt, _a0,
                        self.coerce_to("char*", node.args[1],
                                       self.expr(node.args[1])))
                if func.attr == "sub" and len(node.args) == 2:
                    self._regex_dyn = True
                    return "_cre_sub_any(%s, %s, %s)" % (
                        _pt, self.wrap_obj(node.args[0]),
                        self.coerce_to("char*", node.args[1],
                                       self.expr(node.args[1])))
            if isinstance(func.value, ast.Name) \
                    and func.value.id in self._regex_dyn_vars:
                if func.attr in ("search", "match") and 1 <= len(node.args) <= 3:
                    anc = "1" if func.attr == "match" else "0"
                    txt = self.coerce_to("char*", node.args[0],
                                         self.expr(node.args[0]))
                    pat = self.coerce_to("char*", func.value,
                                         self.expr(func.value))
                    # pos, and optionally endpos -- the same window the
                    # constant-pattern branch above accepts. A pattern built
                    # at runtime is no less entitled to it, and cpprust builds
                    # one per lambda name before scanning a region for it.
                    if len(node.args) >= 2:
                        pos = self.coerce_to("int", node.args[1],
                                             self.expr(node.args[1]))
                        return "_cre_at(%s, %s, %s, %s, %s)" % (
                            pat, txt, pos, self._re_endpos(node, 2), anc)
                    return "_cre_dyn(%s, %s, %s)" % (pat, txt, anc)
                if func.attr == "findall" and len(node.args) == 1:
                    return "_cre_findall(%s, %s)" % (
                        self.coerce_to("char*", func.value,
                                       self.expr(func.value)),
                        self.coerce_to("char*", node.args[0],
                                       self.expr(node.args[0])))
                if func.attr == "sub" and len(node.args) == 2 \
                        and self._is_str_repl(node.args[0]):
                    return "_cre_sub(%s, %s, %s)" % (
                        self.coerce_to("char*", func.value,
                                       self.expr(func.value)),
                        self.coerce_to("char*", node.args[0],
                                       self.expr(node.args[0])),
                        self.coerce_to("char*", node.args[1],
                                       self.expr(node.args[1])))
                if func.attr == "sub" and len(node.args) == 2:
                    return "_cre_sub_any(%s, %s, %s)" % (
                        self.coerce_to("char*", func.value,
                                       self.expr(func.value)),
                        self.wrap_obj(node.args[0]),
                        self.coerce_to("char*", node.args[1],
                                       self.expr(node.args[1])))
                if func.attr == "finditer" and len(node.args) == 1:
                    return "_cre_finditer(%s, %s)" % (
                        self.coerce_to("char*", func.value,
                                       self.expr(func.value)),
                        self.coerce_to("char*", node.args[0],
                                       self.expr(node.args[0])))
            if (self._regex_ids or self._regex_dyn or self._regex_used) and \
                    (_re_recv or (
                        func.attr not in self.method_owners
                        and func.attr not in self.xmethod_owners)):
                if func.attr in ("search", "match") and len(node.args) == 1:
                    anc = "1" if func.attr == "match" else "0"
                    txt = self.coerce_to("char*", node.args[0],
                                         self.expr(node.args[0]))
                    return "_re_search(AS_INT(%s), %s, %s)" % (
                        self.wrap_obj(func.value), txt, anc)
                if func.attr == "group":
                    n_arg = node.args[0] if node.args else None
                    if n_arg is None:
                        gi = "0"
                    elif isinstance(n_arg, ast.Constant) and \
                            isinstance(n_arg.value, int):
                        gi = str(n_arg.value)
                    else:
                        gi = self.coerce_to("int", n_arg, self.expr(n_arg))
                    return "index_obj(%s, %s)" % (
                        self.wrap_obj(func.value), gi)
                if func.attr in ("start", "end") and len(node.args) <= 1:
                    n_arg = node.args[0] if node.args else None
                    if n_arg is None:
                        gi = "0"
                    elif isinstance(n_arg, ast.Constant) and \
                            isinstance(n_arg.value, int):
                        gi = str(n_arg.value)
                    else:
                        gi = self.coerce_to("int", n_arg, self.expr(n_arg))
                    return "_re_span(%s, %s, %d)" % (
                        self.wrap_obj(func.value), gi,
                        0 if func.attr == "start" else 1)
            if self.stdlib_root:
                return self._mp_method_call(func.value, func.attr, node)
            recv = self.expr(func.value)
            return "%s(%s)" % (func.attr, ", ".join([recv] + argstrs))

        # calling any *complex* obj-valued expression (e.g. the result of a
        # method that returns a function/constructor) dispatches through the
        # closure ABI; a bare Name here is an unhandled builtin -> plain call
        if isinstance(func, ast.Name) and func.id == "cls" and self.cur_class:
            ci = self.cur_class
            init = ci.methods.get("__init__")
            if init:
                defs = self.defaults_for(init, True)
                merged = self._merge_keyword_args(init, node.args, node.keywords,
                                                  True) \
                    if node.keywords else node.args
                cargs = self.coerce_args(self.init_param_ctypes(ci), merged, defs)
                cargs = self._pad_ctor_kwargs(init, cargs)
                return "OBJ_OBJ(%s_new(%s))" % (ci.csym, ", ".join(cargs))
        if not isinstance(func, ast.Name) and \
                (self.value_ctype(func) == OBJ or self.is_obj_word(func)):
            varcall = self._lower_varcall(self.expr(func), node)
            if varcall:
                return varcall
            wargs = [self.wrap_obj(a) for a in node.args]
            return self._emit_call_obj(self.expr(func), wargs)
        if isinstance(func, ast.Name) and func.id in self.scope and \
                self.scope[func.id] == OBJ and func.id not in self.func_params \
                and func.id not in self.classes:
            varcall = self._lower_varcall(cname(func.id), node)
            if varcall:
                return varcall
        return "%s(%s)" % (self.expr(func), ", ".join(argstrs))

    def _merge_keyword_args(self, fndef, args, keywords, skip_self=False):
        """Merge explicit keywords into a positional arg list by param name."""
        params = fndef.args.args[1:] if skip_self else fndef.args.args
        names = [a.arg for a in params]
        by_name = {k.arg: k.value for k in keywords if k.arg}
        merged = list(args)
        while len(merged) < len(names):
            pname = names[len(merged)]
            if pname in by_name:
                merged.append(by_name[pname])
            else:
                break
        return merged

    def _pad_ctor_kwargs(self, init, cargs):
        """Ensure **kwargs and trailing defaults are present for a constructor."""
        if init is None:
            return cargs
        cargs = list(cargs)
        pos_defs = self.defaults_for(init, True)
        ko = init.args.kwonlyargs
        kd = init.args.kw_defaults
        ko_defs = []
        for i in range(len(ko)):
            di = i - (len(ko) - len(kd))
            ko_defs.append(kd[di] if di >= 0 else None)
        all_defs = pos_defs + ko_defs
        nparams = len(init.args.args) - 1 + len(ko)
        while len(cargs) < nparams:
            i = len(cargs)
            if i < len(all_defs) and all_defs[i] is not None:
                cargs.append(self.wrap_obj(all_defs[i]))
            elif init.args.kwarg and i + 1 < len(init.args.args) and \
                    init.args.kwarg.arg == init.args.args[i + 1].arg:
                cargs.append("dict_new()")
            else:
                cargs.append("OBJ_NONE")
        return cargs

    def _lower_varcall(self, func_expr, node):
        """Lower func(*args, **kwargs) style calls on a dynamic callable."""
        star = None
        prefix = []
        for a in node.args:
            if isinstance(a, ast.Starred):
                v = a.value
                if isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add):
                    star = "obj_add(%s, %s)" % (self.expr(v.left),
                                                self.expr(v.right))
                else:
                    star = self.expr(v)
            else:
                prefix.append(self.wrap_obj(a))
        if star is None and not node.keywords:
            return None
        if star is None:
            if prefix:
                star = self._list_literal(prefix)
            else:
                star = "list_new()"
        elif prefix:
            star = "obj_add(%s, %s)" % (self._list_literal(prefix), star)
        if not self.stdlib_root and not node.keywords:
            # rpython runtime: call a dynamic callable with a runtime arg list.
            return "call_closure(%s, %s)" % (func_expr, star)
        kw = self._lower_call_kwargs(node)
        return "mp_call_obj(%s, %s, %s)" % (func_expr, star, kw)

    def _lower_call_kwargs(self, node):
        if not node.keywords:
            return "dict_new()"
        if len(node.keywords) == 1 and node.keywords[0].arg is None:
            return self.expr(node.keywords[0].value)
        parts = []
        for k in node.keywords:
            if k.arg:
                parts.append("OBJ_STR(%s)" % c_string(k.arg))
                parts.append(self.wrap_obj(k.value))
        if not parts:
            return "dict_new()"
        return self._emit_dict_of(parts)

    # ---- OO call lowering helpers ---------------------------------------

    def obj_ptr(self, node):
        s = self.expr(node)
        if self.is_obj_word(node) or self.value_ctype(node) == OBJ:
            return "AS_OBJ(%s)" % s
        return "(Obj*)(%s)" % s

    def _narrowed_recv_vt_module(self, func):
        """If `func.value` is a receiver narrowed to a concrete class whose OWN
        module dispatches `func.attr` through its vtable, return that module.
        Lets an ambiguous method name (e.g. `add`, defined in both
        ErrorCollector and ILCode) bind to the receiver's real class vtable
        rather than whichever hierarchy the generic resolver happened to pick."""
        if not (isinstance(func.value, ast.Name)
                and func.value.id in self.narrowed):
            return None
        cls = self.narrowed[func.value.id][:-1]
        ci = self.classes.get(cls) or (self.xclasses[cls][0]
                                       if cls in self.xclasses else None) \
            or self._ci_by_csym(cls)
        if ci is None:
            return None
        owner = ci.find_method_owner(func.attr)
        if owner is None and func.attr in ci.methods:
            owner = ci
        if owner is None or func.attr not in owner.methods:
            return None
        if owner.name in self.classes:      # local impl: not a cross-module call
            return None
        mod = self.xclass_module.get(owner.name) \
            or self.xclasses.get(owner.name, (None, None))[1]
        reg = self.load_xmod(mod) if mod else None
        if reg and func.attr in reg["vt"]:
            return mod
        return None

    def vtable_recv(self, recv_node):
        """Single (Obj*) cast of a receiver for TYPE()/vtable dispatch."""
        s = self.expr(recv_node)
        if self.is_obj_word(recv_node) or self.value_ctype(recv_node) == OBJ:
            return "AS_OBJ(%s)" % s
        return "(Obj*)(%s)" % s

    def _varargs_call(self, recv_node, meth, arg_nodes, fndef, pct):
        """Vtable call for a `*args` method, or None if the shape is unclear.

        `def err(self, msg, *args)` lowers to `err(Obj*, char* msg, obj args)`:
        the declared parameters keep their own types and the surplus is
        collected into one list. Returning None leaves the caller's existing
        fallback in place rather than guessing.
        """
        if fndef is None or not pct:
            return None
        named = len(fndef.args.args) - 1        # minus self
        if named < 0 or len(pct) != named + 1:  # declared params + *args
            return None
        if len(arg_nodes) < named:
            return None
        head, rest = arg_nodes[:named], arg_nodes[named:]
        try:
            cargs = self.coerce_args(pct[:named], head,
                                     self.defaults_for(fndef, True))
        except Exception:
            return None
        packed = self._list_literal([self.wrap_obj(a) for a in rest])
        xo = self.vtable_recv(recv_node)
        return "TYPE(%s)->%s(%s)" % (
            xo, vslot_name(meth), ", ".join([xo] + cargs + [packed]))

    def _mp_method_call_args(self, recv, attr, arg_nodes, fndef=None):
        args = list(arg_nodes)
        if fndef is not None:
            params = fndef.args.args[1:]
            defs = self.defaults_for(fndef, True)
            for i in range(len(args), len(params)):
                if i < len(defs) and defs[i] is not None:
                    args.append(defs[i])
        wrapped = [self.wrap_obj(a) for a in args]
        r = self.wrap_obj(recv) if not isinstance(recv, str) else recv
        sa = c_string(attr)
        n = len(wrapped)
        if n == 0:
            return "mp_call_method(%s, %s, 0)" % (r, sa)
        if n == 1:
            return "mp_call_method(%s, %s, 1, %s)" % (r, sa, wrapped[0])
        if n == 2:
            return "mp_call_method(%s, %s, 2, %s, %s)" % (
                r, sa, wrapped[0], wrapped[1])
        return "mp_call_method(%s, %s, %d, %s)" % (
            r, sa, n, ", ".join(wrapped))

    def _method_has_varargs(self, fndef):
        if fndef is None:
            return True
        return bool(fndef.args.vararg or fndef.args.kwarg or
                    fndef.args.kwonlyargs)

    def _format_direct_method_call(self, owner, m, recv_node, arg_nodes):
        """Direct call to a concrete (non-vtable) class method."""
        # A local class can inherit from an imported base; a method that
        # resolves to that base is emitted as a direct call to the imported
        # symbol, which needs an extern prototype (else gcc assumes int).
        if owner.name not in self.classes:
            if owner.name in self.ambiguous:
                self.used_xmethods_csym[(owner.csym, m.name)] = self._c_ret(m)
            else:
                self.used_xmethods.setdefault((owner.name, m.name), self._c_ret(m))
        recv = self._class_ptr_expr(recv_node, owner.csym)
        pct = [self._imported_arg_ctype(m, a) for a in m.args.args[1:]]
        n_named = len(pct)
        if m.args.vararg and m.name in VTABLE_METHODS:
            extra = arg_nodes[n_named:]
            wrapped = [self.coerce_to(OBJ, a, self.expr(a)) for a in extra]
            parts = [recv]
            if wrapped:
                parts.append(self._list_literal(wrapped))
            else:
                parts.append("list_new()")
            return "%s_%s(%s)" % (owner.csym, m.name, ", ".join(parts))
        if m.args.vararg and m.name not in VTABLE_METHODS:
            extra = arg_nodes[n_named:]
            wrapped = [self.coerce_to(OBJ, a, self.expr(a)) for a in extra]
            parts = [recv]
            if n_named:
                parts.extend(self.coerce_args(pct, arg_nodes[:n_named]))
            parts.append(str(len(wrapped)))
            parts.extend(wrapped)
            return "%s_%s(%s)" % (owner.csym, m.name, ", ".join(parts))
        cargs = self.coerce_args(pct, arg_nodes, self.defaults_for(m, True))
        return "%s_%s(%s%s)" % (
            owner.csym, m.name, recv,
            (", " + ", ".join(cargs)) if cargs else "")

    def _call_args_with_kw(self, node, meth, recv_node=None):
        """Positional arguments for `node`, with keywords placed by name.

        py2c matches arguments positionally, so a keyword argument used to be
        dropped on the floor: `p.parse_impl(out, owner_override=mangled)`
        compiled to `parse_impl(p, out, OBJ_NONE)`, and crust then parsed every
        generic instantiation under the *template's* name -- so `Vec<i32>`
        registered its methods as `Vec_int_*` and looked them up as `Vec_*`.

        A keyword whose position cannot be determined is reported rather than
        discarded; silently losing an argument is what made this cost a day.
        """
        kws = [k for k in getattr(node, "keywords", ()) if k.arg is not None]
        if not kws:
            return node.args
        # A method name can be defined by many classes with different
        # signatures -- `make_il` is on a dozen node types, and only
        # `Compound.make_il` takes `no_scope`. Take the first definition that
        # actually declares every keyword used here, rather than whichever
        # class happens to come first: picking wrong meant rejecting a valid
        # call, and a rejected expression becomes `OBJ_NONE`, which silently
        # deleted the `make_il` that generates every function body.
        wanted = set(k.arg for k in kws)
        fndef = None
        cands = []
        if recv_node is not None:
            for ci in self.method_owners.get(meth, []):
                d = ci.methods.get(meth)
                if d is not None:
                    cands.append(d)
        own = self.func_nodes.get(meth)
        if own is not None:
            cands.append(own)
        for d in cands:
            have = set(a.arg for a in d.args.args) | \
                set(a.arg for a in d.args.kwonlyargs)
            if wanted <= have:
                fndef = d
                break
        if fndef is None and cands:
            fndef = cands[0]
        if fndef is None:
            raise Py2CUnsupported(
                "%s:%s: keyword argument(s) %s to `%s`, whose definition is "
                "not visible here" % (getattr(self, "src_name", "?"),
                                      node.lineno,
                                      ", ".join(k.arg for k in kws), meth))
        names = [a.arg for a in fndef.args.args]
        if names and names[0] == "self":
            names = names[1:]
        names += [a.arg for a in fndef.args.kwonlyargs]
        defaults = self.defaults_for(fndef, True) or []
        # Only fill *trailing* keywords -- ones that sit at or after the last
        # positional argument. Rewriting an earlier slot means synthesising
        # the defaults in between, and `defaults_for` is indexed differently
        # from `names` here: doing that produced a native compiler that built
        # cleanly and then returned 0 from every program. A keyword that
        # cannot be placed this way is reported, not guessed at.
        out = list(node.args)
        for k in kws:
            if k.arg not in names:
                raise Py2CUnsupported(
                    "%s:%s: `%s` has no parameter named `%s`"
                    % (getattr(self, "src_name", "?"), node.lineno, meth,
                       k.arg))
            idx = names.index(k.arg)
            if idx < len(out):
                raise Py2CUnsupported(
                    "%s:%s: keyword `%s` to `%s` also given positionally"
                    % (getattr(self, "src_name", "?"), node.lineno, k.arg,
                       meth))
            if idx != len(out):
                raise Py2CUnsupported(
                    "%s:%s: keyword `%s` to `%s` skips parameter(s); pass "
                    "them positionally"
                    % (getattr(self, "src_name", "?"), node.lineno, k.arg,
                       meth))
            out.append(k.value)
        return out

    def vcall(self, recv_node, meth, arg_nodes):
        # POD class instance -> static dispatch (direct call, no vtable).
        rct = self.value_ctype(recv_node)
        concrete_ci = None
        if isinstance(rct, str) and rct.endswith("*"):
            cls = rct[:-1]
            concrete_ci = self.classes.get(cls) or next(
                (c for c in self.classes.values() if c.csym == cls), None)
            if concrete_ci is not None and concrete_ci.csym in self._pod_set \
                    and meth in concrete_ci.methods:
                fn = concrete_ci.methods[meth]
                pct = [self.arg_ctype_q(fn, a) for a in fn.args.args[1:]]
                defs = self.defaults_for(fn, True)
                cargs = self.coerce_args(pct, arg_nodes, defs)
                recv = self.expr(recv_node)
                return "%s_%s(%s)" % (concrete_ci.csym, method_cname(meth),
                                      ", ".join([recv] + cargs))
        _, pct, fndef = self.method_proto(meth)
        # `meth` ambiguous across UNRELATED classes with different arities (e.g.
        # DeclInfo.get_storage/3 vs Declaration.get_storage/2): the global proto
        # picked above can be the wrong overload, and there is no shared vtable
        # slot to index. When the receiver's own concrete (leaf) class defines
        # `meth` with a different parameter count, dispatch straight to it --
        # otherwise the call degrades to a runtime by-name lookup the receiver's
        # type doesn't answer, and crashes.
        if concrete_ci is not None and meth in concrete_ci.methods and \
                self._class_is_leaf(concrete_ci.name) and \
                concrete_ci.name not in self._project_subclassed_cached():
            own = concrete_ci.methods[meth]
            # Only when the receiver's own method is WIDER than the canonical
            # slot: that means `meth` is ambiguous across unrelated classes and
            # the global proto belongs to a sibling with fewer params, so no
            # shared slot fits this receiver -> call it directly. A NARROWER own
            # method is an ordinary override whose impl is padded to the slot
            # width; it must go through the (default-filling) vtable instead.
            if len(own.args.args) - 1 > len(pct):
                ownpct = [self.arg_ctype_q(own, a) for a in own.args.args[1:]]
                defs = self.defaults_for(own, True)
                cargs = self.coerce_args(ownpct, arg_nodes, defs)
                recv = self.expr(recv_node)
                return "%s_%s(%s)" % (concrete_ci.csym, method_cname(meth),
                                      ", ".join([recv] + cargs))
        if self.stdlib_root:
            return self._mp_method_call_args(recv_node, meth, arg_nodes, fndef)
        if self._method_has_varargs(fndef):
            # A `*args` method compiles to a normal C function whose last
            # parameter is the collected list, so it can be reached through
            # the vtable like any other -- pack the surplus arguments and
            # call it.
            #
            # The old path went through `mp_call_method`, which resolves the
            # name with `rt_getattr`: that finds *data* attributes holding a
            # closure, never a method. It returned None and the call
            # dereferenced it, so `self.err(...)` -- crust's own diagnostic
            # helper -- segfaulted the compiler instead of reporting the
            # error it was called to report.
            packed = self._varargs_call(recv_node, meth, arg_nodes, fndef, pct)
            if packed is not None:
                return packed
            return self._mp_method_call_args(recv_node, meth, arg_nodes, fndef)
        if len(arg_nodes) > len(pct):
            return self._mp_method_call_args(recv_node, meth, arg_nodes, fndef)
        xo = self.vtable_recv(recv_node)
        defs = self.defaults_for(fndef, True) if fndef else None
        cargs = self.coerce_args(pct, arg_nodes, defs)
        return "TYPE(%s)->%s(%s)" % (xo, vslot_name(meth), ", ".join([xo] + cargs))

    def _resolve_init(self, ci):
        """The __init__ FunctionDef for `ci`, inherited from a base when the
        class defines none of its own (so constructor calls to a subclass that
        only inherits its initializer still coerce args to the right types)."""
        init = ci.methods.get("__init__")
        if not init:
            owner = ci.find_method_owner("__init__")
            if owner is not None:
                init = owner.methods.get("__init__")
        return init

    def init_param_ctypes(self, ci):
        init = self._resolve_init(ci)
        if not init:
            return []
        # Mirror `_init_param_list`'s per-arg type decision exactly, so a
        # constructor *call* coerces each argument to the same C type the
        # constructor *definition* declares. Using the bare `arg_ctype` here
        # collapsed `list[float]` / `dict[...]` params to the boxed `obj`, so a
        # call boxed a typed list (`Line_new(OBJ_OBJ(xs), ...)`) into a param
        # the definition typed as `_tlist_double*` -> a pointer/obj ABI clash.
        out = []
        for a in init.args.args[1:]:
            ct = self.arg_ctype_q(init, a)
            if init.name in VTABLE_METHODS and self._is_class_ptr(ct) \
                    and id(init) not in self._pod_method_nodes:
                ct = OBJ
            out.append(ct)
        return out

    def coerce_args(self, param_ctypes, arg_nodes, default_nodes=None):
        out = []
        n = len(param_ctypes) if param_ctypes else len(arg_nodes)
        for i in range(n):
            target = param_ctypes[i] if i < len(param_ctypes) else None
            if i < len(arg_nodes):
                a = arg_nodes[i]
            elif default_nodes and i < len(default_nodes) and \
                    default_nodes[i] is not None:
                a = default_nodes[i]
            else:
                break                       # no more provided args / defaults
            out.append(self.coerce_to(target, a, self.expr(a)))
        # any extra provided args beyond known params (e.g. *args) pass through
        for i in range(n, len(arg_nodes)):
            out.append(self.expr(arg_nodes[i]))
        return out

    def defaults_for(self, fndef, skip_self):
        """List aligned to params: default value node or None."""
        params = fndef.args.args[1:] if skip_self else fndef.args.args
        defs = fndef.args.defaults
        nd = len(defs)
        n = len(params)
        return [None] * (n - nd) + list(defs)

    def _local_method_accepts_argc(self, attr, argc):
        """True if some local class declares a method `attr` callable with
        `argc` positional args (beyond self). Used to disambiguate a builtin
        container method (list/dict `.pop()`, etc.) from a user method of the
        same name: if no user method can take the given argument count, the
        call must be the builtin."""
        for ci in self.method_owners.get(attr, []):
            fn = ci.methods.get(attr)
            if not fn:
                continue
            params = fn.args.args[1:]              # drop self
            lo = len(params) - len(fn.args.defaults)
            hi = (1 << 30) if fn.args.vararg else len(params)
            if lo <= argc <= hi:
                return True
        return False

    def method_on_instance(self, func, node):
        """If `func.value` is a concrete class instance (local or imported) and
        the class declares `func.attr` as a method, emit the direct call."""
        bt = self.value_ctype(func.value)
        if not (bt and bt.endswith("*") and bt != OBJ):
            return None
        cls = bt[:-1]
        if cls in self.classes:
            ci = self.classes[cls]
            owner = ci.find_method_owner(func.attr)
            if owner is None:
                return None
            if func.attr in owner.static_methods:    # @staticmethod: no receiver
                m = owner.methods.get(func.attr)
                pct = [arg_ctype(m, a) for a in m.args.args] if m else []
                defs = self.defaults_for(m, False) if m else None
                cargs = self.coerce_args(pct, node.args, defs)
                return "%s_%s(%s)" % (owner.csym, func.attr, ", ".join(cargs))
            if func.attr in VTABLE_METHODS:
                return self.vcall(func.value, func.attr,
                                  self._call_args_with_kw(node, func.attr, func.value))
            m = owner.methods.get(func.attr)
            if m is not None:
                return self._format_direct_method_call(
                    owner, m, func.value, node.args)
        if cls in self.xclasses or self._ci_by_csym(cls) is not None:
            if cls in self.xclasses:
                ci, modname = self.xclasses[cls]
            else:
                ci = self._ci_by_csym(cls)
                modname = getattr(ci, "defmod", None)
            owner = ci.find_method_owner(func.attr)
            if owner is None:
                return None
            if func.attr in owner.static_methods:    # @staticmethod: no receiver
                m = owner.methods.get(func.attr)
                ret = self._c_ret(m) if m else OBJ
                self.used_xmethods[(owner.name, func.attr)] = ret
                args = [self.expr(a) for a in node.args]
                return "%s_%s(%s)" % (owner.csym, func.attr, ", ".join(args))
            m = owner.methods.get(func.attr)
            if m is not None:
                ret = self._c_ret(m)
                self.used_xmethods[(owner.name, func.attr)] = ret
                return self._format_direct_method_call(
                    owner, m, func.value, node.args)
        return None

    BUILTIN_TYPE_TAGS = {"str": "T_STR", "bytes": "T_STR", "bytearray": "T_OBJ",
                         "int": "T_INT",
                         "bool": "T_BOOL", "list": "T_LIST", "tuple": "T_LIST",
                         "set": "T_LIST", "frozenset": "T_LIST",
                         "dict": "T_DICT"}

    def lower_isinstance(self, val_node, cls_node):
        if isinstance(cls_node, ast.Name) and \
                cls_node.id in getattr(self, "tuple_type_globals", {}):
            parts = [self.lower_isinstance(val_node, ast.Name(id=t))
                     for t in self.tuple_type_globals[cls_node.id]]
            return "(" + " || ".join(parts) + ")"
        if isinstance(cls_node, (ast.Tuple, ast.List)):
            parts = [self.lower_isinstance(val_node, e) for e in cls_node.elts]
            return "(" + " || ".join(parts) + ")"
        # builtin types -> Tier-2 tag test
        if isinstance(cls_node, ast.Name) and \
                cls_node.id in self.BUILTIN_TYPE_TAGS and \
                cls_node.id not in self.classes:
            tag = self.BUILTIN_TYPE_TAGS[cls_node.id]
            return "((%s).tag == %s)" % (self.wrap_obj(val_node), tag)
        type_sym = self._type_symbol(cls_node)
        if self.is_obj_word(val_node) or self.value_ctype(val_node) == OBJ:
            return "OBJ_ISINST(%s, %s)" % (self.expr(val_node), type_sym)
        return "isinstance_of((Obj*)(%s), %s)" % (self.expr(val_node),
                                                  type_sym)

    def _resolve_class_ref(self, ref):
        """Resolve a class reference (`Name`, `alias.Cls`, or a from-imported
        name) to its ClassInfo, preferring the exact alias/re-export resolution
        over the ambiguous bare-name registry (which can pick the wrong
        same-named class). Returns None if `ref` is not a known class."""
        clsname = None
        resolved = None
        if isinstance(ref, ast.Name):
            clsname = ref.id
            if clsname not in self.classes and clsname in self.from_imports:
                kind, info = self.xref(clsname, self.from_imports[clsname])
                if kind == "class":
                    resolved = info
        elif isinstance(ref, ast.Attribute) and \
                isinstance(ref.value, ast.Name) and \
                ref.value.id in self.import_alias:
            clsname = ref.attr
            kind, info = self.xref(clsname, self.import_alias[ref.value.id])
            if kind == "class":
                resolved = info
        if clsname:
            return self.classes.get(clsname) or resolved or \
                (self.xclasses[clsname][0] if clsname in self.xclasses else None)
        return None

    def _type_symbol(self, cls_node):
        """`&Cls_type` for a class reference (local, alias.Cls, or imported)."""
        ci = self._resolve_class_ref(cls_node)
        if ci is not None:
            self._ref_xclass(ci, body=True, typeinfo=True)
            return "&%s_type" % ci.csym
        return "NULL"

    def lower_any_all(self, fn, arg):
        """any(...)/all(...) as a GCC statement-expression loop."""
        self.loop_n += 1
        it, idx = "_it%d" % self.loop_n, "_k%d" % self.loop_n
        init = "false" if fn == "any" else "true"
        hit = "true" if fn == "any" else "false"
        if isinstance(arg, ast.GeneratorExp) and len(arg.generators) == 1:
            gen = arg.generators[0]
            saved = dict(self.scope)
            binds = self.bind_target(gen.target,
                                     "index_obj(%s, %s)" % (it, idx),
                                     force_decl=True)
            conds = " && ".join("(%s)" % self.bool_expr(c) for c in gen.ifs)
            pred = self.bool_expr(arg.elt)
            if fn == "all":
                pred = "!(%s)" % pred
            guard = ("if (%s) " % conds) if conds else ""
            body = "%s %sif (%s) { _r = %s; break; }" % (
                " ".join(binds), guard, pred, hit)
            # Boxed for the same reason as the plain-iterable branch below:
            # the temp is declared `obj`, so a `char*` iterable has to be
            # wrapped or the initialiser is a type error.
            src = self.wrap_obj(gen.iter)
            self.scope = saved
        else:
            pred = "truthy(_e)"
            if fn == "all":
                pred = "!truthy(_e)"
            body = "obj _e = index_obj(%s, %s); if (%s) { _r = %s; break; }" \
                % (it, idx, pred, hit)
            # Boxed: the temp below is declared `obj`, and an iterable that is
            # already a C value -- a `char*` string, most often -- has to be
            # wrapped or the initialiser is a type error. Every sibling loop
            # lowering uses `wrap_obj` here; this one did not.
            src = self.wrap_obj(arg)
        return ("({ bool _r = %s; obj %s = %s; long _n%d = pylen(%s); "
                "for (long %s = 0; %s < _n%d; %s++) { %s } _r; })") % (
            init, it, src, self.loop_n, it, idx, idx, self.loop_n, idx, body)

    def unwrap_obj(self, target, rendered):
        """Coerce a rendered obj expression to the `target` C type."""
        if not target or target == OBJ:
            return rendered
        if target == "int":
            return "AS_INT(%s)" % rendered
        if target == "bool":
            return "truthy(%s)" % rendered
        if target == "char*":
            return "AS_STR(%s)" % rendered
        if target.endswith("*"):
            return "(%s)AS_OBJ(%s)" % (target, rendered)
        return rendered

    def target_ctype(self, tgt):
        """Declared C type of an assignment target, or None if unknown."""
        if isinstance(tgt, ast.Name):
            if tgt.id in self.scope:
                return self.scope[tgt.id]
            if tgt.id in self.singleton_names:
                return self.singleton_names[tgt.id] + "*"
        if isinstance(tgt, ast.Attribute):
            if isinstance(tgt.value, ast.Name) and tgt.value.id == "self" \
                    and self.cur_class:
                return self.cur_class.field_ctype(tgt.attr)
            bt = self.value_ctype(tgt.value)
            if bt and bt.endswith("*") and bt != OBJ:
                cls = bt[:-1]
                if cls in self.xclasses:
                    self.xstructs_needed.add(cls)
                ci = self.classes.get(cls) or \
                    (self.xclasses[cls][0] if cls in self.xclasses else None) or \
                    self._ci_by_csym(cls)   # bt may be a module-qualified csym
                if ci:
                    return ci.field_ctype(tgt.attr)
            if self.is_obj_word(tgt.value) or bt == OBJ:
                owner = self.resolve_attr_owner(tgt.attr)
                if owner:
                    return owner.field_ctype(tgt.attr)
        return None

    @staticmethod
    def _const_true(node):
        """Is `node` a literal True? capture_output=some_flag is not lowered,
        because the emitted call has to know at translation time whether the
        pipes exist."""
        return isinstance(node, ast.Constant) and node.value is True

    def _is_str_repl(self, node):
        """Is this replacement an actual string?

        A callable repl needs the match object handed back into guest code, so
        it must keep warning rather than be lowered. Excluding ast.Lambda alone
        was not enough -- cpprust passes a *named* function, which reached
        _cre_sub as a closure and failed to compile.
        """
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return True
        return self.value_ctype(node) == "char*"

    def _re_intern(self, pattern):
        """Register a static regex `pattern` and return its matcher id.

        Two tiers. A pattern inside the narrow subset `regex_parse` accepts is
        lowered to a specialized C matcher -- no engine, no bytecode, just the
        comparisons that pattern needs. Anything else (alternation, `(?:...)`,
        `\\b`, quantified groups, ...) is handed to the crust_re VM, which is
        linked into the generated program only when tier 2 is actually used.

        Whether crust_re accepts the pattern is deliberately NOT re-decided
        here: mirroring its parser in Python is exactly the kind of duplicated
        rule set that drifts and then lies. The generated program compiles every
        tier-2 pattern during startup and aborts with the pattern and the
        engine's own message if one is rejected. Patterns are compile-time
        constants, so that failure is deterministic and fires on the first run.
        """
        if pattern in self._regex_ids:
            return self._regex_ids[pattern]
        pid = len(self._regex_ids)
        parsed = regex_parse(pattern)
        self._regex_ids[pattern] = pid
        if parsed is None:
            self._regex_vm[pid] = pattern
        else:
            self._regex_parsed[pid] = parsed
        return pid

    def _regex_vm_prelude(self):
        """Engine source, pattern table, startup check and obj bridge for the
        tier-2 (crust_re) patterns. Emitted only when tier 2 is used, so a
        program whose patterns all fit the specializer links no engine."""
        # Loaded by path: py2c is run from many working directories, and
        # rpy_lib is not necessarily on sys.path.
        _src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "rpy_lib", "crust_re_src.py")
        _ns = {}
        with open(_src, encoding="utf-8") as _f:
            exec(compile(_f.read(), _src, "exec"), _ns)
        crust_re_src = type("m", (), _ns)

        pats = sorted(self._regex_vm)
        self._regex_vm_slot = {pid: i for i, pid in enumerate(pats)}
        out = []
        out.append("/* ---- crust_re: regex VM for patterns outside the "
                   "specializer's subset ---- */")
        # The engine is inlined into every module that needs it, so two such
        # modules in one program define the same symbols twice and the link
        # fails -- which is exactly what `cpprust.py` plus the `cpp_auto.py`
        # it imports did. Give this copy module-local names. A #define is
        # enough because the preprocessor matches whole identifiers, so
        # `crust_re_exec` is renamed without disturbing the `crust_re` struct
        # tag, and the header and source below agree because both are
        # rewritten by the same defines.
        _pub = sorted(set(re.findall(r"\b(crust_re_[a-z_]+)\s*\(",
                                     crust_re_src.HEADER)))
        if _pub:
            out.append("/* module-local names: one program may inline this "
                       "engine more than once. */")
            for _n in _pub:
                out.append("#define %s %s__%s" % (_n, self.modname, _n))
        out.extend(crust_re_src.HEADER.splitlines())
        out.extend(crust_re_src.SOURCE.splitlines())
        out.append("")
        n = len(pats)
        out.append("#define _CRE_N %d" % max(n, 1))
        out.append("#define _CRE_ARENA 16384")
        out.append("static const char* _cre_pat[_CRE_N] = {")
        for pid in pats:
            out.append("    %s," % c_string(self._regex_vm[pid]))
        if not pats:
            out.append("    \"\",")   # only dynamic patterns in this program
        out.append("};")
        out.append("static char _cre_arena[_CRE_N][_CRE_ARENA];")
        out.append("static crust_re* _cre_h[_CRE_N];")
        out.append("static int _cre_ready = 0;")
        out.append("")
        # Compile-on-first-use, but check every pattern at once: a pattern the
        # engine rejects must not wait for its own call site to be reached.
        out.append("static void _cre_init(void) {")
        out.append("    int i;")
        out.append("    if (_cre_ready) return;")
        out.append("    _cre_ready = 1;")
        out.append("    for (i = 0; i < %d; i++) {" % n)
        out.append("        const char* e = 0;")
        out.append("        _cre_h[i] = crust_re_compile(_cre_pat[i], _cre_arena[i],")
        out.append("                                     _CRE_ARENA, &e);")
        out.append("        if (!_cre_h[i]) {")
        out.append("            fprintf(stderr, \"regex: cannot compile %s: %s\\n\",")
        out.append("                    _cre_pat[i], e ? e : \"unknown\");")
        out.append("            abort();")
        out.append("        }")
        out.append("    }")
        out.append("}")
        out.append("")
        # Same result shape as the specialized matchers: a LIST of captured
        # strings, so `if m:` and `m.group(n)` need no new runtime type.
        out.append("static obj _cre_run(int slot, char* t, int anc) {")
        out.append("    int caps[128];")
        out.append("    int ng, i, rc;")
        out.append("    obj m;")
        out.append("    if (!t) return OBJ_NONE;")
        out.append("    _cre_init();")
        out.append("    ng = crust_re_ngroups(_cre_h[slot]);")
        out.append("    if (2 * (ng + 1) > 128) return OBJ_NONE;")
        out.append("    rc = crust_re_exec(_cre_h[slot], t, strlen(t), anc,")
        out.append("                       caps, 2 * (ng + 1));")
        out.append("    if (rc != CRUST_RE_MATCH) return OBJ_NONE;")
        out.append("    m = list_new();")
        out.append("    for (i = 0; i <= ng; i++)")
        out.append("        list_append(m, _re_slice(t, caps[2*i], caps[2*i+1]));")
        out.append("    /* spans after the strings: see _re_emit_build */")
        out.append("    for (i = 0; i <= ng; i++) {")
        out.append("        list_append(m, OBJ_INT(caps[2*i]));")
        out.append("        list_append(m, OBJ_INT(caps[2*i+1]));")
        out.append("    }")
        out.append("    return m;")
        out.append("}")
        out.extend(self._regex_dyn_bridge())
        # findall/sub are built on _cre_dyn_get, so they must not be emitted
        # for a program that only has constant patterns -- the bridge that
        # defines it would be absent and the link would fail.
        if self._regex_dyn:
            out.extend(self._regex_api())
        return out

    def _regex_api(self):
        """findall / sub over the dynamic bridge.

        Both walk the subject with repeated anchored-at-offset matching rather
        than re-scanning from zero, so an unanchored pattern over a long string
        stays linear in the number of matches. An empty match advances one byte,
        matching CPython, so `re.sub("x*", "-", "ab")` terminates."""
        out = []
        out.append("")
        out.append("/* ---- re.findall / re.sub ---- */")
        out.append("static obj _cre_findall(char* pat, char* t) {")
        out.append("    obj res = list_new();")
        out.append("    int caps[128];")
        out.append("    crust_re* h;")
        out.append("    long off = 0, len;")
        out.append("    int ng, rc;")
        out.append("    if (!pat || !t) return res;")
        out.append("    h = _cre_dyn_get(pat);")
        out.append("    ng = crust_re_ngroups(h);")
        out.append("    len = (long)strlen(t);")
        out.append("    while (off <= len) {")
        out.append("        rc = crust_re_exec(h, t + off, (size_t)(len - off), 0,")
        out.append("                           caps, 2 * (ng + 1));")
        out.append("        if (rc != CRUST_RE_MATCH) break;")
        out.append("        /* findall yields group 1 when there is exactly one")
        out.append("         * group, else the whole match -- as CPython does. */")
        out.append("        if (ng == 1)")
        out.append("            list_append(res, _re_slice(t, off + caps[2], off + caps[3]));")
        out.append("        else")
        out.append("            list_append(res, _re_slice(t, off + caps[0], off + caps[1]));")
        out.append("        if (caps[1] == caps[0]) off += caps[1] + 1;")
        out.append("        else off += caps[1];")
        out.append("    }")
        out.append("    return res;")
        out.append("}")
        out.append("")
        out.append("/* pat.search(text, pos): match from an offset. Spans are")
        out.append(" * reported relative to the whole subject, as CPython does,")
        out.append(" * so a caller stepping through with .end() keeps working. */")
        out.append("/* `end` is CPython's endpos: -1 means the whole subject. */")
        out.append("static obj _cre_at(char* pat, char* t, int pos, int end, int anc) {")
        out.append("    int caps[128];")
        out.append("    crust_re* h;")
        out.append("    long len, i;")
        out.append("    int ng, rc;")
        out.append("    obj m;")
        out.append("    if (!pat || !t) return OBJ_NONE;")
        out.append("    len = (long)strlen(t);")
        out.append("    if (end >= 0 && (long)end < len) len = (long)end;")
        out.append("    if (pos < 0) pos = 0;")
        out.append("    if (pos > len) return OBJ_NONE;")
        out.append("    h = _cre_dyn_get(pat);")
        out.append("    ng = crust_re_ngroups(h);")
        out.append("    if (2 * (ng + 1) > 128) return OBJ_NONE;")
        # `crust_re_exec_from`, not `crust_re_exec` over `t + pos`: the
        # engine has to keep the whole subject so a lookbehind at `pos` can
        # read what precedes it. Slicing made `(?<=;)x` against ";x" from
        # offset 1 report no match where CPython matches -- a wrong answer,
        # not a missing feature, and `tools/cpprust.py` windows its scans
        # precisely so the lookbehind sees the character before the window.
        # Offsets now come back absolute, so the `pos +` adjustments go too.
        out.append("    rc = crust_re_exec_from(h, t, (size_t)len, pos, anc,")
        out.append("                            caps, 2 * (ng + 1));")
        out.append("    if (rc != CRUST_RE_MATCH) return OBJ_NONE;")
        out.append("    m = list_new();")
        out.append("    for (i = 0; i <= ng; i++)")
        out.append("        list_append(m, _re_slice(t, caps[2*i], caps[2*i+1]));")
        out.append("    for (i = 0; i <= ng; i++) {")
        out.append("        list_append(m, OBJ_INT(caps[2*i]));")
        out.append("        list_append(m, OBJ_INT(caps[2*i+1]));")
        out.append("    }")
        out.append("    return m;")
        out.append("}")
        out.append("")
        out.append("static obj _cre_finditer_at(char* pat, char* t, int pos, int end);")
        out.append("")
        out.append("/* re.finditer -> a LIST of match values, so `for m in ...`")
        out.append(" * iterates it with no iterator protocol needed. */")
        out.append("static obj _cre_finditer(char* pat, char* t) {")
        out.append("    return _cre_finditer_at(pat, t, 0, -1);")
        out.append("}")
        out.append("")
        out.append("/* finditer over text[pos..end), with the whole subject")
        out.append(" * still visible to lookbehind -- see crust_re_exec_from. */")
        out.append("static obj _cre_finditer_at(char* pat, char* t, int pos, int end) {")
        out.append("    obj res = list_new();")
        out.append("    long off, len;")
        out.append("    if (!pat || !t) return res;")
        out.append("    len = (long)strlen(t);")
        out.append("    if (end >= 0 && (long)end < len) len = (long)end;")
        out.append("    off = pos < 0 ? 0 : (long)pos;")
        out.append("    while (off <= len) {")
        out.append("        obj m = _cre_at(pat, t, (int)off, end, 0);")
        out.append("        long e, s;")
        out.append("        if (IS_NONE(m)) break;")
        out.append("        list_append(res, m);")
        out.append("        s = _re_span(m, 0, 0);")
        out.append("        e = _re_span(m, 0, 1);")
        out.append("        off = (e == s) ? e + 1 : e;")
        out.append("    }")
        out.append("    return res;")
        out.append("}")
        out.append("")
        out.append("static char* _cre_sub(char* pat, char* rep, char* t) {")
        out.append("    obj parts = list_new();")
        out.append("    int caps[128];")
        out.append("    crust_re* h;")
        out.append("    long off = 0, len;")
        out.append("    int ng, rc;")
        out.append("    if (!pat || !t) return \"\";")
        out.append("    if (!rep) rep = \"\";")
        out.append("    h = _cre_dyn_get(pat);")
        out.append("    ng = crust_re_ngroups(h);")
        out.append("    len = (long)strlen(t);")
        out.append("    while (off <= len) {")
        out.append("        rc = crust_re_exec(h, t + off, (size_t)(len - off), 0,")
        out.append("                           caps, 2 * (ng + 1));")
        out.append("        if (rc != CRUST_RE_MATCH) break;")
        out.append("        list_append(parts, _re_slice(t, off, off + caps[0]));")
        out.append("        list_append(parts, OBJ_STR(rep));")
        out.append("        if (caps[1] == caps[0]) {")
        out.append("            /* zero-width: emit the byte we step over, or the")
        out.append("             * loop would drop it. */")
        out.append("            if (off + caps[1] < len)")
        out.append("                list_append(parts, _re_slice(t, off + caps[1], off + caps[1] + 1));")
        out.append("            off += caps[1] + 1;")
        out.append("        } else {")
        out.append("            off += caps[1];")
        out.append("        }")
        out.append("    }")
        out.append("    if (off <= len) list_append(parts, _re_slice(t, off, len));")
        out.append("    return pyjoin(\"\", parts);")
        out.append("}")
        out.append("")
        out.append("/* `re.sub` where the replacement is a *value* rather than a")
        out.append(" * literal: either a string, or a function taking the match.")
        out.append(" * Which one is decided here rather than at translation time,")
        out.append(" * because a repl reached through a variable can be either and")
        out.append(" * the tag says so exactly. The match handed to the function is")
        out.append(" * the same list every other frontend builds, so `m.group(1)`")
        out.append(" * inside it works like anywhere else. */")
        out.append("static char* _cre_sub_any(char* pat, obj rep, char* t) {")
        out.append("    obj parts = list_new();")
        out.append("    long off = 0, len, s, e;")
        out.append("    if (!pat || !t) return \"\";")
        out.append("    if (rep.tag == T_STR) return _cre_sub(pat, AS_STR(rep), t);")
        out.append("    len = (long)strlen(t);")
        out.append("    while (off <= len) {")
        out.append("        obj m = _cre_at(pat, t, (int)off, -1, 0);")
        out.append("        obj r;")
        out.append("        if (IS_NONE(m)) break;")
        out.append("        s = _re_span(m, 0, 0);")
        out.append("        e = _re_span(m, 0, 1);")
        out.append("        list_append(parts, _re_slice(t, off, s));")
        out.append("        r = call_obj_a(rep, &m, 1);")
        out.append("        list_append(parts, OBJ_STR(AS_STR(r)));")
        out.append("        if (e == s) {")
        out.append("            /* zero-width: emit the byte stepped over, or the")
        out.append("             * loop would drop it. */")
        out.append("            if (e < len) list_append(parts, _re_slice(t, e, e + 1));")
        out.append("            off = e + 1;")
        out.append("        } else {")
        out.append("            off = e;")
        out.append("        }")
        out.append("    }")
        out.append("    if (off <= len) list_append(parts, _re_slice(t, off, len));")
        out.append("    return pyjoin(\"\", parts);")
        out.append("}")
        return out

    def _regex_dyn_bridge(self):
        """`_cre_dyn(pat, text, anchored)` for patterns only known at runtime.

        Compiled on first use behind a small direct-mapped cache, so a loop
        matching the same pattern repeatedly compiles it once. A pattern the
        engine rejects aborts naming the pattern and the message: there is no
        exception channel here, and returning "no match" would turn a broken
        pattern into a silently wrong answer."""
        if not self._regex_dyn:
            return []
        out = []
        out.append("")
        out.append("/* ---- crust_re: runtime-valued patterns ---- */")
        out.append("#define _CRED_N 8")
        out.append("#define _CRED_PATMAX 512")
        out.append("static char _cred_arena[_CRED_N][_CRE_ARENA];")
        out.append("static char _cred_pat[_CRED_N][_CRED_PATMAX];")
        out.append("static crust_re* _cred_h[_CRED_N];")
        out.append("static int _cred_fill = 0;")
        out.append("")
        out.append("static crust_re* _cre_dyn_get(const char* pat) {")
        out.append("    int i;")
        out.append("    const char* e = 0;")
        out.append("    size_t n = strlen(pat);")
        out.append("    for (i = 0; i < _cred_fill; i++)")
        out.append("        if (strcmp(_cred_pat[i], pat) == 0) return _cred_h[i];")
        out.append("    if (n + 1 > _CRED_PATMAX) {")
        out.append("        fprintf(stderr, \"regex: pattern too long (%lu bytes)\\n\",")
        out.append("                (unsigned long)n);")
        out.append("        abort();")
        out.append("    }")
        out.append("    /* Cache full: recompile into the last slot each time rather")
        out.append("     * than evicting wrongly. Correct, just slower. */")
        out.append("    i = _cred_fill < _CRED_N ? _cred_fill++ : _CRED_N - 1;")
        out.append("    memcpy(_cred_pat[i], pat, n + 1);")
        out.append("    _cred_h[i] = crust_re_compile(pat, _cred_arena[i], _CRE_ARENA, &e);")
        out.append("    if (!_cred_h[i]) {")
        out.append("        fprintf(stderr, \"regex: cannot compile %s: %s\\n\",")
        out.append("                pat, e ? e : \"unknown\");")
        out.append("        abort();")
        out.append("    }")
        out.append("    return _cred_h[i];")
        out.append("}")
        out.append("")
        out.append("static obj _cre_dyn(char* pat, char* t, int anc) {")
        out.append("    int caps[128];")
        out.append("    int ng, i, rc;")
        out.append("    obj m;")
        out.append("    crust_re* h;")
        out.append("    if (!pat || !t) return OBJ_NONE;")
        out.append("    h = _cre_dyn_get(pat);")
        out.append("    ng = crust_re_ngroups(h);")
        out.append("    if (2 * (ng + 1) > 128) return OBJ_NONE;")
        out.append("    rc = crust_re_exec(h, t, strlen(t), anc, caps, 2 * (ng + 1));")
        out.append("    if (rc != CRUST_RE_MATCH) return OBJ_NONE;")
        out.append("    m = list_new();")
        out.append("    for (i = 0; i <= ng; i++)")
        out.append("        list_append(m, _re_slice(t, caps[2*i], caps[2*i+1]));")
        out.append("    /* spans after the strings: see _re_emit_build */")
        out.append("    for (i = 0; i <= ng; i++) {")
        out.append("        list_append(m, OBJ_INT(caps[2*i]));")
        out.append("        list_append(m, OBJ_INT(caps[2*i+1]));")
        out.append("    }")
        out.append("    return m;")
        out.append("}")
        return out

    def coerce_to(self, target, value_node, rendered):
        """Coerce `rendered` (an expr for value_node) to the `target` C type."""
        if not target:
            return rendered
        # A list literal whose target is an unboxed typed list must be *built*
        # as that typed list, not boxed via list_from and pointer-cast: a boxed
        # list stores 16-byte tagged elements, so reinterpreting it as a
        # `_tlist_T*` reads the doubles/ints out of tag memory (garbage). This
        # covers `return [a, b]` from a `-> list[float]` function, a typed-list
        # default/argument given as a literal, etc. An empty `[]` builds an
        # empty typed list, which is also the correct unboxed value.
        if isinstance(value_node, ast.List) and \
                isinstance(target, str) and \
                target.startswith("_tlist_") and target.endswith("*"):
            return self._typed_list_literal(target, value_node)
        vt = self.value_ctype(value_node)
        # Checked before the `target == vt` shortcut below: a call that lowers
        # to a scalar helper is not an obj however `value_ctype` reads it, and
        # that shortcut would hand back the unwrapped `long` from
        # `str_find_from` for an obj target.
        if target == OBJ and self._scalar_helper_ct(rendered) is not None:
            return "OBJ_INT(%s)" % rendered
        # And the mirror image: coercing a scalar helper *to* a scalar is a
        # no-op, but `value_ctype` reads it as obj so the int path below wrapped
        # it in `AS_INT(...)` -- reading the `.u.i` field of something that is
        # already a long.
        if target in ("int", "long") and \
                self._scalar_helper_ct(rendered) is not None:
            return rendered
        if target == vt:
            return rendered
        if target == OBJ:
            return self.wrap_obj(value_node)
        is_objval = vt == OBJ or self.is_obj_word(value_node) or \
            (isinstance(rendered, str) and rendered.startswith(
                ("mp_call_", "mp_getattr", "mp_call_obj", "call_obj",
                 "call_closure", "mp_call_method")))
        if target.endswith("*"):           # target is a (class) pointer
            if target == "char*":
                if vt == "char*":
                    return rendered
                if is_objval:
                    return "AS_STR(%s)" % rendered
                return rendered
            if is_objval:
                if target.endswith("*"):
                    _tc = target[:-1]
                    if _tc not in self.classes:
                        # a `(Cls*)AS_OBJ(...)` cast needs Cls's forward typedef
                        # so ShivyCX's C parser recognizes it as a type; make
                        # sure Cls is loaded and queued for that typedef.
                        self._load_xclass_anywhere(_tc)
                        if _tc in self.xclasses:
                            self.xstructs_needed.add(_tc)
                            # qualify to the module csym so the cast names the
                            # same struct the forward typedef/declaration use.
                            target = self.xcsym(_tc) + "*"
                return "(%s)AS_OBJ(%s)" % (target, rendered)
            if vt and vt.endswith("*") and vt != OBJ:
                return "(%s)(%s)" % (target, rendered)   # base/derived cast
            return rendered
        if is_objval:
            if target == "int":
                return "AS_INT(%s)" % rendered
            if target == "bool":
                return "truthy(%s)" % rendered
            if target == "char*":
                return "AS_STR(%s)" % rendered
            if target in ("double", "float"):
                # pyfloat first so an int-tagged obj converts numerically
                # (a bare AS_FLOAT would read the int slot as a double).
                return "AS_FLOAT(pyfloat(%s))" % rendered
        if target == "bool" and vt in ("int", "bool"):
            return "(%s != 0)" % rendered if vt == "int" else rendered
        if target == OBJ and vt in ("int", "bool", "char*", "double"):
            return self.wrap_obj(value_node)
        return rendered

    # Helpers whose C signature returns a raw scalar rather than an obj.
    # Keyed on the emitted call rather than on the AST, because the same
    # Python spelling can lower either way: `s.index(x)` becomes
    # `str_find_from` (a long) while `xs.index(v)` becomes `list_index` (an
    # obj already). The receiver's static type does not tell them apart --
    # both are plain `obj` parameters in the generated C -- but the helper
    # py2c chose does, exactly.
    _SCALAR_HELPERS = ("str_find_from(", "str_find_range(", "str_count(")

    def _is_nameless_env(self, node):
        """A literal dict that binds no name an expression could read.

        `{}` and `{"__builtins__": {}}` both qualify: the second binds only
        the builtins slot, whose whole purpose there is to be empty. Anything
        else -- a variable, a dict with real entries -- does not.
        """
        if not isinstance(node, ast.Dict):
            return False
        for k, v in zip(node.keys, node.values):
            if not (isinstance(k, ast.Constant) and k.value == "__builtins__"):
                return False
            if not (isinstance(v, ast.Dict) and not v.keys):
                return False
        return True

    def _re_endpos(self, node, idx):
        """The `endpos` argument at `idx`, or -1 when it is not given."""
        if len(node.args) <= idx:
            return "-1"
        return self.coerce_to("int", node.args[idx], self.expr(node.args[idx]))

    def _scalar_helper_ct(self, rendered):
        """C type of `rendered` when it calls a scalar-returning helper."""
        if isinstance(rendered, str) and rendered.startswith(
                self._SCALAR_HELPERS):
            return "int"
        return None

    def wrap_obj(self, node):
        if isinstance(node, ast.IfExp):
            bt = self.value_ctype(node.body)
            ot = self.value_ctype(node.orelse)
            be = self.expr(node.body)
            oe = self.expr(node.orelse)
            # Two integer branches are unified to a scalar by `ex_IfExp`, so
            # the result still needs boxing here -- falling through to the
            # ordinary path below does that. Bailing out returned the raw
            # `long` where an obj was expected.
            _both_int = bt in _IFEXP_INTS and ot in _IFEXP_INTS
            if (bt != ot and not _both_int) or be.startswith("mp_") \
                    or oe.startswith("mp_"):
                return self.expr(node)
        # Checked before the obj bail-out below, not after: a scalar helper in
        # an argument position (`obj_add(i, str_find_from(..))`) needs wrapping
        # just as much as one being assigned, and `value_ctype` reads it as obj
        # -- which is exactly the bail-out that used to return it raw. Rendered
        # once and reused, so `expr` is not run twice for its side effects.
        _rendered = None
        if isinstance(node, ast.Call):
            _rendered = self.expr(node)
            if self._scalar_helper_ct(_rendered) is not None:
                return "OBJ_INT(%s)" % _rendered
        if self.is_obj_word(node) or self.value_ctype(node) == OBJ:
            return _rendered if _rendered is not None else self.expr(node)
        t = self.value_ctype(node)
        s = _rendered if _rendered is not None else self.expr(node)
        if s.startswith(("mp_call_", "mp_getattr", "mp_hasattr")):
            return s
        if isinstance(node, ast.Call) and self.value_ctype(node) == OBJ:
            return s
        if isinstance(node, ast.Call):
            ct = self.value_ctype(node)
            if ct == OBJ or ct is None:
                return s
        if t == "int":
            return "OBJ_INT(%s)" % s
        if t == "long":
            return "OBJ_INT(%s)" % s    # obj stores its integer payload as long
        if t == "char*":
            return "OBJ_STR(%s)" % s
        if t == "bool":
            return "OBJ_BOOL(%s)" % s
        if t == "double":
            return "OBJ_FLOAT(%s)" % s
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return "OBJ_STR(%s)" % s
            if isinstance(node.value, bytes):
                return "OBJ_STR(%s)" % s
            if isinstance(node.value, bool):
                return "OBJ_BOOL(%s)" % s
            if isinstance(node.value, int):
                return "OBJ_INT(%s)" % s
        if t and t.endswith("*") and t != OBJ:
            return "OBJ_OBJ(%s)" % s
        return s

    def is_obj_word(self, node):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)\
                and node.value.id == "self" and self.cur_class:
            return self.cur_class.field_ctype(node.attr) == OBJ
        if isinstance(node, ast.Name):
            if node.id == "self" and self.cur_class:
                return False
            if node.id in self.scope:
                return self.scope[node.id] == OBJ
            if node.id in self.singleton_names:
                return False
            if node.id in self.mod_const_types:
                return self.mod_const_types[node.id] == OBJ
            if node.id in self.mod_global_types:
                return self.mod_global_types[node.id] == OBJ
            if node.id in self.from_imports:
                # A name imported from another module: a singleton is a typed
                # Cls* pointer (not a tagged obj), a func/class is a typed
                # symbol, and a global carries its own declared ctype. Without
                # this, an imported singleton like `error_collector` falls to
                # the name-guess below and is wrongly treated as an obj word,
                # so a method call wraps it in AS_OBJ() and the C won't compile.
                kind, info = self.resolve_import(node.id,
                                                 self.from_imports[node.id])
                if kind in ("singleton", "func", "class"):
                    return False
                if kind == "global":
                    return info == OBJ
            return infer_from_name(node.id) is None
        if isinstance(node, ast.Constant) and node.value is None:
            return True
        return False

    def static_owner(self, ci, name):
        """Class in ci's chain that declares class-static `name`, else None."""
        c = ci
        while c:
            if name in getattr(c, "class_statics", {}):
                return c
            c = c.base
        return None

    def static_type(self, node):
        if isinstance(node, ast.Name):
            if node.id == "self" and self.cur_class:
                return self.cur_class.name + "*"
            if node.id in self.scope:
                return self.scope[node.id]
            h = getattr(self, "_hoisting", None)
            if h is not None and node.id in h:
                return h[node.id]       # type inferred earlier in this hoist pass
            if node.id in self.func_nodes:   # function used as a value -> closure
                return OBJ
            if node.id in self.classes:      # class used as a value -> ctor closure
                return OBJ
            if node.id in self.singleton_names:
                return self.singleton_names[node.id] + "*"
            if node.id in self.mod_const_types:
                return self.mod_const_types[node.id]
            if node.id in self.from_imports:
                kind, info = self.xref(node.id, self.from_imports[node.id])
                if kind == "singleton":
                    return info + "*"
            for gname, gct, _gk, _gv in self.mod_globals:
                if gname == node.id:
                    return gct
            return infer_from_name(node.id)
        if isinstance(node, ast.Attribute):
            if node.attr == "__name__":      # type(x).__name__ -> TYPE(x)->name
                return "char*"
            if node.attr == "buffer" and self.stdlib_root:
                return OBJ
            if isinstance(node.value, ast.Name):
                bn = node.value.id
                if bn in self.import_alias:
                    modname = self.import_alias[bn]
                    consts = self.load_xmod(modname).get("consts", {})
                    if node.attr in consts:
                        v = consts[node.attr]
                        if isinstance(v, bool):
                            return "bool"
                        if isinstance(v, int):
                            return "int"
                        if isinstance(v, str):
                            return "char*"
                # class-static (obj global)
                if bn == "self" and self.cur_class and \
                        self.static_owner(self.cur_class, node.attr):
                    return OBJ
                sci = self.classes.get(bn) or (self.xclasses[bn][0]
                                               if bn in self.xclasses else None)
                if sci is not None and self.static_owner(sci, node.attr):
                    return OBJ
                # A class-name access to a value-typed numeric class constant
                # (`DeclInfo.STATIC`) is inlined by ex_Attribute as
                # wrap_obj(literal) -> an obj. Report obj here so a surrounding
                # obj context does not re-box the already-boxed literal
                # (OBJ_INT(OBJ_INT(3))). Instance access (self.X / obj.X) still
                # reads the typed int field via the field_ctype paths below.
                _lcci = self.classes.get(bn)
                if _lcci is not None \
                        and node.attr in getattr(_lcci, "class_attrs", {}) \
                        and _lcci.field_ctype(node.attr) in ("int", "double"):
                    return OBJ
                # alias.ClassName used as a value -> a constructor closure (obj)
                if bn in self.import_alias:
                    kind, info = self.xref(node.attr, self.import_alias[bn])
                    if kind == "class":
                        return OBJ
                    if kind == "global":
                        return OBJ
                    if kind == "singleton":
                        return info + "*"
            if isinstance(node.value, ast.Name) and \
                    node.value.id == "self" and self.cur_class:
                fc = self.cur_class.field_ctype(node.attr)
                if fc:
                    return fc
                if node.attr in self.cur_class.property_methods:
                    pfn = self.cur_class.methods.get(node.attr)
                    if pfn:
                        return self._logical_ret(pfn)
            if isinstance(node.value, ast.Name) and \
                    node.value.id in self.modules:
                return None
            # isinstance-narrowed base: report the proven concrete field type,
            # so wrap_obj/coercion stays consistent with what ex_Attribute emits.
            if isinstance(node.value, ast.Name) and \
                    node.value.id in self.narrowed:
                cls = self.narrowed[node.value.id][:-1]
                if self._class_has_field(cls, node.attr):
                    ci = self.classes.get(cls) or self._ci_by_csym(cls)
                    return ci.field_ctype(node.attr)
            if self.is_obj_word(node.value) or \
                    self.value_ctype(node.value) == OBJ:
                owner = self.resolve_attr_owner(node.attr)
                if owner:
                    return owner.field_ctype(node.attr)
            # concrete class-pointer base (e.g. self.output.ctype where
            # self.output is ILValue*): report the field's declared type so a
            # further `.size` lowers to `->size`.
            bt = self.value_ctype(node.value)
            if bt and bt.endswith("*") and bt != OBJ:
                cls = bt[:-1]
                ci = self.classes.get(cls) or (self.xclasses[cls][0]
                                               if cls in self.xclasses else None) \
                    or self._ci_by_csym(cls)
                if ci is not None and self._class_has_field(cls, node.attr):
                    return ci.field_ctype(node.attr)
                if ci is not None:
                    sub = self._field_owner_subclass(cls, node.attr)
                    if sub:
                        sci = self.classes.get(sub) or self.xclasses[sub][0]
                        return sci.field_ctype(node.attr)
            return OBJ  # other attribute reads degrade to a Tier-2 obj
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool):
                return "bool"
            if isinstance(v, int):
                return "int"
            if isinstance(v, str):
                return "char*"
        return None

    def bool_expr(self, node):
        """Render `node` as a C truth test, boxing obj values via truthy()."""
        if self._is_sys_argv(node):         # `if sys.argv:` -> any args present
            return "(argc > 0)"
        s = self.expr(node)
        if isinstance(node, (ast.Compare, ast.UnaryOp)):
            return s
        if isinstance(node, ast.BoolOp):
            return self.truth_test(node, s)
        if isinstance(node, ast.Call) and s.startswith(
                ("mp_call_", "mp_getattr", "mp_call_obj", "call_obj",
                 "call_closure", "mp_call_method")):
            return "truthy(%s)" % s
        if self.is_obj_word(node) or \
                (self.static_type(node) or self.value_ctype(node)) == OBJ:
            return "truthy(%s)" % s
        # A raw char* in a boolean context is truthy only when non-empty (Python
        # string truthiness), not merely non-NULL -- otherwise `s if s else t`
        # with an empty string picks the empty `s`. truth_test handles char*
        # ((s && *s)), other pointers (s != NULL), and scalars ((s)) correctly.
        return self.truth_test(node, s)

    # ---- operators -------------------------------------------------------

    def ex_BinOp(self, node):
        if isinstance(node.op, ast.Add) and (self.looks_str(node.left) or
                                             self.looks_str(node.right)):
            return "pyconcat(%s, %s)" % (self.as_str(node.left),
                                         self.as_str(node.right))
        if isinstance(node.op, ast.Mod) and self.looks_str(node.left):
            # `fmt % args` is string formatting, not arithmetic modulo. A tuple
            # right-hand side spreads into multiple args; anything else is one.
            if isinstance(node.right, ast.Tuple):
                args = [self.wrap_obj(e) for e in node.right.elts]
            elif self._is_vararg_name(node.right):
                # `msg % args` inside a `*args` function. Python spreads a
                # tuple across the format's placeholders; py2c represents
                # `*args` as a list, and passing it as one value filled the
                # first `%s` with the whole collection -- crust's `self.err`
                # reported `cannot instantiate ['Vec','T','T'] over None`
                # instead of naming the type and the offender. Spread it.
                return "str_mod_spread(%s, %s)" % (
                    self.as_str(node.left), self.wrap_obj(node.right))
            else:
                args = [self.wrap_obj(node.right)]
            return self._emit_str_mod(self.as_str(node.left), args)
        lt = self.value_ctype(node.left)
        rt = self.value_ctype(node.right)
        numeric = {"int", "bool", "double", "float", "long",
                   "short", "unsigned", "char", "unsigned char"}
        # both sides are concrete numbers -> plain C arithmetic
        if lt in numeric and rt in numeric:
            if isinstance(node.op, ast.Pow):     # C has no ** operator
                return "ipow(%s, %s)" % (self.expr(node.left),
                                         self.expr(node.right))
            if isinstance(node.op, ast.Div):     # Python `/` is float division
                return "((double)%s / (double)%s)" % (self.expr(node.left),
                                                      self.expr(node.right))
            # Python's `%` and `//` *floor*; C's truncate toward zero. They
            # agree for non-negative operands and differ otherwise: `-4 % 8` is
            # 4 in Python and -4 in C. That is not academic -- ShivyCX's stack
            # allocator computes its padding as `size + (-size % 8)`, which
            # under C semantics is `4 + -4 == 0`, so every local landed at
            # offset 0 and overwrote the saved frame pointer.
            if isinstance(node.op, ast.Mod) and "double" not in (lt, rt) \
                    and "float" not in (lt, rt):
                self._need_pymod = True
                return "pymod(%s, %s)" % (self.expr(node.left),
                                          self.expr(node.right))
            if isinstance(node.op, ast.FloorDiv) and "double" not in (lt, rt) \
                    and "float" not in (lt, rt):
                self._need_pymod = True
                return "pyfdiv(%s, %s)" % (self.expr(node.left),
                                           self.expr(node.right))
            return "(%s %s %s)" % (self.expr(node.left),
                                   self.binop_sym(node.op),
                                   self.expr(node.right))
        # otherwise operate on Tier-2 values
        fns = {ast.Add: "obj_add", ast.Sub: "obj_sub", ast.Mult: "obj_mul",
               ast.FloorDiv: "obj_fdiv", ast.Div: "obj_fdiv",
               ast.Mod: "obj_mod", ast.Pow: "obj_pow"}
        f = fns.get(type(node.op))
        if f:
            return "%s(%s, %s)" % (f, self.wrap_obj(node.left),
                                   self.wrap_obj(node.right))
        binop = {ast.BitAnd: "'&'", ast.BitOr: "'|'", ast.BitXor: "'^'",
                 ast.LShift: "'l'", ast.RShift: "'r'"}.get(type(node.op))
        if binop:
            return "obj_bin(%s, %s, %s)" % (binop, self.wrap_obj(node.left),
                                            self.wrap_obj(node.right))
        return "(%s %s %s)" % (self.expr(node.left), self.binop_sym(node.op),
                               self.expr(node.right))

    def looks_str(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return True
        if self.static_type(node) == "char*":
            return True
        # `code[j]` where `code` is a string is a one-character *string*, not
        # an object. Without this, `code[j] != quote` fell through to the
        # identity branch and compiled to a pointer comparison against an
        # obj -- always unequal, so `_blank`'s string-literal scan never
        # terminated and blanked the rest of the file. Every Rust item after
        # an `#include "..."` then vanished before the item scanner ran.
        if isinstance(node, ast.Subscript) and \
                not isinstance(node.slice, ast.Slice):
            base = node.value
            if self.static_type(base) == "char*":
                return True
            if isinstance(base, ast.Name) and \
                    self.scope.get(base.id) == "char*":
                return True
        return False

    def str_operand(self, node):
        s = self.expr(node)
        return ("AS_STR(%s)" % s
                if self.is_obj_word(node) or self.value_ctype(node) == OBJ
                else s)

    def as_str(self, node):
        """Render `node` as a char* expression."""
        t = self.value_ctype(node)
        if t == "char*":
            return self.expr(node)
        if t == OBJ or self.is_obj_word(node):
            return "AS_STR(%s)" % self.expr(node)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return self.expr(node)
        return "pystr(%s)" % self.wrap_obj(node)

    # ---- compiled dynamic getattr/setattr on a typed struct ---------------
    # When the receiver's static type is a known struct, getattr/setattr with a
    # *runtime* key lower to an inline switch on the key's first character (the
    # rpython type-encoding convention: a field's initial letter selects its C
    # type), then a strcmp picks the exact field for direct, typed member
    # access. No hash table and no micropython bridge -- a compiled jump table.
    _DYN_BOX = {"int": "OBJ_INT", "long": "OBJ_INT", "short": "OBJ_INT",
                "char": "OBJ_INT", "bool": "OBJ_BOOL", "double": "OBJ_FLOAT",
                "float": "OBJ_FLOAT", "char*": "OBJ_STR"}
    _DYN_UNBOX = {"int": "AS_INT", "long": "AS_INT", "short": "AS_INT",
                  "char": "AS_INT", "bool": "AS_INT", "double": "AS_FLOAT",
                  "float": "AS_FLOAT", "char*": "AS_STR"}

    def _is_module_ref(self, node):
        """True if `node` names an imported module (not an obj value), so
        dynamic attribute access can't go through rt_getattr/rt_setattr."""
        return isinstance(node, ast.Name) and node.id in self.import_alias

    def _key_charp(self, node):
        """Render an attribute-key node as a `const char*`."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return c_string(node.value)
        return self.as_str(node)

    def _dyn_struct_ci(self, recv_node):
        """ClassInfo if `recv_node`'s static type is a known local struct
        pointer, else None."""
        ct = self.value_ctype(recv_node)
        if ct and ct.endswith("*"):
            return self.classes.get(ct[:-1])
        return None

    def _dyn_box(self, t, e):
        if t in self._DYN_BOX:
            return "%s(%s)" % (self._DYN_BOX[t], e)
        if t.endswith("*"):
            return "OBJ_OBJ(%s)" % e
        return e                    # already an obj field

    def _dyn_unbox(self, t, v):
        if t in self._DYN_UNBOX:
            return "%s(%s)" % (self._DYN_UNBOX[t], v)
        if t.endswith("*"):
            return "(%s)AS_OBJ(%s)" % (t, v)
        return v

    def _dyn_switch(self, ci, mode):
        """`switch(_dk[0]){...}` over ci's fields grouped by name initial. `mode`
        is "get" or "set", selecting the matched-field C statement (inlined here
        rather than passed as a lambda so the source parses under rast)."""
        groups = {}
        for n, t in ci.full_fields():
            groups.setdefault(n[0], []).append((n, t))
        cases = []
        for c in sorted(groups):
            arm_parts = []
            for (n, t) in groups[c]:
                if mode == "get":
                    a = "_dr = %s;" % self._dyn_box(t, "_ds->%s" % cname(n))
                else:
                    a = "{ _ds->%s = %s; }" % (cname(n), self._dyn_unbox(t, "_dv"))
                arm_parts.append('if (!strcmp(_dk, "%s")) %s' % (n, a))
            arms = " else ".join(arm_parts)
            cases.append("case '%s': %s break;" % (c, arms))
        return "switch (_dk[0]) { %s }" % " ".join(cases)

    def _emit_dynget(self, recv_node, ci, key_node, dflt):
        sw = self._dyn_switch(ci, "get")
        return ("({ %s* _ds = %s; const char* _dk = %s; obj _dr = %s; %s _dr; })"
                % (ci.csym, self._class_ptr_expr(recv_node, ci.name),
                   self.as_str(key_node), dflt, sw))

    def _emit_dynset(self, recv_node, ci, key_node, val_node):
        sw = self._dyn_switch(ci, "set")
        return ("({ %s* _ds = %s; const char* _dk = %s; obj _dv = %s; %s "
                "OBJ_NONE; })" % (ci.csym,
                                  self._class_ptr_expr(recv_node, ci.name),
                                  self.as_str(key_node),
                                  self.wrap_obj(val_node), sw))

    def as_long(self, node):
        """Render `node` as a long/int expression."""
        t = self.value_ctype(node)
        if t in _SIGNED_INT_RANK:
            return self.expr(node)           # already a C integer; no roundtrip
        if t == OBJ or self.is_obj_word(node):
            return "AS_INT(%s)" % self.expr(node)
        return "pyint(%s)" % self.wrap_obj(node)

    def _lower_affix(self, m, func, node):
        """Lower `s.startswith(..)` / `s.endswith(..)`.

        Three forms have to be told apart, and getting this wrong is silent.
        A bare `s.startswith("x")` is one runtime call. A start offset --
        `s.startswith(p, i)` -- needs the `_at` variant; emitting the two-
        argument call and dropping `i` compiles fine and then tests the wrong
        position, which is how the Rust front end came to fail only when
        self-hosted. A tuple of prefixes expands to a chain of ors, because
        the runtime takes a `str`, and handing it a list object type-checks
        through `AS_STR` and never matches.

        Anything left over is rejected rather than approximated. A wrong
        answer that compiles is worse than a build that stops.
        """
        a = node.args
        if len(a) > 2:
            raise Py2CUnsupported(
                "%s:%s: %s() with both start and end offsets is not lowered"
                % (getattr(self, "src_name", "?"), node.lineno, m))
        base = self.as_str(func.value)
        fn = "str_startswith" if m == "startswith" else "str_endswith"
        off = self.as_long(a[1]) if len(a) == 2 else None
        if off is not None:
            fn += "_at"
        first = a[0] if a else None
        if isinstance(first, (ast.Tuple, ast.List)):
            parts = []
            for elt in first.elts:
                if off is None:
                    parts.append("%s(%s, %s)" % (fn, base, self.as_str(elt)))
                else:
                    parts.append("%s(%s, %s, %s)"
                                 % (fn, base, self.as_str(elt), off))
            return ("(" + " || ".join(parts) + ")") if parts else "false"
        if off is None:
            return "%s(%s, %s)" % (fn, base, self.as_str(first))
        return "%s(%s, %s, %s)" % (fn, base, self.as_str(first), off)

    def lower_str_method(self, func, node):
        """Lower a string method call; returns C or None if not a str method."""
        m = func.attr
        a = node.args
        if m in ("startswith", "endswith"):
            return self._lower_affix(m, func, node)
        if m in ("strip", "lstrip", "rstrip"):
            mode = {"strip": 0, "lstrip": 1, "rstrip": 2}[m]
            return "str_strip(%s, %d)" % (self.as_str(func.value), mode)
        if m == "split":
            sep = self.as_str(a[0]) if a else "NULL"
            return "str_split(%s, %s)" % (self.as_str(func.value), sep)
        if m == "rsplit":
            # `sep=None` means whitespace, which the runtime spells as a NULL
            # separator -- passing the literal None through `as_str` would
            # hand it a null obj instead and read the tag off it.
            none_sep = a and isinstance(a[0], ast.Constant) \
                and a[0].value is None
            sep = "NULL" if (not a or none_sep) else self.as_str(a[0])
            mx = self.as_long(a[1]) if len(a) > 1 else "-1"
            return "str_rsplit(%s, %s, %s)" % (
                self.as_str(func.value), sep, mx)
        if m == "partition":
            return "str_partition(%s, %s)" % (self.as_str(func.value), self.as_str(a[0]))
        if m == "splitlines":
            return "str_splitlines(%s)" % self.as_str(func.value)
        if m == "replace":
            if len(a) < 2:
                return None
            return "str_replace(%s, %s, %s)" % (self.as_str(func.value), self.as_str(a[0]),
                                                self.as_str(a[1]))
        if m in ("find", "rfind", "rindex", "index"):
            # The `start` argument was silently dropped, so `find(sub, pos)`
            # always returned the *first* match. A scan loop written
            # `pos = text.find(x, pos) + 1` then never advanced -- the
            # self-hosted compiler hung on any input.
            # `rindex`/`index` are the raising forms; Crust/bootstrap code only
            # calls them when the substring is known present, so they share
            # the find lowering (a missing match yields -1 rather than
            # ValueError -- acceptable for the self-hosted build).
            last = "true" if m in ("rfind", "rindex") else "false"
            if len(a) >= 3:
                # `find(sub, start, end)`: the end bound was dropped, so the
                # search ran past it.
                return "str_find_range(%s, %s, %s, %s, %s)" % (
                    self.as_str(func.value), self.as_str(a[0]), last,
                    self.as_long(a[1]), self.as_long(a[2]))
            if len(a) >= 2:
                return "str_find_from(%s, %s, %s, %s)" % (
                    self.as_str(func.value), self.as_str(a[0]), last,
                    self.as_long(a[1]))
            return "str_find(%s, %s, %s)" % (
                self.as_str(func.value), self.as_str(a[0]), last)
        if m in ("isdigit", "isalpha", "isspace", "isalnum"):
            return "str_%s(%s)" % (m, self.as_str(func.value))
        if m == "lower":
            return "str_lower(%s)" % self.as_str(func.value)
        if m == "upper":
            return "str_upper(%s)" % self.as_str(func.value)
        if m == "join":
            return "pyjoin(%s, %s)" % (self.as_str(func.value), self.wrap_obj(a[0]))
        if m == "encode":
            return self.as_str(func.value)
        return None

    STR_METHODS = {"startswith", "endswith", "strip", "lstrip", "rstrip",
                   "split", "rsplit", "partition", "splitlines", "replace",
                   "find",
                   "rfind", "rindex", "index", "isdigit", "isalpha",
                   "isspace", "isalnum", "lower", "upper", "join", "encode"}

    def truth_test(self, node, rendered):
        """A C truth test for `rendered` (the expr of `node`)."""
        if self.is_obj_word(node) or self.value_ctype(node) == OBJ:
            return "truthy(%s)" % rendered
        t = self.value_ctype(node)
        if t == "char*":
            return "(%s && *(%s))" % (rendered, rendered)
        if t and t.endswith("*"):
            return "(%s != NULL)" % rendered
        return "(%s)" % rendered

    def _truth_test_expr(self, v):
        # v -> C truth test of v. A named method (passed like the sibling
        # render(self.expr)/render(self.wrap_obj) callbacks) rather than a lambda,
        # so the source parses under rast.
        return self.truth_test(v, self.expr(v))

    def ex_BoolOp(self, node):
        vals = node.values
        is_and = isinstance(node.op, ast.And)

        def render(fn):
            """Apply each operand's fn left-to-right; for `and`, later operands
            see the isinstance-narrowings implied by the operands before them
            (e.g. `isinstance(x, T) and x.field`). Narrowings are restored."""
            saved, res = {}, []
            for v in vals:
                res.append(fn(v))
                if is_and:
                    for nm, ct in self._narrowings(v):
                        saved.setdefault(nm, self.narrowed.get(nm))
                        self.narrowed[nm] = ct
            for nm, old in saved.items():
                if old is None:
                    self.narrowed.pop(nm, None)
                else:
                    self.narrowed[nm] = old
            return res

        types = render(self.value_ctype)
        same = len(set(types)) == 1 and types[0] in ("int", "bool", "char*")
        if same:
            rend = render(self.expr)
            tests = render(self._truth_test_expr)
        else:                                # unify to Tier-2 obj
            rend = render(self.wrap_obj)
            tests = ["truthy(%s)" % r for r in rend]
        ctype = types[0] if same else OBJ
        expr = rend[-1]
        for i in range(len(vals) - 2, -1, -1):
            if _contains_call(vals[i]):
                # The ternary names the left operand twice -- once to test it,
                # once as the result -- so a call there runs twice. That is
                # invisible for a pure operand and wrong for anything else:
                # crust's `self.accept("const") or self.accept("mut")` consumed
                # `const` in the test and then re-ran `accept`, which found the
                # next token and returned None, so every `*const T` was
                # rejected as a raw pointer missing its qualifier. Bind the
                # operand to a temporary and evaluate it once.
                self._boolop_tmp = getattr(self, "_boolop_tmp", 0) + 1
                tmp = "_bo%d" % self._boolop_tmp
                test = _truth_of(tmp, ctype)
                if is_and:
                    body = "%s ? %s : %s" % (test, expr, tmp)
                else:
                    body = "%s ? %s : %s" % (test, tmp, expr)
                expr = "({ %s %s = %s; %s; })" % (ctype, tmp, rend[i], body)
            elif is_and:                     # a and b -> (test(a) ? b : a)
                expr = "(%s ? %s : %s)" % (tests[i], expr, rend[i])
            else:                            # a or b  -> (test(a) ? a : b)
                expr = "(%s ? %s : %s)" % (tests[i], rend[i], expr)
        return expr

    def ex_UnaryOp(self, node):
        if isinstance(node.op, ast.Not):
            return "(!%s)" % self.bool_expr(node.operand)
        sym = {ast.USub: "-", ast.UAdd: "+", ast.Invert: "~"}[type(node.op)]
        operand = node.operand
        if isinstance(node.op, ast.USub):
            lt = self.value_ctype(operand)
            if self.is_obj_word(operand) or lt == OBJ:
                return "obj_neg(%s)" % self.expr(operand)
            if lt and lt.endswith("*") and lt not in ("char*", OBJ):
                return "obj_neg(OBJ_OBJ(%s))" % self.expr(operand)
        if isinstance(node.op, ast.UAdd):
            lt = self.value_ctype(operand)
            if self.is_obj_word(operand) or lt == OBJ:
                return self.expr(operand)
            if lt and lt.endswith("*") and lt not in ("char*", OBJ):
                return "OBJ_OBJ(%s)" % self.expr(operand)
        if self.is_obj_word(operand) or self.value_ctype(operand) == OBJ:
            if isinstance(node.op, ast.Invert):
                return "obj_invert(%s)" % self.expr(operand)
            return self.expr(operand)        # unary plus: identity
        return "(%s%s)" % (sym, self.expr(operand))

    def ex_Compare(self, node):
        parts = []
        cur = node.left
        for op, comp in zip(node.ops, node.comparators):
            parts.append(self.cmp(cur, op, comp))
            cur = comp
        return "(" + " && ".join(parts) + ")"

    def _wrap_cmp_operand(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return "OBJ_STR(%s)" % c_string(node.value)
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return "OBJ_INT(%s)" % self.expr(node)
        return self.wrap_obj(node)

    def cmp(self, left, op, right):
        ls, rs = self.expr(left), self.expr(right)
        if isinstance(op, (ast.In, ast.NotIn)):
            tdct = self._typed_dict_ct(right)
            if tdct is not None:
                kct = self._tdict_by_name[tdct[:-1]][0]
                inner = "%s_has(%s, %s)" % (
                    tdct[:-1], self.expr(right),
                    self.coerce_to(kct, left, self.expr(left)))
                return ("(!%s)" % inner) if isinstance(op, ast.NotIn) else inner
            if isinstance(right, ast.Name) and right.id in self.str_sets:
                elems = self.str_sets[right.id]
                arr = "(const str[]){%s}" % ", ".join(c_string(e)
                                                      for e in elems)
                inner = "in_str(%s, %s, %d)" % (
                    self.str_operand(left), arr, len(elems))
            else:
                inner = "pycontains(%s, %s)" % (self.wrap_obj(right), self.wrap_obj(left))
            return ("(!%s)" % inner) if isinstance(op, ast.NotIn) else inner
        if isinstance(op, (ast.Is, ast.IsNot)):
            neg = isinstance(op, ast.IsNot)
            if isinstance(right, ast.Constant) and right.value is None:
                lt = self.value_ctype(left)
                if self.is_obj_word(left) or lt == OBJ:
                    s = "IS_NONE(%s)" % ls
                elif lt and lt.endswith("*"):       # char* / class pointer
                    s = "(%s == NULL)" % ls
                else:                                # a non-nullable scalar
                    s = "0"
                return ("(!%s)" % s) if neg else s
            # identity on objects -> obj_eq; otherwise raw pointer/scalar compare
            if self.is_obj_val(left) or self.is_obj_val(right):
                eq = "obj_eq(%s, %s)" % (self._wrap_cmp_operand(left),
                                         self._wrap_cmp_operand(right))
                return ("(!%s)" % eq) if neg else eq
            sym = "!=" if neg else "=="
            return "(%s %s %s)" % (ls, sym, rs)
        sym = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=",
               ast.Gt: ">", ast.GtE: ">="}[type(op)]
        lo, ro = self.is_obj_val(left), self.is_obj_val(right)
        lp, rp = self.is_ptr_val(left), self.is_ptr_val(right)
        if sym in ("==", "!="):
            # obj compared against a real pointer (e.g. self.base == RBP).
            # A string literal/char* is a *value*, not an identity, so exclude
            # it here and let it fall through to obj_eq (which strcmp's strings).
            if ((lo and rp) or (ro and lp)) \
                    and not (self.looks_str(left) or self.looks_str(right)):
                o, p = (ls, rs) if lo else (rs, ls)
                core = "(IS_OBJ(%s) && AS_OBJ(%s) == (Obj*)(%s))" % (o, o, p)
                return core if sym == "==" else "(!%s)" % core
            if lo or ro:
                eq = "obj_eq(%s, %s)" % (self._wrap_cmp_operand(left),
                                        self._wrap_cmp_operand(right))
                return eq if sym == "==" else "(!%s)" % eq
            if self.looks_str(left) or self.looks_str(right):
                return "(strcmp(%s, %s) %s 0)" % (self.str_operand(left),
                                                  self.str_operand(right), sym)
            return "(%s %s %s)" % (ls, sym, rs)
        # ordering
        if lo or ro:
            return "(obj_cmp(%s, %s) %s 0)" % (self.wrap_obj(left),
                                               self.wrap_obj(right), sym)
        if self.looks_str(left) or self.looks_str(right):
            return "(strcmp(%s, %s) %s 0)" % (self.str_operand(left),
                                              self.str_operand(right), sym)
        return "(%s %s %s)" % (ls, sym, rs)

    def is_obj_val(self, node):
        if self.is_obj_word(node) or self.value_ctype(node) == OBJ:
            return True
        if self.stdlib_root and isinstance(node, ast.Attribute):
            cur = node
            while isinstance(cur, ast.Attribute):
                cur = cur.value
            if isinstance(cur, ast.Name) and cur.id in self.modules | \
                    set(self.import_alias):
                return True
        if isinstance(node, ast.Name) and node.id in self.mod_global_names \
                and node.id not in self.scope:
            if self.mod_global_types.get(node.id) == OBJ:
                return True
        if isinstance(node, ast.Call) and self.stdlib_root:
            if isinstance(node.func, ast.Name) and \
                    node.func.id in ("getattr", "mp_getattr", "mp_call_import",
                                     "mp_call_method", "mp_call_obj"):
                return True
            rt = self.value_ctype(node)
            if rt == OBJ or rt is None:
                return True
        if isinstance(node, ast.Name) and node.id in self.scope:
            return self.scope[node.id] == OBJ
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "self" and self.cur_class:
            ft = self.cur_class.field_ctype(node.attr)
            if ft == OBJ:
                return True
        st = self.static_type(node)
        return st == OBJ

    def _recv_class_owns(self, node, attr):
        """True if `node`'s *static* type is an instance pointer of a class that
        defines `attr` (walking bases). Used to decide whether a name that is
        both a container builtin (clear/copy/...) and a user method must
        dispatch as the class method. An `obj`/container receiver returns False
        so the container builtin is used -- otherwise a list `.clear()` would be
        miscompiled into a virtual call through the wrong (class) vtable."""
        ct = self.value_ctype(node)
        if not ct or not ct.endswith("*") or ct == "char*":
            return False
        name = ct[:-1]
        ci = self.classes.get(name) or (self.xclasses[name][0]
                                        if name in self.xclasses else None)
        while ci:
            if attr in ci.methods:
                return True
            ci = ci.base
        return False

    def const_dict_owner(self, attr_node):
        """For an attribute `X.D` (X = self, a class name, or alias.Class),
        return the class declaring D as a specialized const dict (walking
        bases), else None. Registers an extern for imported owners."""
        if not isinstance(attr_node, ast.Attribute):
            return None
        d, xv = attr_node.attr, attr_node.value
        ci = None
        if isinstance(xv, ast.Name):
            if xv.id == "self":
                ci = self.cur_class
            else:
                ci = self.classes.get(xv.id) or (self.xclasses[xv.id][0]
                                                 if xv.id in self.xclasses
                                                 else None)
        elif isinstance(xv, ast.Attribute) and isinstance(xv.value, ast.Name) \
                and xv.value.id in self.import_alias:
            kind, info = self.xref(xv.attr, self.import_alias[xv.value.id])
            if kind == "class":
                ci = info
        while ci:
            if d in ci.const_dicts:
                if ci.name not in self.classes:
                    self.xconstdict_externs.add((ci.name, d))
                return ci
            ci = ci.base
        return None

    def is_ptr_val(self, node):
        t = self.value_ctype(node)
        return bool(t) and t.endswith("*") and t != OBJ

    def _type_dispatch_subscript(self, node):
        """Lower `{Cls: val, ...}[type(x)]` to an isinstance chain, or None if
        the node isn't that idiom. Every key must resolve to a known class."""
        d, sl = node.value, node.slice
        if not isinstance(d, ast.Dict) or not d.keys:
            return None
        if not (isinstance(sl, ast.Call) and isinstance(sl.func, ast.Name)
                and sl.func.id == "type" and len(sl.args) == 1):
            return None
        pairs = []
        for k, v in zip(d.keys, d.values):
            if k is None or self._resolve_class_ref(k) is None:
                return None                    # ** unpack or non-class key
            pairs.append((k, v))
        x = sl.args[0]
        out = "OBJ_NONE"                       # type matched no key (KeyError)
        for k, v in reversed(pairs):
            out = "(%s ? %s : %s)" % (
                self.lower_isinstance(x, k), self.wrap_obj(v), out)
        return out

    def ex_Subscript(self, node):
        if self._is_sys_argv(node.value) and not isinstance(node.slice, ast.Slice):
            return "argv[%s]" % self.as_long(node.slice)   # char* command-line arg
        # `sys.argv[1:]` -- the whole command line minus the program name,
        # which is how a program that hands its arguments to a function reads
        # them. Only indexing was lowered, so this emitted an undeclared
        # `sys_argv`; `tools/cpprust.py` ends with `main(sys.argv[1:])`.
        if self._is_sys_argv(node.value) and isinstance(node.slice, ast.Slice) \
                and node.slice.step is None and node.slice.upper is None:
            lo = self.as_long(node.slice.lower) \
                if node.slice.lower is not None else "0"
            self.loop_n += 1
            k = "_av%d" % self.loop_n
            return ("({ obj %s = list_new(); for (int %s_i = (%s); "
                    "%s_i < argc; %s_i++) "
                    "list_append(%s, OBJ_STR(argv[%s_i])); %s; })"
                    % (k, k, lo, k, k, k, k, k))
        # `{Cls1: v1, Cls2: v2, ...}[type(x)]` -- a type->value dispatch table.
        # A dict keyed by classes can't be built and looked up by type() in the
        # obj model (class-as-value is a ctor closure, type() is a TypeInfo*, so
        # the keys never match), so lower the whole expression to an isinstance
        # chain instead.
        disp = self._type_dispatch_subscript(node)
        if disp is not None:
            return disp
        if isinstance(node.value, ast.Subscript):
            inner = node.value
            owner = self.const_dict_owner(inner.value) \
                if isinstance(inner.value, ast.Attribute) else None
            if owner is not None:
                d = inner.value.attr
                key = self.expr(inner.slice)
                i = self.expr(node.slice)
                return "%s_%s(%s, %s)" % (owner.name, d, key, i)
        sl = node.slice
        if isinstance(sl, ast.Slice):
            if sl.step is not None:
                step = self.as_long(sl.step)
                lo = self.as_long(sl.lower) if sl.lower else "0"
                hi = self.as_long(sl.upper) if sl.upper else "0"
                hl = "1" if sl.lower else "0"
                hh = "1" if sl.upper else "0"
                return "py_slice_step(%s, %s, %s, %s, %s, %s)" % (
                    self.wrap_obj(node.value), lo, hi, step, hl, hh)
            lo = self.as_long(sl.lower) if sl.lower else "0"
            hi = self.as_long(sl.upper) if sl.upper else "PY_SLICE_END"
            return "py_slice(%s, %s, %s)" % (self.wrap_obj(node.value), lo, hi)
        # indexing a Tier-2 obj (list/dict/str) dispatches at runtime
        vct = self.value_ctype(node.value)
        if self.stdlib_root and isinstance(node.value, ast.Attribute):
            bt = self.value_ctype(node.value)
            if bt == OBJ or self.is_obj_word(node.value) or bt is None:
                return "subscript(%s, %s)" % (self.wrap_obj(node.value),
                                              self.wrap_obj(sl))
        if self.is_obj_word(node.value) or vct == OBJ or \
                isinstance(node.value, ast.Call):
            return "subscript(%s, %s)" % (self.expr(node.value),
                                          self.wrap_obj(sl))
        if self.value_ctype(node.value) == "char*":   # s[i] -> 1-char string
            return "char_at(%s, %s)" % (self.expr(node.value), self.as_long(sl))
        # rpython typed list: xs[i] -> xs->data[i] (unboxed). A negative integer
        # *literal* (xs[-1]) wraps to xs->data[xs->len + (-1)] statically -- no
        # runtime branch, so hot numeric loops with non-negative indices keep
        # plain direct indexing. (Dynamic indices are taken as-is, numpy-style.)
        tlct = self._typed_list_ct(node.value)
        if tlct is not None and not isinstance(sl, ast.Slice):
            recv = self.expr(node.value)
            neg = self._neg_int_literal(sl)
            if neg is not None and isinstance(node.value,
                                              (ast.Name, ast.Attribute)):
                return "%s->data[%s->len + (%d)]" % (recv, recv, neg)
            return "%s->data[%s]" % (recv, self.as_long(sl))
        # rpython typed dict: d[k] -> _tdict_K_V_get(d, k)
        tdct = self._typed_dict_ct(node.value)
        if tdct is not None and not isinstance(sl, ast.Slice):
            kct = self._tdict_by_name[tdct[:-1]][0]
            return "%s_get(%s, %s)" % (tdct[:-1], self.expr(node.value),
                                       self.coerce_to(kct, sl, self.expr(sl)))
        # scalar-pointer array (int*/double*/float*/...): native C indexing for
        # ANY index (a numpy-style array), not just literal indices.
        if vct and vct.endswith("*") and vct[:-1] in _SCALAR_CTYPES:
            return "%s[%s]" % (self.expr(node.value), self.as_long(sl))
        if not isinstance(sl, ast.Slice):
            idx = self.expr(sl)
            if not idx.lstrip("-").isdigit():
                return "subscript(%s, %s)" % (self.wrap_obj(node.value),
                                              self.wrap_obj(sl))
        return "%s[%s]" % (self.expr(node.value), self.expr(sl))

    def ex_IfExp(self, node):
        bt = self.value_ctype(node.body)
        ot = self.value_ctype(node.orelse)
        be = self.expr(node.body)
        oe = self.expr(node.orelse)
        if self.stdlib_root and (bt != ot or be.startswith("mp_") or
                                 oe.startswith("mp_")):
            return "(%s ? %s : %s)" % (self.bool_expr(node.test),
                                       self.wrap_obj(node.body),
                                       self.wrap_obj(node.orelse))
        # Two integer branches unify to the wider integer, not to an obj.
        # `j = n if j < 0 else j` with `n` an int and `j` a long used to box
        # both, and the boxed conditional then would not assign back to the
        # `long j` it came from. C's own conversions would do this anyway;
        # boxing was throwing away a scalar for no reason. `value_ctype`
        # applies the same rule over the same order -- they have to agree.
        _INTS = _IFEXP_INTS
        if bt != ot and bt in _INTS and ot in _INTS:
            wide = _INTS[max(_INTS.index(bt), _INTS.index(ot))]
            return "(%s ? (%s)%s : (%s)%s)" % (
                self.bool_expr(node.test), wide, be, wide, oe)
        if bt != ot:                        # unify to a common Tier-2 obj
            return "(%s ? %s : %s)" % (self.bool_expr(node.test),
                                       self.wrap_obj(node.body),
                                       self.wrap_obj(node.orelse))
        return "(%s ? %s : %s)" % (self.bool_expr(node.test),
                                   self.expr(node.body),
                                   self.expr(node.orelse))

    def _ret_ctype(self, returns):
        """Return ctype of a function, preferring rpython typed containers
        (`list[T]`/`dict[K,V]`) over the obj fallback."""
        if returns is not None and getattr(self, "_pod_enabled", False):
            r = self.ann_ctype(returns)
            if r is not None and (r.startswith("_tlist_") or
                                  r.startswith("_tdict_")):
                return r
        return ann_to_ctype(returns) or OBJ

    def _local_ann_ctype(self, name, annotation):
        """C type of an annotated local, preferring rpython typed lists over the
        name-based infer_type fallback (which doesn't know `list[T]`)."""
        if annotation is not None and getattr(self, "_pod_enabled", False):
            r = self.ann_ctype(annotation)
            if r is not None and (r.startswith("_tlist_") or
                                  r.startswith("_tdict_")):
                return r
        return infer_type(name, annotation)

    def _typed_dict_ct(self, node):
        """Return the typed-dict ctype (e.g. '_tdict_charp_int*') of an
        expression, or None if it is not a typed dict."""
        try:
            ct = self.value_ctype(node)
        except Exception:
            return None
        if isinstance(ct, str) and ct.startswith("_tdict_") and ct.endswith("*"):
            return ct
        return None

    def _typed_dict_literal(self, ct, dictnode):
        name = ct[:-1]
        kct, vct = self._tdict_by_name[name]
        pairs = [(k, v) for k, v in zip(dictnode.keys, dictnode.values)
                 if k is not None]
        parts = ["%s* _td = %s_new(%d);" % (name, name, len(pairs) or 4)]
        for k, v in pairs:
            parts.append("%s_set(_td, %s, %s);" % (
                name, self.coerce_to(kct, k, self.expr(k)),
                self.coerce_to(vct, v, self.expr(v))))
        parts.append("_td;")
        return "({ " + " ".join(parts) + " })"

    def _neg_int_literal(self, sl):
        """The integer value of a negative integer literal subscript (e.g. -1
        from xs[-1]), or None if `sl` is not a negative integer literal."""
        if isinstance(sl, ast.UnaryOp) and isinstance(sl.op, ast.USub) \
                and isinstance(sl.operand, ast.Constant) \
                and isinstance(sl.operand.value, int) \
                and not isinstance(sl.operand.value, bool):
            return -sl.operand.value
        return None

    def _typed_list_ct(self, node):
        """Return the typed-list ctype (e.g. '_tlist_int*') of an expression,
        or None if it is not a typed list."""
        try:
            ct = self.value_ctype(node)
        except Exception:
            return None
        if isinstance(ct, str) and ct.startswith("_tlist_") and ct.endswith("*"):
            return ct
        return None

    def _typed_list_minmax(self, fn, ct, listnode):
        """Reduce an unboxed typed list to its min/max with a direct C loop,
        returning the scalar boxed as an obj (so value_ctype stays OBJ). `fn` is
        'min' or 'max'; `ct` is the list's ctype (e.g. '_tlist_double*')."""
        elem = ct[:-1][len("_tlist_"):]       # '_tlist_double*' -> 'double'
        box = "OBJ_FLOAT" if elem in ("double", "float") else "OBJ_INT"
        cmp = "<" if fn == "min" else ">"
        self._list_tmp = getattr(self, "_list_tmp", 0) + 1
        v = "_mm%d" % self._list_tmp
        expr = self.expr(listnode)
        return ("({ %s %s = %s; %s _r = %s->data[0]; long _i; "
                "for (_i = 1; _i < %s->len; _i++) "
                "if (%s->data[_i] %s _r) _r = %s->data[_i]; %s(_r); })") % (
            ct, v, expr, elem, v, v, v, cmp, v, box)

    def _decoder_gen_class(self, node):
        """If `node` is the call `rpy.json.generate_decoder(Cls)` with Cls a
        known local class, return its ClassInfo; else None."""
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "generate_decoder"
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Name)):
            return None
        hf = node.func.value           # expect rpy.json
        if not (isinstance(hf, ast.Attribute) and hf.attr == "json"
                and isinstance(hf.value, ast.Name)):
            return None
        rbase = hf.value.id
        if not (rbase == "rpy" or self.import_alias.get(rbase) == "rpy"):
            return None
        return self.classes.get(node.args[0].id)

    def _resolve_json_hook(self, hook):
        """ClassInfo for an object_hook argument: either the inline call
        `rpy.json.generate_decoder(Cls)`, or a Name bound to one earlier
        (`hook = rpy.json.generate_decoder(Cls)` hoisted out of a loop)."""
        ci = self._decoder_gen_class(hook)
        if ci is not None:
            return ci
        if isinstance(hook, ast.Name):
            return self._json_hook_class.get(hook.id)
        return None

    def _match_json_decode(self, node):
        """If `node` is `json.loads(EXPR, object_hook=rpy.json.generate_decoder(Cls))`
        (or the positional 2-arg form, or a hoisted hook variable), return
        (ClassInfo, src_expr_node); else None. `import rpy` / `import json` are
        never translated -- only this call shape is recognized, and only the
        class argument is read."""
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == "loads"
                and isinstance(f.value, ast.Name)):
            return None
        base = f.value.id
        if not (base == "json" or self.import_alias.get(base) == "json"):
            return None
        if not node.args:
            return None
        hook = None
        for kw in node.keywords:
            if kw.arg == "object_hook":
                hook = kw.value
        if hook is None and len(node.args) >= 2:
            hook = node.args[1]
        if hook is None:
            return None
        ci = self._resolve_json_hook(hook)
        if ci is None:
            return None
        return ci, node.args[0]

    def _json_decode_call(self, node):
        """Lower a recognized typed `json.loads(...)` to a direct call to the
        generated per-class C decoder, registering the decoder + lexer prelude."""
        m = self._match_json_decode(node)
        if m is None:
            return None
        ci, src = m
        self._json_used = True
        if ci not in self._json_decoders:
            self._json_decoders.append(ci)
        return "_json_decode_%s(%s)" % (ci.csym, self.as_str(src))

    _JSON_DEFAULTS = {"char*": '""', "int": "0", "long": "0", "short": "0",
                      "double": "0.0", "float": "0.0f", "bool": "0",
                      "char": "0", "obj": "OBJ_NONE"}

    def _json_field_parse(self, ct, slot):
        """C statements parsing one JSON value at `p` into struct slot `slot`
        (typed `ct`), advancing `p`. A class-pointer field is decoded inline by
        the nested class's cursor decoder (which is registered for emission).
        Unknown/complex types skip the value."""
        if ct == "char*":
            return "p = _json_str(p, &%s);" % slot
        if ct == "long":
            return "p = _json_long(p, &%s);" % slot
        if ct in ("int", "short", "char"):
            return ("{ long _jv; p = _json_long(p, &_jv); %s = (%s)_jv; }"
                    % (slot, ct))
        if ct == "double":
            return "p = _json_double(p, &%s);" % slot
        if ct == "float":
            return ("{ double _jv; p = _json_double(p, &_jv); %s = (float)_jv; }"
                    % slot)
        if ct == "bool":
            return "{ int _jv; p = _json_bool(p, &_jv); %s = _jv; }" % slot
        # nested object: a field typed as a known class pointer is built by that
        # class's cursor decoder, which advances p past the sub-object.
        if isinstance(ct, str) and ct.endswith("*"):
            sub = self.classes.get(ct[:-1]) or self._ci_by_csym(ct[:-1])
            if sub is not None:
                if sub not in self._json_decoders:
                    self._json_decoders.append(sub)
                return "%s = _json_decode_%s_at(&p);" % (slot, sub.csym)
        return "p = _json_skip(p);"

    def _json_list_field_parse(self, elem_ct, slot):
        """C block parsing a JSON array at `p` into a boxed list assigned to
        `slot` (an obj field). Each element is parsed by its scalar type or, for
        a class-pointer element, by the nested cursor decoder, then boxed."""
        if elem_ct in ("double", "float"):
            one = "double _ev; p = _json_double(p, &_ev); list_append(_l, OBJ_FLOAT(_ev));"
        elif elem_ct == "char*":
            one = "char* _ev; p = _json_str(p, &_ev); list_append(_l, OBJ_STR(_ev));"
        elif elem_ct == "bool":
            one = "int _ev; p = _json_bool(p, &_ev); list_append(_l, OBJ_BOOL(_ev));"
        elif elem_ct and elem_ct.endswith("*") and \
                (self.classes.get(elem_ct[:-1]) or self._ci_by_csym(elem_ct[:-1])):
            sub = self.classes.get(elem_ct[:-1]) or self._ci_by_csym(elem_ct[:-1])
            if sub not in self._json_decoders:
                self._json_decoders.append(sub)
            one = ("list_append(_l, OBJ_OBJ(_json_decode_%s_at(&p)));" % sub.csym)
        else:                                  # int/long/short/char
            one = "long _ev; p = _json_long(p, &_ev); list_append(_l, OBJ_INT(_ev));"
        return ("obj _l = list_new(); p = _json_ws(p); "
                "if (*p == '[') { p++; while (1) { p = _json_ws(p); "
                "if (*p == ']' || !*p) { if (*p == ']') p++; break; } "
                "{ %s } p = _json_ws(p); "
                "if (*p == ',') { p++; continue; } "
                "if (*p == ']') { p++; break; } break; } } "
                "else { p = _json_skip(p); } %s = _l;" % (one, slot))

    def _json_decoder_fn(self, ci):
        """Emit the generated decoder for `ci`: a cursor form
        `<Cls>* _json_decode_<Cls>_at(const char** pp)` that parses one object
        starting at `*pp` and advances it (so a nested field can decode inline),
        plus a thin public `<Cls>* _json_decode_<Cls>(const char*)` wrapper."""
        fields = ci.full_fields()
        cs = ci.csym
        out = ["static %s* _json_decode_%s_at(const char** _pp) {" % (cs, cs)]
        out.append("    %s* o = (%s*)malloc(sizeof *o);" % (cs, cs))
        if cs not in self._pod_set:
            out.append("    o->_hdr.type = &%s_type;" % cs)
        for fn, ft in fields:
            dflt = self._JSON_DEFAULTS.get(ft, "0")
            out.append("    o->%s = %s;" % (self.fnsym(fn), dflt))
        out.append("    const char* p = _json_ws(*_pp);")
        out.append("    if (*p != '{') { *_pp = p; return o; }")
        out.append("    p++;")
        out.append("    while (1) {")
        out.append("        p = _json_ws(p);")
        out.append("        if (*p == '}' || !*p) { if (*p == '}') p++; break; }")
        out.append("        char _k[128];")
        out.append("        p = _json_key(p, _k, 128);")
        out.append("        p = _json_ws(p); if (*p == ':') p++; p = _json_ws(p);")
        # field name -> constructor annotation (to spot `list[...]` fields, whose
        # struct ctype is the boxed obj and so loses the element type).
        field_ann = {}
        _init = ci.methods.get("__init__")
        if _init is not None:
            for _a in _init.args.args[1:]:
                if _a.annotation is not None:
                    field_ann[_a.arg] = _a.annotation
        first = True
        for fn, ft in fields:
            kw = "if" if first else "else if"
            first = False
            slot = "o->%s" % self.fnsym(fn)
            ann = field_ann.get(fn)
            elem = ann_elem_ctype(ann) if ann is not None else None
            if elem is not None:
                body = self._json_list_field_parse(elem, slot)
            else:
                body = self._json_field_parse(ft, slot)
            out.append('        %s (strcmp(_k, "%s") == 0) { %s }'
                       % (kw, fn, body))
        if not first:
            out.append("        else { p = _json_skip(p); }")
        else:
            out.append("        p = _json_skip(p);")
        out.append("        p = _json_ws(p);")
        out.append("        if (*p == ',') { p++; continue; }")
        out.append("        if (*p == '}') { p++; break; }")
        out.append("        break;")
        out.append("    }")
        out.append("    *_pp = p;")
        out.append("    return o;")
        out.append("}")
        out.append("static %s* _json_decode_%s(const char* s) {" % (cs, cs))
        out.append("    const char* p = s; return _json_decode_%s_at(&p);" % cs)
        out.append("}")
        return out

    def _typed_list_literal(self, ct, listnode):
        """Build a typed list from a list literal as a statement-expression:
        ({ _tlist_T* _t = _tlist_T_new(n); _tlist_T_push(_t, e0); ...; _t; })."""
        name = ct[:-1]                       # _tlist_T  (drop the '*')
        n = len(listnode.elts)
        parts = ["%s* _tl = %s_new(%d);" % (name, name, n if n else 4)]
        for e in listnode.elts:
            parts.append("%s_push(_tl, %s);" % (name, self.expr(e)))
        parts.append("_tl;")
        return "({ " + " ".join(parts) + " })"

    def _note_vararg(self, name):
        """Remember that `name` holds a function's collected `*args`."""
        if not hasattr(self, "_vararg_names"):
            self._vararg_names = set()
        self._vararg_names.add(name)

    def _is_vararg_name(self, node):
        return (isinstance(node, ast.Name)
                and node.id in getattr(self, "_vararg_names", ()))

    def _emit_str_mod(self, fmt_expr, arg_exprs):
        """`fmt % args` without obj-through-varargs: args go in a stack array."""
        n = len(arg_exprs)
        if n == 0:
            return "str_mod(%s, (obj*)0, 0)" % fmt_expr
        self._list_tmp = getattr(self, "_list_tmp", 0) + 1
        v = "_sm%d" % self._list_tmp
        stores = " ".join("%s[%d] = %s;" % (v, i, e)
                          for i, e in enumerate(arg_exprs))
        return "({ obj %s[%d]; %s str_mod(%s, %s, %d); })" % (
            v, n, stores, fmt_expr, v, n)

    def _emit_call_obj(self, clo_expr, arg_exprs):
        """call_obj without obj-through-varargs: args go into a stack array."""
        n = len(arg_exprs)
        if n == 0:
            return "call_obj_a(%s, (obj*)0, 0)" % clo_expr
        self._list_tmp = getattr(self, "_list_tmp", 0) + 1
        v = "_ca%d" % self._list_tmp
        stores = " ".join("%s[%d] = %s;" % (v, i, e)
                          for i, e in enumerate(arg_exprs))
        return "({ obj %s[%d]; %s call_obj_a(%s, %s, %d); })" % (
            v, n, stores, clo_expr, v, n)

    def _emit_dict_of(self, flat_exprs):
        """dict_of without obj-through-varargs. flat_exprs = k0,v0,k1,v1,..."""
        npairs = len(flat_exprs) // 2
        if npairs == 0:
            return "dict_new()"
        self._list_tmp = getattr(self, "_list_tmp", 0) + 1
        v = "_da%d" % self._list_tmp
        stores = " ".join("%s[%d] = %s;" % (v, i, e)
                          for i, e in enumerate(flat_exprs))
        return "({ obj %s[%d]; %s dict_of_a(%s, %d); })" % (
            v, len(flat_exprs), stores, v, npairs)

    def _list_literal(self, elem_exprs, builder="list_from"):
        """Build a list from rendered element exprs without C varargs: store
        them into a stack array and hand the builder a pointer. A 16-byte obj
        passed through `...` mis-lowers on some backends (only the first arg
        survives), so list/set literals avoid varargs entirely. `builder` is
        the runtime function (list_from, or set_from for de-duplicated sets)."""
        n = len(elem_exprs)
        if n == 0:
            return "list_new()"
        self._list_tmp = getattr(self, "_list_tmp", 0) + 1
        v = "_lt%d" % self._list_tmp
        stores = " ".join("%s[%d] = %s;" % (v, i, e)
                          for i, e in enumerate(elem_exprs))
        return "({ obj %s[%d]; %s %s(%s, %d); })" % (v, n, stores, builder, v, n)

    def ex_List(self, node):
        return self._list_literal([self.wrap_obj(e) for e in node.elts])

    def ex_Tuple(self, node):
        if not node.elts:
            return "list_new() /* () */"
        return self._list_literal([self.wrap_obj(e) for e in node.elts])

    def ex_Set(self, node):
        if not node.elts:
            return "({ obj _es = list_new(); _es.tag = T_SET; _es; })"
        return self._list_literal(
            [self.wrap_obj(e) for e in node.elts],
            builder="set_from") + " /* set */"

    def ex_Dict(self, node):
        pairs = [(k, v) for k, v in zip(node.keys, node.values)
                 if k is not None]
        if not pairs:
            return "dict_new()"
        flat = []
        for k, v in pairs:
            flat.append(self.wrap_obj(k))
            flat.append(self.wrap_obj(v))
        return self._emit_dict_of(flat)

    def ex_ListComp(self, node):
        return self.lower_comp(node, "list")

    def ex_SetComp(self, node):
        return self.lower_comp(node, "set")

    def ex_DictComp(self, node):
        return self.lower_comp(node, "dict")

    def ex_GeneratorExp(self, node):
        return self.lower_comp(node, "list")

    def ex_Lambda(self, node):
        if len(node.args.args) == 1 and isinstance(node.body, ast.Name) and \
                node.body.id == node.args.args[0].arg and \
                not node.args.vararg and not node.args.kwarg:
            return "make_closure(&identity__tramp, OBJ_NONE)"
        return "make_closure(&identity__tramp, OBJ_NONE)"

    def ex_Starred(self, node):
        v = node.value
        if isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add):
            return "obj_add(%s, %s)" % (self.expr(v.left), self.expr(v.right))
        return self.expr(v)

    def ex_JoinedStr(self, node):
        fmt, exprs = [], []
        for part in node.values:
            if isinstance(part, ast.Constant):
                fmt.append(str(part.value))
            elif isinstance(part, ast.FormattedValue):
                fmt.append("{}")
                exprs.append(self.wrap_obj(part.value))
        lit = c_string("".join(fmt))
        n = len(exprs)
        if n == 0:
            return "pyfmt_a(%s, (obj*)0, 0)" % lit
        self._list_tmp = getattr(self, "_list_tmp", 0) + 1
        v = "_pf%d" % self._list_tmp
        stores = " ".join("%s[%d] = %s;" % (v, i, e)
                          for i, e in enumerate(exprs))
        return "({ obj %s[%d]; %s pyfmt_a(%s, %s, %d); })" % (
            v, n, stores, lit, v, n)

    def ex_FormattedValue(self, node):
        return self.expr(node.value)

    def binop_sym(self, op):
        return {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
                ast.Mod: "%", ast.FloorDiv: "/", ast.BitOr: "|",
                ast.BitAnd: "&", ast.BitXor: "^", ast.LShift: "<<",
                ast.RShift: ">>", ast.Pow: "POW"}.get(type(op), "/*op*/")

    def guess_from_value(self, node):
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool):
                return "bool"
            if isinstance(v, int):
                return "int"
            if isinstance(v, str):
                return "char*"
            if isinstance(v, float):
                return "double"
        if isinstance(node, (ast.Compare, ast.BoolOp)):
            return "bool"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in self.classes:
                return node.func.id + "*"
            if node.func.id in ("str", "bytes"):
                return "char*"
            if node.func.id == "getattr" and not (
                    len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)):
                return OBJ          # dynamic attribute -> Tier-2 obj
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "index":   # list.index -> list_index (obj int)
                return OBJ
        if isinstance(node, ast.JoinedStr):
            return "char*"
        if isinstance(node, (ast.List, ast.Dict, ast.Set, ast.Tuple)):
            return OBJ
        return None

    # ---- source helpers --------------------------------------------------

    def src(self, node):
        try:
            return ast.unparse(node)
        except Exception:
            return type(node).__name__

    def src1(self, node):
        s = self.src(node).replace("\n", " ").replace("*/", "* /")
        return s if len(s) <= 120 else s[:117] + "..."


def is_super_call(node):
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
        and node.func.id == "super"


# ==========================================================================
# String literal helper
# ==========================================================================

def c_string(s):
    out = ['"']
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif 32 <= ord(ch) < 127:
            out.append(ch)
        else:
            out.append("\\x%02x" % (ord(ch) & 0xff))
    out.append('"')
    return "".join(out)


# ==========================================================================
# Driver
# ==========================================================================

def _uses_eval_builtin(path):
    """True if the source needs the MicroPython core linked: it calls the
    eval()/exec() builtins, or imports the `micropython` first-class library."""
    if not path.endswith(".py"):
        return False
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except Exception:
        return False
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id in ("eval", "exec"):
            return True
        if isinstance(n, ast.Import) and any(
                a.name == "micropython" for a in n.names):
            return True
        if isinstance(n, ast.ImportFrom) and n.module == "micropython":
            return True
    return False


# Complete-ish MicroPython embedding config emitted for the standalone (non-fork)
# build, so the internal stdlib (sys, io, errno, math, cmath, collections,
# struct, array) is compiled in and importable. Imports resolve against the
# built-in module table only -- no VFS -- so the port glue below is all that is
# needed. The amalgamation is (re)generated against this file.
MP_EMBED_CONFIG_H = r'''/* generated by ShivyCX py2c -- MicroPython embedding config */
#include <port/mpconfigport_common.h>
#define MICROPY_CONFIG_ROM_LEVEL        (MICROPY_CONFIG_ROM_LEVEL_MINIMUM)
#define MICROPY_ENABLE_COMPILER         (1)
#define MICROPY_ENABLE_GC               (1)
#define MICROPY_PY_GC                   (1)
#define MICROPY_ENABLE_EXTERNAL_IMPORT  (0)
#define MICROPY_PY_BUILTINS_FLOAT       (1)
#define MICROPY_FLOAT_IMPL              (MICROPY_FLOAT_IMPL_DOUBLE)
#define MICROPY_CPYTHON_COMPAT          (1)
#define MICROPY_PY_BUILTINS_STR_OP_MODULO (1)
#define MICROPY_PY_SYS                  (1)
#define MICROPY_PY_SYS_PLATFORM         "shivycx"
#define MICROPY_PY_ERRNO                (1)
#define MICROPY_PY_IO                   (1)
#define MICROPY_PY_MATH                 (1)
#define MICROPY_PY_CMATH                (1)
#define MICROPY_PY_COLLECTIONS          (1)
#define MICROPY_PY_COLLECTIONS_ORDEREDDICT (1)
#define MICROPY_PY_STRUCT               (1)
#define MICROPY_PY_ARRAY                (1)
#define MICROPY_PY_MICROPYTHON          (1)
'''

# Port glue emitted alongside the bridge: this embedding has no filesystem, so
# `open` is a stub (lets the io and builtins modules link) and imports stay on
# the built-in module table.
MP_PORT_GLUE_C = r'''/* generated by ShivyCX py2c -- MicroPython port glue (no filesystem) */
#include "py/runtime.h"
#include "py/builtin.h"
#include "py/mperrno.h"
static mp_obj_t shivyc_builtin_open(size_t n_args, const mp_obj_t *args,
                                    mp_map_t *kwargs) {
    (void)n_args; (void)args; (void)kwargs;
    mp_raise_OSError(MP_ENOENT);
    return mp_const_none;
}
MP_DEFINE_CONST_FUN_OBJ_KW(mp_builtin_open_obj, 1, shivyc_builtin_open);
'''


def write_runtime(out_dir, mp_bridge=False, minipy_eval=False):
    with open(os.path.join(out_dir, "shivyc_rt.h"), "w") as f:
        f.write(RUNTIME_H)
    with open(os.path.join(out_dir, "shivyc_rt.c"), "w") as f:
        f.write(RUNTIME_C)
        if not mp_bridge:
            # Define the dynamic-dispatch functions against the object model;
            # with the bridge they are provided by the micropython core instead.
            f.write(MP_NOBRIDGE_C)
            if minipy_eval:
                # Standalone eval(): the native expression evaluator provides
                # rpy_eval* without pulling in the micropython core.
                f.write(MINIPY_EVAL_C)
    if mp_bridge:
        with open(os.path.join(out_dir, "mp_stdlib_bridge.h"), "w") as f:
            f.write(MP_BRIDGE_H)
        with open(os.path.join(out_dir, "mp_stdlib_bridge.c"), "w") as f:
            f.write(MP_BRIDGE_C)
        # Complete-embedding config + port glue, so a standalone build links the
        # MicroPython internal stdlib (sys/math/collections/struct/array/...).
        # Named distinctly so it never shadows a fork's own mpconfigport.h; the
        # embed build copies it into place (see the mrpy README).
        with open(os.path.join(out_dir, "shivyc_mpconfigport.h"), "w") as f:
            f.write(MP_EMBED_CONFIG_H)
        with open(os.path.join(out_dir, "shivyc_mpport.c"), "w") as f:
            f.write(MP_PORT_GLUE_C)


def relative_stdlib_slug(stdlib_dir, py_path):
    rel = os.path.relpath(os.path.abspath(py_path), os.path.abspath(stdlib_dir))
    rel = rel.replace(os.sep, "/")
    slug = rel.replace("/", "_").replace("-", "_")
    if slug.endswith(".py"):
        slug = slug[:-3]
    return slug


def _stdlib_context(path, stdlib_dir=None):
    ap = os.path.abspath(path)
    parts = ap.split(os.sep)
    if stdlib_dir is None and "python-stdlib" in parts:
        i = parts.index("python-stdlib")
        stdlib_dir = os.sep.join(parts[: i + 1])
    if stdlib_dir and os.path.commonpath([ap, os.path.abspath(stdlib_dir)]) == \
            os.path.abspath(stdlib_dir):
        modname = relative_stdlib_slug(stdlib_dir, path)
        return modname, None, stdlib_dir
    if "shivyc" in parts:
        i = parts.index("shivyc")
        base_dir = os.sep.join(parts[:i]) or os.sep
        rel = parts[i:]
        if len(rel) == 2:
            modname = rel[1][:-3]
        else:
            modname = ".".join(rel)[:-3]
        return modname, base_dir, None
    return os.path.splitext(os.path.basename(path))[0], os.path.dirname(ap), None


SHOW_OBJECT_MODEL = False


#: The digest a `.py` publishes so a `.cpp` can inherit from its classes,
#: and vice versa. One artifact, two producers, two consumers -- see
#: CPPRPY.md section 4. Bumped when the shape changes; a consumer that
#: reads a version it does not know refuses rather than guesses.
DECLS_VERSION = 1


def emit_decls(t, modname):
    """The class-interface digest for a transpiled module.

    What a *consumer* needs is narrower than what the transpiler knows: the
    struct's name and field types (to lay out a derived class), the
    descriptor symbol (to link a base chain), and the canonical vtable slot
    list (to emit a table that is layout-compatible slot for slot).

    The slot list is recorded **once, for the module**, not per class. That
    is not a compression: py2c gives every class in a module the same
    `TypeInfo` layout, so the vtable is a property of the hierarchy rather
    than of any one class, and writing it per class would invite a consumer
    to believe two classes could disagree.

    Field *access* is deliberately not described. py2c repeats a base's
    fields in the derived struct (`{Obj, id, side}`) where cpprust nests the
    base (`{{vptr, id}, side}`); those are the same bytes in the same order
    but spelled differently, so each side reaches a field its own way and
    only the layout has to agree.
    """
    slots = []
    for mname in sorted(VTABLE_METHODS):
        ret, params, _ = t.method_proto(mname)
        slots.append({"name": mname, "ret": ret, "params": list(params)})
    classes = []
    for ci in t.class_order:
        methods = []
        for mname in sorted(ci.methods):
            methods.append({"name": mname,
                            "virtual": mname in VTABLE_METHODS,
                            "fn": "%s_%s" % (ci.csym, mname)})
        classes.append({
            "name": ci.name,
            "struct": ci.csym,
            "base": ci.base.name if ci.base else ci.base_name,
            "ibases": list(ci.extra_base_names),
            "typeinfo": "%s_type" % ci.csym,
            "fields": [{"name": n, "ctype": c} for n, c in ci.full_fields()],
            "methods": methods,
        })
    return {"version": DECLS_VERSION, "lang": "rpython", "module": modname,
            "descriptor": {"type": "TypeInfo",
                           "header": ["name", "base", "fields", "tostr",
                                      "eq", "addfn", "objsize"],
                           "slots": slots},
            "classes": classes}


#: C type -> the annotation the stub spells it with, and the zero a stub
#: `__init__` assigns so field-type inference lands on the same C type.
_DECLS_CTYPES = {"int": ("int", "0"), "long": ("long", "0"),
                 "double": ("float", "0.0"), "float": ("float", "0.0"),
                 "bool": ("bool", "0"), "char*": ("str", '""'),
                 "char *": ("str", '""'), "const char*": ("str", '""'),
                 "const char *": ("str", '""')}


def _stub_from_decls(digest, modname, path):
    """A Python module, as source text, describing a C++ digest's classes.

    This is the whole trick of the consumer: rather than synthesizing
    `ClassInfo` objects by hand -- a structure with two dozen fields and a
    parser's worth of downstream expectations -- the digest is rendered as
    the *Python a matching module would have been written as*, and the
    ordinary import path parses it. Everything after `ast.parse` is code
    that already works.

    The stub is interface, not implementation: bodies are zeros. That is
    correct because an importer never emits an imported class's code -- it
    externs the symbols and calls them -- and the symbols are exactly what
    `cpprust --emit-decls` gave external linkage to.

    Field types map through `_DECLS_CTYPES`; a C type outside it is
    refused by name rather than guessed at, since a wrong field type here
    is a wrong struct offset in every importer.
    """
    if digest.get("lang") != "cpp":
        return None
    lines = ["# synthesized from %s -- a C++ module's class digest.\n"
             "# Interface only; the implementation is the C that cpprust\n"
             "# emitted, linked in by the build." % os.path.basename(path)]
    slot_sig = {}
    for sl in (digest.get("descriptor") or {}).get("slots") or ():
        slot_sig[sl["name"]] = sl
    for c in digest.get("classes") or ():
        # Only polymorphic classes are rendered. A C++ class with no
        # virtual methods is a bare struct -- no vptr, no descriptor --
        # and every py2c class carries an `Obj` header at offset zero, so
        # laying one over the other would shift every field by a word.
        # Absent from the stub means importing it is a missing-name
        # diagnostic rather than a silently wrong layout.
        if not any(m.get("virtual") for m in c.get("methods") or ()):
            continue
        base = c.get("base")
        lines.append("class %s%s:" % (c["name"],
                                      "(%s)" % base if base else ""))
        body = []
        fields = c.get("fields") or ()
        # Fields are assigned from *annotated parameters*, not literals:
        # `self.cap = 0` makes py2c infer a boxed `obj` field, and a
        # derived class repeats a base's fields at the base's types -- so
        # one lazily-typed stub field mislays every subclass's struct.
        # (Caught as `obj cap;` in the importer where the C++ struct has
        # `int cap;`.)
        inits, params = [], ["self"]
        first_field = None
        for f in fields:
            ct = f["ctype"].strip()
            if ct not in _DECLS_CTYPES:
                raise RuntimeError(
                    "%s: field %s.%s has C type %r, which the digest "
                    "consumer has no annotation for. Add it to "
                    "_DECLS_CTYPES if the mapping is sound, or drop the "
                    "field from the published class -- a guessed type here "
                    "is a wrong struct offset in every importer."
                    % (path, c["name"], f["name"], ct))
            ann = _DECLS_CTYPES[ct][0]
            params.append('%s: "%s"' % (f["name"], ann))
            inits.append("        self.%s = %s" % (f["name"], f["name"]))
            if first_field is None:
                first_field = f["name"]
        if inits:
            body.append("    def __init__(%s):" % ", ".join(params))
            body.extend(inits)
        for m in c.get("methods") or ():
            sig = slot_sig.get(m["name"])
            if sig is None:
                # A method the digest names but carries no signature for
                # (cpprust only records full signatures for virtuals). It
                # cannot be *called* from this side without one, so it is
                # simply absent from the stub -- absent means a call is a
                # missing-attribute diagnostic, not a miscompiled one.
                continue
            params = ["self"]
            for prm in sig.get("params") or ():
                prm = prm.strip()
                if not prm:
                    continue
                bits = prm.rsplit(" ", 1)
                pct = bits[0].strip() if len(bits) == 2 else prm
                pnm = bits[1].strip("* ") if len(bits) == 2 else "a"
                ann = _DECLS_CTYPES.get(pct, ("int", "0"))[0]
                params.append('%s: "%s"' % (pnm, ann))
            ret = sig.get("ret", "int").strip()
            rann, rzero = _DECLS_CTYPES.get(ret, ("int", "0"))
            # The body reads an instance field when the class has one. Not
            # decoration: py2c's POD classifier treats a class whose
            # methods never touch instance state as vtable-free, importers
            # then skip its slot -- and a C++ class whose methods are all
            # `return 0` stubs is exactly that shape. The read declares
            # what is true of the real implementation: these methods use
            # the object. (Caught as a NULL `take` slot and a segfault.)
            touch = ("self.%s" % first_field) if first_field else None
            wr = ("        %s = %s" % (touch, touch)) if touch else None
            if ret == "void":
                body.append("    def %s(%s):" % (m["name"],
                                                 ", ".join(params)))
                if wr:
                    body.append(wr)
                body.append("        return")
            else:
                body.append('    def %s(%s) -> "%s":'
                            % (m["name"], ", ".join(params), rann))
                if wr:
                    body.append(wr)
                if touch and rann in ("int", "long"):
                    body.append("        return %s * 0" % touch)
                else:
                    body.append("        return %s" % rzero)
        if not body:
            body = ["    pass"]
        lines.extend(body)
        lines.append("")
    return "\n".join(lines) + "\n"


def transpile_file(path, out_dir, stdlib_dir=None):
    # Profile-guided auto-typing: when RPY_PROFILE_GENERATE is set, profile the
    # user's script and compile an auto-annotated copy instead. Single user
    # scripts only -- never stdlib or the compiler's own modules. Best-effort:
    # autotype() returns the original path on any failure, so a build is never
    # broken by profiling.
    if (os.environ.get("RPY_PROFILE_GENERATE") or
            os.environ.get("RPY_PROFILE_USE")) and stdlib_dir is None \
            and path.endswith(".py") \
            and "shivyc" not in os.path.abspath(path).split(os.sep):
        try:
            _here = os.path.dirname(os.path.abspath(__file__))
            if _here not in sys.path:
                sys.path.insert(0, _here)
            import rpy_pgo
            path = rpy_pgo.autotype(
                path,
                profile_in=os.environ.get("RPY_PROFILE_USE") or None,
                profile_out=os.environ.get("RPY_PROFILE_OUT") or None)
        except Exception as e:
            print("  pgo: skipped (%s)" % e)
    src = open(path, encoding="utf-8").read()
    # Neo-Geo specialisation: a source that `import neogeo`s declares a scene out
    # of constant ASCII art. Rather than transpile the declarative API, run the
    # ASCII->pixel-art conversion now (at translate time) and emit the finished
    # palette/tile data as C, plus a small driver -- so the on-target program is
    # just data and a copy loop. (See tools/rpy_neogeo_integration.py.)
    if "neogeo" in src:
        try:
            _here = os.path.dirname(os.path.abspath(__file__))
            if _here not in sys.path:
                sys.path.insert(0, _here)
            import rpy_neogeo_integration as _ng
            if _ng.imports_neogeo(path):
                scene = _ng.bake_source(path)
                cbody = _ng.scene_to_c(scene) + _ng.scene_main_c(scene)
                base = os.path.splitext(os.path.basename(path))[0]
                cpath = os.path.join(out_dir, base + ".c")
                with open(cpath, "w") as _cf:
                    _cf.write(cbody)
                print("  neogeo: baked %d layer(s) from ASCII art -> %s"
                      % (len(scene.backgrounds) + len(scene.sprites), cpath))
                return cpath, None
        except Exception as _e:
            print("  neogeo: bake failed (%s)" % _e)
    modname, base_dir, stdlib_root = _stdlib_context(path, stdlib_dir)
    py_mod = py_modname_from_path(path, stdlib_root) if stdlib_root else None
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as e:
        print("  SYNTAX ERROR in %s: %s" % (path, e))
        return None, str(e)
    try:
        pod_ok = stdlib_root is None and \
            "shivyc" not in os.path.abspath(path).split(os.sep)
        _t = Transpiler(modname, base_dir, stdlib_root=stdlib_root,
                        py_modname=py_mod, pod_classes=pod_ok)
        _t.src_name = os.path.basename(path)
        out = _t.run(tree)
    except Unsupported as e:
        print("  FAIL %s: %s" % (path, e))
        return None, str(e)
    out_path = os.path.join(out_dir, modname + ".c")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out)
    if EMIT_DECLS:
        import json as _json
        dpath = os.path.join(out_dir, modname + ".decls.json")
        with open(dpath, "w", encoding="utf-8") as f:
            _json.dump(emit_decls(_t, modname), f, indent=1, sort_keys=True)
        print("  decls -> %s" % dpath)
    # Always surface the high-precision "object stored in a scalar field"
    # diagnostics (these indicate a real truncation bug). The full POD/object
    # table is opt-in via --show-object-model.
    try:
        for _w in _t.object_model_warnings(tree):
            print("  WARNING: %s" % _w, file=sys.stderr)
        for _w in _t.copy_shadow_warnings(tree):
            print("  WARNING: %s" % _w, file=sys.stderr)
        if SHOW_OBJECT_MODEL:
            print(_t.object_model_report(tree))
    except Exception as _e:
        if SHOW_OBJECT_MODEL:
            print("  (object-model report unavailable: %s)" % _e)
    return out_path, None


def translate_stdlib(stdlib_dir, out_dir, report_path):
    stdlib_dir = os.fspath(stdlib_dir)
    out_dir = os.fspath(out_dir)
    report_path = os.fspath(report_path)
    ok, failed = [], []
    files = []
    for walk_root, _dirs, filenames in os.walk(stdlib_dir):
        for fn in filenames:
            if fn.endswith(".py"):
                files.append(os.path.join(walk_root, fn))
    files = sorted(files)
    os.makedirs(out_dir, exist_ok=True)
    for old in os.listdir(out_dir):
        if old.endswith(".c") or old.endswith(".h"):
            os.remove(os.path.join(out_dir, old))
    write_runtime(str(out_dir), mp_bridge=True)
    for py_path in files:
        rel = os.path.relpath(py_path, stdlib_dir).replace(os.sep, "/")
        res, err = transpile_file(str(py_path), str(out_dir), str(stdlib_dir))
        if res is None:
            failed.append((rel, err or "unknown error"))
        else:
            ok.append(rel)
    lines = [
        "micropython-lib python-stdlib -> C (via tools/py2c.py)",
        "stdlib: %s" % stdlib_dir,
        "output: %s" % out_dir,
        "",
        "OK:   %d" % len(ok),
        "FAIL: %d" % len(failed),
        "",
    ]
    if ok:
        lines.append("=== translated ===")
        lines.extend("  %s" % name for name in ok)
        lines.append("")
    if failed:
        lines.append("=== failures ===")
        for name, err in failed:
            lines.append("  %s" % name)
            lines.append("    %s" % err)
        lines.append("")
    with open(report_path, "w", encoding="utf-8") as _rf:
        _rf.write("\n".join(lines))
    return ok, failed


def transpile_file_legacy(path, out_dir):
    res, _err = transpile_file(path, out_dir)
    return res


def print_conventions():
    print(__doc__)


def _rpyinc_one(argv):
    """`--rpyinc SRC --out DIR`: transpile one file for `shivyc/rpyinc.py`.

    A separate entry point from the ordinary CLI because the caller needs two
    facts back -- where the C landed, and whether the runtime is required --
    and parsing them out of the human-readable listing would be brittle. The
    answer is written to stdout as two lines:

        <path to the generated .c>
        rt | pure

    `rpyinc` runs this in a *subprocess* rather than importing py2c, for the
    same reason `preproc.py` shells out to `cpprust.py`: an import compiles
    into a cross-module symbol reference, and py2c is not part of the
    self-hosted source set, so the reference is undefined at link time. A
    subprocess leaves no symbol behind, and the self-hosted compiler can then
    lower an rpython include by running it.
    """
    src = out = None
    i = 0
    while i < len(argv):
        if argv[i] == "--rpyinc":
            src = argv[i + 1]
            i += 2
        elif argv[i] == "--out":
            out = argv[i + 1]
            i += 2
        else:
            i += 1
    if not src or not out:
        sys.stderr.write("usage: py2c.py --rpyinc <src.py> --out <dir>\n")
        return 2
    try:
        os.makedirs(out)
    except OSError:
        pass
    set_local_module_dirs([src])
    set_compiled_modules([src])
    uses_eval = _uses_eval_builtin(src)
    write_runtime(out, mp_bridge=False, minipy_eval=uses_eval)
    try:
        produced, err = transpile_file(src, out)
    except Exception as e:
        sys.stderr.write("py2c: %s\n" % e)
        return 1
    if err or not produced:
        sys.stderr.write("py2c: %s\n" % (err or "no output"))
        return 1
    sys.stdout.write("%s\n" % produced)
    return 0


def main(argv):
    if "--rpyinc" in argv:
        return _rpyinc_one(argv)
    out_dir = "/tmp"
    stdlib_dir = None
    report_path = None
    profile_gen = bool(os.environ.get("RPY_PROFILE_GENERATE"))
    profile_use = os.environ.get("RPY_PROFILE_USE") or None
    profile_out = os.environ.get("RPY_PROFILE_OUT") or None
    default_profile = "/tmp/rpy_profile.json"
    files = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--out":
            out_dir = argv[i + 1]
            i += 2
            continue
        if a in ("-fprofile-generate", "--fprofile-generate"):
            profile_gen = True
            i += 1
            continue
        if a.startswith("-fprofile-generate=") or \
                a.startswith("--fprofile-generate="):
            profile_gen = True
            profile_out = a.split("=", 1)[1]
            i += 1
            continue
        if a in ("-fprofile-use", "--fprofile-use"):
            profile_use = default_profile
            i += 1
            continue
        if a.startswith("-fprofile-use=") or a.startswith("--fprofile-use="):
            profile_use = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--stdlib-dir":
            stdlib_dir = argv[i + 1]
            i += 2
            continue
        if a == "--report":
            report_path = argv[i + 1]
            i += 2
            continue
        if a == "--decls":
            # Also write `<module>.decls.json`: the class-interface digest a
            # `.cpp` reads to inherit from these classes. Off by default --
            # a build that mixes no languages has no use for it.
            global EMIT_DECLS
            EMIT_DECLS = True
            i += 1
            continue
        if a in ("--show-object-model", "--show-objmodel"):
            global SHOW_OBJECT_MODEL
            SHOW_OBJECT_MODEL = True
            i += 1
            continue
        if a in ("--conventions", "-c"):
            print_conventions()
            return
        files.append(a)
        i += 1

    if stdlib_dir is not None:
        if report_path is None:
            report_path = os.path.join(out_dir, "stdlib_translate_report.txt")
        ok, failed = translate_stdlib(stdlib_dir, out_dir, report_path)
        print("translated %d file(s), %d failed" % (len(ok), len(failed)))
        print("report: %s" % report_path)
        sys.exit(1 if failed else 0)

    if not files:
        here = os.path.dirname(os.path.abspath(__file__))
        shivyc = os.path.normpath(os.path.join(here, "..", "shivyc"))
        if os.path.isdir(shivyc):
            files = sorted(os.path.join(shivyc, f)
                           for f in os.listdir(shivyc) if f.endswith(".py"))
            print("No files given; defaulting to %d files in %s" %
                  (len(files), shivyc))
        else:
            print("error: no input files and no ../shivyc directory", file=sys.stderr)
            sys.exit(2)

    os.makedirs(out_dir, exist_ok=True)
    # Auto-bundle the rpy_torch mini-library when a source imports it.
    try:
        import rpy_torch as _rpy_torch
        files = _rpy_torch.bundle(files)
    except Exception:
        pass
    # Auto-bundle the rpy_plot (matplotlib-style) and rpy_stats (statistics)
    # mini-libraries on import, same mechanism as rpy_torch above.
    try:
        import rpy_plot_integration as _rpy_plot
        files = _rpy_plot.bundle(files)
    except Exception:
        pass
    try:
        import rpy_stats_integration as _rpy_stats
        files = _rpy_stats.bundle(files)
    except Exception:
        pass
    try:
        import rpy_bisect_integration as _rpy_bisect
        files = _rpy_bisect.bundle(files)
    except Exception:
        pass
    try:
        import rpy_heapq_integration as _rpy_heapq
        files = _rpy_heapq.bundle(files)
    except Exception:
        pass
    try:
        import rpy_hashlib_integration as _rpy_hashlib
        files = _rpy_hashlib.bundle(files)
    except Exception:
        pass
    try:
        import rpy_base64_integration as _rpy_base64
        files = _rpy_base64.bundle(files)
    except Exception:
        pass
    # First-class MicroPython: bundle rpy_lib/micropython.py on `import
    # micropython`, so exec_/eval_*/run_file lower to the MicroPython bridge.
    try:
        import rpy_micropython_integration as _rpy_mp
        files = _rpy_mp.bundle(files)
    except Exception:
        pass
    # First-class Wayland / PyQt: bundle rpy_lib/{rwayland,rpyqt}.py and emit the
    # generated glue (runtime + scanned xdg-shell) so no C is hand-written. The
    # integration modules live next to py2c.py; make that directory importable
    # regardless of how py2c was launched (sys.path[0] is normally the script
    # dir, but not when py2c is imported as a module or run via a wrapper).
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)

    def _source_imports(mod):
        # Cheap textual check so we can warn even when the integration import
        # itself fails: True if any input .py imports the given rpython module.
        for _f in files:
            if not _f.endswith(".py"):
                continue
            try:
                _src = open(_f, encoding="utf-8").read()
            except Exception:
                continue
            if ("import " + mod) in _src:
                return True
        return False

    _rwayland_notes = []
    try:
        import rwayland_integration as _rwayland
        files = _rwayland.bundle(files)
        _rwayland_notes = _rwayland.emit_runtime(out_dir, files)
        # PyQt-compatible layer: bundle rpy_lib/rpyqt.py and emit the same runtime
        # (rpyqt binds it directly without importing the rwayland rpython module).
        import rpyqt_integration as _rpyqt
        files = _rpyqt.bundle(files)
        _rwayland_notes += _rpyqt.emit_runtime(out_dir, files)
    except Exception:
        pass                                  # optional integrations; absent on minipy
    # Profile-guided auto-typing. Single .py inputs go through transpile_file's
    # per-file hook (so the ShivyCX front end gets them too); multi-file programs
    # are profiled once here as a set (one run, module-qualified types) so cross
    # -module containers can be typed. -fprofile-use replays a cached profile.
    if profile_gen or profile_use:
        py_files = [f for f in files if f.endswith(".py")]
        if profile_gen and not profile_use and not profile_out:
            profile_out = default_profile     # cache so -fprofile-use can replay
        if len(py_files) == len(files) and len(files) >= 2:
            try:
                _here = os.path.dirname(os.path.abspath(__file__))
                if _here not in sys.path:
                    sys.path.insert(0, _here)
                import rpy_pgo
                mapping = rpy_pgo.autotype_set(
                    files, profile_in=profile_use, profile_out=profile_out)
                files = [mapping.get(f, f) for f in files]
            except Exception as e:
                print("  pgo: skipped (%s)" % e)
            # consumed here -- don't let the per-file hook re-profile.
            for k in ("RPY_PROFILE_GENERATE", "RPY_PROFILE_USE",
                      "RPY_PROFILE_OUT"):
                os.environ.pop(k, None)
        elif len(files) == 1 and files[0].endswith(".py"):
            if profile_gen:
                os.environ["RPY_PROFILE_GENERATE"] = "1"
            if profile_use:
                os.environ["RPY_PROFILE_USE"] = profile_use
            if profile_out:
                os.environ["RPY_PROFILE_OUT"] = profile_out
        else:
            print("  pgo: skipped (need .py input(s))")
    set_local_module_dirs(files)
    set_compiled_modules(files)
    set_build_files(files)
    _, _, stdlib_root = _stdlib_context(files[0] if len(files) == 1 else "", None)
    uses_eval = any(_uses_eval_builtin(p) for p in files)
    # eval() no longer forces the MicroPython core: a standalone program gets the
    # self-contained minipy expression evaluator. The bridge is pulled in only
    # for the actual stdlib (sys/math/...), or when explicitly requested via
    # SHIVYC_MP_EVAL=1 (e.g. to eval expressions outside minipy's grammar).
    mp_bridge = bool(stdlib_dir) or any(
        "python-stdlib" in os.path.abspath(p) for p in files) \
        or (uses_eval and os.environ.get("SHIVYC_MP_EVAL") == "1")
    minipy_eval = uses_eval and not mp_bridge
    write_runtime(out_dir, mp_bridge=mp_bridge, minipy_eval=minipy_eval)
    print("  runtime -> %s/shivyc_rt.{h,c}" % out_dir)
    ok = 0
    for path in files:
        res, _err = transpile_file(path, out_dir)
        if res:
            ok += 1
            print("  %-28s -> %s" % (os.path.basename(path), res))
    print("Transpiled %d/%d files into %s" % (ok, len(files), out_dir))
    for _n in _rwayland_notes:
        print("  rwayland: %s" % _n)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
