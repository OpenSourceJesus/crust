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

C++11 spellings -- `auto`, range-`for`, namespaces, `unique_ptr` and
`shared_ptr` -- are handled in `tools/cpp_auto.py` and the supplied templates
below. None of them widen what the subset expresses: each is rewritten into
something this lowering already handled, before any pass that reads types
runs, because everything downstream reads types by how they are written.

A class may therefore *own* a Crust value rather than point at one. Crust
publishes the types it lowered that own something and the preprocessor passes
them as `--owning Name:dropfn,..`, since this module runs as a subprocess and
cannot see the unit being compiled. A member of such a type is destroyed with
its container -- so a class holding only Crust values needs no destructor at
all -- and the copy rules apply to it, so copying one without a copy
constructor is refused for the same reason as any other owning class. Without
the mapping nothing changes and the member is plain data.

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
  * a by-value owning *parameter* is an object the callee owns: it is
    constructed at the call -- moved in when the site writes `std::move`,
    copied otherwise -- and dropped on every exit from the function, which
    is what C++ does. An argument whose class has no copy constructor is an
    error naming `std::move`. A by-value *return* is still refused unless it
    returns a bare local, since the local is destroyed on the way out and
    the caller would receive a copy of a released object. A class with no
    destructor owns nothing and passes by value freely.
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

try:
    import tools.cpp_auto as cpp_auto
except ImportError:                      # run as a script from tools/
    import cpp_auto


class CppError(Exception):
    """A C++ subset translation error."""

    def __init__(self, message):
        self.args = (message,)
        self.message = message


#: `a += b` becomes `T__augadd(&a, &b)`. Spelled out rather than punctuated
#: because the symbol has to be a C identifier, and `__augadd` reads back to
#: the operator it came from.
_AUG_NAMES = {"+": "add", "-": "sub", "*": "mul", "/": "div", "%": "mod",
              "|": "or", "&": "and", "^": "xor"}

#: `a == b` becomes `T__cmpeq(&a, &b)`. Same reasoning as `_AUG_NAMES`: the
#: symbol has to be a C identifier and should read back to its operator.
_CMP_NAMES = {"==": "eq", "!=": "ne", "<=": "le", ">=": "ge",
              "<": "lt", ">": "gt"}

_AUG_ASSIGN_SPELLINGS = frozenset(
    ["%s=" % k for k in ("+", "-", "*", "/", "%", "|", "&", "^")])

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


def _blank_directives(text):
    """Blank preprocessor directive lines, keeping length and newlines.

    A directive is not code, and its replacement text is not an
    expression this file evaluates. Reading one as code made litehtml's

        #define t_to_string(val)   std::to_string(val)

    look like a call handing a `string` over by value -- the macro's own
    parameter `val` resolving against an unrelated local of that name
    somewhere else in the file. That refusal fired on 22 of 43 sources,
    every one of them for a line no compiler would ever evaluate here.

    Blanked rather than removed, and only in the *scan*: the directives
    themselves still reach the output, where ShivyCX expands them.
    Continuation lines go too, since a `\\` carries the directive on.
    """
    out, i, n = [], 0, len(text)
    while i < n:
        j = text.find("\n", i)
        j = n if j < 0 else j
        line = text[i:j]
        if line.lstrip().startswith("#"):
            out.append(" " * len(line))
            # A trailing backslash continues the directive onto the next
            # line, which is just as much not code as the first.
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
        if m.group(1) in ("=", "[]", "[", "->", "*"):
            continue
        if m.group(1) in _AUG_ASSIGN_SPELLINGS:
            continue
        if m.group(1) in _CMP_NAMES:
            continue
        line = scan.count("\n", 0, m.start()) + 1
        # A *conversion* operator is worth naming separately: it is not one
        # more overload to add but a different kind of thing. It applies
        # where the compiler decides a conversion is wanted, so lowering it
        # means knowing the type every expression is used at -- and this pass
        # reads types by how they are written. Spelled out rather than
        # lumped in with `operator<`, which really is just missing.
        spelled = m.group(1)
        # A conversion operator names a *type*, which may start with `const`.
        if re.match(r"^[A-Za-z_]\w*$", spelled) \
                and (spelled == "const" or spelled not in _KEYWORDS):
            # A conversion operator is *declarable*: it lowers to an ordinary
            # method. What is limited is where the call can be inserted, and
            # that is reported at the use rather than here -- litehtml has
            # exactly one, in a header every file includes, so refusing the
            # declaration refused forty files over two call sites.
            continue
        if False:
            raise CppError(
                "%s:%d: `operator %s()` is a conversion operator, which is "
                "not in the C++ subset. It applies wherever the compiler "
                "decides a conversion is wanted, and this pass reads types "
                "from how they are written -- so there is no honest way to "
                "know where to insert the call. Give the class a named "
                "method and call it."
                % (os.path.basename(path), line, spelled))
        raise CppError(
            "%s:%d: `operator%s` is not in the C++ subset. It supports "
            "`operator=`, a compound assignment (`+=` and friends), "
            "`operator[]`, `operator->` and `operator*`."
            % (os.path.basename(path), line, spelled))
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
                 "init", "virt", "pure", "outline", "definit",
                 "declared_only", "stat")

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
        # Declared `static`: a member function with no receiver. It is
        # emitted without a `this` parameter and called as `Cls::name(..)`
        # rather than through an object.
        self.stat = False
        # Defined out of line, under a qualified name. Its body is emitted
        # where the author wrote it rather than at the class, so a body that
        # reads a file-scope name declared between the two still sees it.
        self.outline = False
        # A C++11 default member initializer: `int x = 5;` or `int x {5};`.
        # C has no such thing on a struct member, so it becomes an assignment
        # at the top of every constructor -- which is what it means.
        self.definit = None
        # Declared with no body and no out-of-line definition here: it lives
        # in another translation unit, so only a prototype is emitted.
        self.declared_only = False


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
        # `member(args)` or C++11's `member{args}`. The braces mean list
        # initialisation, which for everything this subset lowers -- a
        # constructor call or a scalar -- is the same call with the same
        # arguments, so only the spelling differs.
        m = re.match(r"^(\w+)\s*([({])", part)
        if m is None:
            raise CppError("cannot parse initializer %r in class %s"
                           % (part, cname))
        open_ch = m.group(2)
        close_ch = ")" if open_ch == "(" else "}"
        end = _match(part, m.end() - 1, open_ch, close_ch)
        if end is None:
            raise CppError("cannot parse initializer %r in class %s"
                           % (part, cname))
        out.append((m.group(1), part[m.end():end].strip()))
    return out


def _body_brace(body, start, brace):
    """Index of the brace opening a member's body, skipping initializers.

    Only when an initializer list is actually present: outside one, a `{`
    preceded by a name is an anonymous `union`/`struct` member, which is a
    different thing and must not be skipped.
    """
    head = body[start:brace]
    close = head.rfind(")")
    if close < 0 or ":" not in head[close:]:
        return brace
    k = brace
    while k >= 0 and k < len(body):
        j = k - 1
        while j >= 0 and body[j] in " \t\r\n":
            j -= 1
        if j < 0 or not (body[j].isalnum() or body[j] == "_"):
            return k                     # the body
        end = _match(body, k, "{", "}")
        if end is None:
            return -1
        k = body.find("{", end + 1)
        if k < 0:
            return -1
    return -1


def _member_symbol(cname, m):
    """The C name a member lowers to, or None if it has no simple one."""
    if m.kind == "ctor":
        return "%s_new" % cname
    if m.kind == "dtor":
        return "%s_drop" % cname
    if m.kind == "method":
        return "%s_%s" % (cname, m.name)
    return None


#: The heads that may hold a declaration inside their parentheses. C++
#: lets a condition declare a name -- `if (auto *p = f())` -- and that
#: name is in scope for the branch, so it has to reach the symbol table
#: the same way a `for` initialiser does. `switch` is here for the same
#: reason; an ordinary argument list is none of these and is untouched.
_DECL_HEADS = ("for", "if", "while", "switch")


def _in_for_head(look, i, heads=_DECL_HEADS):
    """Is `i` inside the parentheses of a head that may declare a name?

    Walks back to the `(` that is still open and checks the word before it.
    Cheap because it only runs where a declaration pattern already matched.
    """
    depth = 0
    k = i - 1
    while k >= 0:
        c = look[k]
        if c == ")":
            depth += 1
        elif c == "(":
            if depth == 0:
                return _prev_word(look, k) in heads
            depth -= 1
        elif c in ";{}" and depth == 0:
            return False
        k -= 1
    return False


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


def _pure_virtual(decl, cname, line0):
    """Parse `virtual int area() = 0;` -- a slot with no implementation."""
    body = decl[len("virtual"):].strip()
    op = body.find("(")
    cp = _match_paren(body, op) if op >= 0 else None
    if op < 0 or cp is None:
        raise CppError("cannot parse virtual member %r in class %s"
                       % (decl, cname))
    tail = body[cp + 1:].strip()
    # A trailing `const` sits between the parameter list and the `= 0`, and
    # it says the same thing here as anywhere else: nothing this lowering
    # needs to model.
    tail = re.sub(r"^(?:const|override|final|noexcept)\b\s*", "", tail)
    tail = re.sub(r"^(?:const|override|final|noexcept)\b\s*", "", tail)
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


def _drop_trailing_const(head):
    """Remove the qualifiers that follow a member function's parameters.

    `const` says what the body may do; `override` and `final` say what the
    class hierarchy may do. All three are checked by the language rather than
    lowered, and the C front end checks the body regardless -- so what is
    left after the parameter list is dropped.
    """
    op = head.find("(")
    if op < 0:
        return head
    cp = _match_paren(head, op)
    if cp is None:
        return head
    tail = head[cp + 1:]
    new_tail = re.sub(r"(?<![\w])(?:const|override|final|noexcept)(?![\w])",
                      "", tail)
    return head[:cp + 1] + new_tail


def _top_level_eq(decl):
    """Index of an `=` that starts an initializer, or -1.

    Not one inside brackets -- a default template argument or an array
    dimension can hold one -- and not `==`.
    """
    depth = 0
    for k, c in enumerate(decl):
        if c in "([{<":
            depth += 1
        elif c in ")]}>":
            depth -= 1
        elif c == "=" and depth == 0:
            if decl[k + 1:k + 2] == "=" or decl[k - 1:k] in ("=", "!", "<",
                                                             ">"):
                continue
            return k
    return -1


def _has_param_list(decl):
    """Does this `;`-terminated member declaration have a parameter list?

    `void draw()` does; `int width` does not; `int (*fn)(int)` is a function
    *pointer field* and does not either -- the parens belong to the
    declarator, not to the member.
    """
    op = decl.find("(")
    if op < 0:
        return False
    if decl[op + 1:].lstrip().startswith("*"):
        return False
    return bool(re.match(r"^[~\w][\w:<>,&*\s]*$", decl[:op].strip() or "~"))


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
            if decl.startswith("virtual") and decl.rstrip().endswith("0"):
                members.append(_pure_virtual(decl, cname, line0))
                continue
            # A parameter list makes this a *declaration* of a member defined
            # out of line -- `void draw();` in a header against
            # `void Class::draw() {..}` in the source. It classifies exactly
            # as the inline form does, so it goes through the same code with
            # no body; the body is attached once the out-of-line definitions
            # have been read.
            if not _has_param_list(decl):
                definit = None
                eq = _top_level_eq(decl)
                if eq >= 0:
                    definit = decl[eq + 1:].strip()
                    decl = decl[:eq].strip()
                parts = decl.replace("*", " * ").split()
                if len(parts) < 2:
                    raise CppError("cannot parse member %r in class %s"
                                   % (decl, cname))
                # `int arr[10];` -- the declarator suffix is not part of the
                # name. Keeping it there would make field qualification miss
                # every use of `arr` in a method body.
                fname, dim = parts[-1], ""
                b = fname.find("[")
                if b >= 0:
                    fname, dim = fname[:b], fname[b:]
                fm = Member("field", " ".join(parts[:-1]), fname,
                            None, None, line0, dim)
                fm.definit = definit
                members.append(fm)
                continue
            head, inner = decl, None
        else:
            if brace < 0:
                break
            # A C++11 initializer list may use braces -- `: d { p }, n(k)` --
            # and the first `{` after the parameters is then an *initializer*
            # rather than the body. Told apart by what precedes it: an
            # initializer brace follows the member's name, the body brace
            # follows a `)` or the `}` that closed the last initializer.
            brace = _body_brace(body, start, brace)
            if brace < 0:
                break
            head = body[start:brace].strip()
            close = _match_brace(body, brace)
            if close is None:
                raise CppError("unterminated method body in class %s" % cname)
            inner = body[brace + 1:close]
            i = close + 1
        # A trailing `const` on a member function is a promise about what
        # the body does, not part of the signature this lowers: `this` is a
        # pointer either way, and the C front end checks the body regardless.
        # Dropped rather than modelled, and only *after* the parameter list,
        # so a `const` return type or parameter is untouched.
        head = _drop_trailing_const(head)
        # `explicit` constrains implicit conversion, which this lowering does
        # not perform in the first place: every construction is written out.
        head = re.sub(r"(?<![\w])explicit(?![\w])\s*", "", head)
        # `final` says a class may not be derived from, and nothing here
        # derives from anything it is not told about.
        head = re.sub(r"(?<![\w])final(?![\w])\s*", "", head)
        # An anonymous `union { .. };` (or `struct { .. };`) member. C has
        # them and ShivyCX lowers them, so this is a matter of carrying the
        # group through and registering the names inside it -- a body writing
        # `m_value` means `this->m_value` exactly as it would for a plain
        # field, and the qualification pass has to know that.
        # `T name { .. };` -- a default member initializer written with
        # braces. It reaches here because the `{` comes before the `;`, but
        # it is a field, not a method: there is no parameter list.
        bm = (re.match(r"^([A-Za-z_][\w:<>,\s*&]*?)\s*(\w+)$", head)
              if inner is not None and "(" not in head
              and not re.match(r"^(union|struct)\s*\w*$", head) else None)
        if bm:
            j = close + 1
            while j < n and body[j] in " \t\r\n":
                j += 1
            if j < n and body[j] == ";":
                i = j + 1
            fname, dim = bm.group(2), ""
            b = fname.find("[")
            if b >= 0:
                fname, dim = fname[:b], fname[b:]
            fm = Member("field", bm.group(1).strip(), fname, None, None,
                        line0, dim)
            # `""` rather than `None`: `T x {};` is value-initialisation,
            # which is a request, and telling it from "no initializer at
            # all" is the difference between zeroing the member and leaving
            # it alone.
            fm.definit = inner.strip()
            members.append(fm)
            continue

        anon = (re.match(r"^(union|struct)\s*(\w*)$", head)
                if inner is not None else None)
        if anon:
            # A trailing declarator makes it a *named* member of an anonymous
            # type -- `union { .. } u;` -- which is a different thing from an
            # anonymous member: `u.field`, not `field`. Both are C, and both
            # are carried through whole; only the unnamed one contributes its
            # members' names to the class.
            j = close + 1
            while j < n and body[j] in " \t\r\n":
                j += 1
            k = j
            while k < n and (body[k].isalnum() or body[k] == "_"):
                k += 1
            vname = body[j:k]
            if vname:
                i = k
            members.append(Member("anon",
                                  (anon.group(1) + " " + anon.group(2)).strip(),
                                  vname, None, inner, line0))
            continue
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
        elif re.search(r"\boperator\s*->$", sig):
            # `T *operator->()`. C++ applies it repeatedly until something
            # that is not a class comes back; a smart pointer returns a plain
            # `T *` on the first hop, which is the only shape here.
            bits = sig[:sig.index("operator")].strip()
            members.append(Member("arrow", bits, "operator->", params,
                                  inner, line0))
        elif re.search(r"\boperator\s*\*$", sig):
            # `T &operator*()`. Lowered like `operator[]`: the reference
            # return becomes a pointer and the dereference is written back at
            # the use, so `*p = x` still assigns through.
            bits = sig[:sig.index("operator")].strip()
            members.append(Member("star", bits, "operator*", params,
                                  inner, line0))
        elif re.match(r"^operator\s+(?:const\s+)?[A-Za-z_][\w:]*"
                      r"\s*[*&]*$", sig) and not params:
            # `operator T()`. The lowered form is an ordinary method that
            # returns `T`; only the *implicit* application is limited.
            # `operator const T &()` returns a reference, and a reference is
            # a pointer by the time this lowers -- so the `&` is kept and the
            # normal reference handling applies.
            members.append(Member("conv", sig.split(None, 1)[1].strip(),
                                  "operator conv", params, inner, line0))
        elif re.search(r"\boperator\s*(==|!=|<=|>=|<|>)$", sig):
            # A comparison. Unlike an assignment its *result* is the point,
            # so the declared return type is kept.
            cm = re.search(r"\boperator\s*(==|!=|<=|>=|<|>)$", sig)
            bits = sig[:sig.index("operator")].strip()
            members.append(Member("cmp", bits, "operator%s" % cm.group(1),
                                  params, inner, line0))
        elif re.search(r"\boperator\s*(\+|-|\*|/|%|\||&|\^)=$", sig):
            # A compound assignment. Lowered like `operator=`: the result is
            # dropped, so `a += b` is a statement and a chained
            # `c = a += b` is rejected rather than yielding nothing.
            opm = re.search(r"\boperator\s*(\+|-|\*|/|%|\||&|\^)=$", sig)
            members.append(Member("augassign", "void",
                                  "operator%s=" % opm.group(1), params,
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
            # `static` is a storage class, not part of the return type. Left
            # in, it became `static factory` and the method was emitted with
            # a `this` it has no business having.
            is_static = bool(bits) and bits[0] == "static"
            if is_static:
                bits = bits[1:]
            if len(bits) < 2:
                raise CppError("cannot parse method %r in class %s"
                               % (head, cname))
            _m = Member("method", " ".join(bits[:-1]), bits[-1],
                        params, inner, line0, "", None, virt)
            _m.stat = is_static
            members.append(_m)
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


_OUTLINE = re.compile(
    r"(?<![\w:])([A-Za-z_][\w:]*(?:\s*<[^;{}()]*>)?[\s*&]+)?"
    r"([A-Za-z_]\w*)\s*::\s*(~?[A-Za-z_]\w*|operator\s*(?:\[\s*\]|->|\*|=))"
    r"\s*\(")


_QUOTED_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*"([^"]+)"[ \t]*$',
                             re.MULTILINE)

#: Both spellings. Which one was written decides where it is looked for,
#: not whether it is spliced at all: an angle include of a header sitting
#: under an `--incdir` is this project's own, and litehtml includes its
#: headers both ways.
_ANY_INCLUDE = re.compile(
    r'^[ \t]*#[ \t]*include[ \t]*(?:"([^"]+)"|<([^>]+)>)[ \t]*$',
    re.MULTILINE)


#: A conditional this pass is willing to decide. Deliberately small: a
#: name being defined or not, and the literal `0` / `1`. Anything with an
#: operator in it -- `#if A || B`, `#if VER > 2` -- is left alone rather
#: than half-understood, because a wrong answer here silently deletes
#: code rather than reporting anything.
_IFDEF = re.compile(r"^[ \t]*#[ \t]*(ifdef|ifndef)[ \t]+(\w+)[ \t]*$")
_IF_DEFINED = re.compile(
    r"^[ \t]*#[ \t]*if[ \t]+(!)?[ \t]*defined[ \t]*\(?[ \t]*(\w+)"
    r"[ \t]*\)?[ \t]*$")
_IF_LITERAL = re.compile(r"^[ \t]*#[ \t]*if[ \t]+([01])[ \t]*$")
#: A chain of `defined(..)` tests joined by one operator. Real headers
#: guard on a family of names -- litehtml asks
#: `#if defined( WIN32 ) || defined( _WIN32 ) || defined( WINCE )` and
#: puts the rest of the file inside it, so refusing this one shape left
#: everything below it unevaluated. Still only `defined`: a comparison
#: like `_MSC_VER < 1900` needs a value, and this pass has none.
_DEFINED_TERM = re.compile(r"^(!)?[ \t]*defined[ \t]*\([ \t]*(\w+)[ \t]*\)$"
                           r"|^(!)?[ \t]*defined[ \t]+(\w+)$")
_IF_ANY = re.compile(r"^[ \t]*#[ \t]*if(?:def|ndef)?[ \t\(!]")
_ELSE_ANY = re.compile(r"^[ \t]*#[ \t]*el(?:se|if)\b")
_ENDIF = re.compile(r"^[ \t]*#[ \t]*endif\b")
_DEFINE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+(\w+)")
_UNDEF = re.compile(r"^[ \t]*#[ \t]*undef[ \t]+(\w+)")


def _cond_value(line, defines):
    """`True`/`False` for a conditional this pass can decide, else None."""
    m = _IFDEF.match(line)
    if m:
        got = m.group(2) in defines
        return got if m.group(1) == "ifdef" else not got
    m = _IF_DEFINED.match(line)
    if m:
        got = m.group(2) in defines
        return not got if m.group(1) else got
    m = _IF_LITERAL.match(line)
    if m:
        return m.group(1) == "1"
    # `defined(A) || defined(B) || ..`, or the same with `&&`. Mixing the
    # two would need precedence, so a line with both is left undecided
    # rather than answered by evaluation order.
    m = re.match(r"^[ \t]*#[ \t]*if[ \t]+(.*?)[ \t]*$", line)
    if m and "defined" in m.group(1):
        expr = m.group(1)
        if "||" in expr and "&&" in expr:
            return None
        op = "||" if "||" in expr else ("&&" if "&&" in expr else None)
        if op is None:
            return None
        vals = []
        for part in expr.split(op):
            part = part.strip()
            # A redundant wrapping paren is common and means nothing here.
            while part.startswith("(") and part.endswith(")") \
                    and _match_paren(part, 0) == len(part) - 1:
                part = part[1:-1].strip()
            t = _DEFINED_TERM.match(part)
            if not t:
                return None
            neg = t.group(1) or t.group(3)
            name = t.group(2) or t.group(4)
            got = name in defines
            vals.append((not got) if neg else got)
        return any(vals) if op == "||" else all(vals)
    return None


