# macOS on Apple Silicon (`--os macos`)

Crust builds native executables for macOS on Apple Silicon. The architecture
is the existing arm64 back end; what is new is the operating system, selected
by `--os`, which changes the object format (Mach-O instead of ELF), how
symbols are spelled, how addresses are formed, the variadic calling
convention, and how the program is assembled and linked. Intel Macs are out
of scope.

```sh
./crust hello.c -o hello && ./hello              # on an Apple Silicon Mac
python3 -m shivyc.main --target arm64 --os macos -S hello.c   # from Linux
```

## Status

Code generation, assembly and linking are implemented and verified *off* the
Mac: the output is assembled by LLVM's Mach-O assembler and linked by
`ld64.lld` into a PIE arm64 executable, and its symbol table, relocations,
bind/rebase tables and ABI-critical instruction sequences have been checked
by hand and are pinned by tests. **It has not yet been run on a Mac.** The
first thing to do there:

```sh
python3 -m unittest tests.test_macos_target -v
```

The `MacOSNativeRunTests` build with the system toolchain and run the
result; they are skipped everywhere else.

## Headers

Crust does not read the system's `/usr/include`. An angle-bracket include
resolves to Crust's own small headers in `shivyc/include/` unless `-I` names
another directory (on Linux, `--musl` supplies musl's). So `#include
<stdio.h>` works on a Mac without the SDK being involved, and the bundled
headers carry the few facts that differ there, under `__APPLE__`:

* **Standard streams.** libSystem exports them as `__stdinp`, `__stdoutp`
  and `__stderrp` (the SDK maps the names with macros). A plain `extern
  stdout` would be an undefined `_stdout` at link time. As external data
  they are reached through the GOT.
* **`isnan`, `isinf`, `isfinite`, `signbit`.** C99 macros, which Apple's
  header expands to `static inline` helpers that libSystem does not export.
  The bundled `<math.h>` provides its own single-evaluation helpers.

`long double` is `double` in Apple's arm64 ABI (8 bytes, `d` registers), so
under `--os macos` it is accepted and mapped to `double` silently. On ELF
targets it is still rejected unless `-f-long-double-as-double` is given,
because there it really is a wider type.

Headers beyond the bundled set (`<unistd.h>`, `<sys/stat.h>`, `<pthread.h>`
...) mean the SDK's own, via `-I "$(xcrun --show-sdk-path)/usr/include"`.
Those are written for clang; see the first known gap below.

## Selecting the OS

`--os` is orthogonal to `--target`, and takes `linux`, `macos` or `none`
(aliases: `darwin`/`osx` for macos, `baremetal`/`freestanding`/`elf` for
none). `none` is freestanding ELF, and produces the same output as `linux`.

The default behaves like a native compiler: **when the target architecture
is the host's, the OS is the host's**. So a bare `crust hello.c` on an
Apple Silicon Mac builds a Mac executable, while a cross build (e.g.
`--target riscv64` on a Mac) stays ELF. An explicit `--os` always wins, and
the in-repo tools that mean ELF say so: the bare-metal kernel and thread
switcher flows pass `--os none`, and the Raspberry Pi / Jetson board tools
and the qemu-based difftests pass `--os linux`, so they behave identically
on a Linux or a Mac host. `default_target()` returns arm64 on any Mac, since
a Python launched under Rosetta 2 reports `x86_64`.

The target's architecture name stays `"arm64"` under `--os macos`. That is
deliberate: the register pools and `thread_contracts.py` key on the
architecture string, and are correct for both OSes.

These are refused with the reason rather than miscompiled: `--target`
anything but arm64 with `--os macos`; `--musl` (a Linux libc, whose struct
layouts are not libSystem's); `-f-low-mem` (arm64 macOS requires PIE, so there
is no fixed low load address); and `SHIVYC_RASM` / `SHIVYC_RLINK` (they
produce ELF).

## What changes, and where

**Target seam** (`shivyc/targets/__init__.py`). `Target` gains `os`,
`obj_format` (`"elf"` / `"macho"`) and `sym_prefix`; `get_target(name,
os_name)` applies the OS. Back ends branch on `obj_format` or `os`, never on
the host.

**Symbol spelling** (`shivyc/spots.py`). Every definition and reference
already went through `mangle_symbol`, so that is the one place Mach-O's `_`
prefix is applied. Compiler-generated jump labels get the assembler-local
`L` prefix instead, which keeps them out of the object's symbol table. The
format is set beside each `ASMCode` construction in `main.py`, so it cannot
disagree with the unit being emitted.

**Sections and data** (`ASMCode` in `shivyc/asm_gen.py`). `__TEXT,__text`
and `__DATA,__data`; `.build_version macos, 11, 0`; `.comm name,size,align`
and `.lcomm` for file-local tentative definitions (Mach-O has no `.local`);
no `.note.GNU-stack`. Data labels get a natural-alignment `.p2align`: under
chained fixups (the default since macOS 12) a pointer in `__data` must be
8-byte aligned, and an odd-length string would otherwise misalign the next
one.

**Addresses** (`_arm64_adr`). The ten hand-written `adrp` / `:lo12:` pairs
are one helper. ELF output is unchanged. On Mach-O, a symbol defined in this
unit uses `@PAGE` / `@PAGEOFF`; one defined elsewhere uses the GOT
(`@GOTPAGE` / `@GOTPAGEOFF`), because it may resolve into a dylib — libSystem
data such as `environ`, or the address of a libc function passed as a
callback — which cannot be reached PC-relatively. ld64 relaxes the GOT load
back to a direct address when the symbol turns out to be local.

**Variadic calls.** This is the ABI difference that breaks the first
`printf`. In standard AAPCS64 anonymous arguments use registers like named
ones. Apple's arm64 ABI passes every *anonymous* argument on the stack, in
8-byte slots starting exactly at `[sp]`, and libSystem reads them only from
there. Crust's existing ELF convention wrote *every* argument (named ones
too) into a block at `[sp]` and passed its base in `x16`, so under Apple's
reading `printf("%d", 42)` would print its format pointer.

Under `--os macos` the caller writes only the anonymous arguments at `[sp]`.
The callee needs nothing from the caller: its `sp` on entry *is* the first
anonymous argument, so a variadic function captures `mov x16, sp` before
its prologue (it may be a frameless leaf, so this cannot be recovered from
`x29` later), and `va_start` is that base plus 0. Because nothing depends on
the caller setting `x16`, clang-compiled C can call a Crust variadic
function too. Crust's `va_list` is already a `char *` stepping through
8-byte slots, which is exactly Apple's `va_list`.

**Driver** (`shivyc/main.py`). Assemble with `as -arch arm64`. Link with
`cc -arch arm64 -mmacosx-version-min=11.0`: the ELF path hand-assembles the
`ld` command from crt objects and the ELF dynamic loader, none of which
exist on macOS, where the compiler driver supplies startup code, the SDK
sysroot and libSystem. `-rdynamic` maps to `-Wl,-export_dynamic`; the
writable-text mode maps to `-segprot __TEXT rwx rwx`.

**Preprocessor.** `__APPLE__`, `__MACH__`, `__aarch64__`, `__arm64__`,
`__LP64__`, `_LP64`, `__LITTLE_ENDIAN__`, and the deployment target as the
SDK's Availability headers expect it (`110000`).

### Stack arguments and sub-int values

Three more places Apple departs from AAPCS64, each verified by executing
Crust code against clang's (see "Testing"):

* **Stack arguments are packed.** Past the eighth of their class, AAPCS64
  gives every argument an 8-byte slot; Apple places each at the next offset
  aligned to its own size, taking only that size -- an `int`, `char`,
  `short`, `int`, `long` land at `[sp]`, `+4`, `+6`, `+8`, `+16`. Caller and
  callee both follow it, with sized stores and loads (`strb`/`ldrsb` ...).
  Variadic calls are unaffected: anonymous arguments keep 8-byte slots.
* **The caller extends sub-int arguments** to 32 bits in their register;
  clang-built callees use the register as-is.
* **The callee extends sub-int return values** to 32 bits; clang-built
  callers rely on it (clang emits `sxtb` / `and #0xffff` before `ret`).

### ABI facts already satisfied

Apple's arm64 ABI also reserves `x18` (the platform register) and makes
plain `char` signed. The back end never allocates `x18` (value homes are
`x19`–`x28`, scratch `x9`–`x17`), and `char` has always been signed in
Crust. Signed `char` happens to be what Apple uses; it is Linux AArch64 that
differs.

## A fix for ELF found on the way

The data emitter spelled 2-byte data `.word`. That is 2 bytes only to the
x86 assembler; to the AArch64 and RISC-V assemblers it is **4**, in ELF and
Mach-O alike, so on those targets every `short` static initializer and every
16-bit wide string was laid out wrong. It is now `.short`, which is 2 bytes
everywhere. x86-64 output assembles to identical bytes. `tools/arm64_difftest.py`
gained two programs that fail without the fix.

## Known gaps

In rough order of when they will matter:

1. **SDK headers via `-I`.** 29 common headers parse against the stand-in
   (see "Developing against Apple's headers"), but none has been tried
   against the real SDK, whose headers may differ in detail. If a
   header includes `TargetConditionals.h` it will stop with "unknown
   compiler", since Crust defines neither `__clang__` nor `__GNUC__`; the
   likely fix is for Crust to predefine the `TARGET_OS_*` values itself
   rather than claim to be GCC.
2. **Variadic calls with more than eight named arguments of one class**
   are refused explicitly (the named overflow would need packing ahead of
   the anonymous block). Ordinary calls with any number of arguments work.
3. **Self-hosted host detection.** The self-hosted build cannot lower
   `platform.system()`, so there `host_os()` reports `linux`; a self-hosted
   compiler on a Mac needs an explicit `--os macos`.
4. **Unverified paths.** `.weak_definition` on `.set` aliases (weak aliases)
   and the `-segprot` writable-text link have not been exercised on Mach-O.

Struct-by-value parameters are not implemented on arm64 for any OS, so they
are not a macOS gap.

## Developing against Apple's headers

The macOS SDK is licensed for use on Apple hardware, so it cannot be used
on a Linux machine. Apple does publish the *sources* of its headers under
the APSL at github.com/apple-oss-distributions, and
`tools/macos_proxy_sdk.sh OUTDIR` assembles a stand-in for
`$SDK/usr/include` from them (Libc, xnu, libpthread, libmalloc, libplatform,
CarbonHeaders, AvailabilityVersions):

```sh
tools/macos_proxy_sdk.sh /tmp/crust-macos-sdk
python3 -m shivyc.main --target arm64 --os macos -S \
    -I /tmp/crust-macos-sdk/usr/include prog.c
```

The SDK's headers are not the raw sources, so the script replicates the
install steps that change what a compiler sees: Apple's own generators for
the Availability headers, `sys/_symbol_aliasing.h`,
`sys/_posix_availability.h` and the Libc feature flags; Libc's install-time
stripping of `//Begin-Libc` blocks and its `unifdef` pass; and leaving out
the headers Libc marks "Not to be installed". That last one matters: Libc's
own `sys/cdefs.h` is a build wrapper around xnu's, reached with
`#include_next`, and it runs Libc's internal feature verification.

Status against the stand-in: 29 common headers preprocess and parse, all
together in one translation unit, in about 4 s: `stdio.h`, `stdlib.h`,
`string.h`, `strings.h`, `ctype.h`, `errno.h`, `unistd.h`, `limits.h`,
`stdint.h`, `inttypes.h`, `fcntl.h`, `time.h`, `signal.h`, `sys/types.h`,
`sys/stat.h`, `sys/time.h`, `sys/wait.h`, `dirent.h`, `setjmp.h`,
`locale.h`, `assert.h`, `termios.h`, `sys/mman.h`, `wchar.h`,
`sys/socket.h`, `sched.h`, `pthread.h`, `netinet/in.h`, `sys/uio.h`.
Struct layouts come out right from Apple's own definitions: `sizeof(struct
stat)` is 144, the arm64 64-bit-inode layout. (`netdb.h` belongs to libinfo,
which the stand-in does not include.)

The bundled `<stdarg.h>` is what the SDK's headers get for `<stdarg.h>`
(the SDK ships none; it is the compiler's). Under `__APPLE__` it spells
`va_list` as the SDK does -- `void *`, via `__darwin_va_list`, for a compiler
that is neither GCC nor clang -- and honours the SDK's `_VA_LIST_T` guard.
Both spellings are the same 8-byte pointer, which is Apple's arm64 va_list.

Getting there fixed six general preprocessor and lexer issues, none of
them macOS-specific:

* **Feature-test operators.** `__has_include` / `__has_include_next`
  (searching the include path) and `__has_feature`, `__has_attribute`,
  `__has_builtin` and the rest (answering "no") were parse errors in `#if`.
