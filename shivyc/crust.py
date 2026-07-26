"""Crust: a minimal Rust-syntax front end for ShivyCX.

Crust enforces *function-level syntax isolation*: a translation unit may mix
functions written in ISO C with functions written in Rust syntax. Rust items
are recognized at top level (brace depth 0) by the `fn` keyword, parsed by the
recursive-descent parser in this module, and lowered to equivalent C source
text before the C lexer ever runs. Everything downstream -- the C parser, the
IL generator, the register allocator, every whole-program analysis -- is
unchanged, so Rust functions and C functions land in one compilation unit with
no FFI boundary between them.

Supported subset (v1):

  items       fn, with optional `pub`, `unsafe`, `extern "C"` modifiers
  types       i8 i16 i32 i64 isize u8 u16 u32 u64 usize f32 f64 bool char ()
              *const T  *mut T  &T  &mut T  [T; N]
  statements  let (with `mut`, optional annotation), return, if/else, while,
              loop, for x in a..b / a..=b, break, continue, blocks, exprs
  exprs       literals, paths, calls, indexing, field access, unary and binary
              operators, compound assignment, `as` casts
  tails       a trailing block expression becomes the return value

Line numbers are preserved: the emitter syncs to the line of each source
construct, so diagnostics from later passes point at the Rust source line.
"""

import re

__all__ = ["CrustError", "translate", "has_rust", "translate_file"]


class CrustError(Exception):
    """A Crust front-end (Rust-subset) syntax or type error."""


# --------------------------------------------------------------------------
# Type mapping
# --------------------------------------------------------------------------

PRIMITIVES = {
    "i8": "signed char",
    "i16": "short",
    "i32": "int",
    "i64": "long",
    "isize": "long",
    "u8": "unsigned char",
    "u16": "unsigned short",
    "u32": "unsigned int",
    "u64": "unsigned long",
    "usize": "unsigned long",
    "f32": "float",
    "f64": "double",
    "bool": "_Bool",
    "char": "unsigned int",
    "()": "void",
}


class CType:
    """A C type split into a base specifier and a declarator shape."""

    def __init__(self, base, ptr=0, array=None):
        self.base = base
        self.ptr = ptr
        self.array = array          # list of dimension strings, or None

    def decl(self, name=""):
        """Render this type as a C declaration of `name`."""
        stars = "*" * self.ptr
        dims = "".join("[%s]" % d for d in (self.array or []))
        if not name:
            return (self.base + " " + stars + dims).strip() if (stars or dims) \
                else self.base
        return "%s %s%s%s" % (self.base, stars, name, dims)

    def is_void(self):
        return self.base == "void" and not self.ptr and not self.array

    def is_float(self):
        return self.base in ("float", "double") and not self.ptr

    def __repr__(self):                            # pragma: no cover
        return "CType(%s)" % self.decl()


VOID = CType("void")
INT = CType("int")


# --------------------------------------------------------------------------
# Lexer
# --------------------------------------------------------------------------

KEYWORDS = {
    "fn", "let", "mut", "if", "else", "while", "loop", "for", "in", "return",
    "break", "continue", "as", "true", "false", "pub", "unsafe", "extern",
    "const", "static", "struct", "impl", "match", "use", "mod", "crate",
}

# Longest-first so multi-character operators win.
PUNCT = [
    "<<=", ">>=", "..=", "->", "=>", "..", "::", "==", "!=", "<=", ">=",
    "&&", "||", "<<", ">>", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
    "+", "-", "*", "/", "%", "!", "&", "|", "^", "<", ">", "=", "(", ")",
    "{", "}", "[", "]", ",", ";", ":", ".", "#", "?", "@",
]

_NUM_SUFFIX = re.compile(r"(?:i8|i16|i32|i64|isize|u8|u16|u32|u64|usize"
                         r"|f32|f64)$")


class Token:
    __slots__ = ("kind", "val", "line")

    def __init__(self, kind, val, line):
        self.kind = kind            # ident kw num str chr punc eof
        self.val = val
        self.line = line

    def __repr__(self):                            # pragma: no cover
        return "<%s %r @%d>" % (self.kind, self.val, self.line)


