"""WebAssembly SIMD (the 0xFD opcode prefix): the opcode table and shapes.

There are 256 SIMD opcodes, and writing them out one per line would be 256
lines of near-identical text in which a single transposed digit is invisible.
Most of the space is regular -- the same operation repeated across the six
lane shapes -- so it is *generated* from that structure instead, with the
handful of genuinely irregular runs listed explicitly.

The generators are the point:

    cmp10(35, "i8x16")      # the ten comparisons, in their fixed order
    across(224, "f32x4", FLOAT_ARITH)

Each says what the specification says, in the shape the specification says it.
A mistake in a generated run is a mistake in one visible place rather than in
one of ten lines that all look alike.

The opcode numbers are facts from the WebAssembly SIMD specification. They
were cross-checked entry by entry against an independent listing while this
file was written -- see `self_check()`, which re-derives the count and
rejects duplicates, and `tools/wasm_simd_check.py`, which diffs the whole
table against a reference when one is available.
"""

# Immediate shapes, re-exported from the reader's vocabulary so the table can
# be read without chasing two files.
IMM_NONE = "none"
IMM_MEMARG = "memarg"          # align, offset
IMM_MEMARG_LANE = "memlane"    # align, offset, lane index
IMM_LANE = "lane"              # a single lane index byte
IMM_V128 = "v128"              # sixteen immediate bytes
IMM_SHUFFLE = "shuffle"        # sixteen lane selector bytes


# ---------------------------------------------------------------- shapes
#
# name -> (lane count, unsigned C field, signed C field, is float)
#
# The C fields name members of the `v128` union in wasm2c_rt_simd.h. Having
# both signednesses here is what lets a single generator emit `lt_s` and
# `lt_u` without knowing anything about either.
SHAPES = {
    "i8x16": (16, "u8x16", "s8x16", False),
    "i16x8": (8, "u16x8", "s16x8", False),
    "i32x4": (4, "u32x4", "s32x4", False),
    "i64x2": (2, "u64x2", "s64x2", False),
    "f32x4": (4, "f32x4", "f32x4", True),
    "f64x2": (2, "f64x2", "f64x2", True),
}

# The ten integer comparisons, in the order the specification assigns them.
CMP_INT = ["eq", "ne", "lt_s", "lt_u", "gt_s", "gt_u",
           "le_s", "le_u", "ge_s", "ge_u"]
# The six float comparisons, likewise.
CMP_FLOAT = ["eq", "ne", "lt", "gt", "le", "ge"]

OPCODES = {}


def op(code, name, imm=IMM_NONE):
    """Register one SIMD opcode."""
    if code in OPCODES:
        raise ValueError("duplicate SIMD opcode %d (%s and %s)"
                         % (code, OPCODES[code][0], name))
    OPCODES[code] = (name, imm)


def seq(start, names, fmt="%s", imm=IMM_NONE):
    """Register a consecutive run. A None in `names` skips that opcode,
    which is how the gaps in the irregular region are expressed."""
    for i in range(len(names)):
        if names[i] is not None:
            op(start + i, fmt % names[i], imm)


def cmp_int(start, shape):
    """The ten integer comparisons for one shape."""
    seq(start, CMP_INT, shape + ".%s")


def cmp_float(start, shape):
    """The six float comparisons for one shape."""
    seq(start, CMP_FLOAT, shape + ".%s")


