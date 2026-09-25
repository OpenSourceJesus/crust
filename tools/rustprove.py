#!/usr/bin/env python3
"""Prove what `shivyc/rustproof.py` lifts: contracts, and safety obligations.

`rustproof.py` writes source and imports no kernel, so that a compiler pass
can use it where there is none.  This is the other half: it hands the lifted
functions to RosettaMath's `hoare.py`, and for each obligation -- each
`#[ensures]`, and each place the Rust could panic -- reports whether the
kernel proves it.  Nothing is assumed.  An obligation the automation does
not settle is reported *open*, which says nothing about whether it holds.

    python3 tools/rustprove.py leanos/alloc.rs

The automation is `hoare.by_every_bool`: split on every guard, then compute.
That settles an obligation the code's own branches decide -- an index
behind its length check, a subtraction behind its comparison.  What it
leaves goes to `hoare.by_bounds`, which splits the same way but keeps what
each guard said, and chains `<=` facts: `used + n <= max` behind
`used + n <= sizes[heap]`, given that every element of `sizes` is a `u64`.  `by_every_bool` does not bind an obligation's
hypotheses, so an obligation with preconditions is first *weakened*: its
conclusion is proved on its own, and the proof is wrapped in lambdas that
take the hypotheses and ignore them.  The kernel checks the wrapped term
against the original statement, so the weakening is not trusted either.
"""
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from shivyc.rustproof import (lift_all, signatures,  # noqa: E402
                              in_dependency_order)


def rosettamath():
    for path in (os.environ.get("ROSETTAMATH_DIR"),
                 os.path.join(ROOT, "..", "RosettaMath"),
                 os.path.expanduser("~/RosettaMath")):
        if path and os.path.isfile(os.path.join(path, "hoare.py")):
            return os.path.abspath(path)
    return None


def in_big_stack(work):
    """The kernel recurses deeply; give it a stack to do it in."""
    out, err = [], []

    def target():
        sys.setrecursionlimit(300000)
        try:
            out.append(work())
        except BaseException as exc:            # re-raised below
            err.append(exc)

    threading.stack_size(512 * 1024 * 1024)
    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if err:
        raise err[0]
    return out[0]


