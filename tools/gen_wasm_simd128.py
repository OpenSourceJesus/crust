#!/usr/bin/env python3
"""Generate shivyc/include/wasm_simd128.h from shivyc/wasm_simd.py.

    python3 tools/gen_wasm_simd128.py

The header exposes each SIMD operator twice: as the builtin the wasm back end
turns into a single instruction, and as portable scalar C for every other
target. The second form is not a convenience -- it is what lets an ordinary
compiler act as the oracle when checking the vector code, since the same
source then builds and runs natively.

Only operators with a scalar fallback are exposed as `wasm_*` intrinsics, so
that anything the header offers works on both paths. The remaining builtins
are still declared under `__wasm__` and can be called directly by name.

Generating rather than writing this out is the same bargain as the opcode
table: 226 intrinsics from one description of the structure, where a mistake
in a family is a mistake in one visible line.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import shivyc.wasm_simd as S                                  # noqa: E402

OUT = os.path.join(ROOT, "shivyc", "include", "wasm_simd128.h")

CT = {S.K_V128: "v128_t", S.K_I32: "int", S.K_I64: "long",
      S.K_F32: "float", S.K_F64: "double"}

# shape -> (lane count, signed accessor, unsigned accessor, is float)
SHAPE_ACC = {
    "i8x16": (16, "i8", "u8", False),
    "i16x8": (8, "i16", "u16", False),
    "i32x4": (4, "i32", "u32", False),
    "i64x2": (2, "i64", "u64", False),
    "f32x4": (4, "f32", "f32", True),
    "f64x2": (2, "f64", "f64", True),
}

INT_SHAPES = ["i8x16", "i16x8", "i32x4", "i64x2"]
FLOAT_SHAPES = ["f32x4", "f64x2"]

LANE_TYPE = {"i8": "signed char", "u8": "unsigned char",
             "i16": "short", "u16": "unsigned short",
             "i32": "int", "u32": "unsigned int",
             "i64": "long", "u64": "unsigned long",
             "f32": "float", "f64": "double"}


def accessors():
    """Lane getters and setters, one pair per lane type."""
    out = []
    for fld in ("i8", "u8", "i16", "u16", "i32", "u32",
                "i64", "u64", "f32", "f64"):
        ct = LANE_TYPE[fld]
        n = 16 // {"i8": 1, "u8": 1, "i16": 2, "u16": 2, "i32": 4, "u32": 4,
                   "i64": 8, "u64": 8, "f32": 4, "f64": 8}[fld]
        out.append("static %s __wsimd_get_%s(v128_t v, int k) {" % (ct, fld))
        out.append("  %s a[%d]; memcpy(a, v.__v, 16); return a[k];" % (ct, n))
        out.append("}")
        out.append("static void __wsimd_set_%s(v128_t *v, int k, %s x) {"
                   % (fld, ct))
        out.append("  %s a[%d]; memcpy(a, v->__v, 16); a[k] = x;" % (ct, n))
        out.append("  memcpy(v->__v, a, 16);")
        out.append("}")
    return out


def lanewise_binary(name, shape, expr, signed):
    """A scalar fallback for an elementwise two-operand operator."""
    lanes, sf, uf, is_float = SHAPE_ACC[shape]
    acc = sf if signed else uf
    ct = LANE_TYPE[acc]
    fn = "wasm_" + name.replace(".", "_")
    return [
        "static v128_t %s(v128_t a, v128_t b) {" % fn,
        "  v128_t r; int k;",
        "  for (k = 0; k < %d; k++) {" % lanes,
        "    %s x = __wsimd_get_%s(a, k), y = __wsimd_get_%s(b, k);"
        % (ct, acc, acc),
        "    __wsimd_set_%s(&r, k, (%s)(%s));" % (acc, ct, expr),
        "  }",
        "  return r;",
        "}",
    ]


def lanewise_unary(name, shape, expr, signed):
    lanes, sf, uf, is_float = SHAPE_ACC[shape]
    acc = sf if signed else uf
    ct = LANE_TYPE[acc]
    fn = "wasm_" + name.replace(".", "_")
    return [
        "static v128_t %s(v128_t a) {" % fn,
        "  v128_t r; int k;",
        "  for (k = 0; k < %d; k++) {" % lanes,
        "    %s x = __wsimd_get_%s(a, k);" % (ct, acc),
        "    __wsimd_set_%s(&r, k, (%s)(%s));" % (acc, ct, expr),
        "  }",
        "  return r;",
        "}",
    ]


def compare(name, shape, cop, signed):
    """A comparison. A true lane is all ones, not 1 -- which is what makes
    bitselect and the bitmask operators work on the result."""
    lanes, sf, uf, is_float = SHAPE_ACC[shape]
    acc = sf if signed else uf
    ct = LANE_TYPE[acc]
    # The result is written through the *unsigned integer* view of the shape,
    # since all-ones is not representable in a float lane.
    res = {"i8x16": "u8", "i16x8": "u16", "i32x4": "u32", "i64x2": "u64",
           "f32x4": "u32", "f64x2": "u64"}[shape]
    rct = LANE_TYPE[res]
    fn = "wasm_" + name.replace(".", "_")
    return [
        "static v128_t %s(v128_t a, v128_t b) {" % fn,
        "  v128_t r; int k;",
        "  for (k = 0; k < %d; k++) {" % lanes,
        "    %s x = __wsimd_get_%s(a, k), y = __wsimd_get_%s(b, k);"
        % (ct, acc, acc),
        "    __wsimd_set_%s(&r, k, (x %s y) ? (%s)~(%s)0 : (%s)0);"
        % (res, cop, rct, rct, rct),
        "  }",
        "  return r;",
        "}",
    ]


def build():
    exposed = []          # operator names that got a scalar fallback
    body = []

    def emit(lines, name):
        body.extend(lines)
        body.append("")
        exposed.append(name)

    # -- elementwise arithmetic
    for shape in INT_SHAPES:
        emit(lanewise_binary("%s.add" % shape, shape, "x + y", False),
             "%s.add" % shape)
        emit(lanewise_binary("%s.sub" % shape, shape, "x - y", False),
             "%s.sub" % shape)
        if shape != "i8x16":              # no i8x16.mul in the specification
            emit(lanewise_binary("%s.mul" % shape, shape, "x * y", False),
                 "%s.mul" % shape)
        emit(lanewise_unary("%s.neg" % shape, shape, "-x", False),
             "%s.neg" % shape)
        emit(lanewise_unary("%s.abs" % shape, shape, "x < 0 ? -x : x", True),
             "%s.abs" % shape)
    for shape in ["i8x16", "i16x8", "i32x4"]:
        emit(lanewise_binary("%s.min_s" % shape, shape, "x < y ? x : y", True),
             "%s.min_s" % shape)
        emit(lanewise_binary("%s.max_s" % shape, shape, "x > y ? x : y", True),
             "%s.max_s" % shape)
        emit(lanewise_binary("%s.min_u" % shape, shape, "x < y ? x : y",
                             False), "%s.min_u" % shape)
        emit(lanewise_binary("%s.max_u" % shape, shape, "x > y ? x : y",
                             False), "%s.max_u" % shape)
    for shape in FLOAT_SHAPES:
        sfx = "f" if shape == "f32x4" else ""
        for suffix, expr in (("add", "x + y"), ("sub", "x - y"),
                             ("mul", "x * y"), ("div", "x / y")):
            emit(lanewise_binary("%s.%s" % (shape, suffix), shape, expr,
                                 False), "%s.%s" % (shape, suffix))
        emit(lanewise_unary("%s.neg" % shape, shape, "-x", False),
             "%s.neg" % shape)
        for suffix, fn in (("sqrt", "sqrt"), ("abs", "fabs"),
                           ("ceil", "ceil"), ("floor", "floor"),
                           ("trunc", "trunc"), ("nearest", "nearbyint")):
            emit(lanewise_unary("%s.%s" % (shape, suffix), shape,
                                "%s%s(x)" % (fn, sfx), False),
                 "%s.%s" % (shape, suffix))

    # -- comparisons
    for shape in INT_SHAPES:
        pairs = [("eq", "==", False), ("ne", "!=", False)]
        if shape != "i64x2":
            pairs += [("lt_s", "<", True), ("lt_u", "<", False),
                      ("gt_s", ">", True), ("gt_u", ">", False),
                      ("le_s", "<=", True), ("le_u", "<=", False),
                      ("ge_s", ">=", True), ("ge_u", ">=", False)]
        else:
            pairs += [("lt_s", "<", True), ("gt_s", ">", True),
                      ("le_s", "<=", True), ("ge_s", ">=", True)]
        for suffix, cop, signed in pairs:
            emit(compare("%s.%s" % (shape, suffix), shape, cop, signed),
                 "%s.%s" % (shape, suffix))
    for shape in FLOAT_SHAPES:
        for suffix, cop in (("eq", "=="), ("ne", "!="), ("lt", "<"),
                            ("gt", ">"), ("le", "<="), ("ge", ">=")):
            emit(compare("%s.%s" % (shape, suffix), shape, cop, False),
                 "%s.%s" % (shape, suffix))

    # -- shifts. The count is a scalar, masked to the lane width.
    for shape in INT_SHAPES:
        lanes, sf, uf, _f = SHAPE_ACC[shape]
        width = 128 // lanes
        for suffix, acc, cop in (("shl", uf, "<<"), ("shr_s", sf, ">>"),
                                 ("shr_u", uf, ">>")):
            ct = LANE_TYPE[acc]
            fn = "wasm_%s_%s" % (shape, suffix)
            emit(["static v128_t %s(v128_t a, int n) {" % fn,
                  "  v128_t r; int k;",
                  "  for (k = 0; k < %d; k++)" % lanes,
                  "    __wsimd_set_%s(&r, k, (%s)(__wsimd_get_%s(a, k) %s "
                  "(n & %d)));" % (acc, ct, acc, cop, width - 1),
                  "  return r;",
                  "}"], "%s.%s" % (shape, suffix))

    # -- splat, extract, replace
    for shape in SHAPE_ACC:
        lanes, sf, uf, is_float = SHAPE_ACC[shape]
        scalar = CT[S.LANE_SCALAR[shape]]
        emit(["static v128_t wasm_%s_splat(%s x) {" % (shape, scalar),
              "  v128_t r; int k;",
              "  for (k = 0; k < %d; k++)" % lanes,
              "    __wsimd_set_%s(&r, k, (%s)x);" % (sf, LANE_TYPE[sf]),
              "  return r;",
              "}"], "%s.splat" % shape)
        # The narrow integer shapes have both signednesses of extract.
        variants = []
        if shape in ("i8x16", "i16x8"):
            variants = [("extract_lane_s", sf), ("extract_lane_u", uf)]
        else:
            variants = [("extract_lane", sf)]
        for suffix, acc in variants:
            emit(["static %s wasm_%s_%s(v128_t a, int k) {"
                  % (scalar, shape, suffix),
                  "  return (%s)__wsimd_get_%s(a, k);" % (scalar, acc),
                  "}"], "%s.%s" % (shape, suffix))
        emit(["static v128_t wasm_%s_replace_lane(v128_t a, int k, %s x) {"
              % (shape, scalar),
              "  v128_t r = a; __wsimd_set_%s(&r, k, (%s)x); return r;"
              % (sf, LANE_TYPE[sf]),
              "}"], "%s.replace_lane" % shape)

    # -- bitwise, over the vector as a whole
    for suffix, expr in (("and", "x & y"), ("or", "x | y"),
                         ("xor", "x ^ y"), ("andnot", "x & ~y")):
        emit(["static v128_t wasm_v128_%s(v128_t a, v128_t b) {" % suffix,
              "  v128_t r; int k;",
              "  for (k = 0; k < 2; k++) {",
              "    unsigned long x = __wsimd_get_u64(a, k),"
              " y = __wsimd_get_u64(b, k);",
              "    __wsimd_set_u64(&r, k, %s);" % expr,
              "  }",
              "  return r;",
              "}"], "v128.%s" % suffix)
    emit(["static v128_t wasm_v128_not(v128_t a) {",
          "  v128_t r; int k;",
          "  for (k = 0; k < 2; k++)",
          "    __wsimd_set_u64(&r, k, ~__wsimd_get_u64(a, k));",
          "  return r;",
          "}"], "v128.not")
    emit(["static v128_t wasm_v128_bitselect(v128_t a, v128_t b, v128_t c) {",
          "  v128_t r; int k;",
          "  /* Set bits of c select from a, clear bits from b. */",
          "  for (k = 0; k < 2; k++) {",
          "    unsigned long x = __wsimd_get_u64(a, k),"
          " y = __wsimd_get_u64(b, k), m = __wsimd_get_u64(c, k);",
          "    __wsimd_set_u64(&r, k, (x & m) | (y & ~m));",
          "  }",
          "  return r;",
          "}"], "v128.bitselect")
    emit(["static int wasm_v128_any_true(v128_t a) {",
          "  return (__wsimd_get_u64(a, 0) | __wsimd_get_u64(a, 1)) != 0;",
          "}"], "v128.any_true")

    for shape in INT_SHAPES:
        lanes, sf, uf, _f = SHAPE_ACC[shape]
        emit(["static int wasm_%s_all_true(v128_t a) {" % shape,
              "  int k;",
              "  for (k = 0; k < %d; k++)" % lanes,
              "    if (!__wsimd_get_%s(a, k)) return 0;" % uf,
              "  return 1;",
              "}"], "%s.all_true" % shape)
        emit(["static int wasm_%s_bitmask(v128_t a) {" % shape,
              "  int k, m = 0;",
              "  for (k = 0; k < %d; k++)" % lanes,
              "    if (__wsimd_get_%s(a, k) < 0) m |= 1 << k;" % sf,
              "  return m;",
              "}"], "%s.bitmask" % shape)

    return exposed, body


def main():
    exposed, fallback = build()
    exposed_set = {}
    for n in exposed:
        exposed_set[n] = 1

    L = []
    a = L.append
    a("/* WebAssembly SIMD intrinsics.")
    a(" *")
    a(" *     #include <wasm_simd128.h>")
    a(" *")
    a(" * Under --target wasm each intrinsic becomes the single SIMD")
    a(" * instruction it names. Under any other target the same calls")
    a(" * compile to portable scalar C, so a program using them builds and")
    a(" * runs anywhere -- which is what lets an ordinary compiler act as")
    a(" * the oracle when checking the vector code.")
    a(" *")
    a(" * GENERATED by tools/gen_wasm_simd128.py from shivyc/wasm_simd.py.")
    a(" * Edit the generator, not this file.")
    a(" */")
    a("#ifndef _WASM_SIMD128_H")
    a("#define _WASM_SIMD128_H")
    a("")
    a("/* A vector is sixteen bytes. It is a struct rather than a builtin")
    a(" * type because the front end has no vector type: aggregates already")
    a(" * live in memory and are passed by address, which is what a v128")
    a(" * needs anyway. */")
    a("typedef struct { unsigned char __v[16]; } v128_t;")
    a("")
    a("#ifdef __wasm__")
    a("")
    a("/* Every operator the back end can emit, including the ones with no")
    a(" * scalar fallback below. */")
    for code in sorted(S.OPCODES):
        name, imm = S.OPCODES[code]
        if S.is_relaxed(name):
            continue
        if imm in (S.IMM_V128, S.IMM_SHUFFLE, S.IMM_MEMARG_LANE):
            continue
        params, result = S.signature(name)
        ps = [CT[p] for p in params]
        if imm == S.IMM_LANE:
            ps.append("int")
        a("%s %s(%s);" % (CT[result] if result else "void",
                          S.builtin_name(name), ", ".join(ps) or "void"))
    a("")
    for name in exposed:
        params, result = S.signature(name)
        n = len(params)
        has_lane = False
        for code in S.OPCODES:
            if S.OPCODES[code][0] == name and S.OPCODES[code][1] == S.IMM_LANE:
                has_lane = True
        if has_lane:
            n += 1
        args = ["a%d" % i for i in range(n)]
        call_args = list(args)
        if has_lane and "replace_lane" in name:
            # The builtin always takes its lane index last, because that is
            # the one place the back end can find it without a per-operator
            # table. The user-facing order is clang's -- (vector, lane,
            # value) -- so the macro reorders. Getting this wrong produced a
            # module that failed to validate with "invalid lane index", the
            # value having been encoded as the lane.
            call_args = [args[0], args[2], args[1]]
        a("#define wasm_%s(%s) %s(%s)"
          % (name.replace(".", "_"), ", ".join(args),
             S.builtin_name(name), ", ".join(call_args)))
    a("")
    a("#else  /* every other target: portable scalar C */")
    a("")
    a("#include <string.h>")
    a("#include <math.h>")
    a("")
    L.extend(accessors())
    a("")
    L.extend(fallback)
    a("#endif  /* __wasm__ */")
    a("")
    a("#endif  /* _WASM_SIMD128_H */")

    with open(OUT, "w") as f:
        f.write("\n".join(L) + "\n")
    print("wrote %s: %d intrinsics with scalar fallbacks" % (OUT,
                                                             len(exposed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
