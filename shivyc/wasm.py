"""WebAssembly binary encoding.

Every other back end in this tree hands text to an external assembler. WASM has
no assembler in the dependency-free story we want, so this module *is* the
assembler: it emits a complete `.wasm` binary (the MVP format, version 1) with
no wabt, no LLVM, and no npm package involved.

The layout below is the whole of what the integer-core back end needs:

    magic "\\0asm", version 1
    section 1  (type)     -- distinct function signatures
    section 3  (function) -- defined function -> signature index
    section 7  (export)   -- exported function names
    section 10 (code)     -- local declarations + instruction bytes

Sections must appear in ascending numeric order, which `WasmModule.encode`
enforces by construction rather than by checking. Memory, globals, imports and
data land in later stages alongside pointers and string literals; the section
IDs are reserved here (see SEC_*) so the ordering does not have to be
rediscovered then.

Numbers are LEB128: unsigned for indices and counts, *signed* for the operands
of i32.const / i64.const. Mixing the two up is the classic way to produce a
module that validates on one engine and traps on another, so the two encoders
are kept firmly separate below and never fall back to each other.

Self-host note: as elsewhere in the compiler, this sticks to plain loops and
instance attributes so the restricted-Python translator can lower it.
"""


# ---------------------------------------------------------------- value types

I32 = 0x7F
I64 = 0x7E
F32 = 0x7D
F64 = 0x7C

# Result-type shorthand used by block/loop/if: 0x40 means "produces nothing".
BLOCK_VOID = 0x40

# Section IDs. Only TYPE/FUNCTION/EXPORT/CODE are emitted today; the rest are
# named so the ascending-order rule stays obvious when they are filled in.
SEC_CUSTOM = 0
SEC_TYPE = 1
SEC_IMPORT = 2
SEC_FUNCTION = 3
SEC_TABLE = 4
SEC_MEMORY = 5
SEC_GLOBAL = 6
SEC_EXPORT = 7
SEC_START = 8
SEC_ELEMENT = 9
SEC_CODE = 10
SEC_DATA = 11
# Bulk memory adds a DataCount section, which states how many data segments
# follow so that a streaming validator can check memory.init without having
# read the data section yet. The encoder here never needs it (it emits no
# memory.init), but the decoder must know the id to read other producers'
# modules.
SEC_DATACOUNT = 12

# ------------------------------------------------------------------- opcodes

OP_UNREACHABLE = 0x00
OP_NOP = 0x01
OP_BLOCK = 0x02
OP_LOOP = 0x03
OP_IF = 0x04
OP_ELSE = 0x05
OP_END = 0x0B
OP_BR = 0x0C
OP_BR_IF = 0x0D
OP_BR_TABLE = 0x0E
OP_RETURN = 0x0F
OP_CALL = 0x10
OP_CALL_INDIRECT = 0x11

OP_DROP = 0x1A
OP_SELECT = 0x1B

OP_LOCAL_GET = 0x20
OP_LOCAL_SET = 0x21
OP_LOCAL_TEE = 0x22
OP_GLOBAL_GET = 0x23
OP_GLOBAL_SET = 0x24

# Memory access. Each takes an (align, offset) immediate pair after the opcode;
# `align` is a hint expressed as a power of two and must not exceed the natural
# alignment of the access, or validation fails.
OP_I32_LOAD = 0x28
OP_I64_LOAD = 0x29
OP_I32_LOAD8_S = 0x2C
OP_I32_LOAD8_U = 0x2D
OP_I32_LOAD16_S = 0x2E
OP_I32_LOAD16_U = 0x2F
OP_I64_LOAD8_S = 0x30
OP_I64_LOAD8_U = 0x31
OP_I64_LOAD16_S = 0x32
OP_I64_LOAD16_U = 0x33
OP_I64_LOAD32_S = 0x34
OP_I64_LOAD32_U = 0x35
OP_I32_STORE = 0x36
OP_I64_STORE = 0x37
OP_I32_STORE8 = 0x3A
OP_I32_STORE16 = 0x3B
OP_I64_STORE8 = 0x3C
OP_I64_STORE16 = 0x3D
OP_I64_STORE32 = 0x3E