class Prover:
    """One source's lifted functions, read into one kernel environment."""

    def __init__(self, source, own=None):
        """`source` is one unit; `own` names the functions to report and
        certify -- a file's own, when its `// uses:` files were appended
        (see `load_unit`).  None: every function in the source."""
        path = rosettamath()
        if path is None:
            raise RuntimeError("RosettaMath not found; run "
                               "'make install_proofs'")
        sys.path.insert(0, path)
        import hoare
        import lean4
        self.H, self.L = hoare, lean4
        self.lifted, self.refused = lift_all(source)
        self.own = set(self.lifted) | set(self.refused) if own is None \
            else set(own)
        self.types = {"Nat": hoare.NAT, "Bool": hoare.BOOL,
                      "Array": hoare.BYTES, "Int": hoare.INT}
        # Every obligation the kernel settles, kept with the environment it
        # was proved in, so a second kernel can be asked the same question.
        self.certificates = []
        # How many guards `by_every_bool` may split.  Its default, 16, is
        # sized for a guard chain; a lifted body with nested guards, early
        # returns and obligations stated as chains of spellings has more.
        self.limit = 64

    def fresh_env(self, fn):
        """An environment with `fn`'s records, callees and their `__pre`s
        defined, and the signatures to call them by."""
        H = self.H
        env = H.prelude()
        for rec, fields in fn.records:
            H.record(env, rec, [(f, self.types[t]) for f, t in fields])
            self.types[rec] = self.L.Var(rec)
        sig = {}
        for c in in_dependency_order(fn):
            if c is fn:
                continue
            for rec, fields in c.records:
                H.record(env, rec, [(f, self.types[t]) for f, t in fields])
                self.types[rec] = self.L.Var(rec)
            if getattr(c, "recursive", False):
                # known only by its contract, which is a hypothesis of every
                # goal about a caller (`goal_for`); its own theorems prove it
                fty = self.types[c.ret]
                for _, t in reversed(c.params):
                    fty = self.L.Pi("_", self.types[t], fty)
                self.L.declare(env, c.name, fty)
            else:
                H.read_procedure(c.source, env, self.sig(c),
                                 self.trivial(c))
            if c.pre_source is not None:
                H.read_procedure(c.pre_source, env, None,
                                 ["result or not result"])
        for name, (args, ret) in signatures(fn).items():
            sig[name] = ([self.types[a] for a in args], self.types[ret])
        return env, sig

    def sig(self, fn):
        return {name: ([self.types[a] for a in args], self.types[ret])
                for name, (args, ret) in signatures(fn).items()}

    def trivial(self, fn):
        if fn.ret == "Bool":
            return ["result or not result"]
        if fn.ret == "Nat":
            return ["result == result"]
        return ["True"]

    def by_every_bool(self, env, goal, unfolding):
        """`hoare.by_every_bool`, weakened past the goal's hypotheses."""
        H, L = self.H, self.L
        binders, body = [], goal
        while isinstance(body, L.Pi):
            binders.append(body)
            body = body.body
        kept = [b for b in binders if not _is_hypothesis(b, L)]
        hyps = len(binders) - len(kept)
        # Weakening is done only in the layout `read_procedure` builds --
        # parameters, then hypotheses -- where it is a plain renumbering.
        if hyps == 0 or binders[:len(kept)] != kept:
            return H.by_every_bool(env, goal, unfolding=unfolding,
                                   limit=self.limit)
        # Binders are de Bruijn: the body names the parameters by depth,
        # counting the hypotheses in between.  The conclusion mentions no
        # hypothesis (a proposition is not a value), so dropping them is
        # lowering every index past them by their number.
        stripped = L.shift(body, -hyps)
        for b in reversed(kept):
            stripped = L.Pi(b.var_name, b.var_type, stripped)
        inner = H.by_every_bool(env, stripped, unfolding=unfolding,
                                limit=self.limit)
        term = inner
        for b in kept:
            term = L.App(term, L.Var(b.var_name))
        for b in reversed(binders):
            term = L.Lambda(b.var_name, b.var_type, term)
        return H.prove(goal, term, env, verbose=False)

    def by_guards(self, env, goal, unfolding):
        """`hoare.bound_by_ites_or_guards`, as `alloc_eq.py` uses it, for a
        goal splitting alone does not close.  The obligation is opened into
        named variables, at most one precondition is threaded as the
        hypothesis `h`, and the prelude's own lemmas are offered as facts
        about the terms that appear: `le_add_right` (`a <= a + b`) for each
        sum, `sub_le` (`a - b <= a`) for each difference, `eqb_refl` for
        each comparison of a term with itself."""
        H, L = self.H, self.L
        binders, body = [], goal
        while isinstance(body, L.Pi):
            binders.append(body)
            body = body.body
        params = [b for b in binders if not _is_hypothesis(b, L)]
        hyps = binders[len(params):]
        if binders[:len(params)] != params or len(hyps) > 1:
            raise H.TheoremError("only a single trailing precondition is "
                                 "threaded")
        opened, claim = body, None
        if hyps:
            opened = L.instantiate(opened, L.Var("h"))
            claim = hyps[0].var_type
            for b in reversed(params):
                claim = L.instantiate(claim, L.Var(b.var_name))
        for b in reversed(params):
            opened = L.instantiate(opened, L.Var(b.var_name))
        opened = H.unfold(opened, env, set(unfolding))
        # Facts are matched against the reduced goal: a record update read
        # back through a projection, `R.f (R.with_g r v)`, is `R.f r` only
        # once it has reduced.
        facts_in = L.normalize(opened, env)
        others = []
        seen = set()
        terms = _binary_terms(opened, L) + _binary_terms(facts_in, L)
        for fn, x, y in terms:
            key = (fn, x.fullkey(), y.fullkey())
            if key in seen:
                continue
            seen.add(key)
            if fn == "add":
                others.append((H.app("Holds", H.app("leb", x,
                                                    H.app("add", x, y))),
                               H.app("le_add_right", x, y)))
            elif fn == "sub":
                others.append((H.app("Holds", H.app("leb",
                                                    H.app("sub", x, y), x)),
                               H.app("sub_le", x, y)))
            elif fn == "eqb" and x.fullkey() == y.fullkey():
                others.append((H.app("Holds", H.app("eqb", x, x)),
                               H.app("eqb_refl", x)))
        if hyps:
            proof = H.bound_by_ites_or_guards(env, opened, L.Var("h"), claim,
                                              others=others)
        else:
            proof = H.bound_by_ites_or_guards(env, opened, None,
                                              H.app("Holds", L.Var("false")),
                                              others=others)
        if not _complete(proof, L):
            # The tactic answers None -- or a term with a None where a
            # branch's proof should be -- rather than raising, when no guard
            # settles the goal.  That is what a false claim looks like.
            raise H.TheoremError("no guard settles the obligation")
        if hyps:
            proof = L.Lambda("h", hyps[0].var_type, proof)
        for b in reversed(params):
            proof = L.Lambda(b.var_name, b.var_type, proof)
        return H.prove(goal, proof, env, verbose=False)

    def by_bounds(self, env, goal, unfolding):
        """`hoare.by_bounds`: split dependently, keep every hypothesis, and
        chain `<=` facts at a leaf that does not compute -- a guard, a
        `requires`, an integer's range, `n <= s && u <= s - n`."""
        return self.H.by_bounds(env, goal, unfolding=unfolding,
                                limit=self.limit)

    def through_loops(self, fn, text, post, if_returns, always, unfolding):
        """`hoare.by_loop`, with each loop's invariant strengthened.

        The invariant written on a `while` speaks of the Rust's variables;
        what a postcondition needs of the loop is also what an early
        `return` inside it left behind -- `_returned` and `_return_value`,
        the state the lowering carries -- and, for a safety obligation, that
        `_ok` is still true.  So each invariant is conjoined with `always`,
        and with `if_returns` when the loop can return; if that does not read
        (the loop never returns, so there is no `_returned`), without it.
        This changes the proof, not the theorem: the function and its
        statement do not mention the invariant."""
        H = self.H
        failed = (H.TheoremError, H.ContractError, self.L.KernelError)
        last = None
        # With a `return` anywhere before or in the loop, the lowering keeps
        # iterating after it with the returned value frozen, so what the
        # loop keeps need only hold while nothing has returned.
        returning = (["(_returned) or (%s)" % e for e in always]
                     + if_returns, True)
        for extra, guarded in (returning, (always, False)):
            source = _strengthened(text, extra, guarded)
            env, sig = self.fresh_env(fn)
            try:
                proc = H.read_procedure(source, env, sig, post)
            except failed as exc:
                last = exc
                continue
            proof = H.by_loop(env, proc, unfolding=unfolding)
            return env, proc.obligation, proof
        raise last

    def by_integers(self, env, goal, unfolding):
        """`hoare.by_integers`: each `Int` split into its two shapes, then
        `by_bounds` over the natural numbers that are left.  Tried only on
        a goal that has an integer to split."""
        if 'Int' not in self.H.readable(goal):
            raise self.L.TheoremError("no integer to split")
        return self.H.by_integers(env, goal, unfolding=unfolding,
                                  limit=self.limit)

    def first_of(self, env, goal, unfolding, tactics):
        """The first tactic's proof that the kernel accepts."""
        failed = (self.H.TheoremError, self.H.ContractError,
                  self.L.KernelError)
        for k, tactic in enumerate(tactics):
            try:
                return tactic(env, goal, unfolding)
            except failed:
                if k == len(tactics) - 1:
                    raise

    def settles(self, work, function=None, label=None, index=None):
        """True if `work` proves; False if the automation does not.

        `work` returns (env, statement, proof).  A settled obligation is
        kept as a `Certificate`, which is what `RosettaMath/rustlean.py`
        hands to Lean."""
        try:
            env, goal, proof = work()
        except (self.H.TheoremError, self.H.ContractError,
                self.L.KernelError):
            return False
        # Asking again about an obligation already settled proves it again
        # but keeps one certificate, so a count of them counts theorems.
        # Labels are not identities -- two index checks on one line read
        # the same -- so an obligation is keyed by its position too.
        if function is not None and not any(
                c.key == (function, label, index)
                for c in self.certificates):
            self.certificates.append(Certificate(
                theorem_name(function, label,
                             [c.name for c in self.certificates]),
                function, label, env, goal, proof, index))
        return True

    def recursive_goal(self, fn, text, post, unfolding):
        """(env, goal) for a theorem about a recursive function: `text`'s
        obligation with the recursive call a variable `g` -- about which
        the goal assumes the contract, for arguments meeting the `requires`
        with a smaller non-negative `#[variant]`:

            forall g p.., (forall a.., cond(a, p) -> post(a, g a)) -> ...

        Each call site owes `cond` as an obligation of its own, so this is
        the step of a well-founded induction on the variant, whose
        conclusion is the theorem for the function itself."""
        H, L = self.H, self.L
        env, sig = self.fresh_env(fn)
        rec = fn.name + "__rec"
        ptypes = [self.types[t] for _, t in fn.params]
        rtype = self.types[fn.ret]
        fty = rtype
        for t in reversed(ptypes):
            fty = L.Pi("_", t, fty)
        L.declare(env, rec, fty)
        sig = dict(sig)
        sig[rec] = (ptypes, rtype)
        proc = H.read_procedure(text, env, sig, post)
        names = [n for n, _ in fn.params]
        anames = ["%s__a" % n for n in names]
        to_a = dict(zip(names, anames))
        from shivyc.rustproof import _substitute
        cond = " and ".join([_substitute(r, to_a) for r in fn.rec_requires]
                            + ["(Int(0) <= %s)" % _substitute(
                                fn.rec_variant, to_a),
                               "(%s < %s)" % (_substitute(fn.rec_variant,
                                                          to_a),
                                              fn.rec_variant)])
        both = ", ".join("%s: '%s'" % (n, t) for n, t in
                         [(a, t) for a, (_, t) in zip(anames, fn.params)]
                         + list(fn.params))
        H.read_procedure("def %s__cond(%s) -> 'Bool':\n    return %s\n"
                         % (fn.name, both, cond), env, None,
                         ["result or not result"])
        ens = " and ".join(_substitute(e, to_a) for e in fn.ensures) \
            or "True"
        H.read_procedure("def %s__post(%s, result: '%s') -> 'Bool':\n"
                         "    return %s\n" % (
                             fn.name, ", ".join("%s: '%s'" % (a, t) for a, (
                                 _, t) in zip(anames, fn.params)),
                             fn.ret, ens), env, None,
                         ["result or not result"])
        helpers = {fn.name + "__cond", fn.name + "__post"}
        ob = H.unfold(proc.obligation, env, set(unfolding) | helpers)
        # open the parameters, put the hypothesis after them
        pvars, body = [], ob
        for _ in names:
            pvars.append(L.Var(body.var_name + "__p"))
            body = L.instantiate(body.body, pvars[-1])
        avars = [L.Var(a + "__b") for a in anames]
        call = L.Var(rec)
        for a in avars:
            call = L.App(call, a)
        ih = L.Pi("_", L.App(L.Var("Holds"), H.unfold(H.app(
            fn.name + "__cond", *(avars + pvars)), env, helpers)),
            L.App(L.Var("Holds"), H.unfold(H.app(
                fn.name + "__post", *(avars + [call])), env, helpers)))
        for a in reversed(avars):
            ih = L.Pi(a.name, H.INT if False else self.types[
                fn.params[avars.index(a)][1]], L.abstract(ih, a.name))
        goal = L.Pi("_ih", ih, body)
        for (pv, t) in reversed(list(zip(pvars, ptypes))):
            goal = L.Pi(pv.name, t, L.abstract(goal, pv.name))
        goal = L.Pi("g", fty, L.abstract(goal, rec))
        del env[rec]
        return env, goal

    def goal_for(self, fn, text, post, unfolding):
        """(env, goal) for an obligation of `fn`: `recursive_goal` if it
        calls itself, and for each recursive function it calls, that
        function's contract as a hypothesis over a variable standing for
        it --

            forall h.., (forall a.., requires(a) -> ensures(a, h a)) -> ..

        -- a callee known by its contract, proved by its own theorems."""
        H, L = self.H, self.L
        if getattr(fn, "recursive", False):
            env, goal = self.recursive_goal(fn, text, post, unfolding)
        else:
            env, sig = self.fresh_env(fn)
            goal = H.read_procedure(text, env, sig, post).obligation
        from shivyc.rustproof import _substitute
        for c in in_dependency_order(fn):
            if c is fn or not getattr(c, "recursive", False):
                continue
            names = [n for n, _ in c.params]
            anames = ["%s__c" % n for n in names]
            to_a = dict(zip(names, anames))
            params = ", ".join("%s: '%s'" % (a, t) for a, (_, t) in
                               zip(anames, c.params))
            pre = " and ".join(_substitute(r, to_a)
                               for r in c.rec_requires) or "True"
            ens = " and ".join(_substitute(e, to_a) for e in c.ensures) \
                or "True"
            H.read_procedure("def %s__cpre(%s) -> 'Bool':\n    return %s\n"
                             % (c.name, params, pre), env, None,
                             ["result or not result"])
            H.read_procedure("def %s__cpost(%s, result: '%s') -> 'Bool':\n"
                             "    return %s\n" % (c.name, params, c.ret, ens),
                             env, None, ["result or not result"])
            helpers = {c.name + "__cpre", c.name + "__cpost"}
            avars = [L.Var(a + "__d") for a in anames]
            call = L.Var(c.name)
            for a in avars:
                call = L.App(call, a)
            hyp = L.Pi("_", L.App(L.Var("Holds"), H.unfold(H.app(
                c.name + "__cpre", *avars), env, helpers)),
                L.App(L.Var("Holds"), H.unfold(H.app(
                    c.name + "__cpost", *(avars + [call])), env, helpers)))
            for a, (_, t) in reversed(list(zip(avars, c.params))):
                hyp = L.Pi(a.name, self.types[t], L.abstract(hyp, a.name))
            fty = self.types[c.ret]
            for _, t in reversed(c.params):
                fty = L.Pi("_", self.types[t], fty)
            goal = H.unfold(goal, env, set(unfolding) - {c.name})
            goal = L.Pi(c.name + "__f", fty, L.abstract(
                L.Pi("_" + c.name + "__contract", hyp, goal), c.name))
            del env[c.name]
        return env, goal

    def contract(self, name, ensures=None):
        """Does the kernel prove `name`'s `#[ensures]` (or `ensures`)?"""
        fn = self.lifted[name]
        post = fn.ensures if ensures is None else ensures

        def work():
            unfolding = {c.name for c in in_dependency_order(fn)}
            if _has_loop(fn.source):
                extra = ["(not _returned) or (%s)"
                         % _renamed(p, "result", "_return_value")
                         for p in post]
                return self.through_loops(fn, fn.source, post, extra, [],
                                          unfolding)
            if getattr(fn, "recursive", False) or any(
                    getattr(c, "recursive", False)
                    for c in in_dependency_order(fn)):
                env, goal = self.goal_for(fn, fn.source, post, unfolding)
                proof = self.first_of(env, goal, unfolding,
                                      (self.by_bounds, self.by_integers))
                return env, goal, proof
            env, sig = self.fresh_env(fn)
            proc = self.H.read_procedure(fn.source, env, sig, post)
            proof = self.first_of(
                env, proc.obligation, unfolding,
                (self.by_every_bool, self.by_guards, self.by_bounds,
                 self.by_integers))
            return env, proc.obligation, proof
        label = "ensures " + " and ".join(post)
        return in_big_stack(lambda: self.settles(work, name, label))

    def safety(self, name):
        """[(obligation, proved?)] for each place `name` could panic."""
        fn = self.lifted[name]
        out = []
        for k, label in enumerate(fn.obligations):
            def work(k=k):
                unfolding = {name + "__safe"}
                for c in in_dependency_order(fn):
                    if c.pre_source is not None:
                        unfolding.add(c.name + "__pre")
                text = fn.safety(only=k)
                if _has_loop(text):
                    return self.through_loops(
                        fn, text, ["result"],
                        ["(not _returned) or _return_value"], ["_ok"],
                        unfolding)
                if getattr(fn, "recursive", False) or any(
                        getattr(c, "recursive", False)
                        for c in in_dependency_order(fn)):
                    env, goal = self.goal_for(fn, text, ["result"],
                                              unfolding)
                    proof = self.first_of(env, goal, unfolding,
                                          (self.by_bounds, self.by_integers))
                    return env, goal, proof
                env, sig = self.fresh_env(fn)
                proc = self.H.read_procedure(text, env, sig, ["result"])
                proof = self.first_of(env, proc.obligation, unfolding,
                                      (self.by_every_bool, self.by_bounds,
                                       self.by_integers))
                return env, proc.obligation, proof
            out.append((label, in_big_stack(
                lambda: self.settles(work, name, label, k))))
        return out


