# Crust — Rust syntax in the ShivyCX front end

Crust lets one translation unit hold C functions and Rust functions side by
side, with no FFI boundary between them. A Rust function calls a C function
(and vice versa) as a direct call: same IL, same register allocator, same
whole-program passes, full inlining and interprocedural visibility.

```sh
python3 -m shivyc.main mixed.c -o mixed     # C file containing Rust functions
python3 -m shivyc.main prog.rs  -o prog     # all-Rust file
```

## How it works

Per the architectural philosophy, Crust enforces **function-level syntax
isolation**: syntax is chosen per function, never within one. `shivyc/crust.py`
scans the source for top-level `fn` items (brace depth 0, with comments and
string literals blanked so they can't create false matches), parses each with a
recursive-descent parser for the Rust subset, and splices equivalent C text
back in place. The C text outside those items passes through byte-for-byte.

Everything downstream is untouched — the pass runs immediately before
`extensions.preprocess_extensions` in `process_c_file`, so the C lexer never
sees Rust.

Two details make the output behave like ordinary source:

- **Line numbers are preserved.** The emitter syncs to the line of each source
  construct and pads each item to its original line count, so diagnostics from
  the parser, the memory-safety pass, or the register allocator point at the
  Rust line the user wrote.
- **Forward declarations are emitted** for every Rust function, on a single
  physical line prefixed to the file (so no line shifts). Definition order
  doesn't matter, and C code can call Rust functions declared later.

## Supported subset

| Area | Supported |
|---|---|
| Items | `fn`, with optional `pub`, `unsafe`, `extern "C"` |
| Types | `i8 i16 i32 i64 isize`, `u8 u16 u32 u64 usize`, `f32 f64`, `bool`, `char`, `()` |
| Pointers | `*const T`, `*mut T`, `&T`, `&mut T` (all lower to `T *`) |
| Arrays | `[T; N]` |
| Statements | `let` (with `mut` and optional annotation), `return`, `if`/`else if`/`else`, `while`, `loop`, `for x in a..b` and `a..=b`, `break`, `continue`, blocks |
| Expressions | literals, calls, indexing, field access, unary and binary operators, compound assignment, `as` casts |
| Tail expressions | a trailing expression in a function body becomes its return value |

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

## Not yet supported

`struct`/`enum`/`impl` items, traits, generics, `match`, method call syntax,
closures, modules, slices and `Vec`, `Option`/`Result`, lifetimes, and the
borrow checker. Paths (`a::b`) are flattened to `a_b`. These are the natural
next increments — the parser is a few hundred lines of legible Python, in
keeping with the rest of the front end.

## Example

`examples/crust/mixed.c` — Rust `gcd`, `classify`, `sum_to` and `dot`
alongside a C `main` that calls all four. `examples/crust/fib.rs` — an
all-Rust file with recursion.

## Tests

`tests/test_crust.py` covers the translation layer (type mapping, tail
returns, line-number preservation, false-match rejection) and end-to-end
compilation and execution of both directions of the C/Rust call boundary.

```sh
python3 -m pytest tests/test_crust.py -q
```