OP_F32_LOAD = 0x2A
OP_F64_LOAD = 0x2B
OP_F32_STORE = 0x38
OP_F64_STORE = 0x39

OP_I32_CONST = 0x41
OP_I64_CONST = 0x42
OP_F32_CONST = 0x43
OP_F64_CONST = 0x44

OP_I32_EQZ = 0x45
OP_I64_EQZ = 0x50

# Comparison and arithmetic opcodes are contiguous per type, but writing them
# out beats arithmetic on a base opcode: a typo in a table is visible, a typo
# in an offset calculation is not.
I32_CMP = {
    "eq": 0x46, "ne": 0x47,
    "lt_s": 0x48, "lt_u": 0x49, "gt_s": 0x4A, "gt_u": 0x4B,
    "le_s": 0x4C, "le_u": 0x4D, "ge_s": 0x4E, "ge_u": 0x4F,
}

I64_CMP = {
    "eq": 0x51, "ne": 0x52,
    "lt_s": 0x53, "lt_u": 0x54, "gt_s": 0x55, "gt_u": 0x56,
    "le_s": 0x57, "le_u": 0x58, "ge_s": 0x59, "ge_u": 0x5A,
}

I32_BIN = {
    "add": 0x6A, "sub": 0x6B, "mul": 0x6C,
    "div_s": 0x6D, "div_u": 0x6E, "rem_s": 0x6F, "rem_u": 0x70,
    "and": 0x71, "or": 0x72, "xor": 0x73,
    "shl": 0x74, "shr_s": 0x75, "shr_u": 0x76,
}

I64_BIN = {
    "add": 0x7C, "sub": 0x7D, "mul": 0x7E,
    "div_s": 0x7F, "div_u": 0x80, "rem_s": 0x81, "rem_u": 0x82,
    "and": 0x83, "or": 0x84, "xor": 0x85,
    "shl": 0x86, "shr_s": 0x87, "shr_u": 0x88,
}

# Floating point. wasm's comparisons are the *ordered* ones for <, <=, > and
# >= (NaN yields 0) and `ne` is the negation of `eq` (NaN yields 1), which is
# exactly what C requires -- so these map across with no fixups.
F32_CMP = {
    "eq": 0x5B, "ne": 0x5C,
    "lt": 0x5D, "gt": 0x5E, "le": 0x5F, "ge": 0x60,
}

F64_CMP = {
    "eq": 0x61, "ne": 0x62,
    "lt": 0x63, "gt": 0x64, "le": 0x65, "ge": 0x66,
}

F32_BIN = {"add": 0x92, "sub": 0x93, "mul": 0x94, "div": 0x95}
F64_BIN = {"add": 0xA0, "sub": 0xA1, "mul": 0xA2, "div": 0xA3}

OP_F32_NEG = 0x8C
OP_F64_NEG = 0x9A
OP_F32_ABS = 0x8B
OP_F64_ABS = 0x99
OP_F32_SQRT = 0x91
OP_F64_SQRT = 0x9F

# Width conversions between the two float types.
OP_F32_DEMOTE_F64 = 0xB6
OP_F64_PROMOTE_F32 = 0xBB

# Integer -> float. Exact, and named by the *source* signedness.
F32_CONVERT = {("i32", True): 0xB2, ("i32", False): 0xB3,
               ("i64", True): 0xB4, ("i64", False): 0xB5}
F64_CONVERT = {("i32", True): 0xB7, ("i32", False): 0xB8,
               ("i64", True): 0xB9, ("i64", False): 0xBA}

# Float -> integer. The plain `trunc` family TRAPS on NaN or an out-of-range
# value; the saturating family (a 0xFC-prefixed opcode) clamps instead. C
# leaves an out-of-range conversion undefined, and a trap is the worst
# available reading of "undefined" -- it takes down the whole module rather
# than producing a junk number the program might well ignore. So the
# saturating forms are used throughout.
TRUNC_SAT_PREFIX = 0xFC
TRUNC_SAT = {("i32", "f32", True): 0, ("i32", "f32", False): 1,
             ("i32", "f64", True): 2, ("i32", "f64", False): 3,
             ("i64", "f32", True): 4, ("i64", "f32", False): 5,
             ("i64", "f64", True): 6, ("i64", "f64", False): 7}

