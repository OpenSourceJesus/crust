# WebAssembly Back End

`--target wasm` compiles C to a `.wasm` binary module.

```sh
python3 -m shivyc.main --target wasm prog.c -o prog.wasm
node -e 'const b=require("fs").readFileSync("prog.wasm");
         WebAssembly.instantiate(new WebAssembly.Module(b),{})
           .then(i=>console.log(i.exports.main()))'
```

Aliases: `wasm`, `wasm32`, `webassembly`.

## No external tools

Every other back end hands assembler text to `as` and object files to `ld`.
This one hands back a finished module. `shivyc/wasm.py` writes the binary
format directly -- LEB128, section framing, opcode encoding -- so there is no
wabt, no `wat2wasm`, no LLVM and no npm package anywhere in the path. The
compiler's only output is the module.

That is what `Target.is_binary` marks. `main.py` branches on the flag rather
than on the target name, so a second binary target costs nothing there.

## Why none of the middle end is shared

The arm64, riscv64 and m68k back ends all reuse the same liveness analysis,
copy coalescing and linear-scan register allocator (`_il_liveness`,
`_il_intervals`, `_il_linear_scan` in `asm_gen.py`). The wasm back end calls
none of them, for two structural reasons.

**There are no registers.** A wasm function declares as many typed locals as it
likes, and the engine's own JIT does the real allocation against real hardware
registers. Allocating them here would be work done twice, the second time with
less information. Every IL value simply gets its own local; nothing is ever
spilled, because there is nowhere to spill from.

**There is no `goto`.** wasm control flow is structured: a branch may only exit
an enclosing `block` or re-enter an enclosing `loop`. The IL's
`Label`/`Jump`/`JumpZero` triple describes an arbitrary CFG, and that cannot be
emitted edge for edge.

## How control flow is encoded

The function body is split into basic blocks. A `state` local holds the index
of the block to run next, and a `br_table` at the top of a `loop` dispatches to
it:

```
loop $L
  block $b_{n-1}
    ...
      block $b_0
        local.get $state
        br_table 0 1 .. n-1 (default n-1)
      end            ;; branching to depth 0 lands here
      <block 0>      ;; ends: set $state, br $L   -- or return
    end              ;; depth 1 lands here
    <block 1>
  ...
end
unreachable          ;; the loop is only left by a return
```

Branching to depth `i` exits blocks `b_0..b_i` and resumes just past `b_i`'s
`end`, which is exactly where block `i`'s code sits. Every block ends in a
branch back to `$L` or a `return`, so control never falls from one block's code
into the next block's `end`.

Conditional edges push both candidate block indices and pick between them with
`select`, rather than using `if`/`else`. That keeps the `br` out of any nested
construct, so the depth back to `$L` stays a simple function of the block index
and does not have to be adjusted per branch.

This is O(1) to generate and correct for any CFG, at the cost of a dispatch per
edge. Recovering the original loops and `if`s -- a relooper, or LLVM's
Stackifier -- is the obvious follow-up, and it is confined to
`_wasm_emit_body`: nothing else in the back end knows how control flow is
spelled.

## Memory model

wasm locals have no address, so `&x` cannot be formed for one. Anything whose
address is taken -- and every aggregate, which is too big for a value type at
all -- lives in linear memory instead. Everything else stays a local.

```
0            .. 1024        null guard (a null deref lands here, not in an object)
1024         .. data_end    static data: globals, string literals
data_end     .. stack_top   shadow stack, 256 KiB, growing DOWN
```

Global 0 is the shadow stack pointer. A function that needs a frame lowers it
on entry and keeps the frame base in a local; every `return` restores it.
Functions that need no frame -- no address taken, no aggregate -- emit no
prologue and no epilogue and pay nothing.

Static addresses are all assigned before any function body is emitted, so a
global is referred to by a plain `i32.const` with no relocation step.
Uninitialized statics need no data segment at all, since linear memory starts
zeroed.

**Pointers are carried as `i64`.** wasm32 addresses are 32-bit, but this
compiler's `sizeof(void *)` is 8 everywhere -- struct layout, arrays of
pointers, and pointer arithmetic all assume it. Rather than fork the type
system for one target, a pointer is an i64 holding a 32-bit address, wrapped to
i32 at each access. The high half is always zero.

Stack overflow is not detected: a deep enough recursion walks the stack pointer
down through the static data and corrupts it, exactly as it would on a native
target without a guard page. A guard region below the stack that faults on
touch would be a cheap improvement.

