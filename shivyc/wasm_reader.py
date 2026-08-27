"""WebAssembly binary decoding -- the inverse of shivyc/wasm.py.

`shivyc/wasm.py` turns a module structure into bytes; this turns bytes back
into a module structure. Together they let the toolchain go both ways:

    .c  --> shivyc --> .wasm        (shivyc/wasm.py)
    .wasm --> tools/wasm2c.py --> .c   (this module, plus a C writer)

Nothing here is specific to modules Crust produced. The decoder reads the MVP
binary format plus the three extensions the back end relies on (sign
extension, non-trapping float-to-int, bulk memory), so a module from any
producer can be read -- which is the point, since a decompiler that only
accepts its own compiler's output is not much of a decompiler.

Instructions are decoded into a flat list of `Instr` records rather than a
tree. Wasm's control flow is already a linear sequence with explicit `end`
markers, and the C writer wants to walk it linearly anyway, so building a tree
here would only mean taking it apart again.

Self-host note: as elsewhere, plain loops and instance attributes only, so the
restricted-Python translator can lower this.
"""

import shivyc.wasm as w
import shivyc.wasm_simd as simd


# The SIMD value type. Not in shivyc/wasm.py because the encoder never emits
# one; the decoder must still recognise it.
V128 = 0x7B


class WasmDecodeError(Exception):
    """Raised when a module is malformed or uses something not decoded here.

    Carrying the byte offset matters: a truncated or mis-encoded module
    otherwise fails with a message that says nothing about where.
    """

    def __init__(self, message, offset=-1):
        Exception.__init__(self, message)
        self.message = message
        self.offset = offset

    def __str__(self):
        if self.offset >= 0:
            return "%s (at byte 0x%x)" % (self.message, self.offset)
        return self.message


# ------------------------------------------------------- immediate shapes
#
# How to read whatever follows an opcode. Kept as a name rather than a number
# of bytes because several are variable-length.
IMM_NONE = "none"           # no immediate
IMM_BLOCKTYPE = "blocktype"  # 0x40, a valtype byte, or a signed type index
IMM_U32 = "u32"             # one unsigned index (local, global, function, ...)
IMM_U32X2 = "u32x2"         # two, e.g. call_indirect's type and table
IMM_MEMARG = "memarg"       # alignment hint and offset
IMM_BR_TABLE = "brtable"    # a vector of depths plus a default
IMM_I32 = "i32"             # signed LEB
IMM_I64 = "i64"             # signed LEB
IMM_F32 = "f32"             # 4 raw IEEE bytes
IMM_F64 = "f64"             # 8 raw IEEE bytes
IMM_BYTE = "byte"           # a single reserved byte (memory.size / grow)
# SIMD adds four more, defined alongside the opcode table in wasm_simd.py so
# the table and its immediate shapes stay in one place.
IMM_MEMARG_LANE = simd.IMM_MEMARG_LANE
IMM_LANE = simd.IMM_LANE
IMM_V128 = simd.IMM_V128
IMM_SHUFFLE = simd.IMM_SHUFFLE


