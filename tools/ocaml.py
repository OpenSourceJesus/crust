#!/usr/bin/env python3
r"""ocaml.py -- an OCaml subset for Crust: lexer, parser, type inference.

The front end every OCaml backend shares.  Grown from the sketch that started
it (its AST names are kept), with the pieces a backend needs added: every
node carries its line, operators parse by precedence, and every expression
gets a type by Hindley-Milner inference -- Algorithm W, with let-polymorphism
-- so that a backend knows what each value *is*: a layout for code, a
proposition for a proof (`ocamlproof.py`).

What the subset has:

  type definitions   variants `A | B of t | C of t1 * t2`, parameterised
                     (`type ('a, 'b) t = ...`), recursive, and empty
                     (`type void = |`); aliases `type t = u`
  let                `let`, `let rec`, `let .. and ..`, with parameters
                     `x`, `(x : t)`, `()`, `_`, and a result annotation
  expressions        integers, `true`/`false`, `()`, variables, application,
                     constructors, tuples, lists (`[]`, `x :: xs`, `[a; b]`),
                     `fun`, `function`, `match`, `if`, `let .. in`,
                     `+ - * / mod`, `= <> < > <= >=`, `&& ||`, unary `-`,
                     `(e : t)`
  patterns           `_`, variables, integer and boolean literals, `()`,
                     tuples, constructors, `[]`, `::`, `when` guards, and
                     the refutation arm `_ -> .`

What it refuses, by name: records, `ref`/`:=`/`!`, mutable arrays,
exceptions (`raise`, `try`), modules and functors, objects, labelled and
optional arguments, polymorphic variants, GADTs, strings and floats.  Each
refusal is a CompileError naming the construct and the line.

Types are OCaml's, with one tightening, in `check_annotations`: a type
variable written in an annotation is *rigid* -- `let f (x : 'a) : 'b = x`
is refused, where OCaml would quietly unify 'a with 'b and accept it.  An
annotation is a promise about what a definition is for; for a proof it is
the statement.  Backends that want OCaml's looser reading can skip the check.

    python3 ocaml.py FILE.ml        # print every top-level name and its type
"""
import re
import sys


class CompileError(Exception):
    def __init__(self, message, line=None):
        self.line = line
        super().__init__("line %d: %s" % (line, message) if line else message)


# ==========================================
# 1. AST
# ==========================================
class Node:
    line = 0
    ty = None                                   # set by inference


# --- types, as written ---
class TypeVar(Node):
    def __init__(self, name): self.name = name
    def __repr__(self): return "TypeVar(%s)" % self.name


class TypeTuple(Node):
    def __init__(self, types): self.types = types
    def __repr__(self): return "TypeTuple(%s)" % self.types


class TypeArrow(Node):
    def __init__(self, left, right):
        self.left, self.right = left, right
    def __repr__(self): return "TypeArrow(%s -> %s)" % (self.left, self.right)


class TypeApp(Node):
    def __init__(self, name, args):
        self.name, self.args = name, args
    def __repr__(self): return "TypeApp(%s, %s)" % (self.name, self.args)


# --- top level ---
class TypeDef(Node):
    def __init__(self, name, type_vars, variants, alias=None):
        self.name, self.type_vars = name, type_vars
        self.variants, self.alias = variants, alias
    def __repr__(self):
        return "TypeDef(%r, %s, %s)" % (self.name, self.type_vars,
                                        self.alias or self.variants)


class VariantDef(Node):
    def __init__(self, name, of_types):
        self.name = name
        self.of_types = of_types                # [] for a constant constructor
    def __repr__(self): return "Variant(%r, of=%s)" % (self.name, self.of_types)


class LetDef(Node):
    """`pattern params : ret_type = value`; a function when params exist."""
    def __init__(self, pattern, params, ret_type, value):
        self.pattern, self.params = pattern, params
        self.ret_type, self.value = ret_type, value
    def __repr__(self):
        return "LetDef(%s, params=%s, ret=%s, val=%s)" % (
            self.pattern, self.params, self.ret_type, self.value)


class LetGroup(Node):
    """One `let [rec] a = .. and b = ..` at the top level or before `in`."""
    def __init__(self, rec, defs):
        self.rec, self.defs = rec, defs
    def __repr__(self): return "LetGroup(rec=%s, %s)" % (self.rec, self.defs)


# --- expressions and patterns ---
class Variable(Node):
    def __init__(self, name): self.name = name
    def __repr__(self): return "Var(%r)" % self.name


class Const(Node):
    """An integer, `true`/`false`, or `()`."""
    def __init__(self, value): self.value = value
    def __repr__(self): return "Const(%r)" % (self.value,)


class ConstructorApp(Node):
    def __init__(self, name, args):
        self.name, self.args = name, args       # [] for a constant
    def __repr__(self): return "Construct(%r, %s)" % (self.name, self.args)


class ConstructorPat(Node):
    def __init__(self, name, args):
        self.name, self.args = name, args
    def __repr__(self): return "PatConstruct(%r, %s)" % (self.name, self.args)


class TupleNode(Node):
    def __init__(self, elements): self.elements = elements
    def __repr__(self): return "Tuple(%s)" % self.elements


class LetIn(Node):
    def __init__(self, group, body):
        self.group, self.body = group, body
    def __repr__(self): return "LetIn(%s, %s)" % (self.group, self.body)


class Fun(Node):
    def __init__(self, params, body):
        self.params, self.body = params, body   # params: [(pattern, type)]
    def __repr__(self): return "Fun(%s, %s)" % (self.params, self.body)


class MatchWith(Node):
    def __init__(self, match_expr, branches):
        self.match_expr, self.branches = match_expr, branches
    def __repr__(self): return "MatchWith(%s, %s)" % (self.match_expr,
                                                     self.branches)


class MatchBranch(Node):
    def __init__(self, pattern, guard, body):
        self.pattern, self.guard, self.body = pattern, guard, body
    def __repr__(self): return "Branch(%s -> %s)" % (self.pattern, self.body)


