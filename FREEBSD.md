# FreeBSD/amd64 (`--os freebsd`)

Crust builds native FreeBSD executables for amd64, linked against FreeBSD's
own libc, so the whole C library is available.

```sh
python3 crust hello.c -o hello && ./hello          # on FreeBSD
python3 -m shivyc.main --os freebsd -c hello.c     # from Linux: an object
CRUST_FREEBSD_SYSROOT=/path/to/root \
  python3 -m shivyc.main --os freebsd hello.c -o hello    # from Linux: linked
```

## Status

Verified against a real FreeBSD 15.1-RELEASE amd64 system running under QEMU.
On that system, programs compiled by Crust and linked by FreeBSD's `cc`
produce output byte-identical to FreeBSD's own clang on the same source. That
covers stdio (through FreeBSD's `__stdoutp`), `qsort` callbacks, `snprintf`,
libm, `setjmp`/`longjmp` and `ctype`, and the same holds for binaries built
entirely on Linux. Every feature test matches FreeBSD's clang except for three
explained below. **Crust itself has not yet run on FreeBSD**: the test
machine has no Python, and packages could not be installed. That tier of the
tests is written and waiting (see "Testing").

## What is different, and what is not

FreeBSD/amd64 uses the same System V calling convention, the same LP64 data
model and the same ELF object format as Linux. **No code generation changes.**
The target is `x86_64` with `os = "freebsd"`; `abi`, `obj_format` and
`exe_format` are unchanged.

What differs is everything around the code, and each difference was checked
on the real system:

* **The toolchain.** FreeBSD's base system has no `as` at all. `cc` is clang
  (19.1 on 15.1) and `ld` is LLD. On a FreeBSD host Crust therefore assembles
  with `cc -c` and links with `cc`, which knows the startup objects
  (`crt1.o`, `crti.o`, `crtbegin.o`...), the dynamic loader
  (`/libexec/ld-elf.so.1`), `libc.so.7` and `libgcc`. The Linux path builds
  its `ld` line by hand from glibc's layout; none of those files exist here.
* **Branding.** The kernel identifies an executable's ABI from its ELF
  header, and refuses one it cannot identify (`kern.elf64.fallback_brand` is
  -1): an unbranded binary gets `Exec format error`. FreeBSD's linker writes
  the brand (`EI_OSABI = ELFOSABI_FREEBSD`), which is one reason the link
  goes through FreeBSD's toolchain. For the record: the OS/ABI byte alone is
  sufficient. Setting it by hand on an rlink image made it run.
* **libc.** The standard streams are `__stdinp`/`__stdoutp`/`__stderrp`,
  as on macOS, so the bundled `<stdio.h>` takes its BSD branch. `errno` is
  `(*__error())`. libc exports `isnan` and `isinf`, but `isfinite` and
  `signbit` only as `__isfinite`/`__signbit`, so the bundled `<math.h>` uses
  its library-free helpers, as on macOS and Windows. `environ` comes from
  `crt1.o`.
* **Process entry** (relevant only to the self-contained mode below). FreeBSD
  passes the argument block in `rdi`, where Linux has it at `[rsp]`:
  FreeBSD's own `crt1.o` reads `argc` from `[rdi]`.
* **Predefined macros.** These are what FreeBSD's cc predefines, per
  `cc -dM -E`: `__FreeBSD__`, `__unix__`/`__unix`, `__ELF__`,
  `__LP64__`/`_LP64`, and `__x86_64__`/`__amd64__` with their
  single-underscore-suffix forms. `__FreeBSD__` is the major release: the
  host's own on FreeBSD, otherwise 14 (the oldest series still supported).
  `-D__FreeBSD__=N` overrides it.

## The assembler dialect

Crust's assembly had been written against GNU `as`, which accepts three
spellings that LLVM's assembler (FreeBSD's only one) rejects. Every file
failed until they were changed:

| was | now | why |
|---|---|---|
| `.att_syntax noprefix` (end of every file) | `.att_syntax prefix` | AT&T syntax without `%` register prefixes is a GNU extension |
| `.comm name 4` | `.comm name, 4` | LLVM requires the comma |
| `movsx rbx, ebx` | `movsxd rbx, ebx` | Intel's name for the 32-to-64-bit extension; GNU accepts the alias |

These changes are in the shared back end, so they affect Linux too, and they
were checked to be **byte-neutral**. For every feature test and several larger
programs (59 files), the new assembly and the same assembly with the three
spellings reverted assemble to identical objects under GNU `as`, and again
under rasm. rasm's `.comm` parsing was widened on the way: it now accepts
`name size`, `name, size` and the tight `name,size`, which the Mach-O path
writes.