## Floating point

`float` and `double` map to wasm's native `f32` and `f64`; `long double` is an
alias for `double` in this compiler, so there is no third width to synthesise.
Arithmetic, comparisons and both conversion directions lower to single
instructions.

Three details are worth stating because each had a wrong-looking easy answer:

**Negation uses `f*.neg`, not `0 - x`.** The subtraction gives `+0.0` for an
input of `-0.0`, where C requires `-0.0`. `neg` flips the sign bit and is
correct for every input including NaN.

**Comparisons need no fixups.** wasm's `lt`/`le`/`gt`/`ge` are the *ordered*
comparisons -- NaN yields 0 -- and `ne` is the negation of `eq`, so NaN yields
1. That is exactly what C specifies, so the six operators map straight across.

**Float-to-int uses the saturating conversion.** The plain `trunc` family traps
on NaN or an out-of-range value. C leaves that case undefined, and a trap is
the worst available reading of "undefined": it destroys the whole module over
an expression the program may not even use. `trunc_sat` clamps instead.

That last one is an observable difference from the other back ends, and it is
deliberate. `(int)8e9` yields `INT_MAX` here and `INT_MIN` on x86-64 (what
`cvttsd2si` returns). Both are legitimate; the difftest carries it as
`wasm_f_overflow_conv`, marked XFAIL, so the divergence stays visible and
anyone who changes it has to do so on purpose.

## WASI and imports

A function that is declared but not defined becomes an **import**. Where it is
imported from depends on the name: anything matching a WASI preview-1 call
(spelled `__wasi_fd_write` and friends, so it cannot collide with a user
function called `fd_write`) is imported from `wasi_snapshot_preview1`;
everything else comes from `env`, which is the convention a plain JS host
expects.

Imports occupy the low end of the function index space, ahead of every defined
function. That forces a scan for undefined calls *before* the first function is
declared -- discovering an import while emitting a body would shift every
defined function's index out from under calls already written.

**Imports use the wasm32 ABI, not the internal one.** Inside the module a
pointer is an i64 (see above); at an import boundary it narrows to i32, because
a WASI signature is 32-bit throughout. Getting this wrong does not merely
underperform -- a real WASI host rejects the module outright, and a JavaScript
one silently hands the program BigInts.

When `main` is defined, a `_start` entry point is synthesised: it calls main,
masks the result to 8 bits (a process status is 8 bits, so a main returning -1
must exit 255, not 4294967295) and passes it to `proc_exit`. The module's
memory is exported too, since a host reading an iovec needs to reach into it.

### A C library, through the preprocessor

`shivyc/include/wasi.h` implements `write`, `putchar`, `puts` and a `printf`
over `fd_write`:

```c
#include <wasi.h>
int main(void){
    puts("hello from crust wasm");
    printf1("the answer is %d\n", 42);
    return 0;
}
```

```
$ python3 -m shivyc.main --target wasm hello.c -o hello.wasm
$ node tools/wasm_run.js hello.wasm
hello from crust wasm
the answer is 42
```

It is a header rather than a library because the wasm target compiles one
translation unit at a time and there is no linker in the path -- everything a
module needs has to arrive through the preprocessor, so every definition is
`static`.

`printf` is a real variadic function taking `...`, handling `%d %i %u %x %X %c
%s %ld %lu %lx %%` with field widths and the `0` and `-` flags. (`printf1` /
`printf2` / `printf3` survive as thin aliases so code written against the
earlier, pre-variadic version still compiles.)

## Variadic functions

A variadic function takes **no wasm parameters at all**. Every argument, named
and unnamed alike, arrives through a block of 8-byte slots in the caller's
frame, and the block's address is handed over in a wasm global.

That is not a shortcut. A wasm signature is fixed-arity, so the trailing
arguments have nowhere else to go; and giving the named ones a separate path
would mean two mechanisms where the IL already expects one, since `LoadArg`
carries a `base_index` into the block for exactly this purpose.

The global standing in for a register is the same shape the other back ends
use -- riscv64 passes the block address in `t0`, arm64 in a scratch register --
and it is safe for the same reason: the callee's `VaSaveBase` copies it into a
local before the callee can make any call of its own.

Nested variadic calls (`vsum(2, vsum(2,5,5), vsum(1,3))`) work because the IL
evaluates arguments into temporaries before the outer call stages its block, so
two blocks are never being filled at the same time. That is a property of the
IL rather than of this back end, so `wasm_va_nested` tests it explicitly.

