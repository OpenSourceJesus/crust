"""Memory operation and member access expression nodes in the AST."""

import shivyc.ctypes as ctypes
import shivyc.il_cmds.math as math_cmds
from shivyc.ctypes import PointerCType
from shivyc.errors import CompilerError
from shivyc.il_gen import ILValue
from shivyc.tree.expr_base import _RExprNode, _LExprNode
from shivyc.tree.utils import (IndirectLValue, DirectLValue, RelativeLValue,
                               BitFieldLValue, UnalignedLValue, get_size)


class AddrOf(_RExprNode):
    """Address-of expression."""

    def __init__(self, expr):
        """Initialize node."""
        super().__init__()
        self.expr = expr

    def make_il(self, il_code: "il_gen.ILCode", symbol_table: "il_gen.SymbolTable", c):
        """Make code for this node."""
        lvalue = self.expr.lvalue(il_code, symbol_table, c)
        if lvalue:
            # If a tracked string-pointer's address is taken, it could be
            # mutated through that pointer, so stop trusting its length.
            import shivyc.tree.primary_exprs as _pe
            if isinstance(self.expr, _pe.Identifier):
                import shivyc.contracts as _contracts
                try:
                    _contracts.clear_known_len(
                        symbol_table.lookup_variable(self.expr.identifier))
                except CompilerError:
                    pass
            # Taking the address of a struct lets its bytes escape tracked
            # member access (memcpy, casts, I/O), so it must keep all members.
            obj_ctype = lvalue.ctype()
            if obj_ctype and obj_ctype.is_struct_union():
                import shivyc.member_elim as member_elim
                member_elim.mark_ineligible(getattr(obj_ctype, "tag", None))
            return lvalue.addr(il_code)
        else:
            err = "operand of unary '&' must be lvalue"
            raise CompilerError(err, self.expr.r)


class Deref(_LExprNode):
    """Dereference expression."""

    def __init__(self, expr):
        """Initialize node."""
        super().__init__()
        self.expr = expr

    def _lvalue(self, il_code, symbol_table, c):
        addr = self.expr.make_il(il_code, symbol_table, c)

        if not addr.ctype.is_pointer():
            err = "operand of unary '*' must have pointer type"
            raise CompilerError(err, self.expr.r)

        return IndirectLValue(addr)


class ArraySubsc(_LExprNode):
    """Array subscript."""

    def __init__(self, head, arg):
        """Initialize node."""
        super().__init__()
        self.head = head
        self.arg = arg

    def _lvalue(self, il_code, symbol_table, c):
        """Return lvalue form of this node.

        We have two main cases here. The first case covers most simple
        situations, like `array[5]` or `array[x+3]`, and the second case
        covers more complex situations like `array[4][2]` or an array
        within a struct.

        In the first case, one of the two operands is a DirectLValue array
        (i.e. just a variable, not a lookup into another object). This
        means it will have a spot in memory assigned to it by the register
        allocator, and we can return a RelativeLValue object from this
        function. This case corresponds to `matched` being True. A
        RelativeLValue object usually becomes an assembly command like
        `[rbp-40+3*rax]`, which is more efficient than manually computing
        the address like happens in the second case.

        In the second case, neither operand is a DirectLValue array. This is
        the case for two-dimensional arrays, for example. Here, we proceed
        naively and get the address for the pointer side. This is a little
        bit less efficient. Ideally, in cases like `array[2][4]` where the
        lookup address could be computed at compile-time, we'd be able to do
        that, but this is not yet supported (TODO).

        """

        # One operand should be pointer to complete object type, and the
        # other should be any integer type.

        head_lv = self.head.lvalue(il_code, symbol_table, c)
        arg_lv = self.arg.lvalue(il_code, symbol_table, c)

        matched = False
        if isinstance(head_lv, DirectLValue) and head_lv.ctype().is_array():
            array, arith = self.head, self.arg
            matched = True
        elif isinstance(arg_lv, DirectLValue) and arg_lv.ctype().is_array():
            array, arith = self.arg, self.head
            matched = True

        if matched:
            # If one operand was a DirectLValue array
            array_val = array.make_il_raw(il_code, symbol_table, c)
            arith_val = arith.make_il(il_code, symbol_table, c)

            if arith_val.ctype.is_integral():
                return self.array_subsc(array_val, arith_val)

        else:
            # Neither operand was a DirectLValue array
            head_val = self.head.make_il(il_code, symbol_table, c)
            arg_val = self.arg.make_il(il_code, symbol_table, c)

            if head_val.ctype.is_pointer() and arg_val.ctype.is_integral():
                return self.pointer_subsc(head_val, arg_val, il_code)
            elif arg_val.ctype.is_pointer() and head_val.ctype.is_integral():
                return self.pointer_subsc(head_val, arg_val, il_code)

        descrip = "invalid operand types for array subscriping"
        raise CompilerError(descrip, self.r)

    def pointer_subsc(self, point, arith, il_code):
        """Return the LValue for this node.

        This function is called in the case where one operand is a pointer
        and the other operand is an integer.
        """
        if not point.ctype.arg.is_complete():
            err = "cannot subscript pointer to incomplete type"
            raise CompilerError(err, self.r)

        shift = get_size(point.ctype.arg, arith, il_code)
        out = ILValue(point.ctype)
        il_code.add(math_cmds.Add(out, point, shift))
        return IndirectLValue(out)

    def array_subsc(self, array, arith):
        """Return the LValue for this node.

        This function is called in the case where one operand is an array
        and the other operand is an integer.
        """
        el = array.ctype.el
        return RelativeLValue(el, array, el.size, arith)


