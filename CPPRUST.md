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
`Tally_drop` frees it again on the way out.

This is **not** the same shape as this pass's own by-value owning parameters,
which are constructed at the call and dropped by the callee (see "By value
across a call"). A Rust callee taking by value *moves*: it takes the object
and the source must be left out of this side's drops. That is Crust's
move-out rather than a materialised temporary, and conflating the two would
reintroduce exactly the double free this refuses. So it stays refused.

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

A field declaration may carry several declarators — `int x, y;` — and each
star binds to its own name, so `int *p, q;` makes `p` a pointer and `q` an
`int`, exactly as C says. A comma inside `<>` belongs to a template argument
list, so `map<int, int> m;` is one field.

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

A by-value **parameter** of an owning class is an object the *callee* owns.
C++ constructs it at the call and destroys it when the function returns, and
both halves are written out:

```cpp
int sink(Buf b) { return b.val(); }
Buf a;
sink(std::move(a));                     /* moved in  */
sink(a);                                /* copied in */
```
```c
int sink(Buf b) { { int _cpp_ret0 = (Buf_val(&b)); Buf_drop(&b);
                    return _cpp_ret0; } Buf_drop(&b); }
sink(({ Buf _cpp_mv0; Buf_move(&_cpp_mv0, &a); _cpp_mv0; }));
sink(({ Buf _cpp_ba0; Buf_copy(&_cpp_ba0, &(a)); _cpp_ba0; }));
```

The parameter is registered like a local, so it drops on every exit from the
function, and the argument is *constructed* rather than handed over as a
struct copy. **The two halves have to travel together.** Writing only the
callee's drop turns every call into a double free — both sides then own one
buffer and both free it, which is what a sanitizer caught the moment the
refusal was lifted without the call sites rewritten.

This used to be refused, and the refusal was right at the time: neither half
existed, so the copy was never constructed and never destroyed.

**Refused:** an argument whose class has a destructor and no copy
constructor. There is nothing to construct the parameter with — reported at
the *call*, where the choice is, rather than at the declaration, since
`std::move` is the answer and only the call site can write it.

A by-value **return** is a different matter and is unchanged: the local is
destroyed on the way out, so the caller would receive a copy of a released
object. A `return` of a bare local is a move out and is fine; anything else
is refused. Return `T *`, or assign to a local first. A class with no
destructor passes and returns by value freely.

### `std::move`

A **move constructor** `T(T &&o)` lowers to `T_move`, beside `T_copy` and for
the same reason: `T_new` is taken and overloading it is not available. An
`operator=(T &&)` lowers to `T__moveassign`, beside `T__assign`. `T &&o` is a
reference like any other and lowers to `T *o`, so the body reads through `->`
and can null the source.

```cpp
class Buf {
public:
    int *d;
    Buf() { d = (int *)malloc(16); }
    Buf(const Buf &o) { d = (int *)malloc(16); d[0] = o.d[0]; }
    Buf(Buf &&o) { d = o.d; o.d = 0; }      /* the move */
    ~Buf() { free(d); }
};

Buf a;
Buf b = std::move(a);                       /* Buf b; Buf_move(&b, &a); */
```

Three things decide what this means, and each is C++'s rule rather than a
convenience.

**The moved-from object is still destroyed.** This is the one worth stating
outright, because Crust's own move goes the other way. A C++ moved-from object
is valid-but-unspecified, not dead: `a` stays live to the end of its scope and
`Buf_drop(&a)` runs there, exactly as it would have without the move. What
makes that harmless is the move constructor nulling the source — that is what
the author is *for*, and why `~Buf` is written to survive `d == 0`. The scope
rewriting already has a move-out that suppresses a drop (`unwind(moved=)`,
used for `return v;`), and it is deliberately not used here. It is right there
because it is right for `return v;`, where the object is handed over bitwise
with no constructor involved and no second owner. Reaching for it here would
give `std::move` Rust semantics under a C++ spelling, and on a well-written
class the two are indistinguishable — `free(0)` is a no-op, so the leak only
appears on a class whose destructor still has work to do. Both drops are
emitted, in reverse declaration order:

```c
HeavyBuffer source; HeavyBuffer_new(&source, 8, 7);
HeavyBuffer target; HeavyBuffer_move(&target, &source);
HeavyBuffer_drop(&target); HeavyBuffer_drop(&source);
```

**No move constructor means the copy runs.** `std::move` is a cast, not a
call: it produces an rvalue, and `T(const T &)` binds one perfectly well. So a
class that has not been given a move constructor is copied, which is what C++
overload resolution does — and is what makes adding `std::move` to an existing
source safe rather than a rewrite. A class with *neither* is still refused by
the Rule of Three, at the same place and with the same message.

**Only the qualified spelling.** `std::` is stripped rather than resolved, so
`std::move` is read before that happens and rewritten to the internal
`__cpp_move`. After stripping it would be indistinguishable from a project's
own `move` — litehtml moves boxes — and every one of those calls would be
rewritten. The cost is that `using namespace std;` plus a bare `move` is not
recognised, which is a shape worth not guessing at.

**Expression position** — a `return`, an argument, an operand — is lowered
through a GNU **statement expression**, which is the one construct that can
declare a temporary where C has no statement to declare one in:

```cpp
Buf mk(void) { Buf a; return std::move(a); }
```
```c
Buf mk(void) { Buf a; Buf_new(&a);
    { Buf _cpp_ret0 = (({ Buf _cpp_mv0; Buf_move(&_cpp_mv0, &a); _cpp_mv0; }));
      Buf_drop(&a); return _cpp_ret0; } }
```

Declare, move into, yield — what a C++ compiler does with a materialised
temporary, written out. This is a GNU extension rather than ISO C, and it is
used anyway because gcc, clang **and ShivyCX** all implement it; all three
were checked against this exact shape, and all three agree. So there is still
one output and no backend to choose between, which is the property the rest of
the pipeline leans on.

Two orderings make this correct, and both were already there. `return`
evaluates its operand into a temporary *before* the destructors run, because
C++ evaluates the operand first — which is exactly what a move needs: the
source is still alive when it is moved from, and its own drop then finds the
husk. And the temporary is deliberately **not** registered for destruction:
it is yielded by value, so the caller receives a bitwise copy holding the
resource, and destroying the husk left behind would be destroying what the
caller now owns.

**Still refused:** an operand that is not an object this pass can name, and a
class with a destructor but neither a move nor a copy constructor — there is
nothing to construct the temporary with.

### Moving into a container

A container argument is the one expression position that is **not**
materialised, because it must not be. A move overload lowers to
`push_back(T *v)`, so what the call wants is the address of the source; a
statement expression yields an rvalue, and its address cannot be taken.

```cpp
std::vector<std::unique_ptr<Thing>> w;
std::unique_ptr<Thing> p(new Thing());
w.push_back(std::move(p));          /* vector_..._push_back__move(&w, &p) */
```

Three things meet here, and each is the same move already made elsewhere:

* **A move overload.** `push_back(__cpp_rref(T))` sits beside
  `push_back(__cpp_ref(T))`, and the two are told apart by whether the call
  site wrote `std::move` — not by arity, which cannot tell them apart at all.
  That is exactly how `operator=` and `operator=(T &&)` are already chosen.
  `__cpp_rref(T)` is `T &&` for a class and plain `T` for a scalar; a scalar
  has nothing to move, so the two would be one signature and the move
  overload is simply not emitted.
* **`__cpp_movein(T, dst, src)`**, which is to `__cpp_copy` what a move
  constructor is to a copy one: `T_move`, falling back to `T_copy` when the
  element has no move constructor, and a plain assignment for a scalar.
* **Deleted copy members.** A container member whose body copies an element
  the element type cannot copy is *deleted*, exactly as C++ deletes it —
  rather than the whole instantiation being refused over members the program
  never calls. A **call** to one is then an error naming the reason, because
  dropping it silently would turn a diagnostic into an undefined symbol from
  the C front end.

`std::forward` is absent. It means something only inside a template taking
`T &&`, which this subset does not have.

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

Chaining onto a method that returns a class **by value** works too, through
a generated `Cls__byval_meth_<n>` taking its receiver by value:

```cpp
o.make().get()    ->   inner__byval_get_0(outer_make(&o))
```

C cannot take the address of a function result and spilling one needs a
statement, so the value goes in as a value — the same way out the binary
operators take for `a + b + c`, resting on the same condition: the class
must own nothing, since a struct copy of an owning receiver would leave two
objects holding one resource. The variants are emitted only for the names a
source actually chains onto.

**Refused:** the same chain when the returned class *does* own something, and
when the chained method is **virtual** — dispatch needs a receiver whose
address can be taken to reach the vtable. Each says which of the two it is.
Assign to a local first, or return `Cls *`.

(A subscript is fine either way: `v[i].size()` works, because a dereference
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

### Function templates

```cpp
template<class T> void js_register_class(const char* className) { .. }

js_register_class<litehtml::document>("Document");
```

Monomorphised the same way and for the same reason, by substitution **in
place**: what comes out is ordinary subset source, and every pass below
lowers it without knowing a template was involved.

Substituting in place is what makes a *member* template work at no extra
cost. `js_register_class` is a member of `context` whose body names fields
and calls other members; replacing it where it stands with one ordinary
member per instantiation hands the whole problem to the class emitter,
which already knows how to give a method its `this` and mangle its name.

A template argument is mangled the way namespace flattening will spell it --
`litehtml::document` gives `litehtml_document` -- so the two agree without
either knowing about the other. `typename X::y` loses the keyword once `X`
is known, since there is no longer a parser to tell.

An **uninstantiated** template emits nothing, which is what C++ does with
one. That is not an optimisation but the whole answer for a header that
merely declares one: a template's body is not ordinary code, and lowering it
produced diagnostics about statements in a function the translation unit
never called.

**Refused:** a call giving the wrong number of arguments. They are
substituted by position and there are no defaults to fall back on.

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

## Binary arithmetic operators

`+ - * / % | & ^` lower to `T__binadd` and so on, in the one case that has
an honest lowering: **a class that owns nothing**.

```cpp
class vec2 {
public:
    int x, y;
    vec2() { x = 0; y = 0; }
    vec2(int a, int b) { x = a; y = b; }
    vec2 operator+(const vec2 &o) { vec2 r(x + o.x, y + o.y); return r; }
};

vec2 s = a + b + c;      ->   vec2__binadd_v(vec2__binadd(&a, &b), &c)
```

The operator hands back a new object **by value**, and a by-value return of
an owning class is not in this subset — the local is destroyed on the way out
and the caller would receive a copy of a released object. So a class with a
destructor is refused, and pointed at `operator+=`, which writes into an
object that already exists.

A *run* of them chains, through a variant taking its left operand by value:
C cannot take the address of a function result, so the result of the first
call is passed straight into the second as a value. That is sound only
because the class owns nothing, so the by-value parameter is a struct copy
with nothing to construct or destroy.

**Refused, rather than mistranslated:**

* **Mixed precedence.** `a + b * c` would chain left to right into
  `(a + b) * c` — the wrong grouping, and silently wrong arithmetic. Assign
  the tighter-binding part to a temporary. Parentheses do not help.
* **An operand that is not a plain name.** Operands are passed by address,
  and there is none to take of a parenthesised expression or a call result.

`operator*` is told apart from the dereference by whether it takes an
operand, which is the only difference between them on the page.

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

`std::` is stripped rather than resolved. Namespaces themselves are
supported by flattening -- see the C++11 section below -- but `std` is not
one this file declares, so its qualifier is simply removed.

| Type | For | Elements |
|---|---|---|
| `string` | text | `size` `empty` `at` `[]` `c_str` `assign` `append` `push_back` `clear` `reserve` `equals` `compare` `substr` `find` `rfind` `erase` |
| | substrings | `find_str` `find_str_from` `rfind_str` `contains` `starts_with` `ends_with` |
| `vector<T>` | scalars, pointers, plain data | `size` `empty` `get` `set` `ptr` `[]` `push_back` `pop_back` `clear` `reserve` `insert` `erase` `begin` `end` |
| `ownvector<T>` | classes that own something | same, minus `get`/`set` |
| `pair<K,V>` | two values | `first` `second` |
| `map<K,V>` | keyed lookup, **sorted** | `size` `empty` `clear` `[]` `find` `count` `erase` `lower_bound` `at_index` `begin` `end` |
| `set<T>` | membership, **sorted** | `size` `empty` `clear` `insert` `erase` `find` `count` `lower_bound` `begin` `end` |
| `unordered_map` / `unordered_set` | — | aliases of the above; nothing here hashes |
| `priority_queue<T>` | max-heap | `size` `empty` `clear` `push` `pop` `top` `[]` |
| `stack<T>` | LIFO | `size` `empty` `clear` `push` `pop` `top` `[]` |
| `queue<T>` | FIFO | `size` `empty` `clear` `push` `pop` `front` `back` `[]` `begin` `end` |
| `array<T,N>` | fixed size, plain data only | `size` `empty` `[]` `data` `fill` `begin` `end` |
| `optional<T>` | a value or nothing | `has_value` `value` `set` `reset` |

### Ordered containers

`map` and `set` keep their elements **sorted** and binary-search them. A key
that is a class therefore supplies `compare`, returning negative, zero or
positive:

```cpp
class K {
public:
    int v;
    int compare(const K &o) { if (v < o.v) { return -1; }
                              if (o.v < v) { return 1; } return 0; }
};
```

Three-way rather than a `less` predicate because the builtin's operands are
not symmetric — the right one arrives as an already-lowered pointer and the
left as an lvalue, so `b < a` cannot be had by swapping the arguments of
`a < b`. One comparison answers both ordering and equality, so there is no
second requirement to keep consistent with the first.

`unordered_map` and `unordered_set` are rewritten to `map` and `set`.
Nothing here hashes and nothing in this subset can write `hash<T>`
generically, so a separate copy would have the unordered *interface* and the
ordered *behaviour*. The alias says so. Iteration comes out sorted, which
code relying on no order is not broken by; lookups are O(log n), not O(1).

### `<algorithm>` and `<numeric>`

Free function templates over a `T *` range — which is what every container
here hands out, so they work over any of them without an iterator
abstraction existing.

```cpp
#include <algorithm>
#include <vector>

std::vector<int> v;
v.push_back(3); v.push_back(1);
std::sort(v.begin(), v.end());              // T deduced from the range
int at = std::lower_bound(v.begin(), v.end(), 3) - v.begin();
```

| Header | Functions |
|---|---|
| `<algorithm>` | `sort` `lower_bound` `upper_bound` `binary_search` `find` `count` `reverse` `fill` `min_element` `max_element` `swap` `copy` |
| `<numeric>` | `accumulate` `iota` `inner_product` `partial_sum` `adjacent_difference` |

Points where these diverge from `std`, each for a reason the subset forces:

* **`swap` takes pointers** — `swap(&a, &b)`. A `T &` parameter is lowered
  only for a class, so `swap(int &, int &)` would keep its `&`; and
  `__cpp_ref(T)` gives a scalar by value, which is what a swap cannot have.
  Pointers are the one spelling that works for both.
* **`sort` relocates**, with `memmove`, rather than assigning. An owning
  element keeps its one owner and needs no `operator=`. It is an insertion
  sort: a recursive one would need the template to call itself over its own
  parameter, which the instantiation scan cannot see through.
* **`find` and `count` ask `__cpp_eq`**, not `__cpp_cmp`. Matching does not
  need an order, and demanding one would refuse a class that reasonably has
  equality and no ordering.
* **`fill` and `copy` need a constructed destination** when the element owns
  something. Both destroy each destination before constructing over it —
  right for a container's range, and a segfault for raw storage. The
  destination has to be visibly a container's own range (`begin()`, `ptr()`,
  or one local aliasing one); anything else is reported. A plain-data
  element has nothing to destroy, so the check never fires.
* **`<numeric>` is scalars only.** These combine elements with `+` and `*`,
  which a class would need `operator+` for; a class element is reported
  against the call rather than left to fail inside a template body. `accumulate`
  answers to its own name as well as to the header, since it lived in
  `<algorithm>` here before `<numeric>` existed.

### Template arguments are deduced, narrowly

`sort(v.begin(), v.end())` works without spelling `<int>`. Deduction reads
one shape: a parameter written `T *`, matched against an argument whose
pointee this file declares — a container's `begin()`, an array, or a pointer
local. A deduced call is rewritten to spell its arguments the long way and
the ordinary substitution runs on that, so both forms take one code path.

`map` is deliberately out of range: its iterator is a `pair<K,V> *`, so
deducing `K` from `m.begin()` would be *wrong* rather than unsupported.
Anything else — a call result, a by-value parameter, more than one template
parameter — is reported, and you write `f<T>(..)`.

Deduction reads only what is in scope at the call: a brace region that
opened and closed above it is skipped, so another function's locals and an
earlier `if` block's locals cannot answer for a name they merely share.
Within what remains, the nearest declaration wins, so a local still shadows
a global.

It also never reads above your first line. The supplied templates have
ordinary local names in them, and without that bound a `T *a` parameter
inside `swap` answered for a call whose `a` was your own `int a[4]`.

It is not a symbol table — this pass runs before one exists. What it cannot
type confidently it declines to type, and the call is reported rather than
guessed.

`push_back` on `vector<T>` has a **move overload** for a class element, taken
when the call site writes `std::move` -- which is what lets a `vector` hold a
move-only element such as `unique_ptr<T>`. See "Moving into a container".

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

### Regular expressions

`std::regex` is not supplied and will not be: it wants exceptions, locales and
iterators, none of which the subset has. `runtime/crust_re.hpp` gives you
`cre::regex` over the same C engine the Rust and C sides use, and it is written
in this subset for the same reason `string` and `vector` are.

```cpp
#include "crust_re.hpp"

cre::regex re("(\\w+)=(\\d+)");
if (!re.ok()) { /* re.error() names the problem -- nothing is thrown */ }

cre::smatch m;
if (cre::regex_search(re, "port=8080", m)) {
    char buf[64];
    m.str(1, buf, sizeof buf);          // "port"
}
```

One wrinkle worth knowing, because it is a property of the subset rather than
of the header: the capture-less forms are named `regex_matches` and
`regex_contains`, not overloads of `regex_match`/`regex_search`. **Method**
overloading is supported; **free-function** overloading is not, because free
functions lower to plain C names and two `regex_match`es collide in the
generated C. A host C++ compiler accepts the overloaded version happily, which
is why `runtime/run_cpp_test.sh` builds the same source both with `g++` and
through `cpprust.py` and diffs the output.

See [REGEX.md](REGEX.md) for the supported pattern subset.

Supplied container methods are emitted `static inline`, so the ones a
program does not call are not warned about. User classes stay plain
`static`, because you should still hear about your own dead code.

### The element builtins

`ownvector` needs to say "copy an element" and have that mean the copy
constructor for a class. It cannot spell `T_copy`: substitution rewrites
whole words, and `T_copy` is one word. So there are two builtins, resolved
per instantiation:

```cpp
__cpp_copy(T, dst, srcptr)              // T_copy(&dst, srcptr)
__cpp_drop(T, x)                        // T_drop(&x), or nothing
__cpp_movein(T, dst, srcptr)            // T_move(&dst, srcptr)
__cpp_eq(T, a, b)                       // T_equals(&a, b), or `a == b`
__cpp_cmp(T, a, b)                      // T_compare(&a, b), or two `<`s
__cpp_addr(T, x)                        // &(x) for a class, (x) for a scalar
__cpp_ref(T) / __cpp_rref(T)            // `const T &` / `T &&`, or plain `T`
```

`__cpp_cmp` is three-way, like `strcmp`. The scalar form is written with two
comparisons rather than a subtraction, because `a - b` overflows for wide or
unsigned types and gets the order backwards when it does. Both operands are
expanded twice there, so a container must pass side-effect-free expressions.

`__cpp_addr` exists because `__cpp_cmp` and `__cpp_eq` want their *right*
operand as a pointer for a class and a value for a scalar. A container gets
that spelling free from a parameter declared `__cpp_ref(T)`; code that builds
its own value — `sort` holding an element aside while it shifts the tail —
has no such parameter, and no one expression is an address in one
instantiation and a value in the other.

They are an internal seam, but nothing stops you using them to write your
own owning container. One caveat: they are expanded while lowering *classes*,
so a file that defines none — using only `<algorithm>`, say — is reported
rather than emitting `__cpp_cmp(int, ..)` into the C.

## Header and source, split

A class may declare its members and define them afterwards under a qualified
name -- which is how C++ projects are actually laid out:

```cpp
/* shape.h */
class Shape {
    int w, h;
public:
    Shape(int a, int b);
    ~Shape();
    int area() const;
};

/* shape.cpp */
#include "shape.h"
Shape::Shape(int a, int b) { w = a; h = b; }
Shape::~Shape() { .. }
int Shape::area() const { return w * h; }
```

Two things make that work.

**Quoted `#include`s are spliced by this pass**, resolved against the
including file's own directory first and then a search path (`--incdir`,
which `shivyc` fills from its `-I` options), so the class and its bodies
arrive in one translation -- the lowering emits a class and its bodies
together, and only the `#include` brings the two halves together. Each header
is spliced once, which is what an include guard does and saves having to
understand `#pragma once` or the `#ifndef` idiom. A header that cannot be
found is left alone: it may be one the C front end resolves, and this pass is
not the authority on the include path.