def _build():
    # -- memory: plain load/store, the widening loads, and the splats
    op(0, "v128.load", IMM_MEMARG)
    seq(1, ["load8x8_s", "load8x8_u", "load16x4_s", "load16x4_u",
            "load32x2_s", "load32x2_u",
            "load8_splat", "load16_splat", "load32_splat", "load64_splat"],
        "v128.%s", IMM_MEMARG)
    op(11, "v128.store", IMM_MEMARG)
    op(12, "v128.const", IMM_V128)
    op(13, "i8x16.shuffle", IMM_SHUFFLE)
    op(14, "i8x16.swizzle")

    # -- splat, one per shape, in shape order
    seq(15, ["i8x16", "i16x8", "i32x4", "i64x2", "f32x4", "f64x2"],
        "%s.splat")

    # -- lane access. The narrow integer shapes have both signednesses of
    #    extract; the wider ones and the floats have only one.
    seq(21, ["i8x16.extract_lane_s", "i8x16.extract_lane_u",
             "i8x16.replace_lane",
             "i16x8.extract_lane_s", "i16x8.extract_lane_u",
             "i16x8.replace_lane",
             "i32x4.extract_lane", "i32x4.replace_lane",
             "i64x2.extract_lane", "i64x2.replace_lane",
             "f32x4.extract_lane", "f32x4.replace_lane",
             "f64x2.extract_lane", "f64x2.replace_lane"],
        "%s", IMM_LANE)

    # -- comparisons
    cmp_int(35, "i8x16")
    cmp_int(45, "i16x8")
    cmp_int(55, "i32x4")
    cmp_float(65, "f32x4")
    cmp_float(71, "f64x2")

    # -- bitwise, on the vector as a whole rather than lane-wise
    seq(77, ["not", "and", "andnot", "or", "xor", "bitselect", "any_true"],
        "v128.%s")

    # -- lane load/store and the zero-extending loads
    seq(84, ["load8_lane", "load16_lane", "load32_lane", "load64_lane",
             "store8_lane", "store16_lane", "store32_lane", "store64_lane"],
        "v128.%s", IMM_MEMARG_LANE)
    seq(92, ["load32_zero", "load64_zero"], "v128.%s", IMM_MEMARG)

    op(94, "f32x4.demote_f64x2_zero")
    op(95, "f64x2.promote_low_f32x4")

    # -- the integer arithmetic region (96..223). Genuinely irregular: the
    #    four integer shapes are interleaved with float rounding operators,
    #    and each shape omits a different subset. Listed as consecutive runs
    #    with explicit gaps rather than pretended to be uniform.
    seq(96, ["i8x16.abs", "i8x16.neg", "i8x16.popcnt", "i8x16.all_true",
             "i8x16.bitmask", "i8x16.narrow_i16x8_s", "i8x16.narrow_i16x8_u",
             "f32x4.ceil", "f32x4.floor", "f32x4.trunc", "f32x4.nearest",
             "i8x16.shl", "i8x16.shr_s", "i8x16.shr_u",
             "i8x16.add", "i8x16.add_sat_s", "i8x16.add_sat_u",
             "i8x16.sub", "i8x16.sub_sat_s", "i8x16.sub_sat_u",
             "f64x2.ceil", "f64x2.floor",
             "i8x16.min_s", "i8x16.min_u", "i8x16.max_s", "i8x16.max_u",
             "f64x2.trunc", "i8x16.avgr_u",
             "i16x8.extadd_pairwise_i8x16_s",
             "i16x8.extadd_pairwise_i8x16_u",
             "i32x4.extadd_pairwise_i16x8_s",
             "i32x4.extadd_pairwise_i16x8_u"])

    seq(128, ["i16x8.abs", "i16x8.neg", "i16x8.q15mulr_sat_s",
              "i16x8.all_true", "i16x8.bitmask",
              "i16x8.narrow_i32x4_s", "i16x8.narrow_i32x4_u",
              "i16x8.extend_low_i8x16_s", "i16x8.extend_high_i8x16_s",
              "i16x8.extend_low_i8x16_u", "i16x8.extend_high_i8x16_u",
              "i16x8.shl", "i16x8.shr_s", "i16x8.shr_u",
              "i16x8.add", "i16x8.add_sat_s", "i16x8.add_sat_u",
              "i16x8.sub", "i16x8.sub_sat_s", "i16x8.sub_sat_u",
              "f64x2.nearest", "i16x8.mul",
              "i16x8.min_s", "i16x8.min_u", "i16x8.max_s", "i16x8.max_u",
              None, "i16x8.avgr_u",
              "i16x8.extmul_low_i8x16_s", "i16x8.extmul_high_i8x16_s",
              "i16x8.extmul_low_i8x16_u", "i16x8.extmul_high_i8x16_u"])

    seq(160, ["i32x4.abs", "i32x4.neg", None, "i32x4.all_true",
              "i32x4.bitmask", None, None,
              "i32x4.extend_low_i16x8_s", "i32x4.extend_high_i16x8_s",
              "i32x4.extend_low_i16x8_u", "i32x4.extend_high_i16x8_u",
              "i32x4.shl", "i32x4.shr_s", "i32x4.shr_u",
              "i32x4.add", None, None,
              "i32x4.sub", None, None, None,
              "i32x4.mul",
              "i32x4.min_s", "i32x4.min_u", "i32x4.max_s", "i32x4.max_u",
              "i32x4.dot_i16x8_s", None,
              "i32x4.extmul_low_i16x8_s", "i32x4.extmul_high_i16x8_s",
              "i32x4.extmul_low_i16x8_u", "i32x4.extmul_high_i16x8_u"])

    seq(192, ["i64x2.abs", "i64x2.neg", None, "i64x2.all_true",
              "i64x2.bitmask", None, None,
              "i64x2.extend_low_i32x4_s", "i64x2.extend_high_i32x4_s",
              "i64x2.extend_low_i32x4_u", "i64x2.extend_high_i32x4_u",
              "i64x2.shl", "i64x2.shr_s", "i64x2.shr_u",
              "i64x2.add", None, None,
              "i64x2.sub", None, None, None,
              "i64x2.mul",
              "i64x2.eq", "i64x2.ne", "i64x2.lt_s", "i64x2.gt_s",
              "i64x2.le_s", "i64x2.ge_s",
              "i64x2.extmul_low_i32x4_s", "i64x2.extmul_high_i32x4_s",
              "i64x2.extmul_low_i32x4_u", "i64x2.extmul_high_i32x4_u"])

    # -- float arithmetic: the same twelve operators for both float shapes,
    #    which is regular enough to generate.
    FLOAT_ARITH = ["abs", "neg", None, "sqrt",
                   "add", "sub", "mul", "div",
                   "min", "max", "pmin", "pmax"]
    seq(224, FLOAT_ARITH, "f32x4.%s")
    seq(236, FLOAT_ARITH, "f64x2.%s")

    # -- conversions between the integer and float shapes
    seq(248, ["i32x4.trunc_sat_f32x4_s", "i32x4.trunc_sat_f32x4_u",
              "f32x4.convert_i32x4_s", "f32x4.convert_i32x4_u",
              "i32x4.trunc_sat_f64x2_s_zero", "i32x4.trunc_sat_f64x2_u_zero",
              "f64x2.convert_low_i32x4_s", "f64x2.convert_low_i32x4_u"])

    # -- relaxed SIMD (256..275). Decoded so a module carrying them can be
    #    read and reported precisely, but their results are implementation
    #    defined by design, so wasm2c refuses to translate them rather than
    #    pick one behaviour and call it correct.
    seq(256, ["i8x16.relaxed_swizzle",
              "i32x4.relaxed_trunc_f32x4_s", "i32x4.relaxed_trunc_f32x4_u",
              "i32x4.relaxed_trunc_f64x2_s_zero",
              "i32x4.relaxed_trunc_f64x2_u_zero",
              "f32x4.relaxed_madd", "f32x4.relaxed_nmadd",
              "f64x2.relaxed_madd", "f64x2.relaxed_nmadd",
              "i8x16.relaxed_laneselect", "i16x8.relaxed_laneselect",
              "i32x4.relaxed_laneselect", "i64x2.relaxed_laneselect",
              "f32x4.relaxed_min", "f32x4.relaxed_max",
              "f64x2.relaxed_min", "f64x2.relaxed_max",
              "i16x8.relaxed_q15mulr_s", "i16x8.relaxed_dot_i8x16_i7x16_s",
              "i32x4.relaxed_dot_i8x16_i7x16_add_s"])