def _has_loop(source):
    return any(line.lstrip().startswith("while ")
               for line in source.splitlines())


def _renamed(text, old, new):
    import re
    return re.sub(r"\b%s\b" % re.escape(old), new, text)


def _strengthened(source, extra, guarded=False):
    """Every `assert invariant(X)` in a lifted source as
    `assert invariant((X) and (e1) and ..)` -- or, `guarded`, with
    `(_returned) or (X)` for X, the written invariant needing to hold only
    until something returns."""
    if not extra and not guarded:
        return source
    out = []
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("assert invariant(") and \
                stripped.endswith(")"):
            inner = stripped[len("assert invariant("):-1]
            if guarded:
                inner = "(_returned) or (%s)" % inner
            parts = ["(%s)" % inner] + ["(%s)" % e for e in extra]
            line = line[:len(line) - len(stripped)] + \
                "assert invariant(%s)" % " and ".join(parts)
        out.append(line)
    return "\n".join(out) + "\n"


class Certificate:
    """One settled obligation: the statement, the term the kernel accepted
    for it, and the environment both live in."""

    def __init__(self, name, function, label, env, statement, proof,
                 index=None):
        self.name = name                # a Lean-safe theorem name
        self.function = function
        self.label = label
        self.index = index              # which panic obligation; None: ensures
        self.key = (function, label, index)
        self.env = env
        self.statement = statement
        self.proof = proof

    def __repr__(self):
        return "Certificate(%s: %s %s)" % (self.name, self.function,
                                            self.label)