* **`#include_next` was silently dropped** as an unknown directive, so the
  header it named was never read.
* **`__STDC__` was not predefined**, though the standard requires it, so the
  SDK's `<sys/cdefs.h>` took its pre-ANSI path. `__STDC_VERSION__` stays
  undefined for now, which keeps headers on their most conservative paths.
* **`defined` produced by macro expansion** in `#if` was left as a bare
  identifier. C leaves it undefined, but GCC and clang evaluate it, and the
  SDK's `pthread.h` relies on it; it is now resolved after expansion, with
  its operand passed through unexpanded, as GCC does.
* **Lexing was slow on large headers**: 80% of a `<stdio.h>` compile went
  to trying every punctuator at every character. A first-character check
  made it 4.5x faster; token streams over 1047 files (605,001 tokens,
  including positions and diagnostics) are identical before and after.
* **Include-guarded headers were re-lexed on every inclusion.** The
  multiple-include optimization (as GCC's) now skips a header wholly wrapped
  in `#ifndef G` once `G` is defined. The 29 headers above went from 17 s
  to 4 s with byte-identical output.

## Testing

`tests/test_macos_target.py` runs in three tiers. Assembly-text checks pin
the dialect, the ABI sequences and the bundled headers' macOS branches, and
need nothing but Python. With an AArch64 cross-gcc and qemu-user, the
`<math.h>` helpers are also executed (an AArch64 Linux build with
`__APPLE__` forced on runs the same code). With LLVM
installed (`apt install clang lld llvm`), the output is also assembled as
real Mach-O and linked with `ld64.lld` against a generated text-based stub of
libSystem listing exactly the symbols the object imports; no SDK is needed.
On an Apple Silicon Mac, the programs are built with the system toolchain
and run.

The strongest tier needs no Mac at all. `tools/macho_run.py` runs a
self-contained arm64 Mach-O executable's `main` under the Unicorn CPU
emulator (`pip install unicorn`): code that makes no system calls needs no
Darwin kernel. `AppleABIExecutionTests` builds `tests/macos_abi/` with both
Crust and clang (`-target arm64-apple-macos11`, the ABI's reference
implementation) and runs every caller/callee pairing -- Crust calling
clang, clang calling Crust, Crust calling Crust, and clang calling clang as
a control -- over 17 checks covering stack-argument packing, sub-int
argument and return extension (with runtime-computed values, which constants
would hide), FP stack arguments, function pointers and variadic calls.

This found the three ABI rules above, and a bug that was not macOS-specific
at all: the arm64 back end did not implement integer conversions for
`char`/`short`/`_Bool` values held in registers, on Linux too
(`tests/test_arm64_conversions.py`: 795 of 1539 conversions differed from
gcc before the fix).

`make test_macos` runs the file on its own.
