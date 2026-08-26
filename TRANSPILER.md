# The ShivyCX Python→C transpiler (`tools/py2c.py`)

`py2c.py` is a **specialized** source-to-source translator that emits C from the
ShivyCX compiler's own Python source. It is not a general-purpose Python→C
compiler: it understands only the subset of Python that ShivyCX's front end is
written in, and it leans on that narrowness to produce small, readable, idiomatic
C. The end goal is to compile the front end with itself — a ShivyCX-in-C that is
smaller and faster than the `gcc`-built Python interpreter path.

This document explains the object model, the type-inference strategy, the
cross-module machinery, and the conventions (a few small, deliberate
"transpiler-friendly" annotations in the Python source) that make the whole thing
tractable. It also describes the verification methodology, which is the part that
keeps the project honest: **every feature is backed by a test that proves the C
behaves byte-for-byte like the Python it came from.**

The guiding rule throughout is **correctness over coverage**. The transpiler
never emits a silently-wrong stub to make a file compile. If a construct cannot
be translated faithfully, it is left to fail loudly rather than to produce C that
diverges from the Python.


## 1. The three-tier object model

Python is dynamically typed; C is not. The transpiler bridges this with three
representational tiers, and it tries to keep values in the *highest* (most
concrete, most efficient) tier that can be proven safe.

**Tier 0 — concrete C scalars.** When a value's type is known and primitive, it
is emitted as a plain C scalar: `int`, `bool`, `char*`, `long`. A loop counter, a
size, a flag — these never get boxed.

**Tier 1 — concrete class pointers.** Each Python class becomes a C `struct` with
a common header as its first member:

```c
struct Obj { const void* type; };          /* every object starts with this */

typedef struct ILValue {
    Obj      _hdr;          /* type tag -> TypeInfo, enables dispatch & isinstance */
    CType*   ctype;
    obj      literal;
} ILValue;
```

Because the `Obj _hdr` is first and `full_fields()` lays base-class fields ahead
of subclass fields, a `Base*` and a `Derived*` agree on the offset of every field
they share — so an upcast is a no-op pointer cast, exactly as in C++.

Alongside each class the transpiler emits a per-module **`TypeInfo`** record
holding the class name, a pointer to the base's `TypeInfo`, and the class's
virtual-method slots. The `type` field in `Obj` points at this record; it is what
makes runtime dispatch and `isinstance` work.

**Tier 2 — the tagged `obj` word.** When a value's static type genuinely cannot
be pinned down (heterogeneous containers, `getattr`, a `dict`'s values), it falls
back to a tagged union:

```c
typedef struct {
    unsigned char tag;                       /* T_NONE, T_INT, T_BOOL, T_STR,
                                                T_OBJ, T_LIST, T_DICT, T_FUNC */
    union { long i; str s; Obj* o; } u;
} obj;
```

The runtime (`shivyc_rt.h`/`.c`, emitted next to the output) provides the boxing
and unboxing macros — `OBJ_INT`, `OBJ_STR`, `OBJ_OBJ`, `OBJ_BOOL`, `OBJ_NONE`,
`AS_OBJ`, `AS_INT`, `AS_STR`, `IS_NONE`, `TYPE(o)` — plus `truthy(obj)` for
Python truthiness and `OBJ_ISINST(v, t)` for `isinstance`. The runtime compiles
`-Wall`-clean.

**`%` is string formatting on a string.** `fmt % args` where the left operand is
a string lowers to `str_mod` (a printf-style formatter), not the arithmetic
`obj_mod`; a tuple right-hand side spreads into multiple arguments. f-strings
lower to `pyfmt_a`. Both take arguments through a pointer (see below) and size
their output to the arguments. See the `formatting` example.

**Constructors used as values unbox every argument.** A class used as a value
emits a `Cls__ctortramp` that unpacks a runtime arg list and unboxes each entry to
the constructor's C parameter type: `int`->`AS_INT`, `bool`->`truthy`,
`double`/`float`->`as_dbl`, `char*`->`AS_STR`, pointer->`(T)AS_OBJ`. See the
`ctorval` example. (A backend fix rides along: the controlling expression of a C
`switch` is now integer-promoted, so `switch` on a one-byte tag -- e.g. inside
`truthy` -- compares correctly.)

**More container/number builtins.** `bool(x)` yields a normalized 0/1 value (not
just a truthy-in-context expression); `[x]*n` / `n*[x]` repeat a list; `divmod`,
`list.count`, and `list.reverse` are supported (reverse was previously a silent
no-op). Slices take a step, including negative (`xs[::-1]`, `xs[::2]`,
`xs[a:b:c]`), via `py_slice_step`.

*Known divergence from CPython:* integer `//` and `%` use C truncation, so for
negative operands the sign differs from Python's floored result (e.g. `-7 % 3` is
`-1`, not `2`). Avoid relying on it in rpython sources.

