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
  tails       a trailing expression becomes the return value

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
    "str": "const char",
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
            if stars or dims:
                return (self.base + " " + stars + dims).strip()
            return self.base
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
    "enum",
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


class MethodInfo:
    """A lowered `impl` method: how it is named and how it takes `self`."""

    __slots__ = ("owner", "name", "mangled", "ret", "self_kind", "params")

    def __init__(self, owner, name, ret, self_kind, params):
        self.owner = owner
        self.name = name
        self.mangled = "%s_%s" % (owner, name)
        self.ret = ret
        self.self_kind = self_kind      # "ref", "value" or "none"
        self.params = params


class Unit:
    """Whole-translation-unit tables shared by every Crust parse.

    Collected in a first pass over all Rust items so that definition order
    does not matter: a method may call a function declared later, a struct
    may be used before its definition, and so on.
    """

    def __init__(self):
        self.fn_sigs = {}       # name -> (CType ret, [CType] params)
        self.structs = {}       # name -> [(field, CType)]
        self.methods = {}       # (type name, method name) -> MethodInfo
        self.enums = {}         # name -> [(variant, explicit value or None)]
        self.variants = {}      # mangled variant name -> enum name
        self.tuple_structs = set()   # struct names declared in tuple form
        self.consts = {}        # const/static name -> CType
        self.needs = set()      # libc prototypes the lowering requires
        self.slices = {}        # slice struct name -> element CType
        self.options = {}       # Option struct name -> element CType
        self.unwraps = set()    # option types needing an unwrap helper

    def option_type(self, elem):
        """Return (and register) the tagged struct for `Option<elem>`.

        Crust has no generics, so each instantiation is monomorphised into
        its own struct, generated on demand exactly like a slice.
        """
        name = "crust_option_" + re.sub(r"\W+", "_", elem.decl()).strip("_")
        if name not in self.options:
            self.options[name] = elem
            self.structs[name] = [("some", CType("_Bool")),
                                  ("value", elem)]
        return CType(name)

    def slice_type(self, elem):
        """Return (and register) the fat-pointer struct type for `&[elem]`."""
        name = "crust_slice_" + re.sub(r"\W+", "_", elem.decl()).strip("_")
        if name not in self.slices:
            self.slices[name] = elem
            self.structs[name] = [("ptr", CType(elem.base, elem.ptr + 1)),
                                  ("len", CType("unsigned long"))]
        return CType(name)

    def field_type(self, struct_name, field):
        for fname, ftype in self.structs.get(struct_name, ()):
            if fname == field:
                return ftype
        return None


