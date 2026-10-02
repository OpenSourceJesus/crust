#!/usr/bin/env python3
"""C# `string` for csrust, backed by coost's `fastring`  (csrust --coost PATH).

Without --coost csrust refuses `string` (cs2cpp._check_strings). With it, this
pass runs first, on the C# text, and leaves C++-subset text with no `string`,
`char`, or char literal in it, so every later cs2cpp pass sees what it already
knows. The model is unity_pack's (UNITY_PACK.md, "Strings"):

    C#                               lowered
    ------------------------------   --------------------------------------
    field   `string s;`              `fastring s;`            (owned)
    local   `string s = e;`          `fastring s; s.assign_cstr(e);`
    param   `string p`               `_cs_str p`              (borrowed; `_cs_str`
    return  `string F()`             `_cs_str F()`             is `const char *`)
    `s = e`        `s += e`          `s.assign_cstr(e)`  `s.append_X(e)`
    `a == b`       `a != b`          `_cs_str_eq(a, b)`
    `a + b + 1`                      `_cs_cat_i(_cs_cat_s(a, b), 1)`
    `s.Length`     `s[i]`            `s.size()` / `strlen(p)`, `_cs_at_*`
    any other read of an owned `s`   `s.c_str()`
    `char`, `'a'`                    `int`, `97` (a byte: ASCII / Latin-1)

A string that is *returned* or *concatenated* lives in one of sixteen scratch
slots (a ring), valid until sixteen more concatenations: right for a value
used within its statement, which is what a `const char *` result is for. A
value that is *kept* is copied into a `fastring`.

The pass types operands with a small environment (class fields, parameters,
locals). An operand it cannot type, in an expression that has a string in it,
is refused by name and line rather than guessed at.
"""
import re


class StrError(Exception):
    def __init__(self, message, offset=None):
        Exception.__init__(self, message)
        self.message = message
        self.offset = offset


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r'''
    (?P<pp>^[ \t]*\#[^\n]*)
  | (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<ws>\s+)
  | (?P<str>@"(?:[^"]|"")*"|"(?:\\.|[^"\\\n])*")
  | (?P<chr>'(?:\\.|[^'\\\n])+')
  | (?P<num>0[xX][0-9a-fA-F_]+[uUlL]*
           |\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?[fFdDmMuUlL]*
           |\.\d[\d_]*(?:[eE][+-]?\d+)?[fFdDmM]?)
  | (?P<id>@?[A-Za-z_]\w*)
  | (?P<op>\?\?=?|\?\.|=>|==|!=|<=|>=|&&|\|\||\+\+|--|\+=|-=|\*=|/=|%=|&=
           |\|=|\^=|<<=?|>>=?|::|.)
''', re.X | re.S | re.M)

_SKIP = ("ws", "comment", "pp")


class Tok(object):
    __slots__ = ("k", "t")

    def __init__(self, k, t):
        self.k = k
        self.t = t

    def __repr__(self):
        return "%s:%r" % (self.k, self.t)


def tokenize(text):
    return [Tok(m.lastgroup, m.group()) for m in _TOKEN.finditer(text)]


def untokenize(toks):
    return "".join(t.t for t in toks)


def _newlines(toks):
    return sum(t.t.count("\n") for t in toks)


class Seq(object):
    """A token list with the significant tokens indexed and brackets matched."""

    def __init__(self, toks):
        self.toks = toks
        self.sig = [i for i, t in enumerate(toks) if t.k not in _SKIP]
        self.pos = {ti: n for n, ti in enumerate(self.sig)}
        self.match = {}
        stack = []
        for n, ti in enumerate(self.sig):
            t = toks[ti].t
            if toks[ti].k != "op":
                continue
            if t in "([{":
                stack.append(n)
            elif t in ")]}" and stack:
                o = stack.pop()
                self.match[o] = n
                self.match[n] = o

    def __len__(self):
        return len(self.sig)

    def t(self, n):
        return self.toks[self.sig[n]].t if 0 <= n < len(self.sig) else ""

    def k(self, n):
        return self.toks[self.sig[n]].k if 0 <= n < len(self.sig) else ""

    def text(self, a, b):
        """Source text of significant tokens a..b inclusive, whitespace kept."""
        if b < a:
            return ""
        return untokenize(self.toks[self.sig[a]:self.sig[b] + 1])

    def flat(self, a, b):
        return " ".join(self.toks[self.sig[n]].t for n in range(a, b + 1))


# ---------------------------------------------------------------------------
# Kinds
#
#   S   an owned string (a `fastring` field or local)
#   P   a borrowed one (a `const char *`: parameter, literal, call result)
#   C   a char (an int holding a byte)       I  an integer     F  a float
#   B   bool     N  `null`      O  anything else we know is not a string
#   X   a string in a form not lowered yet (string[], List<string>, ...)
# ---------------------------------------------------------------------------

_INTS = frozenset(("int", "uint", "long", "ulong", "short", "ushort", "byte",
                   "sbyte"))
_FLOATS = frozenset(("float", "double"))
_MODS = frozenset(("public", "private", "protected", "internal", "static",
                   "const", "readonly", "virtual", "override", "abstract",
                   "new", "sealed", "extern", "unsafe", "partial", "async",
                   "volatile"))
_NOT_TYPE = frozenset(("return", "new", "throw", "else", "in", "is", "as",
                       "case", "yield", "await", "goto", "break", "continue",
                       "using", "lock", "typeof", "default", "if", "for",
                       "foreach", "while", "do", "switch", "try", "catch",
                       "finally", "out", "ref", "params", "this", "base"))


def kind_of_type(words):
    """Kind for a type written as a list of token texts."""
    flat = "".join(words)
    if flat == "string":
        return "S"
    if "string" in words:
        return "X"
    if flat == "char":
        return "C"
    if flat in _INTS:
        return "I"
    if flat in _FLOATS:
        return "F"
    if flat == "bool":
        return "B"
    return "O"


# ---------------------------------------------------------------------------
# Structure: classes, members, parameters
# ---------------------------------------------------------------------------

class Member(object):
    def __init__(self, what, name):
        self.what = what            # field | method | ctor | prop
        self.name = name
        self.static = False
        self.kind = "O"             # a field's / property's / method's result
        self.type_span = None       # (a, b) significant indices of its type
        self.params = []            # [(name, kind, (a, b))]
        self.body = None            # (lb, rb)
        self.accessors = []         # [(get|set, lb, rb)]
        self.decls = []             # field declarators [(name, init span|None)]
        self.start = 0
        self.end = 0


