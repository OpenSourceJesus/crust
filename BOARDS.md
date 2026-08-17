# Raspberry Pi and Jetson Nano

Both boards are **AArch64 Linux**, so ShivyCX targets them with the same
`arm64` back end documented in [ARM64.md](ARM64.md). There is no separate
"Raspberry Pi target" to select: a Pi 4 and a Jetson Nano differ in SoC and
peripherals, not in instruction set or ABI, and neither needs anything the
generic AArch64 Linux path does not already provide.

What *is* board-specific is the plumbing around the compiler — which
architecture it defaults to, which runtime it links, and whether it needs a
system toolchain at all. That is what this page covers.

## Which boards work

The requirement is a 64-bit ARMv8 CPU **running a 64-bit OS**. That second
condition is the one that catches people out: several Pi models are 64-bit
capable but ship a 32-bit userland by default.

| Board | SoC / core | Works? |
|---|---|---|
| Pi 5 | BCM2712, Cortex-A76 | yes, with 64-bit Pi OS |
| Pi 4 / 400 | BCM2711, Cortex-A72 | yes, with 64-bit Pi OS |
| Pi 3 / 3B+ | BCM2837, Cortex-A53 | yes, with 64-bit Pi OS |
| Pi Zero 2 W | BCM2710A1, Cortex-A53 | yes, with 64-bit Pi OS |
| Pi 2 B (v1.1) | BCM2836, Cortex-A7 | **no** — ARMv7, 32-bit only |
| Pi 1 / Zero / Zero W | BCM2835, ARM1176 | **no** — ARMv6, 32-bit only |
| Jetson Nano (4GB / 2GB) | Tegra X1, Cortex-A57 | yes — L4T is 64-bit |
| Jetson Xavier / Orin | Carmel / Cortex-A78AE | yes (same AArch64 path) |

There is **no 32-bit ARM back end**. On a 32-bit userland `uname -m` reports
`armv6l` or `armv7l`, and ShivyCX has nothing to offer; check with
`uname -m` before starting — it should print `aarch64`.

## Two ways to build

### Cross-compile from an x86-64 machine

```sh
python3 -m shivyc.main prog.c -S -o prog.s --target arm64
aarch64-linux-gnu-gcc -static prog.s -o prog     # or use our own linker, below
scp prog pi@raspberrypi.local:
```

### Natively, on the board

```sh
python3 -m shivyc.main prog.c -o prog            # --target defaults to aarch64
./prog
```

`--target` now defaults to whatever `uname -m` reports, so on a Pi or Jetson
this needs no flags. Previously it defaulted to `x86_64` unconditionally, and
running ShivyCX natively on a board produced x86-64 assembly that the local
assembler rejected.

## Building without a system toolchain

Both boards can build a complete static binary with **no gcc, no binutils and
no libc** — our own assembler, linker and runtime:

```sh
SHIVYC_RASM=1 SHIVYC_RLINK=1 python3 -m shivyc.main prog.c -o prog
./prog
```

That matters more on a board than on a workstation: a Jetson Nano image or a
minimal Pi OS Lite install may not have a compiler, and installing one costs
time and SD-card space. The result is a static `ET_EXEC` with no interpreter
and no dynamic section, which the kernel loads directly.

`tools/selfhosted_cli_test.py` exercises exactly this path — the command line,
not the internal APIs — across all three targets.

## What is verified, and what is not

Everything here is validated under **qemu-user** (`qemu-aarch64`), which
emulates the AArch64 user-mode ISA and the Linux syscall interface the boards
present. The generated binaries are ordinary static AArch64 Linux executables:
System V ABI, AAPCS64, ELF64 little-endian, standard `svc #0` syscalls from
the "generic" table these kernels implement.

**None of this has been run on physical Pi or Jetson hardware.** qemu-user is a
faithful proxy for the ISA and ABI, and there is nothing board-specific in the
output, but that is an argument for expecting it to work rather than evidence
that it does. Anyone with the hardware should run
`python3 tools/arm64_difftest.py` on the board itself — it compares against the
system `gcc`, so it is a real test wherever it runs.

## Known constraints

- **`printf` is not available on arm64 yet.** The C half of the runtime
  (`rlibc.c`) is variadic, and the arm64 back end does not lower `VaSaveBase`.
  Under `SHIVYC_RLINK` a program on this target links against the assembly
  runtime alone — `puts`, `putchar`, `putint`, `write`, `read`, `strlen`,
  `memset`, `memcpy`, `sbrk` — and calling `printf` fails at link time with an
  undefined reference. Linking with the system `gcc` instead gives the full
  libc. Variadic support is the single most useful next addition for these
  boards.
- **4 KB pages are assumed.** The linker aligns segments to `0x1000`. Both
  64-bit Pi OS and Jetson L4T use 4 KB pages, so this is not a constraint in
  practice on either board — but an AArch64 distro built for 64 KB pages
  (some enterprise distros are) would reject the resulting binaries, because
  the kernel requires `p_offset ≡ p_vaddr (mod pagesize)` for every `PT_LOAD`.
- **No GPIO, no peripherals, no bare metal.** These are Linux userland
  binaries. Driving Pi GPIO or Jetson hardware means `mmap`-ing `/dev/mem` or
  using the kernel interfaces, and the runtime does not wrap those yet.
- **No CPU tuning.** The back end emits baseline ARMv8-A. It does not use
  Cortex-A72 or A76 specific scheduling, nor any optional extension (the Pi 5's
  ARMv8.2 features, crypto extensions, or SVE on newer Jetsons).

## Files

- [`ARM64.md`](ARM64.md) — the AArch64 back end these boards use.
- [`tools/selfhosted_cli_test.py`](tools/selfhosted_cli_test.py) — the
  self-hosted command-line path, per target.
- [`tools/arm64_difftest.py`](tools/arm64_difftest.py) — differential tester
  against `gcc`; useful to run on the board itself.
- [`tools/rpy_lib/rcrt_arm64.s`](tools/rpy_lib/rcrt_arm64.s) — the
  freestanding runtime linked when no libc is used.
