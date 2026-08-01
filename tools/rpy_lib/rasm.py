"""rasm -- a tiny x86-64 machine-code assembler, written in RPython-friendly
Python.

Purpose: replace the external GNU assembler (`as`) in the ShivyCX toolchain so
the compiler becomes fully self-contained. ShivyCX emits Intel-syntax x86-64
assembly text; this module encodes that text to machine-code bytes (and, later,
into an ELF relocatable object).

This first cut covers the instruction/operand vocabulary ShivyCX actually
emits: mov, movsx, lea, push, pop, add, sub, imul, idiv, cqo, and, or, xor,
shifts, cmp, test, call, ret, jmp and the Jcc family, with register / immediate
/ memory (base + index*scale + disp, RIP-relative, and symbol) operands.

The encoding logic (REX / ModRM / SIB / displacement / immediate layout) follows
the same model as pycca's assembler (campagnola/pycca) and the Intel SDM, but is
rewritten in a flat, statically-typed style so it can be translated by RPython
(and, eventually, run on minipy itself).

Style constraints for RPython compatibility:
  * no metaclasses, decorators, generators, or **kwargs;
  * uniform object shapes (one Operand class, not a union of types);
  * explicit integer math; bytes built as lists of ints then joined.
"""


# --------------------------------------------------------------------------
# Registers
# --------------------------------------------------------------------------
# Each general-purpose register maps to (encoding value 0..15, size in bits).
# The low 3 bits go in ModRM/SIB; value >= 8 additionally sets a REX extension
# bit (R, X, or B depending on the field). rsp/rbp (and r12/r13) have special
# addressing meaning handled in the memory encoder.

_REG64 = ["rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi",
          "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"]
_REG32 = ["eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi",
          "r8d", "r9d", "r10d", "r11d", "r12d", "r13d", "r14d", "r15d"]
_REG16 = ["ax", "cx", "dx", "bx", "sp", "bp", "si", "di",
          "r8w", "r9w", "r10w", "r11w", "r12w", "r13w", "r14w", "r15w"]
_REG8 = ["al", "cl", "dl", "bl", "spl", "bpl", "sil", "dil",
         "r8b", "r9b", "r10b", "r11b", "r12b", "r13b", "r14b", "r15b"]
# legacy high-byte names (ah/ch/dh/bh) are not emitted by ShivyCX; omit them.
_XMM = ["xmm0", "xmm1", "xmm2", "xmm3", "xmm4", "xmm5", "xmm6", "xmm7",
        "xmm8", "xmm9", "xmm10", "xmm11", "xmm12", "xmm13", "xmm14", "xmm15"]

# name -> (val, bits)
REGISTERS = {}

# Control, debug and segment registers. They are kept out of REGISTERS because
# they share the ModRM.reg field with the general registers but are only legal
# for a handful of opcodes; a separate table keeps `is_register` honest.
CREGS = {"cr0": 0, "cr1": 1, "cr2": 2, "cr3": 3, "cr4": 4, "cr8": 8}
DREGS = {"dr0": 0, "dr1": 1, "dr2": 2, "dr3": 3, "dr6": 6, "dr7": 7}
SREGS = {"es": 0, "cs": 1, "ss": 2, "ds": 3, "fs": 4, "gs": 5}

# Current assembly mode: 64, 32 or 16 bits. A bare-metal kernel starts in
# 32-bit protected mode (that is what Multiboot hands over) and switches to
# long mode itself, so one file legitimately contains both -- `.code32` and
# `.code64` move this between them. It affects REX (illegal outside 64-bit
# mode) and which address size needs the 0x67 prefix.
MODE = 64


def set_mode(bits):
    global MODE
    MODE = bits


def get_mode():
    return MODE


def _init_registers():
    tables = [(_REG64, 64), (_REG32, 32), (_REG16, 16), (_REG8, 8), (_XMM, 128)]
    for pair in tables:
        names = pair[0]
        bits = pair[1]
        i = 0
        while i < len(names):
            REGISTERS[names[i]] = (i, bits)
            i += 1


_init_registers()


def reg_val(name):
    return REGISTERS[name][0]


def reg_bits(name):
    return REGISTERS[name][1]


def is_register(name):
    return name in REGISTERS


# --------------------------------------------------------------------------
# REX / ModRM / SIB primitives
# --------------------------------------------------------------------------
REX_BASE = 0x40
REX_W = 0x08     # 64-bit operand size
REX_R = 0x04     # extension of ModRM.reg
REX_X = 0x02     # extension of SIB.index
REX_B = 0x01     # extension of ModRM.rm / SIB.base / opcode reg


def modrm(mod, reg, rm):
    """mod: 0..3; reg,rm: 0..7 (low 3 bits already masked)."""
    return ((mod & 3) << 6) | ((reg & 7) << 3) | (rm & 7)


def sib(scale_log2, index, base):
    """scale_log2: 0..3 (scale 1/2/4/8); index,base: 0..7."""
    return ((scale_log2 & 3) << 6) | ((index & 7) << 3) | (base & 7)


def scale_to_log2(scale):
    if scale == 1:
        return 0
    if scale == 2:
        return 1
    if scale == 4:
        return 2
    if scale == 8:
        return 3
    raise AssemblerError("bad scale %d" % scale)


def pack_le(value, nbytes):
    """Little-endian encode `value` into `nbytes` bytes (list of ints)."""
    out = []
    v = value & ((1 << (8 * nbytes)) - 1)
    i = 0
    while i < nbytes:
        out.append(v & 0xFF)
        v >>= 8
        i += 1
    return out


def fits_int8(v):
    return -128 <= v <= 127


def fits_int32(v):
    return -2147483648 <= v <= 2147483647


class AssemblerError(Exception):
    pass


# --------------------------------------------------------------------------
# Operands
# --------------------------------------------------------------------------
# One uniform class describes every operand. `kind` selects the interpretation:
#   "reg" : a register           -> reg_v (0..15), size (bits)
#   "imm" : an immediate         -> imm (int)
#   "mem" : a memory reference    -> base/index/scale/disp/sym/rip, size (bits)
# For memory, base/index are register values 0..15 or -1 when absent. `sym` is
# a symbol name ("" if none); when set the displacement is a relocation target.
# `rip` marks RIP-relative addressing ([rip + disp] / [symbol] under -fpic).

MEM_NONE = -1


class Operand(object):
    def __init__(self):
        self.kind = "reg"
        self.reg_v = 0
        self.size = 0          # operand size in bits (8/16/32/64)
        self.imm = 0
        self.base = MEM_NONE
        self.index = MEM_NONE
        self.scale = 1
        self.disp = 0
        self.sym = ""
        self.rip = False
        self.asize = 0         # address size implied by base/index registers


def op_reg(name):
    o = Operand()
    o.kind = "reg"
    o.reg_v = reg_val(name)
    o.size = reg_bits(name)
    return o


def op_special(name):
    """A control, debug or segment register operand."""
    o = Operand()
    if name in CREGS:
        o.kind = "creg"
        o.reg_v = CREGS[name]
        o.size = 64
    elif name in DREGS:
        o.kind = "dreg"
        o.reg_v = DREGS[name]
        o.size = 64
    else:
        o.kind = "sreg"
        o.reg_v = SREGS[name]
        o.size = 16
    return o


def is_special_register(name):
    return name in CREGS or name in DREGS or name in SREGS


def op_imm(value):
    o = Operand()
    o.kind = "imm"
    o.imm = value
    return o


def op_mem(size, base, index, scale, disp, sym, rip, asize=0):
    o = Operand()
    o.kind = "mem"
    o.asize = asize
    o.size = size
    o.base = base
    o.index = index
    o.scale = scale
    o.disp = disp
    o.sym = sym
    o.rip = rip
    return o


# A relocation the caller (ELF writer) must patch: at byte offset `where`,
# a `size`-byte field references `sym` with addend `add`; `pcrel` marks
# RIP/relative (R_X86_64_PC32) vs absolute (R_X86_64_32/32S).
class Reloc(object):
    def __init__(self, where, sym, size, pcrel, add, signed=True):
        self.where = where
        self.sym = sym
        self.size = size
        self.pcrel = pcrel
        self.add = add
        # gas emits R_X86_64_32 (unsigned) for a symbol in a data word and
        # R_X86_64_32S for one used as a sign-extended immediate.
        self.signed = signed


