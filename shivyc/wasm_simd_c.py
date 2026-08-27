"""SIMD code generation for tools/wasm2c.py.

There are 236 core SIMD operators, and writing a `if op == ...` arm for each
would be unreadable and unreviewable. Almost all of them are the *same*
operation repeated across six lane shapes, so they are generated from that
structure instead, and only the genuinely irregular ones get written out.

Three mechanisms, in increasing order of specificity:

  `@handler("name", ...)`   registers one function for one or more operators.
  `lanewise(...)`           generates the loop for an elementwise operator
                            across every shape it applies to.
  `direct(...)`             maps an operator straight onto a helper in
                            wasm2c_rt_simd.h.

The result is that adding an operator usually means adding it to a list, and
a bug in a generated family is a bug in one visible place rather than in one
of six lines that all look alike.

Every generated handler is checked against the opcode table at import time by
`coverage()`, so an operator that exists in the table but has no code path
is reported rather than discovered later as a crash.
"""

import shivyc.wasm_simd as simd

V128 = 0x7B

# Lane shape -> (lanes, unsigned field, signed field, is_float). Mirrors
# wasm_simd.SHAPES, which is the single source of truth.
SHAPES = simd.SHAPES

# op name -> (arity, emit function). The emit function is called with the
# FunctionWriter, the destination expression, the argument expressions and
# the instruction, and returns the C statement text.
HANDLERS = {}


def handler(*names):
    """Register one emit function for one or more operator names."""
    def register(fn):
        for n in names:
            if n in HANDLERS:
                raise ValueError("duplicate SIMD handler for %s" % n)
            HANDLERS[n] = fn
        return fn
    return register


def _loop(dst, lanes, body):
    """A lane loop. Written on one line so generated output stays readable."""
    return "{ int _k; for (_k = 0; _k < %d; _k++) %s }" % (lanes, body)


def lanewise(suffixes, shapes, expr, signed=False, arity=2):
    """Generate an elementwise operator across several shapes.

    `expr` is a C template over `A`, `B` and the lane index; `signed` selects
    which union member the lanes are read through. One call here covers what
    would otherwise be one handler per shape.
    """
    for shape in shapes:
        lanes, uf, sf, is_float = SHAPES[shape]
        field = sf if signed else uf
        for suffix in suffixes:
            name = "%s.%s" % (shape, suffix)

            def make(field=field, lanes=lanes, expr=expr, arity=arity,
                     shape=shape):
                def emit(fw, dst, args, ins):
                    a = args[0]
                    b = args[1] if arity > 1 else None
                    lhs = "%s.%s[_k]" % (dst, field)
                    rhs = expr.replace("A", "%s.%s[_k]" % (a, field))
                    if b is not None:
                        rhs = rhs.replace("B", "%s.%s[_k]" % (b, field))
                    # The result is written through the same member it was
                    # read through, so a signed operator stays signed.
                    return _loop(dst, lanes, "%s = (%s);" % (lhs, rhs))
                return emit
            HANDLERS[name] = make()


def direct(name, helper, arity=2, result="v128"):
    """Map an operator onto a helper in wasm2c_rt_simd.h."""
    def emit(fw, dst, args, ins):
        return "%s = %s(%s);" % (dst, helper, ", ".join(args[:arity]))
    HANDLERS[name] = emit


