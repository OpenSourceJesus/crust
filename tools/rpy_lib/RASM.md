# rasm — self-contained x86-64 assembler

Goal: remove the last external dependency in the ShivyCX toolchain. Today the
compiler emits Intel-syntax assembly and shells out to the GNU assembler (`as`)
to produce ELF `.o` objects (`shivyc/main.py:assemble`). `rasm` replaces `as`
with an RPython-translatable assembler so the whole compile→assemble→link path
is our own code, and can eventually be fused with ShivyCX into a self-contained
JIT.

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

## The linker

`ld` was the one remaining external tool in the self-hosted path. It is now
replaced by **rlink** — see `LINKER.md`. With `SHIVYC_RASM=1 SHIVYC_RLINK=1`
the entire `py2c → C → ShivyCX → rasm → rlink` chain is our own code.