# --------------------------------------------------------------------------
# ModRM/SIB encoding for a (reg-field, rm-operand) pair
# --------------------------------------------------------------------------
# Returns (rex_bits, byte_list, relocs). `reg_field` is the 0..15 value that
# goes in ModRM.reg (a register number or an opcode extension /N). `rm` is an
# Operand that is either a register or a memory reference. `start` is the byte
# offset of the ModRM byte within the whole instruction, used to place any
# relocation for a symbolic displacement.

def encode_rm(reg_field, rm, start):
    global _PENDING_ASIZE
    _PENDING_ASIZE = rm.asize if rm.kind == "mem" else 0
    rex = 0
    if reg_field >= 8:
        rex |= REX_R
    rf = reg_field & 7

    if rm.kind == "reg":
        if rm.reg_v >= 8:
            rex |= REX_B
        return rex, [modrm(3, rf, rm.reg_v & 7)], []

    # memory operand
    relocs = []
    out = []

    # RIP-relative (or symbolic under PIC): mod=00, rm=101, disp32.
    if rm.rip or (rm.sym != "" and rm.base == MEM_NONE and rm.index == MEM_NONE and rm.rip):
        out.append(modrm(0, rf, 5))
        # disp32 follows the ModRM byte
        if rm.sym != "":
            # PC-relative addend: the processor adds the disp32 to the address
            # of the *next* instruction, so the addend must step back over the
            # four displacement bytes (and, in _assemble, over any trailing
            # immediate) to make S + A - P land on the symbol.
            relocs.append(Reloc(start + 1, rm.sym, 4, True, rm.disp - 4))
            out.extend(pack_le(0, 4))
        else:
            out.extend(pack_le(rm.disp, 4))
        return rex, out, relocs

    has_index = rm.index != MEM_NONE
    has_base = rm.base != MEM_NONE

    # absolute [disp32] or [sym] with no base/index: mod=00, rm=100, SIB with
    # base=101 index=100 (none), disp32.
    if not has_base and not has_index:
        if MODE != 64:
            # 32-bit: mod=00, rm=101 is a plain disp32 absolute address.
            out.append(modrm(0, rf, 5))
            if rm.sym != "":
                relocs.append(Reloc(start + 1, rm.sym, 4, False, rm.disp))
                out.extend(pack_le(0, 4))
            else:
                out.extend(pack_le(rm.disp, 4))
            return rex, out, relocs
        out.append(modrm(0, rf, 4))
        out.append(sib(0, 4, 5))
        if rm.sym != "":
            relocs.append(Reloc(start + 2, rm.sym, 4, False, rm.disp))
            out.extend(pack_le(0, 4))
        else:
            out.extend(pack_le(rm.disp, 4))
        return rex, out, relocs

    need_sib = has_index or ((rm.base & 7) == 4)  # rsp/r12 base forces SIB

    if not need_sib:
        b = rm.base
        if b >= 8:
            rex |= REX_B
        # choose mod by displacement; rbp/r13 (rm==5) cannot use mod=00
        if rm.disp == 0 and (b & 7) != 5 and rm.sym == "":
            out.append(modrm(0, rf, b & 7))
        elif rm.sym == "" and fits_int8(rm.disp):
            out.append(modrm(1, rf, b & 7))
            out.extend(pack_le(rm.disp, 1))
        else:
            out.append(modrm(2, rf, b & 7))
            if rm.sym != "":
                relocs.append(Reloc(start + 1, rm.sym, 4, False, rm.disp))
                out.extend(pack_le(0, 4))
            else:
                out.extend(pack_le(rm.disp, 4))
        return rex, out, relocs

    # SIB form
    idx = 4  # 4 == "no index"
    if has_index:
        idx = rm.index
        if idx >= 8:
            rex |= REX_X
    base = 5  # 5 with mod=00 means "no base" (disp32 only)
    mod = 0
    if has_base:
        base = rm.base
        if base >= 8:
            rex |= REX_B
        if rm.disp == 0 and (base & 7) != 5 and rm.sym == "":
            mod = 0
        elif rm.sym == "" and fits_int8(rm.disp):
            mod = 1
        else:
            mod = 2
    out.append(modrm(mod, rf, 4))
    out.append(sib(scale_to_log2(rm.scale), idx & 7, base & 7))
    if mod == 1:
        out.extend(pack_le(rm.disp, 1))
    elif mod == 2 or (not has_base):
        if rm.sym != "":
            relocs.append(Reloc(start + 2, rm.sym, 4, False, rm.disp))
            out.extend(pack_le(0, 4))
        else:
            out.extend(pack_le(rm.disp, 4))
    return rex, out, relocs


def emit_rex(rex, size, force):
    """Return the REX byte list (0 or 1 bytes). `size`==64 sets REX.W."""
    if MODE != 64:
        # REX does not exist in 32/16-bit mode -- the same byte range is inc/dec
        # there -- and r8-r15 and 64-bit operands are simply unavailable.
        if rex != 0 or size == 64:
            raise AssemblerError(
                "64-bit register or operand used in .code%d" % MODE)
        return []
    if size == 64:
        rex |= REX_W
    if rex != 0 or force:
        return [REX_BASE | rex]
    return []


# --------------------------------------------------------------------------
# Instruction encoders
# --------------------------------------------------------------------------
# encode(mnem, ops) -> (byte_list, reloc_list). `ops` is a list of Operand.
# Operand-size handling: 16-bit adds a 0x66 prefix; 64-bit sets REX.W; 8-bit
# selects the low opcode (even) byte. ShivyCX emits mostly 32/64-bit forms.

# mnem -> (opcode_mr, opcode_rm, ext) for the classic ALU group. The immediate
# forms use 0x81 /ext (imm32) or the sign-extended 0x83 /ext (imm8).
# (opcode_mr, opcode_rm, ext, acc8) -- acc8 is the AL,imm opcode; the
# eAX/rAX,imm form is acc8+1. gas uses these shorter accumulator encodings when
# the destination is AL/AX/EAX/RAX and the immediate needs a full imm32.
_ALU = {
    "add": (0x01, 0x03, 0, 0x04),
    "or":  (0x09, 0x0B, 1, 0x0C),
    "and": (0x21, 0x23, 4, 0x24),
    "sub": (0x29, 0x2B, 5, 0x2C),
    "xor": (0x31, 0x33, 6, 0x34),
    "cmp": (0x39, 0x3B, 7, 0x3C),
    "test": (0x85, 0x85, 0, 0xA8),
}

# Jcc: condition mnemonic -> tttn nibble (0x0F 0x8x rel32).
_JCC = {
    "jo": 0x0, "jno": 0x1, "jb": 0x2, "jae": 0x3, "je": 0x4, "jz": 0x4,
    "jne": 0x5, "jnz": 0x5, "jbe": 0x6, "ja": 0x7, "js": 0x8, "jns": 0x9,
    "jp": 0xA, "jnp": 0xB, "jl": 0xC, "jge": 0xD, "jle": 0xE, "jg": 0xF,
}

# shift mnemonic -> ext (/N in 0xC1 /N ib, 0xD3 /N cl, 0xD1 /N by-1)
_SHIFT = {"rol": 0, "ror": 1, "sal": 4, "shl": 4, "shr": 5, "sar": 7}

# setcc (0F 90+tttn /0 r/m8) and cmovcc (0F 40+tttn /r) share the Jcc
# condition nibbles; derive both tables from _JCC so they stay in sync.
_SETCC = {}
_CMOV = {}