Both spellings are spliced, but they are looked for in different places,
which is what keeps the distinction meaningful. A quoted include is searched
from the including file's own directory first and then the `--incdir` path;
an angle one is searched *only* on that path, and is spliced only if found
there. A header that resolves under a directory the caller named is this
project's own, whichever brackets it was written with -- litehtml includes
its own headers both ways, and leaving the angle form alone meant the
classes it declares were never lowered, so a file could translate clean and
then emit C naming a struct nobody defined. Anything not found under an
`--incdir` is left exactly as written, so `<string.h>` still goes to the C
front end and `<string>` still reaches the supplied containers; with no
`--incdir` at all, no angle include is ever spliced.

`<cstdint>` and its family are the C headers under their C++ spellings, and
are rewritten to the C ones -- the same move as pulling in `<stdbool.h>`
when a file writes `bool`. The table is written out rather than computed, so
`<cstring>` cannot be confused with `<string>`, which is a different thing
entirely.

### Conditionals

Simple `#if`s are evaluated **while** splicing, not after -- an `#include` in
a branch that is not taken should never be followed, and a header that
`#define`s a name has to decide the conditionals of the ones below it.

A header that defines a type two ways otherwise contributes both. litehtml's
`os_types.h` gives `tstring` as `std::wstring` or `std::string` under
`#ifndef LITEHTML_UTF8`, and with neither branch resolved the templates over
it were monomorphised twice -- a `vector_wstring` beside the real one, over a
type the subset does not supply.