class ClassInfo(object):
    def __init__(self, name, lb, rb):
        self.name = name
        self.lb = lb
        self.rb = rb
        self.members = []


def parse_type(seq, n, limit):
    """Index just past the type that starts at significant token n, or None."""
    if seq.k(n) != "id" or seq.t(n) in _NOT_TYPE:
        return None
    j = n + 1
    while seq.t(j) == "." and seq.k(j + 1) == "id":
        j += 2
    if seq.t(j) == "<":
        depth = 0
        k = j
        while k < limit:
            t = seq.t(k)
            if t == "<":
                depth += 1
            elif t == ">":
                depth -= 1
            elif t == ">>":
                depth -= 2
            elif t in (";", "{", "}", "(", ")", "="):
                return None
            k += 1
            if depth <= 0:
                break
        if depth != 0:
            return None
        j = k
    while seq.t(j) == "[" and seq.match.get(j, -1) > j and all(
            seq.t(x) == "," for x in range(j + 1, seq.match[j])):
        j = seq.match[j] + 1
    return j


def _words(seq, a, b):
    return [seq.t(n) for n in range(a, b + 1)]


def _split_commas(seq, a, b):
    """Split significant tokens a..b at top-level commas ((), [], <>)."""
    parts, start, depth, n = [], a, 0, a
    while n <= b:
        t = seq.t(n)
        if t in "([{" and seq.k(n) == "op":
            n = seq.match.get(n, n) + 1
            continue
        if t == "<":
            depth += 1
        elif t == ">":
            depth -= 1
        elif t == ">>":
            depth -= 2
        elif t == "," and depth <= 0:
            parts.append((start, n - 1))
            start = n + 1
        n += 1
    if start <= b:
        parts.append((start, b))
    return parts


def parse_params(seq, lp, rp):
    """[(name, kind, (type_a, type_b), mods)] for the list in lp..rp."""
    out = []
    if rp <= lp + 1:
        return out
    for a, b in _split_commas(seq, lp + 1, rp - 1):
        mods = []
        while a <= b and seq.t(a) in ("ref", "out", "params", "this", "in"):
            mods.append(seq.t(a))
            a += 1
        # drop a default value
        eq = None
        d = a
        while d <= b:
            if seq.t(d) in "([" and seq.k(d) == "op":
                d = seq.match.get(d, d) + 1
                continue
            if seq.t(d) == "=":
                eq = d
                break
            d += 1
        end = (eq - 1) if eq is not None else b
        if end < a + 1 or seq.k(end) != "id":
            continue
        out.append((seq.t(end), kind_of_type(_words(seq, a, end - 1)),
                    (a, end - 1), mods))
    return out


def _skip_attrs(seq, n, limit):
    while n < limit and seq.t(n) == "[" and seq.k(n) == "op":
        n = seq.match.get(n, n) + 1
    return n


def parse_class_body(seq, lb, rb, cname, out_classes):
    """Members of the class whose braces are significant tokens lb and rb."""
    cls = ClassInfo(cname, lb, rb)
    out_classes.append(cls)
    n = lb + 1
    while n < rb:
        s = n
        n = _skip_attrs(seq, n, rb)
        hs = n
        j = n
        saw_eq = False
        while j < rb:
            t = seq.t(j)
            if seq.k(j) == "op" and t in "([":
                j = seq.match.get(j, j) + 1
                continue
            if t in ("=", "=>"):
                saw_eq = True
            if t == ";":
                break
            if t == "{":
                if saw_eq:
                    j = seq.match.get(j, j) + 1
                    continue
                break
            j += 1
        term = j
        # modifiers
        m = hs
        mods = set()
        while m < term and seq.t(m) in _MODS:
            mods.add(seq.t(m))
            m += 1
        first = seq.t(m)
        if first in ("class", "struct", "interface", "enum") and seq.t(term) == "{":
            nm = seq.t(m + 1)
            end = seq.match.get(term, term)
            if first in ("class", "struct"):
                parse_class_body(seq, term, end, nm, out_classes)
            n = end + 1
            continue
        if first == "delegate":
            n = term + 1
            continue
        lp = None
        d = m
        while d < term:
            if seq.t(d) == "(" and seq.k(d) == "op":
                lp = d
                break
            if seq.t(d) in ("=", "=>"):
                break
            d += 1
        if lp is not None and lp > m:
            name = seq.t(lp - 1)
            mem = Member("ctor" if lp - 1 == m else "method", name)
            mem.static = "static" in mods
            rp = seq.match.get(lp, lp)
            mem.params = parse_params(seq, lp, rp)
            if lp - 1 > m:
                mem.type_span = (m, lp - 2)
                mem.kind = kind_of_type(_words(seq, m, lp - 2))
            if seq.t(term) == "{":
                end = seq.match.get(term, term)
                mem.body = (term, end)
            elif seq.t(term) == ";" and seq.t(rp + 1) == "=>":
                end = term
            else:
                end = term
            mem.start, mem.end = s, end
            cls.members.append(mem)
            n = end + 1
            continue
        # property / indexer / field
        te = parse_type(seq, m, term + 1)
        if te is None or te > term:
            n = (seq.match.get(term, term) if seq.t(term) == "{" else term) + 1
            continue
        tspan = (m, te - 1)
        kind = kind_of_type(_words(seq, m, te - 1))
        if seq.t(term) == "{" and seq.k(te) == "id":
            mem = Member("prop", seq.t(te))
            mem.static = "static" in mods
            mem.type_span, mem.kind = tspan, kind
            end = seq.match.get(term, term)
            q = term + 1
            while q < end:
                q = _skip_attrs(seq, q, end)
                while q < end and seq.t(q) in _MODS:
                    q += 1
                acc = seq.t(q)
                if acc in ("get", "set", "init"):
                    if seq.t(q + 1) == "{":
                        e2 = seq.match.get(q + 1, q + 1)
                        mem.accessors.append((acc, q + 1, e2))
                        q = e2 + 1
                    else:
                        mem.accessors.append((acc, None, None))
                        q += 2
                else:
                    q += 1
            if seq.t(end + 1) == "=":
                z = end + 1
                while z < rb and seq.t(z) != ";":
                    z += 1
                end = z
            mem.start, mem.end = s, end
            cls.members.append(mem)
            n = end + 1
            continue
        if seq.t(term) == ";" or seq.t(term) == "{":
            mem = Member("field", "")
            mem.static = "static" in mods
            mem.type_span, mem.kind = tspan, kind
            mem.const = "const" in mods
            decls = _split_commas(seq, te, term - 1)
            for a, b in decls:
                if seq.k(a) != "id":
                    continue
                init = None
                for z in range(a, b + 1):
                    if seq.t(z) == "=":
                        init = (z + 1, b)
                        break
                mem.decls.append((seq.t(a), init, a))
            mem.name = mem.decls[0][0] if mem.decls else ""
            end = term
            if seq.t(term) == "{":
                end = seq.match.get(term, term)
                while end < rb and seq.t(end) != ";":
                    end += 1
            mem.start, mem.end = s, end
            cls.members.append(mem)
            n = end + 1
            continue
        n = term + 1
    return cls


