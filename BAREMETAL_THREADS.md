# Bare-metal threads with a partitioned register file

A two-thread program pinned to one core, where the compiler splits the register
file between the threads so a context switch only has to save the registers
actually in use.

The idea is narrow on purpose. It does not generalise to arbitrary threads on
arbitrary cores, and it is not a scheduler. What it is: a paradigm where you
accept two restrictions — **one core, two threads, both known at compile time**
— and get, in exchange, a context switch that costs what your program actually
uses rather than what the architecture might.

The compiler machinery is described in [SHIVYCX.md](SHIVYCX.md) under
*Register-partitioned threads*; the bare-metal side is in
[BAREMETAL_ARM64.md](BAREMETAL_ARM64.md). This page is about why the two
together are interesting, and what they measure.

## The paradigm

```c
void io_thread(void);
void compute_thread(void);

int main()
assert io_thread in threads.left( core=0 )
assert compute_thread in threads.right( core=0 )
{ io_thread(); compute_thread(); return 0; }
```

Two declarations are the whole interface. From them ShivyCX computes each
thread's transitive register footprint from the whole-program call graph,
splits the allocatable registers into disjoint `left` and `right` budgets,
re-runs allocation constrained to each, and emits a context switcher that saves
only the bank in use.

Both threads are pinned to one core, so a switch is a register swap and nothing
else — no cache migration, no cross-core synchronisation, no lock. That is what
makes the register-file split the dominant cost worth attacking.

## Why AArch64 and why IO

**AArch64 has room to split.** ShivyCX's AArch64 allocator draws value homes
from the callee-saved range **x19–x28** — ten registers, none of them argument
or scratch. A two-way split leaves five a side. On x86-64 the pool overlaps the
ABI's argument registers and is far tighter.

**IO is the case with something to split.** A value only needs a callee-saved
home if it must survive a call. A tight arithmetic loop calls nothing, so
almost everything lives in caller-saved scratch and the partition has nearly
nothing to work with. A formatted write is the opposite: it walks a call
graph — format, digit conversion, per-character output, a busy-wait on the
transmit FIFO — and every value crossing one of those calls needs a home in
x19–x28.

This is worth stating because it inverts the x86 intuition. `bench_threads.c`
uses call-free bodies deliberately, "so the register partition fully controls
each thread's footprint". On AArch64 that guarantees a *tiny* footprint
instead: measured, five live values per side still produced a one-register
footprint, and tightening the budget from three registers to one cost nothing
at all.

## What it does to a printf-shaped workload

[`benchmarks/threads/bench_printf.c`](benchmarks/threads/bench_printf.c) puts a
formatted-output thread against a compute thread, using the image's real UART
path — `uart_puts` walking a string, `uart_putc` polling the PL011's transmit
FIFO — rather than a stand-in.

```
thread io_thread     : side=left  (call graph: 7 fns)
thread compute_thread: side=right (call graph: 1 fn)

left  GP footprint : x19, x20, x21, x22, x23, x24
right GP footprint : (none)
footprints are disjoint: left and right share no registers
```

The IO thread's seven-function call graph forces six callee-saved homes. The
compute thread's single leaf function forces **none**. They are disjoint
without the allocator having to work for it, because the two sides want
different things.

The cooperative switcher that falls out:

```asm
switch_to_right:              /* leaving IO for compute */
    stp x19, x20, [x0, #16]
    stp x21, x22, [x0, #32]
    stp x23, x24, [x0, #48]
    mov x9, sp
    str x9,  [x0, #0]
    str x30, [x0, #8]
    ldr x9,  [x1, #0]         /* compute needs no registers restored */
    mov sp, x9
    ldr x30, [x1, #8]
    ret

switch_to_left:               /* leaving compute for IO */
    mov x9, sp                /* nothing to save: its footprint is empty */
    str x9,  [x0, #0]
    str x30, [x0, #8]
    ldp x19, x20, [x1, #16]
    ldp x21, x22, [x1, #32]
    ldp x23, x24, [x1, #48]
    ...
```

Switching *away from* the compute thread saves nothing at all. A switcher
without partition information would save all ten of x19–x28 in both directions,
because it cannot know which are live.

## The preemptive path costs more, necessarily

A cooperative switch is a **call**, so caller-saved registers are already dead
across it by the ABI and preserving x19–x28 is enough. A timer interrupt lands
at an arbitrary instruction, where x0–x18 hold live values too, so the ISR must
save the thread's **full** footprint.

| | cooperative | preemptive |
|---|---|---|
| IO thread | 6 registers | 16 |
| compute thread | 0 registers | 7 |
| no partition | 10 | 31 |

Getting this wrong is not a tuning error. Saving the cooperative set on a timer
tick corrupts the interrupted thread intermittently, depending where the tick
lands — which is close to the worst failure mode available.

## Measured

Two threads preempting each other on `qemu-system-aarch64 -M virt`, generated
switcher installed in the EL1h IRQ vector slot
([`make baremetal-preempt`](Makefile)):

```
left=1227960000  right=2093684604  switches=50522  corrupt(l/r)=0/0
```

Fifty thousand preemptions, and the number that matters is the last one. Each
worker re-derives an arithmetic invariant every iteration, so a live register
the switcher failed to preserve shows up as a mismatch. **Zero across fifty
thousand switches is the evidence that saving only the running group's
footprint is sufficient.**

### Switch cost

`tools/bench_preempt.py` builds the same kernel twice, changing only which
switcher sits in the vector slot, and compares.

| | instructions |
|---|---|
| partitioned ISR | **60** |
| save-all ISR | **75** |
| difference | 15 (20% smaller) |

