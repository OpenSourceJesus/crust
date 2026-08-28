# cpprpy — one object model for the C++ subset and rpython

A design note, not a change. It argues that the several features asked for here
— multiple inheritance, `dynamic_cast`, `typeid`, C++ subclassing rpython and
rpython importing C++, a safe `try` — are not five features. They are one
change and four things that fall out of it.

---

## 1. The finding

`cpprust.py` and `py2c.py` already emit the same object.

```c
/* cpprust: the root of a hierarchy                                   */
struct Shape  { const struct Shape_vtable *_vptr; int id; };
struct Square { Shape _base; int side; };

/* py2c: every boxed class                                            */
struct Obj  { const void* type; };
struct Node { Obj _hdr; /* base fields repeated first */ ... };
```

Both put a **descriptor pointer at offset zero** and lay the **base out first**.
Neither is a coincidence: cpprust chose it so upcasting is a cast, py2c chose it
so base/derived casts are valid. They arrived at the same layout from opposite
directions.

The descriptors differ, and only in one direction:

| | cpprust `Shape_vtable` | py2c `TypeInfo` |
|---|---|---|
| type name | — | `const char* name` |
| base link | — | `const TypeInfo* base` |
| field table | — | `const FieldDesc* fields` |
| object size | — | `unsigned long objsize` |
| `tostr` / `eq` / `addfn` | — | present |
| virtual slots | all of them | all of them |

py2c's descriptor is **cpprust's descriptor with an RTTI header in front of
it**. Everything the C++ subset is missing is a prefix on a struct it already
emits.

So the keystone change is small and mostly subtractive:

> **Give a cpprust vtable py2c's `TypeInfo` header, and give a cpprust root
> `Obj _hdr` instead of a bare `_vptr`.**

Nothing about dispatch changes — the slots keep their order, inherited first,
which is the property the current lowering leans on. The vptr is still at
offset zero. What changes is that the thing it points at now says what type
this is.

---

## 2. What falls out for free

### `dynamic_cast` and `typeid`

`shivyc_rt.h` line 224 already contains the whole implementation:

```c
static inline bool isinstance_of(Obj* o, const void* t) {
    const TypeInfoHdr* want = (const TypeInfoHdr*)t;
    const TypeInfoHdr* k = o ? (const TypeInfoHdr*)o->type : NULL;
    for (; k; k = k->base) if (k == want) return true;
    return false;
}
```

Then:

```cpp
Square *sq = dynamic_cast<Square *>(s);
const char *n = typeid(*s).name();
```
```c
Square *sq = isinstance_of((Obj*)s, &Square_type) ? (Square*)s : 0;
const char *n = ((const TypeInfoHdr*)((Obj*)s)->type)->name;
```

This is a base-chain walk, bounded by hierarchy depth, no allocation, no
locking, no `__cxa` runtime. It is closer to what safety-critical code does by
hand — an enum tag and a switch — than to what `-frtti` costs. The
`dynamic_cast<T&>` form, which throws in real C++, stays refused; only the
pointer form, which yields null, is in.

Worth noting for the JSF audience: the usual objection to RTTI is not the type
check, it is that `dynamic_cast` on a virtual base is a **table search of
unbounded cost**. Section 4 refuses virtual bases, so every cast here is a
linear walk of a chain whose length is fixed at compile time. The cost is
statable in the docs, which is what those standards actually want.

### `isinstance` and `type()` across the boundary

The same call answers an rpython `isinstance(x, Foo)` where `Foo` is a C++
class, because there is now only one notion of "what type is this".

### Dynamic attributes by name

`FieldDesc` gives name → offset → storage code, and `rt_getattr`/`rt_setattr`
already walk it plus the base chain. A C++ class that opts into the header gets
`getattr`-by-name from the rpython side at no extra cost — which is exactly the
"work with the more dynamic pyobject model" the request asks for, and it needs
no `__getattr__` machinery in the C++ subset at all.

### The cost, stated honestly

