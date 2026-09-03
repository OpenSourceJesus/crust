"""`auto` for the C++ subset: resolve it to a written type, or say why not.

This is a *textual* deduction, not a type checker. Everything downstream of
it -- the class emitter, the scope tracker, the call rewriter -- reads types
by their spelling, so `auto` has to become a spelling before any of them run.
That constrains what can be claimed honestly: a form whose type is written
somewhere nearby can be resolved, and anything else is reported rather than
guessed.

What resolves:

    auto a = A();               a named class construction     -> A
    auto p = new A();           a heap allocation              -> A *
    auto b = Box<int>(3);       a template instantiation       -> Box<int>
    auto n = 3;                 literals                       -> int
    auto d = 1.5;                                              -> double
    auto s = "hi";                                             -> const char *
    auto c = 'x';                                              -> char
    auto t = true;                                             -> bool
    auto q = other;             a local whose type was written -> its type
    auto r = mk();              a function declared in-file    -> its return
    auto z = v.size();          a method of a known class      -> its return
    auto &e = v[0];             a reference or pointer form    -> T & / T *

Anything else -- a chained call whose intermediate type is not written, a
conditional, arithmetic mixing widths -- is refused with the reason. Writing
the type is always available, and is what the subset asks for elsewhere too.

Kept in its own module because it is a self-contained pass over the source
text with its own little symbol tables, and `tools/cpprust.py` is long
enough. It runs *after* lambda lowering, which consumes `auto f = [](){..}`
on its own.
"""

import json
import os
import re
import subprocess


class AutoError(Exception):
    """Raised when `auto` cannot be resolved. Message names the fix."""

    def __init__(self, message):
        # `self.args` directly rather than `Exception.__init__(self, message)`:
        # a call to the base's `__init__` has no lowering, and `args` is what
        # that call sets anyway -- so `e.args[0]` and `str(e)` both still give
        # the message, which is what the callers read.
        self.args = (message,)
        self.message = message


# `auto` in a declaration, with the qualifiers it is allowed to carry. The
# initialiser has to be an `=` form: `auto x(1);` is a declaration whose type
# is the thing being deduced, which reads as a call and is not worth the
# ambiguity.
#: `auto x = e` and `auto x { e }`. The braced spelling is list
#: initialisation, which for everything this subset lowers -- a
#: constructor call or a scalar -- is the same initialisation with the
#: same operand, so only the delimiter differs. Group 4 says which was
#: written, because the two end differently: one at the `;`, the other at
#: the matching `}`.
_AUTO_DECL = re.compile(
    r"(?<![\w.])(?:(const)\s+)?auto\s*(\*|&)?\s*(\w+)\s*(=|\{)\s*")

_INT_LIT = re.compile(r"^[+-]?(?:0[xX][0-9a-fA-F]+|0[bB][01]+|\d+)"
                      r"([uU]?[lL]{0,2}|[lL]{0,2}[uU]?)$")
_FLT_LIT = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?[fF]?$")

_KEYWORDS = frozenset((
    "if", "else", "while", "for", "switch", "case", "default", "break",
    "continue", "return", "sizeof", "new", "delete", "this", "true", "false",
    "static", "const", "void", "int", "char", "long", "short", "float",
    "double", "unsigned", "signed", "struct", "class", "union", "enum",
    "template", "typename", "public", "private", "protected", "virtual",
    "operator", "namespace", "using", "auto", "do", "goto"))

_BUILTIN = frozenset((
    "void", "int", "char", "long", "short", "float", "double", "unsigned",
    "signed", "bool", "size_t"))


#: Everything before the `(` of a declaration, then the name, then the
#: parameters. Splitting the return type off the name is done separately
#: rather than in the pattern: a regex that tries both at once backtracks
#: *into* the name -- `int mk(void)` came out as a function `k` returning
#: `int m`, which then made every deduction from it wrong.
_DECLARATOR = re.compile(
    r"(?:(?<=[;{}:\n])|\A)\s*"
    r"((?:[A-Za-z_][\w:]*(?:\s*<[^;{}()]*>)?[\s*&]+)+)"
    r"(\w+)\s*\(([^;{}()]*)\)\s*[;{]")

_FIELD = re.compile(
    r"(?:(?<=[;{}:\n])|\A)\s*"
    r"((?:[A-Za-z_][\w:]*(?:\s*<[^;{}()]*>)?[\s*&]+)+)"
    r"(\w+)\s*[;=]")

_INDEXER = re.compile(
    r"(?:(?<=[;{}:\n])|\A)\s*"
    r"((?:[A-Za-z_][\w:]*(?:\s*<[^;{}()]*>)?[\s*&]+)+)"
    r"operator\s*\[\s*\]\s*\(")

# The three patterns above can only begin just after one of these, or at the
# very start of the text -- that is what their leading lookbehind says. The
# lookahead carries the rest of what they all insist on (whitespace, then the
# first character of a type name), so a boundary that cannot start one is
# rejected by the scan instead of by a failed match attempt.
_DECL_ANCHOR = re.compile(r"[;{}:\n](?=\s*[A-Za-z_])")


def _anchored_finditer(pat, text):
    """`pat.finditer(text)` for a pattern anchored at a declaration boundary.

    Same matches, same non-overlapping rule -- but the boundaries are found
    by one scan for `_DECL_ANCHOR` instead of retrying the whole pattern at
    every offset in the file. The regex engine cannot see the anchor through
    a lookbehind, so left to itself it does retry everywhere, and these
    patterns are expensive to fail: a nested quantifier over type names,
    backtracked at every character of a spliced translation unit.

    `\\A` matches only at offset 0 and not at a `pos` handed to `match`,
    which is why position 0 is tried on its own rather than treated as one
    more boundary.
    """
    n = len(text)
    m = pat.match(text, 0)
    nxt = 0
    if m is not None:
        yield m
        nxt = m.end()
    while True:
        a = _DECL_ANCHOR.search(text, max(nxt - 1, 0))
        if a is None:
            return
        start = a.start() + 1
        if start > n:
            return
        m = pat.match(text, start)
        if m is None:
            nxt = start + 1
            continue
        yield m
        nxt = max(m.end(), start + 1)


_QUALIFIERS = frozenset(("static", "inline", "virtual", "const",
                         "explicit", "mutable", "extern"))



# --------------------------------------------------------------------------
# clang fallback
#
# The pass below reads types from how they are written, which is exact
# where a spelling exists and reports where none does. That is the whole
# design and it stays: a translation that needs no toolchain is the
# default, and the diagnostics name what to write.
#
# Where it reports, though, a C++ compiler already knows the answer --
# deduction is its job. So if `clang++` is on the machine, its answer is
# asked for before the report is raised. Nothing is approximated: clang
# either says what the type is or it does not, and if it does not, the
# original diagnostic stands.
#
# Two things this deliberately does not do. It does not run clang unless
# the textual pass has already failed, so a file that needs no help pays
# nothing. And it does not take a type this subset has no spelling for --
# a nested `iterator`, a closure type -- since emitting one only moves the
# error somewhere less informative.
# --------------------------------------------------------------------------

#: Set once the first lookup has run, so `clang++ --version` is not
#: spawned for every file in a build.
_CLANG_OK = None


def clang_available():
    """Whether `clang++` can be run, checked once."""
    global _CLANG_OK
    if _CLANG_OK is None:
        try:
            subprocess.check_output(["clang++", "--version"],
                                    stderr=subprocess.STDOUT)
            _CLANG_OK = True
        except (OSError, subprocess.CalledProcessError):
            _CLANG_OK = False
    return _CLANG_OK


def _walk_vardecls(node, where, out):
    """Collect `(file, name, type)` for every VarDecl in a clang AST.

    clang's JSON omits a location field that has not changed since the
    node before it, so both the file and the line have to be carried
    forward rather than read off each node -- most `VarDecl`s carry
    neither of their own.

    Tracking the *file* is what makes this usable at all. A dump of one
    litehtml source contains every declaration in every header it
    reaches, several hundred of them from libstdc++ alone, under names
    like `find`, `min`, `next` and `pi` that a program is perfectly
    entitled to use for something else. Only the file being translated
    can be taken from.
    """
    if isinstance(node, dict):
        loc = node.get("loc") or {}
        if isinstance(loc, dict):
            if loc.get("file"):
                where = loc["file"]
            # An `expansionLoc` or `spellingLoc` carries the file for a
            # node that came out of a macro.
            for k in ("expansionLoc", "spellingLoc"):
                sub = loc.get(k)
                if isinstance(sub, dict) and sub.get("file"):
                    where = sub["file"]
        if node.get("kind") == "VarDecl" and node.get("name"):
            ty = (node.get("type") or {}).get("qualType")
            if ty:
                out.append((where, node["name"], ty))
        for kid in node.get("inner") or ():
            where = _walk_vardecls(kid, where, out)
    elif isinstance(node, list):
        for kid in node:
            where = _walk_vardecls(kid, where, out)
    return where


def _from_cxx_spelling(ty):
    """A clang type spelling as this subset writes it, or None.

    `std::` is stripped, the way the rest of this pass strips it. What is
    deliberately not translated is anything the subset has no spelling for
    at all: a nested `iterator`, a closure type, an `auto` clang itself
    left dependent. Returning None for those keeps the textual pass's
    diagnostic, which names what to write, instead of emitting a type that
    means nothing downstream.
    """
    ty = (ty or "").strip()
    if not ty or "(" in ty or "lambda" in ty or "anonymous" in ty:
        return None
    if re.search(r"(?<![\w:])auto(?![\w])", ty):
        return None
    ty = re.sub(r"(?<![\w])std\s*::\s*", "", ty)
    # A qualified name this pass would still have to resolve is not one it
    # can take on trust: `basic_string<char>::iterator` names nothing here.
    if "::" in ty:
        return None
    return ty