def _init_cond_tables():
    for k in _JCC:
        cond = k[1:]                 # "jne" -> "ne"
        _SETCC["set" + cond] = _JCC[k]
        _CMOV["cmov" + cond] = _JCC[k]
    # gas also accepts the "not-equal-or" spellings below; map the common ones.
    _SETCC["setnb"] = _JCC["jae"]
    _SETCC["setna"] = _JCC["jbe"]
    _SETCC["setnae"] = _JCC["jb"]
    _SETCC["setnbe"] = _JCC["ja"]
    _SETCC["setng"] = _JCC["jle"]
    _SETCC["setnge"] = _JCC["jl"]
    _SETCC["setnl"] = _JCC["jge"]
    _SETCC["setnle"] = _JCC["jg"]
    _SETCC["setc"] = _JCC["jb"]
    _SETCC["setnc"] = _JCC["jae"]
    _CMOV["cmovnb"] = _JCC["jae"]
    _CMOV["cmovna"] = _JCC["jbe"]
    _CMOV["cmovnae"] = _JCC["jb"]
    _CMOV["cmovnbe"] = _JCC["ja"]
    _CMOV["cmovng"] = _JCC["jle"]
    _CMOV["cmovnge"] = _JCC["jl"]
    _CMOV["cmovnl"] = _JCC["jge"]
    _CMOV["cmovnle"] = _JCC["jg"]
    _CMOV["cmovc"] = _JCC["jb"]
    _CMOV["cmovnc"] = _JCC["jae"]


_init_cond_tables()

# Instructions with no operands: mnemonic -> fixed opcode bytes.
_NULLARY = {
    "ret": [0xC3],
    "retq": [0xC3],
    "leave": [0xC9],
    "nop": [0x90],
    "cqo": [0x48, 0x99],
    "cdq": [0x99],
    "cdqe": [0x48, 0x98],
    "cwde": [0x98],
    "syscall": [0x0F, 0x05],
    "sysret": [0x0F, 0x07],
    "hlt": [0xF4],
    "int3": [0xCC],
    "ud2": [0x0F, 0x0B],
    "cld": [0xFC],
    "std": [0xFD],
    "cli": [0xFA],
    "sti": [0xFB],
    "pause": [0xF3, 0x90],
    "rdtsc": [0x0F, 0x31],
    "rdmsr": [0x0F, 0x32],
    "wrmsr": [0x0F, 0x30],
    "iret": [0xCF],
    "iretq": [0x48, 0xCF],
    "iretd": [0xCF],
    "leave": [0xC9],
    "clts": [0x0F, 0x06],
    "invd": [0x0F, 0x08],
    "wbinvd": [0x0F, 0x09],
    "sysenter": [0x0F, 0x34],
    "sysexit": [0x0F, 0x35],
    "stosd": [0xAB],
    "lodsd": [0xAD],
    "cpuid": [0x0F, 0xA2],
    "endbr64": [0xF3, 0x0F, 0x1E, 0xFA],
    "lfence": [0x0F, 0xAE, 0xE8],
    "mfence": [0x0F, 0xAE, 0xF0],
    "sfence": [0x0F, 0xAE, 0xF8],
    "movsb": [0xA4],
    "movsw": [0x66, 0xA5],
    "movsl": [0xA5],
    "movsq": [0x48, 0xA5],
    "stosb": [0xAA],
    "stosw": [0x66, 0xAB],
    "stosl": [0xAB],
    "stosq": [0x48, 0xAB],
    "lodsb": [0xAC],
    "lodsq": [0x48, 0xAD],
    "scasb": [0xAE],
    "cmpsb": [0xA6],
}

# Legacy prefixes that may precede a mnemonic ("rep movsb", "lock add ...").
_PREFIX = {"rep": 0xF3, "repe": 0xF3, "repz": 0xF3, "repne": 0xF2,
           "repnz": 0xF2, "lock": 0xF0}

# SSE scalar arithmetic: mnem -> (mandatory prefix, 0F opcode). reg=dst(xmm),
# rm=src; no REX.W (size implied by the prefix).
_SSE_ARITH = {
    "addsd": (0xF2, 0x58), "subsd": (0xF2, 0x5C),
    "mulsd": (0xF2, 0x59), "divsd": (0xF2, 0x5E),
    "addss": (0xF3, 0x58), "subss": (0xF3, 0x5C),
    "mulss": (0xF3, 0x59), "divss": (0xF3, 0x5E),
    "sqrtsd": (0xF2, 0x51), "sqrtss": (0xF3, 0x51),
    "minsd": (0xF2, 0x5D), "maxsd": (0xF2, 0x5F),
    "minss": (0xF3, 0x5D), "maxss": (0xF3, 0x5F),
    "cvtsd2ss": (0xF2, 0x5A), "cvtss2sd": (0xF3, 0x5A),
}
# SSE compare (set EFLAGS): reg=dst(xmm), rm=src.
_SSE_CMP = {
    "ucomisd": (0x66, 0x2E), "ucomiss": (0x00, 0x2E),
    "comisd": (0x66, 0x2F), "comiss": (0x00, 0x2F),
}
# packed logic used for float sign/zero tricks.
_SSE_LOGIC = {
    "xorps": (0x00, 0x57), "xorpd": (0x66, 0x57),
    "andps": (0x00, 0x54), "andpd": (0x66, 0x54),
    "orps": (0x00, 0x56), "orpd": (0x66, 0x56),
    "pxor": (0x66, 0xEF),
}


def _pfx_size(size):
    """0x66 toggles operand size away from the mode's default."""
    if size == 16 and MODE != 16:
        return [0x66]
    if size == 32 and MODE == 16:
        return [0x66]
    return []


def _pfx_addr(op):
    """0x67 toggles address size away from the mode's default.

    A 32-bit base/index register is the default in 32-bit mode but needs the
    prefix in 64-bit mode, and vice versa -- `mov %eax,(%edi)` assembles
    without a prefix under .code32 and with 0x67 under .code64.
    """
    if op is None or op.kind != "mem" or op.asize == 0:
        return []
    if MODE == 64 and op.asize == 32:
        return [0x67]
    if MODE == 32 and op.asize == 16:
        return [0x67]
    return []


def _needs_rex8(op):
    """spl/bpl/sil/dil (8-bit regs with value 4..7) require a REX prefix to be
    encodable at all; without one, values 4..7 decode as ah/ch/dh/bh."""
    return op.kind == "reg" and op.size == 8 and 4 <= op.reg_v <= 7


_PENDING_ASIZE = 0


def _assemble(size, rex, opcode_bytes, mrm, imm_bytes, rl, force=False,
              amem=None):
    """Build a full instruction from parts and fix reloc offsets.

    Layout: [66 prefix?] [REX?] [opcode...] [ModRM/SIB/disp] [imm]. Relocations
    returned by encode_rm are relative to the ModRM byte (start=0); shift them
    by the length of everything that precedes ModRM.
    """
    out = []
    out.extend(_pfx_size(size))
    global _PENDING_ASIZE
    if amem is None and _PENDING_ASIZE != 0:
        a = Operand()
        a.kind = "mem"
        a.asize = _PENDING_ASIZE
        out.extend(_pfx_addr(a))
    else:
        out.extend(_pfx_addr(amem))
    _PENDING_ASIZE = 0
    out.extend(emit_rex(rex, size, force))
    out.extend(opcode_bytes)
    pre = len(out)
    out.extend(mrm)
    out.extend(imm_bytes)
    relocs = []
    i = 0
    while i < len(rl):
        r = rl[i]
        add = r.add
        if r.pcrel:
            # any immediate sits between the displacement field and the end of
            # the instruction, which the PC-relative addend must also skip
            add -= len(imm_bytes)
        relocs.append(Reloc(r.where + pre, r.sym, r.size, r.pcrel, add))
        i += 1
    return out, relocs


