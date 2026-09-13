"""This module defines all of the C types recognized by the compiler."""

import copy
import sys

import shivyc.token_kinds as token_kinds


# Set True by the -f-pointer-compression flag. When set, data/object pointers
# are 4 bytes instead of 8 (V8/x32-style flat 32-bit pointers): the program is
# linked non-PIE in the low 4 GiB (-f-low-mem) so every address fits in 32 bits
# and is recovered by zero-extension. This shrinks pointer-dense data and the
# pointer fields of structs. POINTER_SIZE is read by PointerCType so sizeof,
# struct layout, and the size-driven asm backend all follow automatically.
pointer_compression = False


def pointer_size():
    return 4 if pointer_compression else 8


class CType:
    """Represents a C type, like `int` or `double` or a struct or union.

    size (int) - The result of sizeof on this type.
    """

    def __init__(self, size, const=False):
        """Initialize type."""
        self.size = size
        self.const = const

        # Required because casting to bool is special in C11.
        self._bool = False

        # Required for super hacky struct trick, see the weak_compat
        # function for the struct.
        self._orig = self

    def alignment(self):
        """Return the type's natural (ABI) alignment in bytes.

        For scalar types this equals the size (char=1 .. 8-byte types=8).
        Aggregate types override this. Used by `_Alignof`.
        """
        return self.size if self.size and self.size <= 8 else 8

    def weak_compat(self, other):
        """Check for weak compatibility with `other` ctype.

        Two types are "weakly compatible" if their unqualified version are
        compatible.
        """
        raise NotImplementedError

    def set_members(self, members, bitfields=None):
        """Populate a struct/union type's members. Only struct/union types
        support this; defined on the base so it is part of the canonical CType
        vtable and `ctype.set_members(...)` dispatches virtually on a CType
        whose concrete class isn't statically known."""
        raise NotImplementedError

    def is_complete(self):
        """Check whether this is a complete type."""
        return False

    def is_incomplete(self):
        """Check whether this is an incomplete type.

        An object type must be either complete or incomplete.
        """
        return False

    def is_object(self):
        """Check whether this is an object type."""
        return False

    def is_arith(self):
        """Check whether this is an arithmetic type."""
        return False

    def is_integral(self):
        """Check whether this is an integral type."""
        return False

    def is_floating(self):
        """Check whether this is a floating-point type."""
        return False

    def is_pointer(self):
        """Check whether this is a pointer type."""
        return False

    def is_function(self):
        """Check whether this is a function type."""
        return False

    def is_void(self):
        """Check whether this is a void type."""
        return False

    def is_bool(self):
        """Check whether this is a boolean type."""
        return self._bool

    def is_array(self):
        """Check whether this is an array type."""
        return False

    def is_struct_union(self):
        """Check whether this has struct or union type."""
        return False

    def make_unsigned(self):
        """Return an unsigned version of this type."""
        raise NotImplementedError

    def compatible(self, other):
        """Check whether given `other` C type is compatible with self."""
        return self.weak_compat(other) and self.const == other.const

    def is_scalar(self):
        """Check whether this has scalar type."""
        return self.is_arith() or self.is_pointer()

    def is_const(self):
        """Check whether this is a const type."""
        return self.const

    def make_const(self):
        """Return a const version of this type."""
        const_self = copy.copy(self)
        const_self.const = True
        return const_self

    def make_unqual(self):
        """Return an unqualified version of this type."""
        unqual_self = copy.copy(self)
        unqual_self.const = False
        return unqual_self


class IntegerCType(CType):
    """Represents an integer C type, like 'unsigned long' or 'bool'.

    This class must be instantiated only once for each distinct integer C type.

    size (int) - The result of sizeof on this type.
    signed (bool) - Whether this type is signed.

    """

    def __init__(self, size, signed):
        """Initialize type."""
        self.signed = signed
        super().__init__(size)

    def weak_compat(self, other):
        """Check whether two types are compatible."""

        # TODO: _orig stuff is hacky...
        # Find a more reliable way to talk about types being equal.
        return (other._orig == self._orig and self.signed == other.signed
                and self.is_bool() == other.is_bool())

    def is_complete(self):
        """Check if this is a complete type."""
        return True

    def is_object(self):
        """Check if this is an object type."""
        return True

    def is_arith(self):
        """Check whether this is an arithmetic type."""
        return True

    def is_integral(self):
        """Check whether this is an integral type."""
        return True

    def make_unsigned(self):
        """Return an unsigned version of this type."""
        unsig_self = copy.copy(self)
        unsig_self.signed = False
        return unsig_self


