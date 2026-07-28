# Crust — C+Rust syntax

Crust lets one translation unit hold C functions and Rust functions side by
side, with no FFI boundary between them. A Rust function calls a C function
(and vice versa) as a direct call: same IL, same register allocator, same
whole-program passes, full inlining and interprocedural visibility.

```sh
chmod +x ./crust
./crust mixed.c -o mixed     # C file containing Rust functions
./crust prog.rs -o prog      # all-Rust file
```

or directly with Python (also works with PyPy3)

```sh
python3 -m shivyc.main mixed.c -o mixed     # C file containing Rust functions
python3 -m shivyc.main prog.rs  -o prog     # all-Rust file
```

A C file can also pull in a Rust module with an ordinary include:

```c
#include "vec2.rs"                       /* lowered to C, then included */
```

## How it works

Per the architectural philosophy, Crust enforces **function-level syntax
isolation**: syntax is chosen per function, never within one. `shivyc/crust.py`
scans the source for top-level Rust items -- `fn`, `struct` and `impl`, at
brace depth 0, with comments and string literals blanked so they can't create
false matches -- parses each with a recursive-descent parser for the Rust
subset, and splices equivalent C text back in place. The C text outside those
items passes through byte-for-byte.

Everything downstream is untouched — the pass runs immediately before
`extensions.preprocess_extensions` in `process_c_file`, so the C lexer never
sees Rust. A `.rs` file on the command line goes through the same path.

Two details make the output behave like ordinary source:

- **Line numbers are preserved.** The emitter syncs to the line of each source
  construct and pads each item to its original line count, so diagnostics from
  the parser, the memory-safety pass, or the register allocator point at the
  Rust line the user wrote.
- **A one-line prelude** carries struct declarations, struct definitions (in
  dependency order) and function prototypes. It is prefixed to the first line
  that is neither a preprocessor directive nor the interior of a block
  comment, so no line numbers shift and no `#` line is clobbered. Definition
  order therefore doesn't matter anywhere in the unit.

Item collection runs as a separate first pass over the whole unit, which is
what lets a method call a function declared later, or a struct be used above
its definition.

## Supported subset

| Area | Supported |
|---|---|
| Items | `fn`, `struct`, `impl`, `enum`, `const`, `static`, with optional `pub`, `unsafe`, `extern "C"`; `#[...]` attributes are skipped |
| Generics | `fn f<T>`, `struct S<T>`, `impl<T> S<T>`, turbofish `f::<T>()`, monomorphised per instantiation |
| Core | bundled `Vec<T>`, `Box<T>`, `Cell<T>`, `PhantomData<T>`, and `size_of::<T>()` |
| Traits | `trait`, `impl Trait for Type`, default methods, supertraits, bounds `<T: Trait>`, associated consts through a type parameter (`T::CONST`) and with inherited defaults; static dispatch |
| Macros | `println!`/`print!`/`eprintln!`, `assert!`/`assert_eq!`/`assert_ne!`, `panic!`/`unreachable!`/`todo!`, `debug_assert*!`, `cfg!`, `matches!`, and `macro_rules!` |
| Tuples | tuple types `(A, B)`, tuple expressions, positional access `t.0` |
| Data enums | `enum E { A(T), B { x: T }, C }` as a tagged union, with `match` bindings |
| Derive | `#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]` on structs |
| Closures | non-capturing `|a: T| expr`, lifted to a plain function |
| Paths | `a::b::C` in type position |
| Module items | `use` and `extern crate` are erased; `mod X;` is erased but sibling `X.rs` / `X/mod.rs` are read for type definitions; `core::ffi::c_*` map to their C types |
| Type aliases | `type Name = T;` (optional `pub`), emitted as a C `typedef` |
| Visibility | `pub`, `pub(crate)`, `pub(in path)`, `pub unsafe extern "C"` |
| Lifetimes | `'a` is accepted and dropped |
| Types | `i8 i16 i32 i64 isize`, `u8 u16 u32 u64 usize`, `f32 f64`, `bool`, `char`, `()`, `&str` |
| Pointers | `*const T`, `*mut T`, `&T`, `&mut T` (all lower to `T *`) |
| Arrays | `[T; N]`, and slices `&[T]` / `&mut [T]` |
| Option | `Option<T>`, `Some(x)`, `None`, `is_some`, `is_none`, `unwrap`, `unwrap_or`, `if let`, `while let` |
| Result | `Result<T, E>`, `Ok(x)`, `Err(e)`, `is_ok`, `is_err`, `unwrap`, `unwrap_err`, `unwrap_or`, `ok`, and the `?` operator |
| Statements | `let` (with `mut` and optional annotation), `return`, `if`/`else if`/`else`, `while`, `loop`, `for x in a..b` and `a..=b`, `for x in slice_or_array`, `match`, `break`, `continue`, local `const`, blocks |
| Expressions | literals, array literals `[a, b, c]` and repeats `[v; N]`, calls, indexing, field access, unary and binary operators, compound assignment, `as` casts, `if`/`else` as an expression |
| Tail expressions | a trailing expression in a function body becomes its return value |
| Structs | field declarations, struct literals `P { x: 1 }`, field access with auto-deref, nested struct fields |
| Methods | `&self`, `&mut self`, `self`, associated functions, associated `const`s, `Self`, method calls `p.m()`, path calls `P::m()` |
| Enums | C-like variants with optional discriminants, `E::V` paths, exhaustiveness-checked `match`, `impl` blocks |
| Tuple structs | `struct P(T, U);`, construction `P(a, b)`, positional access `p.0` |
| Unit structs | `struct S;`, used as its own value, with `impl` blocks |

Type mapping is the obvious one (`i32` → `int`, `u64` → `unsigned long`,
`f64` → `double`, `bool` → `_Bool`, `()` → `void`). An unrecognized named type
is assumed to be a C `struct`/`typedef` of the same name, which is how a Rust
function takes a C type as a parameter.

`fn main()` with no return type becomes `int main(void)` with an implicit
`return 0;`, since the compiler requires `int main`.

`!` follows Rust semantics rather than C's: it lowers to `!` when the operand
is known to be `bool` and to `~` otherwise.

Local `let` bindings without an annotation are inferred from the initializer
using a small local type environment (declared locals, parameters, and the
return types of every `fn` in the unit). When inference can't produce a type,
Crust reports an error asking for an annotation rather than guessing.

## Structs and impls

