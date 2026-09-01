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
  * the binary arithmetic operators `+ - * / % | & ^`, lowered to
    `T__binadd` and so on, in the one case that has an honest lowering: a
    class that owns nothing. The operator hands back a new object *by
    value*, and a by-value return of an owning class is not in this subset
    -- the local is destroyed on the way out and the caller would receive a
    copy of a released object -- so an owning class is reported and
    pointed at `operator+=`, which writes into an object that already
    exists. Both operands must be plain names that resolve to the class;
    one that is itself an expression would need a temporary to take the
    address of, so `a + b + c` is not in this. `operator*` is told apart
    from the dereference by whether it takes an operand, which is the only
    difference between them on the page. The body may build its result the
    obvious way (`vec2 r(x + o.x, y + o.y); return r;`) -- a constructor
    call inside a method body used to reach the C unlowered whenever the
    method returned a class, which made that shape look unsupported.
  * `operator=`, lowered to `T__assign`.
    Assignment to an owning object has no safe default -- a struct copy
    leaves two owners -- so this is where the author supplies one. A chained
    `a = b = c` is refused, because the call is lowered to `void`.
  * a small `std`: `string`, `vector<T>`, `map<K,V>` and `set<T>`, supplied
    when the source names them and written in this subset rather than
    special-cased in the lowering. `set<T>` keeps its elements *sorted*,
    where `map` does not: ordering two values generically needs
    `__cpp_cmp(T, a, b)`, a three-way comparison -- two `<`s for a scalar,
    a `compare` method for a class -- and `map` predates it. Three-way
    rather than a `less` predicate because the builtin's operands are not
    symmetric: the right one arrives as an already-lowered pointer and the
    left as an lvalue, so `b < a` cannot be had by swapping the arguments
    of `a < b`. A boolean would therefore leave a container unable to
    derive equality from ordering and force it to demand `equals` too,
    where one comparison answers both. A class element with no `compare`
    is reported rather than ordered by address.
  * `priority_queue<T>`, a max-heap in an array, ordered by `__cpp_cmp`
    like `set` and `map`. Elements are sifted *by hole* -- the one being
    placed is held aside and the ones it passes are relocated with
    `memmove` into the gap -- so an owning element never has two live
    copies and needs no `operator=`. `top()` returns a `T *` rather than
    the reference `std::priority_queue` returns, since a reference return
    is not in this subset; `q[0]` reaches the same element as an lvalue.
  * `stack<T>`, `queue<T>`, `array<T,N>` and `optional<T>`, each with one
    thing it cannot do the way `std` does. `stack::top` and `queue::front`
    return `T *`, since a reference return is not in this subset, and
    `s[0]` is the same element as an lvalue -- `stack` indexes from the
    top so those two agree. `queue` keeps a head index and slides its live
    range back down when the array fills, rather than wrapping: a wrapped
    range cannot be handed out as the pointer pair every container here
    iterates as. `array<T,N>` holds a plain array *member*, which this
    subset neither constructs nor destroys, so it takes plain data only and
    an element with a constructor or destructor is refused with `vector<T>`
    named -- without that check it segfaulted on the first `fill`.
    `optional<T>` keeps its value behind a pointer rather than in a `T`
    member, because a member is constructed and destroyed with its
    container and an empty optional must hold nothing; that costs an
    allocation per engaged value, which is the price of having no
    placement new.
  * a small `<algorithm>`: `lower_bound`, `upper_bound`, `binary_search`,
    `sort`, `find`, `count`, `reverse`, `fill`, `min_element`,
    `max_element`, `swap` and `copy`, as free function templates over a
    `T *` range. `swap` takes *pointers* where `std::swap`
    takes references, because a `T &` parameter is lowered only for a class
    and `__cpp_ref(T)` gives a scalar by value -- neither spells both, and
    a swap cannot have a copy. `fill` and `copy` write into a range that
    already holds constructed elements, destroying each before
    constructing over it -- so for an element type that owns something the
    destination has to be visibly a container's own range (`begin()`,
    `ptr()`, or one local aliasing one). Anything this cannot see through
    is reported: handed raw storage it would destroy garbage and follow
    bytes nothing set, which is a segfault rather than a diagnostic. A
    plain-data element has nothing to destroy, so any destination is fine
    and the check never fires. The
    searching pair ask `__cpp_eq` rather than `__cpp_cmp`, since matching
    does not need an order and demanding one would refuse a class that
    reasonably has equality and no ordering. A call to one of these can be
    the receiver of the next (`min_element(..)->size()`), because the
    return types of the monomorphised copies are known -- unlike a call to
    plain C spelled the same way, which is still left exactly as written.
  * `string` searches a substring as `find_str`, not as an overload of
    `find`. `std::string` overloads that name on `char` and `const char *`,
    which are the same arity, and this subset resolves an overload by
    argument *count* before types are known -- so the two cannot be told
    apart and a separate name says which is meant. `rfind_str`,
    `contains`, `starts_with` and `ends_with` come with it.
  * `<numeric>`: `accumulate`, `iota`, `inner_product`, `partial_sum` and
    `adjacent_difference`, over the same `T *` ranges. These combine
    elements with `+` and `*`, so the element has to be a scalar -- a class
    would need `operator+`, which is not in this subset, and is reported
    against the call rather than left to fail inside a template body the
    author never wrote. `partial_sum` and `adjacent_difference` read each
    element before writing, so the destination may be the source.
    `accumulate` answers to its own name as well as to the header, since it
    lived in `<algorithm>` here before this header existed.
  * `unordered_map` and `unordered_set`, which are `map` and `set` under
    another name. Nothing here hashes and nothing in this subset can write
    `hash<T>` generically, so a separate copy would have the unordered
    interface and the ordered behaviour; the alias says so. Iteration comes
    out sorted, which code relying on no order is not broken by, and
    lookups are O(log n) rather than O(1), which it cannot observe. A range is a
    pair of pointers because that is already what every container here
    hands out, so these work over `vector`, `ownvector`, `set` and `map`
    without an iterator abstraction existing. Ordering goes through
    `__cpp_cmp`, so a class element supplies `compare` and these come with
    it; there is no comparator parameter, which would need a function type
    this subset cannot spell generically. `sort` relocates elements with
    `memmove` rather than assigning them, so an owning element keeps its
    one owner and needs no `operator=`; it is an insertion sort, because a
    recursive one would need the template to call itself over its own
    parameter and the instantiation scan cannot see through that.

    `sort(v.begin(), v.end())` works without spelling `<int>`, but only in
    one narrow shape: a parameter written `T *`, matched against an
    argument whose pointee this file declares -- a container's `begin()`,
    an array, or a pointer local. A deduced call is rewritten to spell its
    arguments the long way and the ordinary substitution runs on that, so
    both forms take one code path. Only the scopes still open at the call
    are read -- a brace region that closed above it is skipped -- so
    another function's locals, or an earlier block's, cannot answer for a
    name they merely share. `map` is left out on purpose: its
    iterator is a `pair<K,V> *`, so deducing `K` from `m.begin()` would be
    wrong rather than merely unsupported. Anything else -- a call result,
    a by-value parameter, more than one template parameter -- is reported,
    as is a template called with no arguments at all, which used to have
    its body blanked and its call left to fail at link time. They also need a class somewhere in the
    unit, since the builtins they compare through are expanded while
    lowering classes; a file using only `<algorithm>` is told so. `std::` is stripped; there is no namespace support and
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
left exactly as written. A method returning a class *by value* continues the chain through a
generated `Cls__byval_meth_<n>` taking its receiver by value: C cannot take
the address of a function result, and spilling one would need a statement,
so the value goes in as a value. That is the same way out the binary
operators take for `a + b + c`, and it rests on the same condition -- the
class must own nothing, since a struct copy of an owning receiver would
leave two objects holding one resource. An owning one still ends the chain
with a diagnostic, as does a virtual call, which needs a receiver whose
address can be taken to reach the vtable; each says which of the two it
is. The variants are emitted only for the names a source actually chains
onto.

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
virtual inheritance, exceptions, most operator overloading (the arithmetic
binaries, the comparisons, `[]`, `=`, `->`, `*` and the compound
assignments are in; a conversion operator, `<<` and the rest are not). Multiple
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


#: Anchors that tie a position in the generated text back to a line of the
#: author's file. A line number is only useful if it names a line they can
#: open, and by the time anything is reported the text bears little
#: relation to what was written: a few hundred lines of supplied `std` sit
#: on top, and every class has been replaced by generated C that does not
#: have the same number of lines the class was written on.
#:
#: So the text carries its own anchors. Each says "the line after me is
#: source line N", and a line number is that N plus the newlines between.
#: One goes above the author's first line, and another after every class
#: emitted, which is what makes the count survive emission -- an anchor
#: costs nothing to re-place and does not care how many lines the emitter
#: added or removed.
#:
#: Spelled as a declaration rather than a comment because `_strip_comments`
#: blanks comments out of the scan that most positions are found in, and an
#: anchor that vanishes from the text being searched is no anchor at all.
_SRC_MARK = "__crust_src_line_"
_SRC_MARK_RE = re.compile(r"__crust_src_line_(\d+)__")


def _src_mark(line):
    """The anchor declaring that the next line is source line `line`."""
    return "typedef int %s%d__;\n" % (_SRC_MARK, line)


#: The origin anchor, above the author's first line.
_SRC_MARK_DECL = _src_mark(1)


def _locate(exc, text, pos, path):
    """Re-raise `exc` with a `file:line:` prefix, if it has none.

    A prefix rather than a rewrite of every message: the two passes that
    report most of this module's diagnostics -- call rewriting and scope
    rewriting -- walk the text with an index, so the position of whatever
    they were looking at is known where the error escapes, even though it
    is not known where each individual message is written. One wrapper
    locates all thirty.

    This was written once before and removed: it reported a copy on line 10
    as line 8, because the anchors did not yet survive class emission, and
    a number that looks right and is wrong is worse than none. They do now.

    Messages that already carry a location keep it: those were written
    against a more specific position than "wherever the scan had reached",
    and it would be wrong to overwrite a class's declaration line with the
    line of the use that tripped over it.
    """
    msg = exc.args[0] if exc.args else str(exc)
    if re.match(r"^[^\s:]+:\d+:", msg):
        return exc
    return CppError("%s:%d: %s"
                    % (os.path.basename(path), _src_line(text, pos), msg))


def _after_origin(text, pos):
    """Index just past the last anchor above `pos`, or 0.

    Where the author's own text begins, as far as anything before `pos` is
    concerned. Used to keep a declaration search out of the supplied
    prelude; the anchors after each class serve here too, since everything
    between them is the author's.
    """
    best = None
    for mm in _SRC_MARK_RE.finditer(text, 0, pos):
        best = mm
    return best.end() if best is not None else 0


def _src_line(text, pos):
    """The author's 1-based line number for `pos`, or the raw one.

    Counted from the nearest anchor above it. Positions above every anchor
    are inside the supplied prelude -- a diagnostic about `vector`'s own
    body, which is this module's bug rather than the author's. Those keep
    counting from the top, since there is no better answer and pretending
    otherwise would point at an unrelated line of their file.
    """
    best = None
    for mm in _SRC_MARK_RE.finditer(text, 0, pos):
        best = mm
    if best is None:
        return text.count("\n", 0, pos) + 1
    return int(best.group(1)) + text.count("\n", best.start(), pos) - 1


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

#: C's precedence among the binary arithmetic operators. Used only to
#: decide whether a *run* of them may be chained left to right: `a + b - c`
#: may, `a + b * c` may not, because the multiply binds tighter and
#: chaining would compute `(a + b) * c`. Rather than build an expression
#: tree for a subset that has no other use for one, a mixed run is
#: reported and the author writes the parentheses.
_BIN_PREC = {"*": 5, "/": 5, "%": 5, "+": 4, "-": 4,
             "&": 3, "^": 2, "|": 1}

#: `a + b` becomes `T__binadd(&a, &b)`. Distinct from `__aug*`, which is
#: the compound assignment `a += b`: that one is a statement whose result
#: is dropped, this one is an expression whose result is the point.
_BIN_NAMES = {"+": "add", "-": "sub", "*": "mul", "/": "div", "%": "mod",
              "|": "or", "&": "and", "^": "xor"}

_AUG_ASSIGN_SPELLINGS = frozenset(
    ["%s=" % k for k in ("+", "-", "*", "/", "%", "|", "&", "^")])

_UNSUPPORTED = ("throw", "catch",
                "dynamic_cast", "typeid")

#: The checked-error state, emitted once per translation unit when the
#: `try`/`except` lowering is used. Deliberately minimal: a flag and one
#: machine word of payload. This is minipy's `st.exc_flag`/`st.exc_val`
#: with C spelling, and it is the whole mechanism -- an error is a value,
#: propagation is a checked return, and there is no unwinder anywhere.
#: A single static per unit; threading is out of scope and said so rather
#: than pretended at.
_EXC_PRELUDE = """\
/* Checked error state. Set by `raise`, tested after every call to a
   function declared `except`, cleared on entry to a handler. No unwinder,
   no unwind tables, no allocation: the error path is the ordinary return
   path with a flag set, which is why destructors still run on it. */
static struct { int flag; long val; } _cpp_exc;
"""

_FALLIBLE_SIG = re.compile(
    r"([A-Za-z_][\w \*]*?)\b([A-Za-z_]\w*)\s*\(([^()]*)\)\s*except\b")

_RAISE_RE = re.compile(r"(?<![\w.>])raise\b")
_TRY_RE = re.compile(r"(?<![\w.>])try\b")


def _except_zero(ret):
    """The dummy a poisoned return carries. Nobody reads it -- the caller
    tests the flag before the value -- but C wants one of the right shape."""
    ret = ret.strip()
    if ret == "void":
        return None
    if ret.endswith("*"):
        return "0"
    if ret in ("float", "double"):
        return "0.0"
    return "0"


def _exc_check(handlers):
    """What a statement that may have failed is followed by: jump to the
    innermost `except`, or -- with no `try` around it but a fallible
    enclosing function -- a poisoned return, which propagates. The two
    spellings are the same decision minipy's block stack makes: nearest
    handler, else hand the flag to the caller."""
    kind, arg = handlers[-1]
    if kind == "try":
        return " if (_cpp_exc.flag) goto %s;" % arg
    zr = arg
    if zr is None:
        return " if (_cpp_exc.flag) return;"
    return " if (_cpp_exc.flag) { return %s; }" % zr


def _lower_except(text, path="<cpp>"):
    """Lower `except` functions, `raise`, and `try`/`except` to the checked
    model, before anything else reads the source.

    Runs this early on purpose: `raise E;` becomes a flag-set and an
    *ordinary return*, and the return lowering that runs later already
    emits every destructor a return needs (`{ int _cpp_ret0 = ..;
    Buf_drop(&b); return _cpp_ret0; }`). Being upstream of that pass is
    what makes the error path run destructors without this pass knowing
    what a destructor is. The same holds for the propagation checks.

    The one thing a `goto` into a handler would skip is the scope-end
    destructor of a class local *declared inside the try block*, so that
    is refused (declare it before the `try`). Everything else is plain
    statement rewriting.
    """
    look = _strip_comments(text)
    if "except" not in look and _RAISE_RE.search(look) is None \
            and _TRY_RE.search(look) is None:
        # Nothing of ours in the file. (`try` is in the test on purpose:
        # it left the generic unsupported-keyword list when this pass took
        # ownership of it, so a `try` this pass ignores would otherwise
        # reach the C compiler verbatim -- caught by exactly that
        # happening to `try { } return 0;`.)
        return text, False

    # -- fallible signatures: collect, then strip the keyword ------------
    fallible = {}
    for m in _FALLIBLE_SIG.finditer(look):
        fallible[m.group(2)] = _except_zero(m.group(1))
    # A constructor that can fail has no return channel to poison and
    # leaves a partially-built object behind -- the exact situation the
    # standards that ban throwing constructors are naming. The checked
    # model's answer is a factory: `static T make(..) except`. Enforced
    # here rather than documented, because the refusal *is* the design.
    for cn in re.findall(r"(?<![\w.>])class\s+([A-Za-z_]\w*)", look):
        cm = re.search(r"(?<![\w.>])~?%s\s*\([^()]*\)\s*except\b"
                       % re.escape(cn), look)
        if cm:
            raise CppError(
                "%s:%d: a %s cannot be declared `except`. It has no "
                "return value to poison, and a failure would leave a "
                "partially-built object with no one owning it. Make a "
                "factory instead: `static %s make(..) except` that "
                "constructs only after the fallible part succeeded."
                % (os.path.basename(path), _src_line(look, cm.start()),
                   "destructor" if look[cm.start()] == "~"
                   else "constructor", cn))
    if not fallible and _TRY_RE.search(look) is None \
            and _RAISE_RE.search(look) is None:
        return text, False
    text = re.sub(r"(\))\s*except\b(\s*[;{])", r"\1\2", text)
    look = _strip_comments(text)

    # Calls are recognized by *name*, whether spelled `f(..)`, `x.f(..)`
    # or `p->f(..)`. Coarse on purpose: if any class marks `take` as
    # `except`, every `take` call is checked. A spurious check on an
    # unrelated same-named method costs one never-taken branch (every
    # fallible call clears or handles the flag in the same statement, so
    # it cannot be left set). What the coarseness *buys* is override
    # safety for free -- a call through a base reference is checked even
    # when only the derived override can fail, which is the classic
    # escape hatch in signature-based schemes.
    call_re = (re.compile(r"(?<!\w)(%s)\s*\("
                          % "|".join(re.escape(n) for n in sorted(fallible)))
               if fallible else None)
    labels = [0]

    # Class names declared in this translation, for the try-local check.
    classes = set(re.findall(r"(?<![\w.>])class\s+([A-Za-z_]\w*)", look))

    def stmt_needs_check(seg, at=None):
        if call_re is None:
            return False
        sl = _strip_comments(seg)
        for cm in call_re.finditer(sl):
            # The definition site of the function itself is not a call.
            before = sl[:cm.start()].rstrip()
            if before.endswith(("int", "long", "void", "double", "float",
                                "char", "*")):
                continue
            if at is not None \
                    and before.count("(") > before.count(")"):
                # Inside another call's argument list. The flag check
                # this pass appends runs after the statement -- too late
                # once the enclosing call has consumed the poisoned
                # value: `printf("%d", c())` prints garbage first and
                # jumps to the handler second. A hazard the model cannot
                # check is a refusal, not a quiet reordering.
                raise CppError(
                    "%s:%d: a call to `%s` (declared `except`) is an "
                    "argument to another call. The failure check runs "
                    "after the statement, which is too late once the "
                    "enclosing call has used the value -- bind it to a "
                    "local first: `T v = %s(..);` then use `v`."
                    % (os.path.basename(path), at, cm.group(1),
                       cm.group(1)))
            return True
        return False

    def walk(body, handlers, off=0, depth=0):
        """Rewrite one block body. `handlers` is the context stack,
        innermost last: ('try', label) or ('fn', zero-or-None) or
        ('none', None) at the bottom, which is where a fallible call with
        nowhere to go becomes a diagnostic.

        `off` is the body's start in the whole file, so a diagnostic's
        line number is counted in the file the author is looking at and
        not in the slice this recursion happens to hold. (The first
        version reported `exc3.cpp:1` for a call on line 2.)"""
        out = []
        i, n = 0, len(body)
        seg_start = 0
        # Hoisted: stripping comments per loop step made the walk
        # quadratic in the block size. Positions align with `body`, so
        # one strip serves every probe below.
        lm = _strip_comments(body)

        def line(at):
            return _src_line(look, off + at)

        def flush(upto, insert_after=None):
            out.append(body[seg_start:upto])
            if insert_after:
                out.append(insert_after)

        while i < n:
            tm = _TRY_RE.match(lm, i)
            rm = _RAISE_RE.match(lm, i)
            if tm:
                ob = body.find("{", tm.end())
                if ob < 0:
                    raise CppError("%s: `try` without a block"
                                   % os.path.basename(path))
                cb = _match_brace(lm, ob)
                if cb is None:
                    raise CppError("%s: `try` block never closes"
                                   % os.path.basename(path))
                after = lm[cb + 1:]
                em = re.match(r"\s*except\b\s*(\(([^()]*)\))?\s*\{",
                              after)
                if em is None:
                    raise CppError(
                        "%s:%d: `try` without an `except` after it. This "
                        "subset's error handling is the checked model -- "
                        "`try { .. } except (long e) { .. }` -- and a "
                        "`try` with no handler has nothing to check into."
                        % (os.path.basename(path), line(i)))
                hob = cb + 1 + em.end() - 1
                hcb = _match_brace(lm, hob)
                if hcb is None:
                    raise CppError("%s: `except` block never closes"
                                   % os.path.basename(path))
                tbody = body[ob + 1:cb]
                hbody = body[hob + 1:hcb]
                # Class locals declared inside the try: the handler is
                # reached by `goto`, which leaves the block without
                # passing its closing brace -- where the scope-end
                # destructor call will later be placed. A destructor the
                # error path silently skips is a leak dressed as
                # handling, so it is refused with the fix in hand.
                tl = _strip_comments(tbody)
                for cn in classes:
                    if re.search(r"(?<![\w.>])%s\s+[A-Za-z_]\w*\s*[(;=]"
                                 % re.escape(cn), tl):
                        raise CppError(
                            "%s:%d: `%s` is declared inside a `try` "
                            "block. The handler is reached by a jump that "
                            "leaves the block early, so a destructor at "
                            "the block's end would be skipped on exactly "
                            "the path it matters most. Declare it before "
                            "the `try`."
                            % (os.path.basename(path),
                               line(i), cn))
                labels[0] += 1
                lab = labels[0]
                hl, dl = "_cpp_h_%d" % lab, "_cpp_done_%d" % lab
                new_t = walk(tbody, handlers + [("try", hl)], off + ob + 1,
                             depth + 1)
                new_h = walk(hbody, handlers, off + hob + 1, depth + 1)
                binder = ""
                if em.group(2) and em.group(2).strip():
                    bd = em.group(2).strip()
                    bm = re.match(r"(?:long|int)\s+([A-Za-z_]\w*)$", bd)
                    if bm is None:
                        raise CppError(
                            "%s:%d: `except (%s)` -- the payload is one "
                            "machine word, so the binding is `long e` (or "
                            "`int e`). Richer payloads are a later step, "
                            "not a missing cast."
                            % (os.path.basename(path),
                               line(i), bd))
                    binder = "long %s = _cpp_exc.val; " % bm.group(1)
                flush(i)
                out.append(
                    "{ %s goto %s; %s: { %s_cpp_exc.flag = 0; %s } %s: ; }"
                    % (new_t, dl, hl, binder, new_h, dl))
                i = hcb + 1
                seg_start = i
                continue
            if rm:
                se = lm.find(";", rm.end())
                if se < 0:
                    raise CppError("%s: `raise` without a `;`"
                                   % os.path.basename(path))
                payload = body[rm.end():se].strip()
                # A raise goes to the *innermost* handler -- the same
                # dispatch a failed call takes. Inside a `try`, that try
                # catches it; in a bare `except` function, it returns
                # poisoned and the caller's check picks it up; a re-raise
                # in a handler body goes to the next handler out, because
                # the handler is walked under the outer context. (The
                # first version always returned: a raise inside a try
                # escaped the function, and a legitimate re-raise in
                # `main` was refused with its outer try standing right
                # there.)
                kind, arg = handlers[-1]
                if kind == "none":
                    raise CppError(
                        "%s:%d: `raise` with nowhere for the error to "
                        "go: no `try` around it, and the enclosing "
                        "function is not declared `except`. Fallibility "
                        "is part of the signature here, so either wrap "
                        "this in `try { .. } except (long e) { .. }` or "
                        "add `except` after the parameter list."
                        % (os.path.basename(path), line(i)))
                if not payload:
                    payload = "_cpp_exc.val"      # re-raise, value kept
                if kind == "try":
                    disp = "goto %s;" % arg
                elif arg is None:
                    disp = "return;"
                else:
                    disp = "return %s;" % arg
                flush(i)
                out.append("{ _cpp_exc.flag = 1; _cpp_exc.val = "
                           "(long)(%s); %s }" % (payload, disp))
                i = se + 1
                seg_start = i
                continue
            c = lm[i]
            if c == ";":
                seg = body[seg_start:i + 1]
                if stmt_needs_check(seg, line(i)):
                    kind, _a = handlers[-1]
                    if kind == "none":
                        raise CppError(
                            "%s:%d: this statement calls a function "
                            "declared `except`, and there is nothing to "
                            "handle a failure: no `try` around it, and "
                            "the enclosing function is not `except` "
                            "itself. An unhandled error is a compile "
                            "error here, not a terminate() later -- wrap "
                            "the call in `try { .. } except (long e) "
                            "{ .. }`, or mark this function `except` to "
                            "pass the error up."
                            % (os.path.basename(path), line(i)))
                    flush(i + 1, _exc_check(handlers))
                    seg_start = i + 1
                i += 1
                continue
            if c in "{}":
                flush(i + 1)
                seg_start = i + 1
                i += 1
                continue
            i += 1
        flush(n)
        return "".join(out)

    # -- per function and method body -----------------------------------
    out, pos = [], 0
    fn_re = re.compile(r"([A-Za-z_][\w \*]*?)\b([A-Za-z_]\w*)\s*"
                       r"\(([^()]*)\)\s*(?:const\s*)?\{")
    #: Statement keywords fn_re would otherwise mistake for a definition
    #: once bodies at depth > 0 are in play: `while (i < 5) {` has the
    #: name-parens-brace shape too.
    _STMT_KW = {"if", "while", "for", "switch", "return", "except",
                "sizeof"}

    def _in_class(at):
        """Whether position `at` sits directly inside a class body -- one
        brace deep, and the brace *that opened this scope* was a class's.
        Method definitions live exactly there; anything deeper is inside
        a body this walker already owns.

        The opener is kept on a stack, not as last-brace-seen: after a
        constructor's `{ ... }` closes, the last brace seen is the
        constructor's, but the scope position `at` is in belongs to the
        class. The first version checked the constructor's brace, decided
        `take()` was not in a class, and every method after the first
        braced member fell through to the leftover refusal."""
        stack = []
        for j, ch in enumerate(look[:at]):
            if ch == "{":
                stack.append(j)
            elif ch == "}" and stack:
                stack.pop()
        if len(stack) != 1:
            return False
        opener = stack[0]
        head = look[max(0, opener - 200):opener]
        return re.search(r"(?<![\w.>])(?:class|struct)\s+[A-Za-z_]\w*"
                         r"[^;{]*$", head) is not None
    while True:
        m = None
        for cand in fn_re.finditer(look, pos):
            if cand.group(2) in _STMT_KW:
                continue
            d = _brace_depth(look, cand.start())
            if d == 0 or (d == 1 and _in_class(cand.start())):
                m = cand
                break
        if m is None:
            break
        ob = m.end() - 1
        cb = _match_brace(look, ob)
        if cb is None:
            break
        body = text[ob + 1:cb]
        name = m.group(2)
        if name in fallible:
            base = [("fn", fallible[name])]
        else:
            base = [("none", None)]
        lb = _strip_comments(body)
        if _TRY_RE.search(lb) or _RAISE_RE.search(lb) \
                or stmt_needs_check(lb + ";"):
            body = walk(body, base, ob + 1)
        out.append(text[pos:ob + 1])
        out.append(body)
        out.append(text[cb:cb + 1])
        pos = cb + 1
    out.append(text[pos:])
    new = "".join(out)
    # A `try`/`raise` still standing can only be inside a constructor or
    # destructor body -- ordinary methods and free functions were walked
    # above. Same position as the `except`-on-a-constructor refusal, and
    # the same fix.
    leftover = _strip_comments(new)
    lm = _TRY_RE.search(leftover) or _RAISE_RE.search(leftover)
    if lm:
        raise CppError(
            "%s:%d: `raise`/`try` inside a constructor or destructor "
            "body. A constructor has no return value to poison and a "
            "failure would leave a partially-built object; a destructor "
            "that can fail has nowhere to report to. Do the fallible "
            "work in a `static T make(..) except` factory (or an "
            "ordinary `except` method) and construct from its result."
            % (os.path.basename(path), _src_line(leftover, lm.start())))
    return new, True


def _brace_depth(look, pos):
    d = 0
    for ch in look[:pos]:
        if ch == "{":
            d += 1
        elif ch == "}":
            d -= 1
    return d


#: Refused only when `--rtti` is off. With it on, these two lower against
#: the descriptor below; the rest of `_UNSUPPORTED` stays refused either way.
_RTTI_KEYWORDS = ("dynamic_cast", "typeid")

#: The type descriptor, emitted only under `--rtti`.
#:
#: Layout-identical to py2c's `TypeInfoHdr` (shivyc_rt.h), deliberately and
#: field for field. That is what lets an object built by either side be
#: asked its type by the other: py2c's `isinstance_of` walks `{name; base}`
#: at a fixed offset and does not care which language wrote the descriptor.
#:
#: The three function-pointer slots py2c fills (`tostr`, `eq`, `addfn`) are
#: spelled `const void *` here and left null. They are a pointer wide either
#: way, so the layouts agree, and spelling them concretely would drag `obj`
#: and `Obj` into a translation unit that may have no rpython in it at all.
#: A `.cpp` therefore gets RTTI with nothing to link.
#:
#: `objsize` is `sizeof` the concrete struct. py2c uses it for a shallow
#: copy; nothing here reads it yet, and it is emitted because leaving a hole
#: in a shared layout is how the two sides drift apart.
_RTTI_PRELUDE = """\
/* Type descriptor. Layout-identical to py2c's `TypeInfoHdr`, so an object
   from either language can be asked its type by the other. */
typedef struct _CppTypeInfo {
    const char *name;
    const struct _CppTypeInfo *base;
    const void *fields;                 /* FieldDesc *; null from C++ */
    const void *tostr;                  /* obj (*)(Obj *); null from C++ */
    const void *eq;                     /* bool (*)(Obj *, obj); null */
    const void *addfn;                  /* obj (*)(Obj *, obj); null */
    unsigned long objsize;
} _CppTypeInfo;

/* The base-chain walk, spelled exactly as `isinstance_of` spells it. Bounded
   by the depth of the hierarchy, which is fixed at compile time -- there are
   no virtual bases in this subset, so this is a walk and never a search. */
static inline int _cpp_isinstance(const void *o, const void *t) {
    const _CppTypeInfo *want = (const _CppTypeInfo *)t;
    const _CppTypeInfo *k = o ? *(const _CppTypeInfo *const *)o : 0;
    for (; k; k = k->base) if (k == want) return 1;
    return 0;
}

/* `dynamic_cast<D *>(p)`: the pointer form, which yields null on failure.
   The reference form throws, and this subset has no exceptions, so it stays
   refused rather than being given a silent null. */
static inline void *_cpp_dyncast(void *o, const void *t) {
    return _cpp_isinstance(o, t) ? o : 0;
}
"""

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


_COMMENT_OPEN = re.compile(r"//|/\*|[\"']")
_NOT_NEWLINE = re.compile(r"[^\n]")


def _strip_comments(text):
    """Blank comments, preserving newlines so line numbers hold.

    The scan jumps between openers instead of walking every character:
    ordinary code is the overwhelming majority of a translation unit and
    none of it needs inspecting, only copying.
    """
    n = len(text)
    out, last, pos = [], 0, 0
    while True:
        m = _COMMENT_OPEN.search(text, pos)
        if m is None:
            break
        i = m.start()
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(text[last:i])
            out.append(" " * (j - i))
            last = pos = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(text[last:i])
            out.append(_NOT_NEWLINE.sub(" ", text[i:j]))
            last = pos = j
        else:                              # a literal, which is kept as is
            q = text[i]
            j = i + 1
            while j < n and text[j] != q:
                j += 2 if text[j] == "\\" else 1
            pos = min(j + 1, n)
        if pos <= i:
            pos = i + 1
    out.append(text[last:])
    return "".join(out)


_PROBE_START = re.compile(r"\*|(?<!\w)\w")


def _probe_positions(*texts):
    """Where the character walkers need to try their patterns at all.

    Both `_rewrite_scopes` and `_rewrite_calls` walk the file one character
    at a time and try a dozen compiled patterns at every position. Every one
    of those patterns can only begin at a `*` or at the first character of a
    word -- their lookbehinds all reject a word character immediately
    before -- and in a real source file the great majority of positions are
    neither. Marking the candidates in one pass per text turns twelve regex
    attempts per character into one byte lookup, which was the largest cost
    left in both passes once the quadratic scans were gone.

    More than one text is taken because a walker decides from the
    comment-blanked copy but occasionally reads the original; marking both
    keeps the filter a superset of what either could match.
    """
    hits = bytearray(len(texts[0]))
    for t in texts:
        for m in _PROBE_START.finditer(t):
            hits[m.start()] = 1
    return hits


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


_QUOTE = re.compile(r"[\"']")


def _blank_strings(text):
    """Blank string and char literal bodies, preserving length and newlines.

    Only the literals are looked at; the code between them is copied in
    whole slices rather than a character at a time.
    """
    n = len(text)
    out, last, pos = [], 0, 0
    while True:
        m = _QUOTE.search(text, pos)
        if m is None:
            break
        i = m.start()
        c = text[i]
        j = i + 1
        while j < n and text[j] != c:
            j += 2 if text[j] == "\\" else 1
        j = min(j + 1, n)
        out.append(text[last:i])
        out.append(c)
        out.append(_NOT_NEWLINE.sub(" ", text[i + 1:j - 1]))
        out.append(c if j - 1 < n else "")
        last = pos = j
    out.append(text[last:])
    return "".join(out)


_STD_FUNCTION = re.compile(r"\bfunction\s*<")


def _skip_angles(text, open_idx):
    """Index just past the `>` matching the `<` at `open_idx`, or None."""
    depth = 0
    for k in range(open_idx, len(text)):
        if text[k] == "<":
            depth += 1
        elif text[k] == ">":
            depth -= 1
            if depth == 0:
                return k + 1
    return None


