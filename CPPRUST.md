# cpprust — C++ syntax

Crust lets one translation unit hold C functions, Rust functions, and C++
classes side by side. A C file pulls in a C++ module with an ordinary
include:

```c
#include "guard.cpp"                     /* lowered to C, then included */
```

`tools/cpprust.py` translates the file to C before the compiler proper sees
it, so everything downstream — the IL, the register allocator, the
whole-program passes — sees one C translation unit with no boundary in it.

```sh
python3 tools/cpprust.py guard.cpp -o guard.c    # or use the include above
```

## Why C++ is here at all

Crust now has a `Drop` trait of its own, so this is no longer the only place
in the project where scope exit can run code. What C++ still brings is a
*richer* object model around the same idea: constructors chosen by arity,
copy construction and `operator=`, member and base construction ordering,
inheritance and virtual dispatch. Where a Rust `impl Drop` gives a type a
destructor, a C++ class gives it a whole lifecycle.

The two meet at the symbol. The lowering is deliberately the same shape
Crust uses for `impl` blocks — a method becomes `Class_method(Class *this,
..)`, a template becomes one struct per instantiation. That is not a
coincidence. It means a C++ class and a Rust `impl` over the same data
produce the same C, so the two can be mixed in one unit without a shim.

That extends to destruction: a Rust `impl Drop for T` lowers to
`T_drop(T *self)`, which is exactly what `~T()` lowers to here. So a C++
class may hold a Crust type **by value** and its member epilogue calls the
Rust destructor directly:

```cpp
class Holder {
public:
    Vec_int nums;                       /* a Crust `Vec<i32>`, by value */
    Res r;                              /* a Crust type with `impl Drop`  */
    Holder() { nums = Vec_int_new(); }
    ~Holder() { Vec_int_free_buf(&nums); Res_drop(&r); }
};
```

No forward declarations are needed. Crust places its prelude above the first
`#include` of a C++ file and emits a `#line` directive so the original line
numbering resumes, which is what makes the struct complete here without
moving anybody's diagnostics; and it seeds the instantiations this file names
even when no Rust or C in the unit mentions them. Note the spelling: a `.cpp`
names the *lowered* type, `Vec_int`, not `Vec<i32>` — the latter is Rust
syntax, and `<>` here means a template of this subset's own.

One ordering difference is worth knowing. Members are destroyed in **reverse
declaration order**, because that is C++'s rule; Crust's field glue frees in
**declaration order**, because that is Rust's. The two languages disagree,
and each side follows its own source language rather than one being made to
match the other. The symbol is shared; the order is not.

### Owning a Crust value, not just pointing at one

The destructor above was written by hand. It need not be. Crust publishes the
types it lowered that own something, and the preprocessor passes them on the
command line:

```sh
python3 tools/cpprust.py t.cpp -o t.c --owning Vec_int:Vec_int_free_buf,Res:Res_drop
```

This module runs as a subprocess and cannot see the unit being compiled, so
it has to be told; the protocol stays one file and one exit status, and this
is only how the caller names the foreign types that own something. Each name
maps to the function that destroys one — `T_drop` for a user `impl Drop`, but
a bundled container keeps its own spelling (`Vec_int_free_buf`), so it is
recorded rather than assumed.

A member of such a type is then destroyed with its container like any other,
which means a class can own Crust values and declare no destructor at all:

```cpp
class Tally {
public:
    Vec_int samples;                  /* a Crust `Vec<i32>` */
    Res mark;                         /* a Crust `impl Drop` type */
    void add(int v) { Vec_int_push(&samples, v); }
};
```

```c
static void Tally_drop(Tally *this) { Res_drop(&this->mark);
                                      Vec_int_free_buf(&this->samples); }
```

It also means the **copy rules apply**. A class owning a Crust value has a
destructor, so copying it without a copy constructor is refused for exactly
the reason any other owning class is:

```
`Tally b(a)`: Tally has a destructor but no copy constructor, so copying it
would leave two objects owning one resource and destroy it twice. Add
`Tally(const Tally &o)`, or pass by reference (`Tally &`).
```

Without the mapping nothing changes: the member is plain data and the class
is left exactly as it was, so a `.cpp` that manages the lifetime itself is
unaffected. `examples/crust/ownmember.cpp` is the owning shape;
`examples/crust/owned.cpp` remains the borrowing one.

### Handing an owned value to Rust

**Refused:** passing an owned object by value to a function this file does
not define. It is the cross-language shape of the double free Crust fixed on
its own side, and it aborted rather than leaked:

```cpp
int go(void) {
    Tally t;  t.start();  t.add(1);
    return consume(t.samples);        /* a Rust `fn consume(v: Vec<i32>)` */
}
```

Crust lowers a by-value owning parameter to a drop when the callee returns —
passing by value is a *move* there — so `consume` frees the buffer, and
`Tally_drop` frees it again on the way out. Refused for the same reason
`_check_by_value` refuses this pass's own by-value owning parameters: doing
it properly means moving out of the source, and this is expression position.

Pass `&t.samples` instead. A Rust `&Vec<i32>` parameter lowers to exactly
that pointer, so a reference-taking signature needs no change on either side.

The check runs on the *lowered* text, and only for callees this file did not
declare. That is what keeps it precise: a by-reference call has already
become `f(&v)` by then, `Buf c(a);` has already become
`Buf c; Buf_copy(&c, &a);`, and a constructor or method call is this pass's
own business rather than a boundary.

## The guiding rule

**Anything the lowering cannot do correctly is reported, not approximated.**

A source-to-source translation has real limits: it does not resolve types
the way a compiler does, and it works on text. Where that runs out, this
translator raises an error naming the reason and the fix, rather than
emitting C that compiles and does something subtly different. Every
"refused" below is a deliberate choice of a diagnostic over a silent
miscompile, and most of them exist because the alternative was found to be
wrong in practice.

---

## Classes

```cpp
class Counter {
    int n;                              // private by default
public:
    Counter() { n = 0; }
    Counter(int start) { n = start; }
    ~Counter() { n = 0; }
    void bump(int by) { n = n + by; }
    int get() { return n; }
};
```

lowers to a `struct` plus free functions:

```c
struct Counter { int n; };
static void Counter_new(Counter *this) { this->n = 0; }
static void Counter_new_1(Counter *this, int start) { this->n = start; }
static void Counter_drop(Counter *this) { this->n = 0; }
static void Counter_bump(Counter *this, int by) { this->n = this->n + by; }
static int Counter_get(Counter *this) { return this->n; }
```

`public:` / `private:` / `protected:` are parsed but **not enforced**. Access
control is a compile-time property and this is a lowering, not a type
checker; claiming to enforce it would be worse than not claiming it.

Members are prototyped before they are defined, so a method may call one
declared below it.

## Constructors, destructors, and scope

A local `Type name(args);` becomes a constructor call at the declaration and
a destructor call at the closing `}` of the enclosing block:

```cpp
void f(void) {
    Counter c(5);
    c.bump(1);
}                                       // ~Counter() runs here
```
```c
void f(void) {
    Counter c; Counter_new_1(&c, 5);
    Counter_bump(&c, 1);
    Counter_drop(&c);
}
```

Drops run on **every** exit from a scope: the closing brace, and also
`return`, `break` and `continue`. `return` unwinds out to the function,
`break` to the enclosing loop or switch, `continue` to the enclosing loop. A
`return` with a value spills it to a temporary before the destructors run,
because C++ evaluates the operand first and `return g.get();` reads the very
object about to be destroyed.

**Refused:** `goto` while a destructor is pending. Where it lands decides
what should have been destroyed, and a lowering that scans forward cannot
know that. With nothing live, `goto` is left alone, so plain C is
unaffected.

### Constructor overloading

Constructors are told apart by argument **count**. A call site is matched
before types are known, so arity is all there is to resolve on.

