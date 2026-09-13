# 64-bit Windows (`--os windows`)

Crust builds native 64-bit Windows console programs with its own compiler,
assembler and linker. Nothing else is involved: no MinGW, no MSVC, no
Windows SDK. On a Windows machine the only prerequisite is Python.

```bat
REM on Windows, where --os windows is implied
python crust hello.c -o hello.exe
hello.exe
```

```sh
python3 -m shivyc.main --os windows hello.c -o hello.exe    # from Linux
```

The result links against `msvcrt.dll` and `kernel32.dll`, which every
Windows since XP ships, so it also runs on a machine with nothing installed.
A hello world is 2 KiB.

## Status

Everything below has been verified off Windows: under Wine 9.0, with
MinGW-w64 gcc as the reference implementation of the ABI, and with Crust
itself running on a genuine Windows build of CPython 3.12 (under Wine). **It
has not yet been run on a real Windows machine.** The first thing to do
there:

```bat
set CRUST_WINDOWS_PYTHON=%CD%\python.exe
python -m unittest tests.test_windows_target -v
```

## How it fits together

The Linux x86-64 back end already had everything needed except the parts that
face the operating system, and those are what changed:

```
C  --shivyc-->  Intel-syntax asm  --rasm-->  ELF .o  --rlink-->  PE32+ .exe
                (Win64 ABI)                  (internal)          (rlink_pe.py)
```

**The objects stay ELF.** They only ever pass from rasm to rlink, both part of
Crust, so there is no reason to teach either a second object format. Only the
final image is a PE. Two consequences: `crust -c` on Windows produces an ELF
object, which Crust can link but MSVC and MinGW cannot; and C symbols are
spelled bare, as Win64 does anyway (only 32-bit Windows prefixes `_`), so the
assembler text is unchanged apart from the calling convention.

## Selecting the OS