def _check_std_function(scan, path):
    """`std::function<..>` is not one of the supplied templates.

    Reported by name rather than left to fall through. An unknown template
    is passed to the C front end untouched on purpose -- it may be declared
    somewhere this pass cannot see -- but `function` is a name the author
    has every reason to expect works, and letting it through emits
    `function<void(const string &)>` into C, which means nothing there.

    It is also not a gap that closes by adding a template. A `function`
    holds a *callable*, and the callable this subset can build is a
    capturing lambda, which is inlined at its call sites and so has no
    value to store -- the same wall the lambda diagnostic names. So the
    replacement is a plain function pointer, with any captured state passed
    beside it.
    """
    for m in _STD_FUNCTION.finditer(scan):
        # A `function` taken *by reference* is a borrow, not a store: it
        # lowers to the pointer the caller already has, and the callable it
        # refers to was built somewhere this declaration does not have to
        # name. litehtml's `html.h` passes two of them to `split_text`, in a
        # header every file includes -- so refusing the borrow refused
        # forty-three files over a shape that is not the problem.
        end = _skip_angles(scan, m.end() - 1)
        if end is not None and scan[end:end + 2].lstrip().startswith("&"):
            continue
        raise CppError(
            "%s:%d: `std::function` is not in the C++ subset: it stores a "
            "callable, and a capturing lambda is inlined at its call sites, "
            "so there is nothing to store. Use a function pointer "
            "(`void (*f)(const string *)`), passing any captured state as a "
            "parameter beside it."
            % (os.path.basename(path), _src_line(scan, m.start())))


def _check_free_overloads(scan, path):
    """Two *free* functions with one name is not something C can hold.

    A method overload is resolved by argument count and refused when two
    share one; a free function has no such machinery at all -- both lower
    to the same symbol with conflicting types, and the C front end is the
    first thing to notice. It has now happened three times in coost
    (`align_up`, `co::alloc`, `milo::dtoa`), each time silently.

    Only definitions at brace depth zero. Depth, not a scan for class
    bodies: a constructor is spelled exactly like a free function and the
    span-based version read every second one as a redefinition of the
    first. A template's body is skipped too -- its instantiations are
    named apart later.
    """
    # Regions the conditional evaluator could not decide. A definition
    # inside one has an alternative in the other branch and only ever one
    # is live -- coost's `murmur_hash` is written twice under `#if
    # __arch64 / #else`, and reading both as a redefinition is wrong.
    # Skipped rather than resolved: whichever branch survives is a
    # question for the C preprocessor, not for this check.
    cond = []
    cdepth, at = 0, 0
    for line in scan.split("\n"):
        stripped = line.lstrip()
        if re.match(r"#\s*if", stripped):
            if cdepth == 0:
                start_at = at
            cdepth += 1
        elif re.match(r"#\s*endif", stripped):
            cdepth -= 1
            if cdepth == 0:
                cond.append((start_at, at + len(line)))
        at += len(line) + 1

    depth, i, n = 0, 0, len(scan)
    tops = []
    while i < n:
        c = scan[i]
        if c == "{":
            if depth == 0:
                tops.append(i)
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    if not tops:
        return
    seen = {}
    for b in tops:
        head = scan[max(0, b - 400):b]
        m = re.search(r"([\w:]+)\s*\**\s*(\w+)\s*\(([^;{}()]*)\)\s*"
                       r"(?:const\s*)?(?:noexcept\s*)?$", head)
        if not m:
            continue
        name = m.group(2)
        if name in ("if", "while", "for", "switch", "catch", "main"):
            continue
        if any(a <= b <= e for a, e in cond):
            continue
        # A class body, a namespace, a template -- none is a free function.
        lead = head[:m.start()].rstrip()
        if re.search(r"(?<![\w])(?:class|struct|union|enum|namespace|"
                      r"template|extern)\b[^;{}]*$", lead):
            continue
        if "::" in m.group(1) or "::" in name:
            continue
        prev = seen.get(name)
        if prev is None:
            seen[name] = m.start(2)
            continue
        raise CppError(
            "%s:%d: `%s` is defined twice as a free function. C has no "
            "overloading for them, so both lower to one symbol and the "
            "second redefines the first. Give them different names."
            % (os.path.basename(path), _src_line(scan, b), name))


def _check_unsupported(scan, path, rtti=False):
    _check_std_function(_blank_strings(scan), path)
    # A literal is data, not code: `puts("new item")` uses no keyword.
    scan = _blank_strings(scan)
    for m in _OPERATOR.finditer(scan):
        if m.group(1) in ("=", "[]", "[", "->", "*"):
            continue
        if m.group(1) in _AUG_ASSIGN_SPELLINGS:
            continue
        if m.group(1) in _CMP_NAMES:
            continue
        if m.group(1) in _BIN_NAMES:
            continue
        line = _src_line(scan, m.start())
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
        if rtti and kw in _RTTI_KEYWORDS:
            continue          # lowered against the descriptor instead
        m = re.search(r"\b%s\b" % kw, scan)
        if m:
            line = _src_line(scan, m.start())
            extra = ""
            if kw in ("throw", "catch"):
                extra = (" Error handling here is the checked model, "
                         "spelled `try { .. } except (long e) { .. }` "
                         "with `raise` -- an error is a value and "
                         "propagation is a checked return, so there is "
                         "no unwinder for `%s` to reach." % kw)
            if kw in _RTTI_KEYWORDS:
                extra = (" It needs a type descriptor on every polymorphic "
                         "class, which is off by default because it costs a "
                         "static descriptor per class; pass `--rtti` to turn "
                         "it on.")
            raise CppError(
                "%s:%d: `%s` is not in the C++ subset. Supported: classes, "
                "constructors, destructors, and templates.%s"
                % (os.path.basename(path), line, kw, extra))


class Member(object):
    __slots__ = ("kind", "ret", "name", "params", "body", "line", "dim",
                 "init", "virt", "pure", "outline", "definit",
                 "declared_only", "stat", "contracts")

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
        # ShivyCX contract clauses written between the parameter list and
        # the body. Carried through the method-to-free-function rewrite
        # unchanged: the pointer parameters keep their names, and `this` is
        # prepended rather than renaming anything.
        self.contracts = []


def _split_contracts(tail):
    """`(["assert not len(o) % 4"], rest)` -- ShivyCX contract clauses.

    A contract sits between the parameter list and the body, which is
    exactly where a constructor's initializer list sits, so the tail parser
    met them first and reported an unparsable member. They already work on
    a *free* function -- this file does not read one, so the clauses passed
    through to ShivyCX untouched -- and stopped at the class boundary,
    which is where a numeric library lives.

    Only the leading run is taken. Anything after it is the ordinary tail
    (`const`, `override`, a `: a(1)` list) and is left for its own parser.
    """
    tail = (tail or "").strip()
    if not re.match(r"^assert\b", tail):
        return [], tail
    parts = re.split(r"(?=\bassert\b)", tail)
    out, rest = [], ""
    for idx, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue
        if not part.startswith("assert"):
            rest = " ".join(p for p in parts[idx:]).strip()
            break
        # A clause runs to the next `assert`, so a trailing `const` or an
        # initializer list is glued to the last one. Split it back off at
        # the first thing that cannot be part of a contract expression.
        cut = re.search(r"\s+(?=(?:const|override|final|noexcept)\b|:)",
                        part)
        if cut is not None:
            out.append(part[:cut.start()].strip())
            rest = part[cut.start():].strip()
            break
        out.append(part)
    return out, rest


def _contract_names(clause):
    """The identifiers a contract clause constrains, i.e. every `len(x)`."""
    return set(re.findall(r"\blen\s*\(\s*(\w+)\s*\)", clause))


class Class(object):
    __slots__ = ("name", "tparams", "members", "line", "base", "extra_bases")

    def __init__(self, name, tparams, members, line, base=None,
                 extra_bases=()):
        self.name = name
        self.tparams = tparams    # tuple of template parameter names, or ()
        self.base = base          # layout base class name, or None
        # Bases after the first. Each contributes a vptr of its own at a
        # fixed offset rather than a struct prefix, so it must carry no
        # data -- checked in `_emit_class`, where the base's members are
        # in hand.
        self.extra_bases = list(extra_bases)
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
    op = _decl_paren(body)
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


_OPERATOR_SYMS = re.compile(r"\boperator\s*(?:[-+*/%^&|~!=<>]+|\[\s*\]|\(\s*\))")


def _decl_paren(decl):
    """Index of the `(` that opens a member's parameter list, or -1.

    Not `decl.find("(")`. A parameter type may itself be spelled with
    parens -- `std::function<void(const std::string &)>` is the shape that
    matters here -- and the first `(` in

        map<int, pair<string, function<void(const string &)>>> subs

    belongs to the *type*, not to the member. Taking it made this field a
    method returning `map<int, pair<string, function<void`, which then
    failed against its own trailing `>>> subs` rather than against
    anything the author wrote.

    Angle brackets nest here for the same reason `_split_declarators`
    lets them: this runs over a declaration, where `<` opens a template
    argument list rather than being a comparison. The one place that is
    not true is an operator's own name -- `operator<`, `operator<<`,
    `operator<=` -- so those spellings are stepped over before the count
    begins.

    An unbalanced `<` means the guess was wrong somewhere above; falling
    back to the plain scan keeps such a member parsing exactly as it did.
    """
    skip = [(m.start(), m.end()) for m in _OPERATOR_SYMS.finditer(decl)]
    angle = depth = 0
    i, n = 0, len(decl)
    while i < n:
        for a, b in skip:
            if a <= i < b:
                i = b
                break
        else:
            c = decl[i]
            if c == "(" and angle == 0 and depth == 0:
                return i
            elif c == "<":
                angle += 1
            elif c == ">" and angle > 0:
                angle -= 1
            elif c == "[":
                depth += 1
            elif c == "]" and depth > 0:
                depth -= 1
            i += 1
    if angle:
        return decl.find("(")
    return -1


def _drop_trailing_const(head):
    """Remove the qualifiers that follow a member function's parameters.

    `const` says what the body may do; `override` and `final` say what the
    class hierarchy may do. All three are checked by the language rather than
    lowered, and the C front end checks the body regardless -- so what is
    left after the parameter list is dropped.
    """
    op = _decl_paren(head)
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
    op = _decl_paren(decl)
    if op < 0:
        return False
    if decl[op + 1:].lstrip().startswith("*"):
        return False
    return bool(re.match(r"^[~\w][\w:<>,&*\s]*$", decl[:op].strip() or "~"))


