# rlink — self-contained static ELF64 linker (x86-64, AArch64, RV64)

Goal: remove the **last** external dependency from the ShivyCX toolchain. With
`rasm` replacing GNU `as`, the self-hosting path was still
`py2c → C → ShivyCX → rasm → ld`. `rlink` replaces that final `ld`, so the whole
chain is our own code and a binary can be produced without invoking a single
external program.

```
$ SHIVYC_RASM=1 SHIVYC_RLINK=1 PYTHONPATH=. python3 shivyc/main.py prog.c -o prog
$ ./prog          # static ET_EXEC, no libc, no dynamic loader, no crt files
```

## Status

Done and validated:

* **ELF reader** (`rlink.py`): parses `ET_REL` x86-64 objects — section headers,
  `.symtab`/`.strtab`, and `SHT_RELA` relocation sections — into a uniform
  input model (`InSection` / `InSymbol` / `Reloc`).
* **Archive reader**: System V `ar` archives, including the `//` long-name
  string table. Members are pulled in only when they define a symbol that is
  still undefined, iterated to a fixpoint, which is the standard `ld` rule.
* **Symbol resolution**: strong definitions beat weak; a second strong
  definition warns and keeps the first; undefined *weak* references resolve to
  0; `SHN_COMMON` tentative definitions are allocated (size-ordered, aligned)
  into a synthetic `.bss` input section.
* **Layout**: input sections merge by output name — `.text.foo` → `.text`,
  `.rodata.str1.1` → `.rodata`, `.data.rel.ro` → `.data`, and so on — then group
  into two page-aligned `PT_LOAD` segments (R+X, then R+W) with file offsets
  congruent to virtual addresses modulo the page size, as the kernel loader
  requires. Default image base `0x400000`.
* **Relocations**: `R_X86_64_64`, `PC32`, `PLT32` (a direct call once statically
  linked), `32`, `32S`, `16`, `PC16`, `8`, `PC8`, `PC64`, and the `GOTPCREL` /
  `GOTPCRELX` / `REX_GOTPCRELX` family via a synthesised `.got`. Every field is
  range-checked, so an out-of-range reference reports a relocation overflow
  instead of silently truncating.
* **Linker-defined symbols**: `__executable_start`, `_etext`/`__etext`,
  `__bss_start`, `_edata`, `_end`/`end` — provided only when referenced and not
  otherwise defined, like a linker script's `PROVIDE()`.
* **Writer**: `ET_EXEC` with program headers, plus a `.symtab`/`.strtab`/
  `.shstrtab` and section header table. The symbol table is not needed to run
  the program, but keeping it means `objdump -d`, `nm` and `readelf` work on
  rlink's output — which makes debugging the linker itself far easier.
* **CLI**: `rlink.py -o OUT [-e ENTRY] [--base ADDR] [-L DIR] [-l NAME] INPUT…`
  — the subset of `ld`'s interface the ShivyCX driver uses.
* **Freestanding runtime** (`rcrt.s`): `_start` plus `exit`, `write`, `read`,
  `puts`, `putchar`, `putint`, `strlen`, `memset`, `memcpy`, `sbrk`, `malloc`,
  written in Intel-syntax assembly and talking to the kernel through `syscall`.
  Assembled by rasm, so a linked program needs no libc at all.
* **Tests** (`rlink_test.py`): **16/16 passing** — 10 C programs through the
  full ShivyCX → rasm → rlink pipeline with exit code *and* stdout compared
  against a gcc-built reference (arithmetic, recursion, globals/bss, strings,
  heap, branch-heavy loops, structs/pointers, bitops, function pointers), plus
  archive member selection, linking gcc-produced objects, and `readelf`/
  `objdump` validity checks on the emitted image.

## Other architectures

rlink links AArch64 and RV64 objects as well. Most of it needed no change: the
ELF reader, archive handling, symbol resolution, layout and the executable
writer were already architecture-neutral. What was hardcoded to x86-64 was the
`e_machine`/`e_flags` header words and the relocation application, both now
behind the `rasm_arch` seam shared with the assembler.

All inputs must agree on one architecture; a mismatch is reported once, rather
than as a storm of unknown-relocation errors later.