def _numeric_table():
    """Opcode -> mnemonic for the numeric operators.

    Built rather than written out because they are contiguous and regular; the
    irregular opcodes are listed explicitly in OPCODES below. Every one of
    these takes no immediate.
    """
    t = {}
    t[0x45] = "i32.eqz"
    t[0x50] = "i64.eqz"
    for name, op in w.I32_CMP.items():
        t[op] = "i32." + name
    for name, op in w.I64_CMP.items():
        t[op] = "i64." + name
    for name, op in w.I32_BIN.items():
        t[op] = "i32." + name
    for name, op in w.I64_BIN.items():
        t[op] = "i64." + name
    for name, op in w.F32_CMP.items():
        t[op] = "f32." + name
    for name, op in w.F64_CMP.items():
        t[op] = "f64." + name
    for name, op in w.F32_BIN.items():
        t[op] = "f32." + name
    for name, op in w.F64_BIN.items():
        t[op] = "f64." + name

    # Unary float operators and the counting/rotating integer ones, which the
    # encoder has no table for because the back end never emits them -- but a
    # module from another producer certainly may.
    extra = {
        0x67: "i32.clz", 0x68: "i32.ctz", 0x69: "i32.popcnt",
        0x77: "i32.rotl", 0x78: "i32.rotr",
        0x79: "i64.clz", 0x7A: "i64.ctz", 0x7B: "i64.popcnt",
        0x89: "i64.rotl", 0x8A: "i64.rotr",
        0x8B: "f32.abs", 0x8C: "f32.neg", 0x8D: "f32.ceil",
        0x8E: "f32.floor", 0x8F: "f32.trunc", 0x90: "f32.nearest",
        0x91: "f32.sqrt", 0x96: "f32.min", 0x97: "f32.max",
        0x98: "f32.copysign",
        0x99: "f64.abs", 0x9A: "f64.neg", 0x9B: "f64.ceil",
        0x9C: "f64.floor", 0x9D: "f64.trunc", 0x9E: "f64.nearest",
        0x9F: "f64.sqrt", 0xA4: "f64.min", 0xA5: "f64.max",
        0xA6: "f64.copysign",
        0xA7: "i32.wrap_i64",
        0xA8: "i32.trunc_f32_s", 0xA9: "i32.trunc_f32_u",
        0xAA: "i32.trunc_f64_s", 0xAB: "i32.trunc_f64_u",
        0xAC: "i64.extend_i32_s", 0xAD: "i64.extend_i32_u",
        0xAE: "i64.trunc_f32_s", 0xAF: "i64.trunc_f32_u",
        0xB0: "i64.trunc_f64_s", 0xB1: "i64.trunc_f64_u",
        0xB2: "f32.convert_i32_s", 0xB3: "f32.convert_i32_u",
        0xB4: "f32.convert_i64_s", 0xB5: "f32.convert_i64_u",
        0xB6: "f32.demote_f64",
        0xB7: "f64.convert_i32_s", 0xB8: "f64.convert_i32_u",
        0xB9: "f64.convert_i64_s", 0xBA: "f64.convert_i64_u",
        0xBB: "f64.promote_f32",
        0xBC: "i32.reinterpret_f32", 0xBD: "i64.reinterpret_f64",
        0xBE: "f32.reinterpret_i32", 0xBF: "f64.reinterpret_i64",
        0xC0: "i32.extend8_s", 0xC1: "i32.extend16_s",
        0xC2: "i64.extend8_s", 0xC3: "i64.extend16_s",
        0xC4: "i64.extend32_s",
    }
    for op in extra:
        t[op] = extra[op]
    return t


def _build_opcodes():
    """Opcode -> (mnemonic, immediate shape)."""
    t = {}
    for op in _numeric_table():
        t[op] = (_numeric_table()[op], IMM_NONE)

    control = {
        0x00: ("unreachable", IMM_NONE),
        0x01: ("nop", IMM_NONE),
        0x02: ("block", IMM_BLOCKTYPE),
        0x03: ("loop", IMM_BLOCKTYPE),
        0x04: ("if", IMM_BLOCKTYPE),
        0x05: ("else", IMM_NONE),
        0x0B: ("end", IMM_NONE),
        0x0C: ("br", IMM_U32),
        0x0D: ("br_if", IMM_U32),
        0x0E: ("br_table", IMM_BR_TABLE),
        0x0F: ("return", IMM_NONE),
        0x10: ("call", IMM_U32),
        0x11: ("call_indirect", IMM_U32X2),
        0x1A: ("drop", IMM_NONE),
        0x1B: ("select", IMM_NONE),
        0x20: ("local.get", IMM_U32),
        0x21: ("local.set", IMM_U32),
        0x22: ("local.tee", IMM_U32),
        0x23: ("global.get", IMM_U32),
        0x24: ("global.set", IMM_U32),
        0x3F: ("memory.size", IMM_BYTE),
        0x40: ("memory.grow", IMM_BYTE),
        0x41: ("i32.const", IMM_I32),
        0x42: ("i64.const", IMM_I64),
        0x43: ("f32.const", IMM_F32),
        0x44: ("f64.const", IMM_F64),
    }
    for op in control:
        t[op] = control[op]

    loads = {
        0x28: "i32.load", 0x29: "i64.load",
        0x2A: "f32.load", 0x2B: "f64.load",
        0x2C: "i32.load8_s", 0x2D: "i32.load8_u",
        0x2E: "i32.load16_s", 0x2F: "i32.load16_u",
        0x30: "i64.load8_s", 0x31: "i64.load8_u",
        0x32: "i64.load16_s", 0x33: "i64.load16_u",
        0x34: "i64.load32_s", 0x35: "i64.load32_u",
        0x36: "i32.store", 0x37: "i64.store",
        0x38: "f32.store", 0x39: "f64.store",
        0x3A: "i32.store8", 0x3B: "i32.store16",
        0x3C: "i64.store8", 0x3D: "i64.store16", 0x3E: "i64.store32",
    }
    for op in loads:
        t[op] = (loads[op], IMM_MEMARG)
    return t


