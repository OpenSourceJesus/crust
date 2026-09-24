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
| `xs.Add(x)`, `xs.Count`, `Insert`, `RemoveAt`, `Remove`, `Clear`, `Contains`, `IndexOf` | vector members and helpers (§5) |
| `Type.Method(..)` for a `static` method | `Type::Method(..)` |
| a field named after a type (`public In In;`) | `this.In` where C# means the field (§5) |
| `T[]`, jagged `T[][]`, `.Length` | `vector`, `.size()` |
| `new T[n]` (primitive or enum `T`) | zero-filled `vector` of length `n` |
| `new T { A = 1 }` in a declaration | `new T()` then `x.A = 1;` |
| `new T()` on a plain struct or class, declared or assigned | zeroed byte by byte |
| `[StructLayout(Sequential/Auto, Pack, Size)]` | checked; a layout-changing `Pack` → `_Pragma("pack(..)")` pair (§2) |
| `MemoryMarshal` over an unmanaged struct | byte-copy helpers (§2) |
| `var`, `foreach`, `this.`, `null` | the written type (`var x = new T(..)`) or `auto`, range-`for`, `this->`, `NULL` |
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

Not refused yet, and failing or misbehaving instead — gaps, not design:
a list of lists (`List<List<int>>`) cannot be declared, because the C++
half cannot copy a vector of vectors; a class *with* a constructor leaves
the fields it does not assign uninitialised, where C# zeroes them (a class
or struct with no constructor and plain fields is zeroed); and an identity
cast to a struct type, `(In)x`, is not C.

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

Known gap: pointer-sized `nint`/`nuint` fields are accepted, but not under
`Pack`, whose check needs a fixed size. A packed struct may be declared
after the struct that holds it; §4 moves its definition and keeps its
`Pack` when it does.

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

## Declaration order (§4)

C# lets a type hold one declared below it. C needs a by-value field's
struct complete first, and a class here is owned, so stored by value — a
class holding a later class has the same need as a struct holding a later
struct. So does a `List<Item>` field: `vector_Item` is itself held back
until `Item` is complete.

`csrust` passes `any_order=True` to the C++ half, which moves the struct
definitions a holder needs to a slot just above it, dependencies first
(`cpprust._order_plan`):

```csharp
public struct Q { public byte A; public In inner; }   // In is below
public struct In { public int V; }
```

Method bodies count too. They are emitted with their class, so `Program`
written first — the usual C# file — declaring a local, taking a parameter
or returning a value of a later type needs that type complete just as a
field does. Declarations are what count (`Row r`, `Row Make()`,
`sizeof(Row)`, `Row(..)`), not every mention: a static call `Row.Make()`
needs only a prototype.

Only the struct definition moves. Method bodies stay where they were
written, so everything they read is complete exactly as before, and the
slot carries line anchors so diagnostics on either side still name the
right line. A moved struct keeps the `Pack` it was written under and does
not take the holder's — its `_Pragma` pair stays behind at its original
position, so the move re-applies the packing in force there. Code that
declares everything in order is untouched: nothing to move, byte-identical
output.

Refused, in C# terms:

- a later type with a base class or an interface, held as a field or
  declared in a method — its lowered struct is tied to its vtables where it
  is declared. Declare it above the holder, or mark it `[Shared]`;
- a cycle of fields (`A` holds `B` holds `A`). Ordinary C#, because fields
  are references; here each would contain the other. Marking one `[Shared]`
  makes a field of that type a reference again, and breaks it.

## Lists, static calls, and names shared with types (§5)

**`List<T>` members** are lowered only where the receiver is *known* to be
a `List`: `Add` and `Count` are ordinary names, and a class of the
author's may have its own. The receiver is resolved the way a reader
would — a local or parameter declared above it in the method, a `foreach`
variable, else a field of the class; then field by field, and element by
element for `[..]` — and anything unresolved is left alone.

| C# | lowers to |
|----|-----------|
| `xs.Add(x)` | `xs.push_back(x)`; a new object or call result is named, then moved in |
| `xs.Count` | `xs.size()` |
| `xs.Insert(i, x)`, `xs.RemoveAt(i)` | `insert` / `erase`, the index checked |
| `xs.Clear()` | `xs.clear()` |
| `xs.Contains(x)`, `xs.IndexOf(x)`, `xs.Remove(x)` | a per-element-type helper using `==` |

