# MINIPY — a tiny RPython Python interpreter for ShivyCX

A plan for a small Python interpreter, written in RPython and compiled to C by
`py2c.py`, whose job is to run `py2c.py` itself plus simple Python scripts —
*without* dragging the whole MicroPython core into the repo.

The headline trick: **we don't write a parser.** RPython/py2c already has typed
JSON support, and CPython already has a perfect Python parser (`ast`). So minipy
shells out to `python3` to turn a `.py` file into a JSON AST, loads that JSON
into typed node structs, and interprets the tree. Because the entire AST is
known before execution starts, the JSON-load step doubles as an **AOT pass**:
we can pre-resolve names to integer slots and emit a script-specialized dispatch
loop, the way TPython does.

---

## 1. Goal and measured scope

North star: `minipy py2c.py hello.py` produces **byte-identical** C to
`python3 py2c.py hello.py`, and `minipy script.py` matches `python3 script.py`
for ordinary scripts (the same bar mrpy already meets against MicroPython).

The target program defines the scope. `tools/py2c.py` is ~14k lines / ~96.6k AST
nodes across **75 distinct node types**. The shape of the work, by frequency:

| Bucket | Nodes (approx) | Implication |
|---|---|---|
| Name / Attribute / Load / Store | ~62k | name + attribute lookup is THE hot path — must be slot-resolved |
| Constant | 6.2k | intern into a flat `consts[]` array |
| Call / keyword / Starred | ~5.5k | full call protocol: pos + kw + `*args` |
| Assign / AugAssign / AnnAssign | ~2.5k | incl. tuple unpacking |
| If / Compare / BoolOp / IfExp | ~5.2k | `and`/`or` short-circuit, chained compares, `in`/`is` |
| BinOp / UnaryOp (`%` = 882!) | ~1.9k | `%` string-formatting is heavily used |
| For / While / Break / Continue | ~600 | loops; fuse compare+incr where possible |
| Comprehensions (list/set/dict/gen) | ~455 | **desugar to explicit loops** |
| Try / ExceptHandler / Raise / With | ~100 | exceptions + context managers |
| FunctionDef / Lambda / Return / Global | ~1.9k | functions, closures, `global` |
| ClassDef | 8 | minimal class support (methods, `self`, single base) |
| Yield / YieldFrom | 4 | **rare** — defer real generators; refactor or thread-trampoline |

Stdlib surface actually used by py2c (measured, not guessed):

- `ast`: `parse`, `walk`, `unparse`, `NodeTransformer`, `iter_child_nodes`,
  `iter_fields`, `copy_location`, `fix_missing_locations`, plus ~70 node
  classes used for `isinstance` checks and node construction. **Central — the
  interpreter must ship a real `ast` module.**
- `os`: `environ`, `path`, `sep`, `fspath`, `listdir`, `makedirs`, `walk`
- `re`: `match` **only** (a single call site)
- `sys`: `argv`, `exit`, `path`, `stderr`
- `pathlib`, `pickle`, `shutil`, `subprocess` (per the requirement)
- builtins: `all any dict enumerate float getattr hasattr int isinstance len
  list open print range repr set sorted str tuple type zip`

This is a bounded, knowable target — not "implement CPython."

---

## 2. Non-goals (what we deliberately do NOT take from MicroPython)

We do **not** vendor the MicroPython VM, its object model (`mp_obj_t`), its GC,
its parser/compiler, or its qstr machinery. The blocking reason is
representation: **our `obj` is 16 bytes; a boxed MicroPython object is 32+ bytes**,
so the two cannot share a tagged union and cannot be passed between compiled
ShivyCX code and an mp interpreter without marshalling. mrpy embeds the *whole*
mp core precisely to avoid that mismatch; minipy goes the other way — own the
object model, stay 16 bytes, and reuse from mp only the **pure-Python,
representation-agnostic leaf modules**:

- `lib/micropython-lib/python-stdlib/pickle` — pure Python, talks only through
  the object protocol → vendorable.
- `ports/unix/lib/ast.py` — a reference for an in-interpreter `ast` node model
  / `unparse`, if we prefer vendoring over delegating.

Nothing that reaches into mp internals comes across.

---

## 3. Value representation — reuse py2c's existing 16-byte `obj`

py2c already emits exactly the object we want:

```c
typedef struct { unsigned char tag;
                 union { long i; str s; Obj* o; double d; } u; } obj;   /* 16 bytes */
```

minipy's value type **is** this `obj`. The payoff is the same one the mrpy README
gestures at under "Foundation for AOT/JIT": values created by interpreted code
and values in py2c-compiled code share a bit-for-bit representation, so a hot
function can later be lowered to C by py2c and called from the interpreter with
**zero marshalling**. That is the whole reason to stay off MicroPython.

Two cheap wins borrowed from TPython's 16-byte packing
(`tp.pyh`, the `tp_obj` union):

- **Tiny inline strings.** The current layout wastes the 7 bytes between `tag`
  and the 8-byte union. A `T_STR_TINY` tag can store ≤7 (or, by overlapping the
  union, ≤15) chars *inline*, so identifiers and short dict keys never touch the
  heap or GC. Most Python hot data is short strings; this is a large constant
  factor.
- **A wider type tag for AOT-specialized types.** TPython's `enum TPTypeID`
  (`tp.pyh:144`) carves out IDs for `VEC2/VEC3/...` and even user objects so the
  VM can dispatch on a 6-bit id instead of chasing a class pointer. We don't need
  geometry types, but the *mechanism* matters: when the AOT pass sees the user's
  classes it can assign each a small type-id and emit type-specialized attribute
  access (see §6).

We keep py2c's tags (`T_INT T_FLOAT T_STR T_LIST T_DICT T_SET T_OBJ T_NONE
T_BOOL`) and add interpreter-only ones (`T_STR_TINY`, `T_FUNC`, `T_CLASS`,
`T_BOUND`, `T_CELL` for closures, `T_NODE` for AST nodes when the program
manipulates its own tree).

---

## 4. The front end: flattened bytecode, not an AST tree

minipy does **not** consume a raw AST. A tree would force the interpreter to walk
a deep, irregular structure and make it hard to generate. Instead the CPython
side (`rpy.py` → `tools/minipy/compiler.py`) lowers the AST to a **flat,
register-based bytecode** over uniform 4-int records `(op, a, b, c)` — the exact
shape a fast dispatch loop wants (TPython's `tp_step`). It serialises to JSON
whose objects map one-to-one onto rpython POD classes, so
`rpy.json.generate_decoder` unpacks it straight into structs.

### 4.1 The bytecode format (`py2json_bytecode`)
```
{ "version", "source",
  "consts":   [ {"t","i","d","s"}, ... ],   # interned, tagged literals/funcs/builtins
  "names":    ["print", "fib", ...],         # global/name slot table
  "nglobals": N,
  "funcs":    [ {"name","nparams","nregs","code":[{"op","a","b","c"}, ...]}, ... ],
  "entry":    0 }                            # module body = func 0
```
Locals (params + assigned names) are fixed low registers; temporaries form a
register stack above them; module-level names are global slots. Opcodes are
banded so AOT-specialised variants get their own range (60-89), exactly like
TPython. Implemented + validated against `python3`: arithmetic, compares,
`and`/`or`, `if`/`while`/`for`-in-`range`, `break`/`continue`, top-level `def`
(positional, recursive), calls, and `print/len/range/int/str/float/abs/bool`.

### 4.2 AOT specialisation, already firing
Because the whole script is in hand, the compiler tracks statically-known numeric
registers — from int/float literals, `*_NN` results, and **`int`/`float`
parameter annotations** propagated across moves — and emits the no-tag-check fast
ops `ADD_NN`/`SUB_NN`/`MUL_NN`/`LT_NN`/… on hot paths. E.g. `def fib(n: int)`'s
inner loop lowers its compare to `LT_NN` and both adds to `ADD_NN`. This is the
TPython op-64/op-80 idea, made concrete. Slot-resolved globals (flat
`globals[]`/`consts[]`) are the array-index analog of `__global_objects__[]`.

### 4.3 The driver: `python3 rpy.py S.py` (no shell-out from the interpreter)
Calling the shell *from inside* the interpreter is unclean, so orchestration
lives on the CPython driver side instead. `python3 tools/rpy.py S.py [args]`:
1. hashes `S.py` **and the project `.py` files it imports** (md5) for the cache key;
2. reuses `/tmp/<md5>.minipy.json` if fresh, else compiles + caches it;
3. executes, forwarding argv; `-i` then drops into a REPL; `-B` forces rebuild.

Today execution uses the CPython **reference VM** in `compiler.py` (faithful
oracle, runs untranslated like the rest of the repo). The documented seam in
`_load_or_build` is where `backend="native"` will AOT-generate a *per-script*
specialised rpython interpreter and py2c-compile it to `/tmp/<md5>.interp.bin` —
the slow step the md5 cache exists to amortise.

### 4.4 Why it's still an interpreter (the `-i` REPL)
Even when the binary is AOT-built for one script, it keeps a generic core so
`python3 rpy.py -i S.py` works like `python3 -i S.py`: run the script, then read-
eval-print interactively against its globals. (v0 REPL shares values by name and
echoes expression results; calling script-defined functions from a later REPL
line awaits the incremental compiler, since a func's index is program-local.)

### 4.5 `ast` for py2c itself
When the script *is* `py2c.py`, it calls `ast.parse`. minipy's own `ast` module
reuses this same front end (parse delegated to a `python3` subprocess returning
the flattened form, or vendored), with `unparse`/`NodeTransformer`/`walk`/
`iter_*` pure-Python. One mechanism serves both layers.

---

## 5. Execution: two staged engines

### Stage 1 — tree-walker (correctness first)
A straight recursive evaluator over the typed node structs: an environment is a
dict of `obj`, `eval_expr(node)->obj`, `exec_stmt(node)`. Control flow
(`Break`/`Continue`/`Return`) via sentinel return codes or RPython exceptions.
Comprehensions are **desugared** into explicit loops that build a `List`/`Dict`/
`Set` — no generator machinery needed. This alone should run py2c.py end-to-end;
it is the reference the fast engine is differentially tested against.

### Stage 2 — generated specialized dispatch (TPython's idea, the speed path)
Because the whole AST is in hand before we run, a codegen pass lowers it to a
flat instruction stream over register/slot arrays and emits a `minipy_step()`
loop with **specialized opcodes**, exactly like `tp_vm.pyc++:tp_step`:

- Globals and constants live in flat arrays `globals[]` / `consts[]`
  (TPython's `__global_objects__[]` / `__const_numbers__[]`), indexed by a small
  int assigned at AOT time — O(1), no dict probe.
- When the AOT pass can prove operand types (literal numerics, annotated args,
  obvious loop counters), it emits a **typed fast op** that skips tag-checking
  and boxing — TPython's op 64 (`RA.number.val = b.number.val + c.number.val`),
  op 80 (fused compare-and-increment for `while i < N`).
- Names/attributes are **resolved to slots at AOT time** — an inline cache that
  is correct by construction because the binding is static in the JSON.

Stage 2 is additive; Stage 1 stays as the fallback for any node kind the fast
engine doesn't specialize, and as the differential oracle.

---

## 6. The AOT specialization pass (what "JSON known up front" buys us)

A pre-pass over the typed AST that runs once and annotates/lowers:

1. **Slot allocation** — every module global, every constant, every local per
   function frame → a dense integer index. Emits `globals[N]`, `consts[M]`,
   per-frame `regs[K]`.
2. **Name resolution** — each `Name`/`Attribute` load/store rewritten to a slot
   op (`LOAD_GLOBAL_SLOT`, `LOAD_FAST`, `LOAD_ATTR_SLOT`) — no string lookup at
   run time.
3. **Type hints** — propagate literal/annotation types to pick `ADD_NUM` vs
   generic `ADD`. Conservative: unknown ⇒ generic op (Stage-1 path).
4. **User-class type-ids** — assign each `ClassDef` a small id (à la `TPTypeID`)
   and lay its instance attributes out in a fixed vector so `obj.attr` is an
   index, not a dict probe. This is the "generate an interpreter with faster
   lookup/dispatch for the user's classes" idea, made concrete.
5. **Comprehension desugaring** and **`%`-format precompilation** (parse the
   format string once, emit a typed writer).

Output is either an in-memory program the Stage-2 loop walks, or — taken further
— generated RPython source that py2c compiles, i.e. a script-specialized
interpreter binary.

---

## 7. Standard library plan

Scope is small and measured (§1). Strategy per module:

- **`ast`** — node classes + `walk`/`iter_*`/`copy_location`/
  `fix_missing_locations`/`NodeTransformer` pure-Python; `parse` via the §4
  bridge; `unparse` vendored. Highest-priority module.
- **`os` / `os.path`** — thin shims over C bridges. `listdir`, `makedirs`,
  `walk`, `environ`, `sep`, `fspath` need small `rpy_lib` C helpers
  (`opendir`/`readdir`, `mkdir`, `getenv`). File read/write already exists in the
  py2c runtime (the `fopen`/`fread` loop mrpy uses).
- **`re`** — only `re.match` is used. Cheapest correct option: shell `re.match`
  to `python3` like the AST bridge; or vendor a tiny anchored matcher. Flag.
- **`sys`** — `argv` (forward the real argv — note mrpy's open limitation here),
  `exit`, `path`, `stderr`.
- **`pathlib`** — a small `Path` over `os.path` (join, exists, parent, name,
  suffix, `read_text`). ~120 lines pure Python.
- **`pickle`** — vendor mp-lib `python-stdlib/pickle`; confirm it leans only on
  the object protocol (`__reduce__`/`__dict__`) and not CPython C internals.
  py2c uses pickle for its cache, so dump/load over our `obj` is enough.
- **`shutil`** — `copy`/`copytree`/`rmtree`/`which` over the os bridges.
- **`subprocess`** — `run`/`check_output` over C `popen`/`system` (the same
  primitive the AST bridge needs anyway).
- **builtins** — the ~22 listed; most are a few lines each over List/Dict/Str.

Rule of thumb: vendor pure-Python leaf modules, bridge OS calls to thin C, and
delegate the two genuinely-hard things (parsing, one regex) to `python3`.

---

## 8. Known-hard corners (call them out early)

- **Generators / `yield`** — 4 sites in py2c. Options, cheapest first:
  (a) refactor those few sites in py2c to eager lists; (b) implement
  `GeneratorExp` only via desugaring (covers comprehensions, not real `yield`);
  (c) full coroutine support via a stackful trampoline. Start with (a)+(b).
- **`%`-formatting** — 882 uses; must match CPython for `%d/%s/%r/%x/%f/%-5s`
  etc. Precompile each format string in the AOT pass.
- **Exceptions** — need a real exception object + traceback path for `Try`/
  `Raise`/`ExceptHandler` and for surfacing interpreter errors CPython-style.
- **Tuple unpacking / `*args` / `**kwargs`** — the full call+assign protocol
  (`Starred`, `keyword`) is used; don't shortcut it.
- **`sys.argv` forwarding** — mrpy already lists this as unsolved; minipy must
  forward the real process argv into the interpreted program.

---

## 9. Bootstrapping and test strategy

Mirror what `make testfast` already does — **differential exit-code/output
comparison across engines** — but add a third party (minipy) next to CPython and
gcc:

1. **Smoke**: `minipy stdlib_demo-style.py` vs `python3` — byte-identical stdout
   (classes, `%`-format, comprehensions, `range`, `dict.values`, `try/except`,
   `__main__` guard — the exact mrpy validation set).
2. **Self-host lite**: `minipy py2c.py tests/fast/syntax_core.py` vs
   `python3 py2c.py …` — the generated C must match.
3. **Engine equivalence**: Stage-1 tree-walk vs Stage-2 specialized loop on the
   same scripts — must agree (Stage 1 is the oracle).
4. **North star**: `minipy py2c.py <self>` → identical C, i.e. py2c front end
   running on minipy. (mrpy aims at the same target on MicroPython; minipy is
   the lighter, representation-compatible route.)

Wire these as a `make testminipy` target next to `testfast`.

---

## 10. Phased milestones

- **P0 — bridge**: `python3`→JSON→typed-node decoders via
  `rpy.json.generate_decoder`; load and re-`unparse` a file, round-trip check.
- **P1 — tree-walker**: expressions, control flow, functions/closures, dict/
  list/str/set ops, `%`-format, comprehensions (desugared), exceptions. Runs the
  smoke set (§9.1).
- **P2 — stdlib**: `ast`, `os`/`os.path`, `sys`, `pathlib`, `subprocess`, then
  `pickle`, `shutil`, `re.match`. Runs `py2c.py` on a trivial input (§9.2).
- **P3 — AOT pass**: slot allocation + name/attr resolution + comprehension
  desugaring as a pre-pass feeding a Stage-2 loop with generic ops only.
- **P4 — specialization**: typed fast ops, flat global/const arrays, user-class
  type-ids, fused loop ops (the TPython speedups), guarded by Stage-1 fallback.
- **P5 — self-host**: north-star differential (§9.4); add `make testminipy`.

---

## 11. Proposed repo layout (keep it small, like the other rpy_lib shims)

```
tools/minipy/
  minipy.py            # entry: argv -> parse-bridge -> run            (rpython)
  interp.py            # Stage-1 tree-walker                            (rpython)
  aot.py               # Stage-3/4 slot alloc + specialization pass     (rpython)
  vm.py                # Stage-2 generated dispatch loop                (rpython)
  ast_bridge.py        # python3 -> JSON -> typed nodes                 (rpython)