def encode(mnem, ops):
    # legacy prefix ("rep movsb", "lock cmpxchg ..."): encode the rest of the
    # instruction and prepend the prefix byte, shifting any relocation offsets.
    sp = mnem.find(" ")
    if sp > 0 and mnem[:sp] in _PREFIX:
        body, rl = encode(mnem[sp + 1:].strip(), ops)
        out = [_PREFIX[mnem[:sp]]]
        out.extend(body)
        shifted = []
        i = 0
        while i < len(rl):
            r = rl[i]
            shifted.append(Reloc(r.where + 1, r.sym, r.size, r.pcrel, r.add))
            i += 1
        return out, shifted

    if mnem in _NULLARY:
        return _NULLARY[mnem], []

    if mnem == "int":
        return [0xCD, ops[0].imm & 0xFF], []
    if mnem in _SETCC:
        return _encode_setcc(_SETCC[mnem], ops[0])
    if mnem in _CMOV:
        return _encode_cmov(_CMOV[mnem], ops[0], ops[1])
    if mnem == "inc":
        return _encode_incdec(ops[0], 0)
    if mnem == "dec":
        return _encode_incdec(ops[0], 1)
    if mnem == "xchg":
        return _encode_xchg(ops[0], ops[1])
    if mnem == "bswap":
        o = ops[0]
        rex = REX_B if o.reg_v >= 8 else 0
        return _assemble(o.size, rex, [0x0F, 0xC8 | (o.reg_v & 7)], [], [], [])
    if mnem == "movabs":
        return _encode_movabs(ops[0], ops[1])
    if mnem == "lgdt":
        return _encode_m_group(ops[0], 2)
    if mnem == "lidt":
        return _encode_m_group(ops[0], 3)
    if mnem == "sgdt":
        return _encode_m_group(ops[0], 0)
    if mnem == "sidt":
        return _encode_m_group(ops[0], 1)
    if mnem == "invlpg":
        return _encode_m_group(ops[0], 7)
    if mnem == "ljmp":
        return _encode_ljmp(ops)
    if mnem == "in":
        return _encode_inout(ops, False)
    if mnem == "out":
        return _encode_inout(ops, True)

    if mnem == "push":
        return _encode_pushpop(ops[0], True)
    if mnem == "pop":
        return _encode_pushpop(ops[0], False)

    if mnem == "call":
        return _encode_calljmp(ops[0], True)
    if mnem == "jmp":
        return _encode_calljmp(ops[0], False)
    if mnem in _JCC:
        return _encode_jcc(_JCC[mnem], ops[0])

    if mnem == "lea":
        return _encode_lea(ops[0], ops[1])

    if mnem in _ALU:
        return _encode_alu(mnem, ops[0], ops[1])
    if mnem == "mov":
        return _encode_mov(ops[0], ops[1])

    if mnem == "imul":
        return _encode_imul(ops)
    if mnem == "idiv":
        return _encode_unary_group3(ops[0], 7)
    if mnem == "div":
        return _encode_unary_group3(ops[0], 6)
    if mnem == "imul1":
        return _encode_unary_group3(ops[0], 5)
    if mnem == "mul":
        return _encode_unary_group3(ops[0], 4)
    if mnem == "neg":
        return _encode_unary_group3(ops[0], 3)
    if mnem == "not":
        return _encode_unary_group3(ops[0], 2)

    if mnem in _SHIFT:
        return _encode_shift(_SHIFT[mnem], ops)

    if mnem == "movsx" or mnem == "movsxd":
        return _encode_movx(ops[0], ops[1], True)
    if mnem == "movzx":
        return _encode_movx(ops[0], ops[1], False)

    # ---- SSE scalar floating point ----
    if mnem == "movsd":
        return _encode_sse_mov(0xF2, ops[0], ops[1])
    if mnem == "movss":
        return _encode_sse_mov(0xF3, ops[0], ops[1])
    if mnem == "movaps":
        return _encode_sse_movalign(0x00, ops[0], ops[1])
    if mnem == "movapd":
        return _encode_sse_movalign(0x66, ops[0], ops[1])
    if mnem in _SSE_ARITH:
        p = _SSE_ARITH[mnem]
        return _sse(p[0], p[1], ops[0], ops[1], False)
    if mnem in _SSE_CMP:
        p = _SSE_CMP[mnem]
        return _sse(p[0], p[1], ops[0], ops[1], False)
    if mnem in _SSE_LOGIC:
        p = _SSE_LOGIC[mnem]
        return _sse(p[0], p[1], ops[0], ops[1], False)
    if mnem == "cvtsi2sd" or mnem == "cvtsi2ss":
        prefix = 0xF2 if mnem == "cvtsi2sd" else 0xF3
        return _sse(prefix, 0x2A, ops[0], ops[1], ops[1].size == 64)
    if mnem in ("cvttsd2si", "cvttss2si", "cvtsd2si", "cvtss2si"):
        prefix = 0xF2 if (mnem == "cvttsd2si" or mnem == "cvtsd2si") else 0xF3
        opc = 0x2C if mnem[0:4] == "cvtt" else 0x2D
        return _sse(prefix, opc, ops[0], ops[1], ops[0].size == 64)
    if mnem == "movq":
        return _encode_movq(ops[0], ops[1])
    if mnem == "movd":
        return _encode_movd(ops[0], ops[1])

    raise AssemblerError("unsupported mnemonic: %s" % mnem)


def _norm_imm(value, size):
    """Reinterpret an immediate written as unsigned into its signed form.

    `and $0xFFFFFFFB, %eax` means "clear bit 2", i.e. -5 in a 32-bit operand,
    and gas therefore encodes it with the one-byte sign-extended immediate.
    Without this the value looks like 4294967291, which does not fit, and the
    instruction comes out three bytes longer.
    """
    if size == 32 and 0x80000000 <= value <= 0xFFFFFFFF:
        return value - 0x100000000
    if size == 16 and 0x8000 <= value <= 0xFFFF:
        return value - 0x10000
    if size == 8 and 0x80 <= value <= 0xFF:
        return value - 0x100
    return value


def _encode_alu(mnem, dst, src):
    tri = _ALU[mnem]
    if src.kind == "imm" and src.sym == "":
        src = op_imm(_norm_imm(src.imm, dst.size))
    f8 = _needs_rex8(dst) or _needs_rex8(src)
    if src.kind == "reg" and (dst.kind == "reg" or dst.kind == "mem"):
        op = tri[0] - 1 if dst.size == 8 else tri[0]
        rex, mrm, rl = encode_rm(src.reg_v, dst, 0)
        return _assemble(dst.size, rex, [op], mrm, [], rl, f8)
    if dst.kind == "reg" and src.kind == "mem":
        op = tri[1] - 1 if dst.size == 8 else tri[1]
        rex, mrm, rl = encode_rm(dst.reg_v, src, 0)
        return _assemble(dst.size, rex, [op], mrm, [], rl, f8)
    if src.kind == "imm":
        size = dst.size
        ext = tri[2]
        if size != 8 and fits_int8(src.imm):
            rex, mrm, rl = encode_rm(ext, dst, 0)
            return _assemble(size, rex, [0x83], mrm, pack_le(src.imm, 1), rl, f8)
        # accumulator short form: AL/AX/EAX/RAX, imm  (no ModRM)
        if dst.kind == "reg" and dst.reg_v == 0:
            acc = tri[3]
            if size == 8:
                out = []
                out.append(acc)
                out.extend(pack_le(src.imm, 1))
                return out, []
            out = []
            out.extend(_pfx_size(size))
            out.extend(emit_rex(0, size, False))
            out.append(acc + 1)
            out.extend(pack_le(src.imm, 4))
            return out, []
        opc = 0x80 if size == 8 else 0x81
        immn = 1 if size == 8 else 4
        rex, mrm, rl = encode_rm(ext, dst, 0)
        return _assemble(size, rex, [opc], mrm, pack_le(src.imm, immn), rl, f8)
    raise AssemblerError("bad operands for %s" % mnem)


