"""cpprust -- a minimal C++ subset, lowered to C.

`#include "foo.cpp"` translates a small C++ dialect in place, the same way
`#include "foo.py"` handles rpython. What the subset buys, and why it is worth
having next to Rust rather than instead of it, is one thing: **destructors**.

Crust has no `Drop`. Scope exit cannot run code, so every allocating type in
its core carries an explicit `free_buf` and the caller has to remember. C++ is
the one of the three languages here whose object model is built around
deterministic destruction, so a C++ class is the natural place to put an RAII
wrapper around a Rust type -- the destructor call is emitted at scope exit, and
the Rust side keeps its explicit API for callers that want it.

The subset, deliberately small:

  * `class` / `struct` with data members and methods
  * constructors and a destructor: a local `Type name(args);` becomes
    `Type_new` at the declaration and `Type_drop` at the closing `}` of the
    enclosing block (inside the `.cpp` only -- the include hook never sees
    the C TU that pulled the file in)
  * `public:` / `private:` / `protected:` labels (parsed, not enforced --
    access control is a compile-time property and this is a lowering, not a
    type checker; pretending to enforce it would be worse than not claiming to)
  * `template<typename T>` on classes, monomorphised on use. Any number of
    parameters (`template<typename K, typename V>`), and a non-type integer
    parameter (`template<typename T, int N>`) works too, because
    monomorphisation is textual substitution and `N` is replaced by the
    literal the use site spelled. Arguments may themselves be
    instantiations (`Holder<Pair<int,char>>`), resolved innermost first, in
    which case the class supplying the argument must be declared above the
    one consuming it -- the same completeness rule a base class obeys. A
    template instantiated only from inside another template's body is
    reported rather than emitted as a dangling name: the scan that
    discovers instantiations cannot see through an unsubstituted parameter.
  * single inheritance, with `virtual` methods and pure virtual (`= 0`)
    declarations. A base is laid out as the first member, so a pointer to a
    derived object already *is* a pointer to its base and upcasting is a
    cast. The vtable pointer sits first in the root of the hierarchy, hence
    at offset zero throughout it, and a derived class's table begins with
    its base's slots -- which is what lets a `Base *` dispatch into a
    derived override. Overrides reached through a table go via a small
    thunk that converts `this`, so the generated table holds no
    function-pointer casts.
  * method call syntax: `g.get()` and `p->get()` become `VecGuard_get(&g)`
    and `VecGuard_get(p)`. Receivers resolve against a scope-tracked symbol
    table -- locals, parameters, and chains through class-typed fields
    (`a.b.get()`). Inside a method, a bare `helper(x)` picks up the implicit
    `this`. Anything that does not resolve to a class is left exactly as
    written, so plain C in the same file is untouched.
  * reference parameters and locals: `T &x` is a pointer the source did not
    have to spell, so it is lowered back to `T *x` and call sites take the
    address. `T &r = e;` becomes `T *r = &(e);`. Member *access* follows the
    same symbol table as a method call, so `c.v` on a lowered reference
    becomes `c->v`, and each step of a chain picks its own operator
    (`o.in.n` on a reference is `o->in.n`, since the member itself is by
    value). A receiver that does not resolve to a class is left alone, so
    plain C struct access is untouched.

A reference *return* (`T& f()`) is rejected rather than lowered. Turning it
into `T*` would silently change what assignment through the result means at
every call site, which is the same failure mode as silently making `virtual`
static.

Only a single receiver is resolved, so `a.get().foo()` -- a method on a
returned value -- is not rewritten and will not compile as C. Detecting it
here would mean flagging `)` followed by `.name(`, which is legitimate C
(`get_ops()->init(x)`), so it is left to the C compiler to reject.

Drops run on every exit from a scope: the closing `}`, and also `return`,
`break` and `continue`. `return` unwinds out to the function, `break` to the
enclosing loop or switch, `continue` to the enclosing loop. A `return` with a
value spills it to a temporary before the destructors run, because C++
evaluates the operand first and `return g.get();` reads the very object about
to be destroyed.

`goto` is rejected when a destructor is pending: where it lands decides what
should have been destroyed, and a lowering that scans forward cannot know
that. With nothing live it is left alone, so plain C is unaffected.

A class-typed member is constructed and destroyed with its container, in
declaration order and reverse declaration order respectively. If a member
needs either and the container declares neither, the container gets an
implicit one, as in C++. A constructor initializer list (`C(int n) : a(n),
k(n) { }`) supplies arguments to a member's constructor, or assigns a scalar
field. A member whose class has no default constructor must appear in the
initializer list -- that is an error rather than a silently unconstructed
object. Pointer and array members are left to the author.

Constructors run base first, then install the vtable pointer, then members,
then the body; destructors run the body, then members in reverse, then the
base. A class with a base, a member, or a vtable that needs either gets an
implicit constructor or destructor. A class with a pure virtual method is
abstract: no table is emitted for it and declaring one by value is an error
rather than an object whose vptr is never set.

`new` and `delete` allocate and destroy a single object. `new T(args)` sits
in expression position and C has no statement expression, so it lowers to a
generated `T__alloc(args)` -- malloc, construct, return -- emitted only for
the classes the source actually applies `new` to. A failed malloc yields
null rather than being constructed through, since the subset has no
exceptions. `delete p` is a statement, so it lowers in place to a guarded
`T_drop(p); free(p);` -- guarded because `delete` on null is a no-op in C++,
and wrapped in `do { } while (0)` so that a delete as a branch's only
statement does not leave a stray `;` before an `else`. The static type of
the operand supplies the destructor, so it must resolve through the symbol
table; a cast or a call is reported rather than guessed at.

Rejected here rather than mistranslated: `new T[n]` and `delete[]`, which
would need the element count recorded beside the allocation; `new` of a
non-class or of an abstract class; `delete` of a by-value object; and
`delete` through a class with a `virtual` destructor, because the vtable
carries methods only and the call could not dispatch -- running the base
destructor and leaking the derived part is the exact bug `virtual ~T()` is
written to prevent.

Not supported, and reported rather than mistranslated: multiple inheritance,
virtual inheritance, exceptions, operator overloading, the STL. Multiple
bases are rejected because the layout admits exactly one: with one base
first, upcasting is free, and that is the property the rest of this
lowering leans on.

The lowering is the same shape Crust uses for `impl` blocks: a method becomes
`Class_method(Class *this, ..)`, a template becomes one struct per
instantiation. That is not a coincidence -- it means a C++ class and a Rust
`impl` over the same data produce the same C, so the two can be mixed in one
unit without a shim.
"""

import os
import re
import sys


class CppError(Exception):
    """A C++ subset translation error."""

    def __init__(self, message):
        self.args = (message,)
        self.message = message


_UNSUPPORTED = ("throw", "try", "catch", "operator",
                "dynamic_cast", "typeid")

# `template<typename T>` / `template<class T, typename U>`. The whole
# parameter list is captured and split separately: the count is not fixed
# here, so a header with two or five parameters is the same shape as one.
_TEMPLATE = re.compile(r"\btemplate\s*<([^<>]*)>")

# One template parameter: `typename T`, `class T`, or a non-type `int N`.
_TPARAM = re.compile(r"^(?:typename|class)\s+(\w+)$")
_TPARAM_NONTYPE = re.compile(r"^(?:int|long|short|char|unsigned|bool|size_t)"
                             r"(?:\s+\w+)*\s+(\w+)$")


def _parse_tparams(inner, where):
    """Split a `template<..>` parameter list into declared parameter names.

    Type parameters (`typename T` / `class T`) and non-type integer
    parameters (`int N`) both lower the same way, because monomorphisation
    here is textual substitution: `N` is replaced by the literal the use
    site spelled, exactly as `T` is replaced by a type name. Anything else
    -- defaults, parameter packs, template template parameters -- is
    reported rather than half-translated.
    """
    names = []
    for part in _split_targs(inner):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            raise CppError("%s: a default template argument is not in the "
                           "C++ subset (`%s`)" % (where, part))
        if "..." in part:
            raise CppError("%s: a template parameter pack is not in the C++ "
                           "subset (`%s`)" % (where, part))
        m = _TPARAM.match(part) or _TPARAM_NONTYPE.match(part)
        if m is None:
            raise CppError("%s: cannot parse template parameter %r"
                           % (where, part))
        names.append(m.group(1))
    if not names:
        raise CppError("%s: empty template parameter list" % where)
    if len(set(names)) != len(names):
        raise CppError("%s: duplicate template parameter name" % where)
    return tuple(names)