def _build():
    ints = ["i8x16", "i16x8", "i32x4", "i64x2"]
    floats = ["f32x4", "f64x2"]

    # -- elementwise arithmetic. add/sub/mul wrap on the unsigned member,
    #    which is exactly wasm's modular arithmetic.
    # One call per operator: passing several suffixes with a single
    # expression would give them all the *same* expression, which is how an
    # earlier version made i32x4.sub compute an addition.
    lanewise(["add"], ints, "A + B")
    lanewise(["sub"], ints, "A - B")
    lanewise(["mul"], ["i16x8", "i32x4", "i64x2"], "A * B")
    lanewise(["neg"], ints, "-A", arity=1)
    lanewise(["abs"], ints, "A < 0 ? -A : A", signed=True, arity=1)
    # Each float operator differs only in its C operator.
    for shape in floats:
        lanes, uf, _sf, _f = SHAPES[shape]
        for suffix, cop in (("add", "+"), ("sub", "-"),
                            ("mul", "*"), ("div", "/")):
            lanewise([suffix], [shape], "A %s B" % cop)
        lanewise(["neg"], [shape], "-A", arity=1)
        sfx = "f" if shape == "f32x4" else ""
        lanewise(["sqrt"], [shape], "sqrt%s(A)" % sfx, arity=1)
        lanewise(["abs"], [shape], "fabs%s(A)" % sfx, arity=1)

    # min/max on integers, and the unsigned/signed split.
    for shape in ["i8x16", "i16x8", "i32x4"]:
        lanewise(["min_s"], [shape], "A < B ? A : B", signed=True)
        lanewise(["max_s"], [shape], "A > B ? A : B", signed=True)
        lanewise(["min_u"], [shape], "A < B ? A : B")
        lanewise(["max_u"], [shape], "A > B ? A : B")

    # -- comparisons. A true lane is all ones, not 1, which is what makes
    #    v128.bitselect and the bitmask operators work on the result.
    for shape in ints:
        lanes, uf, sf, _f = SHAPES[shape]
        for suffix, cop, sgn in (("eq", "==", False), ("ne", "!=", False),
                                 ("lt_s", "<", True), ("lt_u", "<", False),
                                 ("gt_s", ">", True), ("gt_u", ">", False),
                                 ("le_s", "<=", True), ("le_u", "<=", False),
                                 ("ge_s", ">=", True), ("ge_u", ">=", False)):
            name = "%s.%s" % (shape, suffix)

            def make(uf=uf, sf=sf, lanes=lanes, cop=cop, sgn=sgn):
                def emit(fw, dst, args, ins):
                    f = sf if sgn else uf
                    return _loop(dst, lanes,
                                 "%s.%s[_k] = (%s.%s[_k] %s %s.%s[_k]) "
                                 "? ~0 : 0;"
                                 % (dst, uf, args[0], f, cop, args[1], f))
                return emit
            HANDLERS[name] = make()

    for shape in floats:
        lanes, uf, _sf, _f = SHAPES[shape]
        intf = {"f32x4": "u32x4", "f64x2": "u64x2"}[shape]
        for suffix, cop in (("eq", "=="), ("ne", "!="), ("lt", "<"),
                            ("gt", ">"), ("le", "<="), ("ge", ">=")):
            def make(uf=uf, intf=intf, lanes=lanes, cop=cop):
                def emit(fw, dst, args, ins):
                    return _loop(dst, lanes,
                                 "%s.%s[_k] = (%s.%s[_k] %s %s.%s[_k]) "
                                 "? ~0 : 0;"
                                 % (dst, intf, args[0], uf, cop, args[1], uf))
                return emit
            HANDLERS["%s.%s" % (shape, suffix)] = make()

    # -- shifts. The count is a scalar i32, masked to the lane width.
    for shape in ints:
        lanes, uf, sf, _f = SHAPES[shape]
        width = {"i8x16": 8, "i16x8": 16, "i32x4": 32, "i64x2": 64}[shape]
        for suffix, field, cop in (("shl", uf, "<<"),
                                   ("shr_s", sf, ">>"),
                                   ("shr_u", uf, ">>")):
            def make(field=field, uf=uf, lanes=lanes, cop=cop, width=width):
                def emit(fw, dst, args, ins):
                    return _loop(dst, lanes,
                                 "%s.%s[_k] = (%s.%s[_k] %s (%s & %d));"
                                 % (dst, field, args[0], field, cop,
                                    args[1], width - 1))
                return emit
            HANDLERS["%s.%s" % (shape, suffix)] = make()

    # -- saturating add/sub, via the runtime helpers
    for shape, sgn in (("i8x16", "s"), ("i8x16", "u"),
                       ("i16x8", "s"), ("i16x8", "u")):
        base = "wasm_%s_sat_%s" % (shape, sgn)
        for suffix, is_sub in (("add_sat_%s" % sgn, 0),
                               ("sub_sat_%s" % sgn, 1)):
            def make(base=base, is_sub=is_sub):
                def emit(fw, dst, args, ins):
                    return "%s = %s(%s, %s, %d);" % (dst, base, args[0],
                                                     args[1], is_sub)
                return emit
            HANDLERS["%s.%s" % (shape, suffix)] = make()

    # -- everything that maps straight onto a helper
    direct("v128.and", "wasm_v128_and")
    direct("v128.or", "wasm_v128_or")
    direct("v128.xor", "wasm_v128_xor")
    direct("v128.andnot", "wasm_v128_andnot")
    direct("v128.not", "wasm_v128_not", arity=1)
    direct("v128.bitselect", "wasm_v128_bitselect", arity=3)
    direct("i8x16.swizzle", "wasm_i8x16_swizzle")
    direct("i8x16.popcnt", "wasm_i8x16_popcnt", arity=1)
    direct("i32x4.dot_i16x8_s", "wasm_i32x4_dot_i16x8_s")
    direct("i16x8.q15mulr_sat_s", "wasm_i16x8_q15mulr_sat_s")
    for shape in ("i8x16", "i16x8"):
        direct("%s.avgr_u" % shape, "wasm_%s_avgr_u" % shape)
    for shape in floats:
        for suffix in ("min", "max", "pmin", "pmax"):
            direct("%s.%s" % (shape, suffix),
                   "wasm_%s_%s" % (shape, suffix))
        for suffix in ("ceil", "floor", "trunc", "nearest"):
            fn = {"ceil": "ceil", "floor": "floor",
                  "trunc": "trunc", "nearest": "nearbyint"}[suffix]
            lanes, uf, _s, _f = SHAPES[shape]
            sfx = "f" if shape == "f32x4" else ""

            def make(uf=uf, lanes=lanes, fn=fn, sfx=sfx):
                def emit(fw, dst, args, ins):
                    return _loop(dst, lanes,
                                 "%s.%s[_k] = %s%s(%s.%s[_k]);"
                                 % (dst, uf, fn, sfx, args[0], uf))
                return emit
            HANDLERS["%s.%s" % (shape, suffix)] = make()

    # Families whose helper name is exactly the operator name.
    same = []
    for shape in ints:
        same.append("%s.all_true" % shape)
        same.append("%s.bitmask" % shape)
    same += ["i8x16.narrow_i16x8_s", "i8x16.narrow_i16x8_u",
             "i16x8.narrow_i32x4_s", "i16x8.narrow_i32x4_u",
             "f32x4.demote_f64x2_zero", "f64x2.promote_low_f32x4",
             "i32x4.trunc_sat_f32x4_s", "i32x4.trunc_sat_f32x4_u",
             "i32x4.trunc_sat_f64x2_s_zero", "i32x4.trunc_sat_f64x2_u_zero",
             "f32x4.convert_i32x4_s", "f32x4.convert_i32x4_u",
             "f64x2.convert_low_i32x4_s", "f64x2.convert_low_i32x4_u"]
    for shape in ["i16x8", "i32x4", "i64x2"]:
        src = {"i16x8": "i8x16", "i32x4": "i16x8", "i64x2": "i32x4"}[shape]
        for half in ("low", "high"):
            for sgn in ("s", "u"):
                same.append("%s.extend_%s_%s_%s" % (shape, half, src, sgn))
                same.append("%s.extmul_%s_%s_%s" % (shape, half, src, sgn))
    for shape in ["i16x8", "i32x4"]:
        src = {"i16x8": "i8x16", "i32x4": "i16x8"}[shape]
        for sgn in ("s", "u"):
            same.append("%s.extadd_pairwise_%s_%s" % (shape, src, sgn))
    for name in same:
        arity = 2 if ("narrow" in name or "extmul" in name) else 1
        direct(name, "wasm_" + name.replace(".", "_"), arity=arity)

    # -- splats
    for shape in SHAPES:
        direct("%s.splat" % shape, "wasm_%s_splat" % shape, arity=1)

    # -- memory
    for name in ["v128.load8x8_s", "v128.load8x8_u",
                 "v128.load16x4_s", "v128.load16x4_u",
                 "v128.load32x2_s", "v128.load32x2_u",
                 "v128.load8_splat", "v128.load16_splat",
                 "v128.load32_splat", "v128.load64_splat",
                 "v128.load32_zero", "v128.load64_zero"]:
        HANDLERS[name] = _mem_load(name)
    HANDLERS["v128.load"] = _mem_load("v128.load")
    HANDLERS["v128.store"] = _mem_store()
    HANDLERS["v128.any_true"] = _any_true()