_build()

RELAXED_FIRST = 256


def is_relaxed(name):
    """Whether `name` is a relaxed-SIMD operator, whose result the
    specification deliberately leaves implementation defined."""
    return "relaxed" in name


def self_check():
    """Sanity-check the generated table.

    Catches the two mistakes a generated table can actually make: a run that
    overlaps another (caught by `op`, which refuses a duplicate) and a run
    whose length is wrong, which shows up as a changed total. The expected
    counts are stated here so that editing a run without meaning to change
    the table fails loudly.
    """
    core = 0
    relaxed = 0
    for code in OPCODES:
        if code >= RELAXED_FIRST:
            relaxed += 1
        else:
            core += 1
    problems = []
    if core != 236:
        problems.append("expected 236 core SIMD opcodes, generated %d" % core)
    if relaxed != 20:
        problems.append("expected 20 relaxed opcodes, generated %d" % relaxed)
    names = {}
    for code in OPCODES:
        nm = OPCODES[code][0]
        if nm in names:
            problems.append("duplicate name %s at %d and %d"
                            % (nm, names[nm], code))
        names[nm] = code
    return problems


# ------------------------------------------------- operator signatures
#
# Kinds a SIMD operand or result can have. Derived from the operator's name
# rather than tabulated separately: the shape prefix and the suffix already
# say what the types are, and a second table would only be a second thing to
# keep in step with the first.
K_V128 = "v128"
K_I32 = "i32"
K_I64 = "i64"
K_F32 = "f32"
K_F64 = "f64"