OPCODES = _build_opcodes()

# The 0xFC-prefixed opcodes, keyed by the index that follows the prefix.
FC_OPCODES = {
    0: ("i32.trunc_sat_f32_s", IMM_NONE),
    1: ("i32.trunc_sat_f32_u", IMM_NONE),
    2: ("i32.trunc_sat_f64_s", IMM_NONE),
    3: ("i32.trunc_sat_f64_u", IMM_NONE),
    4: ("i64.trunc_sat_f32_s", IMM_NONE),
    5: ("i64.trunc_sat_f32_u", IMM_NONE),
    6: ("i64.trunc_sat_f64_s", IMM_NONE),
    7: ("i64.trunc_sat_f64_u", IMM_NONE),
    8: ("memory.init", IMM_U32X2),
    9: ("data.drop", IMM_U32),
    10: ("memory.copy", IMM_U32X2),
    11: ("memory.fill", IMM_BYTE),
}


class Instr:
    """One decoded instruction: its mnemonic and whatever followed it.

    `args` is a list whose meaning depends on the mnemonic -- a local index, a
    (align, offset) pair, a constant, a branch table. The C writer switches on
    the mnemonic anyway, so a single generic slot beats a dozen named ones.
    """

    def __init__(self, op, args, offset):
        self.op = op
        self.args = args
        self.offset = offset

    def __repr__(self):                                  # pragma: no cover
        return "Instr(%s, %r)" % (self.op, self.args)


class FuncType:
    def __init__(self, params, results):
        self.params = params
        self.results = results

    def __repr__(self):                                  # pragma: no cover
        return "FuncType(%r -> %r)" % (self.params, self.results)


class Import:
    def __init__(self, module, field, kind, type_index):
        self.module = module
        self.field = field
        self.kind = kind
        self.type_index = type_index


class Export:
    def __init__(self, name, kind, index):
        self.name = name
        self.kind = kind
        self.index = index


class Func:
    """A defined (not imported) function: its signature plus its body."""

    def __init__(self, type_index, local_types, instrs):
        self.type_index = type_index
        self.local_types = local_types      # types of locals after the params
        self.instrs = instrs


class Global:
    def __init__(self, valtype, mutable, init):
        self.valtype = valtype
        self.mutable = mutable
        self.init = init                    # a constant, already evaluated


class DataSegment:
    def __init__(self, offset, data):
        self.offset = offset
        self.data = data


class Module:
    """A decoded module.

    Function indices run over imports first and then defined functions, as in
    the binary format; `func_type_index` covers the whole space so a call site
    can look up a signature without caring which side of the boundary it is
    on.
    """

    def __init__(self):
        self.types = []
        self.imports = []
        self.funcs = []                     # defined functions, in index order
        self.func_type_index = []           # imports + defined, whole space
        self.table_size = 0
        self.table_entries = {}             # table index -> function index
        self.memory_pages = None
        self.globals = []
        self.exports = []
        self.data_segments = []
        self.data_count = -1
        self.start = -1

    def num_imported_funcs(self):
        n = 0
        for imp in self.imports:
            if imp.kind == w.EXTERNAL_KIND_FUNC:
                n += 1
        return n

    def type_of_func(self, index):
        """FuncType of function `index`, imported or defined."""
        if index < 0 or index >= len(self.func_type_index):
            raise WasmDecodeError("function index %d out of range" % index)
        return self.types[self.func_type_index[index]]


# Canonical section order. This is *not* ascending by id: bulk memory added
# DataCount (12) between Element (9) and Code (10), so that a streaming
# validator learns the segment count before it has to validate a memory.init.
# Checking raw id order rejects every real-world module that carries one.
SECTION_ORDER = {
    w.SEC_TYPE: 1, w.SEC_IMPORT: 2, w.SEC_FUNCTION: 3, w.SEC_TABLE: 4,
    w.SEC_MEMORY: 5, w.SEC_GLOBAL: 6, w.SEC_EXPORT: 7, w.SEC_START: 8,
    w.SEC_ELEMENT: 9, w.SEC_DATACOUNT: 10, w.SEC_CODE: 11, w.SEC_DATA: 12,
}


