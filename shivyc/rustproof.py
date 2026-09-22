"""Lift a Rust function, contracts included, into `hoare.py`'s fragment.

`ilproof.py` lifts a function from the IL, after the front end, and that
reaches Rust only as far as the IL keeps what a proof needs.  For idiomatic
Rust it keeps little: a `match` arrives as a `switch` the lift sees as
`goto`s, a tail `if` expression leaves a dangling return, `&&` is jumps, and
a `u32` is an `int` of no particular width.  This module lifts from the
*source* instead, where all of that is still there, and it reads the
contracts written on the function:

    #[ensures(result <= 23)]
    fn elf_regs_for_class(cls: u32) -> u32 {
        match cls { 0 => 6, 1 => 10, 2 => 15, _ => 23 }
    }

becomes the same fragment `ilproof.py` writes and a person would, with its
postcondition beside it:

    def elf_regs_for_class(cls: 'Nat') -> 'Nat':
        if (cls == 0):
            return 6
        elif (cls == 1):
            ...

    from shivyc.rustproof import lift
    f = lift(source, 'elf_regs_for_class')
    proc = hoare.read_procedure(f.source, env, signatures, f.ensures)

Like `ilproof.py`, it writes source and imports nothing from RosettaMath, so
it costs nothing where there is no kernel, and everything it writes is
checked downstream by a kernel that trusts none of it.

What is claimed, and what is not
--------------------------------
  * Unsigned integers are Nat.  That is exact for `+`, `*`, `/`, `%` and
    comparison until a value passes the type's maximum, and Rust's `-` on
    an unsigned value panics where Nat's truncates at zero.  A theorem about
    the lifted function is a theorem about that model of the arithmetic;
    overflow and underflow are the next step, as obligations of their own.
    Signed integers are refused.

  * `#[requires]` becomes the fragment's leading `assert`s, `#[ensures]` its
    postconditions, and `#[invariant]`/`#[variant]` on a `while` its loop
    annotations -- the same clauses Crust checks at runtime.  In an
    `ensures`, a parameter means its value at entry, which is what `old(x)`
    says; a clause naming a `mut` parameter without `old` is refused rather
    than read one way or the other.

  * The lift refuses rather than approximates, naming the construct:
    references, fields, methods, indexing, signed or float types, `loop`,
    `break`, macros and recursion all come back as a `LiftError`.  The
    refusals are the roadmap.

`tests/test_rustproof.py` checks the lift the way `test_ilproof.py` checks
its own: each function is compiled by Crust and run, and the lifted term is
evaluated through the kernel on the same inputs.
"""

from shivyc.crust import tokenize, RustToken


class LiftError(Exception):
    """A construct with no lifting.  Says what, rather than guessing."""


class _Probed(Exception):
    """Raised by `deliver` when a probe reaches a braced value's tail; it
    carries the tail's type, which is all the probe wanted."""

    def __init__(self, ty):
        self.ty = ty


_PROBE = "__probe__"


# Rust's unsigned integers and their widths; all are Nat in the fragment.
_UNSIGNED = {"u8": 8, "u16": 16, "u32": 32, "u64": 64, "u128": 128,
             "usize": 64}
_SIGNED = ("i8", "i16", "i32", "i64", "i128", "isize")

# Names a lifted variable must not take: Python's keywords, and the names
# the fragment gives a meaning of its own.
_RESERVED = {"and", "or", "not", "if", "elif", "else", "while", "for", "in",
             "is", "def", "return", "pass", "lambda", "class", "import",
             "from", "as", "with", "try", "except", "finally", "raise",
             "global", "nonlocal", "del", "assert", "yield", "await",
             "async", "break", "continue", "None", "True", "False",
             "result", "len", "range", "invariant", "variant"}

# Binary operators by precedence, lowest first, as Rust groups them.
_LEVELS = [("||",), ("&&",), ("==", "!=", "<", ">", "<=", ">="),
           ("|",), ("^",), ("&",), ("<<", ">>"), ("+", "-"),
           ("*", "/", "%")]


class Lifted:
    """One lifted function: its fragment source and its contract."""

    def __init__(self, name, source, ensures, params, ret, callees):
        self.name = name
        self.source = source            # `def name(..)` in hoare's dialect
        self.ensures = ensures          # postconditions over `result`
        self.params = params            # [(name, 'Nat' | 'Bool')]
        self.ret = ret                  # 'Nat' | 'Bool'
        self.callees = callees          # lifted functions it calls

    def __repr__(self):
        return "Lifted(%s)" % self.name