def clang_auto_types(path, incdirs=(), defines=()):
    """`{name: type}` for the `auto` variables clang deduced, or `{}`.

    Keyed by *name*, not by line: this runs on the original file, while
    the deduction pass runs on text that has had headers spliced into it
    and namespaces flattened, so no line number survives the trip. A name
    clang gave more than one type is dropped rather than guessed between --
    the same answer this pass gives a by-value lambda capture it cannot
    pin down.
    """
    if not clang_available():
        return {}
    cmd = ["clang++", "-Xclang", "-ast-dump=json", "-fsyntax-only",
           "-std=c++20", "-w"]
    for d in incdirs:
        cmd += ["-I", d]
    for d in defines:
        cmd += ["-D", d]
    cmd.append(path)
    try:
        # clang reports on what it cannot parse and still dumps what it
        # did, so a non-zero status is not a reason to discard the answer.
        # No output is.
        # `subprocess.run(capture_output=True)` rather than Popen plus
        # communicate: the latter has no lowering, and this wants exactly
        # what run gives -- the whole of stdout, once, with the exit status
        # ignored. stderr is captured rather than sent to DEVNULL and then
        # dropped, which comes to the same thing here.
        done = subprocess.run(cmd, capture_output=True)
        raw = done.stdout
        if not raw:
            return {}
        tree = json.loads(raw.decode("utf-8", "replace"))
    except (OSError, ValueError, MemoryError):
        return {}
    found = []
    _walk_vardecls(tree, path, found)
    want = os.path.realpath(path)
    seen = {}
    for where, name, ty in found:
        if os.path.realpath(where or path) != want:
            continue                     # a header's declaration, not ours
        spelled = _from_cxx_spelling(ty)
        if spelled is None:
            continue
        if name in seen and seen[name] != spelled:
            seen[name] = None            # two types, one name: ambiguous
        elif name not in seen:
            seen[name] = spelled
    return dict((k, v) for k, v in seen.items() if v)


#: Names the clang fallback answered, for a caller that wants to say so.
#: A build where these are many is one leaning on a compiler that a
#: machine without clang does not have.
CLANG_USED = []


def _spellable(ty, classes, aliases):
    """Whether a fallback type is one this translation can actually use.

    clang answers in C++'s terms, and some of its answers name types that
    exist only inside the standard library. `iterator` is the one that
    matters here: it is a nested typedef, it arrives spelled bare, and
    emitting `iterator i = ..` into C declares a variable of a type
    nothing defines. Worse than the diagnostic it replaced, because the
    error moves from this pass to the C front end and stops naming
    `auto`.

    So a fallback answer is taken only if the translation already knows
    the name: a builtin, a class it has seen, or an alias it can resolve.
    """
    base = re.sub(r"[*&\s]+$", "", ty.strip())
    base = re.sub(r"^(?:const|volatile|struct|union|enum)\s+", "", base)
    base = base.split("<")[0].strip()
    if not base:
        return False
    if base in _BUILTIN or base in classes or base in aliases:
        return True
    # A multi-word builtin -- `unsigned long`, `long long`.
    return all(w in _BUILTIN or w in ("const", "unsigned", "signed",
                                      "long", "short")
               for w in base.split())


def _split_declarator(before):
    """`(type, None)` for a declarator's leading text, or `(None, why)`.

    `before` is everything ahead of the declared name. Qualifiers that are
    not part of the type are dropped; what is left has to be non-empty, or
    this was a call rather than a declaration.
    """
    words = before.replace("*", " * ").replace("&", " & ").split()
    words = [w for w in words if w not in _QUALIFIERS]
    # Access labels sit on the same line as the member that follows them,
    # so `public: int v;` would otherwise give `v` the type `public: int`.
    while words and words[0].rstrip(":") in ("public", "private",
                                             "protected"):
        words.pop(0)
    if not words:
        return None, "no type"
    # `return v;` and `case x:` read exactly like a declaration otherwise,
    # and taking them as one gives a field `v` of type `return`.
    if words[0] in ("return", "else", "case", "goto", "delete", "new",
                    "typedef", "using", "namespace", "template"):
        return None, "statement"
    return " ".join(words), None


def _line_of(text, idx):
    return text.count("\n", 0, idx) + 1


def _match(text, idx, open_ch, close_ch):
    """Index of the bracket closing the one at `idx`, or None."""
    depth = 0
    for k in range(idx, len(text)):
        if text[k] == open_ch:
            depth += 1
        elif text[k] == close_ch:
            depth -= 1
            if depth == 0:
                return k
    return None


def _scan_classes(scan):
    """`(class names, {class: {method: return type}}, {class: {field: type}})`.

    A light scan rather than a parse: the full one happens later in
    `cpprust`, and this pass only needs to answer "what does this name
    resolve to", which the spelling gives directly.
    """
    names, methods, fields, tparams = set(), {}, {}, {}
    for m in re.finditer(r"\b(?:class|struct)\s+(\w+)\s*(?::[^{;]*)?\{", scan):
        cname = m.group(1)
        names.add(cname)
        # `template<typename T>` just above makes the names in the body stand
        # for whatever an instantiation writes, so a return type of `T &` can
        # be read as the element type of a `vector<int>`.
        head = scan[max(0, m.start() - 120):m.start()]
        tm = re.search(r"template\s*<([^<>]*)>\s*$", head)
        if tm:
            tparams[cname] = [w.split()[-1] for w in tm.group(1).split(",")
                              if w.split()]
        open_idx = scan.index("{", m.start())
        close = _match(scan, open_idx, "{", "}")
        if close is None:
            continue
        body = scan[open_idx + 1:close]
        mm = methods.setdefault(cname, {})
        ff = fields.setdefault(cname, {})
        for d in _anchored_finditer(_INDEXER, body):
            ret, extra = _split_declarator(d.group(1))
            if ret and extra is None:
                methods.setdefault(cname, {})["[]"] = ret
        for d in _anchored_finditer(_DECLARATOR, body):
            ret, name = _split_declarator(d.group(1))
            if ret and name is None and d.group(2):
                mm[d.group(2)] = ret
        for d in _anchored_finditer(_FIELD, body):
            ret, name = _split_declarator(d.group(1))
            if ret and name is None and d.group(2):
                ff[d.group(2)] = ret
    return names, methods, fields, tparams


_TYPEDEF = re.compile(r"(?<![\w.])typedef\s+([^;{}]+);")
_USING_ALIAS = re.compile(r"(?<![\w.])using\s+(\w+)\s*=\s*([^;{}]+);")


def _class_body(scan, name):
    """The text between the braces of `class`/`struct` `name`, or None."""
    m = re.search(r"(?<![\w])(?:class|struct)\s+%s\s*(?::[^{;]*)?\{"
                  % re.escape(name), scan)
    if m is None:
        return None
    close = _match(scan, m.end() - 1, "{", "}")
    if close is None:
        return None
    return scan[m.end():close]


def _scan_typedefs(scan, owner=None):
    """`{alias: target}` for type aliases at file scope.

    **Depth zero only.** A typedef inside a class body is scoped to it, and
    litehtml's are named `ptr` and `vector` -- collecting those flatly would
    make every `vector` in the file mean `box::vector`, including the
    supplied template of that name. Namespaces are flattened before this
    runs, so a namespace-scope alias is already at depth zero and is picked
    up; a class-scoped one is left alone rather than half-understood.

    `owner` asks for the aliases of one class instead: the body of that
    class is scanned at its own depth zero, so `typedef std::vector<..> rows;`
    inside `table_grid` is found without `rows` becoming a file-wide name.
    That is what lets a field declared `rows m_cells;` be walked -- without
    it the field's type resolved to the alias itself, which names no class.
    """
    if owner is not None:
        body = _class_body(scan, owner)
        return _scan_typedefs(body) if body is not None else {}
    out, depth, i, n = {}, 0, 0, len(scan)
    while i < n:
        c = scan[i]
        if c == "{":
            depth += 1
            i += 1
            continue
        if c == "}":
            depth -= 1
            i += 1
            continue
        if depth != 0 or not (c.isalpha() or c == "_"):
            i += 1
            continue
        m = _USING_ALIAS.match(scan, i)
        if m:
            out[m.group(1)] = " ".join(m.group(2).split())
            i = m.end()
            continue
        m = _TYPEDEF.match(scan, i)
        if m:
            body = m.group(1).strip()
            if "(" not in body:          # not a function-pointer typedef
                parts = body.rsplit(None, 1)
                if len(parts) == 2 and re.match(r"^\w+$", parts[1]):
                    target = " ".join(parts[0].split())
                    # `typedef struct X X;` names itself, and substituting it
                    # prepends `struct` once per round: the C emitted by
                    # Crust for its own types is full of them, and one pass
                    # produced `struct struct struct .. Vec_int`.
                    if not re.search(r"(?<![\w])%s(?![\w])"
                                     % re.escape(parts[1]), target):
                        out[parts[1]] = target
            i = m.end()
            continue
        i += 1
    return out


def _resolve_alias(ty, tmap):
    """Follow type aliases to what they name, or return `ty` unchanged."""
    seen = set()
    for _round in range(16):
        base = ty.strip()
        star = ""
        while base.endswith("*") or base.endswith("&"):
            star = base[-1] + star
            base = base[:-1].strip()
        if base not in tmap or base in seen:
            return ty
        seen.add(base)
        ty = (tmap[base] + " " + star).strip()
    return ty


def _scan_functions(scan):
    """`{name: return type}` for functions declared or defined at file scope."""
    out = {}
    for m in _anchored_finditer(_DECLARATOR, scan):
        ret, extra = _split_declarator(m.group(1))
        if extra is not None or not ret or m.group(2) in _KEYWORDS:
            continue
        out[m.group(2)] = ret
    return out


def _declared_types(text):
    """`{variable: type}` for every written declaration in the file.

    Flat rather than scoped, which is the honest limit of a textual pass: two
    locals of different types with one name in different functions would
    collide, and the later spelling wins. Both are *written*, so anyone hitting
    that can write the third one too.
    """
    out = {}
    for m in re.finditer(
            # `:` and a newline anchor too: an access label ends in a colon
            # and the member after it is on the next line, so `private:` on
            # its own line hid every field that followed from this scan.
            r"(?:^|[;{}(,:\n])\s*(?:const\s+|static\s+)*"
            r"([A-Za-z_][\w:]*(?:\s*<[^;{}()]*>)?)\s*(\*+|&)?\s*"
            r"(\w+)\s*(?=([;=,)\[]))", text):
        base, star, name = m.group(1), m.group(2) or "", m.group(3)
        if base in _KEYWORDS and base not in _BUILTIN:
            continue
        if name in _KEYWORDS:
            continue
        ty = (base + " " + star).strip() if star else base
        # `int a[3]` is an array of `int`, and the difference matters: it is
        # what a range-`for` walks and what a subscript yields.
        if m.group(4) == "[":
            ty += " []"
        out[name] = ty
    return out


