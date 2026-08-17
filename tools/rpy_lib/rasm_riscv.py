"""rasm_riscv -- RV64 (riscv64) instruction encoder and parser.

The third encoder in the set, after rasm.py (x86-64) and rasm_arm64.py. Like
AArch64, RV64 is a fixed-width 32-bit-word ISA, so it produces the same
outputs -- a byte list plus rasm.Reloc entries -- and plugs into the same
`rasm_obj` driver through the `fixed_width` seam.

Two things make RV64 different from AArch64 in ways that matter here:

  * **Pseudo-instructions are load-bearing.** Almost everything the compiler
    emits (`li`, `mv`, `ret`, `j`, `call`, `beqz`, `seqz`) is an alias the
    assembler expands. `li` in particular expands to *one or two*
    instructions depending on the constant, and `call` always expands to two,
    so `encode_line` returns a variable number of bytes rather than always
    four.

  * **Immediates are scattered across the word.** S-type and B-type split
    their immediate into two non-adjacent fields, and B/J-type additionally
    reorder the bits. Every one of those splits is a place to get a shift
    wrong in a way that still produces a legal instruction, which is why the
    differential test compares against `riscv64-linux-gnu-as` rather than
    checking anything by eye.

Scope is the RV64IMFD base plus the pseudo-instructions ShivyCX emits and a
freestanding runtime needs. Anything else raises rather than guessing.
"""
import rasm


class RiscvError(Exception):
    pass


# ---------------------------------------------------------------------------
# Registers
# ---------------------------------------------------------------------------
# Integer ABI names. x8 is both s0 and fp.
_XABI = {
    "zero": 0, "ra": 1, "sp": 2, "gp": 3, "tp": 4,
    "t0": 5, "t1": 6, "t2": 7,
    "s0": 8, "fp": 8, "s1": 9,
    "a0": 10, "a1": 11, "a2": 12, "a3": 13,
    "a4": 14, "a5": 15, "a6": 16, "a7": 17,
    "s2": 18, "s3": 19, "s4": 20, "s5": 21, "s6": 22, "s7": 23,
    "s8": 24, "s9": 25, "s10": 26, "s11": 27,
    "t3": 28, "t4": 29, "t5": 30, "t6": 31,
}

# Floating-point ABI names.
_FABI = {
    "ft0": 0, "ft1": 1, "ft2": 2, "ft3": 3,
    "ft4": 4, "ft5": 5, "ft6": 6, "ft7": 7,
    "fs0": 8, "fs1": 9,
    "fa0": 10, "fa1": 11, "fa2": 12, "fa3": 13,
    "fa4": 14, "fa5": 15, "fa6": 16, "fa7": 17,
    "fs2": 18, "fs3": 19, "fs4": 20, "fs5": 21, "fs6": 22, "fs7": 23,
    "fs8": 24, "fs9": 25, "fs10": 26, "fs11": 27,
    "ft8": 28, "ft9": 29, "ft10": 30, "ft11": 31,
}


class Op(object):
    """One uniform operand record.

    kind: "reg" (num, is_fp) | "imm" (val) | "mem" (base, off, sym, symkind)
          | "sym" (sym, symkind)
    """

    def __init__(self, kind):
        self.kind = kind
        self.num = 0
        self.is_fp = False
        self.val = 0
        self.base = 0
        self.off = 0
        self.sym = ""
        self.symkind = ""       # "", "hi", "lo", "pcrel_hi", "pcrel_lo"
        # Original operand text. A label may legitimately be spelled like a
        # register -- a function called `f2` or `a0` is ordinary C -- so an
        # operand that parsed as a register has to be recoverable as a symbol
        # when the instruction wanted a label.
        self.text = ""


def _op_reg(num, is_fp):
    o = Op("reg")
    o.num = num
    o.is_fp = is_fp
    return o


def _op_imm(v):
    o = Op("imm")
    o.val = v
    return o


def _all_digits(s):
    i = 0
    while i < len(s):
        if s[i] < "0" or s[i] > "9":
            return False
        i += 1
    return len(s) > 0


def parse_reg(tok):
    """Parse a register name, or return None."""
    t = tok.strip().lower()
    if t == "":
        return None
    if t in _XABI:
        return _op_reg(_XABI[t], False)
    if t in _FABI:
        return _op_reg(_FABI[t], True)
    if t[0] == "x" and _all_digits(t[1:]):
        n = int(t[1:])
        if n <= 31:
            return _op_reg(n, False)
        return None
    if t[0] == "f" and _all_digits(t[1:]):
        n = int(t[1:])
        if n <= 31:
            return _op_reg(n, True)
        return None
    return None


def _parse_int(tok):
    t = tok.strip()
    neg = False
    if t[0:1] == "-":
        neg = True
        t = t[1:]
    elif t[0:1] == "+":
        t = t[1:]
    if t[0:2] == "0x" or t[0:2] == "0X":
        v = int(t[2:], 16)
    elif t[0:2] == "0b" or t[0:2] == "0B":
        v = int(t[2:], 2)
    else:
        v = int(t, 10)
    return -v if neg else v


