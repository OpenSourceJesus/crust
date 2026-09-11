"""The Spot object definition and and some predefined spots, like registers."""

from __future__ import annotations


# GNU assembler Intel-syntax expression operators. A C identifier that is
# spelled exactly like one of these cannot be emitted as a bare symbol name --
# `as` parses e.g. `[shr]` as the shift operator, not a label (gcc avoids this
# only because it emits AT&T syntax). musl has a `static inline shr()` in
# qsort.c. We rename such symbols consistently at every definition and
# reference. The mapping is idempotent and only touches these few names, so
# normal symbols (and cross-TU linkage within ShivyC output) are unaffected.
_ASM_RESERVED = {
    "shr", "shl", "and", "or", "xor", "not", "mod",
    "eq", "ne", "lt", "le", "gt", "ge", "offset",
}


# Object-format symbol spelling, set once per compile by set_object_format.
# ELF (the default) spells a C symbol bare. Mach-O prefixes every C symbol with
# an underscore (`main` -> `_main`), and treats names starting with `L` as
# assembler-local: they never reach the object's symbol table. Compiler-made
# jump labels use that, so they cannot collide with, or be exported beside,
# real symbols.
_SYM_PREFIX = ""
_LOCAL_LABEL_PREFIX = ""
_COMPILER_LABEL_STEM = "__shivyc_label"


def set_object_format(fmt):
    """Select symbol spelling for object format `fmt` ("elf" or "macho")."""
    global _SYM_PREFIX
    global _LOCAL_LABEL_PREFIX
    if fmt == "macho":
        _SYM_PREFIX = "_"
        _LOCAL_LABEL_PREFIX = "L"
    else:
        _SYM_PREFIX = ""
        _LOCAL_LABEL_PREFIX = ""


def mangle_symbol(name: str) -> str:
    """Spell C-level symbol `name` as the assembler must see it.

    Renames a symbol whose spelling collides with a GNU-as operator, then
    applies the object format's prefix. Every definition *and* reference must
    come through here, or the two spellings will not meet at link time.
    Not idempotent under Mach-O: pass the C name, never an already-mangled
    one."""
    n = name
    if name in _ASM_RESERVED:
        n = "__shivyc_sym_" + name
    if _LOCAL_LABEL_PREFIX and name.startswith(_COMPILER_LABEL_STEM):
        return _LOCAL_LABEL_PREFIX + n
    return _SYM_PREFIX + n


class Spot:
    """Spot in the machine where an IL value can be.

    spot_type (enum) - One of the values below describing the general type of
    spot this is.
    detail - Additional information about this spot. The this attribute's type
    and meaning depend on the spot_type; see below for more.

    """

    def __init__(self, detail) -> None:
        """Initialize a spot.

        `detail` should uniquely represent this Spot for this specific spot
        type, because it will be used for hashing and equality testing.
        """
        self.detail = detail

    def asm_str(self, size: int) -> str:
        """Make the ASM form of this spot, for the given size in bytes.

        This function raises NotImplementedError for unsupported sizes.

        Examples:
            spots.RAX.asm_str(4) -> "eax"
            spots.RAX.asm_str(8) -> "rax"
            spot(STACK, -16).asm_str(4) -> "DWORD [rbp-16]"
            spot(LITERAL, 14).asm_str(4) -> "14"

        size (int) - Size in bytes of the data stored at this spot.
        return (str) - ASM form of this spot.

        """
        raise NotImplementedError

    def rbp_offset(self) -> int:
        """Return this spot's offset from RBP.

        If this is a memory spot which resides at a certain negative offset
        away from RBP, then return that offset. This is used by the register
        allocator to figure out how much memory to allocate for this spot.

        If this is not a memory spot relative to RBP, just return 0.
        """
        return 0

    def shift(self, chunk: int, count=None) -> 'Spot':
        """Return a new spot shifted relative to this one.

        For non-memory spots, this function returns itself and throws an
        error if given chunk != 0 or count != None.
        """
        if chunk or count:
            raise NotImplementedError("cannot shift this spot type")
        return self

    def __repr__(self) -> str:  # pragma: no cover
        return self.detail

    def __eq__(self, other) -> bool:
        """Test equality by comparing Spot type and detail."""
        if self.__class__.__name__ != other.__class__.__name__:
            return False

        return self.detail == other.detail

    def __hash__(self) -> int:
        """Hash based on type and detail."""
        return hash((self.__class__.__name__, self.detail))