def _strip_outer(expr):
    expr = expr.strip()
    while expr.startswith("(") and _match(expr, 0, "(", ")") == len(expr) - 1:
        expr = expr[1:-1].strip()
    return expr


def _is_type_spelling(inner, ctx):
    """Whether `inner` reads as a type rather than a value.

    Only used to tell a C-style cast from a parenthesised expression, so
    it errs towards saying no: a wrong yes would take `(a) + b` for a
    cast, and the cost of a wrong no is only that `auto` reports instead
    of deducing, which is the documented behaviour anyway.
    """
    classes, _, _, tparams, _, _, aliases = ctx
    words = inner.replace("*", " ").split()
    if not words or "(" in inner or "[" in inner:
        return False
    for w in words:
        if w in ("const", "unsigned", "signed", "struct", "long", "short"):
            continue
        if w in _BUILTIN or w in classes or w in aliases or w in tparams:
            continue
        return False
    return True


def _chain_type_ctx(expr, ctx):
    """The written type of a `a.b.c` chain, from context alone.

    `_chain_type` is the same walk but needs `scan` and a position, so it
    can fall back to a field of the enclosing class. `_deduce` has neither
    -- it is handed an expression and a context -- and a chain whose head is
    a declared local or parameter needs no more than that.
    """
    classes, _methods, fields, _tp, _funcs, vars_, aliases = ctx
    parts = [p for p in re.split(r"\s*(?:\.|->)\s*", expr) if p]
    if not parts:
        return None
    ty = _resolve_alias(vars_.get(parts[0], "") or "", aliases)
    if not ty:
        return None
    for step in parts[1:]:
        base = re.sub(r"[*&\s]+$", "", ty).split("<")[0].strip()
        if base not in classes:
            base = next((k for k in classes if k.endswith("_" + base)), base)
            if base not in classes:
                return None
        ty = _resolve_alias(fields.get(base, {}).get(step, "") or "", aliases)
        if not ty:
            return None
    return ty


def _deduce(expr, ctx, where):
    """The written type of `expr`, or raise `AutoError` naming why not."""
    classes, methods, fields, tparams, funcs, vars_, aliases = ctx
    e = _strip_outer(expr)
    if not e:
        raise AutoError("%s: `auto` with no initialiser to deduce from" % where)

    # `new T` / `new T(..)` / `new T<..>(..)`
    m = re.match(r"^new\s+([A-Za-z_]\w*)\s*(<[^;]*?>)?\s*[({]?", e)
    if m:
        return (m.group(1) + (m.group(2) or "")).strip() + " *"

    # `&x` -- deduce the operand and add a star.
    if e.startswith("&") and not e.startswith("&&"):
        return _deduce(e[1:], ctx, where) + " *"

    # A named cast. The target type is spelled in the angle brackets, at
    # the point of use, which is as written as a type ever gets -- so this
    # is the one deduction that needs nothing looked up. `dynamic_cast` is
    # deliberately absent: it is refused elsewhere for wanting RTTI, and
    # deducing through it here would turn that refusal into a lowering
    # that compiles and dispatches on nothing.
    m = re.match(r"^(static_cast|reinterpret_cast|const_cast)\s*<", e)
    if m:
        lt = e.index("<")
        gt = _match(e, lt, "<", ">")
        if gt > 0:
            rest = e[gt + 1:].lstrip()
            if rest.startswith("(") and _match(e, e.index("(", gt), "(",
                                               ")") == len(e) - 1:
                ty = e[lt + 1:gt].strip()
                # `const_cast` exists to remove the qualifier; keeping it
                # would declare a local nothing can be assigned through.
                if m.group(1) == "const_cast":
                    ty = re.sub(r"(?<![\w])const\s+", "", ty).strip()
                return ty

    # A C-style cast, `(T *)e`. Told from a parenthesised expression by
    # what is inside: a type, not something that reads as a value.
    if e.startswith("("):
        close = _match(e, 0, "(", ")")
        if close > 0 and close < len(e) - 1:
            inner = e[1:close].strip()
            after = e[close + 1:].strip()
            if after and _is_type_spelling(inner, ctx):
                return inner

    # Literals.
    if _INT_LIT.match(e):
        low = e.lower()
        if "ll" in low:
            return "unsigned long long" if "u" in low else "long long"
        if "l" in low:
            return "unsigned long" if "u" in low else "long"
        if "u" in low:
            return "unsigned int"
        return "int"
    if _FLT_LIT.match(e) and ("." in e or "e" in e.lower()):
        return "float" if e[-1] in "fF" else "double"
    if e.startswith('"'):
        return "const char *"
    if e.startswith("'"):
        return "char"
    if e in ("true", "false"):
        return "bool"

    # `Name(..)` or `Name<..>(..)` -- a construction if `Name` is a class,
    # otherwise a call to a function whose return type is written.
    m = re.match(r"^([A-Za-z_]\w*)\s*(<[^;]*?>)?\s*\(", e)
    if m and _match(e, e.index("("), "(", ")") == len(e) - 1:
        name, targs = m.group(1), m.group(2) or ""
        if name in classes:
            return name + targs
        if name in funcs and not targs:
            return funcs[name]
        raise AutoError(
            "%s: `auto` cannot deduce from `%s(..)` -- this pass reads types "
            "from how they are written, and neither a class nor a function "
            "declaration here names `%s`. Write the type."
            % (where, name, name))

    # `recv.method(..)` / `recv->method(..)` -- one level, and the receiver
    # has to be something whose type is written.
    m = re.match(r"^([A-Za-z_]\w*(?:\s*(?:\.|->)\s*\w+)*)\s*(?:\.|->)\s*"
                 r"(\w+)\s*\(", e)
    if m and _match(e, e.index("("), "(", ")") == len(e) - 1:
        recv, meth = m.group(1), m.group(2)
        rty = _resolve_alias(vars_.get(recv) or "", aliases)
        if not rty and re.search(r"\.|->", recv):
            # A chain receiver -- `src.m_properties.at_index(i)`. Reading
            # only a bare name meant a method called on a *field* could not
            # be typed, which is what a range-`for` over a member container
            # produces once it binds the element.
            rty = _chain_type_ctx(recv, ctx) or ""
        if rty:
            base = re.sub(r"[*&\s]+$", "", rty).split("<")[0].strip()
            ret = methods.get(base, {}).get(meth)
            if ret:
                # A method of a *template* returns the type as the template
                # spells it. `map<int,int>::begin()` reads `pair<K,V> *` in
                # the source, and taking that literally asked for a class
                # called `pair_K_V`.
                return _subst_tparams(ret, base, rty, tparams)
        raise AutoError(
            "%s: `auto` cannot deduce from `%s.%s(..)` -- the return type of "
            "`%s` is not written anywhere this pass can read. Write the type."
            % (where, recv, meth, meth))

    # `recv[i]` -- the element type, read off `operator[]`'s return with the
    # instantiation's arguments put back in place of the template parameters.
    m = re.match(r"^([A-Za-z_]\w*)\s*\[", e)
    if m and _match(e, e.index("["), "[", "]") == len(e) - 1:
        ety = _element_type(m.group(1), ctx)
        if ety:
            return ety
        raise AutoError(
            "%s: `auto` cannot deduce the element type of `%s` -- it is "
            "neither an array with a written size nor a class with an "
            "`operator[]` whose return type is written. Write the type."
            % (where, m.group(1)))

    # `recv.field` / `recv->field`
    m = re.match(r"^([A-Za-z_]\w*)\s*(?:\.|->)\s*(\w+)$", e)
    if m:
        rty = _resolve_alias(vars_.get(m.group(1)) or "", aliases)
        if rty:
            base = re.sub(r"[*&\s]+$", "", rty).split("<")[0].strip()
            fty = fields.get(base, {}).get(m.group(2))
            if fty:
                return fty

    # A plain name.
    if re.match(r"^[A-Za-z_]\w*$", e):
        if e in vars_:
            return _resolve_alias(vars_[e], aliases)
        raise AutoError(
            "%s: `auto` cannot deduce from `%s` -- no declaration of it is "
            "written where this pass can read one. Write the type."
            % (where, e))

    raise AutoError(
        "%s: `auto` cannot deduce from `%s`. This pass reads types from how "
        "they are written, so a compound expression has no spelling to take. "
        "Write the type." % (where, e.strip()[:48]))


def _element_type(name, ctx):
    """The element type of array or container `name`, or None."""
    classes, methods, fields, tparams, funcs, vars_, aliases = ctx
    ty = vars_.get(name)
    if not ty:
        return None
    ty = _resolve_alias(ty, aliases).strip()
    if ty.endswith("[]"):
        return ty[:-2].strip()
    # A container reached through a pointer has the same elements as one
    # reached directly; the star belongs to how it was passed, not to what
    # it holds.
    m = re.match(r"^([A-Za-z_]\w*)\s*(?:<(.*)>)?\s*[*&]*$", ty)
    if m is None:
        return None
    base, args = m.group(1), m.group(2)
    if base not in classes:
        return None
    ret = methods.get(base, {}).get("[]")
    if not ret:
        return None
    ret = re.sub(r"\s*&\s*$", "", ret).strip()
    return _subst_tparams(ret, base, ty, tparams).strip()


def _subst_tparams(ret, base, spelled, tparams):
    """Put an instantiation's arguments back into a template's own spelling.

    `spelled` is how the receiver was declared -- `map<int, int>` -- and
    `ret` is written in terms of the template's parameters, so `pair<K,V> *`
    becomes `pair<int,int> *`.
    """
    params = tparams.get(base) or []
    m = re.search(r"<(.*)>", spelled.strip())
    if not params or m is None:
        return ret
    actual = [a.strip() for a in _split_top(m.group(1))]
    for pname, aval in zip(params, actual):
        ret = re.sub(r"(?<![\w])%s(?![\w])" % re.escape(pname), aval, ret)
    return ret


def _split_top(text):
    """Split on top-level commas."""
    out, depth, cur = [], 0, []
    for ch in text:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def _ctor_args(init, ty, classes):
    """The argument text if `init` constructs exactly `ty`, else None."""
    e = _strip_outer(init)
    m = re.match(r"^([A-Za-z_]\w*)\s*(<[^;]*?>)?\s*\(", e)
    if m is None or m.group(1) not in classes:
        return None
    if (m.group(1) + (m.group(2) or "")).strip() != ty.strip():
        return None
    close = _match(e, e.index("("), "(", ")")
    if close is None or close != len(e) - 1:
        return None
    return e[e.index("(") + 1:close]


