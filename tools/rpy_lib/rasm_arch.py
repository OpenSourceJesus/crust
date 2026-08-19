"""Architecture facts shared by the assembler driver (rasm_obj) and the
linker (rlink).

Both tools were written against x86-64 and hardcoded that in three kinds of
place: the ELF `e_machine`/`e_flags` header words, the mapping from an
abstract relocation request to a numeric ELF relocation type, and the code
that applies a relocation to a field. This module is the seam for the first
two; the third lives in rlink, dispatched on `Arch.name`.

The shape deliberately mirrors `shivyc/targets/__init__.py`: every fact is an
*instance* attribute set in `__init__`, and callers always hold an Arch
*instance* (via `get_arch`), never the class. The self-hosted compiler cannot
read a class attribute through the type, so this is the only portable form.

Relocation model
----------------
`rasm.Reloc` describes a fixup abstractly -- a field of `size` bytes, `pcrel`
or not, `signed` or not -- which is all x86-64 needs, because there the
addressing mode already determines the encoding. Fixed-width ISAs do not work
that way: on AArch64 the *same* 4-byte field carries a 26-bit branch offset, a
21-bit page delta, or a 12-bit page offset depending on the instruction that
owns it. So a Reloc also carries a `kind` string, set by the encoder, and each
Arch maps `kind` to its numeric type. `kind == ""` means "infer from
size/pcrel/signed", which is exactly the old x86-64 behavior.
"""

# ELF e_machine values.
EM_X86_64 = 62
EM_AARCH64 = 183
EM_RISCV = 243

# ---------------------------------------------------------------------------
# x86-64 relocation types
# ---------------------------------------------------------------------------
R_X86_64_NONE = 0
R_X86_64_64 = 1
R_X86_64_PC32 = 2
R_X86_64_PLT32 = 4
R_X86_64_32 = 10
R_X86_64_32S = 11

# ---------------------------------------------------------------------------
# AArch64 relocation types (ELF for the Arm 64-bit Architecture)
# ---------------------------------------------------------------------------
R_AARCH64_NONE = 0
R_AARCH64_ABS64 = 257
R_AARCH64_ABS32 = 258
R_AARCH64_ABS16 = 259
R_AARCH64_PREL64 = 260
R_AARCH64_PREL32 = 261
# Page-relative address materialization: adrp gives the +/-4GB page delta in
# 21 bits, a following add/ldr supplies the 12-bit offset within that page.
R_AARCH64_ADR_PREL_PG_HI21 = 275
# Plain PC-relative address, no page rounding: `adr Xd, sym` reaches +/-1MB and
# needs no companion `add`. Distinct from ADR_PREL_PG_HI21 above, which is the
# adrp form. Without this the kind falls through to the data path and becomes a
# PREL32 -- a 32-bit write that overwrites the whole instruction word instead
# of splicing an immediate into it, turning the `adr` into a `udf`.
R_AARCH64_ADR_PREL_LO21 = 274
R_AARCH64_ADD_ABS_LO12_NC = 277
# Branches. TSTBR14 is tbz/tbnz, CONDBR19 is b.<cc>/cbz/cbnz, JUMP26 is b,
# CALL26 is bl. The last two share an encoding but keep distinct types.
R_AARCH64_TSTBR14 = 279
R_AARCH64_CONDBR19 = 280
R_AARCH64_JUMP26 = 282
R_AARCH64_CALL26 = 283
# ldr/str offset within a page, scaled by the access width. The _NC suffix
# means "no check": the linker must not range-check, because the high bits
# are supplied by the paired adrp.
R_AARCH64_LDST8_ABS_LO12_NC = 278
R_AARCH64_LDST16_ABS_LO12_NC = 284
R_AARCH64_LDST32_ABS_LO12_NC = 285
R_AARCH64_LDST64_ABS_LO12_NC = 286
R_AARCH64_LDST128_ABS_LO12_NC = 299
# movz/movk immediate slices, used to build a 64-bit absolute in four steps.
R_AARCH64_MOVW_UABS_G0_NC = 264
R_AARCH64_MOVW_UABS_G1_NC = 266
R_AARCH64_MOVW_UABS_G2_NC = 268
R_AARCH64_MOVW_UABS_G3 = 269