def theorem_name(function, label, taken):
    """`rust_bump_ensures`, `rust_bump_safe_line57` -- unique in `taken`."""
    if label and label.startswith("ensures"):
        kind = "ensures"
    elif label and "(line " in label:
        kind = "safe_line" + label.rsplit("(line ", 1)[1].rstrip(")")
    else:
        kind = "safe"
    base = "rust_%s_%s" % (function, kind)
    name, n = base, 1
    while name in taken:
        n += 1
        name = "%s_%d" % (base, n)
    return name


def _complete(term, L):
    """True if `term` is a term all the way down (no None for a branch)."""
    stack = [term]
    while stack:
        t = stack.pop()
        if t is None:
            return False
        if isinstance(t, L.App):
            stack.append(t.func)
            stack.append(t.arg)
        elif isinstance(t, (L.Lambda, L.Pi)):
            stack.append(t.var_type)
            stack.append(t.body)
    return True


def _binary_terms(term, L):
    """Every `add`/`sub`/`eqb x y` in a term, as (name, x, y)."""
    out, stack = [], [term]
    while stack:
        t = stack.pop()
        if isinstance(t, L.App):
            f = t.func
            if isinstance(f, L.App) and isinstance(f.func, L.Var) \
                    and f.func.name in ("add", "sub", "eqb"):
                out.append((f.func.name, f.arg, t.arg))
            stack.append(t.func)
            stack.append(t.arg)
        elif isinstance(t, (L.Lambda, L.Pi)):
            stack.append(t.var_type)
            stack.append(t.body)
    return out