class FloatCType(CType):
    """Represents a floating-point C type, like 'float' or 'double'."""

    def __init__(self, size, long_double=False):
        super().__init__(size)
        # True only for the unsupported 80-bit long double type. It is allowed
        # to appear in prototypes but rejected where it would allocate storage
        # or perform arithmetic (see the long-double checks in the tree).
        self.long_double = long_double

    def weak_compat(self, other):
        return other._orig == self._orig

    def is_complete(self):
        return True

    def is_object(self):
        return True

    def is_arith(self):
        return True

    def is_floating(self):
        return True


class VoidCType(CType):
    """Represents a void C type.

    This class must be instantiated only once.

    """

    def __init__(self):
        """Initialize type."""
        super().__init__(1)

    def weak_compat(self, other):
        """Return True iff other is a compatible type to self."""
        return other.is_void()

    def is_incomplete(self):
        """Check if this is a complete type."""
        return True

    def is_void(self):
        """Check whether this is a void type."""
        return True

    def is_object(self):
        return True


class PointerCType(CType):
    """Represents a pointer C type.

    arg (CType) - Type pointed to.

    """

    arg: "CType"

    def __init__(self, arg, const=False):
        """Initialize type."""
        self.arg = arg
        super().__init__(pointer_size(), const)

    def weak_compat(self, other):
        """Return True iff other is a compatible type to self."""
        return other.is_pointer() and self.arg.compatible(other.arg)

    def is_complete(self):
        """Check if this is a complete type."""
        return True

    def is_pointer(self):
        """Check whether this is a pointer type."""
        return True

    def is_object(self):
        """Check if this is an object type."""
        return True


class ArrayCType(CType):
    """Represents an array C type.

    el (CType) - Type of each element in array.
    n (int) - Size of array (or None if this is incomplete)

    """

    # obj-typed so the incomplete-array sentinel `None` is representable and
    # stays distinct from `0` (a GNU zero-length array). As a plain int, None
    # would collapse to 0, making is_complete() always true and defeating
    # array-length deduction from an initializer (`T a[] = {...}`).
    n: "object"

    def __init__(self, el, n: "object"):
        """Initialize type."""
        self.el = el
        self.n = n
        # n == 0 is a GNU zero-length array (a complete type of size 0, used for
        # trailing variable-length struct members); n is None for an incomplete
        # array, whose size is a placeholder of one element.
        super().__init__((1 if n is None else n) * self.el.size)

    def alignment(self):
        """An array's alignment is that of its element type."""
        return self.el.alignment()

    def compatible(self, other):
        """Return True iff other is a compatible type to self."""
        return (other.is_array() and self.el.compatible(other.el)
                and (self.n is None or other.n is None or self.n == other.n))

    def is_complete(self):
        """Check if this is a complete type."""
        return self.n is not None

    def is_incomplete(self):
        return not self.is_complete()

    def is_object(self):
        """Check if this is an object type."""
        return True

    def is_array(self):
        """Check whether this is an array type."""
        return True


class FunctionCType(CType):
    """Represents a function C type.

    args (List(CType)) - List of the argument ctypes, from left to right, or
    None if unspecified.
    ret (CType) - Return value of the function.
    no_info (bool) - True if the function does not have any parameter info.
    This occurs when the function is declared as `int f();` with nothing in
    between the parentheses.
    """

    ret: "CType"

    def __init__(self, args, ret, no_info):
        """Initialize type."""
        self.args = args
        self.ret = ret
        self.no_info = no_info
        self.variadic = False
        super().__init__(1)

    def weak_compat(self, other):
        """Return True iff other is a compatible type to self."""

        if not other.is_function():
            return False
        elif not self.ret.compatible(other.ret):
            return False
        elif not self.no_info and not other.no_info:
            if len(self.args) != len(other.args):
                return False
            elif any(not a1.compatible(a2) for a1, a2 in
                     zip(self.args, other.args)):
                return False

        # TODO: There are special rules for compatibility between a function
        # with parameter list and a function without parameter list. See
        # 6.7.6.3.15.

        return True

    def is_function(self):
        """Check if this is a function type."""
        return True