def _eval_conditionals(text, defines):
    """Drop the dead branches of the conditionals this pass can decide.

    A header that defines a type two ways -- litehtml's `os_types.h` gives
    `tstring` as `std::wstring` or `std::string` under
    `#ifndef LITEHTML_UTF8` -- contributes *both* to one translation
    unless the conditional is resolved. Templates were then monomorphised
    over both, producing a `vector_wstring` alongside the real one, over a
    type the subset does not supply.

    The evaluation is deliberately partial, and what it does with a
    condition it cannot decide is the important half: the whole block is
    passed through untouched, directives and all, for the C front end to
    resolve as it always did. So this only ever *narrows* what reaches the
    rest of the pass, and only where the answer is not in doubt. Nothing
    is reported -- an undecidable `#if` is not an error, it is simply not
    this pass's to answer.

    `#define` and `#undef` in live text are tracked, which is what makes
    an include guard resolve and what lets one header decide a later
    one's conditionals.
    """
    if "#" not in text:
        return text
    out = []
    lines = text.split("\n")
    i = 0
    # Each entry: whether this branch's lines are being kept, and whether
    # any branch of this conditional has been taken yet.
    stack = []
    while i < len(lines):
        line = lines[i]
        live = all(s[0] for s in stack)

        if _IF_ANY.match(line):
            val = _cond_value(line, defines) if live else None
            if val is None:
                # Undecidable, or inside a branch already being dropped.
                # Either way the block is copied verbatim -- and skipped
                # over as a unit, so a nested conditional inside it is not
                # evaluated against defines that may not apply.
                depth, j = 0, i
                while j < len(lines):
                    if _IF_ANY.match(lines[j]):
                        depth += 1
                    elif _ENDIF.match(lines[j]):
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                if live:
                    out.extend(lines[i:j + 1])
                i = j + 1
                continue
            stack.append((val, val))
            i += 1
            continue

        if _ELSE_ANY.match(line) and stack:
            keep, taken = stack[-1]
            if line.lstrip().lstrip("#").lstrip().startswith("elif"):
                val = _cond_value(re.sub(r"#\s*elif", "#if", line, count=1),
                                  defines)
                if val is None:
                    # An `#elif` this pass cannot decide, in a conditional
                    # it started to evaluate. Nothing sound is left to do
                    # with the rest of the chain, so the whole conditional
                    # is abandoned: emit what is left of it verbatim.
                    depth, j = 1, i
                    while j < len(lines) and depth > 0:
                        j += 1
                        if j < len(lines) and _IF_ANY.match(lines[j]):
                            depth += 1
                        elif j < len(lines) and _ENDIF.match(lines[j]):
                            depth -= 1
                    stack.pop()
                    if all(s[0] for s in stack):
                        out.extend(lines[i:j + 1])
                    i = j + 1
                    continue
                stack[-1] = ((not taken) and val, taken or val)
            else:
                stack[-1] = (not taken, True)
            i += 1
            continue

        if _ENDIF.match(line) and stack:
            stack.pop()
            i += 1
            continue

        if live:
            m = _DEFINE.match(line)
            if m:
                defines.add(m.group(1))
            m = _UNDEF.match(line)
            if m:
                defines.discard(m.group(1))
            out.append(line)
        i += 1
    return "\n".join(out)


def _expand_headers(text, basedir, incdirs=(), seen=None, depth=0,
                    defines=None):
    """Splice in `#include "x.h"` so a class and its definitions meet.

    A C++ project declares members in a header and defines them in a source
    file that includes it. The two halves have to be in one translation for
    the lowering to work at all -- it emits a class and its bodies together
    -- and the only thing that brings them together is the `#include`.

    Both spellings are spliced, but they are looked for in different
    places, which is what keeps the distinction meaningful. A quoted
    include is searched from the including file's own directory first and
    then the `--incdir` path. An angle one is searched *only* on that
    path, and only spliced if it is found there -- a header that resolves
    under a directory the caller named is this project's own, whichever
    brackets it was written with, and litehtml includes its own headers
    both ways.

    Anything not found under an `--incdir` is left exactly as written, so
    `<string.h>` still goes to the C front end and `<string>` still
    reaches the supplied containers. The rule never widens on its own:
    with no `--incdir` at all, no angle include is ever spliced.

    Each header is spliced once, which is what an include guard would do and
    saves having to understand `#pragma once` or the `#ifndef` idiom. A
    header that cannot be found is left as-is rather than reported: it may
    well be one the C front end can resolve, and this pass is not the
    authority on the include path.
    """
    if seen is None:
        seen = set()
    if defines is None:
        defines = set()
    if depth > 32:
        raise CppError("`#include` nested more than 32 deep; a cycle?")
    # Before the includes are looked for, not after: an `#include` in a
    # branch that is not taken should never be followed, and a header can
    # `#define` a name that decides a later one's conditionals -- which is
    # how litehtml's LITEHTML_UTF8 reaches os_types.h.
    text = _eval_conditionals(text, defines)
    out, last = [], 0
    for m in _ANY_INCLUDE.finditer(text):
        name = m.group(1) or m.group(2)
        angled = m.group(1) is None
        out.append(text[last:m.start()])
        last = m.end()
        # The including file's own directory first, then the search path --
        # the same order a C++ build uses, and the reason a project whose
        # headers live in `include/` rather than beside the source resolves
        # at all. An angle include skips the first of those: it is not
        # relative to the includer, and searching there would make
        # `<string>` mean a file that happened to sit beside the source.
        inner = cand = None
        for d in (list(incdirs) if angled else [basedir] + list(incdirs)):
            trial = os.path.normpath(os.path.join(d, name))
            if trial in seen:
                cand = trial
                break
            try:
                with open(trial, "r") as f:
                    inner = f.read()
                cand = trial
                break
            except IOError:
                continue
        if cand is None:
            out.append(m.group(0))       # not ours to resolve
            continue
        if inner is None:
            continue                     # already spliced: an include guard
        seen.add(cand)
        out.append(_expand_headers(inner, os.path.dirname(cand), incdirs,
                                   seen, depth + 1, defines))
    out.append(text[last:])
    return "".join(out)


def _mangle_targ(arg):
    """A template argument as part of a C identifier.

    `litehtml::document` -> `litehtml_document`, which is also what
    namespace flattening will call that class, so the two agree without
    either knowing about the other.
    """
    arg = re.sub(r"(?<![\w])(?:const|typename|class|struct)(?![\w])", " ", arg)
    arg = arg.replace("::", "_").replace("*", "ptr").replace("&", "ref")
    arg = re.sub(r"[<>,\s]+", "_", arg)
    return arg.strip("_")


def _monomorphise_function_templates(text, scan, path):
    """Emit one ordinary function per instantiation of a function template.

    The subset already monomorphises class templates by writing out a copy
    per instantiation. A function template is the same idea with a smaller
    body, and it is done here the same way -- by substitution, in place, so
    that what comes out is ordinary subset source and every pass below this
    one lowers it without knowing a template was involved.

    That is what makes a *member* template work at no extra cost. litehtml's

        template<class T> void js_register_class(const char* className)

    is a member of `context`, and its body names fields and calls other
    members. Replacing it, where it stands, with one ordinary member per
    instantiation hands the whole problem to the class emitter, which
    already knows how to give a method its `this` and mangle its name.

    An uninstantiated template still emits nothing, which is what C++ does
    with one. A template whose parameters cannot be matched to an
    instantiation is reported rather than guessed at.
    """
    tmpl = []
    for m in re.finditer(r"(?<![\w])template\s*<", scan):
        lt = m.end() - 1
        gt = _match(scan, lt, "<", ">")
        if gt is None:
            continue
        after = gt + 1
        while after < len(scan) and scan[after].isspace():
            after += 1
        if re.match(r"(?:class|struct)(?![\w])", scan[after:after + 6]):
            continue                      # a class template: not ours
        k, depth, body_open = after, 0, None
        while k < len(scan):
            c = scan[k]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            elif c == ";" and depth <= 0:
                break
            elif c == "{" and depth <= 0:
                body_open = k
                close = _match_brace(scan, k)
                if close is None:
                    return text, scan, []
                k = close
                break
            k += 1
        if k >= len(scan):
            continue
        nm = re.search(r"(\w+)\s*\(", scan[after:k + 1])
        if not nm:
            continue
        params = [p.strip().split()[-1]
                  for p in _split_top(scan[lt + 1:gt]) if p.strip()]
        tmpl.append({
            "name": nm.group(1), "params": params,
            "start": m.start(), "end": k + 1,
            "decl_only": body_open is None,
        })
    if not tmpl:
        return text, scan, []

    # Which arguments each one is instantiated with. A member call is
    # `c.reg<int>(..)`, so a leading `.` cannot be excluded.
    out, last, names = [], 0, []
    for t in sorted(tmpl, key=lambda t: t["start"]):
        args = []
        for u in re.finditer(
                r"(?<![\w])%s\s*<([^;{}()]*)>\s*\(" % re.escape(t["name"]),
                scan):
            if t["start"] <= u.start() < t["end"]:
                continue                  # a recursive use inside the body
            got = [a.strip() for a in _split_top(u.group(1)) if a.strip()]
            if len(got) != len(t["params"]):
                raise CppError(
                    "%s:%d: `%s<%s>` gives %d template argument%s to a "
                    "template that takes %d. This pass substitutes them by "
                    "position and has no defaults to fall back on."
                    % (os.path.basename(path),
                       text.count("\n", 0, u.start()) + 1, t["name"],
                       u.group(1).strip(), len(got),
                       "" if len(got) == 1 else "s", len(t["params"])))
            if got not in args:
                args.append(got)
        body = text[t["start"]:t["end"]]
        # Drop the `template<..>` head; what is left is an ordinary
        # function once the parameters are gone.
        head_gt = _match(body, body.index("<"), "<", ">")
        body = body[head_gt + 1:]
        copies = []
        for got in args:
            one = body
            for pname, arg in zip(t["params"], got):
                one = re.sub(r"(?<![\w])%s(?![\w])" % re.escape(pname),
                             arg, one)
            # `typename X::y` is C++ telling the parser that `y` names a
            # type. With `X` known there is nothing left to tell it.
            one = re.sub(r"(?<![\w])typename\s+", "", one)
            suffix = "_".join(_mangle_targ(a) for a in got)
            one = re.sub(r"(?<![\w])%s(?=\s*\()" % re.escape(t["name"]),
                         "%s_%s" % (t["name"], suffix), one, count=1)
            copies.append(one)
            names.append("%s_%s" % (t["name"], suffix))
        out.append(text[last:t["start"]])
        out.append("\n".join(copies) if copies else
                   re.sub(r"[^\n]", " ", text[t["start"]:t["end"]]))
        last = t["end"]
    out.append(text[last:])
    text = "".join(out)

    # And the call sites, now that the copies exist to be called.
    for t in tmpl:
        def _fix(u, _t=t):
            got = [a.strip() for a in _split_top(u.group(1)) if a.strip()]
            return "%s_%s(" % (_t["name"],
                               "_".join(_mangle_targ(a) for a in got))
        text = re.sub(
            r"(?<![\w])%s\s*<([^;{}()]*)>\s*\(" % re.escape(t["name"]),
            _fix, text)
    return text, _strip_comments(text), names


def _blank_literal_braces(text):
    """Blank `{` and `}` inside literals, leaving the rest of them intact.

    A CSS parser writes `_t('{')`, and its strings hold braces too. Counted
    as real ones they made `css::parse_stylesheet` look like it was never
    closed, so its body was never lifted out of line and the bare member
    calls inside it were read as hand-overs to unknown functions.

    Only the braces, not the whole literal: blanking string contents breaks
    monomorphisation of a member template instantiated in a method, because
    a pass below reads the string in `reg<Doc>("Document")` out of this same
    scan. Braces are the only characters that miscount here, so they are the
    only ones that go.
    """
    out, i, n = list(text), 0, len(text)
    while i < n:
        q = text[i]
        if q not in "\"'":
            i += 1
            continue
        j = i + 1
        while j < n and text[j] != q:
            j += 2 if text[j] == "\\" else 1
        if j >= n:
            break                        # unterminated; leave it as it is
        for k in range(i + 1, j):
            if out[k] in "{}":
                out[k] = " "
        i = j + 1
    return "".join(out)


def _extract_out_of_line(text, scan, names):
    """Pull `Ret Class::method(params) { .. }` definitions out of the file.

    C++ projects are laid out with members *declared* in a class and
    *defined* afterwards under a qualified name. Both halves have to be in
    hand before a class is emitted, because the lowering needs the body and
    the declaration in the same place -- so the definitions are lifted out
    here, keyed by class, name and arity, and attached to the member they
    belong to before anything is emitted.

    Only at brace depth zero. A qualified name *inside* a body is a call
    (`Foo::bar()`), and matching those would tear the middle out of a
    function.

    Returns `(text, scan, defs)` with the definitions removed, so the class
    scan that follows sees the file as if the bodies had been written inline.
    """
    defs, cuts, depth, i, n = {}, [], 0, 0, len(scan)
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
        m = _OUTLINE.match(scan, i)
        oname = m.group(2) if m is not None else None
        via_fallback = False
        if oname is not None and oname not in names:
            # Namespace flattening renames the class but leaves an
            # out-of-line declarator alone, so `void el_title::f()` names a
            # class the scan knows as `litehtml_el_title`. Unmatched, the
            # body was never attached -- and a bare call to an inherited
            # method inside it was then read as a hand-over to an unknown
            # function rather than as `this->f()`.
            oname = next((k for k in names if k.endswith("_" + oname)), oname)
            via_fallback = oname in names
        if m is None or oname not in names:
            i += 1
            continue
        op = m.end() - 1
        cp = _match_paren(scan, op)
        if cp is None:
            i += 1
            continue
        # A trailing `const` is a promise about the body, and the body is
        # checked by the C front end either way; the initializer list of an
        # out-of-line constructor is kept for the class emitter.
        tail_start = cp + 1
        brace = scan.find("{", tail_start)
        if brace < 0:
            i += 1
            continue
        between = scan[tail_start:brace]
        if ";" in between or "}" in between:
            i += 1                       # a declaration, not a definition
            continue
        close = _match_brace(scan, brace)
        if close is None:
            if via_fallback:
                # Reached only through the fallback above. Before it existed
                # this head was not recognised at all and was left in place,
                # so skipping is the old behaviour rather than a new failure.
                i += 1
                continue
            raise CppError("unterminated definition of %s::%s"
                           % (m.group(2), m.group(3)))
        cls, name = oname, re.sub(r"\s+", "", m.group(3))
        params = text[op + 1:cp].strip()
        defs[(cls, name, _arity(params))] = {
            "ret": (m.group(1) or "").strip(),
            "params": params,
            "init": text[tail_start:brace],
            "body": text[brace + 1:close],
        }
        cuts.append((m.start(), close + 1))
        i = close + 1
    if not cuts:
        return text, scan, defs
    out_t, out_s, prev = [], [], 0
    for a, b in cuts:
        out_t.append(text[prev:a])
        out_s.append(scan[prev:a])
        # Newlines are kept so every line number below this point is the one
        # the author wrote.
        keep = "\n" * text.count("\n", a, b)
        out_t.append(keep)
        out_s.append(keep)
        prev = b
    out_t.append(text[prev:])
    out_s.append(scan[prev:])
    return "".join(out_t), "".join(out_s), defs


def _attach_out_of_line(cls, defs, path):
    """Give each declared-but-undefined member the body defined for it."""
    for m in cls.members:
        if m.body is not None or m.kind == "field" or m.pure:
            continue
        # A destructor is written `~Counter` where it is defined and recorded
        # as `Counter` on the member, so the key has to be put back together
        # rather than taken from the name.
        spelled = ("~" + cls.name) if m.kind == "dtor" else m.name
        got = defs.get((cls.name, spelled, _arity(m.params or "")))
        if got is None:
            # Declared here, defined in another translation unit -- which is
            # ordinary once headers are spliced: `css_length.h` declares
            # `fromString` and `css_length.cpp` defines it, and a file that
            # merely includes the header sees only the declaration.
            #
            # So it stays a declaration. A *prototype* with no definition is
            # exactly what C does with one, and the linker says so if nothing
            # supplies it. This used to be refused, on the grounds that an
            # empty body would compile and silently do nothing -- which is
            # true, and is why no empty body is emitted either.
            m.declared_only = True
            continue
        m.params = got["params"]
        m.body = got["body"]
        m.outline = True
        if m.kind == "ctor" and got["init"].strip():
            m.init = _parse_init_list(got["init"], cls.name, cls.name)


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


def _derives_from(cls, tname, targs):
    """Does `cls` derive from `tname<targs>`, however it is spelled?

    Compared against the *written* base, since this runs before the base is
    monomorphised: `class node : public enable_shared_from_this<node>` is
    the shape, and whitespace is the only thing that varies.
    """
    if cls.base is None:
        return False
    want = "%s<%s>" % (tname, ",".join(t.strip() for t in targs))
    return re.sub(r"\s+", "", cls.base) == want


def _base_name(targ):
    """The class name a template argument names, ignoring `*`, `&`, `const`."""
    t = re.sub(r"\b(?:const|volatile)\b", " ", targ)
    t = t.replace("*", " ").replace("&", " ").strip()
    return t.split()[0] if t.split() else targ.strip()


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


#: Scalar spellings that may also be taken by reference. A reference is a
#: pointer the source did not have to spell, and that is as true of `int &`
#: as of `T &` -- it only ever worked for classes because the lowering was
#: driven by the class table. A `map<int, ..>` taking its key by reference is
#: what turned that up: `const int &k` came out unlowered and unparsable.
_SCALAR_TYPES = frozenset((
    "unsigned long long", "signed long long", "unsigned long",
    "unsigned char", "unsigned short", "unsigned int", "long long",
    "long double", "signed char", "unsigned", "double", "float", "short",
    "long", "char", "bool", "int", "size_t"))


def _expand_cpp_rref(params, names):
    """`__cpp_rref(T)` -> `T` for a scalar, `T &&` for a class.

    The rvalue-reference counterpart of `__cpp_ref`, and it exists for the
    same reason: a container cannot pick one spelling for both. A scalar has
    nothing to move and no address to bind, so `push_back(std::move(3))` has
    to stay by value; a class must not cross a call boundary by value at
    all, so it binds a reference the move constructor then empties.
    """
    return _sub_code(
        re.compile(r"(?<![\w.>])__cpp_rref\s*\(\s*([\w:]+)\s*\)"),
        lambda mm: ("%s &&" % mm.group(1)
                    if mm.group(1) in names else mm.group(1)),
        params)


def _expand_cpp_ref(params, names):
    """`__cpp_ref(T)` -> `T` for a scalar, `const T &` for a class.

    `known` here is every class *name* in the translation, not the classes
    emitted so far. The question is whether `T` is a class, which does not
    depend on emission order -- and the supplied containers are emitted
    above the user's classes by construction, so asking the emitted set gave
    `vector<floated_box>` a by-value `push_back` for an owning element.

    A container cannot pick one spelling for both. By value it refuses an
    owning key -- the copy is never constructed or destroyed -- and by
    reference it cannot bind `m[3]`, since a literal has no address. So the
    spelling is decided per instantiation, like the copy and destroy steps
    beside it.
    """
    return _sub_code(
        re.compile(r"(?<![\w.>])__cpp_ref\s*\(\s*([\w:]+)\s*\)"),
        lambda mm: ("const %s &" % mm.group(1)
                    if mm.group(1) in names else mm.group(1)),
        params)


def _declared_param_names(params):
    """Names of the parameters in a lowered parameter list.

    Used to stop field qualification from rewriting a parameter that shares
    a field's name. In C++ the parameter shadows the member, which is why
    `position(int x, ..) { this->x = x; }` is the ordinary way to write a
    constructor -- and why qualifying that bare `x` produced `this->x =
    this->x`, a self-assignment that compiled cleanly and silently dropped
    the argument.

    The last identifier in a declarator is the name: `const string &s` and
    `int buf[4]` both end in one. Anything with no identifier (an unnamed
    parameter, or `void`) contributes nothing.
    """
    out = set()
    for part in _split_top(params or ""):
        part = part.split("=")[0]                    # default argument
        part = re.sub(r"\[[^\]]*\]", " ", part)      # array declarator
        words = re.findall(r"[A-Za-z_]\w*", part)
        if not words:
            continue
        name = words[-1]
        if name in _KEYWORDS or name == "void":
            continue
        out.add(name)
    return out


def _scalar_ref_names(params):
    """Names of parameters declared as a reference to a scalar."""
    out = []
    for part in _split_top(params or ""):
        if "&" not in part:
            continue
        words = [w for w in part.replace("&", " & ").replace("*", " * ").split()
                 if w != "const"]
        if "*" in words or "&" not in words:
            continue
        amp = words.index("&")
        base = " ".join(words[:amp])
        if base in _SCALAR_TYPES and len(words) > amp + 1:
            out.append(words[amp + 1])
    return out


def _with_scalars(names):
    return set(names) | _SCALAR_TYPES


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
    # An rvalue reference is a reference: `T &&o` is a pointer the source did
    # not have to spell, exactly as `T &o` is. Taken before the single-`&`
    # rule below, which is written to skip `&&` and would otherwise leave one
    # `&` behind. There is no expression this could catch by mistake: the
    # left operand of a logical `&&` is a value, and a bare type name is not
    # one.
    text = _sub_code(
        re.compile(r"(?<![\w.&])((?:const\s+)?(?:%s))\s*&&\s*(\w+)" % alt),
        lambda m: "%s *%s" % (m.group(1), m.group(2)), text)
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


def _member_prologue(cname, value_members, initmap, known, fieldset, line,
                     pmap=None):
    """Constructor calls for class-typed members, in declaration order.

    `pmap` maps this constructor's parameter names to their class, for the
    one case where arity is not enough to choose: `url(const string &s) :
    str_(s)` initializes a `string` member *from a string*, which is the
    copy constructor, not the one-argument converting constructor that
    happens to share its arity. Picking by count alone handed a `string *`
    to a constructor expecting a `const char *`.
    """
    pmap = pmap or {}
    lines = []
    seen = set()
    for fname, fcls in value_members:
        if fname in initmap:
            args = initmap[fname]
            seen.add(fname)
            ar = _arity(args)
            bare = (args or "").strip()
            if ar == 1 and pmap.get(bare) == fcls:
                # The argument is an object of the member's own class, so
                # this is a copy. A reference parameter is already a pointer
                # by now, which is exactly what `_copy` wants.
                if known[fcls]["copy"]:
                    lines.append("%s_copy(&this->%s, %s);"
                                 % (fcls, fname, bare))
                else:
                    # Plain data: no copy constructor was emitted because
                    # none is needed, and assignment is the copy.
                    lines.append("this->%s = *(%s);" % (fname, bare))
                continue
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