An index outside the list aborts, as the unhandled
`ArgumentOutOfRangeException` does; the vector's own `insert` would clamp
it and `erase` ignore it. `Contains`/`IndexOf`/`Remove` are for primitive
and enum elements, where C#'s `Equals` is `==`; for a class they are
refused, since that `Equals` is not here. `Sort` and the rest of `List`,
and LINQ's `Count()`, are refused naming what is supported.

A `List` behind an auto-property (`public List<int> Items { get; set; }`)
is reached through its storage, `_Items`, for every read and member call:
the getter returns the list by value, and `b.Items.Add(1)` or
`b.Items[0] = 5` through it would change a copy. In C# the getter returns
the same list, so the storage is what that means. Assigning the property
still goes through its setter.

`var` is spelled out where the type is written on the right — `var xs =
new List<int>()`, `var a = new int[n]` — and a `foreach` variable over a
member chain gets its element type, because the C++ half deduces a loop
variable only from a local's declared type. `foreach` over a list
declared with `var` did not translate before this.

**Static calls** through a type, `Type.Method(..)`, become
`Type::Method(..)` when `Method` is declared `static` in that type.

**A member named after a type** — `public In In;`, most often its own —
is resolved per use, by C#'s rule: in `In.X`, an instance member means the
field and a static one the type. Inside the owning class's methods, every
use that means the field is given an explicit `this.`; `new In(..)`,
`In x`, `(In)x`, `In[]`, generic arguments, `typeof`/`sizeof`/`nameof` and
`In.Static` are the type and are left alone. A local or parameter of the
same name shadows the field, as in C#. (The C that results, `struct Q { In
In; }` followed by `In x;`, is valid C; shivyc's parser had to be fixed to
accept it — see PREPROCESSOR.md.)

## Object models and `lower_body` (§6)

cs2cpp's C# families are not only for whole files. `tools/unity_pack.py`
lowers Unity script bodies with them too, under a different object model:
there a class is an index into a per-class instance array, and a missing
object is `-1`. `cs2cpp.ObjectModel` holds everything the two differ on;
`OWNED` is csrust's, `packed_model(has_objects, byte_arrays)` unity_pack's.

```python
cs2cpp.lower_body(text, model)         # float literals, null, booleans, `this`
cs2cpp.lower_local_types(text, model)  # `string` locals
cs2cpp.lower_byte_arrays(text, model)  # `byte[]`
cs2cpp.lower_string_concat(text, model, string_idents)  # "s" + x
cs2cpp.lower_list_types(text, model)   # List<T>; lower_list_members_named
cs2cpp.lower_map_types(text, model)    # Dictionary<K,V>; lower_map_members_named
cs2cpp.lower_packed_fields(text, owner, members, statics, handles, model)
cs2cpp.lower_packed_collections(text, owner, others, model)  # PackedClass each
cs2cpp.lower_bindings(text, table)     # a library's API: Binding(path, c, form)
cs2cpp.residual_csharp(text, model, known_types, value_ctors)  # C# still left
cs2cpp.code_sub(pattern, repl, text)   # re.sub over code only
```

A `Binding` table is how a caller says what its library is — unity_pack's
UnityEngine tables are the first; `System.Math` for csrust would be the
same shape — and `lower_bindings` applies it outside strings and comments
with one set of boundaries.

`LIST_METHODS` is the one table of collection method spellings, used by
csrust's type-resolved `List` lowering and by the named lowering alike.
A replacement that copies an operand reads it from the text, not the
blanked copy it matched on (`_sub_orig`): a string key used to come out as
spaces.

`translate` runs `lower_body` under `OWNED` (so `2f` became `2.f` here as
well — the subset used to pass it to C, which rejects it). Everything
matches outside strings and comments. `TestLowerBody` pins each family
under both models; unity_pack's packed output is checked end to end by
`tools/unity_pack_golden.py`.

## Layout

| file | role |
|------|------|
| `tools/cs2cpp.py` | refusals + C# → C++ subset (`translate`) |
| `tools/csrust.py` | CLI; `cs2cpp.translate` then `cpprust.translate` |
| `tests/test_csrust.py` | semantics, lowering, Shared, generics, except, digest, plain data, enums, declaration order, lists, type-named fields |

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