| Declared | Symbol | Allocator |
|---|---|---|
| `T()` | `T_new` | `T__alloc` |
| `T(int a)` | `T_new_1` | `T__alloc_1` |
| `T(int a, int b)` | `T_new_2` | `T__alloc_2` |

A class with only one constructor keeps the plain `T_new` whatever its
arity. The no-argument one always keeps it, because that is what member and
base default construction call.

**Refused:** two constructors of the same arity — there is nothing left to
choose between them.

### Members and bases

A class-typed member is constructed and destroyed with its container, in
declaration order and reverse declaration order. If a member needs either
and the container declares neither, the container gets an implicit one, as
in C++. A constructor initializer list supplies arguments:

```cpp
class Holder {
    Counter a;
    int k;
public:
    Holder(int n) : a(n), k(n) { }
};
```

Constructors run base first, then install the vtable pointer, then members,
then the body. Destructors run the body, then members in reverse, then the
base.

**Refused:** a member whose class has no default constructor and is missing
from the initializer list — that is an error rather than a silently
unconstructed object.

## Copying

This is where a naive lowering does real damage, so the rules are strict.

A struct copy duplicates the representation. If the class owns something,
both copies own it, and both destructors run on it.

```cpp
class Buf {
public:
    int *p;
    Buf() { p = (int *)malloc(16); }
    Buf(const Buf &o) { p = (int *)malloc(16); p[0] = o.p[0]; }
    Buf &operator=(const Buf &o) { p[0] = o.p[0]; }
    ~Buf() { free(p); }
};
```

- A **copy constructor** `T(const T &o)` lowers to `T_copy` — the one
  constructor that does not lower to `T_new`, since overloading `T_new`
  would redefine it. `T b = a;` and `T b(a);` call it, and the copy is
  registered for destruction like any other local.
- **`operator=`** lowers to `T__assign`, and `b = a;` calls it. This is the
  one operator overload the subset supports besides `operator[]`, because
  assignment to an owning object has no safe default.

**Refused:**

| Situation | Why |
|---|---|
| copying a class with a destructor and no copy constructor | two owners, one resource — the Rule of Three, named in the message |
| assigning to an owning object with no `operator=` | same, at assignment |
| `a = b = c` | `operator=` is lowered to a `void` call, so there is no result |
| copying from an expression this pass cannot name | guessing is the bug |

A class with **no destructor** owns nothing, and copies bitwise exactly as
C++ would.

### By value across a call

An owning class never crosses a call boundary by value:

```cpp
void take(Buf b);                       // refused
Buf make(void);                         // refused
```

A by-value parameter is a copy no constructor ran for and no destructor will
run for. A by-value **return** is worse: the local is destroyed on the way
out, so the caller receives a copy of a released object — a use-after-free.
Doing either properly means copy-constructing into a temporary at the call
site, which needs a statement, and this is expression position. Pass `T &`
or return `T *`. A class with no destructor passes by value freely.

## Methods and calls

```cpp
g.get()      ->   Counter_get(&g)
p->get()     ->   Counter_get(p)
a.b.get()    ->   Counter_get(&a.b)
```

Receivers resolve against a scope-tracked symbol table: locals, parameters,
and chains through class-typed fields. Inside a method, a bare `helper(x)`
picks up the implicit `this`. Anything that does not resolve to a class is
left exactly as written, so plain C in the same file is untouched.

Member **access** follows the same table, so `c.v` on a reference-lowered
parameter becomes `c->v`, and each step of a chain picks its own operator
(`o.in.n` on a reference is `o->in.n`). Inherited fields are reached through
the `_base` member they actually live in: `id` in a derived method is
`this->_base.id`.

### Chaining

A call can be the receiver of the next one:

```cpp
o.node()->get()   ->   Node_get(Owner_node(&o))
```

Each step is emitted into an expression that becomes the next step's
receiver, so no temporary is needed. A chain only ever starts from a symbol
that resolves to a class, so legitimate C spelled the same way —
`get_ops()->init(x)`, a free function returning a struct pointer — is still
left exactly as written.

