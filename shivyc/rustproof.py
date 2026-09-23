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
    the lifted function is a theorem about that model of the arithmetic --
    and what closes the distance is the *safety* lift (`Lifted.safety`):
    every place the Rust would panic becomes a claim of its own, with the
    type's width still in hand.  `a + b` owes `a + b <= u32::MAX`, `a - b`
    owes `b <= a`, `a / b` owes `b != 0`, `xs[i]` owes `i < len(xs)`, and a
    call owes its callee's `#[requires]`.  Signed integers are refused.

  * Slices (`&[u64]`, `&Vec<u32>`) are the fragment's `Array`, and a struct
    declared in the file is a record.  A function taking one `&mut` struct
    and returning `()` lifts to one *returning* the struct -- the state
    threading `hoare.py` does for a syscall -- and in its `ensures` the
    parameter is the final value, `old(..)` the one it came in with.

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
_USIZE = _UNSIGNED["usize"]
_SIGNED = ("i8", "i16", "i32", "i64", "i128", "isize")

# Names a lifted variable must not take: Python's keywords, and the names
# the fragment gives a meaning of its own.
_RESERVED = {"and", "or", "not", "if", "elif", "else", "while", "for", "in",
             "is", "def", "return", "pass", "lambda", "class", "import",
             "from", "as", "with", "try", "except", "finally", "raise",
             "global", "nonlocal", "del", "assert", "yield", "await",
             "async", "break", "continue", "None", "True", "False",
             "result", "len", "range", "invariant", "variant", "_ok",
             "max_u8", "max_u16", "max_u32", "max_u64", "max_u128"}

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
        self.params = params            # [(name, fragment type name)]
        self.ret = ret                  # fragment type name
        self.callees = callees          # lifted functions it calls
        self.records = []               # [(struct, [(field, type name)])]
        self.pre_source = None          # `name__pre`, if it has `requires`
        self.pre_maxes = []             # the `max_uN` `name__pre` takes
        self.obligations = []           # labels, in the order they arise
        self.unit = None

    def safety(self, only=None):
        """The safety lift: `name__safe`, returning whether no panic is
        reachable -- every obligation, or just obligation `only`.  None if
        the function owes nothing."""
        if not self.obligations:
            return None
        lifter = _FnLifter(self.unit, self.name, True, only)
        return lifter.run().source

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


def _structs(toks):
    """`struct Name { field: T, .. }` at the top level: {name: [(field, _Ty)]}.
    A struct with a field the fragment has no type for is left out, and
    reported when a function uses it."""
    out, depth, k = {}, 0, 0
    while k < len(toks):
        t = toks[k]
        if t.kind == "punc" and t.val == "{":
            depth += 1
        elif t.kind == "punc" and t.val == "}":
            depth -= 1
        elif depth == 0 and t.val == "struct" and toks[k + 1].kind == "ident" \
                and toks[k + 2].val == "{":
            name, j, fields, ok = toks[k + 1].val, k + 3, [], True
            while toks[j].val != "}":
                if toks[j].val == "pub":
                    j += 1
                fname, ftype = toks[j].val, toks[j + 2].val
                if ftype in _UNSIGNED:
                    fields.append((fname, _Ty("nat", _UNSIGNED[ftype])))
                elif ftype == "bool":
                    fields.append((fname, _BOOL))
                else:
                    ok = False
                j += 3
                while toks[j].val not in (",", "}"):
                    j += 1
                if toks[j].val == ",":
                    j += 1
            if ok:
                out[name] = fields
            k = j
            continue
        k += 1
    return out


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
            if callee.pre_source is not None:
                out[callee.name + "__pre"] = (
                    [t for _, t in callee.params]
                    + ["Nat"] * len(callee.pre_maxes), "Bool")
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
        self.structs = _structs(self.toks)

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
        lifted.unit = self
        self.lifted_fns[name] = lifted
        return lifted


class _Ty:
    """A value's type in the lift: Nat with a width, Bool, an Array of Nat
    with its elements' width, or a record by name."""

    def __init__(self, kind, width=0, name=""):
        self.kind = kind                # "nat" | "bool" | "arr" | "rec"
        self.width = width              # bits, for a nat or an array's elements
        self.name = name                # the struct, for a record

    def frag(self):
        if self.kind == "nat":
            return "Nat"
        if self.kind == "bool":
            return "Bool"
        if self.kind == "arr":
            return "Array"
        return self.name