OP_I32_WRAP_I64 = 0xA7
OP_I64_EXTEND_I32_S = 0xAC
OP_I64_EXTEND_I32_U = 0xAD

# Sign-extension operators (the sign_extension feature, universally available
# in the engines we target). These give an exact C narrowing conversion --
# `(char)x` -- in one instruction instead of a shl/shr_s pair.
OP_I32_EXTEND8_S = 0xC0
OP_I32_EXTEND16_S = 0xC1
OP_I64_EXTEND8_S = 0xC2
OP_I64_EXTEND16_S = 0xC3
OP_I64_EXTEND32_S = 0xC4

EXTERNAL_KIND_FUNC = 0x00
EXTERNAL_KIND_TABLE = 0x01
EXTERNAL_KIND_MEMORY = 0x02
EXTERNAL_KIND_GLOBAL = 0x03

# The only element type the MVP table can hold: a function reference.
FUNCREF = 0x70

# Bulk-memory operations share the 0xFC prefix with the saturating
# conversions and are distinguished by the index that follows.
BULK_PREFIX = 0xFC
BULK_MEMORY_COPY = 10
BULK_MEMORY_FILL = 11


# ------------------------------------------------------------------- LEB128

def uleb(n):
    """Unsigned LEB128. Used for every index, count and byte-length."""
    if n < 0:
        raise ValueError("uleb128 of negative value %d" % n)
    out = []
    while True:
        byte = n & 0x7F
        n = n >> 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return out


def sleb(n):
    """Signed LEB128, for i32.const / i64.const operands.

    The terminating byte is the one whose sign bit already agrees with the
    value's sign, which is why this cannot be written as uleb on a two's
    complement bit pattern.
    """
    out = []
    while True:
        byte = n & 0x7F
        n = n >> 7                       # Python's >> is arithmetic: sign-safe
        done = (n == 0 and (byte & 0x40) == 0) or \
               (n == -1 and (byte & 0x40) != 0)
        if done:
            out.append(byte)
            return out
        out.append(byte | 0x80)


def _name_bytes(s):
    """A wasm `name`: byte length followed by UTF-8 bytes."""
    raw = []
    for ch in s.encode("utf-8"):
        raw.append(ch)
    return uleb(len(raw)) + raw


def _vec(items):
    """A wasm vector: element count followed by the already-encoded elements."""
    return uleb(len(items)) + items


def _section(sec_id, payload):
    """One section: id, byte length of the payload, then the payload."""
    if not payload:
        return []
    return [sec_id] + uleb(len(payload)) + payload


# --------------------------------------------------------------- code buffer