class Parser:
    def __init__(self, toks, unit=None):
        self.toks = toks
        self.i = 0
        self.unit = unit or Unit()
        self.fn_sigs = self.unit.fn_sigs
        self.scopes = [{}]              # name -> CType
        self.ret_type = VOID
        self.impl_type = None           # enclosing `impl` type name, if any
        self.no_struct_lit = 0          # >0 while parsing a condition
        self.behind_ref = 0             # >0 while parsing the pointee of `&`
        self.expected = []              # target types, for inferring `None`
        self.tmp_n = 0                  # counter for generated temporaries

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

    def err(self, msg, *args):
        """Raise a CrustError tagged with the line being parsed."""
        line = self.toks[max(self.i - 1, 0)].line
        raise CrustError("line %d: %s" % (line, msg % args if args else msg))

    def expect_gt(self):
        """Consume a closing `>`.

        A `>>` is one token, so a nested `Option<Option<T>>` splits it.
        """
        if self.at(">>", "punc"):
            self.toks[self.i] = Token("punc", ">", self.cur.line)
            return
        self.expect(">")

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

    def _is_slice_type(self):
        """Distinguish `&[T]` (slice) from `&[T; N]` (array reference)."""
        depth = 0
        for j in range(self.i, len(self.toks)):
            v = self.toks[j].val
            if v == "[":
                depth += 1
            elif v == "]":
                depth -= 1
                if depth == 0:
                    return True
            elif v == ";" and depth == 1:
                return False
        return True

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
            if self.at("[", "punc") and self._is_slice_type():
                self.next()
                elem = self.parse_type()
                self.expect("]")
                return self.unit.slice_type(elem)
            self.behind_ref += 1
            try:
                inner = self.parse_type()
            finally:
                self.behind_ref -= 1
            return CType(inner.base, inner.ptr + 1)
        if t.val == "[":
            self.next()
            inner = self.parse_type()
            self.expect(";")
            size = self.parse_expr()
            self.expect("]")
            dims = (inner.array or []) + [size.code]
            return CType(inner.base, inner.ptr, dims)
        if t.kind in ("ident", "kw"):
            name = self.next().val
            if name in PRIMITIVES:
                if name == "str" and not self.behind_ref:
                    self.err("`str` is unsized; write `&str`")
                return CType(PRIMITIVES[name])
            if name == "Option":
                self.expect("<")
                elem = self.parse_type()
                self.expect_gt()
                if elem.is_void():
                    self.err("`Option<()>` is not supported")
                return self.unit.option_type(elem)
            if name == "Self":
                if self.impl_type is None:
                    raise CrustError("line %d: `Self` outside an impl block"
                                     % t.line)
                return CType(self.impl_type)
            # Unknown named type: assume a C struct/typedef of the same name.
            return CType(name)
        raise CrustError("line %d: expected a type, found %r"
                         % (t.line, t.val or "<eof>"))

    # -- expressions ------------------------------------------------------

    def parse_expr(self):
        return self.parse_assign()

    def parse_expr_as(self, ty):
        """Parse an expression whose target type is known.

        `None` carries no type of its own, so it is resolved from the
        context it appears in -- an annotation, a return type or a parameter.
        """
        self.expected.append(ty)
        try:
            return self.parse_expr()
        finally:
            self.expected.pop()

    @property
    def target(self):
        return self.expected[-1] if self.expected else None

    def new_temp(self):
        self.tmp_n += 1
        return "_crust_opt%d" % self.tmp_n

    def parse_cond(self):
        """Parse an expression in condition position.

        Rust forbids a bare struct literal here so that the brace opening the
        body is unambiguous; Crust follows the same rule.
        """
        self.no_struct_lit += 1
        try:
            return self.parse_assign()
        finally:
            self.no_struct_lit -= 1

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
                if e.type is not None and e.type.base in self.unit.slices:
                    return e            # already a fat pointer
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
                if e.code == "Some":
                    e = self.parse_some()
                    continue
                params = self.fn_sigs.get(e.code, (None, []))[1] or []
                args = []
                while not self.at(")", "punc"):
                    want = params[len(args)] if len(args) < len(params) \
                        else None
                    args.append(self.parse_expr_as(want).code)
                    if not self.accept(","):
                        break
                self.expect(")")
                if e.code in self.unit.tuple_structs:
                    e = self.tuple_struct_literal(e.code, args)
                else:
                    ret = self.fn_sigs.get(e.code, (None, None))[0]
                    e = Expr("%s(%s)" % (e.code, ", ".join(args)), ret)
            elif self.at("[", "punc"):
                self.next()
                lo = None
                if not self.at("..", "punc"):
                    lo = self.parse_expr()
                if self.at("..", "punc"):
                    e = self.parse_slicing(e, lo)
                    continue
                self.expect("]")
                e = self.index(e, lo)
            elif self.at(".", "punc"):
                self.next()
                if self.cur.kind == "num":      # tuple field: `p.0`
                    e = self.field_access(e, "_" + self.next().val)
                    continue
                name = self.expect_ident()
                if self.at("(", "punc"):
                    e = self.parse_method_call(e, name)
                else:
                    e = self.field_access(e, name)
            else:
                return e

    def parse_some(self):
        """Lower `Some(x)` to a filled-in Option struct."""
        want = self.target
        elem = None
        if want is not None and want.base in self.unit.options:
            elem = self.unit.options[want.base]
        inner = self.parse_expr_as(elem)
        self.expect(")")
        if elem is None:
            elem = inner.type
        if elem is None:
            self.err("cannot infer the type held by `Some`; annotate the "
                     "target (`let x: Option<i32> = ...`)")
        opt = self.unit.option_type(elem)
        return Expr("(%s){1, %s}" % (opt.base, inner.code), opt)

    def none_expr(self):
        """Lower `None`, whose type comes from the surrounding context."""
        want = self.target
        if want is None or want.base not in self.unit.options:
            self.err("cannot infer the type of `None`; annotate the target "
                     "(`let x: Option<i32> = None`)")
        return Expr("(%s){0}" % want.base, want)

    def option_method(self, recv, name, args):
        """Lower the supported `Option` methods."""
        elem = self.unit.options[recv.type.base]
        if name == "is_some" and not args:
            return Expr("%s.some" % recv.code, CType("_Bool"))
        if name == "is_none" and not args:
            return Expr("(!%s.some)" % recv.code, CType("_Bool"))
        if name == "unwrap" and not args:
            self.unit.unwraps.add(recv.type.base)
            self.unit.needs.add("abort")
            return Expr("%s_unwrap(%s)" % (recv.type.base, recv.code), elem)
        if name == "unwrap_or" and len(args) == 1:
            return Expr("(%s.some ? %s.value : %s)"
                        % (recv.code, recv.code, args[0]), elem)
        self.err("no method `%s` on `Option`; supported: is_some, is_none, "
                 "unwrap, unwrap_or", name)

    def index(self, recv, idx):
        """Lower `recv[idx]`, reaching through a slice's data pointer."""
        ty = recv.type
        if ty is not None and ty.base in self.unit.slices:
            elem = self.unit.slices[ty.base]
            return Expr("%s.ptr[%s]" % (recv.code, idx.code), elem)
        if ty is not None:
            ty = CType(ty.base, max(ty.ptr - 1, 0),
                       (ty.array or [None])[1:] or None)
        return Expr("%s[%s]" % (recv.code, idx.code), ty)

    def parse_slicing(self, recv, lo):
        """Lower `a[..]`, `a[lo..]`, `a[lo..hi]` to a fat pointer."""
        self.expect("..")
        self.accept("=")                 # `..=` end bound is inclusive
        inclusive = self.toks[self.i - 1].val == "="
        hi = None
        if not self.at("]", "punc"):
            hi = self.parse_expr()
        self.expect("]")

        ty = recv.type
        if ty is None:
            self.err("cannot slice a value of unknown type")
        if ty.base in self.unit.slices:
            elem = self.unit.slices[ty.base]
            base, total = recv.code + ".ptr", recv.code + ".len"
        elif ty.array:
            elem = CType(ty.base, ty.ptr)
            base, total = recv.code, ty.array[0]
        elif ty.ptr:
            elem = CType(ty.base, ty.ptr - 1)
            base, total = recv.code, None
        else:
            self.err("`%s` cannot be sliced", ty.decl())

        if hi is None and total is None:
            self.err("slicing a raw pointer needs an end bound, as its "
                     "length is not known")
        end = total if hi is None else hi.code
        if inclusive:
            end = "(%s) + 1" % end
        start = "0" if lo is None else lo.code
        ptr = base if lo is None else "%s + %s" % (base, start)
        length = end if lo is None else "(%s) - (%s)" % (end, start)
        slice_ty = self.unit.slice_type(elem)
        return Expr("(%s){%s, %s}" % (slice_ty.base, ptr, length), slice_ty)

    def field_access(self, recv, field):
        """Lower `recv.field`, auto-dereferencing a pointer receiver.

        Rust's `.` reaches through a reference; C's does not, so a receiver of
        pointer type lowers to `->`.
        """
        ty = recv.type
        if ty is not None and ty.ptr == 1:
            code = "%s->%s" % (recv.code, field)
        elif ty is not None and ty.ptr > 1:
            self.err("field access through a double pointer is not "
                     "supported")
        else:
            code = "%s.%s" % (recv.code, field)
        ftype = None
        if ty is not None:
            ftype = self.unit.field_type(ty.base, field)
            if ftype is None and ty.base in self.unit.structs:
                self.err("struct `%s` has no field `%s`", ty.base, field)
        return Expr(code, ftype)

    def parse_method_call(self, recv, name):
        """Lower `recv.method(args)` to the free function `Type_method`."""
        self.expect("(")
        args = []
        while not self.at(")", "punc"):
            args.append(self.parse_expr().code)
            if not self.accept(","):
                break
        self.expect(")")

        if recv.type is not None and recv.type.base in self.unit.options:
            return self.option_method(recv, name, args)
        if recv.type is not None and recv.type.base in self.unit.slices:
            if name == "len":
                return Expr("%s.len" % recv.code, CType("unsigned long"))
            if name == "as_ptr":
                return Expr("%s.ptr" % recv.code,
                            CType(self.unit.slices[recv.type.base].base,
                                  self.unit.slices[recv.type.base].ptr + 1))
            self.err("no method `%s` on a slice", name)
        if recv.type is not None and recv.type.ptr == 1 \
                and recv.type.base == "const char" and name == "len":
            # `&str` is a plain C string here, so its length is strlen.
            if args:
                self.err("`str::len` takes no arguments")
            self.unit.needs.add("strlen")
            return Expr("strlen(%s)" % recv.code, CType("unsigned long"))
        if recv.type is None:
            self.err("cannot infer the type of the receiver of `.%s()`; "
                     "annotate it", name)
        owner = recv.type.base
        info = self.unit.methods.get((owner, name))
        if info is None:
            self.err("no method `%s` on type `%s`", name, owner)
        if info.self_kind == "none":
            self.err("`%s::%s` is an associated function; call it as "
                     "`%s::%s(...)`", owner, name, owner, name)

        # Rust auto-refs/derefs the receiver; C needs it spelled out.
        if info.self_kind == "ref":
            recv_code = recv.code if recv.type.ptr else "&" + _addressable(
                recv.code)
        else:
            recv_code = ("(*%s)" % recv.code) if recv.type.ptr else recv.code
        return Expr("%s(%s)" % (info.mangled,
                                ", ".join([recv_code] + args)), info.ret)

    def parse_struct_literal(self, name, line):
        """Lower `Name { f: e, .. }` to a C compound literal."""
        self.expect("{")
        inits = []
        seen = set()
        while not self.at("}", "punc"):
            field = self.expect_ident()
            self.expect(":")
            val = self.parse_expr()
            known = self.unit.structs.get(name) is not None
            if known and self.unit.field_type(name, field) is None:
                raise CrustError("line %d: struct `%s` has no field `%s`"
                                 % (line, name, field))
            seen.add(field)
            inits.append(".%s = %s" % (field, val.code))
            if not self.accept(","):
                break
        self.expect("}")
        missing = [f for f, _ in self.unit.structs.get(name, ())
                   if f not in seen]
        if missing:
            raise CrustError("line %d: missing field%s %s in initializer of "
                             "`%s`" % (line, "" if len(missing) == 1 else "s",
                                       ", ".join("`%s`" % f for f in missing),
                                       name))
        return Expr("(%s){%s}" % (name, ", ".join(inits)), CType(name))

    def parse_if_expr(self):
        """Lower `if c { a } else { b }` in expression position to `c ? a : b`.

        Only value-producing arms are accepted here; an `if` whose arms hold
        statements is a statement, and is handled by `parse_stmt`.
        """
        self.expect("if")
        cond = self.parse_cond()
        then = self.parse_block_expr()
        if not self.accept("else"):
            self.err("`if` used as an expression needs an `else` arm")
        if self.at("if", "kw"):
            other = self.parse_if_expr()
        else:
            other = self.parse_block_expr()
        ty = then.type if then.type is not None else other.type
        return Expr("(%s ? %s : %s)" % (cond.code, then.code, other.code), ty)

    def parse_block_expr(self):
        """Parse `{ expr }` used as a value."""
        self.expect("{")
        e = self.parse_expr()
        if not self.at("}", "punc"):
            self.err("only a single expression is allowed in this block "
                     "when `if` is used as an expression")
        self.expect("}")
        return e

    def parse_array_literal(self, open_tok):
        """Lower `[a, b, c]` and the `[0; N]` repeat form to a C initializer.

        C has no repeat-initializer syntax, so only the all-zero form can be
        lowered without knowing `N` at translation time; `{0}` zero-fills the
        whole array.
        """
        if self.at("]", "punc"):
            self.next()
            self.err("empty array literal has no element type")
        first = self.parse_expr()
        if self.accept(";"):
            self.parse_expr()               # length: taken from the annotation
            self.expect("]")
            if first.code.strip() not in ("0", "0.0"):
                self.err("only the all-zero repeat form `[0; N]` is "
                         "supported; list the elements explicitly")
            return Expr("{0}", None)
        items = [first]
        while self.accept(","):
            if self.at("]", "punc"):
                break
            items.append(self.parse_expr())
        self.expect("]")
        ty = items[0].type
        if ty is not None:
            ty = CType(ty.base, ty.ptr, (ty.array or []) + [str(len(items))])
        return Expr("{%s}" % ", ".join(i.code for i in items), ty)

    def tuple_struct_literal(self, name, args):
        """Lower `P(a, b)` for a tuple struct to a compound literal."""
        fields = self.unit.structs.get(name, [])
        if len(args) != len(fields):
            self.err("tuple struct `%s` takes %d field%s, got %d", name,
                     len(fields), "" if len(fields) == 1 else "s", len(args))
        inits = ", ".join(".%s = %s" % (f, a)
                          for (f, _), a in zip(fields, args))
        return Expr("(%s){%s}" % (name, inits), CType(name))

    def parse_primary(self):
        t = self.next()
        if t.kind == "num":
            return Expr(*normalize_number(t))
        if t.kind == "str":
            return Expr(t.val, CType("const char", 1))
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
        if t.val == "[" and t.kind == "punc":
            return self.parse_array_literal(t)
        if t.val == "if" and t.kind == "kw":
            self.i -= 1
            return self.parse_if_expr()
        if t.kind == "ident" or (t.kind == "kw" and t.val == "Self"):
            name = t.val
            if name == "Self" and self.impl_type:
                name = self.impl_type
            while self.at("::", "punc"):        # path: foo::bar -> foo_bar
                self.next()
                name += "_" + self.expect_ident()
            # `Name { ... }` is a struct literal, except in a condition
            # position, where Rust also treats the brace as a block.
            if (self.at("{", "punc") and not self.no_struct_lit
                    and name in self.unit.structs):
                return self.parse_struct_literal(name, t.line)
            if name == "None" and not self.at("::", "punc"):
                return self.none_expr()
            ty = self.lookup(name)
            if ty is None:
                if name in self.unit.variants:
                    ty = CType(self.unit.variants[name])
                elif name in self.unit.consts:
                    ty = self.unit.consts[name]
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

        if t.val == "match" and t.kind == "kw":
            self.parse_match(out, indent, tail_returns)
            return

        if t.val in ("const", "static") and t.kind == "kw":
            self.parse_const_item(out, indent, local=True)
            return

        if t.val == "let" and t.kind == "kw":
            self.next()
            self.accept("mut")
            name = self.expect_ident()
            ty = None
            if self.accept(":"):
                ty = self.parse_type()
            init = None
            if self.accept("="):
                init = self.parse_expr_as(ty)
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
            e = self.parse_expr_as(self.ret_type)
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
            if self.peek().val == "let":
                self.parse_if_let(out, indent, tail_returns)
            else:
                self.parse_if(out, indent, tail_returns)
            return

        if t.val == "while" and t.kind == "kw":
            if self.peek().val == "let":
                self.parse_while_let(out, indent)
                return
            self.next()
            cond = self.parse_cond()
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
            lo = self.parse_cond()
            if self.accept("..="):
                cmp_op = "<="
            else:
                self.expect("..")
                cmp_op = "<"
            hi = self.parse_cond()
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
        e = self.parse_expr_as(self.ret_type if tail_returns else None)
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
        cond = self.parse_cond()
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
        self.expect("if")
        cond = self.parse_cond()
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

    def parse_let_pattern(self):
        """Parse `let Some(NAME) = EXPR` and return (name, expr, elem)."""
        self.expect("let")
        head = self.expect_ident()
        if head != "Some":
            self.err("only `if let Some(x) = ...` is supported, not `%s`",
                     head)
        self.expect("(")
        binding = self.expect_ident()
        self.expect(")")
        self.expect("=")
        subject = self.parse_cond()
        if subject.type is None or subject.type.base not in self.unit.options:
            self.err("`if let Some(..)` needs an `Option`; annotate the "
                     "subject if its type cannot be inferred")
        return binding, subject, self.unit.options[subject.type.base]

    def parse_if_let(self, out, indent, tail_returns):
        """Lower `if let Some(x) = e { .. } else { .. }`.

        The subject is held in a temporary so it is evaluated once, and the
        whole construct is wrapped in a block to scope that temporary.
        """
        t = self.expect("if")
        binding, subject, elem = self.parse_let_pattern()
        tmp = self.new_temp()
        out.line_at(t.line, "{ %s = %s;" % (subject.type.decl(tmp),
                                            subject.code), indent)
        out.write(" if (%s.some)" % tmp)
        self.push()
        self.declare(binding, elem)
        self.emit_binding_block(out, indent, tail_returns, binding, elem, tmp)
        self.pop()
        if self.accept("else"):
            out.write(" else")
            if self.at("if", "kw"):
                if self.peek().val == "let":
                    self.parse_if_let(out, indent, tail_returns)
                else:
                    self.parse_if_tail(out, indent, tail_returns)
            else:
                self.parse_block(out, indent, tail_returns)
        out.write(" }")

    def parse_while_let(self, out, indent):
        """Lower `while let Some(x) = e { .. }` to a loop with a break."""
        t = self.expect("while")
        binding, subject, elem = self.parse_let_pattern()
        tmp = self.new_temp()
        out.line_at(t.line, "for (;;) {", indent)
        out.write(" %s = %s;" % (subject.type.decl(tmp), subject.code))
        out.write(" if (!%s.some) break;" % tmp)
        self.push()
        self.declare(binding, elem)
        self.emit_binding_block(out, indent, False, binding, elem, tmp)
        self.pop()
        out.write(" }")

    def emit_binding_block(self, out, indent, tail_returns, binding, elem,
                           tmp):
        """Emit `{ elem binding = tmp.value; <body> }` for a `let` pattern."""
        open_tok = self.expect("{")
        out.line_at(open_tok.line, "{ %s = %s.value;"
                    % (elem.decl(binding), tmp), indent)
        self.push()
        while not self.at("}", "punc"):
            if self.cur.kind == "eof":
                self.err("unterminated block")
            self.parse_stmt(out, indent + 1, tail_returns)
        close = self.expect("}")
        self.pop()
        out.line_at(close.line, "}", indent)

    def parse_match(self, out, indent, tail_returns):
        """Lower `match` to a C `switch`.

        Rust arms do not fall through, so each arm ends in an explicit
        `break`. When the scrutinee is an enum and no `_` arm is present, the
        arms must cover every variant -- Crust reports the missing ones
        instead of silently falling through.
        """
        t = self.expect("match")
        scrutinee = self.parse_cond()
        out.line_at(t.line, "switch (%s)" % scrutinee.code, indent)
        open_tok = self.expect("{")
        out.line_at(open_tok.line, "{", indent)

        enum_name = None
        sty = scrutinee.type
        if sty is not None and not sty.ptr and sty.base in self.unit.enums:
            enum_name = sty.base
        covered, has_default = set(), False

        while not self.at("}", "punc"):
            if self.cur.kind == "eof":
                self.err("unterminated match")
            arm = self.cur
            labels = self.parse_patterns(enum_name, covered)
            if labels is None:
                has_default = True
                out.line_at(arm.line, "default:", indent + 1)
            else:
                out.line_at(arm.line, " ".join("case %s:" % v
                                               for v in labels), indent + 1)
            self.expect("=>")
            self.parse_arm_body(out, indent + 2, tail_returns)
            out.write(" break;")
            self.accept(",")

        close = self.expect("}")
        out.line_at(close.line, "}", indent)

        if enum_name and not has_default:
            missing = [v for v, _ in self.unit.enums[enum_name]
                       if "%s_%s" % (enum_name, v) not in covered]
            if missing:
                self.err("non-exhaustive match on `%s`: %s not covered; "
                         "add an arm or `_`", enum_name,
                         ", ".join("`%s`" % m for m in missing))

    def parse_patterns(self, enum_name, covered):
        """Parse `P | Q | R` before `=>`; return None for the `_` wildcard."""
        if self.cur.kind == "ident" and self.cur.val == "_":
            self.next()
            return None
        labels = []
        while True:
            labels.append(self.parse_pattern(enum_name, covered))
            if not self.accept("|"):
                return labels

    def parse_pattern(self, enum_name, covered):
        """Parse one `match` pattern into a C `case` label."""
        t = self.next()
        if t.kind == "num":
            return normalize_number(t)[0]
        if t.kind == "chr":
            return t.val
        if t.val == "true":
            return "1"
        if t.val == "false":
            return "0"
        if t.val == "-" and self.cur.kind == "num":
            return "-" + normalize_number(self.next())[0]
        if t.kind == "ident":
            name = t.val
            while self.at("::", "punc"):
                self.next()
                name += "_" + self.expect_ident()
            if name not in self.unit.variants:
                # A bare name would bind, not compare; Crust has no bindings.
                if enum_name and "%s_%s" % (enum_name, name) \
                        in self.unit.variants:
                    name = "%s_%s" % (enum_name, name)
                elif name not in self.unit.consts:
                    self.err("`%s` is not a constant pattern; only literals, "
                             "enum variants and `_` are supported", name)
            covered.add(name)
            return name
        self.err("unsupported pattern %r", t.val or "<eof>")

    def parse_arm_body(self, out, indent, tail_returns):
        """Parse the body of a match arm: a block or a single expression."""
        if self.at("{", "punc"):
            self.parse_block(out, indent, tail_returns)
            return
        t = self.cur
        e = self.parse_expr()
        if tail_returns and not self.ret_type.is_void():
            out.line_at(t.line, "return %s;" % e.code, indent)
        else:
            out.line_at(t.line, e.code + ";", indent)

    def parse_enum(self):
        """Parse `enum Name { A, B = 5, C }`, returning (name, variants)."""
        self.skip_attributes()
        while self.cur.val in ("pub",):
            self.next()
        self.expect("enum")
        name = self.expect_ident()
        self.expect("{")
        variants = []
        while not self.at("}", "punc"):
            self.skip_attributes()
            vname = self.expect_ident()
            if self.at("(", "punc") or self.at("{", "punc"):
                self.err("data-carrying enum variant `%s` is not supported",
                         vname)
            value = None
            if self.accept("="):
                value = self.parse_expr().code
            variants.append((vname, value))
            if not self.accept(","):
                break
        self.expect("}")
        return name, variants

    def parse_const_signature(self):
        """Parse `const|static [mut] NAME: T = expr;` without emitting."""
        self.skip_attributes()
        while self.cur.val in ("pub",):
            self.next()
        kw = self.next().val                     # const or static
        self.accept("mut")
        name = self.expect_ident()
        self.expect(":")
        ty = self.parse_type()
        self.expect("=")
        init = self.parse_expr()
        self.expect(";")
        return kw, name, ty, init

    def parse_const_item(self, out, indent, local=False):
        """Emit a `const`/`static` item as a C declaration."""
        t = self.cur
        kw, name, ty, init = self.parse_const_signature()
        self.unit.consts[name] = ty
        if local:
            self.declare(name, ty)
        quals = "" if local and kw == "static" else ""
        prefix = "const " if kw == "const" else ""
        if not local:
            prefix = "static " + prefix
        out.line_at(t.line, "%s%s%s = %s;" % (prefix, quals, ty.decl(name),
                                              init.code), indent)

    def skip_attributes(self):
        """Skip `#[...]` outer attributes, which Crust does not interpret."""
        while self.at("#", "punc"):
            self.next()
            if not self.at("[", "punc"):
                raise CrustError("line %d: expected `[` after `#`"
                                 % self.cur.line)
            depth = 0
            while True:
                t = self.next()
                if t.kind == "eof":
                    self.err("unterminated attribute")
                if t.val == "[":
                    depth += 1
                elif t.val == "]":
                    depth -= 1
                    if depth == 0:
                        break

    def parse_struct(self):
        """Parse `struct Name { f: T, ... }`, returning (name, fields)."""
        self.skip_attributes()
        while self.cur.val in ("pub",):
            self.next()
        self.expect("struct")
        name = self.expect_ident()
        self.expect("{")
        fields = []
        while not self.at("}", "punc"):
            self.skip_attributes()
            while self.cur.val in ("pub",):
                self.next()
            fname = self.expect_ident()
            self.expect(":")
            ftype = self.parse_type()
            if ftype.is_void():
                self.err("field `%s` of `%s` has unit type", fname, name)
            fields.append((fname, ftype))
            if not self.accept(","):
                break
        self.expect("}")
        return name, fields

    def parse_tuple_struct(self):
        """Parse `struct P(T, U);`, naming the fields `_0`, `_1`, ..."""
        self.skip_attributes()
        while self.cur.val in ("pub",):
            self.next()
        self.expect("struct")
        name = self.expect_ident()
        self.expect("(")
        fields = []
        while not self.at(")", "punc"):
            while self.cur.val in ("pub",):
                self.next()
            fields.append(("_%d" % len(fields), self.parse_type()))
            if not self.accept(","):
                break
        self.expect(")")
        self.expect(";")
        if not fields:
            self.err("tuple struct `%s` needs at least one field", name)
        return name, fields

    def is_assoc_const(self):
        """True if an `impl` body item is `const NAME: T = ...;`."""
        j = self.i
        while self.toks[j].val in ("pub", "#"):
            if self.toks[j].val == "#":
                return False                     # attribute; let the parser
            j += 1                               # handle it normally
        return self.toks[j].val == "const" and self.toks[j].kind == "kw"

    def parse_impl_header(self):
        """Parse `impl Name {`, returning the type name."""
        self.skip_attributes()
        self.expect("impl")
        name = self.expect_ident()
        self.expect("{")
        return name

    def parse_self_param(self):
        """Parse a leading `self` / `&self` / `&mut self` parameter.

        Returns "ref", "value", or "none" if the method takes no receiver.
        """
        ref_self = (self.peek().val == "self"
                    or (self.peek().val == "mut"
                        and self.peek(2).val == "self"))
        if self.at("&", "punc") and ref_self:
            self.next()
            self.accept("mut")
            self.next()                          # self
            return "ref"
        if self.cur.val == "self":
            self.next()
            return "value"
        return "none"

    def parse_method_signature(self, owner):
        """Parse an `impl` method header into a MethodInfo."""
        self.skip_attributes()
        while self.cur.val in ("pub", "unsafe"):
            self.next()
        start = self.expect("fn")
        name = self.expect_ident()
        self.expect("(")
        self_kind = self.parse_self_param()
        if self_kind != "none":
            self.accept(",")
        params = []
        while not self.at(")", "punc"):
            self.accept("mut")
            pname = self.expect_ident()
            self.expect(":")
            params.append((pname, self.parse_type()))
            if not self.accept(","):
                break
        self.expect(")")
        ret = VOID
        if self.accept("->"):
            ret = self.parse_type()
        return start, MethodInfo(owner, name, ret, self_kind, params)

    def skip_to_body_end(self):
        """Skip a `{ ... }` body, used when only collecting signatures."""
        if not self.at("{", "punc"):
            raise CrustError("line %d: expected a body" % self.cur.line)
        depth = 0
        while True:
            t = self.next()
            if t.kind == "eof":
                self.err("unterminated body")
            if t.val == "{" and t.kind == "punc":
                depth += 1
            elif t.val == "}" and t.kind == "punc":
                depth -= 1
                if depth == 0:
                    return

    def parse_impl(self, out):
        """Translate an `impl` block into free functions."""
        owner = self.parse_impl_header()
        self.impl_type = owner
        while not self.at("}", "punc"):
            if self.cur.kind == "eof":
                self.err("unterminated impl block")
            if self.is_assoc_const():
                self.parse_const_signature()     # hoisted into the prelude
                continue
            start, info = self.parse_method_signature(owner)
            params = list(info.params)
            if info.self_kind == "ref":
                params.insert(0, ("self", CType(owner, 1)))
            elif info.self_kind == "value":
                params.insert(0, ("self", CType(owner)))
            self.emit_fn_body(out, start, info.mangled, params, info.ret,
                              False)
        self.expect("}")
        self.impl_type = None

    def parse_fn_signature(self):
        """Parse `[pub] [unsafe] [extern "C"] fn name(params) [-> T]`."""
        self.skip_attributes()
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
        self.emit_fn_body(out, start, name, params, ret, synth_main_ret)
        return name, ret, [p[1] for p in params]

    def emit_fn_body(self, out, start, name, params, ret, synth_main_ret):
        """Emit a C function definition, parsing the Rust body that follows.

        Shared by plain `fn` items and by `impl` methods, which differ only in
        their name mangling and in the synthetic `self` parameter.
        """
        prev_ret = self.ret_type
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
        self.ret_type = prev_ret


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