A `struct` lowers to a C `struct` plus a `typedef` of the same name, hoisted
into the prelude in dependency order so a by-value field is always defined
before its user. A recursive by-value field is rejected with a diagnostic
rather than producing an infinite type.

An `impl` block lowers to free functions named `Type_method`. The receiver
becomes an explicit first parameter: `&self` and `&mut self` become
`Type *self`, and `self` becomes `Type self`. Because the mangling is
predictable and the ABI is just C, **C code calls Rust methods directly** as
`Vec2_len2(&v)` — there is no wrapper and no FFI shim.

At call sites Crust reproduces Rust's auto-ref/auto-deref: `p.norm2()` becomes
`Point_norm2(&p)` when `p` is a value and `Point_norm2(p)` when it is already
a pointer, and `p.x` becomes `p->x` through a reference. This needs the
receiver's type, so a method call on an expression Crust cannot type is a
diagnostic asking for an annotation rather than a guess.

Struct literals lower to C compound literals with designated initializers.
Missing or unknown fields are reported at translation time. As in Rust, a bare
struct literal is not parsed in condition position, so `if p.x > 0 { ... }`
reads the brace as the start of the body.

## Enums and match

An `enum` lowers to a C `enum` whose members are prefixed with the type name,
so `Color::Red` becomes `Color_Red` and the C side can use the same spelling.
Because C and Rust spell a simple enumeration body identically, Crust
distinguishes them by what follows the closing brace: C requires a `;` there
and Rust forbids one. Data-carrying variants are rejected rather than
silently mislowered.

`match` lowers to a `switch`. Rust arms don't fall through, so each arm ends
in an explicit `break`; `|` patterns become stacked `case` labels and `_`
becomes `default`. When the scrutinee is an enum and there is no `_` arm,
Crust checks exhaustiveness and names the variants that are missing:

```
line 2: non-exhaustive match on `E`: `C` not covered; add an arm or `_`
```

Patterns are limited to literals, enum variants, constants and `_`. A bare
identifier would be a *binding* in Rust, and Crust has no bindings, so it is
rejected instead of being silently treated as a comparison.

## Constants

`const NAME: T = expr;` and `static [mut] NAME: T = expr;` are told apart from
their C counterparts by the type annotation, which C never writes here. Both
are hoisted into the prelude, so a constant may be used above its definition.

An integer `const` becomes a C *enum constant* rather than `static const`,
because only the former is a constant expression and so only the former can
size an array — `let a: [i32; N]` works. Floats, pointers and `static` items
become ordinary file-scope objects.

## Strings and slices

`&str` lowers to `const char *`, so a Rust function returning a string is
directly usable from C and a literal needs no conversion. `s.len()` becomes a
`strlen` call, and the prototype is added to the prelude only when something
uses it. Bare `str` is rejected the way Rust rejects it — unsized, write
`&str` — rather than silently becoming a single character.

`&[T]` and `&mut [T]` lower to a generated fat-pointer struct:

```c
struct crust_slice_int { int *ptr; unsigned long len; };
```

One struct is generated per element type, named after it, and emitted only
when used. `xs.len()` reads the `len` field, `xs[i]` indexes through `ptr`,
and `&a[..]`, `&a[lo..]`, `&a[lo..hi]` and `&a[lo..=hi]` build one — taking
the length from the array's own type when no end bound is given. Slicing a
raw pointer without an end bound is an error, since its length isn't known.
`&[T; N]` remains a reference to an array, not a slice, as in Rust.

Because the representation is an ordinary C struct, **C can build and pass
slices too** — `(crust_slice_int){data, 6}` is exactly what Crust emits — so
the boundary stays free of conversion shims.

## Iteration

`for x in xs` walks a slice or an array directly, lowering to the index loop
you would otherwise write by hand:

```rust
for x in xs { total += x; }
```

```c
{ crust_slice_int _crust_opt1 = xs;
  for (unsigned long _crust_i2 = 0; _crust_i2 < _crust_opt1.len; _crust_i2++)
  { int x = _crust_opt1.ptr[_crust_i2]; total += x; } }
```

The subject is held in a temporary, so a call in that position is evaluated
once per loop rather than once per iteration, and the whole construct is
wrapped in a block to scope both the temporary and the induction variable. An
array takes its length from its own type; a raw pointer is rejected, since its
length is not known — slice it first (`&p[0..n]`) or use a range.

The binding is a **copy** of the element, which is `for x in xs.iter().copied()`
rather than Rust's reference binding. Crust has no borrow checker to make the
difference observable, and a copy is what the C on the other side of the
boundary would do. `.iter()` and `.iter_mut()` are accepted as no-ops so the
idiomatic spelling reads the same; there is no iterator protocol behind them,
so adaptors like `.map` are not available.

## Unit structs

`struct S;` declares a struct with no fields, and as in Rust its own name is
its value. C11 has no empty struct, so the lowered type carries a single
placeholder byte named `_crust_unit`; constructing one is just zeroing it.
`impl` blocks work as they do on any other struct.

C spells a forward declaration exactly the same way, and unlike the `enum`
case (told apart by the trailing `;`) or a struct body (told apart by `name:
type` fields), nothing in the text itself distinguishes the two. Reading a C
forward declaration as a complete one-byte type would be a real miscompile —
`sizeof(struct S)` on an incomplete type must fail, and would wrongly start
succeeding. So Crust claims `struct S;` only on evidence:

- the whole file is Rust (a `.rs` input, where there is no C to be ambiguous
  with), or
- the unit gives the name an `impl` block, which C cannot spell.

The cost is that a unit struct with no `impl`, in a `.c` file, is left as C.
That is the safe direction to fail, and it follows the same
disambiguate-on-evidence rule the rest of the front end uses.

## Array repeats

`[v; N]` is written out element by element, so `[7; 3]` becomes `{7, 7, 7}`.
The length must be an integer literal or an integer `const` — which Crust
already lowers to a C enum constant precisely so it can size an array:

```rust
const LANES: usize = 4;
let bias: [i32; LANES] = [3; LANES];
```

The all-zero form `[0; N]` is special-cased to `{0}`, which zero-fills
whatever the annotation sizes and so needs no literal length at all. A
non-zero repeat with a non-constant length has nothing to expand to and is
rejected rather than silently mislowered.

## Option

Crust has no generics, so each `Option<T>` is **monomorphised** into its own
tagged struct, generated on demand exactly like a slice:

```c
struct crust_option_int { _Bool some; int value; };
```

