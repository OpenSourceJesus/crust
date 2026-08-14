"""cpprust -- a minimal C++ subset, lowered to C.

`#include "foo.cpp"` translates a small C++ dialect in place, the same way
`#include "foo.py"` handles rpython. What the subset buys, next to Rust rather
than instead of it, is a full **object lifecycle**: constructors chosen by
arity, copy construction and `operator=`, member and base construction
ordering, inheritance and virtual dispatch.

Crust has a `Drop` trait of its own now, so destruction alone is no longer the
reason this exists. The two meet at the symbol instead: a Rust
`impl Drop for T` lowers to `T_drop(T *self)`, which is exactly what `~T()`
lowers to here, so a C++ class may hold a Crust type *by value* and its member
epilogue calls the Rust destructor with no shim. (One difference worth
knowing: members are destroyed in reverse declaration order, because that is
C++'s rule, while Crust's field glue frees in declaration order, because that
is Rust's. Each side follows its own source language.)

The subset, deliberately small:

  * `class` / `struct` with data members and methods
  * a copy constructor, `T(const T &o)`, lowered to `T_copy` -- the one
    constructor that does not lower to `T_new`, since overloading `T_new`
    would redefine it (a second ordinary constructor is reported rather
    than emitted twice). `T b = a;` and `T b(a);` call it, and the copy is
    registered for destruction like any other local. A class with a
    destructor and no copy constructor cannot be copied: the struct copy
    would leave two objects owning one resource and destroy it twice, so
    that is an error naming the Rule of Three rather than a silent
    double free. `operator=` is not in the subset, so assigning to an
    owning object is refused for the same reason. A class with no
    destructor owns nothing, and copies bitwise exactly as C++ would.
  * an owning class never crosses a call boundary by value. A by-value
    parameter is a copy no constructor ran for and no destructor will run
    for; a by-value *return* is worse, since the local is destroyed on the
    way out and the caller receives a copy of a released object. Both are
    errors naming the fix (`T &`, or `T *`). A class with no destructor owns
    nothing and passes by value freely.
  * constructors and a destructor: a local `Type name(args);` becomes
    `Type_new` at the declaration and `Type_drop` at the closing `}` of the
    enclosing block (inside the `.cpp` only -- the include hook never sees
    the C TU that pulled the file in)
  * `public:` / `private:` / `protected:` labels (parsed, not enforced --
    access control is a compile-time property and this is a lowering, not a
    type checker; pretending to enforce it would be worse than not claiming to)
  * constructor overloading, resolved by argument *count*: a call site is
    matched before types are known, so arity is all there is to resolve on,
    and two constructors of the same arity are refused rather than guessed
    between. The no-argument one keeps the plain `T_new`, since that is
    what member and base default construction call; the others are
    `T_new_<n>`, with a matching `T__alloc_<n>` for `new`.
  * `operator[]`, which must return a reference (`T &`) and lowers to a
    `T *`, so `v[i]` becomes `*T__index(&v, i)` and stays an lvalue -- a
    by-value subscript would make `v[i] = x` write to a copy, so it is
    refused. A subscript on a genuine pointer *field* is left as plain C
    indexing, since `T *p; p[i]` walks an array rather than calling
    anything.
  * `operator=`, lowered to `T__assign`.
    Assignment to an owning object has no safe default -- a struct copy
    leaves two owners -- so this is where the author supplies one. A chained
    `a = b = c` is refused, because the call is lowered to `void`.
  * a small `std`: `string` and `vector<T>`, supplied when the source names
    them and written in this subset rather than special-cased in the
    lowering. `std::` is stripped; there is no namespace support and
    claiming otherwise would be worse. Element access is `get`/`set`/`ptr`,
    and `v[i]`, which the containers now overload. `vector<T>` stores
    elements by assignment, so an element type with a destructor is refused
    -- with `vector<T *>` named as the shape that does work, since a
    pointer copies cleanly and `new`/`delete` carry the lifetime.
  * lambdas, in two shapes. A *non-capturing* lambda is exactly a function
    and lowers to one -- `auto f = [](int y) -> int { .. };` becomes a
    function pointer, so the call site needs no rewriting and the lambda can
    be passed anywhere a callback goes.

    A *capturing* lambda is inlined at each call site instead. A capture
    would otherwise need the captured variable's type, to become a field of
    a closure struct, and that type is an ordinary local this pass cannot
    see -- but a body placed where the call is has those variables in scope
    already, so nothing has to be named. `return` inside the body must leave
    the lambda rather than the enclosing function, so the body goes inside
    `do { } while (0)` and `return` becomes `break`: a structured jump the
    destructor unwinding already understands, where a label and `goto` are
    refused outright whenever anything is live. A by-value capture is a copy
    taken where the lambda is written, so it becomes a snapshot local
    declared there; its type is looked up from the declaration and the
    capture is refused if that is missing or ambiguous, since guessing it
    would silently truncate. `[=]` names nothing to look up and is refused.

    Because it is inlined, a capturing lambda has no value to pass around,
    cannot recurse, and cannot be called from a loop condition or a
    short-circuit operand, where the body would not run exactly once. Each
    of those is reported. A return type must be spelled in both shapes:
    nothing here can deduce one, and defaulting to `int` would truncate.
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
  * inherited fields, reached through the `_base` member they actually live
    in: a derived method naming a base field, and `d->field` on a derived
    object, both resolve through a recorded access path rather than
    assuming every field sits at the top of the class.
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

A call can be the receiver of the next one, so `o.node()->get()` lowers to
`Node_get(Owner_node(&o))` -- each step is emitted into an expression that
becomes the next step's receiver, which is what avoids needing a temporary
in expression position. A chain only ever starts from a symbol that
resolves to a class, so legitimate C spelled the same way
(`get_ops()->init(x)`, a free function returning a struct pointer) is still
left exactly as written. A method returning a class *by value* ends the
chain with a diagnostic rather than a guess: C cannot take the address of a
function result, and spilling one would need a statement.

Dispatching a virtual call on a call result goes through a generated
`Decl__vcall_name` helper that takes the receiver as a parameter. The plain
dispatch form names the receiver twice -- once to reach the vptr, once as
the argument -- which is harmless for a name and wrong for a call, where
`f.make()->area()` would build two objects. The helper is emitted only for
the slots a source actually chains onto.

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
`delete` of an operand whose type does not resolve through the symbol table.

A `virtual` destructor occupies a vtable slot under a reserved name, so
`delete base_ptr` dispatches to the most derived destructor, which then
chains to its base through the ordinary epilogue. A derived class always
overrides that slot -- explicitly, or through the destructor it is given
implicitly to chain to the base -- so `virtual` need not be repeated. The
slot is not addressable as a method. Because the base is the first member,
`new Derived()` assigned to a `Base *` is upcast with an
address-preserving cast, which is also why `free` on the base pointer
releases the whole allocation.

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


_UNSUPPORTED = ("throw", "try", "catch",
                "dynamic_cast", "typeid")

# `operator=` is supported; every other overload is not. Checked separately
# from the keyword list so the diagnostic can name the operator.
_OPERATOR = re.compile(r"\boperator\s*(=(?!=)|\[\s*\]|[^\s(]+)")

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
    for m in _OPERATOR.finditer(scan):
        if m.group(1) in ("=", "[]", "["):
            continue
        line = scan.count("\n", 0, m.start()) + 1
        raise CppError(
            "%s:%d: `operator%s` is not in the C++ subset. `operator=` is "
            "the one overload it supports, because assignment to an owning "
            "object has no safe default."
            % (os.path.basename(path), line, m.group(1)))
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
        elif re.search(r"\boperator\s*\[\s*\]$", sig):
            # `T &operator[](int i)`. The reference return is lowered to a
            # pointer and the subscript to a dereference, which keeps
            # `v[i] = x` an assignable lvalue -- the whole point of the
            # operator. A by-value `T operator[]` would silently make
            # `v[i] = x` write to a copy, so it is refused below.
            bits = sig[:sig.index("operator")].strip()
            members.append(Member("index", bits, "operator[]", params,
                                  inner, line0))
        elif re.search(r"\boperator\s*=$", sig):
            # `T &operator=(const T &o)` or `void operator=(..)`. The return
            # type is dropped: the result is lowered to `void`, so a chained
            # `a = b = c` is rejected rather than silently yielding nothing.
            members.append(Member("assign", "void", "operator=", params,
                                  inner, line0))
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
            ar = _arity(args)
            if ar not in known[fcls]["ctors"]:
                raise CppError(
                    "%s: member `%s` of type `%s` has no constructor taking "
                    "%d argument%s" % (cname, fname, fcls, ar,
                                       "" if ar == 1 else "s"))
            lines.append("%s(&this->%s%s);"
                         % (known[fcls]["ctors"][ar]["fn"], fname,
                            (", " + args) if args else ""))
        elif known[fcls]["ctor"]:
            if 0 not in known[fcls]["ctors"]:
                raise CppError(
                    "%s: member `%s` of type `%s` has no default constructor; "
                    "give it arguments in an initializer list, as "
                    "`%s(..) : %s(..) { }`"
                    % (cname, fname, fcls, cname, fname))
            lines.append("%s(&this->%s);"
                         % (known[fcls]["ctors"][0]["fn"], fname))
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


# The destructor's vtable slot. Not a legal C++ member name, so it cannot
# collide with a method the source declared.
_DTOR_SLOT = "__dtor"


def _slot_fn(slot, impl):
    """The C function implementing `slot` in class `impl`.

    A destructor is emitted as `Class_drop`, not `Class___dtor`, so the two
    kinds of slot spell their implementation differently.
    """
    if slot["name"] == _DTOR_SLOT:
        return "%s_drop" % impl
    return "%s_%s" % (impl, slot["name"])


def _vtable_slots(cls, cname, base_info, known):
    """Ordered vtable layout: inherited slots first, then newly declared.

    A slot keeps the signature of the class that first declared it, so a
    derived vtable stays layout-compatible with its base's and a `Base *`
    can dispatch through it. Overriding replaces the implementation, never
    the slot's position or its `this` type.
    A destructor occupies a slot like any other virtual, under a reserved
    name so it cannot collide with a method. It differs in two ways. Its
    implementation is not `Class_<slot>` but `Class_drop`, so the table entry
    is spelled separately. And a derived class *always* overrides it: if the
    base has a destructor then the derived class gets one too, explicitly or
    implicitly, because its epilogue has to chain to the base. So the slot's
    implementation is this class whenever the slot exists at all -- which is
    knowable here, before the epilogue that proves it has been built.
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

    dtor = next((m for m in cls.members if m.kind == "dtor"), None)
    if _DTOR_SLOT in by_name:
        # Inherited: this class has a destructor either way, so it overrides.
        # `virtual` need not be repeated, exactly as for a method override.
        by_name[_DTOR_SLOT]["impl"] = cname
    elif dtor is not None and dtor.virt:
        slots.append({"name": _DTOR_SLOT, "decl": cname, "ret": "void",
                      "params": "", "pure": False, "impl": cname})
    return slots


