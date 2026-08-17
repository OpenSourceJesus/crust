# The ShivyCX AArch64 and RV64 back ends

ShivyCX compiles the same architecture-neutral IL its x86-64 back end consumes
into AArch64 or RISC-V assembly, selected with `--target arm64` or
`--target riscv64`. Both back ends live in [`shivyc/asm_gen.py`](shivyc/asm_gen.py)
behind a `Target` seam ([`shivyc/targets`](shivyc/targets/__init__.py)); the
x86-64 path is untouched by any of it.

The toolchain below them is also our own. [`rasm`](tools/rpy_lib/RASM.md)
assembles both ISAs and [`rlink`](tools/rpy_lib/LINKER.md) links them, so a C
program becomes a running AArch64 or RV64 binary without invoking a single
external tool and without libc:

```
C  ->  ShivyCX  ->  rasm  ->  rlink  ->  static ELF executable
```

The whole effort was done at the Python (rpython) level, which is the point: a
new bare-metal ISA back end is a few hundred lines of legible Python, and each
increment is checked the same way — differentially, against a real toolchain.

## Trying it

```sh
# C -> assembly
python3 -m shivyc.main prog.c -S -o prog.s --target arm64
python3 -m shivyc.main prog.c -S -o prog.s --target riscv64

# assemble + link + run with the GNU cross toolchain
aarch64-linux-gnu-gcc -static prog.s -o prog && qemu-aarch64 ./prog
riscv64-linux-gnu-gcc -static prog.s -o prog && qemu-riscv64 ./prog
```

The differential testers do this end to end and check each exit code against
the same program compiled by `gcc`:

```sh
python3 tools/arm64_difftest.py     # arm64 difftest: 168 pass, 0 fail
python3 tools/riscv64_difftest.py   # riscv64 difftest: 169 pass, 0 fail, 1 xfail
```

Both need the cross toolchain and qemu:
`apt install gcc-aarch64-linux-gnu gcc-riscv64-linux-gnu qemu-user`.

For the fully self-hosted path — our assembler, our linker, our runtime, no
libc — see [`RASM.md`](tools/rpy_lib/RASM.md); the end-to-end testers
`tools/rpy_lib/rasm_arm64_obj_test.py` and `rasm_riscv_obj_test.py` exercise
it over the same programs.

## What is supported

Both back ends now cover the same ground:

- **Integers** of every width: `char`/`short`/`int`/`long`, signed and
  unsigned, with correct narrowing and widening on assignment. On RV64 that
  needs explicit care: the psABI keeps *every* 32-bit value sign-extended in a
  register regardless of signedness, so widening an unsigned int must
  zero-extend rather than move, and narrowing must truncate to the target
  width and re-extend by the target's signedness.
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
  with `adrp`/`add` on arm64 and `lla` on RV64.
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
architectures equally. It is recorded as an `XFAIL` case
(`rv_g_plainchar_abi`) so the gap stays visible; the harness treats an
unexpected *pass* as a failure, so fixing it will prompt removing the marker
rather than going unnoticed.

## Pipeline

```
C  ->  lexer  ->  parser  ->  tree  ->  il_gen  ->  IL  ->  make_asm  ->  text
                                              (target-neutral)   (arm64 / riscv64)
```

`ASMGen.make_asm` dispatches on `target.name`. On arm64 it calls
`_make_asm_arm64`, which walks each function's IL through `_arm64_function`
(allocation + framing) and `_lower_arm64` (per-command instruction selection);
riscv64 has the matching `_rv_*` pair. Integer values get register homes,
floating-point values a parallel file, and anything in memory (spills,
address-taken locals, aggregates) a frame slot. Scratch registers are reserved
for operand staging and never used as value homes.

## Register allocation: liveness-based linear scan with a caller/callee split

The allocator is the most interesting part, and most of it is
**architecture-neutral** — the same `_il_*` methods serve both back ends. When
floating point was added to riscv64 the allocator needed *no* changes at all:
it already took FP register pools as parameters.

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

Both back ends run a pipeline parallel to the integer one. The interesting part
is how differently the two ISAs express it:

| | AArch64 | RV64 |
|---|---|---|
| precision | in the register name (`s0` vs `d0`) | in the mnemonic (`fadd.s` vs `fadd.d`) |
| comparison | `fcmp` sets flags, then `cset` | `feq`/`flt`/`fle` write 0/1 straight to an integer register |
| float→int | `fcvtzs`/`fcvtzu` truncate by definition | needs an explicit `rtz` operand — the default rounding mode is round-to-nearest |
| literals | `.data` + `adrp`/`add`/`ldr` | `.data` + `lla`/`fld` |

RV64's comparisons are all *ordered* (NaN yields 0), which is what C wants for
`<`, `<=`, `>` and `>=`; `!=` is the negation of `feq` and so correctly yields
1 for NaN. On arm64, FP comparisons are excluded from branch fusion to avoid
the unordered-condition subtleties.

## How it was built and validated

Each back end grew in independently shippable stages, every one a delta on the
previous and gated by three checks:

- **Differential correctness.** [`tools/arm64_difftest.py`](tools/arm64_difftest.py)
  and [`tools/riscv64_difftest.py`](tools/riscv64_difftest.py) compile a corpus
  with both ShivyCX and the cross `gcc`, run both under qemu, and assert the
  exit codes match. Using a real compiler as an oracle is what caught the bugs
  that mattered.
- **No x86 regression.** The full x86-64 test suite (`make testfast`) must stay
  green; the new paths are wholly separate.
- **Self-host safety.** `shivyc/asm_gen.py` must still transpile through the
  Python→C front end (`py2c`), so the compiler can eventually compile itself
  for these targets too.

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
- [`shivyc/asm_gen.py`](shivyc/asm_gen.py) — `_make_asm_arm64` /
  `_make_asm_riscv64`, their `_arm64_*` / `_rv_*` lowering helpers, and the
  shared `_il_*` allocator core.
- [`tools/arm64_difftest.py`](tools/arm64_difftest.py),
  [`tools/riscv64_difftest.py`](tools/riscv64_difftest.py) — the differential
  testers.
- [`tools/rpy_lib/RASM.md`](tools/rpy_lib/RASM.md),
  [`tools/rpy_lib/LINKER.md`](tools/rpy_lib/LINKER.md) — the assembler and
  linker, including their AArch64 and RV64 support.