One pointer per object and one static `TypeInfo` per class — but **only for
classes that already have a vtable**, which pay for a `_vptr` today anyway. A
class with no virtuals stays a plain struct with no header. So the tax on
existing C++ code is: `sizeof` unchanged, one extra `.rodata` struct per
polymorphic class, and constructors store the same one pointer they already
store.

That said, it should still be **opt-in per translation unit** (`--rtti`, or
deriving from a `crust::Object` marker). Some users want the C++ subset for
bare-metal ARM64 where `.rodata` per class is a real number, and the current
"no RTTI at all" behaviour is the right default there. Two modes, one flag, and
the flag is named in the error when a `dynamic_cast` appears without it.

---

## 3. Multiple inheritance, in tiers

CPPRUST.md's refusal is correct as written: *"The layout admits exactly one
base: with one base first, upcasting is free, and that is the property the rest
of this lowering leans on."* Full C++ MI gives that property up. But there is a
subset that keeps it, and py2c has already found it.

### What py2c already does

`py2c.py:2755` — the first base is the **layout base**, further bases are
recorded so their methods resolve to direct calls. And `_reads_self_fields`
(line 2963) **rejects the unsound cases**: a mixin whose methods touch instance
state through the secondary layout is refused, a stateless one is allowed.

That is the shape to adopt, because it is already the shape the rpython side of
any mixed hierarchy will have.

### Tier 1 — interface bases (recommended first)

```cpp
class Drawable { public: virtual void draw() = 0; };          // no data members
class Square : public Shape, public Drawable { ... };
```

Every base after the first has no fields. The secondary base contributes one
word — its own vptr — placed immediately after the primary base:

```c
struct Square { Shape _base; const struct Drawable_vtable *_vptr_Drawable;
                int side; };
```

`(Drawable *)sq` becomes `&sq->_vptr_Drawable`, a **compile-time constant
offset** known from the class declaration. The thunk in `Drawable`'s table
subtracts that same constant to recover `this`.

This is a genuinely small delta on existing machinery, because cpprust already
generates converting thunks: *"Overrides reached through a table go via a small
thunk that converts `this`."* Today that conversion is by zero. MI makes it a
nonzero constant. The thunk generator does not otherwise change.

### Tier 2 — data-carrying secondary bases

Mechanically identical: the offset is still a compile-time constant. Two things
genuinely break, and the new header fixes both.

1. **`delete` through a secondary base pointer** needs the offset back to the
   complete object.
2. **Cross-casting** (`dynamic_cast<OtherBase*>(p)` where neither is an
   ancestor of the other) needs the same.

Add one field to the `TypeInfo` header, per vptr:

```c
long offset_to_top;   /* subtract to reach the complete object */
```

`delete p` becomes "adjust by `offset_to_top`, then dispatch the destructor
slot," and cross-cast becomes "adjust to top, then walk the chain from the most
derived type." Both are two loads and a subtract. Since section 2 is adding a
header anyway, this field costs one word in `.rodata` per class and nothing per
object.

Tier 2 should ship **after** tier 1 has a test suite, not with it. Tier 1 covers
the mixin and interface patterns that motivate MI in practice; tier 2 mostly
serves inheritance hierarchies that a lean language should be arguing against.

### Tier 3 — virtual inheritance: refuse, and say why

This one should be a **permanent design position**, not a TODO, and CPPRUST.md
should say so in the same register it says everything else.

A virtual base's offset depends on the **complete object type, not the static
type**. That forces:

* a vbase-offset table consulted **at runtime** on every access to a virtual
  base member — so a field read stops being a fixed displacement,
* a "most derived" flag threaded through every constructor, because who
  constructs the shared base depends on who is at the top,
* `dynamic_cast` becoming a search rather than a walk, which is precisely the
  unbounded cost that got RTTI banned in the first place.

The whole point of the request is that the language needs a lean reboot. Virtual
inheritance is the single feature where C++ most clearly chose "expressible"
over "predictable." Refusing it is not a gap in the subset; it is the subset
having an opinion.

