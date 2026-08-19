"""rasm_arm64 -- AArch64 (ARM64) instruction encoder and parser.

The x86-64 encoder in rasm.py cannot be parameterised into an AArch64 one:
x86 instructions are variable-length byte streams built from REX/ModRM/SIB
prefixes, while every AArch64 instruction is exactly one 32-bit word with
fixed bit-fields. Almost nothing is shared, so this is a separate module that
produces the same *outputs* -- four bytes plus a list of rasm.Reloc -- and
plugs into the same assembler driver.

Scope is the vocabulary ShivyCX's arm64 back end actually emits (see
`tools/arm64_difftest.py` for the corpus that defines it), plus the handful of
extra instructions a freestanding runtime needs (`svc`, `blr`, `br`, `udiv`,
`ldrh`/`strh`, `ucvtf`, `fcvtzu`). Anything outside that raises
AssemblerError rather than guessing, so an unsupported instruction is a hard
error at assembly time, never a silently wrong word.

Style matches the rest of the dialect: flat functions, uniform record classes,
no metaclasses/generators/**kwargs, bytes as lists of ints.
"""
import rasm


class Arm64Error(Exception):
    pass


# ---------------------------------------------------------------------------
# Operands
# ---------------------------------------------------------------------------
# One uniform record, like rasm.Operand. `kind` selects which fields matter:
#   "reg"   num, size (32/64), is_fp, is_sp  -- x0/w0/sp/xzr/d0/s0
#   "imm"   val
#   "mem"   base, off, sym, symkind, mode    -- [x1, #8] / [x1, :lo12:g]
#   "shift" shiftop ("lsl"/"lsr"/"asr"), val
#   "cond"  cond (condition name)
#   "sym"   sym, symkind                     -- a branch target or :lo12:g
class Op(object):
    def __init__(self, kind):
        self.kind = kind
        self.num = 0
        self.size = 64
        self.is_fp = False
        self.is_sp = False
        self.val = 0
        self.base = 0
        self.off = 0
        self.sym = ""
        self.symkind = ""      # "", "lo12", "abs_g0".."abs_g3"
        self.mode = "off"      # "off" | "pre" | "post"
        self.shiftop = ""
        self.cond = ""
        # Original operand text, so an operand that parsed as a register or a
        # condition code can be recovered as a symbol when the instruction
        # wanted a label (`bl x0`, `b eq` -- both legal symbol names in C).
        self.text = ""


def _op_reg(num, size, is_fp, is_sp):
    o = Op("reg")
    o.num = num
    o.size = size
    o.is_fp = is_fp
    o.is_sp = is_sp
    return o


def _op_imm(v):
    o = Op("imm")
    o.val = v
    return o


# Condition codes, in encoding order.
_CONDS = ["eq", "ne", "cs", "cc", "mi", "pl", "vs", "vc",
          "hi", "ls", "ge", "lt", "gt", "le", "al", "nv"]
# Accepted aliases: hs == cs (unsigned >=), lo == cc (unsigned <).
_COND_ALIAS = {"hs": "cs", "lo": "cc"}


def cond_val(name):
    n = _COND_ALIAS.get(name, name)
    i = 0
    while i < len(_CONDS):
        if _CONDS[i] == n:
            return i
        i += 1
    raise Arm64Error("unknown condition code '%s'" % name)


def _invert_cond(v):
    # The inverse of a condition is its encoding with the low bit flipped.
    return v ^ 1


def parse_reg(tok):
    """Parse a register name, or return None if `tok` is not one."""
    t = tok.strip()
    if t == "":
        return None
    low = t.lower()
    if low == "sp":
        return _op_reg(31, 64, False, True)
    if low == "wsp":
        return _op_reg(31, 32, False, True)
    if low == "xzr":
        return _op_reg(31, 64, False, False)
    if low == "wzr":
        return _op_reg(31, 32, False, False)
    if low == "lr":
        return _op_reg(30, 64, False, False)
    if low == "fp":
        return _op_reg(29, 64, False, False)
    c = low[0]
    rest = low[1:]
    if c not in ("x", "w", "d", "s", "q", "h", "b"):
        return None
    if rest == "" or not _all_digits(rest):
        return None
    n = int(rest)
    if c == "x":
        if n > 30:
            return None
        return _op_reg(n, 64, False, False)
    if c == "w":
        if n > 30:
            return None
        return _op_reg(n, 32, False, False)
    if n > 31:
        return None
    if c == "d":
        return _op_reg(n, 64, True, False)
    if c == "s":
        return _op_reg(n, 32, True, False)
    if c == "h":
        return _op_reg(n, 16, True, False)
    if c == "b":
        return _op_reg(n, 8, True, False)
    return _op_reg(n, 128, True, False)


def _all_digits(s):
    i = 0
    while i < len(s):
        if s[i] < "0" or s[i] > "9":
            return False
        i += 1
    return len(s) > 0


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
            ok = (("0" <= c <= "9") or ("a" <= c <= "f") or ("A" <= c <= "F"))
            if not ok:
                return False
            i += 1
        return len(t) > 0
    return _all_digits(t)


def _split_top(text, sep):
    """Split on `sep`, ignoring separators inside [ ] brackets."""
    parts = []
    depth = 0
    cur = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        if c == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur))
    return parts


def _parse_symref(tok):
    """Parse `:lo12:name`, `#:abs_g1_nc:name`, or a bare symbol name."""
    o = Op("sym")
    t = tok.strip()
    if t[0:1] == "#":
        t = t[1:]
    if t[0:1] == ":":
        end = t.find(":", 1)
        if end < 0:
            raise Arm64Error("malformed symbol modifier '%s'" % tok)
        mod = t[1:end]
        o.sym = t[end + 1:].strip()
        if mod == "lo12":
            o.symkind = "lo12"
        elif mod == "got_lo12":
            o.symkind = "lo12"
        elif mod[0:5] == "abs_g":
            # abs_g0_nc / abs_g1_nc / abs_g2_nc / abs_g3
            o.symkind = mod
        else:
            raise Arm64Error("unsupported symbol modifier ':%s:'" % mod)
        return o
    o.sym = t
    o.symkind = ""
    return o


def parse_operand(text):
    t = text.strip()
    if t == "":
        raise Arm64Error("empty operand")
    if t[0] == "[":
        return _parse_mem(t)
    r = parse_reg(t)
    if r is not None:
        r.text = t
        return r
    if t[0] == "#":
        body = t[1:].strip()
        if body[0:1] == ":":
            return _parse_symref(t)
        if _looks_int(body):
            return _op_imm(_parse_int(body))
        # `#label` -- a symbolic immediate.
        return _parse_symref(t)
    if _looks_int(t):
        return _op_imm(_parse_int(t))
    low = t.lower()
    if low in _CONDS or low in _COND_ALIAS:
        o = Op("cond")
        o.cond = low
        o.text = t
        return o
    parts = low.split()
    if len(parts) == 2 and parts[0] in ("lsl", "lsr", "asr", "ror"):
        o = Op("shift")
        o.shiftop = parts[0]
        amt = parts[1].strip()
        if amt[0:1] == "#":
            amt = amt[1:]
        o.val = _parse_int(amt)
        return o
    if t[0] == ":":
        return _parse_symref(t)
    # A bare name: a branch target or a symbolic address.
    return _parse_symref(t)


