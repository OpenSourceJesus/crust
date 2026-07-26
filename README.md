# Crust
## A Unified C/Rust Compiler Environment for Systems Programming

Main Crust Documentation: [CRUST.md](CRUST.md)

Modern systems development forces engineers to choose between two paradigms:
- The ubiquitous simplicity and legacy ecosystem of C.
- The type safety, spatial memory guarantees, and modern ergonomics of Rust.

Today, systems that use both languages must compile them through isolated pipelines (clang/gcc and rustc) and stitch them together using dynamic (.so) or static (.a) libraries via a foreign function interface (FFI).

This FFI boundary comes at a steep price:
- Forced C ABI lowerings that strip high-level type metadata.
- Opaque calling conventions that inhibit aggressive interprocedural optimizations (IPO) and register allocation across language boundaries.
- Heavy, fragile Link-Time Optimization (LTO) setups that slow compile times dramatically while still missing frontend-level alias and lifetime optimizations.

Crust solves this by uniting C and Rust into a single compiler frontend. By natively accepting ISO C syntax alongside a growing subset of Rust syntax, Crust enables seamless, zero-overhead cohabitation of both paradigms inside a single compilation unit.

# Crust Architectural Philosophy
Function-Level Syntax Isolation
To prevent syntactic ambiguity and parsing complexity, Crust enforces function-level syntax boundaries. A source file contains functions written entirely in standard C syntax alongside functions written in Rust syntax.

---

# ShivyCX — an extended C compiler in pure Python