def discover(seq):
    """Every class in the file, top level and nested."""
    out = []
    n = 0
    total = len(seq)
    while n < total:
        if seq.t(n) in ("class", "struct") and seq.k(n) == "id" and seq.k(n + 1) == "id":
            j = n + 2
            while j < total and seq.t(j) not in ("{", ";"):
                j += 1
            if j < total and seq.t(j) == "{":
                rb = seq.match.get(j, j)
                parse_class_body(seq, j, rb, seq.t(n + 1), out)
                n = rb + 1
                continue
        n += 1
    return out


# ---------------------------------------------------------------------------
# Typing and rewriting one body
# ---------------------------------------------------------------------------

_STOPS_EQ = frozenset((";", ",", "&&", "||", "?", ":", "=", "+=", "-=", "*=",
                       "/=", "%=", "&=", "|=", "^=", "<<=", ">>=", "=>", "&",
                       "|", "^", "==", "!=", "??"))
_STOPS_CAT = _STOPS_EQ | frozenset(("<", ">", "<=", ">=", "<<", ">>"))
_STOP_WORDS = frozenset(("return", "case", "else", "throw", "yield", "in"))
_ASSIGN = ("=", "+=")
_FIELD_FOLLOWERS = frozenset(("c_str", "size", "assign_cstr", "append_cstr",
                              "append_int", "append_double", "append_char",
                              "append_bool", "clear", "resize"))

_HELPER_KIND = {"_cs_cat_s": "P", "_cs_cat_i": "P", "_cs_cat_f": "P",
                "_cs_cat_c": "P", "_cs_cat_b": "P", "_cs_at": "C",
                "_cs_atp": "C", "_cs_str_eq": "B", "_cs_str_empty": "B",
                "strlen": "I"}


class Ctx(object):
    """What is known about the whole file."""

    def __init__(self, classes):
        self.class_names = set(c.name for c in classes)
        self.sfields = set()
        self.nsfields = {}
        self.sprops = set()
        self.methods = {}
        self.fields_of = {}
        self.field_inits = {}
        self.used = set()
        for c in classes:
            fields = self.fields_of.setdefault(c.name, {})
            for m in c.members:
                if m.what == "field":
                    for name, _init, _a in m.decls:
                        fields[name] = m.kind
                        if m.kind == "S":
                            self.sfields.add(name)
                        else:
                            old = self.nsfields.get(name)
                            self.nsfields[name] = (m.kind if old in (None, m.kind)
                                                   else "O")
                elif m.what == "prop":
                    fields[m.name] = "P" if m.kind == "S" else m.kind
                    if m.kind == "S":
                        self.sprops.add(m.name)
                elif m.what == "method":
                    k = "P" if m.kind == "S" else m.kind
                    old = self.methods.get(m.name)
                    self.methods[m.name] = k if old in (None, k) else "?"

    def member_kind(self, name):
        if name in self.sprops:
            return "P"
        s, n = name in self.sfields, name in self.nsfields
        if s and not n:
            return "S"
        if n and not s:
            return self.nsfields[name]
        return None


def _is_ident_tok(seq, n):
    return seq.k(n) == "id" and seq.t(n) not in _NOT_TYPE


