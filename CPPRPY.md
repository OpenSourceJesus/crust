# cpprpy — one object model for the C++ subset and rpython

The specification of the cross-language object model as it exists, and the
design record of how it got here. An earlier version of this document was a
proposal; most of what it proposed has since been built and tested, so it
now describes machinery, states what each piece costs, and marks the parts
that remain design with their acceptance tests. Where a rule exists because
something failed without it, the failure is kept in the text — it is the
best documentation the rule will ever have.

The one-sentence version: multiple inheritance, `dynamic_cast`, `typeid`,
C++ subclassing rpython, rpython subclassing C++, and a checked `try` were
never six features. They are **one change** — a shared type descriptor —
and five things that fall out of it.

---

## 1. The keystone: both compilers already emitted the same object

`cpprust.py` and `py2c.py` lay out a polymorphic object identically:

```c
/* cpprust: the root of a hierarchy                                   */
struct Shape  { const struct Shape_vtable *_vptr; int id; };
struct Square { Shape _base; int side; };

/* py2c: every boxed class                                            */
struct Obj  { const void* type; };
struct Node { Obj _hdr; /* base fields repeated first */ ... };
```

A **descriptor pointer at offset zero**, and the **base laid out first**.
Neither is a coincidence: cpprust chose it so an upcast is a cast, py2c
chose it so base/derived casts are valid. They arrived at the same layout
from opposite directions, and the descriptors differ only in that py2c's
`TypeInfo` is **cpprust's vtable with an RTTI header in front**:

| | cpprust `Shape_vtable` | py2c `TypeInfo` |
|---|---|---|
| `name` / `base` / `fields` | under `--rtti` | always |
| `tostr` / `eq` / `addfn` / `objsize` | under `--rtti` | always |
| virtual slots | all of them | all of them |

So the keystone is small and mostly subtractive: **prefix a cpprust vtable
with the `TypeInfo` header, and the vptr a polymorphic object already
carries *is* the descriptor pointer.** Zero bytes per object, one static
structure per class, dispatch unchanged.

This equivalence is not prose — it is pinned by a test that reads py2c's
source and asserts the `_CppTypeInfo` header matches `TypeInfoHdr`
field-for-field. If either side ever moves a field, the suite says so
before a linker does.

---

## 2. RTTI: implemented under `--rtti`

`dynamic_cast<T*>` and `typeid` work. The cast is a base-chain walk
(`_cpp_isinstance`), the `typeid` is a field read, and the whole runtime is
the loop `shivyc_rt.h` already carried:

```c
for (; k; k = k->base) if (k == want) return true;
```

No allocation, no locking, no `__cxa` machinery. The chain's length is
fixed at compile time, so the cost of every cast is statable in a review —
which is what the standards that distrust `-frtti` actually ask for. The
usual objection to RTTI is not the type check; it is that a cast across a
*virtual* base is a table search of unbounded cost. Section 3 refuses
virtual bases, so that cast cannot be written here.

Refused, with the reason in the diagnostic: the reference form
`dynamic_cast<T&>` (it throws, and there is nothing for it to throw
through); the value form; casts to non-polymorphic targets; casts naming
unknown classes. Abstract classes get a standalone `C__typeinfo` object
(there is no vtable instance to prefix), emitted after the struct because
`sizeof` needs the complete type — found by the compile error, kept as a
comment at the emission site.

Off by default. Bare-metal targets count `.rodata` per class, and "no RTTI
at all" is the right default there; the flag is named in the error when a
`dynamic_cast` appears without it.

Everything on the rpython side compounds: `isinstance(x, Foo)` where `Foo`
is a C++ class is the same chain walk, and `FieldDesc` gives the C++ class
getattr-by-name from rpython with no `__getattr__` machinery at all.

---

## 3. Multiple inheritance, in tiers

**Tier 1 — interface bases: implemented.** One layout base plus any number
of *interface* bases (no data members, at least one virtual). Each
interface costs one `_vptr_<Iface>` at a compile-time offset after the
layout base; dispatch goes through per-class tables of adjusting thunks;
conversions `(I*)&obj` and `(I*)ptr` adjust by `offsetof`. Verified
byte-identical against `g++` on the same sources.

