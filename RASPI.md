# Raspberry Pi

Two different things in this repository target a Raspberry Pi, and it is worth
being clear which is which:

- [BOARDS.md](BOARDS.md) — static **Linux userland** binaries: `svc #0`
  syscalls, an ELF the Pi's kernel loads and runs.
- **This page** — **bare metal**: no operating system at all. The image *is*
  the kernel. It boots itself, handles its own exceptions, brings up its own
  MMU, and takes its own interrupts.

The general AArch64 bare-metal design — boot sequence, exception vectors, MMU,
the `intc_*` seam — lives in [BAREMETAL_ARM64.md](BAREMETAL_ARM64.md). This
page is only what is specific to a Pi, and it is more than the address changes
you might expect.

## Trying it

```sh
make baremetal-raspi          # boot, exceptions, MMU
make baremetal-raspi-irq      # timer interrupts via the BCM controller
make baremetal-echo-raspi     # interrupt-driven serial echo
```

Each builds a bare-metal image and boots it under
`qemu-system-aarch64 -M raspi3b -cpu cortex-a53`. The whole toolchain is ours:

```
C  ->  ShivyCX  ->  rasm  ->  rlink -T raspi_arm64.ld  ->  kernel ELF
```

No gcc, no GNU as, no GNU ld.

```
== Raspberry Pi 3: timer interrupts, no GIC ==
CNTFRQ_EL0        = 62500000 Hz
TIMER_IRQCNTL     = 0x2  (bit 1 = CNTPNSIRQ, the non-secure physical timer)

[masked]   ticks      = 0
           IRQ_SOURCE = 0x2  (nonzero: asserted, waiting on PSTATE.I)

[unmasked] ticks in 100ms at 1000Hz = 94
           spurious=0 unexpected=0
```

## What is specific to a Pi

| | Pi 3 (BCM2837) | Pi 4 (BCM2711) |
|---|---|---|
| load address | `0x80000` | `0x80000` |
| peripheral base | `0x3F000000` | `0xFE000000` |
| PL011 (UART0) | `0x3F201000` | `0xFE201000` |
| GPIO | `0x3F200000` | `0xFE200000` |
| timer routing | ARM local, `0x40000000` | ARM local |
| GPU interrupts | legacy controller, `0x3F00B200` | GIC-400 `0xFF841000` |
| CPU | Cortex-A53 | Cortex-A72 |

`--board raspi3` and `--board raspi4` select these. Only `raspi3` can be booted
here — no qemu machine models a BCM2711 — so `raspi4` builds and `--run`
refuses rather than leaving a blank console to be misread as a hang.

### The load address is a firmware contract

The Pi's firmware loads `kernel8.img` **at `0x80000`** and jumps to its first
byte. That is why `raspi_arm64.ld` starts there and why `.text.boot` is
`KEEP`-ed first: `_start` must be the image's first instruction.

The 32-bit kernels load at `0x8000`. The extra digit is the 64-bit convention,
and mixing them up produces an image that never runs at all — no error, no
output, because the firmware jumped somewhere that was never written.

Below `0x80000` sits the ATAGS/device-tree area the firmware has already
populated, so loading lower would mean overwriting the very information a real
kernel would read.

### The UART is connected to nothing until you say so

The Pi's primary UART *is* a PL011, so the same driver used on qemu's virt
machine works — but on virt the UART is simply present, and on a Pi it is
wired to a pin only after **GPIO 14 and 15 are switched to their ALT0
function**.

Miss that step and the PL011 initialises happily, accepts every byte, and
emits none. That is indistinguishable from a dead console, which is a hard
thing to debug when the console is your only instrument.

The pull-up/pull-down sequence around it is also fixed by the BCM2835
datasheet — write the control register, wait ~150 cycles, write the clock
mask, wait again, clear both. The waits are why it cannot be collapsed: the
hardware latches on a delay, not on a handshake.

## The Pi 3 has no GIC

This is the largest structural difference, and the reason the Pi is the most
interesting board in the tree. Broadcom's arrangement predates an ARM
interrupt controller being a given, and it is split in two:

| | where | what |
|---|---|---|
| **ARM local peripherals** | `0x40000000` | per-core generic timer, mailboxes, GPU routing. Added by the BCM2836 when the Pi went multi-core. |
| **legacy controller** | `0x3F00B200` | GPU peripherals — UART, DMA, system timer. Inherited from the single-core BCM2835, with no per-core notion at all. |

Note the ARM local base does **not** move with the peripheral base: it is
`0x40000000` on both the BCM2836 and BCM2837, unlike the UART and GPIO.

`bcm_irq_arm64.c` implements the same `intc_*` interface as `gic_arm64.c`, so
none of this reaches the board-independent timer and dispatch code. Two
differences shape it:

**There is no acknowledge and no end-of-interrupt.** A GIC hands you an id from
`IAR`, marks it active, and will not present another at that priority until
that id is written back to `EOIR`. Here you read a *source bitmap* of what is
currently asserted, and the interrupt goes away only when the device itself is
quieted — for the timer, by rearming `CNTP_TVAL_EL0`. So `intc_eoi()` is empty,
and says why rather than pretending to do something.

**There are no interrupt ids.** The source register is a bitmap, not a number.
`intc_irq_of()` therefore reports the timer as 30 and the UART as 33 — the ids
they carry on a GIC — so that `irq_arm64.c` stays free of board knowledge. That
is a deliberate fiction, and the only one in the file.

### Which timer bit, and why it matters

The generic timer raises a *different* interrupt depending on the security
state:

| | bit in `TIMER_IRQCNTL` / `IRQ_SOURCE` |
|---|---|
| `CNTPSIRQ` — secure physical | 0 |
| `CNTPNSIRQ` — **non-secure physical** | 1 |
| `CNTHPIRQ` — hypervisor | 2 |
| `CNTVIRQ` — virtual | 3 |

The boot stub sets `SCR_EL3.NS` on the way down, so this image runs non-secure
and the timer is `CNTPNSIRQ`, bit 1.

Enabling the wrong one gives a timer that counts, asserts, and is never routed
to the core. Mutating the driver to use the secure bit produces exactly that:
**zero ticks even with interrupts unmasked**, no error, no spurious count,
nothing pointing at the cause.

### A UART interrupt passes two controllers

The timer is the easy interrupt: private to the core, no routing. The UART is
where the Pi's split arrangement shows:

1. the PL011's own `IMSC` must unmask receive;
2. the **legacy controller** must enable GPU 57 (bit 25 of the second bank);
3. the **ARM local block** must route the aggregate GPU signal to a core;
4. `IRQ_SOURCE` then reports only an undifferentiated "GPU" bit, which must be
   decoded a *second* time against the legacy controller's pending banks.

Enabling step 2 but not step 3 leaves the interrupt asserted at the legacy
controller and invisible to every core — `IRQ_SOURCE` reads zero, with no error
anywhere. Mutating the routing register reproduces it: `ISR_EL1=0x0`, nothing
received.

(GPU 57 is the PL011. The mini-UART is 29, in the *first* bank — a different
device on the same pins, and a common source of confusion.)

## The exception level the Pi hands you

Under `qemu -M raspi3b` the image is entered at **EL3**. That is worth stating
because `qemu -M virt` enters at **EL1**, and real Pi firmware (via `armstub`)
enters at EL2 — three boards, three levels.

