# The ShivyCX AArch64 back end

ShivyCX compiles the same architecture-neutral IL its x86-64 back end consumes
into AArch64 assembly with `--target arm64`. The back end lives in
[`shivyc/asm_gen.py`](shivyc/asm_gen.py) (the `_arm64_*` methods) behind a
`Target` seam ([`shivyc/targets`](shivyc/targets/__init__.py)); the x86-64 path
is untouched by any of it.

Running on a Raspberry Pi or Jetson Nano? Both are AArch64 Linux and use this
back end unchanged; see [BOARDS.md](BOARDS.md) for the board-specific setup.

Running with no operating system underneath at all — boot, exception vectors
and MMU — is a separate path documented in
[BAREMETAL_ARM64.md](BAREMETAL_ARM64.md); it uses this same back end.

There is a second bare-metal target, RISC-V 64, documented separately in
[RISCV64.md](RISCV64.md). It shares the register allocator described below
verbatim — that section is the canonical description for both. This page
otherwise covers AArch64 only.

The toolchain below it is also our own. [`rasm`](tools/rpy_lib/RASM.md)
assembles both ISAs and [`rlink`](tools/rpy_lib/LINKER.md) links them, so a C
program becomes a running AArch64 binary without invoking a single external
tool and without libc:

```
C  ->  ShivyCX  ->  rasm  ->  rlink  ->  static ELF executable
```

The whole effort was done at the Python (rpython) level, which is the point: a
new bare-metal ISA back end is a few hundred lines of legible Python, and each
increment is checked the same way — differentially, against a real toolchain.

## Trying it

```sh
python3 -m shivyc.main prog.c -S -o prog.s --target arm64
aarch64-linux-gnu-gcc -static prog.s -o prog && qemu-aarch64 ./prog
```

The differential testers do this end to end and check each exit code against
the same program compiled by `gcc`:

```sh
python3 tools/arm64_difftest.py     # arm64 difftest: 168 pass, 0 fail
```

Needs `apt install gcc-aarch64-linux-gnu qemu-user`.

For the fully self-hosted path — our assembler, our linker, our runtime, no
libc — see [`RASM.md`](tools/rpy_lib/RASM.md);
`tools/rpy_lib/rasm_arm64_obj_test.py` exercises it over the same programs
(56 pass, 0 skip).

## What is supported

Both bare-metal back ends cover the same ground; where RV64 differs in *how*,
[RISCV64.md](RISCV64.md) has the detail.

- **Integers** of every width: `char`/`short`/`int`/`long`, signed and
  unsigned, with correct narrowing and widening on assignment.
- **Arithmetic and bitwise**: `+ - * / %`, `& | ^ ~`, `<< >>` (logical or
  arithmetic by signedness), unary `-`, with immediate forms where the
  encoding allows.
- **Comparisons** (all six) and **control flow**: `if`, `while`, `for`,
  `switch`, `?:`, `&&`/`||` short-circuiting — with compare/branch fusion on
  arm64 when a comparison feeds only a branch.
- **Floating point**: `float` and `double` arithmetic, comparisons, every
  `int`↔`float`↔`double` conversion, float literals emitted to `.data`, and a
  parallel FP register file with its own caller/callee split.
- **Aggregates and memory**: pointers and address-of, single- and
  multi-dimensional arrays, `struct`/`union` (including by-value copy),
  compound assignment, and string literals.
- **Globals**: file-scope/static storage emitted as `.data`/`.bss`, addressed
  with `adrp`/`add`.
- **Calls**: direct calls and recursion under AAPCS64 and lp64d, including the
  separate integer and floating-point argument sequences and the matching
  return registers.
- **Function pointers**: indirect calls via `blr`/`jalr`, including pointers
  stored in variables, passed as parameters, held in globals, and indexed out
  of an array. The address of a function is still recorded so a *direct* call
  stays a `bl`/`call`, but it is now also materialised, since once function
  pointers exist the value can be stored, copied or reassigned. The target is
  loaded *after* the arguments are staged, so materialising it cannot disturb
  the argument registers.