def _dropfn(info, cname):
    """The function that destroys one object of this class.

    A class lowered here spells it `T_drop`. A type this file did not define
    -- a Crust `Vec<i32>` arriving as `Vec_int` -- spells it whatever Crust
    emits, which is `Vec_int_free_buf` for a core container and `T_drop` for
    a user `impl Drop`. So it is recorded rather than assumed.
    """
    return (info or {}).get("dropfn") or ("%s_drop" % cname)


def _member_epilogue(value_members, known):
    """Destructor calls for class-typed members, in reverse order."""
    lines = ["%s(&this->%s);" % (_dropfn(known[fcls], fcls), fname)
             for fname, fcls in reversed(value_members)
             if known[fcls]["dtor"]]
    return (" " + " ".join(lines)) if lines else ""


def _external_info(name, dropfn):
    """A `cinfo` entry for a type defined outside this file.

    Crust hands over the types it lowered that own something, so a C++ class
    holding one **by value** is destroyed like any other member and obeys the
    same copy rules. Everything else about the type is unknown here: it has
    no methods this pass can call, no constructor, and no copy constructor --
    which is exactly right, since the Rule of Three check then refuses to
    copy a class that owns one, rather than duplicating the buffer.
    """
    return {"ctor": False, "dtor": True, "ctors": {}, "methods": {},
            "fields": {}, "base": None, "slots": [], "root": None,
            "abstract": False, "vdtor": False, "vdtor_decl": None,
            "ctor_refs": set(), "paths": {}, "copy": False, "move": False,
            "assign": False, "moveassign": False, "move_methods": {}, "deleted": {}, "index": None, "arrow": None,
            "star": None, "augassign": {}, "cmp": {}, "conv": None,
            "vcall": {},
            "dropfn": dropfn, "external": True}


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
    # A base may be a template *instantiation* -- `class D : public Box<int>`,
    # and `enable_shared_from_this<T>` is the shape that matters in practice.
    # The spelling has to be monomorphised before it is looked up, or the
    # name searched for is `Box<int>` and no class is ever called that.
    if base is not None and base not in known:
        base = tsub(sub(base)).strip()
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
    anons = [m for m in cls.members if m.kind == "anon"]
    # The members of an anonymous group are members of the class: they are
    # what the body writes and what has to be qualified. Registered but not
    # emitted as fields of their own -- the group is emitted whole, and
    # listing them twice would give the struct both.
    anon_fields = []
    for a in anons:
        if a.name:
            continue                     # reached through `u.`, not bare
        anon_fields.extend(_split_members(a.body or "", cls.name, a.line))

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
    for a in anons:
        parts.append(("%s { %s } %s;"
                      % (a.ret, sub(a.body or "").strip(), a.name))
                     .replace(" ;", ";"))
    head.append("struct %s { %s };" % (cname, " ".join(parts) or
                                       "char _cpp_empty;"))

    mnames = [m.name for m in cls.members if m.kind == "method"]
    if base_info:
        mnames = sorted(set(mnames) | set(base_info["methods"]))
    info = {"ctor": False, "dtor": False, "ctors": {}, "methods": {},
            "fields": {}, "base": base, "slots": slots, "root": root,
            "abstract": abstract, "vdtor": False, "vdtor_decl": None,
            "ctor_refs": set(), "paths": {}, "copy": False, "move": False,
            "assign": False, "moveassign": False, "move_methods": {}, "deleted": {}, "index": None, "arrow": None,
            "star": None, "augassign": {}, "cmp": {}, "conv": None,
            "dropfn": "%s_drop" % cname, "external": False}
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
    for f in fields + anon_fields:
        t = tsub(sub(f.ret))
        b = [x for x in t.replace("*", " ").split() if x != "const"]
        b = b[0] if b else ""
        is_ptr = "*" in t
        info["fields"][f.name] = (b, is_ptr)
        # An own field shadows an inherited one of the same name.
        info["paths"][f.name] = f.name
        if b in known and not is_ptr and not f.dim:
            value_members.append((f.name, b))
    # A *named* member of an anonymous type is a field like any other, and a
    # body writing `u.a` means `this->u.a`. Its own type has no name to
    # record, which is fine: what is behind the dot is plain C from here.
    for a in anons:
        if a.name:
            info["fields"][a.name] = ("", False)
            info["paths"][a.name] = a.name
    fieldset = set(info["fields"])

    ctors = [m for m in cls.members if m.kind == "ctor"]
    # An `&&` parameter satisfies `_is_copy_params` -- it is a reference with
    # one more `&` -- so the two are separated here rather than there. They
    # are not two copy constructors: they are a copy and a *move*, and each
    # gets its own symbol.
    refs = [m for m in ctors
            if _is_copy_params(m.params, cname, cls.name, tsub, sub)]
    moves = [m for m in refs
             if _is_move_params(m.params, cname, cls.name, tsub, sub)]
    copies = [m for m in refs if m not in moves]
    plain = [m for m in ctors if m not in refs]
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
    if len(moves) > 1:
        raise CppError("class %s: more than one move constructor" % cls.name)
    ctor = plain[0] if plain else None
    copy = copies[0] if copies else None
    move = moves[0] if moves else None
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
        # Which of this constructor's parameters are objects of a class we
        # know, by value or by reference? Only those can make an initializer
        # a copy rather than a conversion.
        pmap = {}
        if member is not None:
            for part in _split_top(sub(member.params or "")):
                part = part.strip()
                if not part or "*" in part:
                    continue          # a pointer parameter is not the object
                words = [w for w in part.replace("&", " ").split()
                         if w != "const"]
                if len(words) >= 2:
                    # `_param_name` reads the lowered pointer spelling; these
                    # params are still as written, where the name is simply
                    # the last identifier.
                    pn = words[-1]
                    bt = tsub(words[0])
                    if bt in known and re.match(r"^[A-Za-z_]\w*$", pn):
                        pmap[pn] = bt
        pro += _member_prologue(cname, value_members, initmap, known,
                                fieldset, cls.line, pmap)
        # C++11 default member initializers. C has no such thing on a struct
        # member, so each becomes an assignment at the top of every
        # constructor -- which is what it means. An explicit entry in this
        # constructor's initializer list wins, exactly as in C++.
        for f in fields:
            if f.definit is None or f.name in initmap:
                continue
            expr = sub(f.definit).strip()
            if not expr:
                # `T x {};` is value-initialisation. A class member is
                # already default-constructed by the member prologue above;
                # a scalar one is zeroed here.
                if any(f.name == vn for vn, _c in value_members):
                    continue
                expr = "0"
            pro += " this->%s = %s;" % (f.name, expr)
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

    tail = []
    emitting_outline = [False]

    def emit(kind, mname, params, raw, static=False):
        # `__cpp_ref(T)` in a parameter: `T` for a scalar, `const T &` for a
        # class. A container cannot pick one spelling for both -- by value it
        # refuses an owning key (the copy is never constructed or destroyed),
        # and by reference it cannot bind `m[3]`, since a literal has no
        # address. So the spelling is decided per instantiation, like the
        # copy and destroy steps beside it.
        params = _expand_cpp_ref(_expand_cpp_rref(params, names), names)
        refs = _ref_positions(params, _with_scalars(names))
        # A *scalar* reference parameter needs its uses dereferenced. A class
        # one does not: every use of it is a member access, and the symbol
        # table already turns `o.x` into `o->x`. A bare `k` has no member to
        # go through, so `int &k` lowered to `int *k` left the body comparing
        # a value against a pointer.
        scalar_refs = _scalar_ref_names(params)
        params = _lower_refs(params, _with_scalars(names))
        # `this` is a pointer, exactly as an `impl` method's `self` is --
        # unless the member is `static`, which by definition has no
        # receiver to point at.
        if static:
            arglist = params or "void"
        else:
            arglist = "%s *this" % cname + (", " + params if params else "")
        # Members are emitted in declaration order, but a body may call a
        # method declared below it -- ordinary in a class, and an implicit
        # declaration in C. Prototype everything first.
        mprotos.append("%s %s %s(%s);" % (stor, kind, mname, arglist))
        inner = raw
        for rname in scalar_refs:
            inner = _sub_code(
                re.compile(r"(?<![\w.>&])%s(?![\w])" % re.escape(rname)),
                "(*%s)" % rname, inner)
        inner = _implicit_this(inner, mnames)
        # Bare member names inside a body refer to fields; qualify them.
        # Inherited ones go through `_base`, so the path is substituted
        # rather than the bare name -- `id` in a derived method is
        # `this->_base.id`, not `this->id`, which would not compile.
        # One alternation rather than a pass per field: each pass would have
        # to re-blank the body, and a field qualified by an earlier pass
        # would be re-examined by a later one.
        if info["paths"]:
            # A parameter of the same name shadows the field, exactly as in
            # C++. Without this the bare `x` in `this->x = x` was qualified
            # into `this->x = this->x` -- which compiles, runs, and silently
            # ignores the argument. `position(int x, int y, ..)` is the usual
            # spelling of a constructor, so this was not a corner case.
            shadowed = _declared_param_names(params)
            # A field whose name is also a class name is left alone. The two
            # collide in type position -- litehtml has a `document` field and
            # a `document` class -- and qualifying there turned
            # `shared_ptr<document>` into `shared_ptr<this->document>`.
            # A bare use in *expression* position then goes unqualified and
            # fails loudly, which is the better way round: a refusal can be
            # read and fixed, a mangled type cannot.
            visible = [n for n in info["paths"]
                       if n not in shadowed and n not in names]
            if visible:
                inner = _sub_code(
                    re.compile(r"(?<![\w.>])(%s)\b" % _type_alt(visible)),
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
        (tail if emitting_outline[0] else out).append(
            "%s %s %s(%s) {%s}" % (stor, kind, mname, arglist, inner))
        return refs

    for m in cls.members:
        if m.kind in ("field", "anon") or m.pure:
            continue
        if m.declared_only:
            # Prototype only. `emit` writes both, so the declaration is made
            # here and the definition left to whoever has the body.
            dparams = _lower_refs(_expand_cpp_ref(_expand_cpp_rref(sub(m.params or ""), names), names),
                                  _with_scalars(names))
            mname = _member_symbol(cname, m)
            if mname is not None:
                # External linkage, not `static`: the definition is in
                # another translation unit, and a `static` declaration with
                # no definition there could never be resolved.
                mprotos.append(
                    "%s %s(%s *this%s);"
                    % (tsub(sub(m.ret or "void")).strip() or "void",
                       mname, cname, (", " + dparams) if dparams else ""))
            continue
        emitting_outline[0] = m.outline
        params = sub(m.params or "").strip()
        if _needs_deleted_copy(sub(m.body or ""), known):
            # A member whose body copies an element the element type cannot
            # copy. C++ *deletes* such a member rather than rejecting the
            # class, which is what makes `vector<unique_ptr<T>>` legal there
            # and a copy of one an error at the call. Recorded rather than
            # silently dropped, so a call site gets a diagnostic naming the
            # reason instead of an undefined symbol from the C front end.
            info["deleted"][m.name] = True
            continue
        if m.kind == "ctor" and m is copy:
            # A copy constructor lowers to its own symbol: every other
            # constructor is `T_new`, so overloading it is not available.
            # `T &other` lowers to `T *other` like any reference parameter,
            # so the body reads through `->` as usual.
            emit("void", "%s_copy" % cname, params,
                 make_prologue(m) + sub(m.body or ""))
            info["copy"] = True
        elif m.kind == "ctor" and m is move:
            # A move constructor gets `T_move`, beside `T_copy`, and for the
            # same reason: `T_new` is taken and overloading it is not
            # available. `T &&o` lowers to `T *o` like any other reference
            # parameter, so the body reads `o->d` and can null it -- which is
            # what leaves the source safe for the destructor that still runs.
            emit("void", "%s_move" % cname, params,
                 make_prologue(m) + sub(m.body or ""))
            info["move"] = True
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
                             "ret": tsub(iret.replace("&", "").strip()),
                             "refs": _ref_positions(
                                 _expand_cpp_ref(_expand_cpp_rref(params, names), names),
                                 _with_scalars(names))}
            # The body returns the element; the lowered function returns
            # its address, which is what a reference is.
            ibody = _sub_code(
                re.compile(r"(?<![\w.>])return\s+([^;]+);"),
                lambda mm: "return &(%s);" % mm.group(1).strip(),
                sub(m.body or ""))
            emit(iret.replace("&", "*"), "%s__index" % cname, params, ibody)
        elif m.kind == "arrow":
            aret = sub(m.ret or "").strip()
            if "*" not in aret:
                raise CppError(
                    "class %s: `operator->` has to return a pointer (`%s *`) "
                    "-- C++ keeps applying it until one comes back, and this "
                    "subset does the first hop only."
                    % (cls.name, aret.replace("*", "").strip() or "T"))
            info["arrow"] = {"fn": "%s__arrow" % cname,
                             "ret": tsub(aret)}
            emit(aret, "%s__arrow" % cname, params, sub(m.body or ""))
        elif m.kind == "star":
            sret = sub(m.ret or "").strip()
            if "&" not in sret:
                raise CppError(
                    "class %s: `operator*` has to return a reference "
                    "(`%s &`), so that `*p = x` assigns through rather than "
                    "to a copy."
                    % (cls.name, sret.replace("&", "").strip() or "T"))
            info["star"] = {"fn": "%s__star" % cname,
                            "ret": tsub(sret.replace("&", "").strip())}
            sbody = _sub_code(
                re.compile(r"(?<![\w.>])return\s+([^;]+);"),
                lambda mm: "return &(%s);" % mm.group(1).strip(),
                sub(m.body or ""))
            emit(sret.replace("&", "*"), "%s__star" % cname, params, sbody)
        elif m.kind == "assign" and "&&" in (m.params or ""):
            # `operator=(T &&)` -- move assignment. Its own symbol, because
            # the two overloads would otherwise both be `T__assign` and the
            # second would redefine the first. Which one a statement calls is
            # decided at the call site by whether `std::move` is written
            # there, since that is exactly what decides it in C++.
            emit("void", "%s__moveassign" % cname, params, sub(m.body or ""))
            info["moveassign"] = True
        elif m.kind == "assign":
            # Lowered to `T_assign(T *this, const T *o)`. Assignment is the
            # one place the subset needs a user hook: a struct copy of an
            # owning object leaves two owners, and there is no safe default.
            # `__assign`, not `_assign`: a class may perfectly well declare
            # a method called `assign`, and `string` does.
            emit("void", "%s__assign" % cname, params, sub(m.body or ""))
            info["assign"] = True
        elif m.kind == "cmp":
            op = m.name[len("operator"):]
            fn = "%s__cmp%s" % (cname, _CMP_NAMES[op])
            cret = tsub(sub(m.ret or "int")).strip() or "int"
            info["cmp"][op] = {
                "fn": fn, "ret": cret,
                "refs": _ref_positions(_expand_cpp_ref(sub(m.params or ""),
                                                       known),
                                       _with_scalars(names))}
            emit(cret, fn, params, sub(m.body or ""))
        elif m.kind == "conv":
            cret = tsub(sub(m.ret or "")).strip()
            info["conv"] = {"fn": "%s__conv" % cname, "ret": cret}
            emit(cret, "%s__conv" % cname, params, sub(m.body or ""))
        elif m.kind == "augassign":
            op = m.name[len("operator"):-1]
            fn = "%s__aug%s" % (cname, _AUG_NAMES[op])
            # The operand's own class, which need not be the class the
            # operator belongs to: litehtml's `position` takes `margins` in
            # `operator+=`. Reading it off the declaration keeps the operand
            # check honest -- asking for the left side's type instead
            # rejected `pos += m_padding`, which is exactly what the
            # operator is for.
            _aug_words = [w for w in sub(m.params or "").replace("&", " ")
                          .replace("*", " ").split() if w != "const"]
            _aug_cls = _aug_words[0] if _aug_words else None
            info["augassign"][op] = {
                "fn": fn,
                "operand": _aug_cls if _aug_cls in known else None,
                "refs": _ref_positions(_expand_cpp_ref(sub(m.params or ""),
                                                       known),
                                       _with_scalars(names))}
            emit("void", fn, params, sub(m.body or ""))
        elif m.kind == "dtor":
            emit("void", "%s_drop" % cname, params,
                 sub(m.body or "") + epilogue)
            info["dtor"] = True
        else:
            ar = _arity(params)
            # A method taking `T &&` is a *move* overload. It is not told
            # apart by arity -- `push_back(const T &)` and `push_back(T &&)`
            # both take one argument -- but by whether the call site wrote
            # `std::move`, which is exactly what decides it in C++ and
            # exactly what already decides `operator=` from
            # `operator=(T &&)`. Its own symbol, so the two can coexist.
            #
            # Read from the *expanded* parameters, because a container
            # spells this `__cpp_rref(T)` and the `&&` only appears once the
            # instantiation is known. That is also what makes the scalar
            # case work: `__cpp_rref(int)` is plain `int`, so the two
            # overloads would be the same signature -- there is nothing to
            # move about a scalar -- and the move one is simply not emitted.
            is_move_over = "&&" in _expand_cpp_rref(params, names)
            if "__cpp_rref" in (m.params or "") and not is_move_over:
                continue
            over = len([x for x in cls.members
                        if x.kind == "method" and x.name == m.name
                        and ("__cpp_rref" in (x.params or "")
                             or "&&" in (x.params or "")) == is_move_over]) > 1
            if over and m.virt:
                # One vtable slot per name, so an overloaded virtual has
                # nowhere for its second signature to live.
                raise CppError(
                    "class %s: `%s` is virtual and overloaded. A virtual "
                    "method occupies one vtable slot, so its overloads "
                    "would have to share it." % (cls.name, m.name))
            mfn = ("%s_%s_%d" % (cname, m.name, ar) if over
                   else "%s_%s" % (cname, m.name))
            if is_move_over:
                mfn = "%s__move" % mfn
            slot = "move_methods" if is_move_over else "methods"
            if ar in info[slot].get(m.name, {}) and \
                    info[slot][m.name][ar]["owner"] == cname:
                raise CppError(
                    "class %s: two `%s` methods take %d argument%s. "
                    "Overloads are resolved by argument count here."
                    % (cls.name, m.name, ar, "" if ar == 1 else "s"))
            info[slot].setdefault(m.name, {})[ar] = {
                "refs": emit(sub(m.ret), mfn, params, sub(m.body or ""),
                             static=m.stat),
                # Recorded so a `Cls::name(..)` call can be lowered without
                # inventing a receiver for it.
                "static": m.stat,
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
    # Split three ways rather than two. Only the *name* declarations and the
    # prototypes are safe to hoist: the struct definition has to stay where
    # it is, because a by-value member needs the member's definition above it
    # and moving one moves them all. `head[:2]` is exactly the `struct X;` and
    # its typedef, which is all a pointer field to a class defined later
    # needs -- and that is the shape a template instantiated over a class
    # declared below it always has.
    # An implicit copy constructor, when the class has members that need one
    # and declares none. C++ writes one member-wise, and so does this -- the
    # implicit *destructor* built from the same members already exists, and a
    # class with one and no way to be copied cannot go in a container.
    # C++ deletes the implicit copy when a member cannot be copied, and so
    # does this: a member that owns something and offers no copy constructor
    # -- a Crust `Vec_int` among them -- has no member-wise copy to write,
    # and generating one would duplicate the thing it owns.
    copyable = all(not (known[b]["dtor"] and not known[b]["copy"])
                   for _n, b in value_members if b in known)
    # Only when there is a class-typed member to copy. Plain data keeps its
    # bitwise copy, which is what C++ does and what the rest of this pass
    # expects; and a class whose only owned thing is a *raw pointer* still
    # gets the Rule of Three refusal, because there is no member that knows
    # how to duplicate what it points at.
    if not info["copy"] and copy is None and copyable and value_members:
        lines = []
        if base and known[base]["copy"]:
            lines.append("%s_copy(&this->_base, &o->_base);" % base)
        elif base:
            lines.append("this->_base = o->_base;")
        for f in fields:
            if f.dim:
                continue                 # an array member is not assignable
            lines.append("__cpp_copy(%s, this->%s, &o->%s);"
                         % (info["fields"].get(f.name, ("", False))[0]
                            or "int", f.name, f.name)
                         if info["fields"].get(f.name, ("", False))[0]
                         in known and not info["fields"][f.name][1]
                         else "this->%s = o->%s;" % (f.name, f.name))
        if lines:
            emit("void", "%s_copy" % cname,
                 "const %s &o" % cname, " " + " ".join(lines))
            info["copy"] = True
            # And the implicit assignment, which C++ generates on the same
            # terms. It has to release what is already there first, and
            # guard self-assignment -- `a = a` would otherwise destroy the
            # object and then copy from the wreckage.
            if not info["assign"]:
                emit("void", "%s__assign" % cname, "const %s &o" % cname,
                     " if (this != o) {%s %s }"
                     % (_member_epilogue(value_members, known),
                        " ".join(lines)))
                info["assign"] = True

    # A class that carries `enable_shared_from_this`'s members gets the
    # function `shared_ptr` calls to hand it the control block. Emitted with
    # the class, where its fields are complete.
    # Only a class that *inherits* them: the one that declares them reaches
    # its own fields by their bare names, and its `esp` is a `T *` while its
    # `this` is the base's own type.
    if info["paths"].get("esp", "esp") != "esp" \
            and info["paths"].get("esc", "esc") != "esc":
        mprotos.append("%s void %s__share_hook(%s *this, long *c);"
                       % (stor, cname, cname))
        out.append("%s void %s__share_hook(%s *this, long *c) "
                   "{ this->%s = this; this->%s = c; }"
                   % (stor, cname, cname,
                      info["paths"]["esp"], info["paths"]["esc"]))
    emitting_outline[0] = False
    return (head[:2], mprotos, head[2:] + out, tail), cname, info


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
    # A preprocessor directive is not part of a declaration, and contains
    # none of the characters above -- so `#include <stdio.h>` immediately
    # before `int main()` was read as part of the return type, and the
    # spilled temporary came out as `#include <stdio.h> int _cpp_ret0`.
    # A directive ends at its newline; nothing up to there belongs here.
    for pm in re.finditer(r"(?m)^[ \t]*#.*$", head):
        cut = max(cut, pm.end())
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
    __slots__ = ("live", "kind", "ret", "vals", "ptrs")

    def __init__(self, kind, ret):
        self.live = []        # (ctype, vname), in declaration order
        self.kind = kind      # "file" | "func" | "loop" | "switch" | "block"
        self.ret = ret        # enclosing function's return type
        self.vals = {}        # class-typed locals: vname -> class
        # Names in `vals` that are already pointers -- a reference parameter
        # after lowering, and `this`. They name an object just as a local
        # does, but reaching it needs a dereference rather than an address.
        self.ptrs = set()


def _conv_for(name, scopes, type_info):
    """The conversion operator of `name`'s class, if it has one."""
    for fr in reversed(scopes):
        if name in fr.vals:
            return (type_info.get(fr.vals[name]) or {}).get("conv")
    return None


def _named_object(expr, scopes, type_info):
    """`(path, class)` for an expression that names an object, else None.

    A local, `this`, or a chain of value members from either (`t.nums`,
    `this->str_`). Factored out of `_copy_source`, which asks whether a name
    is an object of a class it already knows; expression-position
    `std::move` has to ask the other question -- which class this name *is*
    -- because there is no declaration beside it to read the type from.

    `this->` is in the chain because by the time this pass runs, field
    qualification has already put it there: a method body that said `str_`
    reaches here as `this->str_`, so a pass that only understood `.` could
    not name a single one of its own fields. That is what refused
    `tstring tmp = str_;` in url.cpp -- not the copy, which is ordinary, but
    the inability to say what was being copied.
    """
    if expr is None:
        return None
    expr = expr.strip()
    # `v[i]` -- a subscript names an element, and `operator[]` returns a
    # reference precisely so that it does. The element type is read off that
    # return rather than assumed, and the expression is handed back as
    # written: the call pass rewrites `v[i]` to `*v__index(&v, i)` later, so
    # an `&` taken here lands on the element either way.
    sub_m = re.match(r"^(\w+(?:\s*(?:\.|->)\s*\w+)*)\s*\[([^\[\]]*)\]$", expr)
    if sub_m is not None:
        base = _named_object(sub_m.group(1), scopes, type_info)
        if base is None:
            return None
        binfo = type_info.get(base[1])
        if binfo is None or not binfo.get("index"):
            return None
        # Emitted in its lowered form rather than left as `v[i]`: this pass
        # runs after the one that rewrites subscripts, so a `v[i]` written
        # here would survive into the C output, where it is a subscript on a
        # struct.
        return ("(*%s(%s, %s))"
                % (binfo["index"]["fn"], _addr_of_expr(base[0]),
                   sub_m.group(2)),
                binfo["index"]["ret"])
    parts = [p for p in re.split(r"\s*(?:\.|->)\s*", expr) if p]
    if not parts or not all(re.match(r"^\w+$", p) for p in parts):
        return None
    cls = None
    is_ptr_base = False
    for fr in reversed(scopes):
        if parts[0] in fr.vals:
            cls = fr.vals[parts[0]]
            is_ptr_base = parts[0] in fr.ptrs
            break
    if cls is None:
        # A bare field of the class being written in. Field qualification
        # puts `this->` in front of one inside a class body, but litehtml
        # defines nearly every method out of line -- `void box::add_element()`
        # -- and those bodies are never rewritten, so `m_items` arrives
        # exactly as written. `this` is in scope either way, and C++ reads
        # the bare name as a member of it.
        for fr in reversed(scopes):
            if "this" in fr.vals:
                tinfo = type_info.get(fr.vals["this"])
                if tinfo is not None and parts[0] in tinfo["fields"]:
                    fcls, fptr = tinfo["fields"][parts[0]]
                    if not fptr:
                        parts = ["this"] + parts
                        cls = fr.vals["this"]
                        is_ptr_base = True
                break
    if cls is None:
        return None
    out = parts[0]
    # `this` and a lowered reference parameter are pointers; every field of
    # a class is a value. So only the first hop off a pointer is an arrow,
    # and the rest are dots either way.
    sep = "->" if (parts[0] == "this" or is_ptr_base) else "."
    if is_ptr_base and len(parts) == 1:
        # Named on its own rather than reached through: the caller wants an
        # object, and `&(*p)` is `p` -- written out so the address the
        # caller takes is the one it wants rather than the pointer's own.
        out = "(*%s)" % parts[0]
    for fld in parts[1:]:
        info = type_info.get(cls)
        if info is None or fld not in info["fields"]:
            return None
        fcls, is_ptr = info["fields"][fld]
        if is_ptr:
            return None              # a pointer member is not the object
        out = "%s%s%s" % (out, sep, info["paths"].get(fld, fld))
        sep = "."
        cls = fcls
    return (out, cls)


def _converting_operand(rhs, scopes, type_info):
    """Is `rhs` something a one-argument constructor should be given?

    `string s = str;` where `str` is a `const char *` is copy-initialization
    through a converting constructor -- C++ builds the temporary and, since
    C++17, constructs `s` directly from it. `string s(str);` already lowers
    here; only the `=` spelling was refused, and the two mean the same thing.

    Deliberately narrow. A literal, or a bare name that is not a known
    object of any class, is something whose type this pass can be sure is
    *not* the class being built. Anything larger -- a conditional, an
    arithmetic expression, a member chain -- could be an object of that
    class the pass simply failed to name, and handing one to a converting
    constructor would build the wrong thing silently. Those keep the
    refusal, which is the honest answer.
    """
    if not rhs:
        return False
    rhs = rhs.strip()
    if re.match(r'^".*"$', rhs, re.S) or re.match(r"^'.*'$", rhs, re.S):
        return True
    if re.match(r"^\w+$", rhs) and rhs not in ("nullptr", "NULL", "true",
                                               "false"):
        return _named_object(rhs, scopes, type_info) is None
    return False


def _fix_ctor_args(args, refs, scopes, type_info):
    """Insert `&` where a constructor's by-reference parameter wants one.

    Method calls go through `fix_args` in the call pass, but a constructor
    is reached from a *declaration* -- `url u(base);` -- which this pass
    lowers itself, and it was passing arguments through untouched. A
    `const string &` parameter is a `const string *` by the time it is
    emitted, so a by-value argument arrived as the wrong type.
    """
    parts = _split_top(args or "")
    for idx in sorted(refs or ()):
        if idx >= len(parts):
            continue
        a = parts[idx].strip()
        if not a or a.startswith("&") or a.startswith("*"):
            continue
        found = _named_object(a, scopes, type_info)
        if found is None:
            continue                  # not something we can take an address of
        parts[idx] = " &" + found[0]
    return ",".join(parts).strip()


def _copy_source(expr, ctype, scopes, type_info):
    """The object being copied, if `expr` names one of class `ctype`.

    A local, or a chain of value members from one (`t.nums`). A call result
    or any other expression is not something this pass can copy-construct
    from, and guessing would be the whole point of the bug.
    """
    found = _named_object(expr, scopes, type_info)
    if found is None:
        return None
    out, cls = found
    return out if cls == ctype else None


_MOVE_CALL = re.compile(r"__cpp_move\s*\(")   # `.match()` anchors; `^` would pin to index 0


def _move_operand(expr):
    """The `x` in `__cpp_move(x)`, or None if this is not one.

    Only when the move is the *whole* expression. `f(__cpp_move(a))` and
    `__cpp_move(a).size()` are expression position, where materialising the
    temporary needs a statement there is nowhere to put -- they are reported
    by `_check_stray_moves` rather than half-handled here.
    """
    if expr is None:
        return None
    expr = expr.strip()
    m = _MOVE_CALL.match(expr)
    if m is None:
        return None
    close = _match_paren(expr, m.end() - 1)
    if close is None or close != len(expr) - 1:
        return None
    return expr[m.end():close].strip()


def _move_temporary(ctype, src, info, n):
    """A move in expression position, as a GNU statement expression.

    `({ T __cpp_mv0; T_move(&__cpp_mv0, &a); __cpp_mv0; })` -- declare a
    temporary, move into it, yield it. That is what a C++ compiler does with
    a materialised temporary, written out.

    A statement expression is what makes this possible at all. Everywhere
    else this pass meets expression position it reports, because a move has
    to construct into something and C has no way to declare a temporary
    inside an expression. `({ .. })` is exactly that way. It is a GNU
    extension rather than ISO C, but gcc, clang and ShivyCX all implement
    it, and all three were checked against this shape -- so the output stays
    one file with no backend to choose between, which is the property the
    whole pipeline is built on.

    The temporary is deliberately **not** registered for destruction. It is
    yielded by value, so what the caller receives is a bitwise copy holding
    the resource, and the husk left behind owns nothing -- destroying it
    would be destroying the copy the caller now owns. The *source* is still
    dropped by its own scope, as every other move here leaves it.
    """
    tmp = "_cpp_mv%d" % n
    if not info["move"]:
        if not info["copy"]:
            return None
        # No move constructor: the copy binds the rvalue, exactly as in
        # statement position and for the same reason.
        return "({ %s %s; %s_copy(&%s, &%s); %s; })" % (
            ctype, tmp, ctype, tmp, src, tmp)
    return "({ %s %s; %s_move(&%s, &%s); %s; })" % (
        ctype, tmp, ctype, tmp, src, tmp)


def _needs_deleted_copy(body, known):
    """Does this body copy an element whose type cannot be copied?

    A supplied container says "copy an element" as `__cpp_copy(T, ..)`, and
    for a `T` that owns something and offers no copy constructor there is no
    such operation. C++ answers by *deleting* the member -- the container is
    still a usable type, it just cannot be copied -- and this is that answer.
    Refusing instead rejected `vector<unique_ptr<T>>` outright, over members
    the program never calls.

    Only a class that owns something. A plain-data element with no copy
    constructor copies bitwise, exactly as `__cpp_copy` already lowers it.
    """
    for mm in re.finditer(r"(?<![\w.>])__cpp_copy\s*\(\s*([\w:]+)\s*,", body):
        ent = known.get(mm.group(1))
        if ent is not None and ent["dtor"] and not ent["copy"]:
            return True
    return False


def _move_method_receiver(text, at, scopes, type_info):
    """Is the `__cpp_move` at `at` the sole argument of `recv.meth(..)`?

    Returns the receiver's class when that method has a move overload, else
    None. This is the one place an expression-position move must *not* be
    materialised: a move overload lowers to `meth(T *v)`, so what the call
    wants is the address of the source, not a temporary yielded by value.
    Materialising here would hand it a statement expression's result, whose
    address cannot be taken.

    Scanned backwards because the call rewriter has not run yet -- at this
    point `v.push_back(..)` is still spelled the way the author wrote it.
    """
    j = at - 1
    while j >= 0 and text[j].isspace():
        j -= 1
    if j < 0 or text[j] != "(":
        return None
    j -= 1
    while j >= 0 and text[j].isspace():
        j -= 1
    end = j + 1
    while j >= 0 and (text[j].isalnum() or text[j] == "_"):
        j -= 1
    meth = text[j + 1:end]
    if not meth:
        return None
    while j >= 0 and text[j].isspace():
        j -= 1
    if j >= 0 and text[j] == ".":
        j -= 1
    elif j >= 1 and text[j - 1:j + 1] == "->":
        j -= 2
    else:
        return None
    rend = j + 1
    while j >= 0 and (text[j].isalnum() or text[j] in "_." or
                      (text[j] == ">" and j >= 1 and text[j - 1] == "-") or
                      (text[j] == "-" and text[j + 1:j + 2] == ">")):
        j -= 1
    recv = text[j + 1:rend].strip()
    if not recv:
        return None
    found = _named_object(recv.replace("->", "."), scopes, type_info)
    if found is None:
        return None
    cls = found[1]
    info = type_info.get(cls)
    if info is None or meth not in info.get("move_methods", {}):
        return None
    return cls


def _materialise_moves(expr, scopes, type_info, mvn):
    """Rewrite every `__cpp_move(x)` in `expr` to a statement expression.

    Called from the scope rewriter's fall-through, which is where an
    expression-position move surfaces, and again from the `return` handler,
    which consumes its operand whole and would otherwise carry one through
    untouched.
    """
    if "__cpp_move" not in expr:
        return expr
    out = []
    i = 0
    while i < len(expr):
        if expr.startswith("__cpp_move", i) and \
                (i == 0 or not (expr[i - 1].isalnum() or expr[i - 1] == "_")):
            om = _MOVE_CALL.match(expr, i)
            if om is not None:
                close = _match_paren(expr, om.end() - 1)
                if close is not None:
                    inner = expr[om.end():close].strip()
                    found = _named_object(inner, scopes, type_info)
                    if found is None:
                        raise CppError(
                            "`std::move(%s)`: the operand has to be an object "
                            "this pass can name -- a local, or a chain of "
                            "value members from one. Assign it to a typed "
                            "local first." % inner)
                    src, ctype = found
                    info = type_info[ctype]
                    made = _move_temporary(ctype, src, info, mvn[0])
                    if made is None:
                        raise CppError(
                            "`std::move(%s)`: %s has a destructor but neither "
                            "a move nor a copy constructor, so there is no "
                            "way to construct the temporary this needs. Add "
                            "`%s(%s &&o)`." % (inner, ctype, ctype, ctype))
                    mvn[0] += 1
                    out.append(made)
                    i = close + 1
                    continue
        out.append(expr[i])
        i += 1
    return "".join(out)


def _move_call(ctype, vname, src, info, where):
    """`T_move(&b, &a);`, falling back to the copy constructor.

    A class with no move constructor is *copied* from an rvalue, because
    `std::move` is a cast rather than a call: it produces an rvalue, and
    `T(const T &)` binds one perfectly well. So the fall-back is not a
    concession, it is what C++ overload resolution does -- which is also
    what makes adding `std::move` to an existing source safe.

    The source is **not** dropped from the enclosing scope. A moved-from
    object in C++ is valid-but-unspecified and is still destroyed; the move
    constructor is what makes that harmless, by leaving the source holding
    nothing. That is the one place this differs from Crust's own move-out,
    which really does hand the object over and suppress the drop.
    """
    if not info["move"]:
        return _copy_call(ctype, vname, src, info, where)
    return "%s_move(&%s, &%s);" % (ctype, vname, src)


def _check_stray_moves(text, path):
    """Any `__cpp_move` left is one the declaration sites did not consume.

    Move construction and move assignment are statements, and both are
    rewritten where they stand. What reaches here is `std::move` in
    *expression* position -- an argument, a `return`, an operand -- where
    lowering it means copy-constructing into a temporary, which needs a
    statement to declare. That is the same wall `_check_by_value` and the
    chaining rule hit, and it is reported for the same reason: emitting
    `__cpp_move(a)` into the C would name a function nothing defines.
    """
    for m in re.finditer(r"(?<![\w.>])__cpp_move\s*\(", text):
        close = _match_paren(text, m.end() - 1)
        inner = text[m.end():close] if close is not None else "?"
        raise CppError(
            "%s: `std::move(%s)` is in expression position, which is not in "
            "the C++ subset yet. A move has to construct into something, and "
            "here there is no declaration to construct into -- the temporary "
            "would need a statement of its own. Move into a local first "
            "(`T tmp = std::move(%s);`) and pass `&tmp`."
            % (os.path.basename(path), inner.strip(), inner.strip()))


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
    # The left side may be a member chain (`this->css_baseurl`), not just a
    # bare local: litehtml assigns to its own fields constantly, and a
    # pattern that only matched a bare name left every one of those to fall
    # through as a plain struct assignment.
    assign_re = re.compile(
        r"(?<![\w.>])(\w+(?:\s*(?:\.|->)\s*\w+)*)\s*=(?!=)\s*([^;]+);")
    # `int w = dv;` / `w = dv;` where `dv` is a class with `operator T()`.
    # A conversion is applied only where the target type is *written*: this
    # pass reads types by their spelling, so a written one is exactly what it
    # can be sure of. Anywhere else the conversion is left out and the C
    # front end reports the type mismatch on the struct.
    conv_init_re = re.compile(
        r"(?<![\w.>])([A-Za-z_][\w]*)\s+(\w+)\s*=\s*(\w+)\s*;")
    conv_assign_re = re.compile(r"(?<![\w.>])(\w+)\s*=(?!=)\s*(\w+)\s*;")
    # `a == b` where `a` is a class with `operator==`. Longest spellings
    # first, so `<=` is not read as `<`. Only a bare name on the left: this
    # pass knows the type of a local, and an expression it would have to
    # infer one for is left alone.
    cmp_re = re.compile(
        r"(?<![\w.>])(\w+)\s*(==|!=|<=|>=|<|>)\s*(\w+)(?![\w(<])")
    aug_re = re.compile(
        r"(?<![\w.>])(\w+)\s*([+\-*/%|&^])=(?!=)\s*([^;]+);")

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

    def unwind(upto, moved=None):
        """Drop calls for frames `upto..top`, innermost and latest first.

        `moved` names a local this path hands to its caller rather than
        destroys -- `return v;` on an owning local is a *move out*, which is
        the same rule Crust follows on the Rust side. Skipping the drop is
        what makes returning a `shared_ptr` by value work: the object the
        caller receives is the one that was here, not a copy of a released
        one. Per path, never a permanent unregister, so the fall-through
        still drops it.
        """
        pieces = []
        for fr in reversed(scopes[upto:]):
            for ctype, vname in reversed(fr.live):
                if moved is not None and vname == moved:
                    continue
                pieces.append("%s(&%s); "
                              % (_dropfn(type_info.get(ctype), ctype), vname))
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
    mvn = [0]              # counter for materialised move temporaries
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
                fr = _Frame(kind, ret)
                if kind == "func":
                    # A by-value owning parameter is an object the callee
                    # owns: C++ destroys it when the function returns, and a
                    # parameter is exactly a local for that purpose. Reference
                    # lowering has already run, so a class still spelled by
                    # value here really is by value -- a `T &` the author
                    # wrote is a `T *` by now.
                    for part in _split_top(_params_at(look, i) or ""):
                        pcls = _by_value_class(part.strip(), type_info)
                        if pcls is None or not type_info[pcls]["dtor"]:
                            continue
                        pnm = _param_name(part.strip())
                        if pnm:
                            fr.live.append((pcls, pnm))
                            fr.vals[pnm] = pcls
                    # A lowered method takes `Cname *this` first. Recording
                    # it lets `this->field` be named like any other object;
                    # it is deliberately not added to `live`, because the
                    # method does not own the receiver and must not drop it.
                    first = (_split_top(_params_at(look, i) or "")
                             or [""])[0].strip()
                    tm = re.match(r"^(?:const\s+)?(\w+)\s*\*\s*this$", first)
                    if tm and tm.group(1) in type_info:
                        fr.vals["this"] = tm.group(1)
                    # A reference parameter of class type. Reference lowering
                    # has already turned `const string &s` into
                    # `const string *s`, so it is a pointer here -- but it
                    # still names an object, and a body assigning or copying
                    # from one was refused for want of anything to name.
                    # Recorded in `ptrs`, not `live`: the callee borrows it
                    # and must not destroy it.
                    for part in _split_top(_params_at(look, i) or ""):
                        pm = re.match(r"^(?:const\s+)?(\w+)\s*\*\s*(\w+)$",
                                      part.strip())
                        if pm is not None and pm.group(2) == "this":
                            continue
                        if pm is not None and pm.group(1) in type_info:
                            fr.vals[pm.group(2)] = pm.group(1)
                            fr.ptrs.add(pm.group(2))
                            continue
                        pv = re.match(r"^(?:const\s+)?(\w+)\s+(\w+)$",
                                      part.strip())
                        if pv is not None and pv.group(1) in type_info:
                            # A by-value class parameter. It names an object
                            # like any local -- `pos += m_padding` on one was
                            # left alone for want of a type -- and is not a
                            # pointer, so it takes no dereference. Ownership
                            # of it is handled elsewhere; this is only about
                            # being able to name it.
                            fr.vals[pv.group(2)] = pv.group(1)
                scopes.append(fr)
            out.append(text[i])
            i += 1
            continue
        if c == "}":
            if aggs:
                aggs -= 1
            fr = scopes.pop() if len(scopes) > 1 else _Frame("block", None)
            for ctype, vname in reversed(fr.live):
                out.append("%s(&%s); "
                           % (_dropfn(type_info.get(ctype), ctype), vname))
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
                # A bare owning local being returned is moved out, so it is
                # left out of this path's drops.
                moved = None
                if end is not None:
                    cand = text[m.end():end].strip()
                    if re.match(r"^\w+$", cand):
                        for fr in scopes[fidx:] if fidx is not None else []:
                            if any(v == cand for _c, v in fr.live):
                                moved = cand
                                break
                drops = unwind(fidx, moved) if fidx is not None else ""
                # `return m_root;` -- an owning value returned from something
                # that is not a local. C++ copy-constructs into the return
                # slot here, which for a `shared_ptr` is exactly the refcount
                # increment that makes the idiom work; a bitwise struct copy
                # would hand the caller a second owner of one resource and
                # both would free it.
                #
                # Handled before the `drops or moved` gate below, because a
                # getter like `document::root()` typically has neither: no
                # owning local to drop, and nothing to move out. Without this
                # it fell through and emitted the bitwise copy.
                if end is not None and moved is None and fidx is not None:
                    rexpr = text[m.end():end].strip()
                    rcls = _owning_return_class(scopes[fidx].ret, type_info)
                    if rcls is not None and rexpr \
                            and not _is_call_result(rexpr) \
                            and _move_operand(rexpr) is None:
                        rsrc = _copy_source(rexpr, rcls, scopes, type_info)
                        if rsrc is not None:
                            if not type_info[rcls]["copy"]:
                                # Owns something and cannot be copied: the
                                # Rule of Three refusal, reported here rather
                                # than emitting a double free.
                                raise CppError(
                                    "`return %s;`: %s owns a resource and has "
                                    "no copy constructor, so returning "
                                    "something this function does not own "
                                    "would hand back a second owner. Add "
                                    "`%s(const %s &o)`, or return `%s *`."
                                    % (rexpr, rcls, rcls, rcls, rcls))
                            name = "_cpp_ret%d" % tmp[0]
                            tmp[0] += 1
                            # Copy first, drop second: the drops may release
                            # objects the source is reached through.
                            out.append("{ %s %s; %s_copy(&%s, &%s); "
                                       "%sreturn %s; }"
                                       % (rcls, name, rcls, name, rsrc,
                                          drops, name))
                            i = end + 1
                            continue
                if end is not None and (drops or moved):
                    expr = text[m.end():end].strip()
                    # A `std::move` here is expression position, and the
                    # spill below is already the statement it needs: the
                    # operand is evaluated into the temporary *before* the
                    # drops run, so the move happens while the source is
                    # still alive and the source's own drop then finds the
                    # husk the move left.
                    expr = _materialise_moves(expr, scopes, type_info, mvn)
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
            if len(scopes) <= 1:
                out.append(m.group(0))
                i = m.end()
                continue
            if not info["ctor"]:
                # No constructor to call, but there may still be something to
                # destroy: a class whose members are all Crust types owns
                # everything through them and declares neither. Leave the
                # declaration exactly as written and register it anyway --
                # otherwise the implicit destructor built from those members
                # is emitted and never called.
                out.append(m.group(0))
                if info["dtor"] and (args is None or not args.strip()):
                    scopes[-1].live.append((ctype, vname))
                    scopes[-1].vals[vname] = ctype
                i = m.end()
                continue
            if len(scopes) <= 1 or not info["ctor"]:
                out.append(m.group(0))
                i = m.end()
                continue
            out.append("%s %s; " % (ctype, vname))
            # `T b(std::move(a));` -- a move construction, which is a copy
            # construction that picks the other constructor. The operand is
            # resolved the same way a copy's is, so a move from something
            # this pass cannot name is refused for the same reason.
            moved = _move_operand(args)
            if moved is not None:
                src = _copy_source(moved, ctype, scopes, type_info)
                if src is None:
                    raise CppError(
                        "`%s %s(std::move(%s));`: the operand of `std::move` "
                        "has to be an object of type %s that this pass can "
                        "name -- a local, or a chain of value members from "
                        "one. Assign to a typed local first."
                        % (ctype, vname, moved, ctype))
                out.append(_move_call(ctype, vname, src, info, ctype))
                # The source stays live: a moved-from object is still
                # destroyed in C++, and the move constructor is what makes
                # that harmless.
                if info["dtor"]:
                    scopes[-1].live.append((ctype, vname))
                scopes[-1].vals[vname] = ctype
                i = m.end()
                continue
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
                out.append(
                    "%s(&%s, %s);"
                    % (info["ctors"][ar]["fn"], vname,
                       _fix_ctor_args(args, info["ctors"][ar].get("refs"),
                                      scopes, type_info)))
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
            # `T b = std::move(a);` -- the benchmark's own shape.
            moved = _move_operand(rhs)
            if moved is not None:
                src = _copy_source(moved, ctype, scopes, type_info)
                if src is None:
                    raise CppError(
                        "`%s %s = std::move(%s);`: the operand of `std::move` "
                        "has to be an object of type %s that this pass can "
                        "name -- a local, or a chain of value members from "
                        "one. Assign to a typed local first."
                        % (ctype, vname, moved, ctype))
                out.append("%s %s; " % (ctype, vname))
                out.append(_move_call(ctype, vname, src, info, ctype))
                if info["dtor"]:
                    scopes[-1].live.append((ctype, vname))
                scopes[-1].vals[vname] = ctype
                i = m.end()
                continue
            # `T x = T(a);` -- copy-initialisation from a temporary of the
            # same type, which C++17 guarantees is elided into direct
            # initialisation. Lowered as `T x; T_new(&x, a);`, the form this
            # subset already emits for `T x(a);`. `make_shared<T>(..)`
            # rewrites to exactly this shape, so it is the case that makes
            # the rewrite usable rather than merely accepted.
            cm = re.match(r"^%s\s*\(" % re.escape(ctype), rhs)
            if cm is not None and _match_paren(rhs, cm.end() - 1) == len(rhs) - 1:
                inner = rhs[cm.end():len(rhs) - 1]
                car = _arity(inner)
                if car in info["ctors"]:
                    fixed = _fix_ctor_args(
                        inner, info["ctors"][car].get("refs"), scopes,
                        type_info)
                    out.append("%s %s; %s(&%s%s);"
                               % (ctype, vname, info["ctors"][car]["fn"],
                                  vname, (", " + fixed) if fixed else ""))
                    if info["dtor"]:
                        scopes[-1].live.append((ctype, vname))
                    scopes[-1].vals[vname] = ctype
                    i = m.end()
                    continue
            src = _copy_source(rhs, ctype, scopes, type_info)
            if src is None and not info["dtor"] and not info["copy"]:
                out.append(m.group(0))       # plain data: a bitwise copy is
                i = m.end()                  # exactly what C++ would do
                continue
            if src is None and _is_call_result(rhs):
                # `T a = f();` -- the callee returned by value, which is a
                # move *out* of its local, so this is a move *in*. The plain
                # struct assignment is exactly right: no constructor to run,
                # no second owner, and `a` is registered so it is dropped
                # here instead of there.
                out.append(m.group(0))
                if info["dtor"]:
                    scopes[-1].live.append((ctype, vname))
                scopes[-1].vals[vname] = ctype
                i = m.end()
                continue
            if src is None and 1 in info["ctors"] and \
                    _converting_operand(rhs, scopes, type_info):
                # `T x = e;` where `e` is plainly not a `T`: copy-initialize
                # through the one-argument constructor, which is what
                # `T x(e);` already does one branch up and what C++ does for
                # both spellings.
                out.append("%s %s; " % (ctype, vname))
                out.append("%s(&%s, %s);"
                           % (info["ctors"][1]["fn"], vname, rhs))
                if info["dtor"]:
                    scopes[-1].live.append((ctype, vname))
                scopes[-1].vals[vname] = ctype
                i = m.end()
                continue
            if src is None:
                raise CppError(
                    "`%s %s = %s;`: %s owns a resource, and the right-hand "
                    "side is neither an object of that type this pass can "
                    "name nor a call returning one. Assign to a typed local "
                    "first." % (ctype, vname, rhs, ctype))
            out.append("%s %s; " % (ctype, vname))
            out.append(_copy_call(ctype, vname, src, info, ctype))
            if info["dtor"]:
                scopes[-1].live.append((ctype, vname))
            scopes[-1].vals[vname] = ctype
            i = m.end()
            continue

        m = cmp_re.match(look, i)
        if m and not aggs:
            lhs = m.group(1)
            ctype = None
            for fr in reversed(scopes):
                if lhs in fr.vals:
                    ctype = fr.vals[lhs]
                    break
            ent = (type_info.get(ctype) or {}).get("cmp", {}).get(m.group(2)) \
                if ctype else None
            if ent is not None:
                rhs = text[m.start(3):m.end(3)].strip()
                src = _copy_source(rhs, ctype, scopes, type_info)
                if src is None:
                    raise CppError(
                        "`%s %s %s`: the right-hand side is not an object of "
                        "type %s that this pass can name. Assign it to a "
                        "typed local first."
                        % (lhs, m.group(2), rhs, ctype))
                out.append("%s(&%s, &%s)" % (ent["fn"], lhs, src))
                i = m.end()
                continue

        m = conv_init_re.match(look, i)
        if m and not aggs and m.group(1) not in type_info:
            ent = _conv_for(m.group(3), scopes, type_info)
            if ent is not None and ent["ret"].replace("*", "").strip() \
                    == m.group(1):
                out.append("%s %s = %s(&%s);"
                           % (m.group(1), m.group(2), ent["fn"], m.group(3)))
                scopes[-1].vals.pop(m.group(2), None)
                i = m.end()
                continue

        m = conv_assign_re.match(look, i)
        if m and not aggs:
            lhs = m.group(1)
            known_lhs = None
            for fr in reversed(scopes):
                if lhs in fr.vals:
                    known_lhs = fr.vals[lhs]
                    break
            if known_lhs is None:
                ent = _conv_for(m.group(2), scopes, type_info)
                if ent is not None:
                    out.append("%s = %s(&%s);"
                               % (lhs, ent["fn"], m.group(2)))
                    i = m.end()
                    continue

        m = aug_re.match(look, i)
        if m and not aggs:
            lhs, op = m.group(1), m.group(2)
            ctype = None
            for fr in reversed(scopes):
                if lhs in fr.vals:
                    ctype = fr.vals[lhs]
                    break
            ent = (type_info.get(ctype) or {}).get("augassign", {}).get(op) \
                if ctype else None
            if ent is not None:
                rhs = text[m.start(3):m.end(3)].strip()
                if _blank_strings(rhs).count("=") > rhs.count("=="):
                    raise CppError(
                        "`%s %s= %s`: a chained assignment is not in the C++ "
                        "subset -- a compound assignment is lowered to a "
                        "`void` call, so there is no result to assign onward."
                        % (lhs, op, rhs))
                # The operand is taken by reference, like `operator=`'s, so
                # it has to be something this pass can name and address.
                otype = ent.get("operand") or ctype
                src = _copy_source(rhs, otype, scopes, type_info)
                if src is None:
                    raise CppError(
                        "`%s %s= %s`: the right-hand side is not an object "
                        "of type %s that this pass can name. Assign it to a "
                        "typed local first." % (lhs, op, rhs, otype))
                out.append("%s(&%s, &%s);" % (ent["fn"], lhs, src))
                i = m.end()
                continue

        m = assign_re.match(look, i)
        if m and not aggs:
            lhs = m.group(1)
            ctype = None
            # `_named_object` resolves a bare local and a member chain alike,
            # and hands back the path to write -- which for a lowered
            # reference parameter is a dereference rather than the name.
            lhs_is_chain = bool(re.search(r"\.|->", lhs))
            lfound = _named_object(lhs, scopes, type_info)
            # A chain ending in a scalar field resolves fine and names no
            # class; everything below reads `type_info[ctype]`, so only a
            # class it knows is taken.
            if lfound is not None and lfound[1] in type_info:
                lhs, ctype = lfound
            _rhs = m.group(2).strip()
            _can_lower = ctype is not None and (
                _rhs in ("nullptr", "NULL")
                or _move_operand(m.group(2)) is not None
                or (type_info[ctype]["assign"]
                    and _copy_source(_rhs, ctype, scopes,
                                     type_info) is not None))
            if lhs_is_chain and ctype is not None and not _can_lower:
                # A member assignment whose right-hand side this pass cannot
                # name as the same class. Before member chains were matched
                # at all these fell through untouched, and refusing them now
                # would reject files that have always translated.
                #
                # litehtml's `borders` is the case in hand: it assigns a
                # `css_border` to a `border`, which is `operator=` overloaded
                # on the parameter *type* at one arity. Overloads here are
                # told apart by argument count, so the second one cannot be
                # represented -- and until it can, the honest thing is to
                # leave the statement exactly as it was rather than claim a
                # refusal the pass has not earned.
                ctype = None
                lhs = m.group(1)
            info_a = type_info.get(ctype) if ctype is not None else None
            if info_a is not None and info_a["dtor"] and 0 in info_a["ctors"] \
                    and m.group(2).strip() in ("nullptr", "NULL"):
                # `p = nullptr;` on an owning class -- release what is held
                # and leave a default-constructed object, which is what
                # `shared_ptr::operator=(nullptr_t)` does. Without this the
                # struct was overwritten with zeroes and whatever it owned
                # was never freed.
                out.append("%s(&%s); %s(&%s);"
                           % (_dropfn(info_a, ctype), lhs,
                              info_a["ctors"][0]["fn"], lhs))
                i = m.end()
                continue
            moved = _move_operand(m.group(2)) if info_a is not None else None
            if moved is not None and (info_a["moveassign"] or info_a["assign"]):
                # `b = std::move(a);`. With no `operator=(T &&)` the const-ref
                # overload binds the rvalue, which is what C++ does -- so a
                # source that gains a `std::move` keeps working before the
                # move assignment is written.
                src = _copy_source(moved, ctype, scopes, type_info)
                if src is None:
                    raise CppError(
                        "`%s = std::move(%s)`: the operand of `std::move` has "
                        "to be an object of type %s that this pass can name. "
                        "Assign it to a typed local first."
                        % (lhs, moved, ctype))
                fn = "%s__moveassign" % ctype if info_a["moveassign"] \
                    else "%s__assign" % ctype
                # The source is not dropped from this scope: a moved-from
                # object is still destroyed in C++.
                out.append("%s(&%s, &%s);" % (fn, lhs, src))
                i = m.end()
                continue
            if ctype is not None and type_info[ctype]["assign"]:
                rhs = m.group(2).strip()
                if "=" in _blank_strings(rhs).replace("==", ""):
                    raise CppError(
                        "`%s = %s`: a chained assignment is not in the C++ "
                        "subset -- `operator=` is lowered to a `void` call, "
                        "so there is no result to assign onward."
                        % (lhs, rhs))
                src = _copy_source(rhs, ctype, scopes, type_info)
                if src is None and 1 in info_a["ctors"] and \
                        _converting_operand(rhs, scopes, type_info):
                    # `str = name;` where `name` is a `const char *`. C++
                    # builds a temporary through the one-argument
                    # constructor and assigns from it; written out, that is
                    # exactly this. The temporary is destroyed straight
                    # after, so nothing outlives the statement.
                    tmpn = "__cpp_cv%d" % mvn[0]
                    mvn[0] += 1
                    out.append("{ %s %s; %s(&%s, %s); %s__assign(&%s, &%s); "
                               "%s(&%s); }"
                               % (ctype, tmpn, info_a["ctors"][1]["fn"],
                                  tmpn, rhs, ctype, lhs, tmpn,
                                  _dropfn(info_a, ctype), tmpn))
                    i = m.end()
                    continue
                if src is None and _is_call_result(rhs):
                    # `a = f();` -- the callee returned by value, which is a
                    # move *out* of its local, so there is no second owner
                    # and nothing to copy. The old value still has to be
                    # destroyed, and the result put in its place.
                    #
                    # Order matters, and the temporary is what gets it
                    # right: `tmp = tmp.substr(1)` reads the very object
                    # being assigned, so dropping first would hand the call
                    # a freed buffer. Evaluate, then drop, then move in --
                    # the order C++ uses too.
                    #
                    # The temporary is deliberately not registered as live:
                    # its representation is handed to `lhs`, which already
                    # is, and dropping both would free once too often.
                    tmpn = "__cpp_as%d" % mvn[0]
                    mvn[0] += 1
                    out.append("{ %s %s = %s; %s(&%s); %s = %s; }"
                               % (ctype, tmpn, rhs,
                                  _dropfn(type_info.get(ctype), ctype), lhs,
                                  lhs, tmpn))
                    i = m.end()
                    continue
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

        if text.startswith("__cpp_move", i) and \
                (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            # Everything above consumed the *statement* positions -- a
            # declaration, an assignment -- and returned before reaching
            # here. What is left is expression position, which is exactly
            # what a statement expression can hold.
            om = _MOVE_CALL.match(text, i)
            if om is not None:
                close = _match_paren(text, om.end() - 1)
                if close is not None:
                    if _move_method_receiver(text, i, scopes,
                                             type_info) is not None:
                        # A move overload takes the source by reference, so
                        # this one is carried through untouched and the call
                        # rewriter picks the overload from it.
                        out.append(text[i:close + 1])
                        i = close + 1
                        continue
                    out.append(_materialise_moves(
                        text[i:close + 1], scopes, type_info, mvn))
                    i = close + 1
                    continue

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
        # Two words usually mean a type and a name -- but not when the first
        # is a keyword. `Holder h(new Thing())` is a local with a constructor
        # argument, and reading `new Thing()` as a parameter made the by-value
        # check refuse the declaration as if it were a function returning one.
        if toks and toks[0] in ("new", "delete", "sizeof", "return"):
            return False
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


def _check_owning_args(text, cinfo, path):
    """Reject handing an owned object to a call by value.

    This is the cross-language shape of the same double free Crust fixed on
    its own side, and it aborts rather than leaks:

        int go(void) {
            Tally t;  t.start();  t.add(1);
            return consume(t.samples);   // a Rust `fn consume(v: Vec<i32>)`
        }

    Crust lowers a by-value owning parameter to a drop when the callee
    returns -- passing by value is a *move* there -- so `consume` frees the
    buffer. `Tally_drop` then frees it again on the way out of `go`.

    Refused rather than lowered, for the reason `_check_by_value` gives just
    below: doing it properly means moving out of the source, and this is
    expression position. The honest fix on the C++ side is to pass a pointer,
    which is also what a Rust `&Vec<i32>` parameter lowers to -- so a
    reference-taking signature needs no change here at all.

    The *lowered* text is what gets scanned, which is what keeps this precise:
    a by-reference call has already become `f(&v)` by now, so only a genuine
    by-value hand-off is left looking like a bare name.
    """
    owning = set(n for n in cinfo if cinfo[n]["dtor"])
    if not owning:
        return
    # Fields of an owning type, and locals declared as one.
    members = {}
    for cls in cinfo:
        for fname, (fcls, is_ptr) in cinfo[cls]["fields"].items():
            if not is_ptr and fcls in owning:
                members[fname] = fcls
    # Kept per enclosing function, not per file. A flat map made every
    # `val` in the translation a `string` because one function declared
    # one: quickjs.h's `JS_NewBool(JSContext *, JS_BOOL val)` was refused
    # for handing a `string` to `JS_MKVAL`, on a parameter that is an
    # `int`. The name is the same; the variable is not.
    locals_ = {}
    for m in re.finditer(r"(?<![\w.>])(\w+)\s+(\w+)\s*[;=]", text):
        if m.group(1) in owning:
            locals_.setdefault(m.group(2), []).append(
                (_toplevel_start(text, m.start()), m.group(1)))

    def owner_at(name, pos):
        """The owning class `name` has where `pos` is, if any.

        A declaration counts only if it sits in the same top-level
        declaration as the use -- which is what makes two functions each
        naming a `val` two variables rather than one.
        """
        here = _toplevel_start(text, pos)
        for start, cls in locals_.get(name, ()):
            if start == here:
                return cls
        return None
    if not members and not locals_:
        return

    # Only calls to something this file did *not* define. A call into a class
    # here -- a constructor, a copy constructor, a method -- already has this
    # pass managing the lifetime, and `Buf c(a);` is a declaration rather than
    # a call at all. What is left is the boundary: a Rust `fn` taking an
    # owning parameter, which is where ownership silently changes hands.
    local_fns = set(cinfo)
    for m in re.finditer(r"(?<![\w.])(\w+)\s*\(", text):
        close = _match_paren(text, m.end() - 1)
        if close is None:
            continue
        tail = text[close + 1:close + 40].lstrip()
        if not (tail.startswith("{") or tail.startswith(";")):
            continue
        # `;` alone is not enough: `return consume(t.samples);` ends in one
        # too. Parameters have a type *and* a name, which is what tells a
        # declaration from a call -- the same test `_check_by_value` makes.
        if _looks_like_params(_split_top(text[m.end():close]), cinfo):
            local_fns.add(m.group(1))    # a definition or a declaration
    for cls in cinfo:
        local_fns.add("%s_drop" % cls)
        local_fns.add(_dropfn(cinfo[cls], cls))
        for meth in cinfo[cls]["methods"]:
            local_fns.add("%s_%s" % (cls, meth))

    for m in re.finditer(r"(?<![\w.>&])(\w+)\s*\(", text):
        fn = m.group(1)
        if fn in _KEYWORDS or fn in local_fns:
            continue
        # This pass's own output: the `__cpp_copy` / `__cpp_drop` placeholders
        # substitution works through, and the generated methods of a supplied
        # container. Their lifetimes are this pass's business, not a boundary.
        if fn.startswith("__") or any(fn.startswith(c + "_") for c in cinfo):
            continue
        close = _match_paren(text, m.end() - 1)
        if close is None:
            continue
        for part in _split_top(text[m.end():close]):
            arg = part.strip()
            if not arg or arg.startswith("&"):
                continue                 # an address: nothing is handed over
            cls = owner_at(arg, m.start())
            if cls is None:
                mm = re.match(r"^[\w]+(?:\.|->)(\w+)$", arg)
                if mm and mm.group(1) in members:
                    cls = members[mm.group(1)]
            if cls is None:
                continue
            raise CppError(
                "%s: `%s(%s)` hands over a `%s` by value, but this side still "
                "owns it and will destroy it -- and a by-value owning "
                "parameter is destroyed by the callee too, so one buffer is "
                "freed twice. Pass `&%s`; a Rust `&%s` parameter lowers to "
                "exactly that pointer."
                % (path, fn, arg, cls, arg, cls))


def _check_by_value(text, cinfo, path):
    """Reject by-value class parameters and returns for owning classes.

    A by-value *return* is a silent miscompile otherwise: the local is
    destroyed on the way out, so the caller receives a copy of an object
    whose resources were just released -- a use-after-free that no
    diagnostic points at.

    A by-value *parameter* used to be refused here for the matching reason,
    that the copy was never constructed and never destroyed. Both halves
    exist now, so instead of refusing this collects them:
    `{function: {position: (class, parameter name)}}`. Classes with no
    destructor own nothing and are left alone.
    """
    byval = {}
    owning = set(n for n in cinfo if cinfo[n]["dtor"])
    if not owning:
        return byval
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
        for idx, part in enumerate(parts):
            cls = _by_value_class(part.strip(), cinfo)
            if cls in owning:
                # No longer refused. A by-value owning parameter is an object
                # the *callee* owns: C++ constructs it at the call and
                # destroys it when the function returns, and both halves are
                # now written out -- the caller constructs into it (below)
                # and the callee's frame drops it like a local. Recorded here
                # so the call sites can be rewritten, since a bitwise copy
                # with no constructor is still what plain C would do.
                nm = _param_name(part.strip())
                if nm:
                    byval.setdefault(m.group(1), {})[idx] = (cls, nm)
        ret = _func_return_type(text, m.end() - 1)
        toks = [t for t in (ret or "").replace("*", " * ").split()
                if t != "const"]
        if toks and toks[0] in owning and "*" not in toks:
            # `return v;` on a bare owning local is a *move out*: the scope
            # rewriting leaves it out of that path's drops, so the object the
            # caller receives is the one that was here rather than a copy of
            # a released one. That is the same rule Crust follows on the Rust
            # side, and it is what makes returning a `shared_ptr` by value --
            # the idiom this whole subset would otherwise have to ban -- both
            # possible and correct.
            #
            # What is still refused is returning something that is *not* a
            # bare local, since there is nothing to move out of.
            body = _func_body(text, m.end() - 1)
            # A *declaration* has no body to read. Its definition is checked
            # wherever it is written, and prototypes are hoisted above every
            # definition now -- so checking them here would report the
            # declaration of a function whose definition is perfectly fine.
            if body is None or _returns_only_bare_locals(body):
                continue
            raise CppError(
                "%s: `%s` returns `%s` by value, and its `return` is not a "
                "bare local. A returned local is moved out -- it is left out "
                "of the drops on that path -- but an expression has nothing "
                "to move from, so the caller would receive a copy of a "
                "released object. Return `%s *`, or assign to a local first."
                % (os.path.basename(path), m.group(1), toks[0], toks[0]))
    return byval


def _construct_byval_args(text, byval, cinfo, path):
    """Copy-construct the arguments a by-value owning parameter takes.

    A by-value owning parameter is an object the callee destroys, so the
    caller has to *construct* it rather than hand over a struct copy --
    otherwise both sides own one resource and both free it. A `std::move`
    argument has already become a statement expression yielding a
    constructed temporary, and is left alone; everything else is a copy, and
    is materialised the same way:

        sink(a)   ->   sink(({ Buf _cpp_ba0; Buf_copy(&_cpp_ba0, &a); _cpp_ba0; }))

    Run after the call rewriting, so what is seen here is the lowered call.
    """
    if not byval:
        return text
    n = [0]

    def one(mtext, fname):
        close = _match_paren(text, mtext.end() - 1)
        if close is None:
            return None
        parts = _split_top(text[mtext.end():close])
        tail = text[close + 1:close + 40].lstrip()
        if (tail.startswith("{") or tail.startswith(";")) and \
                _looks_like_params(parts, cinfo):
            # The declaration or definition, not a call. Told apart by the
            # *parameters* rather than by the terminator: `int r = sink(a);`
            # ends in a `;` too, and reading that as a declaration left its
            # argument handed over as a struct copy -- both sides then owned
            # one buffer and both freed it.
            return None
        slots = byval[fname]
        if len(parts) != len(slots) and not slots:
            return None
        outp = []
        for idx, part in enumerate(parts):
            arg = part.strip()
            if idx not in slots or arg.startswith("({"):
                outp.append(part)
                continue
            cls = slots[idx][0]
            info = cinfo.get(cls)
            if info is None:
                outp.append(part)
                continue
            if not info["copy"]:
                raise CppError(
                    "%s: `%s` takes `%s` by value, which the callee "
                    "destroys, so the argument has to be constructed -- and "
                    "%s has a destructor but no copy constructor. Hand it "
                    "over with `std::move(..)`, or add `%s(const %s &o)`."
                    % (os.path.basename(path), fname, cls, cls, cls, cls))
            tmp = "_cpp_ba%d" % n[0]
            n[0] += 1
            outp.append(" ({ %s %s; %s_copy(&%s, &(%s)); %s; })"
                        % (cls, tmp, cls, tmp, arg, tmp))
        return (close, "%s(%s)" % (fname, ",".join(outp)))

    out, i = [], 0
    while i < len(text):
        m = re.compile(r"(?<![\w.>])(\w+)\s*\(").match(text, i)
        if m is not None and m.group(1) in byval:
            got = one(m, m.group(1))
            if got is not None:
                out.append(got[1])
                i = got[0] + 1
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _param_name(part):
    """The declared name of one parameter, or None."""
    toks = [t for t in part.replace("*", " * ").split() if t != "const"]
    if len(toks) < 2 or not re.match(r"^\w+$", toks[-1]):
        return None
    return toks[-1]


def _addr_of_expr(expr):
    """`&expr`, or `expr` when it is already an address.

    `T_copy` takes a pointer. A copy source is written as a value
    (`*val.m_left`, `other`), so it normally needs an `&` -- except where
    the author already dereferenced a pointer, in which case the two cancel
    and the pointer itself is what to pass.
    """
    expr = expr.strip()
    if expr.startswith("*"):
        return expr[1:].strip()
    if expr.startswith("&"):
        return expr
    return "&(%s)" % expr


def _is_call_result(rhs):
    """Is `rhs` a call -- something whose value was returned to us?

    A returned owning value has been moved out of the callee, so taking it
    is a move rather than a copy. Anything else (a name, a member, a
    subscript) is still an object someone else owns.
    """
    rhs = rhs.strip()
    if not rhs.endswith(")"):
        return False
    # The *last* call in the expression is the one whose value this is, so
    # the opening paren to match is the one that closes at the end. A chain
    # like `a.get()->self()` has earlier parens that close sooner.
    op = -1
    for k, c in enumerate(rhs):
        if c == "(" and _match_paren(rhs, k) == len(rhs) - 1:
            op = k
            break
    if op <= 0:
        return False
    head = rhs[:op].strip()
    # `make_shared<css_selector>(..)` -- a template argument list is part of
    # the callee's *name*, not an operator, but the guard below reads a bare
    # `<` as a comparison and so refused every one of these. Stripping a
    # balanced trailing `<..>` that hangs off an identifier leaves an
    # ordinary callee for that check to pass on.
    #
    # One level only: a nested list (`make_shared<vector<int>>`) is left to
    # be refused rather than half-parsed here.
    tm = re.match(r"^(.*\w)\s*<([^<>]*)>$", head)
    if tm is not None:
        head = tm.group(1)
    # Whatever precedes it has to read as a callee -- a name, a member path,
    # or a chain of calls on one. Anything with an operator in it is an
    # expression whose value is not simply what a call returned.
    return re.match(r"^[\w:.>()\[\]\s-]+$", head) is not None \
        and not re.search(r"[+*/%!&|^~?]|(?<![-<])>(?!)|<(?!)", head)


def _normalise_empty_params(text):
    """`f() {` -> `f(void) {` for a definition at file scope.

    **Not called.** `int f()` means no parameters in C++ and unspecified in
    C, so a free function written the C++ way is not recognised as a
    definition -- but rewriting it here broke eight tests around out-of-line
    definitions and header expansion, and the cause was not diagnosed. Kept
    for whoever picks it up; the workaround is to write `(void)`.
    """
    look = _blank_strings(_strip_comments(text))
    out, depth, i, n = [], 0, 0, len(look)
    last = 0
    while i < n:
        c = look[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif depth == 0 and c == "(":
            m = re.match(r"\(\s*\)\s*(?:const\s*)?\{", look[i:])
            j = i - 1
            while j >= 0 and look[j] in " \t":
                j -= 1
            k = j
            while k >= 0 and (look[k].isalnum() or look[k] == "_"):
                k -= 1
            word = look[k + 1:j + 1]
            if m and word and word not in _KEYWORDS:
                out.append(text[last:i])
                out.append("(void)")
                last = i + look[i:].index(")", 1) + 1
        i += 1
    out.append(text[last:])
    return "".join(out)


def _func_body(text, op):
    """The braced body of the function whose `(` is at `op`, or None."""
    close = _match_paren(text, op)
    if close is None:
        return None
    brace = text.find("{", close)
    if brace < 0 or ";" in text[close + 1:brace]:
        return None
    end = _match_brace(text, brace)
    return None if end is None else text[brace + 1:end]


def _owning_return_class(rtype, type_info):
    """The class of a by-value return type that owns something, else None.

    A pointer return hands back a borrow and needs no copy; a class with no
    destructor owns nothing and its bitwise copy is already correct.
    """
    toks = [t for t in (rtype or "").replace("*", " * ").split()
            if t != "const"]
    if not toks or "*" in toks:
        return None
    info = type_info.get(toks[0])
    if info is None or not info["dtor"]:
        return None
    return toks[0]


def _returns_only_bare_locals(body):
    """Does every `return` in `body` hand an owning value on safely?

    Three shapes are safe. A **bare local** is moved out: the scope
    rewriting leaves it out of that path's drops. A **call result** was
    already moved out of the callee, so passing it straight on moves it
    again -- there is no local here to destroy, and no destructor runs on a
    temporary. A **named object** -- `m_root`, `this->m_root`, `a.b.c` --
    is copy-constructed into the return slot by the scope rewriting, which
    is what C++ does for `return m_root;` and what makes a `shared_ptr`
    getter increment the refcount rather than alias it.

    That last case is only checked for *shape* here, because this pass has
    no scope information to resolve the name with. The rewriting resolves it
    properly and refuses there if the class cannot be copied, so a chain
    that reaches this point and turns out to be uncopyable is still caught.

    Anything else -- a subscript, a dereference, an arithmetic expression --
    names no object this pass can copy from, and returning it would hand
    back a copy that is destroyed twice.
    """
    for m in re.finditer(r"(?<![\w.])return\b([^;]*);", body):
        expr = m.group(1).strip()
        if re.match(r"^\w*$", expr):
            continue                     # a bare local, or `return;`
        if _is_call_result(expr):
            continue
        if re.match(r"^\w+(?:\s*(?:\.|->)\s*\w+)+$", expr):
            continue                     # a named object: copied, not aliased
        return False
    return True


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


_EXTERN_LINKAGE = re.compile(r'\bextern\s*"C(?:\+\+)?"\s*')


def _strip_extern_c(text):
    """Remove `extern "C"` / `extern "C++"` linkage specifications.

    C has one linkage, so the specification means nothing once the C++ is
    gone -- but it is very much something to the C parser downstream, which
    stops at the string literal with `expected ';' after 'extern'`. Both
    spellings appear in real headers: a prefix on one declaration, and a
    brace block wrapping a whole file's worth of them.

    The block form is the one that has to be handled rather than rejected.
    Any C header guarded for C++ inclusion -- which is nearly all of them --
    wraps its entire body in `extern "C" { .. }`, so refusing it refuses the
    header and every file that includes it.

    Everything is blanked in place rather than deleted, and the braces of a
    block are blanked individually so the declarations between them keep
    their offsets. Every pass below reports by line number, and a header
    that lost a line here would move every diagnostic after it.
    """
    scan = _strip_comments(text)
    # Comments are gone but literals are not, because the match *is* a
    # literal: `"C"` blanked is `" "`, which no pattern can find. `look` is
    # consulted only to tell code from the inside of a string -- there, the
    # word `extern` itself would be blank.
    look = _blank_strings(scan)
    blanks = []
    for m in _EXTERN_LINKAGE.finditer(scan):
        if look[m.start():m.start() + 6] != "extern":
            continue                      # inside a string literal
        blanks.append((m.start(), m.end()))
        if scan[m.end():m.end() + 1] == "{":
            close = _match_brace(scan, m.end())
            if close is None:
                line = scan.count("\n", 0, m.start()) + 1
                raise CppError(
                    "%d: `extern \"C\" {` is never closed." % line)
            blanks.append((m.end(), m.end() + 1))
            blanks.append((close, close + 1))

    if not blanks:
        return text
    out = list(text)
    for start, end in blanks:
        for i in range(start, end):
            if out[i] != "\n":
                out[i] = " "
    return "".join(out)


_MAKE_PTR = re.compile(r"(?<![\w.>])make_(shared|unique)\s*<([^<>]+)>\s*\(")


def _lower_make_ptr(text):
    """`make_shared<T>(a, b)` -> `shared_ptr<T>(new T(a, b))`.

    `make_shared` cannot be written as a subset template: it has to forward
    an arbitrary number of arguments of types the call site never spells,
    and this subset has neither variadics nor deduction from a call. What it
    *can* be is the thing it is shorthand for, which is what this rewrite
    produces -- one allocation instead of make_shared's combined one, and
    otherwise the same object with the same lifetime.

    Nested template arguments (`make_shared<vector<int>>`) are left alone
    rather than half-matched; they are refused later, which is the honest
    outcome.
    """
    out, pos = [], 0
    while True:
        m = _MAKE_PTR.search(text, pos)
        if m is None:
            out.append(text[pos:])
            break
        close = _match_paren(text, m.end() - 1)
        if close is None:
            out.append(text[pos:m.end()])
            pos = m.end()
            continue
        kind, ty = m.group(1), m.group(2).strip()
        args = text[m.end():close]
        out.append(text[pos:m.start()])
        out.append("%s_ptr<%s>(new %s(%s))" % (kind, ty, ty, args))
        pos = close + 1
    return "".join(out)


def _mark_std_move(text):
    """`std::move(x)` -> `__cpp_move(x)`, on the qualified spelling only.

    `std::` is stripped rather than resolved, so this has to run before that
    happens: afterwards `move(x)` is just a call, and a project with its own
    `move` -- litehtml moves boxes -- would have every one of them rewritten.
    Requiring the qualifier is the whole safeguard, and it costs only
    `using namespace std;` plus a bare `move`, which is a shape worth not
    guessing at anyway.

    `std::forward` is deliberately absent: it means something only inside a
    template taking `T &&`, which this subset does not have, so a file naming
    it is refused elsewhere rather than quietly moved from.
    """
    return _sub_code(re.compile(r"\bstd\s*::\s*move\s*\("), "__cpp_move(",
                     text)


def _is_move_params(params, cname, raw_name, tsub, sub):
    """Is this parameter list a *move* constructor's -- one `T &&`?

    Read on the spelling, like `_is_copy_params`, and before reference
    lowering for the same reason. An `&&` parameter satisfies that test too
    -- it is a reference with one more `&` -- so the two are told apart by
    this one, and a caller that wants only copies has to subtract them.
    """
    if not _is_copy_params(params, cname, raw_name, tsub, sub):
        return False
    return "&&" in (params or "")


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
        # The receiver is parenthesised: it may already be `&c` for a value,
        # and `&c->_vptr` parses as `&(c->_vptr)` -- the address of the
        # pointer rather than the pointer. Dispatching on a value receiver
        # emitted that and did not compile.
        return ("((const struct %s_vtable *)(%s)->_vptr)->%s(%s%s)"
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
    # `;`, `=` and `,` end a declaration -- and so does a `)` when the
    # declaration is a `for` initialiser with no third clause, but that case
    # is covered by the `;` inside the `for` head. What was missing is that a
    # `for (T *it = ..; ..)` declaration ends at `;` *inside* parentheses,
    # which this pattern already allows; the gap was the pointer form being
    # required to have its star attached to the type.
    decl_re = re.compile(
        r"(?<![\w.])(%s)\s*(\*\s*)?(\w+)\s*(?=[;=,)])" % alt)
    call_re = re.compile(r"(?<![\w.>])(\w+)((?:\s*(?:\.|->)\s*\w+)+)\s*\(")
    # The same chain, but not followed by `(` -- a member read or write
    # rather than a call. The call pattern is tried first, so this only
    # ever sees what that one left behind.
    field_re = re.compile(
        r"(?<![\w.>])(\w+)((?:\s*(?:\.|->)\s*\w+)+)(?!\s*\()")
    builtin_re = re.compile(
        r"(?<![\w.>])(__cpp_copy|__cpp_movein|__cpp_drop|__cpp_eq"
        r"|__cpp_share_hook)"
        r"\s*\(")
    # `v[i]` / `a.b[i]` on a class that overloads subscript.
    index_re = re.compile(r"(?<![\w.>\]])(\w+)((?:\s*(?:\.|->)\s*\w+)*)\s*\[")
    # `p->x` where `p` is a *class* with `operator->`, and `*p` likewise. A
    # class-typed name followed by `->` is otherwise a lowered reference and
    # is left alone; only a class that declares the operator is rewritten.
    arrow_re = re.compile(r"(?<![\w.>\]])(\w+)\s*->\s*(?=[A-Za-z_])")
    star_re = re.compile(r"(?<![\w)\]])\*\s*(\w+)(?![\w\s]*[\[(])")
    # A call continuing a chain: `.g(` or `->g(` right after a `)`.
    cont_re = re.compile(r"\s*(?:\.|->)\s*(\w+)\s*\(")
    plain_re = re.compile(r"(?<![\w.>])(\w+)\s*\(")
    static_re = re.compile(r"(?<![\w.>:])(\w+)\s*::\s*(\w+)\s*\(")
    # `new T(..)` / `new T`, and `delete e` / `delete[] e`. The array forms
    # are matched so they can be reported: they are not simply unsupported
    # syntax, they are the shapes whose lowering would need an element count
    # stored beside the allocation.
    new_re = re.compile(r"(?<![\w.>])new\s+(\w+)\s*(\[)?")
    del_re = re.compile(r"(?<![\w.>])delete\b\s*(\[\s*\])?\s*")
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

        # Declarations are looked for outside parentheses -- an argument list
        # is full of names that are not declarations -- with one exception:
        # a `for` initialiser is a declaration *inside* parentheses, and
        # `for (string *it = v.begin(); ..)` is the iterator idiom the
        # containers here are built around. Recognised by the `for` that
        # opened the paren, so an ordinary argument list is untouched.
        if pdepth == 0 or (pdepth == 1 and _in_for_head(look, i)):
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
                raw = text[op + 1:close]
                mvarg = _move_operand(raw.strip())
                if mvarg is not None and \
                        meth in cinfo[cls].get("move_methods", {}):
                    # `v.push_back(std::move(p))`. The overload is chosen by
                    # the `std::move` being written, exactly as `operator=`
                    # is -- there is no arity to tell them apart. The
                    # operand is passed by reference, so `fix_args` takes
                    # its address like any other reference argument and no
                    # temporary is built.
                    ent = _pick(cinfo[cls]["move_methods"][meth], mvarg,
                                cls, meth)
                    args = fix_args(mvarg, ent["refs"], scopes)
                    expr = _emit_method_call(expr, cls, is_ptr, meth, args,
                                             ent, cinfo)
                    rcls, rptr = _ret_class(ent["ret"], cinfo)
                    expr, end = follow(expr, rcls, rptr, close + 1, meth)
                    out.append(expr)
                    i = end
                    continue
                if meth in cinfo[cls].get("deleted", {}) and \
                        meth not in cinfo[cls]["methods"]:
                    # The member was deleted because its body copies an
                    # element the element type cannot copy. C++ deletes it
                    # too -- but a *call* to a deleted member is an error
                    # there, and it has to be one here rather than an
                    # undefined symbol from the C front end.
                    raise CppError(
                        "`%s::%s` copies an element, and this element type "
                        "has a destructor and no copy constructor -- so the "
                        "member is deleted, exactly as in C++. Give the "
                        "element a copy constructor, or hand the element "
                        "over with `std::move(..)` if it has a move "
                        "constructor." % (cls, meth))
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
            if kind == "__cpp_share_hook":
                # `enable_shared_from_this<T>` needs the control block the
                # first `shared_ptr` made, so that `shared_from_this()` joins
                # it rather than starting a second one and freeing twice.
                # There is no way to ask "does T derive from it" in this
                # subset, so the question is asked of the *fields*: a class
                # that has the hook's members has the hook.
                paths = (cinfo.get(ty) or {}).get("paths") or {}
                if "esp" in paths and "esc" in paths:
                    # Through a function, not by reaching into the object:
                    # `shared_ptr<T>` is emitted above `T`, where `T` is
                    # still an incomplete type and `q->_base` will not
                    # compile. A prototype is enough for an incomplete type,
                    # and prototypes are hoisted above every definition.
                    out.append("%s__share_hook(%s, %s)"
                               % (ty, parts[1], parts[2]))
                else:
                    out.append("(void)0")
                i = close + 1
                continue
            if kind == "__cpp_movein":
                # `__cpp_movein(T, dst, srcptr)` -- construct `dst` from an
                # element the caller has handed over. The counterpart of
                # `__cpp_copy`, and the reason a container can hold a
                # move-only element at all: `__cpp_copy` refuses one, which
                # is correct, so a move needs its own spelling rather than a
                # weakening of that.
                if ty not in cinfo:
                    # A scalar has nothing to move; the assignment is the
                    # whole operation, as it is for `__cpp_copy`.
                    out.append("(%s) = (%s)" % (parts[1], parts[2]))
                elif cinfo[ty]["move"]:
                    out.append("%s_move(&%s, %s)" % (ty, parts[1], parts[2]))
                elif cinfo[ty]["copy"]:
                    # No move constructor: the copy binds the rvalue, which
                    # is what C++ overload resolution does here too.
                    out.append("%s_copy(&%s, %s)" % (ty, parts[1], parts[2]))
                elif not cinfo[ty]["dtor"]:
                    # Neither constructor, but nothing owned either: the
                    # class is plain data, so constructing from a handed-over
                    # element is assignment -- the same reading `__cpp_copy`
                    # takes one branch up, and the same one C++ takes, where
                    # a struct of four ints has an implicit copy constructor
                    # and needs no move.
                    #
                    # Without this a `vector<position>` was refused outright:
                    # the implicit-copy pass deliberately leaves plain data
                    # its bitwise copy and so sets no `copy` flag, and this
                    # hook read that absence as "cannot be copied" rather
                    # than "does not need to be".
                    #
                    # The source is a reference, already lowered to a
                    # pointer, so it is dereferenced here; the scalar branch
                    # above receives a value and does not.
                    out.append("(%s) = (*(%s))" % (parts[1], parts[2]))
                else:
                    raise CppError(
                        "`__cpp_movein(%s, ..)`: %s has neither a move nor a "
                        "copy constructor, so an element cannot be "
                        "constructed in place. Add `%s(%s &&o)`."
                        % (ty, ty, ty, ty))
                i = close + 1
                continue
            if kind == "__cpp_eq":
                # Comparing two elements. Unlike copy and destroy this has to
                # work for a scalar too -- a `map<int, ..>` compares its keys
                # with `==` and a `map<string, ..>` cannot.
                if ty not in cinfo:
                    out.append("((%s) == (%s))" % (parts[1], parts[2]))
                elif "equals" in cinfo[ty]["methods"]:
                    # The second operand arrives as a reference, which is
                    # already a pointer by now; the first is an lvalue.
                    out.append("%s_equals(&(%s), %s)"
                               % (ty, parts[1], parts[2]))
                else:
                    raise CppError(
                        "`__cpp_eq(%s, ..)`: %s is a class with no `equals`, "
                        "so two of them cannot be compared. Add "
                        "`int equals(const %s &o)`." % (ty, ty, ty))
                i = close + 1
                continue
            if ty not in cinfo:
                # A scalar element: copying one is an assignment and
                # destroying one is nothing. The point of these builtins is
                # that a container can say "copy an element" once and have it
                # mean the right thing per instantiation, and a container
                # keyed on `int` is as much an instantiation as one keyed on
                # `string`.
                if kind == "__cpp_drop":
                    out.append("(void)0")
                else:
                    out.append("(%s) = (%s)" % (parts[1], parts[2]))
                i = close + 1
                continue
            if kind == "__cpp_drop":
                out.append("%s(&%s)" % (_dropfn(cinfo[ty], ty), parts[1])
                           if cinfo[ty]["dtor"] else "(void)0")
            else:
                if not cinfo[ty]["copy"] and not cinfo[ty]["dtor"]:
                    # Neither a copy constructor nor a destructor: the class
                    # owns nothing, so copying it *is* assignment -- which is
                    # what C++ does for one too. The refusal below is about
                    # duplicating something owned.
                    #
                    # The source is an address here, not a value: every
                    # caller of this hook on a *class* passes one, because
                    # the `%s_copy` form below takes a pointer and the two
                    # have to agree. The scalar branch further up is the one
                    # that receives a value. Assigning without the
                    # dereference put a pointer where the element goes, which
                    # the C front end rejected as an invalid conversion --
                    # visible only once a plain-data class reached this
                    # branch at all.
                    out.append("(%s) = (*(%s))" % (parts[1], parts[2]))
                    i = close + 1
                    continue
                if not cinfo[ty]["copy"]:
                    raise CppError(
                        "`__cpp_copy(%s, ..)`: %s has no copy constructor, "
                        "so an element copy would duplicate whatever it "
                        "owns. Add `%s(const %s &o)`." % (ty, ty, ty, ty))
                out.append("%s_copy(&%s, %s)" % (ty, parts[1], parts[2]))
            i = close + 1
            continue

        m = arrow_re.match(look, i)
        if m:
            got = resolve(scopes, m.group(1), [])
            # Only on a class *value*. `Ptr *p; p->x` is ordinary member
            # access on `Ptr` in C++, not the operator -- and `this->` is the
            # same shape, so rewriting pointers turned every field access
            # inside the class into a call to its own `operator->`.
            if got is not None and not got[2] and got[1] in cinfo \
                    and cinfo[got[1]]["arrow"] is not None:
                expr, cls, is_ptr = got
                ent = cinfo[cls]["arrow"]
                # `u->v` is `u.operator->()->v`: the operator hands back a
                # plain pointer and the `->` that follows is ordinary C.
                out.append("%s(%s)->" % (ent["fn"], _addr(expr, is_ptr)))
                i = m.end()
                continue

        m = star_re.match(look, i)
        if m:
            got = resolve(scopes, m.group(1), [])
            # Likewise: `*p` on a genuine pointer is a plain dereference.
            if got is not None and not got[2] and got[1] in cinfo \
                    and cinfo[got[1]]["star"] is not None:
                expr, cls, is_ptr = got
                ent = cinfo[cls]["star"]
                # Like `operator[]`: the lowered form yields the address, and
                # the dereference written back keeps `*p = x` an lvalue.
                out.append("(*%s(%s))" % (ent["fn"], _addr(expr, is_ptr)))
                i = m.end()
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
                    # A subscript operator may take its argument by
                    # reference -- `map<string, ..>` has to, since a key
                    # that owns something cannot be passed by value -- so
                    # the call site addresses it like any other.
                    sub_expr = ("(*%s(%s, %s))"
                                % (ent["fn"], _addr(expr, is_ptr),
                                   fix_args(text[ob + 1:cb],
                                            ent.get("refs") or set(),
                                            scopes).strip()))
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
                if ar == 1 and cinfo[tname]["copy"]:
                    # `new T(other)` -- copy construction. The copy
                    # constructor is kept apart from `ctors` (it lowers to
                    # `T_copy`, not `T_new`), so an arity-1 lookup misses it
                    # and the class looks as if it has no such constructor.
                    #
                    # `make_shared<T>(*p)` lowers to exactly this shape, so
                    # refusing it refused every copy through a smart pointer
                    # -- `css_selector`'s own copy constructor among them.
                    #
                    # Allocate, then copy into the storage: the statement
                    # expression yields the pointer, which is what `new`
                    # evaluates to.
                    csrc = raw.strip()
                    out.append(
                        "({ %s *__cpp_nc = %s(); %s_copy(__cpp_nc, %s); "
                        "__cpp_nc; })"
                        % (tname, cinfo[tname]["ctors"][0]["alloc"]
                           if 0 in ctors else "%s__alloc" % tname,
                           tname, _addr_of_expr(csrc)))
                    i = end
                    continue
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
                    "do { if (%s) { ((const struct %s_vtable *)(%s)->_vptr)"
                    "->%s(%s); free(%s); } } while (0)"
                    % (expr, decl, dcast(cinfo[dcls]["root"], expr),
                       _DTOR_SLOT, dcast(decl, expr), expr))
            elif cinfo[dcls]["dtor"]:
                # Guarded and wrapped: `delete` on a null pointer is a no-op
                # in C++, and a bare block would leave a stray `;` before an
                # `else` when the delete is a branch's only statement.
                out.append("do { if (%s) { %s(%s); free(%s); } } "
                           "while (0)" % (expr, _dropfn(cinfo[dcls], dcls),
                                          expr, expr))
            else:
                out.append("free(%s)" % expr)
            i = end
            continue

        # `Cls::name(..)` -- a static member function. It has no receiver,
        # so it is a plain call to the emitted `Cls_name`; without this the
        # qualified name survived into the C output and the callee looked
        # like an unknown external function, which in turn made passing an
        # owning argument look like a double free.
        m = static_re.match(look, i)
        if m:
            _sinfo = (cinfo.get(m.group(1)) or {}).get("methods", {})
            _cands = _sinfo.get(m.group(2)) or {}
            op = m.end() - 1
            close = _match_paren(look, op)
            _ent = None
            if close is not None:
                _ar = _arity(text[op + 1:close])
                _ent = _cands.get(_ar)
            if _ent is not None and _ent.get("static"):
                args = fix_args(text[op + 1:close], _ent["refs"], scopes)
                out.append("%s(%s)" % (_ent["fn"], args))
                i = close + 1
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
void *memmove(void *, const void *, unsigned long);
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
    int length() { return sn; }
    string substr(int pos, int n) {
        string r;
        if (pos < 0) { pos = 0; }
        if (pos > sn) { return r; }
        if (n < 0 || pos + n > sn) { n = sn - pos; }
        r.reserve(n);
        if (n > 0) { memcpy(r.sd, sd + pos, (unsigned long)n); }
        r.sn = n;
        if (r.sd != 0) { r.sd[n] = 0; }
        return r;
    }
    string substr_from(int pos) { return substr(pos, -1); }
    int find_char(char c, int from) {
        int i = from;
        if (i < 0) { i = 0; }
        while (i < sn) { if (sd[i] == c) { return i; } i = i + 1; }
        return -1;
    }
    int find(char c) { return find_char(c, 0); }
    int rfind(char c) {
        int i = sn - 1;
        while (i >= 0) { if (sd[i] == c) { return i; } i = i - 1; }
        return -1;
    }
    int find_first_of(char c) { return find_char(c, 0); }
    int find_last_of(char c) { return rfind(c); }
    void erase(int pos, int n) {
        if (pos < 0 || pos >= sn) { return; }
        if (n < 0 || pos + n > sn) { n = sn - pos; }
        if (n <= 0) { return; }
        memmove(sd + pos, sd + pos + n, (unsigned long)(sn - pos - n));
        sn = sn - n;
        sd[sn] = 0;
    }
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
        while (i < o.vn) { __cpp_copy(T, vd[i], &o.vd[i]); i = i + 1; }
        vn = o.vn;
    }
    vector<T> &operator=(const vector<T> &o) {
        if (vd != o.vd) {
            vn = 0;
            reserve(o.vn);
            int i = 0;
            while (i < o.vn) { __cpp_copy(T, vd[i], &o.vd[i]); i = i + 1; }
            vn = o.vn;
        }
    }
    ~vector() { clear(); free(vd); vd = 0; vcap = 0; }
    int size() { return vn; }
    int empty() { if (vn == 0) { return 1; } return 0; }
    void reserve(int c) {
        if (c > vcap) {
            int m = c;
            T *nd = (T *)realloc(vd, (unsigned long)m * sizeof(T));
            if (nd != 0) { vd = nd; vcap = m; }
        }
    }
    void push_back(__cpp_ref(T) v) {
        if (vn == vcap) {
            int m = vcap * 2;
            if (m < 4) { m = 4; }
            reserve(m);
        }
        if (vn < vcap) { __cpp_copy(T, vd[vn], v); vn = vn + 1; }
    }
    /* The move overload. Told apart from the one above by whether the call
       site wrote `std::move`, not by arity -- both take one argument. This
       is what lets a container hold a move-only element: `__cpp_copy`
       refuses one, correctly, so a move needs its own spelling. */
    void push_back(__cpp_rref(T) v) {
        if (vn == vcap) {
            int m = vcap * 2;
            if (m < 4) { m = 4; }
            reserve(m);
        }
        if (vn < vcap) { __cpp_movein(T, vd[vn], v); vn = vn + 1; }
    }
    void pop_back() { if (vn > 0) { vn = vn - 1; __cpp_drop(T, vd[vn]); } }
    void clear() { while (vn > 0) { vn = vn - 1; __cpp_drop(T, vd[vn]); } }
    /* No `T get(int i)`: returning an element by value copies an object the
       caller never constructed, which is refused for an owning element type
       and would be wrong for it anyway. `v[i]` yields the element itself. */
    void set(int i, __cpp_ref(T) v) { __cpp_drop(T, vd[i]); __cpp_copy(T, vd[i], v); }
    T *ptr(int i) { return vd + i; }
    /* Insert before `pos`, returning an iterator to the new element.
       An iterator here is a `T *` into the buffer, so the position is
       taken as an index *before* `reserve` -- a reallocation moves the
       buffer and would leave the caller's pointer dangling.
       The tail is shifted by its representation rather than element by
       element: moving an object is exactly what that is, and it avoids
       constructing into storage that already holds something. */
    T *insert(T *pos, __cpp_ref(T) v) {
        int idx = (int)(pos - vd);
        if (idx < 0) { idx = 0; }
        if (idx > vn) { idx = vn; }
        reserve(vn + 1);
        if (vn > idx) {
            memmove(vd + idx + 1, vd + idx,
                    (unsigned long)((vn - idx) * (int)sizeof(T)));
        }
        __cpp_copy(T, vd[idx], v);
        vn = vn + 1;
        return vd + idx;
    }
    /* Erase [first, last), returning an iterator to what followed the
       range. The two-iterator form is a separate arity, so it does not
       collide with the one below. */
    T *erase(T *first, T *last) {
        int i = (int)(first - vd);
        int j = (int)(last - vd);
        if (i < 0) { i = 0; }
        if (j > vn) { j = vn; }
        if (j <= i) { return vd + i; }
        int k = i;
        while (k < j) { __cpp_drop(T, vd[k]); k = k + 1; }
        if (vn - j > 0) {
            memmove(vd + i, vd + j, (unsigned long)((vn - j) * (int)sizeof(T)));
        }
        vn = vn - (j - i);
        return vd + i;
    }
    /* Erase at `pos`, returning an iterator to what followed it -- which is
       why a loop written `it = v.erase(it)` keeps working. */
    T *erase(T *pos) {
        int idx = (int)(pos - vd);
        if (idx < 0 || idx >= vn) { return vd + vn; }
        __cpp_drop(T, vd[idx]);
        if (vn - idx - 1 > 0) {
            memmove(vd + idx, vd + idx + 1,
                    (unsigned long)((vn - idx - 1) * (int)sizeof(T)));
        }
        vn = vn - 1;
        return vd + idx;
    }
    T &operator[](int i) { return vd[i]; }
    T *begin() { return vd; }
    T *end() { return vd + vn; }
    /* Reverse iteration, with the same pointer-as-iterator design: `rbegin`
       is the last element and `rend` is one *before* the first, so the loop
       walks with `--it` and compares against `rend()`. */
    T *rbegin() { return vd + vn - 1; }
    T *rend() { return vd - 1; }
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
    /* The same pointer-as-iterator design `vector` and `map` use: `it->f`,
       `++it` and `it != end()` are then plain C on a plain pointer. */
    T *begin() { return od; }
    T *end() { return od + on; }
    T *rbegin() { return od + on - 1; }
    T *rend() { return od - 1; }
};
"""

_STD_UNIQUE = """
template<typename T>
class unique_ptr {
    T *up;
public:
    unique_ptr() { up = 0; }
    unique_ptr(T *q) { up = q; }
    /* The move is what makes this usable without `release()`. There is
       still no copy constructor, so copying one is refused by the Rule of
       Three exactly as before -- which is what move-only means. */
    unique_ptr(unique_ptr<T> &&o) { up = o.up; o.up = 0; }
    unique_ptr<T> &operator=(unique_ptr<T> &&o) {
        /* Guarded: `a = std::move(a)` would otherwise release the object
           and then adopt the pointer it just freed. */
        if (up != o.up) { reset(o.up); o.up = 0; }
    }
    ~unique_ptr() { reset(0); }
    T *get() { return up; }
    T *operator->() { return up; }
    T &operator*() { return *up; }
    T *release() { T *q = up; up = 0; return q; }
    void reset(T *q) {
        if (up) { __cpp_drop(T, *up); free(up); }
        up = q;
    }
};
"""


#: Not supplied yet -- see CPPRUST.md. The control-block hook below works and
#: `shared_ptr` already calls it, but naming this template still fails to
#: monomorphise and a supplied template that errors when used is worse than
#: an absent one. Kept here because the hook it pairs with is live.
_STD_ENABLE_SHARED = """
/* The object remembers the control block that the first `shared_ptr` gave
   it, so `shared_from_this()` joins that one rather than starting a second
   and freeing the object twice.

   Note the name is not written with angle brackets anywhere in this comment:
   the instantiation scan reads the supplied templates before comments are
   stripped, so a `Name<T>` in prose is indistinguishable from a use. */
template<typename T>
class enable_shared_from_this {
public:
    T *esp;
    long *esc;
    enable_shared_from_this() { esp = 0; esc = 0; }
    shared_ptr<T> shared_from_this() {
        shared_ptr<T> r;
        r.adopt(esp, esc);
        return r;
    }
};
"""


_STD_SHARED = """
template<typename T>
class shared_ptr {
    T *sp;
    long *sc;
public:
    shared_ptr() { sp = 0; sc = 0; }
    shared_ptr(T *q) {
        sp = q;
        sc = (long *)malloc(sizeof(long));
        *sc = 1;
        __cpp_share_hook(T, q, sc);
    }
    /* Join an existing control block rather than starting one. */
    void adopt(T *q, long *c) {
        unshare();
        sp = q;
        sc = c;
        if (sc) { *sc = *sc + 1; }
    }
    shared_ptr(const shared_ptr<T> &o) {
        sp = o.sp;
        sc = o.sc;
        if (sc) { *sc = *sc + 1; }
    }
    shared_ptr<T> &operator=(const shared_ptr<T> &o) {
        if (sc != o.sc) {
            unshare();
            sp = o.sp;
            sc = o.sc;
            if (sc) { *sc = *sc + 1; }
        }
    }
    ~shared_ptr() { unshare(); }
    void unshare() {
        if (sc) {
            *sc = *sc - 1;
            if (*sc == 0) { __cpp_drop(T, *sp); free(sp); free(sc); }
            sp = 0;
            sc = 0;
        }
    }
    T *get() { return sp; }
    T *operator->() { return sp; }
    T &operator*() { return *sp; }
    long use_count() { if (sc) { return *sc; } return 0; }
};
"""


_STD_PAIR = """
template<typename K, typename V>
class pair {
public:
    K first;
    V second;
};
"""


# A `map` whose iterator is a *pointer*. That is the whole design: `it->first`,
# `++it`, `it != m.end()` and `*it` are then plain C on a plain pointer, and
# none of `operator++`, `operator!=` or an iterator class has to exist. It
# costs a linear `find` -- the storage is an unsorted array -- which is the
# honest trade for a container written in a subset with no comparison
# operator to order keys by.
_STD_MAP = """
template<typename K, typename V>
class map {
    pair<K,V> *pd;
    int pn;
    int pcap;
public:
    map() { pd = 0; pn = 0; pcap = 0; }
    ~map() { free(pd); }
    int size() { return pn; }
    int empty() { return pn == 0; }
    void clear() { pn = 0; }
    pair<K,V> *begin() { return pd; }
    pair<K,V> *end() { return pd + pn; }
    /* Integer access, for walking the map in a range-`for`. Deliberately
       not an `operator[]` overload: this map is keyed on `K`, and a second
       subscript taking `int` would be an overload on the parameter *type*
       at one arity, which this subset resolves by argument count and so
       cannot tell apart. A separate name says the same thing unambiguously. */
    pair<K,V> *at_index(int i) { return pd + i; }
    void reserve(int c) {
        if (c > pcap) {
            pair<K,V> *nd;
            nd = (pair<K,V> *)realloc(pd, sizeof(pair<K,V>) * c);
            if (nd) { pd = nd; pcap = c; }
        }
    }
    pair<K,V> *find(__cpp_ref(K) k) {
        int i;
        i = 0;
        while (i < pn) {
            if (__cpp_eq(K, pd[i].first, k)) { return pd + i; }
            i = i + 1;
        }
        return pd + pn;
    }
    int count(__cpp_ref(K) k) { if (find(k) == pd + pn) { return 0; } return 1; }
    V &operator[](__cpp_ref(K) k) {
        pair<K,V> *f;
        f = find(k);
        if (f == pd + pn) {
            if (pn == pcap) { reserve(pcap ? pcap * 2 : 8); }
            __cpp_copy(K, pd[pn].first, k);
            pn = pn + 1;
            f = pd + pn - 1;
        }
        return f->second;
    }
    void erase(__cpp_ref(K) k) {
        pair<K,V> *f;
        int i;
        f = find(k);
        if (f != pd + pn) {
            i = (int)(f - pd);
            while (i < pn - 1) { pd[i] = pd[i + 1]; i = i + 1; }
            pn = pn - 1;
        }
    }
};
"""


_STD_INCLUDE = re.compile(
    r"^[ \t]*#\s*include\s*<(vector|string|memory|map|utility)>[ \t]*\n?",
    re.M)


_STD_CLASSES = frozenset(("string", "vector", "ownvector",
                          "unique_ptr", "shared_ptr", "pair", "map",
                          "enable_shared_from_this"))


#: `<cstdint>` and friends: the C headers under their C++ spellings. The
#: mapping is `c<name>` -> `<name>.h` for every one of them, but it is
#: written out rather than computed so that a header this subset has no
#: story for cannot be silently invented -- `<cmath>` is here because
#: `<math.h>` exists, and `<cstring>` is *not* `<string>`.
_CXX_C_HEADERS = {
    "cstdint": "stdint", "cstring": "string", "cstdlib": "stdlib",
    "cstdio": "stdio", "cstddef": "stddef", "cctype": "ctype",
    "cmath": "math", "cassert": "assert", "climits": "limits",
    "cwchar": "wchar", "cerrno": "errno", "ctime": "time",
    "cstdarg": "stdarg", "cfloat": "float", "clocale": "locale",
    "csignal": "signal", "csetjmp": "setjmp", "cwctype": "wctype",
}

_CXX_C_HEADER = re.compile(
    r"#\s*include\s*<\s*(%s)\s*>" % "|".join(sorted(_CXX_C_HEADERS)))


def _std_prelude(text):
    """Strip `std::`, drop `#include <vector|string>`, and supply the classes.

    Returns the rewritten source. `string` is emitted before `vector` so that
    a `vector<string>` finds it complete -- the same declaration-order rule
    every other nested instantiation obeys.
    """
    wanted = set(m.group(1) for m in _STD_INCLUDE.finditer(text))
    # `<memory>` is the header, `unique_ptr`/`shared_ptr` are the classes:
    # asking for the header alone should not supply a template the file never
    # names, since an unused one would still be monomorphised.
    wanted.discard("memory")
    wanted.discard("utility")
    # `<map>` names the header; `map` is the class. A `map` also needs `pair`,
    # which is its element type.
    if "map" in wanted:
        wanted.discard("map")
    probe = _blank_strings(_strip_comments(text))
    for name in ("string", "vector", "ownvector", "unique_ptr",
                 "shared_ptr", "pair", "map", "enable_shared_from_this"):
        if re.search(r"\bstd\s*::\s*%s\b" % name, probe):
            wanted.add(name)
    # `bool` is a keyword in C++ and a header in C. A `.cpp` writing `bool`
    # has included nothing for it and should not have to. The bundled header
    # is pulled in rather than the type redefined here, which would clash
    # with a file that *does* include it -- and before the early return
    # below, since a file using `bool` need name no container at all.
    bool_prefix = ""
    if re.search(r"(?<![\w])(?:bool|true|false)(?![\w])", probe) \
            and not re.search(r"include\s*[<\"]stdbool\.h", probe):
        bool_prefix = "#include <stdbool.h>\n"
    # C++ spells the C headers without the `.h` and with a leading `c`.
    # They name the same headers, so the spelling is rewritten rather than
    # the include dropped -- the declarations are still wanted. Only this
    # fixed list: `<string>` is `std::string`, a different thing entirely
    # from `<string.h>`, and the rest of the STL is not this pass's to
    # supply.
    text = _CXX_C_HEADER.sub(
        lambda m: "#include <%s.h>" % _CXX_C_HEADERS[m.group(1)], text)
    if not wanted:
        return bool_prefix + text
    text = _STD_INCLUDE.sub("", text)
    text = _sub_code(re.compile(r"\bstd\s*::\s*"), "", text)
    if "vector" in wanted or "ownvector" in wanted:
        # `vector<string>` needs `string`; supplying it is cheaper than
        # working out whether this source asks for that combination.
        wanted.add("string")
    parts = [bool_prefix, _STD_DECLS]
    # Dependency order, not alphabetical or historical. An instantiation used
    # as another's *argument* has to be complete first -- a
    # `vector<shared_ptr<el>>` holds a `shared_ptr_el` by value -- and these
    # are emitted where their template is declared. So the ones that get used
    # as arguments come first: `string` and the smart pointers, then the
    # containers, then `map`, which holds a `pair`.
    if "string" in wanted:
        parts.append(_STD_STRING)
    if "unique_ptr" in wanted:
        parts.append(_STD_UNIQUE)
    # `enable_shared_from_this` needs `shared_ptr`, and comes after it.
    if "enable_shared_from_this" in wanted:
        wanted.add("shared_ptr")
    if "shared_ptr" in wanted:
        parts.append(_STD_SHARED)
    if "enable_shared_from_this" in wanted:
        parts.append(_STD_ENABLE_SHARED)
    if "pair" in wanted or "map" in wanted:
        parts.append(_STD_PAIR)
    if "vector" in wanted:
        parts.append(_STD_VECTOR)
    if "ownvector" in wanted:
        parts.append(_STD_OWNVECTOR)
    if "map" in wanted:
        parts.append(_STD_MAP)
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


def translate(text, path="<cpp>", owning=None, basedir=None,
              incdirs=(), defines=(), clang=None):
    """Translate a C++ subset source to C. Raises CppError on anything else.

    `owning` maps the name of a type this file does *not* define to the
    function that destroys one -- the types Crust lowered that own a buffer,
    handed over so a C++ class holding one by value is destroyed like any
    other member, and refuses to be copied for the same reason.
    """
    # `std::string` / `std::vector` are supplied as ordinary subset source,
    # so everything below sees one file with no special cases in it.
    # Headers first, before anything reads a declaration: a member declared
    # in one and defined here has to arrive in the same translation.
    # An `--incdir` is enough on its own: an angle include is searched only
    # there, so a caller that supplies a path but no basedir still gets its
    # headers spliced.
    if basedir is not None or incdirs:
        text = _expand_headers(text, basedir or ".", incdirs,
                               defines=set(defines or ()))
    # Linkage specifications go now: after the splice, so a header that
    # wrapped its body in `extern "C" { .. }` is unwrapped too, and before
    # every pass below, all of which read declarations that one would still
    # be hiding behind a string literal.
    text = _strip_extern_c(text)
    # `std::move` is read here, before `std::` is stripped, because after
    # that it is indistinguishable from a method or function the project
    # named `move` -- and a layout engine moving a box is not a rarity.
    # Rewritten to `__cpp_move`, the spelling the element builtins already
    # use, so the rest of the pass has one reserved name to look for.
    text = _mark_std_move(text)
    text = _std_prelude(text)
    # After `std::` is stripped, so both spellings are already one, and
    # before anything scans for `new` -- the rewrite introduces one, and the
    # class emitter has to see it to emit the allocator.
    text = _lower_make_ptr(text)
    std_classes = _STD_CLASSES
    # Function templates come out before anything reads the file. Their
    # bodies are not ordinary code -- they name types that exist only once
    # the parameters are known -- so lowering one produces diagnostics
    # about statements in a function the translation unit never calls.
    text, _fscan, _ftmpl = _monomorphise_function_templates(
        text, _strip_comments(text), path)
    # Lambdas are lowered before anything else looks at the file: what comes
    # out is ordinary subset source with a static function in it.
    text = _lower_lambdas(text, path)
    # `auto` becomes a written type before anything reads types, because
    # everything downstream -- the class emitter, the scope tracker, the call
    # rewriter -- reads them by their spelling. Lambdas first: `auto f = []..`
    # is consumed by the lowering above and never reaches this.
    try:
        # Range-`for` first: it emits ordinary declarations, some of them
        # `auto`, which the deduction below then resolves. Layered rather
        # than combined, so each pass has one thing to be right about.
        # `using Y = X;` is C++11 spelling for a typedef, and C has only the
        # typedef -- so it becomes one before anything reads declarations.
        text = cpp_auto.resolve_using_alias(
            text, os.path.basename(path), blank=cpp_auto._blank_like(text))
        # Default arguments become one member per arity, before anything
        # counts arguments -- overloads are resolved by count here.
        text = cpp_auto.resolve_default_arguments(
            text, os.path.basename(path), blank=cpp_auto._blank_like(text))
        # `= default` / `= delete` next: they are declarations, and every
        # pass below reads declarations.
        text = cpp_auto.resolve_defaulted(
            text, os.path.basename(path), blank=cpp_auto._blank_like(text))
        # Namespaces next: they rename the types the passes below read.
        text = cpp_auto.resolve_namespaces(
            text, os.path.basename(path), blank=cpp_auto._blank_like(text))
        # `final` on a class says it may not be derived from, and nothing
        # here derives from anything it is not told about. Stripped before
        # the class scans rather than inside each of them.
        text = _sub_code(
            re.compile(r"(?<![\w])(class|struct)\s+(\w+)\s+final(?![\w])"),
            lambda mm: "%s %s" % (mm.group(1), mm.group(2)), text)
        # Nested classes after namespaces (so the enclosing name is already
        # flattened) and before everything that reads a class.
        text = cpp_auto.resolve_nested_classes(
            text, os.path.basename(path), blank=cpp_auto._blank_like(text))
        # Type aliases after namespaces, because a namespace-scope one is at
        # depth zero only once the braces are gone -- and before everything
        # below, which reads types by their spelling.
        text = cpp_auto.resolve_aliases(
            text, os.path.basename(path), blank=cpp_auto._blank_like(text))
        text = cpp_auto.resolve_range_for(
            text, os.path.basename(path), blank=cpp_auto._blank_like(text))
        # The clang fallback is consulted only where the textual pass
        # cannot read a type, and only if clang is installed. It answers
        # from the *original* file, so it is gathered before anything has
        # been spliced or flattened -- but lazily, so a translation that
        # needs no help never spawns a compiler.
        # `clang` is None for "use it if it is there", True to require
        # it, False to forbid it. A build that wants the same answer on
        # every machine pins it: with the fallback available, a `.cpp`
        # whose types are not written still translates, and on a machine
        # without clang the same file does not.
        fallback = {}
        del cpp_auto.CLANG_USED[:]
        if clang is not False and os.path.isfile(path):
            if clang is True and not cpp_auto.clang_available():
                raise CppError(
                    "--clang was given but `clang++` cannot be run. The "
                    "fallback answers `auto` where no written spelling "
                    "can; without it those declarations are reported.")
            if cpp_auto.clang_available():
                fallback = cpp_auto.clang_auto_types(path, incdirs, defines)
        text = cpp_auto.resolve(
            text, os.path.basename(path), blank=cpp_auto._blank_like(text),
            fallback=fallback)
        # After `auto`, which deduces *from* a cast's written type, and
        # before anything that reads an expression -- a surviving
        # `static_cast<T>(e)` reads as a comparison to everything below.
        text = cpp_auto.resolve_casts(
            text, os.path.basename(path), blank=cpp_auto._blank_like(text))
    except cpp_auto.AutoError as e:
        raise CppError(e.message)
    scan = _blank_literal_braces(_strip_comments(text))
    _check_unsupported(scan, path)

    # Out-of-line member definitions come out first, keyed by class. They
    # have to be in hand before any class is emitted, and lifting them also
    # keeps the class scan below from seeing a definition where it expects a
    # declaration.
    cls_names = set(re.findall(r"\b(?:class|struct)\s+(\w+)", scan))
    text, scan, outline = _extract_out_of_line(text, scan, cls_names)

    classes = _find_classes(scan, text)
    # Unconditionally, even with nothing to attach: this is also where a
    # member declared and never defined is caught, and a file with no
    # out-of-line definitions at all is exactly the case where that happens.
    for _s, _e, _c in classes:
        _attach_out_of_line(_c, outline, path)

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

    # Out-of-line bodies were lifted out of `scan` above, taking their
    # template uses with them. `shared_ptr<element>` written only inside one
    # of those would then never be recorded, and asking for it later -- once
    # the body is attached to its class -- reports an instantiation the scan
    # "cannot discover". Appending them for the recording pass alone puts
    # them back where they were read from, without touching the real text.
    _outline_uses = "\n".join(
        "%s %s %s" % (d.get("ret") or "", d.get("params") or "",
                      d.get("body") or "")
        for d in outline.values())
    _monomorphise_uses(_blank_spans(scan, bodies) + "\n" + _outline_uses,
                       tnames, record)

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
    # Types from the other side of the boundary seed the table, so a class
    # emitted below sees them exactly as it sees one declared above it.
    cinfo = dict((n, _external_info(n, fn))
                 for n, fn in sorted((owning or {}).items())
                 if n not in declared)
    prev = 0
    fwd, fwd_protos, outline_bodies = [], [], []
    # A class this translation only ever *declares* -- `class element;` with
    # the definition in a header nobody here included. C++ allows that
    # wherever the type is used through a pointer, which is exactly what a
    # `shared_ptr<element>` does, so litehtml leans on it heavily; the
    # instantiation is then emitted over a name C has never heard of.
    #
    # The declaration lowers the same way a definition's does, minus the
    # body: a struct tag and the typedef that lets the rest of the output
    # spell it without `struct`. Which class is *complete* where is
    # untouched by this -- a by-value member of one still needs a
    # definition, and still says so.
    defined = set(c.name for (_s, _e, c) in classes)
    fwd_only = []
    for m in re.finditer(r"(?<![\w])(?:class|struct)\s+(\w+)\s*;", scan):
        name = m.group(1)
        if name in defined or name in fwd_only:
            continue
        fwd_only.append(name)
    for name in fwd_only:
        fwd.append("struct %s;" % name)
        fwd.append("typedef struct %s %s;" % (name, name))
    if fwd_only:
        # The C++ spelling is dropped where it stood: `class X;` is not C,
        # and the lowered pair has already been hoisted above everything
        # that could name it. Blanked to the same length rather than cut
        # out -- the class spans found above are offsets into this text,
        # and shifting it under them moves every one of them.
        pat = re.compile(r"(?<![\w])class\s+(%s)\s*;"
                         % "|".join(re.escape(n) for n in fwd_only))
        text = pat.sub(lambda m: " " * len(m.group(0)), text)
        scan = pat.sub(lambda m: " " * len(m.group(0)), scan)
    # Where each class sits, so an instantiation can be held back until the
    # classes it is built over are complete.
    at = dict((c.name, k) for k, (_s, _e, c) in enumerate(classes))
    # Each instantiation's slot: the class index it must be emitted after.
    # Two things can push it down, and the second is why this is a fixpoint
    # rather than one pass. A template argument may name a *class*, which is
    # `at`; it may also name another *instantiation*, which has a slot of its
    # own that may itself have been pushed down. `vector<unique_ptr<Thing>>`
    # is exactly that chain: `unique_ptr<Thing>` waits for `Thing`, a user
    # class declared below both templates, so `vector<unique_ptr_Thing>` has
    # to wait for it too. Reading only `at` missed the middle step, and the
    # vector was emitted while its element was still an unknown name -- which
    # cost it the knowledge that the element cannot be copied.
    slot, insts_all = {}, []
    for idx, (_s, _e, cls) in enumerate(classes):
        for targs in (wanted.get(cls.name, []) if cls.tparams else []):
            if not targs:
                continue
            nm = _mono_name(cls.name, targs)
            slot[nm] = idx
            insts_all.append((idx, cls, targs, nm))
    for _ in range(len(insts_all) + 2):
        changed = False
        for idx, cls, targs, nm in insts_all:
            need = slot[nm]
            for a in targs:
                need = max(need, at.get(_base_name(a), -1))
                if a in slot:
                    need = max(need, slot[a])
            # Unless the class it depends on *derives* from it. That is the
            # CRTP shape -- `class node : public enable_shared_from_this<node>`
            # -- and there the base has to come first. It can: such a base
            # holds a `T *`, never a `T`, so it needs no complete type.
            if need > idx and need < len(classes) and \
                    _derives_from(classes[need][2], cls.name, targs):
                need = idx
            if need > slot[nm]:
                slot[nm] = need
                changed = True
        if not changed:
            break
    deferred = {}
    for idx, cls, targs, nm in insts_all:
        if slot[nm] > idx:
            # `vector<floated_box>` copies and destroys its elements, so its
            # body needs `floated_box` *complete* -- and the supplied
            # containers are emitted above the user's classes by
            # construction. Held back to just after the class it needs.
            deferred.setdefault(slot[nm], []).append((cls, targs))

    def emit_one(cls, targs):
        (names_, protos, defs, tails), cname, info = _emit_class(
            cls, names, cinfo, tsub, targs, new_used.get(cls.name),
            chained, cls.name in std_classes)
        # Trailing newline: two instantiations of the same template are
        # emitted back to back, and without it the last line of one runs
        # into the first line of the next.
        pieces.append("\n".join(defs) + "\n")
        fwd.extend(names_)
        fwd_protos.extend(protos)
        outline_bodies.extend(tails)
        cinfo[cname] = info

    for idx, (start, end, cls) in enumerate(classes):
        # Keep everything before the class, minus any `template<..>` header,
        # which has no C equivalent.
        head = text[prev:start]
        head = _TEMPLATE.sub("", head)
        pieces.append(head)
        insts = wanted.get(cls.name, []) if cls.tparams else [None]
        for targs in insts:
            # Compared on the *template* as well as the arguments: two
            # templates instantiated over the same class share `targs`, and
            # matching on those alone held back an instantiation that was
            # never deferred.
            if targs and any(c.name == cls.name and t == targs
                             for ps in deferred.values() for c, t in ps):
                continue                 # emitted after what it is built on
            emit_one(cls, targs)
        prev = end
        for dcls, dtargs in deferred.get(idx, []):
            emit_one(dcls, dtargs)
    pieces.append(text[prev:])
    # Bodies defined out of line go after everything, not at the class: the
    # author wrote them below whatever file-scope names they read, and a
    # header spliced in at the top would otherwise put them above.
    if outline_bodies:
        pieces.append("\n" + "\n".join(outline_bodies) + "\n")
    # Every class name declared up front, before any definition. A template
    # instantiated over a class defined *later* emits its struct where the
    # template sits, and the field type was then an unknown name:
    # `struct Box_Thing { Thing * bp; };` ahead of `struct Thing;`. Which
    # class is complete where still matters -- a by-value member needs a
    # definition, not a declaration -- so this only hoists the names.
    # Every class *name* first, then every prototype, then the definitions
    # where they were. A prototype can mention a class declared below it --
    # `unique_ptr_Thing_new_1(unique_ptr_Thing *, Thing *)` -- so the two
    # groups cannot be interleaved per class.
    # Enum definitions, hoisted whole and given the typedef that lets the
    # rest of the output name them without the `enum` keyword. C++ spells
    # an enum type bare, so the prototypes below do too, and C has no way
    # to forward-declare one -- the definition itself has to come first.
    enums = []
    def _lift_enum(m):
        body_end = _match_brace(out0, m.start("brace"))
        return m.group(0)
    enum_re = re.compile(r"(?<![\w])enum\s+(\w+)\s*(?P<brace>\{)")
    out0 = "".join(pieces)
    lifted, last, k = [], 0, 0
    while True:
        m = enum_re.search(out0, k)
        if m is None:
            lifted.append(out0[last:])
            break
        close = _match_brace(out0, m.start("brace"))
        if close is None:
            k = m.end()
            continue
        end = close + 1
        while end < len(out0) and out0[end] in " \t":
            end += 1
        # Only a plain `enum X { .. };`. A `typedef enum X { .. } X;`
        # already carries its own typedef and must be moved whole or not
        # at all -- quickjs writes them that way, and lifting just the
        # `enum X { .. }` out of one leaves a stray `typedef` and a
        # dangling name behind.
        if end >= len(out0) or out0[end] != ";" \
                or _prev_word(out0, m.start()) == "typedef":
            k = end
            continue
        end += 1
        name = m.group(1)
        enums.append("%s\ntypedef enum %s %s;" % (out0[m.start():end],
                                                 name, name))
        lifted.append(out0[last:m.start()])
        lifted.append(" " * (end - m.start()))
        last = end
        k = end
    if enums:
        pieces = ["".join(lifted)]
    if fwd or fwd_protos or enums:
        head = "\n".join(enums + fwd + fwd_protos) + "\n"
        # These are hoisted above everything, including any `#include
        # <stdbool.h>` the source or its headers already had -- litehtml
        # has one, which is why nothing was added earlier. A prototype
        # returning `bool` then names a type C has not been told about
        # yet, several hundred lines before the include that would.
        # stdbool.h is idempotent, so the safe answer is to carry one
        # along with the block that needs it.
        if re.search(r"(?<![\w])bool(?![\w])", head):
            head = "#include <stdbool.h>\n" + head
        pieces.insert(0, head)
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
    # `vector<T>` copy-constructs and destroys its elements, which is what
    # `std::vector` does, so an owning element type needs no steering. It used
    # to store by assignment and refer the author to `ownvector`; the element
    # builtins accept scalars now, so one implementation is right for both and
    # the split has gone.
    # And the other way. `ownvector` copy-constructs and destroys each
    # element, which a scalar has neither of; steering that to `vector` used
    # to fall out of `__cpp_copy` refusing scalars, but the builtins have to
    # accept them now -- `map<int, ..>` copies a scalar key -- so the
    # guidance is stated where it belongs instead of inferred from a
    # mechanism that no longer implies it.
    for targs in wanted.get("ownvector", []):
        elem = targs[0]
        if elem not in cinfo:
            raise CppError(
                "%s: `ownvector<%s>` copy-constructs and destroys each "
                "element, and %s has neither. Use `vector<%s>`, which stores "
                "by assignment."
                % (os.path.basename(path), elem, elem, elem))

    # After reference lowering, a class still spelled by value really is by
    # value -- a `T &` the author wrote is a `T *` by now.
    # Against the directive-blanked text: a `#define`'s replacement is
    # not an expression this translation unit evaluates.
    byval = _check_by_value(_blank_directives(out), cinfo, path)
    out = _rewrite_scopes(out, cinfo)

    # Rewriting a call copies its arguments through verbatim, so a receiver
    # nested in an argument list surfaces on the next pass. Iterate to a
    # fixed point rather than recursing into every argument.
    for _ in range(8):
        nxt = _rewrite_calls(out, cinfo, free_refs)
        if nxt == out:
            break
        out = nxt

    # After the rewrites, not before: `Buf c(a);` is a copy *construction*
    # until `_rewrite_scopes` turns it into `Buf c; Buf_copy(&c, &a);`, and
    # reading it earlier cannot tell it from a call handing `a` away.
    _check_owning_args(_blank_directives(out), cinfo, path)

    # After the call rewriting, so the calls are in their lowered form. A
    # by-value owning parameter is destroyed by the callee, so its argument
    # has to be constructed here rather than handed over as a struct copy.
    out = _construct_byval_args(out, byval, cinfo, path)

    # After `_rewrite_scopes`, which is what consumes a `std::move` in the
    # statement positions the subset lowers. Anything still spelled here is
    # expression position, and would otherwise reach the C front end as a
    # call to a function nothing declares.
    _check_stray_moves(_blank_directives(out), path)

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

def _parse_owning(spec):
    """`Name:dropfn,Name2:dropfn2` -> a mapping.

    Passed on the command line rather than discovered, because this module
    runs as a subprocess and cannot see the unit Crust is translating. The
    protocol stays one file and one exit status; this is only how the caller
    says which foreign types own something.
    """
    out = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise CppError("--owning wants `Name:dropfn`, got %r" % part)
        name, fn = part.split(":", 1)
        name, fn = name.strip(), fn.strip()
        if not name or not fn:
            raise CppError("--owning wants `Name:dropfn`, got %r" % part)
        out[name] = fn
    return out


def main(argv):
    args = list(argv)
    out_path = None
    owning = {}
    basedir = None
    incdirs = []
    clang = None
    if "--clang" in args:
        clang = True
        args.remove("--clang")
    if "--no-clang" in args:
        clang = False
        args.remove("--no-clang")
    defines = []
    while "-D" in args:
        i = args.index("-D")
        if i + 1 >= len(args):
            sys.stderr.write("cpprust: -D needs a name\n")
            return 2
        defines.append(args[i + 1].split("=")[0])
        del args[i:i + 2]
    while "--incdir" in args:
        i = args.index("--incdir")
        if i + 1 >= len(args):
            sys.stderr.write("cpprust: --incdir needs a directory\n")
            return 2
        incdirs.append(args[i + 1])
        del args[i:i + 2]
    if "--basedir" in args:
        i = args.index("--basedir")
        if i + 1 >= len(args):
            sys.stderr.write("cpprust: --basedir needs a directory\n")
            return 2
        basedir = args[i + 1]
        del args[i:i + 2]
    if "--owning" in args:
        i = args.index("--owning")
        if i + 1 >= len(args):
            sys.stderr.write("cpprust: --owning needs a spec\n")
            return 2
        try:
            owning = _parse_owning(args[i + 1])
        except CppError as e:
            sys.stderr.write("cpprust: %s\n" % e.message)
            return 2
        del args[i:i + 2]
    if "-o" in args:
        i = args.index("-o")
        if i + 1 >= len(args):
            sys.stderr.write("cpprust: -o needs a path\n")
            return 2
        out_path = args[i + 1]
        del args[i:i + 2]
    if len(args) != 1 or out_path is None:
        sys.stderr.write("usage: cpprust.py <source.cpp> -o <out.c> "
                         "[--owning Name:dropfn,..] [--basedir DIR] "
                         "[--incdir DIR].. [-D NAME].. "
                         "[--clang|--no-clang]\n")
        return 2

    src = args[0]
    try:
        with open(src) as f:
            text = f.read()
    except IOError as e:
        sys.stderr.write("cpprust: cannot read %s: %s\n" % (src, e))
        return 2

    try:
        if basedir is None:
            basedir = os.path.dirname(os.path.abspath(src))
        result = translate(text, path=src, owning=owning,
                           basedir=basedir, incdirs=incdirs,
                           defines=defines, clang=clang)
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
    # On stderr, so the protocol stays one file and one exit status. A
    # caller that wants to know how much of a translation leans on clang
    # reads this; nothing depends on it.
    if cpp_auto.CLANG_USED:
        sys.stderr.write(
            "cpprust: clang answered %d `auto` declaration%s: %s\n"
            % (len(cpp_auto.CLANG_USED),
               "" if len(cpp_auto.CLANG_USED) == 1 else "s",
               ", ".join("%s: %s" % nt for nt in cpp_auto.CLANG_USED)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