def _emit_class(cls, names, known, tsub, targs=None, wants_new=False,
                chained=frozenset(), prelude=False):
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
    info = {"ctor": False, "dtor": False, "ctors": {}, "methods": {},
            "fields": {}, "base": base, "slots": slots, "root": root,
            "abstract": abstract, "vdtor": False, "vdtor_decl": None,
            "ctor_refs": set(), "paths": {}, "copy": False,
            "assign": False, "index": None}
    if base_info:
        # Inherited members and methods are reachable on the derived class.
        # A base field is not at the same offset as an own field, though: the
        # base is the first *member*, so reaching `id` means going through
        # `_base`. Each class records the path from `this` to every field it
        # can see, and a derived class prefixes its base's paths.
        for k, v in base_info["fields"].items():
            info["fields"].setdefault(k, v)
        for k, v in base_info["paths"].items():
            info["paths"][k] = "_base." + v
        for k, v in base_info["methods"].items():
            info["methods"][k] = dict((ar, dict(e)) for ar, e in v.items())
    # `vdtor` is not propagated by hand: the destructor slot is inherited
    # through `slots` like any other, and is read back off it below.

    value_members = []
    for f in fields:
        t = tsub(sub(f.ret))
        b = [x for x in t.replace("*", " ").split() if x != "const"]
        b = b[0] if b else ""
        is_ptr = "*" in t
        info["fields"][f.name] = (b, is_ptr)
        # An own field shadows an inherited one of the same name.
        info["paths"][f.name] = f.name
        if b in known and not is_ptr and not f.dim:
            value_members.append((f.name, b))
    fieldset = set(info["fields"])

    ctors = [m for m in cls.members if m.kind == "ctor"]
    copies = [m for m in ctors
              if _is_copy_params(m.params, cname, cls.name, tsub, sub)]
    plain = [m for m in ctors if m not in copies]
    by_arity = {}
    for c in plain:
        ar = _arity(sub(c.params or ""))
        if ar in by_arity:
            # Overloads are told apart by argument *count*: a call site is
            # matched before types are known, so two constructors of the
            # same arity have nothing left to choose between them.
            raise CppError(
                "class %s: two constructors take %d argument%s. Overloads "
                "are resolved by argument count here, so they cannot be "
                "told apart." % (cls.name, ar, "" if ar == 1 else "s"))
        by_arity[ar] = c
    multi = len(plain) > 1
    if len(copies) > 1:
        raise CppError("class %s: more than one copy constructor" % cls.name)
    ctor = plain[0] if plain else None
    copy = copies[0] if copies else None
    dtor = next((m for m in cls.members if m.kind == "dtor"), None)

    def make_prologue(member):
        """Base, then vptr, then members -- for one constructor's init list.

        Built per constructor rather than once, because a copy constructor
        has its own initializer list and its own base arguments.
        """
        initmap = dict(member.init) if member is not None else {}
        pro = ""
        if base:
            bargs = initmap.pop(base, None)
            if known[base]["ctor"]:
                bar = _arity(bargs) if bargs is not None else 0
                if bar not in known[base]["ctors"]:
                    raise CppError(
                        "class %s: base `%s` has no constructor taking %d "
                        "argument%s; pass them as `%s(..) : %s(..) { }`"
                        % (cls.name, base, bar, "" if bar == 1 else "s",
                           cls.name, base))
                pro += "%s(&this->_base%s); " % (
                    known[base]["ctors"][bar]["fn"],
                    (", " + bargs) if bargs else "")
            elif bargs is not None:
                raise CppError("class %s: base `%s` has no constructor to "
                               "pass arguments to" % (cls.name, base))
        if slots and not abstract:
            pro += ("((%s *)this)->_vptr = "
                    "(const struct %s_vtable *)&%s__vtable; "
                    % (root, root, cname))
        pro += _member_prologue(cname, value_members, initmap, known,
                                fieldset, cls.line)
        return pro

    prologue = make_prologue(ctor)
    # Members are destroyed in reverse, and the base last of all.
    epilogue = _member_epilogue(value_members, known)
    if base and known[base]["dtor"]:
        epilogue += " %s_drop(&this->_base);" % base

    mprotos = []
    # The supplied containers define far more methods than any one program
    # calls, and an unused `static` function is a warning. `static inline`
    # is not, and ShivyCX accepts it. Only the prelude is marked: user code
    # should keep hearing about functions it never calls.
    stor = "static inline" if prelude else "static"

    def emit(kind, mname, params, raw):
        refs = _ref_positions(params, names)
        params = _lower_refs(params, names)
        # `this` is a pointer, exactly as an `impl` method's `self` is.
        arglist = "%s *this" % cname + (", " + params if params else "")
        # Members are emitted in declaration order, but a body may call a
        # method declared below it -- ordinary in a class, and an implicit
        # declaration in C. Prototype everything first.
        mprotos.append("%s %s %s(%s);" % (stor, kind, mname, arglist))
        inner = _implicit_this(raw, mnames)
        # Bare member names inside a body refer to fields; qualify them.
        # Inherited ones go through `_base`, so the path is substituted
        # rather than the bare name -- `id` in a derived method is
        # `this->_base.id`, not `this->id`, which would not compile.
        # One alternation rather than a pass per field: each pass would have
        # to re-blank the body, and a field qualified by an earlier pass
        # would be re-examined by a later one.
        if info["paths"]:
            inner = _sub_code(
                re.compile(r"(?<![\w.>])(%s)\b"
                           % _type_alt(list(info["paths"]))),
                lambda m: "this->" + info["paths"][m.group(1)], inner)
        inner = inner.replace("this->this->", "this->")
        # `Shape *twin() { return this; }` inside a derived class returns a
        # `Derived *` where a `Shape *` is declared. The base is the first
        # member, so the cast is address-preserving; without it the C
        # compiler reports incompatible pointer types.
        rcls = [t for t in kind.replace("*", " * ").split() if t != "const"]
        if len(rcls) == 2 and rcls[1] == "*" and rcls[0] != cname \
                and base is not None \
                and (rcls[0] == base or _is_ancestor(rcls[0], base, known)):
            inner = _sub_code(
                re.compile(r"(?<![\w.>])return\s+this\s*;"),
                "return (%s *)this;" % rcls[0], inner)
        out.append("%s %s %s(%s) {%s}" % (stor, kind, mname, arglist, inner))
        return refs

    for m in cls.members:
        if m.kind == "field" or m.pure:
            continue
        params = sub(m.params or "").strip()
        if m.kind == "ctor" and m is copy:
            # A copy constructor lowers to its own symbol: every other
            # constructor is `T_new`, so overloading it is not available.
            # `T &other` lowers to `T *other` like any reference parameter,
            # so the body reads through `->` as usual.
            emit("void", "%s_copy" % cname, params,
                 make_prologue(m) + sub(m.body or ""))
            info["copy"] = True
        elif m.kind == "ctor":
            ar = _arity(params)
            fn = _ctor_name(cname, ar, multi)
            refs = emit("void", fn, params, make_prologue(m) + sub(m.body or ""))
            info["ctors"][ar] = {
                "fn": fn, "params": params, "refs": refs,
                "alloc": fn.replace("_new", "__alloc", 1)}
            info["ctor_refs"] = refs
            info["ctor"] = True
        elif m.kind == "index":
            # The declared `T &` becomes `T *`, and every `v[i]` becomes
            # `*T_index(&v, i)`. Returning a reference is what makes
            # `v[i] = x` mean anything; a by-value return would assign to a
            # copy, so it is rejected rather than quietly lost.
            iret = sub(m.ret or "").strip()
            if "&" not in iret:
                raise CppError(
                    "class %s: `operator[]` has to return a reference "
                    "(`%s &`), so that `v[i] = x` assigns to the element "
                    "rather than to a copy."
                    % (cls.name, iret.replace("&", "").strip() or "T"))
            info["index"] = {"fn": "%s__index" % cname,
                             "ret": tsub(iret.replace("&", "").strip())}
            # The body returns the element; the lowered function returns
            # its address, which is what a reference is.
            ibody = _sub_code(
                re.compile(r"(?<![\w.>])return\s+([^;]+);"),
                lambda mm: "return &(%s);" % mm.group(1).strip(),
                sub(m.body or ""))
            emit(iret.replace("&", "*"), "%s__index" % cname, params, ibody)
        elif m.kind == "assign":
            # Lowered to `T_assign(T *this, const T *o)`. Assignment is the
            # one place the subset needs a user hook: a struct copy of an
            # owning object leaves two owners, and there is no safe default.
            # `__assign`, not `_assign`: a class may perfectly well declare
            # a method called `assign`, and `string` does.
            emit("void", "%s__assign" % cname, params, sub(m.body or ""))
            info["assign"] = True
        elif m.kind == "dtor":
            emit("void", "%s_drop" % cname, params,
                 sub(m.body or "") + epilogue)
            info["dtor"] = True
        else:
            ar = _arity(params)
            over = len([x for x in cls.members
                        if x.kind == "method" and x.name == m.name]) > 1
            if over and m.virt:
                # One vtable slot per name, so an overloaded virtual has
                # nowhere for its second signature to live.
                raise CppError(
                    "class %s: `%s` is virtual and overloaded. A virtual "
                    "method occupies one vtable slot, so its overloads "
                    "would have to share it." % (cls.name, m.name))
            mfn = ("%s_%s_%d" % (cname, m.name, ar) if over
                   else "%s_%s" % (cname, m.name))
            if ar in info["methods"].get(m.name, {}) and \
                    info["methods"][m.name][ar]["owner"] == cname:
                raise CppError(
                    "class %s: two `%s` methods take %d argument%s. "
                    "Overloads are resolved by argument count here."
                    % (cls.name, m.name, ar, "" if ar == 1 else "s"))
            info["methods"].setdefault(m.name, {})[ar] = {
                "refs": emit(sub(m.ret), mfn, params, sub(m.body or "")),
                # The return type is recorded so a call can be a receiver in
                # turn: `o.node()->get()`. Monomorphised, because a method
                # returning `Box<int> *` has to name the emitted struct.
                "ret": tsub(sub(m.ret)), "fn": mfn,
                "owner": cname, "virtual": False, "decl": cname}

    # A base, a member, or a vtable pointer all oblige the class to have a
    # constructor; a base or member destructor obliges a destructor.
    if not plain and prologue:
        # A base, a member, or a vtable obliges a default constructor even
        # when the class declares only constructors that take arguments.
        mprotos.append("%s void %s_new(%s *this);" % (stor, cname, cname))
        out.append("%s void %s_new(%s *this) { %s}"
                   % (stor, cname, cname, make_prologue(None)))
        info["ctors"][0] = {"fn": "%s_new" % cname, "params": "",
                            "refs": set(), "alloc": "%s__alloc" % cname}
        info["ctor"] = True
    if dtor is None and epilogue:
        out.append("%s void %s_drop(%s *this) {%s }"
                   % (stor, cname, cname, epilogue))
        info["dtor"] = True

    # `new T(..)` sits in expression position, so it lowers to a call rather
    # than to inline statements: C has no statement expression to allocate,
    # construct and yield the pointer in one. One helper per class that the
    # source actually applies `new` to -- emitting it unconditionally would
    # leave an unused static function in every translation unit.
    #
    # `delete` needs no helper: it is a statement, so it lowers in place.
    if wants_new and not abstract:
        wants_new = set(wants_new)
        # One allocator per constructor, so `new T(a, b)` reaches the same
        # overload `T x(a, b);` would.
        for ar in sorted(wants_new):
            ent = info["ctors"].get(ar)
            cparams = _lower_refs(ent["params"] if ent else "", names)
            fwd = [n for n in (_param_name(x)
                               for x in _split_top(cparams)) if n]
            alloc = ent["alloc"] if ent else "%s__alloc" % cname
            body = ["%s *p = (%s *)malloc(sizeof(%s));" % (cname, cname, cname)]
            if ent:
                # A failed allocation must not be constructed through. C++
                # would throw here; the subset has no exceptions, so `new`
                # yields null and the caller checks, which is the C
                # convention anyway.
                body.append("if (p) { %s(p%s); }"
                            % (ent["fn"], "".join(", " + f for f in fwd)))
            body.append("return p;")
            out.append("%s %s *%s(%s) { %s }"
                       % (stor, cname, alloc, cparams or "void",
                          " ".join(body)))

    # Virtual methods resolve through the vtable rather than by name. The
    # destructor slot is not addressable as a method, so it is not listed
    # here -- `delete` reaches it through `vdtor_decl`.
    for s in slots:
        if s["name"] == _DTOR_SLOT:
            info["vdtor"] = True
            info["vdtor_decl"] = s["decl"]
            continue
        info["methods"][s["name"]] = {_arity(s["params"]): {
            "refs": _ref_positions(s["params"], names), "owner": s["impl"],
            "ret": tsub(s["ret"]), "virtual": True, "decl": s["decl"],
            "fn": "%s_%s" % (s["impl"], s["name"]) if s["impl"] else None}}

    # Single-evaluation dispatch helpers, for slots the source invokes on a
    # call result. Emitted only by the class that declares the slot, and
    # only for the names that need one -- a helper per slot unconditionally
    # would leave unused static functions all over the output.
    info["vcall"] = {}
    for s in slots:
        if s["decl"] != cname or s["name"] == _DTOR_SLOT \
                or s["name"] not in chained:
            continue
        helper = "%s__vcall_%s" % (cname, s["name"])
        plist = (", " + s["params"]) if s["params"].strip() else ""
        fwd = [n for n in (_param_name(x)
                           for x in _split_top(s["params"])) if n]
        vptr = "this" if root == cname else "((%s *)this)" % root
        vret = "" if s["ret"].strip() == "void" else "return "
        out.append(
            "static %s %s(%s *this%s) { %s((const struct %s_vtable *)"
            "%s->_vptr)->%s(this%s); }"
            % (s["ret"], helper, cname, plist, vret, cname, vptr, s["name"],
               "".join(", " + f for f in fwd)))
        info["vcall"][s["name"]] = helper

    if slots and not abstract:
        protos, entries, thunks = [], [], []
        for s in slots:
            impl = s["impl"]
            plist = (", " + s["params"]) if s["params"].strip() else ""
            if impl == s["decl"]:
                entries.append(_slot_fn(s, impl))
                protos.append("static %s %s(%s *this%s);"
                              % (s["ret"], _slot_fn(s, impl), impl, plist))
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
            thunks.append("static %s %s(%s *this%s) { %s%s((%s *)this%s); }"
                          % (s["ret"], thunk, s["decl"], plist, ret,
                             _slot_fn(s, impl), impl,
                             "".join(", " + f for f in fwd)))
            entries.append(thunk)
        # The constructor installs the table, so the table has to be visible
        # before the constructor is defined -- hence prototypes first.
        head.extend(protos)
        head.append("static const struct %s_vtable %s__vtable = { %s };"
                    % (cname, cname, ", ".join("&" + e for e in entries)))
        out.extend(thunks)
    return head + mprotos + out, cname, info


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
    __slots__ = ("live", "kind", "ret", "vals")

    def __init__(self, kind, ret):
        self.live = []        # (ctype, vname), in declaration order
        self.kind = kind      # "file" | "func" | "loop" | "switch" | "block"
        self.ret = ret        # enclosing function's return type
        self.vals = {}        # class-typed locals: vname -> class