**Profile-guided auto-typing (`-fprofile-generate`).** Where static
inference can't see a container's element type (e.g. it is filled from a function
call), `tools/rpy_pgo.py` profiles the running script to discover the real types.
It instruments the source with per-mutation type probes, bounds every loop to a
small iteration budget so profiling is fast, runs it in a subprocess that dumps
observed types to JSON, then rewrites the original source with concrete
`name: "list[int]"` / `dict[str,int]` annotations (gated by the same escape/usage
analysis as static promotion) for py2c to lower. Triggered by the
`-fprofile-generate` flag or `RPY_PROFILE_GENERATE=1` (which also flows through
the ShivyCX front end); best-effort, so any profiling failure falls back to the
unchanged source. `make testpgo` checks boxed == profile-guided. Three refinements: it **merges** evidence -- a container the run actually populated is typed from the observed types (which also *veto* a statically-clean-looking but at-runtime-mixed container), while a container on a cold path the run never reached falls back to static inference; it profiles **multi-file** programs as a set (one run instruments every module via a shared probe, so cross-module containers are typed); and `-fprofile-use[=PATH]` replays a cached profile written by `-fprofile-generate[=PATH]` without re-running. See the `pgo`
example.

**Opt-in promotion to the unboxed form.** With `PY2C_PROMOTE_CONTAINERS=1`,
an unannotated empty `list`/`dict` whose element (or key/value) types infer to a
single scalar, and whose every use is supported by the typed form, is rewritten
to the unboxed `list[int]` / `dict[str,int]` representation automatically (the
inference even sees through the self-referential read in the `d[k] = d[k] + 1`
counter idiom). It is conservative: any escape (return, pass as arg, alias,
store-in-container), unsupported method, slice, or negative index leaves the
container boxed, and promotion never changes observable behavior. Off by default;
`make testpromote` checks behavior-preservation against CPython. See the
`promote` example.

**Untyped containers are inferred and advised.** An unannotated empty
container (`x = {}` / `[]` / `set()`) compiles to a boxed container and works as
written. The transpiler also infers its element (or key/value) type from use --
literals, arithmetic, `len`/`int`/`str`/... calls, `d.get(k, default)`,
constructors, and `for` variables over `range`/strings/homogeneous literals --
and prints a stderr advisory: a suggested `name: "list[int]"`-style annotation
for the unboxed fast path, a note that it stays boxed (e.g. `dict[str, obj]`), or
a warning when element types are mixed (rpython containers should be
homogeneous). Advisories never change codegen; silence them with
`PY2C_NO_CONTAINER_WARN=1`. See the `untyped` example.

**`dict` has two forms.** A general dict is a first-class runtime value
(`T_DICT`): an insertion-ordered array of boxed key/value `obj`s with linear
`obj_eq` lookup, so keys/values may mix scalar types. It supports literals and
`{k: v for ...}` comprehensions, `d[k]` read/write and `d[k] += …`, `get`,
`setdefault`, `pop`, `update`, `clear`, `copy`, `del d[k]`, `in`, `len`,
`keys`/`values`/`items`, direct key iteration, `|` merge (right wins), and
order-independent `==`. For hot paths, annotating `d: "dict[K, V]"` with scalar
`K`/`V` lowers to unboxed parallel arrays (`{K* keys; V* vals; long len, cap;}`)
with no boxing or hashing runtime -- a missing key reads as 0. See the `dictops`,
`wordfreq`, and `dicts/typed_dict` examples.