def tokenize(src, line0=1):
    """Lex Rust-subset source into a token list."""
    toks = []
    i, n, line = 0, len(src), line0
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c in " \t\r":
            i += 1
            continue
        # comments
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if src.startswith("/*", j):
                    depth += 1
                    j += 2
                elif src.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    if src[j] == "\n":
                        line += 1
                    j += 1
            if depth:
                raise CrustError("line %d: unterminated block comment" % line)
            i = j
            continue
        # identifiers / keywords
        if c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            word = src[i:j]
            toks.append(Token("kw" if word in KEYWORDS else "ident",
                              word, line))
            i = j
            continue
        # numbers
        if c.isdigit():
            j = i
            if src.startswith(("0x", "0X", "0b", "0o"), i):
                j = i + 2
                while j < n and (src[j].isalnum() or src[j] == "_"):
                    j += 1
            else:
                while j < n and (src[j].isdigit() or src[j] == "_"):
                    j += 1
                if j < n and src[j] == "." and j + 1 < n and \
                        src[j + 1].isdigit():
                    j += 1
                    while j < n and (src[j].isdigit() or src[j] == "_"):
                        j += 1
                while j < n and (src[j].isalnum() or src[j] == "_"):
                    j += 1
            toks.append(Token("num", src[i:j], line))
            i = j
            continue
        # strings
        if c == '"':
            j, buf = i + 1, ['"']
            while j < n and src[j] != '"':
                if src[j] == "\\":
                    buf.append(src[j:j + 2])
                    j += 2
                    continue
                if src[j] == "\n":
                    line += 1
                buf.append(src[j])
                j += 1
            if j >= n:
                raise CrustError("line %d: unterminated string" % line)
            buf.append('"')
            toks.append(Token("str", "".join(buf), line))
            i = j + 1
            continue
        # chars (Rust lifetimes are not supported, so `'` is always a literal)
        if c == "'":
            j, buf = i + 1, ["'"]
            while j < n and src[j] != "'":
                if src[j] == "\\":
                    buf.append(src[j:j + 2])
                    j += 2
                    continue
                buf.append(src[j])
                j += 1
            if j >= n:
                raise CrustError("line %d: unterminated char literal" % line)
            buf.append("'")
            toks.append(Token("chr", "".join(buf), line))
            i = j + 1
            continue
        for p in PUNCT:
            if src.startswith(p, i):
                toks.append(Token("punc", p, line))
                i += len(p)
                break
        else:
            raise CrustError("line %d: unexpected character %r" % (line, c))
    toks.append(Token("eof", "", line))
    return toks


# --------------------------------------------------------------------------
# Output buffer with line syncing
# --------------------------------------------------------------------------

class Out:
    """Accumulates C text while tracking (and syncing to) source lines."""

    def __init__(self, line):
        self.parts = []
        self.line = line
        self.col = 0

    def sync(self, line):
        """Emit newlines until output line >= `line`."""
        while self.line < line:
            self.parts.append("\n")
            self.line += 1
            self.col = 0

    def write(self, text):
        self.parts.append(text)
        nl = text.count("\n")
        if nl:
            self.line += nl
            self.col = len(text) - text.rfind("\n") - 1
        else:
            self.col += len(text)

    def line_at(self, line, text, indent=1):
        """Emit `text` on source line `line`, indented."""
        self.sync(line)
        if self.col:
            self.write(" ")
        else:
            self.write("    " * indent)
        self.write(text)

    def text(self):
        return "".join(self.parts)


# --------------------------------------------------------------------------
# Parser / emitter
# --------------------------------------------------------------------------

BINARY_LEVELS = [
    ["||"], ["&&"],
    ["==", "!=", "<", ">", "<=", ">="],
    ["|"], ["^"], ["&"],
    ["<<", ">>"],
    ["+", "-"],
    ["*", "/", "%"],
]

ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
              "<<=", ">>="}


