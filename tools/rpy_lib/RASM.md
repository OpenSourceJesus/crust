# rasm — self-contained assembler (x86-64, AArch64, RV64)

Goal: remove the last external dependency in the ShivyCX toolchain. The
compiler used to emit assembly and shell out to the GNU assembler (`as`) to
produce ELF `.o` objects (`shivyc/main.py:assemble`). `rasm` replaces `as`
with an RPython-translatable assembler so the whole compile→assemble→link path
is our own code, and can eventually be fused with ShivyCX into a self-contained
JIT.

It now assembles **three** architectures — x86-64, AArch64 and RV64 — behind a
shared driver and ELF writer. See the [Other architectures](#other-architectures)
section below; everything before it describes the original x86-64 support.

## Status

Done and validated:

* **Encoder** (`rasm.py`): x86-64 machine-code encoding — REX / ModRM / SIB /
  displacement / immediate layout — for the instruction and operand vocabulary
  ShivyCX emits (`mov movsx movzx movsxd lea push pop add sub or and xor cmp
  test imul idiv div mul neg not cqo cdq sal shl sar shr call jmp ret leave nop`
  and the `Jcc` family), plus **SSE scalar float** (`movsd movss addsd subsd
  mulsd divsd ucomisd ucomiss comisd cvtsi2sd cvtsi2ss cvttsd2si cvttss2si
  cvtsd2ss cvtss2sd movq movd xorps xorpd pxor sqrtsd` over xmm0–xmm15).
  Operands: registers (8/16/32/64-bit incl. r8–r15, xmm0–xmm15),
  immediates (imm8 sign-extension, accumulator short forms, `movabs` for
  64-bit immediates that do not fit imm32), and memory
  (`[base + index*scale + disp]`, constant products, disp8/disp32, RIP-relative,
  absolute, symbolic). Correct REX handling incl. forced REX for the
  `spl/bpl/sil/dil` 8-bit registers. **123/123 differential cases (integer +
  SSE) byte-identical to GNU `as`.**
* **Parser** (`rasm.py`): the Intel-syntax subset ShivyCX emits — directives,
  labels, `SIZE PTR [...]` memory operands, comments.
* **Driver** (`rasm_obj.py`): two passes — lay out `.text`/`.data`/`.bss` from
  the directive stream (`.section .global .comm .quad .int .byte .zero`), record
  labels, encode instructions and data, collect relocations; then resolve
  same-section PC-relative refs to local labels in place, keeping the rest as
  ELF relocations.
* **ELF64 writer** (`rasm_obj.py`): emits an ET_REL x86-64 object — header,
  `.text`/`.data`/`.bss`, `.symtab`/`.strtab`/`.shstrtab`, `.rela.text`/
  `.rela.data`, `.note.GNU-stack` — with STT_SECTION/FUNC/OBJECT symbols,
  SHN_COMMON, and R_X86_64_PC32 / _32S / _64 relocations. Referenced local
  labels (e.g. ShivyCX float literals `__fltlitN` in `.data`) are emitted as
  STB_LOCAL symbols so their relocations resolve.
* **End-to-end test** (`rasm_obj_test.py`): compile a C program with ShivyCX,
  assemble it with **both** rasm and `as`, link both with gcc, run both, and
  require matching results. **10/10 programs pass** (arithmetic, recursion,
  loops, globals, bitops, conditionals, floats, function-pointers-in-data,
  nested calls, arrays, pointers/structs).
* **Corpus coverage**: across 61 ShivyCX-compilable C files (6858 instructions),
  rasm encodes **every instruction** (61/61 files).
* **Integrated pipeline**: with `SHIVYC_RASM=1`, ShivyCX routes assembly through
  rasm instead of `as` for the full compile→assemble→link→run. Over the runnable
  corpus, **55/55 programs** produce results identical to ShivyCX+`as`.

The encoding model mirrors pycca and the Intel SDM, rewritten flat (no
metaclasses/generators/`**kwargs`; one uniform `Operand` class) for RPython.

Added since:

* **Branch relaxation** (`rasm_obj.py`): sections are now built as *fragments*
  rather than a flat byte list, because both relaxation and `.align` make a
  section's layout depend on itself. Layout is computed as a fixpoint — every
  relaxable branch starts short (rel8) and is promoted to rel32 only if it turns
  out not to reach. Promotion is one-way, so the loop terminates. Only branches
  to *local* labels in the *same* section relax; anything else keeps a
  relocation so the linker can resolve or preempt it. On branch-heavy ShivyCX
  output the resulting `.text` is now **byte-identical to GNU `as`**.
* **AT&T-syntax front end** (`rasm.parse_att_line`): `%reg` operands, `$imm`
  immediates, `disp(base,index,scale)` memory, reversed operand order,
  suffix-derived operand sizes (`movl`/`addq`/…), the `movsbl`/`movzwq`/
  `movslq` family, `cltq`/`cqto`/`cwtl` aliases, `*`-indirect branches, and `#`
  comments. It normalises to the same operand form the Intel parser produces, so
  the encoder is shared. `.att_syntax` regions now assemble instead of raising.
* **Extended instruction set** for runtime and linker work: `syscall`, the
  `setcc` and `cmovcc` families, `inc`/`dec`, `xchg` (including the one-byte
  accumulator form), `bswap`, `movabs`, `int imm8`, `hlt`, `int3`, `ud2`,
  `endbr64`, `cld`/`std`/`cli`/`sti`, the fences, `cpuid`/`rdtsc`/`pause`, and
  the string ops (`movsb`/`stosq`/…) with `rep`/`repne`/`lock` prefixes.
* **Directives**: `.align`/`.balign`/`.p2align` now emit real padding (they were
  silently ignored), `.string`/`.asciz`/`.ascii` emit real string data with
  C-escape decoding (they were silently dropped — a data-loss bug), plus
  `.type`, `.size`, `.lcomm`, `.space`, and arbitrary named sections such as
  `.rodata` and `.text.foo`.
* **Two encoder fixes**: `mov reg, symbol` emitted a zero immediate with no
  relocation; and RIP-relative references (`[rip+sym]`) used the wrong
  relocation addend — it must step back over the four displacement bytes and any
  trailing immediate. Both now match GNU `as` exactly (`sym-4`, `sym-8`).
* **AT&T/extended differential test** (`rasm_att_test.py`): **87/87 cases
  byte-identical to GNU `as`.**

## Not yet done

* **RPython cleanup + minipy**: drop `partition` tuple-unpacking, make dict
  iteration order-independent, add type annotations; translate, then run under
  minipy for full self-hosting.

## Integration

`shivyc/main.py:assemble` uses rasm when the `SHIVYC_RASM` environment variable
is set, and the external `as` otherwise (default unchanged). Run e.g.
`SHIVYC_RASM=1 PYTHONPATH=. python3 shivyc/main.py prog.c -o prog`.

## Files

* `tools/rpy_lib/rasm.py` — encoder + parser + operand/relocation model.
* `tools/rpy_lib/rasm_obj.py` — assembler driver + ELF64 object writer.
* `tools/rpy_lib/rasm_test.py` — differential encoder test vs GNU `as`.
* `tools/rpy_lib/rasm_obj_test.py` — end-to-end compile→assemble→link→run test.
* `tools/rpy_lib/rasm_att_test.py` — AT&T-syntax + extended-ISA differential test.

## Other architectures

AArch64 and RV64 are also assembled, end to end. A C program now becomes a
running binary on either without invoking a single external tool:

```
C  ->  ShivyCX  ->  rasm  ->  rlink  ->  static ELF executable
```

The x86-64 encoder could not be parameterised into these: x86 is a
variable-length byte stream built from REX/ModRM/SIB prefixes, while AArch64
and RV64 are single 32-bit words of fixed bit-fields. Almost nothing is shared
at the encoding level, so each ISA gets its own module producing the same
*outputs* — a byte list plus `rasm.Reloc` entries — for the same driver.

### The architecture seam

`rasm_arch.py` holds the facts the driver and linker had hardcoded to x86-64:
ELF `e_machine` and `e_flags`, default `.text` alignment, the widths of the
data-emitting directives, and the map from an abstract fixup to a numeric
relocation type. Every fact is an *instance* attribute, matching
`shivyc/targets` so it stays self-host safe.

Two of those are not cosmetic:

* **Data widths are per-target.** `.word` is 2 bytes to an x86 assembler but
  **4** to an AArch64 or RISC-V one, so a shared table silently emits half the
  data. The RISC targets also get `.half`/`.hword`/`.dword`/`.xword`.
* **`.align` padding.** `0x90` is the x86 nop; on a fixed-width ISA a single
  `0x90` byte is not an instruction at all, so those targets pad with zero.

`rasm.Reloc` also gained a `kind` string. Describing a fixup as
`(size, pcrel, signed)` is enough on x86-64, where the addressing mode already
determines the encoding — but not on a fixed-width ISA, where one AArch64
4-byte word can hold a 26-bit branch, a 21-bit page delta or a 12-bit page
offset depending on the owning instruction. `kind == ""` reproduces the old
inference, so x86-64 output is unchanged.

### AArch64 (`rasm_arm64.py`)

Covers the vocabulary the arm64 back end emits plus what a freestanding runtime
needs: moves (including `mov`-immediate's movz/movn/logical-immediate
selection, as gas chooses), add/sub immediate and shifted-register, logical
ops, mul/madd/msub, sdiv/udiv, shifts in both the `*v` register form and the
bitfield-alias immediate form, sxtw/sxtb/sxth/uxtb/uxth, cset/csel/csinc, the
load/store family (scaled unsigned, unscaled ldur/stur, pre/post-index,
`:lo12:`), ldp/stp, branches, adrp, and the FP set.

**`rasm_arm64_test.py`: 182/182 byte-identical to `aarch64-linux-gnu-as`.**

### RV64 (`rasm_riscv.py`)

Covers RV64IMFD — the R/I/S/B/U/J formats, loads and stores, branches,
lui/auipc, ecall/ebreak, and the F/D set including the full `fcvt` matrix with
explicit rounding-mode operands — plus the pseudo-instructions the compiler
emits and a runtime needs: `li`, `mv`, `ret`, `j`, `jr`, `call`, `tail`, `nop`,
`neg`/`negw`, `not`, `seqz`/`snez`/`sltz`/`sgtz`, `sext.w`, and the branch
aliases.

Two RV64 traits shape the module. **Pseudo-instructions are load-bearing**, so
`encode_line` returns a variable number of bytes — 8 for `call`/`tail`, and up
to five instructions for a `li` of a 64-bit constant. And **immediates are
scattered**: S-type and B-type split theirs across non-adjacent fields, and
B/J-type reorder the bits as well.

`la`/`lla` is the one pseudo-instruction that cannot live in the encoder. The
psABI splits a PC-relative address across `auipc`+`addi` with *two*
relocations, and the low one names a label on the `auipc` rather than the
target — so emitting it requires defining a symbol, which only the driver owns.
`rasm_obj` rewrites it into what gas produces:

```
.Lpcrel_hiN:
        auipc rd, %pcrel_hi(sym)
        addi  rd, rd, %pcrel_lo(.Lpcrel_hiN)
```

**`rasm_riscv_test.py`: 223/223 byte-identical to `riscv64-linux-gnu-as`.**

### Driver integration

`rasm_obj._line` dispatches to `_line_fixed` for fixed-width targets: one
mnemonic, one operand string, one word. Directives, section handling, symbol
binding and data emission are shared with the x86 path unchanged.

One thing had to be *disabled* rather than ported. `_resolve` patches
same-section PC-relative references to local labels in place, writing the
displacement as a plain little-endian field of `r.size` bytes. That is right on
x86-64 — the field *is* those bytes — but would overwrite the opcode and
register bits of a fixed-width word. Those targets keep the relocations for the
linker instead, so our objects carry a few more relocations than gas emits for
the same input; the resulting executables are equivalent.

Comment syntax also differs, and collides with operand syntax on one target:
AArch64 gas uses `//` and reserves `#` for immediates (`mov x0, #5`), so `#`
can only open a comment at the start of a line; RISC-V gas uses `#` anywhere
and rejects `//` outright.

### Freestanding runtimes

`rcrt_arm64.s` and `rcrt_riscv64.s` are the counterparts of `rcrt.s`: `_start`
plus `exit`, `write`, `read`, `puts`, `putchar`, `putint`, `strlen`, `memset`,
`memcpy`, `sbrk` and syscall shims, talking to the kernel through `svc #0` and
`ecall`. Both use the AArch64/RISC-V "generic" syscall table, *not* x86-64's —
`exit_group` is 94, not 231. The RV64 runtime establishes `gp` from
`__global_pointer$` first, inside `.option norelax`, since that sequence cannot
itself become gp-relative.

### End-to-end coverage

`rasm_arm64_obj_test.py` and `rasm_riscv_obj_test.py` compile each program with
ShivyCX, assemble it with **both** rasm and GNU `as`, link both — once with the
cross `gcc`, once with rlink and our runtime — and compare exit codes under
qemu. **56/56 and 65/65, no skips.**

Over the full difftest corpora the encoders handle every instruction the
compiler emits: **arm64 3754 instructions across 168/168 files, riscv64 3413
across 170/170.**

A note on what these tests are worth: the rlink mode initially reported 28/28
passing while every binary was segfaulting identically, because entry was at
`main` with no runtime and `ret` jumped into a garbage link register. Two
matching crashes satisfied the comparison perfectly. The harnesses now treat a
negative exit status as a failure rather than as agreement.

### Files

* `tools/rpy_lib/rasm_arch.py` — the architecture seam.
* `tools/rpy_lib/rasm_arm64.py`, `rasm_riscv.py` — the two encoders.
* `tools/rpy_lib/rasm_arm64_test.py`, `rasm_riscv_test.py` — differential
  encoder tests vs the GNU cross assemblers.
* `tools/rpy_lib/rasm_arm64_obj_test.py`, `rasm_riscv_obj_test.py` —
  end-to-end compile→assemble→link→run tests.
* `tools/rpy_lib/rcrt_arm64.s`, `rcrt_riscv64.s` — freestanding runtimes.

## The linker

`ld` was the one remaining external tool in the self-hosted path. It is now
replaced by **rlink** — see `LINKER.md`. With `SHIVYC_RASM=1 SHIVYC_RLINK=1`
the entire `py2c → C → ShivyCX → rasm → rlink` chain is our own code.