def _addressable(code):
    """Wrap `code` in parens if `&` would otherwise bind too loosely."""
    if code.isidentifier() or (code.startswith("(") and code.endswith(")")):
        return code
    if all(c.isalnum() or c in "_.->[]" for c in code):
        return code
    return "(%s)" % code


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
    is_hex = text.startswith(("0x", "0X"))
    is_float = ("." in text
                or (("e" in text or "E" in text) and not is_hex))
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

_ITEM_START = re.compile(
    r"\b(?P<kw>fn|struct|impl|enum|const|static)\s+"
    r"(?:mut\s+)?(?P<name>[A-Za-z_]\w*)")
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
    """Return (start, end, kind) spans of top-level Rust items in `code`.

    `kind` is "fn", "struct", "tuple_struct", "impl", "enum" or "const". C
    code between the spans is left alone. Because C shares the `struct`,
    `const` and `static` keywords, those are only claimed when the syntax that
    follows is unambiguously Rust: `name: type` fields with no semicolons for
    a struct, and a `NAME: type` annotation for a constant.
    """
    scan = _blank(code)
    depths = _depths(scan)
    spans = []
    for m in _ITEM_START.finditer(scan):
        start = m.start()
        if depths[start] != 0:
            continue
        kw = m.group("kw")
        # C has no `fn`/`impl`/Rust-`enum` syntax, but guard against
        # `foo.fn(...)` and against a name inside a preprocessor directive
        # such as `#define fn(x) ...`. Both checks are line-local: an earlier
        # directive elsewhere in the file says nothing about this item.
        line_start = scan.rfind("\n", 0, start) + 1
        before = scan[line_start:start]
        if before.lstrip().startswith("#"):
            continue
        if before.rstrip().endswith((".", ">")):
            continue

        if kw in ("const", "static"):
            # Rust annotates the type; C never writes `NAME:` here.
            rest = scan[m.end():]
            if not rest.lstrip().startswith(":"):
                continue
            close = scan.find(";", m.end())
            if close < 0:
                raise CrustError("unterminated Rust %s near offset %d"
                                 % (kw, start))
            spans.append((_extend_head(scan, start), close + 1, "const"))
            continue

        after = scan[m.end():]
        if kw == "struct" and after.lstrip().startswith("("):
            # Tuple struct: `struct P(f64, f64);`
            open_idx = scan.index("(", m.end())
            close_paren = _match_paren(scan, open_idx)
            if close_paren is None:
                raise CrustError("unterminated tuple struct near offset %d"
                                 % start)
            semi = scan.find(";", close_paren)
            if semi < 0:
                raise CrustError("tuple struct near offset %d needs `;`"
                                 % start)
            spans.append((_extend_head(scan, start), semi + 1, "tuple_struct"))
            continue

        open_idx = scan.find("{", m.end())
        if open_idx < 0:
            continue
        close = _match_brace(scan, open_idx)
        if close is None:
            raise CrustError("unterminated Rust %s near offset %d"
                             % (kw, start))
        if kw == "struct" and not _is_rust_struct_body(
                scan[open_idx + 1:close]):
            continue                    # a plain C struct definition
        if kw == "enum" and not _is_rust_enum(scan, close):
            continue                    # a plain C enum definition
        real_start = _extend_head(scan, start)
        spans.append((real_start, close + 1, kw))
    # Drop spans nested inside an earlier one (e.g. `fn` inside `impl`).
    spans.sort()
    kept = []
    for span in spans:
        if kept and span[0] < kept[-1][1]:
            continue
        kept.append(span)
    return kept