Two rules in the implementation earned their comments the hard way:

* **A derived class emits its own interface table even when the interface
  arrives through the layout base.** The first version reused the base's
  table; a derived override then dispatched to the base's method — output
  `101` where g++ said `305`. Wrong dispatch that compiles is the exact
  failure class this project exists to not have.
* **Interface conversions are refused for non-symbol operands** (call
  results, array elements) rather than guessed at. The leftover-conversion
  check anchors on the full adjusted shape and runs *after* rewriting, so
  anything it finds is genuinely unhandled. Lifting this is the next MI
  step, not a missing cast.

**Tier 2 — data-carrying secondary bases: designed, not built.** The
layout is known (each secondary base a sub-object at a fixed offset, thunks
adjust `this`); the reason to wait is that tier 1 covers the mixin pattern
the tree actually uses.

**Tier 3 — virtual inheritance: refused, permanently.** A virtual base's
offset depends on the most-derived type, which turns field access into a
runtime table lookup and `dynamic_cast` into an unbounded search — the two
costs this subset exists to not have. This is a design position, not a
gap, and the diagnostic says so.

---

## 4. The digest: one artifact, two producers, two consumers

Cross-language classes do not get two bridges. They get one artifact:

```
                    ┌──────────────────────┐
   some.py ────────▶│  <mod>.decls.json    │◀──────── some.cpp
   (py2c --decls)   │  DECLS_VERSION 1     │   (cpprust --emit-decls)
                    │  lang, classes,      │
                    │  fields+ctypes,      │
                    │  descriptor header   │
                    │  + virtual slots,    │
                    │  typeinfo symbol     │
                    └──────────┬───────────┘
                 ┌─────────────┴─────────────┐
                 ▼                           ▼
       cpprust --decls F reads it:     py2c reads it beside its
       `class C : public PyThing`      inputs: `class C(CppThing)`
       resolves                        resolves
```

`lang` is in the digest because it is the one thing consumers must not
infer: it decides whether a base constructor is spelled `T_new` (C++,
in-place) or split `T_new`/`T___init__` (rpython, alloc then fields), and
whether the descriptor member is `_vptr` or `_hdr.type` — same word, same
offset, different name in the declaring struct.

### Direction 1 — C++ subclasses rpython: runs

`cpprust --decls pool.decls.json` lets a C++ class name an rpython base.
The derived table is emitted with **designated initializers** into the
foreign `TypeInfo` — a field reorder becomes a compile error instead of a
wrong indirect call. The C++ constructor chains into `Pool___init__` and
then stamps `((Obj*)this)->type` itself, because py2c splits allocation
from initialisation and the stamp has nowhere else to happen. Verified: a
mixed unit runs, `TYPE(s)->area` dispatches an rpython call into the C++
override, and `isinstance` crosses the boundary.

cpprust consuming a **cpp** digest is refused: a derived table needs the
base's per-class `struct <B>_vtable`, and a digest carries the
descriptor's *layout*, not that type. Not the intended direction anyway —
include the header.

### Direction 2 — rpython subclasses C++: runs

`import pool_cpp` resolves `pool_cpp.decls.json` beside the inputs;
`class MyPool(pool_cpp.Pool)` lays out over the C++ struct; the derived
`TypeInfo` carries the C++ implementations by their published symbols
(`.take = Pool_take`); `isinstance` walks one chain across the boundary.
The mixed binary in the test suite builds the object in rpython,
dispatches the C++ virtual twice, and reads its own field.

The consumer's design is the part worth keeping: the digest is rendered as
**the Python module a matching class would have been written as**, and
py2c's ordinary import path parses it. No hand-synthesized `ClassInfo` —
everything after `ast.parse` is code that already worked. Three stub
properties are load-bearing, each found by a failure rather than foreseen:

* **Fields assign from annotated parameters, not literals.** `self.cap =
  0` infers a boxed field, and a derived class repeats a base's fields at
  the base's types — one lazily-typed stub field mislays every subclass's
  struct. (Caught as `obj cap;` where the C++ struct has `int cap;`.)