## Cross-compiling from another host

`-S` and `-c` work anywhere. An object is an ordinary x86-64 ELF relocatable
file, which FreeBSD's `cc` links. A full link needs FreeBSD's libraries, so
from another host Crust links only when `CRUST_FREEBSD_SYSROOT` names a copy
of a FreeBSD root. It then runs `clang --target=x86_64-unknown-freebsd14
--sysroot=... -fuse-ld=lld`, the same LLVM toolchain FreeBSD uses, pointed at
its files. Without a sysroot it stops with an error that says what is needed.

A sysroot needs only the files the link reads, about 1.3 MB:

```
lib/libc.so.7  lib/libm.so.5  lib/libgcc_s.so.1
usr/lib/{crt1,crti,crtn,crtbegin,crtend}.o  usr/lib/libc.so  usr/lib/libm.so
usr/lib/libc_nonshared.a  usr/lib/libgcc.a  usr/lib/libgcc_s.so
```

(`usr/lib/libc.so` is a linker script naming `/lib/libc.so.7`; lld resolves
it inside the sysroot.)

## Refused options

`--musl` is refused (a Linux libc), and so is `SHIVYC_RLINK`, whose runtime
makes Linux system calls. Other options take the ordinary ELF paths.

## Not done yet

1. **Crust running on FreeBSD** is unverified. Host detection
   (`platform.system() == "FreeBSD"`), assembling with `cc -c` and linking
   with `cc` are implemented, and tier 5 of the tests exercises them the
   moment the FreeBSD machine has `python3`.
2. **Self-contained static mode.** rasm + rlink with no FreeBSD files at all.
   It is deferred, but its three facts are established above: the OS/ABI
   brand, entry via `rdi`, and FreeBSD's syscall numbers (`exit` 1, `write` 4
   and so on), with errors reported in the carry flag and a positive errno in
   rax rather than as a negative return (verified: a failing `write` returns
   9, `EBADF`, with carry set). It needs an `rcrt_freebsd.s` and a brand in rlink.
3. **arm64 and riscv64.** FreeBSD supports both, and the arm64 and riscv64
   back ends exist; the driver path would be the same.
4. **`-O4` / `-fmetamorphic`** (writable text) have not been tried against
   lld on FreeBSD.

## Feature tests that are not a comparison

Three feature tests give no meaningful comparison with FreeBSD's clang, each
for a reason in the test itself:

* `storage` declares glibc's `extern void *stdout;` by hand. FreeBSD's libc
  has no such symbol, so it fails to link with any compiler (also true on
  Windows).
* `pointer_math` compares the addresses of separate locals, which depends on
  stack layout. clang's answer changes between `-O0` and `-O1`.
* `include` calls `strcpy` into a string literal. clang puts literals in
  read-only memory and faults; Crust puts them in writable data.

## Testing

`tests/test_freebsd_target.py`, five tiers; `make test_freebsd` runs it on
its own.

1. **Pure Python:** OS selection, macros, the headers' FreeBSD branches, the
   three dialect spellings, and driver diagnostics.
2. **clang installed:** every feature test's output assembles with
   `clang --target=x86_64-unknown-freebsd`, which is what FreeBSD's `cc`
   runs. This guards the dialect with no FreeBSD machine. Mutation-tested: each
   of the three spellings reverted is caught here and in tier 1.
3. **`CRUST_FREEBSD_SYSROOT`:** a full cross build, checked for the FreeBSD
   brand and the FreeBSD dynamic loader.
4. **`CRUST_FREEBSD_SSH=user@host`** (with `CRUST_FREEBSD_SSH_PORT` and
   `CRUST_FREEBSD_SSH_PASSWORD` if needed): programs run on FreeBSD. A
   runtime program's output must match FreeBSD's cc byte for byte, and every
   feature test is compared with FreeBSD's cc, linked with the same command
   the driver uses on a FreeBSD host. A cross-linked binary is run there
   too.
5. **That host has `python3`:** Crust itself runs there, with no `--os`.

A FreeBSD machine for tiers 4 and 5 can be a VM. The images published by
`cross-platform-actions/freebsd-builder` (GitHub releases) boot under
`qemu-system-x86_64` without KVM, with the root password `runner` and SSH
enabled. Give the VM no more than half the host's memory, and shut it down
with `shutdown -p now`: during this work a VM that died uncleanly came back
with a corrupted `/tmp`.