**Decided:** `#ifdef`, `#ifndef`, `#if defined(X)`, `#if 0` / `#if 1`, and a
chain of `defined` tests joined by one operator. That last matters in
practice: litehtml wraps nearly all of `os_types.h` in
`#if defined( WIN32 ) || defined( _WIN32 ) || defined( WINCE )`, and
refusing that one shape left everything inside it unevaluated.

**Not decided, and passed through untouched, directives and all:** a
comparison like `_MSC_VER < 1900`, which needs a value this pass does not
have, and a line mixing `||` and `&&`, which needs precedence. Nothing is
reported -- an undecidable `#if` is not an error, it is simply not this
pass's to answer.

The invariant is that this only ever *narrows* what reaches the rest of the
pass, and only where the answer is not in doubt. Names come from `-D` on the
command line and from `#define` in live text.

A class this translation only **declares** -- `class element;`, with the
definition in a header nobody here included -- is lowered like a definition
minus the body: a struct tag and its typedef. C++ allows the declaration
wherever the type is used through a pointer, which is what a
`shared_ptr<element>` does. Which class is *complete* where is untouched: a
by-value member of one still gets no definition, so
`struct Holder { Thing t; };` reaches the C front end, which reports
`field 't' has incomplete type` and names the field.

**Definitions are lifted out and attached** to the member they belong to,
keyed by class, name and arity, before anything is emitted. Only at brace
depth zero: a qualified name *inside* a body is a call, and matching those
would tear the middle out of a function.