The diamond is better served by the two things that stay: **stateless interface
bases** for shared behaviour, and **composition with a reference** for shared
state. That also happens to be what rpython can express, so a mixed hierarchy
does not need one rule on each side.

Suggested diagnostic, in the house style:

```
shape.cpp:14: `class D : public virtual B` is not in the C++ subset: a virtual
base's offset depends on the most-derived type, so every access to a `B` member
would become a runtime table lookup and `dynamic_cast` an unbounded search.
Use a non-virtual interface base (no data members), or hold a `B &`.
```

---

## 4. Which direction should the include go? Both, over one artifact

`#include "some.py"` **already works** — `shivyc/preproc.py:776` dispatches it
to `shivyc/rpyinc.py`, which runs py2c and splices the generated C, with a
`/tmp` cache keyed on the module text and on py2c itself. The mechanism is
built. What is missing is that cpprust cannot *see* the classes.

And cpprust cannot simply reuse rpyinc, because it runs as a subprocess before
preproc splices anything, and it does its own header splicing (`_ANY_INCLUDE`,
line 1320) precisely because *"a class this file uses is only a class if its
declaration is in hand."*

So the answer is not two bridges. It is **one artifact with two producers and
two consumers**:

```
                    ┌──────────────────────┐
   some.py ────────▶│  class digest        │◀──────── some.cpp
   (py2c --decls)   │  name, base(es),     │   (cpprust --emit-decls)
                    │  fields+ctypes,      │
                    │  virtual slots in    │
                    │  canonical order,    │
                    │  TypeInfo symbol     │
                    └──────────┬───────────┘
                               │
                 ┌─────────────┴─────────────┐
                 ▼                           ▼
       cpprust reads it, so         py2c reads it, so
       `class C : public PyThing`   `class C(CppThing)`
       resolves                     resolves
```

Both sides already have the code to consume it:

* **py2c** has `xclasses`, `xtype_externs`, `xvtable_impls` and
  `link_cross_module_hierarchy` (line 6837), whose entire job is *"when local
  classes extend an imported base, link the chain and adopt the hierarchy root's
  full virtual interface as the canonical vtable layout, so every module in the
  hierarchy emits a byte-identical `TypeInfo`."* That is the hard problem, and
  it is solved. A C++ class is just another imported base.
* **cpprust** has the precedent for being told about foreign types on the
  command line: `--owning Vec_int:Vec_int_free_buf,..`, added for exactly this
  reason — *"this module runs as a subprocess and cannot see the unit being
  compiled, so it has to be told."* `--decls some.digest` is the same protocol.

Two properties are worth fixing early:

**The digest must be derivable without a full transpile.** `py2c --decls` should
be a declaration-only pass, so a `.cpp` that includes a `.py` does not pay for
lowering the whole module twice. rpyinc's existing cache key (module text + py2c
source hash) extends to it unchanged.

**Slot order must be canonical, not per-module.** py2c already pins this via
`module_external_canon`. The digest should carry the canonical order explicitly
rather than letting each side derive it, so a mismatch is a diagnostic at
generation time and not a wrong indirect call at runtime. This is the single
most dangerous failure mode in the whole design and it deserves a dedicated
test: build the same hierarchy split across `.py`/`.cpp` in both directions and
assert the emitted `TypeInfo` is byte-identical.

---

## 5. Exceptions: take minipy's, and do not call it `catch`

This is the strongest part of the request, and the implementation already
exists.

minipy does not unwind. `tools/minipy/interp.py` carries `st.exc_flag` and
`st.exc_val` on the interpreter state (line 217), sets the flag at a raise
(line 1311), and checks it at block boundaries (line 2992). `try/finally` was
built on that with **no new opcodes and no interpreter change** (MINIPY.md v29),
and `with` on top of that with none either (v30).

That is not a limited exception mechanism. It is a **different mechanism**: an
error is a value, propagation is a checked return, and control flow stays
visible in the generated C. Which is exactly what JSF, MISRA and AUTOSAR are
asking for when they ban `throw`. They are not banning error handling; they are
banning an unwinder they cannot bound.

