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
behind its length check, a subtraction behind its comparison -- and nothing
that needs arithmetic.  `by_every_bool` does not bind an obligation's
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

    def __init__(self, source):
        path = rosettamath()
        if path is None:
            raise RuntimeError("RosettaMath not found; run "
                               "'make install_proofs'")
        sys.path.insert(0, path)
        import hoare
        import lean4
        self.H, self.L = hoare, lean4
        self.lifted, self.refused = lift_all(source)
        self.types = {"Nat": hoare.NAT, "Bool": hoare.BOOL,
                      "Array": hoare.BYTES}
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
            H.read_procedure(c.source, env, self.sig(c), self.trivial(c))
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

    def settles(self, work):
        """True if `work` proves; False if the automation does not."""
        try:
            work()
            return True
        except (self.H.TheoremError, self.H.ContractError,
                self.L.KernelError):
            return False

    def contract(self, name, ensures=None):
        """Does the kernel prove `name`'s `#[ensures]` (or `ensures`)?"""
        fn = self.lifted[name]
        post = fn.ensures if ensures is None else ensures

        def work():
            env, sig = self.fresh_env(fn)
            proc = self.H.read_procedure(fn.source, env, sig, post)
            unfolding = {c.name for c in in_dependency_order(fn)}
            try:
                self.by_every_bool(env, proc.obligation, unfolding)
            except (self.H.TheoremError, self.H.ContractError,
                    self.L.KernelError):
                self.by_guards(env, proc.obligation, unfolding)
        return in_big_stack(lambda: self.settles(work))

    def safety(self, name):
        """[(obligation, proved?)] for each place `name` could panic."""
        fn = self.lifted[name]
        out = []
        for k, label in enumerate(fn.obligations):
            def work(k=k):
                env, sig = self.fresh_env(fn)
                proc = self.H.read_procedure(fn.safety(only=k), env, sig,
                                             ["result"])
                unfolding = {name + "__safe"}
                for c in in_dependency_order(fn):
                    if c.pre_source is not None:
                        unfolding.add(c.name + "__pre")
                self.by_every_bool(env, proc.obligation, unfolding)
            out.append((label, in_big_stack(lambda: self.settles(work))))
        return out


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


def report(path):
    with open(path) as fh:
        prover = Prover(fh.read())
    for name, why in sorted(prover.refused.items()):
        print("%s: not lifted -- %s" % (name, why))
    for name in sorted(prover.lifted):
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