**Refused:** chaining onto a method that returns a class **by value**. C
cannot take the address of a function result, and spilling one needs a
statement. (A subscript is fine: `v[i].size()` works, because a dereference
*is* addressable.)

### Method overloading

Methods overload by argument count, exactly as constructors do: one `f`
becomes `C_f`, several become `C_f_0`, `C_f_1`, `C_f_2`.

**Refused:** two overloads of the same arity, and overloading a **virtual**
method — a virtual occupies one vtable slot, so its overloads would have to
share it.

### References

`T &x` is a pointer the source did not have to spell, so it is lowered back
to `T *x` and call sites take the address. `T &r = e;` becomes
`T *r = &(e);`.

**Refused:** a reference *return* (`T& f()`). Lowering it to `T*` would
silently change what assignment through the result means at every call site.
`operator[]` is the exception — see below.

## Inheritance and virtual dispatch

Single inheritance, with `virtual` methods and pure virtual (`= 0`)
declarations.

A base is laid out as the **first member**, so a pointer to a derived object
already *is* a pointer to its base and upcasting is a cast. The vtable
pointer sits first in the root of the hierarchy, hence at offset zero
throughout it, and a derived class's table begins with its base's slots —
which is what lets a `Base *` dispatch into a derived override. Overrides
reached through a table go via a small thunk that converts `this`, so the
generated table holds no function-pointer casts.

```cpp
class Shape {
public:
    int id;
    Shape(int i) { id = i; }
    virtual ~Shape() { }
    virtual int area() { return 0; }
};
class Square : public Shape {
public:
    int side;
    Square(int i, int s) : Shape(i) { side = s; }
    ~Square() { }
    int area() { return side * side; }
};

Shape *s = new Square(1, 3);            // upcast inserted
printf("%d\n", s->area());              // dispatches to Square
delete s;                               // runs ~Square, then ~Shape
```

A **virtual destructor** occupies a vtable slot like any other virtual, so
`delete base_ptr` reaches the most derived destructor, which then chains to
its base through the ordinary epilogue. A derived class always overrides
that slot — explicitly, or through the destructor it is given implicitly to
chain to the base — so `virtual` need not be repeated.

`new Derived()` assigned to a `Base *` is upcast with an address-preserving
cast, which is also why `free` on the base pointer releases the whole
allocation.

Dispatching a virtual call on a **call result** goes through a generated
`Decl__vcall_name` helper that takes the receiver as a parameter. The plain
dispatch form names the receiver twice — once to reach the vptr, once as the
argument — which is harmless for a name and wrong for a call, where
`f.make()->area()` would build two objects.

**Refused:** multiple inheritance and virtual inheritance. The layout admits
exactly one base: with one base first, upcasting is free, and that is the
property the rest of this lowering leans on. Also: declaring a value of a
class with a pure virtual method.

## `new` and `delete`

```cpp
Node *p = new Node(5);
delete p;
```
```c
Node *p = Node__alloc_1(5);
do { if (p) { Node_drop(p); free(p); } } while (0);
```

`new T(..)` sits in expression position and C has no statement expression,
so it lowers to a generated allocator — malloc, construct, return — emitted
only for the classes and arities the source actually uses. A failed malloc
yields null rather than being constructed through, since the subset has no
exceptions.

`delete` is a statement, so it lowers in place: guarded because `delete` on
null is a no-op in C++, and wrapped in `do { } while (0)` so a delete as a
branch's only statement does not leave a stray `;` before an `else`.

**Refused:** `new T[n]` and `delete[]` (they need the element count recorded
beside the allocation), `new` of a non-class or of an abstract class,
`delete` of a by-value object, and `delete` of an operand whose type does not
resolve through the symbol table.

## Templates