def _looks_int(tok):
    t = tok.strip()
    if t[0:1] == "-" or t[0:1] == "+":
        t = t[1:]
    if t[0:2] == "0x" or t[0:2] == "0X":
        t = t[2:]
        i = 0
        while i < len(t):
            c = t[i]
            if not (("0" <= c <= "9") or ("a" <= c <= "f")
                    or ("A" <= c <= "F")):
                return False
            i += 1
        return len(t) > 0
    return _all_digits(t)


def _split_top(text, sep):
    """Split on `sep`, ignoring separators inside ( ) parentheses."""
    parts = []
    depth = 0
    cur = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur))
    return parts


_RELMODS = {"%hi": "hi", "%lo": "lo",
            "%pcrel_hi": "pcrel_hi", "%pcrel_lo": "pcrel_lo"}


def _parse_relmod(t):
    """Parse `%hi(sym)` / `%pcrel_lo(label)` and friends, else return None.

    The closing parenthesis must be the one matching our own opening paren
    *and* the end of the token. `%lo(sym)(a1)` is a memory operand, not a
    bare modifier -- taking the last `)` would silently parse the symbol as
    `sym)(a1`."""
    if t[0:1] != "%":
        return None
    lp = t.find("(")
    if lp < 0:
        raise RiscvError("malformed relocation modifier '%s'" % t)
    depth = 0
    i = lp
    close = -1
    while i < len(t):
        if t[i] == "(":
            depth += 1
        elif t[i] == ")":
            depth -= 1
            if depth == 0:
                close = i
                break
        i += 1
    if close < 0:
        raise RiscvError("unterminated relocation modifier '%s'" % t)
    if close != len(t) - 1:
        return None                 # trailing text: `%lo(sym)(base)`
    mod = t[:lp]
    kind = _RELMODS.get(mod)
    if kind is None:
        raise RiscvError("unsupported relocation modifier '%s'" % mod)
    o = Op("sym")
    o.sym = t[lp + 1:close].strip()
    o.symkind = kind
    return o


def parse_operand(text):
    t = text.strip()
    if t == "":
        raise RiscvError("empty operand")
    r = parse_reg(t)
    if r is not None:
        r.text = t
        return r
    m = _parse_relmod(t)
    if m is not None:
        return m
    # `offset(base)` memory form, including `%lo(sym)(base)`.
    lp = t.find("(")
    if lp >= 0 and t[-1:] == ")" and t[0:1] != "%":
        o = Op("mem")
        inner = t[lp + 1:-1].strip()
        b = parse_reg(inner)
        if b is None:
            raise RiscvError("memory operand needs a base register: '%s'" % t)
        o.base = b.num
        head = t[:lp].strip()
        if head == "":
            o.off = 0
        elif _looks_int(head):
            o.off = _parse_int(head)
        else:
            hm = _parse_relmod(head)
            if hm is None:
                raise RiscvError("bad memory offset '%s'" % head)
            o.sym = hm.sym
            o.symkind = hm.symkind
        return o
    if lp >= 0 and t[0:1] == "%":
        # `%lo(sym)(base)` -- two parenthesised groups. _parse_relmod
        # declined above precisely because of the trailing group.
        depth = 0
        i = lp
        close = -1
        while i < len(t):
            if t[i] == "(":
                depth += 1
            elif t[i] == ")":
                depth -= 1
                if depth == 0:
                    close = i
                    break
            i += 1
        head = t[:close + 1]
        tail = t[close + 1:].strip()
        hm = _parse_relmod(head)
        if hm is not None and tail[0:1] == "(" and tail[-1:] == ")":
            b = parse_reg(tail[1:-1].strip())
            if b is None:
                raise RiscvError("bad base register in '%s'" % t)
            o = Op("mem")
            o.base = b.num
            o.sym = hm.sym
            o.symkind = hm.symkind
            return o
    if _looks_int(t):
        return _op_imm(_parse_int(t))
    o = Op("sym")
    o.sym = t
    o.symkind = ""
    return o


def split_operands(rest):
    if rest.strip() == "":
        return []
    out = []
    for p in _split_top(rest, ","):
        s = p.strip()
        if s != "":
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Instruction formats
# ---------------------------------------------------------------------------
OP_OP = 0x33
OP_OP32 = 0x3B
OP_IMM = 0x13
OP_IMM32 = 0x1B
OP_LOAD = 0x03
OP_STORE = 0x23
OP_BRANCH = 0x63
OP_JALR = 0x67
OP_JAL = 0x6F
OP_LUI = 0x37
OP_AUIPC = 0x17
OP_SYSTEM = 0x73
OP_LOADFP = 0x07
OP_STOREFP = 0x27
OP_FP = 0x53


def _r_type(opcode, funct3, funct7, rd, rs1, rs2):
    return ((funct7 & 0x7F) << 25) | ((rs2 & 0x1F) << 20) \
        | ((rs1 & 0x1F) << 15) | ((funct3 & 7) << 12) \
        | ((rd & 0x1F) << 7) | (opcode & 0x7F)


def _i_type(opcode, funct3, rd, rs1, imm):
    return ((imm & 0xFFF) << 20) | ((rs1 & 0x1F) << 15) \
        | ((funct3 & 7) << 12) | ((rd & 0x1F) << 7) | (opcode & 0x7F)