The bodies are then emitted **after everything else**, not at the class. The
author wrote them below whatever file-scope names they read, and a header
spliced in at the top would otherwise put them above.

A member declared and never defined is **reported**. An empty body would
compile and do nothing.

`explicit` is dropped: it constrains implicit conversion, which this
lowering does not perform in the first place, since every construction is
written out. A trailing `const` on a member function is dropped: it constrains what the
body may do, `this` is a pointer either way, and the C front end checks the
body regardless.

## C++11 spellings

None of these change what the subset can express. They are *spellings*, each
rewritten into something the lowering already handled, and each rewritten
before any pass that reads types runs -- because everything downstream reads
types by how they are written.

### `auto`

Resolved to a written type, textually. What has a spelling nearby resolves:

```cpp
auto a = A();               // a class construction     -> A
auto p = new A();           // a heap allocation        -> A *
auto n = 3;                 // literals of each kind    -> int
auto q = other;             // a local, from its decl   -> its type
auto r = mk();              // a function declared here -> its return
auto z = v.size();          // a method of a known class-> its return
auto e = v[0];              // through operator[]       -> the element type
```

`auto a = A();` is emitted as `A a(..)` -- direct-initialisation. It is
written as copy-initialisation but means the other thing: C++17 guarantees
the temporary is elided, and the direct form is the one this subset lowers.