```cpp
template<typename K, typename V>
class Pair {
    K key;
    V val;
public:
    Pair(K k, V v) { key = k; val = v; }
    K first() { return key; }
};

Pair<int, double> p(1, 2.0);            // Pair_int_double
Pair<char, int> q(65, 9);               // Pair_char_int
```

Monomorphised on use, one struct per instantiation. Any number of
parameters. A non-type integer parameter works too
(`template<typename T, int N>` with `T buf[N]`), because monomorphisation is
textual substitution and `N` is replaced by the literal the use site
spelled.

Substitution is **simultaneous**: `template<A, B>` instantiated as
`<B, char>` must not rewrite `A` to `B` and then that `B` to `char`.

Arguments may themselves be instantiations (`Holder<Pair<int,char>>`,
resolved innermost first — `>>` needs no special case), and a template body
may instantiate another (`Outer<T>` holding an `Inner<T>`), which is closed
transitively.

**Refused:** default template arguments, parameter packs, and a nested
instantiation whose class is declared *below* the one that needs it —
classes are emitted in order, so it has to be complete first.

## `operator[]`

Must return a reference, and lowers to a pointer, so the subscript stays an
lvalue:

```cpp
int &operator[](int i) { return d[i]; }
```
```c
static int *Arr__index(Arr *this, int i) { return &(this->d[i]); }
v[2] = 42;   ->   (*Arr__index(&v, 2)) = 42;
```

**Refused:** a by-value `operator[]`, which would make `v[i] = x` write to a
copy.

A subscript on a genuine pointer **field** is left as plain C indexing:
`T *p; p[i]` walks an array rather than calling anything.

## Lambdas

Two shapes, because they can do genuinely different things.

**Non-capturing** lambdas are exactly functions, and lower to one. C already
has function pointers, so an `auto` binding becomes one and the call site
needs no rewriting at all — which means they can be passed as callbacks:

```cpp
auto twice = [](int y) -> int { return y * 2; };
printf("%d\n", apply(twice, 5));
```
```c
static int _cpp_lambda0(int y) { return y * 2; }
int (*twice)(int) = _cpp_lambda0;
```

**Capturing** lambdas are inlined at each call site instead. A capture would
otherwise need the captured variable's type, to become a field of a closure
struct, and that type is an ordinary local this pass cannot see — but a body
placed where the call is has those variables in scope already, so nothing
has to be named.

```cpp
int total = 0;
auto add = [&](int v) -> int { total = total + v; return total; };
int a = add(1);
```
```c
int total = 0;
int _cpp_lam0_r;
do { int v = 1; total = total + v; { _cpp_lam0_r = total; break; } } while (0);
int a = _cpp_lam0_r;
```

`return` inside the body must leave the lambda, not the enclosing function,
so the body goes inside `do { } while (0)` and `return` becomes `break`.
That is a structured jump the destructor unwinding already understands — it
walks out to the enclosing loop frame dropping what is live — where a label
and `goto` are refused outright whenever anything is live, which is most
RAII code.

A **by-value** capture is a copy taken where the lambda is written, so it
becomes a snapshot local declared there; its type is looked up from the
declaration:

```cpp
int x = 10;
auto f = [x](int k) -> int { return x + k; };
x = 99;
f(1);                                   // 11, not 100
```

A return type must be spelled in both shapes. Nothing here can deduce one,
and defaulting to `int` would truncate a `double`.

**Refused:** because it is inlined, a capturing lambda has no value to pass
around, cannot recurse, and cannot be called from a loop condition or a
`&&` / `||` / `?:` operand, where the body would not run exactly once. A
`return` nested inside a loop in the body is refused too, since `break`
would leave only that loop. `[=]` names nothing to look up, and a by-value
capture whose declaration is missing or ambiguous is refused rather than
guessed at — a wrong type there would silently truncate.

## A small `std`