* **Method bodies write an instance field.** A body that doesn't
  classifies as POD, the base's slots are dropped, and the derived table
  carries a NULL where `take` should be. (Caught as a segfault through
  exactly that slot.) The stub's write declares what is true of the real
  implementation: these methods use the object.
* **The digest joins the project hierarchy scan.** Otherwise the subclass
  roots itself and the canonical vtable shrinks to its own methods.
  For digest-backed modules the POD question is not answered by the
  heuristic at all — cpprust already decided which classes carry a
  vtable, and the stub renders only those.

On the cpprust side, `--emit-decls` **is a linkage decision**: exactly the
digest's symbols lose `static` (thunks stay internal — reached through the
table, never by name), and a gcc alias publishes each vtable under the
`<Cls>_type` name py2c links base chains through. Both rewrites are keyed
on the digest, so nothing unpublished changes; the 18-example sweep is
byte-identical with the flag off.

### Scope, honestly stated

* Only **polymorphic** C++ classes render into the stub. A C++ class with
  no virtuals is a bare struct with no header word; laying an
  `Obj`-headed class over it would shift every field. Absent from the
  stub means a missing-name diagnostic, not a silently wrong layout.
* **Non-virtual methods are absent** — the digest records full signatures
  only for virtual slots, and a method that cannot be typed cannot be
  called. Publishing signatures for the rest is a digest extension, not a
  consumer change.
* **C++ code dispatching virtually on an rpython-created object is out of
  scope.** The C++ vtable's slots sit in declaration order; py2c computes
  its own canonical order; today the two agree only by luck. Relatedly,
  `dump_decls` currently sorts the descriptor's slot list by name — 
  harmless for the existing consumers (cpprust fills foreign tables by
  designated initializer, py2c derives order from the stub), but it is
  the exact thing that must become declaration order before this scope
  limit can lift. The earlier version of this document called canonical
  slot order "the single most dangerous failure mode in the whole
  design"; that sentence stays true, which is why the limit is a refusal
  today instead of a hope.

---

## 5. Checked errors: `except`, implemented

The earlier version of this section was a proposal that ended "take
minipy's model, and do not call it `catch`." That is now the implemented
behaviour of `cpprust.py`, so this section is a specification.

```cpp
int parse(int x) except {          /* fallibility is in the signature   */
    if (x < 0) { raise 42; }
    return x * 2;
}

int main(void) {
    int r;
    try {
        r = parse(n);
    } except (long e) {            /* one machine word of payload       */
        log(e);
    }
}
```

The mechanism is minipy's `exc_flag`/`exc_val` with C spelling — a static
per-unit `struct { int flag; long val; } _cpp_exc` — so a mixed
translation unit has **one error model**, not two that have to be bridged.
It is spelled `except`, not `catch`, as a load-bearing signal: a reviewer
working to a standard that bans `catch` can see at a glance this is not
that thing.

### The lowering, and the property it exists for

`raise E;` lowers to a flag-set plus an **ordinary return**, and the pass
runs *before* the return lowering — which already emits every destructor a
return needs. So destructors run on the error path without this pass
knowing what a destructor is. C++ exceptions need an unwinder for that
sentence; the checked model gets it from the existing epilogue. The
behavioural tests pin it in both shapes: the raising function's own local
(`ctor 7 / dtor 7 / caught 9`), and a middle function's local destroyed as
the poisoned return passes through.

Every statement that calls a fallible function is followed by a check, and
the check's action is the **innermost handler**: inside a `try`, a `goto`
to its handler; in a bare `except` function, a poisoned return the
caller's own check picks up; with neither, a **compile-time refusal** — an
unhandled error is a compile error here, not a `terminate()` later, which
is the property `noexcept` never gave anyone.

`raise` takes the same dispatch as a failed call. This was the
implementation's one real semantic bug: the first version always returned,
so a raise inside a `try` escaped the function, and a legitimate re-raise
in `main` was refused with its outer `try` standing right there. The
nested-handler test now pins both directions, including a bare `raise;`
re-raising the held value one level out while a surrounding loop
continues.