**`set` is a real type.** Sets carry their own runtime tag (`T_SET`) — a `List`
whose tag marks it as a set — rather than sharing the list representation. `|`
`&` `-` `^` are union / intersection / difference / symmetric-difference,
dispatched on the tag inside `obj_bin`/`obj_sub` (so they stay bitwise on
integers); set literals, `set(iterable)`, and set comprehensions de-duplicate;
equality is order-independent; `{...}` prints with braces and the empty set as
`set()`; `in`, `add`, `discard`, `remove`, `clear`, and iteration all work. See
the `sets` example.

**Set literals de-duplicate.** A `{a, b, ...}` literal lowers to `set_from`,
which is `list_from` minus elements already present (by `obj_eq`), so
`len({1, 1, 2})` is 2. (Sets are still backed by the list representation.)

**No obj through C varargs.** The tagged `obj` is a 16-byte struct, and passing
a 16-byte struct by value through a `...` parameter mis-lowers on the
self-compiled backend (only the first variadic argument survives). So aggregate
construction never uses varargs: list/tuple/set/dict literals, dynamic-call
argument packing, and the pair-building inside `enumerate`/`zip` store their
`obj` values into a stack array and pass a pointer to a non-variadic helper
(`list_from`, `call_obj_a`, `dict_of_a`, `list_pair`). See the `aggregates`
example.

**Memory.** Everything is allocated from a single bump arena (`aalloc`); there is
no garbage collector. This matches a compiler's lifetime profile — allocate
freely during a compile, drop the whole arena at the end — and removes refcount
traffic from the generated code entirely.


## 2. Type inference

The translator's quality is almost entirely a function of how often it can prove
a Tier-0 or Tier-1 type and avoid boxing. The relevant entry points are
`value_ctype` / `static_type` (what C type does this expression have?),
`guess_from_value`, `infer_from_name`, and `arg_ctype` (what is a parameter's
type?). Signals it uses, in rough priority:

- **Annotations.** A parameter, field, or return annotation is authoritative.
- **Literals and operators.** `len(x)` is `int`, a string literal is `char*`, a
  comparison is `bool`, and so on.
- **Field declarations.** A class's fields are discovered from class-level
  annotations and annotated assignments (`discover_fields`).
- **Usage.** If a parameter is indexed or iterated it is treated as a container;
  if attributes are read off it (and they are not known string/list/dict
  methods), it is treated as an object (`_param_used_as_container`,
  `_param_used_as_object`).
- **Flow facts.** `isinstance` narrowing (see §5) supplies a concrete type within
  the guarded region.

When a base expression is already a concrete class pointer, attribute access
chains stay concrete: given `self.output` typed `ILValue*`, the access
`self.output.ctype` lowers to `self->output->ctype` (type `CType*`), and a
further `.size` lowers to `->size` (type `int`). This chained resolution lives in
both `static_type` (so coercion/boxing stays consistent) and `ex_Attribute` (so
the emitted text matches).

### Bit-field struct members — the `T(N)` annotation

A field annotated `T(N)` (e.g. `int(3)`) is stored as a C bit field of `N`
bits, so several small values pack into one machine word and a struct stays off
the heap / small in the cache — the representation trick TPython uses for speed:

```python
class PackedData:
    def __init__(self, a: int, b: int, c: int):
        self.is_active  : int(1) = a    # unsigned int is_active  : 1;
        self.status     : int(3) = b    # unsigned int status     : 3;
        self.error_code : int(4) = c    # unsigned int error_code : 4;
```

The syntax reads as "a `T` value that occupies `N` bits". Mechanically:

- `ann_text_to_ctype` strips the `(N)` and resolves the annotation to the plain
  scalar `T`, so every *read* and *write* of the field behaves as an ordinary
  `T` (no boxing, no special call sites). Field arithmetic, assignment, and
  comparison are unchanged.
- The width `N` is recorded separately, per class, by `discover_bitfields`
  (`ClassInfo.bitfields: {field -> N}`). It is consumed *only* at struct
  emission, where the member is written `unsigned int <field> : N;` at both
  struct-emission sites instead of `T <field>;`.