Each slot is 8 bytes whatever the argument's own width, because that is what
`va_arg`'s pointer arithmetic assumes: narrow integers are widened and floats
promoted to double, which is what C's default argument promotions require of an
unprototyped argument anyway. Slots are little-endian, so a narrower `va_arg`
load from a slot's start reads the right bytes.

## Function pointers

wasm has no code addresses. A function pointer is therefore an **index into
the module's function table**, and a call through one is `call_indirect`.

Table slot 0 is left permanently empty, which is what makes a call through a
null pointer trap rather than dispatch to whatever landed at index 0. The
table is emitted whenever a `call_indirect` exists, not merely when some
function's address is taken -- `int (*f)(int) = 0; f(1);` contains the
instruction but puts nothing in the table, and the instruction still names
table 0.

`call_indirect` also checks the signature at run time, so calling through a
wrongly-typed pointer traps here where the register back ends would simply run.
That is stricter than native and worth knowing about; the signature is taken
from the pointer's *pointee type* rather than from the argument values, so an
implicit conversion at the call site cannot quietly produce a signature the
callee does not have.

wasm2c makes the same check. A reference carries the identity of its
signature alongside its pointer -- `struct { void *ptr; u32 type; }` -- rather
than the table slot carrying it, because a reference *moves*: `ref.func` puts
one in a local, `table.set` stores it, `table.copy` moves it again, and a
parallel array of types would have to be kept in step through all of that.
Carrying it in the value means an indirect call can check wherever the
reference came from.

Signature identities are canonicalised on the *shape*, not on the module's
type indices, which may list one shape twice -- two indices for one signature
would make a legitimate call trap.

Without the check an indirect call is a cast and a jump, so a table entry of
the wrong shape would be called with arguments read from wherever the ABI
left them. `wasm_ref_difftest.py` carries `funcref_type_mismatch_traps` for
exactly that case.

## Aggregate copies

Struct assignment lowers to `memory.copy` (bulk memory), one instruction
whatever the size, and defined to handle overlapping source and destination --
which matters because assigning from an overlapping object is legal C. The
same path covers an aggregate moving through an array element, a struct
member, or a pointer dereference.

## Structs by value

A struct **parameter** is passed as the i32 address of the caller's object, and
the callee copies it into its own frame on entry. That copy is what makes the
parameter by *value*: the callee may modify it freely without the caller
seeing the change.

A struct **return** is written through a hidden leading pointer parameter into
storage the caller allocated, and the function returns nothing.

The complication is that the front end already implements half of this, and
which half depends on the struct's size, because it encodes the SysV rule
directly:

| Struct size | What arrives here | What the back end does |
| --- | --- | --- |
| over 16 bytes | the front end has already rewritten the call: result storage allocated, its address passed as a hidden first argument, the call marked void | declare that parameter; otherwise nothing |
| 16 bytes or less | SysV returns it in registers, so the front end leaves it as a by-value result | wasm has no such return, so apply the same hidden-pointer trick one size band lower |

Treating the first case like the second passes *two* hidden pointers and
misaligns every later argument. `_wasm_sret_kind` is the single place that
decides which is which.

One further wrinkle: in the over-16 case the hidden pointer's `LoadArg` and the
first real parameter's `LoadArg` **both carry `arg_num` 0** -- the register back
ends tell them apart by assigned register, which wasm does not have. And
`LoadStructArg`, used for a struct parameter over 8 bytes, carries no `arg_num`
at all. So parameters are resolved through an explicit map built by walking the
loads in order, rather than by arithmetic on `arg_num`.

## Static address constants

`static char *p = "hi";` and `int *gp = &g;` work. There is no linker and no
relocation step here -- every address is known while the module is being
built -- so a symbolic initializer resolves to a plain number in the data
segment.

This forced the static layout into two passes: one object's initializer can
name another (`int g; int *gp = &g;`), so all addresses must be assigned
before any image referring to them is built. The first pass also walks the
*symbol table* rather than the IL, because an object referenced only from
another object's initializer appears in no IL command at all and would
otherwise never be placed.

One gap here is not this back end's: `int *ap = &a[2];` is rejected as a
non-constant initializer by the front end on every target, x86-64 included,
though gcc accepts it.

## Scope

