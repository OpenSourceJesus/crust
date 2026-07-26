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
| translated (most of the file lowered) | 27 | 4.4% |
| partial (some items lowered) | 31 | 5.0% |
| failed (items found, translation errored) | 458 | 74.5% |
| empty (no Rust items recognized) | 99 | 16.1% |

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

Second, and more fundamental: the single largest remaining message is now
`no definition for generic type X in this unit` (85 files). Monomorphisation
needs a template, and Redox's generics are overwhelmingly `Vec`, `Box`,
`BTreeMap` and friends — defined in `std`/`core`, which Crust has never seen.
That is not a parser gap that more syntax support will close. Compiling real
Redox needs a story for the standard library, whether that is a Crust-side
minimal `core`, or teaching crustos to follow crate sources.

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