Not yet implemented on either back end: **more than eight arguments** of one
class (stack arguments). arm64
additionally lacks a whole-aggregate copy where one operand is a global, and a
variable array index whose element size is not a power of two. All of these
**raise** rather than emit wrong code, so the differential testers report them
as skips, never as silent miscompiles.

### One known divergence

Plain `char` is **unsigned** on both the AArch64 and RISC-V psABIs but
**signed** on x86-64; ShivyCX treats it as signed on every target. This is a
target-dependent front-end issue, not a back-end one, and it affects both
architectures equally. It is tracked as an `XFAIL` case in the RV64 corpus
(`rv_g_plainchar_abi`) so the gap stays visible.

## Pipeline

```
C  ->  lexer  ->  parser  ->  tree  ->  il_gen  ->  IL  ->  make_asm  ->  text
                                              (target-neutral)   (arm64 / riscv64)
```

`ASMGen.make_asm` dispatches on `target.name`. On arm64 it calls
`_make_asm_arm64`, which walks each function's IL through `_arm64_function`
(allocation + framing) and `_lower_arm64` (per-command instruction selection). Integer values get register homes,
floating-point values a parallel file, and anything in memory (spills,
address-taken locals, aggregates) a frame slot. Scratch registers are reserved
for operand staging and never used as value homes.

## Register allocation: liveness-based linear scan with a caller/callee split

The allocator is the most interesting part, and most of it is
**architecture-neutral** — the same `_il_*` methods serve every back end, and
this section is the canonical description for RV64 as well. When floating point
was added to riscv64 the allocator needed *no* changes at all: it already took
FP register pools as parameters.

1. **Copy coalescing.** A `Set(out, tmp)` copying a single-use temporary can
   let the defining instruction write `out` directly, eliding the move — but
   only when provably safe (`_il_coalesce_safe`): `tmp`'s definition and the
   copy sit in one straight-line block and `out`'s prior value is not read in
   between. This guard is what makes a swap like `t = a + b; a = b; b = t`
   compile correctly instead of clobbering `b` early.

   Three further guards matter, each of which was a real bug before it was
   added: a value must not be coalesced onto a **global** (whose home is a
   symbol, so the store would vanish), a **float** must not be coalesced onto
   an integer (which would eat the conversion), and a coalesced value's
   **floating-point home** must be propagated along with its integer one.

2. **Liveness** (`_il_liveness`). A backward live-variable fixpoint over a CFG
   built from the label/jump structure: `Return` has no successor, `Jump`
   targets its label, `JumpZero`/`JumpNotZero` branch to both fall-through and
   target, and `Call` falls through (it is not a branch).

3. **Intervals and call-crossing** (`_il_intervals`). Each value gets a
   conservative `[start, end]` live interval (safe across loop back-edges). A
   value is flagged as *crossing a call* when it is live both into and out of a
   `Call` — meaning it must survive the call instruction.

4. **Caller/callee split + linear scan** (`_il_linear_scan`). Two register
   pools per file:
   - **Callee-saved** homes for values that cross a call (`x19`-`x28` /
     `d8`-`d15` on arm64; `s2`-`s11` / `fs0`-`fs11` on RV64), preserved by the
     callee and saved once in the prologue.
   - **Caller-saved** homes for call-clean values, needing *no* save/restore.
     The integer caller pool is the argument registers *above* the function's
     needs — `cs = max(max-call-arity, incoming-params, 1)` — so neither
     call-argument set-up nor parameter unloading at entry can clobber a live
     home. That single bound removes the parallel-move hazard without a
     shuffle solver.

   Values are scanned in interval-start order; a register is reused once its
   previous occupant's interval has ended. Only the callee-saved registers
   actually used are saved, at packed offsets — and on arm64 a function that
   needs no callee saves, no spills, and makes no calls is emitted
   **frameless**.

The result beats `gcc -O0` instruction counts on leaf and call-light functions
(which carry zero save/restore overhead), and cuts recursive `fib` to less than
half its size under the previous "every value gets a dedicated callee-saved
home" model.

| function        | ShivyCX | gcc -O0 | callee-saves | frame      |
|-----------------|:-------:|:-------:|:------------:|:----------:|
| `int sq(int)`   |    4    |    6    |      0       | frameless  |
| sum loop (leaf) |   12    |   19    |      0       | frameless  |
| call-light      |   16    |   24    |      0       | —          |
| recursive `fib` |   29    |   20    |      3       | —          |