tools/rpy_lib/
  minipy_ast.py        # ast node model + walk/unparse/NodeTransformer  (vendored)
  minipy_os.py minipy_pathlib.py minipy_subprocess.py minipy_re.py
  minipy_pickle.py     # vendored from mp-lib python-stdlib/pickle
tools/rpy_minipy_integration.py   # auto-bundle on `import minipy`, like the others
MINIPY.md              # this document
```

Total target footprint: a few thousand lines of RPython + a handful of vendored
pure-Python leaf modules — versus pulling in the multi-megabyte MicroPython tree.

---

## Status update — containers, classes, exceptions (native-validated)

Three feature tiers now run end-to-end as a py2c-compiled native binary, each
byte-identical to `python3` and to the pure-Python reference VM:

- **Container values.** list / tuple / dict / set literals; subscript get/set;
  general `for x in <iterable>` (ITER_NEW/ITER_NEXT) alongside the range
  counter fast-path; list/set/dict comprehensions (lowered to loops); membership
  (`in`/`not in`); tuple unpacking; string indexing; the common container/string
  methods; and `%`-formatting (`%s %d %05d %.2f %x %r`). Containers live in a
  side heap indexed from the value box, so the scalar path stays alloc-free.
- **Classes.** instances, `__init__`, data attributes (`self.x`), method
  dispatch, single inheritance, instantiation. A class table (`classes:
  list[ClassInfo]`, each with `list[MethEnt]`) ships in the bytecode and is
  decoded by the same generated cursor decoder.
- **Exceptions.** `try`/`except [Type [as name]]`/bare-except, `raise`, and
  re-raise. A per-frame block stack plus an in-flight `exc` signal on the state
  object unwind across call frames; handler type-matching reuses the class chain
  (`except Base` catches a derived instance). `finally` and `raise ... from` are
  rejected by the compiler for now.

Method calls all flow through `LOAD_METHOD`, which resolves a *user* method
(instance receiver) vs a *builtin* method (container/string receiver) at
runtime, so user methods named `get`/`pop`/`add`/etc. never collide with
container methods.

### py2c bug discovered
`list[int].pop()` (an *unboxed* int list) corrupts the heap in py2c-generated C
("double free or corruption"), while boxed-list `.pop()` (e.g. `list[str]`,
`list[V]`) is fine. The interpreter's exception block-stack therefore avoids
`.pop()` on its `list[int]`, managing depth with an explicit counter and indexed
access. Minimal repro: a function that appends two ints to a `list[int]` and
pops one.

### Opcode / table additions
Containers 9–19 (BUILD_LIST/TUPLE/DICT/SET, INDEX, SETINDEX, ITER_NEW,
ITER_NEXT, CONTAINS, LIST_APPEND, SET_ADD); classes 50–52 (LOAD_ATTR,
STORE_ATTR, LOAD_METHOD); exceptions 70–75 (SETUP_EXCEPT, POP_BLOCK, RAISE,
RERAISE, LOAD_EXC, EXC_MATCH). New value tags: list 7, dict 8, set 9, tuple 10,
iter 11, obj 12, class 13, bound 14, bound-builtin 15. New const kind `class`.

### Known v0 limitations
slicing; `finally`/`from`; `__str__`/`__repr__` dunders (instances print as
`<Name object>`); container value-equality (containers compare by identity);
builtin exception types (`ValueError` etc. are unknown bases → matched only by
user hierarchy); `%`-format zero-pad of negative numbers.

---

## Status update — front-end widening toward the py2c subset (native-validated)

A survey of `py2c.py`'s own AST (`ast.walk`) drove this phase: after operators
that were already handled inside the binop/compare/boolop dispatchers are
discounted, the highest-frequency structural gaps were ternaries (262), slices
(150), and the `|`/`&` set operators, followed by a long tail of builtins and
methods. The following now run end-to-end as a py2c-compiled native binary,
byte-identical to `python3` and the reference VM:

- **Ternary** `a if c else b` (`IfExp`) — both arms target the same register.
- **Slicing** `seq[lo:hi]` on lists, tuples, and strings, with negative and
  open bounds (`xs[:2]`, `xs[3:]`, `xs[-2:]`); step is restricted to 1. Slice
  *assignment* is still unsupported.
- **Set algebra** `|` `&` `^` (union / intersection / symmetric-difference) and,
  on ints, the same tokens as **bitwise** or/and/xor, plus `<<` / `>>` shifts and
  `**` power.
- **Annotated assignment** `x: T = v` (annotation ignored; a bare `x: T` is a
  no-op declaration whose name is still collected as a local).
- **`for` tuple-unpacking** `for i, v in enumerate(xs)` / `for k, v in d.items()`.
- **New builtins:** `isinstance` (user classes, type-builtins like `str`/`int`,
  and tuples of either), `enumerate`, `zip` (2–3 args), `any`, `all`, `ord`,
  `chr`, `reversed`, `getattr` (instance attr or bound method, with default),
  `hasattr`.
- **New methods:** list `extend` / `insert` / `index` / `count`; dict `update` /
  `setdefault`; str `splitlines` / `rstrip` / `lstrip` / `isdigit` / `isupper` /
  `islower`.

### py2c typing notes discovered this phase
- **Local annotations are honoured.** Writing `r: "long" = 1` (or `"double"`)
  makes py2c type the local accordingly. This is the clean fix for accumulators
  that py2c would otherwise box.
- **Pow-accumulator boxing.** A loop accumulator seeded with an int literal
  (`r = 1`) and then multiplied by a `long` field/param gets boxed to `obj`,
  producing a C type error at the `return`. Moving the loop into a small helper
  with typed params and return (`_pw_int(base, e) -> "long"`) — or annotating the
  local — fixes it. Helper names must avoid the runtime's own symbols: the
  runtime already defines `ipow` and `obj_pow`, so the helpers are `_pw_int` /
  `_pw_flt`.

The reference VM gets set `|`/`&`/`^` for free because Python's operators already
span sets and ints; the native interpreter implements union/intersection/
symmetric-difference explicitly over the side-heap set representation.

---

## Status update — Python parser groundwork (`tools/rpy_lib/rast.py`)

The path to running `py2c.py` under minipy needs a Python parser, since CPython's
`ast` module is far out of reach. Rather than build one from scratch, the PEG /
OMeta metacircular interpreter from **pymetaterp** (`asrp/pymetaterp`,
`single_file.py`) was vendored as `tools/rpy_lib/rast.py`. It bootstraps a
meta-grammar, uses it to parse an embedded Python grammar, and then parses real
Python source into a tree of `Node` objects (pymetaterp's own vocabulary —
`funcdef`, `__binary__`, `__call__`, `NAME`, `NUMBER`, … — not CPython `ast`
names).

Two changes were made on the way in, both verified to leave parser output
byte-identical on sample programs:
- **Python 3 port** (the original is Python 2: `print` statements, `map`, lazy
  `zip`).
- **De-eval'd.** The interpreter originally ran embedded grammar actions through
  host `eval`/`exec` (e.g. `-> reformat_binary(start, oper_and_atoms)`,
  `?(any_token(self.input))`, `!(self.indentation.append(...))`). Those are
  impossible in RPython. The action set is finite — eight strings total — so
  `eval`/`exec` were replaced by an explicit dispatch in `Interpreter._action`.
  This is the single most important step toward an RPython/py2c-compilable
  parser.

### Remaining work to run `rast.py` under minipy
An AST survey of `rast.py` against minipy's supported subset gives a short,
concrete gap list. Compiler features to add: default parameter values (6 uses),
step slices `x[::2]` (2), generator expressions as call args (5), and
comprehension `if`-clauses / multiple generators (4). `assert` and
`class Foo(Exception)` are already handled (`assert` added this phase; it
evaluates the test but does not raise in v0). Source adaptations to `rast.py`
(invited by the roadmap — "adapt code to RPython"): rewrite `class Node(list)` to
hold an internal child list instead of subclassing `list`; lift the one nested
closure in `reformat_binary` to a module-level helper threading its state; and
drop the `import sys` / `__main__` entry block in favour of a driver. After that
the boxed-value representation for parser match results (`str` / `Node` / `None`
/ `list`, today held in dynamically-typed Python locals) needs the same kind of
tagged-union treatment minipy already uses for `V`.

---

## Status update — default parameter values (native-validated)

Functions and methods now support default arguments (`def f(a, b=1, c="x")`),
filled **callee-side**: each function carries a `defaults` list (one const index
per parameter, `-1` for required), and on entry the runtime supplies a default
for every trailing parameter the caller omitted. Defaults are restricted to
constant literals (`None`, ints, floats, strings, bools), which matches CPython's
"evaluate once at def time" semantics for the cases that actually occur and keeps
the value embeddable as a constant. This is the highest-frequency remaining gap
for both `rast.py` (6 uses) and `py2c.py`.

The bytecode `funcs[*]` object gains a `defaults: list[int]` field; the native
`Func` struct gains the matching `defaults` member, decoded the same way as the
existing scalar-element `names: list[char*]` (so `rpy.json.generate_decoder`
needed no changes). All three executors agree, full regression
(demo2/3/4, cont, cls, exc, wide, bm, asrt, defs) and `make testfast` pass.

### Remaining gaps to run `rast.py` under minipy
With `assert` and default parameters done, the open items are: step slices
`x[::2]` (2 uses), generator expressions passed as call args (5), comprehension
`if`-clauses / multiple generators (4), and the source-side rewrites
(`class Node(list)` → internal child list; lift the one `reformat_binary`
closure; drop `import`/`__main__`). The Python-2-scoped embedded grammar also
needs py3 extensions before it can parse minipy's own annotated sources.

---

## Status update — comprehension/slice widening, native string methods, cache fix

This phase closes the remaining *compiler-side* feature gaps for `rast.py`:

- **Comprehension tuple targets** — `[v for k, v in pairs]`, `{k: v for k, v in
  pairs}`, multi-generator and `if`-clause comprehensions (the latter two already
  worked; only tuple unpacking was missing). Lowered the same way as `for`-loop
  unpacking (INDEX per element).
- **Step slices** — `x[::2]`, `x[1::2]`, `x[a:b:step]` on lists, tuples, and
  strings. The `SLICE` opcode was redefined to take the sequence in `a` and a
  3-register group `(lo, hi, step)` based at `b` (it previously used `a`/`b`/`c`
  for seq/lo/hi with no room for step). Positive step only in v0; a non-positive
  step falls back to 1.
- **Native string methods.** `split` (whitespace and explicit separator), `join`,
  `strip`, `find`, and `replace` were previously `v_none` stubs in the native
  interpreter; they are now implemented (the reference VM already had them via
  Python). `"".join(...)` in particular is needed by `rast.py`.

### Two py2c gotchas re-encountered
- **Char comparison.** `s[i] == sub[j]` between two computed 1-char strings
  compiles to a *pointer* comparison in py2c (it only strcmp's against string
  literals), so substring search/replace silently failed natively. Comparing
  `ord(s[i]) == ord(sub[j])` is the reliable form.
- **Int-literal accumulator boxing.** `start = 0` then `start = idx + len(sep)`
  boxed `start` to `obj`; the `start: "long" = 0` annotation fixes it, same as the
  earlier pow accumulator.

### rpy.py cache-key fix (latent bug)
The bytecode cache key hashed only the *user's* source files, so editing the
minipy compiler served stale, format-incompatible bytecode from
`/tmp/<key>.minipy.json` (this surfaced as an `IndexError` when the new
register-triple `SLICE` met old two-operand bytecode). The compiler's own source
is now folded into the cache key, so any compiler change invalidates the cache.

After this, the only remaining blockers to compiling `rast.py` through minipy are
source-side: `class Node(list)` (list subclassing), one `import`, and one nested
closure in `reformat_binary` — plus py3 extensions to the embedded grammar.

---

## Status update — rast.py runs end-to-end on the reference VM

With the source adaptations in place (Node no longer subclasses `list`; the
`reformat_binary` closure lifted to a module-level `_rb_parse`; `__main__`/`import`
replaced by a `parse_python(source)` entry; `try/finally` → `except/reraise`;
slice-assignment → element assignment; keyword args → positional; `pop` bounds-checked
instead of catching a host `IndexError`), the parser now **runs end-to-end on the
minipy reference VM**, producing parse trees that are byte-identical to CPython.

### The last blocker: generator-expression short-circuiting
`any_token` used `all(pop(input) == char for char in token)` to match an operator.
CPython short-circuits the generator, so `pop` is called only up to the first
mismatch; minipy **materialises** generator expressions (a documented v0
simplification), so `all(...)` evaluated every element and `pop` over-advanced the
input — which spuriously hit EOF on multi-character operators and failed every
binary-operator parse. The fix is a short-circuiting helper `_all_match(input, token)`
(a side-effecting genexp is un-RPythonic anyway). The other `all(...)`/`any(...)`
sites in rast.py iterate pure expressions, so materialisation is harmless there.

### What parses identically through minipy now
Assignments, NAMEs, NUMBERs, strings; binary operators with full precedence
(`a + b * c` nests correctly); parentheses; calls, attribute access and subscripts
(`foo(1, 2).bar`, `lst[i] + d[k]`); list and dict displays; `if` / `while` / `for`;
`class` with methods; `def` with bodies and `return`; comparisons. Verified by a
structural tree dump diffed against CPython `rast` across all of the above.

This is the first time minipy executes a non-trivial program of its own
(`rast.py`, ~18 functions) on real input. Remaining work to run rast.py *natively*
(py2c-compiled) is the higher bar: the parser's heterogeneous values
(`str | Node | None | list | tuple | bool`) need to flow through the static V boxing,
and `type`/`isalpha`/`isalnum` (added last phase) plus `MatchError`-driven control
flow must all hold up under the C backend.

---

## Status update — rast.py parses natively, including nested blocks

The native (py2c-compiled) interpreter now runs the `rast.py` parser end-to-end and
produces parse trees **byte-identical to CPython** for a broad Python subset,
including deeply nested code: recursive `fib` (an `if` nested inside a `def`),
classes with multiple methods, nested `while`/`for`, and comprehensions.

Getting from "compiles" to "runs natively" meant clearing a chain of walls that only
surfaced under the C backend (all fixed in `interp.py`):

1. **`float("inf")` → 0.0.** Native `float()` ignored string arguments (it read the
   string V's zero `iv` field), so the quantifier upper bound `inf` collapsed to 0 and
   every `*`/`+` matched zero times. Added `_str_to_float`, an `_inf_val()` helper
   (`1e308 * 10` overflows to +inf in C), and `inf`/`-inf` formatting in `_fmt_float`.
2. **`dict([pairs])` → empty.** The `escaped_char` rule builds `dict([...])` then
   indexes it; native `dict()` ignored its argument and returned an empty dict, so
   escape characters resolved to `None`, corrupting the tree and later
   null-dereferencing inside `materialize` (caught precisely with AddressSanitizer).
   Implemented native dict-construction from a list of pairs.
3. **`int("1")` → 0.** Same string-parsing gap as float, which made every NUMBER leaf
   come out `<0>`. Added `_str_to_int`.
4. **`list.pop()` didn't remove anything.** py2c compiles the *indexed* `lst.pop(i)`
   form to `dict_pop` (a no-op on a list), while the runtime's correct `list_pop`
   takes no argument. So `self.indentation.pop()` (the DEDENT action) never shrank the
   indentation stack, and any nested block failed to parse. Rewrote native `pop` to
   use the no-arg `items.pop()` (→ `list_pop`) and to do indexed removal with an
   explicit left-shift.

### A recurring py2c gotcha: per-function name unification
Twice this phase a new local in `do_builtin`/`do_method` reused a name already used as
a `long` elsewhere in the same function (`k` in the dict branch, `j` in `pop`), and
because py2c infers one C type per variable *name per function*, the shared name was
boxed to `obj` and an unrelated `v_int(...)` stopped compiling. Fix: give helper
locals unique names (`dkey`/`dval`, `pop_i`/`pop_j`/...).

### Known native limitation
`parse_python` re-bootstraps both grammars on every call, and the native interpreter
never frees container memory, so repeated calls in one process accumulate in the fixed
1 GiB arena and eventually abort. A single parse fits comfortably; batch use would
need either hoisting the bootstrap out of `parse_python` (parse the grammars once,
reuse the `Interpreter`) or arena reuse/freeing. This is the natural next step.

---

## Status update — benchmarking minipy vs CPython (and PyPy)

Added a cross-runtime benchmark harness, `benchmarks/run_minipy_benchmarks.py`, plus
eight self-contained benchmark programs in `benchmarks/minipy/` (no imports/argv,
since minipy runs top-level module code): `fib` (recursion), `loops` (integer
arithmetic), `sieve`, `matmul`, `sort` (insertion sort), `dicts`, `strings`, and
`collatz`. Each prints a result; the harness runs it under CPython, PyPy3 (when
present), and minipy-native (compile to bytecode, then run the py2c-compiled interp),
checks that all backends print identical stdout, and records best-of-N runtime and
exact peak RSS (via a minimal-footprint C probe). Results land in
`benchmarks/results/minipy_results.json`. PyPy3 is not installed in this environment,
so the runs below are minipy vs CPython.

### Two correctness bugs the benchmarks surfaced (native-only; ref VM was correct)
- **Floored division/modulo.** Native `%` and `//` used C truncated semantics, so
  `-1 % 7` gave `-1` instead of `6` and `-7 // 2` gave `-3` instead of `-4`. Added
  `_floordiv_int`/`_mod_int` (and `_ffloor` for floats) implementing Python's floored
  behaviour; the correction is a no-op under CPython's already-floored `//`, so the
  same code is correct in both the under-CPython and native runs.
