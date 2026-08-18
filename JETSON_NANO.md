# Jetson Nano

Two different things in this repository target a Jetson:

- [BOARDS.md](BOARDS.md) — static **Linux userland** binaries: `svc #0`
  syscalls, an ELF the Jetson's kernel loads and runs.
- **This page** — **bare metal**: no operating system. The image is the kernel.

The general AArch64 bare-metal design — boot sequence, exception vectors, MMU,
the `intc_*` seam — is in [BAREMETAL_ARM64.md](BAREMETAL_ARM64.md). This page is
what is specific to a Tegra.

> **Read this first.** Unlike virt and the Raspberry Pi, **the Jetson image has
> never been booted.** No qemu machine models a Tegra, so there is nowhere to
> run it. What exists is an image that builds at the right address, a console
> driver and an interrupt controller configuration, each verified at register
> level against a RAM-backed harness. That is a real and useful check — it
> catches the failure modes that do not need hardware — but it is not the same
> claim as "it boots". Everything below says which is which.

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

## How something unbootable is tested

No qemu machine models a Tegra, so the choice was between testing nothing and
testing what can actually be tested. The failure modes above need no hardware
to catch, so both drivers are compiled with their bases pointing at ordinary
**RAM**, run under `-M virt` with the PL011 as the real console, and the
resulting register file is dumped and checked.

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

### What these tests do and do not prove

They prove **the drivers write what they mean to write, where they mean to
write it.**

They do not prove that a real Tegra accepts the sequence, that the clock and
divisor suit the board, that anything reaches a wire, or that an interrupt is
ever delivered. Those need hardware.

What reduces the residual risk is that the *interrupt path itself* — the timer,
the dispatch, the acknowledge/EOI protocol — is the same code booted and
exercised at 1000 Hz on virt and on a Raspberry Pi. What is untested on the
Jetson is specifically whether Tegra's addresses and ids are right.

## Unverified constants

Everything on this list is from documentation, not from a running board. If the
image ever reaches hardware, these are the first things to check:

| constant | value | note |
|---|---|---|
| load address | `0x80080000` | U-Boot `booti` convention |
| UART-A base | `0x70006000` | UART-B/C/D at `+0x40`, `+0x200`, `+0x300` |
| register shift | 2 (4 bytes) | wrong value fails silently |
| `UART8250_CLK` | 408 MHz | U-Boot has usually already set the divisor |
| GICD / GICC | `0x50041000` / `0x50042000` | |
| UART interrupt | INTID 68 | never exercised |
| entry exception level | EL2 assumed | U-Boot usually hands off at EL2; the boot stub descends from EL3, EL2 or EL1, so this should not matter |

## What is not done

- **Never booted.** See above.
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