class Expr:
    """A translated expression: C text plus its inferred C type."""

    __slots__ = ("code", "type")

    def __init__(self, code, type_):
        self.code = code
        self.type = type_


class Parser:
    def __init__(self, toks, fn_sigs=None):
        self.toks = toks
        self.i = 0
        self.fn_sigs = fn_sigs or {}    # name -> (CType ret, [CType] params)
        self.scopes = [{}]              # name -> CType
        self.ret_type = VOID

    # -- token helpers ----------------------------------------------------

    @property
    def cur(self):
        return self.toks[self.i]

    def peek(self, k=1):
        j = min(self.i + k, len(self.toks) - 1)
        return self.toks[j]

    def at(self, val, kind=None):
        t = self.cur
        return t.val == val and (kind is None or t.kind == kind)

    def next(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def accept(self, val):
        if self.cur.val == val and self.cur.kind in ("punc", "kw"):
            return self.next()
        return None

    def expect(self, val):
        if not self.accept(val):
            raise CrustError("line %d: expected %r, found %r"
                             % (self.cur.line, val, self.cur.val or "<eof>"))
        return self.toks[self.i - 1]

    def expect_ident(self):
        if self.cur.kind != "ident":
            raise CrustError("line %d: expected identifier, found %r"
                             % (self.cur.line, self.cur.val or "<eof>"))
        return self.next().val

    # -- scopes -----------------------------------------------------------

    def push(self):
        self.scopes.append({})

    def pop(self):
        self.scopes.pop()

    def declare(self, name, type_):
        self.scopes[-1][name] = type_

    def lookup(self, name):
        for s in reversed(self.scopes):
            if name in s:
                return s[name]
        return None

    # -- types ------------------------------------------------------------

    def parse_type(self):
        t = self.cur
        if t.val == "(" and self.peek().val == ")":
            self.next()
            self.next()
            return CType("void")
        if t.val == "*":
            self.next()
            if not (self.accept("const") or self.accept("mut")):
                raise CrustError("line %d: raw pointer needs `const` or `mut`"
                                 % t.line)
            inner = self.parse_type()
            return CType(inner.base, inner.ptr + 1)
        if t.val == "&":
            self.next()
            self.accept("mut")
            inner = self.parse_type()
            return CType(inner.base, inner.ptr + 1)
        if t.val == "[":
            self.next()
            inner = self.parse_type()
            self.expect(";")
            size = self.parse_expr()
            self.expect("]")
            return CType(inner.base, inner.ptr, (inner.array or []) +
                         [size.code])
        if t.kind in ("ident", "kw"):
            name = self.next().val
            if name in PRIMITIVES:
                return CType(PRIMITIVES[name])
            # Unknown named type: assume a C struct/typedef of the same name.
            return CType(name)
        raise CrustError("line %d: expected a type, found %r"
                         % (t.line, t.val or "<eof>"))

    # -- expressions ------------------------------------------------------

    def parse_expr(self):
        return self.parse_assign()

    def parse_assign(self):
        lhs = self.parse_binary(0)
        if self.cur.kind == "punc" and self.cur.val in ASSIGN_OPS:
            op = self.next().val
            rhs = self.parse_assign()
            return Expr("%s %s %s" % (lhs.code, op, rhs.code), lhs.type)
        return lhs

    def parse_binary(self, level):
        if level >= len(BINARY_LEVELS):
            return self.parse_cast()
        ops = BINARY_LEVELS[level]
        left = self.parse_binary(level + 1)
        while self.cur.kind == "punc" and self.cur.val in ops:
            op = self.next().val
            right = self.parse_binary(level + 1)
            if op in ("||", "&&", "==", "!=", "<", ">", "<=", ">="):
                type_ = CType("_Bool")
            elif left.type is not None and not left.type.is_void():
                type_ = left.type
            else:
                type_ = right.type
            left = Expr("(%s %s %s)" % (left.code, op, right.code), type_)
        return left

    def parse_cast(self):
        e = self.parse_unary()
        while self.accept("as"):
            ty = self.parse_type()
            e = Expr("(%s)(%s)" % (ty.decl(), e.code), ty)
        return e

    def parse_unary(self):
        t = self.cur
        if t.kind == "punc" and t.val in ("-", "!", "*", "&"):
            self.next()
            if t.val == "&":
                self.accept("mut")
                e = self.parse_unary()
                return Expr("&%s" % e.code,
                            CType(e.type.base, e.type.ptr + 1)
                            if e.type else None)
            e = self.parse_unary()
            if t.val == "*":
                ty = CType(e.type.base, max(e.type.ptr - 1, 0)) \
                    if e.type else None
                return Expr("(*%s)" % e.code, ty)
            if t.val == "!":
                # Rust `!` is logical on bool and bitwise on integers.
                if e.type is not None and e.type.base == "_Bool" \
                        and not e.type.ptr:
                    return Expr("(!%s)" % e.code, CType("_Bool"))
                return Expr("(~%s)" % e.code, e.type)
            return Expr("(-%s)" % e.code, e.type)
        return self.parse_postfix()

    def parse_postfix(self):
        e = self.parse_primary()
        while True:
            if self.at("(", "punc"):
                self.next()
                args = []
                while not self.at(")", "punc"):
                    args.append(self.parse_expr().code)
                    if not self.accept(","):
                        break
                self.expect(")")
                ret = self.fn_sigs.get(e.code, (None, None))[0]
                e = Expr("%s(%s)" % (e.code, ", ".join(args)), ret)
            elif self.at("[", "punc"):
                self.next()
                idx = self.parse_expr()
                self.expect("]")
                ty = e.type
                if ty is not None:
                    ty = CType(ty.base, max(ty.ptr - 1, 0),
                               (ty.array or [None])[1:] or None)
                e = Expr("%s[%s]" % (e.code, idx.code), ty)
            elif self.at(".", "punc"):
                self.next()
                field = self.expect_ident()
                e = Expr("%s.%s" % (e.code, field), None)
            else:
                return e

    def parse_primary(self):
        t = self.next()
        if t.kind == "num":
            return Expr(*normalize_number(t))
        if t.kind == "str":
            return Expr(t.val, CType("char", 1))
        if t.kind == "chr":
            return Expr(t.val, CType("int"))
        if t.val == "true":
            return Expr("1", CType("_Bool"))
        if t.val == "false":
            return Expr("0", CType("_Bool"))
        if t.val == "(":
            e = self.parse_expr()
            self.expect(")")
            return Expr("(%s)" % e.code, e.type)
        if t.kind == "ident":
            name = t.val
            while self.at("::", "punc"):        # path: foo::bar -> foo_bar
                self.next()
                name += "_" + self.expect_ident()
            ty = self.lookup(name)
            if ty is None and name in self.fn_sigs:
                ty = None                        # function designator
            return Expr(name, ty)
        raise CrustError("line %d: unexpected %r in expression"
                         % (t.line, t.val or "<eof>"))

    # -- statements -------------------------------------------------------

    def parse_block(self, out, indent, tail_returns):
        """Parse `{ ... }`, emitting C into `out`.

        `tail_returns` is True when a trailing expression should become a
        return statement (the function-body case).
        """
        open_tok = self.expect("{")
        out.line_at(open_tok.line, "{", indent)
        self.push()
        while not self.at("}", "punc"):
            if self.cur.kind == "eof":
                raise CrustError("line %d: unterminated block" % self.cur.line)
            self.parse_stmt(out, indent + 1, tail_returns)
        close = self.expect("}")
        self.pop()
        out.line_at(close.line, "}", indent)

    def parse_stmt(self, out, indent, tail_returns):
        t = self.cur

        if t.val == "let" and t.kind == "kw":
            self.next()
            self.accept("mut")
            name = self.expect_ident()
            ty = None
            if self.accept(":"):
                ty = self.parse_type()
            init = None
            if self.accept("="):
                init = self.parse_expr()
            self.expect(";")
            if ty is None:
                if init is None or init.type is None:
                    raise CrustError(
                        "line %d: cannot infer a type for `%s`; add an "
                        "annotation (`let %s: i32 = ...`)"
                        % (t.line, name, name))
                ty = init.type
            self.declare(name, ty)
            code = ty.decl(name)
            if init is not None:
                code += " = " + init.code
            out.line_at(t.line, code + ";", indent)
            return

        if t.val == "return" and t.kind == "kw":
            self.next()
            if self.accept(";"):
                out.line_at(t.line, "return;", indent)
                return
            e = self.parse_expr()
            self.expect(";")
            out.line_at(t.line, "return %s;" % e.code, indent)
            return

        if t.val == "break" and t.kind == "kw":
            self.next()
            self.expect(";")
            out.line_at(t.line, "break;", indent)
            return

        if t.val == "continue" and t.kind == "kw":
            self.next()
            self.expect(";")
            out.line_at(t.line, "continue;", indent)
            return

        if t.val == "if" and t.kind == "kw":
            self.parse_if(out, indent, tail_returns)
            return

        if t.val == "while" and t.kind == "kw":
            self.next()
            cond = self.parse_expr()
            out.line_at(t.line, "while (%s)" % cond.code, indent)
            self.parse_block(out, indent, False)
            return

        if t.val == "loop" and t.kind == "kw":
            self.next()
            out.line_at(t.line, "while (1)", indent)
            self.parse_block(out, indent, False)
            return

        if t.val == "for" and t.kind == "kw":
            self.next()
            var = self.expect_ident()
            self.expect("in")
            lo = self.parse_expr()
            if self.accept("..="):
                cmp_op = "<="
            else:
                self.expect("..")
                cmp_op = "<"
            hi = self.parse_expr()
            ity = wider(lo.type, hi.type)
            out.line_at(t.line, "for (%s = %s; %s %s %s; %s++)"
                        % (ity.decl(var), lo.code, var, cmp_op, hi.code, var),
                        indent)
            self.push()
            self.declare(var, ity)
            self.parse_block(out, indent, False)
            self.pop()
            return

        if t.val == "{" and t.kind == "punc":
            self.parse_block(out, indent, False)
            return

        # expression statement, or a trailing expression
        e = self.parse_expr()
        if self.accept(";"):
            out.line_at(t.line, e.code + ";", indent)
            return
        if not self.at("}", "punc"):
            raise CrustError("line %d: expected `;` after expression"
                             % self.cur.line)
        if tail_returns and not self.ret_type.is_void():
            out.line_at(t.line, "return %s;" % e.code, indent)
        else:
            out.line_at(t.line, e.code + ";", indent)

    def parse_if(self, out, indent, tail_returns):
        t = self.expect("if")
        cond = self.parse_expr()
        out.line_at(t.line, "if (%s)" % cond.code, indent)
        self.parse_block(out, indent, tail_returns)
        if self.accept("else"):
            if self.at("if", "kw"):
                out.write(" else")
                # keep `else if` on the branch's own line
                nxt = self.cur
                out.sync(nxt.line)
                if out.col == 0:
                    out.write("    " * indent)
                    out.write("else ")
                else:
                    out.write(" ")
                self.parse_if_tail(out, indent, tail_returns)
            else:
                out.write(" else")
                self.parse_block(out, indent, tail_returns)

    def parse_if_tail(self, out, indent, tail_returns):
        t = self.expect("if")
        cond = self.parse_expr()
        out.write("if (%s)" % cond.code)
        self.parse_block(out, indent, tail_returns)
        if self.accept("else"):
            if self.at("if", "kw"):
                out.write(" else ")
                self.parse_if_tail(out, indent, tail_returns)
            else:
                out.write(" else")
                self.parse_block(out, indent, tail_returns)

    # -- items ------------------------------------------------------------

    def parse_fn_signature(self):
        """Parse `[pub] [unsafe] [extern "C"] fn name(params) [-> T]`."""
        while self.cur.val in ("pub", "unsafe"):
            self.next()
        if self.accept("extern"):
            if self.cur.kind == "str":
                self.next()
        start = self.expect("fn")
        name = self.expect_ident()
        self.expect("(")
        params = []
        while not self.at(")", "punc"):
            self.accept("mut")
            pname = self.expect_ident()
            self.expect(":")
            ptype = self.parse_type()
            params.append((pname, ptype))
            if not self.accept(","):
                break
        self.expect(")")
        ret = VOID
        if self.accept("->"):
            ret = self.parse_type()
        return start, name, params, ret

    def parse_fn(self, out):
        start, name, params, ret = self.parse_fn_signature()
        synth_main_ret = False
        if name == "main" and ret.is_void():
            ret = INT
            synth_main_ret = True
        self.ret_type = ret
        out.line_at(start.line, "%s(%s)" % (ret.decl(name),
                                            render_params(params)), 0)
        self.push()
        for pname, ptype in params:
            self.declare(pname, ptype)
        # body, with the trailing expression becoming the return value
        open_tok = self.cur
        if not self.at("{", "punc"):
            raise CrustError("line %d: expected function body" % open_tok.line)
        self.expect("{")
        out.line_at(open_tok.line, "{", 0)
        self.push()
        while not self.at("}", "punc"):
            if self.cur.kind == "eof":
                raise CrustError("line %d: unterminated function body"
                                 % self.cur.line)
            self.parse_stmt(out, 1, True)
        close = self.expect("}")
        self.pop()
        if synth_main_ret:
            out.line_at(close.line, "return 0;", 1)
        out.line_at(close.line, "}", 0)
        self.pop()
        return name, ret, [p[1] for p in params]


_RANK = {"signed char": 1, "unsigned char": 1, "short": 2, "unsigned short": 2,
         "int": 3, "unsigned int": 4, "long": 5, "unsigned long": 6}


def wider(a, b):
    """Pick the loop induction type for `lo..hi`, favoring the wider bound.

    Keeps `for i in 0..n` from comparing a signed `int` against an unsigned
    `usize` bound, which would otherwise warn or wrap at the boundary.
    """
    cands = [t for t in (a, b)
             if t is not None and not t.is_void() and not t.ptr
             and t.base in _RANK]
    if not cands:
        return INT
    return max(cands, key=lambda t: _RANK[t.base])


def render_params(params):
    if not params:
        return "void"
    return ", ".join(ty.decl(nm) for nm, ty in params)


def normalize_number(tok):
    """Map a Rust numeric literal onto C, returning (code, CType)."""
    text = tok.val.replace("_", "")
    m = _NUM_SUFFIX.search(text)
    suffix = ""
    if m:
        suffix = m.group(0)
        text = text[:m.start()]
    if text.startswith(("0b", "0B")):
        text = str(int(text[2:], 2))
    elif text.startswith(("0o", "0O")):
        text = str(int(text[2:], 8))
    is_float = ("." in text or
                (("e" in text or "E" in text) and not
                 text.startswith(("0x", "0X"))))
    if suffix.startswith("f") or is_float:
        if suffix == "f32":
            return text + "f", CType("float")
        return text, CType("double")
    if suffix:
        base = PRIMITIVES[suffix]
        c_suffix = ""
        if "unsigned" in base:
            c_suffix += "u"
        if base.endswith("long"):
            c_suffix += "l"
        return text + c_suffix, CType(base)
    return text, CType("int")


# --------------------------------------------------------------------------
# Mixed-source splicing
# --------------------------------------------------------------------------

_FN_START = re.compile(r"\bfn\s+([A-Za-z_]\w*)\s*\(")
_MODIFIER = re.compile(r"(?:\b(?:pub|unsafe)\b\s+|\bextern\s+\"C\"\s+)*$")


def _blank(code):
    """Return `code` with comments and literals blanked, offsets preserved."""
    out = list(code)
    i, n = 0, len(code)
    while i < n:
        c = code[i]
        if c == "/" and i + 1 < n and code[i + 1] == "/":
            while i < n and code[i] != "\n":
                out[i] = " "
                i += 1
        elif c == "/" and i + 1 < n and code[i + 1] == "*":
            j = code.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif c in "\"'":
            quote, j = c, i + 1
            out[i] = " "
            while j < n and code[j] != quote:
                if code[j] == "\\":
                    out[j] = " "
                    if j + 1 < n and out[j + 1] != "\n":
                        out[j + 1] = " "
                    j += 2
                    continue
                if out[j] != "\n":
                    out[j] = " "
                j += 1
            if j < n:
                out[j] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out)