class Body(object):
    def __init__(self, ctx, text, env, cls, base_line, path):
        self.ctx, self.env, self.cls = ctx, env, cls
        self.base_line, self.path = base_line, path
        self.set(text)

    # -- plumbing ---------------------------------------------------------
    def set(self, text):
        self.text = text
        self.seq = Seq(tokenize(text))

    def line(self, n):
        toks = self.seq.toks
        upto = self.seq.sig[n] if 0 <= n < len(self.seq.sig) else len(toks)
        return self.base_line + untokenize(toks[:upto]).count("\n")

    def err(self, msg, n):
        raise StrError(msg, self.line(n))

    def replace(self, a, b, new):
        toks = self.seq.toks
        lo, hi = self.seq.sig[a], self.seq.sig[b]
        # Lines must not move: add only the newlines the new text lacks (it
        # often carries the old operand's own, line breaks and all).
        nl = max(0, _newlines(toks[lo:hi + 1]) - new.count("\n"))
        self.set(untokenize(toks[:lo]) + new + ("\n" * nl) +
                 untokenize(toks[hi + 1:]))

    # -- kinds ------------------------------------------------------------
    def atom(self, n):
        seq = self.seq
        k, t = seq.k(n), seq.t(n)
        if k == "str":
            return "P"
        if k == "chr":
            return "C"
        if k == "num":
            return "F" if (not t.lower().startswith("0x") and
                           re.search(r"[.eEfFdD]", t)) else "I"
        if k == "id":
            if t in ("true", "false"):
                return "B"
            if t == "null":
                return "N"
            if t in self.env:
                v = self.env[t]
                return None if v == "?" else v
            fields = self.ctx.fields_of.get(self.cls, {})
            if t in fields:
                return fields[t]
            if t in self.ctx.class_names:
                return "O"
        return None

    def split_top(self, a, b, ops):
        """Operand spans of a..b split at binary operators in *ops*."""
        seq = self.seq
        spans, start, n = [], a, a
        while n <= b:
            t = seq.t(n)
            if seq.k(n) == "op" and t in "([{":
                n = seq.match.get(n, n) + 1
                continue
            if seq.k(n) == "op" and t in ops and n > a and self.binary_after(n - 1):
                spans.append((start, n - 1))
                start = n + 1
            n += 1
        spans.append((start, b))
        return spans

    def binary_after(self, n):
        seq = self.seq
        return seq.k(n) in ("id", "num", "str", "chr") and seq.t(n) not in _NOT_TYPE \
            or seq.t(n) in (")", "]")

    def kind(self, a, b):
        seq = self.seq
        while a < b and seq.t(a) == "(" and seq.match.get(a) == b:
            a, b = a + 1, b - 1
        if b < a:
            return None
        if a == b:
            return self.atom(a)
        # top-level operators, loosest first
        n, cmp_, tern, arith = a, False, False, False
        while n <= b:
            t = seq.t(n)
            if seq.k(n) == "op" and t in "([{":
                n = seq.match.get(n, n) + 1
                continue
            if seq.k(n) == "op":
                if t in ("==", "!=", "<=", ">=", "&&", "||"):
                    cmp_ = True
                elif t == "?":
                    tern = True
                elif t in ("-", "*", "/", "%") and n > a and self.binary_after(n - 1):
                    arith = True
            n += 1
        if tern:
            return None
        if cmp_:
            return "B"
        spans = self.split_top(a, b, ("+",))
        if len(spans) > 1:
            kinds = [self.kind(x, y) for x, y in spans]
            if any(k in ("S", "P") for k in kinds):
                return "P"
            if all(k in ("I", "C", "F") for k in kinds):
                return "F" if "F" in kinds else "I"
            return None
        if arith:
            kinds = [self.kind(x, y) for x, y in self.split_top(a, b, ("-", "*", "/", "%"))]
            if all(k in ("I", "C", "F") for k in kinds):
                return "F" if "F" in kinds else "I"
            return None
        if seq.t(a) == "!":
            return "B"
        # a cast: `(int) x`
        if seq.t(a) == "(" and seq.match.get(a, b) < b:
            m = seq.match[a]
            words = _words(seq, a + 1, m - 1)
            kk = kind_of_type(words)
            if words and all(re.match(r"^\w+$", w) for w in words):
                return "P" if kk == "S" else kk
        if seq.t(a) == "new":
            return "O"
        t = seq.t(b)
        if t == ")":
            o = seq.match.get(b, -1)
            if o > a and seq.k(o - 1) == "id":
                name = seq.t(o - 1)
                if name in _HELPER_KIND:
                    return _HELPER_KIND[name]
                return self.ctx.methods.get(name) if self.ctx.methods.get(name) != "?" else None
            return None
        if t == "]":
            o = seq.match.get(b, -1)
            if o > a and self.kind(a, o - 1) in ("S", "P"):
                return "C"
            return None
        if seq.k(b) == "id" and b > a and seq.t(b - 1) == ".":
            if seq.t(b) in ("Length", "Count"):
                return "I"
            return self.ctx.member_kind(seq.t(b))
        return None

    # -- extents ----------------------------------------------------------
    def prim_start(self, e):
        seq = self.seq
        while True:
            t = seq.t(e)
            if t == ")":
                o = seq.match.get(e, e)
                start = o - 1 if o > 0 and _is_ident_tok(seq, o - 1) else o
            elif t == "]":
                o = seq.match.get(e, e)
                start = self.prim_start(o - 1) if o > 0 else o
            else:
                start = e
            if start >= 2 and seq.t(start - 1) == ".":
                e = start - 2
                continue
            return start

    def operand_left(self, n, stops):
        seq = self.seq
        i = n
        while i >= 0:
            t, k = seq.t(i), seq.k(i)
            if k == "op" and t in ")]}":
                i = seq.match.get(i, i) - 1
                continue
            if k == "op" and (t in "([{" or t in stops):
                break
            if k == "id" and t in _STOP_WORDS:
                break
            i -= 1
        return i + 1

    def operand_right(self, n, stops):
        seq = self.seq
        i = n
        last = len(seq) - 1
        while i <= last:
            t, k = seq.t(i), seq.k(i)
            if k == "op" and t in "([{":
                i = seq.match.get(i, i) + 1
                continue
            if k == "op" and (t in ")]}" or t in stops):
                break
            i += 1
        return i - 1

    # -- statements -------------------------------------------------------
    def statements(self):
        """[(first, last)] significant spans of simple statements, last being
        the token before the `;`."""
        seq = self.seq
        out, n, total = [], 0, len(seq)
        start = 0
        depth = 0
        while n < total:
            t = seq.t(n)
            if seq.k(n) == "op" and t in "([":
                n = seq.match.get(n, n) + 1
                continue
            if t == "{" or t == "}":
                start = n + 1
            elif t == ";":
                s = self.stmt_start(start, n)
                if s <= n - 1:
                    out.append((s, n - 1))
                start = n + 1
            n += 1
        return out

    def stmt_start(self, s, end):
        seq = self.seq
        while s < end:
            t = seq.t(s)
            if t in ("else", "do"):
                s += 1
            elif t in ("if", "for", "foreach", "while", "switch", "lock", "using",
                       "fixed") and seq.t(s + 1) == "(":
                s = seq.match.get(s + 1, s + 1) + 1
            elif t in ("case", "default"):
                j = s
                while j < end and seq.t(j) != ":":
                    j += 1
                s = j + 1
            else:
                break
        return s

    def top_assign(self, a, b):
        seq = self.seq
        n = a
        while n <= b:
            t = seq.t(n)
            if seq.k(n) == "op" and t in "([{":
                n = seq.match.get(n, n) + 1
                continue
            if seq.k(n) == "op" and t in ("=", "+=", "-=", "*=", "/=", "%=", "|=",
                                          "&=", "^=", "<<=", ">>=", "??="):
                return n
            n += 1
        return None

    # -- declarations -----------------------------------------------------
    def scan_locals(self):
        """Fill self.env with the locals declared in this body."""
        seq = self.seq
        found = {}

        def note(name, kind):
            found[name] = kind if found.get(name, kind) == kind else "?"

        heads = []
        for n in range(len(seq)):
            if seq.t(n) in ("for", "foreach", "using") and seq.t(n + 1) == "(":
                heads.append(n + 2)
        stmts = [a for a, _b in self.statements()] + heads
        for a in sorted(set(stmts)):
            if seq.t(a) == "const":
                a += 1
            e = parse_type(seq, a, len(seq))
            if e is None or seq.k(e) != "id" or seq.t(e) in _NOT_TYPE:
                continue
            if seq.t(e + 1) not in ("=", ";", ",", "in", ")", ":"):
                continue
            words = _words(seq, a, e - 1)
            if words == ["var"]:
                eq = e + 1
                if seq.t(eq) == "=":
                    stop = self.operand_right(eq + 1, frozenset((";",)))
                    k = self.kind(eq + 1, stop)
                    note(seq.t(e), "S" if k in ("S", "P") else (k or "O"))
                else:
                    note(seq.t(e), "O")
                continue
            note(seq.t(e), kind_of_type(words))
        for name, k in found.items():
            if k == "X":
                raise StrError("%s: a string collection is not lowered yet "
                               "(`string[]`, `List<string>`, `Dictionary<string, T>`)"
                               % name, self.base_line)
            if name in self.env and self.env[name] != k:
                self.env[name] = "?"
            else:
                self.env[name] = k

    def rule_decl(self):
        """`string s = e;` -> `fastring s; s = e;`; `char` -> `int`."""
        seq = self.seq
        for a, b in self.statements():
            t = seq.t(a)
            if t == "string" and seq.k(a + 1) == "id":
                name = seq.t(a + 1)
                if seq.t(a + 2) == ",":
                    self.err("`string a, b;`: declare one string per statement", a)
                if seq.t(a + 2) == "=":
                    init = seq.text(a + 3, b)
                    self.replace(a, b, "fastring %s; %s = %s" % (name, name, init))
                else:
                    self.replace(a, b, "fastring %s" % name)
                return True
            if t == "var" and seq.k(a + 1) == "id" and seq.t(a + 2) == "=" and \
                    self.env.get(seq.t(a + 1)) == "S":
                name = seq.t(a + 1)
                self.replace(a, b, "fastring %s; %s = %s" % (name, name, seq.text(a + 3, b)))
                return True
        return False

    # -- the rules --------------------------------------------------------
    def rule_static_members(self):
        seq = self.seq
        for n in range(len(seq) - 2):
            if seq.t(n) == "string" and seq.t(n + 1) == ".":
                nm = seq.t(n + 2)
                if nm == "Empty":
                    self.replace(n, n + 2, '""')
                    return True
                if nm in ("IsNullOrEmpty", "Equals"):
                    self.ctx.used.add("_cs_str_empty" if nm == "IsNullOrEmpty" else "_cs_str_eq")
                    self.replace(n, n + 2, "_cs_str_empty" if nm == "IsNullOrEmpty" else "_cs_str_eq")
                    return True
        return False

    def rule_assign(self):
        seq = self.seq
        for a, b in self.statements():
            q = self.top_assign(a, b)
            if q is None or q == a:
                continue
            lhs_k = self.kind(a, q - 1)
            op = seq.t(q)
            if lhs_k not in ("S", "P") or op not in _ASSIGN:
                continue
            if self.is_declaration(a, q):
                continue
            lhs = seq.text(a, q - 1)
            rk = self.kind(q + 1, b)
            rhs = seq.text(q + 1, b)
            if lhs_k == "S":
                if op == "=":
                    new = "%s.clear()" % lhs if rk == "N" else "%s.assign_cstr(%s)" % (lhs, rhs)
                else:
                    meth = {"S": "append_cstr", "P": "append_cstr", "I": "append_int",
                            "F": "append_double", "C": "append_char", "B": "append_bool"}.get(rk)
                    if meth is None:
                        self.err("`+=` on a string: cannot tell the type of `%s`" % rhs.strip(), q + 1)
                    new = "%s.%s(%s%s)" % (lhs, meth, rhs, ", 6" if meth == "append_double" else "")
                self.replace(a, b, new)
                return True
            if op == "+=":        # a borrowed string or a property: p = p + e
                self.replace(a, b, "%s = %s" % (lhs, self.cat_text([(a, q - 1)] + [(q + 1, b)], q)))
                return True
        return False

    def is_declaration(self, a, q):
        seq = self.seq
        return q - a >= 2 and seq.k(q - 1) == "id" and seq.k(q - 2) == "id" and \
            seq.t(q - 2) not in _NOT_TYPE

    def rule_length(self):
        seq = self.seq
        for n in range(2, len(seq)):
            if seq.t(n) == "Length" and seq.t(n - 1) == ".":
                a = self.prim_start(n - 2)
                k = self.kind(a, n - 2)
                if k in ("S", "P"):
                    recv = seq.text(a, n - 2)
                    if k == "S":
                        self.replace(a, n, "(int)%s.size()" % recv)
                    else:
                        self.ctx.used.add("strlen")
                        self.replace(a, n, "(int)strlen(%s)" % recv)
                    return True
        return False

    def rule_index(self):
        seq = self.seq
        for n in range(1, len(seq)):
            if seq.t(n) == "[" and seq.match.get(n, -1) > n and \
                    (_is_ident_tok(seq, n - 1) or seq.t(n - 1) in (")", "]")):
                a = self.prim_start(n - 1)
                k = self.kind(a, n - 1)
                if k in ("S", "P"):
                    m = seq.match[n]
                    recv, idx = seq.text(a, n - 1), seq.text(n + 1, m - 1)
                    if k == "S":
                        self.ctx.used.add("_cs_at")
                        self.replace(a, m, "_cs_at(%s.c_str(), (int)%s.size(), %s)" % (recv, recv, idx))
                    else:
                        self.ctx.used.add("_cs_atp")
                        self.replace(a, m, "_cs_atp(%s, %s)" % (recv, idx))
                    return True
        return False

    def rule_equality(self):
        seq = self.seq
        for n in range(1, len(seq) - 1):
            if seq.t(n) not in ("==", "!=") or seq.k(n) != "op":
                continue
            la = self.operand_left(n - 1, _STOPS_EQ)
            rb = self.operand_right(n + 1, _STOPS_EQ)
            if la > n - 1 or rb < n + 1:
                continue
            kl, kr = self.kind(la, n - 1), self.kind(n + 1, rb)
            if kl not in ("S", "P") and kr not in ("S", "P"):
                continue
            neg = "!" if seq.t(n) == "!=" else ""
            L, R = seq.text(la, n - 1), seq.text(n + 1, rb)
            if "N" in (kl, kr):
                other = R if kl == "N" else L
                self.ctx.used.add("_cs_str_empty")
                self.replace(la, rb, "%s_cs_str_empty(%s)" % (neg, other))
                return True
            for kk, txt, at in ((kl, L, la), (kr, R, n + 1)):
                if kk is None:
                    self.err("cannot tell the type of `%s` in a string comparison" % txt.strip(), at)
            self.ctx.used.add("_cs_str_eq")
            self.replace(la, rb, "%s_cs_str_eq(%s, %s)" % (neg, L, R))
            return True
        return False

    def cat_text(self, spans, at):
        """Nested helper calls for the operands in *spans*."""
        seq = self.seq
        parts = []
        for a, b in spans:
            parts.append((seq.text(a, b), self.kind(a, b), a))
        first = next((i for i, p in enumerate(parts) if p[1] in ("S", "P")), None)
        if first is None:
            self.err("a string concatenation with no string operand", at)
        for txt, k, a in parts:
            if k is None:
                self.err("cannot tell the type of `%s` in a string concatenation" % txt.strip(), a)
            if k == "O":
                self.err("`%s` is not a string or a number, so concatenating it calls "
                         "its ToString(), which is not lowered" % txt.strip(), a)
        helper = {"S": "_cs_cat_s", "P": "_cs_cat_s", "I": "_cs_cat_i", "F": "_cs_cat_f",
                  "C": "_cs_cat_c", "B": "_cs_cat_b"}
        if first > 0:
            acc = "_cs_cat_%s(\"\", %s)" % ({"F": "f", "C": "c"}.get(parts[0][1], "i"),
                                           " + ".join(p[0].strip() for p in parts[:first]))
            self.ctx.used.add("_cs_cat_%s" % ({"F": "f", "C": "c"}.get(parts[0][1], "i")))
            rest = parts[first:]
            acc = "_cs_cat_s(%s, %s)" % (acc, rest[0][0].strip())
            self.ctx.used.add("_cs_cat_s")
            rest = rest[1:]
        else:
            acc = parts[0][0].strip()
            rest = parts[1:]
        for txt, k, a in rest:
            if k == "N":
                continue
            h = helper[k]
            self.ctx.used.add(h)
            acc = "%s(%s, %s)" % (h, acc, txt.strip())
        return acc

    def rule_concat(self):
        seq = self.seq
        for n in range(1, len(seq) - 1):
            if seq.t(n) != "+" or seq.k(n) != "op" or not self.binary_after(n - 1):
                continue
            la = self.operand_left(n - 1, _STOPS_CAT)
            rb = self.operand_right(n + 1, _STOPS_CAT)
            spans = self.split_top(la, rb, ("+",))
            kinds = [self.kind(x, y) for x, y in spans]
            if not any(k in ("S", "P") for k in kinds):
                continue
            self.replace(la, rb, self.cat_text(spans, n))
            return True
        return False

    def rule_reads(self):
        """An owned string, read: `s` -> `s.c_str()`."""
        seq = self.seq
        for n in range(len(seq)):
            if seq.k(n) != "id" or seq.t(n) in _NOT_TYPE:
                continue
            name = seq.t(n)
            if seq.t(n + 1) == "(":
                continue
            if seq.t(n + 1) == "." and seq.t(n + 2) in _FIELD_FOLLOWERS:
                continue
            if seq.t(n - 1) == "fastring" if n > 0 else False:
                continue
            if n > 0 and seq.t(n - 1) == ".":
                if n >= 2 and seq.t(n - 2) in self.ctx.class_names or True:
                    k = self.ctx.member_kind(name)
            else:
                k = self.atom(n)
            if k == "S":
                self.replace(n, n, "%s.c_str()" % name)
                return True
        return False

    def run(self):
        self.scan_locals()
        for rule in (self.rule_static_members, self.rule_decl, self.rule_assign,
                     self.rule_length, self.rule_index, self.rule_equality,
                     self.rule_concat, self.rule_reads):
            for _ in range(2000):
                if not rule():
                    break
            else:
                raise StrError("string rewrite did not settle in %s" % rule.__name__,
                               self.base_line)
        return self.text


