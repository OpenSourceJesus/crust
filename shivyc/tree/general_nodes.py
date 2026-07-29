"""Nodes in the AST which represent statements or declarations."""

import shivyc.spots as spots
import shivyc.ctypes as ctypes
import shivyc.il_cmds.control as control_cmds
import shivyc.il_cmds.value as value_cmds
import shivyc.token_kinds as token_kinds
from shivyc.tokens import Token
import shivyc.tree.decl_nodes as decl_nodes
from shivyc.ctypes import (PointerCType, ArrayCType, FunctionCType,
                           StructCType, UnionCType)
from shivyc.errors import CompilerError, error_collector
from shivyc.il_gen import ILCode, ILValue
from shivyc.tree.base_nodes import CNode
from shivyc.tree.utils import DirectLValue, report_err


class Root(CNode):
    """Root node of the program."""

    def __init__(self, nodes):
        """Initialize node."""
        super().__init__()
        self.nodes = nodes

    def make_il(self, il_code: "il_gen.ILCode", symbol_table: "il_gen.SymbolTable", c):
        """Make code for the root."""
        for node in self.nodes:
            with report_err():
                c = c.set_global(True)
                node.make_il(il_code, symbol_table, c)


class Compound(CNode):
    """CNode for a compound statement."""

    def __init__(self, items):
        """Initialize node."""
        super().__init__()
        self.items = items

    def make_il(self, il_code: "il_gen.ILCode", symbol_table: "il_gen.SymbolTable", c, no_scope=False):
        """Make IL code for every block item, in order.

        If no_scope is True, then do not create a new symbol table scope.
        Used by function definition so that parameters can live in the scope
        of the function body.
        """
        if not no_scope:
            symbol_table.new_scope()

        c = c.set_global(False)
        for item in self.items:
            with report_err():
                item.make_il(il_code, symbol_table, c)

        if not no_scope:
            symbol_table.end_scope()


class EmptyStatement(CNode):
    """CNode for a statement which is just a semicolon."""

    def __init__(self):
        """Initialize node."""
        super().__init__()

    def make_il(self, il_code: "il_gen.ILCode", symbol_table: "il_gen.SymbolTable", c):
        """Nothing to do for a blank statement."""
        pass


class ExprStatement(CNode):
    """CNode for a statement which contains one expression."""

    def __init__(self, expr):
        """Initialize node."""
        super().__init__()
        self.expr = expr

    def make_il(self, il_code: "il_gen.ILCode", symbol_table: "il_gen.SymbolTable", c):
        """Make code for this expression, and ignore the resulting ILValue."""
        self.expr.make_il(il_code, symbol_table, c)

    def stmt_expr_value(self, il_code, symbol_table, c):
        """Value of a statement expression ending in this statement: the
        contained expression's value (see CNode.stmt_expr_value). `self.expr` is
        a direct field read here because `self` is statically typed."""
        return self.expr.make_il(il_code, symbol_table, c)


class InlineAsm(CNode):
    """CNode for a (narrow subset of) GCC inline assembly statement.

    template - the assembly template string
    outputs / inputs - lists of (constraint_str, expression node)
    clobbers - list of clobber strings (advisory; memory/cc are no-ops)
    """

    def __init__(self, template, outputs, inputs, clobbers):
        """Initialize node."""
        super().__init__()
        self.template = template
        self.outputs = outputs
        self.inputs = inputs
        self.clobbers = clobbers

    def make_il(self, il_code: "il_gen.ILCode", symbol_table: "il_gen.SymbolTable", c):
        """Generate the inline-asm IL command and store back any outputs."""
        import shivyc.il_cmds.asm as asm_cmds_il
        from shivyc.il_gen import ILValue

        out_ops = []
        out_lvalues = []
        for constraint, expr in self.outputs:
            lval = expr.lvalue(il_code, symbol_table, c)
            if lval is None:
                raise CompilerError(
                    "inline asm output operand is not an lvalue", self.r)
            if "m" in constraint:
                # A memory output (`=m`) is realized as the operand's address;
                # the asm writes through it directly, so there is no register
                # result to copy back.
                out_ops.append((constraint, lval.addr(il_code)))
            else:
                tmp = ILValue(lval.ctype())
                if "+" in constraint:
                    # A read-write operand (`+r`) is an input as well as an
                    # output, so the temporary standing in for it has to start
                    # holding the operand's current value. Without this the
                    # asm read whatever the allocator left in the register:
                    # `__asm__("addl $37, %0" : "+r"(x))` with x == 5 produced
                    # an address rather than 42.
                    import shivyc.il_cmds.value as value_cmds_il
                    il_code.add(value_cmds_il.Set(tmp, lval.val(il_code)))
                out_ops.append((constraint, tmp))
                out_lvalues.append((lval, tmp))

        in_ops = []
        for constraint, expr in self.inputs:
            if "m" in constraint:
                # A memory operand is realized as the operand's address.
                lval = expr.lvalue(il_code, symbol_table, c)
                if lval is None:
                    raise CompilerError(
                        "inline asm 'm' operand is not an lvalue", self.r)
                in_ops.append((constraint, lval.addr(il_code)))
            else:
                in_ops.append(
                    (constraint, expr.make_il(il_code, symbol_table, c)))

        il_code.add(asm_cmds_il.InlineAsm(
            self.template, out_ops, in_ops, self.clobbers))

        for lval, tmp in out_lvalues:
            lval.set_to(tmp, il_code, self.r)


LONG_DOUBLE_MSG = ("our compiler uses the x87 Floating Point Unit (FPU) as "
                   "an extra data cache, and we do not allow 80bit floating "
                   "point math")

LONG_DOUBLE_AS_DOUBLE_WARNING = (
    "-f-long-double-as-double: treating 'long double' as 64-bit double. This "
    "compiler never supports the 80-bit extended precision format. Neither "
    "ARM nor RISC-V provide native hardware for it, and on Intel/AMD we use "
    "the x87/SIMD registers as a spill cache to speed up code (see "
    "dotnet/runtime#10444, the LLVM Spill2Reg RFC, and JDK-7175279). Your "
    "long double computations will run at double precision")

# Set once we have emitted the long-double-as-double warning, so it is shown
# at most one time per compilation.
_warned_long_double_as_double = False


def reject_long_double(ctype, range, il_code=None, c=None):
    """Reject the unsupported 80-bit long double type.

    A file-scope long double object cannot be dead-code eliminated, so it is
    rejected immediately. Inside a function the use is recorded against the
    current function and rejected later only if that function survives
    dead-code elimination -- this tolerates the unused `static inline` long
    double helpers that musl's headers define but most files never call.
    """
    if not (ctype.is_floating() and getattr(ctype, "long_double", False)):
        return
    if (il_code is not None and c is not None and not c.is_global
            and getattr(il_code, "cur_func", None) is not None):
        il_code.long_double_taint.setdefault(il_code.cur_func, range)
    else:
        raise CompilerError(LONG_DOUBLE_MSG, range)


