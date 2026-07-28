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

import os
import re
import sys

__all__ = ["CrustError", "translate", "has_rust", "translate_file"]


class CrustError(Exception):
    """A Crust front-end (Rust-subset) syntax or type error."""


# --------------------------------------------------------------------------
# Type mapping
# --------------------------------------------------------------------------

_FFI_PRIMITIVES = {
    "c_char": "char", "c_schar": "signed char", "c_uchar": "unsigned char",
    "c_short": "short", "c_ushort": "unsigned short",
    "c_int": "int", "c_uint": "unsigned int",
    "c_long": "long", "c_ulong": "unsigned long",
    "c_longlong": "long long", "c_ulonglong": "unsigned long long",
    "c_float": "float", "c_double": "double", "c_void": "void",
}

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

# `core::ffi` names the C types exactly, so the mapping is not an
# approximation -- real FFI-facing Rust is full of them.
PRIMITIVES.update(_FFI_PRIMITIVES)


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

# Names the prelude may declare via `unit.needs`; never emit a conflicting
# `extern` for these when core templates call them.
_PRELUDE_LIBC = frozenset({
    "malloc", "realloc", "free", "strlen", "abort", "printf", "fprintf",
})
INT = CType("int")


# --------------------------------------------------------------------------
# Lexer
# --------------------------------------------------------------------------

KEYWORDS = {
    "fn", "let", "mut", "if", "else", "while", "loop", "for", "in", "return",
    "break", "continue", "as", "true", "false", "pub", "unsafe", "extern",
    "const", "static", "struct", "impl", "match", "use", "mod", "crate",
    "enum", "trait", "dyn", "where", "type",
}