# ---------------------------------------------------------------------------
# The file: signatures, constructors, characters, prelude
# ---------------------------------------------------------------------------

_ESC = {"n": 10, "t": 9, "r": 13, "0": 0, "\\": 92, "'": 39, '"': 34,
        "a": 7, "b": 8, "f": 12, "v": 11}


def char_value(lit):
    body = lit[1:-1]
    if body.startswith("\\"):
        c = body[1:]
        if c and c[0] in "xu":
            return int(c[1:], 16)
        if c in _ESC:
            return _ESC[c]
        raise StrError("unknown character escape %s" % lit)
    if len(body) != 1:
        raise StrError("character literal %s is not one character" % lit)
    return ord(body)


_PRELUDE_HEAD = '''/* C# strings (csrust --coost): coost's fastring, spliced in by cpprust */
#include <string.h>
#include <stdlib.h>
#include "co/fastring.h"
#include "src/mem.cc"
#include "src/fast.cc"
#include "src/fastring.cc"
'''

_RING = '''static fastring _cs_ring[16];
static int _cs_ring_i;
static fastring *_cs_slot() {
    fastring *s = &_cs_ring[_cs_ring_i];
    _cs_ring_i = (_cs_ring_i + 1) & 15;
    s->clear();
    return s;
}
'''