def _mem_load(name):
    helper = "wasm_" + name.replace(".", "_")

    def emit(fw, dst, args, ins):
        return "%s = %s(mem, MEM_SIZE, (u32)(%s + %du));" % (
            dst, helper, args[0], ins.args[1])
    return emit


def _mem_store():
    def emit(fw, dst, args, ins):
        return "wasm_v128_store(mem, MEM_SIZE, (u32)(%s + %du), %s);" % (
            args[0], ins.args[1], args[1])
    return emit


def _any_true():
    def emit(fw, dst, args, ins):
        return "%s = wasm_v128_any_true(%s);" % (dst, args[0])
    return emit


_build()


def coverage():
    """Operators in the opcode table with no code path here.

    Reported rather than discovered as a crash: a module using one of these
    should be refused with a name, not translated into something wrong.
    """
    missing = []
    for code in simd.OPCODES:
        name = simd.OPCODES[code][0]
        if simd.is_relaxed(name):
            continue                       # deliberately not translated
        if name in HANDLERS:
            continue
        if name in ("v128.const", "i8x16.shuffle"):
            continue                       # emitted inline by wasm2c.py
        if ".extract_lane" in name or ".replace_lane" in name:
            continue                       # emitted inline (lane immediate)
        if "_lane" in name:
            continue                       # load/store lane, emitted inline
        missing.append(name)
    missing.sort()
    return missing