ShivyCX is a self-hosting-oriented C compiler written in Python3. It descends
from [ShivyC](https://github.com/ShivamSarodia/ShivyC) by Shivam Sarodia,
supports a subset of C11, targets x86-64 System V (Intel-syntax GNU assembler),
and adds a set of **whole-program language extensions and analyses** that are
practical precisely because every pass is a few hundred lines of legible Python.

On top of standard C, ShivyCX adds:

- **`_Nbit` globals** — pack small global flags (`name_Nbit`, 1≤N≤8) into an
  otherwise-idle SIMD register (`xmm15`) via `-fsimd-pack-globals`, collapsing
  *N* flag memory loads in a hot routine to one. A loop pass register-promotes
  packed globals across a loop — decompress into GP registers once before,
  operate on registers inside, recompress once after — turning a read-modify-write
  loop from a regression into a win (~1.4× over memory globals, ~1.8× over
  `gcc -O0`).
- **Contracts** — compile-time precondition clauses that bound argument lengths
  and can discharge a vectorizer's remainder loop.
- **Register-partitioned threads** — a whole-program *left/right* register split
  with specialized context switchers.
- **Argument packing** — an opt-in (`-f-pack-args`) non-standard calling
  convention that bit-packs small integer parameters into as few registers as
  possible (eight `char`s in one register instead of six registers plus two
  stack slots).
- **Callee-saved register allocation** — the allocator uses `rbx`/`r12`–`r15`
  for values live across a call, so they need not spill to memory (always on;
  ~1.5× on deeply nested call chains).
- **Memory safety** — whole-program use-after-free / double-free detection and
  automatic `free` insertion for unannotated C.
- **Bare-metal operation** — freestanding, bootable 64-bit images with an inlined
  mini-OS.
- **Multiple back ends** — besides x86-64, an AArch64 (ARM64) cross target with a
  liveness-based linear-scan register allocator, an integer-core RISC-V 64 target,
  and the first steps of a Motorola 68000 / **Neo-Geo** target — all reusing the
  same allocator, all validated differentially against the GNU cross toolchains
  under qemu, and selected with `--target` (see [ARM64.md](ARM64.md) and
  [NEOGEO.md](NEOGEO.md)).
- **A Python→C transpiler** — toward compiling the front end with itself, which
  doubles as **rpython**: a fast, runtime-free Python subset (typed numpy-style
  arrays, auto-contract SIMD, libm, file/socket I/O) that `shivyc.main` compiles
  straight from `.py` to a native binary.


---

## Quickstart

ShivyCX needs only Python 3.6+; assembling and linking use GNU binutils.

```sh
# compile and run a C file (note: the entry point must be `int main()`)
python3 -m shivyc.main hello.c -o hello && ./hello

```

---

## Memory safety for unannotated C

C's manual `malloc`/`free` is the classic source of **use-after-free** and
**double-free** bugs. Because ShivyCX sees the whole call graph, a Python pass
([`shivyc/memory_safety.py`](shivyc/memory_safety.py)) tracks every allocation,
pointer copy (alias), and free, and:

- **flags use-after-free** — dereferencing (or passing to a callee that
  dereferences) a pointer whose allocation was freed, *through aliases and across
  functions*;
- **flags double-free**;
- **auto-frees** — when escape/region analysis proves an allocation is local with
  no live reference, the compiler inserts the `free` for you.

```sh
# report only (no code generated)
python3 -m shivyc.main tests/dangling_alias.c --check-memory
#   [use-after-free] in main: dereferences a pointer after its allocation was freed

# insert automatic frees during a normal compile
python3 -m shivyc.main tests/autofree_leak.c --auto-free -o leak
```

---

## Register-partitioned threads (left/right)

A function header can declare which side of a two-way register split a thread
runs on. ShivyCX computes each thread's transitive register footprint from the
call graph, splits the register file into disjoint `left` / `right` budgets,
re-runs allocation constrained to each budget, and emits a specialized context
switcher that saves only the bank in use.

```c
int main()
assert foo in threads.left( core=0 )
assert bar in threads.right( core=0 )
{ foo(); bar(); }
```

```sh
python3 -m shivyc.main threads_demo.c \
    --emit-thread-switcher switcher.s
#   left  GP footprint : rax, rcx, rdx, rsi
#   right GP footprint : r8, r9, r10, r11   -> footprints are disjoint
# writes switcher.s (cooperative) and switcher.preempt.s (timer-driven)
```

Details in [`shivyc/README.md`](shivyc/README.md)
---

## Calling convention: argument packing and callee-saved registers

Two optimizations attack the cost of moving values across a call.

**Argument packing (`-f-pack-args`, opt-in).** Under the System V ABI a function
of nine `char`s burns six registers and spills three to the stack. With
`-f-pack-args` ([`shivyc/pack_args.py`](shivyc/pack_args.py)), small integer
parameters are bit-packed by offset into as few registers as possible — the
caller builds each packed register with shifts/`or`s and the callee unpacks it —
so eight `char`s arrive in **one** register and nine in two, with no stack
traffic. Caller and callee recompute the identical layout from the signature, so
the convention is self-describing. It is applied only to statically known
(direct) calls, and any function whose address is taken (including via a global
function-pointer initializer) is **never** packed, so the standard ABI is always
honored at indirect call sites.

```sh
# pack small integer args into shared registers (composes with -fstackless-calls)
python3 -m shivyc.main -f-pack-args prog.c -o prog
```

**Callee-saved register allocation (always on).** The register allocator uses
`rbx` and `r12`–`r15` for values that are *live across a call*, which the call
clobbers in every caller-saved register and would otherwise force to memory.
Each callee-saved register a function uses is saved in its prologue and restored
on every exit path; frameless and `-O4` near-scratch leaves stay on caller-saved
registers so they remain frameless. On deeply nested call chains this is roughly
a **1.5×** speedup and beats `gcc -O0`.

---

## Bare-metal / freestanding operation

A mini-OS (*MiniKraft*) is inlined into [`minikraft.py`](minikraft.py) — every
source file embedded as a raw triple-quoted string, plus a registry of
hand-written 64-bit boot files — so the freestanding runtime travels with the
compiler. The bare-metal driver
([`shivycx_baremetal.py`](shivycx_baremetal.py)) compiles your app, resolves the
OS pieces it needs by transitive **symbol closure**, and links freestanding (no
libc, no CRT).

```sh
# freestanding app linked against the mini-OS console
python3 shivycx_baremetal.py tests/hello.c -o hello.elf

# bootable 64-bit Multiboot image (boot stub + long mode + GDT + IDT)
python3 shivycx_baremetal.py tests/kernel_irq.c -o irq.elf --image
```

The `--image` path emits a Multiboot stub that identity-maps the first GiB,
enables PAE/long mode, installs a flat GDT, and jumps to a 64-bit kernel with a
long-mode IDT (timer + keyboard). The preemptive thread switcher above installs
itself into the timer vector. Boot is validated statically here (header/checksum,
ELF64 entry, symbol resolution)

---

## Multiple back ends: ARM64, RISC-V, and Neo-Geo (m68k) cross targets

The IL produced by `il_gen` is architecture-neutral; everything below it — the
register file, the calling convention, instruction selection, and assembler
syntax — lives behind a `Target` seam ([`shivyc/targets`](shivyc/targets/__init__.py))
and is selected with `--target`. On top of the original x86-64 back end there are
now two cross targets, developed at the Python (rpython) level and validated
*differentially* against the GNU cross toolchains under qemu — each ShivyCX
binary's exit code is compared to the same program compiled by `gcc`.

```sh
# AArch64: full scalar C + floats, arrays, structs, globals, pointers
python3 -m shivyc.main prog.c -S -o prog.s --target arm64
aarch64-linux-gnu-gcc -static prog.s -o prog && qemu-aarch64 ./prog

# RISC-V 64: integer core (locals, arithmetic, control flow, calls, recursion)
python3 -m shivyc.main prog.c -S -o prog.s --target riscv64
riscv64-linux-gnu-gcc -static prog.s -o prog && qemu-riscv64 ./prog

# Motorola 68000 / Neo-Geo: integer core (CISC, big-endian, stack-arg calls)
python3 -m shivyc.main prog.c -S -o prog.s --target m68k
m68k-linux-gnu-gcc -static prog.s -o prog && qemu-m68k ./prog
```

**AArch64** is the more complete target: integer and floating-point arithmetic,
the six comparisons with compare/branch fusion, `if`/`while`, pointers and
address-of, single- and multi-dimensional arrays, structs (by-value copy),
file-scope globals (with address caching), direct calls and recursion, and
AAPCS64 argument/return lowering including the integer/FP register split. Its
register allocator is a **liveness-based linear scan with a caller/callee split**:
values live across a call get callee-saved homes, call-clean values get
caller-saved homes that need no save/restore, and leaf functions come out
frameless — beating `gcc -O0` instruction counts on leaf and call-light code.
See **[ARM64.md](ARM64.md)** for the full design, the staged bring-up, and the
differential-testing methodology.

**RISC-V 64** was brought up next, and deliberately small, to validate the seam:
it implements the integer core and **reuses the entire architecture-neutral
middle end verbatim** — copy-coalescing with its safety check, the live-variable
analysis, live-interval and call-cross detection, and the caller/callee
linear-scan allocator are all shared methods (`_il_*` in
[`asm_gen.py`](shivyc/asm_gen.py)); only instruction selection, the register
file, and the lp64 ABI are new. Standing up a working second ISA — locals,
`+ - * / %`, comparisons, `if`/`while`, direct calls, recursion, register
pressure with spills, and cross-call liveness — therefore took only a lowering
pass, which is exactly the payoff of doing compiler work at the rpython level.

**Motorola 68000 / Neo-Geo** is the first step toward the console's main CPU
(the [ngdevkit](https://github.com/dciabrin/ngdevkit) toolchain cross-compiles C
to m68k). It is the sharpest test of the seam so far, because the 68000 shares
nothing structural with the RISC back ends: it is CISC and big-endian, has two
register files (data `d0`-`d7` / address `a0`-`a7`), two-address ALU instructions
(`dst OP= src`), `.b`/`.w`/`.l` operation sizes, and a fully **stack-based**
calling convention with no register arguments. Even so, the integer-core back end
**reuses the same `_il_*` allocator unchanged** — liveness, intervals, and
linear scan don't care about instruction shape — and only the m68k lowering,
register file, and `link`/`unlk` frame/ABI are new. It already compiles locals,
`+ - * / %`, the six comparisons, `if`/`while`, stack-argument calls and
recursion. See **[NEOGEO.md](NEOGEO.md)** for the architecture notes and the
roadmap to bare-metal Neo-Geo ROMs.

On top of the back end, a small rpython library
([`tools/rpy_lib/neogeo.py`](tools/rpy_lib/neogeo.py)) turns multi-line ASCII art
into Neo-Geo pixel art — a colour palette in the console's native 16-bit format
plus an index buffer — with a four-line API (`neogeo.background.asciiart(...)`,
`neogeo.sprite.asciiart(...)`, `neogeo.scene.add_*`). Because the art is a string
constant, `import neogeo` *specialises* the translator: py2c runs the conversion at
translate time and bakes the finished palette/tile data into the generated C, so
the on-target code is just data plus a copy loop. The demo
[`examples/rpython2c/neogeo/loadscreen.py`](examples/rpython2c/neogeo/loadscreen.py)
draws a framed loading screen with a rocket sprite.

Differential test harnesses live alongside the compiler:
[`tools/arm64_difftest.py`](tools/arm64_difftest.py) (130 cases),
[`tools/riscv64_difftest.py`](tools/riscv64_difftest.py), and
[`tools/m68k_difftest.py`](tools/m68k_difftest.py); each compiles a corpus both
ways and checks the exit codes match under qemu.

---



#### Preprocessor
A token-stream macro engine with conditional compilation, function-like macros,
`#`/`##`, and hide sets ([`preproc.py`](shivyc/preproc.py)), preceded by an
extension pre-pass ([`extensions.py`](shivyc/extensions.py)) that recognizes the
non-standard constructs and blanks them while preserving line/column numbers.

#### Lexer
Implemented in [`lexer.py`](shivyc/lexer.py), with token classes in
[`tokens.py`](shivyc/tokens.py) and recognized keywords/symbols in
[`token_kinds.py`](shivyc/token_kinds.py).

#### Parser
Recursive descent in [`parser/*.py`](shivyc/parser/), producing a parse tree of
nodes from [`tree/*.py`](shivyc/tree/).

#### IL generation
The parse tree is traversed to a flat three-address IL; commands live in
[`il_cmds/*.py`](shivyc/il_cmds/) and the generators in each tree node.

#### ASM generation
IL is lowered to Intel-syntax x86-64; register allocation uses George and
Appel's iterated register coalescing over a pool that includes the callee-saved
registers (`rbx`, `r12`–`r15`), which are saved/restored per function so that
values live across a call can stay in registers. General code in
[`asm_gen.py`](shivyc/asm_gen.py); the argument-packing convention is a
whole-program pass in [`pack_args.py`](shivyc/pack_args.py), and loop
register-promotion of `_Nbit` packed globals is an IL pass in
[`simd_pack_promote.py`](shivyc/simd_pack_promote.py).

The same IL feeds three cross targets selected with `--target`: an AArch64 back
end (see [ARM64.md](ARM64.md)), an integer-core RISC-V 64 back end, and the first
steps of a Motorola 68000 / Neo-Geo back end (see [NEOGEO.md](NEOGEO.md)) — all
sharing a target-neutral liveness-based linear-scan register allocator (the
`_il_*` methods in [`asm_gen.py`](shivyc/asm_gen.py)).

#### Whole-program call graph
The driver can build and print the program-wide call graph
([`callgraph.py`](shivyc/callgraph.py), `--print-call-graph`); it is the
substrate for the thread partitioner, the memory-safety analysis, and member
elimination.

---

## Compiling the front end with itself (Python→C transpiler)

[`tools/py2c.py`](tools/py2c.py) is a **specialized** Python→C translator that
emits C from ShivyCX's own Python source — a step toward a self-hosting front
end that is smaller and faster than the interpreted path. It is not a general
Python→C compiler; it understands only the subset of Python the front end is
written in, and uses that narrowness to produce small, idiomatic C.

It represents values in three tiers — concrete C scalars, concrete class
`struct`s with a shared `Obj` header for dispatch/`isinstance`, and a tagged
`obj` union as the dynamic fallback — and keeps each value in the most concrete
tier it can prove. Type inference, `isinstance` narrowing (including
`isinstance(x, T) and x.field` chains), cross-module method dispatch through
replicated vtables, first-class functions, and nested-function lifting are all
supported. A few small, ordinary type annotations in the front end (for example
typing the IL-command operand fields as `ILValue` via a `TYPE_CHECKING` import)
let the translator lower attribute chains like `self.output.ctype.size` to plain
struct accesses.

Correctness is enforced by **byte-identical behavior harnesses**: for each
feature, the transpiled C and the original Python run on the same inputs and
their outputs must match exactly. The transpiler never emits a silently-wrong
stub to inflate its compile count.

All targeted IL-command modules (`base`, `asm`, `math`, `compare`, `value`,
`control`) currently translate to cleanly-compiling C. See
[`TRANSPILER.md`](TRANSPILER.md) for the full design — object model, type
inference, the annotation convention, downcasting, cross-module machinery, and
the verification methodology.

```sh
# transpile the whole front end into a directory (runtime is emitted alongside)
python3 tools/py2c.py --out /tmp/out
# or a single module
python3 tools/py2c.py --out /tmp/out shivyc/il_cmds/value.py
```

To see exactly how the translator typed every field, parameter, and local —
which values are plain C scalars (POD) and which are boxed into the object model
— pass `--show-object-model`. The report makes the type-inference decisions
visible, which is the fastest way to catch a value that was meant to be an
object but got inferred as a scalar (a common source of runtime crashes):

```sh
python3 tools/py2c.py shivyc/tokens.py --show-object-model
```

---

## Bootstrapping (`make bootstrap`)

The transpiler feeds a classic two-stage bootstrap. Each stage is a `make`
target; everything is built under `/tmp` (override with `BOOTSTRAP_DIR=`).

```sh
make bootstrap     # stage 1: build the native compiler, smoke-test, benchmark
make bootstrap2    # stage 2: compile the compiler with itself, run the suite
sudo make install  # install the bootstrapped compiler to /usr/local/bin/shivycx
```

- **`make bootstrap`** transpiles ShivyCX's own source with `py2c.py`, compiles
  it with gcc into `shivyc_native`, runs a smoke test (arithmetic, control flow,
  recursion, pointers, arrays, strings) through that native binary, and
  benchmarks its compile speed against gcc on the same input. **This works
  today**: 60 modules link into a single binary that compiles and runs real C.
- **`make bootstrap2`** feeds the compiler's own generated C back through the
  stage-1 binary to produce the final self-compiled `shivycx`, then runs the
  full `tests/` suite against it. This is the **bootstrap milestone in
  progress**: until the native compiler accepts every C construct it emits for
  its own source (currently gated on function-pointer declarations), the target
  reports how many of the generated modules already self-compile and the first
  blocker.
- **`make install`** copies the furthest-along bootstrapped compiler
  (`shivycx` if stage 2 produced one, otherwise the stage-1 `shivyc_native`) to
  `$(PREFIX)/bin/shivycx` (default prefix `/usr/local`).

See [`tools/SELFHOST_STATUS.md`](tools/SELFHOST_STATUS.md) for the detailed
stage-by-stage status and the next highest-leverage targets.

> **Note:** `make install` now installs the bootstrapped compiler. To install
> the build/test toolchain (gcc, binutils, qemu, pypy3) as before, use
> `make install_deps`.

---

## rpython — a fast, safe Python subset that compiles to native C

The same transpiler doubles as **rpython**: a restricted-Python dialect that
compiles, with *no runtime and no boxing*, straight to native C and on to a
ShivyCX binary. `shivyc.main` accepts `.py` sources directly — it transpiles
through `tools/py2c.py`, supplies the few libc prototypes the kernel needs, and
compiles and links:

```sh
python3 -m shivyc.main examples/rpython2c/numpy/simd_kernels.py -o simd && ./simd
```

What makes it fast and small:

- **Name- and flow-based type inference.** Unannotated integer drivers (`i`,
  `n`, `count`, `iters`, …) become `int`; locals assigned float literals or
  division become `double` (via a fixpoint). No annotations needed for ordinary
  numeric loops, which lower to plain C with zero boxing.
- **numpy-style typed arrays.** `"f32*"`, `"f64*"`, `"i32*"` (and fixed-size
  `"f32[256]"`) are real C arrays with native indexing — not boxed lists.
- **Typed containers.** `"list[int]"` / `"list[float]"` lower to a growable
  `{T* data; long len, cap;}` array (malloc/realloc), and `"dict[str,int]"` /
  `"dict[int,int]"` to parallel key/value arrays with linear-probe lookup — both
  unboxed and runtime-free, supporting literals, indexing, `append`, `len`,
  `in`, and iteration. A negative integer literal index (`xs[-1]`) wraps to
  `data[len-1]` statically; dynamic indices are taken as-is. Lists of objects
  keep the tagged model.
- **Native byte scanning + 64-bit ints.** `ord(s[i])` on a `char*` compiles to a
  direct byte read (no per-character allocation), and an `"i64"` annotation gives
  true 64-bit arithmetic. Together these let character- and table-driven compiler
  passes (lexers, hashers) be written in rpython and run at native speed — see
  [`examples/rpython2c/compiler/`](examples/rpython2c/compiler/).
- **Auto-contracts → SIMD.** A leading `assert len(x) % 4 == 0` (or an inferred
  divisibility fact from a fixed-size array) is lowered to a ShivyCX contract;
  the compiler proves it at the call site and emits a packed-SSE body with no
  scalar remainder. On an element-wise recurrence this **matches `gcc -O2` and
  beats `gcc -O0` ~20×** (see [`benchmarks/`](benchmarks/)).
- **libm ufuncs.** `exp`, `log`, `sin`, `sqrt`, `tanh`, … (bare, or `math.`/
  `np.`-qualified) type as `double` and lower to native libm calls.
- **Classes become structs.** A plain data class (no inheritance or dynamic
  dispatch) is lowered POD-style to a bare C `struct` with `malloc` and direct
  method calls — no object header, vtable, or runtime. Instances pass by
  pointer, so a function or method can take them directly
  (`def pull(p: "Body*", q: "Body*")` → `void pull(Body* p, Body* q)`). Richer
  classes (inheritance, `isinstance`, virtual dispatch) use the tagged-object
  model, which ShivyCX now compiles end to end — including its own object-model
  runtime — so polymorphism works in self-compiled code, not just under gcc.
- **Multi-file programs.** Pass several `.py` files at once
  (`shivyc.main app.py lib.py -o app`) and they compile as one translation unit:
  `import lib` resolves against the input directory, so functions call directly,
  classes construct and dispatch across the boundary, and fields read/write
  directly (boxing into `obj` fields as needed). A POD class stays POD when used
  from another module — its decision is propagated so layout and dispatch agree.
  Two modules may even define classes with the *same* bare name; the translator
  module-qualifies the colliding symbols and emits a distinct struct for each
  (plus a separate `TypeInfo` for object-model classes, so `isinstance` still
  distinguishes them). A field assigned only `None` in its
  module is typed `obj` (nullable), so another module can store an object in it.
- **System glue.** File I/O (`open`/`read`/`write`/`close`), `input()`,
  `os.system`, `os.fork`, BSD **sockets** (`socket`/`bind`/`connect`/`accept`/
  `send`/`recv`), and `sys.argv` all lower to plain C — enough to write real
  programs (a TCP echo server, a Mandelbrot renderer) with no runtime.
- **Build reports.** `--pdf` renders any build (C or rpython) as a PDF: overview,
  TikZ call graph, safety findings in red, the Python source beside the
  generated C with its auto-inferred contracts, and the program output.

Worked examples live in [`examples/rpython2c/`](examples/rpython2c/): 
- `numpy/` (SIMD kernels, BLAS, ufuncs, matmul),
- `nn/` (a feed-forward neural net showing classes→structs), `
- nbody/` (a gravity sim that passes class instances by pointer),
- `classes/` (inheritance + polymorphism via the object model, plus a POD-vs-object-model comparison),
- `lists/` and `dicts/` (typed `list[T]` / `dict[K,V]` lowered to unboxed C arrays — no boxing, no GC),
- `compiler/` (a C-subset lexer kernel — a ShivyCX hotspot rewritten in rpython, ~18x faster through ShivyCX and ~50x through gcc, with a benchmark harness),
- `memory/` (`del`, compiler-inserted `free` via whole-program escape analysis, and the `--pdf` memory report),
- `multifile/`, `ambig/`, and
- `fieldwrite/` (multi-file programs: cross-module calls, same-named classes module-qualified into distinct structs, and cross-module writes into None-initialised `obj`fields),
- `dynattr/` (compiled `getattr`/`setattr` on a struct by runtime key — an inline first-character type switch, no dict and no bridge),
- `rtattr/` (`getattr`/`setattr` by runtime key on a *polymorphic* object held as a tagged `obj`, dispatched through a per-type field table by the
- `rt_getattr`/`rt_setattr` runtime helpers — again no micropython bridge),
- `crossattr/` (cross-class field discovery: attributes a configurator stamps onto another class via `obj.attr =`/`setattr` are promoted to real slots so the writes persist),
- `aggregates/` (varargs-free list/dict/call construction — a 16-byte `obj` mis-lowers through C `...` on the self-compiled backend, so literals and calls build via a stack array instead),
- `formatting/` (`%` string formatting and f-strings — `fmt % args` is real printf-style formatting, not arithmetic modulo, lowered to a varargs-free `str_mod`),
- `ctorval/` (constructors used as values: trampoline unboxing of int/float/bool arguments, plus the switch-on-narrow-type integer-promotion fix that makes boolean truthiness correct), `sets/` (set as a first-class type with its own runtime tag: union/intersection/difference/symmetric-difference, order-independent equality, de-duplicating literals and comprehensions),
- `dictops/` and `wordfreq/` (the general dict type -- get/setdefault/pop/update/copy/merge/comprehensions, plus realistic frequency-counting and grouping), `untyped/` (untyped dicts/lists/sets with type inference and rpython-rule advisories),
- `promote/` (opt-in auto-promotion of inferred containers to the unboxed typed form),
- `pgo/` (profile-guided auto-typing via `-fprofile-generate`),
- `pgo_multi/` (multi-file profile-guided auto-typing),
- `numpy/fusion.py` (NumPy operator fusion: `out[:]=expr` -> one loop, no temporaries),
- `io/` rpython io files,
- `net/` rpython socket io, and
- `mandelbrot/`.

Run them all with `make rpython`.

`make testfast` is a fast smoke test: a single-file syntax sweep and a multi-file cross-module case, each compiled with CPython (the oracle), the ShivyCX self-compiler, and the py2c->gcc transpiler, requiring all three to agree. It covers most of the language subset in a few seconds.

---
## MiniPy
[`MINIPY.md`](MINIPY.md)

A tiny Python interpreter written in Rpython.  Current status, working on performance, and supporting all the features required to run py2c.py,
thus making the entire project self contained.

---
## Benchmarks
- (June 29, 2026) https://doi.org/10.5281/zenodo.21048364


## References

- [ShivC](https://github.com/ShivamSarodia/ShivC) — the original compiler ShivyCX was rewritten from.
- C11 Specification — http://www.open-std.org/jtc1/sc22/wg14/www/docs/n1570.pdf
- x86-64 ABI — https://github.com/hjl-tools/x86-psABI/wiki/x86-64-psABI-1.0.pdf
- Iterated Register Coalescing (George and Appel) — https://www.cs.purdue.edu/homes/hosking/502/george.pdf
- (B. Hartshorn, viXra 2025, 2026).
    - *Foundational Problems with Compilers and Operating Systems* https://ai.vixra.org/abs/2507.0081
    - *Rethinking Meta-Interpreters for High-Performance Execution* https://ai.vixra.org/abs/2606.0084
    - *Closing the Compiler Loop:
Toward a Self-Hosting, Dependency-Free Python JIT* https://zenodo.org/records/21178004
    - *Beyond WebAssembly: Rethinking the Web Browser with
Post-JavaScript, VM-Free Page Execution* https://ai.vixra.org/pdf/2607.0013v1.pdf

## External Demos
- https://github.com/OpenSourceJesus/ShivyCX-Game-Demos