class RegSpot(Spot):
    """Spot representing a machine register."""

    # Mapping from the 64-bit register name to the 64-bit, 32-bit, 16-bit,
    # and 8-bit register names for each register.
    # TODO: Do I need rex prefix on any of the 8-bit?
    reg_map = {"rax": ["rax", "eax", "ax", "al"],
               "rbx": ["rbx", "ebx", "bx", "bl"],
               "rcx": ["rcx", "ecx", "cx", "cl"],
               "rdx": ["rdx", "edx", "dx", "dl"],
               "rsi": ["rsi", "esi", "si", "sil"],
               "rdi": ["rdi", "edi", "di", "dil"],
               "r8": ["r8", "r8d", "r8w", "r8b"],
               "r9": ["r9", "r9d", "r9w", "r9b"],
               "r10": ["r10", "r10d", "r10w", "r10b"],
               "r11": ["r11", "r11d", "r11w", "r11b"],
               "r12": ["r12", "r12d", "r12w", "r12b"],
               "r13": ["r13", "r13d", "r13w", "r13b"],
               "r14": ["r14", "r14d", "r14w", "r14b"],
               "r15": ["r15", "r15d", "r15w", "r15b"],
               "rbp": ["rbp", "", "", ""],
               "rsp": ["rsp", "", "", ""]}

    def __init__(self, name: str) -> None:
        """Initialize this spot.

        `name` is the string representation of the 64-bit register (e.g.
        "rax").
        """
        super().__init__(name)
        self.name = name

    def asm_str(self, size: int) -> str:  # noqa D102
        if size == 0 or size == 8:
            i = 0
        elif size == 1:
            i = 3
        elif size == 2:
            i = 2
        elif size == 4:
            i = 1
        else:
            raise NotImplementedError("unexpected register size")

        return self.reg_map[self.name][i]

    def name_variants(self):
        """Return this register's name spellings (64/32/16/8-bit) as a list.

        Built by indexing reg_map[self.name][i] one element at a time. The
        self-hosting transpiler only supports the double-subscript form
        reg_map[key][i] (it lowers reg_map to an indexed accessor function);
        a bare class-level reg_map[key] that returns the whole list is
        mis-lowered to a field read off a bogus object and yields garbage.
        Callers that need the variant list must go through here.
        """
        variants = []
        i = 0
        while i < 4:
            variants.append(self.reg_map[self.name][i])
            i += 1
        return variants


class MemSpot(Spot):
    """Spot representing a region in memory, like on stack or .data section.

    `base` can be either a string or a Spot. The string form is used when
    this spot represents an external variable. The Spot form is used when
    this spot represents an offset in memory, like [rbp-5].
    """

    size_map = {1: "BYTE PTR ",
                2: "WORD PTR ",
                4: "DWORD PTR ",
                8: "QWORD PTR "}

    def __init__(self, base, offset: int = 0, chunk: int = 0, count=None) -> None:  # noqa D102
        super().__init__((base, offset, chunk, count))

        self.base = base
        self.offset = offset
        self.chunk = chunk
        self.count = count

    def asm_str(self, size: int) -> str:  # noqa D102
        if isinstance(self.base, Spot):
            base_str = self.base.asm_str(0)
        else:
            base_str = mangle_symbol(self.base)

        total_offset = self.offset
        if not self.count:
            total_offset = self.offset + self.chunk

        if total_offset == 0:
            simple = base_str
        elif total_offset > 0:
            simple = f"{base_str}+{total_offset}"
        else:  # total_offset < 0
            simple = f"{base_str}-{-total_offset}"

        if self.count and self.chunk > 0:
            final = f"{simple}+{self.chunk}*{self.count.asm_str(8)}"
        elif self.count and self.chunk < 0:
            final = f"{simple}-{-self.chunk}*{self.count.asm_str(8)}"
        else:
            final = simple

        size_desc = self.size_map.get(size, "")
        return f"{size_desc}[{final}]"

    def rbp_offset(self) -> int:  # noqa D102
        if self.base == RBP:
            return -self.offset
        else:
            return 0

    def shift(self, chunk: int, count=None) -> 'Spot':  # noqa D102
        """Return a new memory spot shifted relative to this one.

        chunk - A Python integer representing the size of each chunk of offset
        count - If provided, a register spot storing the number of chunks to
        be offset. If this value is provided, then `chunk` must be in {1, 2,
        4, 8}.
        """
        if count and self.count:
            raise NotImplementedError("cannot shift by count")

        if count:
            new_offset = self.offset + self.chunk
            new_chunk = chunk
            new_count = count
        else:  # no count given
            new_offset = self.offset + chunk
            new_chunk = self.chunk
            new_count = self.count

        return MemSpot(self.base, new_offset, new_chunk, new_count)


