# CSRUST — C# subset for Crust

Approach **D** from [issue #25](https://github.com/brentharts/crust/issues/25):
rewrite a C# subset into the existing C++ subset (`tools/cs2cpp.py`), then
run `tools/cpprust.py` unchanged. CLI: `tools/csrust.py`. Includes of
`.cs` files go through the same out-of-process protocol as `.cpp`
(`shivyc/preproc.py`).

Line numbers are preserved across the rewrite so a diagnostic in the
generated C++ still points at the user's `.cs` line.

## What a `class` means (§1)

Crust has no GC. Default `class` is **single ownership**: the lowered
object is a value with one owner; destruction is at scope exit.

```csharp
class Node { }              // single owner
[Shared] class Node { }     // shared_ptr; assignment aliases; cycles may leak
```

Pinned in `tests/test_csrust.py` (`TestSemantics`).

## Phase 1 surface

**In** (see `tools/cs2cpp.py` / `tests/test_csrust.py`):

| Area | Lowering |
|------|----------|
| `class` / `struct` / `interface` | C++ types; interface → pure virtual |
| `enum Kind : byte { A, B }`, `Kind.A` | `enum Kind_values { Kind_A, .. }; typedef unsigned char Kind;` (§3) |
| fields, methods, constructors, `~T` | cpprust method shape `T_method(T *this, …)` |
| single inheritance, `virtual` / `abstract` | vtables |
| `class Box<T>` | `template<typename T> class Box` → monomorphise |
| `List<T>` / `Dictionary<K,V>` | `std::vector` / `std::map` |
| `T[]`, jagged `T[][]`, `.Length` | `vector`, `.size()` |
| `new T[n]` (primitive or enum `T`) | zero-filled `vector` of length `n` |
| `new T { A = 1 }` in a declaration | `new T()` then `x.A = 1;` |
| `new T()` on a plain struct | declared, then zeroed byte by byte |
| `[StructLayout(Sequential/Auto, Pack, Size)]` | checked; a layout-changing `Pack` → `_Pragma("pack(..)")` pair (§2) |
| `MemoryMarshal` over an unmanaged struct | byte-copy helpers (§2) |
| `var`, `foreach`, `this.`, `null` | `auto`, range-`for`, `this->`, `NULL` |
| auto-properties `{ get; set; }` | field + `get_` / `set_` |
| `delegate` | `typedef` function pointer |
| `x => …` lambdas | C++ lambdas |
| `throw` / `catch` | `raise` / `except` (checked model) |
| `using X = Y;` | kept; `using System;` dropped |
| `--emit-decls` | same class digest as C++ (CPPRPY.md) |

**Out** (refused in C# terms before conversion): `async`/`await`, LINQ,
`yield`, `dynamic`, `event`, multidimensional arrays, `ref`/`out`/`in`
parameters, `$"…"`, file-scoped namespaces, `??` / `?.`, `char`, `lock`,
`decimal`, `partial`, `goto`, `params`, `stackalloc`, `checked`/`unchecked`,
top-level statements, `Span<T>` / `ReadOnlySpan<T>`, array initializers
(`new T[] { … }`), collection initializers, object initializers outside a
declaration, `new T[n]` for a non-primitive `T`, `LayoutKind.Explicit`,
enum methods (`ToString`, `Parse`, `HasFlag` …).

Top-level statements are refused rather than lowered because C# requires
them *before* every type declaration (CS8803) and C needs them after: a
function body cannot use a struct declared below it. Moving them would
break the line-number invariant above.

## Plain data and `MemoryMarshal` (§2)

A C# *unmanaged* type — primitives, enums, and structs made only of those,
all the way down — is its bytes. So is a C struct of the same fields, laid
out by the same natural-alignment rule .NET uses for
`LayoutKind.Sequential`. Serialising one is therefore a byte copy, and the
bytes are the ones .NET produces on the same machine
(`TestBlittableSerialization` checks them byte by byte, under gcc *and*
shivyc).

```csharp
[StructLayout(LayoutKind.Sequential, Pack = 1)]
public struct PacketData { public int Id; public float Value; }

PacketData packet = new PacketData { Id = 101, Value = 3.14f };
byte[] raw = MemoryMarshal.AsBytes(MemoryMarshal.CreateSpan(ref packet, 1)).ToArray();
PacketData back = MemoryMarshal.Read<PacketData>(raw);
```

The recognised forms, each read as a whole expression:

| C# | lowers to |
|----|-----------|
| `MemoryMarshal.AsBytes(MemoryMarshal.CreateSpan(ref x, 1)).ToArray()` | `_cs_blit_bytes_T(&x)` → `vector<unsigned char>` |
| `MemoryMarshal.Read<T>(bytes)` | `_cs_blit_read_T(bytes)` → `T` |
| `MemoryMarshal.Write(bytes, ref x)` (or `in x`, or `x`) | `_cs_blit_write_T(bytes, &x)` |
| `Marshal.SizeOf<T>()`, `Unsafe.SizeOf<T>()` | `((int)sizeof(T))` |

The helpers are generated per struct, directly after the struct's outermost
enclosing type, on the same line; `abort` and the zeroing loop go at the
top of the file, outside any namespace.

Rules, each refused in C# terms when broken:

- **`T` must be unmanaged.** A `string`, array or class field is a
  reference, and copying it as bytes would duplicate an owner. A struct
  implementing an interface is refused too: its lowered form carries a
  vtable pointer the C# one lacks. The diagnostic names the offending field.
- **No spans.** `Span<T>` is a pointer and a length into storage something
  else owns, which single ownership rules out. `AsBytes(CreateSpan(..))` is
  accepted only when `.ToArray()` copies it out on the spot, and only with a
  count of `1`.
- **`Read<T>` needs a declarable `T`**: no constructors, or a
  parameterless one. The subset cannot yet declare a struct whose only
  constructors take arguments.
- **A short buffer aborts.** .NET throws `ArgumentOutOfRangeException`;
  unhandled, that ends the process. The checked `except` model would oblige
  every caller to handle it, which C# callers do not do.

**Zeroing.** C# `new T()` on a struct is all zeroes, and so is every field
an object initializer does not mention. A plain struct (no constructor,
unmanaged fields) is therefore declared and then zeroed, not left
uninitialised — otherwise stack garbage ends up in the serialised bytes.
Not `T x = {0};`: cpprust refuses a brace list as the source of a struct
that contains a struct.

**`Pack`.** cs2cpp computes the struct's layout with and without `Pack`.
Equal — the usual case, fields already in size order — means `Pack` changes
nothing, and it is dropped. Different emits a pragma pair around the
struct — the push where the attribute was, the pop after the `};`. The
C++ that cs2cpp hands on, for a `byte Tag; int Id;` struct:

```cpp
_Pragma("pack(push, 1)")
struct Q { unsigned char Tag; int Id; }; _Pragma("pack(pop)")
```

The `_Pragma` operator, not `#pragma`: a directive needs a line of its own,
and this file never adds a line. gcc honours it, and so does shivyc (see
*Struct packing* in [PREPROCESSOR.md](PREPROCESSOR.md)) — which is what
makes emitting it sound. Before shivyc read packing, the same source had one
layout under gcc and another under shivyc, so a layout-changing `Pack` was
refused instead. Each `Pack` is recorded before any layout is computed, so
a natural struct containing a packed one is laid out correctly. `Size` is
accepted when it equals the (packed) field size.

`Pack` is still refused, in C# terms, in two places. On a struct nested in
a class: the C++ half moves nested types out of their class, which would
leave the pragmas behind. And on a struct with a field that is not plain
data: packing misaligns an owner (a `string`, an array) that the generated
code works through by address.

Known gaps: pointer-sized `nint`/`nuint` fields are accepted, but not under
`Pack`, whose check needs a fixed size. A struct used as a field must be
declared before the struct using it — C# allows either order, C does not,
and the pipeline does not reorder definitions (packed or not).

## Enums (§3)

C puts every enum member in one namespace per file and leaves an enum's
size and signedness to the compiler; C# scopes members to their type and
fixes the size. So each member is renamed after its type, and the type is a
typedef of its C# underlying type:

```csharp
public enum Kind : byte { A, B = A + 4 }     // C#
```
```c
enum Kind_values { Kind_A, Kind_B = Kind_A + 4 }; typedef unsigned char Kind;
```

`Kind.A` becomes `Kind_A` at every use, including qualified forms
(`Outer.Kind.A`, `Net.Kind.A`) and siblings named bare in an initializer.
The typedef is load-bearing for plain data: a `: byte` enum field is one
byte, as in C#, where a C enum would be four and move every field after it.
`[Flags]` enums combine with `|` and `&` as integers.

An enum nested in a class is hoisted in front of the outermost type that
contains it — cpprust has no nested enum — collapsed onto that line, with
its own lines left blank, so every line keeps its number.

Refused, in C# terms: two enums with the same name in one file (both would
declare `Kind_…`); enum methods (`ToString`, `Parse`, `HasFlag`, …), which
need the member names in the binary; a value outside the `int` range; a
non-integral underlying type; and a field, parameter or local named the
same as an enum type (`public Color Color;`) — cpprust resolves a typedef by
substituting its name wherever it appears as a word. The property form
`public Color Color { get; set; }` works, since its storage is renamed.

## Layout

| file | role |
|------|------|
| `tools/cs2cpp.py` | refusals + C# → C++ subset (`translate`) |
| `tools/csrust.py` | CLI; `cs2cpp.translate` then `cpprust.translate` |
| `tests/test_csrust.py` | semantics, lowering, Shared, generics, except, digest, plain data, enums |

A Unity *scene* plus these scripts is a different job: see
[UNITY_PACK.md](UNITY_PACK.md). That packer emits `engine.c` / `data.c`
with packed instance arrays (`_Coin_inst_array[i]`) instead of Unity
object headers.

## Deliberately later

Extracting `cpprust_core.py` (approach **C**) — issue §5 milestone 9 —
waits until shared seams are known from real use. Full four-language TU
demo (C + Rust + C++ + C# in one file) builds on the digest C# already
emits; C++ cannot yet *inherit from* a foreign digest entry (include the
header instead).