def _end_of_init(text, at):
    """Index of the `;` ending the initialiser starting at `at`."""
    depth_p = depth_b = depth_c = 0
    k = at
    while k < len(text):
        c = text[k]
        if c in "\"'":
            q, k = c, k + 1
            while k < len(text) and text[k] != q:
                k += 2 if text[k] == "\\" else 1
        elif c == "(":
            depth_p += 1
        elif c == ")":
            depth_p -= 1
            # Below where it started means this is the `)` of an enclosing
            # `if (..)` or `while (..)`, and a declaration in a condition
            # ends there rather than at a `;` -- there is no `;` to find.
            if depth_p < 0:
                return k
        elif c == "[":
            depth_b += 1
        elif c == "]":
            depth_b -= 1
        elif c == "{":
            depth_c += 1
        elif c == "}":
            depth_c -= 1
        elif c == ";" and depth_p <= 0 and depth_b <= 0 and depth_c <= 0:
            return k
        k += 1
    return None


#: The `:` here is the range-`for`'s own, never one half of a `::`. That
#: has to be said, because the type and name groups will happily backtrack
#: to make a `::` fit: `for (tstring::size_type i = 0; ..)` was read as
#: type `t`, name `string`, and the first colon of the `::` as the range
#: colon -- reporting an ordinary indexed loop as an unwalkable range.
_RANGE_FOR = re.compile(
    r"(?<![\w.])for\s*\(\s*(?:(const)\s+)?"
    r"(auto|[A-Za-z_][\w:]*(?:\s*<[^;()]*>)?)\s*(&)?\s*(\w+)\s*:(?!:)\s*")


def _array_len(name, scan):
    """The written length of array `name`, or None."""
    m = re.search(r"(?<![\w.])[A-Za-z_][\w:]*[\s*]+%s\s*\[\s*([^\]]*?)\s*\]"
                  % re.escape(name), scan)
    if m is None:
        return None
    return m.group(1).strip() or None


def _enclosing_class(scan, idx):
    """The innermost class whose body contains `idx`, or None.

    A method body writing a bare `m_attrs` means `this->m_attrs`, so
    resolving one needs to know which class is being written inside.
    """
    best = None
    for m in re.finditer(r"\b(?:class|struct)\s+(\w+)\s*(?::[^{;]*)?\{", scan):
        open_idx = scan.index("{", m.start())
        close = _match(scan, open_idx, "{", "}")
        if close is None or not (open_idx < idx < close):
            continue
        # Innermost wins: a nested class's body is inside its outer's.
        if best is None or open_idx > best[1]:
            best = (m.group(1), open_idx)
    if best is not None:
        return best[0]
    # No lexically enclosing class body -- but an out-of-line definition,
    # `void table_grid::clear() { .. }`, is just as much inside one. C++
    # looks a bare `m_cells` there up in `table_grid`, and litehtml writes
    # nearly every method that way, so without this a member reference in
    # one resolved to nothing at all.
    for m in re.finditer(r"(?<![\w:])(\w+)\s*::\s*~?\w+\s*\(", scan):
        close = _match(scan, m.end() - 1, "(", ")")
        if close is None:
            continue
        j = close + 1
        while j < len(scan) and scan[j] not in "{;":
            j += 1
        if j >= len(scan) or scan[j] != "{":
            continue                     # a declaration, not a definition
        end = _match(scan, j, "{", "}")
        if end is not None and j < idx < end:
            return m.group(1)
    return None


def _chain_type(expr, scan, ctx, idx):
    """The written type of a `a.b->c` chain, or None.

    The head is a local, a parameter, or -- inside a method -- a field of
    the enclosing class; each step after it is a field of what the last
    one resolved to. Every part is read from how it was *declared*, which
    is the same thing this pass does for a bare name; a chain is not a
    different kind of knowledge, only more of the same.
    """
    classes, _methods, fields, _tp, _f, vars_, aliases = ctx
    parts = [p for p in re.split(r"\s*(?:\.|->)\s*", expr) if p]
    if not parts:
        return None
    ty = _resolve_alias(vars_.get(parts[0], ""), aliases)
    if not ty:
        owner = _enclosing_class(scan, idx)
        if owner:
            ty = _resolve_alias(fields.get(owner, {}).get(parts[0], ""),
                                aliases)
    if not ty:
        return None
    for step in parts[1:]:
        base = re.sub(r"[*&\s]+$", "", ty).split("<")[0].strip()
        if base not in classes:
            # A parameter of an out-of-line definition keeps the spelling it
            # was written with -- `const style& src` -- while the class it
            # names became `litehtml_style` when the namespace was
            # flattened. The declarator is not rewritten, so the two only
            # meet if a flattened spelling is accepted here.
            base = next((k for k in classes if k.endswith("_" + base)), base)
            if base not in classes:
                return None
        ty = _resolve_alias(fields.get(base, {}).get(step, ""), aliases)
        if not ty:
            return None
    return ty


def resolve_range_for(text, path="<cpp>", blank=None):
    """Rewrite C++11 range-`for` into the index loop it stands for.

        for (auto &x : v) { .. }
     -> for (int _cpp_it0 = 0; _cpp_it0 < v.size(); _cpp_it0 = _cpp_it0 + 1)
            { .. with `x` reading `v[_cpp_it0]` .. }

    The reference form is done by *substitution*, which is what a reference
    means: the name aliases the element, so writing through it writes to the
    container. The by-value form declares a copy instead, `auto x = v[i]`,
    which the deduction pass above then resolves -- so the two forms differ
    here exactly as they differ in C++, rather than one quietly behaving like
    the other.

    The range has to be a name whose length is readable: an array with a
    written size, or a class with `size()` and `operator[]`. `begin()`/`end()`
    iterators are a different feature and are reported, not guessed at.
    """
    scan = blank if blank is not None else text
    if "for" not in scan:
        return text
    classes, methods, fields, _t = _scan_classes(scan)
    n = 0
    while True:
        m = _RANGE_FOR.search(scan)
        if m is None:
            return text
        head_end = _match(scan, scan.index("(", m.start()), "(", ")")
        if head_end is None:
            raise AutoError("%s:%d: unterminated `for (`"
                            % (path, _line_of(text, m.start())))
        rng = text[m.end():head_end].strip()
        where = "%s:%d" % (path, _line_of(text, m.start()))
        # `*p` is as much a name as `c` is: the length is still read from how
        # the pointed-to thing was declared, and walking a container through
        # a pointer is the ordinary way to pass one to a function.
        deref = bool(re.match(r"^\*\s*[A-Za-z_]\w*$", rng))
        rng = rng.lstrip("*").strip()
        # A name, or a chain of them: `m_right.m_attrs` is written just as
        # plainly as `v` is, and each step has a declared type to read. The
        # restriction to a bare name was narrower than the reason for it,
        # which is only that the length has to be readable from the
        # spelling.
        if not re.match(r"^[A-Za-z_]\w*(?:\s*(?:\.|->)\s*\w+)*$", rng):
            raise AutoError(
                "%s: a range-`for` here needs a named array or container, or "
                "a member of one (optionally through a pointer); `%s` is an "
                "expression, and this pass reads the length from how the "
                "range is written. Assign it to a name first."
                % (where, text[m.end():head_end].strip()[:40]))
        # Where does the body start, and how long is the range?
        body_open = scan.find("{", head_end)
        if body_open < 0:
            raise AutoError(
                "%s: a range-`for` body has to be braced here, so the loop "
                "variable has somewhere to live." % where)
        body_close = _match(scan, body_open, "{", "}")
        if body_close is None:
            raise AutoError("%s: unterminated range-`for` body" % where)

        alen = _array_len(rng, scan)
        cls = None
        chain = bool(re.search(r"\.|->", rng))
        # A class-scoped typedef is invisible at file scope by design, so
        # the aliases of the class being written in are added here. The name
        # comes off an out-of-line declarator (`table_grid::clear`), which
        # namespace flattening leaves unqualified while the class itself
        # became `litehtml_table_grid` -- so a flattened spelling is
        # accepted too rather than the owner silently not being found.
        _own = _enclosing_class(scan, m.start())
        if _own is not None and _own not in classes:
            _own = next((k for k in classes if k.endswith("_" + _own)), _own)
        tmap = dict(_scan_typedefs(scan))
        if _own:
            tmap.update(_scan_typedefs(scan, _own))
        if chain:
            vt = _chain_type(rng, scan, (classes, methods, fields, {}, {},
                                         _declared_types(scan), tmap),
                             m.start()) or ""
            vt = _resolve_alias(vt, tmap)
        else:
            vt = _resolve_alias(_declared_types(scan).get(rng, ""), tmap)
            if not vt:
                # A bare name inside a method may be a field of the class
                # being written, which is `this->name` and just as declared.
                if _own:
                    vt = _resolve_alias(fields.get(_own, {}).get(rng, ""),
                                        tmap)
        cm = re.match(r"^([A-Za-z_]\w*)", vt.strip().lstrip("*"))
        if cm and cm.group(1) in classes:
            cls = cm.group(1)
        # A chain ending in a pointer field is reached with `->`, the same
        # way a dereferenced name is.
        if vt.rstrip().endswith("*"):
            deref = True
        # `at_index(int)` is the integer accessor of a container whose
        # `operator[]` is keyed on something else -- a map. Preferred when
        # present, so walking a map reads its entries in order rather than
        # looking each one up by an integer key it does not have.
        has_at_index = "at_index" in methods.get(cls, {}) if cls else False
        if alen is None and (cls is None
                             or "size" not in methods.get(cls, {})
                             or ("[]" not in methods.get(cls, {})
                                 and not has_at_index)):
            raise AutoError(
                "%s: `%s` is not something this pass can walk -- an array "
                "needs a written size, and a container needs both `size()` "
                "and `operator[]`. Iterators are a separate feature and are "
                "not guessed at here." % (where, rng))

        it = "_cpp_it%d" % n
        n += 1
        arrow = "->" if deref else "."
        # The bare name even through a pointer, not `(*p)[i]`: the subscript
        # rewriting resolves a *symbol*, and a class-typed one that happens
        # to be a pointer is exactly the case it already handles -- a lowered
        # `Bag &` is indistinguishable from a spelled `Bag *`. `(*p)[i]` has
        # no symbol in front of the bracket and comes out as raw C indexing
        # on a struct.
        limit = alen if alen is not None else "%s%ssize()" % (rng, arrow)
        # `at_index` hands back a pointer -- a reference return on a named
        # method is not read as one here, and a pointer needs no such
        # reading. The dereference is written out so the element expression
        # is an lvalue of the element type either way.
        elem = "%s[%s]" % (rng, it)
        body = text[body_open + 1:body_close]
        if has_at_index:
            # A container walked through `at_index` binds the element to a
            # name rather than substituting the call in textually. The
            # substituted form left `property.first.c_str()` reading
            # `(*map_at_index(&m, i)).first.c_str()`, and the call rewriter
            # resolves a *symbol* -- there is none in front of that dot, so
            # the method never lowered. A declared local is a symbol, and a
            # class-typed one that happens to be a pointer is the case the
            # field and call rewriting already handle.
            #
            # `auto`, because the element type has no spelling available
            # here; this pass runs before deduction, which reads it off
            # `at_index`'s written return type.
            decl = " auto %s = %s%sat_index(%s);" % (m.group(4), rng, arrow, it)
            head = ("for (int %s = 0; %s < %s; %s = %s + 1) {%s"
                    % (it, it, limit, it, it, decl))
            text = text[:m.start()] + head + body + text[body_close:]
            scan = _blank_like(text)
            continue
        if m.group(3):                   # `auto &x` / `T &x`: an alias
            # `scan`, not `blank`: after the first rewrite the two no longer
            # line up, and reading the stale one silently substituted nothing.
            body = _sub_name(body, scan[body_open + 1:body_close],
                             m.group(4), elem)
            decl = ""
        else:                            # a copy, with its own declaration
            ty = "auto" if m.group(2) == "auto" else m.group(2)
            decl = " %s%s %s = %s;" % ("const " if m.group(1) else "",
                                       ty, m.group(4), elem)
        head = ("for (int %s = 0; %s < %s; %s = %s + 1) {%s"
                % (it, it, limit, it, it, decl))
        text = text[:m.start()] + head + body + text[body_close:]
        scan = _blank_like(text)
    return text