# Longest-first so multi-character operators win.
PUNCT = [
    "<<=", ">>=", "..=", "->", "=>", "..", "::", "==", "!=", "<=", ">=",
    "&&", "||", "<<", ">>", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
    "+", "-", "*", "/", "%", "!", "&", "|", "^", "<", ">", "=", "(", ")",
    "{", "}", "[", "]", ",", ";", ":", ".", "#", "?", "@", "$",
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
        if c == "'" and i + 1 < n and (src[i + 1].isalpha()
                                       or src[i + 1] == "_") \
                and not (i + 2 < n and src[i + 2] == "'"):
            # A lifetime (`'a`, `'static`, `'_`), not a char literal -- the
            # giveaway is that a char literal closes after one character.
            # Crust has no borrow checker, so a lifetime carries nothing it
            # could act on and is dropped here, where every later pass is
            # spared having to know about it.
            #
            # The comma that follows one inside `<'a, T>` is dropped with it,
            # so the remaining type argument list is still well formed.
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            k = j
            while k < n and src[k] in " \t":
                k += 1
            if k < n and src[k] == ",":
                j = k + 1
            i = j
            continue
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

    __slots__ = ("code", "type", "targs", "from_path")

    def __init__(self, code, type_):
        self.targs = None            # turbofish type arguments, if any
        self.from_path = False       # name came from a `a::b` path
        self.code = code
        self.type = type_


def _c_spec_for(ty):
    """The printf conversion for a value of C type `ty`.

    Rust's `{}` carries no type of its own -- `Display` picks the formatting
    at the call site -- so Crust reads it off the argument's inferred type
    instead. Where the type is unknown, `%d` is the least surprising default
    and is what an untyped integer literal wants.
    """
    if ty is None:
        return "%d"
    if ty.ptr and ty.base == "const char":
        return "%s"
    if ty.ptr:
        return "%p"
    if ty.base in ("float", "double"):
        return "%g"
    if ty.base == "_Bool":
        return "%d"
    if ty.base in ("long", "unsigned long"):
        return "%lu" if ty.base.startswith("unsigned") else "%ld"
    if ty.base.startswith("unsigned"):
        return "%u"
    return "%d"


_FMT_HINTS = {"x": "%x", "X": "%X", "o": "%o", "b": "%d", "e": "%e",
              "p": "%p", "?": None, "": None}


def _rust_format_to_c(fmt, types, newline):
    """Translate a Rust format string into a C one.

    `{}` and `{:?}` take their conversion from the argument's type; `{:x}`
    and friends map to the matching C conversion. `{{` and `}}` are literal
    braces. A `%` in the original must be escaped, since it means nothing in
    Rust but everything to printf.
    """
    out, i, n, argi = [], 0, len(fmt), 0
    while i < n:
        c = fmt[i]
        if c == "{" and i + 1 < n and fmt[i + 1] == "{":
            out.append("{")
            i += 2
            continue
        if c == "}" and i + 1 < n and fmt[i + 1] == "}":
            out.append("}")
            i += 2
            continue
        if c == "%":
            out.append("%%")
            i += 1
            continue
        if c == "{":
            j = fmt.find("}", i)
            if j < 0:
                raise CrustError("unterminated `{` in format string")
            body = fmt[i + 1:j]
            hint = body.split(":", 1)[1] if ":" in body else ""
            hint = hint.lstrip("0123456789.<>^+#")
            ty = types[argi] if argi < len(types) else None
            spec = _FMT_HINTS.get(hint) if hint in _FMT_HINTS else None
            out.append(spec or _c_spec_for(ty))
            argi += 1
            i = j + 1
            continue
        out.append(c)
        i += 1
    if newline:
        out.append("\\n")
    return '"%s"' % "".join(out)




def _instance_name(name, args):
    """The C name of one monomorphisation, e.g. `Pair<i32>` -> `Pair_int`.

    Deterministic and readable, so the generated C can be read and debugged,
    and so two references to the same instantiation always agree.
    """
    return name + "_" + "_".join(_mangle(a) for a in args)


def _mangle(ty):
    """Turn a C type into an identifier fragment for a generated struct."""
    return re.sub(r"\W+", "_", ty.decl()).strip("_")


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
        self.data_enums = {}    # enum -> {variant: [(field|None, CType)]}
        self.result_error = None  # error type of a `Result<T>` alias
        self.derives = {}       # type name -> [derived trait names]
        self.variants = {}      # mangled variant name -> enum name
        self.tuple_structs = set()   # struct names declared in tuple form
        self.unit_structs = set()    # struct names declared as `struct S;`
        self.consts = {}        # const/static name -> CType
        self.const_values = {}  # const name -> initializer text
        self.const_inits = {}   # name -> (kw, init text) for prelude render
        self.type_aliases = {}  # name -> CType (fully resolved)
        self.opaque_structs = set()  # path types with no definition in unit
        # Opaque names that appear by value (struct fields, returns) need a
        # one-byte placeholder body; pointer-only opaques stay incomplete.
        self.opaque_complete = set()
        self.extern_fns = {}    # name -> (ret CType|None, [arg CType|None])
        self.needs = set()      # libc prototypes the lowering requires
        self.slices = {}        # slice struct name -> element CType
        self.tuples = {}        # tuple struct name -> [element CTypes]
        self.fn_ptrs = {}       # fn-pointer typedef -> (ret, params)
        self.options = {}       # Option struct name -> element CType
        self.results = {}       # Result struct name -> (ok, err) CTypes
        self.unwraps = set()    # generated types needing unwrap helpers
        # Generics are monomorphised, exactly like Option/Result and like
        # py2c's `_tlist_` types: a generic item is stored as its *tokens*,
        # and each distinct set of type arguments re-parses those tokens with
        # the parameters bound to concrete types. There is no boxing and no
        # tag, so an instantiation is an ordinary C struct or function and
        # stays directly callable from C.
        self.generic_structs = {}   # name -> (params, tokens)
        self.generic_fns = {}       # name -> (params, tokens)
        self.generic_impls = []     # (params, type name, tokens)
        self.instances = {}         # mangled -> (name, [CType])
        self.emitted = []           # generated C, appended after the unit
        self.struct_order = []      # instantiated structs, in creation order
        self.emitting = set()       # guards against recursive instantiation
        self.core_names = set()     # generics seeded from the bundled core
        self.core_concrete = set()  # concrete core types pulled on demand
        # Traits. Dispatch is static: `impl Trait for T` registers its methods
        # under `T` with the same `T_method` mangling an inherent impl uses,
        # so a trait call is an ordinary direct call -- no vtable, no
        # indirection, and fully inlinable.
        self.traits = {}            # trait -> {method: (params, ret, self_kind)}
        self.trait_defaults = {}    # trait -> {method: tokens of the default}
        self.supertraits = {}       # trait -> [trait names]
        self.trait_impls = []       # (trait, owner, tokens)
        self.const_defaults = {}    # trait -> {const: tokens}
        self.inherited_consts = []  # trait const defaults, for the prelude
        self.macros = {}            # macro_rules! name -> [(pattern, body)]
        self.closure_n = 0          # lifted-closure counter
        self.statics = set()        # fns with internal linkage

    def result_type(self, ok, err):
        """Return (and register) the tagged struct for `Result<ok, err>`."""
        name = "crust_result_%s_e_%s" % (_mangle(ok), _mangle(err))
        if name not in self.results:
            self.results[name] = (ok, err)
            fields = [("ok", CType("_Bool"))]
            if not ok.is_void():
                fields.append(("value", ok))
            fields.append(("error", err))
            self.structs[name] = fields
        return CType(name)

    def option_type(self, elem):
        """Return (and register) the tagged struct for `Option<elem>`.

        Crust has no generics, so each instantiation is monomorphised into
        its own struct, generated on demand exactly like a slice.
        """
        name = "crust_option_" + _mangle(elem)
        if name not in self.options:
            self.options[name] = elem
            self.structs[name] = [("some", CType("_Bool")),
                                  ("value", elem)]
        return CType(name)

    def fn_ptr_type(self, ret, params):
        """Return (and register) a typedef for a function-pointer type."""
        name = "crust_fn_%s_%s" % (_mangle(ret),
                                   "_".join(_mangle(t) for t in params)
                                   or "void")
        if name not in self.fn_ptrs:
            self.fn_ptrs[name] = (ret, params)
        return CType(name)

    def tuple_type(self, elems):
        """Return (and register) the struct for a tuple type `(A, B, ..)`.

        Monomorphised on demand like a slice or an `Option`, with positional
        fields named `_0`, `_1`, ... -- the same names a tuple struct already
        uses, so `t.0` works through the ordinary field-access path.
        """
        name = "crust_tuple_" + "_".join(_mangle(e) for e in elems)
        if name not in self.tuples:
            self.tuples[name] = elems
            self.structs[name] = [("_%d" % i, e) for i, e in enumerate(elems)]
        return CType(name)

    def slice_type(self, elem):
        """Return (and register) the fat-pointer struct type for `&[elem]`."""
        name = "crust_slice_" + _mangle(elem)
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


# A `[v; N]` repeat is written out element by element, so a huge N would
# blow up the generated C rather than the generated binary. The cap is well
# above any hand-written literal and well below anything that would hurt.
_MAX_REPEAT = 4096


def _literal_count(code, unit):
    """Resolve a `[v; N]` length to an int, or None if it is not constant.

    A plain integer literal is the common case; a `const` is resolved through
    the unit's table, since Crust lowers an integer `const` to a C enum
    constant precisely so it can size an array.
    """
    text = code.strip()
    try:
        return int(text, 0)
    except ValueError:
        pass
    seen = set()
    while text in unit.const_values and text not in seen:
        seen.add(text)
        text = unit.const_values[text].strip()
        try:
            return int(text, 0)
        except ValueError:
            continue
    return None


class Parser:
    def __init__(self, toks, unit=None, tysubst=None):
        self.toks = toks
        self.i = 0
        # Bindings for the enclosing generic item's type parameters. Empty for
        # ordinary code; `{"T": CType("int")}` while an instantiation is being
        # generated. Substitution happens in parse_type, so every other part
        # of the parser is reused unchanged.
        self.tysubst = tysubst or {}
        self.binds = []              # match-arm payload bindings
        self.derives = []            # traits from the last `#[..]` run
        self.item_derives = []       # traits on the item being parsed
        self.unit = unit or Unit()
        self.fn_sigs = self.unit.fn_sigs
        self.scopes = [{}]              # name -> CType
        self.ret_type = VOID
        self.impl_type = None           # enclosing `impl` type name, if any
        self.no_struct_lit = 0          # >0 while parsing a condition
        self.behind_ref = 0             # >0 while parsing the pointee of `&`
        self.expected = []              # target types, for inferring `None`
        self.tmp_n = 0                  # counter for generated temporaries
        self.pending = []               # statements hoisted out of `?`

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
        if t.val == "(" and t.kind == "punc":
            # A tuple type. `(T)` is just a parenthesised T, as in Rust.
            self.next()
            elems = [self.parse_type()]
            while self.accept(","):
                if self.at(")", "punc"):
                    break
                elems.append(self.parse_type())
            self.expect(")")
            if len(elems) == 1:
                return elems[0]
            return self.unit.tuple_type(elems)
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
            was_path = False
            if self.at("::", "punc"):
                # A qualified type: `fmt::Write`, `alloc::boxed::Box<T>`.
                # Try the flattened spelling first, since that is what a Crust
                # `mod::Type` definition lowers to; then fall back to the last
                # segment, which is how a std path like `alloc::boxed::Box`
                # finds the bundled `Box`. Neither is a guess: both names are
                # only accepted if the unit actually defines them.
                segs = [name]
                while self.accept("::"):
                    if self.at("<", "punc"):
                        break                       # turbofish on a path
                    segs.append(self.expect_ident())
                name = self._resolve_path(segs)
                was_path = len(segs) > 1
            if name in PRIMITIVES:
                if name == "str" and not self.behind_ref:
                    self.err("`str` is unsized; write `&str`")
                return CType(PRIMITIVES[name])
            if name in self.unit.type_aliases:
                return self.unit.type_aliases[name]
            # Concrete bundled types (atomics, Ordering) -- pull on demand.
            ensure_core_concrete(self.unit, name)
            if name in self.unit.enums and not self.at("<", "punc"):
                return CType(name)
            if name in self.unit.structs and not self.at("<", "punc") \
                    and name not in self.unit.generic_structs:
                return CType(name)
            if name == "Result":
                self.expect("<")
                ok = self.parse_type()
                if self.accept(","):
                    err = self.parse_type()
                else:
                    # `Result<T>` with one argument. A crate that fixes its
                    # error type almost always does it with
                    # `type Result<T> = core::result::Result<T, Error>;` and
                    # then writes the one-argument form everywhere -- Redox
                    # does exactly that. If this unit declares such an alias,
                    # its error type is used; otherwise the crate-wide
                    # convention of a plain integer error code applies.
                    err = self.unit.result_error or CType("int")
                self.expect_gt()
                if err.is_void():
                    self.err("`Result<_, ()>` is not supported")
                return self.unit.result_type(ok, err)
            if name == "Option":
                self.expect("<")
                elem = self.parse_type()
                self.expect_gt()
                if elem.is_void():
                    self.err("`Option<()>` is not supported")
                return self.unit.option_type(elem)
            if name in self.tysubst:
                # A type parameter of the generic item being instantiated.
                return self.tysubst[name]
            if name == "Self":
                if self.impl_type is None:
                    raise CrustError("line %d: `Self` outside an impl block"
                                     % t.line)
                # Bare `Self` in `impl Foo<false>` still needs a C typedef even
                # when `Foo` only exists as a generic template (const params).
                if (self.impl_type not in self.unit.structs
                        and self.impl_type not in self.unit.enums
                        and self.impl_type not in self.unit.type_aliases):
                    self.unit.opaque_structs.add(self.impl_type)
                return CType(self.impl_type)
            if name in self.unit.generic_structs and self.at("<", "punc"):
                return self.instantiate_struct(name, self.parse_type_args())
            if self.at("<", "punc") and name not in PRIMITIVES:
                # A generic we have no template for -- a std type, or one
                # defined in a crate Crust never saw. Say so plainly rather
                # than failing on the `<` as a comparison operator.
                self.err("no definition for generic type `%s` in this unit; "
                         "Crust monomorphises from source and has no std", name)
            # Unknown named type: assume a C struct/typedef of the same name.
            # Qualified paths that resolved to nothing still use the flattened
            # spelling; register them so the prelude can forward-declare.
            # Capitalised bare names, and bare uses of a generic template
            # (const-generic `impl Foo<false>` → `Foo`), get the same.
            if name not in self.unit.structs and name not in self.unit.enums \
                    and name not in self.unit.type_aliases:
                if name in self.unit.generic_structs \
                        or was_path or (name and name[0].isupper()):
                    self.unit.opaque_structs.add(name)
            return CType(name)
        raise CrustError("line %d: expected a type, found %r"
                         % (t.line, t.val or "<eof>"))

    def parse_generic_params(self):
        """Parse `<T, U>` after an item name; return the parameter names.

        Trait bounds (`T: Clone`) and lifetimes (`'a`) are skipped rather than
        recorded: Crust has no traits to check against and no borrow checker,
        so a bound carries no information it could act on. Where a bound would
        have caught a mistake, the resulting C simply fails to compile.
        """
        if not self.at("<", "punc"):
            return []
        self.next()
        params, depth = [], 1
        while depth > 0 and self.cur.kind != "eof":
            v = self.cur.val
            if v == "<":
                depth += 1
            elif v == ">":
                depth -= 1
                if depth == 0:
                    self.next()
                    break
            elif v == ">>":
                depth -= 2
                if depth <= 0:
                    self.next()
                    break
            elif depth == 1 and self.cur.kind == "ident":
                prev = self.toks[self.i - 1].val if self.i > 0 else None
                # Type params start after `<` or `,`. Skip const-param names
                # (`const N`) and the types that follow a `:`.
                if prev != "const" and prev in ("<", ","):
                    params.append(v)
            self.next()
        return params

    def skip_generic_params(self):
        self.parse_generic_params()

    def _resolve_path(self, segs):
        """Pick the name a qualified type path refers to.

        `a::b::C` is looked up as `a_b_C` (a Crust module-style definition)
        and then as `C` (the common case for a std path naming a type the
        bundled core provides). If neither is known, the flattened spelling is
        returned so the usual "no definition" diagnostic names what was
        written.
        """
        flat = "_".join(segs)
        last = segs[-1]
        for cand in (flat, last):
            if (cand in self.unit.structs or cand in self.unit.generic_structs
                    or cand in self.unit.enums or cand in PRIMITIVES
                    or cand in self.tysubst or cand in self.unit.type_aliases):
                return cand
        return flat

    def parse_type_alias(self):
        """Parse `type Name = Type;`; return `(name, CType)` or `(None, None)`.

        A *generic* alias (`type Result<T> = Result<T, Error>;`) has no
        representation here -- Crust monomorphises, and an alias is not an
        item to monomorphise. It is skipped rather than reported, because
        failing the file over one would be far out of proportion: a crate that
        declares such an alias is otherwise perfectly translatable, and the
        one alias that matters in practice (`Result<T>`) is recognized
        separately.
        """
        self.skip_attributes()
        self.skip_visibility()
        self.expect("type")
        name = self.expect_ident()
        if self.at("<", "punc"):
            while not self.at(";", "punc") and self.cur.kind != "eof":
                self.next()
            self.accept(";")
            return None, None
        self.expect("=")
        ty = self.parse_type()
        self.expect(";")
        return name, ty

    def parse_type_args(self):
        """Parse `<T1, T2>` in type position; return the concrete CTypes."""
        self.expect("<")
        args = [self.parse_type()]
        while self.accept(","):
            args.append(self.parse_type())
        self.expect_gt()
        return args

    def instantiate_struct(self, name, args):
        """Monomorphise `Name<args>`; return the concrete CType.

        The template's tokens are re-parsed with the type parameters bound,
        so the instantiation goes through exactly the same code path an
        ordinary struct does and needs no separate lowering.
        """
        params, toks = self.unit.generic_structs[name]
        if len(args) != len(params):
            self.err("`%s` takes %d type argument%s, got %d", name,
                     len(params), "" if len(params) == 1 else "s", len(args))
        mangled = _instance_name(name, args)
        if mangled in self.unit.structs:
            return CType(mangled)
        if mangled in self.unit.emitting:
            self.err("recursive instantiation of `%s` (a generic struct "
                     "cannot contain itself by value)", name)
        if name in self.unit.core_names:
            self.unit.needs.add("alloc")
        self.unit.emitting.add(mangled)
        try:
            sub = dict(zip(params, args))
            p = Parser(list(toks), self.unit, sub)
            p.impl_type = mangled
            _, fields = p.parse_struct()
            self.unit.structs[mangled] = fields
            self.unit.struct_order.append(mangled)
            self.unit.instances[mangled] = (name, args)
            self.instantiate_impls_for(name, mangled, args)
        finally:
            self.unit.emitting.discard(mangled)
        return CType(mangled)

    def instantiate_impls_for(self, name, mangled, args):
        """Generate the methods of every `impl<..> Name<..>` for one instance.

        Two passes, for the same reason the unit-level collector uses two:
        every method of the instance must be known before any body is
        translated, so that one method can call another declared after it.
        """
        matching = [(p, t) for p, owner, t in self.unit.generic_impls
                    if owner == name]
        for params, toks in matching:                 # pass 1: signatures
            sub = dict(zip(params, args))
            p = Parser(list(toks), self.unit, sub)
            p.parse_impl_header()
            p.impl_type = mangled
            while not p.at("}", "punc") and p.cur.kind != "eof":
                if p.is_assoc_const():
                    kw, cname, cty, cinit = p.parse_const_signature()
                    self.unit.consts["%s_%s" % (mangled, cname)] = cty
                    continue
                _, info = p.parse_method_signature(mangled)
                self.unit.methods[(mangled, info.name)] = info
                selfp = []
                if info.self_kind == "ref":
                    selfp = [CType(mangled, 1)]
                elif info.self_kind == "value":
                    selfp = [CType(mangled)]
                self.unit.fn_sigs[info.mangled] = (
                    info.ret, selfp + [t for _, t in info.params])
                p.skip_to_body_end()
        for params, toks in matching:                 # pass 2: bodies
            sub = dict(zip(params, args))
            p = Parser(list(toks), self.unit, sub)
            out = Out(1)
            p.parse_impl(out, owner_override=mangled)
            self.unit.emitted.append(out.text())

    def instantiate_fn(self, name, args):
        """Monomorphise a generic `fn`; return its concrete mangled name."""
        params, toks = self.unit.generic_fns[name]
        mangled = _instance_name(name, args)
        if mangled in self.unit.fn_sigs:
            return mangled
        if mangled in self.unit.emitting:
            return mangled              # recursive call: signature suffices
        self.unit.emitting.add(mangled)
        try:
            sub = dict(zip(params, args))
            sig = Parser(list(toks), self.unit, sub)
            _, _, ps, ret = sig.parse_fn_signature()
            self.unit.fn_sigs[mangled] = (ret, [t for _, t in ps])
            self.unit.instances[mangled] = (name, args)
            body = Parser(list(toks), self.unit, sub)
            out = Out(1)
            body.parse_fn(out, name_override=mangled)
            self.unit.emitted.append(out.text())
        finally:
            self.unit.emitting.discard(mangled)
        return mangled

    def infer_type_args(self, name, arg_types):
        """Infer a generic fn's type arguments from its call's argument types.

        Crust has no type checker, so inference is deliberately shallow: a
        parameter declared as exactly a type variable (`x: T`), or as one
        reference or pointer step from one (`x: &T`), binds that variable to
        the argument's type. Anything deeper -- `&[T]`, `Vec<T>` -- is not
        inferred, and the call needs a turbofish. Failing loudly here is much
        better than guessing wrong and emitting C that miscompiles.
        """
        params, toks = self.unit.generic_fns[name]
        decls = Parser(list(toks), self.unit).fn_param_decls()
        found = {}
        for (pname, depth), aty in zip(decls, arg_types):
            if pname not in params or aty is None or pname in found:
                continue
            if depth == 0:
                found[pname] = aty
            elif aty.ptr >= depth:
                found[pname] = CType(aty.base, aty.ptr - depth, aty.array)
        missing = [p for p in params if p not in found]
        if missing:
            self.err("cannot infer type argument%s %s for `%s`; give it "
                     "explicitly with a turbofish, `%s::<i32>(..)`",
                     "" if len(missing) == 1 else "s",
                     ", ".join("`%s`" % m for m in missing), name, name)
        return [found[p] for p in params]

    def fn_param_decls(self):
        """(declared type name, pointer depth) for each parameter of a `fn`.

        Read off the raw tokens without resolving types, so a parameter
        mentioning a type variable can be recognized before that variable has
        a binding.
        """
        self.skip_attributes()
        self.skip_visibility()
        while self.cur.val in ("unsafe", "extern") or self.cur.kind == "str":
            self.next()
        self.expect("fn")
        self.expect_ident()
        self.skip_generic_params()
        self.expect("(")
        decls = []
        while not self.at(")", "punc") and self.cur.kind != "eof":
            self.expect_ident()
            self.expect(":")
            depth = 0
            while self.cur.val in ("&", "*"):
                if self.next().val == "*":
                    self.accept("const") or self.accept("mut")
                else:
                    self.accept("mut")
                depth += 1
            nm = self.cur.val if self.cur.kind in ("ident", "kw") else None
            decls.append((nm, depth))
            d = 0
            while self.cur.kind != "eof":
                if self.cur.val in ("(", "[", "<"):
                    d += 1
                elif self.cur.val in (")", "]", ">"):
                    if self.cur.val == ")" and d == 0:
                        break
                    d -= 1
                elif self.cur.val == "," and d == 0:
                    break
                self.next()
            if not self.accept(","):
                break
        return decls

    # -- macros -----------------------------------------------------------

    def at_macro(self):
        """True if the cursor is on `name !` followed by a delimiter."""
        return (self.cur.kind == "ident" and self.peek().val == "!"
                and self.peek().kind == "punc"
                and self.peek(2).val in ("(", "[", "{"))

    def macro_args(self):
        """Parse a macro's delimited argument list into raw token slices.

        Rust lets a macro be called with any of `()`, `[]` or `{}`, and the
        contents are arbitrary token trees, so the arguments are split on
        top-level commas without being parsed. Each slice is handed to a
        fresh sub-parser only if the expansion actually needs it as an
        expression.
        """
        self.next()                                  # the `!`
        open_tok = self.next().val
        close = {"(": ")", "[": "]", "{": "}"}[open_tok]
        depth, parts, cur = 1, [], []
        while True:
            t = self.cur
            if t.kind == "eof":
                self.err("unterminated macro invocation")
            if t.val in ("(", "[", "{") and t.kind == "punc":
                depth += 1
            elif t.val in (")", "]", "}") and t.kind == "punc":
                depth -= 1
                if depth == 0:
                    self.next()
                    break
            if depth == 1 and t.val == "," and t.kind == "punc":
                parts.append(cur)
                cur = []
                self.next()
                continue
            cur.append(t)
            self.next()
        if cur:
            parts.append(cur)
        return parts

    def sub_expr(self, toks):
        """Translate one raw token slice as an expression."""
        if not toks:
            return Expr("", None)
        p = Parser(list(toks) + [Token("eof", "", toks[-1].line)],
                   self.unit, self.tysubst)
        p.scopes = self.scopes
        p.impl_type = self.impl_type
        p.ret_type = self.ret_type
        e = p.parse_expr()
        self.pending.extend(p.pending)
        return e

    def parse_macro(self, name):
        """Expand a macro invocation, or report that it is not supported."""
        line = self.cur.line
        if name in self.unit.macros:
            return self.expand_macro_rules(name, line)
        if name not in _BUILTIN_MACROS:
            self.macro_args()
            self.err("macro `%s!` is not supported; Crust expands the "
                     "standard control and printing macros and any "
                     "`macro_rules!` defined in this unit", name)
        return _BUILTIN_MACROS[name](self, self.macro_args(), line)

    # -- the built-in macros ----------------------------------------------

    def m_abort(self, args, line, _msg=None):
        """`panic!` / `unreachable!` / `todo!` -- diverge immediately."""
        self.unit.needs.add("abort")
        return Expr("(abort(), 0)", INT)

    def m_assert(self, args, line):
        if not args:
            self.err("`assert!` needs a condition")
        cond = self.sub_expr(args[0])
        self.unit.needs.add("abort")
        return Expr("((%s) ? 0 : (abort(), 0))" % cond.code, INT)

    def m_assert_cmp(self, args, line, op="=="):
        if len(args) < 2:
            self.err("`assert_eq!` needs two operands")
        a, b = self.sub_expr(args[0]), self.sub_expr(args[1])
        self.unit.needs.add("abort")
        return Expr("(((%s) %s (%s)) ? 0 : (abort(), 0))"
                    % (a.code, op, b.code), INT)

    def m_print(self, args, line, newline=False, stream=None):
        """`println!` and friends -- a printf with the format translated."""
        if not args:
            fmt, rest = '""', []
        else:
            fmt, rest = self._format_string(args[0], line), args[1:]
        vals = [self.sub_expr(a) for a in rest]
        spec = _rust_format_to_c(fmt, [v.type for v in vals], newline)
        if stream:
            self.unit.needs.add("fprintf")
            call = "fprintf(%s, %s" % (stream, spec)
        else:
            self.unit.needs.add("printf")
            call = "printf(%s" % spec
        for v in vals:
            call += ", " + v.code
        return Expr(call + ")", INT)

    def _format_string(self, toks, line):
        if len(toks) != 1 or toks[0].kind != "str":
            raise CrustError("line %d: the first argument of a printing "
                             "macro must be a literal format string" % line)
        text = toks[0].val
        if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
            text = text[1:-1]
        return text

    def m_cfg(self, args, line):
        # `cfg!(..)` is a compile-time predicate over features Crust has no
        # notion of. Reporting false is the honest answer: nothing is
        # configured in.
        return Expr("0", CType("_Bool"))

    def m_matches(self, args, line):
        if len(args) < 2:
            self.err("`matches!` needs a value and a pattern")
        v = self.sub_expr(args[0])
        pat = self.sub_expr(args[1])
        return Expr("((%s) == (%s))" % (v.code, pat.code), CType("_Bool"))

    def m_dbg_noop(self, args, line):
        """`debug_assert*!` -- compiled out, as in a release build."""
        for a in args:
            pass
        return Expr("0", INT)

    def parse_macro_rules(self):
        """Parse `macro_rules! name { (pat) => { body }; ... }`.

        Rules are kept as raw token slices; matching and substitution happen
        at the invocation site. Crust supports the common shape -- literal
        tokens plus `$x:frag` metavariables -- and reports anything else
        rather than expanding it wrongly, since a silently mis-expanded macro
        is far worse than one that does not compile.
        """
        self.expect_ident()                          # `macro_rules`
        self.expect("!")
        name = self.expect_ident()
        self.expect("{")
        rules = []
        while not self.at("}", "punc") and self.cur.kind != "eof":
            pat = self._delimited()
            self.expect("=>")
            body = self._delimited()
            rules.append((pat, body))
            self.accept(";")
        self.expect("}")
        return name, rules

    def _delimited(self):
        """Consume one balanced `(..)`, `[..]` or `{..}`; return its inside."""
        if self.cur.val not in ("(", "[", "{"):
            self.err("expected a delimited group in `macro_rules!`")
        self.next()
        depth, toks = 1, []
        while True:
            t = self.cur
            if t.kind == "eof":
                self.err("unterminated group in `macro_rules!`")
            if t.val in ("(", "[", "{") and t.kind == "punc":
                depth += 1
            elif t.val in (")", "]", "}") and t.kind == "punc":
                depth -= 1
                if depth == 0:
                    self.next()
                    return toks
            toks.append(t)
            self.next()

    def expand_macro_rules(self, name, line):
        """Match an invocation against the macro's rules and expand it."""
        args = self.macro_args()
        # Rejoin the argument slices with the commas that split them, since a
        # pattern matches the raw token stream, not a comma-separated list.
        flat = []
        for k, part in enumerate(args):
            if k:
                flat.append(Token("punc", ",", line))
            flat.extend(part)
        for pat, body in self.unit.macros[name]:
            binds = _match_pattern(pat, flat)
            if binds is not None:
                return self.sub_expr(_substitute(body, binds))
        raise CrustError("line %d: no rule of `%s!` matches this invocation"
                         % (line, name))

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
            e = self.parse_expr()
        finally:
            self.expected.pop()
        # Associated types (`Self::Target`) and other opaque names often
        # disagree with the concrete pointer the body produces. GCC 14+
        # rejects that as an error; insert an explicit cast.
        if ty is not None and ty.ptr and e is not None:
            if e.type is None or (e.type.ptr and e.type.base != ty.base):
                e = Expr("(%s)(%s)" % (ty.decl(), e.code), ty)
        return e

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
                if e.code in ("Ok", "Err"):
                    e = self.parse_result_ctor(e.code)
                    continue
                generic = e.code in self.unit.generic_fns
                targs = e.targs
                # A locally defined function of the same name always wins:
                # `min` is a perfectly ordinary thing to define, and silently
                # replacing it with the intrinsic would be a very confusing
                # bug.
                core_fn = (None if (e.code in self.unit.fn_sigs
                                    or e.code in self.unit.generic_fns)
                           else _core_intrinsic(e.code))
                if core_fn is not None:
                    args, atys = [], []
                    while not self.at(")", "punc"):
                        a = self.parse_expr()
                        args.append(a.code)
                        atys.append(a.type)
                        if not self.accept(","):
                            break
                    self.expect(")")
                    e = core_fn(self, args, atys, targs)
                    continue
                if e.code in ("size_of", "mem_size_of") and targs:
                    # `size_of::<T>()` -- the one intrinsic monomorphised
                    # containers cannot be written without, since Crust has
                    # no `sizeof` of its own.
                    self.expect(")")
                    e = Expr("sizeof(%s)" % targs[0].decl(),
                             CType("unsigned long"))
                    continue
                if generic and targs:
                    e = Expr(self.instantiate_fn(e.code, targs), None)
                    generic = False
                sig = self.fn_sigs.get(e.code)
                if sig is None and e.type is not None \
                        and e.type.base in self.unit.fn_ptrs:
                    sig = self.unit.fn_ptrs[e.type.base]
                params = (sig[1] if sig else []) or []
                args, atypes = [], []
                while not self.at(")", "punc"):
                    want = params[len(args)] if len(args) < len(params) \
                        else None
                    a = self.parse_expr_as(want)
                    args.append(a.code)
                    atypes.append(a.type)
                    if not self.accept(","):
                        break
                self.expect(")")
                if generic:
                    # No turbofish: infer the type arguments from what was
                    # actually passed, then instantiate.
                    e = Expr(self.instantiate_fn(
                        e.code, self.infer_type_args(e.code, atypes)), None)
                if e.code in self.unit.variants and \
                        self._payload_of(e.code) is not None:
                    e = self.data_variant_literal(e.code, args)
                elif e.code in self.unit.tuple_structs:
                    e = self.tuple_struct_literal(e.code, args)
                else:
                    ret = sig[0] if sig else None
                    if sig is None and e.type is None and e.code:
                        # Foreign path call (`rmm::aarch64::init_mair`) or
                        # other unknown callee: emit an extern prototype so
                        # the C front end can compile this TU alone. Prefer
                        # an expected return type when the call sits in a
                        # typed position (`return KernelMapper::lock()`).
                        # Skip names the prelude already declares (alloc).
                        #
                        # Only a name that actually came from a `a::b` path
                        # gets a guessed prototype. A bare identifier may well
                        # be declared by text this unit cannot see yet --
                        # anything pulled in by `#include`, which the
                        # preprocessor expands only *after* translation. That
                        # is how `labels` and `widest` from an included
                        # rpython module ended up with conflicting `extern
                        # void` guesses. A path call cannot come from an
                        # include, so guessing for those stays safe.
                        if e.code not in _PRELUDE_LIBC and e.from_path:
                            ret_ty = self.target
                            prev = self.unit.extern_fns.get(e.code)
                            if prev is None:
                                self.unit.extern_fns[e.code] = (ret_ty, atypes)
                            elif prev[0] is None and ret_ty is not None:
                                self.unit.extern_fns[e.code] = (
                                    ret_ty, prev[1] or atypes)
                    e = Expr("%s(%s)" % (e.code, ", ".join(args)),
                             ret if ret is not None else self.target)
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
            elif self.at("?", "punc"):
                self.next()
                e = self.try_operator(e)
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

    def try_operator(self, e):
        """Lower the `?` operator to a hoisted test and an early return.

        `?` is an expression in Rust but needs a statement in C, so the
        temporary and the early return are queued as pending statements and
        emitted just before the statement that contains them. The expression
        itself becomes a read of the temporary's payload.
        """
        if e.type is None:
            self.err("`?` needs a `Result` or `Option`; the operand's type "
                     "could not be inferred")
        src = e.type.base
        ret = self.ret_type
        tmp = self.new_temp()
        self.pending.append("%s = %s;" % (e.type.decl(tmp), e.code))

        if src in self.unit.results:
            ok, err = self.unit.results[src]
            if ret.base not in self.unit.results:
                self.err("`?` on a `Result` needs the enclosing function to "
                         "return a `Result`")
            _, ret_err = self.unit.results[ret.base]
            if ret_err.decl() != err.decl():
                self.err("`?` cannot convert error type `%s` to `%s`; Crust "
                         "has no `From` conversions", err.decl(),
                         ret_err.decl())
            self.pending.append(
                "if (!%s.ok) return (%s){.ok = 0, .error = %s.error};"
                % (tmp, ret.base, tmp))
            if ok.is_void():
                return Expr("(void)0", VOID)
            return Expr("%s.value" % tmp, ok)

        if src in self.unit.options:
            if ret.base not in self.unit.options:
                self.err("`?` on an `Option` needs the enclosing function to "
                         "return an `Option`")
            self.pending.append("if (!%s.some) return (%s){0};"
                                % (tmp, ret.base))
            return Expr("%s.value" % tmp, self.unit.options[src])

        self.err("`?` applies to `Result` and `Option`, not `%s`",
                 e.type.decl())

    def emit_pending(self, out, line, indent):
        """Emit statements hoisted out of the expression being translated."""
        if not self.pending:
            return
        for stmt in self.pending:
            out.line_at(line, stmt, indent)
        self.pending = []

    def parse_result_ctor(self, which):
        """Lower `Ok(x)` / `Err(e)` using the target `Result` type."""
        want = self.target
        if want is None or want.base not in self.unit.results:
            # Still parse the operand so the error points at the call, not
            # at whatever token happens to follow it.
            self.parse_expr()
            self.accept(")")
            self.err("cannot infer the `Result` type of `%s`; annotate the "
                     "target or give the function a `-> Result<..>` return "
                     "type", which)
        ok, err = self.unit.results[want.base]
        if which == "Ok" and ok.is_void():
            self.expect(")")
            return Expr("(%s){.ok = 1}" % want.base, want)
        inner = self.parse_expr_as(ok if which == "Ok" else err)
        self.expect(")")
        field = "value" if which == "Ok" else "error"
        flag = 1 if which == "Ok" else 0
        return Expr("(%s){.ok = %d, .%s = %s}"
                    % (want.base, flag, field, inner.code), want)

    def result_method(self, recv, name, args):
        """Lower the supported `Result` methods."""
        ok, err = self.unit.results[recv.type.base]
        if name == "is_ok" and not args:
            return Expr("%s.ok" % recv.code, CType("_Bool"))
        if name == "is_err" and not args:
            return Expr("(!%s.ok)" % recv.code, CType("_Bool"))
        if name in ("unwrap", "unwrap_err") and not args:
            if name == "unwrap" and ok.is_void():
                self.err("`unwrap` on `Result<(), _>` yields nothing; "
                         "test `is_ok` instead")
            self.unit.unwraps.add(recv.type.base)
            self.unit.needs.add("abort")
            return Expr("%s_%s(%s)" % (recv.type.base, name, recv.code),
                        ok if name == "unwrap" else err)
        if name == "unwrap_or" and len(args) == 1:
            return Expr("(%s.ok ? %s.value : %s)"
                        % (recv.code, recv.code, args[0]), ok)
        if name == "ok" and not args:
            opt = self.unit.option_type(ok)
            return Expr("(%s){%s.ok, %s.value}"
                        % (opt.base, recv.code, recv.code), opt)
        self.err("no method `%s` on `Result`; supported: is_ok, is_err, "
                 "unwrap, unwrap_err, unwrap_or, ok", name)

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

        if recv.type is not None and recv.type.base in self.unit.results:
            return self.result_method(recv, name, args)
        if recv.type is not None and recv.type.base in self.unit.options:
            return self.option_method(recv, name, args)
        if recv.type is not None and recv.type.base in self.unit.slices:
            if name == "len":
                return Expr("%s.len" % recv.code, CType("unsigned long"))
            if name in ("iter", "iter_mut"):
                # Crust has no iterator protocol; `for x in xs` walks the
                # slice directly, so `.iter()` is accepted as a no-op purely
                # so the idiomatic spelling reads the same as in Rust.
                if args:
                    self.err("`%s` takes no arguments", name)
                return recv
            if name == "is_empty":
                return Expr("(%s.len == 0)" % recv.code, CType("_Bool"))
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
            if recv.type.ptr:
                recv_code = recv.code
            elif _is_lvalue(recv.code):
                recv_code = "&" + _addressable(recv.code)
            else:
                # `a.f().g()` -- the receiver is a value, not a place, and C
                # cannot take its address. Rust materialises a temporary here,
                # so Crust does too, reusing the same pending-statement
                # mechanism `?` uses. This also keeps the receiver evaluated
                # exactly once.
                tmp = self.new_temp()
                self.pending.append("%s = %s;" % (recv.type.decl(tmp),
                                                  recv.code))
                recv_code = "&" + tmp
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

    def _payload_of(self, flat):
        """Payload fields for a flattened `Enum_Variant` name, or None."""
        owner = self.unit.variants.get(flat)
        if owner is None:
            return None
        vname = flat[len(owner) + 1:]
        return self.unit.data_enums.get(owner, {}).get(vname)

    def data_variant_literal(self, flat, args):
        """Build a tagged-union value for `Enum::Variant(a, b)`."""
        owner = self.unit.variants[flat]
        vname = flat[len(owner) + 1:]
        fields = self.unit.data_enums[owner][vname]
        if len(args) != len(fields):
            self.err("`%s` takes %d value%s, got %d", flat, len(fields),
                     "" if len(fields) == 1 else "s", len(args))
        inits = ", ".join(
            ".%s = %s" % (fname or "_%d" % i, a)
            for i, ((fname, _ty), a) in enumerate(zip(fields, args)))
        return Expr("(%s){.tag = %s, .u.%s = {%s}}"
                    % (owner, flat, vname, inits), CType(owner))

    def parse_closure(self):
        """Lower `|a, b| expr` to a lifted top-level function.

        Crust has no closure environment, so only a *non-capturing* closure
        can be lowered: it becomes an ordinary static function, and the
        expression evaluates to that function's name -- which in C is a
        function pointer, so it can be stored, passed and called.

        A closure that reads a local is rejected rather than silently
        compiled with the wrong binding. Detecting that is the whole
        difficulty, and it is done by checking every free identifier in the
        body against the enclosing scopes.
        """
        start = self.cur
        params = []
        if self.accept("||"):
            pass
        else:
            self.expect("|")
            while not self.at("|", "punc"):
                self.accept("mut")
                pname = self.expect_ident()
                pty = self.parse_type() if self.accept(":") else None
                if pty is None:
                    self.err("closure parameter `%s` needs a type "
                             "annotation; Crust does not infer them", pname)
                params.append((pname, pty))
                if not self.accept(","):
                    break
            self.expect("|")
        ret = self.parse_type() if self.accept("->") else None

        body_start = self.i
        if self.at("{", "punc"):
            end = self.skip_to_body_end()
        else:
            depth = 0
            while self.cur.kind != "eof":
                v = self.cur.val
                if v in ("(", "[", "{"):
                    depth += 1
                elif v in (")", "]", "}"):
                    if depth == 0:
                        break
                    depth -= 1
                elif depth == 0 and v in (",", ";"):
                    break
                self.next()
            end = self.i
        body = self.toks[body_start:end]

        bound = {n for n, _ in params}
        for tok in body:
            if tok.kind != "ident" or tok.val in bound:
                continue
            if self.lookup(tok.val) is not None:
                self.err("closure captures `%s` from its environment; Crust "
                         "lowers a closure to a plain function and has no "
                         "environment to capture into", tok.val)

        self.unit.closure_n += 1
        name = "_crust_closure%d" % self.unit.closure_n
        sub = Parser(list(body) + [Token("eof", "", start.line)],
                     self.unit, self.tysubst)
        sub.impl_type = self.impl_type
        sub.push()
        for pname, pty in params:
            sub.declare(pname, pty)
        if ret is None:
            probe = Parser(list(body) + [Token("eof", "", start.line)],
                           self.unit, self.tysubst)
            probe.push()
            for pname, pty in params:
                probe.declare(pname, pty)
            try:
                ret = probe.parse_expr().type or VOID
            except CrustError:
                ret = VOID
        sub.ret_type = ret
        out = Out(start.line)
        out.line_at(start.line, "static %s(%s)"
                    % (ret.decl(name), render_params(params) if params
                       else "void"), 0)
        if self.at("{", "punc"):
            pass
        out.write(" {")
        e = sub.parse_expr()
        for stmt in sub.pending:
            out.write(" " + stmt)
        out.write(" return %s; }" % e.code if not ret.is_void()
                  else " %s; }" % e.code)
        self.unit.emitted.append(out.text())
        self.unit.fn_sigs[name] = (ret, [t for _, t in params])
        self.unit.statics.add(name)
        # The value of a closure expression is a function pointer. C cannot
        # spell one inline in every position a type is needed, so each
        # distinct signature gets a typedef, generated on demand exactly like
        # a slice or tuple struct.
        ptr = self.unit.fn_ptr_type(ret, [t for _, t in params])
        return Expr(name, ptr)

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
        """Lower `[a, b, c]` and the `[v; N]` repeat form to a C initializer.

        C has no repeat-initializer syntax. Two cases can still be lowered:
        an all-zero repeat, where `{0}` zero-fills whatever the annotation
        sizes; and any repeat whose length is an integer literal, which is
        written out as that many elements. A non-literal length with a
        non-zero value has nothing to expand to and is rejected.
        """
        if self.at("]", "punc"):
            self.next()
            self.err("empty array literal has no element type")
        first = self.parse_expr()
        if self.accept(";"):
            count = self.parse_expr()
            self.expect("]")
            if first.code.strip() in ("0", "0.0"):
                return Expr("{0}", None)
            n = _literal_count(count.code, self.unit)
            if n is None:
                self.err("the repeat length in `[v; N]` must be an integer "
                         "literal or a `const` when the value is not 0; "
                         "list the elements explicitly")
            if n > _MAX_REPEAT:
                self.err("repeat length %d exceeds the %d-element limit for "
                         "`[v; N]`; build the array in a loop", n, _MAX_REPEAT)
            if n <= 0:
                self.err("repeat length must be positive")
            ty = first.type
            if ty is not None:
                ty = CType(ty.base, ty.ptr, (ty.array or []) + [str(n)])
            return Expr("{%s}" % ", ".join([first.code] * n), ty)
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
            if self.at(")", "punc"):
                self.next()
                return Expr("0", VOID)              # the unit value
            first = self.parse_expr()
            if not self.at(",", "punc"):
                self.expect(")")
                return Expr("(%s)" % first.code, first.type)
            items = [first]
            while self.accept(","):
                if self.at(")", "punc"):
                    break
                items.append(self.parse_expr())
            self.expect(")")
            if any(i.type is None for i in items):
                self.err("cannot infer the type of a tuple element; "
                         "annotate it")
            ty = self.unit.tuple_type([i.type for i in items])
            inits = ", ".join("._%d = %s" % (k, i.code)
                              for k, i in enumerate(items))
            return Expr("(%s){%s}" % (ty.base, inits), ty)
        if t.val == "[" and t.kind == "punc":
            return self.parse_array_literal(t)
        if t.val == "if" and t.kind == "kw":
            self.i -= 1
            return self.parse_if_expr()
        if (t.val == "|" and t.kind == "punc") or \
                (t.val == "||" and t.kind == "punc"):
            self.i -= 1
            return self.parse_closure()
        if t.val == "move" and t.kind == "ident" and \
                self.cur.val in ("|", "||"):
            self.next() if False else None
            return self.parse_closure()
        if t.val == "unsafe" and t.kind == "kw" and self.at("{", "punc"):
            # `unsafe { expr }` as a value is its single tail expression.
            return self.parse_block_expr()
        if t.kind == "ident" and self.at("!", "punc") and \
                self.peek().val in ("(", "[", "{"):
            return self.parse_macro(t.val)
        if t.kind == "ident" or (t.kind == "kw" and t.val == "Self"):
            name = t.val
            if name == "Self" and self.impl_type:
                name = self.impl_type
            targs = None
            saw_path = False
            if name in self.tysubst and self.at("::", "punc"):
                # `T::CONST` inside a generic item being monomorphised. The
                # type parameter has to be replaced with the concrete type
                # before the path is flattened, or the result names a type
                # that does not exist -- `T_N` rather than `X_N`. This is what
                # makes a trait's associated consts reachable through a bound.
                name = self.tysubst[name].base
            while self.at("::", "punc"):        # path: foo::bar -> foo_bar
                saw_path = True
                self.next()
                if self.at("<", "punc"):
                    # Turbofish. On a generic struct (`Pair::<i32>::new`) it
                    # names an instantiation, so materialise it and continue
                    # down the path; on a generic fn it is carried to the call
                    # site, which is where the instantiation is needed.
                    args = self.parse_type_args()
                    if name in self.unit.generic_structs:
                        name = self.instantiate_struct(name, args).base
                    else:
                        targs = args
                    continue
                name += "_" + self.expect_ident()
            # `Name { ... }` is a struct literal, except in a condition
            # position, where Rust also treats the brace as a block.
            if (self.at("{", "punc") and not self.no_struct_lit
                    and self._payload_of(name) is not None):
                # `Shape::Rect { w: 3.0, h: 4.0 }` -- a struct-form variant.
                fields = self._payload_of(name)
                self.next()
                given = {}
                while not self.at("}", "punc"):
                    fname = self.expect_ident()
                    self.expect(":")
                    want = dict((f, t) for f, t in fields).get(fname)
                    given[fname] = self.parse_expr_as(want).code
                    if not self.accept(","):
                        break
                self.expect("}")
                missing = [f for f, _ in fields if f not in given]
                if missing:
                    self.err("`%s` is missing %s", name,
                             ", ".join("`%s`" % m for m in missing))
                return self.data_variant_literal(
                    name, [given[f] for f, _ in fields])
            if (self.at("{", "punc") and not self.no_struct_lit
                    and name in self.unit.structs):
                return self.parse_struct_literal(name, t.line)
            if (self.at("{", "punc") and not self.no_struct_lit
                    and name in self.unit.generic_structs):
                # `Pair { a: 1, b: 2 }` names a generic, so which
                # instantiation it is comes from context -- a `let`
                # annotation, a return type or a parameter -- exactly as
                # `None` resolves its `Option`.
                want = self.target
                if want is not None and \
                        self.unit.instances.get(want.base, (None, None))[0] \
                        == name:
                    return self.parse_struct_literal(want.base, t.line)
                self.err("cannot tell which instantiation of `%s` this "
                         "literal is; annotate it (`let x: %s<i32> = ..`) or "
                         "use a turbofish (`%s::<i32> { .. }`)",
                         name, name, name)
            if name == "None" and not self.at("::", "punc"):
                return self.none_expr()
            if (name in self.unit.unit_structs and self.lookup(name) is None
                    and not self.at("(", "punc")):
                # A unit struct is its own value in Rust, so a bare mention
                # constructs one. C has no empty struct, so the lowered type
                # carries one placeholder byte; zeroing it is the whole
                # construction.
                return Expr("(%s){0}" % name, CType(name))
            ty = self.lookup(name)
            if ty is None:
                if name in self.unit.variants:
                    owner = self.unit.variants[name]
                    ty = CType(owner)
                    if owner in self.unit.data_enums and \
                            not self.at("(", "punc"):
                        # A payload-free variant of a tagged union is still a
                        # whole value, not a bare tag: `Shape::Empty` has to
                        # build `(Shape){.tag = Shape_Empty}` so it can be
                        # assigned and passed like any other `Shape`.
                        out = Expr("(%s){.tag = %s}" % (owner, name), ty)
                        out.from_path = saw_path
                        return out
                elif name in self.unit.consts:
                    ty = self.unit.consts[name]
            if self.lookup(name) is not None:
                name = _c_name(name)
            out = Expr(name, ty)
            out.from_path = saw_path
            if targs is not None:
                out.targs = targs
            return out
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
            code = ty.decl(_c_name(name))
            if init is not None:
                code += " = " + init.code
            self.emit_pending(out, t.line, indent)
            out.line_at(t.line, code + ";", indent)
            return

        if t.val == "return" and t.kind == "kw":
            self.next()
            if self.accept(";"):
                out.line_at(t.line, "return;", indent)
                return
            e = self.parse_expr_as(self.ret_type)
            self.expect(";")
            self.emit_pending(out, t.line, indent)
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
            self.emit_pending(out, t.line, indent)
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
            if not self.at("..", "punc") and not self.at("..=", "punc"):
                # `for x in xs` over a slice or an array, rather than a range.
                self.parse_for_each(out, indent, t, var, lo)
                return
            if self.accept("..="):
                cmp_op = "<="
            else:
                self.expect("..")
                cmp_op = "<"
            hi = self.parse_cond()
            ity = wider(lo.type, hi.type)
            cvar = _c_name(var)
            out.line_at(t.line, "for (%s = %s; %s %s %s; %s++)"
                        % (ity.decl(cvar), lo.code, cvar, cmp_op, hi.code,
                           cvar), indent)
            self.push()
            self.declare(var, ity)
            self.parse_block(out, indent, False)
            self.pop()
            return

        if t.val == "{" and t.kind == "punc":
            self.parse_block(out, indent, False)
            return

        if (t.val == "unsafe" and t.kind == "kw"
                and self.peek().val == "{"):
            # An `unsafe` block carries no meaning here: Crust has no borrow
            # checker and no safety analysis to switch off, and the C it
            # lowers to is unsafe throughout. So it is exactly its body.
            # (`unsafe fn` is handled with the other item modifiers.)
            self.next()
            self.parse_block(out, indent, tail_returns)
            return

        # expression statement, or a trailing expression
        e = self.parse_expr_as(self.ret_type if tail_returns else None)
        if self.accept(";"):
            self.emit_pending(out, t.line, indent)
            out.line_at(t.line, e.code + ";", indent)
            return
        if not self.at("}", "punc"):
            raise CrustError("line %d: expected `;` after expression"
                             % self.cur.line)
        self.emit_pending(out, t.line, indent)
        if tail_returns and not self.ret_type.is_void():
            out.line_at(t.line, "return %s;" % e.code, indent)
        else:
            out.line_at(t.line, e.code + ";", indent)

    def parse_for_each(self, out, indent, t, var, subject):
        """Lower `for x in xs { .. }` over a slice or an array.

        Rust's `for` drives an iterator; Crust has none, so the loop is
        lowered to the index loop the user would otherwise have written by
        hand -- `for i in 0..xs.len()` plus `let x = xs[i]`. The subject is
        held in a temporary so it is evaluated exactly once even when it is a
        call, and the whole thing is wrapped in a block to scope both the
        temporary and the induction variable.

        The binding is a *copy* of the element, matching
        `for x in xs.iter().copied()` rather than Rust's reference binding.
        Crust has no borrow checker to make the difference observable, and a
        copy is what the C the user is mixing with would do.
        """
        ty = subject.type
        if ty is None:
            self.err("cannot infer the type of the iterated expression; "
                     "annotate it")
        if ty.ptr and self.unit.instances.get(ty.base, (None,))[0] == "PyList":
            pass                       # handled below, unlike other pointers

        idx = self.new_index()
        if ty.base in self.unit.slices:
            elem = self.unit.slices[ty.base]
            tmp = self.new_temp()
            out.line_at(t.line, "{ %s = %s;" % (ty.decl(tmp), subject.code),
                        indent)
            base, count = tmp + ".ptr", tmp + ".len"
        elif ty.array:
            elem = CType(ty.base, ty.ptr, ty.array[1:] or None)
            if elem.array:
                self.err("iterating a multi-dimensional array is not "
                         "supported; index the outer dimension")
            out.line_at(t.line, "{", indent)
            base, count = subject.code, ty.array[0]
        elif self.unit.instances.get(ty.base, (None,))[0] == "PyList":
            # The list an included rpython module built. It carries its own
            # length, so it can be walked exactly like a slice -- which is how
            # Crust gets iteration over a built-up collection without owning
            # an iterator protocol. A pointer to one is accepted too, since
            # that is how py2c hands it back.
            elem = self.unit.instances[ty.base][1][0]
            tmp = self.new_temp()
            arrow = "->" if ty.ptr else "."
            out.line_at(t.line, "{ %s = %s;" % (ty.decl(tmp), subject.code),
                        indent)
            base = "%s%sdata" % (tmp, arrow)
            count = "%s%slen" % (tmp, arrow)
        elif ty.ptr:
            self.err("cannot iterate a raw pointer, as its length is not "
                     "known; slice it first (`&p[0..n]`) or use a range")
        else:
            self.err("`%s` cannot be iterated", ty.decl())

        out.write(" for (unsigned long %s = 0; %s < %s; %s++)"
                  % (idx, idx, count, idx))
        self.push()
        self.declare(var, elem)
        self.emit_bound_block(out, indent, "%s = %s[%s];"
                              % (elem.decl(_c_name(var)), base, idx))
        self.pop()
        out.write(" }")

    def new_index(self):
        self.tmp_n += 1
        return "_crust_i%d" % self.tmp_n

    def emit_bound_block(self, out, indent, decl):
        """Emit `{ <decl> <body> }` for a loop or pattern binding."""
        open_tok = self.expect("{")
        out.line_at(open_tok.line, "{ " + decl, indent)
        self.push()
        while not self.at("}", "punc"):
            if self.cur.kind == "eof":
                self.err("unterminated block")
            self.parse_stmt(out, indent + 1, False)
        close = self.expect("}")
        self.pop()
        out.line_at(close.line, "}", indent)

    def parse_if(self, out, indent, tail_returns):
        t = self.expect("if")
        cond = self.parse_cond()
        self.emit_pending(out, t.line, indent)
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
        self.emit_pending(out, t.line, indent)
        sty0 = scrutinee.type
        data_enum = (sty0 is not None and not sty0.ptr
                     and sty0.base in self.unit.data_enums)
        if data_enum:
            # A tagged union switches on its tag, and each arm may bind the
            # payload. The scrutinee is held in a temporary so it is evaluated
            # once even when it is a call, and so the bindings have something
            # stable to read from.
            subject = self.new_temp()
            out.line_at(t.line, "{ %s = %s;"
                        % (sty0.decl(subject), scrutinee.code), indent)
            out.write(" switch (%s.tag)" % subject)
        else:
            subject = None
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
            self.binds = []
            labels = self.parse_patterns(enum_name, covered)
            if labels is None:
                has_default = True
                out.line_at(arm.line, "default:", indent + 1)
            else:
                out.line_at(arm.line, " ".join("case %s:" % v
                                               for v in labels), indent + 1)
            self.expect("=>")
            binds = self.binds
            self.binds = []
            self.push()
            if binds:
                # Bindings are declared inside a block so their names cannot
                # leak into a later arm, which C's fall-through-free `case`
                # labels would otherwise allow.
                out.write(" {")
                for bname, bty, path in binds:
                    self.declare(bname, bty)
                    out.write(" %s = %s.u.%s;"
                              % (bty.decl(_c_name(bname)), subject, path))
            self.parse_arm_body(out, indent + 2, tail_returns)
            if binds:
                out.write(" }")
            self.pop()
            out.write(" break;")
            self.accept(",")

        close = self.expect("}")
        out.line_at(close.line, "}", indent)
        if data_enum:
            out.write(" }")

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
            fields = self._payload_of(name)
            if self.at("(", "punc") or self.at("{", "punc"):
                if fields is None:
                    self.err("`%s` carries no data to destructure", name)
                self._bind_payload(name, fields)
            return name
        self.err("unsupported pattern %r", t.val or "<eof>")

    def _bind_payload(self, flat, fields):
        """Record the bindings introduced by `Variant(a, b)` / `Variant { x }`.

        Names are collected rather than emitted here, because the arm's body
        has not been parsed yet -- `parse_match` declares them at the top of
        the arm's block, where they are in scope for exactly that arm.
        """
        owner = self.unit.variants[flat]
        vname = flat[len(owner) + 1:]
        if self.accept("("):
            i = 0
            while not self.at(")", "punc"):
                if self.accept("..") or self.accept("..."):
                    break
                bname = self.expect_ident()
                if i >= len(fields):
                    self.err("`%s` has only %d field%s", flat, len(fields),
                             "" if len(fields) == 1 else "s")
                fname, fty = fields[i]
                if bname != "_":
                    self.binds.append(
                        (bname, fty, "%s.%s" % (vname, fname or "_%d" % i)))
                i += 1
                if not self.accept(","):
                    break
            self.expect(")")
            return
        self.expect("{")
        by_name = {f: (k, t) for k, (f, t) in enumerate(fields) if f}
        while not self.at("}", "punc"):
            if self.accept("..") or self.accept("..."):
                break
            fname = self.expect_ident()
            bname = self.expect_ident() if self.accept(":") else fname
            if fname not in by_name:
                self.err("`%s` has no field `%s`", flat, fname)
            _, fty = by_name[fname]
            if bname != "_":
                self.binds.append((bname, fty, "%s.%s" % (vname, fname)))
            if not self.accept(","):
                break
        self.expect("}")

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
        """Parse an enum; return (name, variants, payloads).

        A variant may carry data, in either tuple form `Circle(f64)` or struct
        form `Rect { w: f64, h: f64 }`. Payload fields are recorded positionally
        as `_0`, `_1`, ... for tuple variants and by name for struct ones, so
        both are matched and constructed through one mechanism.
        """
        self.skip_attributes()
        self.skip_visibility()
        self.item_derives = self.derives
        self.expect("enum")
        name = self.expect_ident()
        self.expect("{")
        variants, payloads = [], {}
        while not self.at("}", "punc"):
            self.skip_attributes()
            vname = self.expect_ident()
            payload = None
            if self.at("(", "punc"):
                # Tuple variant: `Circle(f64)`.
                self.next()
                payload = []
                while not self.at(")", "punc"):
                    payload.append((None, self.parse_type()))
                    if not self.accept(","):
                        break
                self.expect(")")
            elif self.at("{", "punc"):
                # Struct variant: `Rect { w: f64, h: f64 }`.
                self.next()
                payload = []
                while not self.at("}", "punc"):
                    self.skip_attributes()
                    fname = self.expect_ident()
                    self.expect(":")
                    payload.append((fname, self.parse_type()))
                    if not self.accept(","):
                        break
                self.expect("}")
            value = None
            if self.accept("="):
                value = self.parse_expr().code
            if payload is not None:
                payloads[vname] = payload
            variants.append((vname, value))
            if not self.accept(","):
                break
        self.expect("}")
        return name, variants, payloads

    def parse_const_signature(self):
        """Parse `const|static [mut] NAME: T = expr;` without emitting."""
        self.skip_attributes()
        self.skip_visibility()
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

    def skip_visibility(self):
        """Skip `pub`, `pub(crate)`, `pub(in path)` and friends."""
        while self.cur.val == "pub":
            self.next()
            if self.at("(", "punc"):
                depth = 0
                while self.cur.kind != "eof":
                    if self.cur.val == "(":
                        depth += 1
                    elif self.cur.val == ")":
                        depth -= 1
                        if depth == 0:
                            self.next()
                            break
                    self.next()

    def skip_attributes(self):
        """Skip `#[...]` outer attributes, recording any `derive` list.

        Everything except `derive` is genuinely ignored -- `#[repr(C)]` is
        already how Crust lays a struct out, and `#[inline]` and friends are
        the backend's business. The derived trait names are kept in
        `self.derives` for the caller that is about to parse the item.
        """
        self.derives = []
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
                elif t.kind == "ident" and t.val == "derive" \
                        and self.at("(", "punc"):
                    d = self.i + 1
                    while d < len(self.toks) and self.toks[d].val != ")":
                        if self.toks[d].kind == "ident":
                            self.derives.append(self.toks[d].val)
                        d += 1

    def parse_struct(self):
        """Parse `struct Name { f: T, ... }`, returning (name, fields)."""
        self.skip_attributes()
        self.skip_visibility()
        self.item_derives = self.derives
        self.expect("struct")
        name = self.expect_ident()
        self.skip_generic_params()
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
            if not ftype.ptr and ftype.base in self.unit.opaque_structs:
                self.unit.opaque_complete.add(ftype.base)
            fields.append((fname, ftype))
            if not self.accept(","):
                break
        self.expect("}")
        return name, fields

    def parse_tuple_struct(self):
        """Parse `struct P(T, U);`, naming the fields `_0`, `_1`, ..."""
        self.skip_attributes()
        self.skip_visibility()
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

    def parse_unit_struct(self):
        """Parse `struct S;`, a struct with no fields.

        C11 has no empty struct, so one placeholder byte is added. The field
        is named with the reserved `_crust_` prefix so it cannot collide with
        anything the user writes, and nothing but the generated compound
        literal ever mentions it.
        """
        self.skip_attributes()
        self.skip_visibility()
        self.expect("struct")
        name = self.expect_ident()
        self.expect(";")
        return name, [("_crust_unit", CType("char"))]

    def is_assoc_const(self):
        """True if an `impl` body item is `const NAME: T = ...;`."""
        j = self.i
        while self.toks[j].val in ("pub", "#"):
            if self.toks[j].val == "#":
                return False                     # attribute; let the parser
            j += 1                               # handle it normally
        return self.toks[j].val == "const" and self.toks[j].kind == "kw"

    def parse_impl_header(self):
        """Parse `impl[<P..>] [Trait for] Name[<A..>] {`; return the type name.

        `self.impl_trait` is set to the trait name for a trait impl and to
        None for an inherent one. The *owner* is always the type after `for`,
        because that is what the methods are named after and what a call site
        will look them up under.
        """
        self.skip_attributes()
        self.expect("impl")
        self.impl_params = self.parse_generic_params()
        self.impl_trait = None
        name = self.parse_path_name()
        if self.at("<", "punc"):
            self.skip_generic_params()
        if self.accept("for"):
            self.impl_trait = name
            name = self.parse_path_name()
            if self.at("<", "punc"):
                self.skip_generic_params()
        self.expect("{")
        return name

    def parse_path_name(self):
        """Parse `Name` or `mod::Name`, flattening the path to `mod_Name`."""
        name = self.expect_ident()
        while self.at("::", "punc"):
            self.next()
            if self.at("<", "punc"):
                self.skip_generic_params()
                continue
            name += "_" + self.expect_ident()
        return name

    def parse_trait(self):
        """Parse `trait Name[: Super..] { .. }`.

        Returns (name, supertraits, methods, defaults). A method with a body
        is a *default*: its tokens are kept so that an `impl` which does not
        override it can have one generated with `Self` bound to the
        implementing type.
        """
        self.skip_attributes()
        self.skip_visibility()
        while self.cur.val == "unsafe":
            self.next()
        self.expect("trait")
        name = self.expect_ident()
        self.skip_generic_params()
        supers = []
        if self.accept(":"):
            while True:
                if self.cur.kind not in ("ident", "kw"):
                    break
                supers.append(self.parse_path_name())
                if self.at("<", "punc"):
                    self.skip_generic_params()
                if not self.accept("+"):
                    break
        while self.cur.val == "where":       # `where` clauses carry no
            while not self.at("{", "punc"):  # information Crust can act on
                if self.cur.kind == "eof":
                    self.err("unterminated `where` clause")
                self.next()
        self.expect("{")
        methods, defaults, const_defaults = {}, {}, {}
        while not self.at("}", "punc"):
            if self.cur.kind == "eof":
                self.err("unterminated trait body")
            if self.cur.val == "const":
                # An associated const. One with a default (`const N: i32 = 7;`
                # or `const M = Self::N;`) is inherited by any impl that does
                # not define it, exactly as a default method is -- rmm's
                # `Arch` trait leans on this heavily.
                start = self.i
                end = self._to_semi()
                has_default = any(t.val == "=" for t in self.toks[start:end])
                if has_default:
                    const_defaults[self.toks[start + 1].val] = \
                        self.toks[start:end]
                self.i = end                  # past the `;`
                continue
            if self.cur.val == "type":
                while not self.at(";", "punc") and self.cur.kind != "eof":
                    self.next()
                self.accept(";")
                continue
            start = self.i
            _, info = self.parse_method_signature(name)
            methods[info.name] = info
            if self.at("{", "punc"):
                defaults[info.name] = self.toks[start:self.skip_to_body_end()]
            else:
                self.expect(";")
        self.expect("}")
        return name, supers, methods, defaults, const_defaults

    def _to_semi(self):
        """Index just past the `;` that ends the item at the cursor."""
        j = self.i
        while j < len(self.toks) and self.toks[j].val != ";":
            j += 1
        return j + 1

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
        self.skip_visibility()
        while self.cur.val == "unsafe":
            self.next()
        start = self.expect("fn")
        name = self.expect_ident()
        self.skip_generic_params()
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
                    return self.i

    def parse_impl(self, out, owner_override=None):
        """Translate an `impl` block into free functions."""
        owner = self.parse_impl_header()
        if owner_override is not None:
            owner = owner_override
        self.impl_type = owner
        while not self.at("}", "punc"):
            if self.cur.kind == "eof":
                self.err("unterminated impl block")
            if self.is_assoc_const():
                self.parse_const_signature()     # hoisted into the prelude
                continue
            start, info = self.parse_method_signature(owner)
            if owner_override is not None:
                self.unit.methods[(owner, info.name)] = info
                selfp = []
                if info.self_kind == "ref":
                    selfp = [CType(owner, 1)]
                elif info.self_kind == "value":
                    selfp = [CType(owner)]
                self.unit.fn_sigs[info.mangled] = (
                    info.ret, selfp + [t for _, t in info.params])
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
        self.skip_visibility()
        while self.cur.val == "unsafe":
            self.next()
        if self.accept("extern"):
            if self.cur.kind == "str":
                self.next()
        start = self.expect("fn")
        name = self.expect_ident()
        self.skip_generic_params()
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

    def parse_fn(self, out, name_override=None):
        start, name, params, ret = self.parse_fn_signature()
        if name_override is not None:
            name = name_override
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


_BUILTIN_MACROS = {
    "assert": Parser.m_assert,
    "debug_assert": Parser.m_dbg_noop,
    "debug_assert_eq": Parser.m_dbg_noop,
    "debug_assert_ne": Parser.m_dbg_noop,
    "assert_eq": lambda p, a, l: Parser.m_assert_cmp(p, a, l, "=="),
    "assert_ne": lambda p, a, l: Parser.m_assert_cmp(p, a, l, "!="),
    "panic": Parser.m_abort,
    "unreachable": Parser.m_abort,
    "unimplemented": Parser.m_abort,
    "todo": Parser.m_abort,
    "abort": Parser.m_abort,
    "print": lambda p, a, l: Parser.m_print(p, a, l, False),
    "println": lambda p, a, l: Parser.m_print(p, a, l, True),
    "eprint": lambda p, a, l: Parser.m_print(p, a, l, False, "stderr"),
    "eprintln": lambda p, a, l: Parser.m_print(p, a, l, True, "stderr"),
    "cfg": Parser.m_cfg,
    "matches": Parser.m_matches,
}


# Fragment specifiers Crust understands. They all capture a token run; the
# difference between them is only how far it extends, and for the shapes
# Crust supports that is decided by what follows in the pattern.
_FRAGMENTS = {"expr", "ident", "ty", "tt", "literal", "path", "block", "stmt"}


def _match_pattern(pat, toks):
    """Match `toks` against a macro rule pattern; return the bindings or None.

    Literal tokens must match exactly. `$x:frag` captures the token run up to
    the pattern's next literal token, respecting nesting so that a comma
    inside `f(a, b)` does not end the capture.
    """
    binds, pi, ti = {}, 0, 0
    while pi < len(pat):
        p = pat[pi]
        if p.val == "$" and pi + 1 < len(pat):
            var = pat[pi + 1].val
            frag = None
            if pi + 3 < len(pat) and pat[pi + 2].val == ":":
                frag = pat[pi + 3].val
                pi += 4
            else:
                pi += 2
            if frag is not None and frag not in _FRAGMENTS:
                return None
            stop = pat[pi].val if pi < len(pat) else None
            if frag in ("ident", "literal", "tt"):
                # Exactly one token (a `tt` that opens a group is handled by
                # the depth walk below, so only the simple case is special).
                if ti >= len(toks):
                    return None
                if not (frag == "tt" and toks[ti].val in ("(", "[", "{")):
                    binds[var] = [toks[ti]]
                    ti += 1
                    continue
            start, depth = ti, 0
            while ti < len(toks):
                t = toks[ti]
                if t.val in ("(", "[", "{") and t.kind == "punc":
                    depth += 1
                elif t.val in (")", "]", "}") and t.kind == "punc":
                    depth -= 1
                elif depth == 0 and stop is not None and t.val == stop:
                    break
                elif depth == 0 and stop is None and t.val == "," \
                        and t.kind == "punc":
                    # An `expr` cannot contain a top-level comma, so the
                    # comma belongs to the caller's argument list, not to
                    # this fragment. Without this a one-argument rule would
                    # greedily swallow a two-argument invocation and the
                    # later, correct rule would never be tried.
                    break
                ti += 1
            if ti == start:
                return None                       # a fragment cannot be empty
            binds[var] = toks[start:ti]
            continue
        if ti >= len(toks) or toks[ti].val != p.val:
            return None
        pi += 1
        ti += 1
    return binds if ti == len(toks) else None


def _substitute(body, binds):
    """Replace every `$name` in a rule body with its captured tokens."""
    out, i = [], 0
    while i < len(body):
        t = body[i]
        if t.val == "$" and i + 1 < len(body) and body[i + 1].val in binds:
            out.extend(binds[body[i + 1].val])
            i += 2
            continue
        out.append(t)
        i += 1
    return out


# Identifiers that are ordinary names in Rust but keywords in C. A Rust
# variable called `double` or `register` is perfectly legal, and lowering it
# verbatim produces C that will not parse -- so locals and parameters are
# renamed on the way out. The trailing underscore cannot collide with a Rust
# identifier that Crust would produce for anything else.
_C_KEYWORDS = {
    "auto", "break", "case", "char", "const", "continue", "default", "do",
    "double", "else", "enum", "extern", "float", "for", "goto", "if",
    "inline", "int", "long", "register", "restrict", "return", "short",
    "signed", "sizeof", "static", "struct", "switch", "typedef", "union",
    "unsigned", "void", "volatile", "while", "_Bool", "_Complex", "asm",
    "typeof", "complex",
}


def _c_name(name):
    """Rename an identifier that would collide with a C keyword."""
    return name + "_" if name in _C_KEYWORDS else name


# `core` free functions with a direct C equivalent.
#
# These are not a standard library -- they are the handful of one-line helpers
# real Rust reaches for constantly and that have an exact lowering. Measured
# across the Redox kernel and relibc: `ptr::null_mut` 21 uses,
# `slice::from_raw_parts` 25, `cmp::min` 15, `hint::spin_loop` 18. Each would
# otherwise be an undefined symbol at link time.
#
# Matched on the tail of the flattened path, so `core::ptr::null_mut`,
# `ptr::null_mut` and `null_mut` all resolve to the same thing -- crates spell
# the import differently and the call site follows whatever was imported.


def _ci_null(p, args, atys, targs):
    ty = targs[0] if targs else CType("void")
    return Expr("((%s)0)" % CType(ty.base, ty.ptr + 1).decl(),
                CType(ty.base, ty.ptr + 1))


def _ci_read(p, args, atys, targs):
    if not args:
        p.err("`ptr::read` needs a pointer")
    ty = atys[0]
    inner = CType(ty.base, max(ty.ptr - 1, 0), ty.array) if ty else None
    return Expr("(*(%s))" % args[0], inner)


def _ci_write(p, args, atys, targs):
    if len(args) < 2:
        p.err("`ptr::write` needs a pointer and a value")
    return Expr("(*(%s) = (%s))" % (args[0], args[1]),
                atys[1] if len(atys) > 1 else None)


def _ci_copy(p, args, atys, targs):
    """`ptr::copy_nonoverlapping(src, dst, count)` -- memcpy, in Rust's order.

    Rust puts the source first and counts *elements*; C's memcpy puts the
    destination first and counts bytes. Getting either backwards silently
    corrupts memory, so the element size is taken from the pointee type
    rather than assumed.
    """
    if len(args) < 3:
        p.err("`copy_nonoverlapping` needs src, dst and count")
    ty = atys[0]
    elem = CType(ty.base, max(ty.ptr - 1, 0)).decl() if ty else "char"
    p.unit.needs.add("memcpy")
    return Expr("memcpy(%s, %s, (%s) * sizeof(%s))"
                % (args[1], args[0], args[2], elem), CType("void"))


def _ci_min(p, args, atys, targs):
    if len(args) < 2:
        p.err("`cmp::min` needs two operands")
    return Expr("((%s) < (%s) ? (%s) : (%s))"
                % (args[0], args[1], args[0], args[1]), atys[0])


def _ci_max(p, args, atys, targs):
    if len(args) < 2:
        p.err("`cmp::max` needs two operands")
    return Expr("((%s) > (%s) ? (%s) : (%s))"
                % (args[0], args[1], args[0], args[1]), atys[0])


def _ci_nop(p, args, atys, targs):
    """`hint::spin_loop`, `hint::black_box` and friends -- nothing to emit.

    `spin_loop` is a `pause` instruction hint; omitting it costs a little
    power in a spin wait and changes no semantics.
    """
    return Expr("((void)0)", CType("void"))


def _ci_swap(p, args, atys, targs):
    if len(args) < 2:
        p.err("`mem::swap` needs two pointers")
    ty = atys[0]
    inner = CType(ty.base, max(ty.ptr - 1, 0)) if ty else CType("int")
    tmp = p.new_temp()
    p.pending.append("%s = *(%s); *(%s) = *(%s); *(%s) = %s;"
                     % (inner.decl(tmp), args[0], args[0], args[1],
                        args[1], tmp))
    return Expr("((void)0)", CType("void"))


def _ci_from_raw_parts(p, args, atys, targs):
    """`slice::from_raw_parts(ptr, len)` -- exactly Crust's own slice."""
    if len(args) < 2:
        p.err("`from_raw_parts` needs a pointer and a length")
    ty = atys[0]
    elem = CType(ty.base, max(ty.ptr - 1, 0)) if ty else CType("unsigned char")
    sl = p.unit.slice_type(elem)
    return Expr("(%s){.ptr = %s, .len = %s}" % (sl.base, args[0], args[1]), sl)


_CORE_FNS = {
    "ptr_null": _ci_null,
    "ptr_null_mut": _ci_null,
    "null": _ci_null,
    "null_mut": _ci_null,
    "ptr_read": _ci_read,
    "ptr_read_volatile": _ci_read,
    "ptr_write": _ci_write,
    "ptr_write_volatile": _ci_write,
    "ptr_copy_nonoverlapping": _ci_copy,
    "copy_nonoverlapping": _ci_copy,
    "cmp_min": _ci_min,
    "cmp_max": _ci_max,
    "min": _ci_min,
    "max": _ci_max,
    "hint_spin_loop": _ci_nop,
    "spin_loop": _ci_nop,
    "hint_black_box": _ci_nop,
    "mem_swap": _ci_swap,
    "swap": _ci_swap,
    "slice_from_raw_parts": _ci_from_raw_parts,
    "slice_from_raw_parts_mut": _ci_from_raw_parts,
    "from_raw_parts": _ci_from_raw_parts,
    "from_raw_parts_mut": _ci_from_raw_parts,
}


def _core_intrinsic(name):
    """The lowering for a flattened `core` path call, or None.

    Matched on the tail so `core_ptr_null_mut`, `ptr_null_mut` and `null_mut`
    all resolve. A locally defined function of the same name shadows this --
    the caller checks its own tables first.
    """
    if name in _CORE_FNS:
        return _CORE_FNS[name]
    for key, fn in _CORE_FNS.items():
        if name.endswith("_" + key):
            return fn
    return None


def _is_lvalue(code):
    """True if `code` denotes a place whose address C can take.

    Deliberately conservative: names, field selections, dereferences and
    indexing are places; anything containing a call or a compound literal is
    not. Being wrong in the safe direction costs one temporary.
    """
    text = code.strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    if "(" in text or "{" in text:
        return False
    return bool(text) and all(c.isalnum() or c in "_.->[] " for c in text)


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
    return ", ".join(ty.decl(_c_name(nm)) for nm, ty in params)


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
    r"\b(?P<kw>fn|struct|impl|enum|const|static|trait|type)"
    r"(?:\s+|\s*(?=<))(?:<[^<>]*>\s*)?"
    r"(?:mut\s+)?(?P<name>[A-Za-z_]\w*)")
