"""This module defines and implements classes representing assembly commands.

The _ASMCommand object is the base class for most ASM commands. Some commands
inherit from _ASMCommandMultiSize or _JumpCommand instead.

"""


class _ASMCommand:
    """Base class for a standard ASMCommand, like `add` or `imul`.

    This class is used for ASM commands which take arguments of the same
    size.
    """

    name = None

    def __init__(self, dest=None, source=None, size=None):
        self.dest = dest.asm_str(size) if dest else None
        self.source = source.asm_str(size) if source else None
        self.size = size

    def __str__(self):
        s = "\t" + self.name
        if self.dest:
            s += " " + self.dest
        if self.source:
            s += ", " + self.source
        return s


class _ASMCommandMultiSize:
    """Base class for an ASMCommand which takes arguments of different sizes.

    For example, `movsx` and `movzx`.
    """

    name = None

    def __init__(self, dest, source, source_size, dest_size):
        self.dest = dest.asm_str(source_size)
        self.source = source.asm_str(dest_size)
        self.source_size = source_size
        self.dest_size = dest_size

    def __str__(self):
        s = "\t" + self.name
        if self.dest:
            s += " " + self.dest
        if self.source:
            s += ", " + self.source
        return s


class _JumpCommand:
    """Base class for jump commands."""

    name = None

    def __init__(self, target):
        self.target = target

    def __str__(self):
        s = "\t" + self.name + " " + self.target
        return s


class Comment:
    """Class for comments."""

    def __init__(self, msg):  # noqa: D102
        self.msg = msg

    def __str__(self):  # noqa: D102
        return "\t// " + self.msg


class Raw:
    """A raw, pre-formatted assembly line (already in Intel syntax).

    Used for instruction sequences (e.g. SIMD bit-packing) that do not map
    onto the size-parameterized _ASMCommand model.
    """

    def __init__(self, text):  # noqa: D102
        self.text = text

    def __str__(self):  # noqa: D102
        return "\t" + self.text


class AsmLabel:
    """Class for label."""

    def __init__(self, label):  # noqa: D102
        self.label = label

    def __str__(self):  # noqa: D102
        from shivyc.spots import mangle_symbol
        return mangle_symbol(self.label) + ":"


class Lea:
    """Class for lea command."""

    name = "lea"

    def __init__(self, dest, source):  # noqa: D102
        self.dest = dest
        self.source = source

    def __str__(self):  # noqa: D102
        return ("\t" + self.name + " " + self.dest.asm_str(8) + ", "
                "" + self.source.asm_str(0))


class Je(_JumpCommand): name = "je"  # noqa: D101


class Jne(_JumpCommand): name = "jne"  # noqa: D101


class Jg(_JumpCommand): name = "jg"  # noqa: D101


class Jge(_JumpCommand): name = "jge"  # noqa: D101


class Jl(_JumpCommand): name = "jl"  # noqa: D101


class Jle(_JumpCommand): name = "jle"  # noqa: D101


class Ja(_JumpCommand): name = "ja"  # noqa: D101


class Jae(_JumpCommand): name = "jae"  # noqa: D101


class Jb(_JumpCommand): name = "jb"  # noqa: D101


class Jbe(_JumpCommand): name = "jbe"  # noqa: D101


class Jmp(_JumpCommand): name = "jmp"  # noqa: D101


class Movsx(_ASMCommandMultiSize):  # noqa: D101
    name = "movsx"

    def __str__(self):
        # Intel names the 32-to-64-bit sign extension `movsxd`. GNU as also
        # accepts `movsx` for it; LLVM's assembler does not. (The inherited
        # field names are swapped: `source_size` is the destination's width
        # and `dest_size` the source's -- see value.py's call.)
        if self.source_size == 8 and self.dest_size == 4:
            return "\tmovsxd " + self.dest + ", " + self.source
        return _ASMCommandMultiSize.__str__(self)


class Movzx(_ASMCommandMultiSize): name = "movzx"  # noqa: D101


class Mov(_ASMCommand): name = "mov"  # noqa: D101


class AsmAdd(_ASMCommand): name = "add"  # noqa: D101


class Sub(_ASMCommand): name = "sub"  # noqa: D101


class AsmNeg(_ASMCommand): name = "neg"  # noqa: D101


class AsmNot(_ASMCommand): name = "not"  # noqa: D101


class AsmDiv(_ASMCommand): name = "div"  # noqa: D101


class Imul(_ASMCommand): name = "imul"  # noqa: D101


class Idiv(_ASMCommand): name = "idiv"  # noqa: D101


class Cdq(_ASMCommand): name = "cdq"  # noqa: D101


class Cqo(_ASMCommand): name = "cqo"  # noqa: D101


class Xor(_ASMCommand): name = "xor"  # noqa: D101


class And(_ASMCommand): name = "and"  # noqa: D101


class Or(_ASMCommand): name = "or"  # noqa: D101


class Cmp(_ASMCommand): name = "cmp"  # noqa: D101


class Pop(_ASMCommand): name = "pop"  # noqa: D101


class Push(_ASMCommand): name = "push"  # noqa: D101


class AsmCall(_ASMCommand): name = "call"  # noqa: D101


class Ret(_ASMCommand): name = "ret"  # noqa: D101


class Sar(_ASMCommandMultiSize): name = "sar"  # noqa: D101


class Shr(_ASMCommandMultiSize): name = "shr"  # noqa: D101


class Sal(_ASMCommandMultiSize): name = "sal"  # noqa: D101


class Movsd(_ASMCommand): name = "movsd"        # noqa: D101
class Movss(_ASMCommand): name = "movss"        # noqa: D101
class Cvtsi2sd(_ASMCommand): name = "cvtsi2sd"  # noqa: D101
class Cvtsi2ss(_ASMCommand): name = "cvtsi2ss"  # noqa: D101
class Cvttsd2si(_ASMCommand): name = "cvttsd2si"  # noqa: D101
class Cvttss2si(_ASMCommand): name = "cvttss2si"  # noqa: D101
class Cvtsd2ss(_ASMCommand): name = "cvtsd2ss"  # noqa: D101
class Cvtss2sd(_ASMCommand): name = "cvtss2sd"  # noqa: D101


# SSE scalar floating-point arithmetic.
class Addsd(_ASMCommand): name = "addsd"  # noqa: D101
class Addss(_ASMCommand): name = "addss"  # noqa: D101
class Subsd(_ASMCommand): name = "subsd"  # noqa: D101
class Subss(_ASMCommand): name = "subss"  # noqa: D101
class Mulsd(_ASMCommand): name = "mulsd"  # noqa: D101
class Mulss(_ASMCommand): name = "mulss"  # noqa: D101
class Divsd(_ASMCommand): name = "divsd"  # noqa: D101
class Divss(_ASMCommand): name = "divss"  # noqa: D101


class Jp(_JumpCommand): name = "jp"   # noqa: D101  (jump if parity/unordered)
class Ucomisd(_ASMCommand): name = "ucomisd"  # noqa: D101
class Ucomiss(_ASMCommand): name = "ucomiss"  # noqa: D101


class Xorps(_ASMCommand): name = "xorps"  # noqa: D101