def _copy_source(expr, ctype, scopes, type_info):
    """The object being copied, if `expr` names one of class `ctype`.

    A local, or a chain of value members from one (`t.nums`). A call result
    or any other expression is not something this pass can copy-construct
    from, and guessing would be the whole point of the bug.
    """
    if expr is None:
        return None
    expr = expr.strip()
    parts = [p for p in re.split(r"\s*\.\s*", expr) if p]
    if not parts or not all(re.match(r"^\w+$", p) for p in parts):
        return None
    cls = None
    for fr in reversed(scopes):
        if parts[0] in fr.vals:
            cls = fr.vals[parts[0]]
            break
    if cls is None:
        return None
    out = parts[0]
    for fld in parts[1:]:
        info = type_info.get(cls)
        if info is None or fld not in info["fields"]:
            return None
        fcls, is_ptr = info["fields"][fld]
        if is_ptr:
            return None              # a pointer member is not the object
        out = "%s.%s" % (out, info["paths"].get(fld, fld))
        cls = fcls
    return out if cls == ctype else None


def _copy_call(ctype, vname, src, info, where):
    """`T_copy(&b, &a);`, or the Rule of Three diagnostic."""
    if not info["copy"]:
        if info["dtor"]:
            raise CppError(
                "`%s %s(%s)`: %s has a destructor but no copy constructor, "
                "so copying it would leave two objects owning one resource "
                "and destroy it twice. Add `%s(const %s &o)`, or pass by "
                "reference (`%s &`)."
                % (ctype, vname, src, ctype, ctype, ctype, ctype))
        # No destructor: nothing owns anything, so a bitwise copy is exactly
        # what C++ would do implicitly.
        return "%s = %s;" % (vname, src)
    return "%s_copy(&%s, &%s);" % (ctype, vname, src)


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

    # `T b = a;` -- copy initialization, which the declaration pattern above
    # cannot match because of the initializer.
    init_re = re.compile(
        r"(?<![\w.])(%s)\s+(\w+)\s*=\s*([^;]+);" % type_alt)
    # `b = a;` on a bare name, checked against the class-typed locals in
    # scope. Compound assignments are not matched: `+=` on a class is not a
    # copy, and C would reject it anyway.
    assign_re = re.compile(r"(?<![\w.>])(\w+)\s*=(?!=)\s*([^;]+);")

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
            src = _copy_source(args, ctype, scopes, type_info)
            ar = _arity(args)
            if src is None and ar not in info["ctors"]:
                raise CppError(
                    "`%s %s(%s)`: %s has no constructor taking %d "
                    "argument%s (it has %s)."
                    % (ctype, vname, (args or "").strip(), ctype, ar,
                       "" if ar == 1 else "s",
                       ", ".join(str(k) for k in sorted(info["ctors"]))
                       or "none"))
            if args is None or not args.strip():
                out.append("%s(&%s);" % (info["ctors"][0]["fn"], vname))
            elif src is not None:
                # `T b(a);` is a copy, not a call to the default constructor
                # with an extra argument.
                out.append(_copy_call(ctype, vname, src, info, ctype))
            else:
                out.append("%s(&%s, %s);"
                           % (info["ctors"][ar]["fn"], vname, args.strip()))
            if info["dtor"]:
                scopes[-1].live.append((ctype, vname))
            scopes[-1].vals[vname] = ctype
            i = m.end()
            continue

        m = init_re.match(look, i)
        if m and not aggs and \
                _prev_word(look, i) not in ("struct", "typedef", "union"):
            # `T b = a;` -- copy initialization. Without this the object was
            # neither constructed nor dropped: a bitwise copy that the scope
            # exit never saw.
            ctype, vname, rhs = m.group(1), m.group(2), m.group(3).strip()
            info = type_info[ctype]
            if len(scopes) <= 1:
                out.append(m.group(0))
                i = m.end()
                continue
            src = _copy_source(rhs, ctype, scopes, type_info)
            if src is None and not info["dtor"] and not info["copy"]:
                out.append(m.group(0))       # plain data: a bitwise copy is
                i = m.end()                  # exactly what C++ would do
                continue
            if src is None:
                raise CppError(
                    "`%s %s = %s;`: %s owns a resource, and the right-hand "
                    "side is not an object of that type this pass can name. "
                    "Assign to a typed local first."
                    % (ctype, vname, rhs, ctype))
            out.append("%s %s; " % (ctype, vname))
            out.append(_copy_call(ctype, vname, src, info, ctype))
            if info["dtor"]:
                scopes[-1].live.append((ctype, vname))
            scopes[-1].vals[vname] = ctype
            i = m.end()
            continue

        m = assign_re.match(look, i)
        if m and not aggs:
            lhs = m.group(1)
            ctype = None
            for fr in reversed(scopes):
                if lhs in fr.vals:
                    ctype = fr.vals[lhs]
                    break
            if ctype is not None and type_info[ctype]["assign"]:
                rhs = m.group(2).strip()
                if "=" in _blank_strings(rhs).replace("==", ""):
                    raise CppError(
                        "`%s = %s`: a chained assignment is not in the C++ "
                        "subset -- `operator=` is lowered to a `void` call, "
                        "so there is no result to assign onward."
                        % (lhs, rhs))
                src = _copy_source(rhs, ctype, scopes, type_info)
                if src is None:
                    raise CppError(
                        "`%s = %s`: the right-hand side is not an object of "
                        "type %s that this pass can name. Assign it to a "
                        "typed local first." % (lhs, rhs, ctype))
                out.append("%s__assign(&%s, &%s);" % (ctype, lhs, src))
                i = m.end()
                continue
            if ctype is not None and type_info[ctype]["dtor"]:
                # A struct assignment copies the representation and leaves
                # both objects owning it, so both destructors run on the same
                # resource. `operator=` is not in the subset, so there is
                # nothing to call instead.
                raise CppError(
                    "`%s = %s`: %s has a destructor, and assigning would "
                    "leave two objects owning one resource -- both would be "
                    "destroyed. Define `%s &operator=(const %s &o)`, or copy "
                    "at construction (`%s b(a);`)."
                    % (lhs, m.group(2).strip(), ctype, ctype, ctype, ctype))

        out.append(text[i])
        i += 1
    return "".join(out)