# Item modifiers that may precede `fn`/`struct`/... The scan text this runs
# against has had string literals blanked, so `extern "C"` appears as
# `extern " "` (or with the quotes gone entirely); matching the literal
# spelling would miss every `pub unsafe extern "C" fn` in real FFI code.
_MODIFIER = re.compile(
    r"(?:\b(?:pub|unsafe)\b(?:\s*\([^)]*\))?\s+|\bextern\b\s*"
    r"(?:\"[^\"]*\"|\'[^\']*\')?\s*)*$")


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
        elif (c == "'" and i + 1 < n and (code[i + 1].isalpha()
                                          or code[i + 1] == "_")
              and not (i + 2 < n and code[i + 2] == "'")):
            # A lifetime, not a char literal. Blanking it as a literal would
            # swallow everything up to the next quote and destroy the item
            # structure this scan exists to find.
            i += 1
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


# Item-level declarations that exist only for Rust's module system and have
# no C counterpart at all. They are *erased*, not translated: `use` brings a
# name into scope, and Crust resolves names by flattening paths instead, so by
# the time the C lexer runs there is nothing for a `use` to do.
#
# This was invisible for a long time because these translate cleanly -- Crust
# passes unrecognized text through byte-for-byte, so `use core::mem;` survives
# translation intact and only fails later, in the C front end. Anything that
# measures `translate()` alone will not see it.
# A macro invoked at file scope -- `global_asm!(..)`, `int_like!(..)`,
# `syscall!(..)`. These expand to items Crust cannot produce, and passing them
# through verbatim guarantees a C syntax error: `global_asm!` in particular
# carries a multi-line assembly string that the C lexer reads as an
# unterminated quote. Erasing loses whatever the macro would have defined,
# which is a real cost, but the alternative is a file that cannot compile at
# all rather than one that compiles without those definitions.
_ITEM_MACRO = re.compile(r"^[ \t]*(?:pub(?:\s*\([^)]*\))?\s+)?"
                         r"[A-Za-z_]\w*!\s*[\(\[\{]", re.M)