def _s_type(opcode, funct3, rs1, rs2, imm):
    # The immediate is split: bits 11:5 at 31:25, bits 4:0 at 11:7.
    return (((imm >> 5) & 0x7F) << 25) | ((rs2 & 0x1F) << 20) \
        | ((rs1 & 0x1F) << 15) | ((funct3 & 7) << 12) \
        | ((imm & 0x1F) << 7) | (opcode & 0x7F)


def _b_type(opcode, funct3, rs1, rs2, imm):
    # Split *and* reordered: [12] at 31, [10:5] at 30:25, [4:1] at 11:8,
    # [11] at 7. Bit 0 is always zero and is not stored.
    return (((imm >> 12) & 1) << 31) | (((imm >> 5) & 0x3F) << 25) \
        | ((rs2 & 0x1F) << 20) | ((rs1 & 0x1F) << 15) \
        | ((funct3 & 7) << 12) | (((imm >> 1) & 0xF) << 8) \
        | (((imm >> 11) & 1) << 7) | (opcode & 0x7F)


def _u_type(opcode, rd, imm20):
    return ((imm20 & 0xFFFFF) << 12) | ((rd & 0x1F) << 7) | (opcode & 0x7F)


def _j_type(opcode, rd, imm):
    # [20] at 31, [10:1] at 30:21, [11] at 20, [19:12] at 19:12.
    return (((imm >> 20) & 1) << 31) | (((imm >> 1) & 0x3FF) << 21) \
        | (((imm >> 11) & 1) << 20) | (((imm >> 12) & 0xFF) << 12) \
        | ((rd & 0x1F) << 7) | (opcode & 0x7F)


def _bytes_of(word):
    w = word & 0xFFFFFFFF
    return [w & 0xFF, (w >> 8) & 0xFF, (w >> 16) & 0xFF, (w >> 24) & 0xFF]


def _check_imm12(v, mnem):
    if v < -2048 or v > 2047:
        raise RiscvError("%s: immediate %d does not fit in 12 signed bits"
                         % (mnem, v))


# ---------------------------------------------------------------------------
# Instruction tables
# ---------------------------------------------------------------------------
# name -> (funct3, funct7)
_RTYPE = {
    "add": (0, 0x00), "sub": (0, 0x20), "sll": (1, 0x00),
    "slt": (2, 0x00), "sltu": (3, 0x00), "xor": (4, 0x00),
    "srl": (5, 0x00), "sra": (5, 0x20), "or": (6, 0x00), "and": (7, 0x00),
    "mul": (0, 0x01), "mulh": (1, 0x01), "mulhsu": (2, 0x01),
    "mulhu": (3, 0x01), "div": (4, 0x01), "divu": (5, 0x01),
    "rem": (6, 0x01), "remu": (7, 0x01),
}
_RTYPE32 = {
    "addw": (0, 0x00), "subw": (0, 0x20), "sllw": (1, 0x00),
    "srlw": (5, 0x00), "sraw": (5, 0x20),
    "mulw": (0, 0x01), "divw": (4, 0x01), "divuw": (5, 0x01),
    "remw": (6, 0x01), "remuw": (7, 0x01),
}
# name -> funct3
_ITYPE = {"addi": 0, "slti": 2, "sltiu": 3, "xori": 4, "ori": 6, "andi": 7}
_ITYPE32 = {"addiw": 0}
# shift-immediate: (funct3, funct7-ish top bits)
_SHIFTI = {"slli": (1, 0x00), "srli": (5, 0x00), "srai": (5, 0x10)}
_SHIFTI32 = {"slliw": (1, 0x00), "srliw": (5, 0x00), "sraiw": (5, 0x20)}
# loads: funct3
_LOAD = {"lb": 0, "lh": 1, "lw": 2, "ld": 3, "lbu": 4, "lhu": 5, "lwu": 6}
_STORE = {"sb": 0, "sh": 1, "sw": 2, "sd": 3}
_LOADFP = {"flw": 2, "fld": 3}
_STOREFP = {"fsw": 2, "fsd": 3}
_BRANCH = {"beq": 0, "bne": 1, "blt": 4, "bge": 5, "bltu": 6, "bgeu": 7}


def _need_reg(ops, i, mnem):
    if i >= len(ops) or ops[i].kind != "reg":
        raise RiscvError("%s: operand %d must be a register" % (mnem, i + 1))
    return ops[i]


def _need_imm(ops, i, mnem):
    if i >= len(ops) or ops[i].kind != "imm":
        raise RiscvError("%s: operand %d must be an immediate" % (mnem, i + 1))
    return ops[i].val


def _sym_of(ops, i, mnem):
    if i >= len(ops):
        raise RiscvError("%s: operand %d must be a label" % (mnem, i + 1))
    o = ops[i]
    if o.kind == "sym":
        return o
    if o.kind == "reg" and o.text != "":
        # It parsed as a register only because the symbol happens to be
        # spelled like one (`call f2`). In a label position it is a symbol.
        r = Op("sym")
        r.sym = o.text
        return r
    raise RiscvError("%s: operand %d must be a label" % (mnem, i + 1))