class FuncBody:
    """An instruction buffer for one function.

    Holds the extra locals (beyond the parameters, which are locals too but are
    declared by the signature) and the raw instruction bytes. The emit helpers
    are deliberately thin -- this is a byte assembler, and instruction
    *selection* belongs to asm_gen.
    """

    def __init__(self):
        self.local_types = []            # types of locals after the params
        self.code = []

    def add_local(self, valtype):
        """Declare one more local and return its index *relative* to the end of
        the parameter list. The caller adds the parameter count."""
        self.local_types.append(valtype)
        return len(self.local_types) - 1

    def op(self, opcode):
        self.code.append(opcode)

    def op_u(self, opcode, operand):
        """Opcode with one unsigned immediate (local index, call target, ...)."""
        self.code.append(opcode)
        self.code.extend(uleb(operand))

    def const_i32(self, val):
        # Wrap into range first: a C literal may legitimately be written as a
        # value that only fits once truncated to the declared type, and
        # i32.const's operand is a signed 32-bit LEB.
        v = val & 0xFFFFFFFF
        if v >= 0x80000000:
            v -= 0x100000000
        self.code.append(OP_I32_CONST)
        self.code.extend(sleb(v))

    def const_i64(self, val):
        v = val & 0xFFFFFFFFFFFFFFFF
        if v >= 0x8000000000000000:
            v -= 0x10000000000000000
        self.code.append(OP_I64_CONST)
        self.code.extend(sleb(v))

    def const_f32(self, val):
        """f32.const takes four raw IEEE-754 bytes, little-endian -- not a
        LEB. Python's float is a double, so this rounds to single first."""
        import struct
        self.code.append(OP_F32_CONST)
        for byte in struct.pack("<f", float(val)):
            self.code.append(byte)

    def const_f64(self, val):
        """f64.const: eight raw IEEE-754 bytes, little-endian."""
        import struct
        self.code.append(OP_F64_CONST)
        for byte in struct.pack("<d", float(val)):
            self.code.append(byte)

    def memory_copy(self):
        """memory.copy: (dest, src, len) are on the stack, all i32.

        Overlap-safe by specification, which matters because a struct
        assignment from an overlapping source is legal C.
        """
        self.code.append(BULK_PREFIX)
        self.code.extend(uleb(BULK_MEMORY_COPY))
        self.code.append(0x00)           # destination memory index
        self.code.append(0x00)           # source memory index

    def call_indirect(self, type_idx):
        # Note: the module must also have set needs_table; the caller does
        # that, since FuncBody has no reference back to the module.
        """call_indirect: the table index is on top of the stack, above the
        arguments. The signature is checked against `type_idx` at run time --
        a mismatch traps rather than corrupting the stack."""
        self.code.append(OP_CALL_INDIRECT)
        self.code.extend(uleb(type_idx))
        self.code.append(0x00)           # table index

    def trunc_sat(self, to_type, from_type, signed):
        """A saturating float-to-integer conversion (0xFC-prefixed)."""
        self.code.append(TRUNC_SAT_PREFIX)
        self.code.extend(uleb(TRUNC_SAT[(to_type, from_type, signed)]))

    def const(self, valtype, val):
        if valtype == I64:
            self.const_i64(val)
        elif valtype == F32:
            self.const_f32(val)
        elif valtype == F64:
            self.const_f64(val)
        else:
            self.const_i32(val)

    def local_get(self, idx):
        self.op_u(OP_LOCAL_GET, idx)

    def local_set(self, idx):
        self.op_u(OP_LOCAL_SET, idx)

    def call(self, func_idx):
        self.op_u(OP_CALL, func_idx)

    def global_get(self, idx):
        self.op_u(OP_GLOBAL_GET, idx)

    def global_set(self, idx):
        self.op_u(OP_GLOBAL_SET, idx)

    def mem(self, opcode, align, offset):
        """A load or store. The address is already on the stack; `offset` is
        folded into the instruction rather than added separately, which is
        both smaller and what engines expect to see.

        `align` is the log2 of the assumed alignment. Claiming more alignment
        than the access really has is a validation error, and claiming less is
        merely a missed optimisation -- so callers pass 0 unless they know.
        """
        self.code.append(opcode)
        self.code.extend(uleb(align))
        self.code.extend(uleb(offset))

    def br(self, depth):
        self.op_u(OP_BR, depth)

    def br_if(self, depth):
        self.op_u(OP_BR_IF, depth)

    def br_table(self, depths, default_depth):
        """br_table: a vector of branch depths plus a mandatory default.

        The value on the stack indexes the vector; anything out of range takes
        the default. That out-of-range fallback is what makes this a total
        dispatch, so the generated block-index switch needs no bounds check.
        """
        self.code.append(OP_BR_TABLE)
        self.code.extend(uleb(len(depths)))
        for d in depths:
            self.code.extend(uleb(d))
        self.code.extend(uleb(default_depth))

    def block(self, blocktype=BLOCK_VOID):
        self.code.append(OP_BLOCK)
        self.code.append(blocktype)

    def loop(self, blocktype=BLOCK_VOID):
        self.code.append(OP_LOOP)
        self.code.append(blocktype)

    def end(self):
        self.code.append(OP_END)

    def encode(self):
        """The `code` section entry for this function: body size, then the
        locals declaration, then the instructions and a closing `end`."""
        # Locals are declared run-length encoded: (count, type) pairs. Runs of
        # the same type are extremely common here (every value of a given width
        # gets a local), so this is worth doing rather than emitting one
        # singleton run per local.
        runs = []
        i = 0
        n = len(self.local_types)
        while i < n:
            t = self.local_types[i]
            j = i
            while j < n and self.local_types[j] == t:
                j += 1
            runs.append((j - i, t))
            i = j

        decl = uleb(len(runs))
        for count, t in runs:
            decl = decl + uleb(count) + [t]

        body = decl + self.code + [OP_END]
        return uleb(len(body)) + body