_CPP_REF_CALL = re.compile(r"__cpp_ref\s*\(\s*([\w:]+)\s*\)")
_BLANK_OPEN = re.compile(r"[\"']|//|/\*")
_BRACE = re.compile(r"[{}]")
_NOT_NEWLINE = re.compile(r"[^\n]")


def _blank_directive_lines(text):
    """Blank preprocessor directive lines, continuations included.

    A `#define` body is not code, and every resolver here is a regex that
    cannot know that on its own. coost's `DISALLOW_COPY_AND_ASSIGN(T)`
    macro spells `T(const T&) = delete;` on a continuation line;
    `resolve_defaulted` matched it, then cut "back to the start of the
    member" -- through the `#define` head and twelve unrelated defines, to
    a typedef's semicolon fourteen lines up. Length and newlines are kept,
    as everywhere in this file, so positions still index the real text.
    Mirrors `cpprust._blank_directives`, which this module cannot import
    without a cycle.
    """
    out, i, n = [], 0, len(text)
    while i < n:
        j = text.find("\n", i)
        j = n if j < 0 else j
        line = text[i:j]
        if line.lstrip().startswith("#"):
            out.append(" " * len(line))
            while line.rstrip().endswith("\\") and j < n:
                i = j + 1
                out.append("\n")
                j = text.find("\n", i)
                j = n if j < 0 else j
                line = text[i:j]
                out.append(" " * len(line))
        else:
            out.append(line)
        if j < n:
            out.append("\n")
        i = j + 1
    return "".join(out)


def _blank_like(text, directives=True):
    """A same-length copy with comment and literal bodies blanked.

    `__cpp_ref(T)` is blanked down to just `T` as well. It is a *type* in a
    parameter list, but it is spelled like a call, and the declarator scan
    reads a parameter list as having no parentheses in it -- so `map`'s
    `find(__cpp_ref(K) k)` looked like no declaration at all, and every
    deduction from `m.find(..)` failed. Length is preserved, as everywhere
    else here, so positions in this copy still index the real text.
    """
    n = len(text)
    out, last, pos = [], 0, 0
    # Jump from one literal or comment opener to the next rather than
    # walking every character: this runs over the whole translation unit
    # once per rewrite, so the characters *between* the interesting ones
    # are the bulk of the work and none of them need looking at.
    while True:
        m = _BLANK_OPEN.search(text, pos)
        if m is None:
            break
        i = m.start()
        c = text[i]
        if c in "\"'":
            q, j = c, i + 1
            while j < n and text[j] != q:
                j += 2 if text[j] == "\\" else 1
            lo, hi, pos = i + 1, min(j, n), min(j + 1, n)
        elif text[i:i + 2] == "//":
            j = text.find("\n", i)
            j = n if j < 0 else j
            lo, hi, pos = i, j, j
        else:
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            lo, hi, pos = i, j, j
        out.append(text[last:lo])
        out.append(_NOT_NEWLINE.sub(" ", text[lo:hi]))
        last = hi
        if pos <= i:
            pos = i + 1
    out.append(text[last:])
    blanked = "".join(out)
    if directives:
        blanked = _blank_directive_lines(blanked)
    if "__cpp_ref" not in blanked:
        return blanked
    out, last = [], 0
    for m in _CPP_REF_CALL.finditer(blanked):
        pad = m.end() - m.start() - len(m.group(1))
        out.append(blanked[last:m.start()])
        out.append(" " * (pad - 1) + m.group(1) + " ")
        last = m.end()
    out.append(blanked[last:])
    return "".join(out)


def _sub_name(body, body_scan, name, repl):
    """Replace whole-word `name` in `body`, guided by the blanked copy."""
    out, last = [], 0
    for m in re.finditer(r"(?<![\w.>])%s(?![\w])" % re.escape(name),
                         body_scan):
        out.append(body[last:m.start()])
        out.append(repl)
        last = m.end()
    out.append(body[last:])
    return "".join(out)


def _flatten_pattern(names):
    """One whole-word pattern matching every one of `names`, or None.

    Built once and reused, because the alternative is what the flattening
    passes below used to do: loop over some hundreds of names and, for each
    one, substitute into the body and re-blank the result. All of that
    collapses into a single scan -- and it is exactly equivalent, because at
    any one position only a single name can match. `(?![\\w])` means a match
    ends where an identifier ends and `(?<![\\w.>])` means it begins where
    one begins, so the only candidate anywhere is the whole word sitting
    there, and which name is tried first cannot change the answer.
    """
    names = list(names)
    if not names:
        return None
    return re.compile(r"(?<![\w.>])(%s)(?![\w])"
                      % "|".join(re.escape(n) for n in names))


def _sub_flattened(body, body_scan, pat, ns):
    """Rewrite every name `pat` finds to `ns_name`, guided by the copy.

    A name written `::name` is qualified with *global* scope and is not the
    namespace's at all -- coost's allocator calls `::free(p)` to reach the
    C library from a class that has its own `free`. Flattening rewrote that
    to `::this->co_free(p)`, which is both invalid C and the wrong
    function. The `::` is dropped on the way out, since C has only the one
    scope for it to mean.
    """
    out, last = [], 0
    for m in pat.finditer(body_scan):
        before = body_scan[:m.start()].rstrip()
        if before.endswith("::"):
            # Leading `::`: global scope. Keep the name, drop the marker --
            # and take the text back to just before it, so the `::` does
            # not survive into the output.
            cut = m.start() - (len(body_scan[:m.start()]) - len(before))
            out.append(body[last:cut - 2])
            out.append(body[cut:m.end()])
            last = m.end()
            continue
        out.append(body[last:m.start()])
        out.append("%s_%s" % (ns, m.group(1)))
        last = m.end()
    out.append(body[last:])
    return "".join(out)


def resolve(text, path="<cpp>", blank=None, fallback=None):
    """Rewrite every `auto` declaration in `text` to a written type.

    `blank` is a copy of `text` with comments and string bodies blanked and
    the same length, so scanning never matches inside either; `text` is what
    gets rewritten. Raises `AutoError` for an `auto` that cannot be resolved.
    """
    scan = blank if blank is not None else text
    if not re.search(r"(?<![\w.])auto\b", scan):
        return text
    classes, methods, fields, tparams = _scan_classes(scan)
    funcs = _scan_functions(scan)
    vars_ = _declared_types(scan)
    aliases = _scan_typedefs(scan)

    out, i = [], 0
    while True:
        m = _AUTO_DECL.search(scan, i)
        if m is None:
            out.append(text[i:])
            break
        braced = m.group(4) == "{"
        if braced:
            # The initialiser is what the braces hold, so it ends at the
            # matching `}` rather than at the `;`.
            close = _match(scan, m.start(4), "{", "}")
            end = close if close and close > 0 else None
            resume = end + 1 if end is not None else None
        else:
            end = _end_of_init(scan, m.end())
            resume = end
        if end is None:
            raise AutoError(
                "%s:%d: `auto %s %s ..` has no %s to end it"
                % (path, _line_of(text, m.start()), m.group(3),
                   m.group(4), "`}`" if braced else "`;`"))
        where = "%s:%d" % (path, _line_of(text, m.start()))
        init = text[m.end():end]
        ctx = (classes, methods, fields, tparams, funcs, vars_, aliases)
        try:
            ty = _deduce(init, ctx, where)
        except AutoError:
            # Only now, and only for this declaration: a file whose types
            # are all written pays nothing for the fallback existing.
            got = (fallback or {}).get(m.group(3))
            if not got or not _spellable(got, classes, aliases):
                raise
            ty = got
            CLANG_USED.append((m.group(3), ty))
        # A deduced reference or pointer keeps the sigil the author wrote;
        # `auto` itself never carries one, so this is additive.
        sigil = m.group(2) or ""
        if sigil == "*" and ty.rstrip().endswith("*"):
            ty, sigil = ty.rstrip()[:-1].rstrip(), "*"
        decl = "%s%s %s%s" % ("const " if m.group(1) else "",
                              ty.strip(), sigil, m.group(3))
        # Record it, so a later `auto` can deduce from this one.
        vars_[m.group(3)] = (ty.strip() + " " + sigil).strip()
        out.append(text[i:m.start()])
        ctor = _ctor_args(init, ty, classes) if not sigil else None
        if ctor is not None:
            # `auto a = A(x);` is written as copy-initialisation but means
            # direct-initialisation -- C++17 guarantees the temporary is
            # elided, and this subset only lowers the direct form. So it is
            # emitted as `A a(x)`, which becomes `A a; A_new(&a, x);`.
            out.append("%s %s(%s)" % (ty.strip(), m.group(3), ctor))
            i = resume                   # the `;` is copied through below
        elif braced:
            # `T x { e }` is `T x = e`. The braces are consumed here, so
            # nothing downstream has to know the spelling existed.
            out.append(decl + " = " + init)
            i = resume
        else:
            out.append(decl + " = ")
            i = m.end()
    return "".join(out)