_HELPERS = {
    "_cs_cat_s": 'static const char *_cs_cat_s(const char *a, const char *b) { fastring *s = _cs_slot(); if (a != 0) { s->append_cstr(a); } if (b != 0) { s->append_cstr(b); } return s->c_str(); }\n',
    "_cs_cat_i": 'static const char *_cs_cat_i(const char *a, long long b) { fastring *s = _cs_slot(); if (a != 0) { s->append_cstr(a); } s->append_int(b); return s->c_str(); }\n',
    "_cs_cat_f": 'static const char *_cs_cat_f(const char *a, double b) { fastring *s = _cs_slot(); if (a != 0) { s->append_cstr(a); } s->append_double(b, 6); return s->c_str(); }\n',
    "_cs_cat_c": 'static const char *_cs_cat_c(const char *a, int b) { fastring *s = _cs_slot(); if (a != 0) { s->append_cstr(a); } s->append_char((char)b); return s->c_str(); }\n',
    "_cs_cat_b": 'static const char *_cs_cat_b(const char *a, int b) { fastring *s = _cs_slot(); if (a != 0) { s->append_cstr(a); } if (b != 0) { s->append_cstr("True"); } else { s->append_cstr("False"); } return s->c_str(); }\n',
    "_cs_str_empty": 'static int _cs_str_empty(const char *a) { return a == 0 || a[0] == 0; }\n',
    "_cs_str_eq": 'static int _cs_str_eq(const char *a, const char *b) { if (a == 0) { a = ""; } if (b == 0) { b = ""; } return strcmp(a, b) == 0; }\n',
    "_cs_at": 'static int _cs_at(const char *s, int n, int i) { if (i < 0 || i >= n) { abort(); } return (unsigned char)s[i]; }\n',
    "_cs_atp": 'static int _cs_atp(const char *s, int i) { int n = (int)strlen(s); if (i < 0 || i >= n) { abort(); } return (unsigned char)s[i]; }\n',
}