_ERASED_HEAD = re.compile(
    r"^[ \t]*(?:pub(?:\s*\([^)]*\))?\s+)?"
    r"(?:use\b|extern\s+crate\b|mod\s+\w+\s*;)", re.M)

# Bare `mod name;` (optional `pub` / `pub(...)`) — not `mod name { ... }`.
# `type Result<T> = ...Result<T, SomeError>;` -- the crate-wide alias that
# makes the one-argument `Result<T>` spelling work. Matched on the text rather
# than parsed, because the alias is generic and the generic-alias machinery
# does not need to exist for this one very common shape.
_RESULT_ALIAS = re.compile(
    r"^[ \t]*(?:pub(?:\s*\([^)]*\))?\s+)?type\s+Result\s*<[^>]*>\s*=\s*"
    r"[\w:]*Result\s*<[^,<>]*,\s*(?P<err>[\w:]+)\s*>", re.M)

_MOD_DECL = re.compile(
    r"^[ \t]*(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+(?P<name>\w+)\s*;", re.M)

_MACRO_RULES = re.compile(r"\bmacro_rules!\s*(?P<name>[A-Za-z_]\w*)\s*\{")


def find_mod_decls(code):
    """Return module names from bare `mod name;` / `pub mod name;` items.

    Inline modules (`mod name { ... }`) are not matched. Comments and string
    literals are blanked first so a `mod` mentioned in prose is ignored.
    """
    scan = _blank(code)
    return [m.group("name") for m in _MOD_DECL.finditer(scan)]