### Relocations

Unlike x86-64, where a relocation overwrites a whole field, these splice a
bit-slice into an instruction word that already holds opcode and register bits
— so the appliers read the existing word, mask in the new immediate, and write
it back.

* **AArch64**: `CALL26`/`JUMP26`, `CONDBR19`, `TSTBR14`,
  `ADR_PREL_PG_HI21`, the `ADD`/`LDST` `_NC` LO12 family, all four
  `MOVW_UABS` slices, and the `ABS`/`PREL` data types.
* **RV64**: `CALL`/`CALL_PLT`, `BRANCH`, `JAL`, `PCREL_HI20`,
  `PCREL_LO12_I`/`_S`, `HI20`, `LO12_I`/`_S`, the data types, and `RELAX`.

Three details are easy to get wrong and each is load-bearing:

* **`adrp` truncates both ends.** It computes the delta between the 4KB *page*
  holding the instruction and the page holding the symbol, so both addresses
  are truncated to their page base first. Forgetting to truncate the PC is
  invisible whenever the two happen to sit at the same offset within their
  pages.
* **RISC-V's `auipc`+`jalr` pair borrows.** `jalr` sign-extends its 12-bit
  half, so when the low half is ≥ `0x800` the high half must be incremented to
  compensate. Dropping that borrow is off-by-4096 on half of all addresses.
* **`PCREL_LO12` names the wrong-looking symbol.** Its symbol is the label on
  the `auipc`, not the target, so its own PC is the wrong base; the value has
  to be recomputed against that instruction's address by finding the paired
  `PCREL_HI20`.

`R_RISCV_RELAX` is deliberately ignored. It only ever *permits* shortening a
sequence, so not relaxing is always correct — but it means our RV64 output is
not byte-identical to `ld`'s, which does relax.

### Small data and `__global_pointer$`

`.sdata`/`.sbss` fold into `.data`/`.bss`, and `__global_pointer$` is provided
for RV64 as `.data + 0x800`. That is a real ABI requirement, not a nicety: crt
code loads `gp` from it before any small-data access, and GNU `ld` will relax
PC-relative sequences into gp-relative ones that fault without it.

## Linker scripts

`-T script.ld` drives the layout instead of the built-in default. This is what
makes a bare-metal image possible at all: such an image *is* its script — the
load address, which section leads, alignment constraints, and the symbols the
boot code resolves against.

Supported, and differentially tested against `ld` in `rlink_script_test.py`
(**11 pass, 0 fail**):

* `ENTRY(sym)`, `OUTPUT_FORMAT`/`OUTPUT_ARCH` (parsed and ignored)
* `SECTIONS { ... }` with output sections `NAME [ALIGN(n)] : { *(pat) ... }`,
  wildcard patterns, `KEEP(...)`, `COMMON`, and `/DISCARD/`
* the location counter: `. = <expr>;`, including `. = ALIGN(n);` and
  `. = . + n;` for reserving a region such as a stack or page tables
* symbol assignment `sym = <expr>;` and `PROVIDE(sym = <expr>)`
* `. = ALIGN(n);` at the end of a section body
* expressions over `+ - * /`, parentheses, numbers with `K`/`M` suffixes,
  `ALIGN(x)` / `ALIGN(addr, align)`, `.`, and previously defined script symbols

Expressions are stored as token lists at parse time and evaluated during
layout, because most of them depend on where `.` has reached — a value that
does not exist until sections are being placed.

Two details are load-bearing:

* **File offsets are derived from addresses, not accumulated.** A script may
  leave large gaps (`. = . + 0x10000` to reserve a stack), and those cost
  address space but no file bytes. Accumulating an offset across a gap pads the
  output with zeros up to the next section, since the writer fills the file to
  each section's `file_off`. Deriving `file_off = first_file_off + (addr -
  first_addr)` keeps `p_offset` congruent to `p_vaddr` modulo the page size —
  which the kernel requires — while leaving gaps free. On the AArch64
  bare-metal kernel this is 16 KiB of output against `ld`'s 83 KiB, for images
  that behave identically.
* **A section a script names is placed whether or not it has `SHF_ALLOC`.**
  `.section .multiboot` in gas carries no flags unless the source spells them
  out, and `boot64.S` does not. Requiring `SHF_ALLOC` silently dropped the
  12-byte Multiboot header, putting `_start` at the image's first byte and
  producing an image GRUB would refuse for having no header in its first 8 KiB.
  Nothing about that failure looks like a linker bug from the outside. Only
  linker bookkeeping (`.rela*`, symtab, strtab) is never placeable.

Not supported: `MEMORY` regions, `AT(...)` load addresses distinct from virtual
ones, `SORT`, section fill patterns, `OVERLAY`, and conditional or
`MAX`/`MIN`/`ADDR`/`SIZEOF` expressions. Each raises rather than being ignored,
so a script using them fails visibly instead of laying out something subtly
wrong.

### Testing

`rlink_cross_test.py` assembles with the GNU cross assembler, links the *same*
object with both `ld` and rlink, runs both under qemu and compares exit codes:
**46 pass, 0 fail**. Using `ld` as the oracle is what makes this meaningful — a
relocation applied with the wrong shift usually still produces a runnable
binary, just one that jumps somewhere wrong.

The corpus is organised by relocation type rather than by language feature, and
its shape came from mutation testing. Hand-written programs missed the two
subtlest faults entirely: a dropped `adrp` page-truncation and a dropped RISC-V
sign-extension borrow both survived, because each only bites at particular
displacements. The generated sweeps walk the displacement across a page — and
padding `.text` and `.data` together turned out to move both ends in step and
never reach the borrow zone, so the pcrel sweep pads only `.data`.

## Integration

`shivyc/main.py:link_objs` uses rlink when `SHIVYC_RLINK` is set, and GNU `ld`
otherwise (default unchanged). Because rlink produces a freestanding static
binary, it links `tools/rpy_lib/rcrt.s` in automatically as the runtime; set
`SHIVYC_NO_RCRT=1` to supply your own `_start`, or `SHIVYC_ENTRY=name` to change
the entry symbol.

## Not yet done

* **Dynamic linking**: no `.dynamic`, `.dynsym`, PLT/GOT-for-shared-libraries,
  or `ET_DYN` output. rlink links statically only. Linking against glibc's
  `libc.a` additionally needs `IRELATIVE`/IFUNC resolution and init-array
  handling, neither of which is implemented; the freestanding `rcrt.s` runtime
  is the supported path.
* **TLS**: thread-local sections are rejected with a clear error rather than
  mis-linked.
* **`.eh_frame` / `.init_array` semantics**: these sections are laid out and
  relocated correctly, but nothing calls the init/fini arrays, and there is no
  `PT_GNU_EH_FRAME` header, so C++-style unwinding would not work.
* **Section GC and identical-code folding** (`--gc-sections`, `--icf`): every
  allocated input section is kept.
* **RISC-V relaxation** (`R_RISCV_RELAX`): the hint is ignored, which is always
  correct but leaves our RV64 output larger than `ld`'s and not byte-identical
  to it.
* **RPython translation + minipy**: same remaining cleanup as rasm — drop the
  few remaining tuple-unpacking sites, make dict iteration order-independent,
  add type annotations, translate, then run under minipy for full self-hosting.

## Files

* `tools/rpy_lib/rlink.py` — reader, symbol resolution, layout, relocation,
  ELF executable writer, CLI.
* `tools/rpy_lib/rasm_arch.py` — the architecture seam shared with the
  assembler: `e_machine`/`e_flags`, relocation numbering, data widths.
* `tools/rpy_lib/rcrt.s`, `rcrt_arm64.s`, `rcrt_riscv64.s` — freestanding
  `_start` + minimal libc (syscalls), one per architecture.
* `tools/rpy_lib/rlink_test.py` — end-to-end pipeline, archive and interop tests.
* `tools/rpy_lib/rlink_cross_test.py` — AArch64/RV64 relocation differential
  test against GNU `ld`.
* `tools/rlink_script_test.py` — linker-script layout differential test against
  GNU `ld`, plus the AArch64 bare-metal image linked both ways and booted.
