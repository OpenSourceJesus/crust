"""Classes for representing tokens.

A TokenKind instance represents one of the kinds of tokens recognized (see
token_kinds.py). A Token instance represents a token as produced by the lexer.

"""


class TokenKind:
    """Class representing the various known kinds of tokens.

    Ex: +, -, ), return, int

    There are also token kind instances for each of 'identifier' and
    'number'. See token_kinds.py for a list of token_kinds defined.

    text_repr (str) - The token's representation in text, if it has a fixed
    representation.

    """

    def __init__(self, text_repr="", kinds: "list[TokenKind]" = []):
        """Initialize a new TokenKind and add it to `kinds`.

        kinds (List[TokenKind]) - List of kinds to which this TokenKind is
        added. This is convenient when defining token kinds in token_kind.py.

        """
        self.text_repr = text_repr
        kinds.append(self)
        kinds.sort(key=lambda kind: -len(kind.text_repr))

    def __str__(self):
        """Return the representation of this token kind."""
        return self.text_repr

    def __reduce__(self):
        """Pickle a TokenKind as a reference to its module-level singleton.

        Token kinds are singletons compared by identity throughout the
        compiler, so a naive pickle (which would create a fresh copy) would
        break those comparisons when an AST is loaded from the cache. We
        instead pickle by a stable name and look the singleton back up.
        """
        _build_kind_registry()
        return (_kind_by_name, (self._regname,))


# Registry mapping a stable name ("<module>:<attr>") to each TokenKind
# singleton, used to round-trip token kinds through pickle (see __reduce__).
_NAME_TO_KIND = None


def _build_kind_registry():
    global _NAME_TO_KIND
    if _NAME_TO_KIND is not None:
        return
    import importlib
    _NAME_TO_KIND = {}
    for modname in ("shivyc.token_kinds", "shivyc.tokens"):
        try:
            mod = importlib.import_module(modname)
        except ImportError:
            continue
        for attr, val in vars(mod).items():
            if isinstance(val, TokenKind) and not hasattr(val, "_regname"):
                val._regname = modname + ":" + attr
                _NAME_TO_KIND[val._regname] = val


def _kind_by_name(name):
    _build_kind_registry()
    return _NAME_TO_KIND[name]


from shivyc.errors import Range  # noqa: F401  (Token.r type)


class Token:
    """Single unit element of the input as produced by the tokenizer.

    kind (TokenKind) - Kind of this token.

    content - Additional content about some tokens. For number tokens,
    this stores the number itself. For identifiers, this stores the identifier
    name. For string, stores a list of its characters.
    rep (str) - The string representation of this token. If not provided, the
    content parameter is used.
    r (Range) - Range of positions that this token covers.

    """

    r: "object"      # token source range (errors.Range); obj-typed so its
                     # representation matches every other class's `r` field
                     # (all 'o'), letting a mis-resolved x.r cast stay sound
    # `content` is polymorphic: a string for identifier/number tokens, but a
    # list of character codes for string-literal tokens. Pin it to obj so the
    # list is not coerced to a char* (which mangled string-literal data).
    content: "object"

    def __init__(self, kind, content: "object" = "", rep="", r=None):
        """Initialize this token."""
        self.kind = kind

        self.content = content if content else str(self.kind)
        self.rep = rep
        self.r = r
        # True for wide (L-prefixed) string/char literals.
        self.wide = False
        # Source logical line; set by the lexer, read by the preprocessor.
        self.logical_line = None
        # On a `struct`/`union` keyword: the maximum member alignment
        # `#pragma pack` / `__attribute__((packed))` asked for, 0 for
        # natural. Set by `shivyc.pack`, read by the parser.
        self.pack = 0

    def __repr__(self):  # pragma: no cover
        return self.content

    def __str__(self):
        """Return the token content."""
        return self.rep if self.rep else self.content


def parse_c_int(s):
    """Parse a C integer-constant spelling into a Python int.

    Handles hexadecimal (0x), binary (0b), octal (leading 0), and decimal,
    with optional u/U and l/L suffixes (in any order). Also accepts a simple
    character constant like 'A'.
    """
    s = s.strip()
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        inner = s[1:-1]
        if inner.startswith("\\"):
            esc = {"n": 10, "t": 9, "r": 13, "0": 0, "\\": 92,
                   "'": 39, '"': 34}
            return esc.get(inner[1:], ord(inner[-1]) if inner[1:] else 0)
        return ord(inner) if inner else 0

    # Strip integer suffixes.
    core = s.rstrip("uUlL")
    if core[:2] in ("0x", "0X"):
        return int(core, 16)
    if core[:2] in ("0b", "0B"):
        return int(core, 2)
    if len(core) > 1 and core[0] == "0":
        return int(core, 8)
    return int(core or "0", 10)