def _encode_mov(dst, src):
    if (dst.kind == "creg" or src.kind == "creg"
            or dst.kind == "dreg" or src.kind == "dreg"):
        return _encode_mov_creg(dst, src)
    if dst.kind == "sreg" or src.kind == "sreg":
        return _encode_mov_sreg(dst, src)
    # Accumulator <-> absolute address has a short "moffs" form (A0..A3) with
    # no ModRM byte, which gas prefers in 32-bit mode: `mov %eax, saved_magic`
    # is five bytes, not six.
    if MODE != 64:
        m = _moffs_mov(dst, src)
        if m is not None:
            return m
    f8 = _needs_rex8(dst) or _needs_rex8(src)
    if src.kind == "reg" and (dst.kind == "reg" or dst.kind == "mem"):
        opc = 0x88 if dst.size == 8 else 0x89
        rex, mrm, rl = encode_rm(src.reg_v, dst, 0)
        return _assemble(dst.size, rex, [opc], mrm, [], rl, f8)
    if dst.kind == "reg" and src.kind == "mem":
        opc = 0x8A if dst.size == 8 else 0x8B
        rex, mrm, rl = encode_rm(dst.reg_v, src, 0)
        return _assemble(dst.size, rex, [opc], mrm, [], rl, f8)
    if src.kind == "imm":
        if src.sym != "":
            # `mov reg, symbol` -- the immediate is an address, so the field
            # needs an absolute relocation rather than a literal value.
            if dst.kind == "reg" and dst.size == 32:
                # B8+r id: no ModRM byte, which is what gas emits for
                # `mov $stack_top, %esp` in 32-bit boot code.
                out = []
                rex = REX_B if dst.reg_v >= 8 else 0
                out.extend(emit_rex(rex, 32, False))
                out.append(0xB8 + (dst.reg_v & 7))
                where = len(out)
                out.extend(pack_le(0, 4))
                return out, [Reloc(where, src.sym, 4, False, src.imm)]
            if dst.kind == "reg" and dst.size == 64:
                rex, mrm, rl = encode_rm(0, dst, 0)
                out, relocs = _assemble(64, rex, [0xC7], mrm, pack_le(0, 4), rl)
                relocs.append(Reloc(len(out) - 4, src.sym, 4, False, src.imm))
                return out, relocs
            rex, mrm, rl = encode_rm(0, dst, 0)
            out, relocs = _assemble(dst.size, rex, [0xC7], mrm,
                                    pack_le(0, 4), rl, f8)
            relocs.append(Reloc(len(out) - 4, src.sym, 4, False, src.imm))
            return out, relocs
        if dst.kind == "reg":
            size = dst.size
            if size == 64:
                if fits_int32(src.imm):
                    # gas: mov r64, imm32 -> REX.W C7 /0 id (sign-extended)
                    rex, mrm, rl = encode_rm(0, dst, 0)
                    return _assemble(size, rex, [0xC7], mrm,
                                     pack_le(src.imm, 4), rl)
                # movabs r64, imm64 : REX.W B8+r io (full 64-bit immediate)
                out = []
                rex = REX_B if dst.reg_v >= 8 else 0
                out.extend(emit_rex(rex, 64, False))
                out.append(0xB8 + (dst.reg_v & 7))
                out.extend(pack_le(src.imm, 8))
                return out, []
            out = []
            out.extend(_pfx_size(size))
            rex = REX_B if dst.reg_v >= 8 else 0
            out.extend(emit_rex(rex, size, _needs_rex8(dst)))
            base_op = 0xB0 if size == 8 else 0xB8
            out.append(base_op + (dst.reg_v & 7))
            immn = 1 if size == 8 else (2 if size == 16 else 4)
            out.extend(pack_le(src.imm, immn))
            return out, []
        opc = 0xC6 if dst.size == 8 else 0xC7
        immn = 1 if dst.size == 8 else 4
        rex, mrm, rl = encode_rm(0, dst, 0)
        return _assemble(dst.size, rex, [opc], mrm, pack_le(src.imm, immn), rl, f8)
    raise AssemblerError("bad operands for mov")


def _is_abs_mem(o):
    return (o.kind == "mem" and o.base == MEM_NONE and o.index == MEM_NONE
            and not o.rip)


def _moffs_mov(dst, src):
    """mov moffs<->accumulator: A0/A1 (load), A2/A3 (store). None if N/A."""
    if src.kind == "reg" and src.reg_v == 0 and _is_abs_mem(dst):
        size = src.size
        opc = 0xA2 if size == 8 else 0xA3
        out = []
        out.extend(_pfx_size(size))
        out.append(opc)
        where = len(out)
        # With a relocation the addend travels in the RELA entry, so the field
        # itself stays zero -- writing the displacement here as well would
        # count it twice.
        out.extend(pack_le(0 if dst.sym != "" else dst.disp, 4))
        if dst.sym != "":
            return out, [Reloc(where, dst.sym, 4, False, dst.disp)]
        return out, []
    if dst.kind == "reg" and dst.reg_v == 0 and _is_abs_mem(src):
        size = dst.size
        opc = 0xA0 if size == 8 else 0xA1
        out = []
        out.extend(_pfx_size(size))
        out.append(opc)
        where = len(out)
        out.extend(pack_le(0 if src.sym != "" else src.disp, 4))
        if src.sym != "":
            return out, [Reloc(where, src.sym, 4, False, src.disp)]
        return out, []
    return None


def _encode_pushpop(o, is_push):
    if o.kind == "reg":
        # push/pop default to 64-bit operand size; no REX.W needed. REX.B for
        # r8..r15. 16-bit push (unusual) would need 0x66; ShivyCX uses r64.
        out = []
        if o.size == 16:
            out.append(0x66)
        if o.reg_v >= 8:
            out.append(REX_BASE | REX_B)
        base = 0x50 if is_push else 0x58
        out.append(base + (o.reg_v & 7))
        return out, []
    if o.kind == "imm" and is_push:
        if fits_int8(o.imm):
            return [0x6A] + pack_le(o.imm, 1), []
        return [0x68] + pack_le(o.imm, 4), []
    if o.kind == "mem":
        ext = 6 if is_push else 0
        opc = 0xFF if is_push else 0x8F
        rex, mrm, rl = encode_rm(ext, o, 0)
        # 64-bit default; do not set REX.W
        return _assemble(0, rex, [opc], mrm, [], rl)
    raise AssemblerError("bad push/pop operand")


def _encode_calljmp(o, is_call):
    if o.kind == "mem" or o.kind == "reg":
        ext = 2 if is_call else 4
        rex, mrm, rl = encode_rm(ext, o, 0)
        return _assemble(0, rex, [0xFF], mrm, [], rl)
    # label / rel32: e8 (call) / e9 (jmp) + rel32 (PC-relative reloc)
    opc = 0xE8 if is_call else 0xE9
    out = [opc] + pack_le(0, 4)
    return out, [Reloc(1, o.sym, 4, True, o.disp - 4)]


def _encode_jcc(tttn, o):
    out = [0x0F, 0x80 | tttn] + pack_le(0, 4)
    return out, [Reloc(2, o.sym, 4, True, o.disp - 4)]


def _encode_lea(dst, src):
    rex, mrm, rl = encode_rm(dst.reg_v, src, 0)
    return _assemble(dst.size, rex, [0x8D], mrm, [], rl)


def _encode_imul(ops):
    if len(ops) == 1:
        return _encode_unary_group3(ops[0], 5)
    dst = ops[0]
    # imul reg, imm  is shorthand for  imul reg, reg, imm
    if len(ops) == 2 and ops[1].kind == "imm":
        imm = ops[1]
        if fits_int8(imm.imm):
            rex, mrm, rl = encode_rm(dst.reg_v, dst, 0)
            return _assemble(dst.size, rex, [0x6B], mrm, pack_le(imm.imm, 1), rl)
        rex, mrm, rl = encode_rm(dst.reg_v, dst, 0)
        return _assemble(dst.size, rex, [0x69], mrm, pack_le(imm.imm, 4), rl)
    src = ops[1]
    if len(ops) == 2:
        # imul r, r/m : 0F AF /r
        rex, mrm, rl = encode_rm(dst.reg_v, src, 0)
        return _assemble(dst.size, rex, [0x0F, 0xAF], mrm, [], rl)
    # imul r, r/m, imm : 6B /r ib (imm8) or 69 /r id
    imm = ops[2]
    if fits_int8(imm.imm):
        rex, mrm, rl = encode_rm(dst.reg_v, src, 0)
        return _assemble(dst.size, rex, [0x6B], mrm, pack_le(imm.imm, 1), rl)
    rex, mrm, rl = encode_rm(dst.reg_v, src, 0)
    return _assemble(dst.size, rex, [0x69], mrm, pack_le(imm.imm, 4), rl)



def _encode_unary_group3(o, ext):
    # F7 /ext (idiv/imul1/mul/div/neg/not); F6 for 8-bit
    opc = 0xF6 if o.size == 8 else 0xF7
    rex, mrm, rl = encode_rm(ext, o, 0)
    return _assemble(o.size, rex, [opc], mrm, [], rl)