# ---------------------------------------------------------------------------
# hi/lo splitting
# ---------------------------------------------------------------------------
def split_hi_lo(value):
    """Split a 32-bit value into the (hi20, lo12) pair a lui/addi sequence
    needs. addi sign-extends its 12-bit immediate, so a low half of 0x800 or
    more must borrow 1 from the high half to compensate. Getting this wrong
    is off-by-4096 on exactly half of all addresses."""
    hi = (value + 0x800) >> 12
    lo = value - (hi << 12)
    return hi, lo


# ---------------------------------------------------------------------------
# Encoders
# ---------------------------------------------------------------------------
def _sext12(v):
    """Sign-extend the low 12 bits of `v`."""
    low = v & 0xFFF
    if low >= 0x800:
        low -= 0x1000
    return low


def _li32_into(rd, v, out):
    """Append the lui/addiw form of a signed 32-bit constant.

    Always the two-instruction shape, even when the value would fit in an
    addi: this is the form gas emits for the *base* of a multi-step
    sequence, and matching it keeps our output byte-identical."""
    hi, lo = split_hi_lo(v)
    if hi != 0:
        out.extend(_bytes_of(_u_type(OP_LUI, rd, hi)))
        if lo != 0:
            out.extend(_bytes_of(_i_type(OP_IMM32, 0, rd, rd, lo)))
    else:
        out.extend(_bytes_of(_i_type(OP_IMM32, 0, rd, 0, lo)))


def _enc_li(ops):
    """`li rd, imm` -- one instruction when the value fits in a signed 12-bit
    field, lui+addiw when it fits in 32 bits, and otherwise the recursive
    shift-and-add sequence gas builds.

    The 64-bit case works from the bottom up: peel off the sign-extended low
    12 bits, then shift the remainder right until its lowest set bit reaches
    bit 0 (skipping runs of zeros keeps the sequence short), and repeat on
    that. Replaying the steps in reverse -- load the reduced constant, then
    `slli` by each recorded shift and `addi` the matching low part -- rebuilds
    the original."""
    rd = _need_reg(ops, 0, "li")
    v = _need_imm(ops, 1, "li")
    if -2048 <= v <= 2047:
        return _bytes_of(_i_type(OP_IMM, 0, rd.num, 0, v)), []
    # Normalise to a signed 64-bit value.
    v = v & 0xFFFFFFFFFFFFFFFF
    if v >= 0x8000000000000000:
        v -= 0x10000000000000000
    if -2147483648 <= v <= 2147483647:
        out = []
        hi, lo = split_hi_lo(v)
        out.extend(_bytes_of(_u_type(OP_LUI, rd.num, hi)))
        if lo != 0:
            out.extend(_bytes_of(_i_type(OP_IMM32, 0, rd.num, rd.num, lo)))
        return out, []
    steps = []
    cur = v
    guard = 0
    while not (-2147483648 <= cur <= 2147483647):
        lower = _sext12(cur)
        upper = cur - lower
        # Wrap to signed 64 bits. The subtraction can overflow -- for INT64_MAX
        # the low part is -1, so `cur - lower` is 2**63, which in 64-bit
        # arithmetic is INT64_MIN. Python's integers do not wrap, so without
        # this the sequence comes out loading 1 where it should load -1.
        upper = upper & 0xFFFFFFFFFFFFFFFF
        if upper >= 0x8000000000000000:
            upper -= 0x10000000000000000
        shift = 12
        while ((upper >> shift) & 1) == 0 and shift < 64:
            shift += 1
        upper = upper >> shift
        steps.append((shift, lower))
        cur = upper
        guard += 1
        if guard > 8:
            raise RiscvError("li: constant %d did not reduce" % v)
    out = []
    _li32_into(rd.num, cur, out)
    i = len(steps) - 1
    while i >= 0:
        shift = steps[i][0]
        lower = steps[i][1]
        out.extend(_bytes_of(_i_type(OP_IMM, 1, rd.num, rd.num, shift)))
        if lower != 0:
            out.extend(_bytes_of(_i_type(OP_IMM, 0, rd.num, rd.num, lower)))
        i -= 1
    return out, []


def _enc_call(mnem, ops):
    """`call sym` / `tail sym` -- auipc plus jalr, covered by one R_RISCV_CALL
    relocation attached to the auipc, which patches both words."""
    s = _sym_of(ops, 0, mnem)
    if mnem == "call":
        link = 1                       # ra
        jalr_rd = 1
    else:
        link = 6                       # t1, per the psABI for tail calls
        jalr_rd = 0
    out = _bytes_of(_u_type(OP_AUIPC, link, 0))
    out.extend(_bytes_of(_i_type(OP_JALR, 0, jalr_rd, link, 0)))
    return out, [rasm.Reloc(0, s.sym, 8, True, 0, True, "call_plt")]


def _enc_la(mnem, ops):
    """`la`/`lla rd, sym` -- auipc plus addi. Unlike `call`, the psABI uses
    *two* relocations here and the low one names a label on the auipc, not
    the target, so it cannot be encoded without help from the driver, which
    owns symbol creation. rasm_obj rewrites these before we see them."""
    raise RiscvError("%s: must be expanded by the assembler driver "
                     "(needs a local label on the auipc)" % mnem)