class _UnionStructCType(CType):
    """Base class for struct and union C types.

    tag - Name of the struct/union as a string, or None if it's anonymous

    members - List of members of this type. Each element of the list should be
    a tuple (str, ctype) where `str` is the string of the identifier used to
    access that member and ctype is the ctype of that member.
    complete - Boolean indicating whether this type is complete
    """

    def __init__(self, tag, members=None):
        self.tag = tag
        self.members = members
        self.offsets = {}
        # Map from member name to (bit_width, signed) for any member declared
        # as a bitfield. Members not in this map are ordinary members.
        self.bitfields = {}
        super().__init__(1)

    def alignment(self):
        """A struct/union's alignment is the max alignment of its members
        (regardless of ShivyC's packed layout). Used by `_Alignof`."""
        if not self.members:
            return 1
        return max((c.alignment() for _, c in self.members), default=1)

    def weak_compat(self, other):
        """Return True if other is a compatible type to self.

        Within a single translation unit, two structs are compatible if
        they are the exact same declaration.
        """
        return self._orig is other._orig

    def is_complete(self):
        """Check whether this is a complete type."""
        return self.members is not None

    def is_incomplete(self):
        return not self.is_complete()

    def is_object(self):
        """Check whether this is an object type."""
        return True

    def is_struct_union(self):
        """Check whether this has struct or union type."""
        return True

    def get_offset(self, member):
        """Get the offset and type of a given member.

        If the member does not exist, this function returns None tuple.
        """
        return self.offsets.get(member, (None, None))

    def get_bitfield(self, member):
        """Return (bit_width, signed) if member is a bitfield, else None."""
        return self.bitfields.get(member)

    def set_members(self, members, bitfields=None):
        """Add the given members to this type.

        The members list is given in the format as described in the class
        description. `bitfields` optionally maps member names to
        (bit_width, signed) tuples.
        """
        raise NotImplementedError

    def _promote_anon(self, member, ctype, base_offset):
        """Promote members of a C11 anonymous struct/union member.

        An anonymous member (internal name "<anon-member-N>") makes its own
        members accessible directly on the enclosing struct/union, at the
        anonymous member's base_offset plus each inner member's offset. Inner
        anonymous members are already promoted within `ctype.offsets`, so this
        flattens arbitrarily nested anonymous members.
        """
        if not (isinstance(member, str)
                and member.startswith("<anon-member-")
                and ctype.is_struct_union()):
            return
        for inner_name, (inner_off, inner_ctype) in ctype.offsets.items():
            if inner_name.startswith("<anon-member-"):
                continue
            self.offsets[inner_name] = (base_offset + inner_off, inner_ctype)
            bf = ctype.bitfields.get(inner_name)
            if bf is not None:
                self.bitfields[inner_name] = bf


class StructCType(_UnionStructCType):
    """Represents a struct ctype."""

    def set_members(self, members, bitfields=None):
        import shivyc.member_elim as member_elim
        has_anon = any(isinstance(m, str) and m.startswith("<")
                       for m, _ in members)

        if member_elim.collecting():
            # Whole-program analysis: record this struct's named members, and
            # mark layout-exposing situations ineligible.
            if has_anon:
                # Structs with anonymous members (e.g. PyObject) are too
                # intricate to shrink safely; keep all members.
                member_elim.mark_ineligible(self.tag)
            member_elim.record_all_members(
                self.tag,
                [m for m, _ in members
                 if isinstance(m, str) and not m.startswith("<")])
            # A nested struct/union (or array thereof) has its layout exposed
            # through this enclosing object, so it must keep all members.
            for _, ctype in members:
                inner = ctype
                while inner.is_array():
                    inner = inner.el
                if inner.is_struct_union():
                    member_elim.mark_ineligible(getattr(inner, "tag", None))
        else:
            # Real compile: drop members the analysis proved unused (never any
            # synthetic anonymous-member placeholder).
            rem = member_elim.removable_for(self.tag)
            if rem:
                members = [(m, c) for (m, c) in members if m not in rem]
                if bitfields:
                    bitfields = {m: b for m, b in bitfields.items()
                                 if m not in rem}

        self.members = members
        self.bitfields = bitfields or {}

        # Lay members out at their natural alignment (standard C / SysV), and
        # pad the whole struct up to its own alignment. ShivyC previously packed
        # members with no padding, which kept structs smaller but placed an
        # 8-byte member (e.g. a union or pointer after an int) at a misaligned
        # offset -- straddling the SysV eightbyte boundary, so passing such a
        # struct by value in registers cross-TU or to/from gcc-compiled code
        # dropped half its bytes. Aligned layout matches the platform ABI.
        cur_offset = 0
        for member, ctype in members:
            al = ctype.alignment()
            if al > 1:
                cur_offset = (cur_offset + al - 1) // al * al
            self.offsets[member] = cur_offset, ctype
            self._promote_anon(member, ctype, cur_offset)
            # A flexible array member (an incomplete array, always the last
            # member -- enforced by the caller) occupies no space.
            if ctype.is_array() and ctype.is_incomplete():
                continue
            cur_offset += ctype.size

        struct_al = self.alignment()
        if struct_al > 1:
            cur_offset = (cur_offset + struct_al - 1) // struct_al * struct_al
        self.size = cur_offset