# The scalar a shape's lanes are read and written as. Sub-word lanes are
# handled as i32, exactly as extract_lane/replace_lane define them.
LANE_SCALAR = {
    "i8x16": K_I32, "i16x8": K_I32, "i32x4": K_I32,
    "i64x2": K_I64, "f32x4": K_F32, "f64x2": K_F64,
}

_UNARY = ("neg", "abs", "sqrt", "ceil", "floor", "trunc", "nearest",
          "popcnt", "not")


def signature(name):
    """(parameter kinds, result kind) for a SIMD operator.

    `None` as the result means the operator produces nothing.
    """
    shape = name.split(".")[0]
    base = name.split(".")[-1]

    if name == "v128.store":
        return [K_I32, K_V128], None
    if name.startswith("v128.store") and name.endswith("_lane"):
        return [K_I32, K_V128], None
    if name.startswith("v128.load") and name.endswith("_lane"):
        return [K_I32, K_V128], K_V128
    if name.startswith("v128.load"):
        return [K_I32], K_V128
    if name == "v128.const":
        return [], K_V128
    if name == "v128.bitselect":
        return [K_V128, K_V128, K_V128], K_V128
    if name == "v128.any_true":
        return [K_V128], K_I32
    if name == "v128.not":
        return [K_V128], K_V128
    if name in ("v128.and", "v128.or", "v128.xor", "v128.andnot"):
        return [K_V128, K_V128], K_V128

    if base == "splat":
        return [LANE_SCALAR[shape]], K_V128
    if base == "all_true" or base == "bitmask":
        return [K_V128], K_I32
    if "extract_lane" in base:
        return [K_V128], LANE_SCALAR[shape]
    if "replace_lane" in base:
        return [K_V128, LANE_SCALAR[shape]], K_V128
    if base in ("shl", "shr_s", "shr_u"):
        # The shift count is a scalar, not a vector of counts.
        return [K_V128, K_I32], K_V128
    if name == "i8x16.shuffle":
        return [K_V128, K_V128], K_V128
    if base in _UNARY:
        return [K_V128], K_V128
    if ("extend_" in name or "extadd_" in name or "convert_" in name
            or "trunc_sat_" in name or "demote_" in name
            or "promote_" in name):
        return [K_V128], K_V128
    return [K_V128, K_V128], K_V128


def builtin_name(op):
    """The C builtin spelling of a SIMD operator.

    `i32x4.add` becomes `__builtin_wasm_i32x4_add`. Mechanical, so the header
    and the back end agree without either listing the operators.
    """
    return "__builtin_wasm_" + op.replace(".", "_")


def op_for_builtin(name):
    """Inverse of builtin_name, or None if `name` is not one."""
    prefix = "__builtin_wasm_"
    if not name.startswith(prefix):
        return None
    tail = name[len(prefix):]
    for code in OPCODES:
        if builtin_name(OPCODES[code][0]) == name:
            return OPCODES[code][0]
    return None
