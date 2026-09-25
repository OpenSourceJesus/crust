#!/usr/bin/env python3
r"""ocaml2rust.py -- the OCaml subset, lowered to Crust's Rust subset.

`ocaml.py` parses and types a program; this writes Rust that Crust compiles,
so an OCaml program runs as native code and its functions are in reach of
the contract and proof tooling the Rust subset has (`rustproof`,
`rustprove`).  The output is ordinary Rust in the subset, one file:

  types        `int` -> `ml_int` (an i64 the proof lift knows is 63-bit),
               `bool` -> bool, `unit` -> nothing (a unit
               parameter is dropped, a unit result is no result), tuples ->
               tuples, `a -> b -> c` -> `fn(A, B) -> C`
  variants     one `enum` per type *and* instantiation -- `'a tree` used at
               int is `ml_tree_int` -- with every enum-typed payload held by
               a pointer, so recursion needs no `Box`; `'a list` is such an
               enum too, `Nil | Cons(A, *mut ml_list_A)`
  functions    one `fn` per function and instantiation (monomorphised from
               the types the front end inferred); curried definitions take
               all their arguments at once; local functions are lambda-
               lifted, with what they capture as leading parameters
  match        tests and projections along paths:
                 `x :: y :: rest` -> ml_is_L_Cons(s) &&
                   ml_is_L_Cons(ml_L_Cons_1(s)) ..
               so nesting reaches through the heap pointers the same way at
               every depth, and `&&` never runs a projection on the wrong
               variant
  values       immutable and shared: a constructor allocates, nothing is
               freed.  OCaml has a collector; this has an arena that is
               never returned.  Fine for a program that finishes, wrong for
               a server; say so where it matters.

Contracts: `[@@requires e]` and `[@@ensures e]` after a function become
`#[requires(..)]` / `#[ensures(..)]`, with exact arithmetic (a
specification states the mathematical value); `rustprove` proves them about
the Rust, and that no `ml_wrap` in it ever wraps.  `[@@variant e]` on a
recursive function becomes `#[variant(e)]`: the measure each recursive call
-- or each `continue` of a tail-recursive loop -- must decrease.

What it refuses, by name: partial application, a closure that captures a
variable used as a value (a closed `fun` is lifted and passed as a `fn`),
`=` on anything but ints and bools, an enum inside a tuple payload
(`of ('a tree * int)`; write `of 'a tree * int`), and every construct the
front end refuses.  Integers are OCaml's 63-bit ones: each `+`, `-` and `*` is
reduced to 63 bits and sign-extended (`ml_wrap`), which is exact.  `/` and
`mod` truncate toward zero, as OCaml's do; dividing by zero is a machine
fault here where OCaml raises `Division_by_zero`.

    python3 ocaml2rust.py FILE.ml           # print the Rust
    python3 ocaml2rust.py FILE.ml -o OUT.rs
"""
import sys

import ocaml as O


class LowerError(Exception):
    def __init__(self, message, line=None):
        self.line = line
        super().__init__("line %d: %s" % (line, message) if line else message)


BUILTINS = {'print_int', 'print_newline', 'not', 'fst', 'snd'}
INT_CONSTANTS = {'min_int': -(1 << 62), 'max_int': (1 << 62) - 1}
RUST_CTOR = {'[]': 'Nil', '::': 'Cons'}


class Ctx:
    """What names mean at one point of one function instance."""

    def __init__(self, sub, locals_=None, local_fns=None, in_main=False):
        self.sub = sub                          # TVar id -> concrete type
        self.locals = dict(locals_ or {})       # name -> (rust expr, type)
        self.local_fns = dict(local_fns or {})  # name -> LocalFn
        self.in_main = in_main
        self.tail = None                        # set while compiling a loop
        self.contract = False                   # a clause: exact arithmetic

    def child(self):
        c = Ctx(self.sub, self.locals, self.local_fns, self.in_main)
        c.tail = self.tail
        c.contract = self.contract
        return c


class LocalFn:
    def __init__(self, d, captures, sub, local_fns, scheme):
        self.d, self.captures, self.sub = d, captures, sub
        self.local_fns, self.scheme = local_fns, scheme