# `Dictionary<string, V>`: a sorted array of (fastring, V), searched by strcmp.
# It mirrors the `map` cpprust supplies (the member names csrust's dictionary
# lowering already emits -- at_ptr, count, erase, size, clear, operator[]) but
# takes a `const char *` key, so a literal, a parameter or a concatenation's
# result is a key as it stands; a key is copied into the map when it is added.
_SMAP = '''template<typename V>
class _cs_smap {
    fastring *kd;
    V *vd;
    int pn;
    int pcap;
public:
    _cs_smap() { kd = 0; vd = 0; pn = 0; pcap = 0; }
    ~_cs_smap() { clear(); free(kd); free(vd); kd = 0; vd = 0; pcap = 0; }
    int size() { return pn; }
    void clear() {
        while (pn > 0) {
            pn = pn - 1;
            __cpp_drop(fastring, kd[pn]);
            __cpp_drop(V, vd[pn]);
        }
    }
    void reserve(int c) {
        if (c > pcap) {
            fastring *nk;
            V *nv;
            nk = (fastring *)realloc(kd, sizeof(fastring) * c);
            if (nk) { kd = nk; }
            nv = (V *)realloc(vd, sizeof(V) * c);
            if (nv) { vd = nv; }
            if (nk && nv) { pcap = c; }
        }
    }
    const char *key_at(int i) { fastring *p = kd + i; return p->c_str(); }
    int lower_index(const char *k) {
        int lo = 0;
        int hi = pn;
        int mid;
        while (lo < hi) {
            mid = lo + (hi - lo) / 2;
            if (strcmp(key_at(mid), k) < 0) { lo = mid + 1; }
            else { hi = mid; }
        }
        return lo;
    }
    int find_index(const char *k) {
        int i = lower_index(k);
        if (i < pn) { if (strcmp(key_at(i), k) == 0) { return i; } }
        return -1;
    }
    int count(const char *k) { if (find_index(k) < 0) { return 0; } return 1; }
    V &operator[](const char *k) {
        int i = lower_index(k);
        if (i < pn) { if (strcmp(key_at(i), k) == 0) { return vd[i]; } }
        if (pn == pcap) { reserve(pcap ? pcap * 2 : 8); }
        if (pn == pcap) { abort(); }
        if (pn > i) {
            memmove(kd + i + 1, kd + i, (unsigned long)((pn - i) * (int)sizeof(fastring)));
            memmove(vd + i + 1, vd + i, (unsigned long)((pn - i) * (int)sizeof(V)));
        }
        memset(&vd[i], 0, sizeof(V));
        fastring *np = kd + i;
        memset(np, 0, sizeof(fastring));
        np->assign_cstr(k);
        pn = pn + 1;
        return vd[i];
    }
    int erase(const char *k) {
        int i = find_index(k);
        if (i < 0) { return 0; }
        __cpp_drop(fastring, kd[i]);
        __cpp_drop(V, vd[i]);
        if (pn - i - 1 > 0) {
            memmove(kd + i, kd + i + 1, (unsigned long)((pn - i - 1) * (int)sizeof(fastring)));
            memmove(vd + i, vd + i + 1, (unsigned long)((pn - i - 1) * (int)sizeof(V)));
        }
        pn = pn - 1;
        return 1;
    }
    V *at_ptr(const char *k) {
        int i = find_index(k);
        if (i < 0) { abort(); }
        return &vd[i];
    }
};
'''


def prelude(text):
    """The coost include block and the helpers *text* calls, or ''."""
    names = set(re.findall(r"\b_cs_\w+", text))
    if "fastring" not in text and not names:
        return ""
    out = _PRELUDE_HEAD
    if "_cs_smap" in text:
        out += _SMAP
    if any(n.startswith("_cs_cat_") for n in names):
        out += _RING
    for n in sorted(names):
        if n in _HELPERS:
            out += _HELPERS[n]
    return out


def string_dictionaries(text):
    """`Dictionary<string, V>` -> `_cs_smap<V>`, wherever it is written.

    A field, a local, a parameter, a `new`: the type is one spelling. A value
    that is itself a string (`Dictionary<string, string>`) is not lowered yet.
    """
    toks = tokenize(text)
    sig = [i for i, t in enumerate(toks) if t.k not in _SKIP]
    edits = []
    n = 0
    while n < len(sig) - 3:
        i = sig[n]
        if toks[i].t == "Dictionary" and toks[sig[n + 1]].t == "<" and \
                toks[sig[n + 2]].t == "string" and toks[sig[n + 3]].t == ",":
            depth, m = 1, n + 2
            while m < len(sig) and depth > 0:
                m += 1
                t = toks[sig[m]].t
                depth += (t == "<") - (t == ">") - 2 * (t == ">>")
            close = sig[m]
            value = [toks[sig[x]].t for x in range(n + 4, m)]
            closer = toks[close].t
            if depth < 0:               # `>>`: this one's `>` and an outer one
                value = [toks[sig[x]].t for x in range(n + 4, m)]
            if "string" in value:
                line = untokenize(toks[:i]).count("\n") + 1
                raise StrError("Dictionary<string, string>: a string value is not "
                               "lowered yet (a string-keyed map of numbers or "
                               "objects is)", line)
            # drop a `System.Collections.Generic.` qualifier
            start = i
            q = n
            while q >= 2 and toks[sig[q - 1]].t == "." and toks[sig[q - 2]].k == "id":
                q -= 2
            start = sig[q]
            vtxt = "".join(untokenize(toks[sig[n + 4]:sig[m - 1] + 1]) if m - 1 >= n + 4 else "")
            repl = "_cs_smap<%s>%s" % (vtxt.strip(), ">" if closer == ">>" else "")
            edits.append((start, close + 1, repl))
            n = m
        n += 1
    out = list(toks)
    for lo, hi, new in sorted(edits, reverse=True):
        out[lo:hi] = [Tok("ws", new)]
    return untokenize(out)