def _split_members(body, cname, line0, path="<cpp>"):
    """Parse a class body into fields, methods, a constructor and destructor.

    `line0` is the line the class was declared on, so a member's line is
    that plus the newlines above it in the body -- which is how the parse
    failures below name a line the author can open. This pass runs before
    class emission, where the correspondence between the text and the
    source is still exact.
    """
    def _at(idx):
        return "%s:%d: " % (os.path.basename(path),
                            line0 + body.count("\n", 0, idx))

    body = _ACCESS.sub("", body)
    members = []
    #: `static const` data members, as (type, name, initialiser). Carried
    #: on the member list rather than returned separately so every existing
    #: caller keeps working.
    statics = []
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
                # `static const int cap = 64;` -- a class *constant*, not a
                # field. C has no static data member, and treating it as
                # one put `static const int cap;` inside the struct (which
                # is not C) and moved the initialiser into the constructor,
                # so every instance re-assigned a constant and every use
                # read it through `this`. Emitted at file scope instead,
                # named `Class_name`, with uses inside the class rewritten
                # to that. coost's vendored `dtoa_milo.h` declares seven.
                sm = re.match(r"^\s*static\s+(?:constexpr\s+)?"
                              r"(?:const\s+)?(.+)$", decl)
                if sm is not None:
                    eq0 = _top_level_eq(decl)
                    if eq0 >= 0:
                        cinit = decl[eq0 + 1:].strip()
                        cdecl = sm.group(1).strip()
                        cdecl = cdecl[:_top_level_eq(cdecl)].strip() \
                            if _top_level_eq(cdecl) >= 0 else cdecl
                        cparts = cdecl.replace("*", " * ").split()
                        if len(cparts) >= 2:
                            statics.append((" ".join(cparts[:-1]),
                                            cparts[-1], cinit))
                            continue
                definit = None
                eq = _top_level_eq(decl)
                if eq >= 0:
                    definit = decl[eq + 1:].strip()
                    decl = decl[:eq].strip()
                # `int x, y;` -- one declaration, several declarators, which
                # is ordinary C++ and was read as a single field named `y`
                # of type `int x,`. The type is whatever precedes the first
                # declarator, and each name after a comma repeats it. `x`
                # was then not a field at all, so a method body using it
                # emitted a bare `x` that named nothing.
                #
                # Split at top level only: a comma inside `<>` belongs to a
                # template argument list, and `map<int, int> m;` is one
                # field, not two.
                decls = [d.strip() for d in _split_declarators(decl)
                         if d.strip()]
                head0, dim0 = _split_array_dim(decls[0])
                first = head0.replace("*", " * ").split()
                if len(first) < 2:
                    raise CppError("%scannot parse member %r in class %s"
                                   % (_at(start), decl, cname))
                # The base type is what precedes the *first* declarator,
                # with its stars removed: in `int *p, q;` the `*` belongs to
                # `p`, and C says `q` is a plain `int`. Carrying the star
                # into the base made `q` a pointer, so a body adding it to
                # an int silently did pointer arithmetic.
                base = " ".join(t for t in first[:-1] if t != "*")
                for idx, one in enumerate(decls):
                    if idx == 0:
                        parts, dim = first, dim0
                    else:
                        # A later declarator carries only its own name, and
                        # its own stars: `int *p, q;` makes `p` a pointer
                        # and `q` an int, exactly as C says.
                        headn, dim = _split_array_dim(one)
                        parts = [base] + headn.replace("*", " * ").split()
                        if len(parts) < 2:
                            raise CppError(
                                "%scannot parse member %r in class %s"
                                % (_at(start), decl, cname))
                    # `int arr[10];` -- the declarator suffix is not part of
                    # the name. Keeping it there would make field
                    # qualification miss every use of `arr` in a method body.
                    # It was split off above rather than here, so a bound
                    # containing a `*` survives tokenising intact.
                    fname = parts[-1]
                    fm = Member("field", " ".join(parts[:-1]), fname,
                                None, None, line0, dim)
                    # An initialiser belongs to the declarator it was
                    # written on, which is the last one here.
                    fm.definit = definit if idx == len(decls) - 1 else None
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
                raise CppError("%sunterminated method body in class %s"
                           % (_at(start), cname))
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
        # `constexpr` asks for compile-time evaluation where the arguments
        # allow it; the lowering emits an ordinary function either way, and
        # the C front end is free to fold it. Dropped here, beside the
        # other specifiers, because a *constructor* is recognised by its
        # signature being exactly the class name -- so `constexpr
        # fastring()` was not recognised as one at all, and coost's
        # `fast::stream` appeared to have no default constructor for its
        # derived classes to call.
        head = re.sub(r"(?<![\w])constexpr(?![\w])\s*", "", head)
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
        op = _decl_paren(head)
        if op < 0:
            raise CppError("%scannot parse member %r in class %s"
                           % (_at(start), head, cname))
        # Match the opening paren rather than taking the last `)`: a ctor
        # initializer list puts more parens after the parameter list.
        cp = _match_paren(head, op)
        if cp is None:
            raise CppError("%scannot parse member %r in class %s"
                           % (_at(start), head, cname))
        params = head[op + 1:cp].strip()
        sig = head[:op].strip()
        virt = bool(re.match(r"virtual\b", sig))
        if virt:
            sig = sig[len("virtual"):].strip()
        # Contract clauses sit exactly where a constructor's initializer
        # list does, so they have to come off before that parser runs.
        contracts, after = _split_contracts(head[cp + 1:])
        init = _parse_init_list(after, sig, cname)
        _before = len(members)
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
        elif re.search(r"\boperator\s*\*$", sig) and not params.strip():
            # `T &operator*()`. The *dereference*, told apart from the
            # binary multiply below by taking no operand -- which is the
            # only difference between them on the page.
            # Lowered like `operator[]`: the reference
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
        elif re.search(r"\boperator\s*(\+|-|/|%|\||&|\^)$", sig) or \
                (re.search(r"\boperator\s*\*$", sig) and params.strip()):
            # A binary arithmetic operator. Like a comparison and unlike a
            # compound assignment, its *result* is the point, so the
            # declared return type is kept.
            #
            # `operator*` is the awkward one: spelled the same as the
            # dereference above, and told apart by whether it takes an
            # operand. A dereference takes none. That test is made here
            # rather than in the pattern because the pattern cannot see the
            # parameter list.
            bm = re.search(r"\boperator\s*(\+|-|\*|/|%|\||&|\^)$", sig)
            bits = sig[:sig.index("operator")].strip()
            members.append(Member("binop", bits, "operator%s" % bm.group(1),
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
                raise CppError("%scannot parse method %r in class %s"
                               % (_at(start), head, cname))
            _m = Member("method", " ".join(bits[:-1]), bits[-1],
                        params, inner, line0, "", None, virt)
            _m.stat = is_static
            members.append(_m)
        # Whichever branch above produced it, the member carries the
        # clauses that preceded its body.
        for _new in members[_before:]:
            _new.contracts = list(contracts)
    for _sty, _snm, _sini in statics:
        _sm = Member("sconst", _sty, _snm, None, None, line0)
        _sm.definit = _sini
        members.append(_sm)
    return members


def _parse_base(clause, cname):
    """`: public B, public I` -> `("B", ["I"])`.

    The first base is the *layout* base: it is laid out as the first
    member, so a pointer to a derived object already is a pointer to it and
    upcasting stays a cast. Every base after the first is a secondary base,
    which reaches its object through a vptr of its own at a fixed offset --
    see `_emit_class`. Which one is which is decided here, by writing
    order, exactly as C++ decides it.

    A `virtual` base is refused rather than ordered, because the property
    that makes the rest of this work -- that a base sits at an offset
    known at compile time -- is the one it gives up.
    """
    clause = (clause or "").strip()
    if not clause.startswith(":"):
        return None, []
    bases = [b.strip() for b in _split_top(clause[1:]) if b.strip()]
    names = []
    for b in bases:
        parts = [p for p in b.split()
                 if p not in ("public", "private", "protected")]
        if "virtual" in parts:
            raise CppError(
                "class %s: `virtual` inheritance is not in the C++ subset. A "
                "virtual base's offset depends on the most-derived type, so "
                "every access to one of its members becomes a runtime table "
                "lookup and `dynamic_cast` an unbounded search -- which is "
                "the cost this subset exists to not have. Use a base with no "
                "data members (any number of those may be inherited), or "
                "hold a reference to the shared object."
                % cname)
        if len(parts) != 1:
            raise CppError("class %s: cannot parse base clause %r"
                           % (cname, clause))
        names.append(parts[0])
    return names[0], names[1:]


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
                        if taken:
                            # An earlier branch was taken and has already
                            # been emitted, so the rest of the chain is
                            # dead. Dropping it is sound and keeps the
                            # directives balanced.
                            pass
                        else:
                            # No branch taken yet, and the opening `#if`
                            # was consumed when it evaluated false -- so
                            # emitting from the `#elif` leaves it with no
                            # `#if` to belong to. coost's `dtoa_milo.h`
                            # ends `#if defined(_MSC_VER) / #elif __GNUC__
                            # .. / #else / #endif` that way, and the C
                            # front end reported three orphaned
                            # directives. The `#elif` becomes the opening
                            # `#if`, which is exactly what the remaining
                            # chain means once the earlier branches are
                            # known false.
                            rest = list(lines[i:j + 1])
                            rest[0] = re.sub(r"#(\s*)elif", r"#\1if",
                                             rest[0], count=1)
                            out.extend(rest)
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
        # An rpython module is not spliced. It is not C++, and reading it as
        # C++ finds a `class` keyword and then fails inside a body that is
        # Python. The preprocessor answers this include by transpiling the
        # module and splicing the *C*; what this pass needs from it is the
        # declarations, which arrive separately as `--decls`. So the line is
        # left exactly where it is, for preproc to handle downstream.
        if name.endswith(".py"):
            continue
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


#: Containers supplied by this module whose `begin()`/`end()` yield a
#: pointer to their *first* template argument. `map` is absent on purpose:
#: its iterator is a `pair<K,V> *`, not a `K *`, so deducing `K` from
#: `m.begin()` would be wrong rather than merely unsupported.
_ELEM_CONTAINERS = ("vector", "ownvector", "set")


#: Words that can stand where a type would in the declaration patterns
#: below, and are not one. `return x;` reads as a declaration of `x` with
#: type `return` to a regex that only knows "word word".
_DECL_NOISE = frozenset(("return", "sizeof", "case", "else", "typedef",
                         "struct", "union", "enum", "const"))


def _last_before(pat, scan, at, lo=0):
    """The last match of `pat` starting in `scan[lo:at]`, or None.

    Declarations are read out of the file text, and `re.search` returns the
    *first* match in it -- which is the wrong one whenever a name is
    declared more than once. The supplied templates are prepended above the
    author's code and have ordinary local names in them, so `T *lo` inside
    `reverse` was found for a call whose `lo` was the author's `string *`,
    and the call deduced a type literally named `T`.

    Searching backwards from the call is both the fix and the more correct
    rule generally: the declaration that governs a name is the nearest one
    above its use.

    Backwards is still not enough on its own, though, which is what `lo`
    is for. `swap` declares a parameter `T *a`, and it sits above the
    author's code, so for a call whose `a` was `int a[4]` the *nearest*
    preceding declaration was still the template's. The caller bounds the
    search at the end of the supplied prelude, so a name the author never
    wrote can never answer for one they did.
    """
    found = None
    for mm in pat.finditer(scan, lo, at):
        found = mm
    return found


def _visible_view(scan, lo, at):
    """`scan` with every scope that has already *closed* blanked out.

    What a declaration search may see. Searching backwards for the nearest
    declaration is right within one scope, but a file has many: a local in
    an unrelated function above the call is nearer than the global the call
    actually means, and a local in an `if` block earlier in the same
    function is nearer still. Both would answer, and answering with the
    wrong type is worse than declining -- it deduced `sort_string` for an
    `int *`.

    The rule is one line of C++: a brace region that opened and closed
    before the call is out of scope; one still open at the call encloses
    it. So every complete `{...}` above the call is blanked and the rest
    left alone, which leaves exactly the enclosing function's own text, the
    blocks around the call, and file scope. Blanked rather than cut so the
    offsets still line up with `scan`.

    Order within a scope is still the backwards search's job -- this says
    which text is eligible, not which match wins.
    """
    buf = list(scan)
    stack = []
    for mm in re.finditer(r"[{}]", scan[lo:at]):
        pos = lo + mm.start()
        if mm.group(0) == "{":
            stack.append(pos)
        elif stack:
            open_at = stack.pop()
            for k in range(open_at, pos + 1):
                if buf[k] != "\n":
                    buf[k] = " "
    return "".join(buf)


def _pointee_of(expr, scan, at):
    """`T` for an expression of type `T *`, or None if that is not clear.

    Deduction, kept to the one shape the algorithms actually take: a range
    is a pair of pointers, so typing the *first* argument types the call.
    Everything here reads declarations out of the source text, because this
    pass runs before any symbol table exists -- which is also why it is
    narrow. Whatever it cannot type confidently it declines to type, and
    the caller reports that rather than guessing a `T`.
    """
    e = expr.strip()
    # Never look above the author's first line. Everything up there is
    # supplied by this module, and its locals and parameters are not names
    # the call could possibly have meant.
    lo = _after_origin(scan, at)
    # And not into a scope that has already closed -- another function, or
    # a block earlier in this one.
    scan = _visible_view(scan, lo, at)
    # `first + i`, `v.end() - 1`: pointer arithmetic does not change the
    # pointee, so the operand carries the type.
    while True:
        mm = re.match(r"^(.*?)\s*[-+]\s*[\w.>\-]+$", e)
        if not mm or not mm.group(1).strip():
            break
        e = mm.group(1).strip()
    while e.startswith("(") and _match_paren(e, 0) == len(e) - 1:
        e = e[1:-1].strip()
    # `c.begin()` / `c->end()` on a supplied container: the element type is
    # the container's first template argument.
    mm = re.match(r"^(\w+)\s*(?:\.|->)\s*(?:begin|end|rbegin|rend|ptr)"
                  r"\s*\(", e)
    if mm:
        cont_pat = re.compile(
            r"(?<![\w])(%s)\s*<([^<>]*)>\s+%s\s*[;=,)]"
            % ("|".join(_ELEM_CONTAINERS), re.escape(mm.group(1))))
        decl = _last_before(cont_pat, scan, at, lo)
        if decl:
            first = _split_top(decl.group(2))[0].strip()
            return first or None
        return None
    # `&x`, which is a `T *` when `x` is a `T`. This is how a call site
    # spells an argument to `swap`, whose parameters are pointers because
    # a reference cannot be spelled for a scalar and a class at once.
    if e.startswith("&"):
        base = e[1:].strip()
        if not re.match(r"^\w+$", base):
            return None
        # `(` as well as the rest: `string x("alpha");` is a declaration
        # of `x` whose type is followed by a constructor argument list, and
        # it is the ordinary way to declare an owning local.
        val_pat = re.compile(
            r"(?<![\w*])([A-Za-z_]\w*)\s+%s\s*[;=,)(]" % re.escape(base))
        d = _last_before(val_pat, scan, at, lo)
        if d and d.group(1) not in _DECL_NOISE:
            return d.group(1)
        return None
    if not re.match(r"^\w+$", e):
        return None
    # A local or parameter declared `T *name`.
    ptr_pat = re.compile(
        r"(?<![\w])([A-Za-z_]\w*)\s*\*\s*%s\s*[;=,)]" % re.escape(e))
    d = _last_before(ptr_pat, scan, at, lo)
    if d and d.group(1) not in _DECL_NOISE:
        return d.group(1)
    # An array `T name[..]`, which decays to `T *`.
    arr_pat = re.compile(
        r"(?<![\w])([A-Za-z_]\w*)\s+%s\s*\[" % re.escape(e))
    d = _last_before(arr_pat, scan, at, lo)
    if d and d.group(1) not in _DECL_NOISE:
        return d.group(1)
    return None


def _bare_call(t, scan, tmpl=()):
    """The first call to `t` that spelled no template arguments, or None.

    Not one inside the template's own span -- that is its definition, or a
    recursive use. And not one inside a class body: a method may share a
    name with a free template (`set::lower_bound` does, exactly as in C++)
    and inside its own class it is called bare, with the implicit `this`.
    Read as a call to the template, that reported every `set<T>`.
    """
    bodies = []
    for cm in re.finditer(r"(?<![\w])(?:class|struct)\s+\w+[^{;]*\{", scan):
        b_open = scan.index("{", cm.start())
        b_close = _match_brace(scan, b_open)
        if b_close is not None:
            bodies.append((cm.start(), b_close))
    def _is_signature(u):
        # A *definition or declaration* of a plain overload spells the
        # same `name(` a call does -- `inline bool operator==(const char
        # *a, ..) {` beside the template of that name -- and fastring.h
        # keeps a family of plain comparison overloads next to each
        # templated one, so every signature was read as a bare call to
        # the template.
        #
        # The trailing `;` does not settle it: a call *statement* ends
        # `);` too, and a first version of this filter read `sort(a, b);`
        # as a declaration and broke every deduction test in the suite.
        # What settles it is a body opening after the close paren, or a
        # *type word* before the name -- a call is preceded by an
        # operator, a brace, or a statement keyword, never by an
        # identifier that names a type.
        cl = _match_paren(scan, u.end() - 1)
        if cl is None:
            return False
        tail = scan[cl + 1:cl + 12].lstrip()
        if tail[:1] == "{" or tail.startswith(("const", "noexcept")):
            return True
        if tail[:1] != ";":
            return False
        head = scan[:u.start()].rstrip()
        pw = re.search(r"([A-Za-z_]\w*|[&*>])$", head)
        return bool(pw) and pw.group(1) not in (
            "return", "else", "do", "case", "goto", "throw", "co_return",
            "co_yield", "and", "or", "not")
    return next((u for u in re.finditer(
        r"(?<![\w.>])%s\s*\(" % re.escape(t["name"]), scan)
        if not _is_signature(u)
        and not (t["start"] <= u.start() < t["end"])
        # Nor inside a *sibling overload's* body: function templates
        # overload, and a call there names the enclosing template's own
        # parameters rather than anything instantiable yet.
        and not any(o["start"] <= u.start() < o["end"] for o in tmpl)
        and not any(b0 <= u.start() < b1 for b0, b1 in bodies)), None)


#: Which argument of a supplied range-writing template is the destination.
#: `fill(first, last, v)` writes over the range itself; `copy(first, last,
#: dst)` writes at `dst`.
_RANGE_WRITERS = {"fill": 0, "copy": 2}

#: A destination this pass can see is *constructed*: a container handing
#: out its own storage. Everything a container gives you here has been
#: through its `push_back` or its constructor, so the elements are real
#: objects and destroying one before writing over it is correct.
_CONSTRUCTED_RANGE = re.compile(
    r"^\s*&?\s*\w+\s*(?:\.|->)\s*(?:begin|end|ptr|data|rbegin)\s*\(")


def _constructed_range(expr, scan, at):
    """Is `expr` visibly a range whose elements have been constructed?

    Directly, when it is a container handing out its own storage. Or
    through one local: `T *dst = v.begin();` is the ordinary way to name a
    range before using it, and refusing that would mean the check fired
    most often on code that was already correct.

    One level only. Following a chain of aliases means tracking
    assignments, which is a dataflow this pass does not do -- and the
    answer for anything deeper is the same as for anything unrecognised:
    say so, rather than assume.
    """
    if _CONSTRUCTED_RANGE.match(expr):
        return True
    e = expr.strip()
    if not re.match(r"^\w+$", e):
        return False
    lo = _after_origin(scan, at)
    d = _last_before(re.compile(
        r"(?<![\w])[A-Za-z_]\w*\s*\*\s*%s\s*=([^;]*);" % re.escape(e)),
        scan, at, lo)
    return bool(d) and bool(_CONSTRUCTED_RANGE.match(d.group(1)))


#: The `<numeric>` templates, which combine elements with `+` and `*`.
_NUMERIC_FNS = ("accumulate", "iota", "inner_product", "partial_sum",
                "adjacent_difference")

#: Which operator each `<numeric>` function combines its elements with,
#: for the ones whose supplied body a class can actually go through.
#: Only `accumulate`: it combines elements and nothing else, so a class
#: with `operator+` works. `inner_product` multiplies as well as adds,
#: `partial_sum` and `adjacent_difference` write sums into a raw range,
#: and `iota` counts rather than combines -- those stay scalars-only, and
#: a class element is reported against the call as before.
_NUMERIC_OPS = {"accumulate": "+"}


def _class_declares_operator(scan, cls, op):
    """Does `class cls` declare `operator<op>` in its own body?

    Read off the text rather than the class table, because this check runs
    before classes are collected. The body is brace-matched from the
    declaration, so an operator on a *different* class does not count.
    """
    m = re.search(r"(?<![\w])(?:class|struct)\s+%s(?![\w])[^{;]*\{"
                  % re.escape(cls), scan)
    if not m:
        return False
    open_at = scan.index("{", m.end() - 1)
    depth, close = 0, None
    for k in range(open_at, len(scan)):
        if scan[k] == "{":
            depth += 1
        elif scan[k] == "}":
            depth -= 1
            if depth == 0:
                close = k
                break
    if close is None:
        return False
    return re.search(r"(?<![\w])operator\s*%s(?![=])" % re.escape(op),
                     scan[open_at:close]) is not None


def _check_numeric_elements(text, scan, path):
    """Refuse a `<numeric>` call over a class element type.

    These sum and multiply their elements, which needs `operator+` -- not
    in this subset. Left alone the failure still surfaced, but as a
    complaint about `sum = sum + *it`, a line inside a supplied template
    the author never wrote and cannot act on. Named here against the call
    instead.
    """
    for name in _NUMERIC_FNS:
        for u in re.finditer(
                r"(?<![\w.>])%s\s*(?:<([^;{}()]*)>)?\s*\(" % name, scan):
            if scan.rfind(_SRC_MARK, 0, u.start()) < 0:
                continue                  # the supplied prelude's own text
            open_at = scan.index("(", u.end() - 1)
            close = _match_paren(scan, open_at)
            if close is None:
                continue
            args = [a.strip() for a in _split_top(scan[open_at + 1:close])]
            if not args:
                continue
            targ = (u.group(1) or "").strip()
            elem = targ or _pointee_of(args[0], scan, u.start())
            if not elem:
                continue                  # deduction reports this itself
            if not re.search(r"(?<![\w])(?:class|struct)\s+%s(?![\w])"
                             % re.escape(elem), scan):
                continue
            # A class that *declares* the operator is fine: the supplied
            # template's `sum = sum + *it` resolves to it like any other
            # use. This used to refuse every class, which was right when no
            # binary operator was in the subset at all -- `string` now has
            # `operator+`, and nlohmann's `accumulate` over a string is the
            # first thing that hits this on a real header.
            op = _NUMERIC_OPS.get(name)
            if op is not None and _class_declares_operator(scan, elem, op):
                continue
            raise CppError(
                "%s:%d: `%s` combines elements with `%s`, and %s is a class "
                "that does not declare `operator%s`. Give it one, sum a "
                "scalar field instead, or write the loop."
                % (os.path.basename(path), _src_line(scan, u.start()),
                   name, op or "++", elem, op or "++"))


def _check_range_writes(text, scan, path):
    """Refuse `fill`/`copy` of an owning element into unrecognised storage.

    Both destroy each destination before constructing over it, which is
    what assignment would have done and is right for a container's range.
    Handed a pointer into memory nothing has constructed -- a `malloc`,
    a plain array -- it destroys garbage and follows whatever the bytes
    happened to be. That is a segfault rather than a diagnostic, and it is
    the same hazard `array<T,N>` of an owning element was refused for.

    Only for an element type that owns something: a class with a
    destructor. Plain data has nothing to destroy, so `__cpp_drop` is a
    no-op on it and any destination is fine -- which is most uses, and none
    of them are made harder by this.

    Recognising the safe shapes rather than proving the unsafe ones: a
    destination that is visibly a container's own range is accepted, and
    anything this pass cannot see through is reported with the container
    form named. Guessing the other way is what crashed.
    """
    for name, dst_idx in _RANGE_WRITERS.items():
        for u in re.finditer(
                r"(?<![\w.>])%s\s*(?:<([^;{}()]*)>)?\s*\(" % name, scan):
            origin = scan.rfind(_SRC_MARK, 0, u.start())
            if origin < 0:
                continue                  # the supplied prelude's own text
            close = _match_paren(scan, scan.index("(", u.end() - 1))
            if close is None:
                continue
            args = [a.strip() for a in
                    _split_top(scan[scan.index("(", u.end() - 1) + 1:close])]
            if dst_idx >= len(args):
                continue
            targ = (u.group(1) or "").strip()
            elem = targ or _pointee_of(args[0], scan, u.start())
            if not elem:
                continue                  # deduction reports this itself
            # Owning is a destructor, the same test used everywhere here.
            # Asked of the text because classes are not parsed yet: this
            # runs before anything reads types, so that a diagnostic about
            # a template body is never emitted.
            if not re.search(r"~\s*%s\s*\(" % re.escape(elem), scan):
                continue
            if _constructed_range(args[dst_idx], scan, u.start()):
                continue
            raise CppError(
                "%s:%d: `%s` writes over `%s` elements, destroying each one "
                "before constructing over it -- which needs a destination "
                "whose elements have been constructed. `%s` is not visibly "
                "a container's own range, and %s owns a resource, so "
                "destroying whatever is there would follow bytes nothing "
                "set. Pass a container's `begin()` or `ptr()`, or use "
                "`push_back` to build the destination."
                % (os.path.basename(path), _src_line(scan, u.start()),
                   name, elem, args[dst_idx], elem))


def _spell_deduced_calls(text, scan, tmpl):
    """Rewrite `f(..)` to `f<T>(..)` wherever `T` can be deduced.

    Deduction is done by *spelling the call the long way* and letting the
    ordinary substitution below run on it, rather than by threading a
    deduced type through the monomorphiser. What comes out is a source the
    author could have written, so there is one code path for both forms
    and no second place for them to disagree.

    Returns the rewritten text, or None if nothing was deduced.
    """
    edits = []
    for t in tmpl:
        while True:
            u = _bare_call(t, scan, tmpl)
            if u is None:
                break
            op = u.end() - 1
            close = _match_paren(scan, op)
            if close is None:
                break
            args = [a.strip() for a in _split_top(scan[op + 1:close])]
            got = _deduce_targs(t, args, scan, u.start())
            if not got:
                break
            edits.append((u.start(), u.end(),
                          "%s<%s>(" % (t["name"], ", ".join(got))))
            # Blank this call's name in the scan so the next round finds
            # the following one rather than looping on this same match.
            scan = (scan[:u.start()]
                    + " " * (u.end() - u.start()) + scan[u.end():])
    if not edits:
        return None
    edits.sort()
    out, prev = [], 0
    for a, b, rep in edits:
        out.append(text[prev:a])
        out.append(rep)
        prev = b
    out.append(text[prev:])
    return "".join(out)


def _value_type_of(expr, scan, at):
    """`T` for an expression of type `T`, or None if that is not clear.

    The by-value counterpart of `_pointee_of`, and just as narrow: a plain
    name declared `T name` somewhere visible above the call. Deduction from
    a by-value parameter is what a *partially* spelled call needs --
    `align_up<64>(x)` gives the non-type argument and leaves `X` to be read
    off `x`.
    """
    e = (expr or "").strip()
    while e.startswith("(") and _match_paren(e, 0) == len(e) - 1:
        e = e[1:-1].strip()
    if not re.match(r"^\w+$", e):
        return None
    lo = _after_origin(scan, at)
    scan = _visible_view(scan, lo, at)
    # `T name;`, `T name = ..`, `T name(..)` -- the same shape the `&x`
    # branch of `_pointee_of` reads, and `*` is excluded ahead of the type
    # so `T *name` is not mistaken for a value.
    val_pat = re.compile(
        r"(?<![\w*])([A-Za-z_]\w*)\s+%s\s*[;=,)(]" % re.escape(e))
    d = _last_before(val_pat, scan, at, lo)
    if d and d.group(1) not in _DECL_NOISE:
        return d.group(1)
    return None


#: A recursive template's instantiation chain is bounded here. Deep enough
#: for the metaprogramming this subset sees (coost's deepest is 8), low
#: enough that a runaway is reported in well under a second.
_MAX_INSTANTIATIONS = 256


def _recurses(t, text):
    """Does this template's own body name itself with explicit arguments?

    Only such a template needs the worklist below, and checking first keeps
    it off the ordinary one-shot instantiation path entirely.
    """
    body = text[t["start"]:t["end"]]
    return re.search(r"(?<![\w])%s\s*<" % re.escape(t["name"]),
                     body[body.index("(") if "(" in body else 0:]) is not None


def _eval_int_targ(arg):
    """`4 - 1` -> `3` for a non-type template argument, else unchanged.

    A recursive template writes its own argument as arithmetic --
    `copy<N - 1>(..)` in coost's `god.h` -- and substitution turns that
    into `copy<4 - 1>`, which is a *different* spelling from `copy<3>` and
    mangles to a symbol nothing defines. C++ evaluates the expression to
    pick the instantiation; so does this.

    Deliberately narrow: integer literals and `+ - * / %` with parentheses,
    nothing else. Anything with a name in it is left exactly as written,
    because a name here is a type argument or a constant this pass cannot
    read.
    """
    s = (arg or "").strip()
    if not s or not re.match(r"^[\d\s()+*/%-]+$", s):
        return arg
    if not re.search(r"\d", s):
        return arg
    try:
        val = eval(s, {"__builtins__": {}}, {})       # digits/operators only
    except Exception:
        return arg
    if isinstance(val, int):
        return str(val)
    return arg


def _spell_partial_targs(text, scan, tmpl):
    """Fill in the arguments a partially spelled call left out.

    `template<size_t A, typename X> X align_up(X x)` called as
    `align_up<64>(x)` gives one argument to a template that takes two. C++
    deduces the rest; this pass substitutes by position, so it used to
    report the call. coost's `god.h` has three of these and they block
    `fastring.h` and `fastream.h` at their first include.

    Explicit arguments bind to the *leading* parameters, so only the tail
    has to be deduced -- each from a function parameter written `P name`
    or `P *name`, against the argument in that position. If any one of them
    cannot be typed the call is left exactly as it was, and the arity check
    reports it as before.

    Returns the rewritten text, or None if nothing was filled in.
    """
    edits = []
    for t in tmpl:
        if t.get("pack"):
            continue      # a shorter list than the parameters is the point
        n = len(t["params"])
        for u in re.finditer(
                r"(?<![\w])%s\s*<([^;{}()<>]*)>\s*\(" % re.escape(t["name"]),
                scan):
            if any(o["start"] <= u.start() < o["end"] for o in tmpl):
                # Inside *some* template's body -- this one's (a recursive
                # use) or a sibling overload's. Either way the call cannot
                # be instantiated yet: its arguments may name the enclosing
                # template's own parameters, as `align_up<A>((size_t)x)`
                # does in coost's `god.h`. Substitution replaces `A` with
                # the real argument when that enclosing template is
                # instantiated, and the pass runs again over the copy.
                #
                # Keyed on every template rather than only `t` because
                # function templates overload: `align_up` is three of them,
                # so a call in one body is outside the range of the other
                # two and was read as a call to them with too few
                # arguments.
                continue
            given = [a.strip() for a in _split_top(u.group(1)) if a.strip()]
            if not given or len(given) >= n:
                continue
            op = u.end() - 1
            close = _match_paren(scan, op)
            if close is None:
                continue
            cargs = [a.strip() for a in _split_top(scan[op + 1:close])]
            rest = []
            for pname in t["params"][len(given):]:
                got = None
                for idx, fp in enumerate(t["fparams"]):
                    if idx >= len(cargs):
                        continue
                    fp = fp.strip()
                    if re.match(r"^%s\s*\*\s*\w+$" % re.escape(pname), fp):
                        got = _pointee_of(cargs[idx], scan, u.start())
                    elif re.match(r"^(?:const\s+)?%s\s+\w+$"
                                  % re.escape(pname), fp):
                        got = _value_type_of(cargs[idx], scan, u.start())
                    if got:
                        break
                if not got:
                    rest = None
                    break
                rest.append(got)
            if not rest:
                continue
            edits.append((u.start(), u.end(),
                          "%s<%s>(" % (t["name"], ", ".join(given + rest))))
    if not edits:
        return None
    edits.sort()
    out, prev = [], 0
    for a, b, rep in edits:
        if a < prev:
            continue
        out.append(text[prev:a])
        out.append(rep)
        prev = b
    out.append(text[prev:])
    return "".join(out)


def _deduce_targs(t, callsite_args, scan, at):
    """Template arguments for a call that spelled none, or None.

    Only the shape the supplied algorithms use: one type parameter, and a
    function parameter written `T *`. Matching that against an argument of
    type `X *` gives `T = X`. Anything else -- more than one parameter,
    no pointer parameter, an argument that will not type -- returns None,
    and the call is reported.
    """
    if len(t["params"]) != 1 or t.get("pack"):
        return None
    tp = t["params"][0]
    for idx, fp in enumerate(t["fparams"]):
        if not re.match(r"^%s\s*\*\s*\w+$" % re.escape(tp), fp.strip()):
            continue
        if idx >= len(callsite_args):
            continue
        got = _pointee_of(callsite_args[idx], scan, at)
        if got:
            return [got]
    return None


def _template_fits_call(t, cargs, scan, at):
    """Does this overload of a function template fit this call?

    Function templates overload -- coost's `god.h` has three `align_up` --
    and this pass keys them by name, so every one of them saw every call
    and instantiated it. Two overloads then lowered to the same symbol and
    the second redefined the first.

    Selection is narrow on purpose. Argument *count* first, which separates
    `align_up(X)` from `align_up(X, A)`. Then pointer-ness of each argument
    the pass can type, which separates `align_up(X x)` from
    `align_up(X *x)` -- that is the whole of C++ overload resolution this
    needs, and anything it cannot type is not held against the candidate.
    """
    fps = [f.strip() for f in t["fparams"] if f.strip()]
    if t.get("pack") and fps and "..." in fps[-1]:
        # The pack parameter absorbs zero or more arguments, so the fixed
        # prefix is what has to fit.
        fps = fps[:-1]
        if len(cargs) < len(fps):
            return False
        cargs = cargs[:len(fps)]
    elif len(fps) != len(cargs):
        return False
    for fp, ca in zip(fps, cargs):
        wants_ptr = bool(re.match(r"^.*\*\s*\w+$", fp))
        if _pointee_of(ca, scan, at):
            is_ptr = True
        elif _value_type_of(ca, scan, at):
            is_ptr = False
        else:
            continue                 # untypeable: not held against it
        if is_ptr != wants_ptr:
            return False
    return True


def _substitute_template(t, got, scan):
    """One instantiation's body, with template parameters substituted.

    For a pack template, `got` holds the fixed arguments followed by the
    pack's elements, and four spellings are expanded before the ordinary
    per-parameter substitution runs:

    * the pack *function parameter* -- `V&& ... v` -- becomes one parameter
      per element, named `__pk1, __pk2, ..`, each spelled as the pack
      parameter was but with `V` replaced by that element's type;
    * `std::forward<V>(v)...` and bare `v...` become `__pk1, __pk2, ..`
      (forwarding is pass-through here: there is no reference collapsing
      to preserve, because every element parameter is spelled concretely);
    * `V...` in template-argument position becomes the element list, which
      is what turns the recursive `f<V...>(v...)` into a *spelled* call the
      worklist can instantiate;
    * a call whose entire argument list is one pack expansion --
      `f(std::forward<V>(v)...)` -- has its template arguments spelled from
      the element types, which is what the recursive-consume idiom writes.

    An empty pack erases the expansion and the comma before it, so
    `f(x, v...)` becomes `f(x)` -- which is how the recursion bottoms out
    at a plain overload.
    """
    body = scan  # caller passes the template's own text slice
    fixed = t["params"]
    elems = got[len(fixed):] if t.get("pack") else []
    one = body
    if t.get("pack"):
        pk = t["pack"]
        vnames = ["__pk%d" % (i + 1) for i in range(len(elems))]
        # The pack function parameter, e.g. `V&& ... v` or `V... v`.
        fp_re = re.compile(
            r"(,\s*)?((?:const\s+)?%s\b[^,()]*?)\.\.\.\s*(\w+)" % re.escape(pk))
        fm = fp_re.search(one)
        vname = fm.group(3) if fm else None
        if fm:
            spelt = []
            for e, vn in zip(elems, vnames):
                sp = re.sub(r"(?<![\w])%s(?![\w])" % re.escape(pk),
                            e, fm.group(2)).strip()
                # A forwarding reference becomes a by-value parameter.
                # Forwarding is pass-through here -- every element is
                # spelled concretely, so there is no reference collapsing
                # to preserve, and `T &&` on a scalar is not C.
                sp = re.sub(r"\s*&&\s*$", "", sp)
                spelt.append(sp + " " + vn)
            rep = ((fm.group(1) or "") + ", ".join(spelt)) if spelt else ""
            one = one[:fm.start()] + rep + one[fm.end():]
        if vname:
            # `std::forward<V>(v)...` and bare `v...`, with the comma
            # before them when the pack is empty.
            # `std::` may already have been stripped by the time this
            # runs, so the qualifier is optional.
            exp_re = re.compile(
                r"(,\s*)?(?:(?:std\s*::\s*)?forward\s*<\s*%s\s*>"
                r"\s*\(\s*%s\s*\)"
                r"|%s)\s*\.\.\." % (re.escape(pk), re.escape(vname),
                                   re.escape(vname)))
            joined = ", ".join(vnames)
            def _exp(mm):
                if not vnames:
                    return ""
                return (mm.group(1) or "") + joined
            one = exp_re.sub(_exp, one)
        # `V...` in a template-argument list.
        one = re.sub(r"(,\s*)?%s\s*\.\.\." % re.escape(pk),
                     lambda mm: ((mm.group(1) or "") + ", ".join(elems))
                     if elems else "", one)
        # A recursive call left bare: `name(__pk1, __pk2)` where `name` is
        # this template family's own. Spell its arguments from the element
        # types so the worklist below can instantiate it. Only when the
        # whole argument list is the expanded pack -- anything mixed is
        # left for deduction or its diagnostic.
        if vnames:
            call_re = re.compile(
                r"(?<![\w<.>])(\w+)\s*\(\s*%s\s*\)"
                % r"\s*,\s*".join(re.escape(v) for v in vnames))
            def _spell(mm):
                # C++ prefers a non-template overload on a match, and the
                # consume idiom's base overloads exist precisely to
                # terminate the recursion -- `int last(int)` beside the
                # pack template. Spelling the template here recursed past
                # the base and emitted a call to a nullary `last()` nothing
                # defines. So a plain overload of this arity wins, and the
                # call is left bare for C to resolve against it.
                if len(vnames) in t.get("plain_arities", ()):
                    return mm.group(0)
                return "%s<%s>(%s)" % (mm.group(1), ", ".join(elems),
                                       ", ".join(vnames))
            one = call_re.sub(
                lambda mm: _spell(mm) if mm.group(1) == t["name"]
                else mm.group(0), one)
    for pname, arg in zip(fixed, got):
        one = re.sub(r"(?<![\w])%s(?![\w])" % re.escape(pname), arg, one)
    return one


def _monomorphise_function_templates(text, scan, path, _depth=0):
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
    # A `#define` body is not code. coost's `DEF_has_method(f)` macro holds
    # a whole function template whose name pastes with `##f`, and the
    # collector below read `##f(` as a template named `f` -- which then
    # refused the author's own `int f()` as a bare call to it. Blanked in
    # the scan only (length-preserving), exactly as the call passes do;
    # the directives themselves still reach the output.
    scan = _blank_directives(scan)
    tmpl = []
    # Class bodies, so a member template can know what encloses it. A
    # template whose *name* is its enclosing class is a constructor
    # template -- coost's SFINAE `template<typename X, god::if_t<..> = 0>
    # shared(const shared<X> &x)` -- and every `shared<T>` in the file
    # names the class, not it. Matching those uses against it gave every
    # one-argument use an arity error against a member nobody can spell.
    class_spans = []
    for cm in re.finditer(r"(?<![\w])(?:class|struct)\s+(\w+)[^{;]*\{",
                          scan):
        b_open = scan.index("{", cm.start())
        b_close = _match_brace(scan, b_open)
        if b_close is not None:
            class_spans.append((cm.start(), b_close, cm.group(1)))

    # Class *template* bodies, so a call inside one can be told apart from
    # a call at file scope. See the refusal below.
    tclass_spans = []
    for _cs, _ce, _cn in class_spans:
        if re.search(r"(?<![\w])template\s*<[^;{}]*>\s*$", scan[:_cs].rstrip()):
            tclass_spans.append((_cs, _ce, _cn))

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
        # `operator=` and friends: the `=` defeats a bare `\w+` name, and
        # the search then fell through into the *body* and took the first
        # word before a paren there -- coost's SFINAE `template<..>
        # unique& operator=(unique<X>&& x)` was collected as a template
        # named `if`, which then refused the author's own `if (...)`
        # statements as bare calls to it. An operator member template is
        # collected under its own spelling instead; uninstantiated, it is
        # blanked like any other, which is what C++ does with one.
        # The optional `<..>` catches an explicit specialisation, whose
        # arguments are spelled after the name -- `int sum_to<0>()`. Without
        # it the name search found no `name(` at all and the whole
        # definition was skipped, so it passed through, got mangled to the
        # same symbol the general template produced for those arguments,
        # and the two collided in the emitted C.
        nm = re.search(r"((?:operator\s*[-+*/%^&|<>=!\[\]]+|\w+))"
                       r"\s*(<[^<>()]*>)?\s*\(",
                       scan[after:k + 1])
        if not nm:
            continue
        # `_split_targs`, not `_split_top`: a head parameter may itself be
        # a template -- coost's SFINAE members write `template<typename X,
        # god::if_t<a(), b(), int> = 0>` -- and the angle-blind split cut
        # at the commas inside it, giving the class's own constructor
        # template four "parameters" and every one-argument use an arity
        # error against it.
        raw_params = [p.strip() for p in _split_targs(scan[lt + 1:gt])
                      if p.strip()]
        # A trailing parameter pack -- `typename ...V`, however spaced.
        # One pack, last, in a *function* template only: that is the shape
        # coost's `make`, `print` and the recursive-consume idiom take.
        # A pack anywhere else keeps the class-template refusal.
        pack = None
        if raw_params and "..." in raw_params[-1]:
            pm = re.match(r"^(?:typename|class)\s*\.\.\.\s*(\w+)$",
                          raw_params[-1])
            if pm:
                pack = pm.group(1)
                raw_params = raw_params[:-1]
        params = [p.split()[-1] for p in raw_params]
        # The function's own parameter list, kept so a call with no
        # explicit arguments can be matched against it below.
        sig_open = scan.index("(", after + nm.start())
        sig_close = _match_paren(scan, sig_open)
        fparams = ([q.strip() for q in
                    _split_top(scan[sig_open + 1:sig_close])]
                   if sig_close is not None else [])
        encl = next((cn for a, b, cn in class_spans
                     if a <= m.start() < b), None)
        # `template<> int sum_to<0>() { .. }` -- an explicit
        # specialisation. It is a *definition*, not a template to
        # instantiate: the arguments are spelled after the name and the
        # head is empty. Recorded so the general template is not
        # instantiated for those same arguments, which is what terminates
        # a recursive one -- coost's `god::copy<0>` is exactly this, and
        # without it the general body was emitted for `0` too, calling
        # `copy<-1>` and redefining the specialisation's symbol.
        spec_args = None
        if not params and nm.group(2):
            spec_args = [_eval_int_targ(a.strip())
                         for a in _split_targs(nm.group(2)[1:-1])
                         if a.strip()]
        tmpl.append({
            "name": nm.group(1), "params": params, "pack": pack,
            "spec_args": spec_args,
            "ctor_of": encl if encl == nm.group(1) else None,
            "start": m.start(), "end": k + 1,
            "decl_only": body_open is None,
            "fparams": [q for q in fparams if q],
        })
    if not tmpl:
        return text, scan, []

    # The arities of each name's *non-template* overloads, so a pack
    # template's recursion can bottom out at one -- read off definitions
    # and declarations outside every template span.
    for t in tmpl:
        if not t.get("pack"):
            continue
        arities = set()
        for pm in re.finditer(r"(?<![\w<.>])%s\s*\(" % re.escape(t["name"]),
                              scan):
            if any(o["start"] <= pm.start() < o["end"] for o in tmpl):
                continue
            cl = _match_paren(scan, pm.end() - 1)
            if cl is None:
                continue
            after = scan[cl + 1:cl + 3].lstrip()[:1]
            if after not in ("{", ";"):
                continue                 # a call, not a signature
            ps = [q for q in _split_top(scan[pm.end():cl]) if q.strip()]
            arities.add(len(ps))
        t["plain_arities"] = arities

    # Deduction, before anything is substituted: a call that spelled no
    # template arguments is rewritten to spell them, and the whole pass
    # restarts over that text. Restarting rather than continuing because
    # every offset recorded above indexes the old text, and the rewrite
    # moves them. `_depth` bounds it -- the second pass has explicit
    # arguments everywhere deduction succeeded, so it deduces nothing new
    # and cannot rewrite again.
    if _depth == 0:
        _check_numeric_elements(text, scan, path)
        _check_range_writes(text, scan, path)
        spelled = _spell_deduced_calls(text, scan, tmpl)
        if spelled is not None:
            return _monomorphise_function_templates(
                spelled, _strip_comments(spelled), path, _depth=1)
        # The same trick for a call that spelled *some* of its arguments:
        # rewrite it the long way and let the ordinary substitution run.
        partial = _spell_partial_targs(text, scan, tmpl)
        if partial is not None:
            return _monomorphise_function_templates(
                partial, _strip_comments(partial), path, _depth=1)

    # Which arguments each one is instantiated with. A member call is
    # `c.reg<int>(..)`, so a leading `.` cannot be excluded.
    # Arguments that an explicit specialisation already defines, per name.
    specialised = {}
    for t in tmpl:
        if t.get("spec_args") is not None:
            specialised.setdefault(t["name"], []).append(t["spec_args"])

    out, last, names = [], 0, []
    for t in sorted(tmpl, key=lambda t: t["start"]):
        if t.get("spec_args") is not None:
            # An explicit specialisation is already a definition. Drop the
            # empty `template<>` head -- C has nothing to do with it, and
            # left in place it reached the C front end verbatim -- and give
            # it the mangled name the general template's instantiations
            # use, so a call reaches it by the same symbol.
            spec = text[t["start"]:t["end"]]
            spec = spec[_match(spec, spec.index("<"), "<", ">") + 1:]
            suffix = "_".join(_mangle_targ(a) for a in t["spec_args"])
            spec = re.sub(
                r"(?<![\w])%s\s*<[^<>()]*>\s*(?=\()" % re.escape(t["name"]),
                "%s_%s" % (t["name"], suffix), spec, count=1)
            out.append(text[last:t["start"]])
            out.append(spec)
            last = t["end"]
            names.append("%s_%s" % (t["name"], suffix))
            continue
        args = []
        for u in ([] if t.get("ctor_of") else re.finditer(
                r"(?<![\w])%s\s*<([^;{}()]*)>\s*\(" % re.escape(t["name"]),
                scan)):
            _tc = next(((cs, ce, cn) for cs, ce, cn in tclass_spans
                        if cs <= u.start() < ce), None)
            if _tc is not None:
                # A call to a function template from inside a *class*
                # template's body. Its arguments may name the enclosing
                # class's parameters -- `la_add<T,N>(..)` inside
                # `Vec<T,N>::operator+` -- and this pass runs before any
                # class is monomorphised, so `T` and `N` are still
                # literally those letters. Instantiating now emitted
                # `la_add_T_N(T o[N], ..)`: a function whose parameter type
                # C has never heard of, with no diagnostic, because nothing
                # downstream knew a template had been involved.
                #
                # Deferring it properly means keeping the definition alive
                # for a second run after the classes substitute, which the
                # pass is not structured for -- it consumes each template
                # once and blanks it. Until it is, this is reported rather
                # than approximated, which is the rule this compiler runs
                # on. The workaround costs one line at the call.
                raise CppError(
                    "%s:%d: `%s<%s>` is called from inside the class "
                    "template `%s`, whose parameters are not substituted "
                    "yet -- so the arguments here are still the letters "
                    "`%s`, not types. Function templates are "
                    "monomorphised before classes are, and this lowering "
                    "has no second pass to catch the call afterwards. "
                    "Give the kernel a concrete instantiation at file "
                    "scope and call that, or write the loop in the method "
                    "body."
                    % (os.path.basename(path), _src_line(scan, u.start()),
                       t["name"], u.group(1).strip(), _tc[2],
                       u.group(1).strip()))

            if any(o["start"] <= u.start() < o["end"] for o in tmpl):
                # Inside *some* template's body -- this one's (a recursive
                # use) or a sibling overload's. Either way the call cannot
                # be instantiated yet: its arguments may name the enclosing
                # template's own parameters, as `align_up<A>((size_t)x)`
                # does in coost's `god.h`. Substitution replaces `A` with
                # the real argument when that enclosing template is
                # instantiated, and the pass runs again over the copy.
                #
                # Keyed on every template rather than only `t` because
                # function templates overload: `align_up` is three of them,
                # so a call in one body is outside the range of the other
                # two and was read as a call to them with too few
                # arguments.
                continue
            # Only the overload this call actually selects. Without this
            # every same-named template instantiated every call, and two
            # of them lowered to one symbol.
            siblings = [o for o in tmpl if o["name"] == t["name"]
                        and not o.get("ctor_of")]
            if len(siblings) > 1:
                op_at = u.end() - 1
                cl = _match_paren(scan, op_at)
                cargs = ([a.strip()
                          for a in _split_top(scan[op_at + 1:cl])]
                         if cl is not None else [])
                fits = [o for o in siblings
                        if _template_fits_call(o, cargs, scan, u.start())]
                if len(fits) == 1 and fits[0] is not t:
                    continue
                if len(fits) > 1 and t is not fits[0]:
                    raise CppError(
                        "%s:%d: `%s<%s>` matches %d overloads of `%s` that "
                        "this pass cannot tell apart. It selects on argument "
                        "count and on whether each argument is a pointer; "
                        "these agree on both. Give the overloads different "
                        "names."
                        % (os.path.basename(path),
                           _src_line(text, u.start()), t["name"],
                           u.group(1).strip(), len(fits), t["name"]))
            got = [_eval_int_targ(a.strip())
                   for a in _split_top(u.group(1)) if a.strip()]
            # A pack absorbs everything after the fixed parameters, so its
            # template takes *at least* that many rather than exactly.
            bad = (len(got) < len(t["params"]) if t.get("pack")
                   else len(got) != len(t["params"]))
            if bad:
                raise CppError(
                    "%s:%d: `%s<%s>` gives %d template argument%s to a "
                    "template that takes %s%d. This pass substitutes them "
                    "by position and has no defaults to fall back on."
                    % (os.path.basename(path),
                       _src_line(text, u.start()), t["name"],
                       u.group(1).strip(), len(got),
                       "" if len(got) == 1 else "s",
                       "at least " if t.get("pack") else "",
                       len(t["params"])))
            if got in specialised.get(t["name"], []):
                continue           # an explicit specialisation defines it
            if got not in args:
                args.append(got)
        # Derived instantiations. A pack template's body calls itself with
        # one fewer element -- `sum<A,B,C>` writes `sum<B, C>(__pk1, __pk2)`
        # once substituted -- and that spelling exists only in the copy,
        # which the scan above never sees. So each new instantiation's
        # substituted body is scanned for further spelled calls to this
        # same template, to a fixpoint. Bounded, because every derived call
        # has strictly fewer template arguments than the one it came from.
        # Runs for a *recursive* template of any kind, not only a pack one:
        # `sum_to<4>`'s substituted body calls `sum_to<3>`, a spelling that
        # exists only in the copy, so without this the chain stopped after
        # one instantiation and the middle ones were never emitted.
        if args and (t.get("pack") or _recurses(t, text)):
            probe_body0 = text[t["start"]:t["end"]]
            probe_body0 = probe_body0[
                _match(probe_body0, probe_body0.index("<"), "<", ">") + 1:]
            queue = list(args)
            while queue:
                one = _substitute_template(t, queue.pop(), probe_body0)
                for u2 in re.finditer(
                        r"(?<![\w])%s\s*<([^;{}()]*)>\s*\("
                        % re.escape(t["name"]), one):
                    # Evaluated, not taken literally: `sum_to<4 - 1>` and
                    # `sum_to<3>` are the same instantiation. Without this
                    # each round produced a longer spelling -- `4 - 1 - 1`,
                    # then `4 - 1 - 1 - 1` -- that never matched anything
                    # already seen, and the loop did not terminate.
                    got2 = [_eval_int_targ(a.strip())
                            for a in _split_top(u2.group(1)) if a.strip()]
                    if got2 in specialised.get(t["name"], []):
                        continue
                    fits = (len(got2) >= len(t["params"]) if t.get("pack")
                            else len(got2) == len(t["params"]))
                    if fits and got2 not in args:
                        if len(args) >= _MAX_INSTANTIATIONS:
                            # A pack shrinks by one element each round, but
                            # arbitrary argument arithmetic need not shrink
                            # at all -- a recursion whose base case is
                            # never reached would otherwise spin here.
                            raise CppError(
                                "%s: `%s` instantiated more than %d times "
                                "and kept going. A recursive template needs "
                                "a base case this pass can reach -- an "
                                "explicit specialisation, or an argument "
                                "that shrinks to one."
                                % (os.path.basename(path), t["name"],
                                   _MAX_INSTANTIATIONS))
                        args.append(got2)
                        queue.append(got2)
        body = text[t["start"]:t["end"]]
        # Drop the `template<..>` head; what is left is an ordinary
        # function once the parameters are gone.
        head_gt = _match(body, body.index("<"), "<", ">")
        body = body[head_gt + 1:]
        copies = []
        for got in args:
            one = _substitute_template(t, got, body)
            # `typename X::y` is C++ telling the parser that `y` names a
            # type. With `X` known there is nothing left to tell it.
            one = re.sub(r"(?<![\w])typename\s+", "", one)
            # `__cpp_ref(T)` in a *free* function's parameters. The class
            # emitter expands it for a method, against the class it belongs
            # to; a free template has none. Here, though, `T` has just been
            # replaced by the type the call spelled, so the question "is
            # this a class" can finally be asked -- which is why this sits
            # after substitution and not before it, where `__cpp_ref(T)`
            # would read `T` as a scalar and pass an owning element by
            # value.
            #
            # `const T &` for a class flows on into the ordinary reference
            # lowering, which makes it a `T *` and takes the address at each
            # call -- exactly the pointer `__cpp_cmp` wants on its right. A
            # scalar stays by value, so a literal argument still binds.
            if "__cpp_ref" in one or "__cpp_rref" in one:
                cnames = set(re.findall(
                    r"(?<![\w])(?:class|struct)\s+(\w+)", scan))
                one = _expand_cpp_ref(_expand_cpp_rref(one, cnames), cnames)
            suffix = "_".join(_mangle_targ(a) for a in got)
            # Overloaded templates share a name *and* their arguments:
            # `align_up<64>` names both the value and the pointer overload,
            # and encoding only the arguments gave the two instantiations
            # one symbol with conflicting types. The parameter shape goes
            # into the name as well -- one `p` per pointer parameter -- but
            # only when this name has more than one template, so an
            # ordinary template's symbol keeps the spelling it had.
            if sum(1 for o in tmpl if o["name"] == t["name"]
                   and not o.get("ctor_of")) > 1:
                shape = "".join(
                    "p" if re.search(r"\*\s*\w*$", fp.strip()) else "v"
                    for fp in t["fparams"] if fp.strip())
                if shape:
                    suffix = "%s_%s" % (suffix, shape)
            one = re.sub(r"(?<![\w])%s(?=\s*\()" % re.escape(t["name"]),
                         "%s_%s" % (t["name"], suffix), one, count=1)
            copies.append(one)
            names.append("%s_%s" % (t["name"], suffix))
        if not copies and not t["decl_only"]:
            # No explicit instantiation anywhere. Blanking the body is right
            # for a template the file never uses -- there is nothing to
            # emit it over -- but wrong, and silently so, if the file calls
            # it *without* arguments: deduction is not implemented here, so
            # the call would survive over a definition that just went away
            # and fail at link time naming a symbol the source never wrote.
            # Reported here, against the source, rather than there.
            bare = _bare_call(t, scan, tmpl)
            if bare is not None:
                raise CppError(
                    "%s:%d: `%s` is a function template called with no "
                    "template arguments, and they could not be deduced "
                    "from the arguments. Deduction here only reads a "
                    "parameter written `T *` against an argument whose "
                    "pointee is declared in this file, so write "
                    "`%s<T>(..)` at the call."
                    % (os.path.basename(path),
                       _src_line(scan, bare.start()),
                       t["name"], t["name"]))
        out.append(text[last:t["start"]])
        out.append("\n".join(copies) if copies else
                   re.sub(r"[^\n]", " ", text[t["start"]:t["end"]]))
        last = t["end"]
    out.append(text[last:])
    text = "".join(out)

    # And the call sites, now that the copies exist to be called.
    scan_now = _strip_comments(text)
    for t in tmpl:
        def _fix(u, _t=t):
            got = [_eval_int_targ(a.strip())
                   for a in _split_top(u.group(1)) if a.strip()]
            suffix = "_".join(_mangle_targ(a) for a in got)
            # Which overload does this call select? The definitions encode
            # their parameter shape in the name when a template family has
            # more than one, so the call has to pick the same one --
            # otherwise every call went to whichever sibling `re.sub`
            # happened to reach first.
            sibs = [o for o in tmpl if o["name"] == _t["name"]
                    and not o.get("ctor_of")]
            if len(sibs) > 1:
                op_at = u.end() - 1
                cl = _match_paren(scan_now, op_at) \
                    if op_at < len(scan_now) else None
                cargs = ([a.strip()
                          for a in _split_top(scan_now[op_at + 1:cl])]
                         if cl is not None else [])
                fits = [o for o in sibs
                        if _template_fits_call(o, cargs, scan_now, u.start())]
                pick = fits[0] if len(fits) == 1 else _t
                shape = "".join(
                    "p" if re.search(r"\*\s*\w*$", fp.strip()) else "v"
                    for fp in pick["fparams"] if fp.strip())
                if shape:
                    suffix = "%s_%s" % (suffix, shape)
            return "%s_%s(" % (_t["name"], suffix)
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


def _find_classes(scan, text, path="<cpp>"):
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
                                             _src_line(scan, m.start()),
                                             path),
                              _src_line(scan, m.start()),
                              *_parse_base(m.group(3), m.group(2)))))
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