def _encode_shift(ext, ops):
    dst = ops[0]
    if len(ops) == 1:
        opc = 0xD0 if dst.size == 8 else 0xD1
        rex, mrm, rl = encode_rm(ext, dst, 0)
        return _assemble(dst.size, rex, [opc], mrm, [], rl)
    amt = ops[1]
    if amt.kind == "reg":  # shift by cl -> D3 /ext
        opc = 0xD2 if dst.size == 8 else 0xD3
        rex, mrm, rl = encode_rm(ext, dst, 0)
        return _assemble(dst.size, rex, [opc], mrm, [], rl)
    if amt.imm == 1:
        opc = 0xD0 if dst.size == 8 else 0xD1
        rex, mrm, rl = encode_rm(ext, dst, 0)
        return _assemble(dst.size, rex, [opc], mrm, [], rl)
    opc = 0xC0 if dst.size == 8 else 0xC1
    rex, mrm, rl = encode_rm(ext, dst, 0)
    return _assemble(dst.size, rex, [opc], mrm, pack_le(amt.imm, 1), rl)


def _encode_movx(dst, src, signed):
    # movsx/movzx dst(r16/32/64), src(r/m8 or r/m16). movsxd for r/m32->r64.
    ssize = src.size
    if signed and ssize == 32:
        # movsxd r64, r/m32 : REX.W 63 /r
        rex, mrm, rl = encode_rm(dst.reg_v, src, 0)
        return _assemble(dst.size, rex, [0x63], mrm, [], rl)
    if ssize == 8:
        opc2 = 0xBE if signed else 0xB6
    else:
        opc2 = 0xBF if signed else 0xB7
    rex, mrm, rl = encode_rm(dst.reg_v, src, 0)
    return _assemble(dst.size, rex, [0x0F, opc2], mrm, [], rl, _needs_rex8(src))


def _encode_m_group(o, ext):
    """0F 01 /ext -- the descriptor-table and TLB group (lgdt/lidt/invlpg)."""
    rex, mrm, rl = encode_rm(ext, o, 0)
    return _assemble(0, rex, [0x0F, 0x01], mrm, [], rl)


def _encode_ljmp(ops):
    """Far jump to an immediate ptr16:32 -- how protected mode reloads CS.

    `ljmp $0x08, $long_start` is the instruction that actually enters long
    mode: the selector picks the 64-bit code descriptor loaded by lgdt.
    """
    seg = ops[0]
    off = ops[1]
    out = [0xEA]
    where = len(out)
    out.extend(pack_le(off.imm, 4))
    out.extend(pack_le(seg.imm, 2))
    if off.sym != "":
        return out, [Reloc(where, off.sym, 4, False, off.imm)]
    return out, []


def _encode_inout(ops, is_out):
    """in/out with either an imm8 port or dx."""
    if is_out:
        port = ops[0]
        acc = ops[1]
    else:
        acc = ops[0]
        port = ops[1]
    size = acc.size
    base = 0xE4 if not is_out else 0xE6      # imm8 port
    dxbase = 0xEC if not is_out else 0xEE    # dx port
    out = []
    out.extend(_pfx_size(size))
    if port.kind == "imm":
        out.append(base + (0 if size == 8 else 1))
        out.extend(pack_le(port.imm, 1))
    else:
        out.append(dxbase + (0 if size == 8 else 1))
    return out, []


def _encode_mov_creg(dst, src):
    """mov to/from a control or debug register: 0F 20/22 (cr), 0F 21/23 (dr).

    These always move the full register width for the mode and take no REX.W,
    so the operand size is forced to 0 here.
    """
    if dst.kind == "creg" or dst.kind == "dreg":
        opc = 0x22 if dst.kind == "creg" else 0x23
        creg = dst
        gp = src
    else:
        opc = 0x20 if src.kind == "creg" else 0x21
        creg = src
        gp = dst
    rex = 0
    if gp.reg_v >= 8:
        rex |= REX_B
    if creg.reg_v >= 8:
        rex |= REX_R
    out = []
    out.extend(emit_rex(rex, 0, False))
    out.extend([0x0F, opc])
    out.append(modrm(3, creg.reg_v & 7, gp.reg_v & 7))
    return out, []


def _encode_mov_sreg(dst, src):
    """mov Sreg, r/m16 (8E /r) and mov r/m16, Sreg (8C /r)."""
    if dst.kind == "sreg":
        rex, mrm, rl = encode_rm(dst.reg_v, src, 0)
        return _assemble(0, rex, [0x8E], mrm, [], rl)
    rex, mrm, rl = encode_rm(src.reg_v, dst, 0)
    return _assemble(0, rex, [0x8C], mrm, [], rl)


def _encode_setcc(tttn, o):
    # 0F 90+tttn /0 r/m8
    rex, mrm, rl = encode_rm(0, o, 0)
    return _assemble(8, rex, [0x0F, 0x90 | tttn], mrm, [], rl, _needs_rex8(o))


def _encode_cmov(tttn, dst, src):
    # 0F 40+tttn /r  (16/32/64-bit only)
    rex, mrm, rl = encode_rm(dst.reg_v, src, 0)
    return _assemble(dst.size, rex, [0x0F, 0x40 | tttn], mrm, [], rl)


def _encode_incdec(o, ext):
    # Outside 64-bit mode inc/dec of a whole register is the one-byte 40+r /
    # 48+r form -- exactly the opcode space that became REX in long mode, which
    # is why it is only legal here.
    if MODE != 64 and o.kind == "reg" and o.size != 8:
        out = []
        out.extend(_pfx_size(o.size))
        out.append((0x40 if ext == 0 else 0x48) + (o.reg_v & 7))
        return out, []
    opc = 0xFE if o.size == 8 else 0xFF
    rex, mrm, rl = encode_rm(ext, o, 0)
    return _assemble(o.size, rex, [opc], mrm, [], rl, _needs_rex8(o))


def _encode_xchg(a, b):
    # gas prefers the one-byte accumulator form (90+r) when one operand is
    # rAX/eAX and the other is a plain register.
    if a.kind == "reg" and b.kind == "reg" and a.size == b.size:
        other = -1
        if a.reg_v == 0:
            other = b.reg_v
        elif b.reg_v == 0:
            other = a.reg_v
        if other > 0:
            rex = REX_B if other >= 8 else 0
            return _assemble(a.size, rex, [0x90 + (other & 7)], [], [], [])
    # 87 /r (r/m, r). Prefer the register in ModRM.reg.
    if b.kind == "reg":
        rex, mrm, rl = encode_rm(b.reg_v, a, 0)
        return _assemble(a.size, rex, [0x87], mrm, [], rl,
                         _needs_rex8(a) or _needs_rex8(b))
    rex, mrm, rl = encode_rm(a.reg_v, b, 0)
    return _assemble(a.size, rex, [0x87], mrm, [], rl, False)


def _encode_movabs(dst, src):
    # REX.W B8+r io -- always the full 64-bit immediate form, so a symbol can
    # be relocated as R_X86_64_64.
    out = []
    rex = REX_B if dst.reg_v >= 8 else 0
    out.extend(emit_rex(rex, 64, False))
    out.append(0xB8 + (dst.reg_v & 7))
    where = len(out)
    out.extend(pack_le(src.imm, 8))
    if src.sym != "":
        return out, [Reloc(where, src.sym, 8, False, src.imm)]
    return out, []


# --------------------------------------------------------------------------
# Short (rel8) branch forms, used by the driver's relaxation pass
# --------------------------------------------------------------------------
# The default encoders always emit rel32 so that any target -- including an
# undefined external symbol -- is encodable. When the driver can prove a branch
# reaches within +/-127 bytes it re-encodes it with these two-byte forms.

def encode_jmp_short(o):
    return [0xEB, 0x00], [Reloc(1, o.sym, 1, True, o.disp - 1)]


def encode_jcc_short(tttn, o):
    return [0x70 | tttn, 0x00], [Reloc(1, o.sym, 1, True, o.disp - 1)]


# Branch classification for the driver: returns (kind, tttn) where kind is
# "jmp", "jcc", "call" or "" (not a relaxable branch).
def branch_kind(mnem, ops):
    if len(ops) != 1 or ops[0].kind != "imm" or ops[0].sym == "":
        return ("", 0)
    if mnem == "jmp":
        return ("jmp", 0)
    if mnem == "call":
        return ("call", 0)
    if mnem in _JCC:
        return ("jcc", _JCC[mnem])
    return ("", 0)