_USE_PATH = re.compile(
    r"^[ \t]*(?:pub(?:\s*\([^)]*\))?\s+)?use\s+"
    r"(?P<path>(?:crate|super|self)(?:::\w+)*)", re.M)


def find_use_paths(code):
    """Module paths named by `use crate::a::b::..` and friends.

    `mod name;` only reaches a *sibling* file. Real crates put shared types
    behind a path -- `use crate::platform::types::{pid_t, size_t};` -- and
    that path says exactly which file defines them, so it is worth following.
    """
    scan = _blank(code)
    return [m.group("path") for m in _USE_PATH.finditer(scan)]


def find_crate_root(path):
    """The directory holding `lib.rs`/`main.rs` above `path`, or None.

    That directory is what `crate::` refers to. Walking up is bounded so a
    file outside any crate cannot send the search to the filesystem root.
    """
    d = os.path.dirname(os.path.abspath(path)) if path else ""
    for _ in range(12):
        if not d:
            return None
        for marker in ("lib.rs", "main.rs"):
            if os.path.isfile(os.path.join(d, marker)):
                return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def resolve_use_path(modpath, path):
    """Resolve `crate::a::b` (or `super::`/`self::`) to a source file."""
    segs = modpath.split("::")
    head, rest = segs[0], segs[1:]
    if head == "crate":
        base = find_crate_root(path)
    elif head == "self":
        base = os.path.dirname(os.path.abspath(path)) if path else ""
    elif head == "super":
        base = os.path.dirname(os.path.dirname(os.path.abspath(path))) \
            if path else ""
    else:
        return None
    if not base or not rest:
        return None
    # The last segment may name an item rather than a module, so try the full
    # path first and then one segment shorter.
    for depth in (len(rest), len(rest) - 1):
        if depth < 1:
            continue
        stem = os.path.join(base, *rest[:depth])
        for cand in (stem + ".rs", os.path.join(stem, "mod.rs")):
            if os.path.isfile(cand):
                return cand
    return None


