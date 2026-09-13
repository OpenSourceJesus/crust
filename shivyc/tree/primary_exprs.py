"""Primary expression nodes in the AST."""

import sys
import shivyc.ctypes as ctypes
import shivyc.lexer as lexer
import shivyc.tree.general_nodes as general_nodes
from shivyc.ctypes import ArrayCType
from shivyc.errors import CompilerError
from shivyc.il_gen import ILValue
from shivyc.tree.expr_base import _RExprNode, _LExprNode
from shivyc.tree.utils import DirectLValue, report_err
from shivyc.tokens import parse_c_int, Token


class MultiExpr(_RExprNode):
    """Expression that is two expressions joined by comma."""

    def __init__(self, left, right, op):
        """Initialize node."""
        super().__init__()
        self.left = left
        self.right = right
        self.op = op

    def make_il(self, il_code: "il_gen.ILCode", symbol_table: "il_gen.SymbolTable", c):
        """Make code for this node."""
        self.left.make_il(il_code, symbol_table, c)
        return self.right.make_il(il_code, symbol_table, c)


class Number(_RExprNode):
    """Expression that is just a single number."""

    def __init__(self, number):
        """Initialize node."""
        super().__init__()
        self.number = number

    def make_il(self, il_code: "il_gen.ILCode", symbol_table: "il_gen.SymbolTable", c):
        """Make code for a literal number.

        This function does not actually make any code in the IL, it just
        returns a LiteralILValue that can be used in IL code by the caller.
        """
        spelling = str(self.number)
        if lexer.is_float_constant(spelling):
            suffix = spelling[-1]
            ctype = ctypes.flt if suffix in "fF" else ctypes.dbl
            body = spelling.rstrip("fFlL")
            try:
                fval = (float.fromhex(body) if body[:2].lower() == "0x"
                        else float(body))
            except (OverflowError, ValueError):
                # A constant outside the representable range of our floating
                # types (e.g. an 80-bit long-double hex constant, since long
                # double is aliased to double). Report cleanly instead of
                # letting the exception escape as a crash.
                err = "floating constant out of range"
                raise CompilerError(err, self.number.r)
            il_value = ILValue(ctype)
            il_code.register_float_literal(il_value, fval)
            return il_value

        v = parse_c_int(spelling)

        # Determine the literal's type per C rules. Extract the u/l suffix and
        # whether the constant is decimal (vs hex/octal/binary): a decimal
        # constant without a 'u' suffix only takes signed types, while a
        # hex/octal constant or a 'u'-suffixed one may take unsigned types.
        i = len(spelling)
        while i > 0 and spelling[i - 1] in "uUlL":
            i -= 1
        suffix = spelling[i:].lower()
        body = spelling[:i]
        has_u = "u" in suffix
        has_l = "l" in suffix
        is_decimal = (body[:2].lower() not in ("0x", "0b")
                      and not (len(body) > 1 and body[0] == "0"))

        I = (ctypes.integer, ctypes.int_min, ctypes.int_max)
        UI = (ctypes.unsig_int, 0, ctypes.uint_max)
        L = (ctypes.longint, ctypes.long_min, ctypes.long_max)
        # The self-host runtime represents integers as signed 64-bit, so the
        # true unsigned-long maximum (2**64 - 1) is not representable. Clamp the
        # bound to the largest representable value there; a literal beyond it
        # could not be represented natively in any case. The translator folds
        # this guard at translation time and emits only the live branch, so the
        # unrepresentable ctypes.ulong_max is never referenced in native code.
        if sys.implementation.name == "shivyc":
            UL = (ctypes.unsig_longint, 0, ctypes.long_max)
        else:
            UL = (ctypes.unsig_longint, 0, ctypes.ulong_max)
        if ctypes.llp64 and has_l and "ll" not in suffix:
            # LLP64: a single `l` names the 4-byte `long`, which C tries
            # before `long long` (C11 6.4.4.1p5).
            WL = (ctypes.win_long, ctypes.int_min, ctypes.int_max)
            UWL = (ctypes.unsig_win_long, 0, ctypes.uint_max)
            if has_u:
                candidates = [UWL, UL]
            elif is_decimal:
                candidates = [WL, L]
            else:
                candidates = [WL, UWL, L, UL]
        elif has_u:
            candidates = [UL] if has_l else [UI, UL]
        elif is_decimal:
            candidates = [L] if has_l else [I, L]
        else:  # hex / octal / binary without 'u'
            candidates = [L, UL] if has_l else [I, UI, L, UL]

        il_value = None
        for ctype, lo, hi in candidates:
            if lo <= v <= hi:
                il_value = ILValue(ctype)
                break
        if il_value is None:
            err = "integer literal too large to be represented by any " \
                  "integer type"
            raise CompilerError(err, self.number.r)

        il_code.register_literal_var(il_value, v)
        return il_value


class String(_LExprNode):
    """Expression that is a string.

    chars (List(int)) - String this expression represents, as a null-terminated
    list of the ASCII representations of each character.

    """

    def __init__(self, chars, wide=False):
        """Initialize CNode.

        wide - True for an L"..." literal, whose elements are wchar_t (int).
        """
        super().__init__()
        self.chars = chars
        self.wide = wide

    def _lvalue(self, il_code, symbol_table, c):
        el = ctypes.integer if getattr(self, "wide", False) else ctypes.char
        il_value = ILValue(ArrayCType(el, len(self.chars)))
        il_code.register_string_literal(il_value, self.chars)
        return DirectLValue(il_value)


