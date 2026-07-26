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
| Types | `i8 i16 i32 i64 isize`, `u8 u16 u32 u64 usize`, `f32 f64`, `bool`, `char`, `()`, `&str` |
| Pointers | `*const T`, `*mut T`, `&T`, `&mut T` (all lower to `T *`) |
| Arrays | `[T; N]`, and slices `&[T]` / `&mut [T]` |
| Option | `Option<T>`, `Some(x)`, `None`, `is_some`, `is_none`, `unwrap`, `unwrap_or`, `if let`, `while let` |
| Result | `Result<T, E>`, `Ok(x)`, `Err(e)`, `is_ok`, `is_err`, `unwrap`, `unwrap_err`, `unwrap_or`, `ok`, and the `?` operator |
| Statements | `let` (with `mut` and optional annotation), `return`, `if`/`else if`/`else`, `while`, `loop`, `for x in a..b` and `a..=b`, `match`, `break`, `continue`, local `const`, blocks |
| Expressions | literals, array literals `[a, b, c]` and `[0; N]`, calls, indexing, field access, unary and binary operators, compound assignment, `as` casts, `if`/`else` as an expression |
| Tail expressions | a trailing expression in a function body becomes its return value |
| Structs | field declarations, struct literals `P { x: 1 }`, field access with auto-deref, nested struct fields |
| Methods | `&self`, `&mut self`, `self`, associated functions, associated `const`s, `Self`, method calls `p.m()`, path calls `P::m()` |
| Enums | C-like variants with optional discriminants, `E::V` paths, exhaustiveness-checked `match`, `impl` blocks |
| Tuple structs | `struct P(T, U);`, construction `P(a, b)`, positional access `p.0` |

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

## Not yet supported

Traits, user-defined generics (`Option` and `Result` are the only
monomorphised types), closures, modules, `Vec`, iterators (`for x in slice` —
use `for i in 0..xs.len()`), `From`/`Into` conversions, lifetimes, and the
borrow checker. Enums cannot carry data, `match` has no bindings, guards or range
patterns, and unit structs are not recognized. Slices carry no bounds
checking.
Non-zero array repeat initializers (`[7; N]`) are rejected because C has no
equivalent syntax. Paths (`a::b`) are flattened to `a_b`. These are the
natural next increments — the parser is a few hundred lines of legible
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

## Tests

`tests/test_crust.py` covers the translation layer (type mapping, tail
returns, line-number preservation, struct and method lowering, false-match
rejection) and end-to-end compilation and execution across the C/Rust
boundary in both directions.

```sh
python3 -m pytest tests/test_crust.py -q
```
