#!/usr/bin/env python3
r"""ocamlinterp.py -- the OCaml subset, evaluated directly.

A reference for the compiled code: `ocaml2rust.py` lowers a program to Rust,
Crust compiles it, and the output is compared with what this prints for the
same program.  Written to be obviously OCaml's semantics rather than fast --
an environment-passing evaluator over the AST `ocaml.py` typed:

  * integers are OCaml's 63-bit ones: arithmetic wraps at 2^62, `/`
    truncates toward zero and `mod` takes the dividend's sign; division by
    zero is an error (OCaml's `Division_by_zero`);
  * values are immutable and shared; functions are curried closures;
  * arguments are evaluated left to right.  OCaml leaves the order
    unspecified (native code happens to go right to left), so a program
    whose output depends on it is not a test of anything.

    python3 ocamlinterp.py FILE.ml
"""
import sys

import ocaml as O

BITS = 63


def wrap(n):
    """An OCaml int: two's complement in 63 bits."""
    n &= (1 << BITS) - 1
    return n - (1 << BITS) if n >= 1 << (BITS - 1) else n


class Ctor:
    __slots__ = ('name', 'args')

    def __init__(self, name, args):
        self.name, self.args = name, args


class Closure:
    """A one-argument function, as OCaml's all are."""
    __slots__ = ('apply',)

    def __init__(self, apply):
        self.apply = apply


class RuntimeFailure(Exception):
    pass


class TailCall:
    """A call in tail position, not yet made: OCaml runs tail calls in
    constant space, so the evaluator returns this instead of recursing, and
    whoever needs the value makes the calls in a loop (`force`)."""
    __slots__ = ('f', 'arg')

    def __init__(self, f, arg):
        self.f, self.arg = f, arg


def force(v):
    while isinstance(v, TailCall):
        v = v.f.apply(v.arg)
    return v