class Reader:
    """A cursor over the module bytes with the primitive readers."""

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def at_end(self):
        return self.pos >= len(self.data)

    def byte(self):
        if self.pos >= len(self.data):
            raise WasmDecodeError("unexpected end of module", self.pos)
        b = self.data[self.pos]
        self.pos += 1
        return b

    def bytes(self, n):
        if self.pos + n > len(self.data):
            raise WasmDecodeError("unexpected end of module", self.pos)
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def uleb(self):
        """Unsigned LEB128."""
        result = 0
        shift = 0
        start = self.pos
        while True:
            b = self.byte()
            result = result | ((b & 0x7F) << shift)
            if not (b & 0x80):
                return result
            shift += 7
            if shift > 63:
                raise WasmDecodeError("LEB128 too long", start)

    def sleb(self, bits=64):
        """Signed LEB128, sign-extended from wherever it terminated."""
        result = 0
        shift = 0
        start = self.pos
        while True:
            b = self.byte()
            result = result | ((b & 0x7F) << shift)
            shift += 7
            if not (b & 0x80):
                # The terminating byte's 0x40 bit is the sign.
                if (b & 0x40) and shift < 64:
                    result = result - (1 << shift)
                return result
            if shift > 70:
                raise WasmDecodeError("LEB128 too long", start)

    def name(self):
        n = self.uleb()
        raw = self.bytes(n)
        return bytes(bytearray(raw)).decode("utf-8")

    def valtype(self):
        b = self.byte()
        if b == V128:
            return V128
        if b in (0x70, 0x6F):
            raise WasmDecodeError(
                "reference types are not decoded yet", self.pos - 1)
        if b not in (w.I32, w.I64, w.F32, w.F64):
            raise WasmDecodeError("unknown value type 0x%x" % b, self.pos - 1)
        return b


def _decode_instrs(r, end_pos):
    """Decode instructions until `end_pos`.

    The body's final `end` is included in the returned list: the C writer uses
    it to close the function's outermost scope, exactly as it closes any other
    block.
    """
    out = []
    while r.pos < end_pos:
        offset = r.pos
        op = r.byte()

        if op == 0xFD:                     # SIMD
            sub = r.uleb()
            if sub not in simd.OPCODES:
                raise WasmDecodeError("unknown SIMD opcode %d" % sub, offset)
            name, imm = simd.OPCODES[sub]
            args = []
            if imm == IMM_MEMARG:
                args = [r.uleb(), r.uleb()]
            elif imm == IMM_MEMARG_LANE:
                args = [r.uleb(), r.uleb(), r.byte()]
            elif imm == IMM_LANE:
                args = [r.byte()]
            elif imm == IMM_V128:
                args = [list(r.bytes(16))]
            elif imm == IMM_SHUFFLE:
                args = [list(r.bytes(16))]
            out.append(Instr(name, args, offset))
            continue

        if op == w.BULK_PREFIX:            # 0xFC: bulk memory + trunc_sat
            sub = r.uleb()
            if sub not in FC_OPCODES:
                raise WasmDecodeError(
                    "unknown 0xFC opcode %d" % sub, offset)
            name, imm = FC_OPCODES[sub]
            args = []
            if imm == IMM_U32X2:
                args = [r.byte(), r.byte()]
            elif imm == IMM_BYTE:
                args = [r.byte()]
            elif imm == IMM_U32:
                args = [r.uleb()]
            out.append(Instr(name, args, offset))
            continue

        if op not in OPCODES:
            raise WasmDecodeError("unknown opcode 0x%x" % op, offset)
        name, imm = OPCODES[op]

        args = []
        if imm == IMM_NONE:
            pass
        elif imm == IMM_BLOCKTYPE:
            b = r.byte()
            if b == w.BLOCK_VOID:
                args = [None]
            elif b in (w.I32, w.I64, w.F32, w.F64):
                args = [b]
            else:
                # A multi-value block type: a signed index into the type
                # section. Decoded so the error names it, but the C writer
                # does not implement multi-value blocks.
                r.pos -= 1
                args = [("typeidx", r.sleb())]
        elif imm == IMM_U32:
            args = [r.uleb()]
        elif imm == IMM_U32X2:
            args = [r.uleb(), r.uleb()]
        elif imm == IMM_MEMARG:
            args = [r.uleb(), r.uleb()]     # align, offset
        elif imm == IMM_BR_TABLE:
            count = r.uleb()
            targets = []
            for _ in range(count):
                targets.append(r.uleb())
            args = [targets, r.uleb()]
        elif imm == IMM_I32:
            args = [r.sleb(32)]
        elif imm == IMM_I64:
            args = [r.sleb(64)]
        elif imm == IMM_F32:
            import struct
            args = [struct.unpack("<f", bytes(bytearray(r.bytes(4))))[0]]
        elif imm == IMM_F64:
            import struct
            args = [struct.unpack("<d", bytes(bytearray(r.bytes(8))))[0]]
        elif imm == IMM_BYTE:
            args = [r.byte()]
        out.append(Instr(name, args, offset))

    if r.pos != end_pos:
        raise WasmDecodeError("instruction ran past its section", r.pos)
    return out