A subscript deduces through the template parameter, so `vector<int>` gives
`int`: `operator[]`'s return type is read and the instantiation's arguments
are put back in place of the parameters.

Anything without a spelling to take -- a compound expression, a chained call
whose intermediate type is written nowhere, an unknown name -- is **reported**
with the reason. That is the change worth knowing: `auto` used to pass
through untouched, so what came back was `expected expression, got 'A'` from
the C front end rather than a diagnostic about `auto`.

A named cast is the one compound expression that does resolve. The type is
spelled in the angle brackets at the point of use, so nothing is inferred:
`static_cast<T>(e)`, `reinterpret_cast`, `const_cast`, and the C-style
`(T *)e`. All three named casts lower to the C cast `((T)(e))` -- the
distinctions between them are checks the C++ front end performs and none
survives lowering. `dynamic_cast` is deliberately absent: it is refused for
wanting RTTI, and deducing through it would turn that refusal into a
lowering that dispatches on nothing.

`auto x { e }` -- brace initialisation -- is the same initialisation with the
same operand for everything this subset lowers, so only the terminator
differs. A declaration inside a condition, `if (auto *p = f())`, declares a
name that is in scope for the branch; its initialiser ends at the enclosing
`)` rather than at a `;` that is not there.

#### The clang fallback

Where this pass reports, a C++ compiler already knows -- deduction is its
job. So if `clang++` is installed, its answer is asked for before the report
is raised, from a `-ast-dump=json` of the original file.

Nothing is approximated, which is what keeps this inside the guiding rule.
clang either says what the type is or it does not, and if it does not the
original diagnostic stands unchanged. Four things bound it:

* **Only the file being translated.** A dump of one real source carries
  every declaration in every header it reaches -- several hundred from
  libstdc++ alone, under names like `find`, `min`, `next` and `pi` that a
  program may perfectly well use for something else.
* **Keyed by name, not line.** It answers from the original file while
  deduction runs on text that has had headers spliced in and namespaces
  flattened, so no line number survives the trip. A name clang gave two
  types is dropped rather than guessed between.
* **Only a type this subset can spell.** A nested `iterator` arrives spelled
  bare, and emitting `iterator i = ..` into C declares a variable of a type
  nothing defines -- worse than the diagnostic it replaced, because the error
  moves to the C front end and stops naming `auto`.
* **Lazily, and only on a declaration that already failed.** A file whose
  types are all written never spawns a compiler.

`--clang` requires it, `--no-clang` forbids it, and the default is to use it
when present. A build wanting the same answer on every machine should pin
it: with the fallback available a `.cpp` whose types are not written still
translates, and on a machine without clang the same file does not. On
success the run names what clang answered, on stderr.

### Range-`for`

Rewritten to the index loop it stands for:

```cpp
for (auto &x : v) { .. }
for (int _cpp_it0 = 0; _cpp_it0 < v.size(); _cpp_it0 = _cpp_it0 + 1) { .. }
```

The reference form is done by **substitution** -- the name aliases the
element, so writing through it writes to the container, which is what a
reference means. The by-value form declares a copy instead. The two differ
here exactly as they differ in C++, rather than one quietly behaving like
the other.

The range has to be written plainly enough to read a length from: a name, or
a chain of them -- `m_right.m_attrs` is written just as plainly as `v` is,
and each step has a declared type. Inside a method a bare name resolves as a
field of the class being written, since that is what `this->name` means.
Either way it must end at an array with a written size, or a class with
`size()` and `operator[]`, optionally through a pointer. `begin()`/`end()`
iterators are a different feature and are reported, not guessed at.