def _u_with_sym(opcode, rd, symop, mnem):
    if symop.symkind == "hi":
        kind = "hi20"
    elif symop.symkind == "pcrel_hi":
        kind = "pcrel_hi20"
    elif symop.symkind == "":
        # `lui rd, sym` is not meaningful; require an explicit modifier.
        raise RiscvError("%s: a symbol needs %%hi() or %%pcrel_hi()" % mnem)
    else:
        raise RiscvError("%s: unsupported modifier for this instruction"
                         % mnem)
    return _u_type(opcode, rd, 0), [rasm.Reloc(0, symop.sym, 4, True, 0,
                                               True, kind)]


def _i_lo_kind(symkind, mnem):
    if symkind == "lo":
        return "lo12_i"
    if symkind == "pcrel_lo":
        return "pcrel_lo12_i"
    raise RiscvError("%s: expected %%lo() or %%pcrel_lo()" % mnem)


def _s_lo_kind(symkind, mnem):
    if symkind == "lo":
        return "lo12_s"
    if symkind == "pcrel_lo":
        return "pcrel_lo12_s"
    raise RiscvError("%s: expected %%lo() or %%pcrel_lo()" % mnem)


def _enc_load(mnem, ops, funct3, opcode, is_fp):
    rd = _need_reg(ops, 0, mnem)
    if len(ops) < 2 or ops[1].kind != "mem":
        raise RiscvError("%s: second operand must be offset(base)" % mnem)
    m = ops[1]
    if m.sym != "":
        kind = _i_lo_kind(m.symkind, mnem)
        return _bytes_of(_i_type(opcode, funct3, rd.num, m.base, 0)), \
            [rasm.Reloc(0, m.sym, 4, m.symkind == "pcrel_lo", 0, True, kind)]
    _check_imm12(m.off, mnem)
    return _bytes_of(_i_type(opcode, funct3, rd.num, m.base, m.off)), []


def _enc_store(mnem, ops, funct3, opcode, is_fp):
    rs2 = _need_reg(ops, 0, mnem)
    if len(ops) < 2 or ops[1].kind != "mem":
        raise RiscvError("%s: second operand must be offset(base)" % mnem)
    m = ops[1]
    if m.sym != "":
        kind = _s_lo_kind(m.symkind, mnem)
        return _bytes_of(_s_type(opcode, funct3, m.base, rs2.num, 0)), \
            [rasm.Reloc(0, m.sym, 4, m.symkind == "pcrel_lo", 0, True, kind)]
    _check_imm12(m.off, mnem)
    return _bytes_of(_s_type(opcode, funct3, m.base, rs2.num, m.off)), []


def _enc_branch(mnem, ops, funct3, swap):
    """Conditional branch. `bgt`/`ble`/`bgtu`/`bleu` are the same
    instructions as blt/bge/bltu/bgeu with the source registers swapped."""
    rs1 = _need_reg(ops, 0, mnem)
    rs2 = _need_reg(ops, 1, mnem)
    s = _sym_of(ops, 2, mnem)
    a = rs2.num if swap else rs1.num
    b = rs1.num if swap else rs2.num
    word = _b_type(OP_BRANCH, funct3, a, b, 0)
    return _bytes_of(word), [rasm.Reloc(0, s.sym, 4, True, 0, True, "branch")]


def _enc_bz(mnem, ops):
    """beqz/bnez/blez/bgez/bltz/bgtz -- a branch against the zero register."""
    rs = _need_reg(ops, 0, mnem)
    s = _sym_of(ops, 1, mnem)
    if mnem == "beqz":
        f3, a, b = 0, rs.num, 0
    elif mnem == "bnez":
        f3, a, b = 1, rs.num, 0
    elif mnem == "bltz":
        f3, a, b = 4, rs.num, 0
    elif mnem == "bgez":
        f3, a, b = 5, rs.num, 0
    elif mnem == "blez":
        f3, a, b = 5, 0, rs.num          # bge zero, rs
    else:                                 # bgtz
        f3, a, b = 4, 0, rs.num          # blt zero, rs
    word = _b_type(OP_BRANCH, f3, a, b, 0)
    return _bytes_of(word), [rasm.Reloc(0, s.sym, 4, True, 0, True, "branch")]


def _enc_jal(mnem, ops):
    if mnem == "j":
        rd = 0
        s = _sym_of(ops, 0, mnem)
    elif len(ops) == 1:
        rd = 1                            # `jal sym` implies ra
        s = _sym_of(ops, 0, mnem)
    else:
        rd = _need_reg(ops, 0, mnem).num
        s = _sym_of(ops, 1, mnem)
    return _bytes_of(_j_type(OP_JAL, rd, 0)), \
        [rasm.Reloc(0, s.sym, 4, True, 0, True, "jal")]