class _ObjLookup(_LExprNode):
    """Struct/union object lookup (. or ->)"""

    def __init__(self, head, member):
        """Initialize node."""
        super().__init__()
        self.head = head
        self.member = member

    def get_offset_info(self, struct_ctype):
        """Given a struct ctype, return the member offset and ctype.

        If the given ctype is None, emits the error for requesting a member
        in something not a structure or union.
        """
        if not struct_ctype or not struct_ctype.is_struct_union():
            err = "request for member in something not a structure or union"
            raise CompilerError(err, self.r)

        # Record the access for the whole-program unused-member analysis.
        import shivyc.member_elim as member_elim
        member_elim.record_access(
            getattr(struct_ctype, "tag", None), self.member.content)

        offset, ctype = struct_ctype.get_offset(self.member.content)
        if offset is None:
            err = f"structure or union has no member '{self.member.content}'"
            raise CompilerError(err, self.r)

        if struct_ctype.is_const():
            ctype = ctype.make_const()

        return offset, ctype

    def _wrap_unaligned(self, base_lvalue, struct_ctype, member_ctype,
                        head_unaligned):
        """Wrap the member in an UnalignedLValue if it can be misaligned.

        It can when packing lowered its alignment, or when the object it is
        a member of is itself possibly misaligned -- `p->inner.x`, with
        `inner` in a packed struct, is as unaligned as `inner` is. A member
        with alignment 1 never needs it.
        """
        if member_ctype.alignment() <= 1:
            return base_lvalue
        if head_unaligned or struct_ctype.maybe_misaligned(member_ctype):
            return UnalignedLValue(base_lvalue)
        return base_lvalue

    def _wrap_bitfield(self, base_lvalue, struct_ctype):
        """Wrap an lvalue in a BitFieldLValue if the member is a bitfield."""
        info = struct_ctype.get_bitfield(self.member.content)
        if info is None:
            return base_lvalue
        width, signed = info
        return BitFieldLValue(base_lvalue, width, signed)


class ObjMember(_ObjLookup):
    """Struct/union object member (. operator)"""

    def _lvalue(self, il_code, symbol_table, c):
        head_lv = self.head.lvalue(il_code, symbol_table, c)
        if head_lv is None:
            # `.` applied to a struct *rvalue* (C99 6.5.2.3): e.g. `f().m` or
            # `(cond ? a : b).m`. There is no lvalue to address, so materialize
            # the struct into a temporary and read the member from it. The
            # result is a non-assignable rvalue (writing it updates the temp).
            import shivyc.il_cmds.value as value_cmds
            head_val = self.head.make_il(il_code, symbol_table, c)
            offset, ctype = self.get_offset_info(head_val.ctype)
            tmp = ILValue(head_val.ctype)
            il_code.add(value_cmds.Set(tmp, head_val))
            struct_addr = ILValue(PointerCType(head_val.ctype))
            il_code.add(value_cmds.AddrOf(struct_addr, tmp))
            shift = ILValue(ctypes.longint)
            il_code.register_literal_var(shift, str(offset))
            out = ILValue(PointerCType(ctype))
            il_code.add(math_cmds.Add(out, struct_addr, shift))
            base = self._wrap_unaligned(IndirectLValue(out), head_val.ctype,
                                        ctype, False)
            return self._wrap_bitfield(base, head_val.ctype)
        struct_ctype = head_lv.ctype() if head_lv else None
        offset, ctype = self.get_offset_info(struct_ctype)

        if isinstance(head_lv, DirectLValue):
            head_val = self.head.make_il(il_code, symbol_table, c)
            base = RelativeLValue(ctype, head_val, offset)
        else:
            struct_addr = head_lv.addr(il_code)

            shift = ILValue(ctypes.longint)
            il_code.register_literal_var(shift, str(offset))

            out = ILValue(PointerCType(ctype))
            il_code.add(math_cmds.Add(out, struct_addr, shift))
            base = IndirectLValue(out)

        base = self._wrap_unaligned(base, struct_ctype, ctype,
                                    isinstance(head_lv, UnalignedLValue))
        return self._wrap_bitfield(base, struct_ctype)


class ObjPtrMember(_ObjLookup):
    """Struct/union pointer object member (-> operator)"""

    def _lvalue(self, il_code, symbol_table, c):
        struct_addr = self.head.make_il(il_code, symbol_table, c)
        if not struct_addr.ctype.is_pointer():
            err = "first argument of '->' must have pointer type"
            raise CompilerError(err, self.r)

        offset, ctype = self.get_offset_info(struct_addr.ctype.arg)
        shift = ILValue(ctypes.longint)
        il_code.register_literal_var(shift, str(offset))

        out = ILValue(PointerCType(ctype))
        il_code.add(math_cmds.Add(out, struct_addr, shift))
        base = self._wrap_unaligned(IndirectLValue(out), struct_addr.ctype.arg,
                                    ctype, False)
        return self._wrap_bitfield(base, struct_addr.ctype.arg)