_TYPE_WORDS = frozenset((
    "void", "int", "char", "long", "short", "float", "double", "unsigned",
    "signed", "struct", "union", "enum", "const", "..."))


def _looks_like_params(parts, cinfo):
    """Do these read as a declaration's parameters rather than arguments?

    `Node n(1);` is a local with a constructor argument, not a function
    returning `Node`; `void f(Node &n);` is a declaration. A parameter has a
    type and a name, so a lone expression gives it away.
    """
    for part in parts:
        part = part.strip()
        if not part:
            continue
        toks = [t for t in part.replace("*", " * ").split() if t != "const"]
        toks = [t for t in toks if t != "*"]
        if len(toks) >= 2:
            continue
        if toks and (toks[0] in _TYPE_WORDS or toks[0] in cinfo):
            continue
        return False
    return True


def _by_value_class(part, cinfo):
    """The class a parameter is taken by value as, or None.

    A declaration's parameter has a type and a name; a call's argument has
    only an expression, which is what tells the two apart here.
    """
    toks = [t for t in part.replace("*", " * ").split() if t != "const"]
    if len(toks) < 2 or "*" in toks or "[" in part:
        return None
    return toks[0] if toks[0] in cinfo else None


def _check_by_value(text, cinfo, path):
    """Reject by-value class parameters and returns for owning classes.

    Both are silent miscompiles otherwise. A by-value parameter is a struct
    copy that no constructor ran for and no destructor will run for. A
    by-value *return* is worse: the local is destroyed on the way out, so
    the caller receives a copy of an object whose resources were just
    released -- a use-after-free that no diagnostic points at.

    Doing these properly means copy-constructing into a temporary at the
    call site, which needs a statement, and this is expression position.
    Classes with no destructor own nothing and are left alone.
    """
    owning = set(n for n in cinfo if cinfo[n]["dtor"])
    if not owning:
        return
    for m in re.finditer(r"(?<![\w.])(\w+)\s*\(", text):
        if m.group(1) in _KEYWORDS:
            continue
        close = _match_paren(text, m.end() - 1)
        if close is None:
            continue
        tail = text[close + 1:close + 40].lstrip()
        if not (tail.startswith("{") or tail.startswith(";")):
            continue                     # a call, not a declaration
        parts = _split_top(text[m.end():close])
        if not _looks_like_params(parts, cinfo):
            continue                     # a local with constructor arguments
        for part in parts:
            cls = _by_value_class(part.strip(), cinfo)
            if cls in owning:
                raise CppError(
                    "%s: `%s` takes `%s` by value, but %s has a destructor -- "
                    "the copy is never constructed and never destroyed. Pass "
                    "`%s &` instead."
                    % (os.path.basename(path), m.group(1), cls, cls, cls))
        ret = _func_return_type(text, m.end() - 1)
        toks = [t for t in (ret or "").replace("*", " * ").split()
                if t != "const"]
        if toks and toks[0] in owning and "*" not in toks:
            raise CppError(
                "%s: `%s` returns `%s` by value, but %s has a destructor -- "
                "the local is destroyed on the way out, so the caller would "
                "receive a copy of a released object. Return `%s *`, or fill "
                "a `%s &` parameter."
                % (os.path.basename(path), m.group(1), toks[0], toks[0],
                   toks[0], toks[0]))


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


