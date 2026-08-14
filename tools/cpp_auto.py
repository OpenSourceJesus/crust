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

import re


class AutoError(Exception):
    """Raised when `auto` cannot be resolved. Message names the fix."""

    def __init__(self, message):
        Exception.__init__(self, message)
        self.message = message


# `auto` in a declaration, with the qualifiers it is allowed to carry. The
# initialiser has to be an `=` form: `auto x(1);` is a declaration whose type
# is the thing being deduced, which reads as a call and is not worth the
# ambiguity.
_AUTO_DECL = re.compile(
    r"(?<![\w.])(?:(const)\s+)?auto\s*(\*|&)?\s*(\w+)\s*=\s*")

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

_QUALIFIERS = frozenset(("static", "inline", "virtual", "const",
                         "explicit", "mutable", "extern"))


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
        for d in _INDEXER.finditer(body):
            ret, extra = _split_declarator(d.group(1))
            if ret and extra is None:
                methods.setdefault(cname, {})["[]"] = ret
        for d in _DECLARATOR.finditer(body):
            ret, name = _split_declarator(d.group(1))
            if ret and name is None and d.group(2):
                mm[d.group(2)] = ret
        for d in _FIELD.finditer(body):
            ret, name = _split_declarator(d.group(1))
            if ret and name is None and d.group(2):
                ff[d.group(2)] = ret
    return names, methods, fields, tparams


def _scan_functions(scan):
    """`{name: return type}` for functions declared or defined at file scope."""
    out = {}
    for m in _DECLARATOR.finditer(scan):
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
            r"(?:^|[;{}(,])\s*(?:const\s+|static\s+)*"
            r"([A-Za-z_][\w:]*(?:\s*<[^;{}()<>]*>)?)\s*(\*+|&)?\s*"
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


def _deduce(expr, ctx, where):
    """The written type of `expr`, or raise `AutoError` naming why not."""
    classes, methods, fields, tparams, funcs, vars_ = ctx
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
    m = re.match(r"^([A-Za-z_]\w*)\s*(?:\.|->)\s*(\w+)\s*\(", e)
    if m and _match(e, e.index("("), "(", ")") == len(e) - 1:
        recv, meth = m.group(1), m.group(2)
        rty = vars_.get(recv)
        if rty:
            base = re.sub(r"[*&\s]+$", "", rty).split("<")[0].strip()
            ret = methods.get(base, {}).get(meth)
            if ret:
                return ret
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
        rty = vars_.get(m.group(1))
        if rty:
            base = re.sub(r"[*&\s]+$", "", rty).split("<")[0].strip()
            fty = fields.get(base, {}).get(m.group(2))
            if fty:
                return fty

    # A plain name.
    if re.match(r"^[A-Za-z_]\w*$", e):
        if e in vars_:
            return vars_[e]
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
    classes, methods, fields, tparams, funcs, vars_ = ctx
    ty = vars_.get(name)
    if not ty:
        return None
    ty = ty.strip()
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
    params = tparams.get(base) or []
    if params and args:
        actual = [a.strip() for a in _split_top(args)]
        for pname, aval in zip(params, actual):
            ret = re.sub(r"(?<![\w])%s(?![\w])" % re.escape(pname), aval, ret)
    return ret.strip()


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


_RANGE_FOR = re.compile(
    r"(?<![\w.])for\s*\(\s*(?:(const)\s+)?"
    r"(auto|[A-Za-z_][\w:]*(?:\s*<[^;()]*>)?)\s*(&)?\s*(\w+)\s*:\s*")