So the proposal is:

```cpp
Result<int> parse(const char *s) except;   /* may fail — part of the signature */

try {
    int n = parse(text);
    use(n);
} except (ParseError &e) {
    log(e.msg());
}
```

lowering to a flag check after each fallible call, a jump to the handler, and
**the same epilogue a normal return uses**:

```c
int n = parse(text);
if (_exc.flag) goto _h1;
use(n);
...
_h1: /* handler */ ;
_done: Buf_drop(&b);   /* the ordinary epilogue — unchanged */
```

Four properties, each of which is the reason a standard banned the C++ version:

1. **Destructors still run**, because the error path reuses the normal epilogue
   rather than a separate unwinder. This makes it *safer* than C++ exceptions,
   not merely more restricted — and it composes with the existing rule that a
   C++ `~T()` and a Rust `impl Drop for T` lower to the same `T_drop` symbol.
2. **Fallibility is in the signature.** A call to a non-`except` function emits
   no check, so a program that does not use it pays nothing — and a function
   that can fail cannot be called without handling it, which is the check
   `noexcept` never gave anyone.
3. **Bounded stack, no allocation, no unwind tables**, so it survives
   `-fno-exceptions`, baremetal, and the ARM64 targets already in the tree.
4. **An unhandled error at a boundary with no handler is a compile-time error**,
   not a runtime `terminate()`.

Spell it `try` / `except`. Not to be cute — it is a load-bearing signal. A
reviewer working to a standard that bans `catch` can see at a glance that this
is not that, and it is literally the same mechanism the rpython side of the same
translation unit is using, so a mixed unit has one error model rather than two
that have to be bridged.

`throw` stays refused, permanently, and the diagnostic can now say what to write
instead.

---

## 6. Rewriting parts of minipy in the C++ subset

Here I would push back, because MINIPY.md already contains the measurements.

The reasoning in the request is sound as far as it goes — minipy predates the
C++ subset, so parts of it were written in rpython that need not have been. But
the docs are specific about where the time and the bytes actually go, and it is
not where a rewrite would help:

* **Dispatch was tested and it was not the bottleneck.** "Experiment 1
  (reverted): if/elif dispatch → C switch in py2c" (line 704). A C++ rewrite of
  the dispatch loop is a slower path to the same reverted result.
* **The value is already 16 bytes.** `_c_union_` got `V` from ~32 to 16 (line
  790) and cut peak RSS 24–32%. C++ would not shrink it further; it is a tag
  plus a union of three 8-byte members.
* **The named remaining lever is allocation, not language.** "each large-int/
  float arithmetic result allocates a 16-byte `V` that is never freed... the
  missing piece is reclaiming dead temporaries" (line 1009). The follow-ups —
  CALL\_METHOD, escape analysis, accumulator fusion — are compiler passes over
  the bytecode. They are language-independent.

Where the C++ subset **does** fit is narrower and more convincing: the parts of
minipy that are about **lifetime** rather than dispatch. The arena, the frame
pool, `regpool`, the block stack. Those are RAII-shaped, they are currently
hand-managed on every exit path, and "the registers return to the pool however
this frame exits" is exactly what a destructor says and what a hand-written
`afree` call forgets. Section 5's `try`/`except` makes that stronger, since the
error path runs the same epilogue.

So: not a rewrite. Pick **one** subsystem — the frame/register pool is the
obvious candidate, since frame pooling was already worth doing (line 646) —
port it, and measure it against the existing benchmark harness
(`benchmarks/run_minipy_benchmarks.py`, thirteen benchmarks, RSS and time). If
it does not move, that is a result worth having and it cost one subsystem
instead of the interpreter.

The two reverted experiments are the point: this project already measures before
it rewrites, and this is a case for keeping that habit rather than making an
exception for a new language.

---

## 7. Implementation status

Steps 1-3 are in `tools/cpprust.py`, behind `--rtti` for the descriptor.
Two corrections to what is written above, both found by building it:

**The object does not grow.** Section 1 proposed replacing cpprust's bare
`_vptr` with py2c's `Obj _hdr`. That is unnecessary: `const struct
X_vtable *_vptr` and `struct Obj { const void *type; }` are already the
same word at the same offset. The whole change is prefixing the *vtable*
with the descriptor fields, so the vptr a polymorphic class already carries
becomes the descriptor pointer. Cost is zero bytes per object, not one
pointer, and a class with no virtuals gains no header and cannot be asked
its type -- which is exactly C++'s own rule that `dynamic_cast` needs a
polymorphic operand.

**Tier-1 MI needs a per-class interface table, not a per-hierarchy one.**
Section 3 described the layout and the thunk and stopped there. It missed
that a derived class overriding an interface method must get a table of its
own even when it inherits the interface through its layout base. Sharing the
base's table made one object answer `draw()` two different ways depending on
which pointer it was asked through -- the layout base gave the override, the
interface gave the base. Caught by a differential test against g++ (101 vs
305), not by anything in the C++ half being inconsistent with itself.

### What works

* `dynamic_cast<T *>` and `typeid`, byte-identical to `g++` on three-level
  hierarchies, cross-casts, `typeid` equality and null operands. `name()`
  returns the plain class name where g++ mangles it; the standard leaves
  that implementation-defined.
* Tier-1 MI: interface bases, one vptr each at a fixed offset, per-class
  tables, adjusting thunks, constructor install, and conversions from a
  named object or member chain.
* Refusals, each with a diagnostic naming the fix: the reference and value
  forms of `dynamic_cast`, a non-polymorphic target, an unknown class, a
  data-carrying secondary base, a secondary base with no virtuals, virtual
  inheritance, and a conversion whose source type is not nameable.

### Step 4: the digest (a first vertical slice)

`py2c.py --decls` writes `<module>.decls.json` beside the generated C;
`cpprust.py --decls <file>` reads it, so a `.cpp` may name an rpython class
as a base. A C++ class three levels below an rpython root dispatches through
py2c's `TypeInfo`, and `isinstance_of` walks a chain that crosses the
boundary.

Four things the build taught, none of them visible from the design:

* **The slot list belongs to the module, not the class.** py2c gives every
  class in a module the same `TypeInfo` layout, so the digest records the
  vtable once under `descriptor` rather than per class. Writing it per class
  would invite a consumer to believe two classes could disagree.
* **Designated initializers remove the ordering hazard entirely.** Section 4
  worried that a slot-order mismatch would surface as a wrong indirect call
  rather than a diagnostic, and proposed carrying canonical order
  explicitly. The digest still does -- but the derived table is emitted by
  field *name*, so a reordering upstream becomes a compile error or nothing
  at all. The dangerous failure mode is designed out rather than tested for.
* **The two structs are layout-compatible and spelled differently.** py2c
  repeats a base's fields (`{Obj, id, side}`); cpprust nests the base
  (`{{vptr, id}, side}`). Same bytes in the same order, so each side reaches
  a field its own way and only the layout has to agree. The digest describes
  the layout and says nothing about access.
* **`__init__` is not the constructor.** py2c splits allocation from
  initialisation: `Square_new` arena-allocates *and* stamps the descriptor,
  while `Square___init__` only assigns fields. So a C++ constructor chaining
  to an rpython base must stamp the descriptor itself -- there is nowhere
  else it would happen. This is the destructor question from section 9 seen
  from the other end, and it is the one place the two memory models really
  differ rather than merely differing in spelling.

cpprust also stops splicing a `#include "some.py"`: it is not C++, and
reading it as C++ finds a `class` keyword and then fails inside a Python
body. The preprocessor already answers that include by transpiling and
splicing the C; the declarations arrive separately as `--decls`.

`tools/test_cpprpy_decls.py`, 20 tests, five of which build and run a real
mixed translation unit.

### What is refused that a later step should take

Converting to a secondary base from anything that is not a symbol or a
member chain -- a call result, another cast, an array element. The
adjustment needs the source type, and the call rewriter can only name one
for those two shapes. It is reported rather than emitted unadjusted,
because an unadjusted pointer compiles cleanly and dispatches through the
wrong table.