def _enc_jalr(mnem, ops):
    if mnem == "jr":
        rs = _need_reg(ops, 0, mnem)
        return _bytes_of(_i_type(OP_JALR, 0, 0, rs.num, 0)), []
    if len(ops) == 1:
        rs = _need_reg(ops, 0, mnem)
        return _bytes_of(_i_type(OP_JALR, 0, 1, rs.num, 0)), []
    rd = _need_reg(ops, 0, mnem)
    if ops[1].kind == "mem":
        m = ops[1]
        _check_imm12(m.off, mnem)
        return _bytes_of(_i_type(OP_JALR, 0, rd.num, m.base, m.off)), []
    rs = _need_reg(ops, 1, mnem)
    off = _need_imm(ops, 2, mnem) if len(ops) > 2 else 0
    _check_imm12(off, mnem)
    return _bytes_of(_i_type(OP_JALR, 0, rd.num, rs.num, off)), []


# Floating point. OP-FP: funct7 selects operation and precision (bit 25 is
# the format: 0 = single, 1 = double).
_FP_ARITH = {"fadd": 0x00, "fsub": 0x04, "fmul": 0x08, "fdiv": 0x0C,
             "fmin": 0x14, "fmax": 0x14, "fsqrt": 0x2C}
_FP_CMP = {"feq": 2, "flt": 1, "fle": 0}


def _fp_fmt(suffix, mnem):
    if suffix == "s":
        return 0
    if suffix == "d":
        return 1
    raise RiscvError("%s: unsupported floating-point format '.%s'"
                     % (mnem, suffix))


def _enc_fp(mnem, ops):
    """Floating-point instructions carry their format as a mnemonic suffix
    (`fadd.d`), and the conversions carry both source and destination
    (`fcvt.w.d`), so the dispatch is on the split name."""
    parts = mnem.split(".")
    base = parts[0]
    if base in _FP_ARITH and len(parts) == 2:
        fmt = _fp_fmt(parts[1], mnem)
        rd = _need_reg(ops, 0, mnem)
        rs1 = _need_reg(ops, 1, mnem)
        if base == "fsqrt":
            return _bytes_of(_r_type(OP_FP, 7, (0x2C | fmt), rd.num,
                                     rs1.num, 0)), []
        rs2 = _need_reg(ops, 2, mnem)
        if base == "fmin" or base == "fmax":
            f3 = 0 if base == "fmin" else 1
            return _bytes_of(_r_type(OP_FP, f3, (0x14 | fmt), rd.num,
                                     rs1.num, rs2.num)), []
        # Rounding mode 7 = dynamic, which is what gas emits by default.
        return _bytes_of(_r_type(OP_FP, 7, (_FP_ARITH[base] | fmt),
                                 rd.num, rs1.num, rs2.num)), []
    if base in _FP_CMP and len(parts) == 2:
        fmt = _fp_fmt(parts[1], mnem)
        rd = _need_reg(ops, 0, mnem)
        rs1 = _need_reg(ops, 1, mnem)
        rs2 = _need_reg(ops, 2, mnem)
        return _bytes_of(_r_type(OP_FP, _FP_CMP[base], (0x50 | fmt),
                                 rd.num, rs1.num, rs2.num)), []
    if base == "fsgnj" or base == "fmv":
        # fmv.d rd, rs is the alias fsgnj.d rd, rs, rs.
        if base == "fmv" and len(parts) == 2:
            fmt = _fp_fmt(parts[1], mnem)
            rd = _need_reg(ops, 0, mnem)
            rs1 = _need_reg(ops, 1, mnem)
            return _bytes_of(_r_type(OP_FP, 0, (0x10 | fmt), rd.num,
                                     rs1.num, rs1.num)), []
        if base == "fsgnj" and len(parts) == 2:
            fmt = _fp_fmt(parts[1], mnem)
            rd = _need_reg(ops, 0, mnem)
            rs1 = _need_reg(ops, 1, mnem)
            rs2 = _need_reg(ops, 2, mnem)
            return _bytes_of(_r_type(OP_FP, 0, (0x10 | fmt), rd.num,
                                     rs1.num, rs2.num)), []
    if base == "fneg" and len(parts) == 2:
        fmt = _fp_fmt(parts[1], mnem)
        rd = _need_reg(ops, 0, mnem)
        rs1 = _need_reg(ops, 1, mnem)
        return _bytes_of(_r_type(OP_FP, 1, (0x10 | fmt), rd.num,
                                 rs1.num, rs1.num)), []
    if base == "fabs" and len(parts) == 2:
        fmt = _fp_fmt(parts[1], mnem)
        rd = _need_reg(ops, 0, mnem)
        rs1 = _need_reg(ops, 1, mnem)
        return _bytes_of(_r_type(OP_FP, 2, (0x10 | fmt), rd.num,
                                 rs1.num, rs1.num)), []
    if base == "fcvt" and len(parts) == 3:
        return _enc_fcvt(mnem, parts[1], parts[2], ops)
    if base == "fmv" and len(parts) == 3:
        return _enc_fmv_x(mnem, parts[1], parts[2], ops)
    raise RiscvError("unsupported floating-point instruction '%s'" % mnem)


