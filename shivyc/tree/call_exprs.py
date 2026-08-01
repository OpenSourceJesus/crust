"""Function call expression nodes in the AST."""

import shivyc.ctypes as ctypes
import shivyc.il_cmds.control as control_cmds
from shivyc.errors import CompilerError
from shivyc.il_gen import ILValue
from shivyc.tree.expr_base import _RExprNode
from shivyc.tree.utils import set_type, check_cast


class FuncCall(_RExprNode):
    """Function call.

    func - Expression of type function pointer
    args - List of expressions for each argument
    """
    def __init__(self, func, args):
        """Initialize node."""
        super().__init__()
        self.func = func
        self.args = args

    def make_il(self, il_code: "il_gen.ILCode", symbol_table: "il_gen.SymbolTable", c):
        """Make code for this node."""

        # This is of function pointer type, so func.arg is the function type.
        func = self.func.make_il(il_code, symbol_table, c)

        # If the callee names a function with contracts, check them against
        # statically-known argument lengths before generating the call.
        import shivyc.tree.primary_exprs as _pe
        if isinstance(self.func, _pe.Identifier):
            import shivyc.contracts as _contracts
            _contracts.check_call(
                self, self.func.identifier.content, symbol_table)

        if not func.ctype.is_pointer() or not func.ctype.arg.is_function():
            descrip = "called object is not a function pointer"
            raise CompilerError(descrip, self.func.r)
        elif (func.ctype.arg.ret.is_incomplete()
              and not func.ctype.arg.ret.is_void()):
            # TODO: C11 spec says a function cannot return an array type,
            # but I can't determine how a function would ever be able to return
            # an array type.
            descrip = "function returns non-void incomplete type"
            raise CompilerError(descrip, self.func.r)

        if func.ctype.arg.no_info:
            final_args = self._get_args_without_prototype(
                il_code, symbol_table, c)
        else:
            final_args = self._get_args_with_prototype(
                func.ctype.arg, il_code, symbol_table, c)

        ret = ILValue(func.ctype.arg.ret)
        ret_ctype = func.ctype.arg.ret
        sret = (ret_ctype.is_struct_union() and ret_ctype.size > 16
                and not getattr(func.ctype.arg, "variadic", False))
        if sret:
            # SysV memory-class return: allocate result storage here and pass
            # its address as a hidden first integer argument. The callee writes
            # the struct through that pointer, so the call itself returns
            # nothing in registers that we need.
            from shivyc.tree.utils import DirectLValue
            addr = DirectLValue(ret).addr(il_code)
            final_args = [addr] + final_args
        call = control_cmds.Call(func, final_args, ret)
        if sret:
            call.void_return = True
            call.ret = None
        call.variadic = getattr(func.ctype.arg, "variadic", False)
        il_code.add(call)
        return ret

    def _get_args_without_prototype(self, il_code, symbol_table, c):
        """Return list of argument ILValues for function this represents.

        Use _get_args_without_prototype when the function this represents
        has no prototype. This function only performs integer promotion on the
        arguments before passing them to the called function.
        """
        final_args = []
        for arg_given in self.args:
            arg = arg_given.make_il(il_code, symbol_table, c)

            # perform integer promotions
            if arg.ctype.is_arith() and arg.ctype.size < 4:
                arg = set_type(arg, ctypes.integer, il_code)

            final_args.append(arg)
        return final_args

    def _get_args_with_prototype(self, func_ctype, il_code, symbol_table, c):
        """Return list of argument ILValues for function this represents.

        Use _get_args_with_prototype when the function this represents
        has a prototype. This function converts all passed arguments to
        expected types.
        """
        arg_types = func_ctype.args
        variadic = getattr(func_ctype, "variadic", False)

        too_few = len(self.args) < len(arg_types)
        wrong_fixed = (not variadic) and len(arg_types) != len(self.args)
        if too_few or wrong_fixed:
            err = ("incorrect number of arguments for function call"
                   f" (expected {len(arg_types)}, have {len(self.args)})")

            if self.args:
                raise CompilerError(err, self.args[-1].r)
            else:
                raise CompilerError(err, self.r)

        final_args = []
        for i, arg_given in enumerate(self.args):
            arg = arg_given.make_il(il_code, symbol_table, c)
            if i < len(arg_types):
                check_cast(arg, arg_types[i], arg_given.r)
                final_args.append(
                    set_type(arg, arg_types[i].make_unqual(), il_code))
            else:
                # Variadic argument: apply the default argument promotions
                # (integer promotion; small integer types widen to int).
                if arg.ctype.is_arith() and arg.ctype.size < 4:
                    arg = set_type(arg, ctypes.integer, il_code)
                final_args.append(arg)
        return final_args