def _strip_comments(text):
    """Blank comments, preserving newlines so line numbers hold."""
    out = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(c if c == "\n" else " " for c in text[i:j]))
            i = j
        elif text[i] in "\"'":
            q = text[i]
            j = i + 1
            while j < n and text[j] != q:
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(text[i:j])
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _match_brace(text, open_idx):
    """Index of the `}` closing the `{` at `open_idx`, or None."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _blank_strings(text):
    """Blank string and char literal bodies, preserving length and newlines."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(c)
            out.append("".join(ch if ch == "\n" else " "
                               for ch in text[i + 1:j - 1]))
            out.append(c if j - 1 < n else "")
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _check_unsupported(scan, path):
    # A literal is data, not code: `puts("new item")` uses no keyword.
    scan = _blank_strings(scan)
    for kw in _UNSUPPORTED:
        m = re.search(r"\b%s\b" % kw, scan)
        if m:
            line = scan.count("\n", 0, m.start()) + 1
            raise CppError(
                "%s:%d: `%s` is not in the C++ subset. Supported: classes, "
                "constructors, destructors, and templates."
                % (os.path.basename(path), line, kw))


class Member(object):
    __slots__ = ("kind", "ret", "name", "params", "body", "line", "dim",
                 "init", "virt", "pure")

    def __init__(self, kind, ret, name, params, body, line, dim="",
                 init=None, virt=False, pure=False):
        self.kind = kind          # "field" | "method" | "ctor" | "dtor"
        self.ret = ret
        self.name = name
        self.params = params
        self.body = body
        self.line = line
        self.dim = dim            # array suffix on a field, e.g. "[10]"
        self.init = init or []    # ctor initializer list: [(field, args)]
        self.virt = virt          # declared `virtual`
        self.pure = pure          # `= 0`, so no implementation here


class Class(object):
    __slots__ = ("name", "tparams", "members", "line", "base")

    def __init__(self, name, tparams, members, line, base=None):
        self.name = name
        self.tparams = tparams    # tuple of template parameter names, or ()
        self.base = base          # single base class name, or None
        self.members = members
        self.line = line


_ACCESS = re.compile(r"\b(public|private|protected)\s*:")