def _depths(scan):
    """Return a list mapping each offset to its brace nesting depth."""
    depth, res = 0, []
    for c in scan:
        if c == "}":
            depth -= 1
        res.append(depth)
        if c == "{":
            depth += 1
    return res


def _match_brace(scan, open_idx):
    depth = 0
    for j in range(open_idx, len(scan)):
        if scan[j] == "{":
            depth += 1
        elif scan[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    return None


def find_rust_items(code):
    """Yield (start, end) spans of top-level Rust `fn` items in `code`."""
    scan = _blank(code)
    depths = _depths(scan)
    spans = []
    for m in _FN_START.finditer(scan):
        start = m.start()
        if depths[start] != 0:
            continue
        # C has no `fn` keyword, but be sure this isn't `foo.fn(` etc.
        prev = scan[:start].rstrip()
        if prev and prev[-1] in ".>#":
            continue
        open_idx = scan.find("{", m.end())
        if open_idx < 0:
            continue
        # a `->` return type may intervene; the body is the next brace
        close = _match_brace(scan, open_idx)
        if close is None:
            raise CrustError("unterminated Rust function body near offset %d"
                             % start)
        # extend backwards over pub / unsafe / extern "C"
        head = _MODIFIER.search(scan[:start])
        real_start = head.start() if head else start
        spans.append((real_start, close + 1))
    return spans


def has_rust(code):
    """True if `code` contains at least one top-level Rust function."""
    try:
        return bool(find_rust_items(code))
    except CrustError:
        return False


def _line_of(code, offset):
    return code.count("\n", 0, offset) + 1


def translate(code):
    """Lower every top-level Rust function in `code` to C.

    C text outside those functions is passed through byte-for-byte, and line
    numbers are preserved throughout, so diagnostics from later compiler
    passes still point at the original source lines.
    """
    spans = find_rust_items(code)
    if not spans:
        return code

    # Pass 1: collect signatures so calls between Rust functions type-check
    # and so order of definition does not matter.
    sigs = {}
    for start, end in spans:
        p = Parser(tokenize(code[start:end], _line_of(code, start)))
        _, name, params, ret = p.parse_fn_signature()
        if name == "main" and ret.is_void():
            ret = INT
        sigs[name] = (ret, [t for _, t in params])

    # Pass 2: translate each item in place.
    pieces, prev = [], 0
    for start, end in spans:
        pieces.append(code[prev:start])
        line0 = _line_of(code, start)
        parser = Parser(tokenize(code[start:end], line0), sigs)
        out = Out(line0)
        parser.parse_fn(out)
        text = out.text()
        # pad so the item occupies exactly as many lines as the original
        want = code.count("\n", start, end)
        have = text.count("\n")
        if have < want:
            text += "\n" * (want - have)
        elif have > want:                          # pragma: no cover
            raise CrustError("internal: line drift in generated C")
        pieces.append(text)
        prev = end
    pieces.append(code[prev:])
    body = "".join(pieces)

    # Forward declarations, on one physical line so line numbers hold.
    protos = " ".join(
        "%s(%s);" % (ret.decl(name),
                     ", ".join(t.decl() for t in ps) if ps else "void")
        for name, (ret, ps) in sigs.items() if name != "main")
    return (protos + " " + body) if protos else body


def translate_file(path):
    with open(path, encoding="utf-8") as f:
        return translate(f.read())