`Some(x)` fills it in and `None` zeroes it. `None` carries no type of its
own, so it is resolved from the context it appears in — a `let` annotation, a
return type or a parameter type. Where there is no context to read, Crust
asks for an annotation instead of guessing.

`unwrap()` is a real check, not a reinterpretation: it calls a generated
helper that aborts when the option is empty, so a mistaken unwrap traps
instead of returning garbage. `unwrap_or(d)` is inlined as a ternary, and
`is_some`/`is_none` read the tag.

`if let Some(x) = e { .. } else { .. }` and `while let Some(x) = e { .. }`
lower to a temporary plus a test, so the subject is evaluated exactly once
and the binding is scoped to the arm that owns it. Only the `Some(x)` pattern
is supported — `match` on an `Option` needs pattern bindings, which Crust
does not have.

## Result and `?`

`Result<T, E>` is monomorphised exactly like `Option<T>`, into a struct
carrying the tag and both payloads:

```c
struct crust_result_int_e_Error { _Bool ok; int value; Error error; };
```

`Ok(x)` and `Err(e)` use designated initializers, so the unused payload is
zeroed rather than left indeterminate. Both read their type from context —
usually the enclosing function's return type — the same way `None` does.

`unwrap` and `unwrap_err` generate checked helpers that abort on the wrong
variant. `.ok()` converts a `Result<T, E>` into an `Option<T>`, generating
that `Option` instantiation if it doesn't already exist.

The `?` operator is the reason error handling stops looking like C. It's an
*expression* in Rust but needs a *statement* in C, so Crust queues the
temporary and the early return as pending statements and emits them just
before the statement containing the `?`; the expression itself becomes a read
of the temporary's payload. This means the operand is evaluated exactly once
even in `Ok(f()? + 1)`.

```rust
let x: i32 = parse_i32(a)?;
```

```c
crust_result_int_e_Error _crust_opt1 = parse_i32(a);
if (!_crust_opt1.ok) return (crust_result_int_e_Error){.ok = 0, .error = _crust_opt1.error};
int x = _crust_opt1.value;
```

`?` also works on `Option` inside a function returning `Option`. Crust has no
`From` conversions, so the error types must match exactly; a mismatch is a
diagnostic naming both types rather than a silent reinterpretation.

## Rust modules by `#include`

`#include "foo.rs"` works from C. The hook is in the preprocessor's include
resolution (`preproc._do_include`), so `-I` directories, quoted vs
angle-bracket lookup, nested includes and include guards all behave normally;
the file's text is passed through Crust before it is lexed.

Diagnostics name the Rust module and its own line, not the including file:

```
prog.c:1:10: error: crust: lib/bad.rs: line 4: struct `P` has no field `nosuchfield`
```

Crust separately reads the *items* of any included `.rs` file when translating
the includer, without inlining its text. That is what lets a Rust function in
the C file construct a `Vec2` or call `Vec2::new`, while the `#include` itself
stays in place for the preprocessor to expand.

## Interop notes

Three things bite when Rust and rpython meet in one unit, all mechanical once
known:

**Strings are `c_char`, not `char`.** py2c spells `str` as C `char *`. A Rust
`char` is four bytes, so `*mut char` lowers to `unsigned int *` and will not
match. The right spelling is `*mut c_char`, which is exactly C's `char *`.

**Module globals need their initialiser.** py2c emits a `<module>_init()` per
module that populates module-level globals. A module whose globals are plain
integers works without it; one holding a list or a string does not, and the
symptom is a silently empty table rather than a crash. Call it once before
anything reads them.

**A struct literal cannot sit directly in an `if` condition.** `if
f(E::V { x: 1 }) { .. }` is ambiguous with the block that follows, in Crust as
in Rust. Bind it first.

## rpython modules by `#include`

The repo already has a second source-to-source front end: `tools/py2c.py`,
which lowers rpython to C. So `#include "foo.py"` can be answered the same way
`#include "foo.rs"` is — transpile the module, splice the generated C in where
the directive stood. The hook is the same one Crust uses
(`preproc._do_include`), so `-I` directories, quoted vs angle-bracket lookup,
nested includes and include guards all behave normally, and one translation
unit can hold all three languages:

```c
#include "vec2.rs"        /* Rust    */
#include "histogram.py"   /* rpython */

fn bar(count: i32, total: i32) -> i32 {   /* Rust, calling rpython */
    scale_to_width(count, total, 40)
}

int main(void) { ... }    /* C, calling both */
```

Everything is ordinary C by the time the lexer runs, so every call across
those boundaries is a direct call — same IL, same register allocator, full
inlining — exactly as it is between C and Rust.

Two post-processing rules, shared with `main.process_py_file` so an included
module and one named on the command line are classified identically:

- A module that touches the transpiler runtime (lists, dicts, strings,
  objects) keeps its `#include "shivyc_rt.h"`. The header is written into the
  same cache directory as the generated C, so the nested include resolves
  relative to it, and `shivyc_rt.c` is compiled and added to the link line.
- A module that does not — a pure numeric kernel — gets the runtime include
  dropped and only the libc/libm prototypes it actually names prepended, so it
  compiles as plain C11 with nothing to link.

### Caching

Transpiling is by far the slowest step, and `shivyc_rt.c` is ~48KB of C that
would otherwise be recompiled on every build. Both are cached under
`/tmp/crust-rpy/`, keyed by a hash of the module text and of `py2c.py` itself,
so editing either busts the cache. The compiled runtime object is cached
alongside, keyed additionally by a hash of every code-generation flag, so an
object is only ever reused for an identical configuration.

The effect on a rebuild that touches nothing:

| build | cold | warm |
|---|---|---|
| module using the runtime | 2.5s | 0.55s |
| `make test_fast_crust` | 3.8s | 1.7s |

Set `CRUST_RPY_CACHE` to move the cache; `make clean_crust` empties it.

### Caveats

`py2c` does not preserve line numbers, so unlike the `.rs` path — where
diagnostics name the Rust module and its own line — a diagnostic from an
included `.py` names the *generated* C in the cache directory. The generated
file is kept on disk precisely so it can be read.

A module with globals needs its generated `<module>_init()` called before
those globals are live; the examples here have none. And `shivyc_rt.h` pulls
in the bundled `<stdio.h>`, which declares `printf` with no prototype, so a
unit that prints a `double` should declare `int printf(const char *, ...);`
itself, as every example in `examples/crust` does.