On the printf workload the same comparison gives 59 instructions leaving the IO
thread and 58 leaving the compute thread, against 75 — **21% smaller**. (Both
directions are similar because an ISR saves the outgoing footprint *and*
restores the incoming one, so each handles the same 23 registers in total.)

Throughput, under `qemu -icount shift=0` so guest time is deterministic:

| tick rate | partitioned | save-all | delta |
|---|---|---|---|
| 8 MHz | 10.00 cycles/switch | 11.00 | **9%** |
| 1 MHz | 65.07 | 66.09 | 1.5% |
| 20 kHz | — | — | none measurable |
| 1 kHz | 569.8M work | 572.0M work | none (0.4% *against*) |

The instruction saving is the same at every rate; what changes is how much work
sits between switches. At 1 kHz the threads run 570 million iterations against
four thousand switches and the switch cost vanishes into rounding.

**So the honest claim is: a 20% smaller switch, worth ~9% of throughput at
8 MHz, ~1.5% at 1 MHz, and nothing below about 20 kHz.** Whether that is
interesting depends entirely on how often you switch.

## Three ways the measurement was wrong first

Recorded because each cost real time and none was obvious.

**The baseline has to be correct, not merely generic.** The natural comparison
is `exc_common` in `vectors_arm64.S`, which saves twenty-two registers. But it
never saves x20–x28, and that is exactly where ShivyCX puts value homes — as a
thread switcher it would silently corrupt state. Comparing against it would
have credited the partition with nine registers it was never entitled to skip.
The honest baseline for "no partition information" is all 31.

**qemu must run under `-icount`.** Without it `CNTPCT` follows *host* wall
time, so two runs at different host loads are not comparable. Measured that
way, the partitioned build looked **1.64× faster** at 20 kHz — and
simultaneously reported *more* cycles per switch, a contradiction that was the
only clue the metric was junk. Under `-icount shift=0` the two came out
identical to 0.001%. The whole 1.64× was noise.

**The benchmark then found a real bug in the emitter.** With a trustworthy
clock the gap was five instructions (70 vs 75), because the save-all baseline
used `stp`/`ldp` pairs while the generated ISR emitted individual `str`/`ldr`.
Thirteen registers unpaired costs thirteen instructions; thirty-one paired
costs sixteen. The register-count win was almost entirely forfeited by
encoding. TCB slots are index-assigned, so consecutive entries are always eight
bytes apart and always pairable — adding pairing took the ISR from 70 to 60 and
turned a non-result into a real one.

## A bug only booting could find

The generated switcher passed every static check — correct save sets, balanced
scratch frames, `ELR` and `SPSR` handled, assembling under both GNU `as` and
our own `rasm`, twenty-three tests including mutations — and could not switch
twice.

`bl timer_ack` clobbers `x9`. It is caller-saved, so the callee is entitled to
destroy it, and does. The cur/next exchange immediately after read `x9` as the
outgoing TCB pointer, so `next_tcb` received garbage and the *second* switch
resumed from a corrupt TCB: `eret` landed at EL0 on a misaligned PC, followed
by an exception storm.

Nothing about that is visible in the emitted text. `qemu -d int` settled it in
one run — two IRQs, then a Prefetch Abort from EL0 with `ESR` EC `0x22`, which
points at a bad `ELR`/`SPSR` pair and therefore at the TCB pointer.

Two diagnostics misled on the way, both worth knowing: `ISR_EL1` reads zero
when interrupts are *unmasked*, because an asserted interrupt is taken rather
than left pending, so it says nothing there; and a flood of stray console
output looks exactly like too-few-registers-saved but was not, since the tick
counter proved the handler never completed.

## Limits

- **Two threads, one core.** The partition is two-way by construction. Nothing
  here schedules more than two contexts or touches a second core.
- **The ISR alternates.** It flips `timer_vector` between the two sides rather
  than consulting a run queue. That is a switcher, not a scheduler.
- **qemu only.** Every number here is emulated. The instruction counts are
  exact — they are a property of the emitted code — but the throughput figures
  are qemu's, and no image has run on physical hardware.
- **The partition can fail to be disjoint**, and says so rather than pretending
  otherwise. `bench_threads_calls.c` puts two call-heavy threads against each
  other; they want eight of the ten homes each and cannot be split. The
  switcher stays correct — it saves the outgoing footprint and restores the
  incoming one however they overlap — just larger.

## Files

| | |
|---|---|
| [`benchmarks/threads/bench_printf.c`](benchmarks/threads/bench_printf.c) | IO thread vs compute thread, real UART call graph |
| [`benchmarks/threads/bench_threads.c`](benchmarks/threads/bench_threads.c) | call-free bodies; the case with little to partition |
| [`benchmarks/threads/bench_threads_calls.c`](benchmarks/threads/bench_threads_calls.c) | two call-heavy threads that cannot be split |
| [`benchmarks/threads/kernel_preempt_bench.c`](benchmarks/threads/kernel_preempt_bench.c) | the throughput benchmark, `TICK_HZ`-parameterised |
| [`baremetal64/switcher_full_arm64.S`](baremetal64/switcher_full_arm64.S) | save-all baseline |
| [`baremetal64/vectors_preempt_arm64.S`](baremetal64/vectors_preempt_arm64.S) | vector table routing IRQ to the generated switcher |
| [`examples/baremetal/kernel_preempt.c`](examples/baremetal/kernel_preempt.c) | two threads preempting, correctness demo |
| [`tools/bench_preempt.py`](tools/bench_preempt.py) | `--build` / `--measure` comparison harness |
| [`tools/thread_partition_test.py`](tools/thread_partition_test.py) | 31 checks, including a booted preemption test |
