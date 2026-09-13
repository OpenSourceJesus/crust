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
`[Shared]` is reserved for a future `shared_ptr` opt-in and is **refused**
today (not silently ignored) — see `tests/test_csrust.py` (`TestSemantics`).

## Phase 1 surface

In (see `tools/cs2cpp.py` / `tests/test_csrust.py`): `class`, `interface`,
fields, methods, constructors, single inheritance, `virtual` / `abstract`,
`var`, `foreach`, arrays → `vector`, primitive map, `using` aliases.

Out (refused in C# terms before conversion): `async`/`await`, LINQ,
`yield`, `dynamic`, multidimensional arrays, `ref` parameters, string
interpolation, file-scoped namespaces, and more — each with reason and
replacement in the diagnostic.

## Layout

| file | role |
|------|------|
| `tools/cs2cpp.py` | refusals + C# → C++ subset (`translate`) |
| `tools/csrust.py` | CLI; `cs2cpp.translate` then `cpprust.translate` |
| `tests/test_csrust.py` | semantics, lowering, interfaces, refusals |

## Not done yet

`[Shared]` refcounting, digest / four-language TU, extracting
`cpprust_core.py`, `async` / LINQ.