## `unsafe`

`unsafe { ... }` is accepted in both statement and expression position and is
exactly its body. Crust has no borrow checker and no safety analysis to switch
off, and the C it lowers to is unsafe throughout, so the block carries no
meaning beyond grouping. `unsafe fn` is handled with the other item modifiers.

This matters out of proportion to its size: in a survey of the Redox kernel,
relibc and ion (615 files), `unsafe` blocks were the single largest blocker
after generics, stopping 44 files on their own.

## Generics

Generics are **monomorphised**, the same way `Option` and `Result` already
were: a generic item is kept as its tokens, and each distinct set of type
arguments re-parses those tokens with the parameters bound to concrete types.

```rust
struct Pair<T> { a: T, b: T }
impl<T> Pair<T> { fn sum(&self) -> T { self.a + self.b } }
```

```c
struct Pair_int    { int a;    int b; };
struct Pair_double { double a; double b; };
int    Pair_int_sum(Pair_int *self);
double Pair_double_sum(Pair_double *self);
```

An instantiation is an ordinary C struct and an ordinary C function. Nothing
is boxed, nothing carries a tag, and there is no vtable or dispatch, so
**C can build and call an instantiation directly** — `Pair_int_sum(&p)` — and
the no-FFI property that motivates Crust survives generics intact.

This is worth being explicit about, because the obvious shortcut is the wrong
one here. py2c has a tagged dynamic word (`obj`) that could represent any `T`
in one uniform layout, and reaching for it would have made the parser work
much easier. But it erases the type: `T` as a struct field would no longer
have the right size or offset, `sizeof` would be wrong, every value would need
boxing and unboxing at the boundary, and C could no longer pass a `Pair<i32>`
without conversion. Rust's own semantics are static, and so is the C we want
out. py2c's *other* container model — the `_tlist_int` / `_tlist_double`
types it generates on demand, one per element type — is the right precedent,
and it is the same one Crust was already following.

Instantiation is demand-driven: an unused generic emits nothing at all, and
two calls at the same type share one instantiation.

**Type arguments** come from a turbofish (`id::<i32>(x)`, `Pair::<f64>::new`)
or are inferred from the call's arguments. Inference is deliberately shallow —
Crust has no type checker. A parameter declared as exactly a type variable
(`x: T`), or one reference or pointer step from one (`x: &T`), binds that
variable. Anything deeper (`&[T]`, `Wrap<T>`) is not inferred and needs a
turbofish. Where a type argument cannot be determined, Crust says so and names
what to write instead of guessing:

```
line 2: cannot infer type argument `T` for `make`; give it explicitly with a
turbofish, `make::<i32>(..)`
```

A generic struct literal reads its instantiation from context — a `let`
annotation, a return type, a parameter — exactly as `None` resolves its
`Option`. Where there is no context, it asks for one.

**Not supported:** trait bounds are parsed and then ignored, since there are
no traits to check against — where a bound would have caught a mistake, the
generated C fails to compile instead. There are no lifetimes, no const
generics, no generic enums, and no `where` clauses. Crust also monomorphises
only from source it can see: a generic from `std`, `core` or another crate has
no template to instantiate, and is reported as such rather than guessed at.

## Module items, and a measurement that was lying

`use` and `extern crate` exist for Rust's module system and have no C
counterpart. Crust resolves names by flattening paths instead, so by the time
the C lexer runs there is nothing for a `use` to do — they are **erased**,
blanked in place so line numbers do not move. `core::ffi::c_int` and its
siblings map to exactly the C types they name.

`mod X;` is erased the same way for line numbers, but Crust also opens the
sibling file (`X.rs`, or `X/mod.rs`) and seeds the current unit from its
type definitions — structs, enums, consts, `type` aliases, and method
signatures. Those decls are hoisted into the prelude so a file that only
*uses* a crate-local type can compile alone under `survey --verify`. Function
bodies stay in the sibling; Crust does not inline whole modules, and does not
model visibility or `use` path binding.

`type Name = T;` (with optional `pub`) is a first-class item: it resolves in
type position and emits as a C `typedef`. That is what makes crate-local
aliases like `pid_t` and `ssize_t` visible across `mod` boundaries. Types that
live only in std/core (`String`, `AtomicU32`, …) still need a definition Crust
can see — there are no fake std stubs.

Both erasures are otherwise trivial. What is worth recording is why they went
unnoticed for so long, because it is a lesson about the tooling rather than
about Rust.

`crust.translate` passes text it does not recognize through byte-for-byte —
that is the whole design, and it is what lets C and Rust share a file. So
`use core::mem;` *translated* perfectly: it came out the other side unchanged
and only failed later, in the C front end. `tools/crustos.py survey` measures
translation, so it reported success for every one of those files. The feature
never appeared in any blocker ranking, and `use` appears in 94.5% of the
corpus.

Measuring compilation instead of translation changed the picture completely:

| | files |
|---|---|
| translate (at least one lowered item) | 105 |
| **actually compile to an object** | **92** |

Erasing `use` and mapping the `c_*` types took that second number from 12 to
33 — nearly a 3x improvement from two small changes that three rounds of
ranking blockers by first-error frequency had never surfaced. Working through
the compile-stage errors that remained (nested `use` groups, `mod X;`,
visibility qualifiers, lifetimes) took it to 50. None of those was a language
feature; all of them were things that translated cleanly and failed later.

`survey --verify` now compiles the translatable files and reports both
numbers, along with what stops the rest. It costs about a minute and it is the
number that actually answers "how much of Redox can Crust compile".

`tools/crustdeep.py` complements it by scanning every file for *all* the
constructs Crust cannot handle, rather than stopping at the first. It reports
how many blockers a typical file has (mean 3.1), how many files each feature
would unlock *on its own*, and a greedy set-cover over the real data — which is
a far better roadmap than a frequency ranking, because a file is only unlocked
when every one of its blockers is gone.

## `#[derive(..)]`

A derived trait generates the same free functions a hand-written `impl` would,
so it dispatches statically and costs nothing extra:

```rust
#[derive(Clone, PartialEq, Default, Debug)]
struct Point { x: i32, y: i32 }
```

```c
Point Point_clone(Point *self)              { return *self; }
_Bool Point_eq(Point *self, Point *other)   { return self->x == other->x && ...; }
Point Point_default(void)                   { Point v = {0}; return v; }
void  Point_debug(Point *self)              { printf("Point { x: %d, y: %d }", ...); }
```