class LiteralSpot(Spot):
    """Spot representing a literal value.

    This is a bit of a hack, since a literal value isn't /really/ a storage
    spot. The value attribute is the integer representation of the value of
    this literal.
    """

    def __init__(self, value) -> None:
        super().__init__(value)
        self.value = value

    def asm_str(self, size: int) -> str:  # noqa D102
        return str(self.value)


# RBX is callee-saved, which is still unsupported
# RBX = RegSpot("rbx")

RAX = RegSpot("rax")
RCX = RegSpot("rcx")


class XmmRegSpot(Spot):
    """Spot representing an SSE (xmm) register; name is size-independent."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name

    def asm_str(self, size: int) -> str:  # noqa D102
        return self.name


XMM0 = XmmRegSpot("xmm0")
XMM1 = XmmRegSpot("xmm1")
XMM2 = XmmRegSpot("xmm2")
XMM3 = XmmRegSpot("xmm3")
XMM4 = XmmRegSpot("xmm4")
XMM5 = XmmRegSpot("xmm5")
XMM6 = XmmRegSpot("xmm6")
XMM7 = XmmRegSpot("xmm7")
xmm_arg_regs = [XMM0, XMM1, XMM2, XMM3, XMM4, XMM5, XMM6, XMM7]
RDX = RegSpot("rdx")
RSI = RegSpot("rsi")
RDI = RegSpot("rdi")
R8 = RegSpot("r8")
R9 = RegSpot("r9")
# SysV AMD64 integer/pointer argument registers, in order. Exposed as a
# module-level list (like xmm_arg_regs) so call sites can read it directly;
# reading it as a class attribute (LoadArg.arg_regs) does not survive the
# self-host runtime, which cannot fetch a class attribute through the type.
int_arg_regs = [RDI, RSI, RDX, RCX, R8, R9]
R10 = RegSpot("r10")
R11 = RegSpot("r11")

# Callee-saved (preserved across calls). A function that allocates any of these
# must save/restore it in its prologue/epilogue. Because Call does not list them
# as clobbered, the allocator may keep a value that is live across a call in one
# of these, avoiding a spill to memory -- the payoff is largest in deeply nested
# call chains (and composes with -f-pack-args, which frees argument registers).
RBX = RegSpot("rbx")
R12 = RegSpot("r12")
R13 = RegSpot("r13")
R14 = RegSpot("r14")
R15 = RegSpot("r15")

# Caller-saved registers are listed first so the graph colorer prefers them for
# short-lived temporaries (no save/restore cost); callee-saved registers come
# last and are used mainly for values that survive a call.
caller_saved_registers = [RAX, RCX, RDX, RSI, RDI, R8, R9, R10, R11]
callee_saved_registers = [RBX, R12, R13, R14, R15]
registers = caller_saved_registers + callee_saved_registers

# The calling convention the lists above currently describe: "sysv" or
# "win64". Selected once per compile by set_abi.
abi = "sysv"


def set_abi(name):
    """Point the register lists at calling convention `name`.

    The lists are rebuilt *in place*, never rebound: ASMGen.alloc_registers,
    Call/LoadArg and thread_contracts hold references to these very list
    objects, so mutating them retargets every user at once, and calling this
    again (the test harness compiles many programs in one process) switches
    back cleanly.

    Microsoft x64 differs from System V in exactly the ways these lists
    carry: four integer argument registers (rcx, rdx, r8, r9) instead of six,
    four vector argument registers (xmm0-3) instead of eight, and rsi/rdi are
    callee-saved rather than scratch. (xmm6-15 are callee-saved too, but the
    x86-64 back end never keeps a value in any xmm register above xmm2, so
    there is nothing to preserve.)
    """
    global abi
    if name == "win64":
        abi = "win64"
        int_args = [RCX, RDX, R8, R9]
        xmm_args = [XMM0, XMM1, XMM2, XMM3]
        caller = [RAX, RCX, RDX, R8, R9, R10, R11]
        callee = [RBX, RSI, RDI, R12, R13, R14, R15]
    else:
        abi = "sysv"
        int_args = [RDI, RSI, RDX, RCX, R8, R9]
        xmm_args = [XMM0, XMM1, XMM2, XMM3, XMM4, XMM5, XMM6, XMM7]
        caller = [RAX, RCX, RDX, RSI, RDI, R8, R9, R10, R11]
        callee = [RBX, R12, R13, R14, R15]
    int_arg_regs[:] = int_args
    xmm_arg_regs[:] = xmm_args
    caller_saved_registers[:] = caller
    callee_saved_registers[:] = callee
    registers[:] = caller + callee


def is_win64():
    """True while compiling for the Microsoft x64 calling convention."""
    return abi == "win64"

RBP = RegSpot("rbp")
RSP = RegSpot("rsp")