`string` and `vector<T>` are supplied when the source names them, and are
**written in this subset** rather than special-cased in the lowering. That
is the point of them: every feature they need — templates, a copy
constructor, `operator=`, `operator[]`, a destructor, methods calling
methods — is one the subset already claims to have, so if the containers
compile, the claim holds. They go through the same passes as user code, and
a bug in them is a bug in the lowering. (Two were found that way: missing
member prototypes, and `operator=` colliding with a method named `assign`.)

```cpp
#include <string>
#include <vector>

std::string s("hello");
s.append(", world");
s[0] = 'H';
printf("%s %d\n", s.c_str(), s.size());

std::vector<int> v;
v.push_back(1);
v[0] = 42;
```

`std::` is stripped. There is no namespace support and claiming otherwise
would be worse than not claiming it.

| Type | For | Elements |
|---|---|---|
| `string` | text | `size` `empty` `at` `[]` `c_str` `assign` `append` `push_back` `clear` `reserve` `equals` |
| `vector<T>` | scalars, pointers, plain data | `size` `empty` `get` `set` `ptr` `[]` `push_back` `pop_back` `clear` `reserve` |
| `ownvector<T>` | classes that own something | same, minus `get`/`set` |

`vector<T>` stores elements by assignment, so an element type with a
destructor would leave two owners. `ownvector<T>` copy-constructs each
element and destroys it, and is a separate template rather than a smarter
`vector` because the two need different **parameter conventions**: a scalar
element wants `push_back(T v)` — you write `v.push_back(3)`, and `3` has no
address — while an owning element must not cross a call boundary by value at
all and wants `push_back(const T &v)`. One template body cannot spell both.

```cpp
std::ownvector<std::string> v;
std::string a("alpha");
v.push_back(a);                         // deep copy
a.assign("changed");                    // v[0] is still "alpha"
```                                     // every element destroyed here

`vector<T *>` with `new`/`delete` is the other shape that works, when you
want the elements to outlive the container.

Supplied container methods are emitted `static inline`, so the ones a
program does not call are not warned about. User classes stay plain
`static`, because you should still hear about your own dead code.

### `__cpp_copy` and `__cpp_drop`

`ownvector` needs to say "copy an element" and have that mean the copy
constructor for a class. It cannot spell `T_copy`: substitution rewrites
whole words, and `T_copy` is one word. So there are two builtins, resolved
per instantiation:

```cpp
__cpp_copy(T, dst, srcptr)              // T_copy(&dst, srcptr)
__cpp_drop(T, x)                        // T_drop(&x), or nothing
```

They are an internal seam, but nothing stops you using them to write your
own owning container.

## Not supported yet

Reported rather than mistranslated: exceptions (`throw` / `try` / `catch`),
operator overloading other than `=` and `[]`, `dynamic_cast`, `typeid`,
multiple and virtual inheritance, namespaces, iterators, and the rest of the
STL.

## Errors

Every refusal above raises a `CppError` naming the reason and the fix. When
driven through an `#include`, the message is reported against the include
line; on the command line it goes to the output file and stderr with a
non-zero status.

```
guard.cpp:12: `new Node[..]` is not in the C++ subset: array `new` has to
store the element count beside the allocation for `delete[]` to destroy each
element. Allocate one object at a time.
```

## How it is driven

`shivyc/preproc.py` runs `tools/cpprust.py` in a **subprocess** rather than
importing it. The reason is self-hosting: py2c transpiles the compiler's own
sources, and an `import tools.cpprust` inside preproc becomes a real
cross-module reference that is then undefined at link time, because this
module is not in the transpiled set — it leans on compiled-pattern objects
and match methods that py2c does not lower, whereas `shivyc/crust.py` stays
inside the supported subset on purpose.

A subprocess removes the symbol entirely, so the self-hosted compiler links
with no reference to this file. The protocol is one file and one exit
status, with the diagnostic written to the output file on failure, so the
self-hosted caller can drive it through `os.system` where capturing a pipe
is awkward. A `.cpp` include therefore needs python3 and this script on disk
at compile time; a `.c` or `.rs` build needs neither.

## Tests

```sh
python3 -m unittest tests.test_cpprust
```