`Copy` and `Eq` are markers in Rust — they add no method — so they are accepted
and generate nothing. A trait Crust cannot derive (`Hash`, `Serialize`, ...) is
ignored rather than reported, since the attribute itself is not an error. **A
hand-written `impl` always wins**: a derived method is never generated for a
name the type already defines.

`Debug`'s method is called `debug`, not `fmt`. Rust's writes into a formatter
and Crust has no `String`, so the derived one prints directly — giving it a
different name keeps that difference visible rather than implying it satisfies
a real `fmt::Debug` bound.

Derivation is skipped, rather than approximated, in two cases. A field that is
not a C scalar cannot be compared with `==` or handed to printf, so a struct
containing one derives nothing (`memcmp` would compare padding too). And a
struct *seeded* from a sibling module is left alone: this translation unit has
its declaration but not its definition, and the sibling's own unit derives it
anyway.

### A backend bug this exposed

`Clone` returns a struct by value, which turned out to crash the backend for
any struct whose size is 3, 5, 6 or 7 bytes — `NotImplementedError: unexpected
register size`, raised from inside register naming rather than as a
diagnostic. SysV returns such a struct in a full eightbyte of RAX, but the
backend was moving exactly `size` bytes, and there is no 3-byte register.

Fixed in `il_cmds/control.py` on both sides of the call, and for the partial
high eightbyte of a 9..16 byte struct (sizes 11, 13, 14, 15 had the same
problem). The widened move is safe from either kind of source: a register
already holds the whole value, and a stack slot is allocated in eightbyte
units. This was never specific to `derive` — any Rust or C function returning
such a struct hit it.

## `#[cfg(..)]`

Real crates offer alternatives — one `ULONG_MAX` for 32-bit pointers and
another for 64 — and Crust used to emit *both*, producing two conflicting
definitions of the same name. That is what a `#[cfg]`-gated file looks like
when the gate is ignored, and it made whole files uncompilable for a reason
that had nothing to do with the language subset.

Crust now evaluates `cfg` predicates against a fixed target:

| key | value |
|---|---|
| `target_arch` | `x86_64` |
| `target_pointer_width` | `64` |
| `target_endian` | `little` |
| `target_os` | `redox` |
| `target_family` | `unix` |
| `target_env` | `relibc` |

`all(..)`, `any(..)`, `not(..)`, `key = "value"` and bare flags all work. An
item whose `cfg` is false is **erased** — blanked in place, so line numbers do
not move — because dropping it from the item list alone would leave its Rust
source in the output for the C front end to choke on.

Two deliberate choices. An **unknown key is false**: treating it as true would
select several arms of the same set of alternatives, which is exactly the
failure this exists to prevent. And an item with **no** `cfg` is always kept —
this mechanism only ever removes an arm written for a different target, it
never gates anything on its own.

There is no way to change the target yet. When there is, it belongs in `CFG`
in `shivyc/crust.py` and should be reachable from the command line.

## `Result<T>` and the crate-wide alias

Almost every crate that fixes its error type does it once —
`type Result<T> = core::result::Result<T, Error>;` — and then writes the
**one-argument** `Result<T>` everywhere after. Redox does exactly that, and it
was the second most common translation failure.

Crust accepts both spellings. With one argument, the error type comes from the
unit's own `Result<T>` alias if it declares one, and otherwise defaults to a
plain integer error code.

A *generic* type alias has no representation here — Crust monomorphises, and
an alias is not an item to monomorphise — so one is **skipped rather than
reported**. Failing a file over an alias would be out of proportion: a crate
that declares one is otherwise perfectly translatable, and the alias that
actually matters is recognized separately.

## The sync and pointer wrappers

`UnsafeCell<T>`, `SyncUnsafeCell<T>` and `NonNull<T>` are **faithful**. In real
Rust `UnsafeCell<T>` is a struct with one field whose only job is to tell the
compiler that aliasing rules do not apply; Crust has no aliasing rules to
suspend, so the wrapper carries the same information here — none. `NonNull<T>`
is a pointer plus a promise the programmer makes, not a runtime check.

`Once<T>` is faithful for a single-threaded caller. What is missing is the
blocking half: a real `Once` makes a second thread wait. There are no threads,
so there is nothing to wait for.

**`Mutex<T>` and `RwLock<T>` do not synchronise.** `lock()` hands back a
pointer to the inner value and nothing else happens. This is defensible only
because Crust has no threads at all — no spawn, no atomics, no memory model —
so there is nothing for a lock to protect against, and a lock that does nothing
is consistent with the rest of the model rather than a hole in it. **The moment
real concurrency exists, these become actively dangerous and must be replaced,
not extended.**

They are deliberately not named something honest like `FakeMutex`, because the
entire point is to accept source written as `Mutex<T>`. The warning has to live
in the documentation instead, which is why it is stated this plainly.

## Data-carrying enums

An enum variant may carry data, in tuple form `Circle(f64)` or struct form
`Rect { w: f64, h: f64 }`. These lower to the tagged union a C programmer
would write by hand:

```c
enum Shape_tag { Shape_Circle, Shape_Rect, Shape_Empty };
struct Shape_Circle_data { double _0; };
struct Shape_Rect_data { double w; double h; };
struct Shape { enum Shape_tag tag; union { ... } u; };
```

Payload structs are declared separately rather than inline: a named type reads
better in a diagnostic, and it avoids depending on anonymous-struct support.
Tuple fields are named positionally (`_0`, `_1`), which is the same convention
tuple structs and tuples already use, so all three are constructed and matched
through one mechanism. A variant with no payload contributes no union member.

A payload-free variant of a data enum is still a whole value — `Shape::Empty`
builds `(Shape){.tag = Shape_Empty}`, not a bare tag — so it can be assigned
and passed like any other `Shape`.

### Matching

`match` on a data enum switches on the tag, with the scrutinee held in a
temporary so it is evaluated once even when it is a call:

```rust
match s {
    Shape::Circle(r)      => 3.14159265 * r * r,
    Shape::Rect { w, h }  => w * h,
    Shape::Empty          => 0.0,
}
```

Each arm's bindings are declared inside their own block, so a name bound in one
arm cannot leak into a later one — C's `case` labels would otherwise allow
exactly that. Struct patterns may rename (`Msg::Move { x: got }`), `_` discards
without declaring anything, and `..` skips the rest. Exhaustiveness is still
checked: a missing variant is reported rather than silently falling through.