def _const_expr(r):
    """Evaluate a constant initializer expression.

    Only the forms that can legally appear in one: a single constant, or a
    global.get of an imported global. Anything else is not a constant
    expression and the module is malformed.
    """
    offset = r.pos
    op = r.byte()
    if op == w.OP_I32_CONST:
        val = r.sleb(32)
    elif op == w.OP_I64_CONST:
        val = r.sleb(64)
    elif op == w.OP_F32_CONST:
        import struct
        val = struct.unpack("<f", bytes(bytearray(r.bytes(4))))[0]
    elif op == w.OP_F64_CONST:
        import struct
        val = struct.unpack("<d", bytes(bytearray(r.bytes(8))))[0]
    elif op == w.OP_GLOBAL_GET:
        val = ("global", r.uleb())
    else:
        raise WasmDecodeError(
            "not a constant expression (opcode 0x%x)" % op, offset)
    end = r.byte()
    if end != w.OP_END:
        raise WasmDecodeError("constant expression not terminated", r.pos - 1)
    return val


def decode(data):
    """Decode a `.wasm` module from bytes and return a Module."""
    r = Reader(data)
    magic = r.bytes(4)
    if list(magic) != [0x00, 0x61, 0x73, 0x6D]:
        raise WasmDecodeError("not a wasm module (bad magic)", 0)
    version = r.bytes(4)
    if list(version) != [0x01, 0x00, 0x00, 0x00]:
        raise WasmDecodeError("unsupported wasm version", 4)

    mod = Module()
    code_bodies = []
    func_type_indices = []
    last_id = 0

    while not r.at_end():
        sec_start = r.pos
        sec_id = r.byte()
        size = r.uleb()
        end = r.pos + size
        if end > len(data):
            raise WasmDecodeError("section runs past end of module", sec_start)

        if sec_id != w.SEC_CUSTOM:
            # Sections must appear in ascending order. Checking is cheap and
            # turns a silently mis-ordered module into a clear error.
            rank = SECTION_ORDER.get(sec_id, -1)
            if rank < 0:
                raise WasmDecodeError("unknown section id %d" % sec_id,
                                      sec_start)
            if rank < last_id:
                raise WasmDecodeError(
                    "section %d out of canonical order" % sec_id, sec_start)
            last_id = rank

        if sec_id == w.SEC_CUSTOM:
            # Names, debug info, producer metadata. Nothing here reads them,
            # but the cursor must still step over the payload -- leaving it
            # parked at the section start makes the next read fail with a
            # confusing "trailing bytes".
            r.pos = end
        elif sec_id == w.SEC_TYPE:
            for _ in range(r.uleb()):
                tag = r.byte()
                if tag != 0x60:
                    raise WasmDecodeError("bad functype tag", r.pos - 1)
                params = []
                for _p in range(r.uleb()):
                    params.append(r.valtype())
                results = []
                for _q in range(r.uleb()):
                    results.append(r.valtype())
                mod.types.append(FuncType(params, results))
        elif sec_id == w.SEC_IMPORT:
            for _ in range(r.uleb()):
                m = r.name()
                f = r.name()
                kind = r.byte()
                if kind == w.EXTERNAL_KIND_FUNC:
                    tidx = r.uleb()
                    mod.imports.append(Import(m, f, kind, tidx))
                    func_type_indices.append(tidx)
                elif kind == w.EXTERNAL_KIND_TABLE:
                    r.byte()                        # element type
                    flags = r.byte()
                    r.uleb()
                    if flags:
                        r.uleb()
                    mod.imports.append(Import(m, f, kind, -1))
                elif kind == w.EXTERNAL_KIND_MEMORY:
                    flags = r.byte()
                    r.uleb()
                    if flags:
                        r.uleb()
                    mod.imports.append(Import(m, f, kind, -1))
                elif kind == w.EXTERNAL_KIND_GLOBAL:
                    r.valtype()
                    r.byte()                        # mutability
                    mod.imports.append(Import(m, f, kind, -1))
                else:
                    raise WasmDecodeError("unknown import kind %d" % kind,
                                          r.pos - 1)
        elif sec_id == w.SEC_FUNCTION:
            for _ in range(r.uleb()):
                func_type_indices.append(r.uleb())
        elif sec_id == w.SEC_TABLE:
            for _ in range(r.uleb()):
                r.byte()                            # element type
                flags = r.byte()
                mod.table_size = r.uleb()
                if flags:
                    r.uleb()
        elif sec_id == w.SEC_MEMORY:
            for _ in range(r.uleb()):
                flags = r.byte()
                mod.memory_pages = r.uleb()
                if flags:
                    r.uleb()                        # maximum
        elif sec_id == w.SEC_GLOBAL:
            for _ in range(r.uleb()):
                vt = r.valtype()
                mut = r.byte()
                mod.globals.append(Global(vt, mut != 0, _const_expr(r)))
        elif sec_id == w.SEC_EXPORT:
            for _ in range(r.uleb()):
                nm = r.name()
                kind = r.byte()
                mod.exports.append(Export(nm, kind, r.uleb()))
        elif sec_id == w.SEC_START:
            mod.start = r.uleb()
        elif sec_id == w.SEC_ELEMENT:
            for _ in range(r.uleb()):
                flags = r.uleb()
                if flags != 0:
                    raise WasmDecodeError(
                        "only active element segments on table 0 are decoded",
                        r.pos)
                base = _const_expr(r)
                if not isinstance(base, int):
                    raise WasmDecodeError(
                        "element segment offset is not a constant", r.pos)
                for k in range(r.uleb()):
                    mod.table_entries[base + k] = r.uleb()
        elif sec_id == w.SEC_CODE:
            for _ in range(r.uleb()):
                body_size = r.uleb()
                body_end = r.pos + body_size
                local_types = []
                for _g in range(r.uleb()):
                    count = r.uleb()
                    vt = r.valtype()
                    for _k in range(count):
                        local_types.append(vt)
                code_bodies.append((local_types,
                                    _decode_instrs(r, body_end)))
                if r.pos != body_end:
                    raise WasmDecodeError("function body size mismatch", r.pos)
        elif sec_id == w.SEC_DATACOUNT:
            # Only a count, for streaming validators. Nothing here needs it,
            # but a module that carries one must still be readable.
            mod.data_count = r.uleb()
        elif sec_id == w.SEC_DATA:
            for _ in range(r.uleb()):
                flags = r.uleb()
                if flags != 0:
                    raise WasmDecodeError(
                        "only active data segments on memory 0 are decoded",
                        r.pos)
                off = _const_expr(r)
                if not isinstance(off, int):
                    raise WasmDecodeError(
                        "data segment offset is not a constant", r.pos)
                n = r.uleb()
                mod.data_segments.append(DataSegment(off, list(r.bytes(n))))
        else:
            raise WasmDecodeError("unknown section id %d" % sec_id, sec_start)

        if r.pos != end:
            raise WasmDecodeError(
                "section %d has trailing bytes" % sec_id, r.pos)

    mod.func_type_index = func_type_indices
    n_imported = mod.num_imported_funcs()
    if len(code_bodies) != len(func_type_indices) - n_imported:
        raise WasmDecodeError(
            "function section declares %d bodies but code section has %d"
            % (len(func_type_indices) - n_imported, len(code_bodies)))
    for i in range(len(code_bodies)):
        local_types, instrs = code_bodies[i]
        mod.funcs.append(
            Func(func_type_indices[n_imported + i], local_types, instrs))
    return mod


def decode_file(path):
    """Decode the module in the file at `path`."""
    with open(path, "rb") as f:
        return decode(list(bytearray(f.read())))
