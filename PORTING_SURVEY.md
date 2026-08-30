# Porting the C++ forks to the Crust subset — a survey

Four libraries are in flight. This is what each one costs, what they cost
*in common*, and the order I would do them in. Measured against
`crust/tools/cpprust.py` as it stands, not estimated.

## The numbers

Counts are raw pattern matches over each library's own headers and sources
— they size a problem, they do not enumerate work items. Two caveats worth
knowing before reading the table: the `<<`/`>>` column catches bit-shifts
as well as stream operators (coost's 998 is mostly shifts; its 135
`operator<<` is the real figure), and `auto` is listed because it is
conspicuous, not because it is expensive — see *What is cheap* below.

| | files | throw | catch | `std::function` | `operator<<` | capturing λ | `template<>` | concepts/`requires` | coroutines | SFINAE-ish |
|---|---|---|---|---|---|---|---|---|---|---|
| **cpp-mcp** | 16 | 37 | 41 | 8 | 0 | 24 | 0 | 0 | 0 | 0 |
| **frugally-deep** | 103 | 1 | 0 | 13 | 0 | 24 | 0 | 2 | 0 | 5 |
| **FunctionalPlus** | 47 | 10 | 3 | 44 | 0 | 34 | 13 | 0 | 0 | 188 |
| **coost** | 113 | 1 | 0 | 49 | 135 | 8 | 5 | 2 | 0 | 17 |
| **micron** | 1114 | 273 | 113 | 0 | 53 | 425 | 509 | 3053 | 474 | 3080 |

## What each one actually needs

### cpp-mcp — in progress, two of six files translating

The remaining work is known and bounded: 37 `throw` sites needing the
checked-error decision, and one handler-table redesign that `mcp_resource`
has already had (function pointer plus context). Its vendored `json.hpp`
and `httplib.h` are the larger question, and are shared with nothing else
here.

### coost — the best next candidate, but not for free

C++11, self-contained, almost no STL: the include scan turns up C headers
plus `<functional>`, `<type_traits>`, `<mutex>`, `<utility>` and little
else. It already ships its own `fastring`, `vector`, `table` and `clist`,
which is exactly the shape the subset wants — no `std::` containers to
translate, and container code written against a small explicit API.

The blocker is its formatting core. `fastream` is **42 `operator<<`
overloads**, and the subset refuses them twice over: `<<` and `>>` are
permanently out, and the overloads are same-arity, which resolves by
argument *count* here. Every logging and formatting call site in the
library goes through them. That is an API redesign to `append(...)`
methods, mechanical but wide.

After that: 49 stored `std::function` (the callable-storage wall, same as
cpp-mcp's), 195 reference returns (cheap — return `T *`), 203 `new`/`delete`
(mostly fine; array `new` is not), and 5 explicit specialisations.

### frugally-deep — its own code is nearly clean; the dependencies are not

103 files and only **one** `throw`, no `catch`, 13 `std::function`. Its own
code is the most subset-friendly of the four. What stops it is what it
sits on:

* **Eigen** — expression templates, CRTP, SFINAE throughout. This is not a
  subset gap, it is the opposite design philosophy, and I do not think it
  ports. Replacing it with a small dense-matrix type is the realistic path.
* **FunctionalPlus** — 14,800 lines, 188 SFINAE-ish constructs, 44 stored
  `std::function`, 13 explicit specialisations. Higher-order function
  templates are the whole library, and storing callables is the thing the
  subset cannot do.
* **nlohmann/json** — shared with cpp-mcp. Its first blocker is already
  gone (see below).

fdeep's own headers are worth translating *after* the dependencies are
replaced, not before. Replacing Eigen and fplus with small purpose-built
types is a smaller job than porting them.

### micron — not a porting target

1,114 files, 425,000 lines, and a deliberate full replacement of libc and
the C++ standard library in C++23. **3,053 `concept`/`requires`, 474
coroutine keywords, 509 explicit specialisations, 3,080 SFINAE-ish
constructs.** None of that is in the subset and none of it is a gap that
closes with a few passes — the library is built out of exactly the features
the subset subtracts.

There is a more interesting observation here than a cost estimate. micron
and the Crust subset are answers to the *same question* — what should
replace the STL for systems work — and they answer it in opposite
directions: micron by using the newest features to rebuild everything,
Crust by removing features until what is left can be lowered to C. Porting
one to the other is not a refactor, it is a rewrite. If micron is wanted, I
would take it as a *source of designs* — its container APIs, its allocator
model, its syscall layer — rather than as code to translate.

## What they need in common

Three problems account for most of the work across cpp-mcp, coost and
fplus, and each is one design decision rather than N edits:

1. **Storing a callable.** 49 + 44 + 8 stored `std::function`. A capturing
   lambda is inlined at its call sites and has no value to store. The
   pattern is a function pointer plus a `void *` context; cpp-mcp's
   subscription map is the worked example.
2. **Error handling.** 37 + 10 `throw`. The subset's replacement is the
   checked `except`/`raise` model, whose syntax is **not valid C++** — so
   adopting it ends the dual build every one of these ports relies on to
   catch mistakes. Macro-bridging, forking the sources, or converting to
   return codes; this needs deciding once, for all of them.
3. **Stream operators.** coost's 135 and cpp-mcp's logger. `<<` is
   permanently refused. Both need `append`-style APIs.

Reference returns and `auto` appear large in the table and are not:
returning `T *` is a one-line change per site, and `auto` only matters
where deduction has nothing written to read from.

### What is cheap, and a measurement worth keeping

`auto` is not a translation-speed cost. Measured: 300 `auto` declarations
versus 300 written types translate in 0.53s and 0.52s. What `auto` causes
is outright *failure* where the type is not written anywhere — `auto it =
m.find(k)` — and that is the only reason to touch it. Removing `auto` from
cpp-mcp's `mcp_resource.cpp` made it *slower*, 0.69s to 0.86s, because it
then got further before failing. Translation time tracks how much work
completes.

## Two subset gaps this survey turned up

**Partial explicit template arguments.** coost's `god.h` has
`template<size_t A, typename X> X align_up(X x)` called as
`align_up<A>(x)` — one argument given, the rest deduced. The subset either
deduces everything from a `T *` parameter or wants every argument spelled.
This is not exotic C++, and it blocks `fastring.h` and `fastream.h` at
their first include. Worth building.

**`accumulate` over a class — fixed this session.** nlohmann's `json.hpp`
folds tokens into a string with `std::accumulate`, which was refused
because "operator overloading is not in this subset". That stopped being
true when `string` gained `operator+`. Three changes: the check now allows
a class that *declares* the operator; the supplied `accumulate` body uses
`sum += *it` rather than `sum = sum + *it`; and `_named_object` learned to
see through `*p` for a pointer local, whose address is `p` itself. Verified
under ASan and LSan. This unblocked the first json wall for **both**
cpp-mcp and frugally-deep.

## Suggested order

1. **coost**, once the `fastream` API is redesigned. Self-contained, C++11,
   its own containers, no dependency chain to untangle first. It is also
   the best proving ground for the callable-storage pattern at scale.
2. **The shared error-handling decision**, which unblocks the tail of
   cpp-mcp and applies to everything after it.
3. **Replacements for Eigen and FunctionalPlus**, then frugally-deep's own
   headers, which are already close.
4. **micron** as a design reference, not a translation target.