def lower(text, path="<cs>"):
    """C# text -> C++-subset text with strings as fastring / const char *.

    Raises StrError(message, line) for a form not lowered yet.
    """
    text = string_dictionaries(text)
    toks = tokenize(text)
    seq = Seq(toks)
    classes = discover(seq)
    ctx = Ctx(classes)
    edits = []          # (token_lo, token_hi_exclusive, text)

    def sig_edit(a, b, new):
        edits.append((seq.sig[a], seq.sig[b] + 1, new))

    def line_of(n):
        return untokenize(toks[:seq.sig[n]]).count("\n") + 1

    for cls in classes:
        has_ctor = any(m.what == "ctor" for m in cls.members)
        inits = []
        for m in cls.members:
            if m.kind == "X" or any(p[1] == "X" for p in m.params):
                a = m.type_span[0] if m.type_span else m.start
                raise StrError("%s: a string collection is not lowered yet "
                               "(`string[]`, `List<string>`, `Dictionary<string, T>`)"
                               % (m.name or cls.name), line_of(a))
            for p in m.params:
                if p[1] == "S" and p[3]:
                    raise StrError("`%s string %s`: a string cannot be passed by "
                                   "reference" % (p[3][0], p[0]), line_of(p[2][0]))
            if m.what == "field":
                if m.kind == "S":
                    if getattr(m, "const", False):
                        raise StrError("`const string %s`: not lowered yet; use a "
                                       "literal where it is read" % m.name,
                                       line_of(m.type_span[0]))
                    for name, init, a in m.decls:
                        if init is not None:
                            if m.static:
                                raise StrError("`static string %s = ...`: a static "
                                               "string's initializer is not lowered "
                                               "yet; assign it at startup" % name,
                                               line_of(a))
                            inits.append("%s = %s;" % (name, seq.text(init[0], init[1])))
                    # drop initializers, retype
                    first = m.decls[0]
                    end = m.end - 1 if seq.t(m.end) == ";" else m.end
                    names = ", ".join(d[0] for d in m.decls)
                    sig_edit(m.type_span[0], end, "fastring %s" % names)
                elif m.kind == "C":
                    sig_edit(m.type_span[0], m.type_span[1], "int")
            elif m.what == "prop":
                if m.kind == "S":
                    if any(a[1] is None for a in m.accessors):
                        raise StrError("`string %s { get; set; }`: an automatic "
                                       "string property would hold a pointer; write a "
                                       "field, or accessors with a body" % m.name,
                                       line_of(m.type_span[0]))
                    sig_edit(m.type_span[0], m.type_span[1], "_cs_str")
                elif m.kind == "C":
                    sig_edit(m.type_span[0], m.type_span[1], "int")
            else:
                if m.type_span:
                    if m.kind == "S":
                        sig_edit(m.type_span[0], m.type_span[1], "_cs_str")
                    elif m.kind == "C":
                        sig_edit(m.type_span[0], m.type_span[1], "int")
                for name, kind, (ta, tb), _mods in m.params:
                    if kind == "S":
                        sig_edit(ta, tb, "_cs_str")
                    elif kind == "C":
                        sig_edit(ta, tb, "int")

        def body_edit(lb, rb, env, extra_prefix=""):
            if rb == lb + 1 and not extra_prefix:
                return
            lo, hi = seq.sig[lb] + 1, seq.sig[rb]
            base = untokenize(toks[:lo]).count("\n") + 1
            inner = extra_prefix + untokenize(toks[lo:hi])
            try:
                out = Body(ctx, inner, env, cls.name, base, path).run()
            except StrError:
                raise
            except Exception as e:                    # a bug here, not in the C#
                raise StrError("internal error in the string pass (%s: %s) -- please "
                               "report it with this file" % (type(e).__name__, e), base)
            edits.append((lo, hi, out))

        for m in cls.members:
            env = {}
            for name, kind, _span, _mods in m.params:
                env[name] = "P" if kind == "S" else kind
            if m.what == "ctor" and m.body:
                body_edit(m.body[0], m.body[1], env, " ".join(inits) + " " if inits else "")
            elif m.what == "method" and m.body:
                body_edit(m.body[0], m.body[1], env)
            elif m.what == "prop":
                for acc, lb, rb in m.accessors:
                    if lb is not None:
                        e2 = dict(env)
                        if acc in ("set", "init"):
                            e2["value"] = "P" if m.kind == "S" else m.kind
                        body_edit(lb, rb, e2)
        if inits and not has_ctor:
            sig_edit(cls.lb, cls.lb, "{ public %s() { %s }" % (cls.name, " ".join(inits)))
            # that ctor body is a string statement list: lower it now
            e = edits.pop()
            text_ctor = Body(ctx, " " + " ".join(inits) + " ", {}, cls.name,
                             line_of(cls.lb), path).run()
            edits.append((e[0], e[1], "{ public %s() {%s}" % (cls.name, text_ctor)))

    # apply, last first
    edits.sort(key=lambda e: e[0], reverse=True)
    out = list(toks)
    for lo, hi, new in edits:
        out[lo:hi] = [Tok("ws", new)]
    # characters are bytes: `'a'` is 97 and `char` is `int`. On the edited text,
    # re-read: the rewritten bodies are in it too.
    res = []
    for t in tokenize(untokenize(out)):
        if t.k == "chr":
            res.append(Tok("num", str(char_value(t.t))))
        elif t.k == "id" and t.t == "char":
            res.append(Tok("id", "int"))
        else:
            res.append(t)
    return untokenize(res)