def resolve_mod_path(name, path):
    """Resolve `mod name;` to a sibling source file, or None if missing.

    Looks for `name.rs` then `name/mod.rs` next to `path`. When `path` is
    None or has no directory, searches the current working directory.
    """
    base = os.path.dirname(path) if path else ""
    candidates = [
        os.path.join(base, name + ".rs") if base else name + ".rs",
        os.path.join(base, name, "mod.rs") if base
        else os.path.join(name, "mod.rs"),
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return None


def find_rust_items(code, rust_file=False):
    """Return (start, end, kind) spans of top-level Rust items in `code`.

    `kind` is "fn", "struct", "tuple_struct", "impl", "enum", "trait"
    or "const". C
    code between the spans is left alone. Because C shares the `struct`,
    `const` and `static` keywords, those are only claimed when the syntax that
    follows is unambiguously Rust: `name: type` fields with no semicolons for
    a struct, and a `NAME: type` annotation for a constant.
    """
    scan = _blank(code)
    depths = _depths(scan)
    spans = []
    for m in _MACRO_RULES.finditer(scan):
        start = m.start()
        if depths[start] != 0:
            continue
        close = _match_brace(scan, scan.index("{", m.end() - 1))
        if close is None:
            raise CrustError("unterminated macro_rules! near offset %d"
                             % start)
        spans.append((start, close + 1, "macro"))
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
        # `>`: turbofish / C template junk. `<`: const generics (`<const N`).
        if before.rstrip().endswith((".", ">", "<")):
            continue

        if kw in ("const", "static"):
            # Rust annotates the type; C never writes `NAME:` here. A `::`
            # is a path, not an annotation -- `*const self::P` names a type,
            # and reading it as a `const` item would swallow the rest of the
            # declaration looking for a `;`.
            rest = scan[m.end():].lstrip()
            if not rest.startswith(":") or rest.startswith("::"):
                continue
            close = scan.find(";", m.end())
            if close < 0:
                raise CrustError("unterminated Rust %s near offset %d"
                                 % (kw, start))
            spans.append((_extend_head(scan, start), close + 1, "const"))
            continue

        after = scan[m.end():]
        if kw == "type":
            # `type Name = Type;` — no braces; ends at the semicolon.
            eq = scan.find("=", m.end())
            close = scan.find(";", m.end())
            if eq < 0 or close < 0 or eq > close:
                continue
            spans.append((_extend_head(scan, start), close + 1, "type"))
            continue

        if kw == "struct" and after.lstrip().startswith(";"):
            # `struct S;` is spelled identically in both languages: a Rust
            # unit struct and a C forward declaration of an incomplete type.
            # Unlike the `enum` and struct-body cases, nothing in the text
            # itself tells them apart, and reading a C forward declaration as
            # a complete one-byte type would make `sizeof(struct S)` wrongly
            # succeed. So claim it only on evidence: the whole file is Rust,
            # or the unit gives the name an `impl` block, which C cannot.
            name = m.group("name")
            if rust_file or _has_impl_block(scan, name):
                semi = scan.index(";", m.end())
                spans.append((_extend_head(scan, start), semi + 1,
                              "unit_struct"))
            continue

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
        if kw == "trait":
            real_start = _extend_head(scan, start)
            spans.append((real_start, close + 1, "trait"))
            continue
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


def _has_impl_block(scan, name):
    """True if the unit has a top-level `impl Name` block.

    C has no `impl`, so its presence is unambiguous evidence that `struct
    Name;` above was meant as a Rust unit struct.
    """
    return re.search(r"\bimpl\s+" + re.escape(name) + r"\s*\{",
                     scan) is not None


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
    """True if a `struct X { ... }` body is Rust rather than C.

    C members end in `;` and Rust fields are separated by `,`, so a semicolon
    is the tell -- but only a *top-level* one. A Rust array type carries one
    inside brackets (`a: [i32; 4]`), and counting that made every struct with
    an array field read as C, which silently passed the whole declaration
    through untranslated instead of reporting anything.
    """
    depth = 0
    for ch in body:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch == ";" and depth <= 0:
            return False
    return re.search(r"[A-Za-z_]\w*\s*:", body) is not None


# The configuration Crust compiles for. Redox is full of `#[cfg]`-gated
# alternatives -- one `ULONG_MAX` for 32-bit and another for 64 -- and
# emitting every arm produces conflicting definitions rather than a choice.
# So a small target is assumed and the predicates are evaluated against it.
CFG = {
    "target_arch": "x86_64",
    "target_pointer_width": "64",
    "target_endian": "little",
    "target_os": "redox",
    "target_family": "unix",
    "target_env": "relibc",
}

# Bare flags (`#[cfg(unix)]`) that hold for that target.
CFG_FLAGS = {"unix"}

_CFG_ATTR = re.compile(r"#\s*\[\s*cfg\s*\(")


def _cfg_predicate(text, i):
    """Evaluate one `cfg` predicate starting at `text[i]`; return (value, end).

    Understands `all(..)`, `any(..)`, `not(..)`, `key = "value"` and bare
    flags. An unrecognized key is *false* rather than true: the point is to
    pick one arm of a set of alternatives, and treating unknowns as true would
    select several.
    """
    while i < len(text) and text[i] in " \t\n\r":
        i += 1
    m = re.compile(r"[A-Za-z_][\w]*").match(text, i)
    if not m:
        return False, i
    word = m.group(0)
    j = m.end()
    while j < len(text) and text[j] in " \t\n\r":
        j += 1
    if j < len(text) and text[j] == "(":          # all / any / not
        j += 1
        vals = []
        while True:
            while j < len(text) and text[j] in " \t\n\r,":
                j += 1
            if j >= len(text) or text[j] == ")":
                j += 1
                break
            v, j = _cfg_predicate(text, j)
            vals.append(v)
        if word == "all":
            return all(vals) if vals else True, j
        if word == "any":
            return any(vals), j
        if word == "not":
            return (not vals[0]) if vals else False, j
        return False, j
    if j < len(text) and text[j] == "=":          # key = "value"
        j += 1
        while j < len(text) and text[j] in " \t\n\r":
            j += 1
        m2 = re.compile(r'"([^"]*)"').match(text, j)
        if not m2:
            return False, j
        return CFG.get(word) == m2.group(1), m2.end()
    return word in CFG_FLAGS, j                   # bare flag


def leading_attrs(code, start):
    """The `#[...]` groups at the head of an item, as one string.

    `_extend_head` has already walked the span start back over them, so they
    sit at the front. Only the leading run is taken: an attribute deeper in
    the item belongs to a field or a method, not to the item itself.
    """
    out, i, n = [], start, len(code)
    while i < n:
        while i < n and code[i] in " \t\r\n":
            i += 1
        if i >= n or code[i] != "#":
            break
        j = code.find("[", i)
        if j < 0:
            break
        depth, k = 0, j
        while k < n:
            if code[k] == "[":
                depth += 1
            elif code[k] == "]":
                depth -= 1
                if depth == 0:
                    k += 1
                    break
            k += 1
        out.append(code[i:k])
        i = k
    return "".join(out)


def cfg_allows(attrs):
    """True unless a `#[cfg(..)]` in `attrs` evaluates false.

    Attributes other than `cfg` are ignored, and an item with no `cfg` is
    always kept -- this only ever *removes* an alternative that was written
    for a different target.
    """
    for m in _CFG_ATTR.finditer(attrs):
        value, _ = _cfg_predicate(attrs, m.end())
        if not value:
            return False
    return True


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


def _generic_params_of(toks, kind):
    """(params, name) if this item is generic, else None.

    Reads the token stream directly rather than the source text, so comments
    and whitespace cannot confuse it, and so `impl<T>` and `impl <T>` are the
    same thing.
    """
    if kind not in ("struct", "fn", "impl"):
        return None
    p = Parser(list(toks))
    try:
        p.skip_attributes()
        while p.cur.val in ("pub", "unsafe", "extern") or p.cur.kind == "str":
            p.next()
        if not p.at(kind, "kw"):
            return None
        p.next()
        if kind == "impl":
            # `impl<const N: ty>` has no type params after skipping const
            # generics, but it is still a template (do not lower the body).
            had_angle = p.at("<", "punc")
            params = p.parse_generic_params()
            name = p.parse_path_name()
            if p.at("<", "punc"):
                p.skip_generic_params()
            if p.accept("for"):
                name = p.parse_path_name()
            if had_angle:
                return (params if params else ["_const"], name)
            return None
        name = p.expect_ident()
        params = p.parse_generic_params()
        # Const-only `struct Foo<const N: ty>` lowers as a plain struct.
        return (params, name) if params else None
    except CrustError:
        return None


def collect_items(code, spans, unit, struct_order, fail=None):
    """Populate `unit` from the Rust items in `code`; return struct names.

    Only signatures and layouts are read here -- no bodies are translated --
    so that a later pass can resolve calls, methods and field types
    regardless of the order things are defined in.
    """
    local = {"structs": [], "enums": [], "consts": [], "impl_consts": [],
             "type_aliases": []}
    for start, end, kind in spans:
        toks = tokenize(code[start:end], _line_of(code, start))
        p = Parser(toks, unit)
        # A generic item is not lowered here: it is a template, kept as
        # tokens and re-parsed once per distinct set of type arguments. Only
        # the instantiations reach the C output, so an unused generic costs
        # nothing, exactly as in Rust.
        gp = _generic_params_of(toks, kind)
        if gp is not None:
            params, name = gp
            if name in unit.core_names:
                # The unit defines this name itself; the seeded core template
                # and its impls step aside entirely.
                unit.core_names.discard(name)
                unit.generic_impls[:] = [g for g in unit.generic_impls
                                         if g[1] != name]
            if kind == "struct":
                unit.generic_structs[name] = (params, toks)
            elif kind == "fn":
                unit.generic_fns[name] = (params, toks)
            elif kind == "impl":
                unit.generic_impls.append((params, name, toks))
            continue
        try:
            if kind in ("struct", "tuple_struct", "unit_struct"):
                if kind == "struct":
                    name, fields = p.parse_struct()
                elif kind == "unit_struct":
                    name, fields = p.parse_unit_struct()
                    unit.unit_structs.add(name)
                else:
                    name, fields = p.parse_tuple_struct()
                    unit.tuple_structs.add(name)
                unit.structs[name] = fields
                struct_order.append(name)
                local["structs"].append(name)
                if p.item_derives:
                    unit.derives[name] = p.item_derives
            elif kind == "enum":
                name, variants, payloads = p.parse_enum()
                if p.item_derives:
                    unit.derives[name] = p.item_derives
                unit.enums[name] = variants
                if payloads:
                    unit.data_enums[name] = payloads
                for vname, _ in variants:
                    unit.variants["%s_%s" % (name, vname)] = name
                local["enums"].append(name)
            elif kind == "macro":
                mname, rules = p.parse_macro_rules()
                unit.macros[mname] = rules
            elif kind == "trait":
                tname, supers, methods, defaults, cdefaults = p.parse_trait()
                unit.traits[tname] = methods
                unit.trait_defaults[tname] = defaults
                unit.const_defaults[tname] = cdefaults
                unit.supertraits[tname] = supers
            elif kind == "type":
                name, ty = p.parse_type_alias()
                if name is not None:
                    unit.type_aliases[name] = ty
                    local["type_aliases"].append(name)
            elif kind == "const":
                kw, name, ty, init = p.parse_const_signature()
                unit.consts[name] = ty
                unit.const_inits[name] = (kw, init.code)
                if kw == "const":
                    unit.const_values[name] = init.code
                local["consts"].append(name)
            elif kind == "fn":
                _, name, params, ret = p.parse_fn_signature()
                if name == "main" and ret.is_void():
                    ret = INT
                unit.fn_sigs[name] = (ret, [t for _, t in params])
            else:                                   # impl
                owner = p.parse_impl_header()
                if p.impl_trait is not None:
                    unit.trait_impls.append((p.impl_trait, owner, toks))
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
        _merge_unit(unit, sub_unit)
        struct_order.extend(sub_order)
        collect_items(text, find_rust_items(text), unit, struct_order)
    return unit, struct_order


def _merge_unit(dst, src):
    """Copy type and signature tables from `src` into `dst` (src wins on clash)."""
    dst.fn_sigs.update(src.fn_sigs)
    dst.structs.update(src.structs)
    dst.methods.update(src.methods)
    dst.enums.update(src.enums)
    dst.variants.update(src.variants)
    dst.consts.update(src.consts)
    dst.const_values.update(src.const_values)
    dst.const_inits.update(src.const_inits)
    dst.type_aliases.update(src.type_aliases)
    dst.opaque_structs |= src.opaque_structs
    dst.opaque_complete |= src.opaque_complete
    dst.extern_fns.update(src.extern_fns)
    dst.generic_structs.update(src.generic_structs)
    dst.generic_fns.update(src.generic_fns)
    dst.traits.update(src.traits)
    dst.trait_defaults.update(src.trait_defaults)
    dst.supertraits.update(src.supertraits)
    dst.tuple_structs |= src.tuple_structs
    dst.unit_structs |= src.unit_structs
    dst.generic_impls.extend(src.generic_impls)
    dst.trait_impls.extend(src.trait_impls)
    dst.macros.update(src.macros)


def collect_mod_items(names, path, unit, struct_order, _seen=None,
                      depth=0):
    """Seed `unit` from sibling `mod name;` files; return seeded type names.

    Resolves each name to `name.rs` or `name/mod.rs`, recursively follows
    their bare `mod` declarations, and runs `collect_items` so the current
    file can see their structs, enums, consts and method signatures. No
    function bodies are translated -- the sibling TU owns those.
    """
    seen = _seen if _seen is not None else set()
    seeded = {"structs": [], "enums": [], "consts": [], "type_aliases": []}
    for name in names:
        filename = (resolve_use_path(name, path) if "::" in name
                    else resolve_mod_path(name, path))
        if not filename or filename in seen:
            continue
        seen.add(filename)
        try:
            with open(filename, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        # Seeding is best effort throughout. A sibling Crust cannot parse
        # costs us the types it defines, but it must never fail the file that
        # merely mentioned it -- otherwise adding a `use` would be strictly
        # worse than leaving the type undeclared, which is the opposite of
        # the point.
        try:
            # `mod` declarations are followed to any depth: they describe this
            # crate's own tree. `use` paths are followed only from the file
            # being compiled, since they reach across a whole workspace and
            # the types worth having are in the file named directly.
            nested = find_mod_decls(text)
            if depth < 1:
                nested = nested + find_use_paths(text)
            sub = collect_mod_items(nested, filename, unit, struct_order,
                                    seen, depth + 1)
            for key in seeded:
                seeded[key].extend(sub[key])
            local = collect_items(
                text, find_rust_items(text, rust_file=True), unit,
                struct_order)
        except (CrustError, RecursionError):
            continue
        seeded["structs"].extend(local["structs"])
        seeded["enums"].extend(local["enums"])
        seeded["consts"].extend(local["consts"])
        seeded["type_aliases"].extend(local["type_aliases"])
    return seeded


# Traits Crust can derive, and what each one generates. `Copy` and `Eq` are
# markers in Rust -- they add no method of their own -- so they are accepted
# and produce nothing rather than being reported as unsupported.
_DERIVABLE = {"Clone", "Copy", "PartialEq", "Eq", "Default", "Debug"}


# C spellings a derived method can safely compare with `==` or hand to
# printf. The test is deliberately a whitelist: a struct field, an array, or a
# type Crust has not seen cannot be handled, and at the time derives are
# generated some of those types are not in `unit.structs` yet -- opaque stubs
# for unknown types are created later, during translation. A negative test
# ("not a struct") therefore lets exactly the wrong cases through.
_SCALAR_BASES = set(PRIMITIVES.values()) | {
    "int", "unsigned int", "long", "unsigned long", "short",
    "unsigned short", "char", "signed char", "unsigned char", "float",
    "double", "long long", "unsigned long long", "_Bool", "long double",
}


def _is_scalar(ty):
    """True if `ty` is something C can compare and print directly."""
    if ty is None or ty.array:
        return False
    if ty.ptr:
        return True
    return ty.base in _SCALAR_BASES


def emit_derives(unit, local_names):
    """Generate the methods named by `#[derive(..)]` on each type.

    These are written exactly as the equivalent hand-rolled `impl` would be --
    ordinary `Type_method` functions -- so they dispatch statically and cost
    nothing extra. An `impl` that defines the same method wins: a hand-written
    `clone` is never overwritten by a derived one.

    Only structs are derived for. A data-carrying enum would need per-variant
    comparison and copying through its union, which is a different piece of
    work; deriving on one is accepted (so the attribute does not fail the
    file) but generates nothing, and the missing method is reported at the
    call site rather than silently doing the wrong thing.
    """
    for name, traits in sorted(unit.derives.items()):
        if name not in local_names:
            # The type was seeded from a sibling module, so this translation
            # unit has its declaration but not its definition. Generating a
            # method here would emit a body for a type the C front end only
            # knows as incomplete -- and the sibling's own TU derives it
            # anyway, so nothing is lost by leaving it alone.
            continue
        fields = unit.structs.get(name)
        if fields is None:
            continue
        for trait in traits:
            if trait not in _DERIVABLE:
                continue
            gen = _DERIVE_IMPL.get(trait)
            if gen is None:
                continue                      # marker trait: nothing to emit
            mname = gen[0]
            if (name, mname) in unit.methods:
                continue                      # a hand-written impl wins
            gen[1](unit, name, fields)


def _derive_clone(unit, name, fields):
    """`fn clone(&self) -> T` -- a value copy, which is what C assignment is."""
    _register(unit, name, "clone", CType(name), [], self_kind="ref")
    unit.emitted.append("%s %s_clone(%s *self) { return *self; }"
                        % (name, name, name))


def _derive_default(unit, name, fields):
    """`fn default() -> T` -- every field zeroed."""
    _register(unit, name, "default", CType(name), [], self_kind=None)
    unit.emitted.append("%s %s_default(void) { %s v = {0}; return v; }"
                        % (name, name, name))


def _derive_eq(unit, name, fields):
    """`fn eq(&self, other: &T) -> bool` -- field by field, in order."""
    tests = []
    for fname, fty in fields:
        if not _is_scalar(fty):
            # An array or a nested struct cannot be compared with `==`, and
            # memcmp would compare padding too. Skip deriving entirely rather
            # than emit a comparison that is quietly wrong.
            return
        tests.append("self->%s == other->%s" % (fname, fname))
    _register(unit, name, "eq", CType("_Bool"),
              [("other", CType(name, 1))], self_kind="ref")
    unit.emitted.append("_Bool %s_eq(%s *self, %s *other) { return %s; }"
                        % (name, name, name, " && ".join(tests) or "1"))


def _derive_debug(unit, name, fields):
    """`fn debug(&self)` -- print the value, the way `{:?}` would.

    Rust's `Debug` writes into a formatter; Crust has no `String`, so the
    derived method prints directly. The name is `debug` rather than `fmt`
    because it takes no formatter and would not satisfy a real `fmt::Debug`
    bound -- calling it something else keeps that difference visible.
    """
    _register(unit, name, "debug", VOID, [], self_kind="ref")
    unit.needs.add("printf")
    parts, args = [], []
    for fname, fty in fields:
        if not _is_scalar(fty):
            parts.append("%s: ..." % fname)
            continue
        parts.append("%s: %s" % (fname, _c_spec_for(fty)))
        args.append("self->%s" % fname)
    body = 'printf("%s { %s }", %s);' % (
        name, ", ".join(parts).replace('"', ""),
        ", ".join(args)) if args else 'printf("%s { %s }");' % (
            name, ", ".join(parts))
    unit.emitted.append("void %s_debug(%s *self) { %s }"
                        % (name, name, body))


def _register(unit, owner, mname, ret, params, self_kind):
    """Register a generated method so call sites resolve it."""
    info = MethodInfo(owner, mname, ret, self_kind or "none", params)
    unit.methods[(owner, mname)] = info
    selfp = [CType(owner, 1)] if self_kind == "ref" else (
        [CType(owner)] if self_kind == "value" else [])
    unit.fn_sigs[info.mangled] = (ret, selfp + [t for _, t in params])


_DERIVE_IMPL = {
    "Clone": ("clone", _derive_clone),
    "Default": ("default", _derive_default),
    "PartialEq": ("eq", _derive_eq),
    "Debug": ("debug", _derive_debug),
}


def emit_trait_defaults(unit):
    """Give every trait impl the default methods it did not override.

    A default body is written against `Self`, so generating one for a type is
    the same substitution a generic instantiation does -- re-parse the stored
    tokens with `Self` bound to the implementing type. Because dispatch is
    static, the result is an ordinary `Type_method` function, identical in
    kind to one the impl had written out by hand.
    """
    for trait, owner, _toks in unit.trait_impls:
        # Associated consts the impl did not define, taken from the trait's
        # default. The default may name `Self::OTHER`, which resolves to the
        # implementing type -- so the emission order below follows the order
        # they were declared in, and a default referring to another default
        # works as long as the other came first, which is how Rust reads too.
        seen_c = set()
        for tr in _trait_chain(unit, trait):
            for cname, ctoks in unit.const_defaults.get(tr, {}).items():
                flat = "%s_%s" % (owner, cname)
                if cname in seen_c or flat in unit.consts:
                    continue
                seen_c.add(cname)
                p = Parser(list(ctoks) + [Token("eof", "", 0)], unit)
                p.impl_type = owner
                try:
                    _kw, _n, cty, cinit = p.parse_const_signature()
                except CrustError:
                    continue
                unit.consts[flat] = cty
                # Into the prelude, not the appended block: a `static const`
                # has to be declared before the code that reads it, and the
                # appended block sits after every function body.
                # `#define`, not `static const`: a default may be derived
                # from another constant (`PAGE_SIZE = 1 << Self::PAGE_SHIFT`),
                # and in C one `static const` is not a constant expression
                # usable in the initialiser of another. The prelude writer
                # already lifts `#define` lines out onto their own lines.
                unit.inherited_consts.append(
                    "#define %s (%s)" % (flat, cinit.code))
        seen = set()
        for tr in _trait_chain(unit, trait):
            for mname, dtoks in unit.trait_defaults.get(tr, {}).items():
                if mname in seen or (owner, mname) in unit.methods:
                    continue
                seen.add(mname)
                p = Parser(list(dtoks), unit)
                p.impl_type = owner
                try:
                    start, info = p.parse_method_signature(owner)
                except CrustError:
                    continue
                params = list(info.params)
                if info.self_kind == "ref":
                    params.insert(0, ("self", CType(owner, 1)))
                elif info.self_kind == "value":
                    params.insert(0, ("self", CType(owner)))
                unit.methods[(owner, info.name)] = info
                unit.fn_sigs[info.mangled] = (
                    info.ret, [t for _, t in params])
                out = Out(1)
                p.emit_fn_body(out, start, info.mangled, params, info.ret,
                               False)
                unit.emitted.append(out.text())


def _trait_chain(unit, trait, seen=None):
    """`trait` and its supertraits, nearest first, without cycling."""
    seen = seen if seen is not None else set()
    if trait in seen:
        return []
    seen.add(trait)
    chain = [trait]
    for sup in unit.supertraits.get(trait, ()):
        chain.extend(_trait_chain(unit, sup, seen))
    return chain


def _is_generic_span(code, start, end, kind):
    """True if the item at this span declares type parameters."""
    if kind not in ("struct", "fn", "impl"):
        return False
    try:
        toks = tokenize(code[start:end], _line_of(code, start))
    except CrustError:
        return False
    return _generic_params_of(toks, kind) is not None


_CORE_CACHE = {}


def _load_core_unit():
    """Parse crust_core/core.rs once into a Unit (generics + concrete items)."""
    if "full" in _CORE_CACHE:
        return _CORE_CACHE["full"]
    path = os.path.join(os.path.dirname(__file__), "crust_core", "core.rs")
    unit = Unit()
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:                                    # pragma: no cover
        _CORE_CACHE["full"] = unit
        return unit
    spans = find_rust_items(text, rust_file=True)
    collect_items(text, spans, unit, [])
    # Emit bodies for concrete (non-generic) impls so callers can link them
    # into a using unit. Generic impls stay as templates.
    for start, end, kind in spans:
        if kind != "impl" or _is_generic_span(text, start, end, kind):
            continue
        line0 = _line_of(text, start)
        parser = Parser(tokenize(text[start:end], line0), unit)
        out = Out(line0)
        try:
            parser.parse_impl(out)
        except CrustError:                              # pragma: no cover
            continue
        unit.emitted.append(out.text())
    _CORE_CACHE["full"] = unit
    return unit


def core_templates():
    """Parse the bundled minimal core once; return its generic templates.

    Returned as (generic_structs, generic_impls). Only templates are taken --
    nothing from core reaches the output unless something instantiates it, so
    a unit that never mentions `Vec<T>` pays only this one-time parse (and
    that is cached across the whole process).
    """
    unit = _load_core_unit()
    return (unit.generic_structs, unit.generic_impls)


def ensure_core_concrete(unit, name):
    """Pull a non-generic core type (`AtomicU32`, `Ordering`, …) into `unit`.

    Demand-driven: only types that are actually named get their struct/enum,
    methods and method bodies. A local definition of the same name wins.
    """
    if name in unit.structs or name in unit.enums \
            or name in unit.generic_structs:
        return
    # Atomics depend on Ordering for their method signatures.
    if name in ("AtomicU32", "AtomicUsize"):
        ensure_core_concrete(unit, "Ordering")
    core = _load_core_unit()
    if name in core.generic_structs:
        return
    if name in core.enums and name not in unit.enums:
        unit.enums[name] = core.enums[name]
        for vname, _ in core.enums[name]:
            unit.variants["%s_%s" % (name, vname)] = name
        unit.core_concrete.add(name)
    if name in core.structs and name not in unit.structs:
        unit.structs[name] = core.structs[name]
        unit.core_concrete.add(name)
        if name in core.tuple_structs:
            unit.tuple_structs.add(name)
        if name in core.unit_structs:
            unit.unit_structs.add(name)
    if name not in unit.core_concrete:
        return
    for (owner, mname), info in core.methods.items():
        if owner != name or (owner, mname) in unit.methods:
            continue
        unit.methods[(owner, mname)] = info
        if info.mangled in core.fn_sigs:
            unit.fn_sigs[info.mangled] = core.fn_sigs[info.mangled]
    # Copy method bodies for this type from the core's emitted list.
    prefix = name + "_"
    for text in core.emitted:
        if prefix in text and text not in unit.emitted:
            unit.emitted.append(text)


def seed_core(unit):
    """Make the core templates available to `unit` without overriding it.

    A definition already in the unit -- the user's own `Vec`, or one from an
    included `.rs` -- always wins, so seeding can never change the meaning of
    code that did not need core in the first place.
    """
    if not hasattr(unit, "core_concrete"):
        unit.core_concrete = set()
    gstructs, gimpls = core_templates()
    for name, tmpl in gstructs.items():
        if name not in unit.generic_structs and name not in unit.structs:
            unit.generic_structs[name] = tmpl
            unit.core_names.add(name)
    for params, owner, toks in gimpls:
        if owner in unit.core_names:
            unit.generic_impls.append((params, owner, toks))


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


def render_data_enum(name, variants, payloads):
    """Lower a data-carrying enum to a tagged union.

    The shape is the one a C programmer would write by hand:

        enum Shape_tag { Shape_Circle, Shape_Rect };
        struct Shape_Circle_data { double _0; };
        struct Shape_Rect_data { double w; double h; };
        typedef struct Shape {
            enum Shape_tag tag;
            union { struct Shape_Circle_data Circle; ... } u;
        } Shape;

    Payload structs are declared separately rather than inline, because a
    named type is easier to read in a diagnostic and avoids relying on
    anonymous-struct support in the C front end. A variant with no payload
    contributes no union member and costs nothing.
    """
    parts = ["enum %s_tag { %s };"
             % (name, ", ".join("%s_%s" % (name, v) for v, _ in variants))]
    members = []
    for vname, _ in variants:
        fields = payloads.get(vname)
        if not fields:
            continue
        decls = " ".join(
            ty.decl(fname or "_%d" % i) + ";"
            for i, (fname, ty) in enumerate(fields))
        parts.append("struct %s_%s_data { %s };" % (name, vname, decls))
        members.append("struct %s_%s_data %s;" % (name, vname, vname))
    parts.append("struct %s; typedef struct %s %s;" % (name, name, name))
    parts.append("struct %s { enum %s_tag tag; union { %s } u; };"
                 % (name, name, " ".join(members) or "char _empty;"))
    return " ".join(parts)


def _render_enum(name, variants):
    """Render a Rust enum as a C enum whose members are `Enum_Variant`."""
    parts = []
    for vname, value in variants:
        member = "%s_%s" % (name, vname)
        parts.append(member if value is None else "%s = %s" % (member, value))
    return "enum %s { %s }; typedef enum %s %s;" % (
        name, ", ".join(parts), name, name)


def _format_int_literal(value, ty):
    """Format an integer for a `#define` / enum, with a C suffix from `ty`."""
    bits = _int_bits(ty)
    if "unsigned" in ty.base and bits:
        value = value & ((1 << bits) - 1)
    if value < 0:
        body = str(value)
    else:
        body = "0x%X" % value if value >= 0x10000 else str(value)
    if "unsigned" in ty.base:
        body += "u"
    if ty.base.endswith("long"):
        body += "l"
    return body


def _int_bits(ty):
    """Width in bits for an integer CType, or 0 if unknown."""
    if ty.ptr or ty.array:
        return 0
    b = ty.base
    if "char" in b:
        return 8
    if "short" in b:
        return 16
    if b.endswith("long"):
        return 64
    if "int" in b or b in _RANK:
        return 32
    return 0


def _struct_new_to_compound(init, ty):
    """Rewrite `Type_new(args)` to a brace initializer when `ty` is that struct.

    Static storage cannot call a function; `AtomicU32::new(x)` must become
    `{ x }` so it is a constant aggregate initializer.
    """
    if ty is None or ty.ptr or ty.array:
        return None
    m = re.match(r"^(%s)_new\((.*)\)$" % re.escape(ty.base), init.strip())
    if not m:
        return None
    return "{ %s }" % m.group(2)


def _render_const(kw, name, ty, init):
    """Render one `const`/`static` as a C file-scope declaration.

    Integer `const`s that fit in a C `int` become enum constants (usable in
    constant expressions). Larger integer consts become `#define`s for the
    same reason -- `static const` is not a constant expression in C, so a
    later `const B: usize = A + 4096` would otherwise fail at file scope.
    """
    if kw == "const":
        folded = _fold_int_const(init, {})
        if folded is not None and not ty.ptr and not ty.array \
                and ty.base in _RANK:
            text = _format_int_literal(folded, ty)
            # Re-fold through format's unsigned mask for the fit check.
            bits = _int_bits(ty)
            check = folded
            if "unsigned" in ty.base and bits:
                check = folded & ((1 << bits) - 1)
            if -(2 ** 31) <= check < 2 ** 31 and "unsigned" not in ty.base:
                return "enum { %s = %s };" % (name, text)
            if -(2 ** 31) <= check < 2 ** 31 and check <= 0x7FFFFFFF:
                return "enum { %s = %s };" % (name, text)
            return "#define %s %s" % (name, text)
        if _fits_c_enum(ty, init):
            return "enum { %s = %s };" % (name, init)
    compound = _struct_new_to_compound(init, ty)
    if compound is not None:
        init = compound
    prefix = "static const " if kw == "const" else "static "
    return "%s%s = %s;" % (prefix, ty.decl(name), init)


def _fold_int_const(init, env):
    """Evaluate a const initializer as an int, or return None.

    Handles integer literals (with Rust `_` separators), parenthesised forms,
    unary `+/-/~`, and binary `+ - *` / `| & ^` / `<< >>` over those. Names
    are resolved through `env` (const name -> int).
    """
    s = init.strip()
    if not s:
        return None
    # Replace known names first -- they may contain underscores (`FOO_BAR`).
    if env:
        for name in sorted(env, key=len, reverse=True):
            if name in s:
                s = re.sub(r"\b%s\b" % re.escape(name), str(env[name]), s)
    # Strip Rust digit separators from numeric literals only.
    s = re.sub(r"(?<=[\da-fA-FxX])_(?=[\da-fA-F])", "", s)
    try:
        import ast
        tree = ast.parse(s, mode="eval")
    except SyntaxError:
        return None
    try:
        return _eval_int_ast(tree.body)
    except Exception:
        return None


def _eval_int_ast(node):
    import ast
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    # Python < 3.8 spelled an integer literal `ast.Num`. Merely *naming* the
    # attribute warns on 3.12+, so the legacy branch is guarded by version
    # rather than by hasattr.
    if sys.version_info < (3, 8):                                   # pragma: no cover
        if isinstance(node, ast.Num):
            return int(node.n)
    if isinstance(node, ast.UnaryOp):
        v = _eval_int_ast(node.operand)
        if isinstance(node.op, ast.UAdd):
            return v
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.Invert):
            return ~v
    if isinstance(node, ast.BinOp):
        a, b = _eval_int_ast(node.left), _eval_int_ast(node.right)
        ops = {
            ast.Add: lambda x, y: x + y,
            ast.Sub: lambda x, y: x - y,
            ast.Mult: lambda x, y: x * y,
            ast.BitOr: lambda x, y: x | y,
            ast.BitAnd: lambda x, y: x & y,
            ast.BitXor: lambda x, y: x ^ y,
            ast.LShift: lambda x, y: x << y,
            ast.RShift: lambda x, y: x >> y,
        }
        fn = ops.get(type(node.op))
        if fn is not None:
            return fn(a, b)
    raise ValueError("not an int const")


def _emit_prelude(body, prelude):
    """Insert prelude at a safe offset.

    Ordinary items share one physical line so source line numbers do not
    move. `#define`s need their own lines (the directive runs to newline),
    so they are placed just before that shared line.
    """
    if not prelude:
        return body
    at = _prelude_offset(body)
    defines = [p for p in prelude if p.startswith("#define ")]
    rest = [p for p in prelude if not p.startswith("#define ")]
    chunk = "".join(d + "\n" for d in defines)
    if rest:
        chunk += " ".join(rest) + " "
    return body[:at] + chunk + body[at:]


def _render_consts(code, spans, unit):
    """Render `const`/`static` items, hoisted so order does not matter.

    A Rust `const` is a compile-time constant, so an integer one is emitted as
    a C enum constant rather than `static const`: only the former is a
    constant expression, and so only the former can size an array. Values
    that do not fit in a C `int` become `#define`s for the same reason.
    Anything else -- floats, pointers, non-literal initializers, `static` --
    becomes an ordinary file-scope object.
    """
    # First pass: fold integer values so later consts can name earlier ones.
    env = {}
    items = []
    for start, end, kind in spans:
        if kind != "const":
            continue
        p = Parser(tokenize(code[start:end], _line_of(code, start)), unit)
        kw, name, ty, init = p.parse_const_signature()
        items.append((kw, name, ty, init.code))
        if kw == "const":
            val = _fold_int_const(init.code, env)
            if val is not None:
                env[name] = val
    out = []
    for kw, name, ty, init in items:
        # Re-fold with the full env so `B = A + 1` sees A's value.
        if kw == "const":
            val = _fold_int_const(init, env)
            if val is not None and not ty.ptr and not ty.array \
                    and ty.base in _RANK:
                text = _format_int_literal(val, ty)
                bits = _int_bits(ty)
                check = val
                if "unsigned" in ty.base and bits:
                    check = val & ((1 << bits) - 1)
                if -(2 ** 31) <= check < 2 ** 31:
                    out.append("enum { %s = %s };" % (name, text))
                else:
                    out.append("#define %s %s" % (name, text))
                continue
        out.append(_render_const(kw, name, ty, init))
    return out


def _fits_c_enum(ty, init):
    """True if a const can be emitted as a C enum constant.

    C enum members have type `int`, so only integer constants that fit are
    eligible.
    """
    if ty.ptr or ty.array or ty.base not in _RANK:
        return False
    folded = _fold_int_const(init, {})
    if folded is None:
        return False
    return -(2 ** 31) <= folded < 2 ** 31


def _render_struct(name, fields):
    body = " ".join("%s;" % ty.decl(f) for f, ty in fields)
    return "struct %s { %s };" % (name, body)


def _match_delim(text, i):
    """Index of the delimiter closing the one at `i`, or None."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    close = pairs.get(text[i])
    if close is None:
        return None
    depth = 0
    while i < len(text):
        c = text[i]
        if c in pairs:
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def erase_module_items(code):
    """Blank `use`, `extern crate` and `mod X;` items, keeping line numbers.

    Scanned rather than matched with a single regular expression, because a
    `use` may nest brace groups arbitrarily -- `use core::{cell::Cell,
    ops::{Deref, DerefMut}};` -- and a regex cannot balance them. The item
    runs to the `;` that closes it at brace depth zero.

    Blanking rather than deleting keeps every later line where the user wrote
    it, so diagnostics still point at the right place. Comments and string
    literals are blanked first so a `use` mentioned in prose is not erased
    from live code.
    """
    scan = _blank(code)
    out = list(code)
    depths = _depths(scan)
    for m in _ITEM_MACRO.finditer(scan):
        if depths[m.start()] != 0:
            continue            # inside a body: an ordinary macro call
        open_idx = m.end() - 1
        close = _match_delim(scan, open_idx)
        if close is None:
            continue
        end = close + 1
        while end < len(scan) and scan[end] in " \t":
            end += 1
        if end < len(scan) and scan[end] == ";":
            end += 1
        for k in range(m.start(), end):
            if out[k] != "\n":
                out[k] = " "
    scan = "".join(out)
    for m in _ERASED_HEAD.finditer(scan):
        i, depth, n = m.start(), 0, len(scan)
        while i < n:
            ch = scan[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif ch == ";" and depth == 0:
                i += 1
                break
            i += 1
        for k in range(m.start(), min(i, n)):
            if out[k] != "\n":
                out[k] = " "
    return "".join(out)


def translate(code, path=None):
    """Lower every top-level Rust item in `code` to C.

    C text outside those items is passed through byte-for-byte, and line
    numbers are preserved throughout, so diagnostics from later compiler
    passes still point at the original source lines.
    """
    # Capture bare `mod name;` before erase blanks them — those names are
    # how we find sibling files for cross-file type knowledge.
    mod_names = find_mod_decls(code) + find_use_paths(code)
    code = erase_module_items(code)
    spans = find_rust_items(
        code, rust_file=bool(path) and path.endswith(".rs"))
    # Drop the arms written for a different target. Without this, every
    # `#[cfg]`-gated alternative is emitted and they collide -- two
    # `#define ULONG_MAX` with different values, say.
    #
    # The text is blanked as well as the span dropped: skipping only the span
    # would leave the Rust source of the rejected arm sitting in the output to
    # be handed to the C front end. Blanking preserves line numbers, so
    # diagnostics still point at the right place.
    dropped = [sp for sp in spans
               if not cfg_allows(leading_attrs(code, sp[0]))]
    if dropped:
        buf = list(code)
        for start, end, _kind in dropped:
            for k in range(start, min(end, len(buf))):
                if buf[k] != "\n":
                    buf[k] = " "
        code = "".join(buf)
        spans = [sp for sp in spans if sp not in dropped]
    included = collect_include_items(code, path)
    if not spans:
        return code

    where = ("%s: " % path) if path else ""
    unit, struct_order = included
    included_fns = set(unit.fn_sigs)
    included_slices = set(unit.slices)
    included_options = set(unit.options)
    included_results = set(unit.results)

    # Sibling mods contribute type tables (and method signatures) only.
    # Their C definitions are not in this TU, so structs/enums/consts are
    # emitted into the prelude below; function *bodies* stay in the sibling.
    mod_seeded = collect_mod_items(mod_names, path, unit, struct_order)

    def fail(exc, start):
        return CrustError("%sline %d: %s" % (where, _line_of(code, start),
                                             exc)
                          if "line " not in str(exc)
                          else "%s%s" % (where, exc))

    # Pass 1: collect structs, function signatures and methods, so that
    # definition order does not matter anywhere in the unit.
    # The bundled core is seeded first, because pass 1 parses signatures and
    # struct fields -- a `Vec<T>` there must already resolve. A local
    # definition of the same name still wins: collect_items overwrites the
    # template and drops the core `impl`s that went with it.
    seed_core(unit)
    m = _RESULT_ALIAS.search(_blank(code))
    if m:
        unit.result_error = CType(m.group("err").split("::")[-1])
    local = collect_items(code, spans, unit, struct_order, fail)
    # Local items win over mod-seeded ones of the same name.
    local_set = (set(local["structs"]) | set(local["enums"])
                 | set(local["consts"]) | set(local["type_aliases"]))
    mod_structs = [n for n in dict.fromkeys(mod_seeded["structs"])
                   if n not in local_set]
    mod_enums = [n for n in dict.fromkeys(mod_seeded["enums"])
                 if n not in local_set]
    mod_consts = [n for n in dict.fromkeys(mod_seeded["consts"])
                  if n not in local_set]
    mod_aliases = [n for n in dict.fromkeys(mod_seeded["type_aliases"])
                   if n not in local_set]

    # Trait impls inherit any default methods they did not override, and
    # `#[derive(..)]` generates its methods. Both run before pass 2 so a body
    # can call a method neither the impl nor the user wrote out.
    emit_trait_defaults(unit)
    emit_derives(unit, set(local["structs"]))

    # Pass 2: translate each item in place. Struct definitions are hoisted
    # into the prelude (see below), so their source region becomes blank.
    pieces, prev = [], 0
    for start, end, kind in spans:
        pieces.append(code[prev:start])
        line0 = _line_of(code, start)
        if kind in ("struct", "tuple_struct", "unit_struct", "enum",
                    "const", "trait", "macro", "type") \
                or _is_generic_span(code, start, end, kind):
            # Hoisted into the prelude, or -- for a generic -- a template
            # whose instantiations are appended after the unit instead.
            text = ""
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
    if "printf" in unit.needs:
        prelude.append("int printf(const char *, ...);")
    if "fprintf" in unit.needs:
        prelude.append("int fprintf(void *, const char *, ...);")
        prelude.append("extern void *stderr;")
    if "memcpy" in unit.needs:
        prelude.append("void *memcpy(void *, const void *, unsigned long);")
    if "alloc" in unit.needs:
        prelude.append("void *malloc(unsigned long);")
        prelude.append("void *realloc(void *, unsigned long);")
        prelude.append("void free(void *);")
    for name in mod_enums + local["enums"]:
        if name in unit.data_enums:
            prelude.append(render_data_enum(name, unit.enums[name],
                                            unit.data_enums[name]))
        else:
            prelude.append(_render_enum(name, unit.enums[name]))
    for name in sorted(unit.core_concrete):
        if name in unit.enums and name not in local_set:
            prelude.append(_render_enum(name, unit.enums[name]))
    # Incomplete types for qualified paths the unit never defined
    # (`crate::percpu::PercpuBlock` -> crate_percpu_PercpuBlock).
    for name in sorted(unit.opaque_structs):
        if name not in unit.structs and name not in local_set:
            if name in unit.opaque_complete:
                prelude.append(
                    "struct %s { char _crust_opaque; }; "
                    "typedef struct %s %s;" % (name, name, name))
            else:
                prelude.append("struct %s; typedef struct %s %s;"
                               % (name, name, name))
    core_structs = [n for n in sorted(unit.core_concrete)
                    if n in unit.structs and n not in local_set]
    for name in core_structs + mod_structs + local["structs"]:
        prelude.append("struct %s; typedef struct %s %s;"
                       % (name, name, name))
    # Slice structs hold only a pointer to their element, so the forward
    # declarations above are enough even for a slice of a user struct.
    for name in unit.results:
        if name not in included_results:
            prelude.append("struct %s; typedef struct %s %s; %s"
                           % (name, name, name,
                              _render_struct(name, unit.structs[name])))
    for name in unit.options:
        if name not in included_options:
            prelude.append("struct %s; typedef struct %s %s; %s"
                           % (name, name, name,
                              _render_struct(name, unit.structs[name])))
    for name, (fret, fparams) in unit.fn_ptrs.items():
        prelude.append("typedef %s (*%s)(%s);"
                       % (fret.decl(), name,
                          ", ".join(t.decl() for t in fparams) or "void"))
    for name in unit.tuples:
        prelude.append("struct %s; typedef struct %s %s; %s"
                       % (name, name, name,
                          _render_struct(name, unit.structs[name])))
    for name in unit.slices:
        if name not in included_slices:
            prelude.append("struct %s; typedef struct %s %s; %s"
                           % (name, name, name,
                              _render_struct(name, unit.structs[name])))
    for name in unit.struct_order:
        if name in unit.structs:
            prelude.append("struct %s; typedef struct %s %s;"
                           % (name, name, name))
    for name in _toposort_structs(
            unit, core_structs + mod_structs + local["structs"]
            + unit.struct_order):
        prelude.append(_render_struct(name, unit.structs[name]))
    # Aliases after structs so `type Handle = Foo` can name a local struct.
    for name in mod_aliases + local["type_aliases"]:
        prelude.append("typedef %s;" % unit.type_aliases[name].decl(name))
    for name in sorted(unit.unwraps):
        if name in unit.options:
            if name in included_options:
                continue
            elem = unit.options[name]
            prelude.append(
                "static %s %s_unwrap(%s o) { if (!o.some) abort(); "
                "return o.value; }" % (elem.decl(), name, name))
        else:
            if name in included_results:
                continue
            ok, err = unit.results[name]
            if not ok.is_void():
                prelude.append(
                    "static %s %s_unwrap(%s r) { if (!r.ok) abort(); "
                    "return r.value; }" % (ok.decl(), name, name))
            prelude.append(
                "static %s %s_unwrap_err(%s r) { if (r.ok) abort(); "
                "return r.error; }" % (err.decl(), name, name))
    for name in mod_consts:
        if name in unit.const_inits:
            kw, init = unit.const_inits[name]
            prelude.append(_render_const(kw, name, unit.consts[name], init))
    for text in _render_consts(code, spans, unit):
        prelude.append(text)
    prelude.extend(local["impl_consts"])
    prelude.extend(unit.inherited_consts)
    for name, info in sorted(unit.extern_fns.items()):
        if name in unit.fn_sigs or name in included_fns:
            continue
        ret_ty, atypes = info if isinstance(info, tuple) else (None, info)
        params = []
        for t in atypes:
            if t is None or t.is_void():
                params.append("void *")
            else:
                params.append(t.decl())
        ret = "void"
        if ret_ty is not None and not ret_ty.is_void():
            ret = ret_ty.decl()
        prelude.append("extern %s %s(%s);"
                       % (ret, name, ", ".join(params) or "void"))
    for name, (ret, ps) in unit.fn_sigs.items():
        if name == "main" or name in included_fns:
            continue
        prelude.append("%s%s(%s);"
                       % ("static " if name in unit.statics else "",
                          ret.decl(name),
                          ", ".join(t.decl() for t in ps) if ps else "void"))
    if unit.emitted:
        # Monomorphised bodies go after all original text, so no line number
        # in the user's own source moves. Their prototypes are in the prelude,
        # so definition order does not matter.
        body = body.rstrip("\n") + "\n\n" + "\n".join(unit.emitted) + "\n"
    if not prelude:
        return body
    return _emit_prelude(body, prelude)


def translate_file(path):
    with open(path, encoding="utf-8") as f:
        return translate(f.read())