class Emitter:
    def __init__(self, items, checker):
        self.items, self.c = items, checker
        self.enums = {}                 # rust name -> (TCon, [(v, fields)])
        self.enum_order = []
        self.fns = []                   # emitted function texts
        self.instances = {}             # key -> rust name
        self.queue = []
        self.counter = 0
        self.globals = {}               # name -> ('fn'|'thunk'|'main', def)
        self.main_types = {}

    # -- names --
    def fresh(self, base):
        self.counter += 1
        return '_ml_%s%d' % (base, self.counter)

    @staticmethod
    def var(name):
        return 'ml_' + name.replace("'", '_q')

    # -- types --
    def apply(self, t, sub):
        t = O.prune(t)
        if isinstance(t, O.TVar):
            if t.id in sub:
                return self.apply(sub[t.id], sub)
            return O.INT                # never constrained: any type will do
        return O.TCon(t.name, [self.apply(a, sub) for a in t.args])

    def mangle(self, t):
        t = O.prune(t)
        if t.name == '*':
            return 'T%d_%s_E' % (len(t.args), '_'.join(self.mangle(a)
                                                       for a in t.args))
        if t.name == '->':
            args, res = self.uncurry(t)
            return 'F%d_%s_%s_E' % (len(args), '_'.join(self.mangle(a)
                                                        for a in args),
                                    self.mangle(res))
        if not t.args:
            return t.name
        return '%s_%s' % (t.name, '_'.join(self.mangle(a) for a in t.args))

    @staticmethod
    def uncurry(t):
        args = []
        t = O.prune(t)
        while isinstance(t, O.TCon) and t.name == '->':
            args.append(O.prune(t.args[0]))
            t = O.prune(t.args[1])
        return args, t

    @staticmethod
    def is_unit(t):
        t = O.prune(t)
        return isinstance(t, O.TCon) and t.name == 'unit'

    def is_enum(self, t):
        t = O.prune(t)
        return isinstance(t, O.TCon) and (t.name == 'list'
                                          or t.name in self.c.typedefs)

    def rt(self, t, line=0):
        """The Rust type of a concrete type."""
        t = O.prune(t)
        if t.name == 'int':
            return 'ml_int'
        if t.name == 'bool':
            return 'bool'
        if t.name == 'unit':
            return '()'
        if t.name == '*':
            return '(%s)' % ', '.join(self.rt(a, line) for a in t.args)
        if t.name == '->':
            args, res = self.uncurry(t)
            params = ', '.join(self.rt(a, line) for a in args
                               if not self.is_unit(a))
            ret = '' if self.is_unit(res) else ' -> %s' % self.rt(res, line)
            return 'fn(%s)%s' % (params, ret)
        return self.enum(t, line)

    def enum(self, t, line=0):
        """The mono enum for a variant type at concrete arguments."""
        name = 'ml_' + self.mangle(t)
        if name in self.enums:
            return name
        self.enums[name] = None                 # recursion stops here
        variants = []
        if t.name == 'list':
            variants = [('Nil', []), ('Cons', [t.args[0], t])]
        else:
            td = self.c.typedefs[t.name]
            sub = dict(zip(td.type_vars, t.args))
            for v in td.variants:
                fields = [self.c.from_ast(a, dict(sub), v.line)
                          for a in v.of_types]
                for f in fields:
                    self.check_payload(f, v)
                variants.append((v.name, fields))
        self.enums[name] = (t, variants)
        self.enum_order.append(name)
        for _, fields in variants:
            for f in fields:
                self.rt(f, line)                # every payload type exists
        return name

    def check_payload(self, t, v):
        t = O.prune(t)
        if isinstance(t, O.TCon) and t.name == '*':
            for a in t.args:
                if self.is_enum(a) or (isinstance(O.prune(a), O.TCon) and
                                       O.prune(a).name == '*' and
                                       self.contains_enum(a)):
                    raise LowerError(
                        "constructor `%s` holds a variant inside a tuple; "
                        "write its arguments `of a * b` rather than "
                        "`of (a * b)`" % v.name, v.line)

    def contains_enum(self, t):
        t = O.prune(t)
        return self.is_enum(t) or (isinstance(t, O.TCon) and t.name == '*'
                                   and any(self.contains_enum(a)
                                           for a in t.args))

    def ctor_fields(self, cname, t):
        """Concrete field types of constructor `cname` at variant type t."""
        e = self.enums[self.enum(t)]
        rust = RUST_CTOR.get(cname, cname)
        for v, fields in e[1]:
            if v == rust:
                return rust, fields
        raise LowerError("no constructor %s" % cname)

    # -- enums, rendered --
    def render_enums(self):
        out = []
        for name in self.enum_order:
            t, variants = self.enums[name]
            vs = []
            for v, fields in variants:
                if fields:
                    vs.append('%s(%s)' % (v, ', '.join(
                        self.field_rt(f) for f in fields)))
                else:
                    vs.append(v)
            out.append('enum %s { %s }' % (name, ', '.join(vs)))
            if not variants:
                continue
            out.append('fn ml_box_%s(v: %s) -> *mut %s { let p: *mut %s = '
                       'malloc(size_of::<%s>()) as *mut %s; p[0] = v; p }'
                       % ((name,) * 6))
            for v, fields in variants:
                wild = '(%s)' % ', '.join('_' for _ in fields) if fields \
                    else ''
                other = ', _ => false' if len(variants) > 1 else ''
                out.append('fn ml_is_%s_%s(v: %s) -> bool { match v { '
                           '%s::%s%s => true%s } }'
                           % (name, v, name, name, v, wild, other))
                for k, f in enumerate(fields):
                    pat = ', '.join('x' if i == k else '_'
                                    for i in range(len(fields)))
                    get = 'x[0]' if self.is_enum(f) else 'x'
                    other = ', _ => panic!("Match_failure")' \
                        if len(variants) > 1 else ''
                    out.append('fn ml_%s_%s_%d(v: %s) -> %s { match v { '
                               '%s::%s(%s) => %s%s } }'
                               % (name, v, k, name, self.rt(f), name, v,
                                  pat, get, other))
        return out

    def field_rt(self, f):
        return '*mut %s' % self.rt(f) if self.is_enum(f) else self.rt(f)

    # -- instances --
    def instance(self, key, make_name, job):
        if key not in self.instances:
            self.instances[key] = make_name()
            self.queue.append((self.instances[key], job))
        return self.instances[key]

    def global_fn(self, name, types, line):
        kind, d = self.globals[name]
        scheme = d.scheme
        key = ('g', name, tuple(self.mangle(t) for t in types))
        suffix = '__' + '_'.join(self.mangle(t) for t in types) \
            if types else ''

        def job():
            sub = {q.id: t for q, t in zip(scheme.quantified, types)}
            if kind == 'thunk':
                return self.function(self.instances[key], [], d.value,
                                     d.ty, Ctx(sub), [], thunk=True)
            params, body = self.params_of(d)
            return self.function(self.instances[key], params, body, d.ty,
                                 Ctx(sub), [], me=('g', name, key),
                                 contracts=d.contracts)
        return self.instance(key, lambda: self.var(name) + suffix, job)

    @staticmethod
    def params_of(d):
        if d.params:
            return d.params, d.value
        if isinstance(d.value, O.Fun):
            return d.value.params, d.value.body
        return [], d.value

    def drain(self):
        while self.queue:
            name, job = self.queue.pop(0)
            self.fns.append(job())

    # -- functions --
    def function(self, name, params, body, ty, ctx, captures, thunk=False,
                 me=None, contracts=()):
        """One Rust `fn`.  `me` identifies the OCaml function being compiled
        -- ('g', name) or ('l', LocalFn) -- so that a call to itself in tail
        position becomes a jump: the body is a `loop`, the parameters mutable
        locals, and the call assigns them and `continue`s.  OCaml writes its
        loops as tail recursion; they must run in constant stack."""
        ctx = ctx.child()
        ctx.locals = {}
        lines, rparams = [], []
        looping = me is not None and self.tail_calls_self(body, me, params,
                                                          ctx)
        for cname, ctype in captures:
            rparams.append('%s: %s' % (self.var(cname), self.rt(ctype)))
            ctx.locals[cname] = (self.var(cname), ctype)
        t = self.apply(ty, ctx.sub)
        arg_types, _ = self.uncurry(t)
        slots, inits, binds = [], [], []
        for (p, _), at in zip(params, arg_types):
            if self.is_unit(at):
                slots.append(None)
                continue
            if isinstance(p, O.Variable):
                slot = self.var(p.name)
                ctx.locals[p.name] = (slot, at)
            else:
                slot = self.fresh('p')
                binds.extend(self.bind_pattern(p, slot, at, ctx))
            slots.append(slot)
            if looping:
                rparams.append('%s_in: %s' % (slot, self.rt(at)))
                inits.append('let mut %s: %s = %s_in; ' % (slot, self.rt(at),
                                                          slot))
            else:
                rparams.append('%s: %s' % (slot, self.rt(at)))
        res = t
        for _ in params:
            res = O.prune(res).args[1]
        ret = '' if self.is_unit(res) else ' -> %s' % self.rt(res)
        attrs = self.contract_attrs(contracts, params, slots, ctx, looping)
        if looping:
            ctx.tail = (me, slots, [a for a in arg_types], self.is_unit(res))
            code = self.tail(body, ctx)
            return '%sfn %s(%s)%s { %sloop { %s%s } }' % (
                attrs, name, ', '.join(rparams), ret, ''.join(inits),
                ''.join(binds), code)
        lines = binds
        body_code = self.expr(body, ctx)
        if self.is_unit(res):
            text = attrs + 'fn %s(%s) { %s%s}' % (name, ', '.join(rparams),
                                          ''.join(lines),
                                          self.stmt(body_code))
        else:
            text = attrs + 'fn %s(%s)%s { %s%s }' % (name, ', '.join(rparams), ret,
                                             ''.join(lines), body_code)
        return text

    def contract_attrs(self, contracts, params, slots, ctx, looping):
        """`#[requires(..)]` / `#[ensures(..)]` for `[@@requires ..]` and
        `[@@ensures ..]`: the clauses read the parameters by their Rust
        names (a looping function's are the `_in` ones), and `result`."""
        if not contracts:
            return ''
        cctx = ctx.child()
        cctx.contract = True
        cctx.tail = None
        for (p, _), slot in zip(params, slots):
            if isinstance(p, O.Variable) and slot is not None:
                cctx.locals[p.name] = (slot + ('_in' if looping else ''),
                                       cctx.locals[p.name][1])
        out = []
        for kind, clause in contracts:
            if kind == 'ensures':
                cctx.locals['result'] = ('result', None)
            # each on a line of its own: Crust loses a function whose
            # attributes share its line
            out.append('#[%s(%s)]\n' % (kind, self.expr(clause, cctx)))
        return ''.join(out)

    def bind_pattern(self, p, code, t, ctx):
        """`let` statements binding an irrefutable pattern to `code`."""
        t = O.prune(t)
        if isinstance(p, O.Variable):
            ctx.locals[p.name] = (self.var(p.name), t)
            if self.is_unit(t):
                return [self.stmt(code)]
            return ['let %s: %s = %s; ' % (self.var(p.name), self.rt(t),
                                           code)]
        if isinstance(p, (O.Wildcard, O.Const)):
            return [self.stmt(code)]
        if isinstance(p, O.TupleNode):
            tmp = self.fresh('t')
            out = ['let %s: %s = %s; ' % (tmp, self.rt(t), code)]
            for k, (sub, st) in enumerate(zip(p.elements, t.args)):
                out.extend(self.bind_pattern(sub, '%s.%d' % (tmp, k), st,
                                             ctx))
            return out
        if isinstance(p, O.ConstructorPat):
            tmp = self.fresh('c')
            out = ['let %s: %s = %s; ' % (tmp, self.rt(t), code)]
            rust, fields = self.ctor_fields(p.name, t)
            ename = self.enum(t)
            for k, (sub, ft) in enumerate(zip(p.args, fields)):
                out.extend(self.bind_pattern(
                    sub, 'ml_%s_%s_%d(%s)' % (ename, rust, k, tmp), ft, ctx))
            return out
        raise LowerError("pattern not supported here", p.line)

    @staticmethod
    def stmt(code):
        """An expression run for its effect, as a statement.  A block or an
        `if` is a statement already; Crust reads a `;` after one as the
        start of an empty expression."""
        if code == '()':
            return ''
        if code.startswith('{') or code.startswith('if '):
            return code + ' '
        return code + '; '

    # -- expressions --
    def expr(self, e, ctx):
        m = getattr(self, 'x_' + type(e).__name__, None)
        if m is None:
            raise LowerError("not supported: %s" % type(e).__name__,
                             getattr(e, 'line', 0))
        return m(e, ctx)

    def x_Const(self, e, ctx):
        v = e.value
        if v == ():
            return '()'
        if isinstance(v, bool):
            return 'true' if v else 'false'
        return '%di64' % v if v >= 0 else '(0i64 - %di64)' % -v

    def x_Annot(self, e, ctx):
        return self.expr(e.expr, ctx)

    def x_Seq(self, e, ctx):
        return '{ %s%s }' % (self.stmt(self.expr(e.first, ctx)),
                             self.expr(e.second, ctx))

    def x_IfThen(self, e, ctx):
        return 'if %s { %s } else { %s }' % (self.expr(e.cond, ctx),
                                             self.expr(e.then, ctx),
                                             self.expr(e.other, ctx))

    def x_TupleNode(self, e, ctx):
        return '(%s)' % ', '.join(self.expr(x, ctx) for x in e.elements)

    def x_BinOp(self, e, ctx):
        op = e.op
        a, b = self.expr(e.left, ctx), self.expr(e.right, ctx)
        if op in ('=', '<>', '<', '>', '<=', '>='):
            t = self.apply(e.left.ty, ctx.sub)
            if t.name not in ('int', 'bool'):
                raise LowerError("`%s` on %s: only ints and bools compare "
                                 "in this subset" % (op, O.show(t)), e.line)
            op = {'=': '==', '<>': '!='}.get(op, op)
        elif op == 'mod':
            op = '%'
        elif op == '/' and not ctx.contract:
            # min_int / -1 is 2^62, which OCaml wraps to min_int
            return 'ml_wrap(%s / %s)' % (a, b)
        elif op in ('+', '-', '*') and ctx.contract:
            # a contract states the mathematical value; the lift proves
            # the code's arithmetic stays where the two agree
            return '(%s %s %s)' % (a, op, b)
        elif op in ('+', '-', '*'):
            # OCaml's ints are 63 bits: the i64 result, reduced to 63 and
            # sign-extended, is exact -- 2^63 divides 2^64, so even a
            # product that wrapped the i64 reduces to OCaml's answer
            return 'ml_wrap(%s %s %s)' % (a, op, b)
        return '(%s %s %s)' % (a, op, b)

    def x_ConstructorApp(self, e, ctx):
        t = self.apply(e.ty, ctx.sub)
        ename = self.enum(t, e.line)
        rust, fields = self.ctor_fields(e.name, t)
        if not fields:
            return '%s::%s' % (ename, rust)
        args = []
        for a, ft in zip(e.args, fields):
            code = self.expr(a, ctx)
            if self.is_enum(ft):
                code = 'ml_box_%s(%s)' % (self.enum(ft), code)
            args.append(code)
        return '%s::%s(%s)' % (ename, rust, ', '.join(args))

    def x_Refute(self, e, ctx):
        return 'panic!("unreachable")'

    def x_Variable(self, e, ctx):
        name = e.name
        if name in ctx.locals:
            return ctx.locals[name][0]
        if name in ctx.local_fns:
            lf = ctx.local_fns[name]
            if lf.captures:
                raise LowerError("`%s` captures %s and is used as a value; "
                                 "a closure is not in the subset yet"
                                 % (name, ', '.join(c for c, _ in
                                                    lf.captures)), e.line)
            return self.local_fn(lf, e, ctx)
        if name in self.globals:
            kind, d = self.globals[name]
            if kind == 'main':
                if not ctx.in_main:
                    raise LowerError(
                        "`%s` is computed when the program starts, and a "
                        "function uses it; make it a function or a value"
                        % name, e.line)
                return self.var(name)
            inst = self.global_fn(name, self.types_at(e, d.scheme, ctx),
                                  e.line)
            return inst + '()' if kind == 'thunk' else inst
        if name in INT_CONSTANTS:
            return self.x_Const(O.Const(INT_CONSTANTS[name]), ctx)
        if name in BUILTINS:
            raise LowerError("`%s` must be applied" % name, e.line)
        raise LowerError("unbound `%s`" % name, e.line)

    def types_at(self, e, scheme, ctx):
        """The concrete types a use instantiates a scheme at."""
        if scheme is None or not scheme.quantified:
            return []
        inst = getattr(e, 'inst', None) or []
        if len(inst) == len(scheme.quantified):
            return [self.apply(t, ctx.sub) for t in inst]
        # a recursive use inside its own body: the same instance
        return [self.apply(q, ctx.sub) for q in scheme.quantified]

    def x_Application(self, e, ctx):
        f = e.func
        args = e.args
        if isinstance(f, O.Variable) and f.name in BUILTINS and \
                f.name not in ctx.locals and f.name not in ctx.local_fns \
                and f.name not in self.globals:
            a = self.expr(args[0], ctx)
            if f.name == 'print_int':
                code = 'print!("{}", %s)' % a
            elif f.name == 'print_newline':
                code = 'println!()'
            elif f.name == 'not':
                code = '(!%s)' % a
            elif f.name == 'fst':
                code = '(%s).0' % a
            else:
                code = '(%s).1' % a
            rest = args[1:]
            return self.apply_rest(code, rest, e, ctx) if rest else code
        arity = self.arity_of(f, ctx)
        if arity is None:
            ft = self.apply(f.ty, ctx.sub)
            arity = len(self.uncurry(ft)[0])
        if len(args) < arity:
            raise LowerError("partial application (%d of %d arguments) is "
                             "not in the subset yet" % (len(args), arity),
                             e.line)
        head = self.expr(f, ctx) if not (isinstance(f, O.Variable) and
                                         f.name in ctx.local_fns) \
            else self.local_fn(ctx.local_fns[f.name], f, ctx)
        pre = []
        if isinstance(f, O.Variable) and f.name in ctx.local_fns:
            pre = [ctx.locals[c][0] if c in ctx.locals else self.var(c)
                   for c, _ in ctx.local_fns[f.name].captures]
        now, rest = args[:arity], args[arity:]
        code = '%s(%s)' % (head if not head.endswith(')') or
                           isinstance(f, O.Variable) else '(%s)' % head,
                           ', '.join(pre + self.args(now, ctx)))
        return self.apply_rest(code, rest, e, ctx) if rest else code

    def apply_rest(self, code, rest, e, ctx):
        """Apply the function value `code` to more arguments."""
        while rest:
            t = self.apply(rest[0].ty, ctx.sub)
            del t
            k = len(rest)
            code = '(%s)(%s)' % (code, ', '.join(self.args(rest[:k], ctx)))
            rest = rest[k:]
        return code

    def args(self, args, ctx):
        out = []
        for a in args:
            if self.is_unit(self.apply(a.ty, ctx.sub)):
                if not (isinstance(a, O.Const) and a.value == ()):
                    raise LowerError("a unit argument other than `()`",
                                     a.line)
                continue
            out.append(self.expr(a, ctx))
        return out

    def arity_of(self, f, ctx):
        """How many arguments a named function takes at once."""
        if not isinstance(f, O.Variable):
            return None
        d = None
        if f.name in ctx.local_fns:
            d = ctx.local_fns[f.name].d
        elif f.name in self.globals and f.name not in ctx.locals:
            kind, d = self.globals[f.name]
            if kind != 'fn':
                return None
        if d is None:
            return None
        params, _ = self.params_of(d)
        return len(params)

    def x_Fun(self, e, ctx):
        free = self.free_vars(e, set(), ctx)
        if free:
            raise LowerError("this `fun` captures %s; a closure used as a "
                             "value is not in the subset yet"
                             % ', '.join(sorted(free)), e.line)
        t = self.apply(e.ty, ctx.sub)
        key = ('lam', id(e), self.mangle(t))
        sub = dict(ctx.sub)
        local_fns = ctx.local_fns
        return self.instance(
            key, lambda: self.fresh('lambda').lstrip('_'),
            lambda: self.function(self.instances[key], e.params, e.body,
                                  e.ty, Ctx(sub, {}, local_fns), []))

    def x_LetIn(self, e, ctx):
        group = e.group
        inner = ctx.child()
        is_fn = lambda d: d.params or isinstance(d.value, O.Fun)
        if group.rec or any(is_fn(d) for d in group.defs):
            if not all(is_fn(d) for d in group.defs):
                raise LowerError("`let rec` of a value is not in the subset",
                                 e.line)
            names = {d.pattern.name for d in group.defs}
            caps = set()
            for d in group.defs:
                params, body = self.params_of(d)
                bound = set(names) if group.rec else set()
                for p, _ in params:
                    bound |= set(O.pattern_names(p))
                caps |= self.free_vars(body, bound, ctx)
            for name in list(caps):             # a called local's captures
                if name in ctx.local_fns:
                    caps.discard(name)
                    caps |= {c for c, _ in ctx.local_fns[name].captures}
            captures = sorted((c, ctx.locals[c][1]) for c in caps
                              if c in ctx.locals)
            scope = dict(ctx.local_fns)
            for d in group.defs:
                lf = LocalFn(d, captures, dict(ctx.sub), scope, d.scheme)
                scope[d.pattern.name] = lf
                inner.local_fns[d.pattern.name] = lf
            if not group.rec:
                for d in group.defs:
                    ctx.local_fns.get(d.pattern.name)
            return self.expr(e.body, inner)
        lines = []
        for d in group.defs:
            code = self.expr(d.value, ctx)
            t = self.apply(d.ty, ctx.sub)
            lines.extend(self.bind_pattern(d.pattern, code, t, inner))
        return '{ %s%s }' % (''.join(lines), self.expr(e.body, inner))

    def local_fn(self, lf, e, ctx):
        types = self.types_at(e, lf.scheme, Ctx(ctx.sub))
        key = ('l', id(lf.d), tuple(self.mangle(t) for t in types),
               tuple(sorted((k, self.mangle(v)) for k, v in lf.sub.items()
                            if not isinstance(O.prune(v), O.TVar))))
        sub = dict(lf.sub)
        if lf.scheme is not None:
            sub.update({q.id: t for q, t in zip(lf.scheme.quantified,
                                                types)})

        def job():
            params, body = self.params_of(lf.d)
            local_fns = dict(lf.local_fns)
            local_fns[lf.d.pattern.name] = lf
            return self.function(self.instances[key], params, body, lf.d.ty,
                                 Ctx(sub, {}, local_fns),
                                 [(c, self.apply(t, sub))
                                  for c, t in lf.captures],
                                 me=('l', lf, key),
                                 contracts=lf.d.contracts)
        return self.instance(
            key, lambda: self.fresh(lf.d.pattern.name).lstrip('_'), job)

    def x_MatchWith(self, e, ctx):
        t = self.apply(e.match_expr.ty, ctx.sub)
        tmp = self.fresh('m')
        head = 'let %s: %s = %s; ' % (tmp, self.rt(t),
                                      self.expr(e.match_expr, ctx)) \
            if not self.is_unit(t) else self.stmt(self.expr(e.match_expr,
                                                             ctx))
        return '{ %s%s }' % (head, self.arms(e.branches, tmp, t, ctx))

    def arms(self, branches, tmp, t, ctx, body=None):
        body_of = body or self.expr
        if not branches:
            return 'panic!("Match_failure")'
        br = branches[0]
        inner = ctx.child()
        tests, binds = self.pattern(br.pattern, tmp, t, inner)
        code = body_of(br.body, inner)
        rest = branches[1:]
        if br.guard is not None:
            guard = self.expr(br.guard, inner)
            code = 'if %s { %s } else { %s }' % (
                guard, code, self.arms(rest, tmp, t, ctx, body))
        block = '{ %s%s }' % (''.join(binds), code)
        if not tests and br.guard is None:
            return block
        cond = ' && '.join(tests) or 'true'
        return 'if %s %s else { %s }' % (cond, block,
                                         self.arms(rest, tmp, t, ctx, body))

    def pattern(self, p, path, t, ctx):
        """(tests, bindings) for matching `p` at `path` of type `t`."""
        t = O.prune(t)
        if isinstance(p, O.Wildcard):
            return [], []
        if isinstance(p, O.Variable):
            ctx.locals[p.name] = (self.var(p.name), t)
            if self.is_unit(t):
                return [], []
            return [], ['let %s: %s = %s; ' % (self.var(p.name),
                                               self.rt(t), path)]
        if isinstance(p, O.Const):
            if p.value == ():
                return [], []
            return ['(%s == %s)' % (path, self.x_Const(p, ctx))], []
        if isinstance(p, O.TupleNode):
            tests, binds = [], []
            for k, (sub, st) in enumerate(zip(p.elements, t.args)):
                a, b = self.pattern(sub, '%s.%d' % (path, k), st, ctx)
                tests += a
                binds += b
            return tests, binds
        if isinstance(p, O.ConstructorPat):
            ename = self.enum(t, p.line)
            rust, fields = self.ctor_fields(p.name, t)
            tests = ['ml_is_%s_%s(%s)' % (ename, rust, path)]
            binds = []
            for k, (sub, ft) in enumerate(zip(p.args, fields)):
                a, b = self.pattern(sub, 'ml_%s_%s_%d(%s)'
                                    % (ename, rust, k, path), ft, ctx)
                tests += a
                binds += b
            return tests, binds
        raise LowerError("pattern not supported", p.line)

    # -- tail calls --
    def is_self_call(self, e, me, ctx):
        """True if `e` calls the function being compiled, fully applied,
        at the same instance."""
        if not isinstance(e, O.Application) or \
                not isinstance(e.func, O.Variable):
            return False
        f = e.func
        kind, who, key = me
        if kind == 'g':
            if f.name != who or f.name in ctx.locals or \
                    f.name in ctx.local_fns:
                return False
            _, d = self.globals[who]
            types = self.types_at(f, d.scheme, ctx)
            same = ('g', who, tuple(self.mangle(t) for t in types)) == key
        else:
            if ctx.local_fns.get(f.name) is not who or f.name in ctx.locals:
                return False
            types = self.types_at(f, who.scheme, Ctx(ctx.sub))
            same = key[2] == tuple(self.mangle(t) for t in types)
        params, _ = self.params_of(who[1] if kind == 'g' and False else
                                   (self.globals[who][1] if kind == 'g'
                                    else who.d))
        return same and len(e.args) == len(params)

    def tail_calls_self(self, e, me, params, ctx):
        """Does `e` call the function being compiled in a tail position?"""
        if isinstance(e, O.IfThen):
            return self.tail_calls_self(e.then, me, params, ctx) or \
                self.tail_calls_self(e.other, me, params, ctx)
        if isinstance(e, O.MatchWith):
            return any(self.tail_calls_self(br.body, me, params, ctx)
                       for br in e.branches)
        if isinstance(e, O.LetIn):
            return self.tail_calls_self(e.body, me, params, ctx)
        if isinstance(e, O.Seq):
            return self.tail_calls_self(e.second, me, params, ctx)
        if isinstance(e, O.Annot):
            return self.tail_calls_self(e.expr, me, params, ctx)
        return self.is_self_call(e, me, ctx)

    def tail(self, e, ctx):
        """`e` in tail position of a looping function, as statements that
        `return` its value or jump back to the top."""
        me, slots, arg_types, unit = ctx.tail
        if isinstance(e, O.IfThen):
            return 'if %s { %s } else { %s }' % (
                self.expr(e.cond, ctx), self.tail(e.then, ctx),
                self.tail(e.other, ctx))
        if isinstance(e, O.Seq):
            return '%s%s' % (self.stmt(self.expr(e.first, ctx)),
                             self.tail(e.second, ctx))
        if isinstance(e, O.Annot):
            return self.tail(e.expr, ctx)
        if isinstance(e, O.MatchWith):
            t = self.apply(e.match_expr.ty, ctx.sub)
            tmp = self.fresh('m')
            head = 'let %s: %s = %s; ' % (
                tmp, self.rt(t), self.expr(e.match_expr, ctx)) \
                if not self.is_unit(t) else self.stmt(
                    self.expr(e.match_expr, ctx))
            return '{ %s%s }' % (head, self.arms(e.branches, tmp, t, ctx,
                                                 body=self.tail))
        if isinstance(e, O.LetIn) and not (
                e.group.rec or any(d.params or isinstance(d.value, O.Fun)
                                   for d in e.group.defs)):
            inner = ctx.child()
            lines = []
            for d in e.group.defs:
                code = self.expr(d.value, ctx)
                lines.extend(self.bind_pattern(
                    d.pattern, code, self.apply(d.ty, ctx.sub), inner))
            return '{ %s%s }' % (''.join(lines), self.tail(e.body, inner))
        if self.is_self_call(e, me, ctx):
            temps, assigns = [], []
            for a, slot, at in zip(e.args, slots, arg_types):
                if slot is None:
                    continue
                tmp = self.fresh('a')
                temps.append('let %s: %s = %s; ' % (tmp, self.rt(at),
                                                     self.expr(a, ctx)))
                assigns.append('%s = %s; ' % (slot, tmp))
            return '{ %s%scontinue; }' % (''.join(temps), ''.join(assigns))
        code = self.expr(e, ctx)
        if unit:
            return '{ %sreturn; }' % self.stmt(code)
        return 'return %s;' % code

    # -- free variables --
    def free_vars(self, e, bound, ctx):
        """Local names `e` uses that are neither bound in it nor global."""
        out = set()

        def pat(p):
            return set(O.pattern_names(p))

        def go(e, bound):
            if isinstance(e, O.Variable):
                if e.name not in bound and (e.name in ctx.locals or
                                            e.name in ctx.local_fns):
                    out.add(e.name)
            elif isinstance(e, O.Fun):
                b = set(bound)
                for p, _ in e.params:
                    b |= pat(p)
                go(e.body, b)
            elif isinstance(e, O.LetIn):
                names = set()
                for d in e.group.defs:
                    names |= pat(d.pattern)
                for d in e.group.defs:
                    b = set(bound) | (names if e.group.rec else set())
                    for p, _ in d.params:
                        b |= pat(p)
                    go(d.value, b)
                go(e.body, set(bound) | names)
            elif isinstance(e, O.MatchWith):
                go(e.match_expr, bound)
                for br in e.branches:
                    b = set(bound) | pat(br.pattern)
                    if br.guard is not None:
                        go(br.guard, b)
                    go(br.body, b)
            else:
                for child in children(e):
                    go(child, bound)
        go(e, set(bound))
        return out

    # -- program --
    def program(self):
        main = []
        mctx = Ctx({}, in_main=True)
        for item in self.items:
            if isinstance(item, O.TypeDef):
                continue
            for d in item.defs:
                is_fn = d.params or isinstance(d.value, O.Fun)
                name = d.pattern.name if isinstance(d.pattern, O.Variable) \
                    else None
                if is_fn:
                    if name is None:
                        raise LowerError("a function must be named", d.line)
                    self.globals[name] = ('fn', d)
                elif name is not None and O.is_value(d.value) and \
                        not item.rec:
                    self.globals[name] = ('thunk', d)
                else:
                    if item.rec:
                        raise LowerError("`let rec` of a value", d.line)
                    t = self.apply(d.ty, {})
                    code = self.expr(d.value, mctx)
                    main.extend(self.bind_pattern(d.pattern, code, t, mctx))
                    for n in O.pattern_names(d.pattern):
                        self.globals[n] = ('main', d)
            self.drain()
        self.drain()
        out = ['void *malloc(unsigned long);',
               # OCaml's `int`: an i64 whose values stay in 63 bits, by name
               # so the proof lift knows the range it may assume and owes
               'type ml_int = i64;',
               # deep non-tail recursion, as OCaml allows it: raise the soft
               # stack limit to the hard one; Linux grows the main stack
               # against the limit in force when it faults, not at exec
               'struct ml_rlimit { unsigned long cur; unsigned long max; };',
               'int getrlimit(int, struct ml_rlimit *);',
               'int setrlimit(int, const struct ml_rlimit *);',
               'static void ml_grow_stack(void) { struct ml_rlimit r; '
               'if (getrlimit(3, &r) == 0) { r.cur = r.max; '
               'setrlimit(3, &r); } }',
               'fn ml_wrap(v: i64) -> i64 { (v << 1) >> 1 }']
        out += self.render_enums()
        out += self.fns
        out.append('fn main() { ml_grow_stack(); %s}' % ''.join(main))
        return '\n'.join(out) + '\n'


def children(e):
    for attr in ('func', 'match_expr', 'cond', 'then', 'other', 'first',
                 'second', 'left', 'right', 'expr', 'body'):
        v = getattr(e, attr, None)
        if isinstance(v, O.Node):
            yield v
    for attr in ('args', 'elements'):
        for v in getattr(e, attr, None) or []:
            if isinstance(v, O.Node):
                yield v


def lower(code):
    """The Rust for an OCaml program."""
    items, c = O.check(code, strict=False)
    return Emitter(items, c).program()


def main(argv):
    out = None
    if '-o' in argv:
        i = argv.index('-o')
        out = argv[i + 1]
        del argv[i:i + 2]
    with open(argv[0]) as fh:
        code = fh.read()
    try:
        rust = lower(code)
    except (O.CompileError, LowerError) as exc:
        print('%s: %s' % (argv[0], exc), file=sys.stderr)
        return 1
    if out:
        with open(out, 'w') as fh:
            fh.write(rust)
    else:
        sys.stdout.write(rust)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