def _split_declarators(decl):
    """Split `int x, y` into its declarators, respecting `<>`.

    Not `_split_top`, which tracks parens and brackets but deliberately not
    angle brackets: it is used for call arguments, where `<` is a
    comparison as often as a template bracket, and treating it as one
    would mis-split `f(a < b, c)`. In a *declaration* there is no such
    ambiguity -- `<` there opens a template argument list -- so
    `map<int, int> m` is one declarator rather than two.
    """
    parts, cur, depth, angle, quote = [], [], 0, 0, None
    for c in decl:
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
        elif c == "<":
            angle += 1
        elif c == ">" and angle > 0:
            angle -= 1
        if c == "," and depth == 0 and angle == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    parts.append("".join(cur))
    return parts


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


_TEMPLATE_OPEN = re.compile(r"(?<![\w.>])(\w+)\s*<")


def _iter_template_uses(text, tnames):
    """Every *innermost* `Name<..>` use in `text`, left to right.

    Innermost first is what makes nesting work without a fixed point over
    the whole file: `Holder<Pair<int,int>>` yields `Pair<int,int>` first, so
    by the time the outer use is looked at its argument already reads
    `Pair_int_int` and mangles to a name that exists.

    Template *bodies* are blanked out before the recording scan rather than
    filtered here, because whether `Holder<T>` names an instantiation depends
    on where it sits, not on how it is spelled: inside a template body `T` is
    a parameter and the use is the pattern, while at file scope `T` could
    perfectly well be a typedef somebody wrote.

    The uses this returns never overlap: an innermost one holds no `<` in its
    arguments, and the pattern above needs one, so the next match can only
    start past the closing `>`. That is what lets a caller rewrite the whole
    run from a single scan instead of one per pass.

    Returns a list rather than generating. Both callers iterate the whole scan
    over a finite string and nothing here has a side effect between elements,
    so the two are equivalent -- and py2c lowers a list, not a generator.
    """
    out = []
    for m in _TEMPLATE_OPEN.finditer(text):
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
        out.append((m.start(), close + 1, m.group(1), tuple(args)))
    return out