### What is refused, and why each refusal is the feature

* **A fallible call embedded in another call's arguments.**
  `printf("%d", c())` would print garbage first and jump to the handler
  second — the statement-level check runs after the statement, which is
  too late once the enclosing call has consumed the poisoned value. The
  diagnostic carries the rewrite: bind it to a local first. Note that
  `return a() + 1;` inside a fallible function stays *sound*: the value
  is garbage, but the caller tests the flag before the value, which is
  the whole contract.
* **A class local declared inside a `try` block.** The handler is reached
  by a jump that leaves the block early, skipping the scope-end
  destructor on exactly the path it matters most. Declare it before the
  `try`.
* **`raise` with nowhere to go**, and **`try` without an `except`**.
* **`try`/`raise` in class bodies** — the slice covers free functions
  first; methods are the named next step.
* `throw` and `catch` stay refused permanently, and their diagnostics now
  name the replacement.

A translation unit that uses none of the keywords is untouched — the pass
early-exits, the prelude is not emitted, and the 18-example sweep is
byte-identical. The payload is one machine word for now; richer payloads
(an error class with fields) are a later step that must not cost the
no-allocation property.

---

## 6. Rewriting minipy in the C++ subset: mostly, don't

MINIPY_MEMORY.md carries the measurements and the verdict, condensed here:
the interpreter's scaling problem was `st.heap` growing without bound, and
the fix was a precise mark-sweep collector over the handle table minipy
already had — peak slots went from linear in iterations to flat. RAII was
the wrong tool for it: interpreter values have dynamic lifetime and no
scope, and the one scope-shaped part (frames) was already pooled. Where
the C++ subset *does* earn a place in minipy is small, self-contained,
non-escaping machinery — a mark bitmap, a free list — which is a much
narrower claim than "rewrite the interpreter," and the one the evidence
supports.

---

## 7. Status

| piece | state | tests |
|---|---|---|
| shared descriptor / `--rtti` | implemented | 50 (`test_cpprust_rtti`) |
| tier-1 MI | implemented | in rtti + extras suites |
| digest, C++ ⊂ rpython | implemented | part of 30 (`test_cpprpy_decls`) |
| digest, rpython ⊂ C++ | implemented | part of 30, incl. mixed binary |
| checked `try`/`except` | implemented, free functions | 21 (`test_cpprust_except`) |
| tier-2 MI | designed | — |
| virtual inheritance | refused permanently | refusal pinned |
| C++ dispatch on rpython objects | out of scope pending slot canon | limit documented |

Baselines that hold throughout: `tests/test_cpprust` at 379 with the same
two pre-existing failures, extras 142, move-lowering 41, the 18 examples
byte-and-return-code identical, minipy 19/19.

## 8. Next, in leverage order

1. **`except` for methods** — same lowering, `this`-qualified names; the
   refusal already points here.
2. **MI cast operands** — lift the non-symbol refusal via the existing
   symbol table; the refusal tests flip to lowering tests, g++-diff.
3. **Digest slot order = declaration order**, then adopt it as py2c's
   canon for digest-rooted hierarchies; acceptance is a byte-identical
   `TypeInfo` for one hierarchy split across the boundary in both
   directions — the test §4 has demanded since it was a proposal.
4. **Non-virtual method signatures in the digest**, so the stub can carry
   them.
5. **Richer `raise` payloads** without allocation — likely a static
   per-unit error object and the flag carrying which.

## 9. Open questions

* Should `--rtti` become implied by `--emit-decls`? Publishing a class
  without a descriptor publishes half an object; today the flags are
  independent and the combination is on the user.
* Threading for `_cpp_exc`: a single static is honest for the current
  single-threaded targets; the moment threads land, it becomes
  thread-local or per-frame, and the choice should be measured, not
  assumed.
* Whether the digest should version per-field rather than per-file:
  DECLS_VERSION=1 bumps globally, which is simple and blunt.