# --------------------------------------------------------------------------
# Namespaces
# --------------------------------------------------------------------------

_NAMESPACE = re.compile(r"(?<![\w.])namespace\s+(\w+)\s*\{")
_USING_NS = re.compile(r"(?<![\w.])using\s+namespace\s+(\w+)\s*;")


_NAMED_CAST = re.compile(
    r"(?<![\w.])(static_cast|reinterpret_cast|const_cast)\s*<")


def resolve_casts(text, path="<cpp>", blank=None):
    """Rewrite `static_cast<T>(e)` and friends to the C cast `((T)(e))`.

    All three mean the same thing once the types are gone, which is what
    C has: the distinctions between them are checks the C++ front end
    performs, and none survives lowering. `const_cast` keeps the cast
    rather than dropping it, because the qualifier is still written on
    the operand and casting it away is the whole point of the call.

    `dynamic_cast` is not here. It is refused in `_check_unsupported`
    for wanting RTTI, and rewriting it to a C cast would turn a
    diagnostic into a silent wrong answer -- the one trade this pass
    never makes.
    """
    scan = blank if blank is not None else text
    out, i = [], 0
    while True:
        m = _NAMED_CAST.search(scan, i)
        if m is None:
            out.append(text[i:])
            break
        lt = m.end() - 1
        gt = _match(scan, lt, "<", ">")
        if gt is None:
            raise AutoError(
                "%s:%d: `%s<` has no closing `>`"
                % (path, _line_of(text, m.start()), m.group(1)))
        op = gt + 1
        while op < len(scan) and scan[op].isspace():
            op += 1
        if op >= len(scan) or scan[op] != "(":
            raise AutoError(
                "%s:%d: `%s<..>` is not followed by a parenthesised "
                "operand" % (path, _line_of(text, m.start()), m.group(1)))
        close = _match(scan, op, "(", ")")
        if close is None:
            raise AutoError(
                "%s:%d: `%s<..>(` has no closing `)`"
                % (path, _line_of(text, m.start()), m.group(1)))
        ty = text[lt + 1:gt].strip()
        operand = text[op + 1:close]
        out.append(text[i:m.start()])
        out.append("((%s)(%s))" % (ty, operand))
        i = close + 1
    return "".join(out)


def resolve_namespaces(text, path="<cpp>", blank=None):
    """Flatten `namespace N { .. }` and `N::x` into `N_x`.

    The same thing Crust does with Rust paths, and for the same reason: C has
    one namespace, so a qualified name has to become an unqualified one, and
    joining the parts with an underscore keeps the generated C readable and
    keeps two references to one name agreeing.

    Nesting works -- `namespace a { namespace b { .. } }` gives `a_b_x` --
    and `using namespace N;` makes the unqualified spellings visible by
    rewriting the names this file declared in `N`.

    What this deliberately does *not* do is overload resolution or
    argument-dependent lookup. Two names that differ only by namespace and
    collide after flattening are reported rather than silently merged, which
    is the one way this could quietly change a program's meaning.
    """
    scan = blank if blank is not None else text
    if not _NAMESPACE.search(scan) and not _USING_NS.search(scan):
        return text
    produced = set()

    # Out-of-line definitions -- `void litehtml::css::parse_selectors(..)`
    # -- look their bodies up in the enclosing namespace. That is done once,
    # here, before the loop below starts rewriting declarators and bodies:
    # every name a namespace declares has to be in hand at the same time,
    # since a body in one header names a class from another, and the `ns::`
    # on the declarator does not survive the first block that touches it.
    ns_names = {}
    for _m in _NAMESPACE.finditer(scan):
        _open = scan.index("{", _m.start())
        _close = _match(scan, _open, "{", "}")
        if _close is None:
            continue
        ns_names.setdefault(_m.group(1), set()).update(
            _declared_in(scan[_open + 1:_close]))
    for _ns in sorted(ns_names):
        text = _rename_in_qualified_defs(text, _blank_like(text), _ns,
                                         ns_names[_ns])
    scan = _blank_like(text)

    # Innermost first, so an inner block is flattened before the outer one
    # renames through it.
    while True:
        best = None
        for m in _NAMESPACE.finditer(scan):
            open_idx = scan.index("{", m.start())
            close = _match(scan, open_idx, "{", "}")
            if close is None:
                raise AutoError("%s:%d: unterminated `namespace %s`"
                                % (path, _line_of(text, m.start()),
                                   m.group(1)))
            if not _NAMESPACE.search(scan[open_idx + 1:close]):
                best = (m, open_idx, close)
                break
        if best is None:
            break
        m, open_idx, close = best
        ns = m.group(1)
        body, body_scan = text[open_idx + 1:close], scan[open_idx + 1:close]
        # Everything the namespace declares, not just this block. A namespace
        # is one scope however many times it is reopened, so a class in one
        # header may derive from a class in another -- and renaming only what
        # this block declares left `class html_tag : public element` pointing
        # at a name that no longer existed.
        declared = _declared_in(body_scan)
        # Flattening is name-mangling, not lookup: if `N_x` is already taken
        # by something declared outside, the two become one symbol. The C
        # front end would report the redefinition, but the *call sites* merge
        # before that -- `geo_twice(1)` and `geo::twice(1)` would already be
        # calling the same function -- so it is caught here instead.
        outside = _declared_in(scan[:m.start()] + scan[close + 1:])
        for name in sorted(declared):
            target = "%s_%s" % (ns, name)
            # A namespace may be reopened, and a project with one per header
            # does exactly that -- forty times, in litehtml's case. A name
            # this pass produced from an earlier block of the same namespace
            # is the same entity, not a collision; only a name the *author*
            # spelled `N_x` is.
            if target in outside and target not in produced:
                raise AutoError(
                    "%s:%d: flattening `%s::%s` gives `%s`, which this file "
                    "already declares. C has one namespace, so the two would "
                    "become one symbol. Rename one of them."
                    % (path, _line_of(text, m.start()), ns, name, target))
        # Names already flattened -- by this pass, when an earlier block of
        # the same namespace declared one and pushed the rename outward -- are
        # left alone. A forward declaration in one header and the definition
        # in another is exactly that shape, and prefixing again gave
        # `litehtml_litehtml_html_tag`.
        renaming = [name for name in declared
                    if not (name.startswith(ns + "_") and name in produced)]
        pat = _flatten_pattern(renaming)
        if pat is not None:
            body = _sub_flattened(body, _blank_like(body), pat, ns)
        produced.update("%s_%s" % (ns, name) for name in renaming)
        text = text[:m.start()] + body + text[close + 1:]
        scan = _blank_like(text)
        # A qualified reference from outside, and any `using` for it.
        #
        # Every name the namespace declares anywhere, not just this block.
        # A qualified name whose block has not been flattened yet is still a
        # name of this namespace, and `_sub_qualified` drops the `ns::` from
        # anything it is not told to rename -- so the first block processed
        # was stripping the qualifier off names belonging to later ones.
        # `litehtml::element_js_object_ref` reached the C front end as a bare
        # `element_js_object_ref` while every registry held
        # `litehtml_element_js_object_ref`, and nothing could resolve it.
        # `ns_names` is the union, already collected above for exactly this
        # reason; the in-block rename below stays per-block.
        _union = declared | ns_names.get(ns, set())
        text = _sub_qualified(text, _blank_like(text), ns, _union)
        # Anything just rewritten is a name *this pass* produced, so record
        # it. Otherwise the collision guard above sees the flattened spelling
        # when the block that declares it comes round, reads it as a name the
        # author spelled `N_x`, and reports a clash of the entity with
        # itself.
        produced.update("%s_%s" % (ns, n) for n in _union)
        scan = _blank_like(text)
        # And the *unqualified* references from the namespace's other blocks.
        # They share this scope, so a class in one header derives from a
        # class in another by its bare name; once this block is flattened,
        # that name is only reachable as `N_x`.
        text = _rename_in_blocks(text, _blank_like(text), ns, declared)
        scan = _blank_like(text)

    # `using namespace N;` -- the names are already `N_x`, so an unqualified
    # spelling has to be pointed at one. All of them are collected first:
    # two `using`s that both provide a name make it ambiguous, which C++
    # rejects, and taking whichever came first would silently pick one.
    opened = []
    while True:
        m = _USING_NS.search(scan)
        if m is None:
            break
        opened.append((m.group(1), _line_of(text, m.start())))
        text = text[:m.start()] + text[m.end():]
        scan = _blank_like(text)
    if not opened:
        return text
    provides = {}
    for ns, line in opened:
        for name in set(re.findall(r"\b%s(\w+)\b" % re.escape(ns + "_"),
                                   scan)):
            provides.setdefault(name, []).append(ns)
    for name in sorted(provides):
        if len(provides[name]) > 1:
            raise AutoError(
                "%s: `%s` is provided by `using namespace` on more than one "
                "of %s, so an unqualified use of it is ambiguous. Qualify it."
                % (path, name, " and ".join("`%s`" % n
                                            for n in sorted(provides[name]))))
    for name in sorted(provides, key=len, reverse=True):
        text = _sub_name(text, _blank_like(text), name,
                         provides[name][0] + "_" + name)
    return text