def functions(source):
    """The top-level `fn` items of a Rust source: {name: token index}."""
    toks = tokenize(source) + [RustToken("eof", "", 0)]
    found, depth = {}, 0
    for k, t in enumerate(toks):
        if t.kind == "punc" and t.val == "{":
            depth += 1
        elif t.kind == "punc" and t.val == "}":
            depth -= 1
        elif depth == 0 and t.kind == "kw" and t.val == "fn" \
                and toks[k + 1].kind == "ident":
            found[toks[k + 1].val] = k
    return found


def lift(source, name):
    """Lift function `name` from Rust `source`; a `Lifted`."""
    return _Unit(source).lift(name)


def lift_all(source):
    """Every top-level function: ({name: Lifted}, {name: refusal})."""
    unit = _Unit(source)
    ok, refused = {}, {}
    for name in unit.fn_index:
        try:
            ok[name] = unit.lift(name)
        except LiftError as exc:
            refused[name] = str(exc)
    return ok, refused


def signatures(lifted):
    """The calls a lifted function makes, as `read_procedure` wants them:
    {callee: ([argument type names], result type name)}.  Names, not kernel
    types, so this module needs no kernel; the caller maps them."""
    out = {}
    for callee in _closure(lifted):
        if callee is not lifted:
            out[callee.name] = ([t for _, t in callee.params], callee.ret)
    return out


def in_dependency_order(lifted):
    """`lifted` and everything it calls, callees first -- the order in which
    to hand them to `read_procedure`."""
    return list(reversed(_closure(lifted)))


def _closure(lifted):
    seen, out, stack = set(), [], [lifted]
    while stack:
        f = stack.pop()
        if f.name in seen:
            continue
        seen.add(f.name)
        out.append(f)
        stack.extend(f.callees)
    return out


class _Unit:
    """The functions of one source, lifted on demand and at most once."""

    # Field names are distinctive on purpose: py2c infers a field's type by
    # its name across the module, and a `done` elsewhere is a bool.
    def __init__(self, source):
        self.toks = tokenize(source) + [RustToken("eof", "", 0)]
        self.fn_index = functions(source)
        self.lifted_fns = {}
        self.in_progress = []

    def lift(self, name):
        if name in self.lifted_fns:
            return self.lifted_fns[name]
        if name not in self.fn_index:
            raise LiftError("no top-level function `%s` in this source" % name)
        if name in self.in_progress:
            raise LiftError("`%s` is recursive, and recursion is not lifted: "
                            "a fold needs a bound the text does not give"
                            % name)
        self.in_progress.append(name)
        try:
            lifted = _FnLifter(self, name).run()
        finally:
            del self.in_progress[-1]
        self.lifted_fns[name] = lifted
        return lifted


class _Ty:
    """A value's type in the lift: Nat with a width, or Bool."""

    def __init__(self, kind, width=0):
        self.kind = kind                # "nat" | "bool"
        self.width = width              # bits, for a nat

    def frag(self):
        return "Nat" if self.kind == "nat" else "Bool"


_BOOL = _Ty("bool")
_LIT = _Ty("nat", 0)            # an integer literal: fits any width