class IfThen(Node):
    def __init__(self, cond, then, other):
        self.cond, self.then, self.other = cond, then, other


class Annot(Node):
    def __init__(self, expr, type_):
        self.expr, self.type_ = expr, type_


class Refute(Node):
    """The body `.` of a refutation arm: this case cannot happen."""
    def __repr__(self): return "Refute()"


class Wildcard(Node):
    def __repr__(self): return "Wildcard()"


class Application(Node):
    def __init__(self, func, args):
        self.func, self.args = func, args
    def __repr__(self): return "App(%s, %s)" % (self.func, self.args)


class BinOp(Node):
    def __init__(self, op, left, right):
        self.op, self.left, self.right = op, left, right
    def __repr__(self): return "BinOp(%r, %s, %s)" % (self.op, self.left,
                                                      self.right)


# ==========================================
# 2. Lexer
# ==========================================
KEYWORDS = {'let', 'rec', 'in', 'match', 'with', 'type', 'of', 'fun',
            'function', 'if', 'then', 'else', 'and', 'when', 'true', 'false',
            'mod', 'not', 'begin', 'end'}
REFUSED_WORDS = {
    'ref': 'references', 'raise': 'exceptions', 'try': 'exceptions',
    'exception': 'exceptions', 'module': 'modules', 'struct': 'modules',
    'sig': 'modules', 'functor': 'functors', 'open': 'modules',
    'object': 'objects', 'method': 'objects', 'new': 'objects',
    'class': 'objects', 'mutable': 'mutable records', 'while': 'loops',
    'for': 'loops', 'lazy': 'lazy values', 'assert': 'assertions',
    'external': 'external declarations', 'include': 'modules',
}


class Token:
    def __init__(self, type_, value, line):
        self.type, self.value, self.line = type_, value, line
    def __repr__(self): return "Token(%s, %r)" % (self.type, self.value)


_RULES = [
    ('TYPEVAR', r"'[a-z_][a-zA-Z0-9_']*"),
    ('INT',     r'[0-9][0-9_]*'),
    ('UID',     r'[A-Z][a-zA-Z0-9_\']*'),
    ('ID',      r'[a-z_][a-zA-Z0-9_\']*'),
    ('OP',      r'->|::|:=|<>|<=|>=|&&|\|\||;;|\[\]|\(\)|[-+*/=<>|:,;.()\[\]{}!~?@^&$%#`"]'),
]
_TOKEN = re.compile('|'.join('(?P<%s>%s)' % r for r in _RULES))


def tokenize(code):
    """Tokens with lines.  Comments `(* .. *)` nest, as OCaml's do; they are
    removed before anything else, so `(*` is never read as `(` then `*`."""
    tokens, i, line, n = [], 0, 1, len(code)
    while i < n:
        c = code[i]
        if c == '\n':
            line += 1
            i += 1
            continue
        if c in ' \t\r':
            i += 1
            continue
        if code.startswith('(*', i):
            depth, start = 1, line
            i += 2
            while i < n and depth:
                if code.startswith('(*', i):
                    depth += 1
                    i += 2
                elif code.startswith('*)', i):
                    depth -= 1
                    i += 2
                else:
                    line += code[i] == '\n'
                    i += 1
            if depth:
                raise CompileError("unterminated comment", start)
            continue
        if c == '"':
            raise CompileError("strings are not in the subset", line)
        m = _TOKEN.match(code, i)
        if not m:
            raise CompileError("unexpected character %r" % c, line)
        kind, value = m.lastgroup, m.group(m.lastgroup)
        if kind == 'INT' and code[m.end():m.end() + 1] == '.':
            raise CompileError("floats are not in the subset", line)
        if kind == 'ID':
            if value in REFUSED_WORDS:
                raise CompileError("`%s`: %s are not in the subset"
                                   % (value, REFUSED_WORDS[value]), line)
            if value in KEYWORDS:
                kind = 'KW'
        tokens.append(Token(kind, value, line))
        i = m.end()
    tokens.append(Token('EOF', '', line))
    return tokens


# ==========================================
# 3. Parser
# ==========================================
BINARY = [                                      # loosest first
    (['||'], 'right'), (['&&'], 'right'),
    (['=', '<>', '<', '>', '<=', '>='], 'left'),
    (['::'], 'right'),
    (['+', '-'], 'left'), (['*', '/', 'mod'], 'left'),
]
_STARTS_ATOM = ('ID', 'UID', 'INT', 'TYPEVAR')