- Because `offsetof` is illegal on a bit field, such fields are omitted from the
  per-type `FieldDesc` table, so they are not reachable through reflective
  `rt_getattr`/`rt_setattr`. Fields used as bit fields are meant for the hot,
  statically-typed path. A bit-field class that is only constructed and has its
  members accessed directly stays POD (no header, no field table), which is the
  intended shape for these small packed structs.

Semantics follow C, not Python: a value wider than `N` bits keeps only its low
`N` bits (a 3-bit `status` stores `12 & 7 == 4`), whereas CPython would keep
`12`. Within range — the intended use, e.g. a 4-bit tag holding 0..15 — the
native and CPython results agree. Mixing bit fields with ordinary fields in the
same class is fine; only the annotated members are packed. No existing rpython
uses the `T(N)` form, so adding it changed no existing output and self-hosting
is unaffected.


## 3. The annotation convention for IL commands

ShivyCX's IL-command classes (`il_cmds/`) store operands named `output`, `arg`,
`val`, `cond`, `func`, `ret` — every one of them an `ILValue`. The methods that
make assembly read those operands' C types constantly: `self.output.ctype.size`,
`self.arg.ctype.signed`, `self.func.ctype.arg.ret.is_void()`. For the transpiler
to lower those chains to struct member accesses rather than runtime attribute
lookups, it must know the fields are `ILValue`.

The complication is an import cycle: `il_gen` (which defines `ILValue`) imports
the IL-command modules, so those modules cannot import `il_gen` back at runtime.
The convention that resolves this is the standard typing idiom:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:                       # never executed at runtime -> no cycle
    from shivyc.il_gen import ILValue   # the transpiler reads it statically

class _ValueCmd(ILCommand):
    output: "ILValue"                   # forward-ref annotations type the fields
    arg:    "ILValue"
    val:    "ILValue"
```

At runtime `TYPE_CHECKING` is `False`, so Python never imports `il_gen` from a
command module — the cycle is broken. The transpiler, which reads the source
statically, *does* see the `if TYPE_CHECKING:` block and records the import, so
the forward-ref annotations resolve to `ILValue*` fields.

Two transpiler features make this self-contained:

- `if TYPE_CHECKING:` blocks are recognized and emit no C (they are type-only).
- `load_xmod` descends into `if TYPE_CHECKING:` blocks when collecting a module's
  imports, so a command module's registry knows `ILValue` comes from `il_gen`
  even though the import is guarded.

A handful of matching annotations on the type hierarchy complete the picture:
`ILValue.ctype: CType`, `PointerCType.arg: CType`, `FunctionCType.ret: CType`.
With those, the whole `self.func.ctype.arg.ret.size` family of chains lowers to
plain struct accesses. These are the only Python-source edits needed; they are
ordinary type hints that also help human readers and static checkers.


## 4. Downcasting to a field's owning subclass

The annotations above type `ILValue.ctype` as the *base* `CType`. But ShivyCX
routinely reads subclass-specific fields off a base-typed value — `ctype.signed`
(only on `IntegerCType`), `ctype.arg` (only on `PointerCType`), `ctype.ret` (only
on `FunctionCType`) — because the surrounding code already knows, from context,
which kind of type it holds. In Python this is just attribute access; in
statically-typed C, `((CType*)p)->signed` does not compile because `CType` has no
such member.

The transpiler handles this with a **downcast to the field's owning subclass**
(`_field_owner_subclass`): when a field is read through a `Base*` and `Base` does
not declare it but exactly one subclass in `Base`'s subtree does, the access is
emitted against that subclass:

```c
((IntegerCType*)(self->arg->ctype))->signed_   /* CType* downcast to read .signed */
```

This is the faithful translation of what the Python means: the code assumed an
`IntegerCType`, so the C asserts the same by casting. The uniqueness requirement
(exactly one owning subclass) keeps the cast unambiguous; if two subclasses
declared the field, the transpiler would not guess. (`signed` is a C keyword, so
the field is mangled to `signed_` by `cname`, which rewrites C keywords and
runtime type names.)


### Bridge-free dynamic attribute access — `rt_getattr` / `rt_setattr`

The downcast above resolves an attribute *statically*, when the field's owner can
be proven. When it cannot — a `getattr(obj, key, default)` / `setattr(obj, key,
val)` whose key is computed at runtime, or whose receiver is a tagged Tier-2
`obj` of unknown concrete type — the access is dispatched at runtime through a
**per-type field table**, with no micropython core involved.

Every object-model class already carries a `TypeInfo` (reached via the `Obj`
header's `type` pointer). Each `TypeInfo` additionally points at a `FieldDesc`
array describing the class's fields — a name, a byte offset, and a 1-char storage
code:

```c
typedef struct FieldDesc { const char* name; long off; char tc; } FieldDesc;
/*  i=int  l=long  b=bool  d=double  f=float  s=char*  o=obj  p=Obj*  */