The `:` here is the range-`for`'s own and never one half of a `::`. That has
to be said because the type and name groups will otherwise backtrack to make
one fit: `for (tstring::size_type i = 0; ..)` was read as type `t`, name
`string`, and the first colon of the `::` as the range colon -- reporting an
ordinary indexed loop as an unwalkable range.

### `= default` and `= delete`

`~T() = default;` asks for the destructor the compiler would have written,
which here is the member epilogue -- and that is appended to whatever body a
destructor has, so it is rewritten to an empty body. Rewritten rather than
dropped, because that keeps `virtual` attached, and `virtual` decides whether
the class gets a vtable slot.

`= delete` asks for the member not to exist, and a member this pass never
sees does not. Dropping it lands on the right behaviour for the case that
matters: a deleted copy constructor leaves a class with a destructor and no
copy constructor, which the Rule of Three check already refuses to copy.

### Type aliases

`typedef X Y;` and `using Y = X;` are resolved to what they name, and a
`using` alias becomes a typedef, since C has only that spelling.

Substituted throughout rather than threaded through each consumer: a field
declared `elements_vector items;` records its type by spelling, and so does a
local, and so does a parameter. Doing it once means every pass below sees the
class the alias stood for without knowing aliases exist.

Three things are deliberately left alone:

- **A class-scoped typedef.** litehtml names its `ptr` and `vector`, and
  taking those flatly would make every `vector` in the file mean
  `box::vector` -- including the supplied template of that name.
- **A typedef that names itself.** `typedef struct X X;` is the ordinary C
  idiom and the C that Crust emits for its own types is full of them;
  substituting it prepends `struct` once per round.
- **A name a template also uses as a parameter.** Inside the template the
  parameter is what the name means.

### Namespaces

`namespace N { .. }` and `N::x` are flattened to `N_x` -- the same thing
Crust does with Rust paths, and for the same reason: C has one namespace, so
a qualified name has to become an unqualified one. Nesting gives `a_b_x`, and
`using namespace N;` makes the unqualified spellings visible.

Only what the namespace *declares* is prefixed. A class's members and a
function's locals are not namespace names, and prefixing them renamed
`Point::x` to `geo_x` and broke every use of it.

`N::x` becomes `N_x` only for the names flattening actually renamed. The
qualification says which namespace to look in, not what the name became, and
this pass does not rename everything a namespace holds -- a typedef keeps its
name so the generated C stays readable. Where the name was not renamed the
qualification is simply dropped, which is what flattening means for a name
that keeps its spelling. Rewriting `N::x` regardless produced a name nothing
declared: litehtml writes `litehtml::tstring` in fourteen places while its
typedef stays `tstring`, so every qualified use became `litehtml_tstring`
and the declaration did not follow -- and around 35 of 43 sources translated
clean and then failed to compile on a type appearing nowhere.

A namespace may be **reopened**, which a project with one per header does --
litehtml does it forty times. A name produced by flattening an earlier block
of the same namespace is the same entity, not a collision.

What this does not do is overload resolution or argument-dependent lookup.
Flattening is name-mangling, not lookup, so the two ways it could quietly
change meaning are **reported**:

- a flattened name that collides with something already declared, since the
  two would become one symbol -- and the call sites merge before the C front
  end ever gets to report the redefinition;
- a name provided by more than one `using namespace`, which C++ rejects as
  ambiguous and which taking the first of would silently resolve.

### `map` and `pair`

Supplied on `#include <map>`, and written in the subset like the others.

```cpp
std::map<std::string, int> t;
t[key] = 1;
if (t.find(key) != t.end()) { .. }
for (auto it = m.begin(); it != m.end(); ++it) { use(it->second); }
```

**The iterator is a pointer.** That is the whole design: `it->first`, `++it`,
`it != m.end()` and `*it` are then plain C on a plain pointer, and none of
`operator++`, `operator!=` or an iterator class has to exist. It costs a
linear `find` -- the storage is an unsorted array -- which is the honest
trade for a container written in a subset with no comparison operator to
order keys by.

Keys are compared with `__cpp_eq`, which is `==` for a scalar and `T_equals`
for a class, decided per instantiation. A key class needs
`int equals(const T &o)`.

A user-defined key class works, provided it has that `equals`. This used to
be refused for a reason that has since gone: the supplied templates are
spliced above the file, so `K` was not a class this pass had *emitted* when
`map<K, ..>` was emitted, and the key got the by-value spelling. Asking
whether `K` is a class of the whole translation rather than of what has been
emitted so far is the whole fix, and instantiations are now held back until
the classes they are built over are complete.

### `unique_ptr` and `shared_ptr`

Supplied on `#include <memory>`, and like `string` and `vector` they are
**written in this subset** rather than special-cased. Naming the header alone
supplies nothing: an unused template would still be monomorphised.

```cpp
std::unique_ptr<Thing> u(new Thing());
u.get()->v = 4;

std::shared_ptr<Thing> a(new Thing());
std::shared_ptr<Thing> b(a);            // use_count() == 2
```                                     // released at zero

`unique_ptr` declares a move constructor and a move assignment and **no copy
constructor**, which is exactly what move-only means -- and the second half is
enforced by the Rule of Three refusal the subset already made, so copying one
is still rejected with the same diagnostic any other owning class gets:

```cpp
std::unique_ptr<Thing> a(new Thing());
std::unique_ptr<Thing> b(std::move(a));   /* moves; `a` is left null */
std::unique_ptr<Thing> c(a);              /* refused, as in C++ */
```

A `vector<unique_ptr<T>>` works, through `push_back`'s move overload.

Both spell the injected class name with its arguments -- `unique_ptr<T> &&o`,
not a bare `unique_ptr &&o` -- because substitution rewrites the template
arguments and the bare name is not one of them. Written bare, the parameter
came out as a type nothing defines. `shared_ptr` refcounts through a copy
constructor and `operator=`.