def _max(width):
    """The largest `uN`, *symbolically*.  An overflow obligation names
    `max_u32`, a parameter of the safety function, with each `u32`
    parameter and each element of a `&[u32]` assumed `<= max_u32`.

    This began as a workaround: the kernel's numerals were unary, and
    `u32::MAX` written out was four billion nested terms.  Numerals are one
    node now, and the symbolic maximum stays because it is the stronger
    statement: a proof that holds for every `max_u32` holds for 2^32 - 1,
    and a proof that needed the particular value would be arithmetic about
    the width, which no obligation here should depend on."""
    return "max_u%d" % width


_BOOL = _Ty("bool")
_LIT = _Ty("nat", 0)            # an integer literal: fits any width


class _FnLifter:
    """Lift one function.  A recursive descent over its tokens that writes
    fragment statements as it goes, as `crust.py` writes C."""

    def __init__(self, unit, name, safety=False, only=None):
        self.unit = unit
        self.toks = unit.toks
        self.name = name
        # The safety lift: the same control flow, with `_ok` conjoined with
        # each obligation where the Rust evaluates it, and every `return`
        # returning `_ok`.  `only` keeps a single obligation, so one that
        # fails can be named.
        self.safety = safety
        self.only = only
        self.cond_n = 0
        self.cond_labels = []
        self.pending_conds = []
        self.guards = []
        self.maxes = []                 # widths whose `max_uN` is named
        # `uN::MAX` as the symbol rather than the number: in the safety lift,
        # and in a clause lifted for it, so a `requires` guarding the body
        # speaks of the bound the obligations name.
        self.symbolic_max = safety
        # Widths whose `uN::MAX` a contract clause names in the model lift:
        # the model then states the Rust ranges for that width too, so a
        # clause like `n <= usize::MAX` is about values that are in range.
        self.clause_limits = []
        self.in_clause = False
        self.state = None               # (rust name, fragment, _Ty) of `&mut`
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
        if self.safety:
            if self.pending_conds and line not in ("else:", "pass"):
                self.flush()
            if line.startswith("return "):
                line = "return _ok"
        self.lines.append("    " * self.indent + line)

    def flush(self):
        """Conjoin the obligations evaluated so far onto `_ok`, here -- so
        each reads the variables as they are when the Rust evaluates it."""
        conds = self.pending_conds
        self.pending_conds = []
        self.lines.append("    " * self.indent + "_ok = (_ok and %s)"
                          % " and ".join(conds))

    def overflow(self, value, width, what):
        if self.safety and width not in self.maxes:
            self.maxes.append(width)
        self.side("(%s <= %s)" % (value, _max(width)),
                  "%s may overflow u%d" % (what, width))

    def side(self, cond, label):
        """Record an obligation: `cond` must hold where it is evaluated.  A
        guard in force (the right of `&&`, a match arm) conditions it."""
        index = self.cond_n
        self.cond_n += 1
        self.cond_labels.append("%s (line %d)" % (label, self.cur.line))
        if not self.safety or (self.only is not None and index != self.only):
            return
        # As a conditional rather than `(not g) or c`: the kernel's
        # `by_every_bool` splits on an `if`, and the guard is then decided
        # in the branch that needs it.
        for g in reversed(self.guards):
            cond = "(%s if %s else True)" % (cond, g)
        self.pending_conds.append(cond)

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
            ty, by_mut = self.read_param_type("parameter `%s`" % pname)
            frag = self.bind(pname, ty)
            if by_mut:
                if self.state is not None:
                    self.fail("only one `&mut` parameter is lifted")
                self.state = (pname, frag, ty)
            params.append((frag, ty))
            if not self.accept(","):
                break
        self.expect(")")
        if self.state is not None:
            if self.accept("->"):
                self.fail("a function with a `&mut` parameter lifts to one "
                          "returning the updated struct, so it cannot also "
                          "return a value")
            ret = self.state[2]
        elif not self.accept("->"):
            self.fail("a function returning `()` has nothing to claim a "
                      "postcondition about")
        else:
            ret = self.read_type("the return type")
        if self.at("where"):
            self.fail("`where` clauses are not lifted")

        # Preconditions first: the fragment reads leading `assert`s as the
        # precondition, which is what `#[requires]` is.
        requires = []
        # `name__pre`, which a caller's safety lift calls, reads each clause
        # again with `uN::MAX` as the symbol, taken as a parameter: the caller
        # passes its own `max_uN`, so a `requires(tid < usize::MAX)` means the
        # bound the caller's range facts are stated against.
        pre_requires, pre_maxes = [], []
        for kind, toks, line in clauses:
            if kind == "requires":
                requires.append(self.clause(toks, line, None))
                if not self.safety:
                    self.emit("assert %s" % requires[-1])
                    saved = self.symbolic_max, self.maxes
                    self.symbolic_max, self.maxes = True, pre_maxes
                    pre_requires.append(self.clause(toks, line, None))
                    self.symbolic_max, self.maxes = saved
        range_at = len(self.lines)
        if self.safety:
            self.emit("_ok = True")
            # In the safety lift a precondition is a guard around the body,
            # not a hypothesis: `safe(args)` for every `args` is then exactly
            # "if `requires` holds, nothing panics", and the kernel's split
            # decides the precondition like any other guard -- in the
            # spelling the obligations use, since it is the same text.
            for req in requires:
                self.emit("if %s:" % req)
                self.indent += 1
        ensures = []
        for kind, toks, line in clauses:
            if kind == "ensures":
                ensures.append(self.clause(toks, line, ret, mutable))

        self.expect("{")
        n_asserts = len(self.lines)
        self.block_body("ret" if self.state is None else None, ret)
        if self.state is not None:
            self.emit("return %s" % self.state[1])
        if self.safety:
            self.indent -= len(requires)
            self.emit("return _ok")
        # A fresh name is never reused, so a starting value cannot be read by
        # anything but the code that then assigns it.
        inits = ["    %s = %s" % (n, "False" if t.kind == "bool" else "0")
                 for n, t in self.nested_locals]
        self.lines[n_asserts:n_asserts] = inits
        extra = []
        if self.safety and self.maxes:
            # A value of a Rust integer type is inside its range: a fact the
            # obligations may use, stated against the symbolic maximum.
            ranges = []
            for frag, ty in params:
                if ty.kind == "nat" and ty.width in self.maxes:
                    ranges.append("    assert (%s <= %s)"
                                  % (frag, _max(ty.width)))
                if ty.kind == "arr" and ty.width in self.maxes:
                    # and so is every element of a slice of them: without
                    # this, `xs[i] + 1` could not be bounded even behind a
                    # guard that compares it with another element.
                    ranges.append("    assert (all_le(%s, %s))"
                                  % (frag, _max(ty.width)))
                if ty.kind == "arr" and _USIZE in self.maxes:
                    # and a slice's length is a `usize`: what bounds a
                    # counter `i + 1` behind `i < xs.len()`
                    ranges.append("    assert (len(%s) <= %s)"
                                  % (frag, _max(_USIZE)))
            self.lines[range_at:range_at] = ranges
            extra = [(_max(w), _Ty("nat", w)) for w in sorted(self.maxes)]
        elif not self.safety and self.clause_limits:
            # A clause named `uN::MAX`: the model states the same ranges the
            # safety lift does, against the number.  They are hypotheses of
            # every theorem about this function, and true of every Rust call.
            lit = lambda w: str((1 << w) - 1)
            ranges = []
            for frag, ty in params:
                if ty.kind == "nat" and ty.width in self.clause_limits:
                    ranges.append("    assert (%s <= %s)" % (frag,
                                                            lit(ty.width)))
                if ty.kind == "arr" and ty.width in self.clause_limits:
                    ranges.append("    assert (all_le(%s, %s))"
                                  % (frag, lit(ty.width)))
                if ty.kind == "arr" and _USIZE in self.clause_limits:
                    ranges.append("    assert (len(%s) <= %s)"
                                  % (frag, lit(_USIZE)))
            self.lines[range_at:range_at] = ranges
        name = self.name + ("__safe" if self.safety else "")
        head = "def %s(%s) -> '%s':" % (
            name, ", ".join("%s: '%s'" % (n, t.frag())
                            for n, t in params + extra),
            "Bool" if self.safety else ret.frag())
        source = "\n".join([head] + self.lines) + "\n"
        out = Lifted(self.name, source, ensures,
                     [(n, t.frag()) for n, t in params], ret.frag(),
                     self.callees)
        out.obligations = self.cond_labels
        used = []
        for _n, t in params + [(None, ret)]:
            if t.kind == "rec" and t.name not in used:
                used.append(t.name)
        out.records = [(r, [(f, ft.frag()) for f, ft in
                            self.unit.structs[r]]) for r in used]
        if requires and not self.safety:
            out.pre_maxes = list(pre_maxes)
            out.pre_source = "def %s__pre(%s) -> 'Bool':\n    return %s\n" % (
                self.name, ", ".join(
                    ["%s: '%s'" % (n, t.frag()) for n, t in params] +
                    ["%s: 'Nat'" % _max(w) for w in pre_maxes]),
                " and ".join(pre_requires))
        return out

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

    def read_param_type(self, where):
        """A parameter's type: (_Ty, is it `&mut`).  A slice, a `&Vec`, a
        struct by value or by reference, or a scalar."""
        if not self.accept("&"):
            return self.read_type(where), False
        by_mut = self.accept("mut") is not None
        if self.accept("["):
            elem = self.read_type(where)
            self.expect("]")
            if elem.kind != "nat":
                self.fail("%s is a slice of `%s`; only slices of unsigned "
                          "integers are lifted" % (where, elem.frag()))
            if by_mut:
                self.fail("%s is `&mut [..]`; writing through a slice is "
                          "not lifted" % where)
            return _Ty("arr", elem.width), False
        if self.at("Vec"):
            self.next()
            self.expect("<")
            elem = self.read_type(where)
            self.expect(">")
            if elem.kind != "nat" or by_mut:
                self.fail("%s: only `&Vec<uN>` is lifted" % where)
            return _Ty("arr", elem.width), False
        ty = self.read_type(where)
        if ty.kind != "rec":
            self.fail("%s is a reference to `%s`; only structs and slices "
                      "are lifted by reference" % (where, ty.frag()))
        return ty, by_mut

    def read_type(self, where):
        t = self.cur
        if t.kind == "ident" and t.val in self.unit.structs:
            self.next()
            return _Ty("rec", 0, t.val)
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
        sub.symbolic_max, sub.maxes = self.symbolic_max, self.maxes
        sub.clause_limits, sub.in_clause = self.clause_limits, True
        sub.indent = self.indent
        state = self.state if ret is not None else None
        sub.toks = list(_strip_old(toks, mutable, self, state)) + \
            [RustToken("eof", "", line)]
        sub.i = 0
        merged = {}
        for scope in self.scopes:
            merged.update(scope)
        sub.scopes = [merged]
        if ret is not None:
            sub.scopes[0]["result"] = ("result", ret)
        if state is not None:
            # In an `ensures`, the `&mut` parameter is its final value -- the
            # result -- and `old(..)` of it is the value it came in with.
            sub.scopes[0][state[0]] = ("result", state[2])
            sub.scopes[0]["__old_state"] = (state[1], state[2])
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
            if (self.at(";") or self.at("}")) and self.state is not None:
                self.accept(";")
                self.emit("return %s" % self.state[1])
                return False
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
        if t.kind == "ident" and self.peek().val == "." \
                and self.peek(2).kind == "ident" \
                and self.peek(3).val in ("=", "+=", "-=", "*=", "/=", "%="):
            self.field_assign_stmt()
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
        value, _ty = self.combine(op[:-1], found[0], found[1], e, ty)
        self.emit("%s = %s" % (found[0], value))

    def field_assign_stmt(self):
        """`s.f = e` / `s.f += e` on the `&mut` struct: a functional update
        of the record the lifted function returns."""
        name = self.next().val
        self.expect(".")
        field = self.next().val
        if self.state is None or name != self.state[0]:
            self.fail("assignment to a field of `%s`; only the `&mut` "
                      "parameter's fields are assigned" % name)
        fty = self._field(self.state[2], field)
        op = self.next().val
        e, ty = self.expr()
        self.expect(";")
        self._check_ty(fty, ty)
        target = "%s.%s" % (self.state[1], field)
        if op != "=":
            e, _ty = self.combine(op[:-1], target, fty, e, ty)
        self.emit("%s = %s" % (target, e))

    def _field(self, rty, field):
        for f, fty in self.unit.structs[rty.name]:
            if f == field:
                return fty
        self.fail("`%s` has no field `%s`" % (rty.name, field))

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
        head_conds = list(self.pending_conds)
        self.emit("while %s:" % cond)
        self.indent += 1
        for kind, toks, line in attrs:
            self.emit("assert %s(%s)" % (kind, self.clause(toks, line, None)))
        self.expect("{")
        self.block_body(None, None)
        if head_conds:
            # The condition is evaluated again before the next pass.
            self.pending_conds = head_conds
            self.flush()
        self.indent -= 1

    def for_stmt(self):
        self.expect("for")
        if self.cur.kind != "ident":
            self.fail("the loop variable must be a plain name")
        var = self.next().val
        self.expect("in")
        if self.cur.kind == "ident" and self.lookup(self.cur.val) is not None \
                and self.lookup(self.cur.val)[1].kind == "arr" \
                or self.at("&"):
            self.for_each(var)
            return
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

    def for_each(self, var):
        """`for x in xs` / `xs.iter()` / `&xs` over a slice: the fold over
        `range(len(xs))`, reading `xs[k]` -- in bounds by construction, so
        it owes nothing."""
        self.accept("&")
        name = self.next().val
        found = self.lookup(name)
        if found is None or found[1].kind != "arr":
            self.fail("`%s` is not a slice" % name)
        if self.accept("."):
            if not self.accept("iter"):
                self.fail("only `.iter()` is lifted on a slice in a `for`")
            self.expect("(")
            self.expect(")")
        k = self.fresh("k")
        self.emit("for %s in range(len(%s)):" % (k, found[0]))
        self.indent += 1
        self.scopes.append({})
        x = self.bind(var, _Ty("nat", found[1].width))
        self.emit("%s = %s[%s]" % (x, found[0], k))
        self.expect("{")
        self.block_body(None, None)
        self.scopes.pop()
        self.indent -= 1

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
        if self._splittable_conjunction():
            self._nested_if(mode, want)
            return
        cond, ty = self.expr(no_struct=True)
        self._check_ty(_BOOL, ty)
        self.emit("if %s:" % cond)
        self._arm_block(mode, want)
        nested = 0
        while self.accept("else"):
            if self.accept("if"):
                cond, ty = self.expr(no_struct=True)
                self._check_ty(_BOOL, ty)
                if self.pending_conds:
                    # The condition owes something, which can only be paid
                    # where it is evaluated: inside the `else`, before an
                    # `if` -- there is no statement between `elif`s.
                    self.emit("else:")
                    self.indent += 1
                    nested += 1
                    self.emit("if %s:" % cond)
                else:
                    self.emit("elif %s:" % cond)
                self._arm_block(mode, want)
                continue
            if mode == "ret":
                # Every branch above returns, so the `else` is what falls
                # through -- and the fragment wants a body that ends in a
                # `return`, not one whose last statement is an `if`.
                self.expect("{")
                self.block_body(mode, want)
                self.indent -= nested
                return
            self.emit("else:")
            self._arm_block(mode, want)
            self.indent -= nested
            return
        self.indent -= nested
        if mode is not None:
            self.fail("an `if` used as a value needs an `else`")

    def _splittable_conjunction(self):
        """Is the condition at the cursor `a && b && ..`, with no `||` at the
        top level, on an `if` with no `else`?  Then it is the same program as
        nested `if`s -- and nested, each conjunct is a guard of its own that
        the kernel's split decides, where `a && b` true decides neither."""
        depth, j, conj = 0, self.i, False
        while self.toks[j].kind != "eof":
            v = self.toks[j].val
            if v in ("(", "["):
                depth += 1
            elif v in (")", "]"):
                depth -= 1
            elif depth == 0 and v == "||":
                return False
            elif depth == 0 and v == "&&":
                conj = True
            elif depth == 0 and v == "{":
                break
            j += 1
        if not conj:
            return False
        depth = 0
        while self.toks[j].kind != "eof":                  # the body
            v = self.toks[j].val
            if v == "{":
                depth += 1
            elif v == "}":
                depth -= 1
                if depth == 0:
                    return self.toks[j + 1].val != "else"
            j += 1
        return False

    def _nested_if(self, mode, want):
        levels = 0
        while True:
            cond, ty = self.binary(2)               # one conjunct
            self._check_ty(_BOOL, ty)
            self.emit("if %s:" % cond)
            self.indent += 1
            levels += 1
            if not self.accept("&&"):
                break
        self.expect("{")
        before = len(self.lines)
        self.block_body(mode, want)
        if len(self.lines) == before:
            self.emit("pass")
        self.indent -= levels
        if mode is not None and mode != "ret":
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
        before = []                     # the tests of the arms above
        while not self.at("}"):
            if self.cur.kind == "eof":
                self.fail("unterminated `match`")
            cond, binds = self.pattern(s, sty)
            guard = None
            self.scopes.append({})
            for bname in binds:
                self.alias(bname, s, sty)
            if self.accept("if"):
                # A guard is evaluated only when its pattern matched and no
                # arm above did; what it owes is conditioned on exactly that.
                path = cond
                for prior in before:
                    path = "((not %s) and %s)" % (prior, path)
                self.guards.append(path)
                try:
                    guard, gty = self.expr()
                finally:
                    del self.guards[-1]
                self._check_ty(_BOOL, gty)
            before.append(cond if guard is None else
                          "(%s and %s)" % (cond, guard))
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
            if op in ("&&", "||"):
                # The right side is evaluated only if the left did not settle
                # it, and what it owes is owed only then.
                self.guards.append(left if op == "&&" else "(not %s)" % left)
                try:
                    right, rty = self.binary(level + 1)
                finally:
                    del self.guards[-1]
            else:
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
            if lty.width and int(right) >= lty.width:
                self.fail("a shift by %s on a u%d always panics"
                          % (right, lty.width))
            # On an unsigned value `>> k` is division by 2**k exactly, with
            # no truncation and no wrap; `<< k` is multiplication, exact
            # until it overflows -- the same claim as `*`.
            if op == ">>":
                return "(%s // %d)" % (left, k), lty
            out = "(%s * %d)" % (left, k)
            if lty.width:
                self.overflow(out, lty.width, "`<<`")
            return out, lty
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
        out = "(%s %s %s)" % (left, py, right)
        ty = _wider(lty, rty)
        if op in ("+", "*") and ty.width:
            self.overflow(out, ty.width, "`%s`" % op)
        elif op == "-":
            self.side(_either(["(%s <= %s)" % (right, left),
                               "(%s < %s)" % (right, left)],
                              "(not (%s < %s))" % (left, right)),
                      "`-` may underflow")
        elif op in ("/", "%"):
            self.side(_either(["(0 < %s)" % right],
                              "(not (%s == 0))" % right),
                      "`%s` by zero" % op)
        return out, ty

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
            # Borrowing or dereferencing a slice or a struct changes nothing
            # the model sees; a reference to a scalar is not lifted.
            self.next()
            e, ty = self.unary()
            if ty.kind not in ("arr", "rec"):
                self.fail("references to `%s` are not lifted" % ty.frag())
            return e, ty
        return self.postfix()

    def postfix(self):
        e, ty = self.primary()
        while True:
            t = self.cur
            if t.val == "." and ty.kind == "rec":
                self.next()
                field = self.next().val
                if self.at("("):
                    self.fail("methods are not lifted")
                e, ty = "%s.%s" % (e, field), self._field(ty, field)
                continue
            if t.val == "." and ty.kind == "arr":
                self.next()
                method = self.next().val
                self.expect("(")
                self.expect(")")
                if method == "len":
                    e, ty = "len(%s)" % e, _Ty("nat", 64)
                elif method == "is_empty":
                    e, ty = "(len(%s) == 0)" % e, _BOOL
                else:
                    self.fail("`.%s()` on a slice is not lifted" % method)
                continue
            if t.val == "[" and ty.kind == "arr":
                self.next()
                i, ity = self.binary(0)
                self.expect("]")
                self._check_ty(_LIT, ity)
                self.side(_either(["(%s < len(%s))" % (i, e)],
                                  "(not (len(%s) <= %s))" % (e, i)),
                          "index out of bounds")
                e, ty = "%s[%s]" % (e, i), _Ty("nat", ty.width)
                continue
            if t.val == ".":
                self.fail("methods on `%s` are not lifted" % ty.frag())
            if t.val == "[":
                self.fail("indexing `%s` is not lifted" % ty.frag())
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
            if self.at("::") and t.val in _UNSIGNED:
                return self.int_limit(t.val)
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

    def int_limit(self, prim):
        """`u64::MAX` or `u64::MIN`.  The safety lift writes the maximum as
        `max_u64`, the same symbol every range fact is stated against, so
        `x <= u64::MAX - y` in the source is a fact about the bound the
        obligations name; the model writes the number, which is one node.
        Both are the same statement: the safety theorem is proved for every
        `max_u64`, the true one included."""
        self.expect("::")
        which = self.cur.val
        self.next()
        width = _UNSIGNED[prim]
        if which == "MIN":
            return "0", _Ty("nat", width)
        if which != "MAX":
            self.fail("`%s::%s` is not lifted" % (prim, which))
        if not self.symbolic_max:
            if self.in_clause and width not in self.clause_limits:
                self.clause_limits.append(width)
            return str((1 << width) - 1), _Ty("nat", width)
        if width not in self.maxes:
            self.maxes.append(width)
        return _max(width), _Ty("nat", width)

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
        save_safety = (self.cond_n, list(self.cond_labels),
                       list(self.pending_conds), list(self.guards))
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
        self.cond_n, self.cond_labels, self.pending_conds, self.guards = \
            save_safety
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
        text = ", ".join(e for e, _ in args)
        if callee.pre_source is not None:
            # A call owes its callee's `#[requires]`, at the caller's own
            # `max_uN` for each limit the clause names.
            limits = []
            for w in callee.pre_maxes:
                if self.safety and w not in self.maxes:
                    self.maxes.append(w)
                limits.append(_max(w))
            self.side("%s__pre(%s)" % (fname, ", ".join(
                          ([text] if text else []) + limits)),
                      "`%s`'s `#[requires]`" % fname)
        if callee.ret == "Bool":
            ret = _BOOL
        elif callee.ret == "Nat":
            ret = _Ty("nat", 64)
        else:
            ret = _Ty("rec", 0, callee.ret)
        return "%s(%s)" % (fname, text), ret