class DeclInfo:
    """Contains information about the declaration of one identifier.

    identifier - the identifier being declared
    ctype - the ctype of this identifier
    storage - the storage class of this identifier
    init - the initial value of this identifier
    """

    # Storage class specifiers for declarations
    AUTO = 1
    STATIC = 2
    EXTERN = 3
    TYPEDEF = 4

    def __init__(self, identifier: "Token", ctype, range,
                 storage=None, init=None, body=None, param_names=None):
        self.identifier = identifier
        self.ctype = ctype
        self.asm_reg = None     # explicit register binding (asm() declarations)
        self.range = range
        self.storage = storage
        self.init = init
        self.body = body
        self.param_names = param_names
        # Width expression if this declarator is a bitfield, else None. Only
        # meaningful for struct/union members.
        self.bitfield = None

    def process(self, il_code, symbol_table, c):
        """Process given DeclInfo object.

        This includes error checking, adding the variable to the symbol
        table, and registering it with the IL.
        """
        if not self.identifier:
            err = "missing identifier name in declaration"
            raise CompilerError(err, self.range)

        # The typedef is special
        if self.storage == self.TYPEDEF:
            self.process_typedef(symbol_table)
            return

        # GCC __auto_type: infer the declared type from the initializer. Probe
        # the initializer's type on a throwaway IL copy (as sizeof does), so no
        # code is emitted twice.
        if isinstance(self.ctype, ctypes.AutoCType):
            if self.init is None or isinstance(self.init, decl_nodes.InitList):
                err = "__auto_type declaration requires a scalar initializer"
                raise CompilerError(err, self.range)
            probe_il = il_code.copy_code()
            init_val = self.init.make_il(probe_il, symbol_table, c)
            self.ctype = init_val.ctype

        if self.body and not self.ctype.is_function():
            err = "function definition provided for non-function type"
            raise CompilerError(err, self.range)

        linkage = self.get_linkage(symbol_table, c)
        defined = self.get_defined(symbol_table, c)
        storage = self.get_storage(defined, linkage, symbol_table)

        if not c.is_global and self.init and linkage:
            err = "local variable with linkage has initializer"
            raise CompilerError(err, self.range)

        # Complete an incomplete array type (`T a[] = {...}`) from the number
        # of top-level elements in its brace initializer.
        if (self.init is not None
                and isinstance(self.init, decl_nodes.InitList)
                and self.ctype.is_array() and self.ctype.is_incomplete()):
            from shivyc.ctypes import ArrayCType
            self.ctype = ArrayCType(self.ctype.el, len(self.init.items))

        # Complete an incomplete char array from a string initializer
        # (`char s[] = "abc";` -> size 4, including the terminating null).
        import shivyc.tree.primary_exprs as _pe
        if (self.init is not None
                and isinstance(self.init, _pe.String)
                and self.ctype.is_array() and self.ctype.is_incomplete()
                and self.ctype.el.size == 1):
            from shivyc.ctypes import ArrayCType
            self.ctype = ArrayCType(self.ctype.el, len(self.init.chars))

        # long double may appear in prototypes (no storage) but not as a
        # declared object: that would allocate 80-bit storage we do not allow.
        if (self.storage != self.TYPEDEF and not self.ctype.is_function()):
            reject_long_double(self.ctype, self.range, il_code, c)

        var = symbol_table.add_variable(
            self.identifier,
            self.ctype,
            defined,
            linkage,
            storage)

        # Pin this variable to a hardware register for inline-asm operands when
        # it was declared `register T v __asm__("reg")` (GCC extension; musl's
        # syscall wrappers bind r10/r8/r9 this way).
        ar = getattr(self, "asm_reg", None)
        if ar:
            var.asm_reg = ar.strip().lstrip("%")

        # Record parameter names so contract checks can map a contract's
        # argument name to the positional argument at each call site.
        if self.ctype.is_function() and self.identifier:
            import shivyc.contracts as _contracts
            _contracts.set_params(self.identifier.content, self.param_names)

        if self.init:
            self.do_init(var, storage, il_code, symbol_table, c)
            # Track variables proven to hold a string literal, so contracts
            # using len() can be checked where such a variable is passed.
            import shivyc.tree.primary_exprs as _pe
            import shivyc.contracts as _contracts
            if isinstance(self.init, _pe.String):
                _contracts.set_known_len(var, max(0, len(self.init.chars) - 1))
        if self.body:
            import shivyc.contracts as _contracts
            prev_fn = _contracts.current_function
            _contracts.current_function = (
                self.identifier.content if self.identifier else None)
            _contracts.reset_function()
            try:
                self.do_body(il_code, symbol_table, c)
            finally:
                _contracts.current_function = prev_fn

        if not linkage and self.ctype.is_incomplete():
            err = "variable of incomplete type declared"
            raise CompilerError(err, self.range)

    def process_typedef(self, symbol_table):
        """Process type declarations."""

        if self.init:
            err = "typedef cannot have initializer"
            raise CompilerError(err, self.range)

        if self.body:
            err = "function definition cannot be a typedef"
            raise CompilerError(err, self.range)

        symbol_table.add_typedef(self.identifier, self.ctype)

    def do_init(self, var, storage, il_code, symbol_table, c):
        """Create code for initializing given variable.

        Caller must check that this object has an initializer.
        """
        if isinstance(self.init, decl_nodes.InitList):
            self.do_aggregate_init(var, storage, il_code, symbol_table, c)
            return

        # A char array initialized by a string literal copies the string's
        # bytes into the array's own storage (it does not point at a separate
        # literal).
        import shivyc.tree.primary_exprs as primary_exprs
        if (isinstance(self.init, primary_exprs.String)
                and var.ctype.is_array() and var.ctype.el.size == 1):
            self.do_string_array_init(var, storage, il_code, symbol_table, c)
            return

        # A static scalar initialized by an address constant (a function
        # pointer, or the address of an external object) is emitted as a
        # relocation in the data section rather than runtime code.
        if storage == symbol_table.STATIC:
            addr = self._static_addr_const(
                self.init, symbol_table, il_code, c)
            if addr is not None:
                il_code.static_initialize_block(
                    var, [(0, var.ctype.size, addr)], var.ctype.size)
                return
            # Evaluate on a probe IL so a non-constant init (e.g. a call)
            # cannot crash with KeyError when cur_func is unset at file scope.
            probe = ILCode()
            probe.start_func("<static-init-probe>")
            init = self.init.make_il(probe, symbol_table, c)
            if not init.literal:
                err = ("non-constant initializer for variable with static "
                       "storage duration")
                raise CompilerError(err, self.init.r)
            il_code.static_initialize(var, getattr(init.literal, "val", None))
            return

        init = self.init.make_il(il_code, symbol_table, c)
        if var.ctype.is_arith() or var.ctype.is_pointer():
            lval = DirectLValue(var)
            lval.set_to(init, il_code, self.identifier.r)
        elif var.ctype.is_struct_union():
            # A struct/union local initialized from a struct-valued expression
            # (another struct, a function result, or a compound literal) is a
            # copy-initialization, handled exactly like struct assignment.
            lval = DirectLValue(var)
            lval.set_to(init, il_code, self.identifier.r)
        else:
            err = "declared variable is not of assignable type"
            raise CompilerError(err, self.range)

    def do_string_array_init(self, var, storage, il_code, symbol_table, c):
        """Initialize a char array from a string literal by copying its bytes
        into the array's own storage (truncating to, or zero-padding up to,
        the array size)."""
        chars = self.init.chars
        size = var.ctype.size
        n = min(len(chars), size)

        if storage == symbol_table.STATIC:
            entries = [(i, 1, chars[i]) for i in range(n)]
            il_code.static_initialize_block(var, entries, size)
            return

        # Automatic storage: store each byte (zero-filling any remainder).
        import shivyc.il_cmds.math as math_cmds
        from shivyc.tree.utils import IndirectLValue
        base_addr = DirectLValue(var).addr(il_code)

        def byte_addr(off):
            shift = ILValue(ctypes.longint)
            il_code.register_literal_var(shift, str(off))
            addr = ILValue(PointerCType(ctypes.char))
            il_code.add(math_cmds.Add(addr, base_addr, shift))
            return addr

        for i in range(size):
            val = ILValue(ctypes.char)
            il_code.register_literal_var(val, str(chars[i] if i < n else 0))
            IndirectLValue(byte_addr(i)).set_to(
                val, il_code, self.identifier.r)

    def do_aggregate_init(self, var, storage, il_code, symbol_table, c):
        """Initialize an array/struct/union from a brace initializer list."""
        entries = []  # (byte_offset, scalar_ctype, expr_node)
        self._flatten_init(var.ctype, self.init, 0, entries, il_code,
                           symbol_table, c)

        if storage == symbol_table.STATIC:
            consts = []
            all_zero = True
            # Probe IL: file-scope cur_func is None; never emit into real code.
            probe = ILCode()
            probe.start_func("<static-init-probe>")
            for off, ctype, expr in entries:
                addr = self._static_addr_const(
                    expr, symbol_table, il_code, c)
                if addr is not None:
                    consts.append((off, ctype.size, addr))
                    all_zero = False
                    continue
                v = expr.make_il(probe, symbol_table, c)
                if not v.literal:
                    err = ("non-constant initializer for variable with static "
                           "storage duration")
                    raise CompilerError(err, self.init.r)
                val = getattr(v.literal, "val", 0)
                consts.append((off, ctype.size, val))
                if val != 0:
                    all_zero = False
            if all_zero:
                il_code.static_initialize(var, 0)
            else:
                il_code.static_initialize_block(var, consts, var.ctype.size)
            return

        # Automatic storage: store each element, then zero any gaps.
        import shivyc.il_cmds.math as math_cmds
        from shivyc.tree.utils import IndirectLValue
        base_addr = DirectLValue(var).addr(il_code)
        total = var.ctype.size
        written = set()

        def elem_addr(off, ctype):
            shift = ILValue(ctypes.longint)
            il_code.register_literal_var(shift, str(off))
            addr = ILValue(PointerCType(ctype))
            il_code.add(math_cmds.Add(addr, base_addr, shift))
            return addr

        for off, ctype, expr in entries:
            val = expr.make_il(il_code, symbol_table, c)
            IndirectLValue(elem_addr(off, ctype)).set_to(
                val, il_code, self.identifier.r)
            for b in range(off, off + ctype.size):
                written.add(b)

        gaps = [b for b in range(total) if b not in written]
        if gaps:
            zero = ILValue(ctypes.unsig_char)
            il_code.register_literal_var(zero, "0")
            for b in gaps:
                IndirectLValue(elem_addr(b, ctypes.unsig_char)).set_to(
                    zero, il_code, self.identifier.r)

    def _flatten_init(self, ctype, init, base, out, il_code, symbol_table, c):
        """Flatten an initializer against a ctype into scalar (offset, ctype,
        expr) entries."""
        if isinstance(init, decl_nodes.InitList):
            if ctype.is_array():
                el = ctype.el
                idx = 0
                for designators, sub in init.items:
                    for kind, val in designators:
                        if kind == "index":
                            iv = val.make_il(il_code, symbol_table, c)
                            idx = iv.literal.val
                    self._flatten_init(el, sub, base + idx * el.size, out,
                                       il_code, symbol_table, c)
                    idx += 1
            elif ctype.is_struct_union():
                members = ctype.members
                mi = 0
                import shivyc.member_elim as member_elim
                for designators, sub in init.items:
                    # A positional (non-designated) struct initializer pins the
                    # member order, so the struct must keep all its members.
                    if not any(k == "member" for k, _ in designators):
                        member_elim.mark_ineligible(getattr(ctype, "tag", None))

                    if designators:
                        # Walk the (possibly nested) designator chain from this
                        # struct, e.g. `.a.b` or `.a[2].c`. Member offsets come
                        # from get_offset, which also resolves names promoted
                        # from C11 anonymous union/struct members. (musl's
                        # sigaction reaches .sa_sigaction via a nested
                        # `.__sa_handler.sa_sigaction` designator.)
                        cur_ctype = ctype
                        cur_off = base
                        for di, (kind, val) in enumerate(designators):
                            if kind == "member":
                                name = val.content
                                if di == 0:
                                    idx = next(
                                        (i for i, (n, _) in enumerate(members)
                                         if n == name), None)
                                    if idx is not None:
                                        mi = idx
                                moff, mctype = cur_ctype.get_offset(name)
                                if moff is None:
                                    raise CompilerError(
                                        f"unknown field '{name}' in"
                                        " initializer", self.range)
                                cur_off += moff
                                cur_ctype = mctype
                            elif kind == "index":
                                iv = val.make_il(il_code, symbol_table, c)
                                cur_off += iv.literal.val * cur_ctype.el.size
                                cur_ctype = cur_ctype.el
                        self._flatten_init(cur_ctype, sub, cur_off, out,
                                           il_code, symbol_table, c)
                        mi += 1
                    elif mi < len(members):
                        name, mctype = members[mi]
                        moff, _ = ctype.offsets[name]
                        self._flatten_init(mctype, sub, base + moff, out,
                                           il_code, symbol_table, c)
                        mi += 1
            else:
                # Scalar wrapped in braces, e.g. `int x = {5};`
                if init.items:
                    self._flatten_init(ctype, init.items[0][1], base, out,
                                       il_code, symbol_table, c)
        else:
            out.append((base, ctype, init))

    def _static_addr_const(self, node, symbol_table, il_code, c=None):
        """If `node` is an address constant with a stable linker symbol,
        return ("sym", name, addend); otherwise None.

        Handled: a function name (decays to its address), the bare name of an
        externally-linked array (decays), `&X` for a function or an
        externally-linked object, and a string literal (its bytes are emitted
        with a stable label which the pointer is initialized to). Parenthesized
        forms are unwrapped.
        """
        import shivyc.tree.primary_exprs as primary_exprs
        import shivyc.tree.memory_exprs as memory_exprs

        # Parentheses around an address constant (common from macro expansions
        # like PyObject_HEAD_INIT(&T) producing `(&T)`) do not change its
        # constness; unwrap them.
        while isinstance(node, primary_exprs.ParenExpr):
            node = node.expr

        # A string literal used to initialize a pointer becomes the address of
        # the emitted string bytes (e.g. `static char *p = "x";`,
        # `PyTypeObject T = { ..., "name", ... }`).
        if isinstance(node, primary_exprs.String):
            name = il_code.intern_static_string(node.chars)
            return ("sym", name, 0)

        # A bare compound literal of array type decays to the address of its
        # storage, which at file scope is static. e.g.
        # `static int *const p = (int[]){ 1, 2, 3 };`.
        import shivyc.tree.type_exprs as _type_exprs
        if isinstance(node, _type_exprs.CompoundLiteral) and c is not None:
            lval = node._lvalue(il_code, symbol_table, c.set_global(True))
            if lval.il_value.ctype.is_array():
                return ("sym", symbol_table.asm_name(lval.il_value), 0)
            return None

        if isinstance(node, memory_exprs.AddrOf):
            ref = self._symbol_ref(node.expr, symbol_table, False)
            if ref is not None:
                return ref
            # &OBJ.m1.m2... : the address of a (possibly nested) member of a
            # static or external object is an address constant (symbol+offset),
            # e.g. CPython's `&_Py_ID(key)` (a member of _PyRuntime) or
            # `&_kwtuple.ob_base.ob_base` (a member of a local static).
            name, off, _ = self._static_member_ref(node.expr, symbol_table)
            if name is not None:
                return ("sym", name, off)
            # &(compound literal): a compound literal at file scope has static
            # storage duration, so its address is an address constant (C11
            # 6.5.2.5p5). Materialize it as an anonymous static object and use
            # that object's label. CPython's module-definition tables use this,
            # e.g. `{Py_mod_..., (void*)&(PyABIInfo){...}}`.
            import shivyc.tree.type_exprs as type_exprs
            if (isinstance(node.expr, type_exprs.CompoundLiteral)
                    and c is not None):
                lval = node.expr._lvalue(il_code, symbol_table,
                                         c.set_global(True))
                return ("sym", symbol_table.asm_name(lval.il_value), 0)
            return None
        if isinstance(node, primary_exprs.Identifier):
            return self._symbol_ref(node, symbol_table, True)

        # A bare member access `OBJ.m1.m2...` whose member type is an array
        # decays to the address of that member (C11 6.3.2.1p3) and is therefore
        # an address constant: symbol-plus-offset. This arises in CPython's
        # _PyRuntime initializer, e.g. a pointer field initialized directly with
        # an array member of the object being defined. Non-array members are an
        # rvalue load, not a constant, so fall through to the normal path.
        if isinstance(node, memory_exprs.ObjMember):
            name, off, mctype = self._static_member_ref(node, symbol_table)
            if name is not None and mctype is not None and mctype.is_array():
                return ("sym", name, off)
            return None

        # A cast of an address constant to a pointer type is still an address
        # constant (C11 6.6p9), e.g. `(PyCFunction)func` in a method table.
        # Only unwrap casts whose target is a pointer, which preserves the
        # full address; casts to narrower types could truncate it.
        import shivyc.tree.type_exprs as type_exprs
        if isinstance(node, type_exprs.Cast):
            try:
                node.set_self_vars(il_code, symbol_table, None)
                base_type, _ = node.make_specs_ctype(node.node.specs, True)
                ctype, _ = node.make_ctype(node.node.decls[0], base_type)
            except Exception:
                return None
            if ctype.is_pointer():
                return self._static_addr_const(
                    node.expr, symbol_table, il_code, c)
            return None

        # Pointer arithmetic on an address constant: `ARRAY + n`, `n + ARRAY`,
        # or `PTR - n`, where n is an integer constant. The result is the same
        # linker symbol with the addend scaled by the pointee size. musl's
        # ctype tables use `static const int32_t *const p = table + 128;`.
        import shivyc.tree.arithmetic_exprs as arith_exprs
        if isinstance(node, (arith_exprs.Plus, arith_exprs.Minus)):
            is_plus = isinstance(node, arith_exprs.Plus)
            candidates = [(node.left, node.right)]
            if is_plus:
                candidates.append((node.right, node.left))  # n + ARRAY
            for addr_node, int_node in candidates:
                base = self._static_addr_const(
                    addr_node, symbol_table, il_code, c)
                if base is None:
                    continue
                n = self._const_int_value(int_node)
                scale = self._static_pointee_size(addr_node, symbol_table)
                if n is None or scale is None:
                    continue
                kind, name, off = base
                delta = n * scale
                return (kind, name, off + (delta if is_plus else -delta))
            return None
        return None

    def _const_int_value(self, node):
        """Return the integer value of a constant-integer node, or None."""
        import shivyc.tree.primary_exprs as primary_exprs
        while isinstance(node, primary_exprs.ParenExpr):
            node = node.expr
        if isinstance(node, primary_exprs.Number):
            from shivyc.tokens import parse_c_int
            spelling = str(node.number).rstrip("uUlL")
            try:
                return parse_c_int(spelling)
            except Exception:
                return None
        return None

    def _static_pointee_size(self, node, symbol_table):
        """For an address-constant node, the size its pointer arithmetic scales
        by (array element size or pointed-to size), or None."""
        import shivyc.tree.primary_exprs as primary_exprs
        while isinstance(node, primary_exprs.ParenExpr):
            node = node.expr
        if isinstance(node, primary_exprs.Identifier):
            try:
                var = symbol_table.lookup_variable(node.identifier)
            except Exception:
                return None
            ct = var.ctype
            if ct.is_array():
                return ct.el.size
            if ct.is_pointer():
                return ct.arg.size
        return None

    def _symbol_ref(self, node, symbol_table, decay):
        """Resolve an identifier node to a stable ("sym", name, 0) or None."""
        import shivyc.tree.primary_exprs as primary_exprs
        if not isinstance(node, primary_exprs.Identifier):
            return None
        try:
            var = symbol_table.lookup_variable(node.identifier)
        except Exception:
            return None
        if var not in symbol_table.names:
            return None
        # Functions always have a stable plain label (never suffixed).
        if var.ctype.is_function():
            return ("sym", symbol_table.names[var], 0)
        # A bare identifier is an address constant only if it is an array
        # (which decays to its address); a scalar identifier is a value.
        if decay and not var.ctype.is_array():
            return None
        # An object with static storage (external or internal/file-scope
        # static) has a stable label usable as an address constant.
        if (symbol_table.storage.get(var) == symbol_table.STATIC
                or symbol_table.linkage_type.get(var) in (
                    symbol_table.EXTERNAL, symbol_table.INTERNAL)):
            return ("sym", symbol_table.asm_name(var), 0)
        return None

    def _static_member_ref(self, node, symbol_table):
        """Resolve a `.`-member access of a static/external object.

        Returns (sym_name, total_offset, ctype) for `OBJ.m1.m2...` rooted at a
        named static or external object, or (None, None, None). Used to make
        `&OBJ.m1.m2...` an address constant (symbol plus a byte offset).
        """
        import shivyc.tree.primary_exprs as primary_exprs
        import shivyc.tree.memory_exprs as memory_exprs

        while isinstance(node, primary_exprs.ParenExpr):
            node = node.expr

        if isinstance(node, primary_exprs.Identifier):
            try:
                var = symbol_table.lookup_variable(node.identifier)
            except Exception:
                return None, None, None
            if var not in symbol_table.names or var.ctype.is_function():
                return None, None, None
            if (symbol_table.storage.get(var) == symbol_table.STATIC
                    or symbol_table.linkage_type.get(var) in (
                        symbol_table.EXTERNAL, symbol_table.INTERNAL)):
                return symbol_table.asm_name(var), 0, var.ctype
            return None, None, None

        if isinstance(node, memory_exprs.ObjMember):
            name, off, head_ctype = self._static_member_ref(
                node.head, symbol_table)
            if name is None:
                return None, None, None
            try:
                moff, mctype = node.get_offset_info(head_ctype)
            except Exception:
                return None, None, None
            return name, off + moff, mctype

        # `(&E)->member` is the same location as `E.member` (C11 6.5.2.3): the
        # `->` dereferences the address produced by `&E`. CPython's _PyRuntime
        # initializer uses this for self-referential members, e.g.
        # `&(_PyRuntime.x.y.z)` re-accessed through `->`. Resolve the inner `E`
        # as a location, then add the member offset within E's type.
        if isinstance(node, memory_exprs.ObjPtrMember):
            head = node.head
            while isinstance(head, primary_exprs.ParenExpr):
                head = head.expr
            if isinstance(head, memory_exprs.AddrOf):
                name, off, ec = self._static_member_ref(
                    head.expr, symbol_table)
                if name is None or ec is None:
                    return None, None, None
                try:
                    moff, mctype = node.get_offset_info(ec)
                except Exception:
                    return None, None, None
                return name, off + moff, mctype
            return None, None, None

        return None, None, None

    def do_body(self, il_code, symbol_table, c):
        """Create code for function body.

        Caller must check that this function has a body.
        """
        is_main = self.identifier.content == "main"

        for param in self.param_names:
            if not param:
                err = "function definition missing parameter name"
                raise CompilerError(err, self.range)

        if is_main:
            self.check_main_type()

        c = c.set_return(self.ctype.ret)
        c = c.set_labels({})
        # Variadic functions receive all arguments on the stack; record the
        # named-parameter count so va_start can locate the first vararg.
        variadic = getattr(self.ctype, "variadic", False)
        if variadic:
            # Named args are passed on the stack, each struct occupying
            # ceil(size/8) eightbyte slots; va_start must skip the *slots*, not
            # the parameter count, to find the first vararg.
            named_slots = sum(
                (ct.size + 7) // 8 if (ct.is_struct_union() and ct.size > 8)
                else 1 for ct in self.ctype.args)
            c = c.set_vararg_named(named_slots)
        il_code.start_func(self.identifier.content)

        symbol_table.new_scope()

        num_params = len(self.ctype.args)
        iter = zip(self.ctype.args, self.param_names, range(num_params))
        int_i, flt_i, stack_i = 0, 0, 0
        vstack_i = 0    # running eightbyte slot index for the all-stack variadic ABI
        int_regs = spots.int_arg_regs
        xmm_regs = spots.xmm_arg_regs

        # SysV AMD64: a function returning a struct larger than 16 bytes
        # receives a hidden pointer to caller-allocated result storage as an
        # implicit first integer argument (in RDI). Real integer parameters
        # therefore start at RSI. `return X` writes the struct through this
        # pointer (see the Return node).
        ret_ctype = self.ctype.ret
        if (not variadic and ret_ctype.is_struct_union()
                and ret_ctype.size > 16):
            sret_ptr = ILValue(PointerCType(ret_ctype))
            # LoadArg args are passed positionally: the self-host transpiler
            # drops keyword arguments that follow a defaulted positional
            # parameter, which would otherwise null out `reg`/`is_float`/
            # `stack_index` and misroute arguments (notably floating arguments,
            # which would then be read from an integer register instead of xmm).
            # Signature: (output, arg_num, all_stack, reg, is_float, stack_index)
            il_code.add(value_cmds.LoadArg(sret_ptr, 0, False, int_regs[0]))
            int_i = 1
            c = c.set_sret_ptr(sret_ptr)

        for ctype, param, i in iter:
            arg = symbol_table.add_variable(
                param, ctype, symbol_table.DEFINED, None,
                symbol_table.AUTOMATIC)
            if variadic:
                # Variadic functions take every argument on the stack; a struct
                # occupies ceil(size/8) eightbyte slots and is copied from them.
                if ctype.is_struct_union() and ctype.size > 8:
                    n = (ctype.size + 7) // 8
                    il_code.add(value_cmds.LoadStructArg(
                        arg, stack_index=vstack_i))
                    vstack_i += n
                else:
                    il_code.add(value_cmds.LoadArg(
                        arg, vstack_i, True))
                    vstack_i += 1
                continue
            if (ctype.is_struct_union() and ctype.size > 8):
                # SysV: a struct of 9..16 bytes (INTEGER class) arrives in two
                # consecutive integer registers, all-or-nothing; a larger one,
                # or one that does not fit the remaining registers, arrives on
                # the stack.
                n = (ctype.size + 7) // 8
                if ctype.size <= 16 and int_i + n <= len(int_regs):
                    regs = [int_regs[int_i + k] for k in range(n)]
                    il_code.add(value_cmds.LoadStructArg(arg, regs=regs))
                    int_i += n
                else:
                    il_code.add(value_cmds.LoadStructArg(
                        arg, stack_index=stack_i))
                    stack_i += n
                continue
            if ctype.is_floating() and flt_i < len(xmm_regs):
                il_code.add(value_cmds.LoadArg(
                    arg, i, False, xmm_regs[flt_i], True))
                flt_i += 1
            elif not ctype.is_floating() and int_i < len(int_regs):
                il_code.add(value_cmds.LoadArg(arg, i, False, int_regs[int_i]))
                int_i += 1
            else:
                il_code.add(value_cmds.LoadArg(arg, i, False, None, False, stack_i))
                stack_i += 1

        self.body.make_il(il_code, symbol_table, c, no_scope=True)
        if not il_code.always_returns() and is_main:
            zero = ILValue(ctypes.integer)
            il_code.register_literal_var(zero, 0)
            il_code.add(control_cmds.Return(zero))
        elif not il_code.always_returns():
            il_code.add(control_cmds.Return(None))

        symbol_table.end_scope()

    def check_main_type(self):
        """Check if function signature matches signature expected of main.

        Raises an exception if this function signature does not match the
        function signature expected of the main function.
        """
        if not self.ctype.ret.compatible(ctypes.integer):
            err = "'main' function must have integer return type"
            raise CompilerError(err, self.range)
        if len(self.ctype.args) not in {0, 2, 3}:
            err = "'main' function must have 0, 2, or 3 arguments"
            raise CompilerError(err, self.range)
        if self.ctype.args:
            first = self.ctype.args[0]
            second = self.ctype.args[1]

            if not first.compatible(ctypes.integer):
                err = "first parameter of 'main' must be of integer type"
                raise CompilerError(err, self.range)

            is_ptr_array = (second.is_pointer()
                             and (second.arg.is_pointer()
                                  or second.arg.is_array()))

            if not is_ptr_array or not second.arg.arg.compatible(ctypes.char):
                err = "second parameter of 'main' must be like char**"
                raise CompilerError(err, self.range)

            # The common POSIX extension `int main(int, char**, char**)` adds a
            # third `envp` parameter, also char**.
            if len(self.ctype.args) == 3:
                third = self.ctype.args[2]
                is_envp = (third.is_pointer()
                           and (third.arg.is_pointer()
                                or third.arg.is_array())
                           and third.arg.arg.compatible(ctypes.char))
                if not is_envp:
                    err = "third parameter of 'main' must be like char**"
                    raise CompilerError(err, self.range)

    def get_linkage(self, symbol_table, c):
        """Get linkage type for given decl_info object.

        See 6.2.2 in the C11 spec for details.
        """
        if c.is_global and self.storage == DeclInfo.STATIC:
            linkage = symbol_table.INTERNAL
        elif (self.storage == DeclInfo.EXTERN
              or (self.ctype.is_function() and not self.storage)):
            # `extern`, and (per 6.2.2p5) a function with no storage-class
            # specifier, take the linkage of a prior visible declaration if one
            # exists; otherwise external. This makes a `static` forward
            # declaration followed by a plain definition keep internal linkage
            # (a common idiom: `static T f(...); ... T f(...) { ... }`).
            cur_linkage = symbol_table.lookup_linkage(self.identifier)
            linkage = cur_linkage or symbol_table.EXTERNAL
        elif c.is_global and not self.storage:
            linkage = symbol_table.EXTERNAL
        else:
            linkage = None

        return linkage

    def get_defined(self, symbol_table, c):
        """Determine whether this is a definition."""
        if (c.is_global and self.storage in {None, self.STATIC}
              and self.ctype.is_object() and not self.init):
            return symbol_table.TENTATIVE
        elif self.storage == self.EXTERN and not (self.init or self.body):
            return symbol_table.UNDEFINED
        elif self.ctype.is_function() and not self.body:
            return symbol_table.UNDEFINED
        else:
            return symbol_table.DEFINED

    def get_storage(self, defined: int, linkage, symbol_table):
        """Determine the storage duration."""
        if defined == symbol_table.UNDEFINED or not self.ctype.is_object():
            storage = None
        elif linkage or self.storage == self.STATIC:
            storage = symbol_table.STATIC
        else:
            storage = symbol_table.AUTOMATIC

        return storage