def _is_hypothesis(binder, L):
    """A binder whose type is a proposition `Holds b` -- a precondition."""
    ty = binder.var_type
    head = ty
    while isinstance(head, L.App):
        head = head.func
    return isinstance(head, L.Var) and head.name in ("Holds", "Eq")


def load_unit(path):
    """(source, own function names) for a Rust file and what it uses.

    A LeanOS file that calls into another says so on a line of its own,

        // uses: memmap.rs elfcheck.rs

    and is one unit with them, as rpython's files were one translation
    unit.  The used files, and theirs, are appended *after* the file itself,
    so its own line numbers -- the ones every obligation names -- are
    exact.  Rust does not care in what order functions are defined."""
    import re
    order, seen = [], set()

    def visit(p):
        p = os.path.normpath(p)
        if p in seen:
            return
        seen.add(p)
        order.append(p)
        with open(p) as fh:
            text = fh.read()
        for m in re.finditer(r"^//\s*uses:\s*(.+)$", text, re.M):
            for dep in m.group(1).split():
                visit(os.path.join(os.path.dirname(p), dep))
    visit(path)
    texts = []
    for p in order:
        with open(p) as fh:
            texts.append(fh.read())
    own = set(re.findall(r"\bfn\s+([A-Za-z_]\w*)", texts[0]))
    return "\n".join(texts), own


def report(path):
    source, own = load_unit(path)
    prover = Prover(source, own)
    for name, why in sorted(prover.refused.items()):
        if name in own:
            print("%s: not lifted -- %s" % (name, why))
    for name in sorted(n for n in prover.lifted if n in own):
        fn = prover.lifted[name]
        if fn.ensures:
            print("%s: ensures %s -- %s" % (
                name, " and ".join(fn.ensures),
                "proved" if prover.contract(name) else "open"))
        for label, ok in prover.safety(name):
            print("%s: %s -- %s" % (name, label, "proved" if ok else "open"))


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        report(arg)