def _blank_braced(text):
    """Blank everything inside braces, preserving length and newlines.

    What a namespace declares is what sits at *its* scope: a class's members
    and a function's locals are not namespace names, and prefixing them
    renamed `Point::x` to `geo_x` and broke every use of it.
    """
    # Only the braces themselves need looking at: the runs between them are
    # either kept whole or blanked whole, so they are handled by the slice
    # rather than a character at a time. This runs over the whole file once
    # per namespace block, and litehtml reopens its namespace forty times.
    out, depth, last = [], 0, 0
    for m in _BRACE.finditer(text):
        i = m.start()
        chunk = text[last:i]
        out.append(_NOT_NEWLINE.sub(" ", chunk) if depth > 0 else chunk)
        out.append(text[i])
        last = i + 1
        if text[i] == "{":
            depth += 1
        else:
            depth = max(0, depth - 1)
    chunk = text[last:]
    out.append(_NOT_NEWLINE.sub(" ", chunk) if depth > 0 else chunk)
    return "".join(out)


_TEMPLATE_HEAD = re.compile(r"template\s*<[^<>]*>")


def _rename_in_qualified_defs(text, scan, ns, names):
    """Rewrite `names` to `ns_name` inside out-of-line definitions of `ns`.

    litehtml writes almost every definition as

        void litehtml::css::parse_selectors(..) { .. css_selector .. }

    rather than reopening `namespace litehtml { }`. C++ looks names up in
    the enclosing namespace of the qualified declarator, so the unqualified
    `css_selector` in that body is `litehtml::css_selector` -- but nothing
    here modelled that, so the body kept a bare name that no longer existed
    once the namespace was flattened.

    It only surfaced where something demanded a *known* class: `new
    css_selector` reported a class not defined in this file, while the same
    expression inside `namespace litehtml { }` a few files over lowered
    fine. Every other unqualified reference in these bodies was equally
    unflattened and simply had not been asked about yet.

    A definition is recognised by its *class*, not by a `ns::` prefix. The
    prefix is unreliable: by the time the block declaring `css_selector` is
    reached, an earlier block has already been over this declarator and
    `litehtml::css::parse_selectors` reads plainly as `css::parse_selectors`.
    The class name survives that, so `X::f(..) {` with `X` declared in `ns`
    is what identifies the body -- which is also the rule C++ itself uses.

    Called once with every name `ns` declares, before the flattening loop
    starts rewriting either the declarators or the bodies.
    """
    if not names:
        return text
    flat = _flatten_pattern(n for n in names if not n.startswith(ns + "_"))
    if flat is None:
        return text
    head = re.compile(r"(?<![\w:])(?:%s\s*::\s*)?(\w+)\s*::\s*~?\w+\s*\("
                      % re.escape(ns))
    pos = 0
    while True:
        m = head.search(scan, pos)
        if m is None:
            return text
        pos = m.end()
        if m.group(1) not in names:
            continue                     # not a class this namespace owns
        close = _match(scan, m.end() - 1, "(", ")")
        if close is None:
            continue
        j = close + 1
        # `const`, `noexcept`, an initialiser list -- skip to the brace.
        while j < len(scan) and scan[j] not in "{;":
            j += 1
        if j >= len(scan) or scan[j] != "{":
            continue                     # a declaration, not a definition
        end = _match(scan, j, "{", "}")
        if end is None:
            continue
        def _flatten(src):
            return _sub_flattened(src, _blank_like(src), flat, ns)

        # The parameter list as well as the body. A parameter written
        # `const std::shared_ptr<media_query_list>& media` in an out-of-line
        # declarator names the class unflattened, so it monomorphised to
        # `shared_ptr_media_query_list` while the same type named inside the
        # namespace became `shared_ptr_litehtml_media_query_list` -- two
        # instantiations of one template, and a copy between them looked
        # like a copy between unrelated classes.
        op = m.end() - 1
        params = text[op + 1:close]
        new_params = _flatten(params)
        body = text[j + 1:end]
        new_body = _flatten(body)
        if new_params != params or new_body != body:
            text = (text[:op + 1] + new_params + text[close:j + 1]
                    + new_body + text[end:])
            scan = _blank_like(text)
            pos = j + 1                  # offsets are stable: same length
    return text


def _rename_in_blocks(text, scan, ns, names):
    """Rewrite `names` to `ns_name` inside every other block of `ns`."""
    if not names:
        return text
    flat = _flatten_pattern(n for n in names if not n.startswith(ns + "_"))
    if flat is None:
        return text
    while True:
        hit = None
        for m in _NAMESPACE.finditer(scan):
            if m.group(1) != ns:
                continue
            open_idx = scan.index("{", m.start())
            close = _match(scan, open_idx, "{", "}")
            if close is None:
                continue
            body = text[open_idx + 1:close]
            new = _sub_flattened(body, _blank_like(body), flat, ns)
            if new != body:
                hit = (open_idx, close, new)
                break
        if hit is None:
            return text
        open_idx, close, new = hit
        text = text[:open_idx + 1] + new + text[close:]
        scan = _blank_like(text)


def _declared_in(body_scan):
    """Names a namespace body declares: types, functions, and variables."""
    # A template parameter list is not a declaration of anything the
    # namespace owns, and `template<class T>` reads exactly like a class
    # declaration to the scan below. litehtml has one, and flattening turned
    # every `T` in the template into `litehtml_T` -- including the `operator
    # T()` that first made this visible.
    body_scan = _TEMPLATE_HEAD.sub(lambda m: " " * (m.end() - m.start()),
                                   body_scan)
    out = set()
    for m in re.finditer(r"\b(?:class|struct|enum|union)\s+(\w+)",
                         body_scan):
        out.add(m.group(1))
    body_scan = _blank_braced(body_scan)
    for m in re.finditer(r"\b(?:class|struct|enum|union)\s+(\w+)", body_scan):
        out.add(m.group(1))
    for m in _anchored_finditer(_DECLARATOR, body_scan):
        ret, extra = _split_declarator(m.group(1))
        if ret and extra is None and m.group(2) not in _KEYWORDS:
            out.add(m.group(2))
    for m in _anchored_finditer(_FIELD, body_scan):
        ret, extra = _split_declarator(m.group(1))
        if ret and extra is None and m.group(2) not in _KEYWORDS:
            out.add(m.group(2))
    return out - _KEYWORDS


def _sub_qualified(text, scan, ns, renamed=None):
    """`N::x` -> `N_x` for the names that flattening actually renamed.

    Only those. The qualification says which namespace to look in, not
    what the name became, and this pass does not rename everything a
    namespace contains -- a typedef is left with the name it was given,
    deliberately, so that `tstring` stays readable in the generated C.

    Rewriting `N::x` regardless produced a name nothing declared.
    litehtml writes `litehtml::tstring` in fourteen places while its
    typedef stays `tstring`, so every use became `litehtml_tstring` and
    the declaration did not follow -- which is how a file could translate
    clean and then fail to compile on a type that appears nowhere. Where
    the name was not renamed, the qualification is simply dropped, which
    is what flattening a namespace means for a name that keeps its
    spelling.
    """
    out, last = [], 0
    for m in re.finditer(r"(?<![\w.])%s\s*::\s*(\w+)" % re.escape(ns),
                         scan):
        name = m.group(1)
        out.append(text[last:m.start()])
        if renamed is None or name in renamed:
            out.append("%s_%s" % (ns, name))
        else:
            out.append(name)
        last = m.end()
    out.append(text[last:])
    return "".join(out)


# --------------------------------------------------------------------------
# `= default` and `= delete`
# --------------------------------------------------------------------------

_DEFAULTED = re.compile(r"\)\s*(?:const\s*)?=\s*(default|delete)\s*;")


def resolve_defaulted(text, path="<cpp>", blank=None):
    """Rewrite `= default` to an empty body and drop `= delete` members.

    `~T() = default;` asks for the destructor the compiler would have
    written, which here is the member epilogue -- and that is appended to
    whatever body the member has, so an empty one gives exactly it. Rewriting
    rather than dropping the member keeps `virtual` attached, which decides
    whether the class gets a vtable slot.

    `= delete` asks for the member *not* to exist, and a member this pass
    never sees does not. Dropping it lands on the right behaviour for the
    case that matters: a deleted copy constructor leaves a class with a
    destructor and no copy constructor, which the Rule of Three check already
    refuses to copy, with a diagnostic that names the fix.
    """
    scan = blank if blank is not None else text
    if "=" not in scan:
        return text
    out, i = [], 0
    while True:
        m = _DEFAULTED.search(scan, i)
        if m is None:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i:m.start()])
        if m.group(1) == "default":
            out.append(") { }")
        else:
            # Drop the whole declaration, back to the start of the member.
            head = out.pop() if out else ""
            cut = max(head.rfind(";"), head.rfind("{"), head.rfind("}"),
                      head.rfind(":"))
            out.append(head[:cut + 1])
        i = m.end()