# fcvt: (dst, src) -> (funct7, rs2-field, rounding mode). The rs2 field
# selects which integer width is involved; funct7 selects direction and FP
# format. The rounding mode matters only for conversions that can actually
# round: gas emits rm=0 for the three that are always exact (32-bit int to
# double, and float to double) and dynamic (rm=7) for everything else, so we
# match that rather than picking one mode uniformly.
_FCVT = {
    ("w", "s"): (0x60, 0, 7), ("wu", "s"): (0x60, 1, 7),
    ("l", "s"): (0x60, 2, 7), ("lu", "s"): (0x60, 3, 7),
    ("w", "d"): (0x61, 0, 7), ("wu", "d"): (0x61, 1, 7),
    ("l", "d"): (0x61, 2, 7), ("lu", "d"): (0x61, 3, 7),
    ("s", "w"): (0x68, 0, 7), ("s", "wu"): (0x68, 1, 7),
    ("s", "l"): (0x68, 2, 7), ("s", "lu"): (0x68, 3, 7),
    ("d", "w"): (0x69, 0, 0), ("d", "wu"): (0x69, 1, 0),
    ("d", "l"): (0x69, 2, 7), ("d", "lu"): (0x69, 3, 7),
    ("s", "d"): (0x20, 1, 7), ("d", "s"): (0x21, 0, 0),
}


_ROUND_MODES = {"rne": 0, "rtz": 1, "rdn": 2, "rup": 3, "rmm": 4, "dyn": 7}


def _enc_fcvt(mnem, dst, src, ops):
    key = (dst, src)
    if key not in _FCVT:
        raise RiscvError("unsupported conversion '%s'" % mnem)
    ent = _FCVT[key]
    rd = _need_reg(ops, 0, mnem)
    rs1 = _need_reg(ops, 1, mnem)
    rm = ent[2]
    if len(ops) > 2:
        # An explicit rounding mode: `fcvt.w.d a0, fa0, rtz`. C's
        # float-to-integer conversion truncates, so this is not optional
        # decoration -- the default dynamic mode rounds to nearest.
        if ops[2].kind != "sym" or ops[2].sym.lower() not in _ROUND_MODES:
            raise RiscvError("%s: third operand must be a rounding mode"
                             % mnem)
        rm = _ROUND_MODES[ops[2].sym.lower()]
    return _bytes_of(_r_type(OP_FP, rm, ent[0], rd.num, rs1.num,
                             ent[1])), []


def _enc_fmv_x(mnem, a, b, ops):
    """fmv.x.w / fmv.x.d move the raw bits out of an FP register; fmv.w.x /
    fmv.d.x move them in. No conversion happens."""
    rd = _need_reg(ops, 0, mnem)
    rs1 = _need_reg(ops, 1, mnem)
    if a == "x" and b == "w":
        f7 = 0x70
    elif a == "x" and b == "d":
        f7 = 0x71
    elif a == "w" and b == "x":
        f7 = 0x78
    elif a == "d" and b == "x":
        f7 = 0x79
    else:
        raise RiscvError("unsupported move '%s'" % mnem)
    return _bytes_of(_r_type(OP_FP, 0, f7, rd.num, rs1.num, 0)), []


