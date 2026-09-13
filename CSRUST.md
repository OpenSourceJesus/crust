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
| `class` / `struct` / `interface` / `enum` | C++ types; interface → pure virtual |
| fields, methods, constructors, `~T` | cpprust method shape `T_method(T *this, …)` |
| single inheritance, `virtual` / `abstract` | vtables |
| `class Box<T>` | `template<typename T> class Box` → monomorphise |
| `List<T>` / `Dictionary<K,V>` | `std::vector` / `std::map` |
| `T[]`, jagged `T[][]`, `.Length` | `vector`, `.size()` |
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
`decimal`, `partial`, `goto`, `params`, `stackalloc`, `checked`/`unchecked`.

## Layout

| file | role |
|------|------|
| `tools/cs2cpp.py` | refusals + C# → C++ subset (`translate`) |
| `tools/csrust.py` | CLI; `cs2cpp.translate` then `cpprust.translate` |
| `tests/test_csrust.py` | semantics, lowering, Shared, generics, except, digest |

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