# ---------------------------------------------------------------------------
# RISC-V relocation types
# ---------------------------------------------------------------------------
R_RISCV_NONE = 0
R_RISCV_32 = 1
R_RISCV_64 = 2
R_RISCV_BRANCH = 16
R_RISCV_JAL = 17
# CALL/CALL_PLT cover the auipc+jalr pair emitted by the `call` pseudo-op;
# the relocation is attached to the auipc and patches both instructions.
R_RISCV_CALL = 18
R_RISCV_CALL_PLT = 19
R_RISCV_PCREL_HI20 = 23
R_RISCV_PCREL_LO12_I = 24
R_RISCV_PCREL_LO12_S = 25
R_RISCV_HI20 = 26
R_RISCV_LO12_I = 27
R_RISCV_LO12_S = 28
R_RISCV_RELAX = 51

# RISC-V e_flags bits: which ABI the object's floating-point arguments use.
# Objects with different float ABIs cannot be linked together.
EF_RISCV_RVC = 0x1
EF_RISCV_FLOAT_ABI_SOFT = 0x0
EF_RISCV_FLOAT_ABI_DOUBLE = 0x4


class Arch(object):
    """The architecture facts rasm_obj and rlink need. Subclasses fill these
    in; all attributes are per-instance for self-host compatibility."""

    def __init__(self):
        self.name = "generic"
        self.elf_machine = 0
        self.elf_flags = 0
        # Natural code alignment for a .text section with no explicit .align.
        self.text_align = 16
        # True if the ISA has fixed-width instructions, so branch relaxation
        # is a matter of expanding to an instruction *sequence* rather than
        # widening one instruction's displacement field.
        self.fixed_width = False
        # Width in bytes of each data-emitting directive. This is genuinely
        # per-target, not cosmetic: `.word` is 2 bytes to an x86 assembler but
        # 4 to an AArch64 or RISC-V one, so sharing one table silently emits
        # half the data on the RISC targets.
        self.data_widths = {}

    def reloc_type(self, kind, size, pcrel, signed):
        """Map an abstract rasm.Reloc to a numeric ELF relocation type."""
        raise NotImplementedError("Arch.reloc_type")


class X86_64Arch(Arch):
    def __init__(self):
        Arch.__init__(self)
        self.name = "x86_64"
        self.elf_machine = EM_X86_64
        self.elf_flags = 0
        self.text_align = 16
        self.fixed_width = False
        self.data_widths = {".byte": 1, ".word": 2, ".short": 2,
                            ".value": 2, ".int": 4, ".long": 4, ".quad": 8}

    def reloc_type(self, kind, size, pcrel, signed):
        # x86-64 needs no instruction-specific kinds: the addressing mode
        # already fixes the field, so size/pcrel/signed determine the type.
        # This is byte-for-byte the pre-seam behavior.
        if kind == "none":
            return R_X86_64_NONE
        if kind == "plt32":
            return R_X86_64_PLT32
        if pcrel:
            return R_X86_64_PC32
        if size == 8:
            return R_X86_64_64
        if signed:
            return R_X86_64_32S
        return R_X86_64_32