class Parser:
    def __init__(self, tokens):
        self.tokens, self.pos = tokens, 0

    # -- plumbing --
    def cur(self):
        return self.tokens[self.pos]

    def at(self, *values):
        t = self.cur()
        return t.type in ('OP', 'KW') and t.value in values

    def at_type(self, *types):
        return self.cur().type in types

    def fail(self, message):
        raise CompileError(message, self.cur().line)

    def expect(self, value):
        if not self.at(value):
            self.fail("expected `%s`, found `%s`" % (value, self.cur().value
                                                     or 'end of file'))
        return self.next()

    def next(self):
        t = self.cur()
        self.pos += 1
        return t

    def node(self, n, line):
        n.line = line
        return n

    # -- program --
    def parse_program(self):
        items = []
        while not self.at_type('EOF'):
            if self.at(';;'):
                self.next()
                continue
            line = self.cur().line
            if self.at('type'):
                items.extend(self.parse_type_defs())
            elif self.at('let'):
                group = self.parse_let_group()
                if self.at('in'):
                    self.fail("a top-level `let .. in` expression is not in "
                              "the subset; bind it with `let () = ..`")
                items.append(group)
            elif self.at('{'):
                self.fail("records are not in the subset")
            else:
                self.fail("a top-level expression is not in the subset; "
                          "write `let () = ..`")
            del line
        return items

    # -- types --
    def parse_type(self):
        line = self.cur().line
        left = self.parse_type_tuple()
        if self.at('->'):
            self.next()
            return self.node(TypeArrow(left, self.parse_type()), line)
        return left

    def parse_type_tuple(self):
        line = self.cur().line
        parts = [self.parse_type_app()]
        while self.at('*'):
            self.next()
            parts.append(self.parse_type_app())
        return parts[0] if len(parts) == 1 else self.node(TypeTuple(parts),
                                                          line)

    def parse_type_app(self):
        line = self.cur().line
        if self.at('('):
            self.next()
            inner = [self.parse_type()]
            while self.at(','):
                self.next()
                inner.append(self.parse_type())
            self.expect(')')
            if len(inner) == 1 and isinstance(inner[0], TypeTuple):
                inner[0].parenthesised = True   # `of (a * b)`: one argument
            if len(inner) > 1:
                if not self.at_type('ID'):
                    self.fail("`(t1, t2)` must be followed by a type name")
                t = self.node(TypeApp(self.next().value, inner), line)
            else:
                t = inner[0]
        elif self.at_type('TYPEVAR'):
            t = self.node(TypeVar(self.next().value), line)
        elif self.at_type('ID'):
            t = self.node(TypeApp(self.next().value, []), line)
        else:
            self.fail("expected a type, found `%s`" % self.cur().value)
        while self.at_type('ID'):                  # postfix: 'a list option
            t = self.node(TypeApp(self.next().value, [t]), line)
        return t

    def parse_type_defs(self):
        self.expect('type')
        defs = [self.parse_type_def()]
        while self.at('and'):
            self.next()
            defs.append(self.parse_type_def())
        return defs

    def parse_type_def(self):
        line = self.cur().line
        tvars = []
        if self.at('('):
            self.next()
            tvars.append(self.expect_typevar())
            while self.at(','):
                self.next()
                tvars.append(self.expect_typevar())
            self.expect(')')
        elif self.at_type('TYPEVAR'):
            tvars.append(self.next().value)
        if not self.at_type('ID'):
            self.fail("expected a type name")
        name = self.next().value
        self.expect('=')
        if self.at('{'):
            self.fail("records are not in the subset")
        if not (self.at('|') or self.at_type('UID')):
            return [self.node(TypeDef(name, tvars, [], self.parse_type()),
                              line)][0]
        variants = []
        if self.at('|'):
            self.next()
        while self.at_type('UID'):
            vline = self.cur().line
            uid = self.next().value
            of = []
            if self.at('of'):
                self.next()
                t = self.parse_type()
                # `of a * b` is two arguments; `of (a * b)` is one tuple
                of = t.types if isinstance(t, TypeTuple) and \
                    not getattr(t, 'parenthesised', False) else [t]
            variants.append(self.node(VariantDef(uid, of), vline))
            if not self.at('|'):
                break
            self.next()
        return self.node(TypeDef(name, tvars, variants), line)

    def expect_typevar(self):
        if not self.at_type('TYPEVAR'):
            self.fail("expected a type variable")
        return self.next().value

    # -- let --
    def parse_let_group(self):
        line = self.expect('let').line
        rec = False
        if self.at('rec'):
            self.next()
            rec = True
        defs = [self.parse_let_def()]
        while self.at('and'):
            self.next()
            defs.append(self.parse_let_def())
        return self.node(LetGroup(rec, defs), line)

    def parse_let_def(self):
        line = self.cur().line
        if self.at_type('ID'):
            pattern = self.node(Variable(self.next().value), line)
            params = self.parse_params()
        else:
            pattern = self.parse_pattern()
            params = []
        ret = None
        if self.at(':'):
            self.next()
            ret = self.parse_type()
        self.expect('=')
        return self.node(LetDef(pattern, params, ret, self.parse_expr()),
                         line)

    def parse_params(self):
        params = []
        while True:
            line = self.cur().line
            if self.at_type('ID'):
                name = self.next().value
                params.append((self.node(Wildcard() if name == '_' else
                                         Variable(name), line), None))
            elif self.at('_'):
                self.next()
                params.append((self.node(Wildcard(), line), None))
            elif self.at('()'):
                self.next()
                params.append((self.node(Const(()), line), None))
            elif self.at('('):
                self.next()
                p = self.parse_pattern()
                t = None
                if self.at(':'):
                    self.next()
                    t = self.parse_type()
                self.expect(')')
                params.append((p, t))
            elif self.at('~', '?'):
                self.fail("labelled and optional arguments are not in the "
                          "subset")
            else:
                return params

    # -- expressions --
    def parse_expr(self):
        """The loosest level: `let`, `fun`, `match`, `if`, then tuples."""
        line = self.cur().line
        if self.at('let'):
            group = self.parse_let_group()
            self.expect('in')
            return self.node(LetIn(group, self.parse_expr()), line)
        if self.at('fun'):
            self.next()
            params = self.parse_params()
            if not params:
                self.fail("`fun` needs a parameter")
            self.expect('->')
            return self.node(Fun(params, self.parse_expr()), line)
        if self.at('function'):
            self.next()
            x = self.node(Variable('_function_arg'), line)
            return self.node(Fun([(x, None)], self.node(
                MatchWith(x, self.parse_cases()), line)), line)
        if self.at('match'):
            self.next()
            scrutinee = self.parse_expr()
            self.expect('with')
            return self.node(MatchWith(scrutinee, self.parse_cases()), line)
        if self.at('if'):
            self.next()
            c = self.parse_expr()
            self.expect('then')
            t = self.parse_expr_no_tuple()
            if not self.at('else'):
                self.fail("`if` without `else` is not in the subset")
            self.next()
            return self.node(IfThen(c, t, self.parse_expr_no_tuple()), line)
        first = self.parse_binary(0)
        if not self.at(','):
            return first
        parts = [first]
        while self.at(','):
            self.next()
            parts.append(self.parse_binary(0))
        return self.node(TupleNode(parts), line)

    def parse_expr_no_tuple(self):
        if self.at('let', 'fun', 'function', 'match', 'if'):
            return self.parse_expr()
        return self.parse_binary(0)

    def parse_cases(self):
        if self.at('|'):
            self.next()
        cases = []
        while True:
            line = self.cur().line
            pat = self.parse_pattern()
            if self.at('|'):
                self.fail("or-patterns are not in the subset; write each "
                          "case as its own arm")
            guard = None
            if self.at('when'):
                self.next()
                guard = self.parse_expr()
            self.expect('->')
            if self.at('.'):
                self.next()
                body = self.node(Refute(), line)
            else:
                body = self.parse_expr()
            cases.append(self.node(MatchBranch(pat, guard, body), line))
            if not self.at('|'):
                return cases
            self.next()

    def parse_binary(self, level):
        if level == len(BINARY):
            return self.parse_unary()
        ops, assoc = BINARY[level]
        line = self.cur().line
        left = self.parse_binary(level + 1)
        while self.at(*ops):
            op = self.next().value
            if assoc == 'right':
                right = self.parse_binary(level)
                return self.node(BinOp(op, left, right), line)
            left = self.node(BinOp(op, left, self.parse_binary(level + 1)),
                             line)
        if self.at(':='):
            self.fail("`:=`: references are not in the subset")
        return left

    def parse_unary(self):
        line = self.cur().line
        if self.at('-'):
            self.next()
            return self.node(BinOp('-', self.node(Const(0), line),
                                   self.parse_unary()), line)
        if self.at('not'):
            self.next()
            arg = self.parse_unary()
            return self.node(Application(self.node(Variable('not'), line),
                                         [arg]), line)
        if self.at('!'):
            self.fail("`!`: references are not in the subset")
        return self.parse_app()

    def starts_atom(self):
        return self.at_type(*_STARTS_ATOM) or self.at(
            '(', '()', '[', '[]', 'true', 'false', 'begin')

    def parse_app(self):
        line = self.cur().line
        head = self.parse_atom()
        args = []
        while self.starts_atom() and not self.at_type('TYPEVAR'):
            args.append(self.parse_atom())
        if isinstance(head, ConstructorApp) and not head.args:
            if len(args) > 1:
                self.fail("constructor `%s` takes one argument (a tuple "
                          "for several)" % head.name)
            if args:
                a = args[0]
                head.args = a.elements if isinstance(a, TupleNode) \
                    and not getattr(a, 'parenthesised_single', False) \
                    else [a]
                head.tuple_arg = isinstance(a, TupleNode)
            return head
        if not args:
            return head
        return self.node(Application(head, args), line)

    def parse_atom(self):
        t = self.cur()
        line = t.line
        if t.type == 'INT':
            self.next()
            return self.node(Const(int(t.value.replace('_', ''))), line)
        if self.at('true', 'false'):
            self.next()
            return self.node(Const(t.value == 'true'), line)
        if self.at('()'):
            self.next()
            return self.node(Const(()), line)
        if self.at('[]'):
            self.next()
            return self.node(ConstructorApp('[]', []), line)
        if t.type == 'ID':
            self.next()
            return self.node(Variable(t.value), line)
        if t.type == 'UID':
            self.next()
            if self.at('.'):
                self.fail("`%s.`: modules are not in the subset" % t.value)
            return self.node(ConstructorApp(t.value, []), line)
        if self.at('begin'):
            self.next()
            e = self.parse_expr()
            self.expect('end')
            return e
        if self.at('['):
            self.next()
            items = []
            while not self.at(']'):
                items.append(self.parse_expr_no_tuple())
                if not self.at(';'):
                    break
                self.next()
            self.expect(']')
            out = self.node(ConstructorApp('[]', []), line)
            for item in reversed(items):
                out = self.node(ConstructorApp('::', [item, out]), line)
            return out
        if self.at('('):
            self.next()
            if self.at(')'):
                self.next()
                return self.node(Const(()), line)
            e = self.parse_expr()
            if self.at(':'):
                self.next()
                e = self.node(Annot(e, self.parse_type()), line)
            self.expect(')')
            return e
        if self.at('{'):
            self.fail("records are not in the subset")
        if self.at('`'):
            self.fail("polymorphic variants are not in the subset")
        self.fail("unexpected `%s`" % (t.value or 'end of file'))

    # -- patterns --
    def parse_pattern(self):
        line = self.cur().line
        first = self.parse_pattern_cons()
        if not self.at(','):
            return first
        parts = [first]
        while self.at(','):
            self.next()
            parts.append(self.parse_pattern_cons())
        return self.node(TupleNode(parts), line)

    def parse_pattern_cons(self):
        line = self.cur().line
        head = self.parse_pattern_app()
        if self.at('::'):
            self.next()
            return self.node(ConstructorPat('::', [
                head, self.parse_pattern_cons()]), line)
        return head

    def parse_pattern_app(self):
        line = self.cur().line
        if self.at_type('UID'):
            name = self.next().value
            if self.starts_pattern_atom():
                arg = self.parse_pattern_atom()
                args = arg.elements if isinstance(arg, TupleNode) else [arg]
                return self.node(ConstructorPat(name, args), line)
            return self.node(ConstructorPat(name, []), line)
        return self.parse_pattern_atom()

    def starts_pattern_atom(self):
        return self.at_type('ID', 'INT', 'UID') or self.at(
            '_', '(', '()', '[]', 'true', 'false', '-')

    def parse_pattern_atom(self):
        t = self.cur()
        line = t.line
        if t.type == 'ID':
            self.next()
            if t.value == '_':
                return self.node(Wildcard(), line)
            return self.node(Variable(t.value), line)
        if self.at('_'):
            self.next()
            return self.node(Wildcard(), line)
        if t.type == 'INT':
            self.next()
            return self.node(Const(int(t.value.replace('_', ''))), line)
        if self.at('-') and self.tokens[self.pos + 1].type == 'INT':
            self.next()
            return self.node(Const(-int(self.next().value)), line)
        if self.at('true', 'false'):
            self.next()
            return self.node(Const(t.value == 'true'), line)
        if self.at('()'):
            self.next()
            return self.node(Const(()), line)
        if self.at('[]'):
            self.next()
            return self.node(ConstructorPat('[]', []), line)
        if t.type == 'UID':
            self.next()
            return self.node(ConstructorPat(t.value, []), line)
        if self.at('('):
            self.next()
            p = self.parse_pattern()
            if self.at(':'):
                self.next()
                p.annot = self.parse_type()
            self.expect(')')
            return p
        if self.at('['):
            self.fail("list patterns `[a; b]` are not in the subset; write "
                      "`a :: b :: []`")
        self.fail("expected a pattern, found `%s`" % (t.value or
                                                       'end of file'))