## Cross-file types

Crust compiles one translation unit at a time, but a Rust crate spreads its
types across files: `pub type pid_t = c_int;` lives in `platform/types.rs` and
is used everywhere. Two mechanisms bring those definitions into view.

`mod X;` declarations are followed to any depth — they describe the crate's own
tree, so following them is cheap and always relevant. `use crate::a::b::{..}`
paths are followed **one level**, from the file being compiled only: a `use`
can reach across an entire workspace, and the types worth having are almost
always in the file named directly. `crate::` resolves against the directory
holding `lib.rs` or `main.rs`, found by walking up from the current file;
`super::` and `self::` resolve relative to it.

Only *declarations* are taken — structs, enums, consts, type aliases and method
signatures. No function body is translated, because the sibling owns its own
translation unit.

**Seeding is best effort and never fatal.** A sibling that cannot be read or
parsed costs the types it defines and nothing more. This is the whole design
constraint: if a broken sibling could fail the current file, then adding a
`use` would be strictly worse than leaving a type undeclared, which is the
opposite of the point.

### File-scope macros

A macro invoked at file scope — `global_asm!(..)`, `int_like!{..}` — expands to
items Crust cannot produce, and passing it through verbatim guarantees a C
syntax error (`global_asm!` in particular carries a multi-line assembly string
the C lexer reads as an unterminated quote). These are erased. The cost is real
— whatever the macro would have defined is lost — but the alternative is a file
that cannot compile at all rather than one that compiles without those
definitions. A macro call *inside* a body is untouched and expands normally.

## Lifetimes and visibility

**Lifetimes are dropped in the lexer.** Crust has no borrow checker, so `'a`
carries nothing it could act on, and removing it there spares every later pass
from knowing about it. A comma that follows one inside `<'a, T>` goes with it,
so the remaining argument list stays well formed. Char literals are unaffected:
the giveaway is that a char literal closes after one character, so `'x'` is a
literal and `'a` is a lifetime.

This one mattered more than its size suggests. The item scanner blanks string
and char literals before looking for items, so a lifetime was being blanked as
an unterminated char literal — swallowing everything up to the next quote and
destroying the structure the scan exists to find. Files using lifetimes were
not *partly* understood; they were invisible.

**Visibility** is skipped wherever it can appear: `pub`, `pub(crate)`,
`pub(in some::path)`, and any combination with `unsafe` and `extern "C"`. The
modifier matcher runs against text whose string literals have been blanked, so
it cannot look for the literal spelling of `extern "C"` — real FFI code writes
`pub unsafe extern "C" fn` constantly, and matching the spelling missed all of
it.

## Paths, tuples and closures

**Qualified paths** work in type position. `a::b::C` is looked up first as the
flattened `a_b_C` -- which is what a Crust `mod::Type` definition lowers to --
and then as the last segment `C`, which is how a std path like
`alloc::boxed::Box<T>` finds the bundled core type. Neither is a guess: a
candidate is only accepted if the unit actually defines it, and otherwise the
flattened spelling is reported so the diagnostic names what was written.

**Tuples** are monomorphised on demand, exactly like slices and `Option`:

```rust
fn divmod(a: i32, b: i32) -> (i32, i32) { (a / b, a % b) }
```

```c
struct crust_tuple_int_int { int _0; int _1; };
```

Fields are positional (`t.0`, `t.1`), reusing the naming a tuple struct
already had, so field access needs no special case. `(T)` is a parenthesised
type rather than a one-element tuple, and `()` remains the unit type.

**Closures** are lowered by lifting, not by capture. A non-capturing closure
becomes an ordinary static function, and the expression's value is that
function -- which in C is a function pointer, so it can be bound, passed and
called:

```rust
let twice = |a: i32| a * 2;
```

```c
static int _crust_closure1(int a) { return (a * 2); }
typedef int (*crust_fn_int_int)(int);
...
crust_fn_int_int twice = _crust_closure1;
```

Each distinct signature gets a typedef, generated on demand like everything
else. **A closure that reads a local is rejected**, because Crust has no
environment to capture into and compiling it against the wrong binding would
be silent and wrong. Parameter types must be annotated; Crust does not infer
them.

### C keyword collisions

`double`, `int`, `register` and friends are ordinary identifiers in Rust and
keywords in C, and real Rust source uses them. Locals, parameters and loop
variables carrying such a name are renamed with a trailing underscore on the
way out, so the generated C parses.

## Macros

Two separate things share the `name!` syntax, and Crust handles them
differently.

**Built-in macros** are expanded directly. The printing family is the
interesting one: Rust's `{}` carries no type of its own — `Display` decides the
formatting at the call site — so Crust reads the conversion off the
*argument's inferred type*.

```rust
println!("n={} f={} s={}", n, f, s);   // i32, f64, &str
```

```c
printf("n=%d f=%g s=%s\n", n, f, s);
```

`{{` and `}}` become literal braces, a `%` in the original is escaped (it is
ordinary text in Rust and a conversion in C), and an explicit hint like `{:x}`
overrides the type-derived choice. `assert!`, `assert_eq!` and `assert_ne!`
lower to a test and `abort()`; `panic!`, `unreachable!`, `todo!` and
`unimplemented!` lower to `abort()` directly. `debug_assert*!` compiles out, as
in a release build. `cfg!` is always false, which is the honest answer when
nothing is configured in. A macro Crust does not know is named in the error
rather than skipped, because a silently dropped macro changes behaviour
invisibly.

**`macro_rules!`** definitions are kept as raw token slices and matched at the
invocation site. Literal tokens must match exactly; `$x:frag` captures a token
run, respecting nesting so the comma in `twice!(add(1, 2))` does not end the
capture. Rules are tried in order, so the usual overload-by-arity shape works:

```rust
macro_rules! pick {
    () => { 0 };
    ($a:expr) => { $a };
    ($a:expr, $b:expr) => { if $a > $b { $a } else { $b } };
}
```

`ident`, `literal` and `tt` capture a single token; `expr`, `ty`, `path`,
`block` and `stmt` capture a run. An `expr` stops at a top-level comma, since
one cannot contain it — without that a one-argument rule would greedily
swallow a two-argument call and the correct rule would never be tried.

**Not supported:** repetition (`$(...),*`), nested macro definitions, hygiene
(captured names are substituted literally, so a body that introduces a
binding can shadow one at the call site), procedural macros, and the macros
that need types Crust does not have — `format!` and `write!` (no `String`),
`vec!`, `asm!`, and `offset_of!`.