def _match_paren(scan, open_idx):
    depth = 0
    for j in range(open_idx, len(scan)):
        if scan[j] == "(":
            depth += 1
        elif scan[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return None


def _is_rust_enum(scan, close):
    """True if the `enum X { ... }` ending at `close` is Rust, not C.

    Both languages spell a simple enumeration identically, so the body is no
    help. What separates them is what follows the closing brace: C requires a
    `;` (or a declarator) to finish the declaration, and Rust forbids one.
    """
    rest = scan[close + 1:].lstrip()
    return not rest.startswith(";")


def _is_rust_struct_body(body):
    """True if a `struct X { ... }` body is Rust rather than C."""
    if ";" in body:
        return False                    # C members end in `;`
    return re.search(r"[A-Za-z_]\w*\s*:", body) is not None


def _extend_head(scan, start):
    """Walk `start` backwards over modifiers and `#[...]` attributes."""
    while True:
        head = _MODIFIER.search(scan[:start])
        if head and head.start() < start:
            start = head.start()
            continue
        stripped = scan[:start].rstrip()
        if stripped.endswith("]"):
            depth, j = 0, len(stripped) - 1
            while j >= 0:
                if stripped[j] == "]":
                    depth += 1
                elif stripped[j] == "[":
                    depth -= 1
                    if depth == 0:
                        break
                j -= 1
            if j > 0 and stripped[:j].rstrip().endswith("#"):
                start = stripped[:j].rstrip().rindex("#")
                continue
        return start


def has_rust(code):
    """True if `code` contains at least one top-level Rust item."""
    try:
        return bool(find_rust_items(code))
    except CrustError:
        return False


def _line_of(code, offset):
    return code.count("\n", 0, offset) + 1


def collect_items(code, spans, unit, struct_order, fail=None):
    """Populate `unit` from the Rust items in `code`; return struct names.

    Only signatures and layouts are read here -- no bodies are translated --
    so that a later pass can resolve calls, methods and field types
    regardless of the order things are defined in.
    """
    local = {"structs": [], "enums": [], "consts": [], "impl_consts": []}
    for start, end, kind in spans:
        p = Parser(tokenize(code[start:end], _line_of(code, start)), unit)
        try:
            if kind in ("struct", "tuple_struct"):
                if kind == "struct":
                    name, fields = p.parse_struct()
                else:
                    name, fields = p.parse_tuple_struct()
                    unit.tuple_structs.add(name)
                unit.structs[name] = fields
                struct_order.append(name)
                local["structs"].append(name)
            elif kind == "enum":
                name, variants = p.parse_enum()
                unit.enums[name] = variants
                for vname, _ in variants:
                    unit.variants["%s_%s" % (name, vname)] = name
                local["enums"].append(name)
            elif kind == "const":
                _, name, ty, _ = p.parse_const_signature()
                unit.consts[name] = ty
                local["consts"].append(name)
            elif kind == "fn":
                _, name, params, ret = p.parse_fn_signature()
                if name == "main" and ret.is_void():
                    ret = INT
                unit.fn_sigs[name] = (ret, [t for _, t in params])
            else:                                   # impl
                owner = p.parse_impl_header()
                p.impl_type = owner
                while not p.at("}", "punc") and p.cur.kind != "eof":
                    if p.is_assoc_const():
                        kw, cname, cty, cinit = p.parse_const_signature()
                        mangled = "%s_%s" % (owner, cname)
                        unit.consts[mangled] = cty
                        local["impl_consts"].append(
                            _render_const(kw, mangled, cty, cinit.code))
                        continue
                    _, info = p.parse_method_signature(owner)
                    unit.methods[(owner, info.name)] = info
                    selfp = []
                    if info.self_kind == "ref":
                        selfp = [CType(owner, 1)]
                    elif info.self_kind == "value":
                        selfp = [CType(owner)]
                    unit.fn_sigs[info.mangled] = (
                        info.ret, selfp + [t for _, t in info.params])
                    p.skip_to_body_end()
        except CrustError as e:
            if fail is not None:
                raise fail(e, start)
            raise
    return local


_RS_INCLUDE = re.compile(
    r'^[ \t]*#[ \t]*include[ \t]*(?P<q>["<])(?P<name>[^">]*\.rs)[">]',
    re.MULTILINE)


def find_rs_includes(code):
    """Return the `#include` header spellings that name a `.rs` file."""
    scan = _blank_directives_only(code)
    return [m.group("q") + m.group("name")
            + ('"' if m.group("q") == '"' else ">")
            for m in _RS_INCLUDE.finditer(scan)]


def _blank_directives_only(code):
    """Blank comments so a commented-out `#include` is not seen."""
    return _blank_comments(code)


def _blank_comments(code):
    out, i, n = list(code), 0, len(code)
    while i < n:
        if code.startswith("//", i):
            while i < n and code[i] != "\n":
                out[i] = " "
                i += 1
        elif code.startswith("/*", i):
            j = code.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


def collect_include_items(code, path, _seen=None):
    """Build a Unit seeded from every `.rs` file `code` includes.

    The `#include` directive itself is left in place -- the preprocessor
    expands it, translating the `.rs` file at that point. Reading its items
    here is what lets Rust code in the *including* file use a struct or call
    a method defined in the included one.
    """
    unit, struct_order = Unit(), []
    seen = _seen if _seen is not None else set()
    headers = find_rs_includes(code)
    if not headers:
        return unit, struct_order
    import shivyc.preproc as preproc
    for header in headers:
        try:
            text, filename = preproc.read_file(header, path or ".")
        except IOError:
            continue                    # the preprocessor reports the error
        if filename in seen:
            continue
        seen.add(filename)
        sub_unit, sub_order = collect_include_items(text, filename, seen)
        unit.fn_sigs.update(sub_unit.fn_sigs)
        unit.structs.update(sub_unit.structs)
        unit.methods.update(sub_unit.methods)
        struct_order.extend(sub_order)
        collect_items(text, find_rust_items(text), unit, struct_order)
    return unit, struct_order


def _prelude_offset(code):
    """Pick where the one-line prelude can be prefixed safely.

    It must sit before any use, but it cannot be prefixed onto a line holding
    a preprocessor directive (`#` must come first on its line) or onto a line
    in the interior of a block comment. So: take the first line that is
    neither. Rust items are always non-blank once comments are stripped, so
    this never scans past the first item.
    """
    scan = _blank_comments(code)
    offset = 0
    continuation = False
    for raw, blanked in zip(code.split("\n"), scan.split("\n")):
        stripped = blanked.strip()
        skip = (continuation or stripped.startswith("#")
                or (not stripped and raw.strip()))
        continuation = bool(skip and blanked.rstrip().endswith("\\"))
        if not skip:
            return offset
        offset += len(raw) + 1
    return offset


def _toposort_structs(unit, order):
    """Order struct definitions so a by-value field precedes its user."""
    emitted, result = set(), []

    def visit(name, stack):
        if name in emitted or name not in unit.structs:
            return
        if name in stack:
            raise CrustError("recursive struct `%s` (use a pointer field)"
                             % name)
        stack.add(name)
        for _, ftype in unit.structs[name]:
            if not ftype.ptr:
                visit(ftype.base, stack)
        stack.discard(name)
        emitted.add(name)
        result.append(name)

    for name in order:
        visit(name, set())
    return result


def _render_enum(name, variants):
    """Render a Rust enum as a C enum whose members are `Enum_Variant`."""
    parts = []
    for vname, value in variants:
        member = "%s_%s" % (name, vname)
        parts.append(member if value is None else "%s = %s" % (member, value))
    return "enum %s { %s }; typedef enum %s %s;" % (
        name, ", ".join(parts), name, name)


def _render_const(kw, name, ty, init):
    """Render one `const`/`static` as a C file-scope declaration."""
    if kw == "const" and _fits_c_enum(ty, init):
        return "enum { %s = %s };" % (name, init)
    prefix = "static const " if kw == "const" else "static "
    return "%s%s = %s;" % (prefix, ty.decl(name), init)


def _render_consts(code, spans, unit):
    """Render `const`/`static` items, hoisted so order does not matter.

    A Rust `const` is a compile-time constant, so an integer one is emitted as
    a C enum constant rather than `static const`: only the former is a
    constant expression, and so only the former can size an array. Anything
    else -- floats, pointers, non-literal initializers, `static` -- becomes an
    ordinary file-scope object.
    """
    out = []
    for start, end, kind in spans:
        if kind != "const":
            continue
        p = Parser(tokenize(code[start:end], _line_of(code, start)), unit)
        kw, name, ty, init = p.parse_const_signature()
        out.append(_render_const(kw, name, ty, init.code))
    return out


def _fits_c_enum(ty, init):
    """True if a const can be emitted as a C enum constant.

    C enum members have type `int`, so only integer constants that fit are
    eligible.
    """
    if ty.ptr or ty.array or ty.base not in _RANK:
        return False
    try:
        value = int(init, 0)
    except ValueError:
        return False
    return -(2 ** 31) <= value < 2 ** 31


def _render_struct(name, fields):
    body = " ".join("%s;" % ty.decl(f) for f, ty in fields)
    return "struct %s { %s };" % (name, body)


def translate(code, path=None):
    """Lower every top-level Rust item in `code` to C.

    C text outside those items is passed through byte-for-byte, and line
    numbers are preserved throughout, so diagnostics from later compiler
    passes still point at the original source lines.
    """
    spans = find_rust_items(code)
    included = collect_include_items(code, path)
    if not spans:
        return code

    where = ("%s: " % path) if path else ""
    unit, struct_order = included
    included_fns = set(unit.fn_sigs)
    included_slices = set(unit.slices)
    included_options = set(unit.options)

    def fail(exc, start):
        return CrustError("%sline %d: %s" % (where, _line_of(code, start),
                                             exc)
                          if "line " not in str(exc)
                          else "%s%s" % (where, exc))

    # Pass 1: collect structs, function signatures and methods, so that
    # definition order does not matter anywhere in the unit.
    local = collect_items(code, spans, unit, struct_order, fail)

    # Pass 2: translate each item in place. Struct definitions are hoisted
    # into the prelude (see below), so their source region becomes blank.
    pieces, prev = [], 0
    for start, end, kind in spans:
        pieces.append(code[prev:start])
        line0 = _line_of(code, start)
        if kind in ("struct", "tuple_struct", "enum", "const"):
            text = ""            # hoisted into the prelude
        else:
            parser = Parser(tokenize(code[start:end], line0), unit)
            out = Out(line0)
            try:
                if kind == "fn":
                    parser.parse_fn(out)
                else:
                    parser.parse_impl(out)
            except CrustError as e:
                raise fail(e, start)
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

    # Prelude, on one physical line so no line numbers shift. Order matters:
    # incomplete-type forward declarations, then full struct definitions in
    # dependency order, then function prototypes that may use either.
    prelude = []
    if "strlen" in unit.needs:
        prelude.append("unsigned long strlen(const char *);")
    if "abort" in unit.needs:
        prelude.append("void abort(void);")
    for name in local["enums"]:
        prelude.append(_render_enum(name, unit.enums[name]))
    for name in local["structs"]:
        prelude.append("struct %s; typedef struct %s %s;"
                       % (name, name, name))
    # Slice structs hold only a pointer to their element, so the forward
    # declarations above are enough even for a slice of a user struct.
    for name in unit.options:
        if name not in included_options:
            prelude.append("struct %s; typedef struct %s %s; %s"
                           % (name, name, name,
                              _render_struct(name, unit.structs[name])))
    for name in unit.slices:
        if name not in included_slices:
            prelude.append("struct %s; typedef struct %s %s; %s"
                           % (name, name, name,
                              _render_struct(name, unit.structs[name])))
    for name in _toposort_structs(unit, local["structs"]):
        prelude.append(_render_struct(name, unit.structs[name]))
    for name in sorted(unit.unwraps):
        if name in included_options:
            continue
        elem = unit.options[name]
        prelude.append(
            "static %s %s_unwrap(%s o) { if (!o.some) abort(); "
            "return o.value; }" % (elem.decl(), name, name))
    for text in _render_consts(code, spans, unit):
        prelude.append(text)
    prelude.extend(local["impl_consts"])
    for name, (ret, ps) in unit.fn_sigs.items():
        if name == "main" or name in included_fns:
            continue
        prelude.append("%s(%s);" % (ret.decl(name),
                                    ", ".join(t.decl() for t in ps)
                                    if ps else "void"))
    if not prelude:
        return body
    at = _prelude_offset(body)
    return body[:at] + " ".join(prelude) + " " + body[at:]


def translate_file(path):
    with open(path, encoding="utf-8") as f:
        return translate(f.read())