# The `_` identifier: the lexer reads `_` as an ID; make it a wildcard.
def parse(code):
    return Parser(tokenize(code)).parse_program()


# ==========================================
# 4. Types and inference
# ==========================================
class TVar:
    """A unification variable; `ref` is what it was unified with."""
    _n = [0]

    def __init__(self, level):
        TVar._n[0] += 1
        self.id, self.ref, self.level = TVar._n[0], None, level
        self.rigid = None                       # an annotation's name, if any

    def __repr__(self):
        return show(self)


class TCon:
    """`int`, `bool`, `unit`, `list`, a user type, `->` or `*`."""
    def __init__(self, name, args=()):
        self.name, self.args = name, list(args)

    def __repr__(self):
        return show(self)


def prune(t):
    while isinstance(t, TVar) and t.ref is not None:
        t = t.ref
    return t


def arrow(a, b):
    return TCon('->', [a, b])


INT, BOOL, UNIT = TCon('int'), TCon('bool'), TCon('unit')


def show(t, names=None):
    names = {} if names is None else names
    t = prune(t)
    if isinstance(t, TVar):
        if t.id not in names:
            names[t.id] = t.rigid or "'" + chr(ord('a') + len(names) % 26) + (
                str(len(names) // 26) if len(names) >= 26 else '')
        return names[t.id]
    if t.name == '->':
        a = show(t.args[0], names)
        if isinstance(prune(t.args[0]), TCon) and \
                prune(t.args[0]).name == '->':
            a = '(%s)' % a
        return '%s -> %s' % (a, show(t.args[1], names))
    if t.name == '*':
        return ' * '.join(
            ('(%s)' % show(a, names)) if isinstance(prune(a), TCon)
            and prune(a).name in ('->', '*') else show(a, names)
            for a in t.args)
    if not t.args:
        return t.name
    if len(t.args) == 1:
        a = show(t.args[0], names)
        if isinstance(prune(t.args[0]), TCon) and \
                prune(t.args[0]).name in ('->', '*'):
            a = '(%s)' % a
        return '%s %s' % (a, t.name)
    return '(%s) %s' % (', '.join(show(a, names) for a in t.args), t.name)


class Scheme:
    """`forall quantified. body`."""
    def __init__(self, quantified, body):
        self.quantified, self.body = quantified, body


def occurs(v, t):
    t = prune(t)
    if t is v:
        return True
    if isinstance(t, TCon):
        return any(occurs(v, a) for a in t.args)
    return False


class Checker:
    """Algorithm W over the AST, recording on each node its type."""

    def __init__(self):
        self.level = 0
        self.types = {'int': 0, 'bool': 0, 'unit': 0, 'list': 1}
        self.aliases = {}
        self.typedefs = {}                      # name -> TypeDef
        # constructor -> (type name, type params, argument type nodes)
        self.ctors = {'[]': ('list', ["'a"], []),
                      '::': ('list', ["'a"], [TypeVar("'a"), TypeApp(
                          'list', [TypeVar("'a")])])}
        a = TVar(0)
        self.values = {
            'not': Scheme([], arrow(BOOL, BOOL)),
            'fst': Scheme([a], arrow(TCon('*', [a, b := TVar(0)]), a)),
            'snd': Scheme([a, b], arrow(TCon('*', [a, b]), b)),
        }
        self.toplevel = []                      # (name, Scheme, LetDef)

    # -- type variables and schemes --
    def fresh(self):
        return TVar(self.level)

    def instantiate(self, scheme, node=None):
        subst = {v.id: self.fresh() for v in scheme.quantified}

        def go(t):
            t = prune(t)
            if isinstance(t, TVar):
                return subst.get(t.id, t)
            return TCon(t.name, [go(a) for a in t.args])
        if node is not None:
            node.inst = [subst[v.id] for v in scheme.quantified]
        return go(scheme.body)

    def generalize(self, t):
        out, seen = [], set()

        def go(t):
            t = prune(t)
            if isinstance(t, TVar):
                if t.level > self.level and t.id not in seen:
                    seen.add(t.id)
                    out.append(t)
            else:
                for a in t.args:
                    go(a)
        go(t)
        return Scheme(out, t)

    def unify(self, a, b, line, what):
        a, b = prune(a), prune(b)
        if a is b:
            return
        if isinstance(a, TVar):
            if occurs(a, b):
                raise CompileError("%s: a value would have to contain "
                                   "itself (%s occurs in %s)"
                                   % (what, show(a), show(b)), line)
            self.lower(b, a.level)
            if isinstance(b, TVar) and a.rigid and not b.rigid:
                b.rigid = a.rigid               # keep the name that was written
            a.ref = b
            return
        if isinstance(b, TVar):
            return self.unify(b, a, line, what)
        if a.name != b.name or len(a.args) != len(b.args):
            names = {}
            raise CompileError("%s: expected %s, found %s"
                               % (what, show(b, names), show(a, names)),
                               line)
        for x, y in zip(a.args, b.args):
            self.unify(x, y, line, what)

    def lower(self, t, level):
        t = prune(t)
        if isinstance(t, TVar):
            t.level = min(t.level, level)
        else:
            for a in t.args:
                self.lower(a, level)

    # -- types as written --
    def from_ast(self, t, tvars, line=0):
        """A written type as a type; `tvars` maps 'a to its variable."""
        if isinstance(t, TypeVar):
            if t.name not in tvars:
                v = self.fresh()
                v.rigid = t.name
                tvars[t.name] = v
            return tvars[t.name]
        if isinstance(t, TypeArrow):
            return arrow(self.from_ast(t.left, tvars, line),
                         self.from_ast(t.right, tvars, line))
        if isinstance(t, TypeTuple):
            return TCon('*', [self.from_ast(x, tvars, line) for x in t.types])
        if isinstance(t, TypeApp):
            if t.name in self.aliases:
                params, body = self.aliases[t.name]
                sub = dict(zip(params, [self.from_ast(a, tvars, line)
                                        for a in t.args]))
                return self.from_ast(body, dict(tvars, **sub), line)
            if t.name in ('string', 'float', 'char', 'array', 'ref',
                          'bytes', 'exn'):
                raise CompileError("type `%s` is not in the subset"
                                   % t.name, t.line or line)
            if t.name not in self.types:
                raise CompileError("unknown type `%s`" % t.name,
                                   t.line or line)
            if self.types[t.name] != len(t.args):
                raise CompileError("type `%s` takes %d argument(s), given %d"
                                   % (t.name, self.types[t.name],
                                      len(t.args)), t.line or line)
            return TCon(t.name, [self.from_ast(a, tvars, line)
                                 for a in t.args])
        raise CompileError("not a type: %r" % (t,), line)

    # -- declarations --
    def declare(self, td):
        if td.name in self.types or td.name in self.aliases:
            raise CompileError("type `%s` is defined twice" % td.name,
                               td.line)
        if td.alias is not None:
            self.aliases[td.name] = (td.type_vars, td.alias)
            self.check_type_vars(td.alias, td.type_vars, td.line)
            return
        self.types[td.name] = len(td.type_vars)
        self.typedefs[td.name] = td
        for v in td.variants:
            if v.name in self.ctors:
                raise CompileError("constructor `%s` is defined twice"
                                   % v.name, v.line)
            self.ctors[v.name] = (td.name, td.type_vars, v.of_types)

    def check_type_vars(self, t, allowed, line):
        if isinstance(t, TypeVar) and t.name not in allowed:
            raise CompileError("type variable %s is not a parameter of the "
                               "type" % t.name, t.line or line)
        for sub in (getattr(t, 'types', None) or getattr(t, 'args', None)
                    or [x for x in (getattr(t, 'left', None),
                                    getattr(t, 'right', None)) if x]):
            if isinstance(sub, Node):
                self.check_type_vars(sub, allowed, line)

    def finish_declarations(self, defs):
        for td in defs:
            for v in td.variants:
                for t in v.of_types:
                    self.check_type_vars(t, td.type_vars, v.line)
                    self.from_ast(t, {n: TVar(0) for n in td.type_vars},
                                  v.line)

    def ctor_type(self, name, line, node=None):
        """(argument types, result type) of a constructor, instantiated."""
        if name not in self.ctors:
            raise CompileError("unknown constructor `%s`" % name, line)
        tname, params, of = self.ctors[name]
        sub = {p: self.fresh() for p in params}
        if node is not None:
            node.inst = [sub[p] for p in params]
        args = [self.from_ast(t, dict(sub), line) for t in of]
        return args, TCon(tname, [sub[p] for p in params])

    # -- expressions --
    def infer(self, e, env):
        t = self._infer(e, env)
        e.ty = t
        return t

    def _infer(self, e, env):
        if isinstance(e, Const):
            v = e.value
            return UNIT if v == () else BOOL if isinstance(v, bool) else INT
        if isinstance(e, Variable):
            if e.name in env:
                return self.instantiate(env[e.name], e)
            raise CompileError("unbound value `%s`" % e.name, e.line)
        if isinstance(e, ConstructorApp):
            args, result = self.ctor_type(e.name, e.line, e)
            if len(args) != len(e.args):
                if len(args) == 1 and len(e.args) > 1:
                    tup = TupleNode(e.args)
                    tup.line = e.line
                    e.args = [tup]
                else:
                    raise CompileError(
                        "constructor `%s` expects %d argument(s), given %d"
                        % (e.name, len(args), len(e.args)), e.line)
            for want, a in zip(args, e.args):
                self.unify(self.infer(a, env), want, a.line,
                           "argument of `%s`" % e.name)
            return result
        if isinstance(e, TupleNode):
            return TCon('*', [self.infer(x, env) for x in e.elements])
        if isinstance(e, Application):
            f = self.infer(e.func, env)
            for a in e.args:
                r = self.fresh()
                self.unify(f, arrow(self.infer(a, env), r), a.line,
                           "this argument")
                f = r
            return f
        if isinstance(e, BinOp):
            return self.infer_binop(e, env)
        if isinstance(e, IfThen):
            self.unify(self.infer(e.cond, env), BOOL, e.cond.line,
                       "the condition of `if`")
            t = self.infer(e.then, env)
            self.unify(self.infer(e.other, env), t, e.other.line,
                       "the `else` branch")
            return t
        if isinstance(e, Annot):
            want = self.from_ast(e.type_, self.annot_vars, e.line)
            self.unify(self.infer(e.expr, env), want, e.line,
                       "this annotation")
            return want
        if isinstance(e, Fun):
            return self.infer_fun(e.params, e.body, None, env, e.line)
        if isinstance(e, LetIn):
            inner = self.infer_group(e.group, env)
            return self.infer(e.body, inner)
        if isinstance(e, MatchWith):
            return self.infer_match(e, env)
        if isinstance(e, Refute):
            return self.fresh()
        raise CompileError("cannot type %r" % (e,), getattr(e, 'line', 0))

    def infer_binop(self, e, env):
        a = self.infer(e.left, env)
        b = self.infer(e.right, env)
        op = e.op
        if op in ('+', '-', '*', '/', 'mod'):
            self.unify(a, INT, e.left.line, "`%s`" % op)
            self.unify(b, INT, e.right.line, "`%s`" % op)
            return INT
        if op in ('&&', '||'):
            self.unify(a, BOOL, e.left.line, "`%s`" % op)
            self.unify(b, BOOL, e.right.line, "`%s`" % op)
            return BOOL
        if op in ('=', '<>', '<', '>', '<=', '>='):
            self.unify(b, a, e.right.line, "`%s`" % op)
            if isinstance(prune(a), TCon) and prune(a).name == '->':
                raise CompileError("functions cannot be compared", e.line)
            return BOOL
        if op == '::':
            lst = TCon('list', [a])
            self.unify(b, lst, e.right.line, "`::`")
            e.__class__ = ConstructorApp        # it is the constructor
            e.name, e.args = '::', [e.left, e.right]
            e.inst = [a]
            return lst
        raise CompileError("operator `%s` is not in the subset" % op, e.line)

    def infer_fun(self, params, body, ret, env, line):
        env = dict(env)
        types = []
        for p, t in params:
            pt = self.fresh()
            if t is not None:
                self.unify(pt, self.from_ast(t, self.annot_vars, line),
                           p.line, "this parameter's annotation")
            self.bind_pattern(p, pt, env)
            types.append(pt)
        rt = self.infer(body, env)
        if ret is not None:
            self.unify(rt, self.from_ast(ret, self.annot_vars, line),
                       body.line, "the result annotation")
        for pt in reversed(types):
            rt = arrow(pt, rt)
        return rt

    def infer_match(self, e, env):
        st = self.infer(e.match_expr, env)
        result = self.fresh()
        for br in e.branches:
            inner = dict(env)
            self.bind_pattern(br.pattern, st, inner)
            if br.guard is not None:
                self.unify(self.infer(br.guard, inner), BOOL,
                           br.guard.line, "a `when` guard")
            bt = self.infer(br.body, inner)
            if isinstance(br.body, Refute):
                if not self.refutable(br.pattern, st):
                    raise CompileError(
                        "`.` says this arm cannot be reached, but a value "
                        "of type %s can match it" % show(st), br.line)
            else:
                self.unify(bt, result, br.body.line, "this match arm")
        self.check_exhaustive(e, st)
        return result

    def bind_pattern(self, p, t, env):
        """Unify a pattern with `t`, adding its variables to `env`."""
        p.ty = t
        if isinstance(p, Variable):
            env[p.name] = Scheme([], t)
        elif isinstance(p, Wildcard):
            pass
        elif isinstance(p, Const):
            v = p.value
            self.unify(t, UNIT if v == () else BOOL if isinstance(v, bool)
                       else INT, p.line, "this pattern")
        elif isinstance(p, TupleNode):
            parts = [self.fresh() for _ in p.elements]
            self.unify(t, TCon('*', parts), p.line, "this tuple pattern")
            for sub, pt in zip(p.elements, parts):
                self.bind_pattern(sub, pt, env)
        elif isinstance(p, ConstructorPat):
            args, result = self.ctor_type(p.name, p.line, p)
            self.unify(t, result, p.line, "pattern `%s`" % p.name)
            subs = p.args
            if len(args) == 1 and len(subs) > 1:
                tup = TupleNode(subs)
                tup.line = p.line
                p.args = subs = [tup]
            if len(args) != len(subs):
                raise CompileError(
                    "constructor `%s` expects %d argument(s) in a pattern, "
                    "given %d" % (p.name, len(args), len(subs)), p.line)
            for sub, at in zip(subs, args):
                self.bind_pattern(sub, at, env)
        else:
            raise CompileError("not a pattern", p.line)
        if getattr(p, 'annot', None) is not None:
            self.unify(t, self.from_ast(p.annot, self.annot_vars, p.line),
                       p.line, "this pattern's annotation")

    def refutable(self, p, t):
        """True if no value can match `p` at type `t`: `t` is a variant
        type with no constructors, or some part of `p` is at one."""
        t = prune(t)
        if isinstance(t, TCon) and t.name in self.typedefs and \
                not self.typedefs[t.name].variants:
            return True
        if isinstance(p, TupleNode) and isinstance(t, TCon) and \
                t.name == '*':
            return any(self.refutable(e, a)
                       for e, a in zip(p.elements, t.args))
        if isinstance(p, ConstructorPat):
            return any(self.refutable(a, prune(a.ty)) for a in p.args)
        return False

    # -- let --
    def infer_group(self, group, env):
        self.level += 1
        env = dict(env)
        pending = []
        if group.rec:
            for d in group.defs:
                if not isinstance(d.pattern, Variable):
                    raise CompileError("`let rec` must bind a name",
                                       d.line)
                env[d.pattern.name] = Scheme([], self.fresh())
        for d in group.defs:
            if d.params:
                t = self.infer_fun(d.params, d.value, d.ret_type, env,
                                   d.line)
            else:
                t = self.infer(d.value, env)
                if d.ret_type is not None:
                    self.unify(t, self.from_ast(d.ret_type, self.annot_vars,
                                                d.line), d.line,
                               "the annotation")
            if group.rec:
                self.unify(env[d.pattern.name].body, t, d.line,
                           "`%s`, used recursively" % d.pattern.name)
            pending.append((d, t))
        self.level -= 1
        out = dict(env)
        for d, t in pending:
            value = d.value if not d.params else Fun(d.params, d.value)
            if is_value(value):
                self.bind_generalized(d.pattern, t, out)
            else:                               # the value restriction
                self.bind_pattern(d.pattern, t, out)
            d.ty = t
        return out

    def bind_generalized(self, p, t, env):
        if isinstance(p, Variable):
            env[p.name] = self.generalize(t)
            p.ty = t
        else:                                   # a destructuring binding
            self.bind_pattern(p, t, env)

    # -- program --
    def check(self, items):
        env = dict(self.values)
        pending_types = []
        for item in items:
            if isinstance(item, TypeDef):
                self.declare(item)
                pending_types.append(item)
                continue
            if pending_types:
                self.finish_declarations(pending_types)
                pending_types = []
            self.annot_vars = {}
            env = self.infer_group(item, env)
            for d in item.defs:
                d.annot_vars = self.annot_vars
                for name in pattern_names(d.pattern):
                    self.toplevel.append((name, env[name], d, item))
        if pending_types:
            self.finish_declarations(pending_types)
        return env

    # -- exhaustiveness --
    def check_exhaustive(self, e, st):
        rows = [br.pattern for br in e.branches if br.guard is None]
        missing = self.missing(rows, prune(st))
        if missing is not None:
            raise CompileError("this match is not exhaustive: %s is not "
                               "matched" % missing, e.line)

    def missing(self, rows, t):
        """An example value no row matches, or None: the usefulness check,
        column by column, over constructors, tuples and literals."""
        rows = [strip_annot(r) for r in rows]
        if any(isinstance(r, (Variable, Wildcard)) for r in rows):
            return None
        t = prune(t)
        if not rows:
            return '_'
        if isinstance(t, TCon) and t.name == '*':
            mat = [r.elements for r in rows]
            return self.missing_tuple(mat, t.args)
        if isinstance(t, TCon) and (t.name in self.typedefs or
                                    t.name == 'list'):
            for name in self.constructors_of(t.name):
                sub = [r for r in rows if isinstance(r, ConstructorPat)
                       and r.name == name]
                args, _ = self.ctor_type(name, 0)
                if not sub:
                    return name + (' _' if args else '')
                if args:
                    inner = self.missing_tuple([r.args for r in sub],
                                               [self.at_type_of(t, name, i)
                                                for i in range(len(args))])
                    if inner is not None:
                        return '%s %s' % (name, inner) if name != '::' \
                            else inner
            return None
        if isinstance(t, TCon) and t.name == 'bool':
            vals = {r.value for r in rows if isinstance(r, Const)}
            for v in (True, False):
                if v not in vals:
                    return str(v).lower()
            return None
        if isinstance(t, TCon) and t.name == 'unit':
            return None
        return '_'                              # an int, say: needs `_`

    def missing_tuple(self, mat, types):
        if not types:
            return None if mat else '()'
        first = [row[0] for row in mat]
        if all(isinstance(strip_annot(p), (Variable, Wildcard))
               for p in first):
            rest = self.missing_tuple([row[1:] for row in mat], types[1:])
            return None if rest is None else '(_, %s)' % rest
        # split on the first column's constructors / literals
        miss = self.missing(first, types[0])
        if miss is not None:
            return '(%s, ..)' % miss
        heads = {self.head(p) for p in first if self.head(p) is not None}
        for h in heads:
            sub = [row for row in mat if self.head(row[0]) in (h, None)]
            rest = self.missing_tuple([row[1:] for row in sub], types[1:])
            if rest is not None:
                return '(%s, %s)' % (h, rest)
        return None

    @staticmethod
    def head(p):
        p = strip_annot(p)
        if isinstance(p, ConstructorPat):
            return p.name
        if isinstance(p, Const):
            return repr(p.value)
        return None

    def constructors_of(self, tname):
        if tname == 'list':
            return ['[]', '::']
        return [v.name for v in self.typedefs[tname].variants]

    def at_type_of(self, t, name, i):
        tname, params, of = self.ctors[name]
        sub = dict(zip(params, t.args))
        return self.from_ast(of[i], dict(sub), 0)

    # -- the tightening --
    def check_annotations(self):
        """Every type variable written in an annotation is still its own
        variable: not unified with a type, nor with another annotation
        variable.  Raises on the first that is not."""
        for name, scheme, d, _ in self.toplevel:
            seen = {}
            for vname, v in getattr(d, 'annot_vars', {}).items():
                r = prune(v)
                if not isinstance(r, TVar):
                    raise CompileError(
                        "`%s`: the annotation says %s for any type, but the "
                        "definition only works for %s" % (name, vname,
                                                          show(r)), d.line)
                if r.id in seen:
                    raise CompileError(
                        "`%s`: the annotation says %s and %s may differ, "
                        "but the definition makes them the same"
                        % (name, seen[r.id], vname), d.line)
                seen[r.id] = vname


def strip_annot(p):
    return p


def pattern_names(p):
    if isinstance(p, Variable):
        return [p.name]
    if isinstance(p, TupleNode):
        return [n for e in p.elements for n in pattern_names(e)]
    if isinstance(p, ConstructorPat):
        return [n for a in p.args for n in pattern_names(a)]
    return []


def is_value(e):
    """The value restriction: only syntactic values are generalized."""
    return isinstance(e, (Fun, Variable, Const)) or (
        isinstance(e, ConstructorApp) and all(is_value(a) for a in e.args)) \
        or (isinstance(e, TupleNode) and all(is_value(a) for a in e.elements))


def check(code, strict=True):
    """Parse and type `code`.  (items, Checker).  `strict` enforces rigid
    annotation variables (see `Checker.check_annotations`)."""
    items = parse(code)
    c = Checker()
    c.check(items)
    if strict:
        c.check_annotations()
    return items, c


def main(argv):
    for path in argv:
        with open(path) as fh:
            try:
                _, c = check(fh.read())
            except CompileError as exc:
                print('%s: %s' % (path, exc))
                return 1
        for name, scheme, _, _ in c.toplevel:
            print('val %s : %s' % (name, show(scheme.body)))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