class UnionCType(_UnionStructCType):
    """Represents a union ctype.

    Similar to struct type, but different offset is used.
    """

    def set_members(self, members, bitfields=None):
        import shivyc.member_elim as member_elim
        if member_elim.collecting():
            # A struct sharing storage with other union members has its layout
            # exposed; it must keep all members.
            for _, ctype in members:
                inner = ctype
                while inner.is_array():
                    inner = inner.el
                if inner.is_struct_union():
                    member_elim.mark_ineligible(getattr(inner, "tag", None))
        self.members = members
        self.bitfields = bitfields or {}
        raw = max([ctype.size for _, ctype in members], default=0)
        al = self.alignment()
        if al > 1:
            raw = (raw + al - 1) // al * al
        self.size = raw
        for member, ctype in members:
            self.offsets[member] = 0, ctype
            self._promote_anon(member, ctype, 0)


# These definitions are here to permit convenient creation of new integer,
# char, etc. types. However, DO NOT test whether a ctype is one of these by
# checking equality. That is, do not use `ctype == ctypes.integer` to check
# whether `ctype` is an integer. This is because functions like
# "make_unsigned" or "make_const" return a copy of the type, so equality
# checking will not work.


void = VoidCType()

bool_t = IntegerCType(1, False)
bool_t._bool = True

char = IntegerCType(1, True)
unsig_char = IntegerCType(1, False)
unsig_char_max = 255

short = IntegerCType(2, True)
unsig_short = IntegerCType(2, False)

integer = IntegerCType(4, True)
unsig_int = IntegerCType(4, False)
int_max = 2147483647
int_min = -2147483648

longint = IntegerCType(8, True)
unsig_longint = IntegerCType(8, False)
long_max = 9223372036854775807
long_min = -9223372036854775808

# LLP64 (64-bit Windows): `long` is 4 bytes while `long long` and pointers stay
# 8. The compiler uses `longint` internally as its pointer-width integer
# (sizeof, pointer arithmetic, size_t), and `long long` already folds onto it,
# so only the spelled keyword `long` changes -- onto this distinct 4-byte type.
# Distinct from `integer` so that `long *` and `int *` stay incompatible, as
# they are under MSVC; arithmetic is size-driven, so it behaves as int does.
# Set per compile by set_data_model.
llp64 = False
win_long = IntegerCType(4, True)
unsig_win_long = IntegerCType(4, False)


def set_data_model(model):
    """Select "lp64" (every Unix target) or "llp64" (64-bit Windows)."""
    global llp64
    llp64 = model == "llp64"

# Upper bounds for the unsigned integer types, used when choosing the type of
# an integer literal. These are module-level constants (not function locals) so
# the self-host translator constant-folds them into correctly-sized literals; a
# local would be typed as a 32-bit int and truncate (e.g. uint_max -> -1).
# uint_max fits in 64 bits; ulong_max (2**64 - 1) does not and is only used on
# CPython (see primary_exprs, which clamps to long_max under the self-host
# runtime, whose integers are signed 64-bit).
uint_max = 4294967295
ulong_max = 18446744073709551615


flt = FloatCType(4)
dbl = FloatCType(8)
longdouble = FloatCType(8, long_double=True)

# Set True by the -f-long-double-as-double flag. When set, `long double` is
# aliased to `double` (64-bit) with a warning instead of being rejected. This
# compiler never implements true 80-bit extended precision.
long_double_as_double = False

# Set True when the target ABI itself defines `long double` as `double`, as
# Apple's arm64 ABI does. Unlike the flag above this is not an approximation,
# so `long double` becomes `double` silently: it is exactly what clang does
# for that target, and what libSystem's `sinl` etc. expect.
long_double_is_double_abi = False


simple_types = {token_kinds.void_kw: void,
                token_kinds.bool_kw: bool_t,
                token_kinds.char_kw: char,
                token_kinds.short_kw: short,
                token_kinds.int_kw: integer,
                token_kinds.long_kw: longint,
                token_kinds.float_kw: flt,
                token_kinds.double_kw: dbl}


class AutoCType:
    """Sentinel type for the GCC `__auto_type` specifier.

    The real type is inferred from the declaration's initializer when the
    declaration is processed, so this placeholder only needs to answer the few
    predicates the declaration machinery queries before that inference runs.
    """

    def is_function(self):
        return False

    def is_array(self):
        return False

    def is_incomplete(self):
        return False

    def is_struct_union(self):
        return False


# Singleton sentinel used wherever `__auto_type` appears as a type specifier.
auto_type = AutoCType()