- **Sequence repeat.** `[x] * n`, `n * [x]`, `(...) * n`, and `n * "s"` were
  unimplemented natively (they fell through to numeric multiply and produced garbage).
  `v_mul` now handles list/tuple/str repeat in both operand orders.

### A memory optimisation: frame pooling
The interpreter allocates a register array and an argument list per call and never
frees them, so allocation-heavy programs ballooned (fib(28) peaked ~717 MB). Since
calls are stack-nested, frames are safe to recycle: added a register-array pool on
`St` that `run_func` borrows from on entry and returns on every exit, and the CALL
opcode now recycles its argument list the same way. fib(28) dropped to ~492 MB
(~30% less) with correctness and speed unchanged.

### Results (work sizes tuned so CPython runs in ~30-160 ms; best-of-3)
| benchmark | minipy vs CPython | minipy peak RSS | note |
|-----------|-------------------|-----------------|------|
| matmul    | ~1.0x  | 8 MB   | tight nested int loops |
| strings   | ~1.0x  | 23 MB  | slicing / split / find |
| sieve     | ~1.8x  | 57 MB  | bool-list indexing |
| loops     | ~3.4x  | 517 MB | integer arithmetic |
| sort      | ~3.6x  | 70 MB  | insertion sort, list writes |
| collatz   | ~5.0x  | 528 MB | int arithmetic, while |
| fib       | ~8.8x  | 492 MB | 1.3M recursive calls |
| dicts     | ~203x  | 25 MB  | **O(n) dict lookup** |

All eight agree with CPython byte-for-byte. The headline numbers: minipy is typically
**~2-9x slower** than CPython on compute (reasonable for a boxed-value register VM that
is itself an interpreter written in the py2c subset), with two clear weaknesses:

1. **`dict` is a linear-scan association list** (`dict_find` is O(n)), so the dict
   benchmark degrades to O(n²) and runs ~200x slower. A real hash table is the
   single biggest available speedup and the obvious next optimisation.
2. **No garbage collection.** Peak RSS tracks *total* allocations, not the live set, so
   long allocation-heavy runs use hundreds of MB. Frame pooling mitigates the call-path
   lists; the remaining cost is unfreed value boxes (every integer result is a heap V).
   Unboxed small integers or an arena reset between top-level statements would help.

Runtime *correctness* is solid across recursion, nested loops, lists, dicts, strings,
classes, and exceptions — the same interpreter that now parses `rast.py` natively.

---

## Status update — dict hash table, and two optimisation experiments

### Dict hash table: 203x -> 2.0x slower than CPython
The dict was a flat `items = [k0, v0, k1, v1, ...]` list with a linear `dict_find`,
so lookup/insert/`in` were all O(n) and the dicts benchmark was O(n^2) (~203x slower
than CPython). It now uses an open-addressing hash index, in the spirit of CPython's
compact dict: `items` stays the ordered backing store (preserving Python insertion
order for iteration, `keys()`, `values()`, `for k in d`), and a new `buckets` array on
the `Cont` indexes into it. Added `v_hash` (int/bool -> value, str -> djb2, none/float
handled), `dict_reindex` (power-of-two capacity, load factor < 2/3, rebuilt on growth),
`dict_lookup`, and `dict_insert`; every dict op (`[]` read/write, `in`, `get`,
`setdefault`, `update`, `BUILD_DICT`) routes through them. minipy has no `del`/dict
deletion, so no tombstones are needed. Result: the dicts benchmark dropped to ~2.0x
CPython, a ~100x speedup, with all benchmarks and the full regression still byte-identical.

One py2c wrinkle worth recording: a `list[int]` *struct field* is not unboxed the way a
`list[int]` *local* is, so `cont.buckets[slot]` came back boxed (`obj`) and the C did not
type-check. Storing the index as a `list[V]` of `v_int` slot values (read via `.iv`, with
a shared `v_int(-1)` empty sentinel) uses the standard container machinery and compiles
cleanly.

### Experiment 1 (reverted): if/elif dispatch -> C switch in py2c
The interpreter's opcode dispatch is a long `if op == 1: elif op == 2: ...` chain. The
hypothesis was that emitting a C `switch` (jump table) would make dispatch O(1) and speed
up every benchmark. A gated transform was added to py2c (`st_If` -> `switch` when a chain
compares one variable to >=5 distinct int constants with no loop-escaping `break`). It
compiled, produced `switch (op)`, and was byte-for-byte correct -- but gave **zero**
measurable speedup. gcc `-O2` already lowers a dense integer `if/elif` chain to a jump
table, so the explicit switch added nothing. The change was reverted to keep py2c
unchanged; the finding is that **dispatch is not the bottleneck**.

### Experiment 2 (reverted): small-int / None / True / False cache
Since the switch result points at allocation rather than dispatch, the next idea was to
stop heap-allocating the most common values: cache `None`, `True`, `False`, and small
integers as shared immutable `V` singletons (`V` is never mutated in place, so sharing is
safe). This needs runtime-initialised module-global state of a class type. py2c currently
emits such globals inconsistently (sometimes `V*`, sometimes `obj`, with a non-constant
static initialiser), so the interpreter did not compile. Reverted.

This is the same capability the design doc's "single large block of instructions"
(`_list_Instr_global_lookup`) and compact-`FuncOpt` ideas depend on, so the concrete
next step toward unboxing is **py2c support for runtime-initialised, class-typed module
globals** (a zero-init `.bss` pointer plus an init function, consistently typed). With
that in place, the small-int/singleton cache and the global instruction block become
straightforward, and the larger goal -- shrinking `V` from ~32 bytes to a 16-byte
tag+union -- can follow. The measurements here show why it matters: the slowest
benchmarks (fib 8.9x, collatz 9.3x) are dominated by per-value heap allocation and the
no-GC memory growth, not by interpreter dispatch.

---

## Status update — py2c: runtime-initialized class-typed globals, and the value cache

### The py2c enabler: class-typed module globals stored as `obj`
The previous unboxing attempt (a shared-singleton value cache) was blocked because a
module-level class-typed global initialized to None -- `_v: "V" = None` -- did not
compile: `st_AnnAssign` emitted `V* _v = (V*)AS_OBJ(OBJ_NONE);`, which is both a
non-constant static initializer and a type-mismatch with every use site (the
global-type registry, and every `global v; v = V(...)` store/read, already treat such a
global as the boxed `obj`). The declaration was the only piece out of step.

Fix (one targeted change in `st_AnnAssign`): when a *toplevel* annotated assignment has
a class-pointer annotation and an explicit `= None`, store it as `obj`. This matches the
registry's existing coercion (`collect_module_globals` already maps `*`+None to obj), so
the file-scope declaration, the stores, and the reads now all agree, and `obj` zero-init
is a valid `.bss` static initializer (T_NONE == 0). The change is gated exactly to that
pattern -- a `grep` of all transpiled compiler/stdlib sources shows it appears nowhere
there, so self-hosting and every existing program transpile byte-for-byte unchanged; 7/8
rpython benchmarks were confirmed identical and the 8th matches the old output too. This
is the capability the design doc's global instruction block and compact-object work both
need.

### The payoff: a small-int / None / True / False value cache
With class-typed globals working, the interpreter now allocates the most common values
once at startup (`setup_cache`) and hands out shared immutable singletons: `None`,
`True`, `False`, and the small integers in [-8, 256]. `V` is never mutated in place and
minipy compares by value (not identity), so sharing is safe. `v_none`, `v_bool`, and
`v_int` return the cached `V` on the hot path and only allocate for out-of-range ints.

This was a large win in *both* speed and memory, because small ints, loop counters, and
the boolean results of every comparison were the bulk of all allocations:

| benchmark | before | after | peak RSS before -> after |
|-----------|--------|-------|--------------------------|
| matmul    | 1.0x   | 0.5-1.0x (~CPython or faster) | 8 MB -> 3 MB |
| sieve     | 1.8x   | ~1.0-1.6x | 56 MB -> 23 MB |
| dicts     | 2.0x   | 2.0x   | 28 MB -> 20 MB |
| strings   | 1.7x   | 1.3-2.0x | 25 MB -> 20 MB |
| sort      | 3.6x   | 2.0x   | 68 MB -> 16 MB |
| loops     | 3.4x   | 3.2x   | 505 MB -> 236 MB |
| collatz   | 9.3x   | 2.8x   | 516 MB -> 48 MB |
| fib       | 8.9x   | 4.1x   | 480 MB -> 82 MB |

The recursion/arithmetic-heavy cases improved the most: collatz 9.3x -> 2.8x with an 11x
memory drop, fib 8.9x -> 4.1x with a 6x memory drop. matmul now runs at or below CPython.
All eight stay byte-identical, and the full regression, `testfast`, and the rast.py
native parse all pass.

