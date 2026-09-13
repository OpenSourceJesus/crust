# CSRUST — C# subset for Crust

Approach **D** from [issue #25](https://github.com/brentharts/crust/issues/25):
normalize a C# subset to the existing C++ subset (`tools/cs2cpp.py`), then
run `tools/cpprust.py` unchanged. CLI: `tools/csrust.py`. Includes of
`.cs` files go through the same out-of-process protocol as `.cpp`
(`shivyc/preproc.py`).

## What a `class` means (§1)

Crust has no GC. Default `class` is **single ownership** (same as
`cpprust --owning`): assignment and copy construction are refused.
Opt in to refcounting per type:

```csharp
class Node { }              // one owner; moves; scope-exit destruction
[Shared] class Node { }     // shared_ptr; aliasing OK; cycles may leak
```

`struct` stays a value type (copy OK). Silent value-copy of a default
`class` is forbidden — see `tools/test_csrust_semantics.py`.

## Phase 1 surface

In: `class` / `struct` / `interface`, fields, methods, constructors,
empty destructor synthesis for owning classes, single inheritance,
`virtual` / `override` / `abstract`, `var`, `foreach`, auto-properties,
`List<T>` → `vector`, `Dictionary<K,V>` → `map`, `throw`/`catch` →
`raise`/`except`, `string`, `null`, `this.`, `new T()`.

Out (refused in C# terms before conversion): `async`/`await`, LINQ,
`yield return`, `dynamic`, `partial class`, `event`, multidimensional
arrays, `stackalloc`, `params`, `checked`/`unchecked`, `goto case`,
`static class`, extension methods (`this` first parameter).

## Layout

| file | role |
|------|------|
| `tools/cs2cpp.py` | refusals + C# → C++ subset normalize |
| `tools/csrust.py` | CLI; normalize then `cpprust.translate` |
| `tools/test_csrust_semantics.py` | pins `=` meaning |
| `tools/test_csrust.py` | Counter, refusals, sugar, Shared/virtual run, `#include` |

## Not in this PR

Digest / four-language TU (`--emit-decls`), extracting `cpprust_core.py`
(approach C), `async`, LINQ, and open questions in the issue (`string`
interning, `--shared` CLI list, bounds checks default).