Working: locals, `+ - * / %`, the bitwise and shift operators, the six
comparisons, `if`/`while`/`for`/`do`/`switch`/`break`/`continue`, direct calls
and recursion, across `char`/`short`/`int`/`long` and their unsigned forms;
pointers (including pointer-to-pointer and pointer arithmetic), arrays
(including multidimensional), structs and struct pointers, file-scope and
static globals with initializers, string literals, `float`/`double` arithmetic,
comparison and conversion, variadic functions, function pointers and indirect
calls (including to variadic functions), struct assignment, structs passed and
returned by value, address constants in static initializers, and imports --
including WASI, so a program can actually print.

Not yet implemented, and **refused rather than miscompiled**:

Nothing in the C subset described above is currently refused. Where a
limitation remains it is in the shared front end rather than here -- for
example `int *ap = &a[2];` is rejected as a non-constant initializer on every
target, x86-64 included, though gcc accepts it.

Anything the back end genuinely cannot lower still raises
`NotImplementedError` naming what is missing, rather than emitting a module
that runs and is wrong.

## Going the other way: wasm2c

    python3 tools/wasm2c.py prog.wasm -o prog.c
    gcc -std=c99 -Itools prog.c -o prog -lm && ./prog

`shivyc/wasm_reader.py` decodes the binary format (the inverse of
`shivyc/wasm.py`) and `tools/wasm2c.py` renders a decoded module as C. Two
things have to be bridged, and both have a standard answer:

**The operand stack.** A valid module's stack depth and types are known
statically at every point, so the stack flattens into ordinary locals: depth 0
of type i32 is always `i0`, depth 1 of type i64 is `j1`. `i32.add` becomes
`i0 = i0 + i1;`. No runtime stack exists in the output.

**Structured control flow.** A `block` becomes a label at its `end` and a
`loop` a label at its start, so `br N` is a `goto` to the label of the Nth
enclosing construct -- which is exactly what `br` means. That single
difference in label placement is the whole distinction between the two.

`tools/wasm2c_rt.h` supplies the operations that cannot be written as a direct
C expression without being wrong: wasm traps on a zero divisor and *defines*
`INT_MIN % -1` as 0 where C leaves both undefined; wasm masks shift counts
where C leaves an over-wide shift undefined; memory access goes through
`memcpy` so an unaligned or type-punned load is not undefined behaviour.
`tools/wasm2c_rt_wasi.h` binds WASI imports to POSIX, so a translated module
becomes a working native binary.

This was written independently. The approach is shared with wabt's `wasm2c`
and other tools -- it is the obvious way to do it -- but no code is taken from
any of them, and the tree stays MIT rather than acquiring wabt's Apache-2.0
terms.

### Reading other producers' modules

The decoder targets the format, not this compiler's habits, so modules from
elsewhere work. A Rust-built `qcms_bg.wasm` (138 functions, 41,660
instructions) decodes, translates to 45,000 lines of C, and compiles clean.

### Writing SIMD in C

    #include <wasm_simd128.h>

    v128_t a = wasm_i32x4_splat(20);
    v128_t b = wasm_i32x4_splat(22);
    int x = wasm_i32x4_extract_lane(wasm_i32x4_add(a, b), 0);   /* 42 */

Under `--target wasm` each intrinsic becomes the single SIMD instruction it
names. Under any other target the *same source* compiles to portable scalar C,
which is not a convenience: it is what lets an ordinary compiler act as the
oracle for vector code. `tools/wasm_simd_compile_difftest.py` builds each case
both ways and requires the answers to agree, then checks the module really
contains vector instructions rather than a scalar expansion that happens to be
right.

`shivyc/include/wasm_simd128.h` is generated by `tools/gen_wasm_simd128.py`
from the opcode table, so the intrinsics and the back end agree without either
listing the operators: `i32x4.add` is `__builtin_wasm_i32x4_add` by
construction. 148 operators have scalar fallbacks and are exposed as `wasm_*`
intrinsics; the rest are still declared as builtins under `__wasm__` and can
be called directly.

A `v128_t` is a 16-byte struct, because the front end has no vector type.
Aggregates already live in the frame and are passed by address, which is what
a vector needs anyway -- so an intrinsic loads its operands with `v128.load`,
runs the instruction, and stores the result back. That costs memory traffic
between consecutive intrinsics, which an engine's optimiser largely removes;
keeping a vector in a wasm local would need a front-end type that does not
exist yet, and is the obvious next improvement.

The lane index of `extract_lane` and `replace_lane` must be a constant: it is
encoded into the instruction, not evaluated. The builtins take it last so the
back end can find it without a per-operator table, and the macros reorder
`replace_lane` to clang's `(vector, lane, value)`.

### SIMD in wasm2c