# --------------------------------------------------------------------------
# Intel-syntax parser (the subset ShivyCX emits)
# --------------------------------------------------------------------------
# parse_line(text) -> ("insn", mnem, ops) | ("label", name, None)
#                     | ("dir", text, None) | ("blank", "", None)
# Operand text forms handled:
#   registers:  rax / eax / r8d ...
#   immediates: 5, -16, 0x1f
#   memory:     QWORD PTR [rbp-8], DWORD PTR [sym+4*rcx], [rip+0], [sym]

_PTR_SIZE = {"BYTE": 8, "WORD": 16, "DWORD": 32, "QWORD": 64}


def _parse_int(tok):
    neg = False
    t = tok
    if t[0:1] == "-":
        neg = True
        t = t[1:]
    elif t[0:1] == "+":
        t = t[1:]
    if t[0:2] == "0x" or t[0:2] == "0X":
        v = int(t[2:], 16)
    else:
        v = int(t)
    if neg:
        v = -v
    return v


def _looks_int(tok):
    t = tok
    if t[0:1] == "-" or t[0:1] == "+":
        t = t[1:]
    if t == "":
        return False
    if t[0:2] == "0x" or t[0:2] == "0X":
        t = t[2:]
        i = 0
        while i < len(t):
            c = t[i]
            if not (("0" <= c <= "9") or ("a" <= c <= "f") or ("A" <= c <= "F")):
                return False
            i += 1
        return len(t) > 0
    i = 0
    while i < len(t):
        if not ("0" <= t[i] <= "9"):
            return False
        i += 1
    return True


def _split_terms(expr):
    """Split an address expression into signed terms, e.g. 'sym+4*rcx-8' ->
    ['+sym', '+4*rcx', '-8']."""
    terms = []
    cur = ""
    i = 0
    while i < len(expr):
        c = expr[i]
        if c == "+" or c == "-":
            if cur != "":
                terms.append(cur)
            cur = c
        else:
            cur += c
        i += 1
    if cur != "":
        terms.append(cur)
    # ensure each term has a leading sign
    out = []
    i = 0
    while i < len(terms):
        t = terms[i]
        if t[0:1] != "+" and t[0:1] != "-":
            t = "+" + t
        out.append(t)
        i += 1
    return out


def parse_memory(inner, size):
    base = MEM_NONE
    index = MEM_NONE
    scale = 1
    disp = 0
    sym = ""
    rip = False
    asize = 0
    terms = _split_terms(inner.replace(" ", ""))
    i = 0
    while i < len(terms):
        t = terms[i]
        sign = t[0]
        body = t[1:]
        if "*" in body:
            # index*scale, scale*index, or a constant product used as disp
            a, star, b = body.partition("*")
            if _looks_int(a) and _looks_int(b):
                d = _parse_int(a) * _parse_int(b)
                if sign == "-":
                    d = -d
                disp += d
            elif _looks_int(a):
                scale = _parse_int(a)
                index = reg_val(b)
            else:
                index = reg_val(a)
                scale = _parse_int(b)
        elif is_register(body):
            if body == "rip":
                rip = True
            elif base == MEM_NONE:
                base = reg_val(body)
                asize = reg_bits(body)
            else:
                index = reg_val(body)
                asize = reg_bits(body)
        elif body == "rip":
            rip = True
        elif _looks_int(body):
            d = _parse_int(body)
            if sign == "-":
                d = -d
            disp += d
        else:
            sym = body   # a symbol
        i += 1
    return op_mem(size, base, index, scale, disp, sym, rip, asize)


def parse_operand(text):
    t = text.strip()
    up = t.upper()
    size = 0
    # size prefix "QWORD PTR [..]"
    j = 0
    for key in _PTR_SIZE:
        if up.startswith(key + " PTR"):
            size = _PTR_SIZE[key]
            t = t[len(key) + 4:].strip()
            break
    if t[0:1] == "[":
        end = t.rfind("]")
        return parse_memory(t[1:end], size)
    if is_register(t):
        return op_reg(t)
    if _looks_int(t):
        return op_imm(_parse_int(t))
    # bare symbol operand (a jump/call target or [sym] written without brackets)
    o = Operand()
    o.kind = "imm"
    o.sym = t
    o.imm = 0
    return o