def _find_template_use(text, tnames):
    """The first *innermost* `Name<..>` use in `text`, or None."""
    for hit in _iter_template_uses(text, tnames):
        return hit
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
    # Every innermost use is rewritten per pass rather than one, so a pass
    # costs a single scan and the number of passes is the *nesting depth* --
    # two or three -- instead of the number of instantiations. Rewriting one
    # at a time rescanned the whole file per use, which is what made
    # `litehtml/src/document.cpp` take minutes: it names some seven thousand
    # uses, so the scan ran seven thousand times over a file that only ever
    # nests two deep.
    #
    # Doing them together is safe because the uses in one scan cannot
    # overlap (see `_iter_template_uses`) and none of them contains another,
    # so no rewrite can invalidate a later hit from the same scan. An *outer*
    # use only becomes innermost once its argument is mangled, which is the
    # next pass -- exactly the order the one-at-a-time loop produced.
    #
    # The bound scales with the file rather than sitting at a constant, and
    # is only a backstop: what actually distinguishes a loop is *progress* --
    # a pass that leaves the text unchanged would spin forever -- so that is
    # checked directly.
    for _ in range(max(1000, len(text))):
        out, last = [], 0
        for start, end, name, targs in _iter_template_uses(text, tnames):
            if start < last:
                continue              # already covered by an earlier rewrite
            if record is not None:
                record(name, targs)
            if known is not None and tuple(targs) not in known.get(name, ()):
                raise CppError(
                    "`%s<%s>` is instantiated from inside another template, "
                    "which this lowering cannot discover. Name it at file "
                    "scope as well (`%s x;`) so it is emitted."
                    % (name, ", ".join(targs), _mono_name(name, targs)))
            out.append(text[last:start])
            out.append(_mono_name(name, targs))
            last = end
        if not out:
            return text
        out.append(text[last:])
        rewritten = "".join(out)
        if rewritten == text:
            raise CppError("template instantiation did not terminate")
        text = rewritten
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

    Directive lines are blanked too, continuations included, and for the
    same reason: a `#define` body is not code this file evaluates. coost's
    `DISALLOW_COPY_AND_ASSIGN(T)` macro spells `T(const T&) = delete;`
    across continuation lines, and the reference-lowering rule matched into
    it -- consuming the `#define` head and leaving the last continuation
    orphaned in the output, where the delete handler then read it as a
    statement. The directives themselves still reach the output untouched;
    only the *matching* is blind to them.
    """
    look = _blank_directives(_blank_strings(_strip_comments(text)))
    out, pos = [], 0
    for m in pattern.finditer(look):
        out.append(text[pos:m.start()])
        out.append(repl(m) if callable(repl) else m.expand(repl))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _split_array_dim(decl):
    """`("T d", "[R * C]")` -- the declarator, and its array suffix.

    The suffix is taken off *before* the declarator is tokenised, because
    tokenising splits on `*` to find pointer stars and an array bound may
    contain one: `T d[R * C]` split that way gives `["T", "d[R", "*", "C]"]`,
    whose last token is `C]` and whose type is `T d[R *`. That is how a
    non-type parameter in a bound came out half-substituted -- `R` was in
    the type and got substituted, `C` was in the *name* and did not, so
    `Mat<float,4,3>` emitted `float d[4 * C];` and the C front end reported
    an undeclared `C` in a struct the source never mentioned.

    Bounds nest (`T d[A][B]`), so everything from the first top-level `[`
    to the end of the declarator is the suffix.
    """
    depth = 0
    for k, c in enumerate(decl):
        if c in "(<":
            depth += 1
        elif c in ")>":
            depth = max(0, depth - 1)
        elif c == "[" and depth == 0:
            return decl[:k].rstrip(), decl[k:].strip()
    return decl, ""


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


def _subst_injected(text, name, mangled):
    """`Vec` -> `Vec_float_8` inside its own instantiated body.

    C++ calls this the *injected class name*: inside `template<class T, int
    N> class Vec`, a bare `Vec` means `Vec<T, N>`. Nothing in the parameter
    substitution covers it -- `Vec` is not a template parameter -- so a
    class whose own methods name their class came out referring to a type C
    has never heard of:

        static Vec Vec_float_8__binadd(Vec_float_8 *this, const Vec *o);

    which is `unknown type name 'Vec'`. It bites in return position, in a
    parameter, and on a local, so every operator on a fixed-size matrix or
    vector hit it on the first line. The workaround was to spell `Vec<T,N>`
    at every occurrence, which no realistic header does.

    A use that *is* spelled `Vec<..>` is left alone: by the time this runs
    the arguments have been substituted, so it reads `Vec<float, 8>` and is
    the ordinary template use that `_monomorphise_uses` mangles. Rewriting
    it here would produce `Vec_float_8<float, 8>`.
    """
    if not name or name == mangled:
        return text
    return _sub_code(
        re.compile(r"\b%s\b(?!\s*<)" % re.escape(name)),
        lambda m: mangled, text)


#: Bytes per element, for working out how many fit an SSE lane. Only the
#: spellings a fixed-size array parameter can actually have.
_ELEM_BYTES = {
    "char": 1, "signed char": 1, "unsigned char": 1, "bool": 1,
    "short": 2, "unsigned short": 2,
    "int": 4, "unsigned": 4, "unsigned int": 4, "float": 4,
    "long": 8, "unsigned long": 8, "long long": 8,
    "unsigned long long": 8, "double": 8,
}

#: `float a[16]` in a parameter list: a scalar element and a literal bound.
_FIXED_ARRAY_PARAM = re.compile(
    r"^\s*(?:const\s+)?([A-Za-z_][\w ]*?)\s+(\w+)\s*\[\s*(\d+)\s*\]\s*$")

_DEF_HEAD = re.compile(
    r"(?<![\w])(?:static\s+)?[A-Za-z_][\w ]*?[\w\s\*]\s*(\w+)\s*\(")


#: `struct Vec_float_16 { float d[16]; };` -- a struct whose whole content
#: is one fixed-size scalar array, which is what a fixed-size vector or
#: matrix lowers to.
_VALUE_STRUCT = re.compile(
    r"(?<![\w])struct\s+(\w+)\s*\{\s*([A-Za-z_][\w ]*?)\s+\w+\s*"
    r"\[\s*([\d\s*+]+?)\s*\]\s*;\s*\}")

#: `const Vec_float_16 *o` / `Vec_float_16 *this` in a parameter list.
_VALUE_STRUCT_PARAM = re.compile(
    r"^\s*(?:const\s+)?(?:struct\s+)?(\w+)\s*\*\s*(\w+)\s*$")


def _value_structs(text):
    """`{struct name: (element spelling, element count)}` for every struct
    that is one fixed-size scalar array.

    A `Vec<T,N>` lowers to exactly this, so the element count of a method's
    receiver and operand is on the page -- which means the contracts can be
    inferred for an operator the same way they already are for a kernel
    taking `float a[16]`. That matters more than it looks: contracts are a
    ShivyCX extension, so a header that *writes* them is not C++ and does
    not compile under g++. Inferring them instead keeps the library
    ordinary C++ that happens to vectorize when ShivyCX compiles it.
    """
    out = {}
    for m in _VALUE_STRUCT.finditer(text):
        elem = " ".join(m.group(2).split())
        if elem not in _ELEM_BYTES:
            continue
        try:
            count = int(eval(m.group(3), {"__builtins__": {}}, {}))
        except Exception:
            continue
        if count > 0:
            out[m.group(1)] = (elem, count)
    return out


def _auto_contracts(text):
    """Derive ShivyCX contract clauses from fixed-size array parameters.

    This is the whole point of putting the size in the type. `Mat<T,R,C>`
    monomorphises to a kernel taking `float d[16]`, and 16 is a multiple of
    the four floats an SSE lane holds -- so the divisibility the contract
    vectorizer needs to drop its scalar remainder is *already known*, and
    making the author write it out again as an `assert` would be asking
    them to restate what they just said in the template argument.

    Ported from `tools/py2c.py::_auto_contracts`, which does the same thing
    on the rpython side from `x: "f32[256]"`. The two now agree: annotate a
    fixed size in either language and the asserts disappear.

    Only *definitions* with a body, only scalar elements, and only where
    the bound divides the lane count -- anything else is left exactly as
    written. A function that already carries clauses is left alone by the
    same check -- what follows its parameter list is an `assert`, not a
    `{` -- and that is the right answer, since the author said something
    more specific than this pass can infer.
    """
    out, pos = [], 0
    look = _blank_directives(_blank_strings(_strip_comments(text)))
    vstructs = _value_structs(look)
    for m in _DEF_HEAD.finditer(look):
        if m.start() < pos:
            continue
        op = m.end() - 1
        cp = _match_paren(look, op)
        if cp is None:
            continue
        # A definition, not a prototype or a call: what follows the
        # parameter list has to be the body.
        k = cp + 1
        while k < len(look) and look[k] in " \t\r\n":
            k += 1
        if k >= len(look) or look[k] != "{":
            continue
        params = text[op + 1:cp]
        if "[" not in params and not vstructs:
            continue
        clauses = []
        for part in _split_top(params):
            pm = _FIXED_ARRAY_PARAM.match(part)
            if pm is not None:
                elem = " ".join(pm.group(1).split())
                size = int(pm.group(3))
                name = pm.group(2)
            else:
                # A pointer to a value struct: the receiver or operand of an
                # operator on a fixed-size vector.
                sm = _VALUE_STRUCT_PARAM.match(part)
                got = vstructs.get(sm.group(1)) if sm is not None else None
                if got is None:
                    continue
                elem, size = got
                name = sm.group(2)
            width = _ELEM_BYTES.get(elem)
            if width is None:
                continue
            lanes = 16 // width
            # A bound that does not fill a whole lane has nothing to
            # promise, and one that is not a multiple of the lane count is
            # exactly the case the scalar remainder exists for.
            if lanes < 2 or size < lanes or size % lanes:
                continue
            clauses.append("assert not len(%s) %% %d" % (name, lanes))
            clauses.append("assert len(%s) >= %d" % (name, lanes))
        if not clauses:
            continue
        out.append(text[pos:cp + 1])
        out.append("\n" + "\n".join(clauses) + "\n")
        pos = cp + 1
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)


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
    # Scalars too, not just the classes of this unit. `int &get()` lowered
    # to `int_&get`, which is not an identifier -- the rejection this
    # function exists for was documented but only ever applied to class
    # types, so a reference return of a built-in type produced invalid C
    # with no diagnostic at all.
    #
    # `operator[]` is exempt without being special-cased: the name pattern
    # below wants a word followed by `(`, and `operator[](` is not one. A
    # reference return is *required* there, which is why it must not be
    # caught here.
    names = set(names) | set(_SCALAR_TYPES)
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
            line = _src_line(scan, m.start())
            raise CppError(
                "%s:%d: `%s&` return type is not in the C++ subset -- return "
                "`%s *` explicitly. Reference *parameters* are supported."
                % (os.path.basename(path), line, m.group(1), m.group(1)))


def _strip_default_args(params):
    """Drop `= expr` from each parameter of a parameter list.

    C has no default arguments, so one that survives into a prototype is a
    syntax error there rather than a value nobody passes. A member *defined*
    in this file loses them on the way to its definition; a member only
    **declared** here did not, so litehtml's

        void fromString(const tstring &str, const tstring &predefs = _t(""),
                        int defValue = 0);

    reached the C front end with the defaults intact and stopped it at the
    first `=`. That declaration is in `css_length.h`, which most of the
    tree includes.

    Split on top-level commas, since a default may be a call with commas of
    its own, and cut at the first `=` that is not part of `==`, `<=`, `>=`
    or `!=` -- a default argument is an expression and may compare.
    """
    out = []
    for part in _split_top(params or ""):
        depth = 0
        cut = -1
        for k, c in enumerate(part):
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == "=" and depth == 0:
                if part[k + 1:k + 2] == "=" or part[k - 1:k] in ("=", "!",
                                                                 "<", ">"):
                    continue
                cut = k
                break
        out.append(part[:cut] if cut >= 0 else part)
    return ", ".join(p.strip() for p in out if p.strip())


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
            "star": None, "augassign": {}, "cmp": {}, "binop": {},
            "conv": None,
            "vcall": {},
            "dropfn": dropfn, "external": True}


# The member a derived class stores its base in. Written once here because
# both the path builder and the name walker have to agree on it.
_BASE_MEMBER = "_base"


def _ivptr(bname):
    """The vptr field a secondary base contributes. Named after the base so
    a class may have several, and prefixed so it cannot collide with a
    member the author wrote."""
    return "_vptr_%s" % bname


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


#: The digest version this reads. A file claiming another one is refused
#: rather than guessed at: the whole point of the artifact is that both
#: sides agree on a layout, so a disagreement about its shape is exactly
#: the thing not to paper over.
DECLS_VERSION = 1


def load_decls(paths):
    """Read class-interface digests into `cinfo` entries.

    A class from a digest is *external*: its struct, its descriptor and its
    methods are emitted by the other language and are already in this
    translation unit, because the `.py` was spliced in above. What this
    needs from the digest is only enough to inherit: the fields (so a
    derived struct lays out behind them), the descriptor symbol (so the
    base chain links), and the virtual slot names (so an override is known
    to be one).

    Slot *order* is deliberately not used. A derived table is emitted with
    designated initializers, so the C compiler places each slot by name in
    whatever order the other language's descriptor declares -- which means
    a reordering there cannot silently produce a wrong indirect call here.
    The order is still carried in the digest, because a future consumer
    that must emit a positional table will need it.
    """
    import json
    out = {}
    for path in paths or ():
        try:
            with open(path) as f:
                d = json.load(f)
        except (IOError, ValueError) as e:
            raise CppError("--decls %s: cannot read it (%s)" % (path, e))
        if d.get("version") != DECLS_VERSION:
            raise CppError(
                "--decls %s: this is version %r and cpprust reads version "
                "%d. Regenerate it with the py2c that matches this tree."
                % (path, d.get("version"), DECLS_VERSION))
        if (d.get("lang") or "rpython") == "cpp":
            raise CppError(
                "--decls %s: this digest describes C++ classes, and cpprust "
                "cannot yet inherit from one. A C++ base needs its "
                "`struct <Base>_vtable` declared here to build a derived "
                "table, and a digest carries the descriptor's *layout* "
                "rather than that per-class type. The C++ digest exists for "
                "the other consumer -- py2c, so an rpython class can "
                "subclass a C++ one. Include the header instead." % path)
        desc = d.get("descriptor") or {}
        vnames = set(sl["name"] for sl in desc.get("slots") or ())
        for c in d.get("classes") or ():
            slots = []
            for sl in desc.get("slots") or ():
                # The declaring class is the hierarchy root as far as this
                # side is concerned; `impl` is a name, not a class here,
                # since the other language already emitted the function.
                slots.append({"name": sl["name"], "decl": "Obj",
                              "ret": sl["ret"], "params": "", "pure": False,
                              "impl": c["struct"], "external": True})
            methods = {}
            for m in c.get("methods") or ():
                if m["name"] == "__init__":
                    continue
                methods[m["name"]] = {0: {
                    "refs": set(), "owner": c["struct"], "ret": "int",
                    "virtual": m["name"] in vnames, "decl": c["struct"],
                    "fn": m["fn"]}}
            out[c["struct"]] = {
                "ctor": True, "dtor": False,
                "ctors": {}, "methods": methods,
                "fields": dict((f["name"], (f["ctype"], False))
                               for f in c.get("fields") or ()),
                "paths": dict((f["name"], f["name"])
                              for f in c.get("fields") or ()),
                "base": c.get("base"), "slots": slots,
                "root": c["struct"], "abstract": False,
                "vdtor": False, "vdtor_decl": None, "ctor_refs": set(),
                "copy": False, "move": False, "assign": False,
                "moveassign": False, "move_methods": {}, "deleted": {},
                "index": None, "arrow": None, "ibases": [],
                "ibases_all": [],
                # What marks this as somebody else's class. Every emission
                # path checks it before writing a struct, a table or a
                # function: the other language wrote all three already.
                "external": True,
                "lang": d.get("lang") or "rpython",
                "typeinfo": c["typeinfo"],
                "descriptor": desc.get("type") or "TypeInfo",
                "vslots": sorted(vnames),
            }
    return out


def _ext_descriptor(cls, cinfo):
    """The descriptor type of `cls`'s hierarchy if it is rooted in another
    language, else None. Walking the chain rather than reading one flag
    because a C++ class three levels below an rpython base is as much part
    of that hierarchy as its immediate child."""
    seen = set()
    while cls in cinfo and cls not in seen:
        seen.add(cls)
        info = cinfo[cls]
        if info.get("external"):
            return info.get("descriptor") or "TypeInfo"
        cls = info.get("base")
    return None


def _publish_decls_linkage(text, digest, rtti):
    """Give the digest's symbols external linkage, and alias the descriptor.

    Two rewrites, both keyed on the digest so nothing unpublished changes:

    * `static <ret> <fn>(` -> `<ret> <fn>(` for every function the digest
      names, plus each class's `_new` and `_drop`, in both the prototype
      and the definition.
    * `static const struct C_vtable C__vtable` -> external, plus a gcc
      alias `C_type` for it -- because py2c links a derived class's base
      chain through `extern const TypeInfo C_type`, and the alias is what
      lets that resolve to the vtable object without a second copy of it.
      The layouts agree by the pinned field-order test; the alias makes
      them agree by *address* too.
    """
    names = set()
    for c in digest.get("classes") or ():
        cname = c["struct"]
        names.add("%s_new" % cname)
        names.add("%s_drop" % cname)
        for m in c.get("methods") or ():
            names.add(m["fn"])
    for fn in sorted(names):
        text = re.sub(r"\bstatic (\w[\w \*]*?%s\()" % re.escape(fn),
                      r"\1", text)
    if rtti:
        for c in digest.get("classes") or ():
            cname = c["struct"]
            vt = "static const struct %s_vtable %s__vtable" % (cname, cname)
            if vt in text:
                text = text.replace(vt, vt[len("static "):], 1)
                anchor = "const struct %s_vtable %s__vtable" % (cname, cname)
                idx = text.find(anchor)
                end = text.find(";", idx)
                alias = ("\nextern const struct %s_vtable %s_type "
                         "__attribute__((alias(\"%s__vtable\")));"
                         % (cname, cname, cname))
                text = text[:end + 1] + alias + text[end + 1:]
            ti = ("static const struct _CppTypeInfo %s__typeinfo"
                  % cname)
            if ti in text:
                text = text.replace(ti, ti[len("static "):], 1)
                anchor = ("const struct _CppTypeInfo %s__typeinfo" % cname)
                idx = text.find(anchor)
                end = text.find(";", idx)
                alias = ("\nextern const struct _CppTypeInfo %s_type "
                         "__attribute__((alias(\"%s__typeinfo\")));"
                         % (cname, cname))
                text = text[:end + 1] + alias + text[end + 1:]
    return text


def dump_decls(cinfo, module):
    """The digest for the classes *this* translation defines.

    The mirror of `load_decls`, and deliberately the same shape: one
    artifact, two producers, two consumers. What differs is only what the
    two languages call things -- `lang` says which, so a consumer knows
    whether a base constructor is spelled `T_new` or `T___init__`.

    External classes are skipped. They came from somebody else's digest and
    re-publishing them would make two files claim to define one class,
    which is how a base chain acquires two roots.

    The slot list goes under `descriptor`, once, exactly as py2c writes it
    -- but here it is genuinely per hierarchy rather than per module, so a
    file with two unrelated hierarchies publishes the union. A consumer
    reads slots by name, so a name it does not implement is simply absent
    from its table; the union costs it nothing.
    """
    slots, seen = [], set()
    classes = []
    for cname in sorted(cinfo):
        info = cinfo[cname]
        if info.get("external") or info.get("declared_only"):
            continue
        for sl in info.get("slots") or ():
            if sl["name"] in seen or sl["name"] == _DTOR_SLOT:
                continue
            seen.add(sl["name"])
            slots.append({"name": sl["name"], "ret": sl["ret"],
                          "params": [p.strip() for p in
                                     _split_top(sl["params"]) if p.strip()]})
        methods = []
        for mname in sorted(info.get("methods") or {}):
            entries = info["methods"][mname]
            ent = list(entries.values())[0] if entries else {}
            methods.append({"name": mname,
                            "virtual": bool(ent.get("virtual")),
                            "fn": ent.get("fn") or "%s_%s" % (cname, mname)})
        classes.append({
            "name": cname,
            "struct": cname,
            "base": info.get("base"),
            "ibases": list(info.get("ibases") or ()),
            # A concrete class *is* its own descriptor -- the vtable's
            # header prefix -- so the symbol is the table. An abstract one
            # has a standalone header object instead. Same two spellings
            # `_typeinfo_ref` picks between, published so a consumer need
            # not know the rule.
            "typeinfo": ("%s__typeinfo" % cname if info.get("abstract")
                         else "%s__vtable" % cname),
            "abstract": bool(info.get("abstract")),
            "fields": [{"name": f, "ctype": t}
                       for f, (t, _p) in sorted(
                           (info.get("fields") or {}).items())],
            "methods": methods,
        })
    return {"version": DECLS_VERSION, "lang": "cpp", "module": module,
            "descriptor": {"type": "_CppTypeInfo",
                           "header": [r.split()[-1].rstrip(";").lstrip("*")
                                      for r in _RTTI_ROWS],
                           "slots": sorted(slots, key=lambda x: x["name"])},
            "classes": classes}


def _ext_ctor(base, info):
    """The symbol that initialises an external base in place.

    rpython splits the two: `Cls_new` arena-allocates *and* stamps the
    descriptor, while `Cls___init__` only assigns fields -- and a C++
    constructor chaining into it stamps the descriptor itself afterwards,
    because there is nowhere else that would happen. A C++ base from another
    translation has no such split, so its `Cls_new` is the initialiser.
    """
    if (info.get("lang") or "rpython") == "cpp":
        return "%s_new" % base
    return "%s___init__" % base


def _ext_lang(cls, cinfo):
    """The language of the hierarchy root of `cls`, or None if it is local."""
    seen = set()
    while cls in cinfo and cls not in seen:
        seen.add(cls)
        info = cinfo[cls]
        if info.get("external"):
            return info.get("lang") or "rpython"
        cls = info.get("base")
    return None


def _find_impl(mname, cls, cname, base_info):
    """The C function this class supplies for an interface slot, or None.

    Looked for on the class itself first, then along its *primary* chain --
    which is where an inherited implementation lives, since the primary
    chain is the one with storage. A secondary base's own implementation is
    not searched: it is what `None` means here, and the caller uses the
    slot's recorded implementation for that case, unadjusted.

    A destructor is not looked up by name. An interface's destructor slot
    is filled by the class's own `_drop`, which the epilogue emits whether
    or not the author wrote one.
    """
    if mname == _DTOR_SLOT:
        return "%s_drop" % cname
    for m in cls.members:
        if m.kind == "method" and m.name == mname:
            return "%s_%s" % (cname, mname)
    if base_info and mname in base_info.get("methods", {}):
        # Inherited through the layout base. Its `this` is a prefix of this
        # class's, so the address the thunk computes is right for it too
        # and no second adjustment is needed. The arity map holds one entry
        # per overload; an interface slot is one name, so a single entry is
        # the only shape that can fill it.
        overloads = base_info["methods"][mname]
        if len(overloads) == 1:
            fn = list(overloads.values())[0].get("fn")
            if fn:
                return fn
    return None


#: The header rows prefixed onto a vtable struct under `--rtti`, in
#: `_CppTypeInfo` order. The slots follow, so a dispatch site -- which names
#: a slot rather than indexing one -- is unchanged by their presence, and a
#: derived table stays prefix-compatible with its base's exactly as before.
_RTTI_ROWS = ["const char *name;",
              "const struct _CppTypeInfo *base;",
              "const void *fields;",
              "const void *tostr;",
              "const void *eq;",
              "const void *addfn;",
              "unsigned long objsize;"]


def _typeinfo_ref(cname, info):
    """The C expression for a pointer to `cname`'s descriptor.

    A concrete class *is* its own descriptor: the vtable's header prefix is
    the descriptor, and the vptr already points at it, so no second object
    is needed and the cast is free.

    An abstract class has no vtable instance -- nothing may be constructed
    to point at one -- but a derived class still has to name it as its base,
    and `dynamic_cast<Abstract *>` is a legitimate question to ask. So it
    gets a standalone header object instead. Two spellings, one meaning.
    """
    if info.get("abstract"):
        return "&%s__typeinfo" % cname
    return "(const struct _CppTypeInfo *)&%s__vtable" % cname


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
                chained=frozenset(), prelude=False, rtti=False):
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
    cname = cls.name if targs is None else _mono_name(cls.name, targs)
    sub = ((lambda s: _subst_injected(
                _subst_type(s, cls.tparams, targs), cls.name, cname))
           if targs else (lambda s: s))
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

    # Secondary bases. The layout base is a struct prefix; these are not.
    # Each contributes one word -- a vptr of its own -- at an offset fixed
    # by the declaration, so `(I *)d` is a constant adjustment and the
    # thunks in `I`'s table subtract the same constant to get `this` back.
    # That is the whole of the mechanism, and it is only sound while the
    # base carries no data: a field reached through `I *` would be read at
    # `I`'s offset, and there is no `I` there to read it from.
    extras = []
    for bn in cls.extra_bases:
        rn = bn if bn in known else tsub(sub(bn)).strip()
        if rn not in known:
            raise CppError(
                "class %s: base class `%s` is not defined above it. A base "
                "has to be complete before the class that inherits it."
                % (cls.name, rn))
        binfo = known[rn]
        if binfo.get("fields"):
            raise CppError(
                "class %s: secondary base `%s` has data members (%s), which "
                "is not in the C++ subset. Only the first base is laid out "
                "as a struct prefix; the others are reached through a vptr "
                "of their own, so their fields would have no storage to sit "
                "in. Make `%s` an interface (methods only), or make it the "
                "first base."
                % (cls.name, rn, ", ".join(sorted(binfo["fields"])), rn))
        if not binfo.get("slots"):
            raise CppError(
                "class %s: secondary base `%s` has no virtual methods, so "
                "inheriting it adds nothing that could be dispatched and "
                "nothing that could be laid out. Give it a virtual method, "
                "or drop it from the base list."
                % (cls.name, rn))
        extras.append((rn, binfo))
    # An interface reached through the layout base needs a table of *this*
    # class too: the field is declared once, by whoever named the base, but
    # the table it points at has to carry this class's overrides. Without
    # this a derived class dispatches to its base's implementation through
    # the interface while dispatching to its own through the layout base --
    # the same object answering two different ways.
    all_extras = [(bn, bi, "") for bn, bi in extras]
    if base_info:
        for bn, bi, bpath in base_info.get("ibases_all") or ():
            if any(bn == x[0] for x in all_extras):
                continue
            all_extras.append((bn, bi, "%s.%s" % (_BASE_MEMBER, bpath)
                               if bpath else "%s." % _BASE_MEMBER))

    slots = _vtable_slots(cls, cname, base_info, known)
    # Is this hierarchy rooted in a class the other language emitted? If so
    # its descriptor type, its layout and its slot names are already fixed,
    # and this class joins them rather than defining its own.
    ext_root = None
    walk = base_info
    while walk is not None:
        if walk.get("external"):
            ext_root = walk
            break
        walk = known.get(walk.get("base")) if walk.get("base") else None
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
        rows = list(_RTTI_ROWS) if rtti else []
        for s in slots:
            args = "%s *this" % s["decl"]
            if s["params"]:
                args += ", " + s["params"]
            rows.append("%s (*%s)(%s);" % (s["ret"], s["name"], args))
        if not ext_root:
            head.append("struct %s_vtable { %s };" % (cname, " ".join(rows)))

    parts = []
    if base:
        parts.append("%s _base;" % base)
    elif slots:
        parts.append("const struct %s_vtable *_vptr;" % cname)
    # One vptr per secondary base, after the layout base and before this
    # class's own fields. Position is what makes the adjustment a constant,
    # so it is fixed here rather than left to fall out of declaration order.
    for bn, _bi in extras:
        parts.append("const struct %s_vtable *%s;" % (bn, _ivptr(bn)))
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
    # `static const` members, at file scope and qualified. They go *above*
    # the struct: one may be defined in terms of another, and a method body
    # refers to them by the qualified name.
    for _sm in cls.members:
        if _sm.kind != "sconst":
            continue
        _sinit = _sm.definit or "0"
        for _o in cls.members:
            if _o.kind == "sconst":
                _sinit = re.sub(r"(?<![\w.>])%s(?![\w])" % re.escape(_o.name),
                                "%s_%s" % (cname, _o.name), _sinit)
        head.append("static const %s %s_%s = %s;"
                    % (_sm.ret, cname, _sm.name, _sinit))
    head.append("struct %s { %s };" % (cname, " ".join(parts) or
                                       "char _cpp_empty;"))
    # An abstract class gets its descriptor as an object of its own, since it
    # will never have a vtable instance for one to sit in front of. It still
    # needs one: a derived class names it as its base, and asking
    # `dynamic_cast<Abstract *>` is legitimate.
    #
    # After the struct rather than beside the vtable type above, because it
    # is the first thing here to need the struct *complete* rather than
    # merely declared -- `objsize` is a `sizeof`.
    if rtti and abstract:
        head.append(
            "static const struct _CppTypeInfo %s__typeinfo = "
            "{ \"%s\", %s, 0, 0, 0, 0, sizeof(struct %s) };"
            % (cname, cname,
               (_typeinfo_ref(base, base_info)
                if base_info and base_info.get("slots") else "0"),
               cname))

    mnames = [m.name for m in cls.members if m.kind == "method"]
    if base_info:
        mnames = sorted(set(mnames) | set(base_info["methods"]))
    info = {"ctor": False, "dtor": False, "ctors": {}, "methods": {},
            "fields": {}, "base": base, "slots": slots, "root": root,
            "ibases": [bn for bn, _ in extras],
            "ibases_all": [(bn, bi, pth) for bn, bi, pth in all_extras],
            "abstract": abstract, "vdtor": False, "vdtor_decl": None,
            "ctor_refs": set(), "paths": {}, "copy": False, "move": False,
            "assign": False, "moveassign": False, "move_methods": {}, "deleted": {}, "index": None, "arrow": None,
            "star": None, "augassign": {}, "cmp": {}, "binop": {},
            "conv": None,
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
            if known[base].get("external"):
                # Which symbol depends on which language wrote the base:
                # rpython splits allocation from initialisation and names
                # the latter `Cls___init__`, while a C++ base emitted by
                # another translation is an ordinary `Cls_new`. This is what
                # `lang` is in the digest for. The arity is whatever that
                # function takes -- checked by the C compiler rather than
                # here, since the digest records a symbol, not a signature.
                pro += "%s(&this->_base%s); " % (
                    _ext_ctor(base, known[base]),
                    (", " + bargs) if bargs else "")
            elif known[base]["ctor"]:
                # `: base(std::move(s))` in a move constructor -- the C++
                # idiom for handing the base subobject to the base's own
                # move constructor. Routed to the base's move symbol with
                # the *source's* base subobject; the plain path below would
                # count it as a one-argument constructor call and refuse.
                mv = re.match(r"^__cpp_move\s*\(\s*(\w+)\s*\)$",
                              (bargs or "").strip())
                if mv is not None and known[base].get("move_fn"):
                    pro += "%s(&this->_base, &%s->_base); " % (
                        known[base]["move_fn"], mv.group(1))
                    bargs = None
                elif mv is not None:
                    raise CppError(
                        "class %s: `: %s(std::move(%s))` asks for a move "
                        "constructor `%s` does not declare."
                        % (cls.name, base, mv.group(1), base))
                else:
                    bar = _arity(bargs) if bargs is not None else 0
                    if bar not in known[base]["ctors"]:
                        raise CppError(
                            "class %s: base `%s` has no constructor taking "
                            "%d argument%s; pass them as `%s(..) : %s(..) "
                            "{ }`"
                            % (cls.name, base, bar, "" if bar == 1 else "s",
                               cls.name, base))
                    pro += "%s(&this->_base%s); " % (
                        known[base]["ctors"][bar]["fn"],
                        (", " + bargs) if bargs else "")
            elif bargs is not None:
                raise CppError("class %s: base `%s` has no constructor to "
                               "pass arguments to" % (cls.name, base))
        if slots and not abstract and ext_root \
                and ext_root.get("lang") != "cpp":
            # The rpython root spells its descriptor pointer `_hdr.type`
            # through `Obj`, not `_vptr`. Same word at the same offset --
            # which is the whole reason the two models meet -- but the
            # member has to be named the way that struct declares it. A C++
            # root from another translation declares `_vptr` like any
            # other, so it falls through to the ordinary path below.
            pro += ("((Obj *)this)->type = &%s__vtable; " % cname)
        elif slots and not abstract:
            pro += ("((%s *)this)->_vptr = "
                    "(const struct %s_vtable *)&%s__vtable; "
                    % (root, root, cname))
        # Each secondary base's vptr, beside the primary one. A `B *` taken
        # before this ran would dispatch through an uninitialised word, so
        # it is installed in the same prologue for the same reason.
        for _bn, _bi, _pth in all_extras:
            pro += ("this->%s%s = &%s__vtable_%s; "
                    % (_pth, _ivptr(_bn), cname, _bn))
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

    # Contract clauses for the member being emitted. A cell rather than an
    # argument because every operator kind reaches `emit` by its own path,
    # and a kernel written as an `operator*` deserves its contracts as much
    # as one written as a method.
    cur_contracts = [[]]

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
        # `static const` members are file-scope constants, not fields, so a
        # bare use in a body names `Class_name` rather than going through
        # `this`. Done after the field qualification above, which never
        # sees them -- they are not in `info["paths"]`.
        sconsts = [x.name for x in cls.members if x.kind == "sconst"]
        if sconsts:
            inner = _sub_code(
                re.compile(r"(?<![\w.>])(%s)\b" % _type_alt(sconsts)),
                lambda m: "%s_%s" % (cname, m.group(1)), inner)
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
        # ShivyCX contract clauses go between the parameter list and the
        # body, which is the same place they occupied in the C++ source --
        # the method-to-free-function rewrite prepends `this` and renames
        # nothing, so a clause naming a parameter still names it.
        cbits = ""
        if cur_contracts[0]:
            declared = set(_declared_param_names(params))
            for cl in cur_contracts[0]:
                unknown = _contract_names(cl) - declared
                if unknown:
                    raise CppError(
                        "class %s: `%s` on `%s` constrains %s, which is not "
                        "a parameter of it. A contract is proven at the call "
                        "site from the argument passed, so it can only speak "
                        "about parameters -- a field has no call site to be "
                        "proven at. Pass the array as a parameter, or put "
                        "the kernel in a free function."
                        % (cname, cl, mname, ", ".join(sorted(unknown))))
            cbits = "\n" + "\n".join(cur_contracts[0]) + "\n"
        (tail if emitting_outline[0] else out).append(
            "%s %s %s(%s)%s {%s}" % (stor, kind, mname, arglist, cbits,
                                     inner))
        return refs

    for m in cls.members:
        cur_contracts[0] = list(getattr(m, "contracts", ()) or ())
        if m.kind in ("field", "anon") or m.pure:
            continue
        if m.declared_only:
            # Prototype only. `emit` writes both, so the declaration is made
            # here and the definition left to whoever has the body.
            dparams = _lower_refs(_expand_cpp_ref(_expand_cpp_rref(sub(m.params or ""), names), names),
                                  _with_scalars(names))
            dparams = _strip_default_args(dparams)
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
            # The symbol, for a *derived* class whose own move constructor
            # writes `: base(std::move(s))` -- its prologue routes here.
            info["move_fn"] = "%s_move" % cname
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
            # A second `operator[]` lowers to the same `T__index` symbol
            # and the C is invalid -- the same collision `operator=` is
            # refused for. The usual pair is const/non-const with one body;
            # this pass tracks no constness, so the two are the same
            # function here and one of them says everything.
            if info["index"] is not None:
                raise CppError(
                    "class %s: two `operator[]` overloads lower to one "
                    "symbol (`%s__index`) and the second redefines the "
                    "first. A const/non-const pair is the same function to "
                    "this pass, which tracks no constness -- keep the "
                    "non-const one."
                    % (cls.name, cname))
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
            #
            # `Base::operator=(std::move(s))` in the body -- the delegation
            # idiom -- is refused rather than half-lowered: without this it
            # reached the C compiler as a `::`-scoped call on a
            # materialised temporary, wrong twice over (the base should
            # receive the *source*, and nothing destroys the temporary).
            # The base's fields being reachable (they must be, for the
            # derived move *constructor* to mean anything) the member-wise
            # steal says the same thing in the subset.
            if re.search(r"::\s*operator\s*=\s*\(", m.body or ""):
                raise CppError(
                    "class %s: a base-scoped `operator=` call is not in the "
                    "C++ subset. Write the move assignment member-wise -- "
                    "steal the base's fields and null the source -- as the "
                    "base's own move constructor does." % cls.name)
            emit("void", "%s__moveassign" % cname, params, sub(m.body or ""))
            info["moveassign"] = True
        elif m.kind == "assign":
            # Lowered to `T_assign(T *this, const T *o)`. Assignment is the
            # one place the subset needs a user hook: a struct copy of an
            # owning object leaves two owners, and there is no safe default.
            # `__assign`, not `_assign`: a class may perfectly well declare
            # a method called `assign`, and `string` does.
            #
            # A *second* `operator=` taking one argument lands on this same
            # symbol. The move overload above escapes that by having a
            # symbol of its own, but two copy-assignments differing only in
            # their operand type -- litehtml's `border` takes both a
            # `border` and a `css_border` -- have nothing to tell them
            # apart, and the emitted C redefined the first. Refused for the
            # reason any same-arity overload is: resolution here is by
            # argument count, and both take one.
            if info["assign"]:
                raise CppError(
                    "class %s: two `operator=` overloads take 1 argument. "
                    "Overloads are resolved by argument count here, so both "
                    "lower to `%s__assign` and the second would redefine "
                    "the first. Give one of them a name (`assign_from`) and "
                    "call it directly."
                    % (cls.name, cname))
            emit("void", "%s__assign" % cname, params, sub(m.body or ""))
            info["assign"] = True
        elif m.kind == "binop":
            op = m.name[len("operator"):]
            # A binary operator hands back a new object by value. That used
            # to be refused outright for a class with a destructor, and the
            # refusal was right at the time: a by-value return of an owning
            # class was not in the subset at all.
            #
            # It is now. A returned bare local is *moved out* -- left out of
            # the drops on that path -- so `T operator+(..) { T r; ..;
            # return r; }` lowers exactly as the same body under an ordinary
            # method name already did. The two spellings had drifted apart
            # for no reason left standing: `Buf plus(const Buf &)` was
            # emitted and `Buf operator+(const Buf &)` was refused.
            #
            # What is still owning-specific is the *chain*, handled below.
            fn = "%s__bin%s" % (cname, _BIN_NAMES[op])
            cret = tsub(sub(m.ret or "")).strip()
            # The class of the right operand, which need not be this one.
            # `Vec<T,R> operator*(const Vec<T,C> &)` on a `Mat` is how a
            # matrix multiplies a vector, and the rewriter used to require
            # both operands to be the same class -- so `A * x` matched
            # nothing and reached the C front end as a raw `*` on two
            # structs. Recording the declared operand type lets the call be
            # lowered against it instead of against the receiver.
            # `tsub` as well as `sub`: the parameter is spelled
            # `const Vec<T,C> &`, and substitution alone leaves
            # `Vec<float, 4>` -- a template *use*, which is not the name any
            # class is known by. Mangling it gives `Vec_float_4`, which is.
            _bp = _parse_param(
                _expand_cpp_ref(tsub(sub(m.params or "")), known),
                _with_scalars(names))
            info["binop"][op] = {
                "fn": fn, "ret": cret, "arg": _bp[0] if _bp else None,
                "refs": _ref_positions(_expand_cpp_ref(sub(m.params or ""),
                                                       known),
                                       _with_scalars(names))}
            emit(cret, fn, params, sub(m.body or ""))
            # A by-value front door, so a chain can nest.
            #
            # `a + b + c` is `(a + b) + c`, and the left operand of the
            # second `+` is the *result* of the first. C cannot take the
            # address of a function result, so the ordinary form -- which
            # wants `&lhs` -- has nowhere to point. This variant takes its
            # left operand by value instead, and a call to the ordinary
            # form can be passed straight into it.
            #
            # Safe precisely because this wrapper is only emitted for a
            # class that owns nothing: the by-value parameter is a struct
            # copy with no constructor or destructor to run, exactly as C++
            # would pass it. For an owning class the copy would make a
            # second owner of one buffer, so no wrapper is emitted and a
            # chain is refused at the call site instead -- `a + b` on its
            # own stays available, which is the common case and the one
            # `string` needs.
            if cret == cname and dtor is None:
                vfn = "%s_v" % fn
                info["binop"][op]["vfn"] = vfn
                mprotos.append("static %s %s(%s lhs, const %s *o);"
                               % (cname, vfn, cname, cname))
                (tail if emitting_outline[0] else out).append(
                    "static %s %s(%s lhs, const %s *o) "
                    "{ return %s(&lhs, o); }"
                    % (cname, vfn, cname, cname, fn))
            # A *both* by-value door, which is what a tree needs.
            #
            # `_v` above lets a chain nest to the left, because that is the
            # only direction a left-to-right fold ever nests. Precedence
            # nests to the right as well -- `a + b * c` is
            # `add(a, mul(b, c))` -- and the right operand of the ordinary
            # form is `const T *`, which a call result has no address for.
            # Taking both by value is the one spelling that composes in
            # either direction, so an operand may be a name or another
            # application without the rewriter caring which.
            #
            # Same ownership guard as `_v`, extended to the operand: a
            # by-value copy of an owning class would make a second owner,
            # and here there are two operands that could be one. Without
            # the wrapper the mixed-precedence refusal stands, which is the
            # behaviour an owning class had before.
            # The symmetric operator names its *own* class, which is being
            # emitted now and so is not in `known` yet -- looking it up
            # there found nothing and silently withheld the wrapper from
            # every `T operator+(const T &)` there is.
            _arg = info["binop"][op]["arg"]
            _argi = known.get(_arg) if _arg and _arg != cname else None
            if dtor is None and (_arg is None or _arg == cname
                                 or (_argi is not None
                                     and not _argi["dtor"])):
                _acn = info["binop"][op]["arg"] or cname
                vvfn = "%s_vv" % fn
                info["binop"][op]["vvfn"] = vvfn
                mprotos.append("static %s %s(%s lhs, %s o);"
                               % (cret, vvfn, cname, _acn))
                (tail if emitting_outline[0] else out).append(
                    "static %s %s(%s lhs, %s o) { return %s(&lhs, &o); }"
                    % (cret, vvfn, cname, _acn, fn))
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
            # Same collision `operator=` is refused for, and it had no
            # check here: coost's `fastring` declares `operator+=` for
            # `fastring`, `std::string`, `const char*` and `char`, and all
            # four lowered to one `fastring__augadd`. The header translated
            # without complaint and the emitted C would not compile, which
            # is the one outcome this pass exists to prevent.
            if op in info["augassign"]:
                raise CppError(
                    "class %s: two `operator%s=` overloads take 1 argument. "
                    "Overloads are resolved by argument count here, so both "
                    "lower to `%s` and the second would redefine the first. "
                    "Give one of them a name and call it directly."
                    % (cls.name, op, fn))
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
                # The lowered parameter list, kept so a by-value receiver
                # variant can repeat it. Only the *reference* positions
                # were recorded before, which is enough to fix up a call
                # but not to declare a forwarder.
                "params": _lower_refs(
                    _expand_cpp_ref(_expand_cpp_rref(params, names), names),
                    _with_scalars(names)),
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

    # By-value receiver variants, for methods the source invokes on a call
    # *result*. C cannot take the address of a function result, so
    # `o.make().get()` has no object to call `get` on -- the same wall the
    # binary operators hit for `a + b + c`, and the same way out: a variant
    # taking its receiver by value, which the result is passed straight
    # into.
    #
    # Only for a class that owns nothing. The receiver crosses the call
    # boundary as a struct copy, and for an owning class that would leave
    # two objects holding one resource -- which is the same condition the
    # by-value rules enforce everywhere else here.
    #
    # And only for the names actually chained onto: a variant per method
    # unconditionally would leave unused static functions all over the
    # output, exactly as the dispatch helpers below note.
    info["byval"] = {}
    if not info["dtor"]:
        for mname_, ars in info["methods"].items():
            if mname_ not in chained:
                continue
            for ar, ent in ars.items():
                if ent.get("virtual") or not ent.get("fn"):
                    continue
                vfn = "%s__byval_%s_%d" % (cname, mname_, ar)
                plist = (ent.get("params") or "").strip()
                fwd = "".join(
                    ", " + n for n in
                    (_param_name(x) for x in _split_top(plist)) if n)
                ret = (ent.get("ret") or "void").strip() or "void"
                proto = ("static %s %s(%s self%s);"
                         % (ret, vfn, cname,
                            (", " + plist) if plist.strip() else ""))
                if ret == "void":
                    body = "%s(&self%s);" % (ent["fn"], fwd)
                else:
                    body = ("%s _cpp_bv = %s(&self%s); return _cpp_bv;"
                            % (ret, ent["fn"], fwd))
                mprotos.append(proto)
                out.append("static %s %s(%s self%s) { %s }"
                           % (ret, vfn, cname,
                              (", " + plist) if plist.strip() else "",
                              body))
                info["byval"].setdefault(mname_, {})[ar] = vfn

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
        call = ("((const struct %s_vtable *)%s->_vptr)->%s(this%s)"
                % (cname, vptr, s["name"],
                   "".join(", " + f for f in fwd)))
        if s["ret"].strip() == "void":
            body = "%s;" % call
        else:
            # Through a local rather than `return <call>;`. A by-value return
            # of an owning class is checked for being a bare local -- a
            # returned local is moved out, an expression has nothing to move
            # from -- and this forwarder was tripping that check on its own
            # generated code. Forwarding a callee's return value is a pure
            # move, so the local says exactly that, and the check stays
            # general rather than growing an exemption.
            body = ("%s _cpp_vr = %s; return _cpp_vr;" % (s["ret"], call))
        out.append("static %s %s(%s *this%s) { %s }"
                   % (s["ret"], helper, cname, plist, body))
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
        # The descriptor values come first because the header rows do. The
        # base link is the *nearest base that has a table*: a base with no
        # virtuals has no descriptor to point at, and a chain that skipped
        # it would claim a relationship the layout does not have.
        if ext_root:
            # Designated initializers, so the *other* language's field
            # order decides the layout and a reordering there cannot
            # silently produce a wrong indirect call here. `base` links
            # into its chain, which is what makes `isinstance` and
            # `dynamic_cast` work across the boundary.
            named = [".name = \"%s\"" % cname,
                     ".base = (const struct %s *)&%s"
                     % (ext_root["descriptor"], base_info["typeinfo"]
                        if base_info.get("external")
                        else "%s__vtable" % base),
                     ".objsize = sizeof(struct %s)" % cname]
            for sl, e in zip(slots, entries):
                if sl["name"] == _DTOR_SLOT:
                    continue
                named.append(".%s = &%s" % (sl["name"], e))
            head.extend(protos)
            head.append("static const %s %s__vtable = { %s };"
                        % (ext_root["descriptor"], cname, ", ".join(named)))
            out.extend(thunks)
        else:
            vals = []
            if rtti:
                bref = "0"
                if base_info and base_info.get("slots"):
                    bref = _typeinfo_ref(base, base_info)
                vals = ["\"%s\"" % cname, bref, "0", "0", "0", "0",
                        "sizeof(struct %s)" % cname]
            head.extend(protos)
            head.append("static const struct %s_vtable %s__vtable = { %s };"
                        % (cname, cname,
                           ", ".join(vals + ["&" + e for e in entries])))
            out.extend(thunks)

    # A table per secondary base. Emitted whether or not this class has a
    # primary table of its own: a class may implement an interface and
    # declare no virtual of its own, and the interface still has to
    # dispatch.
    for bn, binfo, ipath in all_extras:
        bentries, bprotos, bthunks = [], [], []
        for sl in binfo["slots"]:
            plist = (", " + sl["params"]) if sl["params"].strip() else ""
            fwd = [n for n in (_param_name(x)
                               for x in _split_top(sl["params"])) if n]
            own = _find_impl(sl["name"], cls, cname, base_info)
            if own is None:
                if sl["impl"] is None:
                    raise CppError(
                        "class %s: `%s::%s` is pure and %s does not "
                        "implement it, so there is nothing to put in the "
                        "table. Declaring a value of such a class is "
                        "refused anyway, so this is a gap rather than an "
                        "abstract class."
                        % (cls.name, bn, sl["name"], cls.name))
                # The base implements it and `this` is already a `B *`,
                # so the slot takes the base's function unadjusted.
                bentries.append(_slot_fn(sl, sl["impl"]))
                continue
            # This class overrides it. The slot hands over a `B *` pointing
            # at the vptr field; the implementation wants the whole object,
            # which is that address less the field's offset.
            thunk = "%s__ithunk_%s_%s" % (cname, bn, sl["name"])
            ret = "" if sl["ret"].strip() == "void" else "return "
            bprotos.append("static %s %s(%s *this%s);"
                           % (sl["ret"], thunk, bn, plist))
            bthunks.append(
                "static %s %s(%s *this%s) { %s%s((%s *)((char *)this - "
                "offsetof(struct %s, %s%s))%s); }"
                % (sl["ret"], thunk, bn, plist, ret, own, cname,
                   cname, ipath, _ivptr(bn),
                   "".join(", " + f for f in fwd)))
            bentries.append(thunk)
        bvals = []
        if rtti:
            # The descriptor names *this* class, not the interface: asking
            # a `B *` what it is should answer with what it really is. Its
            # base is the interface, which is the relationship a
            # `dynamic_cast<B *>` walks.
            bvals = ["\"%s\"" % cname, _typeinfo_ref(bn, binfo),
                     "0", "0", "0", "0", "sizeof(struct %s)" % cname]
        head.extend(bprotos)
        head.append("static const struct %s_vtable %s__vtable_%s = { %s };"
                    % (bn, cname, bn,
                       ", ".join(bvals + ["&" + e for e in bentries])))
        out.extend(bthunks)
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
_DIRECTIVE_LINE = re.compile(r"(?m)^[ \t]*#.*$")


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
    # Bounds only -- the text before the declaration is never sliced out.
    # This runs once per brace at file scope, and copying the whole prefix
    # each time is what turned a 1175-line file into minutes of work.
    stop = open_paren
    while stop > 0 and text[stop - 1] in " \t\r\n":
        stop -= 1
    cut = -1
    for ch in ";}{):":
        cut = max(cut, text.rfind(ch, 0, stop))
    # A preprocessor directive is not part of a declaration, and contains
    # none of the characters above -- so `#include <stdio.h>` immediately
    # before `int main()` was read as part of the return type, and the
    # spilled temporary came out as `#include <stdio.h> int _cpp_ret0`.
    # A directive ends at its newline; nothing up to there belongs here.
    # Only the line `cut` sits on can carry one past it: every earlier
    # directive ends at a newline that is itself before `cut`.
    lo = text.rfind("\n", 0, max(cut, 0)) + 1
    for pm in _DIRECTIVE_LINE.finditer(text, lo, stop):
        cut = max(cut, pm.end())
    decl = text[cut + 1:stop].strip()
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
    __slots__ = ("live", "kind", "ret", "vals", "ptrs", "ptrvals")

    def __init__(self, kind, ret):
        self.live = []        # (ctype, vname), in declaration order
        self.kind = kind      # "file" | "func" | "loop" | "switch" | "block"
        self.ret = ret        # enclosing function's return type
        self.vals = {}        # class-typed locals: vname -> class
        # Names in `vals` that are already pointers -- a reference parameter
        # after lowering, and `this`. They name an object just as a local
        # does, but reaching it needs a dereference rather than an address.
        self.ptrs = set()
        # Pointer locals of class type, kept apart from `vals`. The name
        # walker needs them; the copy and assignment handlers must not
        # see them, because `p = q` on two pointers is a pointer
        # assignment, not a class one -- putting them in `vals` made the
        # supplied containers' own `T *nd = realloc(..)` look like a
        # class assignment and broke them.
        self.ptrvals = {}


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
    # `*p` where `p` is a pointer local to a class. The object is the
    # pointee, and its address is `p` itself -- so this hands back `(*p)`
    # and the `&` every caller puts on it folds straight back to `p`.
    #
    # Without this, the supplied `accumulate` could not write `sum += *it`
    # over a class element: `*it` named nothing, and the diagnostic pointed
    # at a line inside a supplied template that the author never wrote.
    deref_m = re.match(r"^\*\s*(\w+)$", expr)
    if deref_m is not None:
        nm = deref_m.group(1)
        for fr in reversed(scopes):
            if nm in fr.ptrvals:
                return ("(*%s)" % nm, fr.ptrvals[nm])
            if nm in fr.vals:
                break            # a value local: `*v` is not the object
        return None
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
        # Only when something is reached *through* it. A pointer local named
        # on its own is a pointer, not the object: `p = q` is a pointer
        # assignment, and resolving it to the pointee's class made the
        # assignment handler demand an `operator=` for a copy that is not
        # happening.
        if len(parts) > 1 and parts[0] in fr.ptrvals:
            cls = fr.ptrvals[parts[0]]
            is_ptr_base = True
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
        # `_base` is the synthesized hop to the base class, not a declared
        # field, so it is not in `fields` -- an inherited field is flattened
        # into the derived class's `fields` under its own name instead. But
        # field qualification has already rewritten the body, and it writes
        # the *path*: a method that said `m_children` reaches here as
        # `this->_base.m_children`. Walking that path meant stepping through
        # a hop this loop could not name, so no inherited field of an owning
        # type could be named at all -- `return m_children[idx];` in a
        # derived class was refused for that reason and no other. Stepping
        # into the base keeps the rest of the walk unchanged, and nests for
        # `_base._base.x`.
        if (info is not None and fld == _BASE_MEMBER
                and fld not in info["fields"] and info.get("base")):
            out = "%s%s%s" % (out, sep, _BASE_MEMBER)
            sep = "."
            cls = info["base"]
            continue
        # A smart pointer hop. `el_ptr->m_children` reaches a field of the
        # *pointee*, not of the handle, so a walk that only looked in the
        # handle's own fields stopped here -- and `shared_ptr<T>` is how
        # litehtml passes every element around, so no field reached through
        # one could be named. `operator->` already has a lowered form
        # registered; going through it is the same step the call pass takes,
        # written here so the name walker agrees with it.
        if (info is not None and fld not in info["fields"]
                and info.get("arrow")):
            ent = info["arrow"]
            pointee = ent["ret"].replace("*", "").strip()
            pinfo = type_info.get(pointee)
            if pinfo is not None and fld in pinfo["fields"]:
                # `sep` is "->" exactly when what we have is already a
                # pointer -- `this`, or a lowered reference parameter. Taking
                # its address again would hand `operator->` a pointer to the
                # handle rather than the handle.
                recv = out if sep == "->" else _addr_of_expr(out)
                out = "%s(%s)" % (ent["fn"], recv)
                sep = "->"
                cls = pointee
                info = pinfo
            else:
                return None
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
                    if found is None and re.match(r"^\w+$", inner):
                        # `std::move(s)` where `s` is a `T &&` parameter --
                        # a pointer once lowered, and a bare pointer name
                        # deliberately does not answer as an object. Under
                        # a move it is exactly the object that is meant:
                        # the C++ idiom for delegating a derived move to a
                        # base writes `operator=(std::move(s))` with `s`
                        # the rvalue-reference parameter. The dereference
                        # names the pointee, and its address is `s` again.
                        found = _named_object("*" + inner, scopes,
                                              type_info)
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



def _rewrite_scopes(text, type_info, path="<cpp>"):
    """`_rewrite_scopes_inner`, with a line number on whatever it reports.

    The scan's own index is the position: this pass reports about
    the construct it has just reached, and none of its messages said
    where. Locating them here rather than at each `raise` keeps one
    place that knows the file name.
    """
    pos = [0]
    try:
        return _rewrite_scopes_inner(text, type_info, pos)
    except CppError as e:
        raise _locate(e, text, pos[0], path)



def _rewrite_scopes_inner(text, type_info, _pos):
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
    # `T name;` or `T name(args);`. The argument list may not span a brace:
    # `[^;]*` alone let it run straight past its own closing paren, so for a
    # method whose *return type* is a class
    #
    #     static vec2 mk(vec2 *this) { vec2 r(5); return r; }
    #
    # the pattern matched from the function's own name to the first `;`
    # inside the body -- one bogus declaration of a variable called `mk`,
    # swallowing the real declaration of `r`, which therefore never got its
    # constructor and never got declared. It only bit where the return type
    # was a class, since that is what puts the header in `type_alt`, which
    # is why `T name(args)` worked everywhere else and looked supported.
    #
    # Braces are excluded rather than parens: an argument may legitimately
    # contain a nested call (`vec2 r(f(1));`), and the balance check below
    # is what keeps that honest.
    decl_re = re.compile(
        r"(?<![\w.])(%s)\s+(\w+)\s*(?:\(([^;{}]*)\))?\s*;" % type_alt)

    # `T *p = ..;` -- a pointer local of class type. Recorded so the name
    # walker can reach through it, exactly as it already does for a pointer
    # *parameter*. Only parameters were registered before, so a body that
    # bound a local to an element (`const attr_t *a = &v[i];`) could not name
    # `a->val` and every copy out of one was refused. The declaration itself
    # is left exactly as written: a pointer local owns nothing, so it takes
    # no constructor, no drop, and no rewriting -- this is only about being
    # able to name what it points at.
    ptr_decl_re = re.compile(
        r"(?<![\w.])(?:const\s+)?(%s)\s*\*\s*(\w+)\s*(?==|;)" % type_alt)

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

    def opens_aggregate(at):
        """Does the `{` at `at` open a struct/union/enum body?

        `agg_re` cannot reach back past a `;`, `{` or `}` -- its tail forbids
        all three -- so only the run since the nearest one is searched.
        Handing the pattern that window instead of a fresh `look[:at]` slice
        is what keeps this linear: the slice made every brace in the file
        cost a copy of everything before it.
        """
        j = max(look.rfind(";", 0, at), look.rfind("{", 0, at),
                look.rfind("}", 0, at))
        return agg_re.search(look, j + 1, at) is not None

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
    probe = _probe_positions(look, text)
    i, n = 0, len(text)
    in_str = None
    while i < n:
        _pos[0] = i
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
            if aggs or opens_aggregate(i):
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

        # Nothing below can start here -- see `_probe_positions`.
        if not probe[i]:
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

        m = ptr_decl_re.match(look, i)
        if m and not aggs and len(scopes) > 1 and \
                _prev_word(look, i) not in ("struct", "typedef", "union"):
            scopes[-1].ptrvals[m.group(2)] = m.group(1)
            scopes[-1].ptrs.add(m.group(2))
            out.append(text[m.start():m.end()])
            i = m.end()
            continue

        m = decl_re.match(look, i)
        if m and not aggs and \
                _prev_word(look, i) not in ("struct", "typedef", "union"):
            ctype, vname, args = m.group(1), m.group(2), m.group(3)
            # And the parens have to balance. Without this a declaration
            # whose argument holds a nested call could still match across
            # its own closing paren into whatever followed.
            if args is not None and (args.count("(") != args.count(")")):
                out.append(text[i])
                i += 1
                continue
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
            if src is None and (_is_call_result(rhs)
                                or _is_binop_result(rhs, scopes, type_info, ctype)):
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
            lit = (_binop_literal_operand(rhs, ctype, scopes, type_info)
                   if src is None and 1 in info["ctors"] else None)
            if lit is not None:
                # `T c = a + "lit";` -- the literal becomes a temporary
                # through the one-argument constructor, the operator runs on
                # it, and the temporary is destroyed straight after, so
                # nothing outlives the statement. `c` itself is a move in
                # from the call, exactly as `T c = a + b;` is.
                ent, litexpr, other, lit_left = lit
                tmpn = "__cpp_op%d" % mvn[0]
                mvn[0] += 1
                args = (("&%s, &%s" % (tmpn, other)) if lit_left
                        else ("&%s, &%s" % (other, tmpn)))
                out.append("%s %s; %s(&%s, %s); %s %s = %s(%s); %s(&%s);"
                           % (ctype, tmpn, info["ctors"][1]["fn"], tmpn,
                              litexpr, ctype, vname, ent["fn"], args,
                              _dropfn(info, ctype), tmpn))
                if info["dtor"]:
                    scopes[-1].live.append((ctype, vname))
                scopes[-1].vals[vname] = ctype
                i = m.end()
                continue
            if src is None:
                why = _binop_refusal(rhs, scopes, type_info)
                if why is not None:
                    raise CppError(why)
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
                if src is None and (_is_call_result(rhs)
                                    or _is_binop_result(rhs, scopes, type_info, ctype)):
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

    # A qualified name is skipped -- `Cls::name(..)` is a static member,
    # which lowers to `Cls_name(..)` a pass below with its parameters
    # handled there. Read as a bare call it looked like an unknown function,
    # and passing an owning argument to one looks like a hand-over.
    for m in re.finditer(r"(?<![\w.>&:])(\w+)\s*\(", text):
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


def _unaddressable_arg(a):
    """Why `&a` is not a thing, or None if it might be.

    `fix_args` puts an `&` on an argument whose parameter is a reference.
    That is right for a name, a member chain or a subscript, and meaningless
    for anything whose value does not live anywhere: a call result, an
    overloaded operator's result, a literal. Those were emitted anyway --
    `sink(&a + b)`, `sink(&string_substr(&b, 0, 1))` -- and the C front end
    then complained about the generated struct rather than the argument that
    was written.

    Deliberately conservative: it names only the shapes it is *sure* have no
    address, and leaves anything it cannot classify to the C front end
    exactly as before. A false refusal here would fail every caller of the
    function.
    """
    a = (a or "").strip()
    if not a:
        return None
    if re.match(r'^(".*"|\'.*\')$', a, re.S):
        return "a literal has no address"
    # A trailing `)` that closes a call opening after the first character:
    # `f(..)`, `o.m(..)`. Not a cast or a parenthesised name, which start
    # with the paren.
    if a.endswith(")") and not a.startswith("("):
        for k, c in enumerate(a):
            if c == "(" and _match_paren(a, k) == len(a) - 1 and k > 0:
                return "a call result has no address"
    # A top-level binary operator: `a + b`, already rewritten to a call by
    # the time this runs, or left as written when it was not.
    depth = 0
    for k, c in enumerate(a):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and c in "+-*/%|^" and k > 0 \
                and a[k + 1:k + 2] != "=" \
                and not (c == "-" and a[k + 1:k + 2] == ">") \
                and a[k - 1] not in "+-*/%|^&<>=!":
            return "the result of an operator has no address"
    return None


def _binop_literal_operand(rhs, ctype, scopes, type_info):
    """`(entry, literal, other, literal_is_left)` for `a + "lit"`, else None.

    C++ builds a temporary through the one-argument constructor and passes
    that; written out, that is exactly what this lowers to -- the same shape
    `str = name;` already takes for a converting assignment, so it is the
    pass's existing behaviour rather than a new liberty.

    It is also what makes `"lit" + s` work. That one is a *free*
    `operator+` in C++, which this subset has no notion of, but with the
    literal materialised the member operator on the temporary means the
    same thing: `tmp + s`, with `tmp` constructed from the literal.

    Narrow on purpose, like `_converting_operand`: exactly one operand a
    string or character literal, the other a named object of `ctype`, and
    `ctype` declaring that operator. A non-literal expression could be an
    object of the class this pass simply failed to name, and materialising
    one from it would build the wrong thing silently.
    """
    rhs = (rhs or "").strip()
    m = re.match(r"^(.+?)\s*([-+*/%|&^])\s*(.+)$", rhs, re.S)
    if not m:
        return None
    left, op, right = (m.group(1).strip(), m.group(2), m.group(3).strip())
    lit_re = re.compile(r'^(".*"|\'.*\')$', re.S)
    if lit_re.match(left) and re.match(r"^\w+$", right):
        lit, other, lit_left = left, right, True
    elif lit_re.match(right) and re.match(r"^\w+$", left):
        lit, other, lit_left = right, left, False
    else:
        return None
    named = _named_object(other, scopes, type_info)
    if named is None or named[1] != ctype:
        return None
    ent = (type_info.get(ctype) or {}).get("binop", {}).get(op)
    if not ent or ent.get("ret") != ctype:
        return None
    return ent, lit, other, lit_left


def _binop_refusal(rhs, scopes, type_info):
    """Why an overloaded-operator right-hand side cannot be lowered, or None.

    The generic initialiser refusal describes the *declaration* -- "the
    right-hand side is neither an object of that type nor a call returning
    one" -- which is true but names neither the operator that produced it
    nor the fix. A run of overloaded operators is worth its own message,
    because each shape has a different answer.
    """
    rhs = (rhs or "").strip()
    ops = r"[-+*/%|&^]"

    def _binop_of(name, op):
        got = _named_object(name, scopes, type_info) if name else None
        if got is None:
            return None, None
        return got[1], (type_info.get(got[1]) or {}).get("binop", {}).get(op)

    # `a + b + c` -- a chain. The by-value front door it needs is emitted
    # only for a class that owns nothing.
    m = re.match(r"^(\w+)\s*(%s)\s*(\w+)\s*(%s)\s*(\w+)$" % (ops, ops), rhs)
    if m:
        cls, ent = _binop_of(m.group(1), m.group(2))
        if ent is not None and ent.get("vfn") is None:
            if _BIN_PREC[m.group(2)] != _BIN_PREC[m.group(4)]:
                # Mixed precedence *and* no by-value door. Both facts
                # matter, and the chain message below names only the
                # second -- worse, its `t %s= ..; t %s= ..` advice folds
                # left, which is the grouping this run does not have. It
                # was telling the reader to write the wrong program.
                return ("`%s`: mixing `%s` and `%s` in one run of "
                        "overloaded operators needs each result passed on "
                        "by value, and %s owns a resource, so the copy "
                        "would make a second owner of it. Assign the "
                        "tighter-binding part to a local first (`%s t = "
                        "%s %s %s;`) and then apply the other -- note that "
                        "`%s= ..; %s= ..` would group left, which is not "
                        "what this expression means."
                        % (rhs, m.group(2), m.group(4), cls, cls,
                           m.group(3), m.group(4), m.group(5),
                           m.group(2), m.group(4)))
            return ("`%s`: chaining `%s` over %s is not in this subset. A run "
                    "passes the first result into the second by value, and %s "
                    "owns a resource, so the copy would make a second owner "
                    "of it. Use `%s=` into a local instead (`%s t = %s; t %s= "
                    "%s; t %s= %s;`)."
                    % (rhs, m.group(2), cls, cls, m.group(2), cls,
                       m.group(1), m.group(2), m.group(3), m.group(4),
                       m.group(5)))
        return None

    # `a + "lit"` -- an operand with no address to take.
    m = re.match(r"^(\w+)\s*(%s)\s*(.+)$" % ops, rhs, re.S)
    if m and not re.match(r"^\w+$", m.group(3).strip()):
        cls, ent = _binop_of(m.group(1), m.group(2))
        if ent is not None:
            return ("`%s`: the right-hand side of an overloaded `%s` has to "
                    "be a plain name of the same class, or a literal. "
                    "Operands are passed by address and there is none to "
                    "take of an expression or a call result. Assign it to a "
                    "%s local first, or use `%s=`."
                    % (rhs, m.group(2), cls, m.group(2)))

    # `"lit" + a` -- C++ resolves this with a *free* operator, which this
    # subset has no notion of: an overloaded operator is a member, so its
    # left operand has to be an object of the class.
    m = re.match(r"^(.+?)\s*(%s)\s*(\w+)$" % ops, rhs, re.S)
    if m and not re.match(r"^\w+$", m.group(1).strip()):
        cls, ent = _binop_of(m.group(3), m.group(2))
        if ent is not None:
            return ("`%s`: an overloaded `%s` is a *member* here, so its left "
                    "operand has to be an object of the class. C++ would pick "
                    "a free `operator%s` for this, and there are none in this "
                    "subset. Build the left operand as a %s local first "
                    "(`%s t(..); t %s= %s;`)."
                    % (rhs, m.group(2), m.group(2), cls, cls, m.group(2),
                       m.group(3)))
    return None


def _binop_tree(operands, ops, lookup, scopes, cinfo):
    """Lower a run of overloaded operators respecting precedence, or None.

    The fold this replaces went strictly left to right, so a run mixing `+`
    and `*` would have computed `(a + b) * c` and was refused rather than
    mistranslated. The refusal was correct and it refused `y = A * x + b`,
    which is the one line every linear algebra program contains.

    Reassociating is safe here for a reason particular to this subset: an
    operand in such a run is already required to be a plain *name*, so the
    run is a flat list of names and operators with no sub-expressions to
    evaluate and therefore no evaluation order to disturb. Shunting-yard
    over that list is a regrouping of calls, not a change to what runs.

    Every application goes through the `_vv` door, so an operand may be a
    name or another application indifferently. `None` if any step lacks
    one -- an owning class has no by-value door, and the caller then
    reports the refusal it always did.
    """
    out, stack = [], []
    for idx, name in enumerate(operands):
        out.append(("leaf", name))
        if idx < len(ops):
            while stack and _BIN_PREC[stack[-1]] >= _BIN_PREC[ops[idx]]:
                out.append(("op", stack.pop()))
            stack.append(ops[idx])
    while stack:
        out.append(("op", stack.pop()))

    work = []
    for kind, val in out:
        if kind == "leaf":
            sym = lookup(scopes, val)
            if sym is None or sym[1]:      # a pointer has no value to pass
                return None
            work.append((sym[0], val))
            continue
        if len(work) < 2:
            return None
        (lcls, ltext), (rcls, rtext) = work[-2], work[-1]
        del work[-2:]
        ent = (cinfo.get(lcls) or {}).get("binop", {}).get(val)
        if ent is None or ent.get("vvfn") is None:
            return None
        if rcls != (ent.get("arg") or lcls):
            return None
        work.append((ent["ret"], "%s(%s, %s)" % (ent["vvfn"], ltext, rtext)))
    if len(work) != 1:
        return None
    return work[0][1]


def _is_binop_result(rhs, scopes, type_info, ctype=None):
    """Is `rhs` a single overloaded binary operator over named objects?

    `Buf s = a + b;` becomes `Buf s = Buf__binadd(&a, &b);` -- a call whose
    value was returned to us, so taking it is a move in, exactly as
    `Buf s = a.plus(b);` already is. The two spellings lower to the same
    call, and only the operator one was refused: this check runs before the
    operator is rewritten, so `a + b` did not yet look like the call it
    becomes.

    One operator, both operands plain names, and the left one an object of
    a class declaring that operator with a return of its own type. A *run*
    (`a + b + c`) deliberately does not match: chaining needs the by-value
    front door, which an owning class does not get.
    """
    rhs = (rhs or "").strip()
    m = re.match(r"^(\w+)\s*([-+*/%|&^])\s*(\w+)$", rhs)
    if not m:
        return False
    named = _named_object(m.group(1), scopes, type_info)
    if named is None:
        return False
    cls = named[1]
    ent = (type_info.get(cls) or {}).get("binop", {}).get(m.group(2))
    # What has to match is the *declaration's* type, not the receiver's:
    # `Vec y = A * x;` is a move-in from a call returning a `Vec`, and the
    # receiver being a `Mat` says nothing about that. Comparing against the
    # receiver refused every operator whose result is a different class,
    # which is matrix-times-vector and matrix-times-scalar -- most of what
    # a linear algebra type does.
    return bool(ent) and ent.get("ret") == (ctype or cls)


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
        tail = text[close + 1:close + 200].lstrip()
        # A contract run may sit between the parameter list and the body.
        while tail.startswith("assert"):
            nl = tail.find("\n")
            if nl < 0:
                break
            tail = tail[nl + 1:].lstrip()
        if not (tail.startswith("{") or tail.startswith(";")):
            continue          # a call, not a declaration
        refs = _ref_positions(text[m.end():close], names)
        if refs:
            out[fname] = refs
    return out


#: A run of ShivyCX contract clauses sitting between a parameter list and
#: a body. They are not C, and every pass that finds a function by looking
#: for `)` immediately before `{` has to step over them.
_CONTRACT_RUN = re.compile(r"(?:\s*assert\b[^\n{;]*)+\s*$")


def _skip_contracts_back(text, j):
    """Index of the last character before any contract run ending at `j`.

    A contract sits exactly where nothing else does -- after the `)` and
    before the `{` -- so a pass that walks back from the brace expecting a
    close paren found the tail of `assert not len(o) % 4` instead and gave
    up. That silently cost the *body* its reference lowering: the enclosing
    function's parameters could not be found, so `o.d` was never rewritten
    to `o->d` and the C front end reported a member access on something
    that is not a structure. Every operator in a numeric library takes
    `const Vec &`, so this made contracts and reference parameters mutually
    exclusive -- which is exactly the combination the library needs.
    """
    m = _CONTRACT_RUN.search(text[:j + 1])
    return (m.start() - 1) if m is not None else j


def _params_at(text, brace_idx):
    """The parameter list of the function header ending just before `{`."""
    j = brace_idx - 1
    while j >= 0 and text[j] in " \t\r\n":
        j -= 1
    j = _skip_contracts_back(text, j)
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


# Both patterns above spell out nothing but identifiers, `*`, `=` and
# whitespace, so a match can only begin inside the run of those characters
# ending where the search does.
_TARGET_CHARS = frozenset("*= \t\r\n\f\v")


def _assign_target(look, at, scopes, cinfo):
    """The class a `new` expression is being assigned into, or None.

    Two shapes are recognised, which is what covers `Base *p = new
    Derived();` and a later `p = new Derived();`. Anything else -- a
    `return`, an argument, a field write through a chain -- yields None and
    no cast is inserted, so the C compiler still reports a real mismatch.

    Both patterns are anchored at `at`, and only the short run before it can
    hold a match, so that is the window they are given. Handing them a fresh
    `look[:at]` copied the file before every `new` in it.
    """
    lo = at
    while lo > 0:
        c = look[lo - 1]
        if c in _TARGET_CHARS or c.isalnum() or c == "_":
            lo -= 1
        else:
            break
    # `search(look, lo, at)` rather than a slice, so the lookbehinds still
    # see the character before the window -- a `.` there means the name is a
    # member and neither pattern applies.
    m = _DECL_TARGET.search(look, lo, at)
    if m is not None:
        return m.group(1) if m.group(1) in cinfo else None
    m = _ASSIGN_TARGET.search(look, lo, at)
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


def _drop_global_scope(text):
    """Remove the global-scope marker, once every name-resolving pass is done.

    `::free(p)` is marked rather than stripped where it is found, because
    the passes in between resolve a bare name against the enclosing class:
    coost's `system_allocator` calls `::free(p)` from a *static* method,
    and with the marker gone early that became `this->co_free(p)` -- a
    member call in a function with no `this`. Marked, the name is not a
    bare one and no pass claims it.

    Applied at every exit from `translate`, including the early one for a
    file that defines no classes -- which is exactly the file most likely
    to call a C library function this way.
    """
    return text.replace("__gsq__", "")


#: The type spellings a functional-style cast may use. Deliberately only
#: builtins and the fixed-width aliases: a *class* name followed by
#: parentheses is a constructor call, which is materialised elsewhere, and
#: confusing the two would turn every construction into a cast.
_CAST_WORDS = ("char", "short", "int", "long", "unsigned", "signed",
               "float", "double", "bool", "void")
_CAST_ALIASES = re.compile(
    r"^(?:u?int(?:8|16|32|64)_t|size_t|ptrdiff_t|intptr_t|uintptr_t)$")


def _lower_functional_casts(text, scan):
    """`int(x)` and `uint64_t(1)` -> `((int)(x))`.

    C++ lets a type be written like a call to convert to it. C does not,
    and the spelling survived into the output: coost's `dtoa_milo.h` writes
    `l & (uint64_t(1) << 63)`, which the C front end read as a call to
    something named `uint64_t`.

    Only one argument, and only a builtin type spelling -- a run of the
    keywords above, or one of the fixed-width aliases. Anything else is
    left alone, because `pt(1, 2)` is a constructor call and rewriting it
    as a cast would be silently wrong.
    """
    alt = "|".join(_CAST_WORDS)
    pat = re.compile(r"(?<![\w.>])((?:(?:%s)\s+)*(?:%s)|\w+)\s*\(" % (alt, alt))
    out, last = [], 0
    for m in pat.finditer(scan):
        ty = m.group(1).strip()
        words = ty.split()
        if not all(w in _CAST_WORDS for w in words) and not (
                len(words) == 1 and _CAST_ALIASES.match(ty)):
            continue
        op = m.end() - 1
        close = _match_paren(scan, op)
        if close is None:
            continue
        inner = scan[op + 1:close]
        if not inner.strip() or _split_top(inner)[1:]:
            continue                 # no argument, or more than one
        # A *declarator*, not a cast. `int (*g)(int) = ..` declares a
        # function pointer, and rewriting it as a cast gave
        # `((int)(*g))(int) = ..`. Two shapes give it away: the parenthesis
        # holding a `*` or `&`, and another parameter list following it.
        if inner.lstrip()[:1] in ("*", "&"):
            continue
        after = scan[close + 1:close + 2]
        if after == "(":
            continue
        # A declaration, not a cast: `int (x);` at statement start would be
        # one, but so would a cast used as a statement -- which is dead code
        # either way. Skipped when what follows is a `;` immediately.
        out.append(text[last:m.start()])
        out.append("((%s)(%s))" % (ty, text[op + 1:close]))
        last = close + 1
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)


def _materialise_ctor_temporaries(text, scan, path):
    """`Cls(a, b).method()` -> a hoisted local, then the call on it.

    A construction in expression position is not something C has. The
    return-position case is rewritten into a declaration; this one has no
    statement of its own to become, so the temporary is hoisted to just
    before the statement that contains it -- the same move, and the same
    soundness rule, as an inlined lambda body.

    Only where the construction is *evaluated exactly once and
    unconditionally*: `_stmt_start` refuses an operand of `?:`, `&&` or
    `||`, and a loop condition that re-evaluates it. Those are reported
    rather than hoisted, because hoisting there would change when the
    constructor runs.

    Only the `.`/`->` shape is handled -- coost's `dtoa_milo.h` writes
    `DiyFp(f, e).NormalizeBoundary()`. A bare `Cls(a, b)` elsewhere in an
    expression is left for the diagnostics that already cover it.
    """
    cls_names = set(re.findall(r"(?<![\w])(?:class|struct)\s+(\w+)", scan))
    if not cls_names:
        return text
    edits, n = [], 0
    for m in re.finditer(r"(?<![\w.>])(\w+)\s*\(", scan):
        name = m.group(1)
        if name not in cls_names:
            continue
        op = m.end() - 1
        close = _match_paren(scan, op)
        if close is None:
            continue
        tail = scan[close + 1:close + 3].lstrip()
        # `x = Cls(a, b);` -- the construction is the whole right-hand side
        # of an assignment to something already declared. Hoisted into a
        # local of its own and assigned from that, which is the same move
        # as the `.method()` case below and reuses the same declaration
        # lowering. Not a *declaration* (`Cls x = Cls(..)`), which the
        # initialiser lowering already handles on its own.
        if tail.startswith(";"):
            lhs = scan[:m.start()].rstrip()
            am = re.search(r"(?<![=!<>+\-*/%&|^])=\s*$", lhs)
            if am:
                head_txt = lhs[:am.start()].rstrip()
                # A declaration ends with the declared name preceded by a
                # type; an assignment's left side is just a name or a
                # member path. Told apart by whether the class name occurs
                # as the type immediately before it.
                if not re.search(r"(?<![\w])%s\s+\w+$" % re.escape(name),
                                 head_txt):
                    start, why = _stmt_start(scan, m.start())
                    if start is not None:
                        edits.append((start, m.start(), close + 1, name,
                                      text[op + 1:close], n, True))
                        n += 1
                        continue
        if not (tail.startswith(".") or tail.startswith("->")):
            # A construction in a conditional operand is the one other
            # shape seen in practice, and it cannot be hoisted at all: the
            # branch may not be evaluated. Reported rather than passed
            # through, which is what it used to be -- silently, into C
            # that does not compile.
            # Only `?`. A leading `:` is far more often a constructor's
            # initializer list -- `fastring() : fast::stream() {}` -- or a
            # label or an access specifier, and treating those as ternary
            # branches refused 140 files in the suite.
            before = scan[:m.start()].rstrip()[-1:]
            if before == "?":
                raise CppError(
                    "%s:%d: `%s(..)` builds an object in a branch of `?:`, "
                    "which may not be evaluated. The temporary cannot be "
                    "hoisted out of it -- assign each branch to a local and "
                    "choose between them with an `if`."
                    % (os.path.basename(path),
                       _src_line(scan, m.start()), name))
            continue
        if _prev_word(scan, m.start()) == "new":
            continue
        start, why = _stmt_start(scan, m.start())
        if start is None:
            raise CppError(
                "%s:%d: `%s(..)` builds an object inside an expression that "
                "%s. The temporary has to be hoisted to a statement of its "
                "own, and hoisting it here would change when the "
                "constructor runs. Assign it to a local first."
                % (os.path.basename(path), _src_line(scan, m.start()),
                   name, why))
        edits.append((start, m.start(), close + 1, name,
                      text[op + 1:close], n, False))
        n += 1
    if not edits:
        return text
    out, last = [], 0
    for start, s, e, name, args, idx, is_assign in edits:
        if start < last:
            continue                 # two in one statement: one pass each
        tmp = "__cpp_tmp%d" % idx
        out.append(text[last:start])
        out.append("%s %s = %s(%s); " % (name, tmp, name, args))
        out.append(text[start:s])
        out.append(tmp)
        last = e
    out.append(text[last:])
    return "".join(out)


def _materialise_ctor_returns(text, scan):
    """`return Cls(a, b);` -> a named local, then return it.

    A constructor call in a return expression is not something C has. For
    an *owning* class this is refused (there is nothing to move out of),
    but a class with no destructor slipped straight through and reached the
    C front end as `return dp__fpt(v, 1);` -- coost's `fast.h` has
    seventeen of these, one per `dp::_1` .. `dp::_9`.

    Rewritten into the declaration form -- `Cls __cpp_ret0 = Cls(a, b);
    return __cpp_ret0;` -- which the ordinary initialiser lowering already
    turns into `Cls __cpp_ret0; Cls_new(&__cpp_ret0, a, b);`. One code path
    rather than a second that could drift from it.
    """
    cls_names = set(re.findall(r"(?<![\w])(?:class|struct)\s+(\w+)", scan))
    if not cls_names:
        return text
    out, last, n = [], 0, 0
    pat = re.compile(r"(?<![\w.>])return\s+(\w+)\s*\(")
    for m in pat.finditer(scan):
        name = m.group(1)
        if name not in cls_names:
            continue
        op = m.end() - 1
        close = _match_paren(scan, op)
        if close is None:
            continue
        semi = scan.find(";", close)
        if semi < 0 or scan[close + 1:semi].strip():
            continue                  # not a bare `return Cls(..);`
        args = text[op + 1:close]
        tmp = "__cpp_ret%d" % n
        n += 1
        out.append(text[last:m.start()])
        out.append("%s %s = %s(%s); return %s;"
                   % (name, tmp, name, args, tmp))
        last = semi + 1
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)


def _strip_attribute_macros(text):
    """Blank an export/visibility macro sitting between `class` and its name.

    `class __coapi fastring : public fast::stream` is the ordinary way a
    library marks a type for a shared build -- `__declspec(dllexport)`, or
    `__attribute__((visibility("default")))`, or nothing at all, behind one
    object-like macro. Every class scan here reads the name with
    `(?:class|struct)\\s+(\\w+)`, so all of them collected the *macro* as the
    class: coost's `fastring` was collected as `__coapi`, which is why its
    members went missing and its constructors' initializer lists never
    bound. Thirty declarations across fifteen of its headers take this
    shape, and it is a common C++ idiom, so it is handled here rather than
    in the fork.

    Only a macro this translation unit actually `#define`s, and only one
    whose body is empty or an attribute -- `__declspec(..)` or
    `__attribute__((..))`. A second identifier that is not one of those is
    left exactly where it is, since it is not something this can identify.
    Blanked rather than removed, so every offset already taken stays valid.
    """
    macros = set()
    for m in re.finditer(
            r"^[ \t]*#[ \t]*define[ \t]+(\w+)[ \t]*([^\r\n\\]*)$",
            text, re.M):
        body = m.group(2).strip()
        if body == "" or body.startswith(("__declspec", "__attribute__")):
            macros.add(m.group(1))
    if not macros:
        return text
    def _blank(mm):
        if mm.group(2) not in macros:
            return mm.group(0)
        return mm.group(1) + " " * len(mm.group(2)) + mm.group(3)
    return re.sub(
        r"((?:class|struct)\s+)(\w+)(\s+\w+\s*(?=[:{]))", _blank, text)


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
                line = _src_line(scan, m.start())
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


def _emit_method_call(expr, cls, is_ptr, meth, args, ent, cinfo,
                      recv_const=False):
    """One lowered method call, as a C expression.

    Factored out because a chained call needs to produce a receiver
    expression rather than write straight to the output.
    """
    recv = _addr(expr, is_ptr)
    tail = (", " + args) if args else ""

    def cast(want, e):
        # Parenthesised: `->` binds tighter than a cast, so
        # `(Shape *)&sq->_vptr` would read the wrong thing.
        #
        # A const receiver is cast even when the class already matches,
        # because the cast is also what discards the qualifier: `this` is a
        # plain `T *`, so a `const T *` -- what a `const T &` parameter
        # lowers to -- cannot be passed as one. Written only for that case,
        # so every other call comes out byte for byte as before.
        if want == cls and not recv_const:
            return e
        return "((%s *)%s)" % (want, e)

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
        # A hierarchy rooted in the other language keeps its descriptor
        # pointer under the name that language gave it -- `_hdr.type`
        # through `Obj`, not `_vptr`. Same word at the same offset, so only
        # the spelling changes; the slot is still reached by name.
        xroot = _ext_descriptor(cls, cinfo)
        if xroot is not None and _ext_lang(cls, cinfo) != "cpp":
            return ("((const %s *)((Obj *)%s)->type)->%s((Obj *)%s%s)"
                    % (xroot, recv, meth, recv, tail))
        return ("((const struct %s_vtable *)(%s)->_vptr)->%s(%s%s)"
                % (ent["decl"], cast(cinfo[cls]["root"], recv), meth,
                   cast(ent["decl"], recv), tail))
    # An inherited method takes the base as `this`; the base is the first
    # member, so a cast reaches it.
    return "%s(%s%s)" % (ent["fn"], cast(ent["owner"], recv), tail)


#: A chain call -- `a.get()` / `p->get()` -- appearing inside an argument
#: list. Matched to decide whether a call is ready to be rewritten yet.
_NESTED_CHAIN = re.compile(r"(?<![\w.>])(\w+)((?:\s*(?:\.|->)\s*\w+)+)\s*\(")


def _defers_to_nested(raw, scopes, lookup):
    """Does this argument list still hold a method call to be lowered first?

    Rewriting a call by reference *consumes* its arguments -- the scan
    resumes past the closing paren -- so a method call nested in one was
    never visited, on this pass or any later one, and reached the C as
    `take(&a, a.get())`. The fixed-point loop above cannot help, because
    every pass makes the same jump.

    So the call waits instead. While an argument still names a receiver
    that resolves to a class, this reports true and the call is left for
    the ordinary character-by-character path, which descends into the
    arguments and lowers what is in them; on the next pass round the
    arguments are plain and the reference rewriting fires.

    Only a receiver that *resolves* -- checking that, rather than the
    shape, is what stops a wait that would never end. Plain C spelled the
    same way (`s.field`, or a struct the file never declared) resolves to
    nothing, is never going to be rewritten, and so must not defer, or the
    loop would run out its iterations with no `&` inserted at all.
    """
    for mm in _NESTED_CHAIN.finditer(raw):
        if lookup(scopes, mm.group(1)) is not None:
            return True
    return False



#: `dynamic_cast<T *>(e)` and `typeid(e)`. The angle brackets are matched
#: non-greedily and without nesting: a target type here is a class name and
#: a `*`, never another template, because the descriptor is per class and a
#: `dynamic_cast<vector<int> *>` has nothing to walk to.
_DYNCAST = re.compile(r"(?<![\w.>])dynamic_cast\s*<([^<>]*)>\s*\(")
_TYPEID = re.compile(r"(?<![\w.>])typeid\s*\(")

#: `.name()` immediately after a `typeid(..)`, which is the only member of
#: `std::type_info` this subset answers.
_TYPEID_NAME = re.compile(r"\s*\.\s*name\s*\(\s*\)")


def _rtti_operand_addr(operand, path, line, what):
    """The address of a `typeid` operand.

    `typeid(*p)` asks about the object `p` points at, so the address is `p`
    with the dereference removed rather than `&*p` -- which is the same
    address, but written the way the source already had it. Anything else is
    an object, so its address is taken.
    """
    operand = operand.strip()
    if not operand:
        raise CppError("%s:%d: `%s()` needs an operand."
                       % (os.path.basename(path), line, what))
    if operand.startswith("*"):
        inner = operand[1:].strip()
        if inner:
            return "(%s)" % inner
    return "&(%s)" % operand


def _lower_rtti(text, cinfo, path="<cpp>"):
    """Lower `dynamic_cast` and `typeid` against the emitted descriptors.

    Both read the descriptor pointer that a polymorphic object already
    carries at offset zero -- the vptr *is* the descriptor pointer under
    `--rtti`, since the descriptor is the vtable's own header. So neither
    needs the symbol table: the operand's static type does not matter, only
    that it is a pointer to something polymorphic, and a non-polymorphic
    operand is exactly what C++ refuses here too.

    `typeid(e)` yields the descriptor pointer itself rather than a
    `type_info` object. That is what makes `typeid(a) == typeid(b)` mean the
    right thing with no class behind it, and `.name()` a field read.
    """
    out, i, n = [], 0, len(text)
    look = _strip_comments(text)
    while i < n:
        m = _DYNCAST.search(look, i)
        t = _TYPEID.search(look, i)
        if m is None and t is None:
            out.append(text[i:])
            break
        if m is not None and (t is None or m.start() <= t.start()):
            close = _match_paren(look, m.end() - 1)
            if close is None:
                raise CppError("`dynamic_cast` without a closing `)`")
            line = _src_line(look, m.start())
            targ = m.group(1).strip()
            if targ.endswith("&"):
                raise CppError(
                    "%s:%d: `dynamic_cast<%s>` is the reference form, which "
                    "throws on failure -- and this subset has no exceptions, "
                    "so there is nothing for it to do. Use the pointer form "
                    "and test the result for null."
                    % (os.path.basename(path), line, targ))
            if not targ.endswith("*"):
                raise CppError(
                    "%s:%d: `dynamic_cast<%s>` casts to a value, which is a "
                    "conversion rather than a downcast. Cast to `%s *`."
                    % (os.path.basename(path), line, targ, targ))
            tname = targ[:-1].strip()
            info = cinfo.get(tname)
            if info is None:
                raise CppError(
                    "%s:%d: `dynamic_cast<%s *>`: `%s` is not a class defined "
                    "in this translation. The descriptor to compare against "
                    "is emitted with the class, so it has to be here."
                    % (os.path.basename(path), line, tname, tname))
            if not info.get("slots"):
                raise CppError(
                    "%s:%d: `dynamic_cast<%s *>`: `%s` has no virtual "
                    "methods, so it carries no type descriptor and there is "
                    "nothing to check at run time. C++ refuses this too. "
                    "Give the hierarchy a virtual method (a virtual "
                    "destructor is the usual one), or use a plain cast."
                    % (os.path.basename(path), line, tname, tname))
            operand = text[m.end():close].strip()
            out.append(text[i:m.start()])
            out.append("((%s *)_cpp_dyncast((void *)(%s), %s))"
                       % (tname, operand, _typeinfo_ref(tname, info)))
            i = close + 1
            continue
        close = _match_paren(look, t.end() - 1)
        if close is None:
            raise CppError("`typeid` without a closing `)`")
        line = _src_line(look, t.start())
        operand = text[t.end():close].strip()
        addr = _rtti_operand_addr(operand, path, line, "typeid")
        desc = "(*(const struct _CppTypeInfo *const *)%s)" % addr
        end = close + 1
        nm = _TYPEID_NAME.match(look, end)
        if nm:
            desc = "%s->name" % desc
            end = nm.end()
        out.append(text[i:t.start()])
        out.append(desc)
        i = end
    return "".join(out)


def _ibase_owner(dcls, iname, cinfo):
    """The member path from a `dcls *` to the vptr field for `iname`, or None.

    A class that names the interface itself has the field directly, so the
    path is empty. A class that inherits it through its *layout* base has
    the field inside `_base`, one hop per level -- which is exactly why the
    layout base is a struct prefix: the address arithmetic stays static
    however deep the chain is.

    None means no class on that chain names the interface, so this is not a
    conversion to a secondary base and nothing should be adjusted.
    """
    info = cinfo.get(dcls)
    if info is None:
        return None
    for bn, _bi, pth in info.get("ibases_all") or ():
        if bn == iname:
            return pth
    return None


def _check_ibase_conversions(text, cinfo, path="<cpp>"):
    """Refuse a conversion to a secondary base, which is not lowered yet.

    A secondary base is reached through a vptr at a fixed offset, so `(I *)d`
    becomes `(I *)&d->_vptr_I` -- an adjustment by a compile-time constant.
    The call rewriter inserts it wherever it can name the source type, which
    is a symbol or a member chain. What is left over is every other operand
    shape: a call result, another cast, an array element. For those there is
    no offset to apply, and an unadjusted pointer compiles cleanly while
    dispatching through the wrong table.

    That is a silent miscompile, which is the one outcome this translator is
    written never to produce, so the leftovers are reported here. The check
    runs *after* the rewriting for that reason: what remains is exactly what
    could not be lowered.
    """
    ibases = set()
    for info in cinfo.values():
        ibases.update(info.get("ibases") or ())
    if not ibases:
        return
    look = _strip_comments(text)
    for bn in sorted(ibases):
        # An unlowered cast: the operand was not a plain member chain, so
        # the walker had no type to adjust from. A cast the walker *did*
        # lower still spells `(I *)`, so the adjusted shape -- the cast, an
        # `&`, and the vptr field it reaches -- is matched and skipped
        # first. Anchoring on that shape rather than merely looking for the
        # field name nearby is what keeps an unlowered cast that happens to
        # sit near a lowered one from being passed.
        adjusted = re.compile(
            r"\(\s*%s\s*\*\s*\)\s*&\([^;]{0,200}?\)->[\w.]*%s(?![\w])"
            % (re.escape(bn), re.escape(_ivptr(bn))))
        m = None
        for cast in re.finditer(r"\(\s*%s\s*\*\s*\)" % re.escape(bn),
                                look):
            if adjusted.match(look, cast.start()):
                continue
            m = cast
            break
        if m is None:
            # Or an implicit conversion -- `I *p = &d;` with no cast at
            # all. Adjusted initialisers name the vptr field by now, so
            # one that does not is a conversion that never happened.
            for d in re.finditer(
                    r"(?<![\w])%s\s*\*\s*\w+\s*=([^;]*);" % re.escape(bn),
                    look):
                if _ivptr(bn) not in d.group(1) and d.group(1).strip() != "0":
                    m = d
                    break
        if m is None:
            continue
        raise CppError(
            "%s:%d: cannot convert this to the secondary base `%s`. `%s` is "
            "reached through a vptr of its own at a fixed offset, so the "
            "conversion has to adjust the address by that offset -- which "
            "needs the type of what is being converted *from*. That is "
            "known for a named object or a member chain (`(%s *)&obj`, "
            "`(%s *)ptr`), and not for anything else: a call result, a "
            "cast, an array element. An unadjusted pointer would dispatch "
            "through the wrong table and compile without complaint, so it "
            "is reported instead. Assign it to a typed local first."
            % (os.path.basename(path), _src_line(look, m.start()),
               bn, bn, bn, bn))


def _rewrite_calls(text, cinfo, free_refs, free_rets=None, path="<cpp>"):
    """`_rewrite_calls_inner`, with a line number on whatever it reports.

    The scan's own index is the position: this pass reports about
    the construct it has just reached, and none of its messages said
    where. Locating them here rather than at each `raise` keeps one
    place that knows the file name.
    """
    pos = [0]
    try:
        return _rewrite_calls_inner(text, cinfo, free_refs, free_rets, pos)
    except CppError as e:
        raise _locate(e, text, pos[0], path)



def _rewrite_calls_inner(text, cinfo, free_refs, free_rets, _pos):
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
        r"(?<![\w.])(const\s+)?(%s)\s*(\*\s*)?(\w+)\s*(?=[;=,)])" % alt)
    call_re = re.compile(r"(?<![\w.>])(\w+)((?:\s*(?:\.|->)\s*\w+)+)\s*\(")
    # The same chain, but not followed by `(` -- a member read or write
    # rather than a call. The call pattern is tried first, so this only
    # ever sees what that one left behind.
    field_re = re.compile(
        r"(?<![\w.>])(\w+)((?:\s*(?:\.|->)\s*\w+)+)(?!\s*\()")
    # `a + b`, where both sides are plain names that resolve to a class
    # with that operator. Rewritten here rather than with the other
    # operators in `_rewrite_scopes`, because a binary operator's result is
    # an *expression*: `vec2 c = a + b;` is a declaration whose initialiser
    # has to be lowered in place, and by this pass the declaration is
    # already plain C with the sum still sitting in it.
    #
    # Both operands must be plain names. One that is itself an expression
    # would need a temporary to take the address of, which is the same wall
    # a by-value method return hits, so `a + b + c` is not in this. `=`
    # must not follow the operator, or this would eat the `+` of `a += b`.
    bin_re = re.compile(
        r"(?<![\w.>])(\w+)\s*([+\-*/%|&^])(?!=)\s*(\w+)(?![\w(<])")
    # The continuation of a run: `+ c` after `a + b` has already been
    # consumed. No leading name, because the left operand is the result
    # carried in from the previous step.
    bin_re_cont = re.compile(
        r"\s*([+\-*/%|&^])(?!=)\s*(\w+)(?![\w(<])")
    # The same head with *no* usable right operand. Tried after `bin_re`,
    # so it only ever sees what that one declined: a parenthesised
    # subexpression, a call, a literal. Reported rather than left in the C
    # as `a + <something>`, which the C front end would complain about in
    # terms of the generated struct rather than the operator written.
    bin_head_re = re.compile(
        r"(?<![\w.>])(\w+)\s*([+\-*/%|&^])(?!=)\s*(?=[(])")
    builtin_re = re.compile(
        r"(?<![\w.>])(__cpp_copy|__cpp_movein|__cpp_drop|__cpp_eq|__cpp_cmp"
        r"|__cpp_addr"
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
    # `(I *)&obj` / `(I *)ptr`. Only a plain member chain as the operand:
    # anything else needs the type of an arbitrary expression, which this
    # pass does not have, and a conversion it cannot adjust is reported by
    # `_check_ibase_conversions` rather than emitted unadjusted.
    ibase_cast_re = re.compile(
        r"\(\s*(\w+)\s*\*\s*\)\s*(&\s*)?([A-Za-z_][\w.]*(?:->\w+)*)")
    all_ibases = set()
    for _ci in cinfo.values():
        all_ibases.update(_ci.get("ibases") or ())
    # As in `_rewrite_scopes`: match against comment-blanked text so a `.`
    # or a parenthesis inside prose cannot be read as code. Same length, so
    # the indices address `text`, which is what is emitted.
    #
    # Directive lines go the same way, continuations included. coost's
    # `DISALLOW_COPY_AND_ASSIGN` macro body holds `void operator=(const T&)
    # = delete` with no `;` -- the backslash carries the line on -- and the
    # delete handler below read it as a *statement*, then ran past the
    # continuation into the `#if` that follows, quoting a preprocessor line
    # inside its own diagnostic. The same hazard the function-template
    # collector already blanks for.
    look = _blank_directives(_strip_comments(text))

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
            nxt = _match_paren(look, nm.end() - 1)
            if nxt is None:
                return expr, pos
            ent = _pick(cinfo[cls]["methods"][meth], text[nm.end():nxt],
                        cls, meth)
            args = fix_args(text[nm.end():nxt], ent["refs"], scopes)
            if not is_ptr and not addressable:
                # C cannot take the address of a function *result*, and a
                # method needs an addressable receiver. A dereference is a
                # different matter -- `&(*p)` is fine -- which is why the
                # subscript branch says so.
                #
                # So the receiver goes in by *value* instead, through a
                # variant emitted for exactly the methods a source chains
                # onto. That exists only for a class with no destructor:
                # a struct copy of an owning receiver would leave two
                # objects holding one resource, which is why the refusal
                # below still stands for one.
                vfn = (cinfo[cls].get("byval", {})
                       .get(meth, {}).get(_arity(text[nm.end():nxt])))
                if vfn is None:
                    # Two different reasons, and saying the wrong one sends
                    # the author looking for a resource their class has not
                    # got.
                    if cinfo[cls]["dtor"]:
                        why = ("owns a resource, so it cannot be copied "
                               "into the call")
                    else:
                        why = ("reaches `%s` through the vtable, and a "
                               "virtual call needs a receiver whose address "
                               "can be taken" % meth)
                    raise CppError(
                        "`%s().%s()`: %s is returned by value and %s -- and "
                        "there is no address to take of a function result. "
                        "Assign it to a local first, or return `%s *`."
                        % (from_meth, meth, cls, why, cls))
                expr = "%s(%s%s)" % (vfn, expr, (", " + args) if args else "")
                cls, is_ptr = _ret_class(ent["ret"], cinfo)
                addressable = False
                pos, from_meth = nxt + 1, meth
                continue
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
            #
            # A parameter with a *default argument* is one too, and did not
            # parse as one: `const string &quote = _t(" ")` has an `=` in it
            # and `_parse_param` returned None, so litehtml's tokenizer
            # declaration was treated as a call and had an `&` put on the
            # default. Strip the default before asking.
            if _parse_param(a, names) is not None:
                continue
            if _top_level_eq(a) >= 0 and \
                    _parse_param(_strip_default_args(a), names) is not None:
                continue
            sym = lookup(scopes, a) if re.match(r"^\w+$", a) else None
            if sym is not None and sym[1]:
                continue          # already a pointer
            why = _unaddressable_arg(a)
            if why is not None:
                raise CppError(
                    "`%s` is passed to a reference parameter, and %s. This "
                    "lowering passes a reference as a pointer, so the "
                    "argument has to be something with an address -- a name, "
                    "a member, or a subscript. Assign it to a local first "
                    "and pass that." % (a, why))
            parts[idx] = " &" + a
        return ",".join(parts).strip()

    free_rets = free_rets or {}
    out = []
    scopes = [{}]
    pdepth = 0
    probe = _probe_positions(look, text)
    i, n = 0, len(text)
    quote = None
    while i < n:
        _pos[0] = i
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
        # `(I *)e` where `I` is a secondary base of `e`'s class. The base is
        # reached through a vptr of its own, so the conversion is an
        # adjustment by that field's offset -- which is why it is done here,
        # where the symbol table can say what `e` is. Without the source
        # type there is no offset to apply, and an unadjusted pointer
        # dispatches through the wrong table while compiling cleanly.
        #
        # In the character dispatch rather than with `new` and `delete`
        # below: those are matched where a *word* can begin, and a cast
        # begins with a parenthesis, so a pattern tried there is never
        # reached.
        if c == "(" and all_ibases:
            m = ibase_cast_re.match(look, i)
            if m and m.group(1) in all_ibases:
                iname, amp, chain_txt = m.group(1), m.group(2), m.group(3)
                chain = [q for q in re.split(r"\s*(?:\.|->)\s*", chain_txt)
                         if q]
                got = (resolve(scopes, chain[0], chain[1:])
                       if all(re.match(r"^\w+$", q) for q in chain) else None)
                if got is not None:
                    expr, dcls, is_ptr = got
                    owner = _ibase_owner(dcls, iname, cinfo)
                    # `&obj` on a value, or a bare pointer. `&ptr` is a
                    # pointer to a pointer and `obj` alone is not a pointer
                    # at all -- neither is a conversion to a base, so both
                    # are left for the C front end to judge in its own terms.
                    ptr = (("&(%s)" % expr) if (amp and not is_ptr)
                           else (expr if (not amp and is_ptr) else None))
                    if owner is not None and ptr is not None:
                        out.append("((%s *)&(%s)->%s%s)"
                                   % (iname, ptr, owner, _ivptr(iname)))
                        i = m.end()
                        continue

        if c == "{":
            frame = {}
            params = _params_at(look, i)
            for p in _split_top(params or ""):
                got = _parse_param(p, names)
                if got is not None:
                    frame[got[2]] = (got[0], got[1])
                    if re.match(r"^\s*const\b", p):
                        # A `const T &` parameter lowers to `const T *`, and
                        # `this` is a plain `T *` -- so using one as a
                        # receiver needs the qualifier cast away. Noted here
                        # because a parameter never goes through the
                        # declaration pattern below: that one is read
                        # outside parentheses.
                        frame[("const", got[2])] = True
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

        # Nothing below can start here -- see `_probe_positions`. Checked
        # before the `for`-head lookback too, which is not free either.
        if not probe[i]:
            out.append(text[i])
            i += 1
            continue

        # Declarations are looked for outside parentheses -- an argument list
        # is full of names that are not declarations -- with one exception:
        # a `for` initialiser is a declaration *inside* parentheses, and
        # `for (string *it = v.begin(); ..)` is the iterator idiom the
        # containers here are built around. Recognised by the `for` that
        # opened the paren, so an ordinary argument list is untouched.
        if pdepth == 0 or (pdepth == 1 and _in_for_head(look, i)):
            m = decl_re.match(look, i)
            if m and _prev_word(look, i) not in ("struct", "typedef", "union"):
                scopes[-1][m.group(4)] = (m.group(2), bool(m.group(3)))
                if m.group(1):
                    # Recorded beside the symbol, under a key no identifier
                    # can collide with. A method takes a plain `T *this` --
                    # the trailing `const` on a declaration is dropped,
                    # since nothing here models it -- so a `const T *`
                    # receiver needs the qualifier cast away to be used as
                    # one at all. Only that case, so every other emission
                    # stays exactly as it was.
                    scopes[-1][("const", m.group(4))] = True
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
                # Only when the receiver is the const symbol itself. Reached
                # through a field, the qualifier is the field's business and
                # the chain has already been built.
                rconst = bool(lookup(scopes, ("const", m.group(1)))) \
                    and not chain[:-1]
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
                                             ent, cinfo, rconst)
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
                                             ent, cinfo, rconst)
                    rcls, rptr = _ret_class(ent["ret"], cinfo)
                    expr, end = follow(expr, rcls, rptr, close + 1, meth)
                    out.append(expr)
                    i = end
                    continue

        m = bin_re.match(look, i)
        if m:
            sym = lookup(scopes, m.group(1))
            ent = None
            if sym is not None and sym[0] in cinfo:
                ent = cinfo[sym[0]].get("binop", {}).get(m.group(2))
            if ent is not None:
                rsym = lookup(scopes, m.group(3))
                # The operand class the operator declares, which is this
                # class for the symmetric `T operator+(const T &)` and a
                # different one for `Vec operator*(const Vec &)` on a `Mat`.
                want = ent.get("arg") or sym[0]
                if rsym is not None and rsym[0] == want:
                    # Look the whole run over first. While every operator in
                    # it binds equally tightly the left-to-right fold below
                    # is already right, and is kept so that the common case
                    # emits exactly what it always did. A run that *mixes*
                    # precedence needs regrouping, and gets it from
                    # `_binop_tree` -- or, if any step has no by-value door,
                    # falls through to the refusal that stood before.
                    _rops, _rnames = [m.group(2)], [m.group(1), m.group(3)]
                    _scan = m.end()
                    while True:
                        _cm = bin_re_cont.match(look, _scan)
                        if _cm is None:
                            break
                        _rops.append(_cm.group(1))
                        _rnames.append(_cm.group(2))
                        _scan = _cm.end()
                    if len(set(_BIN_PREC[o] for o in _rops)) > 1:
                        built = _binop_tree(_rnames, _rops, lookup, scopes,
                                            cinfo)
                        if built is not None:
                            out.append(built)
                            i = _scan
                            continue
                    ldr = "" if sym[1] else "&"
                    rdr = "" if rsym[1] else "&"
                    expr = ("%s(%s%s, %s%s)"
                            % (ent["fn"], ldr, m.group(1),
                               rdr, m.group(3)))
                    pos = m.end()
                    # Continue the run: each further operand takes the
                    # previous result by value, which is what the `_v`
                    # wrapper exists for.
                    while True:
                        cm2 = bin_re_cont.match(look, pos)
                        if cm2 is None:
                            break
                        op2, rhs2 = cm2.group(1), cm2.group(2)
                        if _BIN_PREC[op2] != _BIN_PREC[m.group(2)]:
                            raise CppError(
                                "`%s %s %s %s %s`: mixing `%s` and `%s` in "
                                "one run of overloaded operators is not in "
                                "this subset -- they have different "
                                "precedence and this lowering goes left to "
                                "right, so it would compute the wrong "
                                "grouping. Assign the tighter-binding part "
                                "to a temporary first (`T t = %s %s %s;`) "
                                "-- parentheses do not help, since an "
                                "operand has to be a plain name."
                                % (m.group(1), m.group(2), m.group(3),
                                   op2, rhs2, m.group(2), op2,
                                   m.group(3), op2, rhs2))
                        ent2 = cinfo[sym[0]].get("binop", {}).get(op2)
                        rs2 = lookup(scopes, rhs2)
                        if ent2 is None or ent2.get("vfn") is None \
                                or rs2 is None \
                                or rs2[0] != (ent2.get("arg") or sym[0]):
                            break
                        expr = ("%s(%s, %s%s)"
                                % (ent2["vfn"], expr,
                                   "" if rs2[1] else "&", rhs2))
                        pos = cm2.end()
                    out.append(expr)
                    i = pos
                    continue

        m = bin_head_re.match(look, i)
        if m:
            sym = lookup(scopes, m.group(1))
            if sym is not None and sym[0] in cinfo \
                    and cinfo[sym[0]].get("binop", {}).get(m.group(2)):
                raise CppError(
                    "`%s %s ...`: the right-hand side of an overloaded `%s` "
                    "has to be a plain name of the same class. This lowering "
                    "passes operands by address, and there is no address to "
                    "take of a parenthesised expression or a call result. "
                    "Assign it to a temporary first."
                    % (m.group(1), m.group(2), m.group(2)))

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
            if kind == "__cpp_addr":
                # `__cpp_addr(T, x)` -- spell `x` the way `__cpp_cmp` and
                # `__cpp_eq` want their *right* operand: a pointer for a
                # class, the value itself for a scalar.
                #
                # A container gets that spelling for free, because the
                # value arrives in a parameter declared `__cpp_ref(T)`,
                # which is already a pointer for a class. Code that builds
                # its own value -- `sort` holding an element aside while it
                # shifts the tail -- has no such parameter, and no way to
                # write one expression that is an address in one
                # instantiation and a value in the other. This is that way.
                if ty not in cinfo:
                    out.append("(%s)" % parts[1])
                else:
                    out.append("&(%s)" % parts[1])
                i = close + 1
                continue
            if kind == "__cpp_cmp":
                # Three-way ordering: negative, zero, positive, like
                # `strcmp`. Three-way rather than a boolean `less` because
                # the builtin's operands are *not* symmetric -- the right
                # one arrives as an already-lowered pointer and the left as
                # an lvalue -- so `b < a` cannot be spelled by swapping the
                # arguments of `a < b`. A predicate would therefore leave a
                # container unable to derive equality from ordering, and it
                # would have to demand `equals` as well; one comparison
                # answers both questions and is what a class supplies once.
                if ty not in cinfo:
                    # A scalar. Written with two comparisons rather than a
                    # subtraction: `a - b` overflows for wide or unsigned
                    # types and gets the order backwards when it does.
                    # Both operands are expanded twice, so a container must
                    # pass side-effect-free expressions here -- which is
                    # equally true of `__cpp_eq` above.
                    out.append("((%s) < (%s) ? -1 : ((%s) < (%s) ? 1 : 0))"
                               % (parts[1], parts[2], parts[2], parts[1]))
                elif "compare" in cinfo[ty]["methods"]:
                    # As in `__cpp_eq`: the second operand is a reference,
                    # already lowered to a pointer, the first an lvalue.
                    out.append("%s_compare(&(%s), %s)"
                               % (ty, parts[1], parts[2]))
                else:
                    raise CppError(
                        "`__cpp_cmp(%s, ..)`: %s is a class with no "
                        "`compare`, so two of them cannot be ordered. Add "
                        "`int compare(const %s &o)` returning negative, zero "
                        "or positive." % (ty, ty, ty))
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
            target = _assign_target(look, i, scopes, cinfo)
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
        if m and (m.group(1) in free_refs or m.group(1) in free_rets):
            op = m.end() - 1
            close = _match_paren(look, op)
            if close is not None and not _defers_to_nested(
                    text[op + 1:close], scopes, lookup):
                fn = m.group(1)
                args = fix_args(text[op + 1:close], free_refs.get(fn), scopes)
                expr = "%s(%s)" % (fn, args)
                # A free function returning `T *` can be the receiver of the
                # next call, the same way a method returning one can:
                # `min_element(..)->c_str()`. Without this the chain was
                # left as written, because a chain only ever starts from a
                # *symbol* that resolves to a class and a call result is not
                # one -- which is right for plain C spelled the same way,
                # but wrong for the supplied templates, whose return types
                # this pass does know.
                rcls, rptr = free_rets.get(fn, (None, False))
                expr, end = follow(expr, rcls, rptr, close + 1, fn)
                out.append(expr)
                i = end
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
void *memset(void *, int, unsigned long);
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
    /* Concatenation. `r` is a bare local returned by value, so it is moved
       out rather than copied -- which is what makes this expressible for a
       class that owns a buffer. A *run* (`a + b + c`) is refused, since
       chaining needs a by-value front door and an owning class does not
       get one; build it with `+=`, which writes into an object that
       already exists and has no such limit. */
    string operator+(const string &o) {
        string r;
        r.reserve(sn + o.sn);
        if (r.sd != 0) {
            if (sn > 0) { memcpy(r.sd, sd, (unsigned long)sn); }
            if (o.sn > 0) { memcpy(r.sd + sn, o.sd, (unsigned long)o.sn); }
            r.sn = sn + o.sn;
            r.sd[r.sn] = 0;
        }
        return r;
    }
    string &operator+=(const string &o) {
        reserve(sn + o.sn);
        if (sd != 0 && o.sn > 0) {
            memcpy(sd + sn, o.sd, (unsigned long)o.sn);
            sn = sn + o.sn;
            sd[sn] = 0;
        }
    }
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
    /* Substring search, under a different name than `find`. `std::string`
       overloads `find` on `char` and `const char *`, which are the same
       arity -- and this subset resolves an overload by argument *count*,
       before types are known, so the two cannot be told apart. A separate
       name says it unambiguously rather than picking one and silently
       mismatching the other.

       Plain O(n*m) scanning: no Boyer-Moore, no KMP table, because the
       table would need storage this has nowhere to put. */
    int find_str_from(const char *s, int from) {
        int k = (int)strlen(s);
        int i = from;
        int j;
        int ok;
        if (i < 0) { i = 0; }
        if (k == 0) { if (i > sn) { return -1; } return i; }
        while (i + k <= sn) {
            ok = 1;
            j = 0;
            while (j < k) {
                if (sd[i + j] != s[j]) { ok = 0; j = k; }
                else { j = j + 1; }
            }
            if (ok) { return i; }
            i = i + 1;
        }
        return -1;
    }
    int find_str(const char *s) { return find_str_from(s, 0); }
    int rfind_str(const char *s) {
        int k = (int)strlen(s);
        int i = sn - k;
        int j;
        int ok;
        if (k == 0) { return sn; }
        while (i >= 0) {
            ok = 1;
            j = 0;
            while (j < k) {
                if (sd[i + j] != s[j]) { ok = 0; j = k; }
                else { j = j + 1; }
            }
            if (ok) { return i; }
            i = i - 1;
        }
        return -1;
    }
    int contains(const char *s) {
        if (find_str_from(s, 0) < 0) { return 0; }
        return 1;
    }
    int starts_with(const char *s) {
        int k = (int)strlen(s);
        if (k > sn) { return 0; }
        if (memcmp(sd, s, (unsigned long)k) == 0) { return 1; }
        return 0;
    }
    int ends_with(const char *s) {
        int k = (int)strlen(s);
        if (k > sn) { return 0; }
        if (memcmp(sd + sn - k, s, (unsigned long)k) == 0) { return 1; }
        return 0;
    }
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
    /* Lexicographic, which is what `std::set<string>` and `std::map` order
       by. `memcmp` over the shorter length first, then length breaks the
       tie -- comparing over the longer one would read past the end of the
       shorter buffer. Three-way, like `std::string::compare`, which is
       also exactly what `__cpp_cmp` asks a class for. */
    int compare(const string &o) {
        int n = sn;
        int r;
        if (o.sn < n) { n = o.sn; }
        if (n > 0) {
            r = memcmp(sd, o.sd, (unsigned long)n);
            if (r != 0) { return r; }
        }
        if (sn < o.sn) { return -1; }
        if (sn > o.sn) { return 1; }
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
# none of `operator++`, `operator!=` or an iterator class has to exist.
#
# Sorted by key, and binary-searched, now that `__cpp_cmp` can order two
# `K`s. It was an unsorted array with a linear `find` when there was no way
# to ask which of two keys came first -- which also meant it iterated in
# insertion order, quietly unlike `std::map`, so code that walked one and
# depended on the order was wrong in a way nothing reported. A key class
# therefore supplies `compare` where it used to supply `equals`.
_STD_MAP = """
template<typename K, typename V>
class map {
    pair<K,V> *pd;
    int pn;
    int pcap;
public:
    map() { pd = 0; pn = 0; pcap = 0; }
    ~map() { clear(); free(pd); pd = 0; pcap = 0; }
    int size() { return pn; }
    int empty() { if (pn == 0) { return 1; } return 0; }
    /* Both halves of each entry are destroyed, not just the array freed.
       A `map<string, string>` owns two objects per element, and releasing
       only the block they sat in leaks both. */
    void clear() {
        while (pn > 0) {
            pn = pn - 1;
            __cpp_drop(K, pd[pn].first);
            __cpp_drop(V, pd[pn].second);
        }
    }
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
    /* The first entry whose key is not less than `k` -- the insertion
       point, and the start of every lookup. */
    int lower_index(__cpp_ref(K) k) {
        int lo = 0;
        int hi = pn;
        int mid;
        while (lo < hi) {
            mid = lo + (hi - lo) / 2;
            if (__cpp_cmp(K, pd[mid].first, k) < 0) { lo = mid + 1; }
            else { hi = mid; }
        }
        return lo;
    }
    pair<K,V> *lower_bound(__cpp_ref(K) k) { return pd + lower_index(k); }
    pair<K,V> *find(__cpp_ref(K) k) {
        int i = lower_index(k);
        if (i < pn) { if (__cpp_cmp(K, pd[i].first, k) == 0) { return pd + i; } }
        return pd + pn;
    }
    int count(__cpp_ref(K) k) { if (find(k) == pd + pn) { return 0; } return 1; }
    V &operator[](__cpp_ref(K) k) {
        int i;
        i = lower_index(k);
        if (i < pn) {
            if (__cpp_cmp(K, pd[i].first, k) == 0) { return pd[i].second; }
        }
        if (pn == pcap) { reserve(pcap ? pcap * 2 : 8); }
        if (pn == pcap) { return pd[0].second; }
        /* Shift by representation, which is what moving an object is: the
           tail is relocated rather than assigned, so an owning key or
           value keeps its one owner and nothing is destroyed on the way. */
        if (pn > i) {
            memmove(pd + i + 1, pd + i,
                    (unsigned long)((pn - i) * (int)sizeof(pair<K,V>)));
        }
        /* The value slot is zeroed before the key goes in. `std::map`
           value-initialises a new mapped value, and the storage `realloc`
           returned holds whatever was there before -- so reading `m[k]`
           for an absent key gave a garbage int, and a `map<K,string>`
           would have destroyed a pointer nobody set. Zero is what an
           empty `string`, `vector` and pointer all are here; a class whose
           default constructor does something else still needs assigning
           to, exactly as before. */
        memset(&pd[i].second, 0, sizeof(V));
        __cpp_copy(K, pd[i].first, k);
        pn = pn + 1;
        return pd[i].second;
    }
    void erase(__cpp_ref(K) k) {
        pair<K,V> *f;
        int i;
        f = find(k);
        if (f != pd + pn) {
            i = (int)(f - pd);
            __cpp_drop(K, pd[i].first);
            __cpp_drop(V, pd[i].second);
            if (pn - i - 1 > 0) {
                memmove(pd + i, pd + i + 1,
                        (unsigned long)((pn - i - 1) * (int)sizeof(pair<K,V>)));
            }
            pn = pn - 1;
        }
    }
};
"""

# A `set` that keeps its elements *sorted*, which is where this diverges
# from `map`. `map` is an unsorted array because it was written before there
# was any way to order two `K`s generically; `__cpp_cmp` is that way, so a set
# can binary-search its lookups and -- the part that actually matters --
# iterate in the order `std::set` promises, rather than in insertion order
# that happens to look right until it doesn't.
#
# Because the comparison is three-way, an element type supplies `compare`
# and nothing else: equality is `compare(..) == 0`, so there is no separate
# `equals` to keep consistent with it.
#
# The iterator is a `T *` into the buffer, the same design `vector` and `map`
# use, so `++it`, `*it` and `it != end()` stay plain C. The cost is that
# `insert` shifts the tail, which `std::set` does not; for the sizes this
# subset is aimed at that is the cheaper mistake to make.
_STD_SET = """
template<typename T>
class set {
    T *td;
    int tn;
    int tcap;
public:
    set() { td = 0; tn = 0; tcap = 0; }
    ~set() { clear(); free(td); td = 0; tcap = 0; }
    int size() { return tn; }
    int empty() { if (tn == 0) { return 1; } return 0; }
    void clear() { while (tn > 0) { tn = tn - 1; __cpp_drop(T, td[tn]); } }
    T *begin() { return td; }
    T *end() { return td + tn; }
    T *rbegin() { return td + tn - 1; }
    T *rend() { return td - 1; }
    void reserve(int c) {
        if (c > tcap) {
            T *nd = (T *)realloc(td, (unsigned long)c * sizeof(T));
            if (nd != 0) { td = nd; tcap = c; }
        }
    }
    /* The first element not less than `v` -- `std::lower_bound`, and the
       insertion point. Every other lookup is phrased in terms of this one. */
    int lower_index(__cpp_ref(T) v) {
        int lo = 0;
        int hi = tn;
        int mid;
        while (lo < hi) {
            mid = lo + (hi - lo) / 2;
            if (__cpp_cmp(T, td[mid], v) < 0) { lo = mid + 1; }
            else { hi = mid; }
        }
        return lo;
    }
    T *lower_bound(__cpp_ref(T) v) { return td + lower_index(v); }
    T *find(__cpp_ref(T) v) {
        int i = lower_index(v);
        /* `lower_index` lands on the first element not less than `v`, so
           the only candidate is that one, and a zero comparison decides
           it -- the same comparison the search already used, rather than a
           separate equality that could disagree with it. */
        if (i < tn) { if (__cpp_cmp(T, td[i], v) == 0) { return td + i; } }
        return td + tn;
    }
    int count(__cpp_ref(T) v) { if (find(v) == td + tn) { return 0; } return 1; }
    /* Returns 1 if the element was new, 0 if it was already there -- the
       `.second` of what `std::set::insert` returns, spelled as the whole
       result because this subset has no `pair` return by value. */
    int insert(__cpp_ref(T) v) {
        int i = lower_index(v);
        if (i < tn) { if (__cpp_cmp(T, td[i], v) == 0) { return 0; } }
        if (tn == tcap) {
            int m = tcap * 2;
            if (m < 4) { m = 4; }
            reserve(m);
        }
        if (tn == tcap) { return 0; }
        if (tn > i) {
            memmove(td + i + 1, td + i,
                    (unsigned long)((tn - i) * (int)sizeof(T)));
        }
        __cpp_copy(T, td[i], v);
        tn = tn + 1;
        return 1;
    }
    int erase(__cpp_ref(T) v) {
        T *f = find(v);
        int i;
        if (f == td + tn) { return 0; }
        i = (int)(f - td);
        __cpp_drop(T, td[i]);
        if (tn - i - 1 > 0) {
            memmove(td + i, td + i + 1,
                    (unsigned long)((tn - i - 1) * (int)sizeof(T)));
        }
        tn = tn - 1;
        return 1;
    }
};
"""


# `<algorithm>`, as function templates over a `T *` range. A range is a pair
# of pointers because that is already what every container here hands out:
# `vector`, `ownvector`, `set` and `map` all iterate as `T *`, so these work
# on any of them without an iterator abstraction existing.
#
# Ordering goes through `__cpp_cmp`, so a class element supplies `compare`
# and these come with it -- there is no second predicate to pass and no
# comparator parameter, which would need a function type this subset cannot
# spell generically.
#
# Called with explicit arguments (`lower_bound<int>(..)`), since template
# argument deduction is not implemented; calling one without them is
# reported rather than blanked.
_STD_ALGORITHM = """
template<typename T>
T *lower_bound(T *first, T *last, __cpp_ref(T) v) {
    int lo = 0;
    int hi = (int)(last - first);
    int mid;
    while (lo < hi) {
        mid = lo + (hi - lo) / 2;
        if (__cpp_cmp(T, *(first + mid), v) < 0) { lo = mid + 1; }
        else { hi = mid; }
    }
    return first + lo;
}
template<typename T>
T *upper_bound(T *first, T *last, __cpp_ref(T) v) {
    int lo = 0;
    int hi = (int)(last - first);
    int mid;
    while (lo < hi) {
        mid = lo + (hi - lo) / 2;
        /* The one difference from `lower_bound`: `<= 0` rather than `< 0`,
           so an element equal to `v` moves the low end past it and the
           result is one *after* the last match. */
        if (__cpp_cmp(T, *(first + mid), v) <= 0) { lo = mid + 1; }
        else { hi = mid; }
    }
    return first + lo;
}
/* An insertion sort, moving elements by their representation rather than
   by assignment. `memmove` is what a *move* of an object is here: the
   element is not copied and not destroyed, it is relocated, so an owning
   element keeps its one owner and no copy constructor is required. An
   assignment-based sort would need `operator=` on the element and would
   destroy the object it overwrote.

   Insertion rather than quicksort because the recursion would need the
   template to call itself over its own parameter, which the instantiation
   scan cannot see through -- the same limit `binary_search` notes below.
   It is quadratic, and that is the honest cost of the restriction. */