### Tests

`tools/test_cpprust_rtti.py`, 50 tests. The one worth keeping in mind reads
`TypeInfoHdr` out of `py2c.py` and compares field order against the C++
descriptor, rather than restating the layout: a test that restated it could
not detect it drifting, and that drift is what the digest in section 4 would
inherit as a wrong indirect call rather than a diagnostic.

Regression: `tests/test_cpprust.py` 379 (2 pre-existing failures unchanged),
`test_cpprust_extras.py` 142, `test_std_move_lowering.py` 41, all green. With
the flag off, all 18 example `.cpp` files and two real litehtml sources
(~550 KB of generated C each) are byte-identical to the pre-change
translator.

---

## 8. Suggested order

Each step is useful alone and none needs the next.

1. **`Obj` header + `TypeInfo` prefix on cpprust vtables**, behind a flag.
   Unblocks everything else. Acceptance: `litehtml_test.py --groups` shows no
   new failures, and existing non-RTTI output is byte-identical.
2. **`dynamic_cast` (pointer form) and `typeid`.** Small, once (1) lands.
   Removes two entries from *Not supported yet* by calling a function that is
   already in the runtime.
3. **Tier-1 MI: stateless interface bases**, with py2c's `_reads_self_fields`
   check ported as the guardrail — a mixin that touches instance state is
   refused, and the refusal is a test, since "for this pass a refusal *is* the
   contract."
4. **The class digest**, both directions, with the byte-identical-`TypeInfo`
   test as the acceptance criterion.
5. **`try` / `except`.** Independent of 1–4; could go first if the safety story
   is more urgent than the interop one.
6. **Tier-2 MI** (`offset_to_top`), only if tier 1 leaves real gaps.
7. **Virtual inheritance:** write the refusal into CPPRUST.md as a position.

Steps 1–4 make the C++ subset and rpython one object model. Step 5 gives it an
error model that the standards which ban C++ exceptions would accept. That is a
reasonable claim to "a better and safer C++" without a `std::` in sight.

---

## 9. The open questions

Things I could not settle from the sources, in rough order of how much they
would change the design:

* **Does the C++ side want py2c's `tostr`/`eq`/`addfn` slots, or just the four
  RTTI fields?** Carrying all of them keeps the descriptors literally identical
  and makes the digest trivial; carrying four keeps `.rodata` smaller for
  baremetal. The layouts only need to agree on `{name, base}` for
  `isinstance_of` to work across both — the header comment at `py2c.py:179` says
  as much — so a shorter C++ header is *sound*, but then the digest has to
  describe which shape a class uses. I lean toward identical, with the pruning
  as a later `-Os` concern.
* **Destructor slot naming.** cpprust reserves `_DTOR_SLOT` (line 3013) as a
  name that cannot collide with a method; py2c has no destructor concept at all,
  since it is arena-allocated with no refcounting or GC. A C++ class held by an
  rpython one therefore never gets destroyed. Is that acceptable (arena frees it
  all in one shot, which is the documented model), or does an owning rpython
  field need to call `T_drop`? This is the one place where the two memory models
  genuinely disagree rather than merely differing in spelling, and it should be
  decided before step 4, not during it.
* **Pointer compression.** `SHIVYC_PCOMPRESS` makes class-pointer fields 32-bit
  (`py2c.py:184`). A cpprust `_base` member is a real embedded struct, not a
  pointer, so it is unaffected — but a C++ class holding an rpython class *by
  pointer* would need `PTR_PACK`/`PTR_UNPACK` at the boundary. Probably just
  "the digest records whether a field is packed," but it needs checking against
  a real build.
* **Does `#include "some.py"` from a `.cpp` need py2c on the path at C++
  lowering time?** Today a `.cpp` include needs python3 and cpprust.py on disk;
  this would add py2c to that set. Acceptable, but it should be a deliberate
  entry in "What a translation costs" rather than a surprise.
