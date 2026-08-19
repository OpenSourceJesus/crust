# Bare metal on AArch64

[BOARDS.md](BOARDS.md) covers the Raspberry Pi and Jetson Nano as *Linux*
targets: static userland binaries, `svc #0` syscalls, an ELF the kernel loads.
This page covers the other thing those boards can do — running with no
operating system underneath at all.

The starting point is `qemu-system-aarch64 -M virt`, deliberately. It is a
plain ARMv8 machine with a PL011 UART and nothing board-specific, so the boot
path, the exception model and the MMU can be brought up and verified before
any of the Pi or Jetson particulars (firmware hand-off, `config.txt`,
`kernel8.img`, U-Boot, Tegra's 8250-style UART) are in play. Those come next
and are noted at the bottom.

```
C  ->  ShivyCX  ->  rasm  ->  .o  ]
S  ->  rasm     ->  .o           ]--> rlink -T virt_arm64.ld -> bootable ELF
```

No external tool is involved at any stage. `--gnu-ld` switches the final link
to `aarch64-linux-gnu-ld`, which is useful only as an oracle: the two should
produce images that behave identically, and `tools/rlink_script_test.py` links
this kernel both ways and boots both to check it.

## Trying it

```sh
make baremetal-arm64-run
python3 tools/baremetal_arm64.py examples/baremetal/kernel_arm64.c --run
```

which prints, on the emulated serial console:

```
[compute] fib(0..12): 0 1 1 2 3 5 8 13 21 34 55 89 144
[except] storing to 0xdeadbe000 with the MMU off
*** exception: EL1h synchronous
    ESR = 0x96000050  EC = 0x25 (data abort, same EL)
    external abort
[mmu] ... MMU=1 dcache=1 icache=1
[mmu] summing an array through the MMU: 6048 (expect 6048)
[mmu] the same kind of bad store, now translated:
    translation fault, level 1
[irq] GIC + generic timer at 100 Hz
  interrupts still masked, waiting 200ms: 0 ticks (expect 0)
  unmasked, waiting 300ms: 30 ticks (expect ~30)
== all stages ok ==
```

The last part is the useful bit: the *same* kind of bad store reports an
**external abort** before the MMU is on and a **translation fault, level 1**
after. That difference can only come from a real page-table walk, so it is
evidence the MMU is working rather than merely evidence the machine did not
crash.

## Why none of the x86 bare-metal path carries over

There is already a bare-metal path in this repository —
[`shivycx_baremetal.py`](shivycx_baremetal.py), `baremetal64/boot64.S`,
`kernel64.ld` — and almost none of it applies here:

| | x86-64 | AArch64 |
|---|---|---|
| entry | 32-bit protected mode, Multiboot1 header | already 64-bit, no header |
| getting to 64-bit | enable PAE, long mode, install a GDT | nothing to do |
| privilege | rings, no transition needed at boot | drop EL2 → EL1 via `eret` |
| paging | **mandatory** for long mode | optional; MMU off is a valid state |
| interrupt table | IDT: 256 descriptors, CPU reads an address | 16 slots of *code*, CPU branches to the slot |
| console | VGA text buffer at `0xB8000` | PL011 UART at `0x09000000` |
| cores at reset | one, others started via APIC IPI | all of them, at `_start`, at once |

So the *shape* of the pipeline is reused and none of the OS is. In particular
this does not link against minikraft: minikraft is a 32-bit x86 mini-OS whose
drivers are a VGA console, a PS/2 keyboard and a PIC/PIT timer, and not one of
those devices exists on an AArch64 machine.

## The boot sequence

[`baremetal64/boot_arm64.S`](baremetal64/boot_arm64.S), in order, and the order
matters:

1. **Park the secondary cores.** `-M virt` starts all four at `_start`
   simultaneously. `MPIDR_EL1`'s low bits are the CPU id; anything nonzero goes
   to `wfi`. Without this, four cores race through the same `.bss` clear and
   the same `kmain`.
2. **Drop to EL1** if `CurrentEL` says EL2 — set `HCR_EL2.RW` so EL1 is
   AArch64, point `ELR_EL2` at the continuation, `SPSR_EL2 = 0x3C5` (EL1h,
   interrupts masked), `eret`.
3. **Enable FP/SIMD** via `CPACR_EL1.FPEN`. ShivyCX has a floating-point
   register file and emits `d`/`s` registers for FP code, which trap at the
   *first FP instruction* while FPEN is 0 — arbitrarily far from the cause.
4. **Install the vectors** in `VBAR_EL1`, before anything can fault.
5. **Stack**, then **clear `.bss`** — nothing has zeroed it, since on a hosted
   target the kernel's ELF loader does that.
6. `bl kmain`.

## The exception vectors

`VBAR_EL1` points at one 2 KiB block of code: 16 slots, 128 bytes each,
grouped by where the exception came from (current EL with SP_EL0, current EL
with SP_ELx, lower EL AArch64, lower EL AArch32) and within each group by kind
(synchronous, IRQ, FIQ, SError). Kernel faults arrive in the second group,
because we run at EL1h.

**128 bytes is 32 instructions and it is a hard ceiling.** The first version of
[`vectors_arm64.S`](baremetal64/vectors_arm64.S) inlined the register save into
every slot: 33 instructions, one over. `.balign 128` does not report that — it
rounds the *next* entry up to the following boundary, so the slots ended up 256
bytes apart, eight of the sixteen fell outside the table, and the CPU (which
computes its target as `VBAR` + a hardcoded offset) branched into the middle of
the wrong handler. A synchronous EL1h data abort was reported as an FIQ.

Nothing about that failure points at the size of a macro. So each slot now
holds four instructions and branches to a shared `exc_common`, and
[`tools/vectors_size_test.py`](tools/vectors_size_test.py) asserts the geometry
against the assembled bytes: 16 slots, exactly 128 apart, each setting a kind
equal to its index.

`exc_arm64.c` decodes `ESR_EL1` — the exception class, and for aborts the fault
status code — and prints `FAR_EL1` only for the classes that actually define
it, since for the others it holds a stale address from an earlier fault and
looks exactly like a real one.

## The MMU

`mmu_arm64.c` builds a flat identity map of the low 1 GiB using 2 MiB blocks:
peripherals (including the UART) as Device memory, RAM as Normal cacheable
inner-shareable. `mmu_enable_arm64.S` commits `MAIR_EL1`, `TCR_EL1` and
`TTBR0_EL1`, then sets `SCTLR_EL1.M`, `.C` and `.I`.

Two details that produce silent failures rather than errors:

- A **table descriptor is `0b11`** — valid *and* table. Writing `0b10` gives an
  entry the walker treats as invalid, so every translation faults the instant
  the MMU comes on, at the first fetch after `msr sctlr_el1`, with no way left
  to report it. A **block descriptor is `0b01`**.
- Mapping the **UART as Normal cacheable** memory would let console writes sit
  in the cache instead of reaching the device, so the console goes silent
  exactly when caching is enabled — which reads as "the MMU broke everything".

`T0SZ = 25` gives a 39-bit address space, so the walk **starts at level 1**. A
48-bit configuration (`T0SZ = 16`) starts at level 0, and the same code would
then install a table one level too shallow.

## A silent miscompile this work exposed

Taking the address of an `extern` object defined elsewhere — including one
defined only by a **linker script**, which is how a bare-metal image finds
`__bss_start` or its page-table region — was broken on **both** bare-metal back
ends:

```c
extern unsigned long __linker_sym;
unsigned long get(void) { return (unsigned long)&__linker_sym; }
```

x86-64 emitted `lea rax, [__linker_sym]`. arm64 emitted `add x1, x29, #16` and
riscv64 `addi x11, sp, 0` — **frame addresses**, with the symbol absent from the
output entirely. The code compiled, linked and ran, and read the stack.

The cause was a gap between two tests in `asm_gen.py`: a value was treated as
symbol-addressed if it had `DEFINED` external linkage or `STATIC` storage
duration. An undefined `extern` has EXTERNAL linkage but *no* storage duration,
because nothing in this translation unit allocates it — so it matched neither
and fell through to the local-frame path.

[ARM64.md](ARM64.md) says unimplemented features "**raise** rather than emit
wrong code, so the differential testers report them as skips, never as silent
miscompiles". This was a counterexample, and it was invisible to
`arm64_difftest.py` because a hosted program compiled and run under qemu never
references a symbol it does not itself define. The bug needs a linker script to
become reachable, and a linker script only appears on this path.

[`tools/extern_symbol_test.py`](tools/extern_symbol_test.py) now checks all
three targets, on the emitted assembly rather than an exit code — because there
is no exit code that distinguishes the two.

## Assembler work this needed

`rasm` could not encode a single system instruction: no `msr`/`mrs`, no
barriers, no `eret`, no `tlbi`/`ic`/`dc`, no `adr`. Every one of boot,
exceptions and MMU is built from exactly those. Added in
[`rasm_arm64.py`](tools/rpy_lib/rasm_arm64.py) and diff-tested against
`aarch64-linux-gnu-as` in
[`rasm_arm64_sys_test.py`](tools/rpy_lib/rasm_arm64_sys_test.py) (83 cases):

- `msr`/`mrs` over a 35-register table, plus the architectural
  `S<op0>_<op1>_C<n>_C<m>_<op2>` form so an unlisted register is never a wall,
  and the PSTATE immediate form (`msr daifset, #2`)
- `isb`/`dsb`/`dmb` with the full option set, the hint space
  (`wfi`/`wfe`/`sev`/`sevl`/`yield`), `eret`
- `tlbi`/`ic`/`dc` maintenance operations
- `adr`, with an `adr_prel_lo21` relocation
- **logical immediates** for `and`/`orr`/`eor`/`ands` — not planned, but a boot
  stub dies at its second instruction without `and x0, x0, #3`

The differential method earned its keep immediately: `wfi` first encoded as
`0xd5032060` instead of `0xd503207f`, because deriving the base by shifting
`0xD503201F` right by 12 drops the fixed `Rt=31` in the low five bits. A legal
instruction, wrong bits, and nothing at runtime would have pointed here.

Separately, **`.macro` was dead code**. `rasm_macro.py` works and has its own
36-case test, but nothing outside that test ever called it, so a `.macro` in
real input reached the encoder with `\arg` unsubstituted and failed as an
unsupported operand form — pointing at the instruction rather than at the
missing expansion. It is now wired into `Assembler.assemble()`.

## Interrupts: the GIC and the generic timer

The vectors handled IRQ slots from the start, but nothing raised one. Two
pieces close that: a GICv2 driver and the ARM generic timer.

The **generic timer** is architectural — part of the CPU, not a device on a
bus — so it needs no device tree entry and no MMIO mapping, and the EL1
physical timer is always PPI 30. That makes it the right first interrupt
source for a bare-metal image: the same code works on virt and on a Pi, which
differ only in `CNTFRQ_EL0` (62.5 MHz against 54 MHz) and read it rather than
assume it. `TVAL` is a countdown with **no auto-reload**, so the handler
rewrites it; a handler that EOIs without rearming takes exactly one tick and
then goes quiet, which looks like the GIC dropping interrupts.

The **GICv2** replaces minikraft's 8259 PIC and shares nothing with it: it is
memory-mapped rather than port-programmed, has 1020 interrupt IDs rather than
15, and splits acknowledge from end-of-interrupt — the value read from
`GICC_IAR` must be written back to `GICC_EOIR` unchanged, since it carries the
source CPU id for an SGI. It has two halves: the global **distributor** (which
interrupts exist, their priority, which core they target) and the per-core
**CPU interface** (what actually presents, acknowledges and ends one).

### Five gates, all silent

An interrupt passes five gates, and a machine with any one shut does not fail
— it simply never takes an interrupt, with nothing to say which:

```
CNTP_CTL_EL0.ENABLE=1, IMASK=0     the timer asserts
GICD_ISENABLER bit 30              the distributor forwards it
GICC_CTLR=1, GICC_PMR > priority   the CPU interface presents it
PSTATE.I clear                     the core accepts it
VBAR_EL1 + 0x280                   the IRQ vector runs
```

The priority gate is the easiest to get wrong in a way that looks like broken
hardware: `0xFF` is the *lowest* priority and `GICC_PMR` masks anything at or
below its own value, so leaving both at the reset default delivers nothing at
all. `timer_selftest()` reports each gate's state for exactly this reason.

### Proving the ticks are interrupts

Counting ticks does not show they came from the interrupt path. So
[`tools/irq_timer_test.py`](tools/irq_timer_test.py) toggles `PSTATE.I` and
requires the count to follow — no ticks while masked, ticks at the programmed
rate while unmasked, none again once re-masked — and checks `ISR_EL1` is
**nonzero while masked**. That last one is the load-bearing observation: it
shows the GIC really was presenting the interrupt and only PSTATE held it
back. Were it zero, the later ticks would prove nothing about the controller.

The test also covers the interactions: interrupts survive the MMU being
enabled underneath them, and a synchronous fault is still handled correctly
with the timer live, with neither counter contaminating the other. **11 pass,
0 fail.**

## Boards

Per-board details, including how each differs from qemu, are in
[RASPI.md](RASPI.md) and [JETSON_NANO.md](JETSON_NANO.md).

`--board` selects a profile. Only three things differ between them; the boot,
exception and MMU code is shared unchanged.

| board | load address | console | GPIO setup | GIC |
|---|---|---|---|---|
| `virt` | `0x40080000` | PL011 @ `0x09000000` | none | GICv2 @ `0x08000000` |
| `raspi3` | `0x80000` | PL011 @ `0x3F201000` | GPIO 14/15 → ALT0 | BCM local @ `0x40000000` |
| `raspi4` | `0x80000` | PL011 @ `0xFE201000` | GPIO 14/15 → ALT0 | not wired up |
| `jetson` | `0x80080000` | 16550 @ `0x70006000` | none | GICv2 @ `0x50041000` |

```sh
python3 tools/baremetal_arm64.py app.c --board raspi3 --run
python3 tools/baremetal_arm64.py --boards
```

The Pi's load address is not arbitrary: firmware loads `kernel8.img` at
`0x80000` and jumps to its first byte. (32-bit kernels load at `0x8000` — the
extra digit is the 64-bit convention, and confusing the two gives an image that
never runs.) The Pi also needs GPIO 14 and 15 switched to their ALT0 function
before the UART is connected to any pin; without that the PL011 runs happily
and is wired to nothing, which is indistinguishable from a dead console.

### The Jetson's console is not a PL011

Every board up to this point used a PL011, so `uart_arm64.c` served all of
them with only its base address changed. Tegra does not: the Jetson's debug
console is UART-A at `0x70006000`, a 16550-style port. The two are unrelated
designs, and each of the differences is a way to get it silently wrong:

| | PL011 | 16550 / Tegra |
|---|---|---|
| data register | `DR` at `0x00` | `THR` at `0x00` |
| "can I write?" | `FR.TXFF`, set when **full** | `LSR.THRE`, set when **empty** |
| baud rate | `IBRD` + `FBRD`, plain registers | `DLL` + `DLM`, only while `LCR.DLAB` is set |
| register spacing | 4 bytes | 1 byte architecturally; **Tegra uses 4** |

The polarity row is the trap. `while (*fr & TXFF)` and
`while (!(*lsr & THRE))` express the same intent inversely, so a driver
written by analogy with the PL011 waits exactly when it should write. The
spacing row is the other: with the wrong shift, `LCR` writes land on `IIR/FCR`
and the port is configured almost at random — with no error, because every
address in the range decodes to a real register. And leaving `DLAB` set after
programming the divisor means every character afterwards goes into the divisor
latch instead of being transmitted.

This split the console in two. `console_arm64.c` holds `uart_puts`,
`uart_puthex` and `uart_putdec`, written entirely in terms of a single
`uart_putc()`; the board profile picks which driver supplies that. A third
board no longer means a third copy of the formatting code, or a third place
for a formatting bug to be fixed in only two of them.

#### Testing a driver for hardware that cannot be emulated

No qemu machine models a Tegra, so the Jetson image cannot be booted here —
`--run` refuses rather than leaving a blank console to be misread as a hang.
That leaves a choice between testing nothing and testing what can be tested,
and the three failure modes above need no real hardware to catch.

[`tools/uart_8250_test.py`](tools/uart_8250_test.py) compiles the driver with
its base pointing at ordinary RAM, runs it under `-M virt` with the PL011 as
the real console, and checks the resulting register file: the divisor written
behind `DLAB`, `LCR` left at 8N1 with `DLAB` clear, the FIFOs enabled, and
nothing written past the offsets the 4-byte spacing implies. The fake registers
are pre-loaded with `LSR.THRE` set so the polling loops complete — which is
also what makes a polarity inversion visible, as it hangs instead.

It does **not** prove a real Tegra accepts the sequence, that the clock and
divisor suit the board, or that anything reaches a wire. It proves the driver
writes what it means to write, where it means to write it. All three mutations
above are caught.

### Interrupts on the Jetson: a GIC that has moved

The Tegra X1 has a GICv2, so unlike the Pi 3 it needs no new interrupt
controller at all — `gic_arm64.c`, `timer_arm64.S` and `irq_arm64.c` are
included for `jetson` unchanged, with only the two base addresses passed in.

The bases are two separate parameters, not a base plus a constant, and that is
the whole point:

| | distributor | CPU interface | gap |
|---|---|---|---|
| qemu virt | `0x08000000` | `0x08010000` | `0x10000` |
| Jetson (T210) | `0x50041000` | `0x50042000` | `0x1000` |

The gap is not architectural. A driver deriving the CPU interface as
`GICD_BASE + 0x10000` works perfectly on virt and, on a Jetson, puts every
CPU-interface write `0xF000` into the *distributor's* address space — where the
writes are accepted and quietly do something else. Nothing would report an
error; interrupts would simply never arrive.

[`tools/gic_base_test.py`](tools/gic_base_test.py) is built around exactly that
failure. It compiles the driver against two RAM regions **`0x1000` apart**, the
Tegra spacing, runs `gic_init` under `-M virt`, and checks both regions. If the
offset were assumed rather than passed, the CPU-interface region would stay
empty — so the test reports "nothing was written to the CPU-interface region at
all" and names the likely cause, rather than reporting a generic mismatch.

Same limits as the 8250 test: this proves the driver writes where the two
parameters say, not that a real Tegra GIC accepts the sequence or that an
interrupt is ever delivered on one.

### Interrupts on the Pi: no GIC at all

The BCM2837 is the one board here with no ARM interrupt controller. Broadcom's
arrangement predates that being a given, and it is split in two:

| | where | what |
|---|---|---|
| ARM local peripherals | `0x40000000` | per-core generic timer, mailboxes, GPU routing — added by the BCM2836 for multi-core |
| legacy controller | `0x3F00B200` | GPU peripherals (UART, DMA, system timer), inherited from the single-core BCM2835, no per-core notion |

The timer lives in the first, so that is what `bcm_irq_arm64.c` drives. Two
differences from a GIC shape it:

**No acknowledge and no end-of-interrupt.** A GIC hands you an id from `IAR`,
marks it active, and will not present another at that priority until that id
is written to `EOIR`. Here you read a *source bitmap* of what is currently
asserted, and the interrupt goes away only when the device itself is quieted —
for the timer, by rearming `CNTP_TVAL_EL0`, which `irq_arm64.c` already does
because a GIC's timer needs it too. So `intc_eoi()` is empty, and says why
rather than pretending to do something.

**No interrupt ids.** The source register is a bitmap, not a number, so
`intc_irq_of()` reports the timer as 30 — the id it carries on a GIC — to keep
`irq_arm64.c` free of controller knowledge. That is a deliberate fiction and
the only one in the file.

#### Which timer bit, and why it matters

The generic timer raises a different interrupt depending on the security
state: `CNTPSIRQ` when secure, `CNTPNSIRQ` when not. The boot stub sets
`SCR_EL3.NS` on the way down, so this image runs non-secure and the timer is
`CNTPNSIRQ` — bit 1.

Enabling the wrong one produces a timer that counts, asserts, and is never
routed to the core, with nothing to indicate why. Mutating the driver to use
the secure bit gives exactly that: **zero ticks even with interrupts
unmasked**, no error, no spurious count.

### The intc_* seam

Adding the Pi did not mean a second copy of the timer code. Only four call
sites in `irq_arm64.c` were controller-specific, so the GIC's entry points
were renamed to a neutral `intc_*` interface that `gic_arm64.c` and
`bcm_irq_arm64.c` both implement; the board profile selects one.

The check on whether that seam is real is that
[`tools/irq_timer_test.py`](tools/irq_timer_test.py) runs the *same program*
with the *same eleven assertions* on both bootable boards — 22 pass — across a
GICv2 and the BCM2837's ARM local peripherals. A seam that were not doing its
job would show up as the Pi needing its own copy of those checks.

`ISR_EL1` is what makes the masked case meaningful on both: it reflects the
core's interrupt signal whatever asserted it, so `ISR_EL1=0x80` with zero ticks
means *presented and blocked by `PSTATE.I`*, not *never delivered* — the
distinction a tick count alone cannot make.

### The first non-timer interrupt

The timer is the easy interrupt: private to the core, no routing, identical on
every board. A UART is a shared peripheral, and getting one to the core is
where boards actually diverge.

| | path to the core |
|---|---|
| virt | GIC **SPI 33**; the distributor is told to route it |
| Pi 3 | **GPU 57**: enable bit 25 of the legacy controller's second bank at `0x3F00B200`, then route the aggregate GPU signal to a core via the ARM local block at `0x40000000` |
| Jetson | GIC **INTID 68** (UART-A) — configured, never exercised |

The Pi is the instructive one: a GPU interrupt must pass **two** controllers,
and arrives as an undifferentiated "GPU" bit that `intc_acknowledge()` has to
decode a second time against the pending banks. Enabling only the legacy
controller leaves the interrupt asserted and invisible to every core —
`IRQ_SOURCE` stays zero, with no error anywhere. Mutating the routing register
reproduces exactly that: `ISR_EL1=0x0`, nothing received.

Two requirements are shared and neither is optional:

- **Enable receive *timeout*, not just receive.** A PL011's RX interrupt fires
  at the FIFO trigger level; a single keystroke never reaches it. Without
  `RTIM` the console appears dead until enough characters arrive at once. (The
  16550 folds this into one interrupt, so its driver needs one bit where the
  PL011 needs two.)
- **Drain the FIFO, don't read one byte.** The UART asserts while data
  remains, so a handler taking a single byte re-enters immediately and the
  machine livelocks. Mutating the drain loop to a single `if` gives `rx=1 of 9`
  on *both* boards — the same failure shape as forgetting to rearm the timer,
  from the opposite cause.

`make baremetal-echo` and `make baremetal-echo-raspi` run an interrupt-driven
echo with the timer ticking at the same time, so the two sources can be seen
sharing the vector.

#### Sending input to a guest without a race

[`tools/uart_rx_irq_test.py`](tools/uart_rx_irq_test.py) does not pipe input in
at launch. `uart_init()` clears `CR` to reconfigure the port, which flushes the
receive FIFO — so characters qemu delivered before that point are simply gone,
and the test fails perhaps one run in five with a driver that is entirely
correct. It waits for the guest to print `READY` and types afterwards. A flaky
test here would be worse than none: it teaches a reader to rerun rather than
believe the result.

### A second vector table: register-partitioned preemption

`vectors_preempt_arm64.S` is the vector table used by
`examples/baremetal/kernel_preempt.c`. It is identical to `vectors_arm64.S`
except in one slot: the EL1h IRQ entry (`VBAR_EL1 + 0x280`) branches to a timer
ISR **ShivyCX generated from a whole-program register partition**, rather than
to the generic `exc_common` path.

That is the point of the exercise. `exc_common` saves a fixed twenty-two
registers on every tick because it cannot know what the interrupted code was
using; the compiler does know, so the generated entry saves only the running
thread's footprint. Every other slot still routes to `exc_common` — a data
abort is a fault to report, not a thread switch.

`make baremetal-preempt` boots two threads that preempt each other:

```
  left=1227960000  right=2093684604  switches=50522  corrupt(l/r)=0/0
```

The mechanism, the measured save sets, and the caller-saved bug that took a
boot to find are documented in [SHIVYCX.md](SHIVYCX.md) under
*Register-partitioned threads*, and the paradigm and its benchmarks in
[BAREMETAL_THREADS.md](BAREMETAL_THREADS.md). Two pieces of this page are load-bearing for
it: `thread_launch` (which `eret`s into the first thread, so the first entry
and every later resume take the same route) and `irq_ack_timer` (the board hook
the ISR calls, since rearming `CNTP_TVAL` and the EOI protocol differ per
board).

### The exception level a board hands you

The boot stub descends from EL3, EL2 or EL1, whichever it is entered at,
because there is no way to know in advance:

| | entered at |
|---|---|
| `qemu -M virt` | **EL1** |
| `qemu -M raspi3b` | **EL3** |
| real Pi firmware (armstub) | EL2 |
| U-Boot | EL2, usually |

That virt enters at EL1 matters more than it looks: it means the descent code
**never ran** on virt. See below.

## A latent miscompile that only a real board could expose

`adr` was added to `rasm` with an `adr_prel_lo21` relocation kind — but nothing
taught `rasm_arch` or `rlink` what that kind meant. It fell through to the
*data* relocation path and became a `PREL32`: a plain 32-bit write that
overwrote the entire instruction word instead of splicing an immediate into it.
Every `adr` in the tree assembled to `udf`.

It survived three sessions of green tests. The only `adr` is in the boot stub's
exception-level descent, and virt enters at EL1, so `b.eq at_el1` jumped
straight past it. The first Pi boot executed it and died instantly, branching
to `0x200` with no vectors installed.

The test that should have caught it was mine, and it checked the wrong thing:
it verified the *unrelocated* instruction word and that a relocation of the
right name was attached. Neither says anything about whether anything
downstream knows how to apply that name. `rlink_script_test.py` now links a
relocation corpus through both toolchains and compares the disassembled result,
so an unapplied relocation shows up as different code rather than as a passing
test.

## Linker script support in rlink

A bare-metal image *is* its linker script — the load address, which section
leads, the 2 KiB-aligned vector section, and the symbols (`__bss_start`,
`__stack_top`, `__pgtbl_start`) the boot stub resolves against. `rlink` already
had partial script support for the x86 Multiboot path; what this needed was the
location counter used as a value rather than a constant.

The old parser evaluated `. = <number>` at *parse* time, which cannot express
anything depending on where the counter has reached. Expressions are now stored
as token lists and evaluated during layout, so `. = ALIGN(4096);` and
`. = . + 0x10000;` work — the second being how a stack or a page-table region
is reserved. Section attributes (`.vectors ALIGN(2048) :`), general
`sym = <expr>;`, `PROVIDE()`, `KEEP()` and an end-of-body `. = ALIGN(n);` came
with it. See [LINKER.md](tools/rpy_lib/LINKER.md) for the full list.

**File offsets are derived from addresses rather than accumulated.** Advancing
a file offset across a reserved region pads the output with zeros up to the
next section, since the writer fills to each section's offset. Deriving
`file_off = first_file_off + (addr - first_addr)` keeps `p_offset` congruent to
`p_vaddr` modulo the page size — which the kernel requires — while gaps cost
address space only. This kernel is **16 KiB from rlink against `ld`'s 83 KiB**,
for images that boot identically.

### A bug this exposed in the x86 path

rlink required `SHF_ALLOC` to place an input section. But `.section .multiboot`
in gas carries *no flags* unless the source spells them out, and `boot64.S`
does not — so rlink silently dropped the 12-byte Multiboot header, leaving
`_start` at the image's first byte and producing an image GRUB would refuse for
having no header in its first 8 KiB. This was pre-existing and affected every
rlink-produced x86 bare-metal image. A section a script explicitly names is now
placed whatever its flags; only linker bookkeeping (`.rela*`, symtab, strtab)
never is.

### Refusing what it cannot do

`MEMORY`, `AT(...)`, `SORT(...)`, `OVERLAY`, `PHDRS` and the
`MAX`/`MIN`/`ADDR`/`SIZEOF` expression functions all *move things*, so silently
ignoring one yields a layout that differs from what the script asked for — and
a bare-metal image whose sections are elsewhere fails at boot with nothing
pointing back at the script. Each is rejected with a message naming it.

## What is not done

- **Only the timer interrupt.** The GIC is initialised for shared peripheral
  interrupts too, but nothing routes the UART's, so console input is still
  polled. Nothing preempts: the handler runs to completion with interrupts
  masked, so there is no scheduler and no nesting.
- **Only the timer and UART receive.** No GPIO, DMA or transmit interrupts.
  The Jetson's UART interrupt id is configured but never exercised, since no
  qemu machine models a Tegra.
- **Secondary cores are parked.** The boot stub sends every core but the first
  to `wfi`; nothing uses the Pi's spin table or PSCI on virt to start them.
- **The Jetson is built but not booted.** No qemu machine models a Tegra, so
  its 16550 console and its relocated GICv2 are verified at register level
  only (see above), never against hardware. The interrupt path is the same
  code that is booted and exercised on virt; what is untested is that Tegra's
  addresses are right.
- **`raspi4`'s GIC-400 is not wired up.** The BCM2711 has one (GICD
  `0xFF841000`, GICC `0xFF842000`) and the parameterisation would cover it,
  but nothing here can exercise it, so it is left off rather than shipped
  untested.