All 256 SIMD opcodes decode, and 216 of the 236 core operators translate to C.
The remaining twenty are **relaxed SIMD**, whose results the specification
leaves implementation defined by design -- translating them would mean picking
one behaviour and presenting it as the answer, so they are refused by name
instead.

Both halves are generated rather than written out. `shivyc/wasm_simd.py`
builds the opcode table from the structure of the space:

    cmp_int(35, "i8x16")        # the ten comparisons, in their fixed order
    seq(224, FLOAT_ARITH, "f32x4.%s")

and `shivyc/wasm_simd_c.py` generates the C for each operator family:

    lanewise(["add"], ints, "A + B")
    lanewise(["sub"], ints, "A - B")

That is 256 opcodes and 216 handlers from roughly 300 lines, and -- more to
the point -- a mistake in a family is a mistake in one visible line rather
than in one of six that all look alike.

Generation has its own failure mode, though, which is why both halves are
checked. `self_check()` re-derives the opcode counts and rejects duplicates,
`coverage()` reports any operator in the table with no code path, and
`tools/wasm_simd_difftest.py` builds small SIMD modules and compares the
module's result under node against its translation compiled by cc. That last
check immediately caught `i32x4.sub` computing an *addition*: a single
`lanewise(["add", "sub"], ints, "A + B")` had given both operators the same
expression. Six operators, one wrong line, invisible on inspection.

A v128 is rendered as a union of lane arrays, so `i32x4.add` becomes a
four-iteration loop over `.u32x4`. No vector intrinsics are used: the point is
portable C that says what the specification says, and compilers re-vectorise
these loops perfectly well.

### Reference types

`funcref` and `externref` decode and translate. A reference is opaque -- null,
a function, or a host value -- so it becomes `void *` in C, with `NULL` as
`ref.null`. Tables are arrays of those: a funcref table's entries are function
pointers, which is what makes `call_indirect` a cast and a call, and an
externref table's are whatever the host put there.

What the proposal actually added, and what had to change:

  - **Reference value types**, so a local, a parameter or a table slot can
    hold one. `select` gained a typed form (`0x1C`) because the untyped one
    cannot say which reference it is choosing.
  - **Multiple tables**, each with its own element type. The old code assumed
    exactly one funcref table; `call_indirect` now honours the table index in
    its immediate.
  - **Table instructions** -- `table.get/set/size/grow/fill/copy/init` and
    `elem.drop` -- none of which existed in the MVP.
  - **Seven new element-segment encodings.** Before, there was one form:
    active, table 0, a vector of function indices. Reference types added
    passive and declarative segments, explicit table indices, and initialisers
    written as expressions, selected by the low three bits of a flags field.
    Refusing those seven is what made wasm-bindgen output undecodable.

`tools/wasm_ref_difftest.py` builds modules using each of these and compares
the result against node. The modules are hand-encoded because Crust's own back
end emits none of it -- which also makes it the only test exercising the
element-segment forms no other producer here generates.

### What real modules do

Three third-party modules, all built by toolchains other than this one, and
all of them *run*, not merely compile:

| Module | Functions | Instructions | SIMD | C lines | Checks |
| --- | --- | --- | --- | --- | --- |
| `qcms_bg.wasm` (Rust) | 138 | 41,660 | 0 | 45k | 84 pass |
| `jbig2.wasm` | 121 | 49,924 | 709 | 55k | 20 pass |
| `openjpeg.wasm` | 202 | 112,690 | 1,790 | 119k | 16 pass |

`tools/wasm_module_difftest.py` calls every export with a fixed argument
vector under node and under the translated C, and compares the return value
*and a hash of the whole of linear memory*. The memory hash is what makes it
worth running: a wrong store offset, a lane written in the wrong order, a load
that sign-extends when it should not -- none of those need change a return
value, but all of them change memory.

Each export runs in its own process, because a trap on the C side exits and
there would be no way to reach the next one. Imports are stubbed to return
zero on both sides -- not a simulation of the host, but a *deterministic* one,
which is all the comparison needs.

Two caveats worth stating. The arguments are generic (0, 1, 16, 1024), so
these calls reach the modules' entry points and allocators rather than every
SIMD kernel inside them; driving `jbig2` with a real image would exercise far
more. And a stubbed host means code paths that depend on host replies are not
reached at all.

### Linear memory grows

Memory is `calloc`ed and `realloc`ed rather than a fixed array, because real
modules grow it -- an allocator does so on its first call, and a fixed array
turns that into a trap. `memory.grow` honours the module's declared maximum
and zeroes the new pages, as the specification requires.