class Interp:
    def __init__(self):
        self.out = []
        self.globals = {
            'print_int': Closure(lambda n: self.emit(str(n))),
            'print_newline': Closure(lambda _: self.emit('\n')),
            'not': Closure(lambda b: not b),
            'fst': Closure(lambda p: p[0]),
            'snd': Closure(lambda p: p[1]),
            'min_int': -(1 << (BITS - 1)), 'max_int': (1 << (BITS - 1)) - 1,
        }

    def emit(self, text):
        self.out.append(text)
        return ()

    # -- expressions --
    def eval(self, e, env):
        """The value of `e`, every tail call made."""
        return force(self.ev(e, env, False))

    def ev(self, e, env, tail):
        """The value of `e`, or -- in tail position -- a `TailCall`."""
        if isinstance(e, O.Const):
            return e.value
        if isinstance(e, O.Variable):
            v = env[e.name] if e.name in env else self.globals[e.name]
            return v() if callable(v) and not isinstance(v, Closure) else v
        if isinstance(e, O.ConstructorApp):
            return Ctor(e.name, [self.eval(a, env) for a in e.args])
        if isinstance(e, O.TupleNode):
            return tuple(self.eval(x, env) for x in e.elements)
        if isinstance(e, O.Application):
            f = self.eval(e.func, env)
            args = [self.eval(a, env) for a in e.args]
            for a in args[:-1]:
                f = force(f.apply(a))
            if tail:
                return TailCall(f, args[-1])
            return force(f.apply(args[-1]))
        if isinstance(e, O.BinOp):
            return self.binop(e, env)
        if isinstance(e, O.IfThen):
            return self.ev(e.then if self.eval(e.cond, env) else e.other,
                           env, tail)
        if isinstance(e, O.Seq):
            self.eval(e.first, env)
            return self.ev(e.second, env, tail)
        if isinstance(e, O.Annot):
            return self.ev(e.expr, env, tail)
        if isinstance(e, O.Fun):
            return self.closure(e.params, e.body, env)
        if isinstance(e, O.LetIn):
            return self.ev(e.body, self.bind_group(e.group, env), tail)
        if isinstance(e, O.MatchWith):
            v = self.eval(e.match_expr, env)
            for br in e.branches:
                inner = dict(env)
                if self.match(br.pattern, v, inner) and (
                        br.guard is None or self.eval(br.guard, inner)):
                    if isinstance(br.body, O.Refute):
                        raise RuntimeFailure("reached a refuted case")
                    return self.ev(br.body, inner, tail)
            raise RuntimeFailure("Match_failure")
        raise RuntimeFailure("cannot evaluate %r" % (e,))

    def binop(self, e, env):
        op = e.op
        if op == '&&':
            return self.eval(e.left, env) and self.eval(e.right, env)
        if op == '||':
            return self.eval(e.left, env) or self.eval(e.right, env)
        a, b = self.eval(e.left, env), self.eval(e.right, env)
        if op == '+':
            return wrap(a + b)
        if op == '-':
            return wrap(a - b)
        if op == '*':
            return wrap(a * b)
        if op in ('/', 'mod'):
            if b == 0:
                raise RuntimeFailure("Division_by_zero")
            q = abs(a) // abs(b)
            q = q if (a >= 0) == (b >= 0) else -q
            return wrap(q) if op == '/' else wrap(a - q * b)
        if op in ('=', '<>'):
            same = self.equal(a, b)
            return same if op == '=' else not same
        return {'<': a < b, '>': a > b, '<=': a <= b, '>=': a >= b}[op]

    def equal(self, a, b):
        if isinstance(a, Ctor):
            return a.name == b.name and all(
                self.equal(x, y) for x, y in zip(a.args, b.args))
        if isinstance(a, tuple):
            return all(self.equal(x, y) for x, y in zip(a, b))
        if isinstance(a, Closure):
            raise RuntimeFailure("compare: functional value")
        return a == b

    def closure(self, params, body, env):
        def make(k, env):
            if k == len(params):
                return self.ev(body, env, True)     # a function's body is
                                                    # in tail position

            def apply(v, k=k, env=env):
                inner = dict(env)
                if not self.match(params[k][0], v, inner):
                    raise RuntimeFailure("Match_failure")
                return make(k + 1, inner)
            return Closure(apply)
        return make(0, env)

    # -- patterns --
    def match(self, p, v, env):
        if isinstance(p, O.Variable):
            env[p.name] = v
            return True
        if isinstance(p, O.Wildcard):
            return True
        if isinstance(p, O.Const):
            return p.value == v
        if isinstance(p, O.TupleNode):
            return all(self.match(sub, x, env)
                       for sub, x in zip(p.elements, v))
        if isinstance(p, O.ConstructorPat):
            return v.name == p.name and all(
                self.match(sub, x, env) for sub, x in zip(p.args, v.args))
        raise RuntimeFailure("bad pattern")

    # -- let --
    def bind_group(self, group, env):
        env = dict(env)
        if group.rec:
            cells = {}
            for d in group.defs:
                env[d.pattern.name] = (lambda n=d.pattern.name:
                                       cells[n])
            for d in group.defs:
                cells[d.pattern.name] = self.value_of(d, env)
            for d in group.defs:
                env[d.pattern.name] = cells[d.pattern.name]
            return env
        values = [self.value_of(d, env) for d in group.defs]
        for d, v in zip(group.defs, values):
            if not self.match(d.pattern, v, env):
                raise RuntimeFailure("Match_failure")
        return env

    def value_of(self, d, env):
        if d.params:
            return self.closure(d.params, d.value, env)
        return self.eval(d.value, env)

    def run(self, items):
        env = {}
        for item in items:
            if isinstance(item, O.LetGroup):
                env = self.bind_group(item, env)
        return ''.join(self.out)


def run(code):
    """What the program prints.  Evaluated on a thread with a large stack:
    each OCaml call is several Python frames, and a program that recurses
    100000 deep -- which compiled OCaml does -- must not stop here first."""
    import threading
    items, _ = O.check(code, strict=False)
    result, error = [], []

    def work():
        try:
            result.append(Interp().run(items))
        except BaseException as exc:            # carried to the caller
            error.append(exc)
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(10 ** 7)
    old_size = threading.stack_size(1 << 30)
    try:
        t = threading.Thread(target=work)
        t.start()
        t.join()
    finally:
        threading.stack_size(old_size)
        sys.setrecursionlimit(old_limit)
    if error:
        raise error[0]
    return result[0]


if __name__ == '__main__':
    sys.stdout.write(run(open(sys.argv[1]).read()))