template<typename T>
void sort(T *first, T *last) {
    int n = (int)(last - first);
    int i = 1;
    int j;
    char tmp[sizeof(T)];
    while (i < n) {
        j = i;
        memcpy(tmp, first + i, sizeof(T));
        while (j > 0) {
            if (__cpp_cmp(T, *(first + j - 1), __cpp_addr(T, *(T *)tmp)) <= 0) { break; }
            memmove(first + j, first + j - 1, sizeof(T));
            j = j - 1;
        }
        memcpy(first + j, tmp, sizeof(T));
        i = i + 1;
    }
}
/* `find`/`count` ask `__cpp_eq`, not `__cpp_cmp`. Searching a range does
   not need an order and `std::find` asks only for `==`, so requiring
   `compare` here would refuse a class that reasonably has equality and no
   ordering. The two builtins are separate for exactly this reason: a
   container that sorts says `__cpp_cmp`, one that only matches says
   `__cpp_eq`, and a class supplies whichever its uses need. */
template<typename T>
T *find(T *first, T *last, __cpp_ref(T) v) {
    T *it = first;
    while (it != last) {
        if (__cpp_eq(T, *it, v)) { return it; }
        it = it + 1;
    }
    return last;
}
template<typename T>
int count(T *first, T *last, __cpp_ref(T) v) {
    T *it = first;
    int n = 0;
    while (it != last) {
        if (__cpp_eq(T, *it, v)) { n = n + 1; }
        it = it + 1;
    }
    return n;
}
/* Swapping by representation, as `sort` moves by it: the two elements are
   relocated past each other rather than assigned, so an owning element
   keeps its one owner and no `operator=` is needed. */