## Testing

```sh
make test_wasm                       # fixed corpus vs gcc
make roundtrip_wasm                  # C -> wasm -> C -> native, vs gcc
make fuzz_wasm SEED=3 COUNT=300      # random programs vs gcc
make test_wasm_simd                  # SIMD translation (wasm2c) vs node
make test_wasm_simd_compile          # SIMD compilation (C -> wasm) vs cc
make test_wasm_ref                   # funcref/externref vs node
make test_wasm_module MODULES=x.wasm # run a real module both ways
make wasm2c WASM=prog.wasm           # translate one module back to C
```

`tools/wasm_difftest.py` runs a fixed corpus: compile with ShivyC, run under
node, compare against the exit status of the same program built by gcc. The
out-of-scope cases are part of the corpus and are expected to *refuse*, so the
scope boundary is tested rather than merely documented; a case that starts
passing is reported as `newly-supported` and fails the run until the list is
updated.

`wasm_run.js` is a minimal WASI preview-1 host (fd_write, proc_exit, and stubs
that return ENOSYS rather than trapping). Because passing against our own host
would only prove the module matches our own reading of the spec, the difftest
also runs one module under **node's real WASI** and checks its stdout and exit
status -- an independent implementation, which is what would catch a subtly
wrong import signature or `_start` contract.

`tools/wasm_roundtrip.py` closes the loop:

    prog.c --shivyc--> prog.wasm --wasm2c--> prog_back.c --cc--> binary

and requires that binary to agree with `cc prog.c` on both stdout and exit
status. It is a stronger check than either half alone, for two reasons. A
back-end bug that the difftest misses because the module happens to run
correctly on one engine still has to survive being read back, re-expressed as
C, and compiled by a different compiler. And the encoder and decoder were
written from the specification rather than from each other, so a
misunderstanding on one side shows up here instead of cancelling out.

Its corpus is imported from `wasm_difftest.py`, so every case that file gains
is round-tripped too, with no second list to maintain. One case is expected to
*differ* -- the float-to-int saturation above -- and is listed in
`EXPECTED_DIVERGENT`; if it ever stops diverging, that means wasm semantics
were lost in translation, so the harness fails the run.

`tools/wasm_fuzz.py` generates random integer programs -- nested expressions,
mixed widths and signedness, loops, calls -- and checks each against gcc.

When a fuzz case disagrees with gcc, the harness also asks the **x86-64** back
end. The front end is shared by every target, so a mistyped expression is wrong
everywhere; if x86-64 gives the same wrong answer, both back ends are
faithfully lowering an IL that is already wrong, and the bug is upstream. Those
are reported separately and do not fail the run, which keeps a wasm regression
from being buried under pre-existing front-end noise.

### Two harness traps worth knowing about

Both cost real debugging time here.

A C exit status is the low 8 bits of what `main` returned, so **all 256 values
are legal answers** and none of them can double as an error sentinel. An early
version of the runner exited with the program's status and reserved 70 for "the
engine rejected the module" -- and a program returning `-186` produces exactly
70, so a perfectly valid module was reported as broken. The runner now writes
the result to stdout and uses a non-zero exit purely for engine errors.

`tools/riscv64_difftest.py` lowercases the compiler output into `blob` and then
tests `if "NotImplementedError" in blob`, which can never match. Its SKIP branch
is dead, so every riscv64 refusal is currently reported as an ERROR. This
harness matches on the untouched text instead. The riscv64 one is worth fixing
separately.

## A front-end bug this back end surfaced (now fixed)

Shifts were being given the wrong result *type*. `_ArithBinOp.make_il`
(`shivyc/tree/arithmetic_exprs.py`) applied the usual arithmetic conversions to
both operands, and `_BitShift` inherited that. C99 6.5.7p3 says the integer
promotions are performed on each operand *separately*, and the result type is
that of the promoted **left** operand -- the right operand must not pull the
left one to unsigned.

```c
int main(void){ int a=467; unsigned int b=1;
                return ((a>>b) >= -423) ? 1 : 2; }   /* gcc: 1, shivyc was: 2 */
```

The shifted bits were right; only the type was wrong, which flipped the
following comparison to unsigned so `-423` compared as a huge number. It
reproduced identically on x86-64 -- it was never a wasm bug, just one the
differential fuzzer hit constantly. `_BitShift` now overrides
`_convert_operands` to promote each operand independently.