def _parse_mem(t):
    """Parse `[base]`, `[base, #imm]`, `[base, #imm]!`, `[base], #imm`,
    `[base, :lo12:sym]`."""
    o = Op("mem")
    close = t.rfind("]")
    if close < 0:
        raise Arm64Error("unterminated memory operand '%s'" % t)
    inner = t[1:close].strip()
    tail = t[close + 1:].strip()
    if tail[0:1] == "!":
        o.mode = "pre"
    elif tail[0:1] == ",":
        o.mode = "post"
        rest = tail[1:].strip()
        if rest[0:1] == "#":
            rest = rest[1:]
        o.off = _parse_int(rest)
    else:
        o.mode = "off"
    parts = _split_top(inner, ",")
    base = parse_reg(parts[0].strip())
    if base is None:
        raise Arm64Error("memory operand needs a base register: '%s'" % t)
    o.base = base.num
    if len(parts) > 1 and o.mode != "post":
        second = parts[1].strip()
        if second[0:1] == "#" and second[1:2] == ":":
            sr = _parse_symref(second)
            o.sym = sr.sym
            o.symkind = sr.symkind
        elif second[0:1] == ":":
            sr = _parse_symref(second)
            o.sym = sr.sym
            o.symkind = sr.symkind
        else:
            v = second
            if v[0:1] == "#":
                v = v[1:]
            if _looks_int(v):
                o.off = _parse_int(v)
            else:
                sr = _parse_symref(second)
                o.sym = sr.sym
                o.symkind = sr.symkind
    elif len(parts) > 1 and o.mode == "post":
        raise Arm64Error("post-index takes its offset outside the brackets")
    return o


def split_operands(rest):
    """Split an operand list on commas outside brackets.

    Post-index addressing puts its offset *after* the closing bracket
    (`[sp], #32`), so a naive comma split turns one operand into two. Any
    immediate that directly follows a bracket-closing operand is folded back
    in and the operand marked post-index."""
    if rest.strip() == "":
        return []
    out = []
    for p in _split_top(rest, ","):
        s = p.strip()
        if s != "":
            out.append(s)
    merged = []
    i = 0
    while i < len(out):
        cur = out[i]
        if (cur[0:1] == "[" and cur[-1:] == "]" and i + 1 < len(out)
                and _is_plain_imm(out[i + 1])):
            merged.append(cur + ", " + out[i + 1])
            i += 2
            continue
        merged.append(cur)
        i += 1
    return merged


def _is_plain_imm(tok):
    t = tok.strip()
    if t[0:1] == "#":
        t = t[1:]
    return _looks_int(t)


# ---------------------------------------------------------------------------
# Bit-field helpers
# ---------------------------------------------------------------------------
def _w(*fields):
    """Assemble a 32-bit word from (value, shift) pairs."""
    word = 0
    for f in fields:
        word |= (f[0] & f[1]) << f[2]
    return word & 0xFFFFFFFF


def _bytes_of(word):
    w = word & 0xFFFFFFFF
    return [w & 0xFF, (w >> 8) & 0xFF, (w >> 16) & 0xFF, (w >> 24) & 0xFF]


def _not_label(ops, i):
    """True if operand `i` cannot serve as a branch target."""
    o = ops[i]
    if o.kind == "sym":
        return False
    return not ((o.kind == "reg" or o.kind == "cond") and o.text != "")


def _label_sym(ops, i):
    """Symbol name of a label operand, recovering one that parsed as a
    register or condition code because it is spelled like one."""
    o = ops[i]
    if o.kind == "sym":
        return o.sym
    return o.text


def _need_reg(ops, i, mnem):
    if i >= len(ops) or ops[i].kind != "reg":
        raise Arm64Error("%s: operand %d must be a register" % (mnem, i + 1))
    return ops[i]


def _need_imm(ops, i, mnem):
    if i >= len(ops) or ops[i].kind != "imm":
        raise Arm64Error("%s: operand %d must be an immediate" % (mnem, i + 1))
    return ops[i].val


def _sf(reg):
    return 1 if reg.size == 64 else 0


def _check_same_size(a, b, mnem):
    if a.size != b.size:
        raise Arm64Error("%s: mixed operand sizes (%d and %d)"
                         % (mnem, a.size, b.size))


# ---------------------------------------------------------------------------
# Encoders, by instruction group
# ---------------------------------------------------------------------------
# add/sub immediate: sf op S 100010 sh imm12 Rn Rd
_ADDSUB_IMM = {"add": 0x11000000, "adds": 0x31000000,
               "sub": 0x51000000, "subs": 0x71000000}
# add/sub shifted register: sf op S 01011 shift 0 Rm imm6 Rn Rd
_ADDSUB_REG = {"add": 0x0B000000, "adds": 0x2B000000,
               "sub": 0x4B000000, "subs": 0x6B000000}
# logical shifted register: sf opc 01010 shift N Rm imm6 Rn Rd
_LOGIC_REG = {"and": 0x0A000000, "orr": 0x2A000000,
              "eor": 0x4A000000, "ands": 0x6A000000,
              "bic": 0x0A200000, "orn": 0x2A200000,
              "eon": 0x4A200000, "bics": 0x6A200000}
_SHIFT_CODE = {"lsl": 0, "lsr": 1, "asr": 2, "ror": 3}


def _enc_addsub(mnem, ops, base_imm, base_reg):
    rd = _need_reg(ops, 0, mnem)
    rn = _need_reg(ops, 1, mnem)
    if len(ops) < 3:
        raise Arm64Error("%s: needs three operands" % mnem)
    third = ops[2]
    sf = _sf(rd)
    if third.kind == "imm":
        v = third.val
        sh = 0
        if v < 0:
            raise Arm64Error("%s: negative immediate; use the opposite "
                             "instruction" % mnem)
        if v > 0xFFF:
            if (v & 0xFFF) == 0 and (v >> 12) <= 0xFFF:
                sh = 1
                v = v >> 12
            else:
                raise Arm64Error("%s: immediate 0x%x is not encodable "
                                 "(needs imm12 optionally shifted by 12)"
                                 % (mnem, third.val))
        # A shift operand may follow: `add x0, x1, #1, lsl #12`.
        if len(ops) > 3 and ops[3].kind == "shift":
            if ops[3].shiftop != "lsl" or ops[3].val != 12:
                raise Arm64Error("%s: only `lsl #12` may follow an immediate"
                                 % mnem)
            sh = 1
        return _w((base_imm >> 24, 0xFF, 24), (sf, 1, 31), (sh, 1, 22),
                  (v, 0xFFF, 10), (rn.num, 0x1F, 5), (rd.num, 0x1F, 0)), []
    if third.kind == "reg":
        rm = third
        shift = 0
        amount = 0
        if len(ops) > 3:
            if ops[3].kind != "shift":
                raise Arm64Error("%s: expected a shift after the register"
                                 % mnem)
            shift = _SHIFT_CODE[ops[3].shiftop]
            amount = ops[3].val
            if shift == 3:
                raise Arm64Error("%s: ror is not valid here" % mnem)
        if rd.is_sp or rn.is_sp:
            # add/sub with SP as an operand uses the extended-register form.
            # Only the no-shift case is needed here.
            if shift != 0 or amount != 0:
                raise Arm64Error("%s: shifted register with sp is not "
                                 "supported" % mnem)
            opt = 3 if rd.size == 64 else 2       # uxtx / uxtw
            ext_base = 0x0B200000 if base_reg == 0x0B000000 else (
                0x2B200000 if base_reg == 0x2B000000 else (
                    0x4B200000 if base_reg == 0x4B000000 else 0x6B200000))
            return _w((ext_base >> 21, 0x7FF, 21), (sf, 1, 31),
                      (rm.num, 0x1F, 16), (opt, 7, 13),
                      (rn.num, 0x1F, 5), (rd.num, 0x1F, 0)), []
        return _w((base_reg >> 24, 0xFF, 24), (sf, 1, 31), (shift, 3, 22),
                  (rm.num, 0x1F, 16), (amount, 0x3F, 10),
                  (rn.num, 0x1F, 5), (rd.num, 0x1F, 0)), []
    if third.kind == "sym":
        # `add x1, x1, :lo12:sym`
        if third.symkind != "lo12":
            raise Arm64Error("%s: only :lo12: is valid as an operand" % mnem)
        word = _w((base_imm >> 24, 0xFF, 24), (sf, 1, 31),
                  (rn.num, 0x1F, 5), (rd.num, 0x1F, 0))
        rel = rasm.Reloc(0, third.sym, 4, False, 0, False, "add_lo12")
        return word, [rel]
    raise Arm64Error("%s: unsupported operand form" % mnem)


# Logical immediate: sf opc 100100 N immr imms Rn Rd. Only the four base
# operations have an immediate form -- `bic`/`orn`/`eon` do not, since the
# assembler would have to invert the immediate, and an inverted value is very
# often no longer a valid logical immediate at all.
_LOGIC_IMM = {"and": 0x12000000, "orr": 0x32000000,
              "eor": 0x52000000, "ands": 0x72000000}