class Identifier(_LExprNode):
    """Expression that is a single identifier."""

    identifier: Token

    def __init__(self, identifier):
        """Initialize node."""
        super().__init__()
        self.identifier = identifier

    def _lvalue(self, il_code, symbol_table, c):
        # An enum constant is an rvalue, not an lvalue, so it has no lvalue
        # form. Returning None (rather than looking it up as a variable, which
        # would wrongly report it undeclared) lets nodes that probe an operand's
        # lvalue -- e.g. array subscript `a[ENUM]` or `&x` -- handle it via
        # make_il instead.
        if symbol_table.lookup_enum_const(self.identifier.content) is not None:
            return None
        var = symbol_table.lookup_variable(self.identifier)
        return DirectLValue(var)

    def make_il(self, il_code: "il_gen.ILCode", symbol_table: "il_gen.SymbolTable", c):
        """Resolve an enum constant to an integer literal, else load lvalue."""
        enum_val = symbol_table.lookup_enum_const(self.identifier.content)
        if enum_val is not None:
            out = ILValue(ctypes.integer)
            il_code.register_literal_var(out, str(enum_val))
            return out
        return super().make_il(il_code, symbol_table, c)

    def make_il_raw(self, il_code: "il_gen.ILCode", symbol_table: "il_gen.SymbolTable", c):
        """Same as make_il for enum constants (no lvalue to decay)."""
        if symbol_table.lookup_enum_const(self.identifier.content) is not None:
            return self.make_il(il_code, symbol_table, c)
        return super().make_il_raw(il_code, symbol_table, c)


class ParenExpr(general_nodes.CNode):
    """Expression in parentheses.

    This is implemented a bit hackily. Rather than being an LExprNode or
    RExprNode like all the other nodes, a paren expression can be either
    depending on what's inside. So for all function calls to this function,
    we simply dispatch to the expression inside.
    """

    def __init__(self, expr):
        """Initialize node."""
        super().__init__()
        self.expr = expr

    def lvalue(self, il_code, symbol_table, c):
        """Return lvalue of this expression."""
        return self.expr.lvalue(il_code, symbol_table, c)

    def make_il(self, il_code: "il_gen.ILCode", symbol_table: "il_gen.SymbolTable", c):
        """Make IL code for this expression."""
        return self.expr.make_il(il_code, symbol_table, c)

    def make_il_raw(self, il_code: "il_gen.ILCode", symbol_table: "il_gen.SymbolTable", c):
        """Make raw IL code for this expression."""
        return self.expr.make_il_raw(il_code, symbol_table, c)


class StmtExpr(_RExprNode):
    """A GCC statement expression: ({ statements... }).

    Evaluates to the value of the last statement if that statement is an
    expression statement, and to void otherwise. Declarations and control flow
    inside the block are permitted and live in their own scope.
    """

    def __init__(self, items):
        """items - the block items (statements/declarations) of the braced block."""
        super().__init__()
        self.items = items

    def make_il(self, il_code: "il_gen.ILCode", symbol_table: "il_gen.SymbolTable", c):
        """Generate IL for the block, returning the last expression's value."""
        symbol_table.new_scope()
        c = c.set_global(False)

        # Linear new_scope/end_scope with a per-item report_err(), matching the
        # idiom used by Compound and the loop nodes. The previous version used
        # try/finally purely to guarantee end_scope(); that rides py2c's
        # exception-frame machinery, which the self-hosted (native) compiler did
        # not lower faithfully here -- the trailing expression's value was lost
        # and the whole statement-expression came out as void, so assigning it
        # (e.g. `int x = ({ ...; e; });`) failed with "invalid conversion". The
        # report_err() context already swallows a CompilerError from any item,
        # so control always reaches end_scope() without try/finally.
        # Linear new_scope/end_scope. The trailing item's value must be read
        # OUTSIDE a report_err() frame: report_err lowers to a setjmp/longjmp
        # exception frame, and a value assigned to `result` inside that frame
        # is not reliably preserved after control leaves it in the self-hosted
        # (native) compiler -- the trailing expression's value was lost and the
        # whole statement-expression came out as void, so `int x = ({...; e;})`
        # failed with "invalid conversion". Non-final statements still run under
        # report_err so a CompilerError in them is collected, not fatal.
        result = None
        n = len(self.items)
        i = 0
        while i < n:
            item = self.items[i]
            if i == n - 1:
                # stmt_expr_value runs the statement and returns its value (the
                # contained expression's value for an expression statement, else
                # None), dispatched virtually -- no isinstance, no dynamic attr.
                result = item.stmt_expr_value(il_code, symbol_table, c)
            else:
                with report_err():
                    item.make_il(il_code, symbol_table, c)
            i = i + 1
        symbol_table.end_scope()

        if result is None:
            # No trailing expression: the statement expression has type void.
            result = ILValue(ctypes.void)
        return result