class Arm64Arch(Arch):
    def __init__(self):
        Arch.__init__(self)
        self.name = "arm64"
        self.elf_machine = EM_AARCH64
        self.elf_flags = 0
        self.text_align = 4
        self.fixed_width = True
        self.data_widths = {".byte": 1, ".half": 2, ".hword": 2,
                            ".short": 2, ".word": 4, ".int": 4,
                            ".long": 4, ".quad": 8, ".dword": 8,
                            ".xword": 8}

    def reloc_type(self, kind, size, pcrel, signed):
        if kind == "none":
            return R_AARCH64_NONE
        if kind == "call26":
            return R_AARCH64_CALL26
        if kind == "jump26":
            return R_AARCH64_JUMP26
        if kind == "condbr19":
            return R_AARCH64_CONDBR19
        if kind == "tstbr14":
            return R_AARCH64_TSTBR14
        if kind == "adr_pg_hi21":
            return R_AARCH64_ADR_PREL_PG_HI21
        if kind == "adr_prel_lo21":
            return R_AARCH64_ADR_PREL_LO21
        if kind == "add_lo12":
            return R_AARCH64_ADD_ABS_LO12_NC
        if kind == "ldst8_lo12":
            return R_AARCH64_LDST8_ABS_LO12_NC
        if kind == "ldst16_lo12":
            return R_AARCH64_LDST16_ABS_LO12_NC
        if kind == "ldst32_lo12":
            return R_AARCH64_LDST32_ABS_LO12_NC
        if kind == "ldst64_lo12":
            return R_AARCH64_LDST64_ABS_LO12_NC
        if kind == "ldst128_lo12":
            return R_AARCH64_LDST128_ABS_LO12_NC
        if kind == "movw_g0_nc":
            return R_AARCH64_MOVW_UABS_G0_NC
        if kind == "movw_g1_nc":
            return R_AARCH64_MOVW_UABS_G1_NC
        if kind == "movw_g2_nc":
            return R_AARCH64_MOVW_UABS_G2_NC
        if kind == "movw_g3":
            return R_AARCH64_MOVW_UABS_G3
        # Plain data words (.quad/.int pointing at a symbol) carry no kind.
        if pcrel:
            return R_AARCH64_PREL64 if size == 8 else R_AARCH64_PREL32
        if size == 8:
            return R_AARCH64_ABS64
        if size == 2:
            return R_AARCH64_ABS16
        return R_AARCH64_ABS32


class RiscV64Arch(Arch):
    def __init__(self):
        Arch.__init__(self)
        self.name = "riscv64"
        self.elf_machine = EM_RISCV
        # lp64d: hard-float double ABI, matching riscv64-linux-gnu-gcc's
        # default. Objects whose float ABI differs cannot be linked together,
        # so this word is load-bearing, not cosmetic.
        self.elf_flags = EF_RISCV_FLOAT_ABI_DOUBLE
        self.text_align = 4
        self.fixed_width = True
        self.data_widths = {".byte": 1, ".half": 2, ".hword": 2,
                            ".short": 2, ".word": 4, ".int": 4,
                            ".long": 4, ".quad": 8, ".dword": 8,
                            ".xword": 8}

    def reloc_type(self, kind, size, pcrel, signed):
        if kind == "none":
            return R_RISCV_NONE
        if kind == "call":
            return R_RISCV_CALL
        if kind == "call_plt":
            return R_RISCV_CALL_PLT
        if kind == "branch":
            return R_RISCV_BRANCH
        if kind == "jal":
            return R_RISCV_JAL
        if kind == "pcrel_hi20":
            return R_RISCV_PCREL_HI20
        if kind == "pcrel_lo12_i":
            return R_RISCV_PCREL_LO12_I
        if kind == "pcrel_lo12_s":
            return R_RISCV_PCREL_LO12_S
        if kind == "hi20":
            return R_RISCV_HI20
        if kind == "lo12_i":
            return R_RISCV_LO12_I
        if kind == "lo12_s":
            return R_RISCV_LO12_S
        if kind == "relax":
            return R_RISCV_RELAX
        if size == 8:
            return R_RISCV_64
        return R_RISCV_32


def get_arch(name):
    """Return a fresh Arch instance for `name` (default x86-64). Accepts the
    same aliases as shivyc.targets.get_target."""
    n = name if name else "x86_64"
    if n == "x86_64" or n == "amd64":
        return X86_64Arch()
    if n == "arm64" or n == "aarch64":
        return Arm64Arch()
    if n == "riscv64" or n == "rv64":
        return RiscV64Arch()
    return X86_64Arch()


def arch_for_machine(machine):
    """Return an Arch for an ELF e_machine value, or None if unsupported.
    Used by the linker to identify an input object."""
    if machine == EM_X86_64:
        return X86_64Arch()
    if machine == EM_AARCH64:
        return Arm64Arch()
    if machine == EM_RISCV:
        return RiscV64Arch()
    return None


def machine_name(machine):
    """Human-readable name for an e_machine value, for error messages."""
    if machine == EM_X86_64:
        return "x86-64"
    if machine == EM_AARCH64:
        return "aarch64"
    if machine == EM_RISCV:
        return "riscv"
    return "machine %d" % machine
