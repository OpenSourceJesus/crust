#!/usr/bin/env python3
r"""ocamlproof.py -- an OCaml definition as a proof, by Curry-Howard.

A well-typed OCaml term of type T is, on a fragment of the language, a proof
of the proposition T:

  'a, 'b            propositions
  t1 -> t2          implication
  t1 * t2           conjunction (`Prod`, `mk`, `fst`, `snd`)
  type ('a,'b) t =  a disjunction: an inductive type of its own, one
    | L of 'a       constructor per case; `match` on it is its recursor
    | R of 'b
  type void = |     falsehood: no constructors; a refutation arm `_ -> .`
                    is its recursor with no cases -- ex falso

and a polymorphic definition proves its statement for every proposition
substituted for its type variables.  This file lowers each top-level
definition of an `.ml` file (parsed and typed by `ocaml.py`) into a term of
RosettaMath's kernel, `lean4.py`, with its type as the statement, and has
the kernel check it; then it exports every theorem to Lean 4, which checks
it again.  Nothing here is trusted: a lowering mistake is a kernel refusal.

OCaml is not a consistent logic, so the fragment matters as much as the
lowering.  Refused, each with its reason:

  let rec           `let rec f x = f x` has type 'a -> 'b: general recursion
                    inhabits every type, so it proves nothing
  exceptions        refused by the front end already; `raise` would too
  int, bool, if,    values, not propositions: a proof of `int` says nothing
  literals, lists
  `when` guards,    a case split the recursor cannot express as written
  nested patterns

and annotations are rigid (`ocaml.check_annotations`): the statement proved
is the one written, never a special case the inference narrowed it to.

    python3 ocamlproof.py FILE.ml       # check each definition; run Lean
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ocaml as O                                             # noqa: E402


def _rosettamath():
    import rustprove
    path = rustprove.rosettamath()
    if path is None:
        raise RuntimeError("RosettaMath not found beside crust; run "
                           "'make install_proofs'")
    if path not in sys.path:
        sys.path.insert(0, path)
    import lean4
    import hoare
    return lean4, hoare


class ProofError(Exception):
    def __init__(self, message, line=None):
        self.line = line
        super().__init__("line %d: %s" % (line, message) if line else message)


class Lowering:
    """Typed OCaml AST -> kernel terms, in one environment."""

    def __init__(self, checker):
        self.L, self.H = _rosettamath()
        self.c = checker
        self.env = self.H.prelude()
        self.T = self.L.Universe(1)             # Type
        self.theorems = []                      # (name, statement)
        self.counter = 0

    # -- names --
    def fresh(self, base='_x'):
        self.counter += 1
        return '%s%d' % (base, self.counter)

    @staticmethod
    def type_name(v):
        """A type variable's kernel name: 'a -> A.  Capitalised, so that no
        OCaml term variable -- always lower case -- can capture it."""
        base = (v.rigid or "'t%d" % v.id).lstrip("'")
        return base[:1].upper() + base[1:]

    # -- types --
    def declare(self, td):
        L, H = self.L, self.H
        if td.alias is not None:
            return                              # expanded by the checker
        if td.name in self.env:
            raise ProofError("type `%s` would shadow `%s` of the kernel's "
                             "prelude" % (td.name, td.name), td.line)
        params = [(self.type_name_of(p), self.T) for p in td.type_vars]
        sub = {p: L.Var(self.type_name_of(p)) for p in td.type_vars}
        ctors = []
        for v in td.variants:
            args = []
            for t in v.of_types:
                if isinstance(t, O.TypeApp) and t.name == td.name:
                    if [getattr(a, 'name', None) for a in t.args] != \
                            td.type_vars:
                        raise ProofError("`%s`: a recursive occurrence must "
                                         "repeat the parameters as declared"
                                         % v.name, v.line)
                    args.append(L.REC)
                else:
                    args.append(self.written(t, sub, v.line))
            ctors.append(('%s.%s' % (td.name, v.name), args))
        L.inductive(self.env, td.name, ctors, params=params)

    @staticmethod
    def type_name_of(p):
        p = p.lstrip("'")
        return p[:1].upper() + p[1:]

    def written(self, t, sub, line):
        """A type as written in a declaration, as a kernel type."""
        L, H = self.L, self.H
        if isinstance(t, O.TypeVar):
            return sub[t.name]
        if isinstance(t, O.TypeArrow):
            return H.arrow(self.written(t.left, sub, line),
                           self.written(t.right, sub, line))
        if isinstance(t, O.TypeTuple):
            return self.prod([self.written(x, sub, line) for x in t.types])
        if isinstance(t, O.TypeApp):
            if t.name in self.c.aliases:
                params, body = self.c.aliases[t.name]
                inner = dict(sub)
                for p, a in zip(params, t.args):
                    inner[p] = self.written(a, sub, line)
                return self.written(body, inner, line)
            self.proposition_type(t.name, line)
            return H.app(t.name, *[self.written(a, sub, line)
                                   for a in t.args])
        raise ProofError("not a type", line)

    def proposition_type(self, name, line):
        if name in ('int', 'bool', 'list', 'unit'):
            raise ProofError("`%s` is a type of values, not a proposition; "
                             "it is not in the proof fragment" % name, line)

    def prod(self, types):
        if len(types) == 1:
            return types[0]
        return self.H.app('Prod', types[0], self.prod(types[1:]))

    def ty(self, t, line):
        """An inferred type as a kernel type."""
        H, L = self.H, self.L
        t = O.prune(t)
        if isinstance(t, O.TVar):
            return L.Var(self.type_name(t))
        if t.name == '->':
            return H.arrow(self.ty(t.args[0], line), self.ty(t.args[1], line))
        if t.name == '*':
            return self.prod([self.ty(a, line) for a in t.args])
        self.proposition_type(t.name, line)
        return H.app(t.name, *[self.ty(a, line) for a in t.args])

    # -- terms --
    def term(self, e):
        H, L = self.H, self.L
        if isinstance(e, O.Variable):
            if e.name in self.globals and getattr(e, 'inst', None):
                return H.app(e.name, *[self.ty(a, e.line) for a in e.inst])
            return L.Var(e.name)
        if isinstance(e, O.Application):
            return H.app(self.term(e.func), *[self.term(a) for a in e.args])
        if isinstance(e, O.Fun):
            return self.lam(e.params, e.body)
        if isinstance(e, O.TupleNode):
            return self.tuple(e.elements, [a.ty for a in e.elements], e.line)
        if isinstance(e, O.ConstructorApp):
            tname = self.c.ctors[e.name][0]
            self.proposition_type(tname, e.line)
            return H.app('%s.%s' % (tname, e.name),
                         *([self.ty(a, e.line) for a in e.inst]
                           + [self.term(a) for a in e.args]))
        if isinstance(e, O.Annot):
            return self.term(e.expr)
        if isinstance(e, O.LetIn):
            if e.group.rec:
                raise ProofError(self.REC_WHY, e.line)
            body = self.term(e.body)
            for d in reversed(e.group.defs):
                value = self.lam(d.params, d.value) if d.params \
                    else self.term(d.value)
                body = self.bind(d.pattern, value, d.ty, body, d.line)
            return body
        if isinstance(e, O.MatchWith):
            return self.match(e)
        if isinstance(e, O.Const):
            raise ProofError("a literal is a value, not a proof", e.line)
        if isinstance(e, O.IfThen):
            raise ProofError("`if` decides a bool, which is not a "
                             "proposition here; match on a variant instead",
                             e.line)
        if isinstance(e, O.BinOp):
            raise ProofError("`%s` computes a value, not a proof" % e.op,
                             e.line)
        if isinstance(e, O.Refute):
            raise ProofError("`.` outside a match arm", e.line)
        raise ProofError("not in the proof fragment: %r" % (e,), e.line)

    REC_WHY = ("`let rec` is not a proof: `let rec f x = f x` has type "
               "'a -> 'b, so general recursion would prove anything")

    def lam(self, params, body):
        """`fun p1 .. pn -> body`, each pattern bound by name."""
        L = self.L
        out = self.term(body)
        for p, _ in reversed(params):
            ty = self.ty(p.ty, p.line)
            if isinstance(p, O.Variable):
                out = L.Lambda(p.name, ty, out)
            else:
                name = self.fresh('_p')
                out = L.Lambda(name, ty, self.bind(p, L.Var(name), p.ty, out,
                                                   p.line))
        return out

    def tuple(self, elements, types, line):
        H = self.H
        if len(elements) == 1:
            return self.term(elements[0])
        return H.app('mk', self.ty(types[0], line),
                     self.prod([self.ty(t, line) for t in types[1:]]),
                     self.term(elements[0]),
                     self.tuple(elements[1:], types[1:], line))

    def bind(self, pattern, value, ty, body, line):
        """`let pattern = value in body`, as beta-redexes: a variable is a
        lambda applied to the value, a tuple its projections."""
        L, H = self.L, self.H
        if isinstance(pattern, O.Variable):
            return H.app(L.Lambda(pattern.name, self.ty(ty, line), body),
                         value)
        if isinstance(pattern, O.Wildcard):
            return body
        if isinstance(pattern, O.TupleNode):
            parts = O.prune(ty).args
            rest_ty = parts
            here = value
            for k, sub in enumerate(pattern.elements):
                if k == len(parts) - 1:
                    body = self.bind(sub, here, parts[k], body, line)
                    break
                a = self.ty(parts[k], line)
                b = self.prod([self.ty(x, line) for x in parts[k + 1:]])
                body = self.bind(sub, H.app('fst', a, b, here), parts[k],
                                 body, line)
                here = H.app('snd', a, b, here)
            del rest_ty
            return body
        raise ProofError("this pattern is not in the proof fragment; bind "
                         "names, `_` or tuples of them", line)

    def match(self, e):
        """`match s with ..` on a variant: its recursor, one case per
        constructor, each taken from the first arm that covers it."""
        L, H = self.L, self.H
        st = O.prune(e.match_expr.ty)
        scrut = self.term(e.match_expr)
        result = self.ty(e.ty, e.line)
        if isinstance(st, O.TCon) and st.name == '*':
            arms = [br for br in e.branches]
            br = arms[0]
            if br.guard is not None or len(arms) != 1:
                raise ProofError("a match on a tuple must be one arm that "
                                 "names its parts", e.line)
            return self.bind(br.pattern, scrut, st, self.arm_body(br, e),
                             br.line)
        if not (isinstance(st, O.TCon) and st.name in self.c.typedefs):
            raise ProofError("a match on %s is not in the proof fragment"
                             % O.show(st), e.line)
        td = self.c.typedefs[st.name]
        targs = [self.ty(a, e.line) for a in st.args]
        motive = L.Lambda('_m', H.app(td.name, *targs), result)
        minors = []
        for v in td.variants:
            br = self.arm_for(e, v.name)
            arg_types = [self.c.at_type_of(st, v.name, i)
                         for i in range(len(v.of_types))]
            rec_flags = [isinstance(t, O.TypeApp) and t.name == td.name
                         for t in v.of_types]
            names, body = self.case_body(br, e, v, arg_types, st)
            minor = body
            # recursive arguments bring an induction hypothesis, unused
            for k in reversed(range(len(names))):
                if rec_flags[k]:
                    minor = L.Lambda(self.fresh('_ih'), result, minor)
            for k in reversed(range(len(names))):
                minor = L.Lambda(names[k], self.ty(arg_types[k], e.line),
                                 minor)
            minors.append(minor)
        return H.app('%s.rec' % td.name, *(targs + [motive] + minors
                                           + [scrut]))

    def arm_for(self, e, cname):
        for br in e.branches:
            if br.guard is not None:
                raise ProofError("`when` guards are not in the proof "
                                 "fragment", br.line)
            p = br.pattern
            if isinstance(p, (O.Variable, O.Wildcard)):
                return br
            if isinstance(p, O.ConstructorPat) and p.name == cname:
                return br
        raise ProofError("no arm for `%s`" % cname, e.line)

    def case_body(self, br, e, v, arg_types, st):
        """(argument names, body) of the case for constructor `v`."""
        L, H = self.L, self.H
        p = br.pattern
        names = [self.fresh('_a') for _ in arg_types]
        if isinstance(p, O.ConstructorPat):
            body = self.arm_body(br, e, dict(zip(
                range(len(names)), names)), p, arg_types)
            for k, sub in enumerate(p.args):
                if isinstance(sub, O.Variable):
                    names[k] = sub.name
                elif isinstance(sub, O.Wildcard):
                    pass
                elif isinstance(sub, O.TupleNode):
                    body = self.bind(sub, L.Var(names[k]), arg_types[k],
                                     body, br.line)
                else:
                    raise ProofError("nested constructor patterns are not in "
                                     "the proof fragment yet", br.line)
            return names, body
        # a catch-all arm: the scrutinee, rebuilt from the case's arguments
        body = self.arm_body(br, e, dict(zip(range(len(names)), names)),
                             None, arg_types)
        if isinstance(p, O.Variable):
            td = self.c.typedefs[O.prune(st).name]
            rebuilt = H.app('%s.%s' % (td.name, v.name),
                            *([self.ty(a, br.line) for a in O.prune(st).args]
                              + [L.Var(n) for n in names]))
            body = H.app(L.Lambda(p.name, self.ty(st, br.line), body),
                         rebuilt)
        return names, body

    def arm_body(self, br, e, names=None, pat=None, arg_types=None):
        if not isinstance(br.body, O.Refute):
            return self.term(br.body)
        # `.`: some bound value is of a type with no constructors
        result = self.ty(e.ty, br.line)
        candidates = []
        if pat is not None:
            for k, sub in enumerate(pat.args):
                nm = sub.name if isinstance(sub, O.Variable) else names[k]
                candidates.append((nm, arg_types[k]))
        else:
            candidates.append((None, e.match_expr.ty))
        for nm, t in candidates:
            t = O.prune(t)
            if isinstance(t, O.TCon) and t.name in self.c.typedefs and \
                    not self.c.typedefs[t.name].variants:
                target = self.term(e.match_expr) if nm is None \
                    else self.L.Var(nm)
                motive = self.L.Lambda('_m', self.H.app(t.name), result)
                return self.H.app('%s.rec' % t.name, motive, target)
        raise ProofError("`.` here refutes a value this fragment cannot "
                         "name; match on the empty value directly", br.line)

    # -- definitions --
    def define(self, name, scheme, d):
        L = self.L
        if d.params:
            value = self.lam(d.params, d.value)
        else:
            value = self.term(d.value)
        stmt = self.ty(scheme.body, d.line)
        for v in reversed(scheme.quantified):
            n = self.type_name(v)
            stmt = L.Pi(n, self.T, stmt)
            value = L.Lambda(n, self.T, value)
        try:
            L.define(self.env, name, stmt, value)
        except L.KernelError as exc:
            raise ProofError("the kernel refused `%s`: %s" % (name, exc),
                             d.line)
        self.theorems.append((name, stmt))


def prove(code):
    """Check every definition in `code` as a proof.  A `Lowering` holding
    the environment and the theorems, or an error naming the first refusal
    and its line."""
    items, c = O.check(code, strict=True)
    low = Lowering(c)
    low.globals = set()
    for item in items:
        if isinstance(item, O.TypeDef):
            low.declare(item)
            continue
        if item.rec:
            raise ProofError(Lowering.REC_WHY, item.line)
        for d in item.defs:
            if not isinstance(d.pattern, O.Variable):
                raise ProofError("a top-level theorem must be named", d.line)
            name = d.pattern.name
            scheme = next(s for n, s, dd, _ in c.toplevel if dd is d)
            low.define(name, scheme, d)
            low.globals.add(name)
    return low


def lean_source(low):
    import leanexport as X
    names = [n for n, _ in low.theorems]
    slow = low.H.prelude(fast=False)
    values = {n: low.L.value_of(slow, n) for n in low.H.ACCELERATED}
    src, _ = X.export(low.env, names, values,
                      ['#print axioms %s' % n for n in names])
    return src


def find_lean():
    return shutil.which('lean') or next(
        (p for p in (os.path.expanduser('~/.local/lean/bin/lean'),
                     os.path.expanduser('~/.elan/bin/lean'))
         if os.path.exists(p)), None)


def lean_check(low, directory=None):
    """(accepted, [theorems free of axioms], output) from Lean 4, or None
    when Lean is not installed."""
    import tempfile
    lean = find_lean()
    if lean is None:
        return None
    path = os.path.join(directory or tempfile.mkdtemp(), 'OCaml.lean')
    with open(path, 'w') as fh:
        fh.write(lean_source(low))
    run = subprocess.run([lean, path], capture_output=True, text=True)
    free = [n for n, _ in low.theorems
            if "'RM.%s' does not depend on any axioms" % n in run.stdout]
    return run.returncode == 0, free, run.stdout + run.stderr


def main(argv):
    status = 0
    for path in argv:
        with open(path) as fh:
            code = fh.read()
        try:
            low = prove(code)
        except (O.CompileError, ProofError) as exc:
            print('%s: %s' % (path, exc))
            status = 1
            continue
        for name, stmt in low.theorems:
            print('proved %s : %s' % (name, low.H.readable(stmt)))
        verdict = lean_check(low)
        if verdict is None:
            print('(lean not found; checked by lean4.py only)')
        else:
            ok, free, out = verdict
            print('Lean 4: %s, %d of %d theorems depending on no axioms'
                  % ('accepted' if ok else 'REJECTED', len(free),
                     len(low.theorems)))
            if not ok:
                print(out[-2000:])
                status = 1
    return status


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