The boot stub descends from whichever it is given. This is not a theoretical
robustness exercise: the EL3→EL2 path was never executed on virt, and the
first time a Pi ran it, it uncovered a compiler bug that had been latent for
three sessions of entirely green tests (see
[BAREMETAL_ARM64.md](BAREMETAL_ARM64.md) — "A latent miscompile that only a
real board could expose"). Every `adr` in the tree was assembling to `udf`, and
nothing on virt ever executed one.

## qemu versus real hardware

Everything here is verified under `qemu-system-aarch64 -M raspi3b`. **None of
it has run on a physical Pi.** Where the two are likely to differ:

- **Clocks.** qemu reports `CNTFRQ_EL0` as 62.5 MHz; a real Pi 3 reports
  19.2 MHz. Nothing here hardcodes it — the timer code reads `CNTFRQ_EL0` — but
  it means the tick arithmetic is exercised at only one frequency.
- **UART baud.** qemu ignores the PL011 divisors entirely; there is no wire to
  clock. The driver writes divisors for a 24 MHz `UARTCLK` (`IBRD` 13,
  `FBRD` 1), which is what the virt machine uses and what a Pi's firmware
  normally leaves configured — but a Pi's `UARTCLK` is set by firmware and can
  differ, and none of this has been checked against an oscilloscope. On real
  hardware this is the most likely single cause of a garbled console.
- **GPIO pull-up/down timing.** qemu accepts the datasheet sequence without
  needing the delays. Real silicon does need them, and a too-short delay fails
  intermittently rather than cleanly.
- **Firmware hand-off.** A real Pi arrives via `armstub` at EL2 with the
  secondary cores parked in a spin table; qemu enters at EL3 with all four
  cores at `_start`. The boot stub handles both, but only one has been run.
- **`config.txt` packaging.** Booting on hardware needs `arm_64bit=1` and the
  image renamed to `kernel8.img` on the boot partition. That packaging step is
  not automated here.
- **Caches and the MMU.** qemu's memory model is far more forgiving than real
  silicon about barriers and cache maintenance around enabling translation. The
  `dsb`/`isb` sequence in `mmu_enable_arm64.S` follows the architecture
  requirements rather than what qemu happens to accept, but "follows the spec"
  and "verified on hardware" are not the same claim.

## What is not done

- **Pi 4 interrupts are on, but not verified on hardware.** The BCM2711's
  GIC-400 (GICD `0xFF841000`, GICC `0xFF842000`) is now wired up and boots:
  no qemu machine models a BCM2711, so it runs under
  [armulator](https://github.com/crustos/armulator) instead --
  `python3 tools/jetson_armulator.py --board raspi4` takes timer interrupts
  at 100 Hz with none spurious. armulator models the CPU, GIC, timer and
  console, not the SoC, so that is evidence about the image rather than
  about silicon. The UART's own interrupt (INTID 153) is still unexercised:
  the timer arrives as PPI 30 and nothing drives the UART's SPI.
- **No GPIO, DMA or transmit interrupts.** Only the timer and UART receive.
- **Secondary cores are parked.** The boot stub sends every core but the first
  to `wfi`. The Pi's spin-table protocol for starting them is not implemented,
  and `core_id()` in `bcm_irq_arm64.c` is a documented placeholder returning 0.
- **The mini-UART is not supported.** Only the PL011.

## Files

| | |
|---|---|
| [`baremetal64/raspi_arm64.ld`](baremetal64/raspi_arm64.ld) | image layout, load at `0x80000` |
| [`baremetal64/bcm_irq_arm64.c`](baremetal64/bcm_irq_arm64.c) | ARM local + legacy interrupt controllers |
| [`baremetal64/uart_arm64.c`](baremetal64/uart_arm64.c) | PL011, with the Pi's GPIO ALT0 setup |
| [`examples/baremetal/kernel_raspi.c`](examples/baremetal/kernel_raspi.c) | boot, exceptions, MMU |
| [`examples/baremetal/kernel_raspi_irq.c`](examples/baremetal/kernel_raspi_irq.c) | timer interrupts, showing raw controller state |
| [`examples/baremetal/kernel_echo.c`](examples/baremetal/kernel_echo.c) | interrupt-driven echo, timer and UART sharing the vector |
| [`tools/irq_timer_test.py`](tools/irq_timer_test.py) | timer interrupts, booted |
| [`tools/uart_rx_irq_test.py`](tools/uart_rx_irq_test.py) | UART receive interrupts, booted |

Everything else — boot stub, vectors, MMU, timer, dispatch — is shared with the
other boards and documented in [BAREMETAL_ARM64.md](BAREMETAL_ARM64.md).