# ---------------------------------------------------------------------------
# Top-level dispatch
# ---------------------------------------------------------------------------
def encode(mnem, ops):
    m = mnem.lower()

    # -- pseudo-instructions ------------------------------------------
    if m == "nop":
        return _bytes_of(_i_type(OP_IMM, 0, 0, 0, 0)), []
    if m == "li":
        return _enc_li(ops)
    if m == "mv":
        rd = _need_reg(ops, 0, m)
        rs = _need_reg(ops, 1, m)
        return _bytes_of(_i_type(OP_IMM, 0, rd.num, rs.num, 0)), []
    if m == "ret":
        return _bytes_of(_i_type(OP_JALR, 0, 0, 1, 0)), []
    if m == "neg" or m == "negw":
        rd = _need_reg(ops, 0, m)
        rs = _need_reg(ops, 1, m)
        opcode = OP_OP if m == "neg" else OP_OP32
        return _bytes_of(_r_type(opcode, 0, 0x20, rd.num, 0, rs.num)), []
    if m == "not":
        rd = _need_reg(ops, 0, m)
        rs = _need_reg(ops, 1, m)
        return _bytes_of(_i_type(OP_IMM, 4, rd.num, rs.num, -1)), []
    if m == "seqz":
        rd = _need_reg(ops, 0, m)
        rs = _need_reg(ops, 1, m)
        return _bytes_of(_i_type(OP_IMM, 3, rd.num, rs.num, 1)), []
    if m == "snez":
        rd = _need_reg(ops, 0, m)
        rs = _need_reg(ops, 1, m)
        return _bytes_of(_r_type(OP_OP, 3, 0, rd.num, 0, rs.num)), []
    if m == "sltz":
        rd = _need_reg(ops, 0, m)
        rs = _need_reg(ops, 1, m)
        return _bytes_of(_r_type(OP_OP, 2, 0, rd.num, rs.num, 0)), []
    if m == "sgtz":
        rd = _need_reg(ops, 0, m)
        rs = _need_reg(ops, 1, m)
        return _bytes_of(_r_type(OP_OP, 2, 0, rd.num, 0, rs.num)), []
    if m == "sext.w":
        rd = _need_reg(ops, 0, m)
        rs = _need_reg(ops, 1, m)
        return _bytes_of(_i_type(OP_IMM32, 0, rd.num, rs.num, 0)), []
    if m == "call" or m == "tail":
        return _enc_call(m, ops)
    if m == "la" or m == "lla":
        return _enc_la(m, ops)
    if m in ("beqz", "bnez", "blez", "bgez", "bltz", "bgtz"):
        return _enc_bz(m, ops)
    if m == "bgt":
        return _enc_branch(m, ops, 4, True)
    if m == "ble":
        return _enc_branch(m, ops, 5, True)
    if m == "bgtu":
        return _enc_branch(m, ops, 6, True)
    if m == "bleu":
        return _enc_branch(m, ops, 7, True)
    if m == "j" or m == "jal":
        return _enc_jal(m, ops)
    if m == "jr" or m == "jalr":
        return _enc_jalr(m, ops)

    # -- base integer -------------------------------------------------
    if m in _RTYPE:
        f = _RTYPE[m]
        rd = _need_reg(ops, 0, m)
        rs1 = _need_reg(ops, 1, m)
        rs2 = _need_reg(ops, 2, m)
        return _bytes_of(_r_type(OP_OP, f[0], f[1], rd.num, rs1.num,
                                 rs2.num)), []
    if m in _RTYPE32:
        f = _RTYPE32[m]
        rd = _need_reg(ops, 0, m)
        rs1 = _need_reg(ops, 1, m)
        rs2 = _need_reg(ops, 2, m)
        return _bytes_of(_r_type(OP_OP32, f[0], f[1], rd.num, rs1.num,
                                 rs2.num)), []
    if m in _ITYPE or m in _ITYPE32:
        opcode = OP_IMM if m in _ITYPE else OP_IMM32
        f3 = _ITYPE[m] if m in _ITYPE else _ITYPE32[m]
        rd = _need_reg(ops, 0, m)
        rs1 = _need_reg(ops, 1, m)
        if len(ops) > 2 and ops[2].kind == "sym":
            kind = _i_lo_kind(ops[2].symkind, m)
            return _bytes_of(_i_type(opcode, f3, rd.num, rs1.num, 0)), \
                [rasm.Reloc(0, ops[2].sym, 4,
                            ops[2].symkind == "pcrel_lo", 0, True, kind)]
        imm = _need_imm(ops, 2, m)
        _check_imm12(imm, m)
        return _bytes_of(_i_type(opcode, f3, rd.num, rs1.num, imm)), []
    if m in _SHIFTI or m in _SHIFTI32:
        is32 = m in _SHIFTI32
        f = _SHIFTI32[m] if is32 else _SHIFTI[m]
        rd = _need_reg(ops, 0, m)
        rs1 = _need_reg(ops, 1, m)
        sh = _need_imm(ops, 2, m)
        limit = 32 if is32 else 64
        if sh < 0 or sh >= limit:
            raise RiscvError("%s: shift %d out of range" % (m, sh))
        # The shift amount occupies the low bits of the immediate field; the
        # arithmetic/logical choice lives in the bits above it. On RV64 a
        # 64-bit shift borrows one bit from funct7, hence the 6-bit field.
        imm = (f[1] << 6) | sh if not is32 else (f[1] << 5) | sh
        return _bytes_of(_i_type(OP_IMM32 if is32 else OP_IMM, f[0],
                                 rd.num, rs1.num, imm)), []
    if m in _LOAD:
        return _enc_load(m, ops, _LOAD[m], OP_LOAD, False)
    if m in _STORE:
        return _enc_store(m, ops, _STORE[m], OP_STORE, False)
    if m in _LOADFP:
        return _enc_load(m, ops, _LOADFP[m], OP_LOADFP, True)
    if m in _STOREFP:
        return _enc_store(m, ops, _STOREFP[m], OP_STOREFP, True)
    if m in _BRANCH:
        return _enc_branch(m, ops, _BRANCH[m], False)
    if m == "lui" or m == "auipc":
        opcode = OP_LUI if m == "lui" else OP_AUIPC
        rd = _need_reg(ops, 0, m)
        if len(ops) > 1 and ops[1].kind == "sym":
            word, rl = _u_with_sym(opcode, rd.num, ops[1], m)
            return _bytes_of(word), rl
        imm = _need_imm(ops, 1, m)
        return _bytes_of(_u_type(opcode, rd.num, imm)), []
    if m == "ecall":
        return _bytes_of(_i_type(OP_SYSTEM, 0, 0, 0, 0)), []
    if m == "ebreak":
        return _bytes_of(_i_type(OP_SYSTEM, 0, 0, 0, 1)), []
    if m == "fence":
        return _bytes_of(0x0FF0000F), []

    # -- floating point -----------------------------------------------
    if m[0:1] == "f" and m.find(".") > 0:
        return _enc_fp(m, ops)

    raise RiscvError("unsupported riscv64 instruction '%s'" % mnem)


def encode_line(mnem, rest):
    """Parse an operand string and encode. Returns (bytes, relocs). The byte
    list is 4 bytes for a real instruction but 8 for `call`/`tail` and for
    `li` with a constant that needs lui+addiw."""
    ops = []
    for tok in split_operands(rest):
        ops.append(parse_operand(tok))
    return encode(mnem, ops)