def _split_ops(rest):
    """Split operands on commas not inside brackets."""
    out = []
    cur = ""
    depth = 0
    i = 0
    while i < len(rest):
        c = rest[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        if c == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    if cur.strip() != "":
        out.append(cur)
    return out


def parse_line(raw):
    line = raw
    # strip // comments
    ci = line.find("//")
    if ci >= 0:
        line = line[:ci]
    line = line.strip()
    if line == "":
        return ("blank", "", None)
    if line[0:1] == ".":
        return ("dir", line, None)
    if line.endswith(":"):
        return ("label", line[:-1], None)
    # label followed by nothing else; instruction otherwise
    sp = line.find(" ")
    tab = line.find("\t")
    cut = sp
    if tab >= 0 and (tab < sp or sp < 0):
        cut = tab
    if cut < 0:
        return ("insn", line, [])
    mnem = line[:cut]
    rest = line[cut:].strip()
    ops = []
    parts = _split_ops(rest)
    i = 0
    while i < len(parts):
        p = parts[i].strip()
        if p != "":
            ops.append(parse_operand(p))
        i += 1
    return ("insn", mnem, ops)


# --------------------------------------------------------------------------
# SSE scalar-float encoders
# --------------------------------------------------------------------------
# General two-operand SSE form: [prefix] [REX] 0F <opc> ModRM. `reg_op` fills
# ModRM.reg (an xmm or GP register), `rm_op` is the r/m (xmm/GP register or
# memory). REX.W is set only when `w` (used by the int<->float conversions and
# 64-bit movq).

def _sse(prefix, opc, reg_op, rm_op, w):
    rex, mrm, rl = encode_rm(reg_op.reg_v, rm_op, 0)
    out = []
    if prefix != 0:
        out.append(prefix)
    size = 64 if w else 0
    out.extend(emit_rex(rex, size, False))
    out.append(0x0F)
    out.append(opc)
    pre = len(out)
    out.extend(mrm)
    relocs = []
    i = 0
    while i < len(rl):
        r = rl[i]
        add = r.add
        if r.pcrel:
            # any immediate sits between the displacement field and the end of
            # the instruction, which the PC-relative addend must also skip
            add -= len(imm_bytes)
        relocs.append(Reloc(r.where + pre, r.sym, r.size, r.pcrel, add))
        i += 1
    return out, relocs


def _encode_sse_mov(prefix, dst, src):
    # movsd/movss: load form (10, reg=dst) vs store form (11, reg=src).
    if dst.kind == "mem":
        return _sse(prefix, 0x11, src, dst, False)
    return _sse(prefix, 0x10, dst, src, False)


def _encode_sse_movalign(prefix, dst, src):
    # movaps/movapd: 28 (load, reg=dst) / 29 (store, reg=src)
    if dst.kind == "mem":
        return _sse(prefix, 0x29, src, dst, False)
    return _sse(prefix, 0x28, dst, src, False)


def _encode_movq(dst, src):
    # movq between xmm and 64-bit GP/mem, or xmm<-xmm/m64.
    if dst.size == 128 and src.size == 128:
        return _sse(0xF3, 0x7E, dst, src, False)      # xmm <- xmm
    if dst.size == 128 and src.kind == "mem":
        return _sse(0xF3, 0x7E, dst, src, False)      # xmm <- m64
    if dst.size == 128:
        return _sse(0x66, 0x6E, dst, src, True)       # xmm <- r64
    if src.size == 128 and dst.kind == "mem":
        return _sse(0x66, 0xD6, src, dst, False)      # m64 <- xmm
    return _sse(0x66, 0x7E, src, dst, True)           # r64 <- xmm


def _encode_movd(dst, src):
    # movd between xmm and 32-bit GP/mem (no REX.W).
    if dst.size == 128:
        return _sse(0x66, 0x6E, dst, src, False)      # xmm <- r/m32
    return _sse(0x66, 0x7E, src, dst, False)          # r/m32 <- xmm


# --------------------------------------------------------------------------
# AT&T-syntax parser
# --------------------------------------------------------------------------
# ShivyCX emits Intel syntax, but hand-written inline asm (and anything copied
# from gcc output) is AT&T: `%reg` operands, `$imm` immediates, `disp(base,
# index,scale)` memory, operands in the opposite order, and the operand size
# carried by a mnemonic suffix instead of a `SIZE PTR` prefix. This front end
# normalises such a line into the same (mnem, [Operand]) form the Intel parser
# produces, so the encoder itself is shared.

_ATT_SUFFIX = {"b": 8, "w": 16, "l": 32, "q": 64}

# Mnemonics that take a b/w/l/q size suffix in AT&T syntax.
_ATT_SIZED = {
    "mov": True, "add": True, "sub": True, "and": True, "or": True,
    "xor": True, "cmp": True, "test": True, "push": True, "pop": True,
    "inc": True, "dec": True, "neg": True, "not": True, "mul": True,
    "imul": True, "div": True, "idiv": True, "lea": True, "xchg": True,
    "shl": True, "shr": True, "sar": True, "sal": True, "rol": True,
    "ror": True, "call": True, "jmp": True, "movabs": True,
}

# AT&T spellings of the sign/zero-extending moves, mapped to the Intel
# mnemonic the encoder knows plus (src bits, dst bits).
_ATT_MOVX = {
    "movsbw": ("movsx", 8, 16), "movsbl": ("movsx", 8, 32),
    "movsbq": ("movsx", 8, 64), "movswl": ("movsx", 16, 32),
    "movswq": ("movsx", 16, 64), "movslq": ("movsxd", 32, 64),
    "movzbw": ("movzx", 8, 16), "movzbl": ("movzx", 8, 32),
    "movzbq": ("movzx", 8, 64), "movzwl": ("movzx", 16, 32),
    "movzwq": ("movzx", 16, 64),
}

_ATT_ALIAS = {
    "cltq": "cdqe", "cqto": "cqo", "cltd": "cdq", "cwtl": "cwde",
    "retq": "ret", "leaveq": "leave",
}


def _att_strip_comment(line):
    out = ""
    i = 0
    while i < len(line):
        c = line[i]
        if c == "#":
            break
        if c == "/" and line[i + 1:i + 2] == "/":
            break
        out += c
        i += 1
    return out


def _att_symbolic_imm(body):
    """`sym`, `sym+8`, `sym-4` -> (symbol, addend)."""
    plus = body.find("+")
    minus = body.find("-", 1)
    if plus > 0:
        return (body[:plus], _parse_int(body[plus + 1:]))
    if minus > 0:
        return (body[:minus], -_parse_int(body[minus + 1:]))
    return (body, 0)


def parse_att_operand(text, size, is_branch):
    t = text.strip()
    if t == "":
        raise AssemblerError("empty AT&T operand")
    if t[0:1] == "*":                      # indirect call/jmp: *%rax, *(%rax)
        return parse_att_operand(t[1:], size, False)
    if t[0:1] == "$":                      # immediate
        body = t[1:]
        if _looks_int(body):
            return op_imm(_parse_int(body))
        o = Operand()
        o.kind = "imm"
        pair = _att_symbolic_imm(body)
        o.sym = pair[0]
        o.imm = pair[1]
        return o
    if t[0:1] == "%":                      # register
        name = t[1:]
        if name in CREGS or name in DREGS or name in SREGS:
            return op_special(name)
        return op_reg(name)
    lp = t.find("(")
    if lp >= 0:                            # disp(base,index,scale)
        rp = t.rfind(")")
        head = t[:lp].strip()
        inner = t[lp + 1:rp]
        base = MEM_NONE
        index = MEM_NONE
        scale = 1
        rip = False
        asize = 0
        parts = inner.split(",")
        if len(parts) > 0 and parts[0].strip() != "":
            rname = parts[0].strip()
            if rname[0:1] == "%":
                rname = rname[1:]
            if rname == "rip":
                rip = True
            else:
                base = reg_val(rname)
                asize = reg_bits(rname)
        if len(parts) > 1 and parts[1].strip() != "":
            iname = parts[1].strip()
            if iname[0:1] == "%":
                iname = iname[1:]
            index = reg_val(iname)
            asize = reg_bits(iname)
        if len(parts) > 2 and parts[2].strip() != "":
            scale = _parse_int(parts[2].strip())
        disp = 0
        sym = ""
        if head != "":
            if _looks_int(head):
                disp = _parse_int(head)
            else:
                pair = _att_symbolic_imm(head)
                sym = pair[0]
                disp = pair[1]
        return op_mem(size, base, index, scale, disp, sym, rip, asize)
    if is_branch:                          # bare label target
        o = Operand()
        o.kind = "imm"
        o.sym = t
        o.imm = 0
        return o
    if _looks_int(t):                      # absolute address
        return op_mem(size, MEM_NONE, MEM_NONE, 1, _parse_int(t), "", False)
    pair = _att_symbolic_imm(t)            # bare symbol = absolute memory ref
    return op_mem(size, MEM_NONE, MEM_NONE, 1, pair[1], pair[0], False)


def _att_split_ops(rest):
    """Split on commas that are not inside parentheses."""
    out = []
    cur = ""
    depth = 0
    i = 0
    while i < len(rest):
        c = rest[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    if cur.strip() != "":
        out.append(cur)
    return out


def parse_att_line(raw):
    """Same contract as parse_line, for AT&T-syntax input."""
    line = _att_strip_comment(raw).strip()
    if line == "":
        return ("blank", "", None)
    if line[0:1] == ".":
        return ("dir", line, None)
    if line.endswith(":"):
        return ("label", line[:-1], None)
    sp = line.find(" ")
    tab = line.find("\t")
    cut = sp
    if tab >= 0 and (tab < sp or sp < 0):
        cut = tab
    if cut < 0:
        mnem = line
        rest = ""
    else:
        mnem = line[:cut]
        rest = line[cut:].strip()

    if mnem in _ATT_ALIAS:
        mnem = _ATT_ALIAS[mnem]
    if mnem in _PREFIX and rest != "":
        sub = parse_att_line(rest)
        if sub[0] != "insn":
            raise AssemblerError("bad prefixed instruction: %s" % line)
        return ("insn", mnem + " " + sub[1], sub[2])
    if mnem in _NULLARY and rest == "":
        return ("insn", mnem, [])

    # sign/zero-extending moves carry both operand sizes in the mnemonic
    if mnem in _ATT_MOVX:
        spec = _ATT_MOVX[mnem]
        parts = _att_split_ops(rest)
        src = parse_att_operand(parts[0], spec[1], False)
        dst = parse_att_operand(parts[1], spec[2], False)
        return ("insn", spec[0], [dst, src])

    size = 0
    base = mnem
    if len(mnem) > 2 and mnem[-1:] in _ATT_SUFFIX and (
            mnem[:-1] in _ATT_SIZED or mnem[:-1] in _CMOV):
        size = _ATT_SUFFIX[mnem[-1:]]
        base = mnem[:-1]
    is_branch = (base == "jmp" or base == "call" or base in _JCC)
    if is_branch and rest[0:1] == "*":
        is_branch = False

    ops = []
    parts = _att_split_ops(rest)
    i = 0
    while i < len(parts):
        p = parts[i].strip()
        if p != "":
            ops.append(parse_att_operand(p, size, is_branch))
        i += 1
    if base == "ljmp" or base == "lcall":
        # `ljmp $0x08, $target` is already selector-then-offset in AT&T
        return ("insn", base, ops)
    ops.reverse()                          # AT&T is src,dst; Intel is dst,src
    if base == "mov":
        # `movq %xmm0, %rax` is the SSE move, not a suffixed general `mov`.
        xmm = False
        i = 0
        while i < len(ops):
            if ops[i].kind == "reg" and ops[i].size == 128:
                xmm = True
            i += 1
        if xmm:
            base = "movq" if size == 64 else "movd"
    # an untyped memory operand paired with a register takes the register size
    if size == 0 and len(ops) == 2:
        if ops[0].kind == "mem" and ops[1].kind == "reg":
            ops[0].size = ops[1].size
        elif ops[1].kind == "mem" and ops[0].kind == "reg":
            ops[1].size = ops[0].size
    return ("insn", base, ops)