template<typename T>
void reverse(T *first, T *last) {
    T *lo = first;
    T *hi = last - 1;
    char tmp[sizeof(T)];
    while (lo < hi) {
        memcpy(tmp, lo, sizeof(T));
        memmove(lo, hi, sizeof(T));
        memcpy(hi, tmp, sizeof(T));
        lo = lo + 1;
        hi = hi - 1;
    }
}
template<typename T>
void fill(T *first, T *last, __cpp_ref(T) v) {
    T *it = first;
    while (it != last) {
        __cpp_drop(T, *it);
        __cpp_copy(T, *it, v);
        it = it + 1;
    }
}
/* `end` for an empty range, which is what `std::min_element` returns and
   the only answer that does not invent an element. */
template<typename T>
T *min_element(T *first, T *last) {
    T *best = first;
    T *it = first;
    while (it != last) {
        if (__cpp_cmp(T, *it, __cpp_addr(T, *best)) < 0) { best = it; }
        it = it + 1;
    }
    return best;
}
template<typename T>
T *max_element(T *first, T *last) {
    T *best = first;
    T *it = first;
    while (it != last) {
        if (__cpp_cmp(T, *it, __cpp_addr(T, *best)) > 0) { best = it; }
        it = it + 1;
    }
    return best;
}
/* `swap` takes *pointers*, where `std::swap` takes references. A `T &`
   parameter is lowered to `T *` only for a class -- `names` there is the
   set of class names -- so `swap(int &a, int &b)` would reach the C with
   the `&` still on it. And `__cpp_ref(T)`, which does spell both, gives a
   scalar *by value*, which is exactly what a swap cannot have. Pointers
   are the one spelling that works for both, so the call site writes
   `swap(&a, &b)`.

   By representation, like every other relocation here, so an owning
   element keeps its one owner and needs no `operator=`. */
template<typename T>
void swap(T *a, T *b) {
    char tmp[sizeof(T)];
    if (a == b) { return; }
    memcpy(tmp, a, sizeof(T));
    memmove(a, b, sizeof(T));
    memcpy(b, tmp, sizeof(T));
}
/* Copy into a range that already holds constructed elements -- a
   container's, not raw storage. Each destination is destroyed before being
   constructed over, which is what assignment would have done; handing this
   a `T *` into memory nothing has constructed would destroy garbage, the
   same way `array<T,N>` of an owning element did before that was refused.
   Returns one past the last element written, as `std::copy` does. */