- **`raspi4` is built but not booted.** No qemu machine models a BCM2711, so
  `--run` refuses rather than leaving a blank console to be misread as a hang.
- **Not run on physical hardware** — same caveat as [BOARDS.md](BOARDS.md).

## Files

- [`baremetal64/boot_arm64.S`](baremetal64/boot_arm64.S) — core parking, EL2→EL1, stack, `.bss`
- [`baremetal64/vectors_arm64.S`](baremetal64/vectors_arm64.S) — the 16-slot table and `exc_common`
- [`baremetal64/exc_arm64.c`](baremetal64/exc_arm64.c) — `ESR_EL1` decoding
- [`baremetal64/mmu_arm64.c`](baremetal64/mmu_arm64.c), [`mmu_enable_arm64.S`](baremetal64/mmu_enable_arm64.S) — page tables and turning them on
- [`baremetal64/gic_arm64.c`](baremetal64/gic_arm64.c) — GICv2 (virt, Jetson), behind the intc_* seam
- [`baremetal64/bcm_irq_arm64.c`](baremetal64/bcm_irq_arm64.c) — BCM2837 ARM local controller (Pi 3)
- [`baremetal64/timer_arm64.S`](baremetal64/timer_arm64.S), [`irq_arm64.c`](baremetal64/irq_arm64.c) — generic timer and IRQ dispatch
- [`baremetal64/console_arm64.c`](baremetal64/console_arm64.c) — board-independent formatting
- [`baremetal64/uart_arm64.c`](baremetal64/uart_arm64.c) — PL011 console (virt, Pi)
- [`baremetal64/uart_8250.c`](baremetal64/uart_8250.c) — Tegra 16550 console (Jetson)
- [`baremetal64/virt_arm64.ld`](baremetal64/virt_arm64.ld), [`raspi_arm64.ld`](baremetal64/raspi_arm64.ld), [`jetson_arm64.ld`](baremetal64/jetson_arm64.ld) — image layout per board
- [`baremetal64/irq_none_arm64.c`](baremetal64/irq_none_arm64.c) — IRQ stub for boards with no controller wired up
- [`baremetal64/vectors_preempt_arm64.S`](baremetal64/vectors_preempt_arm64.S) — vector table routing IRQ to a generated switcher
- [`examples/baremetal/kernel_preempt.c`](examples/baremetal/kernel_preempt.c) — two threads preempting each other
- [`tools/baremetal_arm64.py`](tools/baremetal_arm64.py) — build and run driver
- [`tools/rlink_script_test.py`](tools/rlink_script_test.py) — linker-script
  layout, differentially against `ld`
- [`tools/uart_8250_test.py`](tools/uart_8250_test.py) — Tegra 16550 console, register-level
- [`tools/gic_base_test.py`](tools/gic_base_test.py) — relocated GIC bases, register-level
- [`tools/irq_timer_test.py`](tools/irq_timer_test.py) — timer interrupts, booted on every board that can be
- [`tools/uart_rx_irq_test.py`](tools/uart_rx_irq_test.py) — UART receive interrupts, booted on every board that can be
- [`examples/baremetal/kernel_arm64.c`](examples/baremetal/kernel_arm64.c) — the example above