def _parse_init_list(tail, sig, cname):
    """Parse `: a(1), b(x)` following a constructor's parameter list."""
    tail = tail.strip()
    if not tail:
        return []
    if not tail.startswith(":"):
        raise CppError("cannot parse %r after %s in class %s"
                       % (tail, sig, cname))
    out = []
    for part in _split_top(tail[1:]):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\w+)\s*\(", part)
        end = _match_paren(part, part.index("(")) if m else None
        if m is None or end is None:
            raise CppError("cannot parse initializer %r in class %s"
                           % (part, cname))
        out.append((m.group(1), part[m.end():end].strip()))
    return out


def _pure_virtual(decl, cname, line0):
    """Parse `virtual int area() = 0;` -- a slot with no implementation."""
    body = decl[len("virtual"):].strip()
    op = body.find("(")
    cp = _match_paren(body, op) if op >= 0 else None
    if op < 0 or cp is None:
        raise CppError("cannot parse virtual member %r in class %s"
                       % (decl, cname))
    tail = body[cp + 1:].strip()
    if not re.match(r"^=\s*0$", tail):
        raise CppError(
            "class %s: `%s` is a virtual declaration without a body; the "
            "subset needs either a definition or `= 0`." % (cname, decl))
    params = body[op + 1:cp].strip()
    sig = body[:op].strip()
    if sig.startswith("~"):
        raise CppError("class %s: a pure virtual destructor is not in the "
                       "C++ subset." % cname)
    bits = sig.replace("*", " * ").split()
    if len(bits) < 2:
        raise CppError("cannot parse virtual member %r in class %s"
                       % (decl, cname))
    return Member("method", " ".join(bits[:-1]), bits[-1], params, None,
                  line0, "", None, True, True)


def _split_members(body, cname, line0):
    """Parse a class body into fields, methods, a constructor and destructor."""
    body = _ACCESS.sub("", body)
    members = []
    i, n = 0, len(body)
    while i < n:
        while i < n and body[i] in " \t\r\n;":
            i += 1
        if i >= n:
            break
        start = i
        # A member is `~name(..)`, `name(..)`, or `type name(..)` / `type name;`
        brace = body.find("{", i)
        semi = body.find(";", i)
        if semi >= 0 and (brace < 0 or semi < brace):
            decl = body[i:semi].strip()
            i = semi + 1
            if not decl:
                continue
            if decl.startswith("virtual"):
                members.append(_pure_virtual(decl, cname, line0))
                continue
            parts = decl.replace("*", " * ").split()
            if len(parts) < 2:
                raise CppError("cannot parse member %r in class %s"
                               % (decl, cname))
            # `int arr[10];` -- the declarator suffix is not part of the name.
            # Keeping it in the name would make field qualification miss every
            # use of `arr` in a method body.
            fname, dim = parts[-1], ""
            b = fname.find("[")
            if b >= 0:
                fname, dim = fname[:b], fname[b:]
            members.append(Member("field", " ".join(parts[:-1]), fname,
                                  None, None, line0, dim))
            continue
        if brace < 0:
            break
        head = body[start:brace].strip()
        close = _match_brace(body, brace)
        if close is None:
            raise CppError("unterminated method body in class %s" % cname)
        inner = body[brace + 1:close]
        i = close + 1
        op = head.find("(")
        if op < 0:
            raise CppError("cannot parse member %r in class %s" % (head, cname))
        # Match the opening paren rather than taking the last `)`: a ctor
        # initializer list puts more parens after the parameter list.
        cp = _match_paren(head, op)
        if cp is None:
            raise CppError("cannot parse member %r in class %s" % (head, cname))
        params = head[op + 1:cp].strip()
        sig = head[:op].strip()
        virt = bool(re.match(r"virtual\b", sig))
        if virt:
            sig = sig[len("virtual"):].strip()
        init = _parse_init_list(head[cp + 1:], sig, cname)
        if sig == "~" + cname:
            members.append(Member("dtor", "void", cname, params, inner, line0,
                                  "", None, virt))
        elif sig == cname:
            members.append(Member("ctor", "void", cname, params, inner, line0,
                                  "", init))
        else:
            bits = sig.replace("*", " * ").split()
            if len(bits) < 2:
                raise CppError("cannot parse method %r in class %s"
                               % (head, cname))
            members.append(Member("method", " ".join(bits[:-1]), bits[-1],
                                  params, inner, line0, "", None, virt))
    return members


def _parse_base(clause, cname):
    """`: public B` -> `B`. Single inheritance only."""
    clause = (clause or "").strip()
    if not clause.startswith(":"):
        return None
    bases = [b.strip() for b in _split_top(clause[1:]) if b.strip()]
    if len(bases) > 1:
        raise CppError(
            "class %s: multiple inheritance is not in the C++ subset -- a "
            "base is laid out as the first member, which admits one base."
            % cname)
    parts = [p for p in bases[0].split()
             if p not in ("public", "private", "protected", "virtual")]
    if len(parts) != 1:
        raise CppError("class %s: cannot parse base clause %r"
                       % (cname, clause))
    return parts[0]


def _find_classes(scan, text):
    """Locate `class`/`struct` definitions with bodies, template-aware."""
    classes = []
    for m in re.finditer(r"\b(class|struct)\s+(\w+)\s*(:[^{;]*)?\{", scan):
        open_idx = scan.index("{", m.start())
        close = _match_brace(scan, open_idx)
        if close is None:
            raise CppError("unterminated class %s" % m.group(2))
        # A `template<..>` immediately before makes this a template class.
        tparams = ()
        head = scan[:m.start()]
        tm = None
        for tm in _TEMPLATE.finditer(head):
            pass
        if tm is not None and not head[tm.end():].strip():
            tparams = _parse_tparams(
                tm.group(1),
                "class %s" % m.group(2))
        classes.append((m.start(), close + 1,
                        Class(m.group(2), tparams,
                              # From `scan`, not `text`: a member is emitted
                              # onto a single line, so a `//` comment carried
                              # through from the class body would comment out
                              # the generated declaration that follows it.
                              # `_strip_comments` preserves length and string
                              # literals, so bodies are otherwise unchanged.
                              _split_members(scan[open_idx + 1:close],
                                             m.group(2),
                                             scan.count("\n", 0, m.start()) + 1),
                              scan.count("\n", 0, m.start()) + 1,
                              _parse_base(m.group(3), m.group(2)))))
    return classes


def _match_paren(text, open_idx):
    """Index of the `)` closing the `(` at `open_idx`, or None."""
    depth = 0
    i, n = open_idx, len(text)
    quote = None
    while i < n:
        c = text[i]
        if quote is not None:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _split_top(text, sep=","):
    """Split on `sep` at paren/bracket depth zero, ignoring string bodies."""
    parts, cur, depth, quote = [], [], 0, None
    for c in text:
        if quote is not None:
            cur.append(c)
            if c == quote:
                quote = None
            continue
        if c in "\"'":
            quote = c
        elif c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        if c == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    parts.append("".join(cur))
    return parts


def _split_targs(text):
    """Split a template argument/parameter list on top-level commas.

    Like `_split_top`, but `<` and `>` also nest, so `Pair<int, Holder<int>>`
    splits into two arguments rather than three.
    """
    parts, cur, depth, quote = [], [], 0, None
    for c in text:
        if quote is not None:
            cur.append(c)
            if c == quote:
                quote = None
            continue
        if c in "\"'":
            quote = c
        elif c in "([<":
            depth += 1
        elif c in ")]>":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    parts.append("".join(cur))
    return parts


def _match_angle(text, open_idx):
    """Index of the `>` closing the `<` at `open_idx`, or None.

    Bounded by the tokens that cannot appear inside an argument list, so a
    stray relational operator on a name that happens to match a template
    cannot run away to the end of the file. `>>` needs no special case: two
    closers decrement twice.
    """
    depth = 0
    for i in range(open_idx, len(text)):
        c = text[i]
        if c in ";{}()":
            return None
        if c == "<":
            depth += 1
        elif c == ">":
            depth -= 1
            if depth == 0:
                return i
    return None


def _blank_spans(text, spans):
    """Blank the given ranges, preserving length and newlines."""
    out = list(text)
    for start, end in spans:
        for i in range(start, min(end, len(out))):
            if out[i] != "\n":
                out[i] = " "
    return "".join(out)


def _mono_name(name, targs):
    """The monomorphised name for `name<targs..>`."""
    return "%s_%s" % (name, "_".join(_mangle(a) for a in targs))


def _find_template_use(text, tnames):
    """The first *innermost* `Name<..>` use in `text`, or None.

    Innermost first is what makes nesting work without a fixed point over
    the whole file: `Holder<Pair<int,int>>` yields `Pair<int,int>` first, so
    by the time the outer use is looked at its argument already reads
    `Pair_int_int` and mangles to a name that exists.

    Template *bodies* are blanked out before the recording scan rather than
    filtered here, because whether `Holder<T>` names an instantiation depends
    on where it sits, not on how it is spelled: inside a template body `T` is
    a parameter and the use is the pattern, while at file scope `T` could
    perfectly well be a typedef somebody wrote.
    """
    for m in re.finditer(r"(?<![\w.>])(\w+)\s*<", text):
        if m.group(1) not in tnames:
            continue
        open_idx = m.end() - 1
        close = _match_angle(text, open_idx)
        if close is None:
            continue
        inner = text[open_idx + 1:close]
        if "<" in inner:
            continue                      # an outer use; its turn comes later
        args = [a.strip() for a in _split_targs(inner)]
        if not args or not all(args):
            continue
        return m.start(), close + 1, m.group(1), tuple(args)
    return None


def _monomorphise_uses(text, tnames, record=None, known=None):
    """Rewrite every `Name<..>` to its mangled name, innermost use first.

    With `record`, each instantiation is reported as `(name, targs)` as it is
    rewritten -- which is how the set of classes to emit is discovered, in an
    order that already has the inner ones first.

    With `known`, a use that was never recorded is an error rather than a
    mangled name with no class behind it. That happens when one template's
    body instantiates another (`Holder<T>` inside `Outer<T>`): the recording
    scan cannot see it, because at that point `T` is still a parameter.
    Emitting `Holder_int` there would produce a C file referring to a struct
    that is never defined, and the failure would surface as a confusing error
    from the C compiler rather than from here.
    """
    if not tnames:
        return text
    for _ in range(1000):
        hit = _find_template_use(text, tnames)
        if hit is None:
            return text
        start, end, name, targs = hit
        if record is not None:
            record(name, targs)
        if known is not None and tuple(targs) not in known.get(name, ()):
            raise CppError(
                "`%s<%s>` is instantiated from inside another template, "
                "which this lowering cannot discover. Name it at file scope "
                "as well (`%s x;`) so it is emitted."
                % (name, ", ".join(targs), _mono_name(name, targs)))
        text = text[:start] + _mono_name(name, targs) + text[end:]
    raise CppError("template instantiation did not terminate")


_KEYWORDS = frozenset((
    "if", "for", "while", "switch", "return", "sizeof", "do", "else",
    "case", "default", "break", "continue", "goto", "static", "const"))


def _param_name(text):
    """The declared name of one parameter, for forwarding it on."""
    text = text.strip()
    if not text or text == "void":
        return None
    toks = text.replace("&", " ").replace("*", " * ").split()
    if not toks:
        return None
    name = toks[-1]
    b = name.find("[")
    if b >= 0:
        name = name[:b]
    if not name or not (name[0].isalpha() or name[0] == "_"):
        return None
    return name


def _parse_param(text, names):
    """`(class, is_ptr, varname)` for one parameter, or None if not a class.

    A reference parameter counts as a pointer: `T &x` is lowered to `T *x`,
    so every use of `x` on the C side goes through `->`.
    """
    text = text.strip()
    if not text or text == "void":
        return None
    is_ref = "&" in text
    toks = text.replace("&", " ").replace("*", " * ").split()
    toks = [t for t in toks if t != "const"]
    if len(toks) < 2:
        return None
    name = toks[-1]
    if not (name[0].isalpha() or name[0] == "_") or name in _KEYWORDS:
        return None
    if toks[0] not in names:
        return None
    return (toks[0], "*" in toks[:-1] or is_ref, name)


def _ref_positions(params, names):
    """Indices of the parameters in `params` that are taken by reference."""
    out = set()
    for idx, p in enumerate(_split_top(params or "")):
        if "&" in p and _parse_param(p, names) is not None:
            out.add(idx)
    return out


def _sub_code(pattern, repl, text):
    """`pattern.sub(repl, text)`, but only where the match is real code.

    Every rewriting pass in this file that touches a body -- field
    qualification, implicit `this`, template substitution, reference
    lowering -- is a regex over source text, and a regex cannot tell a
    field named `key` from the word `key` inside `printf("key=%d\\n", key)`.
    Rewriting the literal changes what the program prints; rewriting a `//`
    comment can comment out the code that follows it.

    Matching runs against a copy with comment and literal *bodies* blanked,
    so neither can contain a match. Both blanking passes preserve length, so
    the match offsets address `text`, which is what gets emitted -- the same
    `look`/`text` discipline `_rewrite_scopes` and `_rewrite_calls` use.
    """
    look = _blank_strings(_strip_comments(text))
    out, pos = [], 0
    for m in pattern.finditer(look):
        out.append(text[pos:m.start()])
        out.append(repl(m) if callable(repl) else m.expand(repl))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _subst_type(text, tparams, concretes):
    """Replace each template parameter with its argument, all in one pass.

    One pass matters once there is more than one parameter: substituting
    sequentially lets `template<typename T, typename U>` instantiated as
    `<U, int>` rewrite `T` to `U` and then that same `U` to `int`, so both
    fields come out `int`. A single alternation with a lookup cannot
    re-examine text it has already produced.
    """
    if not tparams:
        return text
    mapping = dict(zip(tparams, concretes))
    # A parameter name inside a literal is text the program prints, not a
    # type to substitute: `puts("T")` must not become `puts("int")`.
    return _sub_code(
        re.compile(r"\b(%s)\b" % "|".join(re.escape(p) for p in tparams)),
        lambda m: mapping[m.group(1)], text)


def _mangle(name):
    return re.sub(r"\W+", "_", name).strip("_")


def _type_alt(names):
    return "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))


def _check_ref_returns(scan, names, path):
    """Reject `T& f()`. A reference return has no honest lowering here.

    Lowering it to `T*` would silently change what `f(x)` means at every call
    site -- assignment through the result would become a pointer assignment.
    Following the rest of the subset, that is reported rather than guessed at.
    """
    if not names:
        return
    pat = re.compile(r"(?<![\w.])(?:const\s+)?(%s)\s*&\s*(\w+)\s*\("
                     % _type_alt(names))
    for m in pat.finditer(scan):
        close = _match_paren(scan, scan.index("(", m.end() - 1))
        if close is None:
            continue
        tail = scan[close + 1:close + 40].lstrip()
        if tail.startswith("{") or tail.startswith(";"):
            line = scan.count("\n", 0, m.start()) + 1
            raise CppError(
                "%s:%d: `%s&` return type is not in the C++ subset -- return "
                "`%s *` explicitly. Reference *parameters* are supported."
                % (os.path.basename(path), line, m.group(1), m.group(1)))


def _lower_refs(text, names):
    """`T &x` -> `T *x`, and `T &r = e;` -> `T *r = &(e);`.

    Parameters and locals only: a reference is a pointer that the source did
    not have to spell, so the lowering restores the spelling. Uses of `r` then
    go through `->`, which the call rewriter handles from the symbol table.
    """
    if not names:
        return text
    alt = _type_alt(names)
    # A reference local binds something; take its address.
    text = _sub_code(
        re.compile(
            r"(?<![\w.])((?:const\s+)?(?:%s))\s*&\s*(\w+)\s*=\s*([^;]+);" % alt),
        lambda m: "%s *%s = &(%s);" % (m.group(1), m.group(2),
                                       m.group(3).strip()),
        text)
    # Everything else: a reference parameter.
    text = _sub_code(
        re.compile(r"(?<![\w.&])((?:const\s+)?(?:%s))\s*&(?!&)\s*(\w+)" % alt),
        lambda m: "%s *%s" % (m.group(1), m.group(2)), text)
    return text


def _implicit_this(body, mnames):
    """`helper(x)` inside a method -> `this->helper(x)`.

    Rewriting to an explicit receiver rather than straight to
    `Cname_helper(this, x)` means the ordinary call pass resolves it, so a
    bare call to an inherited method upcasts and a bare call to a virtual
    one dispatches -- both for free, and both correct.
    """
    if not mnames:
        return body
    return _sub_code(re.compile(r"(?<![\w.>])(%s)\s*\(" % _type_alt(mnames)),
                     lambda m: "this->%s(" % m.group(1), body)


def _member_prologue(cname, value_members, initmap, known, fieldset, line):
    """Constructor calls for class-typed members, in declaration order."""
    lines = []
    seen = set()
    for fname, fcls in value_members:
        if fname in initmap:
            args = initmap[fname]
            seen.add(fname)
            lines.append("%s_new(&this->%s%s);"
                         % (fcls, fname, (", " + args) if args else ""))
        elif known[fcls]["ctor"]:
            if known[fcls]["ctor_args"]:
                raise CppError(
                    "%s: member `%s` of type `%s` has no default constructor; "
                    "give it arguments in an initializer list, as "
                    "`%s(..) : %s(..) { }`"
                    % (cname, fname, fcls, cname, fname))
            lines.append("%s_new(&this->%s);" % (fcls, fname))
    # Anything else in the initializer list is a plain assignment.
    for fname, args in initmap.items():
        if fname in seen:
            continue
        if fname not in fieldset:
            raise CppError("%s: `%s` in the initializer list is not a member"
                           % (cname, fname))
        lines.append("this->%s = %s;" % (fname, args))
    return (" ".join(lines) + " ") if lines else ""


def _member_epilogue(value_members, known):
    """Destructor calls for class-typed members, in reverse order."""
    lines = ["%s_drop(&this->%s);" % (fcls, fname)
             for fname, fcls in reversed(value_members)
             if known[fcls]["dtor"]]
    return (" " + " ".join(lines)) if lines else ""


def _vtable_slots(cls, cname, base_info, known):
    """Ordered vtable layout: inherited slots first, then newly declared.

    A slot keeps the signature of the class that first declared it, so a
    derived vtable stays layout-compatible with its base's and a `Base *`
    can dispatch through it. Overriding replaces the implementation, never
    the slot's position or its `this` type.
    """
    slots = [dict(s) for s in (base_info["slots"] if base_info else [])]
    by_name = dict((s["name"], s) for s in slots)
    for m in cls.members:
        if m.kind == "method" and m.virt:
            if m.name in by_name:
                slot = by_name[m.name]
                slot["impl"] = None if m.pure else cname
                slot["pure"] = m.pure
            else:
                slot = {"name": m.name, "decl": cname, "ret": m.ret,
                        "params": m.params or "", "pure": m.pure,
                        "impl": None if m.pure else cname}
                slots.append(slot)
                by_name[m.name] = slot
        elif m.kind == "method" and m.name in by_name:
            # An override without the keyword still overrides.
            by_name[m.name]["impl"] = cname
            by_name[m.name]["pure"] = False
    return slots


def _emit_class(cls, names, known, tsub, targs=None, wants_new=False):
    """Emit a class as a C struct plus free functions.

    Returns the lines, the mangled name, and an info dict describing the
    class to the later call-rewriting pass. `known` holds the classes already
    emitted, which is what makes member construction and inheritance
    possible: a base and a member type must both be complete, so both are
    always emitted first.

    A base class is laid out as the first member, so a pointer to a derived
    object is already a pointer to its base -- upcasting is a cast and
    nothing more. The vtable pointer sits first in the root of the
    hierarchy, hence at offset zero throughout it.
    """
    sub = ((lambda s: _subst_type(s, cls.tparams, targs)) if targs
           else (lambda s: s))
    cname = cls.name if targs is None else _mono_name(cls.name, targs)
    base = cls.base
    if base is not None and base not in known:
        raise CppError(
            "class %s: base class `%s` is not defined above it. A base is "
            "laid out as the first member, so it has to be complete first."
            % (cls.name, base))
    base_info = known[base] if base else None

    slots = _vtable_slots(cls, cname, base_info, known)
    root = (base_info["root"] if base_info else cname) if slots else None
    abstract = any(s["impl"] is None for s in slots)

    head = ["struct %s;" % cname, "typedef struct %s %s;" % (cname, cname)]
    out = []
    fields = [m for m in cls.members if m.kind == "field"]

    # The vtable type is emitted per class; the leading slots match the
    # base's exactly, which is what makes the derived table usable through
    # a base pointer.
    if slots:
        rows = []
        for s in slots:
            args = "%s *this" % s["decl"]
            if s["params"]:
                args += ", " + s["params"]
            rows.append("%s (*%s)(%s);" % (s["ret"], s["name"], args))
        head.append("struct %s_vtable { %s };" % (cname, " ".join(rows)))

    parts = []
    if base:
        parts.append("%s _base;" % base)
    elif slots:
        parts.append("const struct %s_vtable *_vptr;" % cname)
    # The dimension is substituted as well as the type: a non-type parameter
    # (`template<typename T, int N>` with a field `T buf[N];`) appears only
    # in the declarator suffix, and leaving it alone would emit `[N]` with
    # no `N` in scope.
    parts.extend("%s %s%s;" % (sub(f.ret), f.name, sub(f.dim))
                 for f in fields)
    head.append("struct %s { %s };" % (cname, " ".join(parts) or
                                       "char _cpp_empty;"))

    mnames = [m.name for m in cls.members if m.kind == "method"]
    if base_info:
        mnames = sorted(set(mnames) | set(base_info["methods"]))
    info = {"ctor": False, "dtor": False, "ctor_args": "", "methods": {},
            "fields": {}, "base": base, "slots": slots, "root": root,
            "abstract": abstract, "vdtor": False, "ctor_refs": set()}
    if base_info:
        # Inherited members and methods are reachable on the derived class.
        for k, v in base_info["fields"].items():
            info["fields"].setdefault(k, v)
        for k, v in base_info["methods"].items():
            info["methods"][k] = dict(v)
        info["vdtor"] = base_info["vdtor"]

    value_members = []
    for f in fields:
        t = tsub(sub(f.ret))
        b = [x for x in t.replace("*", " ").split() if x != "const"]
        b = b[0] if b else ""
        is_ptr = "*" in t
        info["fields"][f.name] = (b, is_ptr)
        if b in known and not is_ptr and not f.dim:
            value_members.append((f.name, b))
    fieldset = set(info["fields"])

    ctor = next((m for m in cls.members if m.kind == "ctor"), None)
    dtor = next((m for m in cls.members if m.kind == "dtor"), None)
    if dtor is not None and dtor.virt:
        info["vdtor"] = True
    initmap = dict(ctor.init) if ctor is not None else {}

    # Base construction runs first, then the vptr is installed, then members.
    prologue = ""
    if base:
        bargs = initmap.pop(base, None)
        if known[base]["ctor"]:
            if bargs is None and known[base]["ctor_args"]:
                raise CppError(
                    "class %s: base `%s` has no default constructor; pass its "
                    "arguments as `%s(..) : %s(..) { }`"
                    % (cls.name, base, cls.name, base))
            prologue += "%s_new(&this->_base%s); " % (
                base, (", " + bargs) if bargs else "")
        elif bargs is not None:
            raise CppError("class %s: base `%s` has no constructor to pass "
                           "arguments to" % (cls.name, base))
    if slots and not abstract:
        prologue += "((%s *)this)->_vptr = (const struct %s_vtable *)&%s__vtable; " % (
            root, root, cname)
    prologue += _member_prologue(cname, value_members, initmap, known,
                                 fieldset, cls.line)
    # Members are destroyed in reverse, and the base last of all.
    epilogue = _member_epilogue(value_members, known)
    if base and known[base]["dtor"]:
        epilogue += " %s_drop(&this->_base);" % base

    def emit(kind, mname, params, raw):
        refs = _ref_positions(params, names)
        params = _lower_refs(params, names)
        # `this` is a pointer, exactly as an `impl` method's `self` is.
        arglist = "%s *this" % cname + (", " + params if params else "")
        inner = _implicit_this(raw, mnames)
        # Bare member names inside a body refer to fields; qualify them.
        # One alternation rather than a pass per field: each pass would have
        # to re-blank the body, and a field qualified by an earlier pass
        # would be re-examined by a later one.
        if fields:
            inner = _sub_code(
                re.compile(r"(?<![\w.>])(%s)\b"
                           % _type_alt([f.name for f in fields])),
                lambda m: "this->" + m.group(1), inner)
        inner = inner.replace("this->this->", "this->")
        out.append("static %s %s(%s) {%s}" % (kind, mname, arglist, inner))
        return refs

    for m in cls.members:
        if m.kind == "field" or m.pure:
            continue
        params = sub(m.params or "").strip()
        if m.kind == "ctor":
            info["ctor_refs"] = emit("void", "%s_new" % cname, params,
                                     prologue + sub(m.body or ""))
            info["ctor"] = True
            info["ctor_args"] = params
        elif m.kind == "dtor":
            emit("void", "%s_drop" % cname, params,
                 sub(m.body or "") + epilogue)
            info["dtor"] = True
        else:
            info["methods"][m.name] = {
                "refs": emit(sub(m.ret), "%s_%s" % (cname, m.name), params,
                             sub(m.body or "")),
                "owner": cname, "virtual": False, "decl": cname}

    # A base, a member, or a vtable pointer all oblige the class to have a
    # constructor; a base or member destructor obliges a destructor.
    if ctor is None and prologue:
        out.append("static void %s_new(%s *this) { %s}"
                   % (cname, cname, prologue))
        info["ctor"] = True
    if dtor is None and epilogue:
        out.append("static void %s_drop(%s *this) {%s }"
                   % (cname, cname, epilogue))
        info["dtor"] = True

    # `new T(..)` sits in expression position, so it lowers to a call rather
    # than to inline statements: C has no statement expression to allocate,
    # construct and yield the pointer in one. One helper per class that the
    # source actually applies `new` to -- emitting it unconditionally would
    # leave an unused static function in every translation unit.
    #
    # `delete` needs no helper: it is a statement, so it lowers in place.
    if wants_new and not abstract:
        cparams = _lower_refs(info["ctor_args"], names)
        fwd = [n for n in (_param_name(x) for x in _split_top(cparams)) if n]
        body = ["%s *p = (%s *)malloc(sizeof(%s));" % (cname, cname, cname)]
        if info["ctor"]:
            # A failed allocation must not be constructed through. C++ would
            # throw here; the subset has no exceptions, so `new` yields null
            # and the caller checks, which is the C convention anyway.
            body.append("if (p) { %s_new(p%s); }"
                        % (cname, "".join(", " + f for f in fwd)))
        body.append("return p;")
        out.append("static %s *%s__alloc(%s) { %s }"
                   % (cname, cname, cparams or "void", " ".join(body)))

    # Virtual methods resolve through the vtable rather than by name.
    for s in slots:
        info["methods"][s["name"]] = {
            "refs": _ref_positions(s["params"], names), "owner": s["impl"],
            "virtual": True, "decl": s["decl"]}

    if slots and not abstract:
        protos, entries, thunks = [], [], []
        for s in slots:
            impl = s["impl"]
            plist = (", " + s["params"]) if s["params"].strip() else ""
            if impl == s["decl"]:
                entries.append("%s_%s" % (impl, s["name"]))
                protos.append("static %s %s_%s(%s *this%s);"
                              % (s["ret"], impl, s["name"], impl, plist))
                continue
            # The slot's `this` is the declaring class; the implementation
            # takes its own. A thunk converts, which keeps the table free of
            # function-pointer casts.
            fwd = [n for n in (_param_name(x)
                               for x in _split_top(s["params"])) if n]
            thunk = "%s__thunk_%s" % (cname, s["name"])
            ret = "" if s["ret"].strip() == "void" else "return "
            protos.append("static %s %s(%s *this%s);"
                          % (s["ret"], thunk, s["decl"], plist))
            thunks.append("static %s %s(%s *this%s) { %s%s_%s((%s *)this%s); }"
                          % (s["ret"], thunk, s["decl"], plist, ret, impl,
                             s["name"], impl,
                             "".join(", " + f for f in fwd)))
            entries.append(thunk)
        # The constructor installs the table, so the table has to be visible
        # before the constructor is defined -- hence prototypes first.
        head.extend(protos)
        head.append("static const struct %s_vtable %s__vtable = { %s };"
                    % (cname, cname, ", ".join("&" + e for e in entries)))
        out.extend(thunks)
    return head + out, cname, info


def _prev_word(text, idx):
    """Word immediately before `idx`, skipping whitespace."""
    j = idx - 1
    while j >= 0 and text[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return ""
    end = j + 1
    while j >= 0 and (text[j].isalnum() or text[j] == "_"):
        j -= 1
    return text[j + 1:end]


_STORAGE = re.compile(r"^(?:static|extern|inline|register|auto)\s+")


def _open_paren_before(text, close_idx):
    """Index of the `(` matching the `)` at `close_idx`, or None."""
    depth = 0
    j = close_idx
    while j >= 0:
        if text[j] == ")":
            depth += 1
        elif text[j] == "(":
            depth -= 1
            if depth == 0:
                return j
        j -= 1
    return None


def _func_return_type(text, open_paren):
    """The return type of the function whose parameter list opens here.

    Needed because a `return` that unwinds has to evaluate its expression
    before the destructors run, which means spilling it to a temporary of
    the right type.
    """
    head = text[:open_paren].rstrip()
    cut = max(head.rfind(";"), head.rfind("}"), head.rfind("{"),
              head.rfind(")"), head.rfind(":"))
    decl = head[cut + 1:].strip()
    m = re.match(r"^(.*?)([A-Za-z_]\w*)$", decl, re.S)
    if m is None:
        return None
    ret = " ".join(m.group(1).split())
    while True:
        stripped = _STORAGE.sub("", ret)
        if stripped == ret:
            break
        ret = stripped
    return ret or None


def _brace_kind(text, idx, at_file_scope):
    """Classify the block opening at `idx` for unwinding purposes."""
    j = idx - 1
    while j >= 0 and text[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return "block", None
    if text[j] == ")":
        op = _open_paren_before(text, j)
        if op is None:
            return "block", None
        word = _prev_word(text, op)
        if word in ("for", "while"):
            return "loop", None
        if word == "switch":
            return "switch", None
        if word in ("if", "catch"):
            return "block", None
        if at_file_scope:
            return "func", _func_return_type(text, op)
        return "block", None
    word = _prev_word(text, j + 1)
    if word == "do":
        return "loop", None
    return "block", None


def _stmt_end(text, i):
    """Index of the `;` ending the statement starting at `i`, or None."""
    depth, quote = 0, None
    n = len(text)
    while i < n:
        c = text[i]
        if quote is not None:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif c == ";" and depth == 0:
            return i
        i += 1
    return None


class _Frame(object):
    __slots__ = ("live", "kind", "ret")

    def __init__(self, kind, ret):
        self.live = []        # (ctype, vname), in declaration order
        self.kind = kind      # "file" | "func" | "loop" | "switch" | "block"
        self.ret = ret        # enclosing function's return type


def _rewrite_scopes(text, type_info):
    """Emit ctor calls at local decls and dtor calls on every exit from scope.

    `type_info` maps mangled class name -> {"ctor": bool, "dtor": bool}.
    Only by-value locals inside a block are rewritten; file-scope decls,
    pointers, and `struct`/`typedef` forms are left alone.

    Falling off the end of a block drops at the `}`. `return` unwinds every
    live object out to the function, `break` out to the enclosing loop or
    switch, and `continue` out to the enclosing loop. A `return` with a value
    spills it to a temporary first, because C++ evaluates the operand before
    running destructors and the operand routinely reads the object about to
    be destroyed (`return g.get();`).

    `goto` is rejected when anything is live: where it lands decides what
    should have been destroyed, and that is not knowable from this pass.
    """
    if not type_info:
        return text
    names = sorted(type_info, key=len, reverse=True)
    type_alt = "|".join(re.escape(n) for n in names)
    # `Type name;` or `Type name(args);` -- not `Type *p` (star between).
    decl_re = re.compile(
        r"(?<![\w.])(%s)\s+(\w+)\s*(?:\(([^;]*)\))?\s*;" % type_alt)

    agg_re = re.compile(r"\b(struct|union|enum)\b[^;{}]*$")
    # Every lookback and keyword match below runs against a comment-blanked
    # copy. `_strip_comments` preserves length, so indices still line up with
    # `text`, which is what gets emitted. Without this, prose containing the
    # word "struct" reads as a struct body and quietly suppresses every
    # constructor after it.
    look = _strip_comments(text)
    ret_re = re.compile(r"(?<![\w.])return\b")
    brk_re = re.compile(r"(?<![\w.])(break|continue)\s*;")
    goto_re = re.compile(r"(?<![\w.])goto\s+(\w+)")

    def unwind(upto):
        """Drop calls for frames `upto..top`, innermost and latest first."""
        pieces = []
        for fr in reversed(scopes[upto:]):
            for ctype, vname in reversed(fr.live):
                pieces.append("%s_drop(&%s); " % (ctype, vname))
        return "".join(pieces)

    def frame_index(kinds):
        for k in range(len(scopes) - 1, -1, -1):
            if scopes[k].kind in kinds:
                return k
        return None

    out = []
    scopes = [_Frame("file", None)]
    aggs = 0               # depth of enclosing struct/union/enum bodies
    tmp = [0]              # counter for return-value temporaries
    i, n = 0, len(text)
    in_str = None
    while i < n:
        # Decide from the blanked copy, emit from the original. An apostrophe
        # in prose ("the class's table") would otherwise open a string
        # literal and swallow every brace up to the next one.
        c = look[i]
        if in_str is not None:
            out.append(text[i])
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in "\"'":
            in_str = c
            out.append(text[i])
            i += 1
            continue
        if c == "{":
            # A struct/union/enum body is not a scope: its members are field
            # declarations, not locals, so no ctor runs and nothing drops.
            if aggs or agg_re.search(look[:i]):
                aggs += 1
                scopes.append(_Frame("block", None))
            else:
                kind, ret = _brace_kind(look, i, len(scopes) == 1)
                if kind != "func":
                    ret = scopes[-1].ret
                scopes.append(_Frame(kind, ret))
            out.append(text[i])
            i += 1
            continue
        if c == "}":
            if aggs:
                aggs -= 1
            fr = scopes.pop() if len(scopes) > 1 else _Frame("block", None)
            for ctype, vname in reversed(fr.live):
                out.append("%s_drop(&%s); " % (ctype, vname))
            if not scopes:
                scopes = [_Frame("file", None)]
            out.append(text[i])
            i += 1
            continue

        if not aggs:
            m = ret_re.match(look, i)
            if m is not None:
                fidx = frame_index(("func",))
                end = _stmt_end(look, m.end())
                drops = unwind(fidx) if fidx is not None else ""
                if drops and end is not None:
                    expr = text[m.end():end].strip()
                    rtype = scopes[fidx].ret
                    if not expr:
                        out.append("{ %sreturn; }" % drops)
                    elif rtype and rtype != "void":
                        name = "_cpp_ret%d" % tmp[0]
                        tmp[0] += 1
                        # Evaluate before destroying: the operand may read
                        # the very object that is about to be dropped.
                        out.append("{ %s %s = (%s); %sreturn %s; }"
                                   % (rtype, name, expr, drops, name))
                    else:
                        out.append("{ %sreturn %s; }" % (drops, expr))
                    i = end + 1
                    continue

            m = brk_re.match(look, i)
            if m is not None:
                kinds = (("loop", "switch") if m.group(1) == "break"
                         else ("loop",))
                idx = frame_index(kinds)
                drops = unwind(idx) if idx is not None else ""
                if drops:
                    out.append("{ %s%s; }" % (drops, m.group(1)))
                    i = m.end()
                    continue

            m = goto_re.match(look, i)
            if m is not None:
                fidx = frame_index(("func",))
                if fidx is not None and unwind(fidx):
                    # Not a line number: class lowering has already shifted
                    # them, so the label is the findable thing.
                    raise CppError(
                        "`goto %s` cannot be lowered while a destructor is "
                        "pending -- where it lands decides what should be "
                        "destroyed. Restructure, or call `_drop` explicitly."
                        % m.group(1))

        m = decl_re.match(look, i)
        if m and not aggs and \
                _prev_word(look, i) not in ("struct", "typedef", "union"):
            ctype, vname, args = m.group(1), m.group(2), m.group(3)
            info = type_info[ctype]
            if info.get("abstract") and len(scopes) > 1:
                raise CppError(
                    "`%s %s`: %s has a pure virtual method and cannot be "
                    "instantiated. Declare a `%s *` instead."
                    % (ctype, vname, ctype, ctype))
            # File-scope: leave the spelling alone (no automatic Drop).
            if len(scopes) <= 1 or not info["ctor"]:
                out.append(m.group(0))
                i = m.end()
                continue
            out.append("%s %s; " % (ctype, vname))
            if args is None or not args.strip():
                out.append("%s_new(&%s);" % (ctype, vname))
            else:
                out.append("%s_new(&%s, %s);" % (ctype, vname, args.strip()))
            if info["dtor"]:
                scopes[-1].live.append((ctype, vname))
            i = m.end()
            continue

        out.append(text[i])
        i += 1
    return "".join(out)


def _free_ref_funcs(text, names):
    """`{function name: set of by-reference parameter positions}`.

    Collected before references are lowered, because afterwards a `T *` that
    was written `T &` is indistinguishable from one the author spelled.
    """
    out = {}
    for m in re.finditer(r"(?<![\w.])(\w+)\s*\(", text):
        fname = m.group(1)
        if fname in _KEYWORDS:
            continue
        close = _match_paren(text, m.end() - 1)
        if close is None:
            continue
        tail = text[close + 1:close + 40].lstrip()
        if not (tail.startswith("{") or tail.startswith(";")):
            continue          # a call, not a declaration
        refs = _ref_positions(text[m.end():close], names)
        if refs:
            out[fname] = refs
    return out


def _params_at(text, brace_idx):
    """The parameter list of the function header ending just before `{`."""
    j = brace_idx - 1
    while j >= 0 and text[j] in " \t\r\n":
        j -= 1
    if j < 0 or text[j] != ")":
        return None
    depth = 0
    while j >= 0:
        if text[j] == ")":
            depth += 1
        elif text[j] == "(":
            depth -= 1
            if depth == 0:
                return text[j + 1:_find_close(text, j)]
        j -= 1
    return None


def _find_close(text, open_idx):
    close = _match_paren(text, open_idx)
    return close if close is not None else len(text)


def _addr(expr, is_ptr):
    return expr if is_ptr else "&" + expr


def _rewrite_calls(text, cinfo, free_refs):
    """`g.get()` -> `VecGuard_get(&g)`, `p->get()` -> `VecGuard_get(p)`.

    Receivers are resolved against a scope-tracked symbol table: locals,
    function parameters (including the generated `T *this`), and chains
    through class-typed fields. Anything that does not resolve to a class in
    `cinfo` is left exactly as written, so plain C is untouched.

    Also inserts `&` on arguments passed to a by-reference parameter.
    """
    if not cinfo:
        return text
    names = set(cinfo)
    alt = _type_alt(names)
    decl_re = re.compile(
        r"(?<![\w.])(%s)\s+(\*\s*)?(\w+)\s*(?=[;=,])" % alt)
    call_re = re.compile(r"(?<![\w.>])(\w+)((?:\s*(?:\.|->)\s*\w+)+)\s*\(")
    # The same chain, but not followed by `(` -- a member read or write
    # rather than a call. The call pattern is tried first, so this only
    # ever sees what that one left behind.
    field_re = re.compile(
        r"(?<![\w.>])(\w+)((?:\s*(?:\.|->)\s*\w+)+)(?!\s*\()")
    plain_re = re.compile(r"(?<![\w.>])(\w+)\s*\(")
    # `new T(..)` / `new T`, and `delete e` / `delete[] e`. The array forms
    # are matched so they can be reported: they are not simply unsupported
    # syntax, they are the shapes whose lowering would need an element count
    # stored beside the allocation.
    new_re = re.compile(r"(?<![\w.>])new\s+(\w+)\s*(\[)?")
    del_re = re.compile(r"(?<![\w.>])delete\s*(\[\s*\])?\s*")
    # As in `_rewrite_scopes`: match against comment-blanked text so a `.`
    # or a parenthesis inside prose cannot be read as code. Same length, so
    # the indices address `text`, which is what is emitted.
    look = _strip_comments(text)

    def lookup(scopes, name):
        for s in reversed(scopes):
            if name in s:
                return s[name]
        return None

    def resolve(scopes, base, fields_path):
        """Resolve a receiver chain to `(expr_text, class, is_ptr)`."""
        sym = lookup(scopes, base)
        if sym is None:
            return None
        cls, is_ptr = sym
        expr = base
        for fld in fields_path:
            if cls not in cinfo:
                return None
            fields = cinfo[cls]["fields"]
            if fld not in fields:
                return None
            expr = "%s%s%s" % (expr, "->" if is_ptr else ".", fld)
            cls, is_ptr = fields[fld]
        return (expr, cls, is_ptr)

    def rewrite_fields(expr, scopes):
        """Fix `.` to `->` in every member chain inside an expression.

        The main loop cannot reach these: a call to a by-reference function
        is emitted whole, arguments included, so the scan resumes past them.
        Re-running the pass does not help either -- the next pass matches the
        same call and copies the same arguments again. So the argument text
        is rewritten here, where it is being copied.
        """
        parts, pos = [], 0
        while True:
            m = field_re.search(expr, pos)
            if m is None:
                parts.append(expr[pos:])
                return "".join(parts)
            chain = [p for p in re.split(r"\s*(?:\.|->)\s*", m.group(2)) if p]
            got = resolve(scopes, m.group(1), chain)
            parts.append(expr[pos:m.start()])
            parts.append(got[0] if got is not None else m.group(0))
            pos = m.end()

    def fix_args(raw, refs, scopes):
        """Insert `&` where a by-reference parameter wants an address."""
        parts = [rewrite_fields(p, scopes) for p in _split_top(raw)]
        for idx in sorted(refs or ()):
            if idx >= len(parts):
                continue
            a = parts[idx].strip()
            if not a or a.startswith("&") or a.startswith("*"):
                continue
            # `void take(Inner *r, int k);` is a declaration, not a call: its
            # "arguments" parse as parameters. Leave the prototype alone.
            if _parse_param(a, names) is not None:
                continue
            sym = lookup(scopes, a) if re.match(r"^\w+$", a) else None
            if sym is not None and sym[1]:
                continue          # already a pointer
            parts[idx] = " &" + a
        return ",".join(parts).strip()

    out = []
    scopes = [{}]
    pdepth = 0
    i, n = 0, len(text)
    quote = None
    while i < n:
        # As above: state machine on the blanked copy, output from `text`.
        c = look[i]
        if quote is not None:
            out.append(text[i])
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'":
            quote = c
            out.append(text[i])
            i += 1
            continue
        if c == "{":
            frame = {}
            params = _params_at(look, i)
            for p in _split_top(params or ""):
                got = _parse_param(p, names)
                if got is not None:
                    frame[got[2]] = (got[0], got[1])
            scopes.append(frame)
            out.append(text[i])
            i += 1
            continue
        if c == "}":
            if len(scopes) > 1:
                scopes.pop()
            out.append(text[i])
            i += 1
            continue
        if c == "(":
            pdepth += 1
        elif c == ")":
            pdepth = max(0, pdepth - 1)

        if pdepth == 0:
            m = decl_re.match(look, i)
            if m and _prev_word(look, i) not in ("struct", "typedef", "union"):
                scopes[-1][m.group(3)] = (m.group(1), bool(m.group(2)))
                out.append(m.group(0))
                i = m.end()
                continue

        m = call_re.match(look, i)
        if m:
            op = m.end() - 1
            close = _match_paren(look, op)
            chain = [p for p in re.split(r"\s*(?:\.|->)\s*", m.group(2)) if p]
            meth = chain[-1]
            got = (resolve(scopes, m.group(1), chain[:-1])
                   if close is not None else None)
            if got is not None and got[1] in cinfo:
                expr, cls, is_ptr = got
                methods = cinfo[cls]["methods"]
                if meth in methods:
                    ent = methods[meth]
                    args = fix_args(text[op + 1:close], ent["refs"], scopes)
                    recv = _addr(expr, is_ptr)
                    tail = (", " + args) if args else ""

                    def cast(want, e):
                        # Parenthesised: `->` binds tighter than a cast, so
                        # `(Shape *)&sq->_vptr` would read the wrong thing.
                        return e if want == cls else "((%s *)%s)" % (want, e)

                    if ent["virtual"]:
                        # Dispatch through the table. The vptr lives at
                        # offset zero in the root, so the cast is free.
                        out.append(
                            "((const struct %s_vtable *)%s->_vptr)->%s(%s%s)"
                            % (ent["decl"], cast(cinfo[cls]["root"], recv),
                               meth, cast(ent["decl"], recv), tail))
                    else:
                        # An inherited method takes the base as `this`; the
                        # base is the first member, so a cast reaches it.
                        out.append("%s_%s(%s%s)"
                                   % (ent["owner"], meth,
                                      cast(ent["owner"], recv), tail))
                    i = close + 1
                    continue

        m = field_re.match(look, i)
        if m:
            # A member access that is not a call. `_lower_refs` turned
            # `T &c` into `T *c`, so the `.` the author wrote is now a `.`
            # applied to a pointer -- which does not compile. `resolve`
            # already picks the operator from each step's pointer-ness, so
            # rewriting the chain through it fixes the reference case and
            # leaves a by-value receiver spelled exactly as it was.
            chain = [p for p in re.split(r"\s*(?:\.|->)\s*", m.group(2)) if p]
            got = resolve(scopes, m.group(1), chain)
            if got is not None:
                out.append(got[0])
                i = m.end()
                continue

        m = new_re.match(look, i)
        if m:
            tname = m.group(1)
            if tname not in cinfo:
                raise CppError(
                    "`new %s`: %s is not a class defined in this file. The "
                    "subset allocates only its own classes, because it has "
                    "to know the constructor to call." % (tname, tname))
            if m.group(2):
                raise CppError(
                    "`new %s[..]` is not in the C++ subset: array `new` has "
                    "to store the element count beside the allocation for "
                    "`delete[]` to destroy each element. Allocate one object "
                    "at a time." % tname)
            if cinfo[tname]["abstract"]:
                raise CppError(
                    "`new %s`: %s has a pure virtual method and cannot be "
                    "instantiated." % (tname, tname))
            args = ""
            end = m.end()
            after = look[end:len(look)].lstrip()
            if after.startswith("("):
                op = look.index("(", end)
                close = _match_paren(look, op)
                if close is not None:
                    args = fix_args(text[op + 1:close],
                                    cinfo[tname]["ctor_refs"], scopes)
                    end = close + 1
            out.append("%s__alloc(%s)" % (tname, args))
            i = end
            continue

        m = del_re.match(look, i)
        if m:
            if m.group(1):
                raise CppError(
                    "`delete[]` is not in the C++ subset: it has to know how "
                    "many elements to destroy, which array `new` would have "
                    "had to record.")
            end = _stmt_end(look, m.end())
            if end is None:
                raise CppError("`delete` without a terminating `;`")
            operand = text[m.end():end].strip()
            chain = [p for p in re.split(r"\s*(?:\.|->)\s*", operand) if p]
            got = (resolve(scopes, chain[0], chain[1:])
                   if all(re.match(r"^\w+$", p) for p in chain) else None)
            if got is None or got[1] not in cinfo:
                raise CppError(
                    "`delete %s`: cannot tell what type this is, so the "
                    "destructor to call is unknown. Assign it to a typed "
                    "local first." % operand)
            expr, dcls, is_ptr = got
            if not is_ptr:
                raise CppError(
                    "`delete %s`: this is an object, not a pointer to one. "
                    "A by-value local is destroyed at the end of its scope."
                    % operand)
            if cinfo[dcls]["vdtor"]:
                # The vtable holds methods only, so a destructor call cannot
                # dispatch. Deleting through a base pointer would run the
                # base destructor and leave the derived part untouched --
                # exactly the bug `virtual ~T()` is written to prevent.
                raise CppError(
                    "`delete %s`: %s has a virtual destructor, which this "
                    "lowering cannot dispatch -- the vtable carries methods "
                    "only. Call the concrete `_drop` and `free` explicitly."
                    % (operand, dcls))
            if cinfo[dcls]["dtor"]:
                # Guarded and wrapped: `delete` on a null pointer is a no-op
                # in C++, and a bare block would leave a stray `;` before an
                # `else` when the delete is a branch's only statement.
                out.append("do { if (%s) { %s_drop(%s); free(%s); } } "
                           "while (0)" % (expr, dcls, expr, expr))
            else:
                out.append("free(%s)" % expr)
            i = end
            continue

        m = plain_re.match(look, i)
        if m and m.group(1) in free_refs:
            op = m.end() - 1
            close = _match_paren(look, op)
            if close is not None:
                args = fix_args(text[op + 1:close], free_refs[m.group(1)],
                                scopes)
                out.append("%s(%s)" % (m.group(1), args))
                i = close + 1
                continue

        out.append(text[i])
        i += 1
    return "".join(out)


def translate(text, path="<cpp>"):
    """Translate a C++ subset source to C. Raises CppError on anything else."""
    scan = _strip_comments(text)
    _check_unsupported(scan, path)

    classes = _find_classes(scan, text)

    # Which classes does the source apply `new` to? Scanned from a copy with
    # literals and comments blanked, so the word inside `puts("new item")`
    # asks for nothing. A template is matched by its bare name, so every one
    # of its instantiations gets a helper.
    #
    # Checked here, before the no-class early return: `new int` is not a
    # class allocation at all, and a file with no classes can still contain
    # the keyword.
    heap = _blank_strings(_strip_comments(text))
    new_used = set(re.findall(r"(?<![\w.>])new\s+(\w+)", heap))
    uses_heap = bool(new_used) or bool(
        re.search(r"(?<![\w.>])delete\b", heap))
    declared = set(cls.name for _s, _e, cls in classes)
    for tname in sorted(new_used - declared):
        raise CppError(
            "%s: `new %s` is not in the C++ subset -- %s is not a class "
            "defined in this file, and the lowering has to know the "
            "constructor to call. Use `malloc` directly."
            % (os.path.basename(path), tname, tname))
    if uses_heap and not classes:
        raise CppError(
            "%s: `new`/`delete` are lowered against a class defined in this "
            "file, and this file defines none. Use `malloc`/`free`."
            % os.path.basename(path))

    if not classes:
        return text

    tclasses = dict((cls.name, cls) for _s, _e, cls in classes if cls.tparams)
    tnames = set(tclasses)

    # Which instantiations does the file ask for? Template bodies are blanked
    # first: inside one, `Holder<T>` is the pattern rather than a request for
    # a class called `Holder_T`. Scanning innermost-first means a nested
    # argument is already mangled by the time the use containing it is
    # recorded, so `Holder<Pair<int,int>>` records `Pair<int,int>` and then
    # `Holder<Pair_int_int>`.
    bodies = [(s, e) for s, e, cls in classes if cls.tparams]
    wanted = {}

    def record(name, targs):
        cls = tclasses[name]
        if len(targs) != len(cls.tparams):
            raise CppError(
                "%s: `%s` takes %d template argument%s, %d given (`%s<%s>`)"
                % (os.path.basename(path), name, len(cls.tparams),
                   "" if len(cls.tparams) == 1 else "s", len(targs),
                   name, ", ".join(targs)))
        seen = wanted.setdefault(name, [])
        if targs not in seen:
            seen.append(targs)

    _monomorphise_uses(_blank_spans(scan, bodies), tnames, record)

    # Every name a class-typed declaration could spell, mangled and not, so
    # reference parameters can be recognised before anything is emitted.
    names = set()
    for _s, _e, cls in classes:
        names.add(cls.name)
        for targs in wanted.get(cls.name, ()):
            names.add(_mono_name(cls.name, targs))
    _check_ref_returns(scan, names, path)

    # An instantiation used as another's argument has to be complete first,
    # and classes are emitted in declaration order -- so the class supplying
    # the argument must be declared above the one consuming it. Same rule as
    # a base class, and reported the same way rather than silently emitting a
    # member whose type is not yet a known class.
    order = {}
    for idx, (_s, _e, cls) in enumerate(classes):
        for targs in wanted.get(cls.name, ()):
            order[_mono_name(cls.name, targs)] = idx
    for idx, (_s, _e, cls) in enumerate(classes):
        for targs in wanted.get(cls.name, ()):
            for arg in targs:
                for tok in re.findall(r"\b\w+\b", arg):
                    if order.get(tok, idx) > idx:
                        raise CppError(
                            "class %s: template argument `%s` names an "
                            "instantiation of a class declared below it. A "
                            "template argument has to be complete first."
                            % (cls.name, tok))

    # A field spelled `Holder<int>` has to be recognised as `Holder_int`
    # while the containing class is emitted, not after.
    def tsub(s):
        return _monomorphise_uses(s, tnames, known=wanted)

    pieces = []
    cinfo = {}
    prev = 0
    for start, end, cls in classes:
        # Keep everything before the class, minus any `template<..>` header,
        # which has no C equivalent.
        head = text[prev:start]
        head = _TEMPLATE.sub("", head)
        pieces.append(head)
        insts = wanted.get(cls.name, []) if cls.tparams else [None]
        for targs in insts:
            emitted, cname, info = _emit_class(
                cls, names, cinfo, tsub, targs, cls.name in new_used)
            # Trailing newline: two instantiations of the same template are
            # emitted back to back, and without it the last line of one runs
            # into the first line of the next.
            pieces.append("\n".join(emitted) + "\n")
            cinfo[cname] = info
        prev = end
    pieces.append(text[prev:])
    out = "".join(pieces)

    # Rewrite uses: `Ring<int> r;` -> `Ring_int r;`. Field types were already
    # normalised through `tsub` while their class was emitted; this catches
    # the rest -- locals, parameters, and method bodies copied through
    # verbatim.
    out = _monomorphise_uses(out, tnames, known=wanted)

    # Which free functions take a reference? Collected before lowering, while
    # a `&` is still on the page.
    free_refs = _free_ref_funcs(_strip_comments(out), names)
    out = _lower_refs(out, names)
    out = _rewrite_scopes(out, cinfo)

    # Rewriting a call copies its arguments through verbatim, so a receiver
    # nested in an argument list surfaces on the next pass. Iterate to a
    # fixed point rather than recursing into every argument.
    for _ in range(8):
        nxt = _rewrite_calls(out, cinfo, free_refs)
        if nxt == out:
            break
        out = nxt

    # `new` and `delete` lower to `malloc`/`free`, so their declarations have
    # to be in scope. Spelled the way the rest of Crust spells them rather
    # than by including <stdlib.h>: a `.cpp` include is compiled by ShivyCX
    # in the same unit as freestanding code, which has no libc headers.
    # Redeclaring these identically is legal C, so a source that already
    # declared them is unaffected.
    if uses_heap:
        out = ("void *malloc(unsigned long);\nvoid free(void *);\n") + out
    return out


# ==========================================================================
# Command line entry point
#
# `shivyc/preproc.py` runs this in a subprocess rather than importing it. The
# reason is self-hosting: py2c transpiles the compiler's own sources, and an
# `import tools.cpprust` inside preproc becomes a real cross-module reference
# to `cpprust__translate`, which is then undefined at link time because this
# module is not in the transpiled set. It cannot easily join that set either
# -- it leans on compiled-pattern objects and match methods (`.sub`, `.start`,
# `.finditer`) that py2c does not lower, whereas `shivyc/crust.py` stays
# inside the supported subset on purpose.
#
# A subprocess removes the symbol entirely, so the self-hosted compiler links
# with no reference to this file, and lowers a `.cpp` include by running it.
# That does mean a `.cpp` include needs python3 and this script on disk at
# compile time; a `.c` or `.rs` build needs neither.
#
# The protocol is deliberately small so the self-hosted caller can use it too,
# where capturing a pipe is awkward: the translated source is written to the
# output file on success, and on failure the *diagnostic* is written to that
# same file and the exit status is non-zero. One file, one status, no pipes.
# ==========================================================================

def main(argv):
    args = list(argv)
    out_path = None
    if "-o" in args:
        i = args.index("-o")
        if i + 1 >= len(args):
            sys.stderr.write("cpprust: -o needs a path\n")
            return 2
        out_path = args[i + 1]
        del args[i:i + 2]
    if len(args) != 1 or out_path is None:
        sys.stderr.write("usage: cpprust.py <source.cpp> -o <out.c>\n")
        return 2

    src = args[0]
    try:
        with open(src) as f:
            text = f.read()
    except IOError as e:
        sys.stderr.write("cpprust: cannot read %s: %s\n" % (src, e))
        return 2

    try:
        result = translate(text, path=src)
    except CppError as e:
        # The message goes where the output would have gone; the caller
        # reads it back and reports it against the `#include` line.
        try:
            with open(out_path, "w") as f:
                f.write(e.message)
        except IOError:
            pass
        sys.stderr.write("cpprust: %s\n" % e.message)
        return 1

    with open(out_path, "w") as f:
        f.write(result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
