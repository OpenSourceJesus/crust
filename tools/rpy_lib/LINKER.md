# rlink — self-contained static ELF64 linker

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
* **Tests** (`rlink_test.py`): **14/14 passing** — 10 C programs through the
  full ShivyCX → rasm → rlink pipeline with exit code *and* stdout compared
  against a gcc-built reference (arithmetic, recursion, globals/bss, strings,
  heap, branch-heavy loops, structs/pointers, bitops, function pointers), plus
  archive member selection, linking gcc-produced objects, and `readelf`/
  `objdump` validity checks on the emitted image.

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
* **Linker scripts**: layout is fixed (a sensible default script's worth); only
  `--base` is adjustable.
* **RPython translation + minipy**: same remaining cleanup as rasm — drop the
  few remaining tuple-unpacking sites, make dict iteration order-independent,
  add type annotations, translate, then run under minipy for full self-hosting.

## Files

* `tools/rpy_lib/rlink.py` — reader, symbol resolution, layout, relocation,
  ELF executable writer, CLI.
* `tools/rpy_lib/rcrt.s` — freestanding `_start` + minimal libc (syscalls).
* `tools/rpy_lib/rlink_test.py` — end-to-end pipeline, archive and interop tests.