template<typename T>
T *copy(T *first, T *last, T *dst) {
    T *it = first;
    T *out = dst;
    while (it != last) {
        __cpp_drop(T, *out);
        __cpp_copy(T, *out, __cpp_addr(T, *it));
        it = it + 1;
        out = out + 1;
    }
    return out;
}
template<typename T>
int binary_search(T *first, T *last, __cpp_ref(T) v) {
    /* The search is repeated rather than delegated to `lower_bound<T>`.
       Instantiations are found by scanning for `name<args>(`, and inside a
       template body `T` is still the parameter -- so a call spelled
       `lower_bound<T>(..)` there is recorded as an instantiation over a
       type literally named `T`, and emitted as one. Until the scan can see
       through an unsubstituted parameter, a template calling another over
       its own parameter is not available. */
    int lo = 0;
    int hi = (int)(last - first);
    int mid;
    while (lo < hi) {
        mid = lo + (hi - lo) / 2;
        if (__cpp_cmp(T, *(first + mid), v) < 0) { lo = mid + 1; }
        else { hi = mid; }
    }
    if (first + lo == last) { return 0; }
    if (__cpp_cmp(T, *(first + lo), v) == 0) { return 1; }
    return 0;
}
"""

# A max-heap in an array, which is what `std::priority_queue` is too. The
# element never crosses a call boundary by value: `push` copies into the
# slot with `__cpp_copy`, and every sift step *relocates* with `memmove`
# rather than assigning, so an owning element keeps its one owner and needs
# no `operator=` -- the same argument `sort` makes.
#
# Sift by hole rather than by swapping. The element being placed is held
# aside in a buffer and the ones it passes are moved up (or down) into the
# hole, which is half the moves of a swap chain and, more importantly,
# never has two live copies of an owning object at once.
#
# `top()` returns a *pointer*, where `std::priority_queue` returns a
# reference. A reference return has no honest lowering in this subset --
# turning it into `T *` would silently change what assignment through the
# result means -- and is rejected, `operator[]` being the one exception
# because a by-value subscript would make `v[i] = x` write to a copy. So
# `top()` is spelled the way the subset can say it, and `q[0]` reaches the
# same element as an lvalue for anyone who wants one.
#
# There is no `T pop()` returning the element either: that would copy an
# object the caller never constructed, which is refused for an owning
# element and would be wrong for it anyway. Read the top, then `pop()`.
_STD_PRIORITY_QUEUE = """
template<typename T>
class priority_queue {
    T *qd;
    int qn;
    int qcap;
public:
    priority_queue() { qd = 0; qn = 0; qcap = 0; }
    ~priority_queue() { clear(); free(qd); qd = 0; qcap = 0; }
    int size() { return qn; }
    int empty() { if (qn == 0) { return 1; } return 0; }
    void clear() { while (qn > 0) { qn = qn - 1; __cpp_drop(T, qd[qn]); } }
    void reserve(int c) {
        if (c > qcap) {
            T *nd = (T *)realloc(qd, (unsigned long)c * sizeof(T));
            if (nd != 0) { qd = nd; qcap = c; }
        }
    }
    T *top() { return qd; }
    T &operator[](int i) { return qd[i]; }
    void push(__cpp_ref(T) v) {
        int i;
        int par;
        char tmp[sizeof(T)];
        if (qn == qcap) {
            int m = qcap * 2;
            if (m < 4) { m = 4; }
            reserve(m);
        }
        if (qn == qcap) { return; }
        __cpp_copy(T, qd[qn], v);
        qn = qn + 1;
        i = qn - 1;
        memcpy(tmp, qd + i, sizeof(T));
        par = (i - 1) / 2;
        while (i > 0) {
            if (__cpp_cmp(T, qd[par], __cpp_addr(T, *(T *)tmp)) >= 0) {
                break;
            }
            memmove(qd + i, qd + par, sizeof(T));
            i = par;
            par = (i - 1) / 2;
        }
        memcpy(qd + i, tmp, sizeof(T));
    }
    void pop() {
        int i;
        int l;
        int r;
        int big;
        char tmp[sizeof(T)];
        if (qn == 0) { return; }
        __cpp_drop(T, qd[0]);
        qn = qn - 1;
        if (qn == 0) { return; }
        /* The last element is the one looking for a home: the root's slot
           is the hole, and it sifts down into it. */
        memcpy(tmp, qd + qn, sizeof(T));
        i = 0;
        l = 1;
        while (l < qn) {
            big = l;
            r = l + 1;
            if (r < qn) {
                if (__cpp_cmp(T, qd[l], __cpp_addr(T, qd[r])) < 0) { big = r; }
            }
            if (__cpp_cmp(T, qd[big], __cpp_addr(T, *(T *)tmp)) <= 0) {
                break;
            }
            memmove(qd + i, qd + big, sizeof(T));
            i = big;
            l = i * 2 + 1;
        }
        memcpy(qd + i, tmp, sizeof(T));
    }
};
"""


# LIFO over the same growable array `vector` uses. An adapter in name only:
# `std::stack` wraps a container and forwards, and forwarding would need a
# member of a template type this subset would have to monomorphise twice
# over. The storage is three fields either way.
#
# `top()` returns a `T *`, not the reference `std::stack` returns, for the
# reason `priority_queue::top` does: a reference return is not in this
# subset. `s[0]` is the top as an lvalue for anyone who wants one.
_STD_STACK = """
template<typename T>
class stack {
    T *sk;
    int sn;
    int scp;
public:
    stack() { sk = 0; sn = 0; scp = 0; }
    ~stack() { clear(); free(sk); sk = 0; scp = 0; }
    int size() { return sn; }
    int empty() { if (sn == 0) { return 1; } return 0; }
    void clear() { while (sn > 0) { sn = sn - 1; __cpp_drop(T, sk[sn]); } }
    void reserve(int c) {
        if (c > scp) {
            T *nd = (T *)realloc(sk, (unsigned long)c * sizeof(T));
            if (nd != 0) { sk = nd; scp = c; }
        }
    }
    /* Index 0 is the top, so `s[0]` and `top()` agree. Counting down from
       the end rather than up from the start is what makes that true. */
    T &operator[](int i) { return sk[sn - 1 - i]; }
    T *top() { return sk + sn - 1; }
    void push(__cpp_ref(T) v) {
        if (sn == scp) {
            int m = scp * 2;
            if (m < 4) { m = 4; }
            reserve(m);
        }
        if (sn == scp) { return; }
        __cpp_copy(T, sk[sn], v);
        sn = sn + 1;
    }
    void pop() { if (sn > 0) { sn = sn - 1; __cpp_drop(T, sk[sn]); } }
};
"""


# FIFO as a head index into the same array, rather than a ring. A ring
# would wrap, and a wrapped range cannot be handed out as a pointer pair --
# which is the iteration every other container here offers. Instead the
# live range slides back down to the front when the array fills, so the
# cost is paid once per growth rather than on every access, and `front()`
# stays a plain pointer into the buffer.
_STD_QUEUE = """
template<typename T>
class queue {
    T *cd;
    int chd;
    int ctl;
    int ccp;
public:
    queue() { cd = 0; chd = 0; ctl = 0; ccp = 0; }
    ~queue() { clear(); free(cd); cd = 0; ccp = 0; }
    int size() { return ctl - chd; }
    int empty() { if (ctl == chd) { return 1; } return 0; }
    void clear() {
        while (ctl > chd) { ctl = ctl - 1; __cpp_drop(T, cd[ctl]); }
        chd = 0;
        ctl = 0;
    }
    void reserve(int c) {
        if (c > ccp) {
            T *nd = (T *)realloc(cd, (unsigned long)c * sizeof(T));
            if (nd != 0) { cd = nd; ccp = c; }
        }
    }
    T *front() { return cd + chd; }
    T *back() { return cd + ctl - 1; }
    T &operator[](int i) { return cd[chd + i]; }
    T *begin() { return cd + chd; }
    T *end() { return cd + ctl; }
    void push(__cpp_ref(T) v) {
        if (ctl == ccp) {
            if (chd > 0) {
                /* Reclaim the space popped elements left at the front,
                   by relocation -- the elements are moved, not copied and
                   destroyed, so an owning element keeps its one owner. */
                memmove(cd, cd + chd,
                        (unsigned long)((ctl - chd) * (int)sizeof(T)));
                ctl = ctl - chd;
                chd = 0;
            }
        }
        if (ctl == ccp) {
            int m = ccp * 2;
            if (m < 4) { m = 4; }
            reserve(m);
        }
        if (ctl == ccp) { return; }
        __cpp_copy(T, cd[ctl], v);
        ctl = ctl + 1;
    }
    void pop() {
        if (ctl > chd) { __cpp_drop(T, cd[chd]); chd = chd + 1; }
    }
};
"""


# Fixed size, from the non-type template parameter -- `N` is replaced by
# the literal the use site spelled, since monomorphisation is textual.
#
# The elements are a plain array member, and this subset leaves array
# members to the author: they are not constructed with the container and
# not destroyed with it. So `array<T,N>` holds *plain data*. An owning
# element wants `vector<T>`, which does construct and destroy what it
# holds. That is a real difference from `std::array` and is why there is
# no `clear()` here to imply otherwise.
_STD_ARRAY = """
template<typename T, int N>
class array {
public:
    T ad[N];
    int size() { return N; }
    int empty() { if (N == 0) { return 1; } return 0; }
    T &operator[](int i) { return ad[i]; }
    T *data() { return ad; }
    T *begin() { return ad; }
    T *end() { return ad + N; }
    T *rbegin() { return ad + N - 1; }
    T *rend() { return ad - 1; }
    void fill(__cpp_ref(T) v) {
        int i = 0;
        while (i < N) { __cpp_copy(T, ad[i], v); i = i + 1; }
    }
};
"""


# The value lives behind a pointer rather than in a `T` member. A member of
# class type is constructed with its container and destroyed with it, by
# this subset's own rules -- which is exactly what an `optional` must not
# do, since an empty one holds nothing to destroy and `reset()` would then
# be a second destruction of an object the epilogue also destroys. A
# pointer makes "has a value" and "owns a value" the same fact.
#
# The cost is an allocation per engaged value, which `std::optional` does
# not pay. It is the honest price of not having placement new or a union
# with a non-trivial member.
_STD_OPTIONAL = """
template<typename T>
class optional {
    T *op;
public:
    optional() { op = 0; }
    ~optional() { reset(); }
    int has_value() { if (op != 0) { return 1; } return 0; }
    void reset() {
        if (op != 0) { __cpp_drop(T, *op); free(op); op = 0; }
    }
    void set(__cpp_ref(T) v) {
        reset();
        op = (T *)malloc(sizeof(T));
        if (op != 0) { __cpp_copy(T, *op, v); }
    }
    /* Null when empty, which is the check `has_value()` makes too. There
       is no `T value()` returning by value: that would copy an object the
       caller never constructed, refused for an owning element. */
    T *value() { return op; }
};
"""


# `<numeric>`, separately from `<algorithm>` now that it is more than one
# function. Everything here combines elements arithmetically, so the
# element type has to be one `+` and `*` apply to -- a scalar. A class
# would need `operator+`, which is not in this subset, so a class element
# is reported rather than left to fail as C.
_STD_NUMERIC = """
template<typename T>
T accumulate(T *first, T *last, T init) {
    T *it = first;
    T sum = init;
    while (it != last) {
        /* `sum += *it` rather than `sum = sum + *it`: the second needs an
           operator result assigned from an operand that is a dereference,
           and neither has an address to pass. Compound assignment writes
           into `sum` in place, which is what this wants anyway, and it
           means a class element only has to supply `operator+=`. */
        sum += *it;
        it = it + 1;
    }
    return sum;
}
/* Fill with successive values from `start`, as `std::iota` does. */
template<typename T>
void iota(T *first, T *last, T start) {
    T *it = first;
    T v = start;
    while (it != last) {
        *it = v;
        v = v + 1;
        it = it + 1;
    }
}
/* The sum of products of two ranges. The second is given by its start
   only, as `std::inner_product` takes it -- it is required to be at least
   as long as the first, which nothing here can check. */
template<typename T>
T inner_product(T *first, T *last, T *first2, T init) {
    T *it = first;
    T *it2 = first2;
    T sum = init;
    while (it != last) {
        sum = sum + (*it) * (*it2);
        it = it + 1;
        it2 = it2 + 1;
    }
    return sum;
}
/* Running totals into `dst`, returning one past the last written.
   Assignment rather than `__cpp_copy`, because the value written is a sum
   rather than a copy of an element -- which is another way of saying
   these are for scalars. `dst` may be `first`, and the read of the
   element happens before the write, so writing in place is safe. */
template<typename T>
T *partial_sum(T *first, T *last, T *dst) {
    T *it = first;
    T *out = dst;
    T sum;
    if (it == last) { return out; }
    sum = *it;
    *out = sum;
    it = it + 1;
    out = out + 1;
    while (it != last) {
        sum = sum + *it;
        *out = sum;
        it = it + 1;
        out = out + 1;
    }
    return out;
}
/* The difference between each element and the one before it. The previous
   element is held in a local before the write, so this too may run in
   place. */
template<typename T>
T *adjacent_difference(T *first, T *last, T *dst) {
    T *it = first;
    T *out = dst;
    T prev;
    T cur;
    if (it == last) { return out; }
    prev = *it;
    *out = prev;
    it = it + 1;
    out = out + 1;
    while (it != last) {
        cur = *it;
        *out = cur - prev;
        prev = cur;
        it = it + 1;
        out = out + 1;
    }
    return out;
}
"""


_STD_INCLUDE = re.compile(
    r"^[ \t]*#\s*include\s*<(vector|string|memory|map|set|algorithm"
    r"|queue|stack|array|optional|unordered_map|unordered_set"
    r"|numeric"
    r"|utility)>[ \t]*\n?",
    re.M)


_STD_CLASSES = frozenset(("string", "vector", "ownvector",
                          "unique_ptr", "shared_ptr", "pair", "map", "set",
                          "priority_queue", "stack", "queue", "array",
                          "optional", "enable_shared_from_this"))


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


#: `unordered_map` and `unordered_set` are the ordered ones under another
#: name. Neither hashes: there is no `hash<T>` here and no way to write one
#: generically in this subset, so what would be supplied is a container
#: with the unordered *interface* and the ordered container's behaviour.
#:
#: Saying so by aliasing is more honest than a separate copy that pretends
#: otherwise. The difference an author can observe is iteration order --
#: `std::unordered_map` promises none, these iterate sorted, and code that
#: relies on no order is not broken by getting one. The difference they
#: cannot observe is complexity: these are O(log n) lookups, not O(1).
_STD_UNORDERED = {"unordered_map": "map", "unordered_set": "set"}


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
    # `<set>` names the header, `set` the class -- same rule as `<map>`.
    wanted.discard("set")
    # `<queue>` is the header; `priority_queue` and `queue` are the classes,
    # and the header shares its name with one of them -- so including it is
    # not by itself a request for `queue`, which the probe below decides.
    wanted.discard("queue")
    # Same for the rest: header named, class asked for by use.
    wanted.discard("stack")
    wanted.discard("array")
    wanted.discard("optional")
    wanted.discard("unordered_map")
    wanted.discard("unordered_set")
    # `<algorithm>` is the header *and* the only way to ask for these: they
    # are free functions, so there is no `std::algorithm` spelling for the
    # probe below to find. Including it is the request.
    algorithm = "algorithm" in wanted
    wanted.discard("algorithm")
    # `<numeric>` the same way -- and *also* by name, unlike `<algorithm>`.
    # `accumulate` lived in `<algorithm>` here until this header existed,
    # so a file that included only that one and called it would otherwise
    # have stopped compiling, with a link error naming a function the
    # author did write. Answering to the name as well costs nothing and
    # keeps `std::accumulate` meaning what it did.
    numeric = "numeric" in wanted
    wanted.discard("numeric")
    probe = _blank_strings(_strip_comments(text))
    for fn in ("accumulate", "iota", "inner_product", "partial_sum",
               "adjacent_difference"):
        if re.search(r"\bstd\s*::\s*%s\b" % fn, probe):
            numeric = True
    # Rewritten to the ordered spelling before anything looks for classes,
    # so every pass below sees one container rather than two names for it.
    for un, real in _STD_UNORDERED.items():
        if re.search(r"\b(?:std\s*::\s*)?%s\b" % un, probe):
            text = _sub_code(re.compile(r"(?<![\w])%s(?![\w])" % un),
                             real, text)
            wanted.add(real)
    probe = _blank_strings(_strip_comments(text))
    for name in ("string", "vector", "ownvector", "unique_ptr",
                 "shared_ptr", "pair", "map", "set", "priority_queue",
                 "stack", "queue", "array", "optional",
                 "enable_shared_from_this"):
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
    if not wanted and not algorithm and not numeric:
        return bool_prefix + text
    # Blanked to the same *line* count, not cut out. Removing the line
    # shifted every line of the author's file up by one per `#include`
    # dropped, so a diagnostic named a line two or three above the one it
    # meant -- and the more headers a file used, the further off it got.
    text = _STD_INCLUDE.sub(
        lambda m: "\n" if m.group(0).endswith("\n") else "", text)
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
    if "set" in wanted:
        parts.append(_STD_SET)
    if "priority_queue" in wanted:
        parts.append(_STD_PRIORITY_QUEUE)
    if "stack" in wanted:
        parts.append(_STD_STACK)
    if "queue" in wanted:
        parts.append(_STD_QUEUE)
    if "array" in wanted:
        parts.append(_STD_ARRAY)
    if "optional" in wanted:
        parts.append(_STD_OPTIONAL)
    if algorithm:
        parts.append(_STD_ALGORITHM)
    if numeric:
        parts.append(_STD_NUMERIC)
    # The marker goes last, immediately above the author's first line, so
    # that everything supplied above it is what a line number counts past.
    return "".join(parts) + _SRC_MARK_DECL + text


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
                       _src_line(look, m.start()))
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
              incdirs=(), defines=(), clang=None, rtti=False, decls=(),
              decls_out=None, contracts=False, mem_safe=False):
    """Translate a C++ subset source to C. Raises CppError on anything else.

    `owning` maps the name of a type this file does *not* define to the
    function that destroys one -- the types Crust lowered that own a buffer,
    handed over so a C++ class holding one by value is destroyed like any
    other member, and refuses to be copied for the same reason.

    `rtti` prefixes every vtable with a type descriptor, which is what
    `dynamic_cast` and `typeid` read. It is off by default: it costs one
    static descriptor per polymorphic class and nothing per object, but
    "nothing per object" is only true because the vptr a polymorphic class
    already carries *is* the descriptor pointer -- so a class with no
    virtuals gains no header and cannot be asked its type, exactly as in
    C++, where `dynamic_cast` requires a polymorphic operand.
    """
    # `std::string` / `std::vector` are supplied as ordinary subset source,
    # so everything below sees one file with no special cases in it.
    # Headers first, before anything reads a declaration: a member declared
    # in one and defined here has to arrive in the same translation.
    # An `--incdir` is enough on its own: an angle include is searched only
    # there, so a caller that supplies a path but no basedir still gets its
    # headers spliced.
    # Before anything reads the source. A digest that cannot be read or is
    # the wrong version is a caller error, and reporting it should not
    # depend on whether this particular file happens to declare a class --
    # which is what deferring it to the class table did.
    external = load_decls(decls)
    if basedir is not None or incdirs:
        text = _expand_headers(text, basedir or ".", incdirs,
                               defines=set(defines or ()))
    # Linkage specifications go now: after the splice, so a header that
    # wrapped its body in `extern "C" { .. }` is unwrapped too, and before
    # every pass below, all of which read declarations that one would still
    # be hiding behind a string literal.
    text = _strip_extern_c(text)
    # `class __coapi fastring : ..` -- an export macro between the keyword
    # and the name, which every class scan below would read as the name.
    text = _strip_attribute_macros(text)
    # Before the templates are instantiated: a function template's copies
    # are named by their arguments and two of them may still share a name,
    # but that is a *mangling* question with its own answer, not a source
    # written with two definitions of one free function.
    # Comment-stripped but *not* directive-blanked: the check needs to see
    # `#if` / `#else` to know which definitions are alternatives.
    _check_free_overloads(_strip_comments(text), path)
    # A leading `::` qualifies a name with *global* scope. C has only the
    # one scope, so the marker is dropped -- but it has to go before any
    # pass reads names, because coost's allocator reaches the C library
    # with `::free(p)` from a class that has its own `free`, and namespace
    # flattening turned that into `::this->co_free(p)`: invalid C, and the
    # wrong function. The lookbehind keeps `a::b` intact.
    text = re.sub(r"(?<![\w:])::(?=\w)", "__gsq__", text)
    # `constexpr` asks for compile-time evaluation; C has no such keyword
    # and the lowering emits an ordinary definition either way. Dropped at
    # file scope as well as on members -- coost's `mem.h` writes
    # `constexpr size_t co_cache_line_size = ..` outside any class, which
    # the member-level strip never saw and which reached the C front end
    # verbatim.
    text = re.sub(r"(?<![\w])constexpr(?![\w])\s*", "", text)
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
    # `return Cls(a, b);` becomes a named local before anything reads
    # declarations, so the ordinary initialiser lowering handles it.
    text = _materialise_ctor_returns(text, _strip_comments(text))
    text = _lower_functional_casts(text, _strip_comments(text))
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
        # Casts are rewritten inside `#define` bodies too, which is why the
        # blank here keeps directive lines. `static_cast<T>(e)` -> `((T)(e))`
        # is a token-local rewrite that does not depend on the surrounding
        # structure, and a macro body is exactly where coost's vendored
        # `dtoa_milo.h` puts them -- `UINT64_C2(h, l)` expands to two, and
        # every one of its 181 uses became a `<` comparison in the C.
        text = cpp_auto.resolve_casts(
            text, os.path.basename(path),
            blank=cpp_auto._blank_like(text, directives=False))
    except cpp_auto.AutoError as e:
        raise CppError(e.message)
    # Again, after the typedefs have been expanded: `u64(1)` only becomes
    # a recognisable `unsigned long long(1)` at that point, and the first
    # pass above could not see it. Idempotent -- an already-lowered
    # `((int)(x))` has its type followed by `)`, not `(`.
    text = _lower_functional_casts(text, _strip_comments(text))
    text = _materialise_ctor_temporaries(text, _strip_comments(text), path)
    scan = _blank_literal_braces(_strip_comments(text))
    text, _exc_used = _lower_except(text, path)
    if _exc_used:
        text = _EXC_PRELUDE + text
    # Directives blanked before any class is looked for. coost's
    # `DEF_has_method(f)` macro body holds `struct _has_method_##f { struct
    # _R_ { .. }; .. }`, and the class collector read those as real classes:
    # the emitter then rewrote inside the macro, breaking its backslash
    # continuation chain so the tail stopped being a `#define` body and
    # reached the C front end as code. The directives themselves still pass
    # through untouched; only the *finding* is blind to them.
    scan = _blank_directives(_strip_comments(text))
    _check_unsupported(scan, path, rtti=rtti)

    # Out-of-line member definitions come out first, keyed by class. They
    # have to be in hand before any class is emitted, and lifting them also
    # keeps the class scan below from seeing a definition where it expects a
    # declaration.
    cls_names = set(re.findall(r"\b(?:class|struct)\s+(\w+)", scan))
    text, scan, outline = _extract_out_of_line(text, scan, cls_names)

    classes = _find_classes(scan, text, path)
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
    # Directives blanked for the same reason as everywhere else: coost's
    # `DISALLOW_COPY_AND_ASSIGN` macro body says `= delete`, and this scan
    # read it as heap use in a file with no classes and refused it.
    heap = _blank_directives(_blank_strings(_strip_comments(text)))
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
        # Nothing to lower -- unless a supplied *free* template left a
        # builtin behind. `__cpp_cmp` and friends are expanded by the call
        # rewriting below, which this return skips; a file that uses only
        # `<algorithm>` defines no class, so returning here emitted
        # `__cpp_cmp(int, ..)` into the C and let the compiler report it
        # against generated code the author never wrote. Reported here
        # instead, against something they can act on.
        # Falling through with an empty class list does *not* work: the
        # emission path below is guarded again further down and never
        # reaches the call rewriting, so the builtin survives just the
        # same. Restructuring that is a real change and not this one's, so
        # the limitation is reported rather than half-fixed.
        left = re.search(r"(?<![\w.>])(__cpp_\w+)\s*\(",
                         _strip_comments(text))
        if left:
            raise CppError(
                "%s: `%s` survived: it is expanded while lowering classes, "
                "and this file defines none. `lower_bound` and friends "
                "compare elements through it, so a file using only "
                "`<algorithm>` has nothing to hang that pass off. Use them "
                "on a container (`<vector>`, `<set>`), which supplies one."
                % (os.path.basename(path), left.group(1)))
        # Same last step as the class path below. A file of free kernels --
        # which is what a numeric library's bottom layer is -- defines no
        # classes and takes this exit, so inferring contracts only on the
        # other side would have skipped exactly the functions the inference
        # exists for.
        return _drop_global_scope(
            _auto_contracts(text) if contracts else text)

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
    # And classes from the other language, read from a digest. Seeded the
    # same way and for the same reason: a class emitted below should see one
    # exactly as it sees a class declared above it, so that inheriting from
    # an rpython class is not a special case anywhere further down.
    cinfo.update(external)
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
            chained, cls.name in std_classes, rtti=rtti)
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
        # Re-anchor. The generated C above does not have the same number of
        # lines the class was written on, so from here down a count from
        # the previous anchor would be off by the difference -- and by the
        # sum of every class's difference further on. An anchor naming the
        # line the class ended on costs one line of output and makes the
        # count exact again for everything below it.
        # Not the line *after* the class: `end` sits just past its closing
        # brace, and the source resumes there -- with the `;` and the rest
        # of that same line still to come. So the anchor names the line the
        # brace is on, and the newline that ends it is counted like any
        # other.
        pieces.append(_src_mark(_src_line(text, end)))
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
    if rtti:
        # Above the forward declarations, not with them: every vtable struct
        # names `struct _CppTypeInfo` in its first row, so the descriptor has
        # to be complete before the first of them. Emitted whenever the flag
        # is on rather than when a cast is found -- the helpers are `static
        # inline`, so an unused one costs nothing and warns about nothing.
        fwd.insert(0, _RTTI_PRELUDE)
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
        # Same reasoning for `offsetof`, which an interface thunk uses to
        # step from a secondary base's vptr back to the whole object.
        if "offsetof" in head or any("offsetof" in p for p in pieces):
            head = "#include <stddef.h>\n" + head
        # And for `size_t`, which is the commonest of the three: coost's
        # allocators take one in every method, so every hoisted prototype
        # named a type the C had not been told about yet -- the `#include
        # <stddef.h>` that would have was several lines *below* them.
        # stddef.h is idempotent, so carrying one along is safe even when
        # the source already has it.
        elif re.search(r"(?<![\w])(?:size_t|ptrdiff_t)(?![\w])", head):
            head = "#include <stddef.h>\n" + head
        # The fixed-width types are the same story one header along:
        # coost's `dtoa_milo.h` takes a `uint64_t` in every constructor, so
        # the hoisted prototypes named a type the C had not been told about.
        if re.search(r"(?<![\w])u?int(?:8|16|32|64)_t(?![\w])", head):
            head = "#include <stdint.h>\n" + head
        pieces.insert(0, head)
    out = "".join(pieces)

    if decls_out:
        # Here, not at the end: this is the point where every class this
        # file defines has an entry and none of the later passes add or
        # remove one. Written even if a later pass then refuses the file --
        # the digest describes declarations, and a body that does not lower
        # does not change what this file declares.
        import json as _json
        digest = dump_decls(cinfo,
                            os.path.splitext(os.path.basename(path))[0])
        with open(decls_out, "w") as _df:
            _json.dump(digest, _df, indent=1, sort_keys=True)
        # Publishing a class *is* a linkage decision. Everything cpprust
        # emits is `static`, which is right for a self-contained unit and
        # wrong the moment another translation unit -- py2c's, importing
        # this module through the digest -- calls `Pool_take` by symbol:
        # an `extern` declaration followed by a `static` definition in the
        # combined build is a hard error, and separate objects simply fail
        # to link. So the symbols the digest names, and only those, are
        # given external linkage here. Thunks stay static: they are
        # reached through the table, never by name.
        #
        # Applied to the assembled text at the very end of `translate`, not
        # to `pieces` here: the prototypes and the vtable live in the
        # forward-declaration block, which is only spliced in front of the
        # pieces later, so a rewrite at this point ran over text that did
        # not yet contain most of what it was for. (It did exactly nothing,
        # which is how it was caught: `nm` showed every symbol still
        # lowercase.)
        _publish = (digest, rtti)

    # Rewrite uses: `Ring<int> r;` -> `Ring_int r;`. Field types were already
    # normalised through `tsub` while their class was emitted; this catches
    # the rest -- locals, parameters, and method bodies copied through
    # verbatim.
    out = _monomorphise_uses(out, tnames, known=wanted)

    # Which free functions take a reference? Collected before lowering, while
    # a `&` is still on the page.
    free_refs = _free_ref_funcs(_strip_comments(out), names)
    # The return type of each function the template monomorphiser emitted,
    # so a call to one can be chained onto. Read back off the definition
    # rather than threaded down from the substitution: what was substituted
    # is `T *`, and the concrete spelling is what is on the page now.
    free_rets = {}
    for fn in _ftmpl:
        dm = re.search(r"(?<![\w])(\w+)\s*(\*\s*)?%s\s*\("
                       % re.escape(fn), _strip_comments(out))
        if dm and dm.group(1) in cinfo:
            free_rets[fn] = (dm.group(1), bool(dm.group(2)))
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

    # `array<T,N>` holds its elements in a plain array *member*, and this
    # subset leaves array members to the author: they are neither
    # constructed with the container nor destroyed with it. For plain data
    # that is exactly `std::array`. For a class that owns something it is
    # not: the elements start as whatever the stack held, so the first
    # `fill` copy-constructs over a garbage destination and follows a wild
    # pointer -- a segfault rather than a diagnostic.
    #
    # A destructor is the test, as everywhere else here: a class with one
    # owns something, and a class without owns nothing and copies bitwise.
    for targs in wanted.get("array", []):
        elem = targs[0]
        ent = cinfo.get(elem)
        if ent is not None and (ent["dtor"] or ent["ctor"]):
            raise CppError(
                "%s: `array<%s, %s>` holds its elements in a plain array "
                "member, which this subset does not construct or destroy, "
                "and %s has a constructor or destructor. Use `vector<%s>`, "
                "which constructs and destroys what it holds."
                % (os.path.basename(path), elem, targs[1], elem, elem))

    # After reference lowering, a class still spelled by value really is by
    # value -- a `T &` the author wrote is a `T *` by now.
    # Against the directive-blanked text: a `#define`'s replacement is
    # not an expression this translation unit evaluates.
    byval = _check_by_value(_blank_directives(out), cinfo, path)
    # Before scope and call rewriting, not after: `dynamic_cast<T *>(e)` has
    # angle brackets and a `::`-free type name, which the scope rewriter has
    # no reason to understand, and `typeid(x).name()` looks exactly like a
    # method call on an object named `typeid` to the call rewriter. Lowering
    # both here leaves those passes nothing unusual to see.
    if rtti:
        out = _lower_rtti(out, cinfo, path)
    out = _rewrite_scopes(out, cinfo, path)

    # Rewriting a call copies its arguments through verbatim, so a receiver
    # nested in an argument list surfaces on the next pass. Iterate to a
    # fixed point rather than recursing into every argument.
    for _ in range(8):
        nxt = _rewrite_calls(out, cinfo, free_refs, free_rets, path)
        if nxt == out:
            break
        out = nxt
    # After the rewriting, not before: a conversion this pass *could*
    # adjust has already been lowered by now, so what is left is exactly
    # what it could not, which is what this reports.
    _check_ibase_conversions(out, cinfo, path)

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
    # The origin marker has done its job -- every diagnostic that was going
    # to be raised has been. Blanked rather than cut, so the C keeps the
    # line numbering the diagnostics used and a `#line` directive or a
    # debugger still lands where the messages said.
    # Every anchor, not just the origin one: a re-anchor is emitted after
    # each class too, and stripping only the first left the rest in the C
    # as stray typedefs. Blanked to a bare newline rather than cut, so the
    # output keeps the line numbering the diagnostics were counted against.
    if mem_safe:
        # --mem-safe: turn the origin anchors into real `#line` directives
        # instead of blanking them.
        #
        # This is what makes the C++ tier work on standalone output. The
        # compiler tells C++-derived code from hand-written C by the file name
        # on each position, and it gets that for free when a `.cpp` is
        # `#include`d, because the lowering happens inside the same compile.
        # Run separately, `cpprust.py foo.cpp -o foo.c` throws that away: the
        # C looks like C. The anchors already record which source line each
        # output line came from -- they are what the diagnostics here are
        # counted against -- so re-emitting them as `#line` hands the same
        # information downstream, and puts the right file *and* line on every
        # report rather than the position in the generated C.
        #
        # A blanket `#line 1 "foo.cpp"` would not do: the output is not line
        # for line with the input (a four-line class body becomes seven lines
        # of C), so the file would be right and every line wrong.
        src_name = path.replace("\\", "/")

        def _to_line_directive(m):
            return '#line %s "%s"' % (m.group(1), src_name)

        out = re.sub(r"typedef int __crust_src_line_(\d+)__;",
                     _to_line_directive, out)
        # A leading directive as well, because the anchors are placed per
        # class rather than per line and the declarations hoisted above the
        # first one would otherwise still read as generated C -- including
        # every method body, which is exactly where the checks belong. With
        # this, file attribution is complete; line numbers are exact from each
        # anchor onward and approximate in the reordered preamble.
        out = '#line 1 "%s"\n' % (src_name,) + out
    out = _SRC_MARK_RE.sub("", out).replace("typedef int ;\n", "\n")
    if decls_out:
        out = _publish_decls_linkage(out, _publish[0], _publish[1])
    # Last, over the assembled text: a kernel reaches its final parameter
    # spelling only after monomorphisation, so the bound this reads
    # (`float d[16]`) is what the substitution produced. Running earlier
    # would have seen `T d[N]` and been able to infer nothing.
    if contracts:
        out = _auto_contracts(out)
    return _drop_global_scope(out)


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
    rtti = False
    decls = []
    decls_out = None
    if "--emit-decls" in args:
        i = args.index("--emit-decls")
        if i + 1 >= len(args):
            sys.stderr.write("cpprust: --emit-decls needs a path\n")
            return 2
        decls_out = args[i + 1]
        del args[i:i + 2]
    while "--decls" in args:
        i = args.index("--decls")
        if i + 1 >= len(args):
            sys.stderr.write("cpprust: --decls needs a file\n")
            return 2
        decls.append(args[i + 1])
        del args[i:i + 2]
    want_contracts = False
    if "--contracts" in args:
        want_contracts = True
        args.remove("--contracts")
    want_mem_safe = False
    if "--mem-safe" in args:
        want_mem_safe = True
        args.remove("--mem-safe")
    if "--rtti" in args:
        rtti = True
        args.remove("--rtti")
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
                         "[--incdir DIR].. [-D NAME].. [--rtti] "
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
        result = translate(text, path=src, mem_safe=want_mem_safe,
                           owning=owning,
                           basedir=basedir, incdirs=incdirs,
                           defines=defines, clang=clang, rtti=rtti,
                           decls=decls, decls_out=decls_out,
                           contracts=want_contracts)
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