def resolve_using_alias(text, path="<cpp>", blank=None):
    """`using Y = X;` -> `typedef X Y;`.

    An alias declaration is C++11 spelling for a typedef, and C has only the
    typedef. Rewritten rather than resolved away, so the alias keeps its name
    in the generated C and stays readable. `using namespace N;` has no `=`
    and is left for the namespace pass.
    """
    scan = blank if blank is not None else text
    if "using" not in scan:
        return text
    out, last = [], 0
    for m in _USING_ALIAS.finditer(scan):
        out.append(text[last:m.start()])
        out.append("typedef %s %s;" % (text[m.start(2):m.end(2)].strip(),
                                       m.group(1)))
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def resolve_aliases(text, path="<cpp>", blank=None):
    """Replace file-scope type aliases with what they name.

    Threading an alias map through every consumer was not enough: a field
    declared `int_vector items;` records its type by spelling, and so does a
    local, and so does a parameter. Substituting once here means every pass
    below sees the class the alias stood for without knowing aliases exist.

    A typedef keeps the *name* it declares -- rewriting
    `typedef vector_int int_vector;` in place would give
    `typedef vector_int vector_int;` -- so the alias keeps its name in the
    generated C and the header stays readable. Only that name is held
    back, not the whole declaration: an alias built on another alias,
    `typedef vector<tstring> string_vector;`, has one to resolve on its
    right-hand side, and skipping the declaration whole was how a template
    came to be monomorphised over the alias rather than over what it names
    -- `vector_tstring` instead of `vector_string`.

    Depth zero only, for the reason `_scan_typedefs` gives: a class-scoped
    `typedef .. vector;` must not be allowed to mean `vector` everywhere.
    """
    scan = blank if blank is not None else text
    aliases = _scan_typedefs(scan)
    # A template parameter shadows a file-scope alias of the same name, and
    # inside the template the parameter is what the name means. Substituting
    # there turned `template<typename A, typename B> class Two { A a; B b; }`
    # against `typedef int B;` into a class with two `int` fields. Excluded
    # wholesale rather than scoped: that falls back to leaving the alias
    # alone, which is what happened before this pass existed.
    shadowed = set()
    for m in re.finditer(r"template\s*<([^<>]*)>", scan):
        for part in m.group(1).split(","):
            words = part.split()
            if words:
                shadowed.add(words[-1])
    for name in shadowed:
        aliases.pop(name, None)
    if not aliases:
        return text
    for _round in range(8):
        # The declared name only: everything else in a typedef is a type
        # to resolve like any other.
        spans = []
        for td in _TYPEDEF.finditer(scan):
            nm = None
            for w in re.finditer(r"\w+", td.group(1)):
                nm = w
            if nm:
                base = td.start(1)
                spans.append((base + nm.start(), base + nm.end()))
        out, last, changed = [], 0, False
        pat = re.compile(r"(?<![\w.>])(%s)(?![\w])"
                         % "|".join(re.escape(a) for a in sorted(aliases)))
        for m in pat.finditer(scan):
            if any(a <= m.start() < b for a, b in spans):
                continue                 # the declaration of the alias
            target = _resolve_alias(m.group(1), aliases)
            if target.strip() == m.group(1):
                continue
            out.append(text[last:m.start()])
            out.append(target)
            last = m.end()
            changed = True
        out.append(text[last:])
        text = "".join(out)
        scan = _blank_like(text)
        if not changed:
            break
    return text


# --------------------------------------------------------------------------
# Nested classes
# --------------------------------------------------------------------------

_CLASS_HEAD = re.compile(
    r"(?<![\w.])(class|struct)\s+(\w+)(?:\s+final)?\s*(?::[^{;]*)?\{")


def resolve_nested_classes(text, path="<cpp>", blank=None):
    """Hoist `class Outer { struct Inner { .. }; }` to a top-level class.

    The same thing namespaces get, and for the same reason: C has one flat
    namespace of struct tags, so a name scoped to a class has to become an
    unqualified one. `Outer::Inner` and a bare `Inner` written inside `Outer`
    both become `Outer_Inner`.

    Hoisted *before* the enclosing class rather than after: the outer class
    may hold one by value, and a by-value member needs its type complete
    above it.

    Innermost first, so `A { B { C { } } }` gives `A_B_C` and the enclosing
    rewrites see a body with no nesting left in it.
    """
    scan = blank if blank is not None else text
    for _round in range(32):
        found = None
        for m in _CLASS_HEAD.finditer(scan):
            open_idx = scan.index("{", m.start())
            close = _match(scan, open_idx, "{", "}")
            if close is None:
                continue
            body = scan[open_idx + 1:close]
            inner = _CLASS_HEAD.search(body)
            if inner is None:
                continue
            # Innermost: if this nested one itself nests, do that one first.
            i_open = open_idx + 1 + body.index("{", inner.start())
            i_close = _match(scan, i_open, "{", "}")
            if i_close is None:
                continue
            if _CLASS_HEAD.search(scan[i_open + 1:i_close]):
                continue
            found = (m, open_idx, close, inner, i_open, i_close)
            break
        if found is None:
            return text
        m, open_idx, close, inner, i_open, i_close = found
        outer, iname = m.group(2), inner.group(2)
        new_name = "%s_%s" % (outer, iname)

        # The nested declaration runs to the `;` after its closing brace.
        end = i_close + 1
        while end < len(scan) and scan[end] in " \t\r\n":
            end += 1
        if end < len(scan) and scan[end] == ";":
            end += 1
        i_start = open_idx + 1 + inner.start()
        decl = text[i_start:end]
        decl = _sub_name(decl, _blank_like(decl), iname, new_name)

        # Remove it from the body, then rename what is left to point at it.
        rest = text[:i_start] + text[end:]
        rest_scan = _blank_like(rest)
        rest = _sub_qualified_class(rest, rest_scan, outer, iname, new_name)
        rest_scan = _blank_like(rest)
        # A bare `Inner` inside `Outer` means `Outer::Inner`. The outer body
        # has shrunk by what was cut, so its span is recomputed.
        om = _CLASS_HEAD.search(rest_scan, max(0, m.start() - 1))
        if om is not None:
            o_open = rest_scan.index("{", om.start())
            o_close = _match(rest_scan, o_open, "{", "}")
            if o_close is not None:
                obody = rest[o_open + 1:o_close]
                obody = _sub_name(obody, _blank_like(obody), iname, new_name)
                rest = rest[:o_open + 1] + obody + rest[o_close:]
        # Out-of-line definitions of the enclosing class see the nested name
        # unqualified too: `void element::create_js_object()` writes
        # `new js_object_ref(..)`, and C++ looks that up in `element`. The
        # body is not lexically inside the class, so the rename above never
        # reached it and the allocation named a class that does not exist.
        #
        # The declarator may spell the class either way -- namespace
        # flattening renames the class but leaves an out-of-line head alone
        # -- so a suffix match is accepted as well.
        rest_scan = _blank_like(rest)
        _head = re.compile(r"(?<![\w:])(\w+)\s*::\s*~?\w+\s*\(")
        _pos = 0
        while True:
            hm = _head.search(rest_scan, _pos)
            if hm is None:
                break
            _pos = hm.end()
            if hm.group(1) != outer and not outer.endswith("_" + hm.group(1)):
                continue
            hclose = _match(rest_scan, hm.end() - 1, "(", ")")
            if hclose is None:
                continue
            bj = hclose + 1
            while bj < len(rest_scan) and rest_scan[bj] not in "{;":
                bj += 1
            if bj >= len(rest_scan) or rest_scan[bj] != "{":
                continue
            bend = _match(rest_scan, bj, "{", "}")
            if bend is None:
                continue
            span = rest[hm.end():bend]
            new_span = _sub_name(span, _blank_like(span), iname, new_name)
            if new_span != span:
                rest = rest[:hm.end()] + new_span + rest[bend:]
                rest_scan = _blank_like(rest)

        # Hoist above the enclosing class.
        at = rest.rfind("\n", 0, m.start()) + 1
        text = rest[:at] + decl.strip() + "\n" + rest[at:]
        scan = _blank_like(text)
    raise AutoError("%s: classes nested more than 32 deep" % path)


def _sub_qualified_class(text, scan, outer, iname, new_name):
    """`Outer::Inner` -> `Outer_Inner`."""
    out, last = [], 0
    for m in re.finditer(r"(?<![\w.])%s\s*::\s*%s(?![\w])"
                         % (re.escape(outer), re.escape(iname)), scan):
        out.append(text[last:m.start()])
        out.append(new_name)
        last = m.end()
    out.append(text[last:])
    return "".join(out)


# --------------------------------------------------------------------------
# Default arguments
# --------------------------------------------------------------------------

def resolve_default_arguments(text, path="<cpp>", blank=None):
    """Expand `f(A a, B b = e)` into one member per callable arity.

    Overloads are resolved by argument *count* in this subset, so a default
    argument is not a spelling -- it is several members that happen to share
    a body. They are written out:

        f(A a, B b, C c) { body }
        f(A a, B b)      { C c = e2; body }
        f(A a)           { B b = e1; C c = e2; body }

    The shorter forms declare the missing parameters as locals holding their
    defaults, which is exactly what the caller would have passed. Delegating
    to the longest form would be tidier, but delegating constructors are not
    in the subset either, and this works for a constructor and a method
    alike.

    Only members with a body. A declaration whose definition is out of line
    has nothing here to copy, and is left for the reference-return-style
    report rather than half-expanded.
    """
    scan = blank if blank is not None else text
    if "=" not in scan:
        return text
    out, i = [], 0
    while True:
        m = _DEFAULTED_PARAMS.search(scan, i)
        if m is None:
            out.append(text[i:])
            return "".join(out)
        op = scan.index("(", m.start())
        cp = _match(scan, op, "(", ")")
        if cp is None:
            out.append(text[i:m.end()])
            i = m.end()
            continue
        parts = _split_top(text[op + 1:cp])
        split = [_split_default(p) for p in parts]
        if not any(d is not None for _n, d in split):
            out.append(text[i:cp + 1])
            i = cp + 1
            continue
        # The body has to follow, with nothing but qualifiers between.
        brace = scan.find("{", cp)
        between = scan[cp + 1:brace] if brace >= 0 else ";"
        if brace < 0 or ";" in between or "}" in between:
            out.append(text[i:cp + 1])
            i = cp + 1
            continue
        close = _match(scan, brace, "{", "}")
        if close is None:
            out.append(text[i:cp + 1])
            i = cp + 1
            continue
        head = text[m.start():op]
        body = text[brace + 1:close]
        first = next(k for k, (_n, d) in enumerate(split) if d is not None)
        forms = []
        for keep in range(len(split), first - 1, -1):
            sig = ", ".join(n for n, _d in split[:keep])
            pre = "".join(" %s = %s;" % (split[k][0].strip(), split[k][1])
                          for k in range(keep, len(split)))
            forms.append("%s(%s) {%s%s}" % (head.rstrip(), sig, pre, body))
        out.append(text[i:m.start()])
        out.append("\n".join(forms))
        i = close + 1


_DEFAULTED_PARAMS = re.compile(
    r"(?:(?<=[;{}:\n])|\A)\s*(?:explicit\s+)?"
    r"(?:[A-Za-z_][\w:]*(?:\s*<[^;{}()]*>)?[\s*&]+)*"
    r"~?[A-Za-z_]\w*\s*\([^;{}()]*=[^;{}()]*\)")


def _split_default(part):
    """`(declarator, default)` for one parameter, `default` None if absent."""
    eq = -1
    depth = 0
    for k, c in enumerate(part):
        if c in "([{<":
            depth += 1
        elif c in ")]}>":
            depth -= 1
        elif c == "=" and depth == 0 and part[k + 1:k + 2] != "=" \
                and part[k - 1:k] not in ("=", "!", "<", ">"):
            eq = k
            break
    if eq < 0:
        return part.strip(), None
    return part[:eq].strip(), part[eq + 1:].strip()