The remaining allocation hot spot is large integers (fib's growing sums, loops'
accumulator), which fall outside the small-int range and still allocate. The clear next
steps, both now unblocked by the class-typed-global support: (1) move the per-call
instruction list into a single global block (the design doc's `_list_Instr_global_lookup`)
and shrink `Func`, and (2) the larger goal of shrinking `V` itself from ~32 bytes to a
16-byte tag+union, which needs py2c support for union/overlapping POD fields.

---

## Status update — the 16-byte tag+union V (py2c anonymous-union support)

### py2c: a union of overlapping POD fields
The interpreter's value `V` carried four fields -- `tag` plus `iv` (long), `dv`
(double), `sv` (char*) -- but a value is exactly one of int/heap-index, float,
or string, chosen by `tag`, so `iv`/`dv`/`sv` never need to coexist. They were
nonetheless laid out as three separate 8-byte slots, making V ~32 bytes when 16
would do. py2c's own boxed `obj` is already a 16-byte tag+union; user classes
just couldn't express the same overlap.

Added that capability (25 lines, in three places): a class can declare
`_c_union_ = ("iv", "dv", "sv")`, and `emit_struct` groups those members into a
single anonymous C union at the position of the first one. Anonymous unions mean
every existing access (`v->iv`, `v->dv`, `v->sv`) keeps working unchanged -- no
access-site rewriting. The feature is gated entirely on the `_c_union_` marker,
which a grep confirms appears in no transpiled compiler/stdlib source, so
self-hosting and every other program are byte-identical; the 8 rpython
benchmarks still transpile and gcc-compile, and `testfast` passes.

`V` now emits as `struct V { int tag; union { long iv; double dv; char* sv; }; }`
-- confirmed `sizeof(V) == 16` (was ~32).

### interp.py: constructing into a union
Because the members overlap, a 4-argument constructor that set all of
`iv`/`dv`/`sv` would have each write clobber the last. So `V.__init__` now takes
just `(tag, iv)` (the common integer/heap-index case), the union members are
declared at class level (`iv: "long"` etc., which py2c's field discovery already
honors), and the two non-integer constructors set their member after
construction (`v_float`: `r = V(2, 0); r.dv = x`; `v_str`: `r = V(3, 0);
r.sv = t`). Every field *read* was already tag-guarded, so no read sites changed.

### Result: a real memory win
Halving the size of every heap value cut peak RSS substantially, on top of the
earlier value-cache savings:

| benchmark | RSS (32-byte V) | RSS (16-byte V) | delta |
|-----------|-----------------|-----------------|-------|
| loops     | 231 MB          | 158 MB          | -32%  |
| collatz   | 47 MB           | 33 MB           | -30%  |
| sort      | 16 MB           | 11.5 MB         | -28%  |
| sieve     | 23 MB           | 17 MB           | -25%  |
| dicts     | 20 MB           | 15 MB           | -24%  |
| strings   | 19 MB           | 17.6 MB         | -9%   |
| matmul    | 3.3 MB          | 3.1 MB          | -6%   |
| fib       | 80 MB           | 80 MB           | 0%    |

All eight stay byte-identical to CPython. (fib is unchanged because its peak is
dominated by per-call argument-list allocations, not by V; shrinking the
per-call list is a separate future step.) Runtime is unchanged within noise --
anonymous-union access compiles to a plain field read, and construction now does
fewer stores.

### Two pre-existing compiler limitations surfaced (orthogonal to this work)
While validating, two of the regression scripts failed -- but at the *compiler*,
not the interpreter, and independent of the union:
- `ty`: the compiler's `BUILTINS` list ends at `hasattr`; `type` is absent, so
  `type(5)` compiles to an uninstalled global (resolves to None). The interpreter
  already implements `type` at builtin id 27 -- the compiler just never emits it.
  A one-line fix (append `"type"` to `BUILTINS`) would reconnect them.
- `aug`: `st_AugAssign` only accepts a Name target, so `d["x"] += 10` and
  `c.n += 7` raise `CompileError`. Subscript/attribute aug-assign is unimplemented
  in this compiler revision.
Both are easy to confirm: every other regression script (18/20) and all 8
benchmarks pass with the union. (Note: only `fib.py` and `dicts.py` are committed
under `benchmarks/minipy/`; the other six benchmark programs need to be
re-committed.)

---

## Status update — compiler feature fixes + two memory optimizations

### Compiler: four features reconnected (full suite green on all three executors)
The `minipy v13` compiler had drifted behind the interpreter. Four fixes bring
them back in sync, each verified against CPython, the reference VM, and native:
- `type` added to `BUILTINS` (id 27) -- the interpreter already implemented it;
  the compiler just never emitted it. The reference VM gained a matching
  `_typeof` returning `("builtin", id)` / `("class", cid)`.
- Subscript and attribute augmented assignment (`d["x"] += 10`, `c.n += 7`):
  `st_AugAssign` now evaluates the target object/index once, loads via
  INDEX/LOAD_ATTR, applies the op, and stores back via SETINDEX/STORE_ATTR.
- Negative-literal defaults (`def f(a, b=-1, c=-2.5)`): `_literal_const_index`
  folds a `UnaryOp(USub, Constant)` to the negated literal.
- `isalpha`/`isalnum` added to the compiler `METHODS` map and the reference VM's
  `_method` dispatch (the interpreter already had ids 128/129).

All 20 regression scripts now pass on cpython == ref == native.

### Optimization 1: shared empty block-stack sentinel (fib -98%)
Every `run_func` call allocated a fresh block-stack list for exception handling,
even though most functions (and all of fib's ~832k calls) never use try/except --
this single allocation dominated fib's peak. A pooling attempt failed because
popping a `list[int]` out of a `list[list[int]]` is mis-typed by py2c (the nested
element doesn't behave as `list[int]`; reads come back as None). The fix sidesteps
that entirely: a function borrows a shared read-only empty sentinel and only swaps
in a private list on its first SETUP_EXCEPT, and the stack is stored as `list[V]`
(handler PCs via `v_int`) since `list[V]` is uniformly boxed where `list[int]` is
not. Result: **fib peak RSS 82 MB -> 2 MB (-98%, ~40x)**, exceptions still correct.

### Optimization 2: memoized constant values (loops -20%, dicts -24%)
`const_to_v` allocated a fresh V on every constant load, so a large literal in a
hot loop -- e.g. the `1000000` bound re-loaded each iteration of a while-condition
-- allocated a V per iteration. Constants are immutable, so each is now
materialized once at startup into a shared V (same principle as the small-int
cache). Peak RSS: **loops 158 -> 127 MB (-20%), dicts 15.4 -> 11.7 MB (-24%),
strings -6%**, all byte-identical.

### Cumulative benchmark peak RSS (this phase)
| benchmark | start | now | total |
|-----------|-------|-----|-------|
| fib       | 82 MB    | 2.0 MB  | -98% |
| dicts     | 15.4 MB  | 11.7 MB | -24% |
| loops     | 158 MB   | 127 MB  | -20% |
| strings   | 17.7 MB  | 16.6 MB | -6%  |
| collatz/sort/sieve/matmul | (already low) | ~unchanged | |

### Next frontier: reclaiming arithmetic temporaries
loops' remaining ~127 MB is dominated by short-lived integer temporaries
(`i*3`, `i*3-1`, the new accumulator, the new counter) -- roughly four dead V's
per iteration that are never reclaimed (no GC). The arena already supports
`afree` with size-bucketed reuse, so the missing piece is knowing a V is dead and
unaliased. The safe route is compiler-side escape analysis: mark temporary
registers that never escape (no MOVE/store/call) and emit explicit free hints the
interpreter honors via `afree`. That is a larger, separate change and is the
clear next step for the while-loop integer benchmarks.

---

## Status update -- benchmark report section + eval powered by minipy

### LaTeX/PDF report: a minipy lead section
`make benchmarks_report` already typesets `tools/benchmarks.tex` (the rpython
cross-runtime suite). It now opens with a minipy section. `benchmarks/
plot_minipy.py` reads `benchmarks/results/minipy_results.json` and emits
`minipy_body.tex` (an intro to minipy and the three-stage toolchain, plus a
CPython-vs-minipy table: runtime, ratio, peak RSS, compile time) and a two-panel
`minipy_summary.pdf` figure (runtime relative to CPython; peak memory, log
scale). `tools/benchmarks.tex` `\input`s it right after `\maketitle`, and the
Makefile target runs the minipy harness + plot before pdflatex. (The report's
rpython half still needs the self-hosted compiler to build, which is environment
-dependent; the minipy section renders on its own.)

### eval() without MicroPython: minipy's native evaluator
`examples/rpython/eval/eval_demo.py` previously needed the MicroPython core to be
linked, because py2c lowered `eval(s)` to `rpy_eval*` provided by that core. Now
that we have minipy, eval no longer pulls in MicroPython. py2c emits a
self-contained native expression evaluator (`MINIPY_EVAL_C`) that defines
`rpy_eval` / `rpy_eval_int|float|bool|str` directly against the ShivyCX object
model -- a recursive-descent evaluator for Python numeric expressions:
int/float literals, `+ - * / // % **` with Python floor/mod semantics, unary
signs, parentheses, and the six comparisons. `main()` no longer forces the mp
bridge for eval; it sets `minipy_eval` instead, and `write_runtime` appends the
evaluator. (Setting `SHIVYC_MP_EVAL=1` still selects the MicroPython path for
expressions outside this grammar.)

This covers the overwhelmingly common pattern -- `eval(f"{a}+{b}")`, whose
f-string is built in the caller so the evaluator only sees a literal expression.
`examples/rpython/eval/repl_demo.py` is a small interactive calculator REPL on
the same path, and `eval_bench.py` is a microbenchmark.

Result on the million-call benchmark (`eval_bench.py`), identical output
(4000000) on both:

| backend | 1e6 `eval(f"{a}+{b}")` | speedup |
|---------|------------------------|---------|
| CPython `eval()`        | 5.53 s | 1x       |
| minipy-native eval      | 0.32 s | **17x**  |

### Not yet done (clear next steps)
- **General eval** (names, calls, containers, statements/`exec`) needs minipy's
  parser+compiler compiled to C -- the frontend currently runs only under
  CPython. `rpy_exec` is a stub for now.
- **A typed EVAL opcode in the minipy interpreter itself**, so a minipy program
  (not just a py2c-transpiled one) can `eval` through the same native path, with
  the typed form skipping the box -- a small further speedup.
- **Multi-argument `print`** in py2c emits only the first argument (a pre-existing
  limitation, unrelated to this work); the demos use single-value prints.

---

## Status update -- profiling new workloads, OOP fix, container shrink

### Five new benchmarks to map strengths and weaknesses
Added `objects` (method-dispatch-heavy OOP), `bintree` (object allocation +
recursion), `ack` (Ackermann -- call overhead), `wordfreq` (dict + string keys),
and `nbody` (floating point) to `benchmarks/minipy/`. Differentially checked
against CPython like the rest. They sort minipy's behaviour into clear bands
(ratios are noisy on a shared box, but the pattern is stable):

- **Competitive / faster than CPython**: `matmul` (~0.7x), `strings` (~1.3x) --
  compute-bound, allocation-light inner loops.
- **~2x**: `ack`, `bintree`, `dicts`, `wordfreq`, `sieve` -- call/lookup bound.
- **Weakest (3--6x, and the memory hogs)**: `objects`, `nbody`, `loops`,
  `collatz` -- all dominated by allocating a fresh value for every large-integer
  or float arithmetic result (no reclamation yet).

### OOP fix: a bound method is now a single 16-byte value
Profiling `objects` exposed the worst case: **every** `obj.method(...)` call
allocated a heap `Cont` (40 bytes) plus a list to hold the receiver, none of it
ever reclaimed -- 1.6M calls drove peak RSS to **605 MB**. A bound *user* method
is now packed into one `V` (tag 14) whose `iv` holds `instance_heap_index *
SHIFT + function_index`; the instance already lives on the heap, so the receiver
`V(12, hidx)` reconstructs exactly, and nothing is allocated for the binding.
The per-call argument list (previously leaked on this path) is now pooled.
Result: `objects` peak RSS **605 MB -> 173 MB (-71%)** and runtime ~7.9x -> ~6.1x.

### Container shrink: a shared empty dict-index
Only dicts use the `Cont.buckets` field, yet `_heap_put` allocated a fresh empty
list there for *every* container (list, set, tuple, instance, iterator, bound
builtin). They now borrow a single shared read-only sentinel; a dict swaps in its
own list on first reindex (the lookup/insert paths already handle an empty
index). Peak RSS: **strings 16.6 -> 12.0 MB (-28%)**, plus smaller drops on
matmul/sieve/dicts. (`Cont` itself is still 40 bytes -- shrinking it below that
needs py2c to store list fields as raw 8-byte pointers instead of boxed 16-byte
`obj`, which is a larger py2c change.)

### Report now covers thirteen benchmarks
`plot_minipy.py` / `minipy_body.tex` describe the workload spread and render the
13-row table + figure; the intro explains where minipy wins and loses.

### The dominant remaining lever
`objects`, `nbody`, `loops`, and `collatz` are all bottlenecked on the same
thing: each large-int/float arithmetic result allocates a 16-byte `V` that is
never freed. The arena already supports `afree` with size-bucketed reuse, so the
missing piece is reclaiming dead temporaries. Two concrete next steps:
1. **Fuse LOAD_METHOD+CALL into a CALL_METHOD opcode** so an immediately-called
   method allocates no bound-method value at all (packing nargs and the name
   const together into one operand) -- removes the last per-call OOP allocation.
2. **Compiler-side escape analysis** to mark non-escaping arithmetic temporaries
   and emit free hints the interpreter honours via `afree` -- this is what turns
   the accumulator/float loops from allocation-bound back into compute-bound.

---

## Status update -- CALL_METHOD: zero-allocation method calls

The previous turn packed a bound method into a single 16-byte value; this turn
removes even that. `obj.method(args)` was still two opcodes -- LOAD_METHOD (which
built a bound value) then CALL. They are now fused into one **CALL_METHOD**
opcode (53): the receiver sits at the call base with the args right after, the
operand `c` packs the method-name const and the argument count (`name*256 +
nargs`), and the interpreter resolves the method (user vs builtin, by receiver
type) and calls it directly with `[receiver, *args]` -- no bound value is ever
created. The per-call argument list is pooled. The compiler falls back to the old
LOAD_METHOD+CALL only for the rare `>=256`-arg or huge-const-pool cases, and a
bare `m = obj.method` reference still produces the packed bound value.

This applies to **both** user methods and builtin methods (`.append`, `.upper`,
`.split`, ...), so it helps OOP and string/list-method code alike:

| benchmark | RSS before | RSS after | runtime |
|-----------|-----------|-----------|---------|
| objects   | 169 MB    | 75 MB (-56%)  | 6.2x -> 4.3x |
| strings   | 11.7 MB   | 7.6 MB (-35%) | 1.3x -> 1.1x |

Cumulatively over the last two turns `objects` has gone from the original
**605 MB / 7.9x to 75 MB / 4.3x** (8x less memory, ~1.8x faster). All three
executors (CPython, reference VM, native) stay byte-identical; full regression
ALLGREEN, testfast PASS.

The remaining `objects` cost (75 MB) and the `loops`/`nbody`/`collatz` memory are
now squarely the unreclaimed large-int/float arithmetic temporaries -- the
escape-analysis frontier is the next big lever.

---

## Status update -- reclaiming dead arithmetic temporaries (escape analysis)

The biggest remaining cost on `loops`/`nbody`/`collatz` was that every
large-int/float arithmetic result allocated a 16-byte value that was never
freed. The interpreter now recycles the *scratch* ones in place.

**Compiler (conservative escape analysis).** A value produced by a binop or
unary `-` lives only in its temporary register -- it was never loaded from a
name, const, index, or attribute, all of which alias a live binding -- so once
it is consumed as an operand it is provably dead. `ex_BinOp` marks each operand
that is itself such a fresh arithmetic sub-expression and encodes two "free this
operand" hint bits into the high bits of the destination field (bit 29 -> free
reg b, bit 30 -> free reg c) for the six ops the interpreter decodes (ADD, SUB,
MUL, DIV, MOD, FLOORDIV, and their NN fast-paths). Because the hints ride on the
existing instruction, no extra opcodes are emitted -- crucially, this reclaims
the *left*-associative temporaries (`i*3`, `i*3-1` in `i*3-1 ... `) that a
post-hoc free could not reach. The reference VM masks the bits off (a no-op
there, since Python is garbage-collected).

**Interpreter (free-list + in-place reuse).** A dead operand is pushed onto a
free-list; `v_int`/`v_float` pop and mutate one in place instead of allocating.
A hard runtime gate (`_free_v`) only ever accepts large ints (outside the
small-int cache) and floats, so a shared singleton, interned const, string, or
container can never reach the free-list even if a hint were somehow misapplied --
the safety does not rest on the compiler analysis alone.

Result (peak RSS, all three executors byte-identical):

| benchmark | RSS before | RSS after | runtime |
|-----------|-----------|-----------|---------|
| nbody     | 197 MB    | 92 MB (-53%)  | 3.7x -> 3.1x |
| loops     | 121 MB    | 63 MB (-48%)  | 3.4x -> 2.7x |
| collatz   | 30 MB     | 23 MB (-25%)  | ~5x |
| matmul    | 2.7 MB    | 2.4 MB        | ~0.9x (faster than CPython) |

The reclaimed loops also run *faster*: recycling a temporary is cheaper than
growing the arena. Validated on the full regression (cpython == ref == native),
all 13 benchmarks (cpython == native), testfast, and five dedicated aliasing
stress tests (value reused after a binop, the same sub-expression twice, a temp
stored into a list then re-read, accumulator snapshots, call results in
arithmetic) -- all byte-identical.

`objects` is unchanged (75 MB): its growth is the accumulator pattern -- each
`self.balance = self.balance + amt` discards the *old* balance, which is a live
binding's value, not a scratch temporary. Freeing those needs per-variable
escape analysis (is the local ever aliased/stored/returned?), which is the next,
more delicate step. (Pre-existing, unrelated: the Python reference VM hits its
recursion limit on `ack` and has a separate `strings` discrepancy; the native
interpreter matches CPython on both.)

---

## Status update -- reclaiming accumulator old-values (global escape analysis)

Scratch reclamation freed temporaries *inside* an expression; this turn frees the
value an accumulator *discards* when it is overwritten. `total = total + x`
throws away the old `total` -- a live binding's value, not a scratch temp -- so
reclaiming it needs to prove the binding is never aliased.

**Compiler escape analysis.** A new pass classifies every module global. A global
is *reclaimable* only if it never appears in an escaping position anywhere in the
program: aliased to another name (`x = g`), returned, stored into a container /
attribute / another global, built into a container literal, or passed to a call
that might retain it. Arithmetic and comparison operands, conditions, and
subscript indices merely read the value and are safe; `print`/`len` are known
read-only. The pass is conservative -- any unrecognised position is an escape --
and tracks per-function local scopes so a name that is local in one function and
global elsewhere is judged correctly. For a reclaimable global, `STORE_GLOBAL`
carries a hint bit (reusing the same dispatch decode as the arithmetic hints);
the interpreter frees the slot's old value after overwriting it, and the runtime
`_free_v` gate still independently guarantees only large ints/floats are ever
recycled.

The analysis correctly excludes, e.g., objects' loop index `i` (it flows into
`Account(i)`, which stores it) while still reclaiming `total` and `step`.

| benchmark | RSS before | RSS after |
|-----------|-----------|-----------|
| loops     | 61 MB     | 1.9 MB (-97%) |
| objects   | 73 MB     | 50 MB (-31%)  |
| sieve     | 16.5 MB   | 13.5 MB |
| dicts     | 9.9 MB    | 8.4 MB |

`loops` is now allocation-flat: with both `total` and `i` reclaimed in place,
the accumulator loop holds two live values and recycles everything else. Over
three turns it has gone 121 MB -> 63 MB (scratch) -> 1.9 MB. Validated across the
full regression (cpython == ref == native), all 13 benchmarks, testfast, the five
arithmetic aliasing tests, and four new global-escape stress tests (a global
aliased to another, one stored through a user function, one returned from a
function, and pure accumulators) -- all byte-identical, and in each escaping case
the analysis correctly declines to reclaim.

Still open: the two remaining heavy benchmarks are bottlenecked on *container*
and *attribute* accumulators -- `nbody` (~90 MB) overwrites `vx[i]` list slots and
`objects` (~50 MB) overwrites `self.balance` -- which need SETINDEX / STORE_ATTR
old-value reclamation with element/attribute-level escape analysis. Local-variable
accumulators (sort's inner loop) are the other natural extension of this pass.

---

## Status update -- optional type annotations and fused typed opcodes

Goal: keep the existing benchmarks as the untyped baseline, and add `*_typed.py`
variants whose annotations are ordinary Python (ignored by CPython) but let minipy
emit faster code. The annotation vocabulary is deliberately small: `list[int]` and
`list[int:N]` mark a list of ints (the `:N` is a fixed-size hint), and
`list[list[int:N]]` a matrix of such rows.

**Type inference.** A per-scope pass seeds variable types from annotations, then
propagates: if `A` is annotated `list[list[int:n]]` and the code does
`A.append(rowa)`, then `rowa` is inferred to be `list[int:n]`. Copy edges (`y = x`)
propagate too. So the user only annotates `A` and `B`; `rowa`/`rowb` are inferred.
`_type_of` then types arbitrary expressions, so `A[i]` is known to be a typed row
and `A[i][k]` a typed int.

**What did *not* help.** The first attempt was specialised index opcodes
(`INDEX_INT`/`SETINDEX_INT`) that skip the generic tag-dispatch and negative-index
fixup. Measured speedup: ~0% on both matmul and a pure 1-D reduction. gcc -O2
already turns the generic index path into tight code, and the boxed-V model plus
per-opcode fetch/decode/dispatch dominate -- skipping a couple of branches saves
nothing. Raw unboxed `long` arrays would help, but py2c cannot express them: a
module-global `list[int]` crashes the transpiler, and a `list[int]` struct field
compiles but produces wrong results. So typed storage stays boxed.

**What did help: fusion.** The real cost is the number of opcodes executed, so
typing is used to *fuse* an accumulator's whole inner step into one opcode:

- `ACC_ADD_GINT` -- `g = g + tlist[k]` (g a reclaimable global, tlist a typed int
  list) collapses load-global + typed-index + add + store-global(reclaim) into a
  single dispatch. Reduction benchmark: **1.24x faster** than the untyped baseline.
- `ACC_MAC_GINT` -- `g = g + tA[i][k] * tB[k][j]` (the matmul inner product) fuses
  two typed indexes, a multiply, an add and the reclaiming store into one opcode
  (the row registers are packed with their indices). matmul: **1.26x faster** at
  n=70, 1.21x at n=28.

All three executors (CPython, the reference VM, and the native build) stay
byte-identical on every typed program; CPython runs the annotated source unchanged.

### A soundness bug found and fixed along the way

Making inner-loop globals reclaimable (the module-assigned set previously only
scanned top-level statements, so `s`/`j`/`k` inside the matmul loops were never
even considered) exposed a latent hole in the global-escape analysis from the
previous version: it treated every subscript index as a read-only position. That
is true for lists but **false for dicts**, where `d[k] = v` retains `k` as a key.
`dicts` began returning wrong totals because reclaimed keys were being freed out
from under the dictionary.

The analysis is now stricter on two fronts. A global is reclaimable only if every
assignment gives it a *freshly built, uniquely-owned* value -- arithmetic, or a
small cached-int constant the free gate never recycles -- so a global that ever
receives an alias (another name, a subscript element, an attribute, a call result,
a for-loop or unpack target, or a large/float/str constant that may be a shared
memoised const) is excluded. And a subscript index in a store target is now
treated as escaping, since it may be a retained dict key. This fixed `dicts` and
also correctly dropped `collatz`'s `best = c` / `besti = i` from the reclaimable
set -- those were Name aliases that the looser analysis had wrongly accepted (they
happened not to corrupt output, but were unsound). Net memory effect of the
stricter-but-wider analysis: `matmul` 2.4 -> 1.9 MB and `nbody` ~90 -> ~46 MB
(their inner-loop accumulators are now safely reclaimed), everything else
unchanged.

Validated: full regression (cpython == ref == native, 38 programs), all 16
benchmarks including the three typed ones, testfast, the arithmetic-aliasing and
global-escape stress tests, and a dict-key stress (`dicts`). Raw unboxed arrays
remain the one lever blocked by py2c; with boxed storage, fusion is what pays.

---

## Status update -- general accumulator fusion (and the limits of going further)

The typed opcodes above fuse a typed-array access into an accumulator's update.
The same fusion is worth doing for *every* reclaimable-global accumulator, typed
or not, so `g = g + <expr>` now lowers to one `ACC_ADD_G` opcode that loads the
global, adds the right-hand side, stores, and reclaims the old value in a single
dispatch. Because the fused opcode reads `g` *after* evaluating the right-hand
side, it is only emitted when that side-effect-free (no calls) -- otherwise a call
could mutate `g` and break CPython's left-to-right order. One subtlety cost a
correctness-of-memory bug first: the fused op must also free the right-hand side
when it is a fresh arithmetic temp (e.g. the `a[k]*b[k]` product in a dot product),
exactly as the normal `ADD` path would. Missing that leaked ~75 MB on `dotprod`
before the fresh-temp free flag was added; `dotsum` never leaked (its `arr[k]` is a
live list element, correctly *not* freed) and `loops` never leaked (its products
are cached small ints).

This is a pure speed win -- the reclamation is unchanged, so memory stays flat --
and it helps the untyped baselines too: `loops` 3.2x -> ~1.9x CPython, `dotsum`
2.55x -> ~1.5x, `dotprod` ~1.85x. The typed opcodes still win because they fuse the
array indexing as well: on a flat dot product `total = total + a[k]*b[k]`, the
baseline runs index, index, multiply, accumulate (four ops) while the typed build
runs a single `ACC_MAC_GINT`. Net typed-vs-baseline speedups, measured best-of-six:

- `dotprod`  -- **1.33x** faster typed (the clearest showcase; 4 ops -> 1)
- `matmul` n=70 -- **1.16x** faster typed
- `dotsum`  -- **1.12x** faster typed (one index fused)

All three executors stay byte-identical on every program; CPython runs the
annotated source unchanged. Validated: 43-program differential regression, the
global-escape and aliasing stress tests, all benchmarks (including nbody) and
testfast.

### Why the big memory consumers stay put (objects 50 MB, nbody 46, collatz 23)

Each is an accumulator whose *initial* value cannot be proven uniquely owned, so
freeing it on the first overwrite is unsound:

- `objects`: `self.balance = balance` in `__init__` aliases a constructor argument
  the caller may still hold.
- `nbody`: `vx = [0.0, 0.0, 0.0]` -- `const_v` returns one shared memoised `0.0`,
  and `BUILD_LIST` stores that same pointer in all three slots, so freeing one slot
  on its first update would corrupt the others and the constant pool.
- `collatz`: `n = i` aliases the live loop counter before the fresh chain updates.

Reclaiming these safely needs either per-value ownership (a refcount or an origin
flag) or flow-sensitivity to prove the aliasing init is dead by the first
overwrite, or a const-marker bit so `_free_v` always skips memoised constants. Each
is a real change to the value model rather than the analysis, and a wrong guess
corrupts memory silently -- so they are deliberately left for a future pass with
proper ownership tracking rather than risked here. The safe, proven lever (all-fresh
accumulators) is now fully exploited for globals, both for memory and for speed.

---

## Status update -- local-variable accumulator reclamation

Global accumulators have been reclaimed since v18; the same treatment now extends
to function locals. `_reclaimable_locals` is a per-function mirror of the global
escape analysis: a local may have its old value freed on reassignment only if every
assignment to it is a freshly built, uniquely-owned value (arithmetic or a cached
small-int constant) and it never escapes -- aliased to another name, returned,
stored, passed to a retaining call, used as a container/attribute base, or bound by
a for/unpack target. Parameters are always excluded, because a parameter aliases the
caller's argument, which the caller may still hold.

The reclaiming store is done as fused opcodes rather than a general reclaiming MOVE,
which keeps it safe against the register machine's temp/local interactions: since
reading a local always copies it into a fresh temporary, a local's register has no
persistent alias, so `loc = loc + x` and `loc = loc - x` can be lowered to a single
`ACC_ADD_L` / `ACC_SUB_L` that reads the local's register, combines, writes it back,
and reclaims the old value in place (and frees the right-hand side too when it is a
fresh arithmetic temp). As with the global accumulate, the fused op reads the local
after evaluating the right-hand side, so it is only emitted for call-free
expressions, preserving CPython's left-to-right order.

Effect: `sort`, whose insertion-sort inner loop runs `i = i + 1` and `j = j - 1`
hundreds of thousands of times on indices well above the small-int cache, drops from
**10.8 MB to 6.4 MB** -- the accumulator half of its allocation. (The remaining
memory is the fresh `j + 1` subscript-index temporaries in `xs[j+1] = xs[j]`; those
can only be freed once the container is known to be a list and not a dict, since a
dict would retain the index as a key -- another place typing could later help.) No
other benchmark has leaking function-local accumulators, so the rest are unchanged.

Validated: 43-program differential regression (cpython == ref == native), four new
local-reclamation stress tests (an accumulator aliased into another local, a
reassigned parameter, a string accumulator for numeric-flag correctness, and a
returned accumulator -- each must, and does, keep the value live), all benchmarks
including nbody, and testfast. The safe all-fresh lever is now exploited for both
globals and locals, for memory and (via fusion) for speed.

---

## Status update -- fused compare-and-branch and loop rotation (v21)

Two control-flow fusions, both safe and broadly applicable, since with gcc -O2's
jump-table dispatch the real cost is the number of opcodes executed per iteration.

**Fused compare-and-branch.** A `while`/`if` whose test is a single rich comparison
(`i < n`, `a == b`, `n % 2 == 0`, `i != j`, ...) previously compiled to a compare
opcode that built a boolean, followed by JUMP_IF_FALSE. These now collapse into one
opcode, `JF_LT`/`JF_LE`/`JF_GT`/`JF_GE`/`JF_EQ`/`JF_NE`, laid out `[op, left, target,
right]` so the jump target stays at the usual field and patching is unchanged. The
branch jumps when the comparison is false (skipping the body), matching the original
compare-then-JUMP_IF_FALSE exactly, including type semantics (it reuses v_cmp /
v_eq_bool). Anything that is not a single comparison -- membership, boolean `and`/`or`,
a bare truthiness test -- falls back to the old two-opcode form.

**Loop rotation.** A `while` is now emitted as a guard test at entry, then the body,
then a copy of the test as a conditional back-edge (`JT_*` / JUMP_IF_TRUE jumping back
when the test is still true), instead of a top test plus an unconditional JUMP at the
bottom. The steady state runs one branch per iteration rather than a branch plus a
JUMP, and the test is evaluated exactly as many times as before, so side effects are
unaffected. `break` still targets the loop end; `continue` now targets the back-edge
(patched as a forward reference), which re-tests just like the original top test.
for-loops keep their existing structure.

Measured native-to-native (best of eight, isolating the bytecode change on a fixed
interpreter binary), the two fusions together: collatz 10.3% faster, dotsum 10.0%,
sieve 8.8%, loops 8.3%, dotprod 7.0%, fib 6.6%, nbody 4.9%, dicts 2.7%, sort 2.0%
(its hot inner condition is a boolean `and`, which does not fuse) -- about 7% overall
on the loop-heavy set. Validated: 47-program differential regression (cpython == ref
== native, including break/continue and the global/local reclamation stress tests),
all benchmarks, and testfast. CPython is still ahead on the call-bound benchmarks
(fib, collatz, sort); narrowing those will want call-frame and 2D-index fusions next.

---

## Status update -- short-circuit `and` fusion and numeric fast paths (v22)

Two more safe wins, again aimed at executing fewer opcodes per iteration.

**Short-circuit `and` in conditions.** A `while`/`if` whose test is `A and B and ...`
previously built a boolean (via the short-circuit expression machinery) and then
branched on it. `_emit_branch_false` now recurses through an `and`, emitting one
(fused) branch per operand straight to the false target -- any false operand jumps
out, exactly preserving Python's left-to-right short-circuit, with no boolean
materialised. Because this can emit several jumps, the helper returns a list and the
`if`/`while` sites patch them all to the same target. A `while` with a compound test
uses the top-test form (its `continue` re-tests at the top); a `while` with a single
comparison still uses the rotated form from v21. `or` is left on the boolean-building
fallback for now. The clearest beneficiary is `sort`, whose insertion-sort inner loop
is `while j >= 0 and xs[j] > key`: it now runs `JF_GE` + `JF_GT` instead of building a
bool, **9.5% faster**.

**Integer fast paths for the numeric arithmetic opcodes.** `ADD_NN`/`SUB_NN`/`MUL_NN`
are emitted by the compiler only when both operands are statically known to be numbers,
but they were still calling the fully general `v_add`/`v_sub`/`v_mul`, which first rule
out string concatenation and list/tuple joining. They now take a direct integer path
when both operands are tagged int and fall back to the general routine otherwise (so
float operands stay correct). This trims a few tag checks from every counter and
small-constant arithmetic op; `dotsum` 5.3% and `sieve` 3.4% see the most benefit.

Measured native-to-native against v21 (best of eight on a fixed interpreter binary):
sort 9.5%, dotsum 5.3%, sieve 3.4%, collatz 2.9%, dotprod 2.4%, nbody 1.6%, others
within noise -- about 2.5% overall, concentrated where `and` conditions or integer
arithmetic dominate. Validated: 47-program differential regression (cpython == ref ==
native), an added `while ... and ...` with continue/break edge case, all benchmarks
including nbody, and testfast.

---

## Status update -- call overhead: lean frame init and direct calls (v23)

Two changes aimed at the per-call cost, which dominates the recursive benchmarks.

**Lean frame initialisation.** Every call set up its frame by writing all `nregs`
registers -- copying arguments into the parameters and zeroing every remaining slot
to None. Named locals occupy registers `0..nlocals-1` and temporaries occupy
`nlocals..nregs-1`, and a temporary is always written by the opcode that produces it
before it is ever read. So clearing the temporary slots is unnecessary: `run_func`
now writes the parameters, clears only the other named locals (preserving the
defined "unbound local reads as None" behaviour), and merely grows the pooled
register list to length without touching reused temp slots. The compiler emits a new
`nlocals` field per function for this. For an expression-heavy leaf like `fib`
(one parameter, several temporaries) this removes most of the per-call setup.

**Direct calls.** A call to a module-level `def` used to load the function object
with LOAD_GLOBAL and then run do_call's runtime type dispatch. The compiler now
records each def's index and, when the callee is a plain name that is a known def,
is never rebound anywhere in the module, and is not shadowed by a local, emits a
single `CALL_FUNC` whose operand packs the function index and argument count
(`fidx*256 + nargs`). It goes straight to `run_func`, skipping both the global load
and the dispatch. Builtins, method calls, reassigned names, and locally-shadowed
names all fall back to the existing `CALL` path, so first-class function use is
unaffected -- verified with a mutual-recursion plus function-reassignment test
(`f = is_even` correctly disables the direct call for `f`).

Measured native-to-native against v22 (best of twelve): fib **17.0% faster**
(register init ~10% and direct calls ~7%), objects 3.6% (its methods still go through
the method-call path but benefit from the leaner frame), sort 1.8% (only two calls,
dominated by the sort itself). fib moves from ~4.9x to ~4.07x CPython. Validated:
47-program differential regression (cpython == ref == native), the mutual-recursion /
reassignment edge cases, all benchmarks including nbody, and testfast. CPython's C
call path is still ahead on deep recursion; closing more would mean passing arguments
straight into the callee frame instead of through the pooled argument list.

---

## Investigation (not landed) -- eliminating the call argument double-copy

A direct `CALL_FUNC` still moves arguments twice: the opcode copies them from the
caller's registers into a pooled `cargs` list, and `run_func` then copies that list
into the callee's parameter registers. The natural fix is to give `run_func` a
`(args, base, nargs)` interface so it reads arguments straight from any register
array at an offset; `CALL_FUNC` then passes the caller's own register list and the
argument base, and the intermediate list disappears. This was implemented and is
correct -- all 48 differential tests pass, including mutual recursion, a
function-reassignment guard, and deep nested multi-argument calls -- and it is a real
algorithmic win on call-bound code: **fib 7.5% faster, a deep multi-arg recursion
benchmark 6.8% faster**.

It was **reverted** because it is net-negative on this toolchain. The register
dispatch loop lives inside `run_func`, so changing the frame-setup code shifts gcc's
code generation for the loop itself, and here that shift regressed every loop-bound
benchmark by 2-9% (loops -3.4%, sort -8.8%, sieve -4.6%, collatz -3.9%, dicts -3.2%),
about -2% across the measured set. Two checks established this is a genuine codegen
effect rather than a clean win:

- Extracting the loop into its own `exec_frame(st, fn, regs)` function (to decouple
  it from the setup) did **not** recover the regression -- the standalone loop
  compiled no better.
- A no-op change to v23 (one comment inside the loop) left loops/sort/sieve within
  0.3%, while the real change moved them 3-9% -- so those benchmarks are not merely
  layout-volatile; the change specifically penalised them.

The underlying cause is the monolithic `if/elif` dispatch: its compiled form is
sensitive to surrounding code, so a per-frame micro-optimisation can pay for itself
on calls yet lose more on the loop body. The double-copy is genuinely the remaining
structural call cost, and on a call-heavy workload (or a threaded/computed-goto
dispatch that pins the loop's codegen) the change would be worth landing; on this
loop-heavy suite with this dispatch shape it is not. The register-init and CALL_FUNC
wins from v23 are kept; fib stays at ~3.8-4.0x CPython.

---

## py2c bit-field support (`T(N)` annotation) -- new transpiler capability

py2c now lowers a field annotated `T(N)` to a C bit field of N bits, so several
small values pack into one word and a struct stays small in the cache (the
TPython packing idea). A field reads and writes as an ordinary `T`; only its
storage is packed. See TRANSPILER.md sec.2 ("Bit-field struct members") and the
runnable `tools/bitfield_example.py`.

```python
class PackedData:
    def __init__(self, a: int, b: int, c: int):
        self.is_active  : int(1) = a    # -> unsigned int is_active  : 1;
        self.status     : int(3) = b    # -> unsigned int status     : 3;
        self.error_code : int(4) = c    # -> unsigned int error_code : 4;
```

Implementation is additive and self-host-safe: the width is recorded per class
(`ClassInfo.bitfields`) and consumed only at struct emission; `ann_text_to_ctype`
resolves `T(N)` to plain `T` so no read/write/cast site changes; bit fields are
excluded from the reflective field table (`offsetof` is illegal on them). No
existing rpython uses `T(N)`, so testfast (157 programs), the 13 benchmarks, and
the 32-program regression suite are all unchanged.

**Where it helps minipy.** The lever is any struct with several small fields that
currently each take a full word. The hot `V` value is *not* an immediate
candidate: it is `tag` + an 8-byte union, so 8-byte alignment pins it at 16 bytes
whether or not `tag` is packed. The natural first uses are metadata structs whose
small integer fields can share a word (e.g. `Func`'s `nparams`/`nregs`/`nlocals`,
or `Cont`'s `kind`/`cursor`), and -- for a real hot-path win -- a future value
representation that stores a small int or short string inline in a packed word
rather than boxing it. That value-rep redesign, together with the boxing-reduction
typing fixes, is the next direction.

---

## py2c feature: C bit fields in RPython (`T(N)` annotations)

py2c now supports C struct bit fields, declared with a `T(N)` field annotation
where `N` is the width in bits:

```python
class PackedData:
    def __init__(self, a: int, b: int, c: int):
        self.is_active  : int(1) = a   # 1 bit  (0..1)
        self.status     : int(3) = b   # 3 bits (0..7)
        self.error_code : int(4) = c   # 4 bits (0..15)
```

emits

```c
typedef struct packed__PackedData {
    unsigned int is_active : 1;
    unsigned int status : 3;
    unsigned int error_code : 4;
} packed__PackedData;
```

That whole struct is **4 bytes** instead of the 12 three plain `int`s would take.
Both the bare form `int(3)` and the string form `"int(3)"` are accepted, in `self.x`
ctor assignments and in class-level annotations.

Semantics and limits:
- The field's *value* type is the plain scalar `T`, so reads and writes behave as
  ordinary integers; only the struct declaration carries the width. A field assigned
  a value that does not fit wraps modulo `2**N` (e.g. `status = 9` reads back `1` in a
  3-bit field) -- this is plain C bit-field truncation.
- Fields are emitted **unsigned** (`unsigned int : N`), matching the intended use of
  packing small non-negative values; the comments in the example (`0..7`, `0..15`)
  reflect that. Signed packing is not provided.
- Widths 1..32 are supported (one `unsigned int` storage unit). A width beyond the
  storage type is rejected by the C compiler, not silently truncated.
- Bit fields are skipped in the reflection field table (C forbids `offsetof` on a bit
  field), so they are not introspectable via the runtime field descriptors. POD
  classes (all-scalar, no `_hdr`) -- the typical use -- have no field table anyway.

Implementation (all in py2c.py): `ann_text_to_ctype` maps `T(N)` to the scalar `T`;
`discover_bitfields(classnode)` records `{field: N}` per class into `ClassInfo.bitfields`;
the two struct-emission sites emit `unsigned int name : N` for those fields; and
`emit_field_table` filters them out. Self-host (`make testfast`) and the full minipy
regression are unaffected.

### Where this helps minipy
Not every struct benefits: `V` (a 4-bit tag plus an 8-byte `iv`/`dv`/`sv` union) and
`Cont` (small `kind`/`cursor` plus two list pointers) are dominated by 8-byte members,
so alignment keeps them at 16 bytes whether or not the small fields are packed. The
real candidate is **`Instr`** (`op`, `a`, `b`, `c` -- four ints, 16 bytes, one per
bytecode instruction). `op` needs 7 bits; `a`/`b`/`c` are register indices that today
carry the fresh-operand flags encoded in by adding 2^30/2^29 (hence the
`if ra >= 1073741824` decode in the dispatch loop). Splitting those flags out into
1-bit fields and storing the small indices alongside `op` would let an instruction fit
in **8 bytes**, halving bytecode footprint (better I-cache on the hot loop) and
replacing the decode arithmetic with direct field reads. That is a larger change that
edits the dispatch loop -- subject to the codegen-fragility caveat noted above -- so it
is left as a deliberate follow-up rather than bundled with the codegen feature here.

---

## v24 -- packed `Instr` (8-byte bit-field instruction)

The first use of the new bit-field feature inside minipy. `Instr` was four plain
ints (`op`, `a`, `b`, `c` = 16 bytes); it is now a packed bit-field struct of 8
bytes:

```c
unsigned int op : 8;   unsigned int fb : 1;   unsigned int fc : 1;
unsigned int ra : 22;  unsigned int b : 16;   unsigned int c : 16;   // two units, 8 bytes
```

The compiler had been packing the free-operand hints into the high bits of `a`
(bit 30 = free reg c, bit 29 = free reg b), which the dispatch loop decoded on
*every* instruction (`ra = a; if ra >= 2**30 ...; if ra >= 2**29 ...`). Those hints
are now split out **once at compile time** into the `ra`/`fb`/`fc` fields, so the
loop reads them directly and the per-iteration decode is gone. Field widths were
chosen from the actual operand ranges across every benchmark and regression program
(max stripped reg 217, max b 230, max c 22272), leaving large margins: ra < 4M,
b/c < 65536.

Measured field-value ranges fit with headroom; the layout packs into exactly two
32-bit storage units (op+fb+fc+ra = 32, b+c = 32).

### How it threads through the existing machinery
The native JSON decoder is generated to fill struct fields *by name*, not by calling
`__init__` -- so the constructor's body never runs at load time. The compiler
therefore emits the already-split `ra`/`fb`/`fc` keys in the bytecode JSON
(`_instr_json` in compiler.py), **and keeps `a`** so the pure-Python ref VM and the
disassembler -- which read `a` and strip the hints themselves -- need no changes.
`Instr.__init__` just lists the six fields with their bit widths for py2c's field
discovery. Only the dispatch-loop preamble changed in interp.py (read ra/fb/fc from
the struct instead of decoding `a`).

### Result
8-byte vs 16-byte `Instr` on identical bytecode (native-to-native, isolating the
change): **every benchmark faster, none regressed**, with the speedup tracking how
dispatch-bound each one is:

| collatz | fib  | sort | loops | sieve | dicts | dotsum | dotprod |
|---------|------|------|-------|-------|-------|--------|---------|
| +14.5%  |+12.8%| +9.3%| +8.9% | +6.8% | +4.6% | +2.8%  | +0.1%   |

Cheap-instruction loops (collatz, fib, loops) gain most; arithmetic-bound kernels
already dominated by their fused math (dotprod, dotsum) gain least. Unlike the
reverted double-copy experiment, this change *removes* work from the hot loop and
*shrinks* the data it streams, so there is no codegen-fragility penalty -- the effect
is uniformly positive. All 49 regression programs stay byte-identical
(cpython == ref == native), nbody matches, and `make testfast` (self-host) passes.
This is the first concrete payoff of bit fields for interpreter speed; the same
representation leaves room to widen `op` or repurpose spare bits later.

---

## v25 -- de-boxing via a py2c int/long unification fix (big win)

The largest single speedup so far, and it came from fixing a type-inference bug in
py2c rather than changing minipy. py2c infers one C type per local per function by
reconciling the types of all its assignments; when two assignments disagreed it fell
straight back to a boxed `obj` (`hoist_locals.consider`: `types[name] = OBJ`). But
the dispatch loop's program counter `pc` is assigned an `int` almost everywhere
(`pc = 0`, `pc = pc + 1`, `pc = b`) and a `long` in one place -- the exception-handler
jump `pc = blocks[bn].iv` (`.iv` is a `long`). int + long "disagree", so `pc` was
boxed. The cost was brutal: **every instruction** ran `pc = obj_add(pc, OBJ_INT(1))`
(a dynamic add that heap-allocates a fresh boxed integer once pc passes the small-int
cache) and `obj_cmp(pc, OBJ_INT(n))` for the loop test.

The fix is a three-line addition: when two inferred integer types disagree, widen to
the larger signed integer type (`_int_widen`: char < short < int < long) instead of
boxing. `pc` becomes a plain `long` -- `pc < n`, `pc + 1`, no per-instruction
allocation -- and so does every other int/long local across the interpreter.

### Result
Same interpreter source, recompiled with the fixed py2c (boxed `pc` vs `long pc`,
identical bytecode, same machine):

| fib | loops | collatz | sieve | sort | dicts | dotsum | **avg** |
|-----|-------|---------|-------|------|-------|--------|---------|
|+20% | +29%  | +26%    | +28%  | +35% | +15%  | +40%   | **+31%** |

Current cpython-relative standing afterwards: fib 1.9x (from ~3.8x), matmul
0.3-0.4x, dotprod_typed / dotsum_typed 0.8x (faster than CPython), dotsum 1.0x,
loops 1.2x, sieve 1.8x, dicts 2.0x. All 51 regression programs stay byte-identical
(cpython == native), nbody matches, and `make testfast` (self-host) still passes --
the widening only triggers when both inferred types are signed integers, so anything
involving `obj` or a non-integer is untouched.

This is the boxing-reduction direction paying off concretely: a single correct
widening rule in py2c removed a heap allocation from the interpreter's innermost loop.
The same class of fix (reconciling concrete types instead of boxing) likely helps
elsewhere -- worth auditing other `-> obj` fallbacks in py2c next.

---

## Toward running py2c.py on minipy -- syntax gap analysis

A full AST-node audit of py2c.py against minipy's compiler shows the compiler already
handles the overwhelming majority of what py2c uses. Notably, **f-strings are not used
by py2c.py at all**, and `Slice` (151 uses) is already supported (it is handled inline
in `ex_Subscript`, verified working end-to-end for lists and strings). This turn added
`Global`/`Nonlocal` (declared names are excluded from a frame's locals so reads/writes
route to the module global -- py2c uses `global` in a few spots).

Remaining compiler-side syntax gaps, by use count in py2c.py: `Import`/`ImportFrom`
(19), `With` (8), `Lambda` (9), generators (`Yield`/`YieldFrom`, 5), `Starred`/`*args`
(3). Each is a discrete, achievable feature.

The real blocker to *running* py2c.py is not syntax but **the standard library**: py2c
imports `ast`, `re`, `sys`, and `os`, and its whole purpose is to parse Python via
`ast`. Making minipy execute py2c therefore requires a module/import layer plus
minipy-native implementations (or ports) of at least `ast` (a Python parser), `re`,
`sys`, and `os` -- a substantial project in its own right, well beyond the syntax
features. The syntax work above is necessary but not sufficient; the stdlib layer is
the long pole.

---

## Variadic functions -- `*args`

minipy now supports `*args` parameters (`def total(*nums): ...`,
`def label(prefix, *rest): ...`). The trailing positional arguments are collected
into a tuple bound to the starred parameter; calling with no extra arguments binds
an empty tuple, exactly like CPython.

The implementation is deliberately contained so it cannot perturb the hot paths:

- The compiler appends the starred name to the parameter list, records its register
  in a new per-function `vararg` field (-1 when absent), and gives that slot no
  default. Defaults still align to the regular parameters.
- `Func` carries `vararg`; the native run-time fills it from the bytecode JSON like
  any other field.
- Only `run_func`'s argument-binding prologue changed: when `vararg >= 0`, the
  parameter at that index collects `args[vararg:]` into a tuple. Every existing
  (non-variadic) function has `vararg = -1`, so its binding -- and the entire
  dispatch loop and call fast-path -- is byte-for-byte unchanged. Measured: the
  de-boxing speedups are fully intact (fib +23%, sort +40%, dotsum +42% vs the
  boxed baseline), with no call-path regression.

Verified end-to-end (cpython == ref == native) for zero, partial, and many trailing
arguments; all 52 regression programs pass and `make testfast` stays green.

Remaining toward parsing all of py2c.py: `With`, `Lambda` (needs nested-function /
closure support, which minipy does not yet have), generators (`Yield`), call-site
`*expr` unpacking, and `Import`. Running py2c.py still additionally requires the
stdlib layer (`ast`, `re`, `sys`, `os`) -- the long pole.

---

## v26 -- direct list access for the dispatch loop (`_lget`/`_lset`)

With safe integer de-boxing exhausted (every index in `run_func` is now `int`/`long`,
and `fn.*`/`ins.*` already compile to direct struct reads), the remaining
per-instruction cost was the generic `subscript()` call used for every register and
instruction-fetch access. Each `regs[a]` compiled to `subscript(regs, OBJ_INT(a))` --
a cross-TU call that dispatches on container type and then calls `list_get()`, which
itself handles negative indices and bounds. An arithmetic instruction pays this ~4
times (fetch `code[pc]`, read `regs[b]`/`regs[c]`, write `regs[ra]`).

LTO was tried first (let gcc inline `subscript`/`list_get` across translation units)
and rejected: it was a wash-to-negative because cross-TU inlining bloats and reshuffles
the fragile monolithic dispatch loop's codegen (fib +4% but collatz -8%, sieve -9%).

Instead, two opt-in py2c intrinsics were added -- `_lget(lst, i)` and
`_lset(lst, i, v)` -- that lower directly to `((List*)AS_OBJ(lst))->data[i]` (a raw
array load/store), skipping the container dispatch and the negative/bounds handling.
This is unchecked, so it is only valid where the index is provably non-negative and in
range; the bytecode interpreter's register and pc accesses are exactly that (register
indices `< nregs`, `pc < n`). The dispatch loop's `regs[...]` accesses (132 reads, 57
writes) and the `code[pc]` fetch were rewritten to use them.

### Result
Removing the calls -- which *shrinks* the hot loop rather than inlining bodies into it
-- helps uniformly, with no codegen-fragility penalty:

| fib | loops | collatz | sort | dotsum | sieve | matmul | dicts | **avg** |
|-----|-------|---------|------|--------|-------|--------|-------|---------|
|+34% | +32%  | +37%    | +41% | +28%   | +28%  | +25%   | +11%  | **+32%** |

All 52 regression programs stay byte-identical (cpython == native), nbody matches, and
`make testfast` (self-host) passes -- the intrinsics only fire on literal `_lget`/`_lset`
calls, which py2c.py itself never makes.

Stacked on the v25 de-boxing, the dispatch loop is now roughly twice as fast as it was
two versions ago. Extending the same rewrite to `st.heap`/`st.glob` *together* was a
wash here -- but see v27: `st.glob` alone is a clean win, and `st.heap` is the part that
hurts.

---

## v27 -- `as_long` typing fix + `st.glob` direct access

Two more passes at the same lever, after confirming from the generated C that integer
de-boxing is fully exhausted (the hot loop has *zero* `obj_add`/`obj_sub`/`obj_mul` and
*zero* box/unbox roundtrips like `pyint(OBJ_INT(...))` left anywhere in `interp.c`).

**`as_long` typing fix (py2c).** `as_long(node)` rendered any value whose C type was not
literally `int`/`bool` by boxing then unboxing it -- `pyint(OBJ_INT(pc))` for a `long`.
The fetch `code[pc]` runs every instruction, so this roundtrip sat on the single hottest
line in the interpreter. The fix reuses the v25 signed-integer set: a value already typed
as any C integer (`char`/`short`/`int`/`long`/`bool`) is emitted directly, so the fetch
becomes a clean `data[pc]`. gcc was *not* folding the roundtrip away -- removing it gave
a uniform **+13.6%** (dotsum +19.8%, fib +13%, loops +12%) with no regressions. This is a
general py2c improvement (every long-typed index across all compiled code), and
`make testfast` self-host stays green.

**`st.glob` direct access.** With regs/code already direct (v26), the remaining
`subscript()` calls were all on `st.glob` (every LOAD_GLOBAL/STORE_GLOBAL) and `st.heap`
(object/dict ops). `st.glob` is fixed-size after program load, so the same unchecked
`_lget`/`_lset` rewrite is safe there. Applied to `st.glob` alone it is a clean
**+10.3%** (dotsum +22%, sort +15%, matmul +16%, loops +14% -- these keep accumulators or
arrays in globals), with fib/collatz flat.

`st.heap` was tried on top and *reverted*: it regressed fib by ~20% and even hurt the
heap-heavy benchmarks it was meant to help (objects, wordfreq). The heap accesses live in
branches whose inline expansion reshuffles the layout-sensitive dispatch loop badly. So
the direct-access rewrite stays scoped to `regs`, `code`, and `st.glob` -- the three
always-valid, non-growing index sites. `st.heap` would need a less layout-sensitive
dispatch (e.g. threaded/computed-goto) to pay off.

### Cumulative (this stage: v25 -> v27)

| fib | loops | collatz | sort | dotsum | sieve | dicts | matmul | **avg** |
|-----|-------|---------|------|--------|-------|-------|--------|---------|
|1.79x|1.95x  |1.81x    |1.82x | 2.16x  |1.73x  |1.21x  |1.78x   |**1.88x** |

De-boxing (v25) removed the per-instruction arithmetic *calls*; direct list access
(v26/v27) removed the per-instruction indexing *calls*; the `as_long` fix removed the
last per-instruction boxing roundtrip. Together they nearly halve dispatch time. All 52
regression programs stay byte-identical to CPython, nbody matches (12982328), and
self-host (`make testfast`) passes.

---

## v28 -- running a real Python parser on minipy (stdlib/self-host step)

The long pole for self-hosting (eventually running `py2c.py` itself on minipy) was
never the syntax -- it is the standard library, above all an `ast` to turn Python source
back into a tree. `tools/rpy_lib/rast.py` is that piece: a PEG Python parser (ported from
pymetaterp, de-`eval`'d into explicit semantic-action dispatch so it is py2c-portable). It
is import-free and uses only minipy-supported constructs -- the `yield`/`lambda` tokens in
it are *grammar rules for parsing* Python that contains them, not constructs the parser
itself uses -- so `compiler.compile_file` accepts it as-is.

**Result:** minipy compiles and runs the parser, and its output is byte-identical to
CPython across all three executors. `tools/rpy_lib/rast_test.py` parses ten Python
snippets (operator precedence, `def`, `if/else`, `while`, `for`, lists, dicts, calls,
boolean/compare chains) and dumps each Node tree:

```
PASS: cpython == ref == native  (10 snippets, 172 output lines)
```

This is the first end-to-end run of a non-trivial real-world Python program on the native
py2c-compiled interpreter -- exercising classes, methods, recursion, exceptions,
dict/list comprehensions, generator expressions and heavy string work all at once.

**One fix was needed to make it practical, not to make it correct.** `parse_python`
re-bootstrapped the entire 3-stage grammar (meta-grammar -> grammar -> Python grammar) on
*every* call. Under minipy's free-once bump arena (no GC; nothing is reclaimed until the
program exits) that meant each parse re-allocated the whole grammar, and a handful of
parses exhausted even the 1 GiB arena (overflow currently faults rather than erroring
cleanly -- a separate robustness TODO). Since the grammar stages are source-independent
and `match()` resets its per-source state on each top-level call, the Python-grammar
interpreter is now built once and cached (`_python_interp()`), turning N parses from
N x (grammar + tree) into grammar-once + N x tree. The ten-snippet test then fits the
default arena comfortably.

### Remaining gaps to *running py2c.py itself*
Confirmed by feeding each to the compiler:
- **`with`** -> needs `try/finally` first (minipy's `st_Try` still rejects `finalbody`); a
  correct `with` must call `__exit__` on the exception path, so finally is the real
  prerequisite. A cleanup-skipping `with` was deliberately *not* shipped.
- **generators (`yield`)** and **`lambda`** -> both need nested-function / closure support,
  which minipy still lacks; these stay the larger items.
- The rest of the stdlib `py2c.py` leans on (`re`, `sys`, `os`) remains, but `rast.py`
  shows the `ast` half of the path is real and runs natively today.

---

## Basic minipy rules (what it deliberately does *not* support)

minipy is meant to stay small and fast, not to cover every corner of Python. As a
standing rule it does **not** implement `yield` (generators) or `async`/`await`. These
add a second control-flow model (suspendable frames / an event loop) that would bloat the
register dispatch loop and the value model for little gain in the workloads minipy targets.

Consequences and the chosen alternatives:
- **Generator expressions / comprehensions are still fully supported** -- they are desugared
  *eagerly* into a built list (`sum(x*x for x in xs)`, `all(... for ...)`, list/dict/set
  comprehensions with filters all work and match CPython).
- **py2c's own generators are written as list-builders.** The transpiler had one real
  generator, `_walk_live` (a branch-aware `ast.walk`); it now returns an eager pre-order
  list instead of `yield`-ing. Same order, same callers (`for n in self._walk_live(t)`),
  and self-host (`make testfast`) still passes. py2c now contains zero `yield`s, one step
  closer to compiling under minipy itself.

## v29 -- `try/finally`

`try/finally` (and `try/except/finally`) now compile, with **no new opcodes and no
interpreter change** -- it is expressed entirely in terms of the existing
`SETUP_EXCEPT`/`POP_BLOCK`/`RERAISE`. An outer catch-all wraps the body and runs the
finally then re-raises; the normal fall-through path runs a copy of the finally; and
`return`/`break`/`continue` inside the body run the pending finallys (tracked on a per-
function `_finallys` stack) before they exit. `try/except/finally` is handled by nesting
the existing except logic inside the catch-all.

The finally therefore runs on every exit path -- normal completion, caught/propagated
exception, `return`, and loop `break`/`continue` -- including correctly ordered nested
finallys (inner before outer). Verified byte-identical across CPython, the ref VM and the
native interpreter (`tools/minipy/test_tryfinally.py`), and all 52 regression programs
plus the existing `try/except` suite still pass.

This unblocks a *correct* `with` (desugar to manager-protocol + `try/finally`, so
`__exit__` runs on the exception path too). The one remaining wrinkle for `with` is giving
the context-manager temp a real local slot: minipy collects locals up-front, so the
desugaring must either run before local collection or hold the manager in a reserved
register -- a small, self-contained next step.

---

## v30 -- `with` statement

`with` now compiles, built entirely on v29's `try/finally` -- again with no new opcode and
no interpreter change. An AST pass (`_WithDesugar`, run right after parse, before local
collection) rewrites it to the manager protocol wrapped in try/finally:

```
with A as a:          _with_N = A
    BODY        ->    a = _with_N.__enter__()
                      try:
                          BODY
                      finally:
                          _with_N.__exit__(None, None, None)
```

Running the desugaring before `_collect_locals` is what closes the wrinkle noted in v29:
the `_with_N` manager temp is picked up as an ordinary local, so it gets a fixed register
slot and is per-call / recursion-safe (rather than leaking to a global). Multiple items
nest left-to-right, so `__exit__`s fire in reverse order; nested `with`s are handled by
desugaring inner ones first.

Because the body is under try/finally, `__exit__` runs on **every** exit path -- normal
completion, a propagating exception, and `return` from inside the body. Verified
byte-identical across CPython, the ref VM and the native interpreter
(`tools/minipy/test_with.py`): normal, no-`as`, exception-in-body, return-in-body,
multiple managers (LIFO exit order) and nested `with` all match, and the full regression
plus the rast parser self-test still pass.

**v0 limitation:** `__exit__` is called with `None, None, None` and its return value is
ignored, so a manager always runs its cleanup but cannot inspect or *suppress* the
exception. That is the standard cleanup case (locks, timers, buffers); exception-aware /
suppressing managers would need the exception triple threaded into the finally, a later
refinement.

---

## v31 -- builtin exception types

`raise ValueError(...)` and stdlib-style `except ValueError:` / `except Exception:` now
work. Previously the only exceptions were user classes, and names like `ValueError`
resolved to nothing (`raise ValueError(...)` failed with "not callable: None") -- and
`class Err(Exception)` quietly got *no* base, because `Exception` itself wasn't a class.

This is implemented **entirely in the compiler, with no interpreter change**: the
interpreter's `is_instance` already walks a class's `.base` chain and `instantiate`
already works for a class with no `__init__`, so builtin exceptions are just pre-registered
classes. A hierarchy table (`_BUILTIN_EXC`: `BaseException` -> `Exception` -> `ValueError` /
`TypeError` / `LookupError` -> `KeyError`/`IndexError`, etc.) is materialised as real
classes at module start, before any user code, with their `.base` links wired up. So:

- `raise ValueError("x")` instantiates a real `ValueError`; `raise RuntimeError` (bare)
  works too.
- `except ValueError:` matches by walking the instance's base chain; `except Exception:`
  catches every builtin (and user) exception; clause order and propagation behave like
  CPython.
- `class Err(Exception)` now gets a real base, so a user exception deriving a builtin
  (`class MyErr(ValueError)`) is caught by `except ValueError:`.

Registration is **demand-driven**: a pre-scan registers only the builtin exception names a
program actually references, plus their ancestor chain (needed for matching). Programs that
never touch exceptions (e.g. the numeric benchmarks) register zero of them, so there is no
bloat. Verified byte-identical across CPython, the ref VM and native
(`tools/minipy/test_builtin_exc.py`), with all 55 regression programs -- including the
existing user-exception suite -- and the rast parser still passing.

**v0 limitation (unchanged from user exceptions):** the constructor argument is not
stored, so `str(e)` gives `<ValueError object>` rather than the message, and there is no
`e.args`. Control flow (raise / match / propagate) is exact; surfacing the message is a
separate, interp-side enhancement (`instantiate` storing args + `str`/`.args` reading them)
and is the natural next step -- it is also what a *suppressing* `with` manager would want,
alongside threading the live exception into `__exit__`.

---

## v32 -- exception messages (`str(e)` and `e.args`)

The v31 limitation is gone: a raised exception now carries its message. When an exception
class with no user `__init__` is instantiated, `instantiate` records the constructor
arguments as `.args` (a tuple), so:

- `str(ValueError("bad"))` -> `"bad"` (the message), `str(RuntimeError())` -> `""`,
  `str(ValueError("a", "b"))` -> `"('a', 'b')"` -- matching CPython's rules (one arg ->
  the message, none -> empty, many -> the args tuple).
- `e.args` works (`e.args[0]`, `len(e.args)`), because `.args` is a real attribute.
- The CPython `KeyError` quirk is reproduced: `str(KeyError("k"))` -> `"'k'"` (KeyError
  alone stringifies via `repr` of its key).

Both executors were updated to match: the native interpreter (`instantiate` stores `.args`;
`to_disp` formats an exception instance from them, with a `KeyError` chain check) and the
ref VM (`_invoke` stores `.args`; `_pystr` formats them). "Is this an exception?" is just
"does the class's base chain reach `BaseException`", reusing the v31 hierarchy -- no new
per-object state in the value model. Verified byte-identical CPython == ref == native
(`tools/minipy/test_exc_msg.py`), with the full regression, nbody, and the rast parser
still passing.

*(Aside: the ref VM miscounts one string benchmark vs CPython -- a pre-existing ref-only
bug, unaffected by this change; the native interpreter is correct there.)*

This is the half of "exception-aware `with`" that concerns the exception *object*. The
other half -- a *suppressing* manager -- is now well-defined: change the `with` desugaring
from a plain `try/finally` to `try/except/else`, pass `(type(e), e, None)` to `__exit__` on
the exception path, and skip the re-raise when `__exit__` returns truthy.

---

## v33 -- exception-aware / suppressing `with`

The v30 `with` limitation is gone: a context manager's `__exit__` now receives the live
exception and can suppress it. The desugaring moved from a plain `try/finally` to the full
PEP-343 shape, built on v29's `try/except/finally` and v32's real exception objects:

```
with A as a:        _with_N = A
    BODY        ->  a = _with_N.__enter__()
                    _hit_N = False
                    try:
                        BODY
                    except BaseException as _exc_N:
                        _hit_N = True
                        if not _with_N.__exit__(type(_exc_N), _exc_N, None):
                            raise
                    finally:
                        if not _hit_N:
                            _with_N.__exit__(None, None, None)
```

The `_hit_N` flag is the PEP-343 trick: the finally runs the clean
`__exit__(None, None, None)` only when no exception was raised, so normal completion,
`return`, `break` and `continue` all get it (via the finally), while an exception goes
through the handler -- which passes `(type(e), e, None)` and re-raises **only if
`__exit__` returns falsy**. A truthy result therefore suppresses the exception and
execution continues after the `with`. (Traceback is `None`; minipy has no traceback
objects.)

Verified byte-identical CPython == ref == native (`tools/minipy/test_with_suppress.py`):
clean exit, a suppressing manager (execution continues past the `with`), a non-suppressing
manager (exception propagates and is caught outside), `__exit__` seeing the right
exception object (`exc=boom`), and `return`-in-body still getting the clean exit. Full
regression, rast parser and self-host all still pass.

A small supporting fix: `except E as name:` now registers `name` as a real local (it was
previously treated as a global), so the desugaring's `_exc_N` -- and any user `except ...
as e` -- is per-call and recursion-safe.

---

## v34 -- negative-step slicing (`s[::-1]`), and a ref-VM oracle fix

Chasing the one benchmark where the ref VM disagreed with CPython (`strings`: 480000 vs
896000) turned up a real feature bug, not just an oracle glitch: **negative-step slicing
was broken in every executor**, and the benchmark only happened to mask it because it
checks slice *lengths*, not contents.

- The native interpreter forced `step <= 0` to `1` ("v0: positive step only"), so
  `"alpha"[::-1]` iterated *forwards* -> `"alpha"` (wrong content, right length).
- The ref VM rewrote an omitted stop to `len(seq)`, which is the wrong default for a
  negative step, so `"alpha"[::-1]` came out **empty** -> the 480000.
- The compiler defaulted an omitted *start* to `0`, also only correct for positive step.

Fixed across all three to CPython's `slice.indices()` semantics: an omitted bound is now
emitted as a `None` sentinel (not a hardcoded `0`/end), and each runtime resolves the
defaults from the **sign of the step** -- start defaults to `len-1` and stop to "before 0"
when stepping backwards, and the iteration walks `while k > hi` for negative step. The ref
VM, running on real Python values, simply passes the sentinels straight into Python's own
slicing. So `s[::-1]`, `s[::-2]`, `s[5:1:-1]`, list reversal `xs[::-1]`, and the existing
forward/`negative-index`/step cases all now agree byte-for-byte.

Verified CPython == ref == native (`tools/minipy/test_slice.py`) across reverse, reverse-
step, bounded-reverse, forward, step, negative-index and list slices; the full regression
now passes **3-way with no exceptions** (the `strings` ref divergence noted in v32 is
gone), and rast + self-host still pass.

---

## v35 -- German-string-inspired string comparison (and why not the full thing)

We looked at adopting "German strings" (the Umbra/CedarDB 16-byte view: a 4-byte length, a
4-byte inline prefix, and either 8 inline bytes or a pointer) to speed minipy's string
work. The conclusion: take the part that pays, skip the part that doesn't.

**Why the full 16-byte representation is not a fit here.** minipy's `V` is *already* a
16-byte tagged union (`tag` + an 8-byte slot holding `iv`/`dv`/`sv`). A German string needs
all 16 bytes for the string itself, so it cannot live in the 8-byte `sv` slot. The options
are both bad for this interpreter:
- *Grow `V` to 24 bytes* -- every value (ints, floats, list elements, registers) gets
  bigger and the dispatch loop moves more memory. The whole v24/v25 effort went the other
  way (32B -> 16B) precisely because value size dominates.
- *Point `sv` at a separate 16-byte string object* -- adds an indirection to every `s[i]`
  and keeps two allocations for long strings. And minipy's strings are overwhelmingly
  *constants* (literals, grammar tokens), already interned once by the loader, so the
  inline-short-string allocation win mostly doesn't apply.

**The part that pays: prefix comparison.** A German string's real speed win on
comparison-heavy work is rejecting unequal strings without a full scan. minipy's `_strcmp`
did the opposite -- it called `len()` (i.e. `strlen`) on *both* operands before comparing,
two full scans up front -- and it is the hot core behind every `==`, dict-key probe, and
attribute/method-name lookup (9 call sites). Rewritten as a single null-terminated pass it
stops at the first differing byte, which for short keys is usually byte 0 or 1 -- the same
early-out a prefix compare gives, with no representation change. `truthy("...")` likewise
no longer `strlen`s just to test non-empty; it checks the first byte.

**Measured (native, same-load):**

| objects | bintree | wordfreq | dicts | rast parse | fib | sort |
|---------|---------|----------|-------|-----------|-----|------|
| +13.4%  | +13.3%  | +9.8%    | +2.7% | +4.7%     | +4.3% | +1.1% |

The big movers are exactly the attribute / method / dict-key lookups that scan item names.
All 55 regression programs stay byte-identical (cpython == native), the rast parser stays
3-way identical, and self-host passes. (Caching an explicit length -- the other German-
string benefit -- would only help bare `len(s)` calls, which aren't hot once compares
early-exit, so it isn't worth the representation churn yet.)

---

## v36 -- a minijit: repurposing the tag's dead bytes as an inline-cache key

`V`'s tag only ever holds 0-15, yet it occupied a full 4-byte `int` (the other
3 bytes pure padding). We split it the way the value-representation literature
suggests: a 2-byte `tag` and a 2-byte **`jitcode`** scratch slot, union unchanged,
so `V` stays exactly 16 bytes.

```c
typedef struct V { short tag; short jitcode; union { long iv; double dv; char* sv; }; } V;
```

The struct change alone is free (within noise on every benchmark) -- a `short` tag is a
single 2-byte load, no worse than the `int`.

**What the "jit" does.** A real machine-code JIT is a much larger thing; what pays here is
the technique JITs use to make dispatch fast -- an **inline cache**. `jitcode` holds the
*class id* of an object value, written once at object creation (`instantiate`, and the
bound-method `self` reconstruction) and carried along for free by the by-value `V` copy
(it can't drift, since an object never changes class -- which is exactly why a string
*length* couldn't live here: by-value copies would desync). Method dispatch then keys a
monomorphic cache by the method-name const id:

```
CALL_METHOD obj.m(...):  cls = obj.jitcode                 # class id, no heap deref
                         if mcache[nameid].cls == cls: fidx = mcache[nameid].fidx   # hit
                         else: fidx = lookup_method(cls, m); mcache[nameid] = (cls, fidx)
```

On a hit -- the overwhelming case for monomorphic call sites -- this skips `lookup_method`
entirely: no base-chain walk, no `_strcmp` scan over the class's method table. The cache
lives on `St` as two `list[V]` slabs sized to the consts table (struct-field `list[int]`
is one of py2c's broken paths; boxed `V` with `_lget`/`_lset` is the proven one), filled
with `-1` sentinels at startup.

**Layout lesson.** Inlining the ~8-line cache check straight into the dispatch loop won
+11% on `objects` but cost `sort` -7% -- the monolithic loop is jump-table-layout
sensitive, and the extra code shifted unrelated handlers. Moving the cache into a one-call
helper (`mcache_lookup`) kept the loop footprint identical to the old single
`lookup_method` call: the `objects` win held and `sort` returned to baseline.

**Measured (native, best-of-15, same load):**

| objects | dicts | fib | ack | (others) | total |
|---------|-------|-----|-----|----------|-------|
| +10.4%  | +4.2% | +2.7% | +1.0% | within noise | +3.0% |

`objects` is the OOP-heavy case where `lookup_method` dominated; the gain is concentrated
exactly there and costs nothing elsewhere. All 55 regression programs stay byte-identical
(cpython == native == ref VM -- the cache is transparent), nbody = 12982328, rast is 3-way
identical, and self-host passes. (`bintree` is flat: its single small class made
`lookup_method` cheap already, so there was nothing to cache away.)

---

## v37 (investigated, not shipped) -- attribute inline cache doesn't pay

The natural follow-on to the v36 method cache was to apply the same idea to `LOAD_ATTR`:
cache `(class -> slot offset into items[])` keyed by the attr-name const id, so attribute
reads skip `inst_get`'s linear `_strcmp` scan. I built it, measured it carefully, and
reverted it. The negative result is worth recording.

**Why methods and attributes are not symmetric.** A method is a property of the *class*:
`lookup_method(class, name)` is deterministic, so caching `class -> fidx` is exact and needs
no check on a hit -- that's why v36 wins ~10%. An attribute's *offset*, by contrast, is a
property of the *instance*: two instances of one class can diverge in layout (e.g. a
data-dependent `__init__` assigns attributes in different orders), so a cached offset must
be re-confirmed with one `_strcmp` at that offset before it can be trusted. Because that
verify is mandatory, the class/`jitcode` key adds nothing for attributes -- the offset cache
plus verify is the whole mechanism, and dropping the class check was strictly faster.

**Why even the verified offset cache loses.** The verify is itself a `_strcmp`. So the cache
fast path costs `items_of + offset-load + bounds-check + one _strcmp`, versus a scan that
costs `one _strcmp per attribute until found`. The cache only comes out ahead when the
target sits *deep* in `items[]`. But minipy objects are small: `objects.py`'s `Account` has
a single attribute at offset 0, so its scan is already one `_strcmp` found immediately, and
the cache is pure overhead on top of that.

**Measured (v36 -> v37, best-of-30, repeated):** `bintree` (deeper node access) gained a
stable +6%, but `objects` (one attribute, the primary OOP benchmark) regressed -2% to -8%,
and the monolithic dispatch's layout sensitivity added noise on unrelated benchmarks. A
change that slows the common shallow-attribute case to speed a rarer deep one -- with no
net win across the suite -- isn't worth the dispatch-loop real estate, so it stays out.

The lesson generalizes: an inline cache pays only when the thing it replaces is *expensive*.
`lookup_method` (base-walk + full method-table scan) was; a one-or-two element attribute
scan with the v35 single-pass `_strcmp` already isn't. The jitcode class-id key remains
exactly the right tool for v36's method dispatch, and the wrong tool here.

---

## v38 -- the real-app benchmark: minipy2c.py + rast.py (parse + transpile)

The micro-benchmarks each stress one path, and at 0.02-0.4s their absolute times are small
enough that dispatch-loop *layout* shifts (jump-table reordering from unrelated code changes)
swamp real algorithmic effects -- which is exactly the noise that made the attribute-cache
verdict hard to read. The fix is a single heavy, realistic workload that exercises the whole
interpreter at once, so an optimization's effect on it actually reflects real-world impact.

**minipy2c.py** is a *mini* version of tools/py2c.py: a Python->C transpiler written in the
minipy subset. It consumes the Node tree that **rast.py** (the PEG Python parser) produces and
walks it to emit C -- functions, params, `if/elif/else`, `while`, assignments (declare vs
reassign, tracked in a list), `return`, calls and binary expressions. Because minipy has no
imports, the runnable benchmark is the concatenation `rast.py + minipy2c.py + driver`, built
by `tools/rpy_lib/build_minipy2c_bench.py` into `benchmarks/minipy/minipy2c.py`. The driver
parses and transpiles a real four-function program (recursion, loops, branches, arithmetic)
N=15 times and checksums the emitted C.

This is the truest thing minipy runs: a parser and a compiler, end to end, entirely on the
native interpreter -- classes, methods, deep recursion, list work and heavy string building
all firing together. It validates 3-way like everything else:

  * `tools/rpy_lib/minipy2c_test.py` -- prints the emitted C for five snippets and asserts
    cpython == ref VM == native, byte-for-byte.
  * `benchmarks/minipy/minipy2c.py` -- the checksum benchmark; cpython == ref == native = 6360.

**Characteristics.** ~1.5s native, ~1.0% run-to-run noise (on par with the micro-benchmarks
in *relative* terms, but exercising far more of the interpreter). N is capped at ~35 by the
no-GC bump arena: each `parse_python` call leaves ~30 MB of PEG matcher state that is never
reclaimed (a genuine minipy property this benchmark surfaces -- it is memory-bound in a way
none of the micro-benchmarks are), so N=15 keeps a comfortable margin under 1 GiB. For larger
N, build the interpreter with `-DSHIVYC_ARENA_LOG2=31` (2 GiB).

From here, optimizations should be judged primarily on minipy2c: it is the workload that
behaves like a real program, and the one where a real win shows up as a real number.

---

## v39 -- fixing rast.py's per-parse memory, and a parser speedup

v38 noted minipy2c was memory-bound: each `parse_python` left ~30 MB the free-once arena
never reclaims, capping the loop near N=35. Two fixes, one structural and one in rast.py
itself.

**Do the PEG step once.** The benchmark re-parsed the *same* source every iteration, but a
fixed source always yields the same tree, so the parse only needs to happen once. minipy2c
now exposes `transpile_tree(tree)` (the parse-free half of `transpile`), and the benchmark
parses the sample a single time and transpiles the cached tree N times. The dominant
allocation -- the PEG matcher's backtracking garbage (every failed alternative builds
`outputs` lists and Nodes that are never freed) -- is inherent to a GC-less PEG parse and
can't be cheaply reclaimed, so the fix is simply not to pay it per iteration. The arena
ceiling went from ~35 to tens of thousands; the benchmark now runs N=5000 with ~10x headroom.

**Stop copying the input list in match().** Independently, rast.py's `match()` carried the
parse position as `self.input = [source_string, position]` and snapshotted it with
`self.input[:]` at the top of *every* recursive call and at every backtrack point -- a fresh
two-element list per match, hundreds of thousands per parse. But the source string is
invariant; only the integer position moves. Saving and restoring just that int (by value,
no allocation) is semantically identical and removes all those list copies. Since the
grammar bootstrap and the parse are themselves heavy `match()` users, this sped the whole
benchmark up **+9.4%** with byte-identical output, and validates 3-way (rast_test,
minipy2c_test, the benchmark itself: cpython == ref == native).

**The benchmark now.** ~1.7s native, ~1.7% noise, not memory-bound. Its time is dominated by
the one-time grammar bootstrap + parse (~1.2s of dense PEG matching -- real interpreter work)
plus the N-iteration transpile loop; both are interpreter-bound, so a faster interpreter
moves the whole number. This is the workload to optimize against. (A still-open lever: the
transpiler builds output with `out = out + s`, which is O(n^2); a list+join would cut the
per-transpile allocation and raise the loop ceiling further, but it isn't the bottleneck yet.)

---

# Porting rast.py to native code: `tools/rpy_lib/rpy_ast.py` (the py2c-compiled parser)

`tools/rpy_lib/rast.py` is a pymetaterp-derived PEG parser for a Python subset. Until now it
ran as *minipy bytecode* on the native minipy interpreter — i.e. the PEG matching logic was
itself interpreted. `rpy_ast.py` is `rast.py` compiled **directly to native C via py2c**,
bypassing the bytecode layer entirely. Same algorithm, same `parse_python(source)` entry
point, same pymetaterp `Node` tree — but as a standalone ELF.

## Result

Native `rpy_ast` is **byte-identical to CPython** on 18 diverse snippets (assignments,
precedence, `def`/`class`, `if`/`while`/`for`, lists, dicts, list/dict comprehensions,
chained method calls, `try`/`except`, boolean logic, nested calls, slicing and negative
indexing). The two-stage grammar bootstrap plus a parse matches CPython for **328,024+**
`match()` operations before producing the identical tree.

Speed, parsing the same 10 snippets 30× (300 parses, one grammar bootstrap):

    rast.py as minipy bytecode (on the py2c minipy interp) : 3.62 s
    rpy_ast.py, py2c-compiled to a native ELF               : 0.50 s   -> ~7.3x faster

(The py2c-compiled parser is still ~1.8x slower than the same code under CPython itself —
py2c's fully-boxed dynamic dispatch can't beat CPython's C interpreter on this dynamic-heavy
workload — but it decisively beats the bytecode-on-interpreter baseline, which is the point.)

Build / run:

    rm -rf /tmp/ra && python3 tools/py2c.py tools/rpy_lib/rpy_ast.py --out /tmp/ra
    python3 -c "import sys;sys.path.insert(0,'tools');import py2c;py2c.write_runtime('/tmp/ra')"
    gcc -std=c99 -O2 -DSHIVYC_ARENA_LOG2=31 -I/tmp/ra /tmp/ra/*.c -o /tmp/ra/rpy_ast
    /tmp/ra/rpy_ast                    # native self-test (dumps 10 trees)
    python3 tools/rpy_lib/rpy_ast.py   # same under CPython (must match byte-for-byte)

Note the `-DSHIVYC_ARENA_LOG2=31` (2 GiB arena): the bootstrap generates a lot of PEG
backtracking garbage and there is no GC, only a bump arena.

## The py2c blocker catalog (what the port had to work around)

py2c compiles `rast.py`'s fully-dynamic code (heterogeneous `Node` children boxed as generic
`obj`, runtime type tags) surprisingly well, but seven concrete gaps had to be worked around.
Each is a real py2c limitation worth knowing for future ports.

1. **`type(x) == list` / `type(x) == str` does not compile** (emits an undeclared C `list`/
   `str`). Use `isinstance(x, list)` / `isinstance(x, str)` instead — these lower to a tag
   check and work.

2. **Non-constant `obj` module globals are never initialized in a standalone `main()`
   program.** py2c defers list/dict/tuple/string/float module globals to a `<mod>_init()`
   that is never called (not from C `main`, not via a `__main__` guard). Symptoms: a list
   global reads as `None`, a string global reads as empty. A `None`-initialized global is
   fine (a static `obj` zero-inits to `None`). **Pattern that works:** declare the global as
   `X = None` and build it lazily on first use (exactly how the interpreter cache `_PYI`
   already worked). In `rpy_ast.py`, `binary_ops`, `priority` and `inf` are `None`-globals
   filled by `_ensure_tables()`, and `tree`/`grammar`/`python_grammar`/`extra` became
   functions `_meta_tree()`/`_grammar()`/`_python_grammar()`/`_extra()`.

3. **`float("inf")` does not produce infinity** (`pyfloat("inf")` yields a small/zero value;
   every comparison against it is false, so a `*` quantifier loops zero times). Since `inf`
   is only ever an unbounded-quantifier ceiling and the loops always break on `MatchError`
   first, a large int (`1000000000`) is a correct drop-in.

4. **`dict(["t\t", "n\n", ...])` — building a dict from an iterable of 2-char strings (a real
   CPython feature where each string is a key/value pair) — creates the keys but leaves the
   values null.** This silently corrupted every escaped char (`\t`, `\n`, ...) in the grammar,
   which zero-widthed a quantifier and exhausted the arena. Fix: an explicit dict literal.

5. **`def main()` must be annotated `def main() -> "int":`** or py2c emits no C entry point.
   (Param annotations work too, see #7.)

6. **Virtual method dispatch on a class with no `TypeInfo`.** py2c compiled `pyi.match(...)`
   as a *vtable* call `TYPE(pyi)->match(...)` because `pyi` came back as generic `obj` (the
   cache getter returns `obj`). But `Interpreter` was never used with `isinstance`, so py2c
   emitted **no `Interpreter_type` TypeInfo** and `Interpreter_new` never set `->type` — the
   vtable pointer was uninitialized garbage and the call jumped into hyperspace *before*
   `match` ran (the crash was in the call, not the body — a dead giveaway). Fix: add
   `if isinstance(pyi, Interpreter):` before the call. That single `isinstance` forces py2c to
   emit the TypeInfo (populating the `.match` slot) and to set `->type` in the constructor.

7. **Parameter type inference can silently pick `int` for an `obj` parameter.** py2c typed
   `reformat_binary(start, tokens)`'s `start` as `int` (it flows through `lhs = start` into an
   accumulator that also holds a loop index). Passing a `Node` then coerced the pointer to an
   int, so `return start` handed back a garbage integer exactly where a `NAME`/`NUMBER` token
   node belonged — and because it was heap-layout-sensitive, merely adding a `print` "fixed"
   it. AddressSanitizer was clean (valid pointer, wrong *tag*: `isinstance(x, int)` was true).
   Fix: annotate the parameter — `def reformat_binary(start: "obj", tokens):`.

## Generated from rast.py

`rpy_ast.py` is **generated** from `rast.py` by `tools/rpy_lib/build_rpy_ast.py`, so `rast.py`
stays the single source of truth and the two cannot drift. The generator applies exactly the
transforms above -- `isinstance`-not-`type` (#1), lazy-init tables + grammar-as-functions (#2),
int `inf` (#3), explicit escaped-char dict (#4), the `isinstance(pyi, Interpreter)` narrowing
(#6), the `start: "obj"` annotation (#7), plus a `main() -> "int":` self-test -- each as a
string substitution that asserts its input is present, so if `rast.py` changes in a way that
invalidates a transform, generation fails loudly instead of emitting a broken parser.

    python3 tools/rpy_lib/build_rpy_ast.py            # regenerate rpy_ast.py after editing rast.py
    python3 tools/rpy_lib/build_rpy_ast.py --check    # CI check: fail if rpy_ast.py is stale

The generated body is byte-identical to the reviewed parser; only the module docstring is
replaced with a "GENERATED -- do not edit" banner. Do not edit `rpy_ast.py` by hand; edit
`rast.py` and regenerate.

## Regression test: a four-way agreement check

`tools/rpy_lib/rast_test.py` parses the same spread of snippets under **four** executors and
asserts their canonical tree dumps are byte-identical:

1. **CPython** -- `rast.py` run directly (ground truth).
2. **ref VM** -- the pure-Python minipy reference VM (`rpy.py --ref`).
3. **native (bytecode)** -- `rast.py` as minipy bytecode on the py2c-compiled minipy
   interpreter (`rpy.py`).
4. **compiled (native ELF)** -- `rpy_ast.py` compiled straight to C by py2c and run as a
   standalone binary (the port).

    python3 tools/rpy_lib/rast_test.py
    # PASS: cpython == ref == native == compiled  (10 snippets, 172 output lines)

The fourth executor first runs `build_rpy_ast.py --check`: if `rpy_ast.py` has drifted from
`rast.py` the test **fails** (drift is a real bug). If the C toolchain (py2c + gcc) is
unavailable the fourth executor is **skipped**, not failed, so the test still runs in
environments without a compiler. This makes the compiled parser a first-class, continuously
checked executor: any py2c regression or source drift that changes a parse tree is caught.

## Status update — rast.py parses the whole self-hosting path; float correctness

Two pieces of work, found by measuring rather than by porting: `rast.py` was
already much closer to complete than the plan assumed, and the gaps that
remained were narrow. Fixing them exposed two float bugs that would have made
a byte-identical self-hosted compile impossible.

### rast.py now parses every file in the self-hosting path

Before: `rast.py` already parsed `tools/py2c.py` in full (15,678 lines, ~50s
under CPython), but failed on `tools/minipy/interp.py`. Three grammar gaps were
responsible, all now closed:

| gap | symptom | fix |
|---|---|---|
| `NUMBER` accepted only decimal and hex integers | any float constant failed, which is why `interp.py` did not parse | seven alternatives capturing the token as text, one `_numval()` action |
| `ann_assign` required a bare `NAME` target | `self.op: "int(8)" = v` failed — the pattern `interp.py`'s `Instr` uses | target is now `trailed_atom` (name, attribute, subscript) |
| `lambdef` used `parameters`, which permits `: annotation` | `lambda x: x` failed; only the zero-argument form worked, because the greedy optional ate the colon and the body | a separate annotation-free `lambda_parameters`, mirroring CPython's `varargslist` vs `typedargslist` |

`_numval` uses `float(s)` for floats and hand-rolled digit accumulation for
non-decimal integers. That split is forced: minipy's `float(str)` agrees with
CPython exactly, but `int(s, base)` ignores its base argument — the same reason
the older `_hexval` helper exists. Reconstructing floats from parts would lose
mantissa bits, so they go to `float()`.

Now parsing: `rast.py`, `interp.py`, `compiler.py`, `mpyc.py`, `rasm.py`,
`rasm_obj.py`, `rlink.py`, `py2c.py`. **The `python3` shellout for an AST is no
longer structurally required.**

### Float repr and round-tripping

The north star is `minipy py2c.py x.py` producing byte-identical C to CPython.
Float constants pass through that pipeline, so repr and parse must both be
exact. Neither was.

**`str(float)` was `%g` — six significant digits.** In `py2c.py`'s runtime,
`pystr` formatted `T_FLOAT` with `sprintf(b, "%g", ...)`:

| value | CPython | minipy native (before) |
|---|---|---|
| `3.141592653589793` | `3.141592653589793` | `3.14159` |
| `1.0000000000000002` | `1.0000000000000002` | `1` |
| `123456789012345.0` | `123456789012345.0` | `1.23457e+14` |

`1.0000000000000002` collapsing to `1` is a value change, not a formatting
nit. Replaced with `fmt_double()`, which implements CPython's algorithm:
shortest decimal that round-trips through `strtod`, then fixed or exponential
by the decimal exponent (`-4 <= exp < 16` → fixed, with at least one decimal
place so integral values keep their `.0`). Both halves are needed —
shortest-round-trip alone renders `15000000000.0` as `1.5e+10`.

**`float(str)` accumulated error.** `_str_to_float` in `interp.py` scaled the
exponent with a loop of `p = p * 10.0`; across 308 iterations that turned
`1e308` into `9.999999999999998e+307`. The comment on that loop claimed the
values would "round-trip exactly", which is exactly what they did not do. It
now delegates to `float(s)`, which is `strtod` natively and CPython's converter
under the reference VM.

**`_fmt_float` shortcut removed.** It special-cased integral doubles as
`str(int(d)) + ".0"`. Wrong twice: `int(d)` is a 32-bit C `int` in the compiled
interpreter, so anything past 2^31 overflowed into the `%g` path, and
`int(-0.0)` is `0`, so negative zero came back as `"0.0"`.

`tools/minipy/test_float_repr.py` pins all of this three ways, on values chosen
to sit on the boundaries: where CPython switches notation (1e15/1e16,
1e-4/1e-5), where 17 significant digits are needed, at both ends of the double
range including a denormal, and at negative zero.

### Cache-invalidation bug in `tools/rpy.py`

`_ensure_native()` keyed the cached native interpreter on the md5 of
`interp.py` alone. But the binary also depends on `py2c.py`, which is both the
compiler and the source of the linked C runtime — so the `%g` fix above kept
silently serving the stale binary. The key now folds in `py2c.py` too. Worth
knowing when a runtime change appears to have no effect.

### Known limitation, not on the critical path

`v_pow()` returns `0.0` for a float or negative exponent — an explicit v0 stub,
so `2.0 ** 0.5` is wrong. It does not block self-hosting: nothing in `py2c.py`,
`rasm.py`, `rasm_obj.py`, `rlink.py` or `rast.py` uses `**` with anything but a
non-negative integer exponent. Fixing it wants a real `pow()` (exp/log).

### What is not yet established

Parsing is the front half. `rast.py` parsing `py2c.py` in ~50s says nothing
about how long that takes under minipy-native, and nothing at all about
*executing* `py2c.py` under minipy, which has not been attempted.

## Status update — running py2c.py under minipy: sys.exit, and where it stops

With `rast.py` parsing everything, the next question is execution. Answer so
far: **`py2c.py` compiles to minipy bytecode cleanly (690 functions, ~3s) and
runs partway under the reference VM**, but does not yet complete under either
executor.

### py2c.py compiles fine

`compiler.compile_file("tools/py2c.py")` succeeds in about 3 seconds. Nothing
in py2c.py is outside minipy's front end. The problems are all at run time.

### First blocker: `sys.exit` (fixed)

minipy has no `sys` module object -- `ex_Attribute` special-cases exactly
`sys.argv` and `sys.path` into pre-bound globals. `sys.exit` was not among
them, so the attribute lookup failed. py2c.py's last statement is
`sys.exit(main(sys.argv[1:]) or 0)`, so it died before producing any output.

The diagnostic was the worst part: `minipy: unhandled exception: Exception`,
with no message and no location. `_raise_attr_error` falls back to a bare
`Exception` when the program registers no `AttributeError` class, which loses
the attribute name. `MINIPY_TRACE_ATTR=1` now prints it; that is what turned an
unactionable message into "sys.exit" in one run, and it is worth reaching for
first the next time a bare `Exception` appears.

The fix follows CPython rather than special-casing:

* `SystemExit` added to `_BUILTIN_EXC`, based on `BaseException` -- so a
  blanket `except Exception:` does not swallow an interpreter exit.
* `ex_Call` rewrites `sys.exit(n)` into `raise SystemExit(n)`, reusing the
  existing exception machinery rather than adding an exit opcode.
* `_needed_builtin_excs` registers `SystemExit` when it sees a `sys.exit`
  call. That scan runs over the *source* AST, before codegen synthesises the
  `Name("SystemExit")`, so without this the class is absent when the RAISE
  executes -- the rewrite compiled but could not run.
* Both executors treat an unhandled `SystemExit` as a normal ending carrying a
  status: `VM.exit_status` in compiler.py, `_exit_status()` in interp.py.
  `rpy.py` propagates it, so `sys.exit(n)` means the same thing as under
  CPython.
* `sys.exit()` with no argument builds `SystemExit()` with empty `.args`, not
  `SystemExit(0)`. Both mean status 0, but `len(e.args)` differs and CPython's
  is 0.

`tools/minipy/test_sys_exit.py` pins the observable semantics three ways:
catchability, `.args`, passing through `except Exception`, and `finally`
running on the way out.

### Where it stops now

| executor | result |
|---|---|
| reference VM | runs on past `sys.exit`, writes `shivyc_rt.{h,c}`, still going when the harness timed out |
| native | fails immediately, `unhandled exception: Exception`, no output |

The reference VM getting further than native is the informative part: this is a
*second, different* failure, in the compiled interpreter rather than in the
program or the bytecode. Next step is to instrument `raise_attr_error` in
interp.py the way `_raise_attr_error` is instrumented in compiler.py, and
rebuild -- the native binary has no equivalent trace today.

Speed is the other open question. The reference VM is a pure-Python bytecode
interpreter and is far too slow to transpile a file of any size; native is the
only realistic target, and its throughput on py2c.py has not been measured
because it has not yet completed a run.