def _enc_logic_imm(mnem, ops, rd, rn):
    """`and/orr/eor/ands Xd, Xn, #imm` -- the bitmask-immediate form.

    Boot code leans on this constantly (`and x0, x0, #3` to take the CPU id
    out of MPIDR_EL1, `orr x0, x0, #(1 << 31)` to set HCR_EL2.RW), so its
    absence stops a boot stub at the second instruction.
    """
    if mnem not in _LOGIC_IMM:
        raise Arm64Error("%s: no immediate form; use a register operand"
                         % mnem)
    width = 64 if rd.size == 64 else 32
    v = ops[2].val
    uv = v & ((1 << width) - 1)
    bits = _logical_imm_bits(uv, width)
    if bits < 0:
        raise Arm64Error("%s: 0x%x is not a valid logical immediate; load it "
                         "into a register first" % (mnem, v))
    n = (bits >> 12) & 1
    immr = (bits >> 6) & 0x3F
    imms = bits & 0x3F
    return _w((_LOGIC_IMM[mnem] >> 24, 0xFF, 24), (_sf(rd), 1, 31),
              (n, 1, 22), (immr, 0x3F, 16), (imms, 0x3F, 10),
              (rn.num, 0x1F, 5), (rd.num, 0x1F, 0)), []


def _enc_logic(mnem, ops):
    base = _LOGIC_REG[mnem]
    rd = _need_reg(ops, 0, mnem)
    rn = _need_reg(ops, 1, mnem)
    if len(ops) > 2 and ops[2].kind == "imm":
        return _enc_logic_imm(mnem, ops, rd, rn)
    rm = _need_reg(ops, 2, mnem)
    shift = 0
    amount = 0
    if len(ops) > 3:
        if ops[3].kind != "shift":
            raise Arm64Error("%s: expected a shift" % mnem)
        shift = _SHIFT_CODE[ops[3].shiftop]
        amount = ops[3].val
    return _w((base >> 21, 0x7FF, 21), (_sf(rd), 1, 31), (shift, 3, 22),
              (rm.num, 0x1F, 16), (amount, 0x3F, 10),
              (rn.num, 0x1F, 5), (rd.num, 0x1F, 0)), []


# move wide: sf opc 100101 hw imm16 Rd
_MOVW = {"movn": 0x12800000, "movz": 0x52800000, "movk": 0x72800000}


def _enc_movw(mnem, ops):
    rd = _need_reg(ops, 0, mnem)
    sf = _sf(rd)
    hw = 0
    if len(ops) > 2:
        if ops[2].kind != "shift" or ops[2].shiftop != "lsl":
            raise Arm64Error("%s: expected `lsl #shift`" % mnem)
        amt = ops[2].val
        if amt % 16 != 0 or amt > 48 or (sf == 0 and amt > 16):
            raise Arm64Error("%s: bad shift %d" % (mnem, amt))
        hw = amt // 16
    base = _MOVW[mnem]
    if len(ops) > 1 and ops[1].kind == "sym":
        # movz/movk with #:abs_gN:sym
        kindmap = {"abs_g0_nc": "movw_g0_nc", "abs_g1_nc": "movw_g1_nc",
                   "abs_g2_nc": "movw_g2_nc", "abs_g3": "movw_g3",
                   "abs_g0": "movw_g0_nc", "abs_g1": "movw_g1_nc",
                   "abs_g2": "movw_g2_nc"}
        k = kindmap.get(ops[1].symkind)
        if k is None:
            raise Arm64Error("%s: unsupported modifier for a symbol" % mnem)
        shmap = {"movw_g0_nc": 0, "movw_g1_nc": 1,
                 "movw_g2_nc": 2, "movw_g3": 3}
        hw = shmap[k]
        word = _w((base >> 23, 0x1FF, 23), (sf, 1, 31), (hw, 3, 21),
                  (rd.num, 0x1F, 0))
        return word, [rasm.Reloc(0, ops[1].sym, 4, False, 0, False, k)]
    imm = _need_imm(ops, 1, mnem)
    if imm < 0 or imm > 0xFFFF:
        raise Arm64Error("%s: immediate 0x%x does not fit in 16 bits"
                         % (mnem, imm))
    return _w((base >> 23, 0x1FF, 23), (sf, 1, 31), (hw, 3, 21),
              (imm, 0xFFFF, 5), (rd.num, 0x1F, 0)), []


def _logical_imm_bits(value, size):
    """Encode `value` as an AArch64 logical immediate (N:immr:imms), or
    return -1 if it is not one. Logical immediates are bit patterns made of a
    repeated run of ones, which is what makes `mov x0, #0xff` a single
    instruction while `mov x0, #0x1234` is not."""
    if size == 32:
        value = value & 0xFFFFFFFF
        if value == 0 or value == 0xFFFFFFFF:
            return -1
    else:
        value = value & 0xFFFFFFFFFFFFFFFF
        if value == 0 or value == 0xFFFFFFFFFFFFFFFF:
            return -1
    # Find the smallest element width whose repetition reproduces `value`.
    esize = 2
    while esize <= size:
        mask = (1 << esize) - 1
        elem = value & mask
        ok = True
        pos = esize
        while pos < size:
            if ((value >> pos) & mask) != elem:
                ok = False
                break
            pos += esize
        if ok:
            # The element must be a single contiguous run of ones, possibly
            # rotated. Rotate it until the run is right-aligned.
            rot = 0
            e = elem
            while rot < esize:
                if (e & 1) != 0 and ((e >> (esize - 1)) & 1) == 0:
                    break
                e = ((e >> 1) | ((e & 1) << (esize - 1))) & mask
                rot += 1
            if rot >= esize:
                return -1
            ones = 0
            t = e
            while (t & 1) != 0:
                ones += 1
                t >>= 1
            if t != 0:
                return -1               # not contiguous
            immr = (esize - rot) % esize
            imms = (((-esize) << 1) & 0x3F) | (ones - 1)
            n = 1 if esize == 64 else 0
            if esize != 64:
                imms = (imms & 0x3F)
            return (n << 12) | (immr << 6) | (imms & 0x3F)
        esize = esize * 2
    return -1


# logical immediate: sf opc 100100 N immr imms Rn Rd
_LOGIC_IMM = {"and": 0x12000000, "orr": 0x32000000,
              "eor": 0x52000000, "ands": 0x72000000}