class VaStartAddr(_RExprNode):
    """The `__builtin_va_start_addr()` builtin.

    Returns a `char *` pointing at the first variadic argument of the
    enclosing variadic function. Used by the va_start macro in <stdarg.h>.
    """

    def __init__(self):
        """Initialize node."""
        super().__init__()

    def make_il(self, il_code: "il_gen.ILCode", symbol_table: "il_gen.SymbolTable", c):
        """Make code for this node."""
        import shivyc.il_cmds.value as value_cmds
        from shivyc.ctypes import PointerCType
        named = getattr(c, "vararg_named", None)
        if named is None:
            err = "va_start used outside of a variadic function"
            raise CompilerError(err, self.r)
        out = ILValue(PointerCType(ctypes.char))
        il_code.add(value_cmds.VaStartAddr(out, named,
                                           getattr(c, "vararg_base", None)))
        return out


class VaArg(_RExprNode):
    """The `__builtin_va_arg(ap, type)` builtin.

    Reads the next variadic argument (of the given type) from the va_list
    `ap` and advances `ap`. ShivyC passes every variadic argument on the
    stack and a va_list is a `char *` walking those 8-byte-aligned slots, so
    this reads `*(type *)ap` and then advances `ap` by the slot size.
    """

    def __init__(self, ap, node):
        """Initialize node. `ap` is the va_list expression, `node` the type."""
        super().__init__()
        self.ap = ap
        self.node = node

    def make_il(self, il_code: "il_gen.ILCode", symbol_table: "il_gen.SymbolTable", c):
        """Make code for this node."""
        import shivyc.il_cmds.value as value_cmds
        import shivyc.il_cmds.math as math_cmds
        import shivyc.tree.general_nodes as general_nodes
        from shivyc.ctypes import PointerCType
        from shivyc.tree.utils import set_type

        # Resolve the requested type using the declaration helpers.
        helper = general_nodes.Declaration(self.node)
        helper.set_self_vars(il_code, symbol_table, c)
        base_type, _ = helper.make_specs_ctype(self.node.specs, True)
        ctype, _ = helper.make_ctype(self.node.decls[0], base_type)

        # Current va_list pointer value, and its lvalue (to advance it).
        ap_val = self.ap.make_il(il_code, symbol_table, c)
        ap_lval = self.ap.lvalue(il_code, symbol_table, c)
        if ap_lval is None:
            err = "__builtin_va_arg requires a modifiable va_list"
            raise CompilerError(err, self.r)

        # result = *(ctype *)ap
        ptr = set_type(ap_val, PointerCType(ctype), il_code)
        out = ILValue(ctype)
        il_code.add(value_cmds.ReadAt(out, ptr))

        # Advance ap by the 8-byte-aligned stack slot size of ctype.
        slot = max(8, (ctype.size + 7) // 8 * 8)
        as_int = set_type(ap_val, ctypes.unsig_longint, il_code)
        slot_val = ILValue(ctypes.unsig_longint)
        il_code.register_literal_var(slot_val, slot)
        newint = ILValue(ctypes.unsig_longint)
        il_code.add(math_cmds.Add(newint, as_int, slot_val))
        newptr = set_type(newint, ap_val.ctype, il_code)
        ap_lval.set_to(newptr, il_code, self.r)

        return out