static const FieldDesc ILValue__fields[] = {
    { "ctype",   offsetof(ILValue, ctype),   'p' },
    { "literal", offsetof(ILValue, literal), 'o' },
    { NULL, 0, 0 }
};
```

`rt_getattr(recv, name, dflt)` casts `recv`'s `TypeInfo` to the shared
`TypeInfoHdr` (whose first three members — `name`, `base`, `fields` — are common
to every module's `TypeInfo`), walks the field table and the base chain, and on a
name match reads the field directly at its offset and boxes it per the storage
code; an absent field yields `dflt`. `rt_setattr` is the mirror: unbox and store.
Both live in `shivyc_rt.c`, so a program that uses only object-model dynamic
attributes **links without the micropython bridge** (`mp_getattr` lives in the
separate bridge translation unit and is only emitted for stdlib-porting mode).

The two lowerings are complementary: when the receiver's *static* struct type is
known the transpiler inlines the field-selection `switch` at the call site (see
the `dynattr` example); when it is a boxed `obj` it emits an `rt_getattr` /
`rt_setattr` call against the runtime field table (the `rtattr` example). Module
references (`getattr(some_imported_module, attr)`) are excluded — a module is not
an object value — and fall back to the default.

The table is built from a class's *declared* fields (those assigned on `self`).
An attribute that is instead written through a *different* receiver — a
configurator doing `target.attr = …` or `setattr(target, "attr", …)` — is also
captured, **provided the receiver's class is statically known**: from a
`var = Cls()` binding or a parameter annotation (`target: "Widget"`). A pre-pass
(`discover_fields_from_ctor_locals`) walks top-level functions and method bodies,
and promotes each such `attr` to a field on the receiver's class, which its
subclasses then inherit. This is what lets a cross-class dynamic write land in a
real slot instead of being dropped by `rt_setattr` — see the `crossattr` example.

The promoted field's type is `obj` by default, but is inferred when every write
to it assigns a direct constructor result of one local class — `target.style =
Style(3)` makes `style` a real `Style*`, so it can be used as a typed pointer
(`b.style.inset()` becomes a direct call) rather than a boxed obj. Any
disagreement between sites, or any non-constructor right-hand side, keeps the
field `obj`.

Two cases remain out of reach. A receiver that stays an untyped `obj` at the
write site gives no class to attribute the field to. And the type inference
fires only for a direct local-class constructor on the right-hand side; a value
reached indirectly (e.g. `target.x = self.y`) still lands as `obj`.


## 5. `isinstance` narrowing — blocks and `and`-chains

`isinstance` is the front end's main type discriminator, so narrowing it well
pays off everywhere. Two forms are supported.

**Block narrowing.** Inside `if isinstance(x, T):` the variable `x` is treated as
`T*` for the extent of the block, and restored afterward (narrowing is
block-scoped; `st_If` saves and restores the narrowing map). An `and`-chain of
`isinstance` tests in the condition narrows all of them.

**`and`-chain narrowing.** A common ShivyCX idiom guards a field access on the
same line as the check:

```python
if isinstance(slot, LiteralSpot) and not (-(2**31) <= int(slot.value) < 2**31):
    ...