def _strip_old(toks, mutable, lifter, state=None):
    """`old(e)` is `e`: a parameter already means its value at entry.  A
    `mut` parameter named outside `old` is refused -- read at the end or at
    entry, the clause would say two different things.  With a `&mut`
    parameter, its name inside `old(..)` becomes the entry value."""
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
            for inner in toks[k + 2:j]:
                if state is not None and inner.kind == "ident" \
                        and inner.val == state[0]:
                    inner = RustToken("ident", "__old_state", inner.line)
                out.append(inner)
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


def _either(sufficient, exact):
    """An obligation as a chain: `True` if any sufficient spelling holds,
    else the exact one.  Over Nat they all say the same thing, but as
    booleans they are different terms, and the kernel's split-then-compute
    decides only the spelling a guard used -- `i >= len` early-returned
    decides `len <= i`, not `i < len`.  Each premise implies the claim, so
    the chain is the claim; it just lets the proof find the guard."""
    out = exact
    for cond in reversed(sufficient):
        out = "(True if %s else %s)" % (cond, out)
    return out


def _is_name(text):
    return text.replace("_", "a").isalnum() and not text[0].isdigit()


def _is_int(text):
    return text.isdigit()


def _wider(a, b):
    if a.kind != "nat" or b.kind != "nat":
        return a
    return a if a.width >= b.width else b
