"""Base classes for expression nodes in the AST."""

from shivyc.ctypes import PointerCType
from shivyc.tree.base_nodes import CNode
from shivyc.tree.utils import set_type


class _ExprNode(CNode):
    """Base class for representing expression nodes in the AST.

    There are two types of expression nodes, RExprNode and LExprNode.
    An expression node which can be used as an lvalue (that is, an expression
    node which can be the argument of an address-of operator) derives from
    LExprNode. Expression nodes which cannot be used as lvalues derive from
    RExprNode.
    """
    def __init__(self):
        """Initialize this ExprNode."""
        super().__init__()

    def make_il(self, il_code, symbol_table, c):
        """Generate IL code for this node and returns ILValue.

        Note that a RExprNode-derived node can never return an ILValue of
        function or array type because these must decay to addresses,
        and because an RExprNode does not represent an lvalue, no address
        can exist.

        il_code - ILCode object to add generated code to.
        symbol_table - Symbol table for current node.
        c - Context for current node, as above. This function should not
        modify this object.
        return - An ILValue representing the result of this computation.
        """
        raise NotImplementedError

    def make_il_raw(self, il_code, symbol_table, c):
        """As above, but do not decay the result."""
        raise NotImplementedError

    def lvalue(self, il_code, symbol_table, c):
        """Return the LValue representing this node.

        If this node has no LValue representation, return None.
        """
        raise NotImplementedError


class _RExprNode(_ExprNode):
    """Base class for representing an rvalue expression node in the AST.

    An RExprNode-derived node implements only the _make_il function.
    """
    def __init__(self):  # noqa D102
        CNode.__init__(self)

    def make_il(self, il_code, symbol_table, c):  # noqa D102
        raise NotImplementedError

    def make_il_raw(self, il_code, symbol_table, c):  # noqa D102
        return self.make_il(il_code, symbol_table, c)

    def lvalue(self, il_code, symbol_table, c):  # noqa D102
        return None


class _LExprNode(_ExprNode):
    """Base class for representing an lvalue expression node in the AST.

    An LExprNode-derived node implements only the _lvalue function. This
    function returns an LValue object representing this node. The
    implementation of this class automatically sets up the appropriate
    make_il function which calls the lvalue implementation.

    Note that calling both make_il and make_il_raw for a single node may
    generate unnecessary or repeated code!
    """

    def __init__(self):  # noqa D102
        super().__init__()
        self._cache_lvalue = None

    def make_il(self, il_code, symbol_table, c):  # noqa D102
        # An lvalue node read as a value is where ReadAt is emitted, so this is
        # the funnel that gives a dereference its own position rather than the
        # enclosing statement's.
        prev = il_code.set_range(self.r)
        try:
            return self._make_il(il_code, symbol_table, c)
        finally:
            il_code.restore_range(prev)

    def _make_il(self, il_code, symbol_table, c):
        lvalue = self.lvalue(il_code, symbol_table, c)

        # Decay array
        if lvalue.ctype().is_array():
            addr = lvalue.addr(il_code)
            return set_type(addr, PointerCType(lvalue.ctype().el), il_code)

        # Decay function
        elif lvalue.ctype().is_function():
            return lvalue.addr(il_code)

        # Nothing to decay
        else:
            return lvalue.val(il_code)

    def make_il_raw(self, il_code, symbol_table, c):  # noqa D102
        prev = il_code.set_range(self.r)
        try:
            return self.lvalue(il_code, symbol_table, c).val(il_code)
        finally:
            il_code.restore_range(prev)

    def lvalue(self, il_code, symbol_table, c):
        """Return an LValue object representing this node."""
        if not self._cache_lvalue:
            prev = il_code.set_range(self.r)
            try:
                self._cache_lvalue = self._lvalue(il_code, symbol_table, c)
            finally:
                il_code.restore_range(prev)
        return self._cache_lvalue

    def _lvalue(self, il_code, symbol_table, c):
        """Return an LValue object representing this node.

        Do not call this function directly, because it does not cache the
        result. Multiple calls to this function may generate multiple error
        messages or output repeated code to il_code.
        """
        raise NotImplementedError