`--os windows` (aliases `win64`, `win32`, `win`, `mingw`, `nt`) pairs with
`--target x86_64` only; Windows on Arm would need an arm64 PE and its own ABI
work. As with macOS, the default behaves like a native compiler: running on
Windows with the host's architecture targets Windows, so a plain `crust
hello.c` does the right thing there. The target carries `abi = "win64"` and
`exe_format = "pe"` while `obj_format` stays `"elf"` (see
`shivyc/targets/__init__.py`).

## The C library

A Windows program reaches the operating system through DLLs, not system
calls, so the Linux runtime's `syscall` wrappers have no counterpart. Instead:

* **msvcrt.dll is the C library.** It has almost everything: stdio, the
  allocator, strings, `qsort`, the math functions. rlink imports whatever a
  program leaves undefined, by name.
* **Startup** (`tools/rpy_lib/rcrt_win64.s`): `mainCRTStartup` calls
  `__getmainargs` for argc/argv/envp, then `main`, then `exit`, so atexit
  handlers run and stdio is flushed.
* **`setjmp`/`longjmp` are Crust's own**, in the same file. msvcrt's `longjmp`
  unwinds through every frame's SEH unwind data, which Crust does not emit, so
  it would terminate the process. Crust's version saves exactly what Win64
  obliges a callee to preserve, including xmm6-xmm15.
* **C99 gaps** (`tools/rpy_lib/rlibc_win64.c`): msvcrt predates C99 and has
  no `snprintf`/`vsnprintf`, only `_snprintf`, which does not terminate on
  truncation and returns -1. These are wrapped to C99 semantics. The file is
  compiled by Crust on first use, cached under `tools/rpy_lib/build/`, and
  linked only if something calls it. Crust's `va_list` is msvcrt's (a `char *`
  over 8-byte slots), so lists pass straight across.

### Imports

`tools/rpy_lib/win64_imports.py` lists what msvcrt.dll and kernel32.dll
export. It holds only names, never code: the names come from MinGW-w64's
import libraries (`libmsvcrt-os.a`, which is the DLL's exports without MinGW's
own emulations, and `libkernel32.a`). Whether each is a function or data comes
from checking Wine's `msvcrt.dll`: an export is data if its address is outside
every executable section. Both sources were needed. MinGW marks data by giving
it no code thunk, but it also withholds the thunk from functions it
reimplements (`sin`, `pow`, `atexit`, ...), so that marking alone
misclassifies them. To regenerate the file, install
`gcc-mingw-w64-x86-64` and `wine64` and rerun the extraction; the file's
docstring says exactly what was read.

A function import resolves to a 6-byte stub, `jmp QWORD PTR [rip+slot]`, so an
ordinary `call printf` needs no change in the compiler. A *data* export cannot
work that way, because a stub is code and cannot stand in for a variable. Such
an export is reached through its import slot, `__imp_NAME`. A direct
reference is a link error that says so:

```c
extern char ***__imp__environ;          /* not: extern char **_environ; */
char **env = *__imp__environ;
```

The bundled headers never need this: `stdout` is `&__iob_func()[1]`, a function
call, rather than msvcrt's `_iob` array.

**Linking against any DLL.** `-lNAME` looks for `NAME.dll` (then
`libNAME.dll`) in the `-L` directories and reads its export table directly, so
no import library or `.def` file is needed. The ABI tests link a MinGW-built
DLL this way. `-lm` and `-lc` are accepted and ignored, since both libraries
are msvcrt.dll.

## The image

`tools/rpy_lib/rlink_pe.py` adds a PE mode to rlink. Symbol resolution,
archive handling and relocation are rlink's own, unchanged. The new code
synthesises imports from undefined symbols, lays sections out under PE rules
(4 KiB RVA alignment, 512-byte file alignment) and writes the headers. The
sections are `.text` (with the stubs), `.rdata`, `.data`, `.idata` (the
import table) and `.bss`. The image is a console-subsystem PE32+ with NX
enabled, a 2 MiB stack reserve and a zero timestamp, so builds are
reproducible.

**Fixed base, no ASLR.** The x86-64 back end addresses globals with absolute
32-bit displacements (`R_X86_64_32S`), so the image must lie below 2 GiB. It
is linked at `0x400000` with relocations stripped and without
`DYNAMIC_BASE`, and Windows loads it exactly there. ASLR would first need
RIP-relative addressing in the back end, and then base relocations for the
remaining 64-bit absolute data; see the known gaps.

## The calling convention

Microsoft x64 differs from System V in ways that each miscompile silently.
Each one below is exercised by `tests/windows_abi/` in both directions.

* **Arguments are positional.** The first four go in rcx, rdx, r8, r9, or in
  xmm0-3 for a floating one; the two sequences share a single counter. So
  `f(int, double, int, double)` uses ecx, xmm1, r8d, xmm3.
* **Home space.** Every call reserves 32 bytes above the return address for
  the callee, even one with no arguments. Stack arguments therefore start at
  `[rsp+32]` at the call, or `[rbp+48]` in the callee.
* **rsi and rdi are callee-saved** (System V makes them scratch). xmm6-15 are
  too, but the back end never keeps a value above xmm2, so nothing needs
  saving there.
* **Structs** of exactly 1, 2, 4 or 8 bytes travel as integers. Any other size
  is passed as a pointer to a copy the *caller* makes (the callee may modify
  it), and returned through a hidden pointer that arrives as argument 0 and
  comes back in rax. Both rewrites happen in the tree (`call_exprs.py`,
  `_make_il_win64`), so the back end only ever sees values that fit a register.
* **Variadic calls** also copy every floating argument into the integer
  register of the same position. A variadic callee spills rcx, rdx, r8 and r9
  into its home space, and from then on the named and anonymous arguments form
  one run of 8-byte slots that `va_arg` can walk. That is the whole of the
  convention. It needs neither System V's vector count in al nor Crust's r11
  argument-block hand-off, so Crust-built and Microsoft-built variadic
  functions are interchangeable.
* **No red zone.** Windows may overwrite anything below rsp at any moment. A
  frameless leaf function parks scratch values in its home space instead
  (`[rsp+8]`..`[rsp+32]`), which gives it four slots.
* **Stack probing.** Windows commits a thread's stack lazily behind a single
  guard page, so a frame larger than 4 KiB that skipped straight past the guard
  would fault. Such prologues step down one page at a time and touch each page,
  as MSVC's `__chkstk` does.

Where the code lives: `spots.set_abi` rebuilds the register lists *in place*,
so that `ASMGen`, `Call` and `LoadArg`, which hold references to them, all
follow. The caller side is `Call._make_asm_win64` in `il_cmds/control.py`; the
callee side is `_make_il_win64` in `tree/general_nodes.py`; the variadic spill
is `VaSaveBase` in `il_cmds/value.py`. The System V paths are untouched.

## LLP64

On 64-bit Windows `long` is 4 bytes, while `long long` and pointers are 8.
Crust already used its 8-byte `longint` internally as the pointer-width
integer, and folded `long long` onto it, so only the spelled keyword `long`
changes. It maps to a distinct 4-byte type, kept distinct from `int` so that
`long *` and `int *` stay incompatible, as under MSVC. `L` literals follow
C11's rules (`1L` is 4 bytes, `3000000000L` is 8). `long double` is `double`,
as it is in Microsoft's ABI.

The predefined macros say so: `_WIN32`, `_WIN64`, `__x86_64__`, `_M_X64`,
`__LLP64__`, `__SIZEOF_LONG__ 4`, and `__SIZE_TYPE__` and friends spelled
`long long`. Neither `_MSC_VER` nor `__GNUC__` is claimed. The bundled headers
gain `_WIN64` branches for `size_t`, `ptrdiff_t`, `<stdint.h>`, msvcrt's 48-byte
`FILE`, `isnan`/`signbit` (msvcrt exports only `_isnan`), and `jmp_buf`. The
bit builtins were written for LP64: `__builtin_clzl` held its operand in an
`unsigned long` but tested bit 63, which never terminates when `long` is 4
bytes. On Windows the `l` forms are therefore 32-bit and the `ll` forms 64-bit,
matching MinGW.

Ten programs in `tests/feature_tests` assume LP64: they store 2^40 in a
`long`, or check `sizeof(long) == 8`. Under Windows they return what MinGW gcc
returns for the same source. `LP64_DEPENDENT` in the tests records those
values, and a tier-3 test re-derives each one from MinGW so the table cannot
drift.

## Refused options

These are rejected with a reason rather than miscompiled: `--musl` (a Linux
libc), `-f-pack-args` (built on the System V argument registers),
`-fmetamorphic` (it jumps into the callee without the home space the callee
owns), `-fsimd-pack-globals` (it reserves xmm15, which is callee-saved on
Win64), `-O4` (its near-function scratch needs writable text),
`-f-pointer-compression` (msvcrt's heap is not confined to the low 4 GiB),
`-rdynamic`, and `--thread-alloc-json` (its budgets name System V registers).

## Fixes found on the way

Several bugs surfaced during the port. Some were Windows-specific; others were
latent on Linux too.

* **Variadic `float` was never promoted to `double`** (C11 6.5.2.2p7), on any
  target. `printf("%f", 1.5f)` printed `0.000000`. Tests are in
  `tests/test_float.py`.
* **Callee-saved registers named by inline asm went unsaved.** The prologue
  decides what to save by scanning instruction operands, and inline asm is
  opaque text to that scan. So a clobber of `rbx` lost the caller's value on
  Linux too. Registers a command *declares* it clobbers are now saved as well,
  and under Win64 the inline-asm operand pool no longer includes rsi/rdi.
* **In-process linker errors were swallowed.** A missing symbol reported only
  "linker returned non-zero status". This affected the existing Linux
  `SHIVYC_RLINK` path too.
* **rasm did not parse `XMMWORD PTR`**; its differential test against GNU `as`
  caught this when `movups` was added.
* **Running on Windows at all** needed three changes. The compiler asked for a
  1 GiB thread stack, which Windows CPython refuses (its limit is exclusive at
  256 MiB). The preprocessor's path helper split only on `/`, so the bundled
  headers resolved only when Crust ran from its own checkout, and quoted
  includes beside a source file failed too. And the AST cache defaulted to
  `C:\tmp`; it now uses the per-user temp directory.

## Testing

`tests/test_windows_target.py` runs in four tiers; `make test_windows` runs it
on its own.

1. **Pure Python, anywhere.** Checks on the emitted assembly for each rule
   above, the LLP64 sizes and headers, and a real compile, assemble and link
   whose PE is parsed back by an independent reader in the test file. That
   reader checks headers, alignment, the import table, and that every stub
   jumps through an import slot. It also covers the link diagnostics. rasm and
   rlink are Python, so this tier needs nothing installed.
2. **Wine** (`apt install wine64`). Programs run: argv and exit status, msvcrt
   stdio, `qsort` callbacks, C99 `snprintf`, `setjmp`/`longjmp` across 50
   frames, a 70 KiB frame, the bit builtins, and every runnable program in
   `tests/feature_tests`.
3. **Wine and MinGW-w64 gcc** (`apt install gcc-mingw-w64-x86-64`), used as
   the ABI oracle; nothing of MinGW ends up in a Crust build. The
   `tests/windows_abi/` programs are built by both compilers and linked
   across a DLL boundary, and run in all four pairings: Crust calling Crust,
   Crust calling gcc, gcc calling Crust, and gcc calling gcc as the control.
   There are 22 checks: positional registers, stack arguments, float and
   sub-int values, every struct size class in both register and stack
   positions, variadic functions, a variadic function with a hidden result
   pointer, and callbacks. Direct calls go through the import stubs. An
   assembly probe fills every Win64 callee-saved register, calls a Crust
   function that is known to allocate rsi and rdi, and reports any register
   that came back changed.

   The suite was **mutation-tested**: eight plausible Win64 mistakes were
   planted in a copy of the compiler, and all eight are caught. One initially
   escaped, a caller that passes a struct without copying it. gcc had
   optimised away the callee's store to its dead parameter, so the write that
   should have exposed the missing copy never happened; the check now writes
   through a `volatile` pointer.
4. **Crust on Windows**, when `CRUST_WINDOWS_PYTHON` names a Windows
   `python.exe` (under Wine elsewhere). It runs `python -m shivyc.main hello.c`
   with no `--os`, from outside the checkout, with a fresh runtime build. This
   tier found the three host bugs above. A Windows CPython for Linux-side
   testing can be had from the python-build-standalone releases
   (`x86_64-pc-windows-msvc-install_only`).

Output on Linux is semantically unchanged: all 50 compilable feature tests
give the same assembly, once labels are renamed canonically and data blocks
are compared as a set. The comparison cannot be byte-for-byte because Crust's
output already varied with `PYTHONHASHSEED`, independently of this work.

## Known gaps

In rough order of when they will matter:

1. **Not yet run on real Windows.** Wine is a good stand-in but not the
   reference, and one difference is certain: Wine does not enforce guard
   pages, so the stack probe has been checked only structurally. Its encoding
   matches GNU `as`, and programs with 70 KiB frames run. An unprobed build
   also runs under Wine, so there Wine cannot show the probe is needed.
2. **No unwind data (.pdata/.xdata).** Programs run normally, but Windows
   cannot walk Crust frames. An access violation terminates the process
   without useful context, debuggers show no stack past a Crust frame, and SEH
   and C++ exceptions cannot pass through Crust code. This is also why
   msvcrt's `longjmp` cannot be used.
3. **No ASLR** (see "Fixed base, no ASLR"). The image works as it is, but
   mandatory-ASLR policies would reject it.
4. **msvcrt's C99 coverage.** Beyond `snprintf`, functions such as `round`,
   `trunc`, `log2`, `cbrt` and `fmin` are missing from msvcrt.dll, and calling
   one is a link error ("undefined reference"). Its `printf` is msvcrt's:
   `%e` prints three exponent digits (`1e+010`), and `%zu`, though it works
   under Wine, may not be recognised by older msvcrt.dll versions; `%llu` and
   `%I64u` are the portable spellings. `%lld` and `%I64d` work. A program that prints a 64-bit
   value with `%ld` gets 32 bits, because `long` is 4 bytes here.
5. **Console programs only.** There is no `-mwindows` (GUI subsystem), no
   `wmain`, and no resources or manifests.
6. **Objects interoperate only with Crust**, because they are ELF. Linking
   MSVC or MinGW objects in would need a COFF reader in rlink; linking
   their DLLs already works.
7. **Self-hosted host detection.** As with macOS, the self-hosted build cannot
   lower `platform.system()`, so there `host_os()` reports `linux`, and a
   self-hosted compiler on Windows needs an explicit `--os windows`.
8. **Inline asm** using the `S`/`D` (rsi/rdi) constraints now saves and
   restores them, verified in the emitted assembly but not yet executed.