Both use `__cpp_drop(T, *p)` rather than `delete p`. Inside a template, `T`
is not known to be a class when the body is parsed, so a plain `delete` frees
the memory without running the element's destructor; the builtin is resolved
per instantiation and does.

Access is through `get()`, `operator->` or `operator*`:

```cpp
u->v = 4;                               // operator->
(*u).v = 4;                             // operator*
u.get()->v = 4;                         // the explicit form
```

Both are supported on any class, not just these. `operator->` returns a plain
pointer -- C++ keeps applying it until one comes back, and this subset does
the first hop only. `operator*` returns a reference and is lowered like
`operator[]`, so `*p = x` assigns through rather than to a copy.

Neither applies to a genuine pointer: `Ptr *p; p->x` means a member of `Ptr`
in C++, not the operator, and `this->` is the same shape -- rewriting
pointers turned every field access inside a class into a call to its own
`operator->`.

### Compound assignment

`operator+=` and its siblings (`-= *= /= %= |= &= ^=`) are supported, lowered
like `operator=`: the result is dropped, so `a += b` is a statement and a
chained `c = a += b` is rejected rather than quietly yielding nothing. Each
gets its own symbol -- `a += b` becomes `T__augadd(&a, &b)` -- spelled out
because the name has to be a C identifier.

The operand is taken like `operator=`'s: it has to be something this pass can
name and address.

### Comparison operators

`operator==`, `!=`, `<`, `<=`, `>`, `>=`. Unlike an assignment the *result*
is the point, so the declared return type is kept: `a == b` becomes
`P__cmpeq(&a, &b)`.

Only a bare name on the left. This pass knows the type of a local; an
expression it would have to infer one for is left alone -- which is also what
keeps `vec<int> v;` from reading as a comparison.

### Conversion operators

`operator T()` is **declarable**, and lowers to an ordinary method
`T__conv(Class *this)`. Refusing the declaration was refusing whole files
over a feature they never used: litehtml has exactly one conversion
operator, in a header every file includes.

What is limited is where the call gets inserted. It goes in where the target
type is **written**:

```cpp
int w = dv;          /* a declaration with a written type */
w = dv;              /* assignment to a local of known type */
```

Anywhere else the conversion is left out, and the C front end reports the
type mismatch on the struct. A conversion applies wherever the compiler
decides one is wanted, and knowing that means knowing the type every
expression is *used at* -- which is type checking, not the reading of written
types this pass does. Written targets are exactly the cases it can be sure
of.

### Nested classes

`class Outer { struct Inner { .. }; };` is hoisted to a top-level
`Outer_Inner`, and both `Outer::Inner` and a bare `Inner` written inside
`Outer` are rewritten to it. The same thing namespaces get, and for the same
reason: C has one flat namespace of struct tags.

Hoisted *above* the enclosing class rather than below, since the outer class
may hold one by value and a by-value member needs its type complete above
it. Innermost first, so `A { B { C { } } }` gives `A_B_C`.

### Default member initializers

`int x = 5;` and `int x {5};` on a member. C has no such thing on a struct
member, so each becomes an assignment at the top of **every** constructor,
which is what it means. An explicit entry in a constructor's initializer list
wins, exactly as in C++.

`T x {};` is value-initialisation and is distinguished from having no
initializer at all: a scalar member is zeroed, and a class member is already
default-constructed by the member prologue.

### Brace initializers

A constructor's initializer list may use either spelling:

```cpp
Ref(Doc *p, int k) : d { p }, n(k) { }
```

The braces mean list initialisation, which for everything this subset lowers
-- a constructor call or a scalar -- is the same call with the same
arguments, so only the spelling differs.

Telling an initializer brace from the body's is done by what precedes it: an
initializer brace follows the member's *name*, the body's follows a `)` or
the `}` that closed the last initializer. That rule is applied only where an
initializer list is actually present, since `union {` has a name before it
too and is a different thing.

### Members defined elsewhere

A member declared with no body and no out-of-line definition in this
translation stays a **declaration**: a prototype is emitted, with external
linkage, and nothing else. That is what C does with one, and the linker says
so if nothing supplies it.

This is ordinary once headers are spliced -- `css_length.h` declares
`fromString` and `css_length.cpp` defines it, so a file that merely includes
the header sees only the declaration. It used to be refused, on the grounds
that an empty body would compile and silently do nothing; that is true, which
is why no empty body is emitted either.

**Translating is not linking.** A member defined in another `.cpp` still
needs that file compiled and linked, which this pipeline does not yet do --
it lowers one translation unit at a time.

### Member specifiers

`final` on a class, and `override`, `final`, `noexcept` or a trailing `const`
after a member function's parameter list, are dropped -- including on a pure
virtual, where they sit between the parameters and the `= 0`. All of them say
what the language may do rather than what the lowering must, and the C front
end checks the body regardless.

### Anonymous unions and structs

Both forms are supported, and both are carried through whole -- C has them
and ShivyCX lowers them, so nothing needs inventing:

```cpp
class css_length {
    union { float m_value; int m_predef; };   /* anonymous member  */
    union { int a; float b; } u;              /* named, anon type  */
};
```

The difference is what the names mean. An *anonymous* member contributes its
own members to the class, so a body writing `m_value` means
`this->m_value`. A *named* one does not: `a` is reached through `u`, so the
body writes `u.a` and gets `this->u.a`. Its own type has no name to record,
which is fine -- what is behind the dot is plain C from there.

### `bool`

A keyword in C++ and a header in C. A `.cpp` writing `bool`, `true` or
`false` has included nothing for it and should not have to, so
`<stdbool.h>` is pulled in -- rather than the type being redefined here,
which would clash with a file that does include it.

### Implicit copy and assignment

A class with a class-typed member gets the copy constructor and assignment
C++ would have written, member by member. The implicit *destructor* built
from the same members already existed; without these a class with an owning
member could not go in a container at all.

Both are generated on C++'s terms:

- **Only when a member knows how to copy itself.** Plain data keeps its
  bitwise copy, and a class whose only owned thing is a *raw pointer* still
  gets the Rule of Three refusal -- no member knows how to duplicate what it
  points at.