def _match_bracket(text, open_idx):
    """Index of the `]` closing the `[` at `open_idx`, or None."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return i
    return None


def _addr(expr, is_ptr):
    return expr if is_ptr else "&" + expr


def _is_ancestor(maybe_base, derived, cinfo):
    """Is `maybe_base` a base of `derived`, however far up?"""
    seen = set()
    cur = cinfo.get(derived, {}).get("base")
    while cur and cur not in seen:
        if cur == maybe_base:
            return True
        seen.add(cur)
        cur = cinfo.get(cur, {}).get("base")
    return False


_DECL_TARGET = re.compile(r"(?<![\w.])(\w+)\s*\*\s*\w+\s*=\s*$")
_ASSIGN_TARGET = re.compile(r"(?<![\w.>])(\w+)\s*=\s*$")


def _assign_target(before, scopes, cinfo):
    """The class a `new` expression is being assigned into, or None.

    Two shapes are recognised, which is what covers `Base *p = new
    Derived();` and a later `p = new Derived();`. Anything else -- a
    `return`, an argument, a field write through a chain -- yields None and
    no cast is inserted, so the C compiler still reports a real mismatch.
    """
    m = _DECL_TARGET.search(before)
    if m is not None:
        return m.group(1) if m.group(1) in cinfo else None
    m = _ASSIGN_TARGET.search(before)
    if m is not None:
        for s in reversed(scopes):
            if m.group(1) in s:
                cls, is_ptr = s[m.group(1)]
                return cls if is_ptr else None
    return None


def _ret_class(ret, cinfo):
    """`(class, is_ptr)` for a method's return type, or `(None, False)`.

    Only a single-level pointer to a known class can go on to be a receiver.
    A `T **` is not an object, and a non-class return simply ends the chain.
    """
    toks = [t for t in (ret or "").replace("*", " * ").split()
            if t != "const"]
    if not toks or toks[0] not in cinfo:
        return None, False
    stars = toks.count("*")
    if stars > 1:
        return None, False
    return toks[0], stars == 1


def _arity(params):
    """How many parameters a list declares."""
    return len([p for p in _split_top(params or "")
                if p.strip() and p.strip() != "void"])


def _ctor_name(cname, arity, multi):
    """`T_new`, or `T_new_<n>` when the class overloads its constructor.

    The no-argument constructor keeps the plain name whenever there is one,
    because that is what member and base default construction call.
    """
    if not multi or arity == 0:
        return "%s_new" % cname
    return "%s_new_%d" % (cname, arity)


def _is_copy_params(params, cname, raw_name, tsub, sub):
    """Is this parameter list a copy constructor's -- one `T &` or `const T &`?

    Checked on the spelling before reference lowering, because afterwards a
    `T *` the author wrote is indistinguishable from one this pass made.
    """
    parts = [p for p in _split_top(params or "") if p.strip()]
    if len(parts) != 1 or "&" not in parts[0]:
        return False
    toks = [t for t in tsub(sub(parts[0])).replace("&", " ")
            .replace("*", " * ").split() if t != "const"]
    return len(toks) >= 2 and "*" not in toks and toks[0] in (cname, raw_name)


def _emit_method_call(expr, cls, is_ptr, meth, args, ent, cinfo):
    """One lowered method call, as a C expression.

    Factored out because a chained call needs to produce a receiver
    expression rather than write straight to the output.
    """
    recv = _addr(expr, is_ptr)
    tail = (", " + args) if args else ""

    def cast(want, e):
        # Parenthesised: `->` binds tighter than a cast, so
        # `(Shape *)&sq->_vptr` would read the wrong thing.
        return e if want == cls else "((%s *)%s)" % (want, e)

    if ent["virtual"]:
        # Dispatch through the table. The vptr lives at offset zero in the
        # root, so the cast is free.
        #
        # The plain form mentions the receiver twice -- once to reach the
        # vptr, once as the argument -- which is fine for a name but wrong
        # for a call: `f.make()->area()` would run the factory twice. When
        # the receiver is an expression, dispatch goes through a helper that
        # takes it as a parameter, so it is evaluated once. C has no
        # statement expression to spill it into.
        if "(" in recv:
            helper = cinfo[ent["decl"]].get("vcall", {}).get(meth)
            if helper is None:
                raise CppError(
                    "`%s` is dispatched on a call result, which needs a "
                    "single-evaluation helper that was not emitted. Assign "
                    "the receiver to a local first." % meth)
            return "%s(%s%s)" % (helper, cast(ent["decl"], recv), tail)
        return ("((const struct %s_vtable *)%s->_vptr)->%s(%s%s)"
                % (ent["decl"], cast(cinfo[cls]["root"], recv), meth,
                   cast(ent["decl"], recv), tail))
    # An inherited method takes the base as `this`; the base is the first
    # member, so a cast reaches it.
    return "%s(%s%s)" % (ent["fn"], cast(ent["owner"], recv), tail)


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
    builtin_re = re.compile(r"(?<![\w.>])(__cpp_copy|__cpp_drop)\s*\(")
    # `v[i]` / `a.b[i]` on a class that overloads subscript.
    index_re = re.compile(r"(?<![\w.>\]])(\w+)((?:\s*(?:\.|->)\s*\w+)*)\s*\[")
    # A call continuing a chain: `.g(` or `->g(` right after a `)`.
    cont_re = re.compile(r"\s*(?:\.|->)\s*(\w+)\s*\(")
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
            # Inherited fields sit inside `_base`, so the recorded path is
            # what reaches them; an own field's path is just its name.
            path = cinfo[cls]["paths"].get(fld, fld)
            expr = "%s%s%s" % (expr, "->" if is_ptr else ".", path)
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

    def _pick(entries, raw, cls, meth):
        """The overload of `meth` matching this argument count."""
        ar = _arity(raw)
        if ar in entries:
            return entries[ar]
        if len(entries) == 1:
            # Not an overload set: let the C compiler report the arity, as
            # it did before overloading existed.
            return list(entries.values())[0]
        raise CppError(
            "`%s::%s` has no overload taking %d argument%s (it has %s)."
            % (cls, meth, ar, "" if ar == 1 else "s",
               ", ".join(str(k) for k in sorted(entries))))

    def follow(expr, cls, is_ptr, pos, from_meth, addressable=False):
        """Consume `.g(..)` / `->g(..)` chained onto an expression.

        The result of one call is the receiver of the next, so each step is
        emitted into a string that the next step receives. Shared by the
        call branch and the subscript branch, since `v[i]->name()` chains
        for exactly the same reason `o.node()->name()` does.
        """
        while True:
            nm = cont_re.match(look, pos)
            if nm is None:
                return expr, pos
            meth = nm.group(1)
            if cls is None or meth not in cinfo[cls]["methods"]:
                return expr, pos
            if not is_ptr and not addressable:
                # C cannot take the address of a function *result*, and a
                # method needs an addressable receiver. A dereference is a
                # different matter -- `&(*p)` is fine -- which is why the
                # subscript branch says so.
                raise CppError(
                    "`%s().%s()`: %s is returned by value, so there is no "
                    "object to call `%s` on. Assign it to a local first, or "
                    "return `%s *`." % (from_meth, meth, cls, meth, cls))
            nxt = _match_paren(look, nm.end() - 1)
            if nxt is None:
                return expr, pos
            ent = _pick(cinfo[cls]["methods"][meth], text[nm.end():nxt],
                        cls, meth)
            args = fix_args(text[nm.end():nxt], ent["refs"], scopes)
            expr = _emit_method_call(expr, cls, is_ptr, meth, args, ent,
                                     cinfo)
            cls, is_ptr = _ret_class(ent["ret"], cinfo)
            # The result of a call is a value, addressable no longer.
            addressable = False
            pos, from_meth = nxt + 1, meth

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
                if meth in cinfo[cls]["methods"]:
                    ent = _pick(cinfo[cls]["methods"][meth],
                                text[op + 1:close], cls, meth)
                    args = fix_args(text[op + 1:close], ent["refs"], scopes)
                    expr = _emit_method_call(expr, cls, is_ptr, meth, args,
                                             ent, cinfo)
                    rcls, rptr = _ret_class(ent["ret"], cinfo)
                    expr, end = follow(expr, rcls, rptr, close + 1, meth)
                    out.append(expr)
                    i = end
                    continue

        m = builtin_re.match(look, i)
        if m:
            # `__cpp_copy(T, dst, src)` / `__cpp_drop(T, x)`. A template body
            # is textual, so it can spell `T` but not `T_copy`: substitution
            # rewrites whole words, and `T_copy` is one word. These are the
            # hook that lets a container say "copy an element" and have it
            # mean the copy constructor for a class and an assignment for a
            # scalar, decided per instantiation.
            close = _match_paren(look, m.end() - 1)
            if close is None:
                raise CppError("unterminated `%s`" % m.group(1))
            parts = [p.strip() for p in _split_top(text[m.end():close])]
            kind, ty = m.group(1), (parts[0] if parts else "")
            if ty not in cinfo:
                raise CppError(
                    "`%s(%s, ..)`: %s is not a class. These are for element "
                    "types with constructors; a scalar element needs no "
                    "copy or destroy step." % (kind, ty, ty))
            if kind == "__cpp_drop":
                out.append("%s_drop(&%s)" % (ty, parts[1])
                           if cinfo[ty]["dtor"] else "(void)0")
            else:
                if not cinfo[ty]["copy"]:
                    raise CppError(
                        "`__cpp_copy(%s, ..)`: %s has no copy constructor, "
                        "so an element copy would duplicate whatever it "
                        "owns. Add `%s(const %s &o)`." % (ty, ty, ty, ty))
                out.append("%s_copy(&%s, %s)" % (ty, parts[1], parts[2]))
            i = close + 1
            continue

        m = index_re.match(look, i)
        if m:
            chain = [p for p in re.split(r"\s*(?:\.|->)\s*", m.group(2) or "")
                     if p]
            got = resolve(scopes, m.group(1), chain)
            # A subscript on a genuine pointer is plain C indexing, not
            # `operator[]` on what it points at -- `T *p; p[i]` walks an
            # array. Fields record their declared pointer-ness truthfully,
            # so a chain ending in a pointer field is left alone. A bare
            # symbol is not so clear: a reference parameter has already been
            # lowered to a pointer and is indistinguishable from one the
            # author spelled, and between the two readings `v[i]` on a
            # `vector &` is the one people write.
            if got is not None and chain and got[2]:
                got = None
            if got is not None and got[1] in cinfo \
                    and cinfo[got[1]]["index"] is not None:
                ob = m.end() - 1
                cb = _match_bracket(look, ob)
                if cb is not None:
                    expr, cls, is_ptr = got
                    ent = cinfo[cls]["index"]
                    # `v[i]` is `*v.at(i)` in the lowered form: the operator
                    # yields the element's address, and the dereference
                    # keeps `v[i] = x` an lvalue.
                    sub_expr = ("(*%s(%s, %s))"
                                % (ent["fn"], _addr(expr, is_ptr),
                                   text[ob + 1:cb].strip()))
                    ecls, eptr = _ret_class(ent["ret"], cinfo)
                    sub_expr, i = follow(sub_expr, ecls, eptr, cb + 1,
                                         "operator[]", addressable=True)
                    out.append(sub_expr)
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
            raw = ""
            after = look[end:len(look)].lstrip()
            if after.startswith("("):
                op = look.index("(", end)
                close = _match_paren(look, op)
                if close is not None:
                    raw = text[op + 1:close]
                    end = close + 1
            ar = _arity(raw)
            ctors = cinfo[tname]["ctors"]
            if not ctors and ar == 0:
                # No constructor at all: `new T` is just the allocation.
                out.append("%s__alloc()" % tname)
                i = end
                continue
            if ar not in ctors:
                raise CppError(
                    "`new %s(%s)`: %s has no constructor taking %d "
                    "argument%s (it has %s)."
                    % (tname, raw.strip(), tname, ar, "" if ar == 1 else "s",
                       ", ".join(str(k) for k in sorted(ctors)) or "none"))
            args = fix_args(raw, ctors[ar]["refs"], scopes) if raw else ""
            alloc = "%s(%s)" % (ctors[ar]["alloc"], args)
            # `Base *p = new Derived(..)` is the shape the whole virtual
            # story rests on, and C will not convert `Derived *` to `Base *`
            # on its own. The base is the first member, so the cast is
            # address-preserving; it is inserted only when the target really
            # is an ancestor, so an unrelated mismatch still gets diagnosed
            # by the C compiler rather than silently cast away.
            target = _assign_target(look[:i], scopes, cinfo)
            if target is not None and target != tname \
                    and _is_ancestor(target, tname, cinfo):
                alloc = "(%s *)%s" % (target, alloc)
            out.append(alloc)
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
                # Dispatch: the static type may be a base, and the object may
                # be a derived one whose destructor has to run. The vptr sits
                # at offset zero in the root, and the base is the first
                # member, so both casts are address-preserving -- which is
                # also why `free` on the base pointer frees the allocation.
                decl = cinfo[dcls]["vdtor_decl"]

                def dcast(want, e):
                    return e if want == dcls else "((%s *)%s)" % (want, e)

                out.append(
                    "do { if (%s) { ((const struct %s_vtable *)%s->_vptr)"
                    "->%s(%s); free(%s); } } while (0)"
                    % (expr, decl, dcast(cinfo[dcls]["root"], expr),
                       _DTOR_SLOT, dcast(decl, expr), expr))
            elif cinfo[dcls]["dtor"]:
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


# ==========================================================================
# A very small `std`: `string` and `vector`, written in this subset rather
# than special-cased in the lowering.
#
# That is the point of them being here. Every feature they need -- templates,
# a copy constructor, `operator=`, a destructor, methods calling methods --
# is one the subset already claims to have, so if the containers compile,
# the claim holds. Nothing below is privileged: it goes through the same
# passes as user code, and a bug in it shows up as a bug in the lowering.
#
# `std::` is stripped rather than modelled. There is no namespace support and
# pretending otherwise would be worse than not claiming it.
#
# Deliberately not here: `operator[]`, iterators, `<<`. `operator=` is the
# only overload the subset has, so element access is `get`/`set`/`ptr` and
# not `v[i]`.
# ==========================================================================

_STD_DECLS = """void *malloc(unsigned long);
void *realloc(void *, unsigned long);
void free(void *);
unsigned long strlen(const char *);
void *memcpy(void *, const void *, unsigned long);
int memcmp(const void *, const void *, unsigned long);
"""

_STD_STRING = """
class string {
public:
    char *sd;
    int sn;
    int scap;
    string() { sd = 0; sn = 0; scap = 0; }
    string(const char *s) { sd = 0; sn = 0; scap = 0; assign(s); }
    string(const string &o) {
        sd = 0; sn = 0; scap = 0;
        reserve(o.sn);
        if (o.sn > 0) { memcpy(sd, o.sd, (unsigned long)o.sn); }
        sn = o.sn;
        if (sd != 0) { sd[sn] = 0; }
    }
    string &operator=(const string &o) {
        if (sd != o.sd) {
            sn = 0;
            reserve(o.sn);
            if (o.sn > 0) { memcpy(sd, o.sd, (unsigned long)o.sn); }
            sn = o.sn;
            if (sd != 0) { sd[sn] = 0; }
        }
    }
    ~string() { free(sd); sd = 0; sn = 0; scap = 0; }
    int size() { return sn; }
    int empty() { if (sn == 0) { return 1; } return 0; }
    void reserve(int c) {
        if (c + 1 > scap) {
            int m = c + 1;
            char *nd = (char *)realloc(sd, (unsigned long)m);
            if (nd != 0) { sd = nd; scap = m; }
        }
    }
    void clear() { sn = 0; if (sd != 0) { sd[0] = 0; } }
    void push_back(char ch) {
        reserve(sn + 1);
        if (sd != 0) { sd[sn] = ch; sn = sn + 1; sd[sn] = 0; }
    }
    void assign(const char *s) {
        int k = (int)strlen(s);
        sn = 0;
        reserve(k);
        if (sd != 0) { memcpy(sd, s, (unsigned long)k); sn = k; sd[sn] = 0; }
    }
    void append(const char *s) {
        int k = (int)strlen(s);
        reserve(sn + k);
        if (sd != 0) { memcpy(sd + sn, s, (unsigned long)k); sn = sn + k;
                       sd[sn] = 0; }
    }
    char at(int i) { return sd[i]; }
    char &operator[](int i) { return sd[i]; }
    const char *c_str() { if (sd == 0) { return ""; } return sd; }
    int equals(const string &o) {
        if (sn != o.sn) { return 0; }
        if (sn == 0) { return 1; }
        if (memcmp(sd, o.sd, (unsigned long)sn) == 0) { return 1; }
        return 0;
    }
};
"""

_STD_VECTOR = """
template<typename T>
class vector {
public:
    T *vd;
    int vn;
    int vcap;
    vector() { vd = 0; vn = 0; vcap = 0; }
    vector(int n) { vd = 0; vn = 0; vcap = 0; reserve(n); }
    vector(const vector<T> &o) {
        vd = 0; vn = 0; vcap = 0;
        reserve(o.vn);
        int i = 0;
        while (i < o.vn) { vd[i] = o.vd[i]; i = i + 1; }
        vn = o.vn;
    }
    vector<T> &operator=(const vector<T> &o) {
        if (vd != o.vd) {
            vn = 0;
            reserve(o.vn);
            int i = 0;
            while (i < o.vn) { vd[i] = o.vd[i]; i = i + 1; }
            vn = o.vn;
        }
    }
    ~vector() { free(vd); vd = 0; vn = 0; vcap = 0; }
    int size() { return vn; }
    int empty() { if (vn == 0) { return 1; } return 0; }
    void reserve(int c) {
        if (c > vcap) {
            int m = c;
            T *nd = (T *)realloc(vd, (unsigned long)m * sizeof(T));
            if (nd != 0) { vd = nd; vcap = m; }
        }
    }
    void push_back(T v) {
        if (vn == vcap) {
            int m = vcap * 2;
            if (m < 4) { m = 4; }
            reserve(m);
        }
        if (vn < vcap) { vd[vn] = v; vn = vn + 1; }
    }
    void pop_back() { if (vn > 0) { vn = vn - 1; } }
    void clear() { vn = 0; }
    T get(int i) { return vd[i]; }
    void set(int i, T v) { vd[i] = v; }
    T *ptr(int i) { return vd + i; }
    T &operator[](int i) { return vd[i]; }
};
"""

# The owning sibling of `vector`. It exists separately rather than as a
# smarter `vector` because the two need different *parameter conventions*:
# a scalar element wants `push_back(T v)` (you write `v.push_back(3)`, and
# `3` has no address), while an owning element must not cross a call
# boundary by value at all and wants `push_back(const T &v)`. One template
# body cannot spell both, so there are two, each honest about what it takes.
#
# The element copy and destroy go through `__cpp_copy` / `__cpp_drop`, which
# is the whole reason those builtins exist: `T` substitutes to a class name
# but `T_copy` does not, since substitution rewrites whole words.
_STD_OWNVECTOR = """
template<typename T>
class ownvector {
public:
    T *od;
    int on;
    int ocap;
    ownvector() { od = 0; on = 0; ocap = 0; }
    ~ownvector() { clear(); free(od); od = 0; ocap = 0; }
    int size() { return on; }
    int empty() { if (on == 0) { return 1; } return 0; }
    void reserve(int c) {
        if (c > ocap) {
            int m = c;
            T *nd = (T *)realloc(od, (unsigned long)m * sizeof(T));
            if (nd != 0) { od = nd; ocap = m; }
        }
    }
    void push_back(const T &v) {
        if (on == ocap) {
            int m = ocap * 2;
            if (m < 4) { m = 4; }
            reserve(m);
        }
        if (on < ocap) { __cpp_copy(T, od[on], v); on = on + 1; }
    }
    void pop_back() { if (on > 0) { on = on - 1; __cpp_drop(T, od[on]); } }
    void clear() { while (on > 0) { on = on - 1; __cpp_drop(T, od[on]); } }
    T *ptr(int i) { return od + i; }
    T &operator[](int i) { return od[i]; }
};
"""

_STD_INCLUDE = re.compile(r"^[ \t]*#\s*include\s*<(vector|string)>[ \t]*\n?",
                          re.M)


_STD_CLASSES = frozenset(("string", "vector", "ownvector"))


def _std_prelude(text):
    """Strip `std::`, drop `#include <vector|string>`, and supply the classes.

    Returns the rewritten source. `string` is emitted before `vector` so that
    a `vector<string>` finds it complete -- the same declaration-order rule
    every other nested instantiation obeys.
    """
    wanted = set(m.group(1) for m in _STD_INCLUDE.finditer(text))
    probe = _blank_strings(_strip_comments(text))
    for name in ("string", "vector", "ownvector"):
        if re.search(r"\bstd\s*::\s*%s\b" % name, probe):
            wanted.add(name)
    if not wanted:
        return text
    text = _STD_INCLUDE.sub("", text)
    text = _sub_code(re.compile(r"\bstd\s*::\s*"), "", text)
    if "vector" in wanted or "ownvector" in wanted:
        # `vector<string>` needs `string`; supplying it is cheaper than
        # working out whether this source asks for that combination.
        wanted.add("string")
    parts = [_STD_DECLS]
    if "string" in wanted:
        parts.append(_STD_STRING)
    if "vector" in wanted:
        parts.append(_STD_VECTOR)
    if "ownvector" in wanted:
        parts.append(_STD_OWNVECTOR)
    return "".join(parts) + text


_LAMBDA = re.compile(r"\[([^\]]*)\]\s*\(([^()]*)\)\s*(?:->\s*([\w ]+(?:\s*\*)*)\s*)?\{")
_AUTO_LAMBDA = re.compile(r"(?<![\w.])auto\s+(\w+)\s*=\s*$")


_CONTROL = frozenset(("if", "while", "for", "switch"))


def _stmt_start(text, idx):
    """`(start, None)` for the statement containing `idx`, or `(None, why)`.

    An inlined lambda body is a block, and a block cannot sit inside an
    expression, so the expansion is hoisted to just before the statement
    that contains the call. That is only sound where the call is evaluated
    exactly once and unconditionally: a loop condition re-evaluates it, and
    an operand of `&&`, `||` or `?:` may not evaluate it at all.
    """
    depth, j = 0, idx - 1
    while j >= 0:
        c = text[j]
        if c in ")]":
            depth += 1
        elif c in "([":
            if depth == 0:
                word = _prev_word(text, j)
                if word in _CONTROL:
                    return None, ("the controlling expression of `%s`" % word)
                depth = 0        # an enclosing call's argument list
            else:
                depth -= 1
        elif depth == 0 and c in ";{}":
            seg = text[j + 1:idx]
            for op in ("&&", "||", "?"):
                if op in seg:
                    return None, "an operand of `%s`" % op
            return j + 1, None
        j -= 1
    return 0, None


def _enclosing_end(text, pos):
    """Index of the `}` closing the block that `pos` sits in."""
    depth = 0
    for k in range(pos, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            if depth == 0:
                return k
            depth -= 1
    return len(text)


def _toplevel_start(text, idx):
    """Index where the top-level declaration containing `idx` begins.

    A generated function has to be defined before the code that names it,
    but after anything that code depends on. The enclosing top-level
    declaration is the nearest point satisfying both.
    """
    depth, bound = 0, 0
    for k, c in enumerate(text[:idx]):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth <= 0:
                depth = 0
                bound = k + 1
        elif c == ";" and depth == 0:
            bound = k + 1
    return bound


def _param_types(params):
    """Just the types from a parameter list, for a function pointer type."""
    out = []
    for part in _split_top(params or ""):
        part = part.strip()
        if not part or part == "void":
            continue
        toks = part.replace("*", " * ").split()
        # Drop the declared name; keep everything that spells the type.
        if len(toks) > 1 and toks[-1] not in ("*",):
            toks = toks[:-1]
        out.append(" ".join(toks).replace(" *", " *"))
    return ", ".join(out) or "void"


def _local_type(text, start, end, name):
    """The declared type of local `name` between `start` and `end`, or None.

    Used only for a by-value capture, which is a copy made where the lambda
    is written and therefore needs a type to declare. The declaration is
    looked up rather than guessed: if the name is declared more than once
    with different types, or not found at all, this returns None and the
    capture is refused. A wrong type here would silently truncate, which is
    exactly the kind of guess the rest of this lowering does not make.
    """
    pat = re.compile(
        r"(?<![\w.>])((?:const\s+)?[A-Za-z_]\w*(?:\s*\*)*)\s+%s\s*(?=[;,=)\[])"
        % re.escape(name))
    found = set()
    for m in pat.finditer(text, start, end):
        ty = " ".join(m.group(1).split())
        if ty.split()[-1].strip("*") in ("return", "else", "case", "auto"):
            continue
        found.add(ty)
    return found.pop() if len(found) == 1 else None


def _inline_lambda(text, look, m, close, captures, params, ret, body, n,
                   path):
    """Expand one call of a by-reference capturing lambda, in place.

    A capture needs the captured variable's *type* if it is to become a
    field, and that type is an ordinary local this pass cannot see. Inlining
    sidesteps the question entirely: put the body where the call is, and the
    captured variables are simply in scope. Nothing has to be named.

    A lambda `return` must leave the lambda, not the enclosing function, so
    the body goes inside `do { } while (0)` and `return` becomes `break`.
    That is a structured jump rather than a label, which matters here: the
    destructor unwinding already understands `break` -- it walks out to the
    enclosing loop frame dropping what is live -- whereas `goto` is refused
    outright whenever anything is live, which is most RAII code.

    One call site per invocation; the caller loops. Splicing invalidates
    every index, and rescanning is cheaper to be sure of than an offset.
    """
    where = "%s:%d" % (os.path.basename(path),
                       look.count("\n", 0, m.start()) + 1)
    am = _AUTO_LAMBDA.search(look[:m.start()])
    if am is None:
        raise CppError(
            "%s: a capturing lambda has to be bound to a name "
            "(`auto f = [&](..) -> T { .. };`) -- it is inlined at its call "
            "sites, so there has to be a name to find them by." % where)
    semi = look.find(";", close)
    if semi < 0:
        raise CppError("%s: lambda declaration without a `;`" % where)
    name = am.group(1)

    # A by-value capture is a copy taken where the lambda is written, so it
    # becomes a snapshot local declared there and the body reads that
    # instead. Its type is looked up from the declaration; `[=]` is refused
    # because it names nothing to look up.
    fn_start = _toplevel_start(look, am.start())
    fn_end = _enclosing_end(look, semi + 1)
    snaps = []
    for cap in captures.split(","):
        cap = cap.strip()
        if not cap or cap.startswith("&"):
            continue
        if cap == "=":
            raise CppError(
                "%s: `[=]` captures everything by value, and a by-value "
                "capture has to be declared, which means naming it. List the "
                "variables (`[x, y]`), or capture by reference (`[&]`)."
                % where)
        if not re.match(r"^\w+$", cap):
            raise CppError("%s: cannot parse capture `%s`" % (where, cap))
        ty = _local_type(look, fn_start, fn_end, cap)
        if ty is None:
            raise CppError(
                "%s: `%s` is captured by value, but its declaration is not "
                "findable here (or is ambiguous), and a copy has to be "
                "declared with a type. Capture it by reference (`[&%s]`), or "
                "pass it as a parameter." % (where, cap, cap))
        snap = "_cpp_cap_%s_%s" % (name, cap)
        snaps.append((cap, snap, ty))

    body = _sub_code(
        re.compile(r"(?<![\w.>])(%s)(?![\w])"
                   % "|".join(re.escape(c) for c, _s, _t in snaps)),
        lambda mm: dict((c, s) for c, s, _t in snaps)[mm.group(1)],
        body) if snaps else body

    probe = _blank_strings(_strip_comments(body))
    if re.search(r"(?<![\w.>])%s\s*\(" % re.escape(name), probe):
        raise CppError(
            "%s: `%s` calls itself; an inlined lambda cannot recurse."
            % (where, name))
    if re.search(r"(?<![\w.>])return\b", probe) and \
            re.search(r"(?<![\w.>])(while|for|switch|do)\b", probe):
        raise CppError(
            "%s: `%s` returns from inside a loop or switch. The body is "
            "inlined and `return` becomes `break`, which would leave only "
            "that loop. Move the body into a function." % (where, name))

    region_end = _enclosing_end(look, semi + 1)
    call = re.compile(r"(?<![\w.>])%s\s*\(" % re.escape(name))
    hit = call.search(look, semi + 1, region_end)
    if hit is None:
        # Every call has been expanded. The declaration has no meaning in C,
        # but the by-value snapshots it stood for do -- they are the copies
        # taken at this point, and the expansions read them.
        rest = look[semi + 1:region_end]
        if re.search(r"(?<![\w.>])%s(?![\w])" % re.escape(name), rest):
            raise CppError(
                "%s: `%s` is used as a value. A capturing lambda is inlined "
                "at its call sites, so it has no representation to pass "
                "around -- use a non-capturing lambda for a callback."
                % (where, name))
        keep = " ".join("%s %s = %s;" % (ty, snap, cap)
                        for cap, snap, ty in snaps)
        return text[:am.start()] + keep + text[semi + 1:], n

    op = hit.end() - 1
    cclose = _match_paren(look, op)
    if cclose is None:
        raise CppError("%s: unterminated call to `%s`" % (where, name))
    start, why = _stmt_start(look, hit.start())
    if start is None:
        raise CppError(
            "%s: `%s` is called from %s. The body is inlined before the "
            "statement, which is only sound where the call runs exactly "
            "once -- assign it to a local first." % (where, name, why))

    args = [a.strip() for a in _split_top(text[op + 1:cclose])]
    decls = []
    for idx, p in enumerate(_split_top(params)):
        p = p.strip()
        if not p or p == "void":
            continue
        if idx >= len(args) or not args[idx]:
            raise CppError("%s: `%s` called with too few arguments"
                           % (where, name))
        # The parameter list carries its own types, so the arguments need no
        # inference -- unlike the captures.
        decls.append("%s = %s;" % (p, args[idx]))

    uid = "_cpp_lam%d" % n
    res = "%s_r" % uid
    inner = _sub_code(re.compile(r"(?<![\w.>])return\s*;"), "break;", body)
    if ret != "void":
        inner = _sub_code(
            re.compile(r"(?<![\w.>])return\s+([^;]+);"),
            lambda mm: "{ %s = %s; break; }" % (res, mm.group(1).strip()),
            inner)
        head = "%s %s; " % (ret, res)
        repl = res
    else:
        head = ""
        repl = "(void)0"
    block = "%sdo { %s%s } while (0); " % (head, " ".join(decls), inner)
    return (text[:start] + block + text[start:hit.start()] + repl +
            text[cclose + 1:]), n + 1


def _lower_lambdas(text, path):
    """`[](int y) -> int { .. }` becomes a static function.

    A lambda with no captures is exactly a function, so that is what it
    lowers to -- and because C already has function pointers, an `auto`
    binding becomes one and the call site needs no rewriting at all.

    A *capturing* lambda is refused. It would need a generated class with a
    field per capture, and this pass does not know the captured variable's
    type: it is an ordinary local, which may be plain C that no class table
    describes. Guessing the type is exactly the kind of thing the rest of
    this lowering refuses to do.

    A return type must be spelled (`-> int`) when the body returns a value.
    C++ deduces it from the body; nothing here can, and defaulting to `int`
    would silently truncate a `double`.
    """
    n, pos = 0, 0
    while True:
        look = _blank_strings(_strip_comments(text))
        m = _LAMBDA.search(look, pos)
        if m is None:
            return text
        if _prev_word(look, m.start()) == "operator":
            # `operator[](int i) { .. }` is a subscript overload, not a
            # lambda with an empty capture list.
            pos = m.end()
            continue
        pos = 0
        captures = m.group(1).strip()
        close = _match_brace(look, look.index("{", m.end() - 1))
        if close is None:
            raise CppError("unterminated lambda body")
        params = m.group(2).strip()
        ret = (m.group(3) or "void").strip()
        body = text[m.end():close]
        if captures:
            text, n = _inline_lambda(text, look, m, close, captures, params,
                                     ret, body, n, path)
            continue
        name = "_cpp_lambda%d" % n
        n += 1
        fn = "\nstatic %s %s(%s) {%s}\n" % (ret, name, params or "void",
                                             body)

        # `auto f = [](..){..};` binds a function pointer, which is the C
        # spelling of exactly this.
        head = look[:m.start()]
        am = _AUTO_LAMBDA.search(head)
        tail = close + 1
        if am is not None:
            semi = look.find(";", close)
            repl = "%s (*%s)(%s) = %s" % (ret, am.group(1),
                                          _param_types(params), name)
            start, tail = am.start(), (semi if semi >= 0 else close + 1)
        else:
            repl, start = name, m.start()
        at = _toplevel_start(look, start)
        text = text[:at] + fn + text[at:start] + repl + text[tail:]
    return text


def translate(text, path="<cpp>"):
    """Translate a C++ subset source to C. Raises CppError on anything else."""
    # `std::string` / `std::vector` are supplied as ordinary subset source,
    # so everything below sees one file with no special cases in it.
    text = _std_prelude(text)
    std_classes = _STD_CLASSES
    # Lambdas are lowered before anything else looks at the file: what comes
    # out is ordinary subset source with a static function in it.
    text = _lower_lambdas(text, path)
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
    # Which class, at which argument count? An allocator is emitted per
    # constructor the source actually applies `new` to, so an unused arity
    # does not leave an unused function behind.
    new_used = {}
    for hm in re.finditer(r"(?<![\w.>])new\s+(\w+)\s*", heap):
        ar, at = 0, hm.end()
        if heap[at:at + 1] == "<":
            # This scan runs before monomorphisation, so `new Box<int>(3)`
            # still carries its template arguments.
            ang = _match_angle(heap, at)
            at = (ang + 1) if ang is not None else at
            while at < len(heap) and heap[at] in " \t":
                at += 1
        if heap[at:at + 1] == "(":
            hclose = _match_paren(heap, at)
            if hclose is not None:
                ar = _arity(heap[at + 1:hclose])
        new_used.setdefault(hm.group(1), set()).add(ar)
    # Method names the source invokes on a call result. A virtual one needs
    # a single-evaluation dispatch helper, because the plain form names the
    # receiver twice and a call receiver must not run twice. The pattern is
    # exactly the chained-call syntax, so this neither misses a case the
    # rewriter will take nor is it worth narrowing further.
    chained = set(re.findall(r"\)\s*(?:\.|->)\s*(\w+)\s*\(", heap))
    uses_heap = bool(new_used) or bool(
        re.search(r"(?<![\w.>])delete\b", heap))
    declared = set(cls.name for _s, _e, cls in classes)
    # A template is matched by its bare name, so every instantiation of it
    # gets the allocators its uses ask for.
    for tname in sorted(set(new_used) - declared):
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

    # A template body may instantiate another template: `Outer<T>` holding an
    # `Inner<T>` asks for `Inner<int>` only once `T` is known. So the set is
    # closed transitively -- substitute each instantiation's arguments into
    # its own body and scan that for further uses, until nothing new appears.
    # The class supplying a nested instantiation still has to be declared
    # above the one that needs it, since classes are emitted in order.
    tspan = dict((cls.name, (s, e, cls))
                 for s, e, cls in classes if cls.tparams)
    cindex = dict((cls.name, idx)
                  for idx, (_s, _e, cls) in enumerate(classes))
    pending = [(n, t) for n in list(wanted) for t in list(wanted[n])]
    seen = set(pending)
    while pending:
        name, targs = pending.pop()
        s, e, cls = tspan[name]
        body = _subst_type(scan[s:e], cls.tparams, targs)
        found = []
        _monomorphise_uses(body, tnames,
                           lambda n2, t2: found.append((n2, t2)))
        for pair in found:
            if pair[0] != name and cindex[pair[0]] > cindex[name]:
                raise CppError(
                    "class %s: it instantiates `%s`, which is declared below "
                    "it. A nested instantiation has to be complete first."
                    % (name, pair[0]))
            record(*pair)
            if pair not in seen:
                seen.add(pair)
                pending.append(pair)

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
                cls, names, cinfo, tsub, targs, new_used.get(cls.name),
                chained, cls.name in std_classes)
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
    # `vector<T>` stores elements by assignment, which for an owning class
    # would leave two objects holding one resource. Caught here, against the
    # element type the source asked for, rather than as a by-value complaint
    # about a `push_back` the author never wrote.
    for targs in wanted.get("vector", []):
        elem = targs[0]
        if elem in cinfo and cinfo[elem]["dtor"]:
            raise CppError(
                "%s: `vector<%s>` stores its elements by assignment, and %s "
                "has a destructor -- two elements would own one resource. "
                "Use `ownvector<%s>`, which copy-constructs each element, "
                "or `vector<%s *>` with `new`/`delete`."
                % (os.path.basename(path), elem, elem, elem, elem))

    # After reference lowering, a class still spelled by value really is by
    # value -- a `T &` the author wrote is a `T *` by now.
    _check_by_value(out, cinfo, path)
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