def _enc_mov(mnem, ops):
    """`mov` is an alias: register-to-register is `orr Rd, xzr, Rm`, anything
    involving sp is `add Rd, Rn, #0`, and an immediate becomes movz/movn or a
    logical-immediate orr, exactly as gas chooses."""
    rd = _need_reg(ops, 0, "mov")
    if len(ops) < 2:
        raise Arm64Error("mov: needs two operands")
    src = ops[1]
    sf = _sf(rd)
    if src.kind == "reg":
        if rd.is_sp or src.is_sp:
            return _w((0x11000000 >> 24, 0xFF, 24), (sf, 1, 31),
                      (src.num, 0x1F, 5), (rd.num, 0x1F, 0)), []
        return _w((0x2A000000 >> 24, 0xFF, 24), (sf, 1, 31),
                  (src.num, 0x1F, 16), (31, 0x1F, 5), (rd.num, 0x1F, 0)), []
    if src.kind == "imm":
        v = src.val
        width = 64 if sf == 1 else 32
        uv = v & ((1 << width) - 1)
        # movz if it fits in one 16-bit slice.
        sh = 0
        while sh < width:
            if (uv & ~(0xFFFF << sh)) == 0:
                return _w((0x52800000 >> 23, 0x1FF, 23), (sf, 1, 31),
                          (sh // 16, 3, 21), ((uv >> sh) & 0xFFFF, 0xFFFF, 5),
                          (rd.num, 0x1F, 0)), []
            sh += 16
        # movn if the inverse fits in one slice.
        inv = (~uv) & ((1 << width) - 1)
        sh = 0
        while sh < width:
            if (inv & ~(0xFFFF << sh)) == 0:
                return _w((0x12800000 >> 23, 0x1FF, 23), (sf, 1, 31),
                          (sh // 16, 3, 21), ((inv >> sh) & 0xFFFF, 0xFFFF, 5),
                          (rd.num, 0x1F, 0)), []
            sh += 16
        bits = _logical_imm_bits(uv, width)
        if bits >= 0:
            n = (bits >> 12) & 1
            immr = (bits >> 6) & 0x3F
            imms = bits & 0x3F
            return _w((0x32000000 >> 24, 0xFF, 24), (sf, 1, 31), (n, 1, 22),
                      (immr, 0x3F, 16), (imms, 0x3F, 10), (31, 0x1F, 5),
                      (rd.num, 0x1F, 0)), []
        raise Arm64Error("mov: immediate 0x%x needs a movz/movk sequence; "
                         "emit one explicitly" % v)
    raise Arm64Error("mov: unsupported operand form")


def _enc_mvn(ops):
    rd = _need_reg(ops, 0, "mvn")
    rm = _need_reg(ops, 1, "mvn")
    return _w((0x2A200000 >> 21, 0x7FF, 21), (_sf(rd), 1, 31),
              (rm.num, 0x1F, 16), (31, 0x1F, 5), (rd.num, 0x1F, 0)), []


def _enc_neg(ops):
    rd = _need_reg(ops, 0, "neg")
    rm = _need_reg(ops, 1, "neg")
    shift = 0
    amount = 0
    if len(ops) > 2 and ops[2].kind == "shift":
        shift = _SHIFT_CODE[ops[2].shiftop]
        amount = ops[2].val
    return _w((0x4B000000 >> 24, 0xFF, 24), (_sf(rd), 1, 31), (shift, 3, 22),
              (rm.num, 0x1F, 16), (amount, 0x3F, 10), (31, 0x1F, 5),
              (rd.num, 0x1F, 0)), []


def _enc_cmp(mnem, ops):
    """cmp/cmn are adds/subs writing to the zero register."""
    rn = _need_reg(ops, 0, mnem)
    fake = [_op_reg(31, rn.size, False, False), rn]
    i = 1
    while i < len(ops):
        fake.append(ops[i])
        i += 1
    if mnem == "cmp":
        return _enc_addsub("subs", fake, 0x71000000, 0x6B000000)
    return _enc_addsub("adds", fake, 0x31000000, 0x2B000000)


def _enc_tst(ops):
    rn = _need_reg(ops, 0, "tst")
    rm = _need_reg(ops, 1, "tst")
    return _w((0x6A000000 >> 21, 0x7FF, 21), (_sf(rn), 1, 31),
              (rm.num, 0x1F, 16), (rn.num, 0x1F, 5), (31, 0x1F, 0)), []


# 3-source: madd/msub -- sf 00 11011 000 Rm o0 Ra Rn Rd
def _enc_madd(mnem, ops):
    rd = _need_reg(ops, 0, mnem)
    rn = _need_reg(ops, 1, mnem)
    rm = _need_reg(ops, 2, mnem)
    if mnem == "mul":
        ra = 31
        o0 = 0
    elif mnem == "mneg":
        ra = 31
        o0 = 1
    else:
        ra = _need_reg(ops, 3, mnem).num
        o0 = 1 if mnem == "msub" else 0
    return _w((0x1B000000 >> 24, 0xFF, 24), (_sf(rd), 1, 31),
              (rm.num, 0x1F, 16), (o0, 1, 15), (ra, 0x1F, 10),
              (rn.num, 0x1F, 5), (rd.num, 0x1F, 0)), []


# 2-source: sf 0 0 11010110 Rm opcode Rn Rd
_DP2 = {"udiv": 2, "sdiv": 3, "lslv": 8, "lsrv": 9, "asrv": 10, "rorv": 11}


def _enc_dp2(mnem, ops, opcode):
    rd = _need_reg(ops, 0, mnem)
    rn = _need_reg(ops, 1, mnem)
    rm = _need_reg(ops, 2, mnem)
    return _w((0x1AC00000 >> 21, 0x7FF, 21), (_sf(rd), 1, 31),
              (rm.num, 0x1F, 16), (opcode, 0x3F, 10),
              (rn.num, 0x1F, 5), (rd.num, 0x1F, 0)), []


def _enc_bitfield(mnem, ops):
    """lsl/lsr/asr by an immediate, and sxtw/sxtb/sxth/uxtb/uxth, are all
    aliases of the bitfield-move instructions."""
    rd = _need_reg(ops, 0, mnem)
    rn = _need_reg(ops, 1, mnem)
    sf = _sf(rd)
    width = 64 if sf == 1 else 32
    n = sf
    if mnem == "sxtw":
        return _bfm(0x93400000, 1, 1, 0, 31, rn.num, rd.num), []
    if mnem == "sxtb":
        return _bfm(0x13000000, sf, n, 0, 7, rn.num, rd.num), []
    if mnem == "sxth":
        return _bfm(0x13000000, sf, n, 0, 15, rn.num, rd.num), []
    if mnem == "uxtb":
        return _bfm(0x53000000, 0, 0, 0, 7, rn.num, rd.num), []
    if mnem == "uxth":
        return _bfm(0x53000000, 0, 0, 0, 15, rn.num, rd.num), []
    sh = _need_imm(ops, 2, mnem)
    if sh < 0 or sh >= width:
        raise Arm64Error("%s: shift %d out of range for a %d-bit register"
                         % (mnem, sh, width))
    if mnem == "lsl":
        return _bfm(0x53000000, sf, n, (width - sh) % width,
                    width - 1 - sh, rn.num, rd.num), []
    if mnem == "lsr":
        return _bfm(0x53000000, sf, n, sh, width - 1, rn.num, rd.num), []
    if mnem == "asr":
        return _bfm(0x13000000, sf, n, sh, width - 1, rn.num, rd.num), []
    raise Arm64Error("%s: not a bitfield alias" % mnem)


def _bfm(base, sf, n, immr, imms, rn, rd):
    return _w((base >> 23, 0x1FF, 23), (sf, 1, 31), (n, 1, 22),
              (immr, 0x3F, 16), (imms, 0x3F, 10), (rn, 0x1F, 5), (rd, 0x1F, 0))


def _enc_cset(ops):
    rd = _need_reg(ops, 0, "cset")
    if len(ops) < 2 or ops[1].kind != "cond":
        raise Arm64Error("cset: second operand must be a condition")
    c = _invert_cond(cond_val(ops[1].cond))
    # csinc Rd, zr, zr, invert(cond)
    return _w((0x1A800400 >> 21, 0x7FF, 21), (_sf(rd), 1, 31),
              (31, 0x1F, 16), (c, 0xF, 12), (1, 1, 10),
              (31, 0x1F, 5), (rd.num, 0x1F, 0)), []


def _enc_csinc(mnem, ops):
    rd = _need_reg(ops, 0, mnem)
    rn = _need_reg(ops, 1, mnem)
    rm = _need_reg(ops, 2, mnem)
    if len(ops) < 4 or ops[3].kind != "cond":
        raise Arm64Error("%s: fourth operand must be a condition" % mnem)
    c = cond_val(ops[3].cond)
    op2 = {"csel": 0, "csinc": 1, "csinv": 2, "csneg": 3}[mnem]
    return _w((0x1A800000 >> 21, 0x7FF, 21), (_sf(rd), 1, 31),
              (rm.num, 0x1F, 16), (c, 0xF, 12), (op2 & 1, 1, 10),
              ((op2 >> 1) & 1, 1, 30), (rn.num, 0x1F, 5),
              (rd.num, 0x1F, 0)), []


# ---------------------------------------------------------------------------
# Loads and stores
# ---------------------------------------------------------------------------
# size(2) 111 V(1) 00 opc(2) ... unsigned-offset form is size 111 V 01 opc imm12
def _ldst_params(mnem, rt):
    """Return (size_field, V, opc, scale) for a load/store mnemonic."""
    if rt.is_fp:
        if rt.size == 32:
            sz = 2
            scale = 2
        elif rt.size == 64:
            sz = 3
            scale = 3
        elif rt.size == 128:
            sz = 0
            scale = 4
        else:
            raise Arm64Error("%s: unsupported FP access width" % mnem)
        opc = 1 if mnem == "ldr" else 0
        if rt.size == 128:
            opc = 3 if mnem == "ldr" else 2
        return sz, 1, opc, scale
    if mnem == "ldr" or mnem == "str":
        sz = 3 if rt.size == 64 else 2
        scale = 3 if rt.size == 64 else 2
        opc = 1 if mnem == "ldr" else 0
        return sz, 0, opc, scale
    if mnem == "ldrb" or mnem == "strb":
        return 0, 0, (1 if mnem == "ldrb" else 0), 0
    if mnem == "ldrh" or mnem == "strh":
        return 1, 0, (1 if mnem == "ldrh" else 0), 1
    if mnem == "ldrsb":
        # opc 10 loads into an X register, 11 into a W register.
        return 0, 0, (2 if rt.size == 64 else 3), 0
    if mnem == "ldrsh":
        return 1, 0, (2 if rt.size == 64 else 3), 1
    if mnem == "ldrsw":
        return 2, 0, 2, 2
    raise Arm64Error("%s: not a load/store" % mnem)


_LDST_LO12_KIND = {0: "ldst8_lo12", 1: "ldst16_lo12", 2: "ldst32_lo12",
                   3: "ldst64_lo12", 4: "ldst128_lo12"}


def _enc_ldst(mnem, ops):
    rt = _need_reg(ops, 0, mnem)
    if len(ops) < 2 or ops[1].kind != "mem":
        raise Arm64Error("%s: second operand must be a memory reference"
                         % mnem)
    m = ops[1]
    sz, v, opc, scale = _ldst_params(mnem, rt)
    if m.sym != "":
        if m.symkind != "lo12":
            raise Arm64Error("%s: only :lo12: is valid in a memory operand"
                             % mnem)
        word = _w((sz, 3, 30), (7, 7, 27), (v, 1, 26), (1, 3, 24),
                  (opc, 3, 22), (m.base, 0x1F, 5), (rt.num, 0x1F, 0))
        kind = _LDST_LO12_KIND[scale]
        return word, [rasm.Reloc(0, m.sym, 4, False, 0, False, kind)]
    if m.mode == "off":
        off = m.off
        if off >= 0 and (off & ((1 << scale) - 1)) == 0 \
                and (off >> scale) <= 0xFFF:
            return _w((sz, 3, 30), (7, 7, 27), (v, 1, 26), (1, 3, 24),
                      (opc, 3, 22), (off >> scale, 0xFFF, 10),
                      (m.base, 0x1F, 5), (rt.num, 0x1F, 0)), []
        # Unscaled signed 9-bit form (ldur/stur) covers the rest.
        if off < -256 or off > 255:
            raise Arm64Error("%s: offset %d is out of range" % (mnem, off))
        return _w((sz, 3, 30), (7, 7, 27), (v, 1, 26), (0, 3, 24),
                  (opc, 3, 22), (off & 0x1FF, 0x1FF, 12),
                  (m.base, 0x1F, 5), (rt.num, 0x1F, 0)), []
    # pre/post-index: size 111 V 00 opc 0 imm9 mode Rn Rt
    off = m.off
    if off < -256 or off > 255:
        raise Arm64Error("%s: index offset %d is out of range" % (mnem, off))
    mode_bits = 3 if m.mode == "pre" else 1
    return _w((sz, 3, 30), (7, 7, 27), (v, 1, 26), (0, 3, 24),
              (opc, 3, 22), (off & 0x1FF, 0x1FF, 12), (mode_bits, 3, 10),
              (m.base, 0x1F, 5), (rt.num, 0x1F, 0)), []


def _enc_ldstp(mnem, ops):
    """ldp/stp: opc 101 V mode L imm7 Rt2 Rn Rt."""
    rt = _need_reg(ops, 0, mnem)
    rt2 = _need_reg(ops, 1, mnem)
    _check_same_size(rt, rt2, mnem)
    if len(ops) < 3 or ops[2].kind != "mem":
        raise Arm64Error("%s: third operand must be a memory reference"
                         % mnem)
    m = ops[2]
    if rt.is_fp:
        v = 1
        if rt.size == 32:
            opc = 0
            scale = 2
        elif rt.size == 64:
            opc = 1
            scale = 3
        else:
            opc = 2
            scale = 4
    else:
        v = 0
        opc = 2 if rt.size == 64 else 0
        scale = 3 if rt.size == 64 else 2
    L = 1 if mnem == "ldp" else 0
    if m.mode == "off":
        mode_bits = 2
    elif m.mode == "pre":
        mode_bits = 3
    else:
        mode_bits = 1
    off = m.off
    if (off & ((1 << scale) - 1)) != 0:
        raise Arm64Error("%s: offset %d must be a multiple of %d"
                         % (mnem, off, 1 << scale))
    imm7 = off >> scale
    if imm7 < -64 or imm7 > 63:
        raise Arm64Error("%s: offset %d is out of range" % (mnem, off))
    return _w((opc, 3, 30), (5, 7, 27), (v, 1, 26), (mode_bits, 7, 23),
              (L, 1, 22), (imm7 & 0x7F, 0x7F, 15), (rt2.num, 0x1F, 10),
              (m.base, 0x1F, 5), (rt.num, 0x1F, 0)), []


# ---------------------------------------------------------------------------
# Branches and PC-relative
# ---------------------------------------------------------------------------
def _enc_branch(mnem, ops):
    if len(ops) < 1 or _not_label(ops, 0):
        raise Arm64Error("%s: needs a label" % mnem)
    base = 0x94000000 if mnem == "bl" else 0x14000000
    kind = "call26" if mnem == "bl" else "jump26"
    return base, [rasm.Reloc(0, _label_sym(ops, 0), 4, True, 0, True, kind)]


def _enc_bcond(cond, ops):
    if len(ops) < 1 or _not_label(ops, 0):
        raise Arm64Error("b.%s: needs a label" % cond)
    word = _w((0x54000000 >> 24, 0xFF, 24), (cond_val(cond), 0xF, 0))
    return word, [rasm.Reloc(0, _label_sym(ops, 0), 4, True, 0, True, "condbr19")]


def _enc_cbz(mnem, ops):
    rt = _need_reg(ops, 0, mnem)
    if len(ops) < 2 or _not_label(ops, 1):
        raise Arm64Error("%s: needs a label" % mnem)
    base = 0x35000000 if mnem == "cbnz" else 0x34000000
    word = _w((base >> 24, 0xFF, 24), (_sf(rt), 1, 31), (rt.num, 0x1F, 0))
    return word, [rasm.Reloc(0, _label_sym(ops, 1), 4, True, 0, True, "condbr19")]


def _enc_tbz(mnem, ops):
    rt = _need_reg(ops, 0, mnem)
    bit = _need_imm(ops, 1, mnem)
    if len(ops) < 3 or _not_label(ops, 2):
        raise Arm64Error("%s: needs a label" % mnem)
    base = 0x37000000 if mnem == "tbnz" else 0x36000000
    word = _w((base >> 24, 0xFF, 24), ((bit >> 5) & 1, 1, 31),
              (bit & 0x1F, 0x1F, 19), (rt.num, 0x1F, 0))
    return word, [rasm.Reloc(0, _label_sym(ops, 2), 4, True, 0, True, "tstbr14")]


def _enc_adrp(ops):
    rd = _need_reg(ops, 0, "adrp")
    if len(ops) < 2 or _not_label(ops, 1):
        raise Arm64Error("adrp: second operand must be a symbol")
    word = _w((0x90000000 >> 24, 0xFF, 24), (rd.num, 0x1F, 0))
    return word, [rasm.Reloc(0, ops[1].sym, 4, True, 0, False,
                             "adr_pg_hi21")]


def _enc_ret(ops):
    rn = 30
    if len(ops) > 0:
        rn = _need_reg(ops, 0, "ret").num
    return _w((0xD65F0000 >> 16, 0xFFFF, 16), (rn, 0x1F, 5)), []


def _enc_br(mnem, ops):
    rn = _need_reg(ops, 0, mnem).num
    base = 0xD63F0000 if mnem == "blr" else 0xD61F0000
    return _w((base >> 16, 0xFFFF, 16), (rn, 0x1F, 5)), []


# ---------------------------------------------------------------------------
# Floating point
# ---------------------------------------------------------------------------
# Floating-point data-processing (2 source):
#   M 0 S 11110 type 1 Rm opcode(4) 10 Rn Rd
# The opcode sits at bits 15:12 and bits 11:10 are a fixed 0b10. Deriving the
# base by shifting a full constant right by 21 silently drops both, which is
# exactly the bug this table avoids.
_FP_DP2 = {"fmul": 0, "fdiv": 1, "fadd": 2, "fsub": 3,
           "fmax": 4, "fmin": 5, "fnmul": 8}


def _fp_type(reg):
    if reg.size == 32:
        return 0
    if reg.size == 64:
        return 1
    if reg.size == 16:
        return 3
    raise Arm64Error("unsupported floating-point width")


def _enc_fp_dp2(mnem, ops):
    rd = _need_reg(ops, 0, mnem)
    rn = _need_reg(ops, 1, mnem)
    rm = _need_reg(ops, 2, mnem)
    _check_same_size(rd, rn, mnem)
    _check_same_size(rd, rm, mnem)
    return _w((0x1E, 0xFF, 24), (_fp_type(rd), 3, 22), (1, 1, 21),
              (rm.num, 0x1F, 16), (_FP_DP2[mnem], 0xF, 12), (2, 3, 10),
              (rn.num, 0x1F, 5), (rd.num, 0x1F, 0)), []


# Floating-point data-processing (1 source):
#   M 0 S 11110 type 1 opcode(6) 10000 Rn Rd
# opcode is bits 20:15; bits 14:10 are a fixed 0b10000.
_FP_DP1 = {"fmov": 0, "fabs": 1, "fneg": 2, "fsqrt": 3}


def _fp_dp1(ftype, opcode, rn, rd):
    return _w((0x1E, 0xFF, 24), (ftype, 3, 22), (1, 1, 21),
              (opcode, 0x3F, 15), (0x10, 0x1F, 10),
              (rn, 0x1F, 5), (rd, 0x1F, 0))


def _enc_fneg(mnem, ops):
    rd = _need_reg(ops, 0, mnem)
    rn = _need_reg(ops, 1, mnem)
    _check_same_size(rd, rn, mnem)
    return _fp_dp1(_fp_type(rd), _FP_DP1[mnem], rn.num, rd.num), []


def _enc_fmov(ops):
    """fmov moves between two FP registers, or between an FP and a general
    register (a bit-pattern move, not a conversion)."""
    rd = _need_reg(ops, 0, "fmov")
    rn = _need_reg(ops, 1, "fmov")
    if rd.is_fp and rn.is_fp:
        return _enc_fneg("fmov", ops)
    if rd.is_fp and not rn.is_fp:
        # general -> FP
        if rd.size != rn.size:
            raise Arm64Error("fmov: register widths must match")
        sf = 1 if rn.size == 64 else 0
        ftype = _fp_type(rd)
        return _w((0x1E260000 >> 16, 0xFFFF, 16), (sf, 1, 31),
                  (ftype, 3, 22), (7, 7, 16), (rn.num, 0x1F, 5),
                  (rd.num, 0x1F, 0)), []
    if not rd.is_fp and rn.is_fp:
        if rd.size != rn.size:
            raise Arm64Error("fmov: register widths must match")
        sf = 1 if rd.size == 64 else 0
        ftype = _fp_type(rn)
        return _w((0x1E260000 >> 16, 0xFFFF, 16), (sf, 1, 31),
                  (ftype, 3, 22), (6, 7, 16), (rn.num, 0x1F, 5),
                  (rd.num, 0x1F, 0)), []
    raise Arm64Error("fmov: unsupported operand form")


def _enc_fcmp(mnem, ops):
    # FP compare: M 0 S 11110 type 1 Rm op(2) 1000 Rn opcode2(5)
    # Bits 13:10 are a fixed 0b1000; op is bits 15:14.
    rn = _need_reg(ops, 0, mnem)
    rm = _need_reg(ops, 1, mnem)
    _check_same_size(rn, rm, mnem)
    opc2 = 0 if mnem == "fcmp" else 0x10      # fcmpe sets bit 4 of opcode2
    return _w((0x1E, 0xFF, 24), (_fp_type(rn), 3, 22), (1, 1, 21),
              (rm.num, 0x1F, 16), (8, 0xF, 10), (rn.num, 0x1F, 5),
              (opc2, 0x1F, 0)), []


def _enc_fcvt(ops):
    # fcvt is the 1-source group with opcode 0b0001<<2 | destination type.
    rd = _need_reg(ops, 0, "fcvt")
    rn = _need_reg(ops, 1, "fcvt")
    src = _fp_type(rn)
    dst = _fp_type(rd)
    if src == dst:
        raise Arm64Error("fcvt: source and destination have the same width")
    return _fp_dp1(src, 4 | dst, rn.num, rd.num), []


# float<->int conversions: sf 0 0 11110 type 1 rmode opcode 000000 Rn Rd
_FCVT_INT = {"fcvtzs": 3, "fcvtzu": 3, "scvtf": 0, "ucvtf": 0}
_FCVT_OPC = {"fcvtzs": 0, "fcvtzu": 1, "scvtf": 2, "ucvtf": 3}


def _enc_fcvt_int(mnem, ops):
    rd = _need_reg(ops, 0, mnem)
    rn = _need_reg(ops, 1, mnem)
    if mnem == "scvtf" or mnem == "ucvtf":
        ftype = _fp_type(rd)
        sf = 1 if rn.size == 64 else 0
    else:
        ftype = _fp_type(rn)
        sf = 1 if rd.size == 64 else 0
    rmode = _FCVT_INT[mnem]
    opcode = _FCVT_OPC[mnem]
    return _w((0x1E200000 >> 21, 0x7FF, 21), (sf, 1, 31), (ftype, 3, 22),
              (rmode, 3, 19), (opcode, 7, 16), (rn.num, 0x1F, 5),
              (rd.num, 0x1F, 0)), []


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
def _enc_svc(ops):
    imm = 0
    if len(ops) > 0:
        imm = _need_imm(ops, 0, "svc")
    return _w((0xD4000001 >> 21, 0x7FF, 21), (imm, 0xFFFF, 5), (1, 0x1F, 0)), []


def _enc_nop():
    return 0xD503201F, []


# ---------------------------------------------------------------------------
# System instructions
# ---------------------------------------------------------------------------
# Everything bare metal needs that ordinary user-mode code never does: reading
# and writing system registers, barriers, the exception return, TLB and cache
# maintenance, and PC-relative address formation with `adr`.
#
# A system register is identified by five fields (op0, op1, CRn, CRm, op2), and
# `mrs`/`msr` simply drop them into fixed positions. The table below is the
# subset the boot, exception and MMU paths use; anything absent can still be
# written in the architectural `S<op0>_<op1>_C<n>_C<m>_<op2>` form, which
# `_parse_sysreg` also accepts, so an unlisted register is never a hard wall.
_SYSREGS = {
    # -- identification ------------------------------------------------
    "midr_el1":     (3, 0, 0, 0, 0),
    "mpidr_el1":    (3, 0, 0, 0, 5),
    "id_aa64mmfr0_el1": (3, 0, 0, 7, 0),
    "currentel":    (3, 0, 4, 2, 2),
    "daif":         (3, 3, 4, 2, 1),
    # -- exception handling --------------------------------------------
    "vbar_el1":     (3, 0, 12, 0, 0),
    "vbar_el2":     (3, 4, 12, 0, 0),
    "esr_el1":      (3, 0, 5, 2, 0),
    "esr_el2":      (3, 4, 5, 2, 0),
    "far_el1":      (3, 0, 6, 0, 0),
    "far_el2":      (3, 4, 6, 0, 0),
    "elr_el1":      (3, 0, 4, 0, 1),
    "elr_el2":      (3, 4, 4, 0, 1),
    "elr_el3":      (3, 6, 4, 0, 1),
    "spsr_el1":     (3, 0, 4, 0, 0),
    "spsr_el2":     (3, 4, 4, 0, 0),
    "spsr_el3":     (3, 6, 4, 0, 0),
    "sp_el0":       (3, 0, 4, 1, 0),
    "sp_el1":       (3, 4, 4, 1, 0),
    # -- control -------------------------------------------------------
    "sctlr_el1":    (3, 0, 1, 0, 0),
    "sctlr_el2":    (3, 4, 1, 0, 0),
    "cpacr_el1":    (3, 0, 1, 0, 2),
    "hcr_el2":      (3, 4, 1, 1, 0),
    "scr_el3":      (3, 6, 1, 1, 0),
    # -- translation ---------------------------------------------------
    "ttbr0_el1":    (3, 0, 2, 0, 0),
    "ttbr1_el1":    (3, 0, 2, 0, 1),
    "tcr_el1":      (3, 0, 2, 0, 2),
    "mair_el1":     (3, 0, 10, 2, 0),
    # -- timer / thread ------------------------------------------------
    # The generic timer is the only interrupt source a bare-metal image gets
    # for free: no device tree, no driver, just three registers per timer.
    # CTL is the enable/mask/status word, TVAL a countdown the hardware
    # decrements, CVAL an absolute compare value against the counter.
    "cntfrq_el0":   (3, 3, 14, 0, 0),
    "cntpct_el0":   (3, 3, 14, 0, 1),
    "cntvct_el0":   (3, 3, 14, 0, 2),
    "cntp_tval_el0": (3, 3, 14, 2, 0),
    "cntp_ctl_el0": (3, 3, 14, 2, 1),
    "cntp_cval_el0": (3, 3, 14, 2, 2),
    "cntv_tval_el0": (3, 3, 14, 3, 0),
    "cntv_ctl_el0": (3, 3, 14, 3, 1),
    "cntv_cval_el0": (3, 3, 14, 3, 2),
    "cntkctl_el1":  (3, 0, 14, 1, 0),
    "cnthctl_el2":  (3, 4, 14, 1, 0),
    "cntvoff_el2":  (3, 4, 14, 0, 3),
    # Interrupt Status: which of I/F/A is pending, readable without taking
    # the exception. Useful for a polled sanity check before unmasking.
    "isr_el1":      (3, 0, 12, 1, 0),
    "tpidr_el0":    (3, 3, 13, 0, 2),
    "tpidr_el1":    (3, 0, 13, 0, 4),
}


def _sysreg_text(op):
    """The raw text of an operand that should name a system register."""
    if op.kind == "sym":
        return op.sym
    if op.text != "":
        return op.text
    return ""


def _parse_sysreg(name):
    """(op0, op1, CRn, CRm, op2) for a system register name, or None.

    Accepts a known name or the architectural `S<op0>_<op1>_C<n>_C<m>_<op2>`
    escape hatch, so a register missing from the table above is still
    reachable without editing this file.
    """
    low = name.lower()
    if low in _SYSREGS:
        return _SYSREGS[low]
    if low[0:1] != "s":
        return None
    parts = low[1:].split("_")
    if len(parts) != 5:
        return None
    vals = []
    for i in range(5):
        p = parts[i]
        # CRn and CRm are written `c4`; op0/op1/op2 are bare digits.
        if i in (2, 3):
            if p[0:1] != "c":
                return None
            p = p[1:]
        if not _all_digits(p):
            return None
        vals.append(int(p))
    if vals[0] not in (2, 3):
        return None
    return (vals[0], vals[1], vals[2], vals[3], vals[4])


# `msr <pstatefield>, #imm` is a different encoding from `msr <sysreg>, <Xt>`:
# the immediate form addresses a PSTATE field, not a register file entry.
# op1 and op2 select the field; CRn is fixed at 0b0100.
_PSTATE_FIELDS = {
    "spsel":   (0, 5),
    "daifset": (3, 6),
    "daifclr": (3, 7),
}


def _enc_msr_mrs(mnem, ops):
    """`mrs Xt, sysreg` / `msr sysreg, Xt` / `msr pstatefield, #imm`."""
    if len(ops) < 2:
        raise Arm64Error("%s: needs two operands" % mnem)

    if mnem == "mrs":
        rt = _need_reg(ops, 0, "mrs")
        if rt.size != 64:
            raise Arm64Error("mrs: destination must be a 64-bit register")
        name = _sysreg_text(ops[1])
        el = 1
        rt_num = rt.num
    else:
        name = _sysreg_text(ops[0])
        # PSTATE immediate form, e.g. `msr daifset, #2`.
        low = name.lower()
        if low in _PSTATE_FIELDS:
            op1, op2 = _PSTATE_FIELDS[low]
            crm = _need_imm(ops, 1, "msr")
            if crm < 0 or crm > 15:
                raise Arm64Error("msr %s: immediate must be 0..15" % low)
            word = _w((0xD5000000 >> 24, 0xFF, 24), (0, 1, 21),
                      (op1, 0x7, 16), (4, 0xF, 12), (crm, 0xF, 8),
                      (op2, 0x7, 5), (0x1F, 0x1F, 0))
            return word, []
        rt = _need_reg(ops, 1, "msr")
        if rt.size != 64:
            raise Arm64Error("msr: source must be a 64-bit register")
        el = 0
        rt_num = rt.num

    if name == "":
        raise Arm64Error("%s: expected a system register name" % mnem)
    fields = _parse_sysreg(name)
    if fields is None:
        raise Arm64Error("%s: unknown system register '%s'" % (mnem, name))
    op0, op1, crn, crm, op2 = fields
    # 1101 0101 00 L op0 op1 CRn CRm op2 Rt -- L=1 reads (mrs), L=0 writes.
    word = _w((0xD5000000 >> 24, 0xFF, 24), (el, 1, 21), (op0, 0x3, 19),
              (op1, 0x7, 16), (crn, 0xF, 12), (crm, 0xF, 8),
              (op2, 0x7, 5), (rt_num, 0x1F, 0))
    return word, []


# Barrier options, in encoding order; `sy` (full system) is the default and
# the only one the boot path needs, but the rest are cheap to accept.
_BARRIER_OPTS = {
    "oshld": 1, "oshst": 2, "osh": 3,
    "nshld": 5, "nshst": 6, "nsh": 7,
    "ishld": 9, "ishst": 10, "ish": 11,
    "ld": 13, "st": 14, "sy": 15,
}
_BARRIERS = {"dsb": 4, "dmb": 5, "isb": 6}


def _enc_barrier(mnem, ops):
    """`isb`, `dsb <opt>`, `dmb <opt>` -- CRm carries the option."""
    crm = 15                                    # `sy` when omitted
    if len(ops) > 0:
        if ops[0].kind == "imm":
            crm = ops[0].val
        else:
            opt = _sysreg_text(ops[0]).lower()
            if opt not in _BARRIER_OPTS:
                raise Arm64Error("%s: unknown barrier option '%s'"
                                 % (mnem, opt))
            crm = _BARRIER_OPTS[opt]
    if crm < 0 or crm > 15:
        raise Arm64Error("%s: barrier option out of range" % mnem)
    word = _w((0xD5033000 >> 12, 0xFFFFF, 12), (crm, 0xF, 8),
              (_BARRIERS[mnem], 0x7, 5), (0x1F, 0x1F, 0))
    return word, []


# The hint space. `nop` is hint 0 and already has its own encoder; the rest
# matter for parking a core (`wfi`/`wfe`) in the boot path.
_HINTS = {"nop": 0, "yield": 1, "wfe": 2, "wfi": 3, "sev": 4, "sevl": 5}


def _enc_hint(mnem, ops):
    # Shifting the full constant right by 12 to get the base drops the fixed
    # Rt=31 in the low five bits, so it is restored explicitly here -- the
    # same trap the _FP_DP2 table comments warn about.
    return _w((0xD503201F >> 12, 0xFFFFF, 12), (_HINTS[mnem], 0x7F, 5),
              (0x1F, 0x1F, 0)), []


def _enc_eret(ops):
    return 0xD69F03E0, []


# TLB and cache maintenance share one encoding with the register-argument
# system instructions: 1101 0101 0000 1 op1 CRn CRm op2 Rt, with Rt = 31 when
# the operation takes no register.
_SYS_OPS = {
    ("tlbi", "vmalle1"):   (0, 8, 7, 0),
    ("tlbi", "vmalle1is"): (0, 8, 3, 0),
    ("tlbi", "alle1"):     (4, 8, 7, 4),
    ("tlbi", "alle1is"):   (4, 8, 3, 4),
    ("tlbi", "alle2"):     (4, 8, 7, 0),
    ("tlbi", "vae1"):      (0, 8, 7, 1),
    ("tlbi", "vae1is"):    (0, 8, 3, 1),
    ("tlbi", "aside1"):    (0, 8, 7, 2),
    ("tlbi", "vaae1"):     (0, 8, 7, 3),
    ("ic",   "ialluis"):   (0, 7, 1, 0),
    ("ic",   "iallu"):     (0, 7, 5, 0),
    ("ic",   "ivau"):      (3, 7, 5, 1),
    ("dc",   "ivac"):      (0, 7, 6, 1),
    ("dc",   "isw"):       (0, 7, 6, 2),
    ("dc",   "csw"):       (0, 7, 10, 2),
    ("dc",   "cisw"):      (0, 7, 14, 2),
    ("dc",   "zva"):       (3, 7, 4, 1),
    ("dc",   "cvac"):      (3, 7, 10, 1),
    ("dc",   "cvau"):      (3, 7, 11, 1),
    ("dc",   "civac"):     (3, 7, 14, 1),
}
# Operations that take a register argument; the rest encode Rt = 31.
_SYS_NEEDS_REG = {
    ("tlbi", "vae1"), ("tlbi", "vae1is"), ("tlbi", "aside1"),
    ("tlbi", "vaae1"), ("ic", "ivau"),
    ("dc", "ivac"), ("dc", "isw"), ("dc", "csw"), ("dc", "cisw"),
    ("dc", "zva"), ("dc", "cvac"), ("dc", "cvau"), ("dc", "civac"),
}


def _enc_sysinstr(mnem, ops):
    """`tlbi <op>[, Xt]`, `ic <op>[, Xt]`, `dc <op>, Xt`."""
    if len(ops) < 1:
        raise Arm64Error("%s: needs an operation" % mnem)
    opname = _sysreg_text(ops[0]).lower()
    key = (mnem, opname)
    if key not in _SYS_OPS:
        raise Arm64Error("%s: unsupported operation '%s'" % (mnem, opname))
    op1, crn, crm, op2 = _SYS_OPS[key]
    rt = 31
    if key in _SYS_NEEDS_REG:
        if len(ops) < 2:
            raise Arm64Error("%s %s: needs a register operand"
                             % (mnem, opname))
        rt = _need_reg(ops, 1, mnem).num
    elif len(ops) > 1:
        raise Arm64Error("%s %s: takes no register operand" % (mnem, opname))
    word = _w((0xD5080000 >> 19, 0x1FFF, 19), (op1, 0x7, 16), (crn, 0xF, 12),
              (crm, 0xF, 8), (op2, 0x7, 5), (rt, 0x1F, 0))
    return word, []


def _enc_adr(ops):
    """`adr Xd, label` -- PC-relative, +/-1 MiB, no page rounding.

    Unlike `adrp` this needs no companion `add`, which is what makes it the
    natural way for the boot stub to find the stack top and the vector table
    before any of the addressing machinery is up.
    """
    rd = _need_reg(ops, 0, "adr")
    if rd.size != 64:
        raise Arm64Error("adr: destination must be a 64-bit register")
    if len(ops) < 2 or _not_label(ops, 1):
        raise Arm64Error("adr: second operand must be a symbol")
    word = _w((0x10000000 >> 24, 0xFF, 24), (rd.num, 0x1F, 0))
    return word, [rasm.Reloc(0, ops[1].sym, 4, True, 0, False, "adr_prel_lo21")]


# ---------------------------------------------------------------------------
# Top-level dispatch
# ---------------------------------------------------------------------------
def encode(mnem, ops):
    """Encode one instruction. Returns (word, relocs); the caller turns the
    word into bytes and fixes up reloc offsets."""
    m = mnem.lower()
    if m[0:2] == "b." and len(m) > 2:
        return _enc_bcond(m[2:], ops)
    if m == "b" or m == "bl":
        return _enc_branch(m, ops)
    if m == "br" or m == "blr":
        return _enc_br(m, ops)
    if m == "ret":
        return _enc_ret(ops)
    if m == "nop":
        return _enc_nop()
    if m == "svc":
        return _enc_svc(ops)
    if m == "adrp":
        return _enc_adrp(ops)
    if m == "adr":
        return _enc_adr(ops)
    if m == "mrs" or m == "msr":
        return _enc_msr_mrs(m, ops)
    if m in _BARRIERS:
        return _enc_barrier(m, ops)
    if m in _HINTS:
        return _enc_hint(m, ops)
    if m == "eret":
        return _enc_eret(ops)
    if m == "tlbi" or m == "ic" or m == "dc":
        return _enc_sysinstr(m, ops)
    if m == "cbz" or m == "cbnz":
        return _enc_cbz(m, ops)
    if m == "tbz" or m == "tbnz":
        return _enc_tbz(m, ops)
    if m == "mov":
        return _enc_mov(m, ops)
    if m == "mvn":
        return _enc_mvn(ops)
    if m == "neg":
        return _enc_neg(ops)
    if m == "cmp" or m == "cmn":
        return _enc_cmp(m, ops)
    if m == "tst":
        return _enc_tst(ops)
    if m in _ADDSUB_IMM and m in _ADDSUB_REG:
        return _enc_addsub(m, ops, _ADDSUB_IMM[m], _ADDSUB_REG[m])
    if m in _LOGIC_REG:
        return _enc_logic(m, ops)
    if m in _MOVW:
        return _enc_movw(m, ops)
    if m == "mul" or m == "madd" or m == "msub" or m == "mneg":
        return _enc_madd(m, ops)
    if m in _DP2:
        return _enc_dp2(m, ops, _DP2[m])
    if m == "lsl" or m == "lsr" or m == "asr":
        # Register-operand form is the *v variant; immediate is a bitfield.
        if len(ops) > 2 and ops[2].kind == "reg":
            return _enc_dp2(m + "v", ops, _DP2[m + "v"])
        return _enc_bitfield(m, ops)
    if m in ("sxtw", "sxtb", "sxth", "uxtb", "uxth"):
        return _enc_bitfield(m, ops)
    if m == "cset":
        return _enc_cset(ops)
    if m in ("csel", "csinc", "csinv", "csneg"):
        return _enc_csinc(m, ops)
    if m in ("ldr", "str", "ldrb", "strb", "ldrh", "strh",
             "ldrsb", "ldrsh", "ldrsw"):
        return _enc_ldst(m, ops)
    if m == "ldp" or m == "stp":
        return _enc_ldstp(m, ops)
    if m in _FP_DP2:
        return _enc_fp_dp2(m, ops)
    if m == "fmov":
        return _enc_fmov(ops)
    if m in ("fabs", "fneg", "fsqrt"):
        return _enc_fneg(m, ops)
    if m == "fcmp" or m == "fcmpe":
        return _enc_fcmp(m, ops)
    if m == "fcvt":
        return _enc_fcvt(ops)
    if m in _FCVT_INT:
        return _enc_fcvt_int(m, ops)
    raise Arm64Error("unsupported aarch64 instruction '%s'" % mnem)


def encode_line(mnem, rest):
    """Parse an operand string and encode. Returns (bytes4, relocs)."""
    ops = []
    for tok in split_operands(rest):
        ops.append(parse_operand(tok))
    word, relocs = encode(mnem, ops)
    return _bytes_of(word), relocs