class _FnLifter:
    """Lift one function.  A recursive descent over its tokens that writes
    fragment statements as it goes, as `crust.py` writes C."""

    def __init__(self, unit, name):
        self.unit = unit
        self.toks = unit.toks
        self.name = name
        self.i = self._attrs_start(unit.fn_index[name])
        self.lines = []
        self.indent = 1
        self.scopes = [{}]              # rust name -> (fragment name, _Ty)
        self.used = set()
        self.callees = []
        self.loop_attrs = []
        # Locals first bound inside an `if` or a loop, with their types: the
        # fragment wants every variable to have a value before the branch or
        # loop that assigns it, so these are given one at the top.
        self.nested_locals = []

    # -- tokens -------------------------------------------------------------

    @property
    def cur(self):
        return self.toks[self.i]

    def peek(self, k=1):
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def next(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def at(self, val):
        return self.cur.val == val and self.cur.kind in ("punc", "kw",
                                                         "ident")

    def accept(self, val):
        if self.at(val):
            return self.next()
        return None

    def expect(self, val):
        if not self.at(val):
            self.fail("expected `%s`, found `%s`" % (val, self.cur.val))
        return self.next()

    def fail(self, message):
        raise LiftError("`%s`, line %d: %s" % (self.name, self.cur.line,
                                               message))

    def _attrs_start(self, fn_at):
        """Back up from `fn` over `pub` and `#[..]` attributes."""
        k = fn_at
        while k > 0:
            t = self.toks[k - 1]
            if t.val == "pub" or t.val in ("unsafe", "const"):
                k -= 1
                continue
            if t.val == "]":
                depth, j = 0, k - 1
                while j >= 0:
                    if self.toks[j].val == "]":
                        depth += 1
                    elif self.toks[j].val == "[":
                        depth -= 1
                        if depth == 0:
                            break
                    j -= 1
                if j > 0 and self.toks[j - 1].val == "#":
                    k = j - 1
                    continue
            break
        return k

    # -- output -------------------------------------------------------------

    def emit(self, line):
        self.lines.append("    " * self.indent + line)

    def fresh(self, hint):
        base = hint if hint not in _RESERVED else hint + "_"
        name, n = base, 1
        while name in self.used:
            n += 1
            name = "%s_%d" % (base, n)
        self.used.add(name)
        return name

    def bind(self, rust_name, ty):
        """Declare a Rust binding; a fresh fragment name if it shadows."""
        frag = self.fresh(rust_name)
        self.scopes[-1][rust_name] = (frag, ty)
        self.note_local(frag, ty)
        return frag

    def note_local(self, frag, ty):
        if self.indent > 1 and len(self.scopes) > 1:
            self.nested_locals.append((frag, ty))

    def alias(self, rust_name, frag, ty):
        self.scopes[-1][rust_name] = (frag, ty)

    def lookup(self, rust_name):
        for scope in reversed(self.scopes):
            if rust_name in scope:
                return scope[rust_name]
        return None

    # -- the function ---------------------------------------------------------

    def run(self):
        clauses = self.attributes()
        for kind, _toks, _line in clauses:
            if kind in ("invariant", "variant"):
                self.fail("`#[%s]` belongs on a loop" % kind)
        self.accept("pub")
        self.expect("fn")
        self.expect(self.name)
        if self.at("<"):
            self.fail("generic functions are not lifted")
        self.expect("(")
        params, mutable = [], set()
        while not self.at(")"):
            if self.accept("mut"):
                mutable.add(self.cur.val)
            if self.cur.kind != "ident":
                self.fail("a parameter must be a plain name, not a pattern")
            pname = self.next().val
            self.expect(":")
            ty = self.read_type("parameter `%s`" % pname)
            params.append((self.bind(pname, ty), ty))
            if not self.accept(","):
                break
        self.expect(")")
        if not self.accept("->"):
            self.fail("a function returning `()` has nothing to claim a "
                      "postcondition about")
        ret = self.read_type("the return type")
        if self.at("where"):
            self.fail("`where` clauses are not lifted")

        # Preconditions first: the fragment reads leading `assert`s as the
        # precondition, which is what `#[requires]` is.
        for kind, toks, line in clauses:
            if kind == "requires":
                self.emit("assert %s" % self.clause(toks, line, None))
        ensures = []
        for kind, toks, line in clauses:
            if kind == "ensures":
                ensures.append(self.clause(toks, line, ret, mutable))

        self.expect("{")
        n_asserts = len(self.lines)
        self.block_body("ret", ret)
        # A fresh name is never reused, so a starting value cannot be read by
        # anything but the code that then assigns it.
        inits = ["    %s = %s" % (n, "False" if t.kind == "bool" else "0")
                 for n, t in self.nested_locals]
        self.lines[n_asserts:n_asserts] = inits
        head = "def %s(%s) -> '%s':" % (
            self.name, ", ".join("%s: '%s'" % (n, t.frag())
                                 for n, t in params), ret.frag())
        source = "\n".join([head] + self.lines) + "\n"
        return Lifted(self.name, source, ensures,
                      [(n, t.frag()) for n, t in params], ret.frag(),
                      self.callees)

    def attributes(self):
        """The `#[..]` run at the cursor: [(kind, tokens, line)] for the
        contract clauses; everything else is skipped."""
        out = []
        while self.at("#"):
            self.next()
            self.expect("[")
            j = self.i
            while self.toks[j].kind == "ident" and self.toks[j + 1].val == "::":
                j += 2
            head = self.toks[j]
            depth = 1
            start = None
            if head.val in ("requires", "ensures", "invariant", "variant") \
                    and self.toks[j + 1].val == "(":
                start = j + 2
            while depth:
                t = self.next()
                if t.kind == "eof":
                    self.fail("unterminated attribute")
                if t.val == "[":
                    depth += 1
                elif t.val == "]":
                    depth -= 1
            if start is not None:
                # the clause runs to the `)` just before the closing `]`
                out.append((head.val, self.toks[start:self.i - 2],
                            head.line))
        return out

    def read_type(self, where):
        t = self.cur
        if t.val in _UNSIGNED:
            self.next()
            return _Ty("nat", _UNSIGNED[t.val])
        if t.val == "bool":
            self.next()
            return _BOOL
        if t.val in _SIGNED:
            self.fail("%s is `%s`; signed integers are not lifted yet, only "
                      "unsigned ones (which are Nat)" % (where, t.val))
        self.fail("%s has type `%s`, which is not lifted; the fragment has "
                  "unsigned integers and `bool`" % (where, t.val))

    def clause(self, toks, line, ret, mutable=()):
        """A contract clause, as a fragment expression."""
        for k, t in enumerate(toks):
            if t.kind == "ident" and t.val in ("forall", "exists") \
                    and k + 1 < len(toks) and toks[k + 1].val == "(":
                self.fail("quantified clauses (`%s`) are not lifted yet"
                          % t.val)
        # A second lifter over the clause's tokens, sharing this one's names
        # and output so a clause reads the function's bindings.
        sub = _FnLifter(self.unit, self.name)
        sub.used, sub.callees, sub.lines = self.used, self.callees, self.lines
        sub.indent = self.indent
        sub.toks = list(_strip_old(toks, mutable, self)) + \
            [RustToken("eof", "", line)]
        sub.i = 0
        merged = {}
        for scope in self.scopes:
            merged.update(scope)
        sub.scopes = [merged]
        if ret is not None:
            sub.scopes[0]["result"] = ("result", ret)
        expr, _ty = sub.expr_pure()
        if sub.cur.kind != "eof":
            sub.fail("unexpected `%s` in a contract clause" % sub.cur.val)
        return expr

    # -- statements -----------------------------------------------------------

    def block_body(self, mode, want):
        """The statements of a block up to its `}`, which is consumed.

        `mode` is what the block's tail value is for: "ret" returns it, a
        fragment variable name receives it, and None means the block is a
        statement and has no value.
        """
        self.scopes.append({})
        try:
            while not self.at("}"):
                if self.cur.kind == "eof":
                    self.fail("unterminated block")
                if self.statement(mode, want):
                    break
            self.expect("}")
        finally:
            self.scopes.pop()

    def statement(self, mode, want):
        """One statement.  True if it was the block's tail value."""
        self.loop_attrs = self.attributes() if self.at("#") else []
        t = self.cur
        if self.loop_attrs and not (t.val in ("while", "for") and
                                    t.kind == "kw"):
            self.fail("loop contracts must be on a `while` or `for` here")
        if t.kind == "kw" and t.val == "let":
            self.let_stmt()
            return False
        if t.kind == "kw" and t.val == "return":
            self.next()
            if self.at(";") or self.at("}"):
                self.fail("a `return` with no value")
            e, _ = self.expr()
            self.accept(";")
            self.emit("return %s" % e)
            return False
        if t.kind == "kw" and t.val in ("loop", "break", "continue"):
            self.fail("`%s` is not lifted: a fold needs the loop's bound up "
                      "front, and `break` has no place in one" % t.val)
        if t.kind == "kw" and t.val == "while":
            self.while_stmt()
            return False
        if t.kind == "kw" and t.val == "for":
            self.for_stmt()
            return False
        if t.kind == "kw" and t.val in ("if", "match") or t.val == "{":
            # A braced statement: the tail value if nothing follows it.
            if self._ends_block():
                self.value_into(mode, want)
                return True
            self.value_into(None, None)
            self.accept(";")
            return False
        if t.kind == "ident" and self.peek().val in ("=", "+=", "-=", "*=",
                                                     "/=", "%="):
            self.assign_stmt()
            return False
        # A tail expression, or an expression statement with no effect.
        e, ty = self.expr()
        if self.accept(";"):
            self.fail("an expression statement has no effect in the model")
        if not self.at("}"):
            self.fail("expected `;` or `}` after an expression")
        self.deliver(mode, want, e, ty)
        return True

    def _ends_block(self):
        """Is the braced statement at the cursor the last thing in its block?"""
        depth, j = 0, self.i
        while self.toks[j].kind != "eof":
            v = self.toks[j].val
            if v == "{":
                depth += 1
            elif v == "}":
                depth -= 1
                if depth == 0:
                    nxt = self.toks[j + 1]
                    if nxt.val == "else":
                        j += 1
                        continue
                    k = j + 1
                    while self.toks[k].val == ";":
                        k += 1
                    return self.toks[k].val == "}"
            j += 1
        return False

    def deliver(self, mode, want, e, ty):
        if mode == _PROBE:
            raise _Probed(ty)
        if mode is None:
            self.fail("a value where a statement was expected")
        self._check_ty(want, ty)
        if mode == "ret":
            self.emit("return %s" % e)
        else:
            self.emit("%s = %s" % (mode, e))

    def _check_ty(self, want, ty):
        if want is not None and ty is not None and want.kind != ty.kind:
            self.fail("a `%s` where a `%s` is wanted" % (ty.frag(),
                                                         want.frag()))

    def let_stmt(self):
        self.expect("let")
        self.accept("mut")
        if self.cur.kind != "ident":
            self.fail("`let` with a pattern is not lifted; bind a plain name")
        name = self.next().val
        want = self.read_type("`%s`" % name) if self.accept(":") else None
        if not self.accept("="):
            self.fail("`let %s;` with no value: every binding needs one "
                      "going in" % name)
        e, ty = self.expr()
        self.expect(";")
        if want is not None:
            self._check_ty(want, ty)
            ty = want
        frag = self.bind(name, ty)
        self.emit("%s = %s" % (frag, e))

    def assign_stmt(self):
        name = self.next().val
        found = self.lookup(name)
        if found is None:
            self.fail("assignment to `%s`, which is not a local" % name)
        op = self.next().val
        e, ty = self.expr()
        self.expect(";")
        self._check_ty(found[1], ty)
        if op == "=":
            self.emit("%s = %s" % (found[0], e))
            return
        py = {"+=": "+", "-=": "-", "*=": "*", "/=": "//", "%=": "%"}[op]
        self.emit("%s = (%s %s %s)" % (found[0], found[0], py, e))

    def while_stmt(self):
        attrs = self.loop_attrs
        self.expect("while")
        if self.at("let"):
            self.fail("`while let` is not lifted")
        cond, ty = self.expr(no_struct=True)
        self._check_ty(_BOOL, ty)
        variants = [a for a in attrs if a[0] == "variant"]
        if not variants:
            self.fail("a `while` needs `#[variant(..)]`: a Nat that "
                      "strictly decreases, which is what makes it a fold")
        self.emit("while %s:" % cond)
        self.indent += 1
        for kind, toks, line in attrs:
            self.emit("assert %s(%s)" % (kind, self.clause(toks, line, None)))
        self.expect("{")
        self.block_body(None, None)
        self.indent -= 1

    def for_stmt(self):
        self.expect("for")
        if self.cur.kind != "ident":
            self.fail("the loop variable must be a plain name")
        var = self.next().val
        self.expect("in")
        lo, lty = self.expr(no_struct=True)
        if self.accept("..="):
            inclusive = True
        else:
            self.expect("..")
            inclusive = False
        hi, hty = self.expr(no_struct=True)
        for ty in (lty, hty):
            self._check_ty(_LIT, ty)
        # `for i in lo..hi` is `range(hi - lo)` shifted by `lo`. When
        # `hi < lo`, Nat's `-` floors at 0 and the loop runs no times --
        # which is exactly Rust's empty range, so here the truncation is
        # not a paraphrase.
        count = "(%s - %s)" % (hi, lo)
        if inclusive:
            count = "((%s + 1) - %s)" % (hi, lo)
        self.scopes.append({})
        if lo == "0":
            idx = self.bind(var, _wider(lty, hty))
            self.emit("for %s in range(%s):" % (idx, hi if not inclusive
                                                else "(%s + 1)" % hi))
            self.indent += 1
        else:
            k = self.fresh("k")
            self.emit("for %s in range(%s):" % (k, count))
            self.indent += 1
            idx = self.bind(var, _wider(lty, hty))
            self.emit("%s = (%s + %s)" % (idx, lo, k))
        self.expect("{")
        self.block_body(None, None)
        self.indent -= 1
        self.scopes.pop()

    # -- values of braced expressions -------------------------------------------

    def value_into(self, mode, want):
        """An `if`, `match` or block, its value going to `mode`."""
        t = self.cur
        if t.val == "{":
            self.next()
            self.block_body(mode, want)
        elif t.val == "if":
            self.if_value(mode, want)
        elif t.val == "match":
            self.match_value(mode, want)
        else:
            self.fail("expected `if`, `match` or a block")

    def if_value(self, mode, want):
        self.expect("if")
        if self.at("let"):
            self.fail("`if let` is not lifted")
        cond, ty = self.expr(no_struct=True)
        self._check_ty(_BOOL, ty)
        self.emit("if %s:" % cond)
        self._arm_block(mode, want)
        while self.accept("else"):
            if self.accept("if"):
                cond, ty = self.expr(no_struct=True)
                self._check_ty(_BOOL, ty)
                self.emit("elif %s:" % cond)
                self._arm_block(mode, want)
                continue
            if mode == "ret":
                # Every branch above returns, so the `else` is what falls
                # through -- and the fragment wants a body that ends in a
                # `return`, not one whose last statement is an `if`.
                self.expect("{")
                self.block_body(mode, want)
                return
            self.emit("else:")
            self._arm_block(mode, want)
            return
        if mode is not None:
            self.fail("an `if` used as a value needs an `else`")

    def _arm_block(self, mode, want):
        self.expect("{")
        self.indent += 1
        before = len(self.lines)
        self.block_body(mode, want)
        if len(self.lines) == before:
            self.emit("pass")
        self.indent -= 1

    def match_value(self, mode, want):
        r"""A `match` as an `if`/`elif` chain over the scrutinee.

        Literal patterns test for equality, `a | b` either, `lo..=hi` a
        range, `_` and a bare name anything -- the name standing for the
        scrutinee in its arm.  A guard is conjoined.  The last arm, if it
        has no guard, becomes the `else`: Rust has already checked the arms
        cover every value, so whatever reaches it matches it.
        """
        self.expect("match")
        s, sty = self.expr(no_struct=True)
        if not _is_name(s):
            tmp = self.fresh("scrut")
            self.note_local(tmp, sty)
            self.emit("%s = %s" % (tmp, s))
            s = tmp
        self.expect("{")
        arms = []
        while not self.at("}"):
            if self.cur.kind == "eof":
                self.fail("unterminated `match`")
            cond, binds = self.pattern(s, sty)
            guard = None
            self.scopes.append({})
            for bname in binds:
                self.alias(bname, s, sty)
            if self.accept("if"):
                guard, gty = self.expr()
                self._check_ty(_BOOL, gty)
            self.expect("=>")
            arms.append((cond, guard, self.i, dict(self.scopes[-1])))
            self.scopes.pop()
            self._skip_arm_body()
        self.expect("}")
        end = self.i
        for n, (cond, guard, body_at, scope) in enumerate(arms):
            test = cond
            if guard is not None:
                test = guard if cond == "True" else "(%s and %s)" % (cond,
                                                                     guard)
            last = n == len(arms) - 1
            falls = last and guard is None
            if falls and mode == "ret" and n:
                pass        # the fall-through, unindented: see `if_value`
            elif falls:
                self.emit("else:" if n else "if True:")
            else:
                self.emit(("if %s:" if n == 0 else "elif %s:") % test)
            if not (falls and mode == "ret" and n):
                self.indent += 1
            before = len(self.lines)
            saved = self.i
            self.i = body_at
            self.scopes.append(scope)
            try:
                if self.at("{"):
                    self.next()
                    self.block_body(mode, want)
                else:
                    e, ty = self.expr()
                    if mode is None:
                        self.fail("a `match` arm with a value in statement "
                                  "position")
                    self.deliver(mode, want, e, ty)
            finally:
                self.scopes.pop()
            if len(self.lines) == before:
                self.emit("pass")
            if not (falls and mode == "ret" and n):
                self.indent -= 1
            self.i = saved
        self.i = end

    def _skip_arm_body(self):
        """Step over one arm's body and its `,`, without lowering it."""
        depth = 0
        if self.at("{"):
            while True:
                t = self.next()
                if t.val == "{":
                    depth += 1
                elif t.val == "}":
                    depth -= 1
                    if depth == 0:
                        break
            self.accept(",")
            return
        while self.cur.kind != "eof":
            v = self.cur.val
            if v in ("(", "[", "{"):
                depth += 1
            elif v in (")", "]", "}"):
                if depth == 0:
                    return
                depth -= 1
            elif v == "," and depth == 0:
                self.next()
                return
            self.next()

    def pattern(self, s, sty):
        """`p | q | ..` against scrutinee `s`: (condition, bound names)."""
        self.accept("|")
        conds, binds = [], []
        while True:
            c, b = self.pattern_one(s, sty)
            conds.append(c)
            binds.extend(b)
            if not self.accept("|"):
                break
        if len(conds) > 1 and binds:
            self.fail("a binding inside `|` alternatives is not lifted")
        if "True" in conds:
            return "True", binds
        if len(conds) == 1:
            return conds[0], binds
        return "(%s)" % " or ".join(conds), binds

    def pattern_one(self, s, sty):
        t = self.cur
        if t.kind == "ident" and t.val == "_":
            self.next()
            return "True", []
        if t.kind == "kw" and t.val in ("true", "false"):
            self.next()
            return ("%s" % s if t.val == "true" else "(not %s)" % s), []
        if t.kind == "num":
            lo = str(_int_literal(self.next(), self))
            if self.at("..=") or self.at(".."):
                inclusive = self.next().val == "..="
                if self.cur.kind != "num":
                    self.fail("a range pattern needs two literal bounds")
                hi = str(_int_literal(self.next(), self))
                if inclusive:
                    return "(%s <= %s and %s <= %s)" % (lo, s, s, hi), []
                return "(%s <= %s and %s < %s)" % (lo, s, s, hi), []
            return "(%s == %s)" % (s, lo), []
        if t.val == "-":
            self.fail("a negative pattern cannot match an unsigned value")
        if t.kind == "ident" and self.peek().val not in ("::", "(", "{"):
            self.next()
            return "True", [t.val]
        self.fail("the pattern `%s` is not lifted; the fragment matches "
                  "integer literals, ranges, `_` and bindings" % t.val)

    # -- expressions --------------------------------------------------------------

    def expr(self, no_struct=False):
        """An expression: (fragment text, _Ty).  May emit statements, for an
        `if`, `match` or block used as a value.  (`no_struct` documents a
        condition position; the lift has no struct literals to confuse with
        a block.)"""
        return self.binary(0)

    def expr_pure(self):
        before = len(self.lines)
        e = self.expr()
        if len(self.lines) != before:
            self.fail("a contract clause must be an expression, not an "
                      "`if`, `match` or block")
        return e

    def binary(self, level):
        if level == len(_LEVELS):
            return self.cast()
        left, lty = self.binary(level + 1)
        while self.cur.kind == "punc" and self.cur.val in _LEVELS[level]:
            if self.cur.val == "==" and self.peek().val == ">":
                break                           # `==>`, handled below
            op = self.next().val
            right, rty = self.binary(level + 1)
            left, lty = self.combine(op, left, lty, right, rty)
        # `==>` is how Creusot and Prusti write implication; it lexes as `==`
        # and `>`, so it is caught here, at the lowest level.
        if level == 0 and self.at("==") and self.peek().val == ">":
            self.next()
            self.next()
            right, rty = self.binary(0)
            self._check_ty(_BOOL, lty)
            self._check_ty(_BOOL, rty)
            return "((not %s) or %s)" % (left, right), _BOOL
        return left, lty

    def combine(self, op, left, lty, right, rty):
        if op in ("&&", "||"):
            self._check_ty(_BOOL, lty)
            self._check_ty(_BOOL, rty)
            return "(%s %s %s)" % (left, "and" if op == "&&" else "or",
                                   right), _BOOL
        if op in ("|", "^", "&"):
            self.fail("bitwise `%s` is not lifted; the fragment's integers "
                      "are Nat, not bit vectors" % op)
        if op in ("<<", ">>"):
            if not _is_int(right):
                self.fail("a shift by a non-literal amount is not lifted")
            k = 2 ** int(right)
            # On an unsigned value `>> k` is division by 2**k exactly, with
            # no truncation and no wrap; `<< k` is multiplication, exact
            # until it overflows -- the same claim as `*`.
            if op == ">>":
                return "(%s // %d)" % (left, k), lty
            return "(%s * %d)" % (left, k), lty
        if op in ("==", "!=", "<", ">", "<=", ">="):
            if lty.kind != rty.kind:
                self.fail("comparing a `%s` with a `%s`" % (lty.frag(),
                                                            rty.frag()))
            if lty.kind == "bool" and op not in ("==", "!="):
                self.fail("ordering on `bool` is not lifted")
            if op == "!=":
                # The fragment has no `!=`; it is `not ==`.
                return "(not (%s == %s))" % (left, right), _BOOL
            return "(%s %s %s)" % (left, op, right), _BOOL
        self._check_ty(_LIT, lty)
        self._check_ty(_LIT, rty)
        py = {"+": "+", "-": "-", "*": "*", "/": "//", "%": "%"}[op]
        return "(%s %s %s)" % (left, py, right), _wider(lty, rty)

    def cast(self):
        e, ty = self.unary()
        while self.at("as"):
            self.next()
            to = self.read_type("the target of `as`")
            if ty.kind == "bool" and to.kind == "nat":
                e, ty = "(1 if %s else 0)" % e, to
                continue
            if ty.kind != to.kind:
                self.fail("`as` from `%s` to `%s` is not lifted"
                          % (ty.frag(), to.frag()))
            if ty.width and ty.width > to.width:
                # Narrowing keeps the low bits, and Nat has none to keep.
                self.fail("a narrowing `as` (u%d to u%d) is not lifted"
                          % (ty.width, to.width))
            ty = to
        return e, ty

    def unary(self):
        t = self.cur
        if t.kind == "punc" and t.val == "!":
            self.next()
            e, ty = self.unary()
            if ty.kind != "bool":
                self.fail("`!` on an integer is bitwise, which is not lifted")
            return "(not %s)" % e, _BOOL
        if t.kind == "punc" and t.val == "-":
            self.fail("negation has no meaning on an unsigned value")
        if t.kind == "punc" and t.val in ("&", "*"):
            self.fail("references are not lifted yet")
        return self.postfix()

    def postfix(self):
        e, ty = self.primary()
        t = self.cur
        if t.val == ".":
            self.fail("fields and methods are not lifted yet")
        if t.val == "[":
            self.fail("indexing is not lifted yet")
        if t.val == "?":
            self.fail("`?` is not lifted")
        return e, ty

    def primary(self):
        t = self.cur
        if t.kind == "num":
            self.next()
            return str(_int_literal(t, self)), _LIT
        if t.kind == "kw" and t.val in ("true", "false"):
            self.next()
            return ("True" if t.val == "true" else "False"), _BOOL
        if t.val == "(":
            self.next()
            e, ty = self.binary(0)
            if self.at(","):
                self.fail("tuples are not lifted")
            self.expect(")")
            return e, ty
        if t.kind == "kw" and t.val in ("if", "match") or t.val == "{":
            return self.braced_value()
        if t.kind == "ident":
            self.next()
            if self.at("!"):
                self.fail("the macro `%s!` is not lifted" % t.val)
            if self.at("::"):
                self.fail("paths (`%s::..`) are not lifted" % t.val)
            if self.at("("):
                return self.call(t.val)
            found = self.lookup(t.val)
            if found is None:
                self.fail("`%s` is not a local or parameter of the function"
                          % t.val)
            return found
        if t.kind == "str" or t.kind == "chr":
            self.fail("string and character values are not lifted")
        self.fail("`%s` is not lifted" % t.val)

    def braced_value(self):
        """An `if`, `match` or block inside an expression: its value goes to a
        fresh variable, and the statements computing it come first."""
        ty = self._value_type_ahead()
        tmp = self.fresh("v")
        self.note_local(tmp, ty)
        self.emit("%s = %s" % (tmp, "False" if ty.kind == "bool" else "0"))
        self.value_into(tmp, ty)
        return tmp, ty

    def _value_type_ahead(self):
        """The type of a braced value, read off its first arm's tail."""
        save_i, save_lines = self.i, len(self.lines)
        save_used, save_callees = set(self.used), list(self.callees)
        save_indent, save_depth = self.indent, len(self.scopes)
        save_nested = len(self.nested_locals)
        try:
            self.value_into(_PROBE, None)
            ty = None
        except _Probed as found:
            ty = found.ty
        except LiftError:
            ty = None
        self.i = save_i
        del self.lines[save_lines:]
        del self.scopes[save_depth:]
        del self.nested_locals[save_nested:]
        self.indent = save_indent
        self.used, self.callees = save_used, save_callees
        if ty is None:
            self.fail("cannot tell the type of this value; bind it with "
                      "`let x: T = ..` first")
        return ty

    def call(self, fname):
        """A call to another function of the same source, lifted with it."""
        callee = self.unit.lift(fname) if fname in self.unit.fn_index \
            else None
        if callee is None:
            self.fail("`%s` is not a function in this source" % fname)
        self.expect("(")
        args = []
        while not self.at(")"):
            e, ty = self.binary(0)
            args.append((e, ty))
            if not self.accept(","):
                break
        self.expect(")")
        if len(args) != len(callee.params):
            self.fail("`%s` takes %d argument(s)" % (fname,
                                                     len(callee.params)))
        for (e, ty), (_n, want) in zip(args, callee.params):
            if ty.frag() != want:
                self.fail("argument of type `%s` where `%s` wants `%s`"
                          % (ty.frag(), fname, want))
        if callee not in self.callees:
            self.callees.append(callee)
        ret = _BOOL if callee.ret == "Bool" else _Ty("nat", 64)
        return "%s(%s)" % (fname, ", ".join(e for e, _ in args)), ret


def _strip_old(toks, mutable, lifter):
    """`old(e)` is `e`: a parameter already means its value at entry.  A
    `mut` parameter named outside `old` is refused -- read at the end or at
    entry, the clause would say two different things."""
    out, k = [], 0
    while k < len(toks):
        t = toks[k]
        if t.kind == "ident" and t.val == "old" and k + 1 < len(toks) \
                and toks[k + 1].val == "(":
            depth, j = 0, k + 1
            while j < len(toks):
                if toks[j].val == "(":
                    depth += 1
                elif toks[j].val == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            out.append(RustToken("punc", "(", t.line))
            out.extend(toks[k + 2:j])
            out.append(RustToken("punc", ")", t.line))
            k = j + 1
            continue
        if t.kind == "ident" and t.val in mutable:
            lifter.fail("the clause names `mut` parameter `%s` without "
                        "`old(..)`; at the end and at entry it may differ"
                        % t.val)
        out.append(t)
        k += 1
    return out


def _int_literal(tok, lifter):
    """The value of an integer literal, suffix and `_` separators dropped."""
    text = tok.val.replace("_", "")
    for suffix in sorted(_UNSIGNED, key=len, reverse=True):
        if text.endswith(suffix):
            text = text[:-len(suffix)]
            break
    for suffix in _SIGNED:
        if text.endswith(suffix):
            lifter.fail("the literal `%s` is signed" % tok.val)
    try:
        if text.startswith(("0x", "0X")):
            return int(text[2:], 16)
        if text.startswith(("0b", "0B")):
            return int(text[2:], 2)
        if text.startswith(("0o", "0O")):
            return int(text[2:], 8)
        return int(text)
    except ValueError:
        lifter.fail("`%s` is not an integer literal the lift reads"
                    % tok.val)


def _is_name(text):
    return text.replace("_", "a").isalnum() and not text[0].isdigit()


def _is_int(text):
    return text.isdigit()


def _wider(a, b):
    if a.kind != "nat" or b.kind != "nat":
        return a
    return a if a.width >= b.width else b