- **Deleted when a member cannot be copied**, exactly as in C++. A Crust
  type handed over as owning is one such member.
- **Assignment releases first and guards self-assignment**, since `a = a`
  would otherwise destroy the object and copy from the wreckage.

### Element builtins

A template body is textual, so it can spell `T` but not `T_copy`:
substitution rewrites whole words. Four builtins are the hook that lets a
container say what it wants once and have it mean the right thing per
instantiation:

| | class | scalar |
|---|---|---|
| `__cpp_copy(T, dst, src)` | `T_copy(&dst, src)` | `dst = src` |
| `__cpp_movein(T, dst, src)` | `T_move(&dst, src)` | `dst = src` |
| `__cpp_drop(T, x)` | `T_drop(&x)` | nothing |
| `__cpp_eq(T, a, b)` | `T_equals(&a, b)` | `a == b` |
| `__cpp_ref(T)` | `const T &` | `T` |
| `__cpp_rref(T)` | `T &&` | `T` |

`__cpp_ref` exists because a container cannot pick one spelling for a key
parameter: by value it refuses an owning key (the copy is never constructed
or destroyed), and by reference it cannot bind `m[3]`, since a literal has no
address.

Scalar **references** are lowered too -- `int &x` becomes `int *x` and its
uses are dereferenced. A class reference needs no dereference, because every
use of one is a member access and the symbol table already turns `o.x` into
`o->x`; a bare `k` has no member to go through.

## Not supported yet

Reported rather than mistranslated: exceptions (`throw` / `try` / `catch`),
`dynamic_cast`, `typeid`, multiple and virtual inheritance, a conversion
operator, the stream operators, and the rest of the STL.

Operator overloading is **partly** in: `=`, the compound assignments, the
comparisons, `[]`, `->`, `*`, and the binary arithmetic operators (see
above). What is out is everything else, including `<<` and `>>`.

Two shapes are worth calling out because they are legal C++ that this subset
cannot express, rather than features not yet written:

* **A move-only type in a container.** `std::vector<std::unique_ptr<T>>`
  works now -- see "Moving into a container" below. What is still out is a
  container of a type with *neither* a copy nor a move constructor: there is
  no way to get an element into place.
* **A class-scoped typedef.** `X::ptr` and `X::vector` are deliberately left
  alone -- taking them flatly would make every `vector` in the file mean
  `X::vector`, including the supplied template of that name. A qualified use
  therefore reaches the C front end with its `::` intact, so write the type
  out.

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

## What a translation costs

A `.cpp` is not lowered on its own. Every header it includes is spliced in
first, because a class this file uses is only a class if its declaration is
in hand -- so `litehtml/src/document.cpp`, 1175 lines on disk, is a little
over a megabyte by the time the passes below run over it.

That makes the shape of each pass matter more than its constant. A pass that
rescans the unit once per thing it finds is quadratic in the file, and a
translation unit this size is where that stops being theoretical: it is the
difference between a wait and a break. The passes are held to one scan per
sweep, and the tests in `TestTranslationScales` pin the properties that
keep them there:

* **Monomorphisation** rewrites every innermost `Name<..>` use in one pass,
  so the number of passes is the nesting depth rather than the number of
  instantiations. `document.cpp` names some seven thousand uses and nests
  two deep.
* **Lookbacks** -- the return type before a `(`, the target of a `new`,
  whether a `{` opens a struct body -- read the run of characters they can
  actually match, never a fresh copy of the file up to that point.
* **The character walkers** (`_rewrite_scopes`, `_rewrite_calls`) try their
  patterns only where one could begin, which is a `*` or the first character
  of a word. Between those they copy and move on.
* **Blanking** (comments, literals, braces) jumps from one opener to the
  next instead of walking every character, and namespace flattening blanks a
  body once per visit rather than once per name it renames.

None of this changes what is translated or what is refused. If a change here
makes a file translate that did not before, or produces different C, that is
a bug in the change and not an improvement.

## Tests

```sh
python3 -m unittest tests.test_cpprust          # the subset itself
python3 tools/test_cpprust_extras.py            # features in flight
python3 tools/test_std_move_lowering.py         # `std::move`
python3 tools/litehtml_test.py --groups         # against real litehtml
```

`tests/test_cpprust.py` is the suite for the subset as documented here.

`tools/test_cpprust_extras.py` is the inner loop for work in progress: each
test is a *distilled* version of a shape found in real litehtml, cut down to
the few lines that exercise the gap, and each docstring names the file it
came from -- so when a test there passes and the corresponding litehtml file
still fails, the distillation was incomplete, and that difference is itself
worth knowing. Guardrail tests sit beside the feature tests, because for
this pass a refusal *is* the contract: a feature that lands by turning a
diagnostic into a silent miscompile is a regression, and the tests have to
be able to say so.

`tools/test_std_move_lowering.py` is the same loop for `std::move`, kept in
its own file while expression position is still open. Its centre of gravity
is `TestMovedFromIsStillDestroyed`, which asserts something no *output* is
wrong without: that the source is dropped. A regression there produces C that
compiles, runs, and passes every other test in the tree, because the classes
one writes to be moved from are exactly the ones whose destructors tolerate
being run on a husk. It folds into `test_cpprust_extras.py` when the feature
is whole.

`tools/litehtml_test.py` is the acceptance test. It lowers the real litehtml
sources with the include path already set and then runs `gcc -fsyntax-only`
on the result -- ShivyCX is the real target, but gcc is much faster and
rejects the same broken C. The compile stage is not a formality: it is what
catches a lowering that *succeeded* and produced C that does not mean
anything, which translation alone cannot see.

`--groups` reports failures by cause rather than by file. One refusal in a
shared header fails every file that includes it, and grouping is what shows
which single fix buys the most files. Translations are cached against the
translator's own sources, so editing `cpprust.py` invalidates everything.