```

Here `slot.value` must see `slot` already narrowed to `LiteralSpot`, even though
no block has opened yet. `ex_BoolOp` renders an `and`-chain left-to-right and,
before rendering each operand, applies the narrowings implied by the operands to
its left — so the second operand's `slot.value` resolves against `LiteralSpot`.
The narrowings are unwound when the boolean expression finishes. (`or` and
negation contribute nothing, matching Python's short-circuit semantics.)


## 6. Cross-module translation

ShivyCX is many modules, and a faithful translation must dispatch and lay out
data the same way across them. The relevant pieces:

- **Import resolution.** `import_alias` and `from_imports` map names to defining
  modules. `load_xmod` parses an imported module into a registry of its classes,
  functions, singletons, vtables, declaration order, imports, constants, and
  globals. The result is cached in a process-global `_XMOD_CACHE`, with a cycle
  guard so mutually-importing modules terminate.
- **`xclasses`.** Every class of every imported module is registered as
  `name -> (ClassInfo, module)`, with base links resolved across modules so
  ancestor walks work. `build_externs` emits `extern` declarations and the
  forward typedefs / struct bodies an importing `.c` needs.
- **Method dispatch.** A method on an imported receiver resolves through several
  strategies, most-specific first: `resolve_xmethod_owner` devirtualizes a call
  when a single module defines the method; `resolve_xvirtual` issues a polymorphic
  call through a replicated `VT_<mod>` vtable struct (`xvcall`); and a shared
  base vtable handles a class hierarchy split across modules. The dispatch gate
  fires for any obj-typed *or* concrete-class-pointer receiver.
- **First-class functions.** Functions used as values become `T_FUNC`-tagged
  `Closure`s; `make_closure`, `call_closure`, and `call_obj` plus per-function
  trampolines let a function pointer be stored, passed, and invoked dynamically.
- **Class-level attributes and statics.** Class-level scalar attributes become
  instance fields; class statics become module-level globals.
- **Nested functions** are lifted to module scope (closing over captured names
  explicitly) before emission.

### Struct-dependency closure and on-demand module loading

A subtle cross-module problem: a module may use a class (say `value_cmds.AddrOf`)
whose struct body now contains an `ILValue*` field, and `ILValue`'s body in turn
names `CType*` — yet that module never imported `il_gen` or `ctypes` itself. The
typedefs for `ILValue` and `CType` must still be emitted, in order, before the
struct that needs them.

The transpiler computes the **transitive closure of field-type dependencies** of
every struct it must lay out, and **loads missing classes on demand**: it follows
the importing module's own imports to find where a referenced class is defined,
loads that whole module (so sibling subclasses are available for the downcasts of
§4), and emits a forward typedef. When even the import graph does not name the
class directly — e.g. `getattr(v, "ctype", None)` is inferred as `CType*` from the
uniquely-owned `ctype` field — a reachable-module search (`_load_xclass_anywhere`)
locates and registers it so its body can be emitted. Forward typedefs are always
ordered ahead of the struct bodies that reference them.


## 7. Verification methodology

The transpiler is validated two ways, and the second is the one that matters.

**Compile-clean count.** Every generated `.c` is compiled with
`gcc -c -O2`; the headline metric is how many of the front end's top-level
modules compile with zero errors, and the total error count across the rest.
This is a coarse progress signal, not a correctness proof.

**Byte-identical behavior harnesses.** For each feature, a small driver runs the
*transpiled C* and the *original Python* on the same inputs and asserts the
outputs match exactly. The current suite covers, among others: spot/register
formatting, error reporting, list/dict comprehensions, `tokens.parse_c_int`,
nested-function lifting, `isinstance` narrowing, first-class functions, a class
used as a value, polymorphic class-attribute access, and a class hierarchy split
across modules. A change to the transpiler is not accepted unless **all** of
these still pass, because the whole point is that the C *is* the Python.

These harnesses are what justify the "correctness over coverage" rule: it is
always better to leave a construct uncompiled than to pass the compile-clean
count while quietly failing a behavior harness.


## 8. Style guide: don't shadow `copy` / `copy.copy()`

The transpiler lowers `x.copy()` differently depending on what `x` is. On a
builtin container it is a shallow copy (`pycopy`, i.e. `list`/`dict`/`set`
`.copy()`); on a class instance it is a method call. At an **untyped-obj**
call site the static type is `obj`, so the only way to tell the two apart is a
runtime type-switch on the receiver's `TypeInfo`. That switch is reliable only
when `copy` names exactly one thing across the project.

So the rule is: **if your project imports the stdlib `copy` module and calls
`copy.copy()`, do not also define a method named `copy` on your classes.** The
two meanings collide, and an untyped `recv.copy()` can no longer be
disambiguated with full confidence — this pattern is only partially supported.
It is most likely to bite when the `copy`-defining classes are roots of
separate, unrelated hierarchies (each in its own module, so the shared name
sits at a different vtable slot per module).

The fix is a one-line rename to an unambiguous, intention-revealing name, after
which every remaining `.copy()` in the project is unambiguously a container
shallow copy:

```python
class NodeGraph:    def copy_node(self)  -> "NodeGraph": ...
class ILCode:       def copy_code(self)  -> "ILCode":    ...
class State:        def copy_state(self) -> "State":     ...
```

The transpiler detects the offending combination — a project that uses
`copy.copy()` *and* defines one or more `copy` methods — and prints a stderr
advisory naming each offending class (e.g. `WARNING: ILCode defines a method
named 'copy', but this project also uses the stdlib 'copy' module ...`). The
advisory never changes the generated code; it only points at the classes to
rename.

## 9. Stdlib lowering, and the rule about guessing

A growing set of stdlib calls lower to C shims rather than being left to the
`is not lowered; substituted None` path. `regex` is documented separately in
[REGEX.md](REGEX.md); the shape of the rest:

| Call | Lowers to |
|---|---|
| `subprocess.run(argv, capture_output=, text=, cwd=)` | `fork`/`execvp` with both pipes drained by `poll()` |
| `tempfile.mkdtemp(prefix=)` | `mkdtemp(3)` under `$TMPDIR` |
| `re.*`, match `.group()`/`.start()`/`.end()` | the `crust_re` engine, or a specialized matcher |

**Partial support is refused, not approximated.** `subprocess.run` lowers only
for `capture_output`, `text` and `cwd`. `check=` would have to raise, and
`env=`/`stdout=`/`stderr=` change the plumbing, so a call using them keeps
warning instead of having the option silently dropped. Likewise `capture_output`
must be a literal `True`, because the emitted call has to know at translation
time whether the pipes exist.

That is the general rule for this area. The failure mode being avoided is not a
missing feature — it is a program that compiles, runs, and quietly does
something other than what the Python did. `py2c` already warns and substitutes
`None` rather than failing, so the transpiler is only as trustworthy as its
willingness to say no.

Two shim details that cost real debugging and are easy to repeat:

- A shim returning a **string** should return `char*`, not `obj`. Returning
  `obj` from `mkdtemp` made a comparison against its result lower to a pointer
  compare between an `obj` and a `char*` — no error, wrong answer.
- A shim's return type must be registered in `value_ctype`, or callers default
  to `int` and `str()`/`join()` of the result fails to compile. Match
  `.start()`/`.end()` return `long`; without that, `m.end() - 1` lowered to
  `obj` arithmetic on a `long`.

Both pipes of a captured `subprocess.run` are drained together with `poll()`.
Reading `stdout` to EOF first deadlocks the moment the child fills `stderr`,
which is why the test captures 5000 lines rather than a greeting.

## 10. Layout and running it

The transpiler is a single file, `tools/py2c.py`. Run from `tools/`:

```sh
# transpile the whole front end (top-level modules) into a chosen directory
python3 py2c.py --out /tmp/out

# transpile a single module (e.g. an il_cmds submodule)
python3 py2c.py --out /tmp/out ../shivyc/il_cmds/value.py
```

The runtime (`shivyc_rt.h`, `shivyc_rt.c`) is written into the output directory
alongside the translated sources.

### Status and scope

- All targeted IL-command modules — `base`, `asm`, `math`, `compare`, `value`,
  `control` — translate to C that compiles cleanly.
- The IL-command modules compile but do not yet link standalone: they reference
  parts of the back end (`asm_gen`, `ASMCode`, token-kind globals) that are not
  transpiled yet, so a clean compile is the current bar for them.
- `cache.py` depends on `os`/`pickle` and is intentionally out of scope.
- The `tree/` and `parser/` packages are not yet attempted.

The architecture is deliberately incremental: each new construct is a small,
local change to `py2c.py` plus a behavior harness, and the cross-module and
type-inference machinery described above is what lets those local changes compose
into whole-module translations.