## Traits

`impl Trait for Type` lowers to exactly what an inherent `impl` does: free
functions named `Type_method`. **Dispatch is static.** A trait call is an
ordinary direct call -- no vtable, no function pointer, no indirection -- so it
inlines like any other call and C can invoke it with no shim:

```rust
trait Metric { fn bytes(&self) -> i64; }
impl Metric for Disk { fn bytes(&self) -> i64 { self.sectors * 512 } }
```

```c
long Disk_bytes(Disk *self) { return (self->sectors * 512); }
```

A trait declares nothing at C level; it exists only through its impls.

**Default methods** are stored as tokens and generated per implementing type
with `Self` bound to it -- the same substitution a generic instantiation does.
An impl that overrides a default simply wins. **Supertraits** are followed, so
`trait Device: Metric` gives `Device`'s impls the defaults of both.

**Trait bounds** (`fn f<T: Metric>(x: T)`) compose with monomorphisation: the
bound itself is not checked (Crust has no trait solver), but each instantiation
resolves its method calls against the concrete type, so `f<Disk>` calls
`Disk_bytes` directly. Where a bound would have caught a mistake, the generated
C fails to compile instead.

Trait paths are flattened, so `impl fmt::Debug for X` -- which Redox writes
constantly -- gives `X_fmt`.

**Not supported:** `dyn Trait` and trait objects (which would need the vtable
this design deliberately avoids), associated types and associated consts
(parsed and skipped), generic traits with their own parameters, blanket impls,
and operator-trait sugar -- `a + b` never becomes `Add::add`.

### Cost

Static dispatch is the reason this is worth doing rather than boxing. On a
20-million-iteration loop through a trait method, the generated C contains
**zero** indirect calls, and the trait call is indistinguishable from a direct
one. What is *not* yet competitive is the optimizer behind it: the same
generated C, built by gcc -O2, runs about twice as fast as ShivyCX's own
backend (0.094s vs 0.195s on that loop). So the lowering is right and the
dispatch is free; the remaining gap is codegen quality, not the trait design.
A comparison against rustc has not been made -- it could not be installed in
the environment this was developed in -- so no claim is made about it.

## The bundled minimal core

Monomorphisation needs a template, so a generic with no source in the unit
cannot be instantiated. Real Rust reaches constantly for `Vec<T>` and `Box<T>`,
which live in `alloc`/`core` and are written in a dialect far outside this
subset — traits, intrinsics, allocator plumbing. Rather than try to compile
the real ones, Crust ships small equivalents **written in the Crust subset
itself** (`shivyc/crust_core/core.rs`) and seeds every unit with them.

| type | what it is |
|---|---|
| `Vec<T>` | growable array: `new`, `with_capacity`, `push`, `pop`, `get`, `set`, `len`, `capacity`, `last`, `clear`, `as_ptr`, `free_buf` |
| `Box<T>` | one heap value: `new`, `get`, `set`, `as_ptr`, `free_box` |
| `Cell<T>` | a named holder for a value |
| `PhantomData<T>` | zero-sized marker |

`Vec<i32>` lowers to exactly the three words the real one uses, so C reads it
with no conversion:

```c
struct Vec_int { int *ptr; unsigned long len; unsigned long cap; };
```

Seeding is free: templates are tokens in a table, and instantiation is
demand-driven, so a unit that never mentions `Vec<T>` emits nothing — not even
the allocation prototypes. **A local definition always wins**: a unit that
brings its own `Vec` displaces the bundled template and its `impl`s entirely.

`size_of::<T>()` is provided as an intrinsic, lowering to C `sizeof`. It is
the one thing a monomorphised container cannot be written without.

**This is a reimplementation, not the standard library.** The shape and the
common methods match, so ordinary code reads the same. What is missing is
missing rather than faked: no iterators, no traits, no `Drop` (buffers are
released by an explicit `free_buf`/`free_box`), no bounds checking, and
nothing thread-safe — `Arc`, `Mutex` and `RwLock` are deliberately *not*
included, because without atomics or threads they could only be lies.

## Finding crashes: `tools/crustfuzz.py`

A compiler has two acceptable outcomes for any input: compile it, or reject it
with a diagnostic. A Python traceback is never one of them.

The `unexpected register size` bug found while implementing `#[derive(Clone)]`
was reachable by any function returning a 3-byte struct, and had been for as
long as struct returns existed — it was found by luck. There are 57
`raise NotImplementedError` sites in the backend and no way to tell by reading
them which are reachable, so `crustfuzz` answers the question by construction:
it generates small programs across the axes that tend to matter (type widths,
struct layouts, operators, casts, calling conventions) and classifies what
comes back.

```sh
python3 tools/crustfuzz.py                # all families
python3 tools/crustfuzz.py --family cast
```

An input that produces a *diagnostic* is fine — this is not looking for
unsupported constructs, only for the ones that fail in the wrong way.

762 probes currently: 760 compile, 2 report, 0 crash. The two reports are
genuine C errors (a bitfield wider than its type).

### What it found, and how it was fixed

Passing a struct of 3, 5, 6 or 7 bytes **by value** crashed the same way
returning one did — a second family, on the other side of the call.

The fix is asymmetric, because the two directions are:

* **Returning** reads a slot into RAX, so the read is widened to a whole
  eightbyte. Safe: the extra bytes are padding the caller ignores.
* **Passing, caller side** reads the struct out of its slot into an argument
  register — also a read, so also widened. Stack slots are padded to eight
  bytes so the read stays inside the slot rather than picking up the
  neighbouring local.
* **Passing, callee side** does the opposite: it *writes* an argument register
  into the parameter's home. Widening a write clobbers whatever follows, so
  the store is **split into power-of-two chunks** instead — 3 becomes 2+1, 7
  becomes 4+2+1 — shifting the value down between them so each chunk writes
  exactly the bytes it should.

Widening the store was the obvious move and it was wrong: it compiled, and
then the program hung on a corrupted frame. A build that fails is visible; a
program that silently corrupts its own stack is not, which is why the chunked
store is worth the extra instructions.

Verified field-by-field against gcc on the same inputs.

Code-generation errors are now collected like any other diagnostic rather than
escaping as exceptions, so a limitation found this late still reads as a
compiler message.

## Compiling real Rust: `tools/crustos.py`