The remaining gap to `gcc` on branchy/recursive code is live-range *splitting*:
intervals are whole-range `[min, max]` with no holes, so a value live across
one call but idle for a long stretch still holds its register throughout. That
is the natural next allocator step.

## Floating point

Both back ends run a pipeline parallel to the integer one, over a separate
register file with its own caller/callee split. The interesting part is how
differently the two ISAs express it:

| | AArch64 | RV64 |
|---|---|---|
| precision | in the register name (`s0` vs `d0`) | in the mnemonic (`fadd.s` vs `fadd.d`) |
| comparison | `fcmp` sets flags, then `cset` | `feq`/`flt`/`fle` write 0/1 straight to an integer register |
| float→int | `fcvtzs`/`fcvtzu` truncate by definition | needs an explicit `rtz` operand — the default rounding mode is round-to-nearest |
| literals | `.data` + `adrp`/`add`/`ldr` | `.data` + `lla`/`fld` |

On arm64, FP comparisons are excluded from branch fusion to avoid the
unordered-condition subtleties. The RV64 side is covered in
[RISCV64.md](RISCV64.md).

## How it was built and validated

Each back end grew in independently shippable stages, every one a delta on the
previous and gated by three checks:

- **Differential correctness.** [`tools/arm64_difftest.py`](tools/arm64_difftest.py)
  compiles a corpus with both ShivyCX and the cross `gcc`, runs both under
  qemu, and asserts the exit codes match. Using a real compiler as an oracle is
  what caught the bugs that mattered.
- **No x86 regression.** The full x86-64 test suite (`make testfast`) must stay
  green; the new paths are wholly separate.
- **Self-host safety.** `shivyc/asm_gen.py` must still transpile through the
  Python→C front end (`py2c`) and compile as C, so the compiler can eventually
  compile itself for these targets too. `make selfhost_asmgen` enforces this.

  That gate was stated from the start but not *enforced*, and it silently
  regressed: a helper added for the arm64 back end took a register name and an
  ILValue, and py2c inferred the wrong C type for both, emitting calls that
  passed a `char *` where the prototype said `int`. Nothing caught it, because
  the Python is valid and every differential test passes — only the transpiled
  C is wrong, and only as a compiler *warning*. The lesson for new helpers is
  to pass register *numbers* and plain flags rather than formatted names and IL
  objects, matching the surrounding code; the test now fails on any new warning
  in the transpiled output.

### On the corpora

The test corpora were shaped by **mutation testing** — deliberately breaking a
decision and checking that something fails — rather than by writing programs
that merely looked representative. That process repeatedly found the corpus
measuring less than it appeared to, and the same trap came up five separate
times: **an exit code carries only the low 8 bits**. `return c` cannot
distinguish a sign-extended `char` from a zero-extended one, because both agree
in that byte. Neither can adding a constant, nor dividing by 2 — the wrong
value differs by a multiple of 256 or 65536, which halving preserves. A divisor
coprime with 256 is what makes the difference visible.

The same blind spot in another guise: a load or store of the wrong *width*
through a pointer is invisible unless something reads the **neighbouring**
bytes. That bug was live on arm64 for the whole project, passing three of four
hand-written char-pointer cases by coincidence, until the corpus wrote one
element of a padded array and checked its neighbours survived.

## Files

- [`shivyc/targets/__init__.py`](shivyc/targets/__init__.py) — the `Target`
  seam (`X86_64Target`, `Arm64Target`, `RiscV64Target`, `get_target`).
- [`shivyc/asm_gen.py`](shivyc/asm_gen.py) — `_make_asm_arm64`, the `_arm64_*`
  lowering helpers, and the shared `_il_*` allocator core.
- [`tools/arm64_difftest.py`](tools/arm64_difftest.py) — the differential
  tester.
- [`RISCV64.md`](RISCV64.md) — the RV64 back end.
- [`tools/rpy_lib/RASM.md`](tools/rpy_lib/RASM.md),
  [`tools/rpy_lib/LINKER.md`](tools/rpy_lib/LINKER.md) — the assembler and
  linker, including their AArch64 and RV64 support.
