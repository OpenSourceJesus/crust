# Jetson Nano

Two different things in this repository target a Jetson:

- [BOARDS.md](BOARDS.md) — static **Linux userland** binaries: `svc #0`
  syscalls, an ELF the Jetson's kernel loads and runs.
- **This page** — **bare metal**: no operating system. The image is the kernel.

The general AArch64 bare-metal design — boot sequence, exception vectors, MMU,
the `intc_*` seam — is in [BAREMETAL_ARM64.md](BAREMETAL_ARM64.md). This page is
what is specific to a Tegra.

> **Read this first.** The Jetson image **now boots under
> [armulator](https://github.com/crustos/armulator)**, a pure-Python ARM
> emulator with a Cortex-A57 core — the Nano's actual core — and a Tegra X1
> board model. It runs to completion: EL1 entry, the MMU brought up and
> translating, a deliberate fault taken and recovered, and timer interrupts
> arriving through the GIC at 100 Hz with none spurious.
>
> No qemu machine models a Tegra, so `--run` still refuses there; armulator is
> what fills that gap. But armulator models the **CPU, the GIC and the
> console**, not the SoC — there is no clock-and-reset controller, no memory
> controller, no PMIC, display or USB. So a passing run is evidence about the
> image's CPU, MMU, exception and interrupt behaviour, and **not** evidence
> that a physical Nano boots it. **Still never run on hardware.** Everything
> below says which claim is which.

## Trying it

```sh
make baremetal-jetson
```

builds `build/kernel_jetson.elf`. `--run` deliberately **refuses**:

```
[baremetal-arm64] cannot run jetson here: no qemu machine models this board's
peripherals, so the image would boot to a silent console. The image itself is
built and can be copied to hardware.
```

A blank console is exactly what a hung image looks like, so refusing is better
than producing one.

### Booting it under armulator

```sh
git clone https://github.com/crustos/armulator.git ../armulator
python3 tools/jetson_armulator.py
```

[`tools/jetson_armulator.py`](tools/jetson_armulator.py) builds the image,
loads its `PT_LOAD` segments into armulator's `JetsonNanoA64` board, streams
the console, and exits nonzero if the image faults through the vector table or
never prints what was expected — so it works as a CI check, not just a demo.
It ends with:

```
  unmasked, waiting 300ms: 30 ticks (expect ~30)
  spurious=0 unexpected=0

== all stages ok ==

[jetson-armulator] 300000 instructions, halted=False fault_loop=False
[jetson-armulator] OK
```

That is the same output the virt machine produces under qemu, on a board qemu
cannot model at all.

`--elf` boots a prebuilt image, `--armulator PATH` points at a checkout
elsewhere, and `--expect` changes the string that has to appear.

`halted=False` is expected rather than a problem: once timer interrupts are
live, the firmware's parked halt loop is entered and left on every tick, so
the tight-self-branch detection never fires. The tool stops on the expected
text instead.

The toolchain is entirely ours:

```
C  ->  ShivyCX  ->  rasm  ->  rlink -T jetson_arm64.ld  ->  kernel ELF
```

## What is specific to a Tegra

| | Jetson Nano / TX1 (Tegra X1, T210) |
|---|---|
| load address | `0x80080000` |
| console | 16550-style UART-A at `0x70006000` |
| register spacing | **4 bytes**, not 1 |
| interrupt controller | GICv2, GICD `0x50041000`, GICC `0x50042000` |
| UART interrupt | INTID 68 (UART-A) |
| CPU | Cortex-A57 |

### The load address is a U-Boot convention, not hardware

The Jetson does not boot like a Pi. There is no firmware that loads a
fixed-name image at a fixed address: it comes up through cboot into **U-Boot**,
which loads an arm64 `Image` and jumps to it with `booti`.

DRAM starts at `0x80000000`, and the arm64 Linux convention — which U-Boot
follows — places the image 2 MiB in, at **`0x80080000`**, leaving room below
for the device tree and initrd U-Boot has already written there.

So unlike the Pi's `0x80000`, this is a *convention* rather than a constraint.
`booti <addr>` will happily run an image elsewhere; `0x80080000` is what the
default boot scripts use and what an `Image`-format kernel expects.

## The console is not a PL011

Every other board here uses a PL011, so one driver served them all with only a
base address changed. Tegra's debug console is UART-A at `0x70006000`, a
**16550-style** port. The two are unrelated designs, and each difference is a
way to be silently wrong:

| | PL011 | 16550 / Tegra |
|---|---|---|
| data register | `DR` at `0x00` | `THR` at `0x00` |
| "can I write?" | `FR.TXFF`, set when **full** | `LSR.THRE`, set when **empty** |
| baud rate | `IBRD` + `FBRD`, plain registers | `DLL` + `DLM`, reachable only while `LCR.DLAB` is set |
| register spacing | 4 bytes | 1 byte architecturally; **Tegra uses 4** |
| receive interrupt | `RXIM` *and* `RTIM` (timeout) | one `ERBFI` bit; timeout folded in |

Three traps, in order of how quietly they fail:

**Flag polarity.** `while (*fr & TXFF)` and `while (!(*lsr & THRE))` express the
same intent inversely. A driver written by analogy with the PL011 waits exactly
when it should write, and hangs on the first character.

**Register spacing.** Much 16550 documentation assumes one-byte spacing. Tegra,
like most SoCs embedding one on a 32-bit bus, spaces them four apart. With the
wrong shift, `LCR` writes land on `IIR/FCR` and the port is configured almost at
random — with no error, because every address in the range decodes to a real
register.

**The DLAB dance.** `DLL` and `DLM` are aliases of `THR` and `IER`, reachable
only while `LCR.DLAB` is set. Leave it set and every character afterwards goes
into the divisor latch instead of being transmitted.

This is what split the console in two. [`console_arm64.c`](baremetal64/console_arm64.c)
holds `uart_puts`, `uart_puthex` and `uart_putdec`, written entirely in terms of
a single `uart_putc()`; the board profile picks which driver supplies it. A
third board no longer means a third copy of the formatting code — or a third
place for a formatting bug to be fixed in only two of them.

## Interrupts: a GIC that has moved

Unlike the Pi, the Tegra X1 **has a GICv2**, so it needs no new interrupt
controller at all — `gic_arm64.c`, `timer_arm64.S` and `irq_arm64.c` are
included unchanged, with only the base addresses passed in.

The two bases are separate parameters rather than a base plus a constant, and
that is the whole point:

| | distributor | CPU interface | gap |
|---|---|---|---|
| qemu virt | `0x08000000` | `0x08010000` | `0x10000` |
| **Jetson (T210)** | `0x50041000` | `0x50042000` | **`0x1000`** |

The gap is not architectural. A driver deriving the CPU interface as
`GICD_BASE + 0x10000` works perfectly on virt and, on a Jetson, puts every
CPU-interface write `0xF000` into the *distributor's* address space — where the
writes are accepted and quietly do something else. Nothing reports an error;
interrupts simply never arrive.

## How this is tested

There are now two layers, and they catch different things.

**Register level.** No qemu machine models a Tegra, so both drivers are also
compiled with their bases pointing at ordinary **RAM**, run under `-M virt`
with the PL011 as the real console, and the resulting register file is dumped
and checked. This predates the armulator work and still runs: it is fast, needs
no emulator, and pins down exactly which register each write lands on.

[`tools/uart_8250_test.py`](tools/uart_8250_test.py) checks: the divisor written
behind `DLAB`, `LCR` left at 8N1 with `DLAB` *clear*, FIFOs enabled, DTR/RTS
asserted, a character reaching `THR`, and nothing written past the offsets the
4-byte spacing implies. The fake registers are pre-loaded with `LSR.THRE` set so
the polling loops complete — which is also what makes a polarity inversion
visible, since it hangs instead.

[`tools/gic_base_test.py`](tools/gic_base_test.py) uses two RAM regions
**`0x1000` apart** — the Tegra spacing specifically. If the CPU-interface offset
were assumed rather than passed, that region would stay empty; the test reports
"nothing was written to the CPU-interface region at all" and names the likely
cause rather than a generic mismatch.

Both are mutation-tested: wrong spacing, `DLAB` left set, inverted polarity,
derived GIC offset, ignored base override, wrong `ISENABLER` offset — all
caught.

**Emulated boot.** `tools/jetson_armulator.py` runs the whole image on
armulator's Tegra X1 board. This is the layer that catches what register checks
structurally cannot: that the parts compose, in order, on a running core.

### What each layer does and does not prove

The register tests prove **the drivers write what they mean to write, where
they mean to write it.**

The armulator boot proves the image **gets from reset to the end of `kmain`**
on a Cortex-A57 with a Tegra memory map: the exception-level descent, the
`.bss` clear, the MMU tables, translation, fault entry and return, GIC
enable, and timer interrupts at the rate asked for.

Neither proves that a real Tegra accepts the sequence, that the clock and
divisor suit the board, that anything reaches a wire, or that the SoC's clock
and reset controller has even enabled UART-A by the time the image runs. Those
need hardware.

The residual risk is narrower than it was. The *interrupt path* — timer,
dispatch, acknowledge/EOI — is the same code booted and exercised at 1000 Hz on
virt and on a Raspberry Pi, and now at 100 Hz on the Tegra map. What is still
untested on a Jetson is specifically whether Tegra's addresses and ids are
right on silicon.

### What booting it caught

Worth recording, because it is the argument for having done this: the MMU
identity map in `baremetal64/mmu_arm64.c` **could never have worked on a
Jetson**. It hardcoded level-1 entries 0 and 1 with `RAM_BASE` at
`0x40000000`, which is right for virt and for the Pi. The Nano's DRAM is at
`0x80000000` — level-1 entry **2** — so the image mapped neither the code it
was executing nor the vector table it would fault into.

The failure is as undiagnosable as that sounds: `ESR = 0x86000005`, an
instruction abort with a level-1 translation fault whose `FAR` is the vector
address itself, looping forever with nothing left that could report it. No
register-level test would have found it, because every individual register
write was correct.

The map is now built from `RAM_BASE`, `PERIPH_BASE` and `PERIPH_SIZE` supplied
per board, per gigabyte and per 2 MiB block. Per-block matters because on the
Pi the two are not separable — RAM at 0 and peripherals at `0x3F000000` share
gigabyte 0 — and the window has to be able to straddle a gigabyte boundary,
because the Pi 3's ARM *local* peripherals sit at `0x40000000` in the next one.

## Unverified constants

Everything on this list is from documentation, not from a **physical** board.
Booting under armulator exercises them against a model built from the same
documentation, so agreement there is a consistency check between two readings
of the TRM rather than confirmation from silicon. If the image ever reaches
hardware, these are still the first things to check:

| constant | value | note |
|---|---|---|
| load address | `0x80080000` | U-Boot `booti` convention |
| UART-A base | `0x70006000` | UART-B/C/D at `+0x40`, `+0x200`, `+0x300` |
| register shift | 2 (4 bytes) | wrong value fails silently |
| `UART8250_CLK` | 408 MHz | U-Boot has usually already set the divisor |
| GICD / GICC | `0x50041000` / `0x50042000` | |
| UART interrupt | INTID 68 | wired in armulator; never exercised on silicon |
| RAM base | `0x80000000` | decides which level-1 entry the MMU fills |
| entry exception level | EL2 assumed | U-Boot usually hands off at EL2; the boot stub descends from EL3, EL2 or EL1, so this should not matter |

## What is not done

- **Never run on hardware.** It boots under armulator; that is not the same
  claim. See the note at the top of this page.
- **No SoC model.** armulator has the CPU, the GIC, the console and the GPIO
  block. Tegra's clock and reset controller, memory controller, power
  management, display and USB are all absent, so nothing that depends on them
  is exercised anywhere.
- **No packaging story.** Getting an image onto a Jetson means going through
  U-Boot — `booti` from a TFTP or SD load, or a flashed partition. None of that
  is automated here.
- **No device tree.** U-Boot passes a DTB in `x0`; the boot stub ignores it. A
  real kernel would use it to find the UART and GIC rather than hardcoding
  them, which is what would make these constants self-checking.
- **Only the timer and UART receive interrupts**, and the UART one is
  configured but never exercised.
- **Secondary cores are parked** in `wfi`; PSCI bring-up is not implemented.

## Files

| | |
|---|---|
| [`baremetal64/jetson_arm64.ld`](baremetal64/jetson_arm64.ld) | image layout, load at `0x80080000` |
| [`baremetal64/uart_8250.c`](baremetal64/uart_8250.c) | Tegra 16550 console |
| [`baremetal64/console_arm64.c`](baremetal64/console_arm64.c) | board-independent formatting |
| [`baremetal64/gic_arm64.c`](baremetal64/gic_arm64.c) | GICv2, bases parameterised |
| [`tools/uart_8250_test.py`](tools/uart_8250_test.py) | 16550 driver, register-level |
| [`tools/gic_base_test.py`](tools/gic_base_test.py) | relocated GIC bases, register-level |

Everything else is shared with the other boards and documented in
[BAREMETAL_ARM64.md](BAREMETAL_ARM64.md).