def _array_len(name, scan):
    """The written length of array `name`, or None."""
    m = re.search(r"(?<![\w.])[A-Za-z_][\w:]*[\s*]+%s\s*\[\s*([^\]]*?)\s*\]"
                  % re.escape(name), scan)
    if m is None:
        return None
    return m.group(1).strip() or None


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
    classes, methods, _f, _t = _scan_classes(scan)
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
        if not re.match(r"^[A-Za-z_]\w*$", rng):
            raise AutoError(
                "%s: a range-`for` here needs a named array or container "
                "(optionally through a pointer); `%s` is an expression, and "
                "this pass reads the length from how the range is written. "
                "Assign it to a name first."
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
        vt = _declared_types(scan).get(rng, "")
        cm = re.match(r"^([A-Za-z_]\w*)", vt.strip().lstrip("*"))
        if cm and cm.group(1) in classes:
            cls = cm.group(1)
        if alen is None and (cls is None
                             or "size" not in methods.get(cls, {})
                             or "[]" not in methods.get(cls, {})):
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
        elem = "%s[%s]" % (rng, it)
        body = text[body_open + 1:body_close]
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


def _blank_like(text):
    """A same-length copy with comment and literal bodies blanked."""
    out, i, n = list(text), 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            q, j = c, i + 1
            while j < n and text[j] != q:
                j += 2 if text[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = min(j + 1, n)
        elif c == "/" and text[i:i + 2] == "//":
            j = text.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c == "/" and text[i:i + 2] == "/*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        else:
            i += 1
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


def resolve(text, path="<cpp>", blank=None):
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

    out, i = [], 0
    while True:
        m = _AUTO_DECL.search(scan, i)
        if m is None:
            out.append(text[i:])
            break
        end = _end_of_init(scan, m.end())
        if end is None:
            raise AutoError(
                "%s:%d: `auto %s = ..` has no `;` to end it"
                % (path, _line_of(text, m.start()), m.group(3)))
        where = "%s:%d" % (path, _line_of(text, m.start()))
        init = text[m.end():end]
        ctx = (classes, methods, fields, tparams, funcs, vars_)
        ty = _deduce(init, ctx, where)
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
            i = end                      # the `;` is copied through below
        else:
            out.append(decl + " = ")
            i = m.end()
    return "".join(out)


# --------------------------------------------------------------------------
# Namespaces
# --------------------------------------------------------------------------

_NAMESPACE = re.compile(r"(?<![\w.])namespace\s+(\w+)\s*\{")
_USING_NS = re.compile(r"(?<![\w.])using\s+namespace\s+(\w+)\s*;")


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
        declared = _declared_in(body_scan)
        # Flattening is name-mangling, not lookup: if `N_x` is already taken
        # by something declared outside, the two become one symbol. The C
        # front end would report the redefinition, but the *call sites* merge
        # before that -- `geo_twice(1)` and `geo::twice(1)` would already be
        # calling the same function -- so it is caught here instead.
        outside = _declared_in(scan[:m.start()] + scan[close + 1:])
        for name in sorted(declared):
            target = "%s_%s" % (ns, name)
            if target in outside:
                raise AutoError(
                    "%s:%d: flattening `%s::%s` gives `%s`, which this file "
                    "already declares. C has one namespace, so the two would "
                    "become one symbol. Rename one of them."
                    % (path, _line_of(text, m.start()), ns, name, target))
        for name in sorted(declared, key=len, reverse=True):
            body = _sub_name(body, _blank_like(body), name, ns + "_" + name)
            body_scan = _blank_like(body)
        text = text[:m.start()] + body + text[close + 1:]
        scan = _blank_like(text)
        # A qualified reference from outside, and any `using` for it.
        text = _sub_qualified(text, _blank_like(text), ns)
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
    out, depth = list(text), 0
    for i, ch in enumerate(text):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                depth = 0
        elif depth > 0 and out[i] != "\n":
            out[i] = " "
    return "".join(out)


def _declared_in(body_scan):
    """Names a namespace body declares: types, functions, and variables."""
    out = set()
    for m in re.finditer(r"\b(?:class|struct|enum|union)\s+(\w+)",
                         body_scan):
        out.add(m.group(1))
    body_scan = _blank_braced(body_scan)
    for m in re.finditer(r"\b(?:class|struct|enum|union)\s+(\w+)", body_scan):
        out.add(m.group(1))
    for m in _DECLARATOR.finditer(body_scan):
        ret, extra = _split_declarator(m.group(1))
        if ret and extra is None and m.group(2) not in _KEYWORDS:
            out.add(m.group(2))
    for m in _FIELD.finditer(body_scan):
        ret, extra = _split_declarator(m.group(1))
        if ret and extra is None and m.group(2) not in _KEYWORDS:
            out.add(m.group(2))
    return out - _KEYWORDS


def _sub_qualified(text, scan, ns):
    """`N::x` -> `N_x`, outside comments and literals."""
    out, last = [], 0
    for m in re.finditer(r"(?<![\w.])%s\s*::\s*(\w+)" % re.escape(ns),
                         scan):
        out.append(text[last:m.start()])
        out.append("%s_%s" % (ns, m.group(1)))
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
