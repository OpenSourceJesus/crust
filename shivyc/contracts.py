"""Compile-time checking of function preconditions ("contracts").

A function definition may carry one or more precondition clauses between its
parameter list and its body, borrowing Python's `assert` syntax:

    void process_input(char *user_input)
        assert len(user_input) <= 16
    { ... }

These clauses are recognized and stripped to plain C by `extensions.py`, which
records them as structured bounds per argument:

    {'user_input': {'len<=': 16}}

This module performs the *checking*: at each call site where an argument's
length is statically known (a string literal, or a variable proven to hold
one), it evaluates the bound and reports a violation as a compile-time error,
catching e.g. a too-long string before the program ever runs.

The analysis is conservative and never reports a false violation: a bound is
checked only when the argument's length is known; otherwise it is left for
runtime as usual.
"""

from shivyc.errors import CompilerError
from shivyc import proofs

# func name -> {arg_name -> {'len<=': N, 'len>=': N, 'div-by': N}}
_meta = {}
# func name -> [parameter name, ...] in declaration order
_params = {}
# Name of the function whose body is currently being compiled (for messages).
current_function = None
# id(ILValue) -> known strlen, for variables proven to hold a string literal.
_known_len = {}


def reset_unit():
    """Clear all per-translation-unit contract state."""
    _meta.clear()
    _params.clear()
    _known_len.clear()


def reset_function():
    """Clear per-function value tracking (called when a body begins)."""
    _known_len.clear()


def install_contracts(contracts):
    """Install the parsed contract bounds for this translation unit."""
    _meta.clear()
    if contracts:
        for name, args in contracts.items():
            _meta[name] = {a: dict(b) for a, b in args.items()}


def set_params(name, param_tokens):
    """Record a function's parameter names (in order) for call-site mapping."""
    if name:
        _params[name] = [
            (p.content if p is not None else None)
            for p in (param_tokens or [])
        ]


def set_known_len(ilvalue, length):
    """Record that `ilvalue` holds a string of known length."""
    if ilvalue is not None:
        _known_len[id(ilvalue)] = length


def clear_known_len(ilvalue):
    """Forget any known length for `ilvalue` (e.g. after reassignment)."""
    if ilvalue is not None:
        _known_len.pop(id(ilvalue), None)


def known_len(ilvalue):
    """Return the known string length of `ilvalue`, or None."""
    if ilvalue is None:
        return None
    return _known_len.get(id(ilvalue))


_TYPE_NAMES = {}


def _describe_ctype(ctype):
    """Best-effort human-readable rendering of a ctype, e.g. 'char *'."""
    import shivyc.ctypes as ctypes
    if not _TYPE_NAMES:
        labels = {
            "void": "void", "bool_t": "_Bool", "char": "char",
            "unsig_char": "unsigned char", "short": "short",
            "unsig_short": "unsigned short", "integer": "int",
            "unsig_int": "unsigned int", "longint": "long",
            "unsig_longint": "unsigned long", "flt": "float", "dbl": "double"}
        for attr, label in labels.items():
            obj = getattr(ctypes, attr, None)
            if obj is not None:
                _TYPE_NAMES[id(obj)] = label
    if ctype is None:
        return "?"
    if ctype.is_pointer():
        return _describe_ctype(ctype.arg) + " *"
    if ctype.is_array():
        return _describe_ctype(ctype.el) + " []"
    return _TYPE_NAMES.get(id(ctype), "?")


def _arg_str_len(arg, symbol_table):
    """Return the known string length of a call argument, or None."""
    import shivyc.tree.primary_exprs as prim
    if isinstance(arg, prim.String):
        # chars includes the terminating null; strlen excludes it.
        return max(0, len(arg.chars) - 1)
    if isinstance(arg, prim.Identifier):
        try:
            return known_len(symbol_table.lookup_variable(arg.identifier))
        except CompilerError:
            return None
    return None


def _describe_arg(arg, symbol_table):
    """Describe a call argument for a diagnostic, e.g. 'char * large_string'."""
    import shivyc.tree.primary_exprs as prim
    if isinstance(arg, prim.Identifier):
        name = arg.identifier.content
        try:
            var = symbol_table.lookup_variable(arg.identifier)
            return f"the variable `{_describe_ctype(var.ctype)} {name}`"
        except CompilerError:
            return f"the variable `{name}`"
    if isinstance(arg, prim.String):
        return "the string literal argument"
    return "the argument"


def _bound_text(arg_name, bound, length=None):
    """The clause this length actually breaks, as it was written.

    It used to be the first clause present, which for a contract of several
    clauses named one the argument might well satisfy while breaking another.
    """
    broken = proofs.failing(length, bound) if length is not None else []
    if not broken:
        broken = [key for key in proofs.BOUNDS if key in bound]
    if not broken:
        return "", "violates the contract"
    return proofs.clause_text(arg_name, broken[0], bound)


def _violates(length, bound):
    """Whether a known length violates the contract -- any clause of it.

    Delegated to `shivyc.proofs` so that this pass and `simd_contracts` read
    one contract the same way, and so that the reading can be checked by a
    proof kernel when `CRUST_PROOFS=1`.
    """
    return proofs.violates(length, bound)


def check_call(callsite, callee_name, symbol_table):
    """Check a call against the callee's contracts; raise on a violation."""
    meta = _meta.get(callee_name)
    if not meta:
        return
    params = _params.get(callee_name)
    if params is None:
        return
    args = callsite.args

    for arg_name, bound in meta.items():
        if arg_name not in params:
            continue
        pos = params.index(arg_name)
        if pos >= len(args):
            continue
        length = _arg_str_len(args[pos], symbol_table)
        if length is None:
            continue
        if _violates(length, bound):
            text, verb = _bound_text(arg_name, bound, length)
            proofs.certified(length, bound)      # raises on a disagreement
            where = (f"in the function `{current_function}`, "
                     if current_function else "")
            arg_desc = _describe_arg(args[pos], symbol_table)
            raise CompilerError(
                f"{where}{arg_desc} {verb} to pass to the function "
                f"`{callee_name}` because of the contract: `assert {text}`",
                callsite.r)
