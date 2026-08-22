# Raspberry Pi and Jetson Nano

Both boards are **AArch64 Linux**, so ShivyCX targets them with the same
`arm64` back end documented in [ARM64.md](ARM64.md). There is no separate
"Raspberry Pi target" to select: a Pi 4 and a Jetson Nano differ in SoC and
peripherals, not in instruction set or ABI, and neither needs anything the
generic AArch64 Linux path does not already provide.

What *is* board-specific is the plumbing around the compiler — which
architecture it defaults to, which runtime it links, and whether it needs a
system toolchain at all. That is what this page covers.

# Armulator

A pure-Python ARM emulator with Raspberry Pi and Jetson board models:
https://github.com/crustos/armulator

It is how the bare-metal Jetson image gets booted at all — no qemu machine
models a Tegra. See [`tools/jetson_armulator.py`](tools/jetson_armulator.py)
and [JETSON_NANO.md](JETSON_NANO.md).

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

## The board tools

`tools/raspi.py` and `tools/jetnano.py` take your sources, compile them with
everything the board needs, package the result so the directory can be copied
straight over, and optionally run it here under the board's actual core:

```sh
python3 tools/raspi.py prog.c --qemu               # build and run
python3 tools/jetnano.py prog.c --cross --qemu     # link with glibc instead
python3 tools/raspi.py prog.c --test-script=t.py --debug
python3 tools/raspi.py --info                      # what is and isn't emulated
```

By default they use **our own assembler, linker and runtime** — no gcc, no
binutils, no libc. That is also the only mode that works when cross-compiling
from an x86-64 host without a cross toolchain installed, since otherwise
ShivyCX hands AArch64 assembly to the host's `as`. `--cross` switches to the
GNU cross toolchain and brings in the full glibc.

`--qemu` runs under qemu-user with `-cpu` set to the board's real core —
`cortex-a72` for the Pi 4, `cortex-a57` for the Jetson Nano.

### Test scripts

`--test-script=FILE` runs a plain Python file against the result, with
`record`, `registers`, `stdout`, `stderr`, `exit_code` and `qemu_log` in
scope. Fail by raising, by asserting, or by returning a message from
`check(rec)`:

```python
def check(rec):
    if rec.exit_code != 42:
        return "expected 42, got %d" % rec.exit_code
    if rec.registers.get("X00", 0) & 0xFF != 42:
        return "X0 did not hold the return value"
```

With `--debug`, qemu's `-d cpu` log is captured and the final register state
parsed into `rec.registers`, so a test can assert on machine state rather than
just on a program's exit code — which is what makes this useful for chasing
miscompiles and for diff-testing against a known-good build.

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

- **`printf` works, via our own libc.** Variadic functions are supported on
  both AArch64 and RV64, so `rlibc.c` (printf, malloc, getenv, syscall
  wrappers) is compiled and linked for these targets. ShivyCX's variadic
  convention is its own — every argument in one contiguous stack block, base
  handed over in `x16` — rather than AArch64's real `va_list`, so a *glibc*
  variadic callee reached without `--cross` would not see its arguments. Both
  sides are ours in the default mode, so this does not arise in practice.
- **4 KB pages are assumed.** The linker aligns segments to `0x1000`. Both
  64-bit Pi OS and Jetson L4T use 4 KB pages, so this is not a constraint in
  practice on either board — but an AArch64 distro built for 64 KB pages
  (some enterprise distros are) would reject the resulting binaries, because
  the kernel requires `p_offset ≡ p_vaddr (mod pagesize)` for every `PT_LOAD`.
- **No GPIO and no peripherals from Linux userland.** The binaries on this
  page are Linux userland binaries; driving Pi GPIO or Jetson hardware from
  one means `mmap`-ing `/dev/mem` or using the kernel interfaces, and the
  runtime does not wrap those yet. There is now a separate *bare-metal*
  AArch64 path — no OS at all, with its own boot, exception and MMU bring-up —
  documented in [BAREMETAL_ARM64.md](BAREMETAL_ARM64.md), with the
  board-specific details in [RASPI.md](RASPI.md) and
  [JETSON_NANO.md](JETSON_NANO.md). It boots on
  `qemu-system-aarch64 -M virt` and on `-M raspi3b`, each with its own load
  address, console and interrupt controller — a GICv2 on virt, the BCM2837's
  ARM local peripherals on the Pi — and takes timer interrupts on both. The
  **Jetson and the Pi 4 boot too, under
  [armulator](https://github.com/crustos/armulator)** rather than qemu, which
  models neither a Tegra nor a BCM2711:
  `python3 tools/jetson_armulator.py --board jetson|raspi4` runs each to
  `== all stages ok ==`, on a Cortex-A57 with the Tegra X1 map and a
  Cortex-A72 with the BCM2711 map respectively, both taking timer interrupts
  through a GIC-400 at 100 Hz with none spurious. armulator models the CPU,
  GIC, architected timer and console but not the SoC, so that is evidence
  about the image, not about silicon — no board here has been run on physical
  hardware.
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
- [`tools/jetson_armulator.py`](tools/jetson_armulator.py) — boots a
  bare-metal Jetson or Pi 4 image under armulator, since qemu models neither.
- [`tools/armulator_boards_test.py`](tools/armulator_boards_test.py) — boots
  both of those under armulator and checks the timer rate, the abort on an
  unmapped store, and the interrupt counts. Skips if armulator is absent.
- [`tools/board_machine_test.py`](tools/board_machine_test.py) — checks that
  `--board` and `--machine` cannot be silently confused.