`tools/crustos.py` walks a Rust source tree, runs every `.rs` file through the
Crust front end, and reports what happened -- then compiles the ones that
work. It ignores Cargo completely, which is the point: no toolchain pin, no
build scripts, no proc macros, no package manager.

```sh
python3 tools/crustos.py survey ~/redox-kernel --blockers --files
python3 tools/crustos.py build  ~/redox-kernel -o build/crustos
python3 tools/crustos.py stage  ~/redox-kernel -o /tmp/mini-redox
make crustos_survey REDOX=~/redox-kernel
```

The survey is deliberately pessimistic about what counts as success. Crust
passes a file through unchanged when it finds no Rust items in it, which is
right for a C file but would score a Rust file Crust understood *nothing* of
as a pass. So a file only counts when Crust actually lowered at least one
item, and files it recognized nothing in are reported separately as `empty`.

Where things stand on Redox (kernel + relibc + ion, 615 files):

| outcome | files | |
|---|---|---|
| translated (most of the file lowered) | 32 | 5.2% |
| partial (some items lowered) | 35 | 5.7% |
| failed (items found, translation errored) | 459 | 74.6% |
| empty (no Rust items recognized) | 89 | 14.5% |
| **compile to an object** (`survey --verify`) | **93** | **15.1%** |

6355 top-level Rust items parse. With generics landed, the ranked blockers
behind the 458 remaining failures are paths in type position (7.9%), `impl
Trait for Type` (7.0%), trait method resolution (6.1%), closures and tuples
(5.2%), data-carrying enum variants (4.6%), and macros (4.4%).

Two honest caveats the numbers make plain. First, most failing files are
blocked by *several* features at once, so landing any one of them moves the
headline count less than its own share suggests: `unsafe` blocks went from 44
files to 3 while total failures fell only from 471 to 469, and generics went
from 142 files to 21 while total failures fell from 469 to 458. The next
blocker in the same file was already waiting.

Traits removed `impl Trait for Type` from the blocker list entirely (it was
7.0%), and pushed `empty` down from 99 files to 92 as trait-only files started
being recognized. But total failures went *up*, 457 to 463: files that used to
stop at the first `impl ... for` now parse further and reach a later blocker.
That is progress the headline number actively hides, and it is worth naming.

Second, and more instructive: adding the bundled core (above) moved these
numbers almost not at all — 58 files to 59. It resolves `Vec` and `Box`
outright, but the files using them are blocked by traits and macros as well,
and the *remaining* unresolved generics are mostly Redox's own types
(`LockToken`, `PageFlags`, `RawCell`), defined in files that fail for those
same other reasons. So the core's real value is that it makes OS-shaped code
*writable* in Crust — see `examples/crust/kernel.rs` — not that it unlocks
upstream Redox. Compiling Redox itself needs traits, and after that macros;
there is no shortcut left that avoids them.

## Not yet supported

Traits, user-defined generics (`Option` and `Result` are the only
monomorphised types), closures, modules, `Vec`, the iterator protocol proper
(`.map`, `.filter`, `.zip`, chained adaptors), `From`/`Into` conversions,
lifetimes, and the borrow checker. Enums cannot carry data, and `match` has no
bindings, guards or range patterns. Slices carry no bounds checking. A repeat
initializer whose length is not a literal or `const` is rejected, since C has
no repeat syntax to lower it to. Paths (`a::b`) are flattened to `a_b`. These
are the natural next increments — the parser is a few hundred lines of legible
Python, in keeping with the rest of the front end.

## Examples

- `examples/crust/mixed.c` — Rust `gcd`, `classify`, `sum_to` and `dot`
  alongside a C `main` that calls all four.
- `examples/crust/fib.rs` — an all-Rust file with recursion.
- `examples/crust/vec2.rs` and `examples/crust/shapes.c` — a Rust struct with
  an `impl` block, included into C, with C calling the methods and a Rust
  function in the C file using the struct.
- `examples/crust/tokens.rs` — `enum`, exhaustiveness-checked `match`, `const`,
  a tuple struct, array literals and `if` as an expression.
- `examples/crust/stats.c` — slices, `&str` and an associated constant, with C
  building a slice itself and calling the methods.
- `examples/crust/lookup.rs` — `Option<T>` for fallible lookup, with `if let`,
  `while let` and `unwrap_or`.
- `examples/crust/parse.rs` — `Result<T, E>` with an error enum, the `?`
  operator, `unwrap_err` and `.ok()`.
- `examples/crust/iter.rs` — `for x in xs` over slices and arrays, a unit
  struct with an `impl` block, and the `[v; N]` repeat initializer.
- `examples/crust/generic.rs` — a generic struct with an `impl` block and
  generic functions, instantiated at two types each.
- `examples/crust/kernel.rs` — a mini-kernel sketch: tasks, an enum state
  machine, a generic ring buffer over the bundled `Vec<T>`, and `Box<T>`.
- `examples/crust/traits.rs` — a trait with a default method, a supertrait,
  two impls and a bounded generic, all statically dispatched.
- `examples/crust/macros.rs` — the printing and assertion macros, and
  `macro_rules!` with several rules chosen by arity.
- `examples/crust/derive.rs` — the derivable traits, and a hand-written
  `impl` overriding a derived method.
- `examples/crust/enums.rs` — data-carrying enum variants in both forms, and
  `match` arms that destructure them.
- `examples/crust/tail.rs` — tuple returns, a qualified path to the bundled
  `Box`, non-capturing closures, and identifiers that are C keywords.
- `examples/crust/histogram.py` and `examples/crust/polyglot.c` — C, Rust and
  rpython in one translation unit, each calling the other two.
- `examples/crust/tally.py` and `examples/crust/tally.c` — an rpython module
  that uses lists and strings, so the py2c runtime is linked in automatically.

## Tests

`tests/test_crust.py` covers the translation layer (type mapping, tail
returns, line-number preservation, struct and method lowering, false-match
rejection) and end-to-end compilation and execution across the C/Rust
boundary in both directions.

```sh
python3 -m pytest tests/test_crust.py -q
```

Two make targets wrap this up with the examples:

```sh
make test_crust        # unit tests + every example, output and exit status
make test_fast_crust   # a subset covering all three front ends, ~1.7s warm
make clean_crust       # drop the /tmp transpile and runtime-object caches
```

Both drive `tools/crust_examples.py`, which holds the expected stdout and exit
status for each example, so a silent miscompile that still exits 0 is caught
rather than passing.
