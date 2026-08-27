#!/usr/bin/env python3
"""wasm2c: turn a `.wasm` module back into C.

    python3 tools/wasm2c.py prog.wasm -o prog.c
    gcc -std=c99 -I tools prog.c -o prog && ./prog

This closes the loop the rest of the toolchain opened. Crust already lowers
C (and Rust, and C++) *to* WebAssembly; this brings a module back to C, so a
`.wasm` from any producer can be compiled, inspected, or run natively by the
same pipeline as everything else.

## How it works

Two things have to be bridged, and both have a standard answer.

**The operand stack.** Wasm is a stack machine and C is not. Because a valid
module's stack depth and types are known statically at every point, the stack
can be flattened into ordinary local variables: depth 0 of type i32 is always
the variable `i0`, depth 1 of type i64 is `j1`, and so on. `i32.add` then
becomes `i0 = i0 + i1;`. No runtime stack exists in the output.

**Structured control flow.** Wasm has `block`/`loop`/`if` and depth-relative
branches; C has labels and `goto`. A `block` becomes a label placed at its
`end` and a `loop` a label at its start, so that `br N` is a `goto` to the
label of the Nth enclosing construct -- which is precisely what `br` means.

That pairing (stack to typed temporaries, structured control flow to labels
and goto) is the obvious way to do this and is shared with wabt's wasm2c and
other tools. It was implemented independently here; no code is taken from any
of them, and this file is MIT like the rest of the tree.

## Imports

WASI imports are bound to the POSIX equivalents in `tools/wasm2c_rt_wasi.h`,
so a module compiled from C by Crust and translated back by this tool
produces a native binary that behaves the same as compiling the original C
directly. That round trip is what `tools/wasm_roundtrip.py` checks.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shivyc.wasm as w                                       # noqa: E402
import shivyc.wasm_reader as reader                           # noqa: E402
import shivyc.wasm_simd as simd                               # noqa: E402
import shivyc.wasm_simd_c as simd_c                           # noqa: E402


class Wasm2CError(Exception):
    """Something in the module has no C rendering here."""


# Value type -> (C type, temporary-name prefix). The prefixes follow the usual
# convention: i/j for the two integer widths, f/d for the two float widths.
VALTYPE = {
    w.I32: ("u32", "i"),
    w.I64: ("u64", "j"),
    w.F32: ("f32", "f"),
    w.F64: ("f64", "d"),
    reader.V128: ("v128", "v"),
}

# Binary operators that are a plain C infix expression on the wasm type.
# Signedness is handled by casting first; see `_binop`.
INFIX = {
    "add": "+", "sub": "-", "mul": "*",
    "and": "&", "or": "|", "xor": "^",
    "div": "/",
}

# Comparisons, by mnemonic suffix.
COMPARE = {
    "eq": "==", "ne": "!=",
    "lt": "<", "gt": ">", "le": "<=", "ge": ">=",
    "lt_s": "<", "lt_u": "<", "gt_s": ">", "gt_u": ">",
    "le_s": "<=", "le_u": "<=", "ge_s": ">=", "ge_u": ">=",
}

# Operators that map to a helper in wasm2c_rt.h rather than an expression.
HELPERS = {
    "i32.div_s", "i32.div_u", "i32.rem_s", "i32.rem_u",
    "i64.div_s", "i64.div_u", "i64.rem_s", "i64.rem_u",
    "i32.shl", "i32.shr_s", "i32.shr_u",
    "i64.shl", "i64.shr_s", "i64.shr_u",
    "i32.rotl", "i32.rotr", "i64.rotl", "i64.rotr",
    "i32.clz", "i32.ctz", "i32.popcnt",
    "i64.clz", "i64.ctz", "i64.popcnt",
    "f32.min", "f32.max", "f64.min", "f64.max",
}

# Load/store mnemonic -> (helper, wasm result type, C cast applied after).
LOADS = {
    "i32.load":     ("wasm_load_u32", w.I32, "u32"),
    "i64.load":     ("wasm_load_u64", w.I64, "u64"),
    "f32.load":     ("wasm_load_f32", w.F32, "f32"),
    "f64.load":     ("wasm_load_f64", w.F64, "f64"),
    "i32.load8_s":  ("wasm_load_u8",  w.I32, "u32)(s32)(s8"),
    "i32.load8_u":  ("wasm_load_u8",  w.I32, "u32"),
    "i32.load16_s": ("wasm_load_u16", w.I32, "u32)(s32)(s16"),
    "i32.load16_u": ("wasm_load_u16", w.I32, "u32"),
    "i64.load8_s":  ("wasm_load_u8",  w.I64, "u64)(s64)(s8"),
    "i64.load8_u":  ("wasm_load_u8",  w.I64, "u64"),
    "i64.load16_s": ("wasm_load_u16", w.I64, "u64)(s64)(s16"),
    "i64.load16_u": ("wasm_load_u16", w.I64, "u64"),
    "i64.load32_s": ("wasm_load_u32", w.I64, "u64)(s64)(s32"),
    "i64.load32_u": ("wasm_load_u32", w.I64, "u64"),
}

STORES = {
    "i32.store":   ("wasm_store_u32", "u32"),
    "i64.store":   ("wasm_store_u64", "u64"),
    "f32.store":   ("wasm_store_f32", "f32"),
    "f64.store":   ("wasm_store_f64", "f64"),
    "i32.store8":  ("wasm_store_u8",  "u8"),
    "i32.store16": ("wasm_store_u16", "u16"),
    "i64.store8":  ("wasm_store_u8",  "u8"),
    "i64.store16": ("wasm_store_u16", "u16"),
    "i64.store32": ("wasm_store_u32", "u32"),
}

# Conversions expressed as a C cast: mnemonic -> (result type, cast text).
CASTS = {
    "i32.wrap_i64":      (w.I32, "(u32)"),
    "i64.extend_i32_s":  (w.I64, "(u64)(s64)(s32)"),
    "i64.extend_i32_u":  (w.I64, "(u64)"),
    "i32.extend8_s":     (w.I32, "(u32)(s32)(s8)"),
    "i32.extend16_s":    (w.I32, "(u32)(s32)(s16)"),
    "i64.extend8_s":     (w.I64, "(u64)(s64)(s8)"),
    "i64.extend16_s":    (w.I64, "(u64)(s64)(s16)"),
    "i64.extend32_s":    (w.I64, "(u64)(s64)(s32)"),
    "f32.convert_i32_s": (w.F32, "(f32)(s32)"),
    "f32.convert_i32_u": (w.F32, "(f32)(u32)"),
    "f32.convert_i64_s": (w.F32, "(f32)(s64)"),
    "f32.convert_i64_u": (w.F32, "(f32)(u64)"),
    "f64.convert_i32_s": (w.F64, "(f64)(s32)"),
    "f64.convert_i32_u": (w.F64, "(f64)(u32)"),
    "f64.convert_i64_s": (w.F64, "(f64)(s64)"),
    "f64.convert_i64_u": (w.F64, "(f64)(u64)"),
    "f32.demote_f64":    (w.F32, "(f32)"),
    "f64.promote_f32":   (w.F64, "(f64)"),
}

# Conversions that call a helper: mnemonic -> result type.
CALL_CONV = {
    "i32.trunc_sat_f32_s": w.I32, "i32.trunc_sat_f32_u": w.I32,
    "i32.trunc_sat_f64_s": w.I32, "i32.trunc_sat_f64_u": w.I32,
    "i64.trunc_sat_f32_s": w.I64, "i64.trunc_sat_f32_u": w.I64,
    "i64.trunc_sat_f64_s": w.I64, "i64.trunc_sat_f64_u": w.I64,
    "i32.trunc_f32_s": w.I32, "i32.trunc_f32_u": w.I32,
    "i32.trunc_f64_s": w.I32, "i32.trunc_f64_u": w.I32,
    "i64.trunc_f32_s": w.I64, "i64.trunc_f32_u": w.I64,
    "i64.trunc_f64_s": w.I64, "i64.trunc_f64_u": w.I64,
}

REINTERPRET = {
    "i32.reinterpret_f32": (w.I32, "wasm_f32_bits"),
    "i64.reinterpret_f64": (w.I64, "wasm_f64_bits"),
    "f32.reinterpret_i32": (w.F32, "wasm_f32_from_bits"),
    "f64.reinterpret_i64": (w.F64, "wasm_f64_from_bits"),
}

UNARY_FLOAT = {
    "abs": "fabs", "neg": None, "ceil": "ceil", "floor": "floor",
    "trunc": "trunc", "nearest": "nearbyint", "sqrt": "sqrt",
}


def c_ident(name):
    """Turn a wasm export or import name into a valid C identifier."""
    out = []
    for ch in name:
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    s = "".join(out)
    if not s or s[0].isdigit():
        s = "_" + s
    return s


class Block:
    """One entry on the control stack.

    `kind` is block/loop/if. `label` is the C label this construct's branches
    target -- placed at the *end* for a block or if, and at the *start* for a
    loop, which is the whole difference between the two. `depth` is the
    operand-stack depth on entry, restored when the construct ends.
    """

    def __init__(self, kind, label, depth, result, used):
        self.kind = kind
        self.label = label
        self.depth = depth
        self.result = result
        self.used = used


class FunctionWriter:
    """Emits the C body of one wasm function."""

    def __init__(self, mod, index, out):
        self.mod = mod
        self.index = index
        self.out = out
        self.stack = []            # value types, innermost last
        self.max_depth = {}        # valtype -> deepest slot used
        self.blocks = []
        self.label_n = 0
        self.unreachable = False

    # -- operand stack -----------------------------------------------------

    def push(self, valtype):
        """Push a value and return the C variable that now holds it."""
        name = self._slot(valtype, len(self.stack))
        self.stack.append(valtype)
        d = len(self.stack)
        if self.max_depth.get(valtype, 0) < d:
            self.max_depth[valtype] = d
        return name

    def pop(self):
        """Pop a value and return (C variable, valtype)."""
        if not self.stack:
            if self.unreachable:
                # After a br or return the stack is polymorphic: the
                # validator allows any number of pops. Nothing emitted here is
                # reachable, so a dummy keeps the walk going.
                return self._slot(w.I32, 0), w.I32
            raise Wasm2CError("operand stack underflow in function %d"
                              % self.index)
        vt = self.stack.pop()
        return self._slot(vt, len(self.stack)), vt

    def _slot(self, valtype, depth):
        return "%s%d" % (VALTYPE[valtype][1], depth)

    def peek_name(self, valtype):
        """The name the next push of `valtype` would use, without pushing."""
        return self._slot(valtype, len(self.stack))

    # -- emission ----------------------------------------------------------

    def emit(self, line):
        if self.unreachable:
            # Code after an unconditional branch is unreachable. Wasm still
            # requires it to validate, but emitting it would mean tracking a
            # polymorphic stack for no benefit -- and some of it cannot be
            # typed at all.
            return
        self.out.append("  " + line)

    def label(self):
        self.label_n += 1
        return "L%d_%d" % (self.index, self.label_n)

    # -- the walk ----------------------------------------------------------

    def write(self, func, ftype):
        nparams = len(ftype.params)
        body = []
        self.out = body

        # The function's own scope behaves exactly like a block: `br` to the
        # outermost depth returns.
        self.blocks.append(Block("func", "Lret_%d" % self.index, 0,
                                 ftype.results, [False]))

        for ins in func.instrs:
            self.instr(ins, func, ftype, nparams)

        decls = []
        for vt in (w.I32, w.I64, w.F32, w.F64, reader.V128):
            n = self.max_depth.get(vt, 0)
            if n:
                names = []
                for k in range(n):
                    names.append(self._slot(vt, k))
                decls.append("  %s %s;" % (VALTYPE[vt][0], ", ".join(names)))
        return decls, body

    def instr(self, ins, func, ftype, nparams):
        op = ins.op
        a = ins.args

        # ---- control flow
        if op == "block" or op == "loop":
            res = self._blocktype(a[0])
            lbl = self.label()
            blk = Block(op, lbl, len(self.stack), res, [False])
            if op == "loop":
                # A loop's label goes at the top: br re-enters it.
                self.out.append("  %s: ;" % lbl)
            self.blocks.append(blk)
            return
        if op == "if":
            res = self._blocktype(a[0])
            cond, _ = self.pop()
            lbl = self.label()
            self.emit("if (%s) {" % cond)
            self.blocks.append(Block("if", lbl, len(self.stack), res, [False]))
            return
        if op == "else":
            blk = self.blocks[-1]
            if blk.kind != "if":
                raise Wasm2CError("`else` outside an `if`")
            self._settle_result(blk)
            self.unreachable = False
            self.out.append("  } else {")
            self.stack = self.stack[:blk.depth]
            return
        if op == "end":
            blk = self.blocks.pop()
            if blk.kind == "func":
                self._emit_return(ftype)
                return
            self._settle_result(blk)
            if blk.kind == "if":
                self.out.append("  }")
            self.unreachable = False
            self.stack = self.stack[:blk.depth]
            if blk.kind != "loop":
                # A block's or if's label sits at its end, which is where a
                # branch out of it lands.
                if blk.used[0]:
                    self.out.append("  %s: ;" % blk.label)
            for vt in blk.result:
                self.push(vt)
            return
        if op == "br":
            self._branch(a[0])
            self.unreachable = True
            return
        if op == "br_if":
            cond, _ = self.pop()
            self.emit("if (%s) {" % cond)
            self._branch(a[0])
            self.emit("}")
            return
        if op == "br_table":
            idx, _ = self.pop()
            self.emit("switch (%s) {" % idx)
            for k in range(len(a[0])):
                self.emit("case %d:" % k)
                self._branch(a[0][k])
            self.emit("default:")
            self._branch(a[1])
            self.emit("}")
            self.unreachable = True
            return
        if op == "return":
            self._emit_return(ftype, inline=True)
            self.unreachable = True
            return
        if op == "unreachable":
            self.emit('wasm_trap("unreachable");')
            self.unreachable = True
            return
        if op == "nop":
            return

        # ---- calls
        if op == "call":
            self._call(a[0])
            return
        if op == "call_indirect":
            self._call_indirect(a[0])
            return

        # ---- variables
        if op == "local.get":
            vt = self._local_type(func, self.mod.types[func.type_index], a[0])
            dst = self.push(vt)
            self.emit("%s = %s;" % (dst, self._local_name(a[0])))
            return
        if op == "local.set" or op == "local.tee":
            src, vt = self.pop()
            self.emit("%s = %s;" % (self._local_name(a[0]), src))
            if op == "local.tee":
                self.push(vt)
            return
        if op == "global.get":
            vt = self.mod.globals[a[0]].valtype
            dst = self.push(vt)
            self.emit("%s = g%d;" % (dst, a[0]))
            return
        if op == "global.set":
            src, _ = self.pop()
            self.emit("g%d = %s;" % (a[0], src))
            return

        # ---- constants
        if op == "i32.const":
            dst = self.push(w.I32)
            self.emit("%s = %su;" % (dst, a[0] & 0xFFFFFFFF))
            return
        if op == "i64.const":
            dst = self.push(w.I64)
            self.emit("%s = %sull;" % (dst, a[0] & 0xFFFFFFFFFFFFFFFF))
            return
        if op == "f32.const" or op == "f64.const":
            vt = w.F32 if op == "f32.const" else w.F64
            dst = self.push(vt)
            self.emit("%s = %s;" % (dst, self._float_literal(a[0], vt)))
            return

        # ---- parametric
        if op == "drop":
            self.pop()
            return
        if op == "select":
            cond, _ = self.pop()
            b, vt = self.pop()
            aa, _ = self.pop()
            dst = self.push(vt)
            self.emit("%s = %s ? %s : %s;" % (dst, cond, aa, b))
            return

        # ---- memory
        if op in LOADS:
            helper, vt, cast = LOADS[op]
            addr, _ = self.pop()
            dst = self.push(vt)
            self.emit("%s = (%s)%s(mem, MEM_SIZE, (u32)(%s + %du));"
                      % (dst, cast, helper, addr, a[1]))
            return
        if op in STORES:
            helper, cast = STORES[op]
            val, _ = self.pop()
            addr, _ = self.pop()
            self.emit("%s(mem, MEM_SIZE, (u32)(%s + %du), (%s)%s);"
                      % (helper, addr, a[1], cast, val))
            return
        if op == "memory.size":
            dst = self.push(w.I32)
            self.emit("%s = (u32)(MEM_SIZE / 65536u);" % dst)
            return
        if op == "memory.grow":
            delta, _ = self.pop()
            dst = self.push(w.I32)
            self.emit("%s = wasm_memory_grow(%s);" % (dst, delta))
            return
        if op == "memory.copy":
            n, _ = self.pop()
            src, _ = self.pop()
            dst_a, _ = self.pop()
            self.emit("wasm_memory_copy(mem, MEM_SIZE, %s, %s, %s);"
                      % (dst_a, src, n))
            return
        if op == "memory.fill":
            n, _ = self.pop()
            val, _ = self.pop()
            dst_a, _ = self.pop()
            self.emit("wasm_memory_fill(mem, MEM_SIZE, %s, %s, %s);"
                      % (dst_a, val, n))
            return

        # ---- SIMD
        if self._is_simd(op):
            self._simd(ins)
            return

        # ---- numeric
        self._numeric(op)

    # -- SIMD --------------------------------------------------------------

    def _is_simd(self, op):
        if op.startswith("v128."):
            return True
        for shape in simd.SHAPES:
            if op.startswith(shape + "."):
                return True
        return False

    def _simd(self, ins):
        """Lower one SIMD instruction.

        Almost everything goes through the generated handler table in
        wasm_simd_c; only the operators carrying an immediate (a lane index,
        a shuffle mask, a literal vector) are emitted here, because the
        immediate has to be spliced into the C text.
        """
        op = ins.op
        V = reader.V128

        if simd.is_relaxed(op):
            # Relaxed SIMD leaves its results implementation defined by
            # design. Translating it would mean picking one behaviour and
            # presenting it as the answer, which is worse than refusing.
            raise Wasm2CError(
                "relaxed SIMD operator '%s' has implementation-defined "
                "results and is not translated" % op)

        if op == "v128.const":
            dst = self.push(V)
            byts = ", ".join(["%d" % b for b in ins.args[0]])
            self.emit("{ static const u8 _c[16] = {%s}; "
                      "memcpy(%s.bytes, _c, 16); }" % (byts, dst))
            return

        if op == "i8x16.shuffle":
            b, _ = self.pop()
            a, _ = self.pop()
            dst = self.push(V)
            sel = ins.args[0]
            # Selectors 0..15 index the first vector, 16..31 the second.
            parts = []
            for k in range(16):
                s_k = sel[k]
                src = a if s_k < 16 else b
                parts.append("%s.u8x16[%d]" % (src, s_k & 15))
            self.emit("{ v128 _t; %s %s = _t; }"
                      % (" ".join(["_t.u8x16[%d] = %s;" % (k, parts[k])
                                   for k in range(16)]), dst))
            return

        if ".extract_lane" in op:
            shape = op.split(".")[0]
            lanes, uf, sf, is_float = simd.SHAPES[shape]
            src, _ = self.pop()
            rt = {"i8x16": w.I32, "i16x8": w.I32, "i32x4": w.I32,
                  "i64x2": w.I64, "f32x4": w.F32, "f64x2": w.F64}[shape]
            dst = self.push(rt)
            field = sf if op.endswith("_s") else uf
            cast = ""
            if op.endswith("_s"):
                cast = "(u32)(s32)"
            self.emit("%s = %s%s.%s[%d];" % (dst, cast, src, field,
                                             ins.args[0]))
            return

        if ".replace_lane" in op:
            shape = op.split(".")[0]
            lanes, uf, sf, is_float = simd.SHAPES[shape]
            val, _ = self.pop()
            vec, _ = self.pop()
            dst = self.push(V)
            self.emit("%s = %s; %s.%s[%d] = %s;"
                      % (dst, vec, dst, uf, ins.args[0], val))
            return

        if op.startswith("v128.load") and op.endswith("_lane"):
            width = op[len("v128.load"):-len("_lane")]
            field = {"8": "u8x16", "16": "u16x8",
                     "32": "u32x4", "64": "u64x2"}[width]
            loader = {"8": "wasm_load_u8", "16": "wasm_load_u16",
                      "32": "wasm_load_u32", "64": "wasm_load_u64"}[width]
            vec, _ = self.pop()
            addr, _ = self.pop()
            dst = self.push(V)
            self.emit("%s = %s; %s.%s[%d] = %s(mem, MEM_SIZE, "
                      "(u32)(%s + %du));"
                      % (dst, vec, dst, field, ins.args[2], loader,
                         addr, ins.args[1]))
            return

        if op.startswith("v128.store") and op.endswith("_lane"):
            width = op[len("v128.store"):-len("_lane")]
            field = {"8": "u8x16", "16": "u16x8",
                     "32": "u32x4", "64": "u64x2"}[width]
            storer = {"8": "wasm_store_u8", "16": "wasm_store_u16",
                      "32": "wasm_store_u32", "64": "wasm_store_u64"}[width]
            vec, _ = self.pop()
            addr, _ = self.pop()
            self.emit("%s(mem, MEM_SIZE, (u32)(%s + %du), %s.%s[%d]);"
                      % (storer, addr, ins.args[1], vec, field,
                         ins.args[2]))
            return

        fn = simd_c.HANDLERS.get(op)
        if fn is None:
            raise Wasm2CError("unhandled SIMD instruction '%s'" % op)

        # Operand and result types, worked out from the operator's name.
        arity, result = self._simd_shape(op)
        args = []
        for _ in range(arity):
            name, _vt = self.pop()
            args.append(name)
        args.reverse()
        if result is None:
            self.emit(fn(self, None, args, ins))
            return
        dst = self.push(result)
        self.emit(fn(self, dst, args, ins))

    def _simd_shape(self, op):
        """(argument count, result type) for a SIMD operator.

        Derived from the name rather than tabulated: the shape prefix and the
        operator suffix already say what the types are, and a second table
        would only be a second thing to keep in step.
        """
        V = reader.V128
        if op == "v128.store":
            return 2, None
        if op == "v128.bitselect":
            return 3, V
        if op == "v128.any_true":
            return 1, w.I32
        if op.endswith(".all_true"):
            return 1, w.I32
        if op.endswith(".bitmask"):
            return 1, w.I32
        if op.endswith(".splat"):
            # Takes one *scalar* and produces a vector. The scalar's type is
            # not needed here -- popping does not depend on it -- but the
            # result is a v128, not the scalar type the shape names.
            return 1, V
        if op.startswith("v128.load"):
            return 1, V
        # A shift takes a vector and a scalar count; everything else with two
        # operands takes two vectors.
        base = op.split(".")[-1]
        if base in ("shl", "shr_s", "shr_u"):
            return 2, V
        unary = ("neg", "abs", "sqrt", "ceil", "floor", "trunc", "nearest",
                 "popcnt", "not")
        if base in unary:
            return 1, V
        if ("extend_" in op or "extadd_" in op or "convert_" in op
                or "trunc_sat_" in op or "demote_" in op
                or "promote_" in op):
            return 1, V
        return 2, V

    # -- helpers -----------------------------------------------------------

    def _blocktype(self, bt):
        if bt is None:
            return []
        if isinstance(bt, tuple):
            raise Wasm2CError("multi-value block types are not supported")
        return [bt]

    def _settle_result(self, blk):
        """Move a construct's result into the slot it will occupy afterwards.

        Inside the construct the result sits at whatever depth the body left
        it; outside, it must be at the construct's entry depth. Without this
        move, a block that produces a value would leave it in the wrong
        variable.
        """
        if not blk.result or self.unreachable:
            return
        if len(self.stack) <= blk.depth:
            return
        src, vt = self.stack[-1], None
        vt = self.stack[-1]
        src = self._slot(vt, len(self.stack) - 1)
        dst = self._slot(vt, blk.depth)
        if src != dst:
            self.emit("%s = %s;" % (dst, src))

    def _branch(self, depth):
        """Emit a branch out of (or back into) the `depth`-th enclosing
        construct."""
        if depth >= len(self.blocks):
            raise Wasm2CError("branch depth %d exceeds nesting" % depth)
        blk = self.blocks[len(self.blocks) - 1 - depth]
        if blk.kind == "func":
            self._emit_return(blk.result and None or None, inline=True,
                              results=blk.result)
            return
        # A branch carries the construct's result with it.
        if blk.result and not self.unreachable and len(self.stack) > blk.depth:
            vt = self.stack[-1]
            src = self._slot(vt, len(self.stack) - 1)
            dst = self._slot(vt, blk.depth)
            if src != dst:
                self.emit("%s = %s;" % (dst, src))
        blk.used[0] = True
        self.emit("goto %s;" % blk.label)

    def _emit_return(self, ftype, inline=False, results=None):
        res = results if results is not None else (
            ftype.results if ftype is not None else [])
        if res:
            if self.unreachable or not self.stack:
                self.emit("return 0;")
            else:
                name, _ = self.pop() if inline else (
                    self._slot(self.stack[-1], len(self.stack) - 1), None)
                self.emit("return %s;" % name)
        else:
            self.emit("return;")

    def _local_name(self, index):
        return "L%d" % index

    def _local_type(self, func, ftype, index):
        if index < len(ftype.params):
            return ftype.params[index]
        k = index - len(ftype.params)
        if k >= len(func.local_types):
            raise Wasm2CError("local index %d out of range" % index)
        return func.local_types[k]

    def _float_literal(self, val, vt):
        import math
        if math.isnan(val):
            return "(f32)NAN" if vt == w.F32 else "(f64)NAN"
        if math.isinf(val):
            s = "INFINITY" if val > 0 else "-INFINITY"
            return "(f32)%s" % s if vt == w.F32 else "(f64)%s" % s
        # %r on a Python float round-trips exactly, which is what matters:
        # a decimal that reads back as a different double would silently
        # change the program.
        return "%r%s" % (val, "f" if vt == w.F32 else "")

    def _call(self, index):
        ftype = self.mod.type_of_func(index)
        args = []
        for _ in ftype.params:
            name, _vt = self.pop()
            args.append(name)
        args.reverse()
        call = "%s(%s)" % (self._func_name(index), ", ".join(args))
        if ftype.results:
            dst = self.push(ftype.results[0])
            self.emit("%s = %s;" % (dst, call))
        else:
            self.emit("%s;" % call)

    def _call_indirect(self, type_index):
        ftype = self.mod.types[type_index]
        idx, _ = self.pop()
        args = []
        for _ in ftype.params:
            name, _vt = self.pop()
            args.append(name)
        args.reverse()
        ret = VALTYPE[ftype.results[0]][0] if ftype.results else "void"
        sig = ", ".join([VALTYPE[p][0] for p in ftype.params]) or "void"
        # The cast must wrap the table lookup, not the whole call: without
        # the outer parentheses C reads this as casting the call's *result*,
        # and then applies the arguments to a function it thinks returns u32.
        cast = "(%s (*)(%s))" % (ret, sig)
        call = "((%swasm_table_get(%s)))(%s)" % (cast, idx, ", ".join(args))
        if ftype.results:
            dst = self.push(ftype.results[0])
            self.emit("%s = %s;" % (dst, call))
        else:
            self.emit("%s;" % call)

    def _func_name(self, index):
        return func_c_name(self.mod, index)

    def _numeric(self, op):
        if "." not in op:
            raise Wasm2CError("unhandled instruction '%s'" % op)
        prefix, rest = op.split(".", 1)
        vt = {"i32": w.I32, "i64": w.I64,
              "f32": w.F32, "f64": w.F64}.get(prefix)
        if vt is None:
            raise Wasm2CError("unhandled instruction '%s'" % op)
        ctype = VALTYPE[vt][0]
        is_float = vt in (w.F32, w.F64)

        if op in CASTS:
            rtype, cast = CASTS[op]
            src, _ = self.pop()
            dst = self.push(rtype)
            self.emit("%s = %s%s;" % (dst, cast, src))
            return
        if op in CALL_CONV:
            src, _ = self.pop()
            dst = self.push(CALL_CONV[op])
            self.emit("%s = wasm_%s(%s);" % (dst, op.replace(".", "_"), src))
            return
        if op in REINTERPRET:
            rtype, helper = REINTERPRET[op]
            src, _ = self.pop()
            dst = self.push(rtype)
            self.emit("%s = %s(%s);" % (dst, helper, src))
            return
        if rest == "eqz":
            src, _ = self.pop()
            dst = self.push(w.I32)
            self.emit("%s = (%s == 0);" % (dst, src))
            return
        if op in HELPERS:
            fn = "wasm_" + op.replace(".", "_")
            # clz/ctz/popcnt are unary; everything else here is binary.
            if rest in ("clz", "ctz", "popcnt"):
                src, _ = self.pop()
                dst = self.push(vt)
                self.emit("%s = %s(%s);" % (dst, fn, src))
                return
            b, _ = self.pop()
            a, _ = self.pop()
            dst = self.push(vt)
            self.emit("%s = %s(%s, %s);" % (dst, fn, a, b))
            return
        if is_float and rest in UNARY_FLOAT:
            src, _ = self.pop()
            dst = self.push(vt)
            if rest == "neg":
                self.emit("%s = -%s;" % (dst, src))
            else:
                fn = UNARY_FLOAT[rest]
                suffix = "f" if vt == w.F32 else ""
                self.emit("%s = %s%s(%s);" % (dst, fn, suffix, src))
            return
        if rest in COMPARE:
            b, _ = self.pop()
            a, _ = self.pop()
            dst = self.push(w.I32)
            cmp_op = COMPARE[rest]
            if rest.endswith("_s"):
                st = "s32" if vt == w.I32 else "s64"
                self.emit("%s = ((%s)%s %s (%s)%s);"
                          % (dst, st, a, cmp_op, st, b))
            else:
                self.emit("%s = (%s %s %s);" % (dst, a, cmp_op, b))
            return
        base = rest.split("_")[0]
        if base in INFIX:
            b, _ = self.pop()
            a, _ = self.pop()
            dst = self.push(vt)
            self.emit("%s = (%s)(%s %s %s);"
                      % (dst, ctype, a, INFIX[base], b))
            return
        raise Wasm2CError("unhandled instruction '%s'" % op)


def func_c_name(mod, index):
    """C name for a function, imported or defined."""
    n_imported = mod.num_imported_funcs()
    if index < n_imported:
        k = 0
        for imp in mod.imports:
            if imp.kind != w.EXTERNAL_KIND_FUNC:
                continue
            if k == index:
                return "wasm_import_%s_%s" % (c_ident(imp.module),
                                              c_ident(imp.field))
            k += 1
    return "w2c_f%d" % index


def write_module(mod, module_name="module", emit_main=True):
    """Render a decoded module as C source text.

    `emit_main` off leaves out the entry point, so the translation can be
    linked into a driver of the caller's own -- which is how
    tools/wasm_module_difftest.py drives each export separately.
    """
    lines = []
    ap = lines.append

    ap("/* Generated by tools/wasm2c.py from a WebAssembly module.")
    ap(" *")
    ap(" * The operand stack has been flattened into typed locals (i0, j0,")
    ap(" * f0, d0 ...) and wasm's structured control flow into labels and")
    ap(" * goto. Compile with:")
    ap(" *")
    ap(" *   gcc -std=c99 -I tools this.c -o prog -lm")
    ap(" */")
    ap('#include "wasm2c_rt.h"')
    ap('#include "wasm2c_rt_simd.h"')
    ap("")

    # -- memory
    pages = mod.memory_pages if mod.memory_pages else 0
    if pages:
        # Memory is allocated rather than a fixed array, because a module may
        # grow it -- an allocator in a real module does so on its first call,
        # and a fixed array turns that into a trap.
        ap("#define MEM_INITIAL_PAGES %d" % pages)
        ap("#define MEM_MAX_PAGES %d" % (mod.memory_max_pages
                                         if mod.memory_max_pages >= 0
                                         else 65536))
        ap("static u8 *mem = 0;")
        ap("static u64 wasm_mem_size = 0;")
        ap("#define MEM_SIZE wasm_mem_size")
        ap("static u32 wasm_memory_grow(u32 delta);")
    else:
        # A module with no memory still compiles: the helpers take the base
        # and size as arguments, so a zero-sized memory simply traps on any
        # access -- which cannot happen, since such a module has no loads.
        ap("#define MEM_INITIAL_PAGES 0")
        ap("#define MEM_MAX_PAGES 0")
        ap("static u8 *mem = 0;")
        ap("static u64 wasm_mem_size = 0;")
        ap("#define MEM_SIZE wasm_mem_size")
        ap("static u32 wasm_memory_grow(u32 delta);")
    ap("")
    # The WASI shims reach into linear memory, so they can only be defined
    # once MEM_SIZE and `mem` exist.
    ap('#include "wasm2c_rt_wasi.h"')
    ap("")

    # -- globals
    for i in range(len(mod.globals)):
        g = mod.globals[i]
        init = g.init
        if isinstance(init, tuple):
            raise Wasm2CError("imported globals are not supported")
        ctype = VALTYPE[g.valtype][0]
        if g.valtype in (w.F32, w.F64):
            text = repr(init) + ("f" if g.valtype == w.F32 else "")
        elif g.valtype == w.I64:
            text = "%dull" % (init & 0xFFFFFFFFFFFFFFFF)
        else:
            text = "%du" % (init & 0xFFFFFFFF)
        ap("static %s g%d = %s;" % (ctype, i, text))
    if mod.globals:
        ap("")

    # -- forward declarations
    n_imported = mod.num_imported_funcs()
    for i in range(len(mod.func_type_index)):
        ft = mod.type_of_func(i)
        ret = VALTYPE[ft.results[0]][0] if ft.results else "void"
        params = ", ".join([VALTYPE[p][0] for p in ft.params]) or "void"
        name = func_c_name(mod, i)
        if i < n_imported:
            ap("extern %s %s(%s);" % (ret, name, params))
        else:
            ap("static %s %s(%s);" % (ret, name, params))
    ap("")

    # -- function table
    if mod.table_size:
        ap("/* The function table. Entries are stored as generic pointers and")
        ap(" * cast back to the right signature at each call_indirect, whose")
        ap(" * type index tells us what that signature is. */")
        ap("typedef void (*wasm_anyfunc)(void);")
        ap("static wasm_anyfunc wasm_table[%d];" % mod.table_size)
        ap("static wasm_anyfunc wasm_table_get(u32 i) {")
        ap("  if (i >= %du || !wasm_table[i])" % mod.table_size)
        ap('    wasm_trap("undefined element");')
        ap("  return wasm_table[i];")
        ap("}")
        ap("")

    # -- data segments
    if mod.data_segments:
        ap("static void wasm_init_memory(void) {")
        for seg in mod.data_segments:
            chunks = []
            for b in seg.data:
                chunks.append("%d" % b)
            ap("  {")
            ap("    static const u8 d[] = {%s};" % ",".join(chunks))
            ap("    memcpy(mem + %du, d, sizeof(d));" % seg.offset)
            ap("  }")
        ap("}")
        ap("")
    else:
        ap("static void wasm_init_memory(void) {}")
        ap("")

    # -- bodies
    for i in range(len(mod.funcs)):
        func = mod.funcs[i]
        gidx = n_imported + i
        ft = mod.types[func.type_index]
        ret = VALTYPE[ft.results[0]][0] if ft.results else "void"
        params = []
        for k in range(len(ft.params)):
            params.append("%s L%d" % (VALTYPE[ft.params[k]][0], k))
        sig = ", ".join(params) or "void"
        ap("static %s %s(%s) {" % (ret, func_c_name(mod, gidx), sig))

        fw = FunctionWriter(mod, gidx, [])
        decls, body = fw.write(func, ft)

        for k in range(len(func.local_types)):
            vt = func.local_types[k]
            # A v128 is a union and cannot be initialised from 0; the
            # designated-initialiser form zeroes the whole thing.
            if vt == reader.V128:
                zero = "{{0}}"
            elif vt in (w.F32, w.F64):
                zero = "0.0"
            else:
                zero = "0"
            ap("  %s L%d = %s;" % (VALTYPE[vt][0], len(ft.params) + k, zero))
        for d in decls:
            ap(d)
        for line in body:
            ap(line)
        ap("  Lret_%d: ;" % gidx)
        if ft.results:
            ap("  return 0;")
        ap("}")
        ap("")

    # -- initialisation and entry point
    ap("/* Grow linear memory by `delta` pages, returning the previous size")
    ap(" * in pages, or -1 if it cannot grow. The new pages read as zero, as")
    ap(" * the specification requires. */")
    ap("static u32 wasm_memory_grow(u32 delta) {")
    ap("  u64 old_pages = wasm_mem_size / 65536u;")
    ap("  u64 new_pages = old_pages + (u64)delta;")
    ap("  u8 *p;")
    ap("  if (new_pages > (u64)MEM_MAX_PAGES) return (u32)-1;")
    ap("  p = (u8 *)realloc(mem, (size_t)(new_pages * 65536u));")
    ap("  if (!p) return (u32)-1;")
    ap("  memset(p + wasm_mem_size, 0,")
    ap("         (size_t)(new_pages * 65536u - wasm_mem_size));")
    ap("  mem = p;")
    ap("  wasm_mem_size = new_pages * 65536u;")
    ap("  return (u32)old_pages;")
    ap("}")
    ap("")
    ap("/* Exposed so an external driver can inspect memory. */")
    ap("u8 *wasm_memory(void) { return mem; }")
    ap("u64 wasm_memory_size(void) { return MEM_SIZE; }")
    ap("")
    ap("void wasm_init(void) {")
    ap("  /* calloc, so memory starts zeroed as the specification requires;")
    ap("   * the data segments are then laid over it. */")
    ap("  size_t _init = (size_t)MEM_INITIAL_PAGES * 65536u;")
    ap("  mem = (u8 *)calloc(_init ? _init : 1u, 1u);")
    ap('  if (!mem) wasm_trap("out of memory");')
    ap("  wasm_mem_size = (u64)MEM_INITIAL_PAGES * 65536u;")
    ap("  wasm_init_memory();")
    for idx in sorted(mod.table_entries):
        ap("  wasm_table[%d] = (wasm_anyfunc)%s;"
           % (idx, func_c_name(mod, mod.table_entries[idx])))
    ap("}")
    ap("")

    start_name = None
    main_name = None
    for exp in mod.exports:
        if exp.kind != w.EXTERNAL_KIND_FUNC:
            continue
        if exp.name == "_start":
            start_name = func_c_name(mod, exp.index)
        elif exp.name == "main":
            main_name = func_c_name(mod, exp.index)

    # Exported functions get a wrapper under their exported name, so the
    # translated module can be linked against like any other C.
    for exp in mod.exports:
        if exp.kind != w.EXTERNAL_KIND_FUNC:
            continue
        if exp.name in ("main", "_start"):
            continue
        ft = mod.type_of_func(exp.index)
        ret = VALTYPE[ft.results[0]][0] if ft.results else "void"
        params = []
        args = []
        for k in range(len(ft.params)):
            params.append("%s a%d" % (VALTYPE[ft.params[k]][0], k))
            args.append("a%d" % k)
        sig = ", ".join(params) or "void"
        ap("%s w2c_export_%s(%s) {" % (ret, c_ident(exp.name), sig))
        ap("  %s%s(%s);" % ("return " if ft.results else "",
                            func_c_name(mod, exp.index), ", ".join(args)))
        ap("}")
        ap("")

    if not emit_main:
        return "\n".join(lines) + "\n"

    ap("int main(int argc, char **argv) {")
    ap("  (void)argc; (void)argv;")
    ap("  wasm_init();")
    if start_name is not None:
        ap("  %s();" % start_name)
        ap("  return wasm_exit_code;")
    elif main_name is not None:
        ft = mod.type_of_func(
            [e.index for e in mod.exports if e.name == "main"][0])
        if ft.results:
            ap("  return (int)(%s() & 0xFF);" % main_name)
        else:
            ap("  %s();" % main_name)
            ap("  return 0;")
    else:
        ap('  wasm_trap("module exports neither _start nor main");')
        ap("  return 1;")
    ap("}")
    return "\n".join(lines) + "\n"


def main(argv):
    args = []
    out_path = None
    emit_main = True
    i = 1
    while i < len(argv):
        if argv[i] == "-o":
            i += 1
            out_path = argv[i]
        elif argv[i] == "--no-main":
            emit_main = False
        else:
            args.append(argv[i])
        i += 1
    if not args:
        print("usage: wasm2c.py <module.wasm> [-o out.c] [--no-main]")
        return 2

    in_path = args[0]
    try:
        mod = reader.decode_file(in_path)
    except reader.WasmDecodeError as e:
        sys.stderr.write("wasm2c: cannot read %s: %s\n" % (in_path, e))
        return 1
    try:
        text = write_module(mod, emit_main=emit_main)
    except Wasm2CError as e:
        sys.stderr.write("wasm2c: %s\n" % e)
        return 1

    if out_path:
        with open(out_path, "w") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