# -------------------------------------------------------------- module build

class WasmModule:
    """Accumulates signatures, functions and exports, then encodes a module.

    Function indices are assigned in definition order. Once imports arrive they
    will occupy the low indices and shift the defined functions up; `func_index`
    is the single place that would need to know, which is why callers go through
    it instead of doing the arithmetic themselves.
    """

    def __init__(self):
        self.types = []                  # list of (params tuple, results tuple)
        self.type_keys = {}              # signature -> type index (dedup)
        self.imports = []                # (module, field, type index)
        self.import_names = []           # parallel: local name of each import
        self.func_names = []             # defined functions, in index order
        self.func_types = []             # parallel: signature index each
        self.func_index_of = {}          # name -> function index
        self.bodies = {}                 # name -> FuncBody
        self.exports = []                # list of (name, function index)
        self.export_memory = False
        # Linear memory: None until requested, then a minimum size in 64 KiB
        # pages. Only one memory may exist in the MVP.
        self.memory_pages = None
        # Mutable i32 globals, as (initial value) in index order. Index 0 is
        # the shadow stack pointer; nothing else uses globals yet.
        self.globals = []
        # Data segments: (byte offset, list of byte values).
        self.data_segments = []
        # Function table, for indirect calls. Entry 0 is left empty so that a
        # null function pointer traps when called instead of dispatching to
        # whatever landed at index 0.
        self.table_entries = []
        # Set when a call_indirect is emitted. A module can contain one
        # without ever taking a function's address -- `int (*f)(int) = 0;
        # f(1);` -- and the instruction names table 0, so the table has to
        # exist even when nothing was ever put in it.
        self.needs_table = False

    def set_memory(self, pages):
        self.memory_pages = pages

    def add_global_i32(self, initial):
        self.globals.append(initial)
        return len(self.globals) - 1

    def add_data(self, offset, data_bytes):
        if data_bytes:
            self.data_segments.append((offset, data_bytes))

    def table_index(self, name):
        """Index of `name` in the function table, adding it on first use.

        Index 0 is reserved for the null pointer, so the first real entry is
        1 -- which is what makes a call through a null pointer trap.
        """
        for i in range(len(self.table_entries)):
            if self.table_entries[i] == name:
                return i + 1
        self.table_entries.append(name)
        return len(self.table_entries)

    def type_index(self, params, results):
        """Intern a signature and return its index in the type section."""
        key = (tuple(params), tuple(results))
        if key in self.type_keys:
            return self.type_keys[key]
        idx = len(self.types)
        self.type_keys[key] = idx
        self.types.append(key)
        return idx

    def declare_import(self, name, module, field, params, results):
        """Import a function, and reserve its index.

        Imported functions occupy the *low* end of the function index space,
        ahead of every defined function -- so all imports must be declared
        before the first declare_func, or the defined functions' indices shift
        out from under any call already emitted. declare_func enforces that.
        """
        if name in self.func_index_of:
            return self.func_index_of[name]
        if self.func_names:
            raise ValueError(
                "wasm: import '%s' declared after a defined function; "
                "imports occupy the low indices and must come first" % name)
        idx = len(self.imports)
        self.func_index_of[name] = idx
        self.imports.append((module, field, self.type_index(params, results)))
        self.import_names.append(name)
        return idx

    def declare_func(self, name, params, results):
        """Reserve an index for `name`. Calls can then be emitted before the
        body exists, which is what makes forward references and mutual
        recursion work without a second pass."""
        if name in self.func_index_of:
            return self.func_index_of[name]
        idx = len(self.imports) + len(self.func_names)
        self.func_index_of[name] = idx
        self.func_names.append(name)
        self.func_types.append(self.type_index(params, results))
        return idx

    def func_index(self, name):
        return self.func_index_of.get(name, -1)

    def set_body(self, name, body):
        self.bodies[name] = body

    def export_func(self, name, export_as=None):
        idx = self.func_index(name)
        if idx < 0:
            return
        self.exports.append((export_as if export_as else name, idx))

    def encode(self):
        """Encode the whole module to a list of byte values."""
        out = [0x00, 0x61, 0x73, 0x6D,   # "\0asm"
               0x01, 0x00, 0x00, 0x00]   # version 1

        # --- type section
        payload = []
        for params, results in self.types:
            entry = [0x60]               # func type tag
            entry = entry + uleb(len(params))
            for p in params:
                entry.append(p)
            entry = entry + uleb(len(results))
            for r in results:
                entry.append(r)
            payload = payload + entry
        out = out + _section(SEC_TYPE, _vec_n(payload, len(self.types)))

        # --- import section
        payload = []
        for module, field, tidx in self.imports:
            payload = payload + _name_bytes(module) + _name_bytes(field) \
                + [EXTERNAL_KIND_FUNC] + uleb(tidx)
        out = out + _section(SEC_IMPORT, _vec_n(payload, len(self.imports)))

        # --- function section
        payload = []
        for t in self.func_types:
            payload = payload + uleb(t)
        out = out + _section(SEC_FUNCTION,
                             _vec_n(payload, len(self.func_types)))

        # --- table section
        if self.table_entries or self.needs_table:
            size = len(self.table_entries) + 1     # + the reserved null slot
            payload = [FUNCREF, 0x00] + uleb(size)
            out = out + _section(SEC_TABLE, _vec_n(payload, 1))

        # --- memory section
        if self.memory_pages is not None:
            # limits: flag 0 = a minimum only, no maximum, so the engine may
            # grow it if memory.grow is ever used.
            payload = [0x00] + uleb(self.memory_pages)
            out = out + _section(SEC_MEMORY, _vec_n(payload, 1))

        # --- global section
        payload = []
        for initial in self.globals:
            # type i32, mutable, initialised by a constant expression
            payload = payload + [I32, 0x01, OP_I32_CONST] + sleb(initial) \
                + [OP_END]
        out = out + _section(SEC_GLOBAL, _vec_n(payload, len(self.globals)))

        # --- export section
        payload = []
        count = 0
        for name, idx in self.exports:
            payload = payload + _name_bytes(name) + \
                [EXTERNAL_KIND_FUNC] + uleb(idx)
            count += 1
        if self.export_memory:
            # A WASI host writes into and reads out of the module's memory --
            # an iovec handed to fd_write is a pointer into it -- so the
            # memory must be exported for anything to work.
            payload = payload + _name_bytes("memory") + [0x02] + uleb(0)
            count += 1
        out = out + _section(SEC_EXPORT, _vec_n(payload, count))

        # --- element section: fill the table, starting at index 1
        if self.table_entries:
            payload = [0x00, OP_I32_CONST] + sleb(1) + [OP_END]
            payload = payload + uleb(len(self.table_entries))
            for name in self.table_entries:
                payload = payload + uleb(self.func_index_of[name])
            out = out + _section(SEC_ELEMENT, _vec_n(payload, 1))

        # --- code section
        payload = []
        count = 0
        for name in self.func_names:      # imports have no body, and are not here
            body = self.bodies.get(name)
            if body is None:
                # A declared-but-undefined function would leave a hole in the
                # code section and desynchronise every later index. Callers
                # must refuse such a program before getting here.
                raise ValueError("wasm: no body emitted for function '%s'"
                                 % name)
            payload = payload + body.encode()
            count += 1
        out = out + _section(SEC_CODE, _vec_n(payload, count))

        # --- data section (after code, per the section ordering)
        payload = []
        for offset, data_bytes in self.data_segments:
            # memory index 0, then the offset as a constant expression.
            payload = payload + [0x00, OP_I32_CONST] + sleb(offset) \
                + [OP_END] + uleb(len(data_bytes)) + list(data_bytes)
        out = out + _section(SEC_DATA,
                             _vec_n(payload, len(self.data_segments)))

        return out


def _vec_n(payload, count):
    """A vector whose elements are already concatenated into `payload`.

    `_vec` cannot be used for these because the element boundaries are gone by
    the time the payload is built, so the count is carried alongside.
    """
    if count == 0:
        return []
    return uleb(count) + payload


def module_bytes(mod):
    """Encode `mod` and return it as a `bytes` object ready to write to disk."""
    return bytes(bytearray(mod.encode()))
