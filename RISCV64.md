# The ShivyCX RV64 back end

ShivyCX compiles its architecture-neutral IL to RISC-V 64 assembly with
`--target riscv64`. The back end lives in [`shivyc/asm_gen.py`](shivyc/asm_gen.py)
(the `_rv_*` methods) behind the `Target` seam in
[`shivyc/targets`](shivyc/targets/__init__.py).

The toolchain below it is also our own — [`rasm`](tools/rpy_lib/RASM.md)
assembles RV64 and [`rlink`](tools/rpy_lib/LINKER.md) links it — so a C program
becomes a running RV64 binary with no external tool and no libc:

```
C  ->  ShivyCX  ->  rasm  ->  rlink  ->  static ELF executable
```

RV64 was brought up second, after AArch64, specifically to test whether the
architecture-neutral middle end was actually neutral. It was: the register
allocator, liveness analysis, live-interval construction and copy coalescing
are shared verbatim, and when floating point was added the allocator needed
**no changes at all** — it already took FP register pools as parameters. See
[ARM64.md](ARM64.md) for the allocator's design, which is not repeated here.

## Trying it

```sh
python3 -m shivyc.main prog.c -S -o prog.s --target riscv64
riscv64-linux-gnu-gcc -static prog.s -o prog && qemu-riscv64 ./prog
```

The differential tester compiles a corpus with both ShivyCX and the cross
`gcc`, runs both under qemu, and asserts the exit codes match:

```sh
python3 tools/riscv64_difftest.py   # riscv64 difftest: 169 pass, 0 fail, 1 xfail
```

Needs `apt install gcc-riscv64-linux-gnu qemu-user`.

For the fully self-hosted path — our assembler, our linker, our runtime:

```sh
python3 tools/rpy_lib/rasm_riscv_obj_test.py   # 65 pass, 0 fail, 0 skip
```

## What is supported

The back end is feature-complete for the C subset ShivyCX accepts, and the
difftest runs with **zero skips**:

- **Integers** of every width, signed and unsigned, with correct narrowing and
  widening (see [the sign-extension rule](#the-sign-extension-rule) below).
- **Arithmetic and bitwise**: `+ - * / %`, `& | ^ ~`, `<< >>`, unary `-`, with
  immediate forms where the encoding allows and the `w` variants where a
  32-bit result is wanted.
- **Comparisons** and all control flow: `if`, `while`, `for`, `switch`, `?:`,
  short-circuiting `&&`/`||`.
- **Floating point**: `float` and `double` arithmetic, comparisons, every
  `int`↔`float`↔`double` conversion, and literals emitted to `.data`.
- **Aggregates and memory**: pointers and address-of, multi-dimensional
  arrays, `struct`/`union` including by-value copy, and string literals.
- **Globals**: file-scope and static storage in `.data`/`.bss`, addressed with
  `lla`.
- **Calls**: direct, indirect through function pointers (`jalr`), recursion,
  and the full lp64d convention including stack arguments.

### One known divergence

Plain `char` is **unsigned** on the RISC-V psABI (and on AArch64) but signed on
x86-64; ShivyCX treats it as signed on every target. This is a
target-dependent *front-end* issue, not a back-end one. It is recorded as an
`XFAIL` case (`rv_g_plainchar_abi`) so the gap stays visible, and the harness
treats an unexpected **pass** as a failure — fixing it will prompt removing the
marker rather than going unnoticed.

## Where RV64 differs from AArch64

Most of the interest in a second ISA is in what it does *not* share. These are
the places the two back ends genuinely diverge, each of which was a source of
at least one bug.

### The sign-extension rule

The RV64 psABI keeps **every 32-bit value sign-extended in a register,
regardless of its signedness**. That is a rule about register contents, not a
choice, and it drives all the conversion code:

- Widening an **unsigned** 32-bit value cannot be a `mv` — the register holds
  it sign-extended, so a move carries the sign into the high half and
  `unsigned x = 2147483648u; long y = x;` yields a negative `y`. It has to
  zero-extend explicitly (`slli 32` / `srli 32`).
- Narrowing cannot stop at `addiw`, which only truncates to 32 bits.
  `short s = (short)70000;` must truncate to the target width and re-extend by
  the *target's* signedness.

Both were live bugs. Neither was visible until the corpus carried the
difference past bit 7 — see [the low-byte trap](#on-the-corpus).

### Frame layout and stack arguments

AArch64 addresses its frame off `x29`, so `sp` can move freely around a call to
carve out an outgoing-argument area. **RV64 addresses its frame off `sp`**, so
moving `sp` would shift every slot offset out from under the function. The
outgoing area is instead reserved at the *bottom* of the frame and every other
slot shifts past it.

### `la`/`lla` and the PC-relative pair

The psABI splits a PC-relative address across `auipc`+`addi` with **two**
relocations, and the low one does not name the target — it names a label on the
`auipc`, because that is the instruction whose PC the high half was computed
against. Emitting it therefore requires *defining a symbol*, which an
instruction encoder cannot do. The assembler driver rewrites it into what gas
produces:

```
.Lpcrel_hiN:
        auipc rd, %pcrel_hi(sym)
        addi  rd, rd, %pcrel_lo(.Lpcrel_hiN)
```

### Floating point

| | AArch64 | RV64 |
|---|---|---|
| precision | in the register name (`s0` vs `d0`) | in the mnemonic (`fadd.s` vs `fadd.d`) |
| comparison | `fcmp` sets flags, then `cset` | `feq`/`flt`/`fle` write 0/1 straight to an integer register |
| float→int | `fcvtzs`/`fcvtzu` truncate by definition | needs an explicit `rtz` operand |
| literals | `.data` + `adrp`/`add`/`ldr` | `.data` + `lla`/`fld` |

The `rtz` operand is not decoration. C truncates toward zero, but RISC-V's
default rounding mode is *dynamic* (round-to-nearest), so omitting it is wrong
on every value with a fractional part of 0.5 or more.

RV64's comparisons are all **ordered** — NaN yields 0 — which is what C wants
for `<`, `<=`, `>` and `>=`. `!=` is the negation of `feq`, and so correctly
yields 1 for NaN.

## Register usage

lp64d, with integer and floating-point arguments counted in **separate**
sequences — so a function's third parameter may arrive in `a0` if the first two
were doubles.

| role | integer | floating-point |
|---|---|---|
| arguments / return | `a0`-`a7` | `fa0`-`fa7` |
| callee-saved homes | `s2`-`s11` | `fs0`-`fs11` |
| caller-saved homes | `a<cs>`-`a7`, `t4`-`t6` | `ft0`-`ft7`, unused `fa`, `ft8`-`ft9` |
| scratch (never a home) | `t0`-`t3` | `ft10`, `ft11` |

The caller-saved integer pool starts *above* the argument registers this
function actually uses (`cs = max(max-call-arity, incoming-params, 1)`), so
neither call-argument setup nor parameter unloading can clobber a live home.

Scratch assignments are load-bearing and worth knowing before adding code:
`t0`/`t1` stage operands, `t2` scales a variable array index *and* addresses
float literals, and `t3` receives computed addresses. Float literals
deliberately avoid `t3`: a literal operand is loaded *after* an address is
formed, so using `t3` would overwrite the destination of the store being set
up, and `v.x = 1.5` would write into `.data` instead of the struct.

## How it was built and validated

The back end grew in independently shippable stages — integer core, globals,
bitwise and shifts, pointers and aggregates, floating point, string literals,
function pointers, stack arguments — each gated by four checks:

- **Differential correctness** against the cross `gcc` under qemu
  ([`tools/riscv64_difftest.py`](tools/riscv64_difftest.py)).
- **No x86 regression** (`make testfast`).
- **Self-host safety** (`make selfhost_asmgen`): `asm_gen.py` must keep
  transpiling through py2c and compiling as C.
- **Toolchain agreement**: the encoder is byte-compared against
  `riscv64-linux-gnu-as` (223/223) and the linker's relocations against GNU
  `ld` (`rlink_cross_test.py`).

### On the corpus

The corpus was shaped by **mutation testing** — deliberately breaking a
decision and checking that something fails — rather than by writing programs
that looked representative. That repeatedly found the corpus measuring less
than it appeared to.

The recurring trap is that **an exit code carries only the low 8 bits**.
`return u` cannot distinguish a zero-extended `unsigned char` from a
sign-extended one, because both agree in that byte. Neither can adding a
constant, nor dividing by 2 — the wrong value differs by a multiple of 256 or
65536, which halving preserves. A divisor coprime with 256 is what makes the
difference visible, and several cases in the corpus divide by 3 for exactly
that reason.

The same blind spot in another guise: a load or store of the wrong *width*
through a pointer is invisible unless something reads the **neighbouring**
bytes. The corpus therefore writes one element of a padded array and checks its
neighbours survived.

Two bugs were found not by a failing test at all, but by measuring **encoder
coverage** — feeding every instruction the compiler emits through our own
assembler. The difftest assembles with gas, so a gap in `rasm` is invisible
there. That is how `li` with a 64-bit constant, and a label spelled like a
register (`call f2`), were caught.

## Files

- [`shivyc/asm_gen.py`](shivyc/asm_gen.py) — `_make_asm_riscv64`, the `_rv_*`
  lowering helpers, and the shared `_il_*` allocator core.
- [`shivyc/targets/__init__.py`](shivyc/targets/__init__.py) — the `Target`
  seam (`RiscV64Target`).
- [`tools/riscv64_difftest.py`](tools/riscv64_difftest.py) — differential
  tester against the cross `gcc`.
- [`tools/rpy_lib/rasm_riscv.py`](tools/rpy_lib/rasm_riscv.py) — the RV64
  instruction encoder, with
  [`rasm_riscv_test.py`](tools/rpy_lib/rasm_riscv_test.py) comparing it
  byte-for-byte against GNU `as`.
- [`tools/rpy_lib/rcrt_riscv64.s`](tools/rpy_lib/rcrt_riscv64.s) — freestanding
  `_start` and runtime over `ecall`.
- [`tools/rpy_lib/rasm_riscv_obj_test.py`](tools/rpy_lib/rasm_riscv_obj_test.py)
  — end-to-end compile→assemble→link→run.
- [`ARM64.md`](ARM64.md) — the AArch64 back end and the shared register
  allocator.
- [`tools/rpy_lib/RASM.md`](tools/rpy_lib/RASM.md),
  [`tools/rpy_lib/LINKER.md`](tools/rpy_lib/LINKER.md) — assembler and linker.