class Declaration(CNode):
    """Line of a general variable declaration(s).

    node (decl_nodes.Root) - a declaration tree for this line
    body (Compound(CNode)) - if this is a function definition, the body of
    the function
    """

    def __init__(self, node, body=None):
        """Initialize node."""
        super().__init__()
        self.node = node
        self.body = body

    def make_il(self, il_code: "il_gen.ILCode", symbol_table: "il_gen.SymbolTable", c):
        """Make code for this declaration."""

        self.set_self_vars(il_code, symbol_table, c)
        decl_infos = self.get_decl_infos(self.node)
        for info in decl_infos:
            with report_err():
                info.process(il_code, symbol_table, c)

    def set_self_vars(self, il_code, symbol_table, c):
        """Set il_code, symbol_table, and context as attributes of self.

        Helper function to prevent us from having to pass these three
        arguments into almost all functions in this class.

        """
        self.il_code = il_code
        self.symbol_table = symbol_table
        self.c = c

    def get_decl_infos(self, node, in_struct=False):
        """Given a node, returns a list of decl_info objects for that node."""

        any_dec = bool(node.decls)
        base_type, storage = self.make_specs_ctype(node.specs, any_dec)

        out = []
        bitfields = getattr(node, "bitfields", None) or [None] * len(node.decls)
        asm_regs = getattr(node, "asm_regs", None) or [None] * len(node.decls)
        for decl, init, bitfield, asm_reg in zip(
                node.decls, node.inits, bitfields, asm_regs):
            with report_err():
                ctype, identifier = self.make_ctype(decl, base_type)

                if ctype.is_function():
                    param_identifiers = self.extract_params(decl)
                else:
                    param_identifiers = []

                di = DeclInfo(
                    identifier, ctype, decl.r, storage, init,
                    self.body, param_identifiers)
                di.bitfield = bitfield
                di.asm_reg = asm_reg
                out.append(di)

        # C11 anonymous struct/union member: a struct/union specifier with no
        # declarator and no tag introduces its members into the enclosing
        # struct/union. Represent it as a nameless DeclInfo so the member
        # collection can lay it out and promote its members.
        #
        # Test the tag with `not base_type.tag` rather than `is None`: the
        # self-host runtime stores an absent tag as a null char* (a falsy
        # string), not as None, so `base_type.tag is None` would never hold
        # natively and anonymous members would be silently dropped.
        if (in_struct and not out and not any_dec
                and base_type.is_struct_union() and not base_type.tag):
            spec_range = node.specs[0].r + node.specs[-1].r
            di = DeclInfo(None, base_type, spec_range, storage, None,
                          self.body, [])
            di.bitfield = None
            out.append(di)

        return out

    def make_ctype(self, decl, prev_ctype):
        """Generate a ctype from the given declaration.

        Return a `ctype, identifier token` tuple.

        decl - CNode of decl_nodes to parse. See decl_nodes.py for explanation
        about decl_nodes.
        prev_ctype - The ctype formed from all parts of the tree above the
        current one.
        """
        if isinstance(decl, decl_nodes.Pointer):
            new_ctype = PointerCType(prev_ctype, decl.const)
        elif isinstance(decl, decl_nodes.Array):
            new_ctype = self._generate_array_ctype(decl, prev_ctype)
        elif isinstance(decl, decl_nodes.Function):
            new_ctype = self._generate_func_ctype(decl, prev_ctype)
        elif isinstance(decl, decl_nodes.Identifier):
            return prev_ctype, decl.identifier

        return self.make_ctype(decl.child, new_ctype)

    def _generate_array_ctype(self, decl, prev_ctype):
        """Generate a function ctype from a given a decl_node."""

        if decl.n:
            il_value = decl.n.make_il(self.il_code, self.symbol_table, self.c)
            if not il_value.ctype.is_integral():
                err = "array size must have integral type"
                raise CompilerError(err, decl.r)
            if not il_value.literal:
                err = "array size must be compile-time constant"
                raise CompilerError(err, decl.r)
            if il_value.literal.val < 0:
                err = "array size must be positive"
                raise CompilerError(err, decl.r)
            if not prev_ctype.is_complete():
                err = "array elements must have complete type"
                raise CompilerError(err, decl.r)
            return ArrayCType(prev_ctype, il_value.literal.val)
        else:
            return ArrayCType(prev_ctype, None)

    def _generate_func_ctype(self, decl, prev_ctype):
        """Generate a function ctype from a given a decl_node."""

        # Prohibit storage class specifiers in parameters.
        for param in decl.args:
            decl_info = self.get_decl_infos(param)[0]
            if decl_info.storage:
                err = "storage class specified for function parameter"
                raise CompilerError(err, decl_info.range)

        # Create a new scope because if we create a new struct type inside
        # the function parameters, it should be local to those parameters.
        self.symbol_table.new_scope()
        args = [
            self.get_decl_infos(decl)[0].ctype
            for decl in decl.args
        ]
        self.symbol_table.end_scope()

        # adjust array and function parameters
        has_void = False
        for i in range(len(args)):
            ctype = args[i]
            if ctype.is_array():
                args[i] = PointerCType(ctype.el)
            elif ctype.is_function():
                args[i] = PointerCType(ctype)
            elif ctype.is_void():
                has_void = True
        if has_void and len(args) > 1:
            decl_info = self.get_decl_infos(decl.args[0])[0]
            err = "'void' must be the only parameter"
            raise CompilerError(err, decl_info.range)
        if prev_ctype.is_function():
            err = "function cannot return function type"
            raise CompilerError(err, self.r)

        # A struct passed or returned by value has its layout exposed through
        # the call's ABI (possibly to a function outside this program), so it
        # must keep all members. Pointer parameters are unaffected.
        import shivyc.member_elim as member_elim
        for ctype in args + [prev_ctype]:
            if ctype.is_struct_union():
                member_elim.mark_ineligible(getattr(ctype, "tag", None))
        if prev_ctype.is_array():
            err = "function cannot return array type"
            raise CompilerError(err, self.r)

        if not args and not self.body:
            new_ctype = FunctionCType([], prev_ctype, True)
        elif has_void:
            new_ctype = FunctionCType([], prev_ctype, False)
        else:
            new_ctype = FunctionCType(args, prev_ctype, False)
        new_ctype.variadic = getattr(decl, "variadic", False)
        return new_ctype

    def extract_params(self, decl):
        """Return the parameter list for this function."""

        identifiers = []
        func_decl = None
        while decl and not isinstance(decl, decl_nodes.Identifier):
            if isinstance(decl, decl_nodes.Function):
                func_decl = decl
            decl = decl.child

        if not func_decl:
            # This condition is true for the following code:
            #
            # typedef int F(void);
            # F f { }
            #
            # See 6.9.1.2
            err = "function definition missing parameter list"
            raise CompilerError(err, self.r)

        for param in func_decl.args:
            decl_info = self.get_decl_infos(param)[0]
            # A lone `void` parameter means the function takes no arguments;
            # it contributes no named parameter (and the ctype already has no
            # args), so skip it rather than treating the missing name as an
            # error.
            if (decl_info.identifier is None
                    and decl_info.ctype is not None
                    and decl_info.ctype.is_void()):
                continue
            identifiers.append(decl_info.identifier)

        return identifiers

    def make_specs_ctype(self, specs, any_dec):
        """Make a ctype out of the provided list of declaration specifiers.

        any_dec - Whether these specifiers are used to declare a variable.
        This value is important because `struct A;` has a different meaning
        than `struct A *p;`, since the former forward-declares a new struct
        while the latter may reuse a struct A that already exists in scope.

        Return a `ctype, storage class` pair, where storage class is one of
        the above values.
        """
        spec_range = specs[0].r + specs[-1].r
        storage = self.get_storage([spec.kind for spec in specs], spec_range)
        const = token_kinds.const_kw in {spec.kind for spec in specs}

        struct_union_specs = {token_kinds.struct_kw, token_kinds.union_kw}
        if any(s.kind in struct_union_specs for s in specs):
            node = [s for s in specs if s.kind in struct_union_specs][0]

            # This is a redeclaration of a struct if there are no storage
            # specifiers and it declares no variables.
            redec = not any_dec and storage is None
            base_type = self.parse_struct_union_spec(node, redec)

        # is an enum
        elif any(s.kind == token_kinds.enum_kw for s in specs):
            node = [s for s in specs if s.kind == token_kinds.enum_kw][0]
            base_type = self.parse_enum_spec(node)

        # is a GCC __typeof__(expr): probe the expression's type and use it.
        elif any(isinstance(s, decl_nodes.TypeofSpec) for s in specs):
            ts = [s for s in specs
                  if isinstance(s, decl_nodes.TypeofSpec)][0]
            probe_il = self.il_code.copy_code()
            val = ts.expr.make_il(probe_il, self.symbol_table, self.c)
            base_type = val.ctype

        # is a GCC __auto_type: the real type is inferred from the initializer
        # later (in DeclInfo.process); use the auto sentinel for now.
        elif any(s.kind == token_kinds.auto_type_kw for s in specs):
            base_type = ctypes.auto_type

        # is a C11 _Atomic(type-name) specifier: use the inner type (ShivyCX is
        # single-threaded, so an atomic type degrades to its underlying type).
        elif any(isinstance(s, decl_nodes.AtomicSpec) for s in specs):
            atomic = [s for s in specs
                      if isinstance(s, decl_nodes.AtomicSpec)][0]
            inner_base, _ = self.make_specs_ctype(atomic.root.specs, True)
            base_type, _ = self.make_ctype(atomic.root.decls[0], inner_base)

        # is a typedef
        elif any(s.kind == token_kinds.identifier for s in specs):
            ident = [s for s in specs if s.kind == token_kinds.identifier][0]
            base_type = self.symbol_table.lookup_typedef(ident)

        else:
            base_type = self.get_base_ctype(specs, spec_range)

        if const: base_type = base_type.make_const()
        return base_type, storage

    def get_base_ctype(self, specs, spec_range):
        """Return a base ctype given a list of specs."""

        base_specs = set(ctypes.simple_types)
        base_specs |= {token_kinds.signed_kw, token_kinds.unsigned_kw}

        our_base_specs = [spec.kind.text_repr for spec in specs
                          if spec.kind in base_specs]
        specs_str = " ".join(sorted(our_base_specs))

        # replace "long long" with "long" for convenience
        specs_str = specs_str.replace("long long", "long")

        specs = {
            "void": ctypes.void,

            "_Bool": ctypes.bool_t,

            "char": ctypes.char,
            "char signed": ctypes.char,
            "char unsigned": ctypes.unsig_char,

            "short": ctypes.short,
            "short signed": ctypes.short,
            "int short": ctypes.short,
            "int short signed": ctypes.short,
            "short unsigned": ctypes.unsig_short,
            "int short unsigned": ctypes.unsig_short,

            "int": ctypes.integer,
            "signed": ctypes.integer,
            "int signed": ctypes.integer,
            "unsigned": ctypes.unsig_int,
            "int unsigned": ctypes.unsig_int,

            "long": ctypes.longint,
            "long signed": ctypes.longint,
            "int long": ctypes.longint,
            "int long signed": ctypes.longint,
            "long unsigned": ctypes.unsig_longint,
            "int long unsigned": ctypes.unsig_longint,

            "float": ctypes.flt,
            "double": ctypes.dbl,
            "double long": ctypes.longdouble,
        }

        # `long double` (sorted "double long") is normally the unsupported
        # 80-bit sentinel. Under -f-long-double-as-double we alias it to plain
        # double (64-bit) and warn once that 80-bit math is never supported.
        if specs_str == "double long" and ctypes.long_double_as_double:
            global _warned_long_double_as_double
            if not _warned_long_double_as_double:
                _warned_long_double_as_double = True
                error_collector.add(CompilerError(
                    LONG_DOUBLE_AS_DOUBLE_WARNING, spec_range, warning=True))
            return ctypes.dbl

        if specs_str in specs:
            return specs[specs_str]

        # TODO: provide more helpful feedback on what is wrong
        descrip = "unrecognized set of type specifiers"
        raise CompilerError(descrip, spec_range)

    def get_storage(self, spec_kinds, spec_range):
        """Determine the storage class from given specifier token kinds.

        If no storage class is listed, returns None.
        """
        storage_classes = {token_kinds.auto_kw: DeclInfo.AUTO,
                           token_kinds.register_kw: DeclInfo.AUTO,
                           token_kinds.static_kw: DeclInfo.STATIC,
                           token_kinds.extern_kw: DeclInfo.EXTERN,
                           token_kinds.typedef_kw: DeclInfo.TYPEDEF}

        storage = None
        for kind in spec_kinds:
            if kind in storage_classes and not storage:
                storage = storage_classes[kind]
            elif kind in storage_classes:
                descrip = "too many storage classes in declaration specifiers"
                raise CompilerError(descrip, spec_range)

        return storage

    def parse_enum_spec(self, node):
        """Process an enum specifier node, returning its (integer) ctype.

        Registers any enumerators as integer constants in the symbol table.
        An enum has type `int`; enumerators without an explicit value take the
        previous value plus one, starting from zero.
        """
        if node.enumerators is not None:
            next_val = 0
            for name_tok, value_expr in node.enumerators:
                if value_expr is not None:
                    # Evaluate in a throwaway IL copy: a constant expression
                    # folds to a literal and emits nothing, while a
                    # non-constant one is reported cleanly rather than crashing
                    # (e.g. at file scope where there is no current function).
                    dummy = self.il_code.copy_code()
                    literal = None
                    try:
                        val_il = value_expr.make_il(
                            dummy, self.symbol_table, self.c)
                        literal = val_il.literal
                    except Exception:
                        literal = None
                    if literal is None:
                        err = "enumerator value must be a constant integer"
                        raise CompilerError(err, value_expr.r)
                    next_val = literal.val
                self.symbol_table.add_enum_const(name_tok, next_val)
                next_val += 1
        return ctypes.integer

    def parse_struct_union_spec(self, node, redec):
        """Parse struct or union ctype from the given decl_nodes.Struct node.

        node (decl_nodes.Struct/Union) - the Struct or Union node to parse
        redec (bool) - Whether this declaration is alone like so:

           struct S;
           union U;

        or declares variables/has storage specifiers:

           struct S *p;
           extern struct S;
           union U *u;
           extern union U;

        If it's the first, then this is always a forward declaration for a
        new `struct S` but if it's the second and a `struct S` already
        exists in higher scope, it's just using the higher scope struct.
        """
        has_members = node.members is not None

        if node.kind == token_kinds.struct_kw:
            ctype_req = StructCType
        else:
            ctype_req = UnionCType

        if node.tag:
            tag = str(node.tag)
            ctype = self.symbol_table.lookup_struct_union(tag)

            if node.kind == token_kinds.struct_kw:
                wrong_kind = ctype and not isinstance(ctype, StructCType)
            else:
                wrong_kind = ctype and not isinstance(ctype, UnionCType)
            if wrong_kind:
                err = f"defined as wrong kind of tag '{node.kind} {tag}'"
                raise CompilerError(err, node.r)

            if not ctype or has_members or redec:
                ctype = self.symbol_table.add_struct_union(tag, ctype_req(tag))

            if has_members and ctype.is_complete():
                err = f"redefinition of '{node.kind} {tag}'"
                raise CompilerError(err, node.r)

        else:  # anonymous struct/union
            ctype = ctype_req(None)

        if not has_members:
            return ctype

        # Struct or union does have members
        members = []
        members_set = set()
        bitfields = {}
        anon_bitfield_count = 0
        anon_member_count = 0
        for member in node.members:
            decl_infos = []  # needed in case get_decl_infos below fails
            with report_err():
                # NB: pass in_struct positionally. The self-host transpiler
                # drops a keyword argument that follows a defaulted positional
                # parameter, so `in_struct=True` would arrive as the default
                # False -- which skips the C11 anonymous-member path in
                # get_decl_infos and silently drops anonymous struct/union
                # members from the enclosing type.
                decl_infos = self.get_decl_infos(member, True)

            for decl_info in decl_infos:
                with report_err():
                    if decl_info.bitfield is not None:
                        entry = self._make_bitfield_member(
                            decl_info, node.kind, members_set,
                            anon_bitfield_count)
                        if entry is None:
                            continue  # zero-width: contributes nothing
                        name, mctype, width, signed, was_anon = entry
                        if was_anon:
                            anon_bitfield_count += 1
                        members_set.add(name)
                        members.append((name, mctype))
                        bitfields[name] = (width, signed)
                        continue

                    # C11 anonymous struct/union member: give it an internal,
                    # unreferenceable name so it occupies space; set_members
                    # promotes its members into the enclosing struct/union.
                    if (decl_info.identifier is None
                            and decl_info.ctype.is_struct_union()):
                        anon_name = "<anon-member-%d>" % anon_member_count
                        anon_member_count += 1
                        members.append((anon_name, decl_info.ctype))
                        continue

                    self._check_struct_member_decl_info(
                        decl_info, node.kind, members_set)

                    # A flexible array member must be the last member: if we
                    # already recorded one, no further members may follow.
                    if (members and members[-1][1].is_array()
                            and members[-1][1].is_incomplete()):
                        err = "flexible array member must be the last member"
                        raise CompilerError(err, decl_info.range)

                    name = decl_info.identifier.content
                    members_set.add(name)
                    members.append((name, decl_info.ctype))

        ctype.set_members(members, bitfields)
        return ctype

    def _make_bitfield_member(self, decl_info, kind, members, anon_count):
        """Validate and lay out a bitfield member.

        Returns (name, ctype, width, signed, was_anonymous) or None if the
        bitfield has zero width (which declares no member). Anonymous
        bitfields are given an internal, unreferenceable name.
        """
        ctype = decl_info.ctype
        if not ctype.is_integral():
            err = "bit-field must have integral type"
            raise CompilerError(err, decl_info.range)

        width_val = decl_info.bitfield.make_il(
            self.il_code, self.symbol_table, self.c)
        if not width_val.literal:
            err = "bit-field width must be a compile-time constant"
            raise CompilerError(err, decl_info.range)
        width = width_val.literal.val
        if width < 0:
            err = "bit-field width must be non-negative"
            raise CompilerError(err, decl_info.range)
        if width > ctype.size * 8:
            err = "bit-field width exceeds its type"
            raise CompilerError(err, decl_info.range)

        if decl_info.identifier is None:
            # Anonymous bitfield: zero width declares nothing; positive width
            # reserves storage but cannot be named/accessed.
            if width == 0:
                return None
            name = "<anon-bitfield-%d>" % anon_count
            return name, ctype, width, ctype.signed, True

        # Named bitfield.
        if width == 0:
            err = "named bit-field cannot have zero width"
            raise CompilerError(err, decl_info.range)
        name = decl_info.identifier.content
        if name in members:
            err = f"duplicate member '{name}'"
            raise CompilerError(err, decl_info.identifier.r)
        return name, ctype, width, ctype.signed, False

    def _check_struct_member_decl_info(self, decl_info, kind, members):
        """Check whether given decl_info object is a valid struct member."""

        if decl_info.identifier is None:
            # someone snuck an abstract declarator into here!
            err = f"missing name of {kind} member"
            raise CompilerError(err, decl_info.range)

        if decl_info.storage is not None:
            err = f"cannot have storage specifier on {kind} member"
            raise CompilerError(err, decl_info.range)

        if decl_info.ctype.is_function():
            err = f"cannot have function type as {kind} member"
            raise CompilerError(err, decl_info.range)

        # 6.7.2.1.18: a flexible array member -- an array of unknown size --
        # is permitted as the last member of a struct. Layout (set_members)
        # gives it an offset but no size; the caller enforces that it is last.
        if not decl_info.ctype.is_complete():
            if not decl_info.ctype.is_array():
                err = f"cannot have incomplete type as {kind} member"
                raise CompilerError(err, decl_info.range)

        # TODO: 6.7.2.1.13 (anonymous structs)
        if decl_info.identifier.content in members:
            err = f"duplicate member '{decl_info.identifier.content}'"
            raise CompilerError(err, decl_info.identifier.r)
