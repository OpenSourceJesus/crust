# Crust — Rust syntax in the ShivyCX front end

Crust lets one translation unit hold C functions and Rust functions side by
side, with no FFI boundary between them. A Rust function calls a C function
(and vice versa) as a direct call: same IL, same register allocator, same
whole-program passes, full inlining and interprocedural visibility.

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
| Items | `fn`, `struct`, `impl`, with optional `pub`, `unsafe`, `extern "C"`; `#[...]` attributes are skipped |
| Types | `i8 i16 i32 i64 isize`, `u8 u16 u32 u64 usize`, `f32 f64`, `bool`, `char`, `()` |
| Pointers | `*const T`, `*mut T`, `&T`, `&mut T` (all lower to `T *`) |
| Arrays | `[T; N]` |
| Statements | `let` (with `mut` and optional annotation), `return`, `if`/`else if`/`else`, `while`, `loop`, `for x in a..b` and `a..=b`, `break`, `continue`, blocks |
| Expressions | literals, calls, indexing, field access, unary and binary operators, compound assignment, `as` casts |
| Tail expressions | a trailing expression in a function body becomes its return value |
| Structs | field declarations, struct literals `P { x: 1 }`, field access with auto-deref, nested struct fields |
| Methods | `&self`, `&mut self`, `self`, associated functions, `Self`, method calls `p.m()`, path calls `P::m()` |

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

`enum`, traits, generics, `match`, closures, modules, slices and `Vec`,
`Option`/`Result`, lifetimes, and the borrow checker. Tuple structs and unit
structs are not recognized. Paths (`a::b`) are flattened to `a_b`. These are
the natural next increments — the parser is a few hundred lines of legible
Python, in keeping with the rest of the front end.

## Examples

- `examples/crust/mixed.c` — Rust `gcd`, `classify`, `sum_to` and `dot`
  alongside a C `main` that calls all four.
- `examples/crust/fib.rs` — an all-Rust file with recursion.
- `examples/crust/vec2.rs` and `examples/crust/shapes.c` — a Rust struct with
  an `impl` block, included into C, with C calling the methods and a Rust
  function in the C file using the struct.

## Tests

`tests/test_crust.py` covers the translation layer (type mapping, tail
returns, line-number preservation, struct and method lowering, false-match
rejection) and end-to-end compilation and execution across the C/Rust
boundary in both directions.

```sh
python3 -m pytest tests/test_crust.py -q
```
