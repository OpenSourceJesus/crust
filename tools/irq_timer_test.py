#!/usr/bin/env python3
"""Boot-level test for the AArch64 GIC and generic timer.

An interrupt has to pass five gates before it is taken, and every one of them
is silent when shut:

    CNTP_CTL_EL0.ENABLE=1, IMASK=0      the timer asserts
    GICD_ISENABLER bit 30               the distributor forwards it
    GICC_CTLR=1, GICC_PMR > priority    the CPU interface presents it
    PSTATE.I clear                      the core accepts it
    VBAR_EL1 + 0x280                    the IRQ vector runs

A machine with any one of them wrong does not fail -- it simply never takes an
interrupt, with nothing to say which. So this test does not just check that
ticks happen; it checks that they happen *because* of the interrupt path, by
toggling PSTATE.I and requiring the tick count to follow:

    masked -> no ticks, but ISR_EL1 nonzero (pending, not taken)
    unmasked -> ticks at the programmed rate
    masked again -> ticks stop

The middle observation is the load-bearing one. Ticks appearing while masked
would mean something other than the vector table is advancing the counter, and
ISR_EL1 reading zero while masked would mean the GIC never presented anything
-- so the later ticks would prove nothing about the controller.

    python3 tools/irq_timer_test.py
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

HZ = 100

PROGRAM = r"""
void uart_init(void);
void uart_puts(char *s);
void uart_putdec(long v);
void uart_puthex(unsigned long v);

void irq_init(void);
void irq_enable(void);
void irq_disable(void);
void timer_start(int hz);

void mmu_init(void);
void mmu_enable(void);

unsigned long ticks(void);
unsigned long spurious(void);
unsigned long unexpected(void);
unsigned long timer_count(void);
unsigned long timer_freq(void);
unsigned long timer_ctl(void);
unsigned long irq_pending(void);

void exc_expect(int n);
int exc_taken(void);

static void wait_ms(unsigned long ms)
{
    unsigned long f = timer_freq();
    unsigned long t = timer_count() + (f / 1000UL) * ms;
    while (timer_count() < t) {
    }
}

void kmain(void)
{
    unsigned long t0, t1, t2, t3;

    uart_init();
    uart_puts("BEGIN\n");

    uart_puts("freq="); uart_putdec((long)timer_freq()); uart_puts("\n");

    irq_init();
    timer_start(%(hz)d);

    /* Gate check: the timer is running but PSTATE.I is still set. */
    wait_ms(200);
    t0 = ticks();
    uart_puts("masked_ticks="); uart_putdec((long)t0); uart_puts("\n");
    uart_puts("isr_masked="); uart_puthex(irq_pending()); uart_puts("\n");
    uart_puts("ctl="); uart_puthex(timer_ctl()); uart_puts("\n");

    irq_enable();
    wait_ms(500);
    t1 = ticks();
    uart_puts("live_ticks="); uart_putdec((long)(t1 - t0)); uart_puts("\n");

    irq_disable();
    wait_ms(200);
    t2 = ticks();
    uart_puts("remasked_ticks="); uart_putdec((long)(t2 - t1)); uart_puts("\n");

    /* Interrupts must survive translation coming on, and must coexist with
     * synchronous faults without either counter contaminating the other. */
    irq_enable();
    mmu_init();
    mmu_enable();
    wait_ms(300);
    t3 = ticks();
    uart_puts("mmu_ticks="); uart_putdec((long)(t3 - t2)); uart_puts("\n");

    exc_expect(1);
    {
        volatile unsigned long *bad = (volatile unsigned long *)0x800000000UL;
        *bad = 1;
    }
    uart_puts("faults="); uart_putdec((long)exc_taken()); uart_puts("\n");

    wait_ms(200);
    uart_puts("after_fault_ticks="); uart_putdec((long)(ticks() - t3));
    uart_puts("\n");

    uart_puts("spurious="); uart_putdec((long)spurious()); uart_puts("\n");
    uart_puts("unexpected="); uart_putdec((long)unexpected()); uart_puts("\n");
    irq_disable();
    uart_puts("END\n");
}
""" % {"hz": HZ}


def fields(text):
    out = {}
    for m in re.finditer(r"^(\w+)=(\S+)", text.replace("\r", ""), re.M):
        out[m.group(1)] = m.group(2)
    return out


def as_int(v):
    if v is None:
        return None
    try:
        return int(v, 16) if v.startswith("0x") else int(v)
    except ValueError:
        return None


def run_board(board):
    """Build and run the interrupt program for one board, and check it.

    Both bootable boards run the *same* program: the interrupt controllers are
    entirely different -- a GICv2 on virt, the BCM2837's ARM local peripherals
    on the Pi -- but that difference is behind the intc_* seam, so the timer
    policy, the tick counting and every check below are shared. If a board
    needed its own copy of these assertions, the seam would not be doing its
    job.
    """
    import baremetal_arm64 as bm

    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "irqprog.c")
        with open(src, "w") as f:
            f.write(PROGRAM)
        elf = os.path.join(d, "irq.elf")
        try:
            bm.build([src], elf, os.path.join(d, "obj"), board=board)
        except Exception as e:
            print("  FAIL  [%s] build: %s" % (board, e))
            return 0, 1
        prof = bm.BOARDS[board]
        out = bm.qemu_run(elf, machine=prof["machine"], cpu=prof["cpu"],
                          timeout=90)

    f = fields(out)
    checks = []

    if "END" not in out.replace("\r", ""):
        print("  FAIL  [%s] program did not run to completion" % board)
        print("        last output: %r" % out.replace("\r", "")[-200:])
        return 0, 1

    freq = as_int(f.get("freq"))
    checks.append(("timer frequency is plausible",
                   freq is not None and freq > 1000000,
                   "freq=%s" % f.get("freq")))

    # The timer must be enabled and unmasked before any of the rest means
    # anything: ENABLE is bit 0, IMASK bit 1.
    ctl = as_int(f.get("ctl"))
    checks.append(("timer enabled and unmasked",
                   ctl is not None and (ctl & 1) == 1 and (ctl & 2) == 0,
                   "CNTP_CTL=%s" % f.get("ctl")))

    masked = as_int(f.get("masked_ticks"))
    checks.append(("no ticks while PSTATE.I is set",
                   masked == 0, "masked_ticks=%s" % f.get("masked_ticks")))

    # Pending at the CPU interface but not taken. Zero here would mean the GIC
    # never presented the interrupt, so later ticks would prove nothing.
    isr = as_int(f.get("isr_masked"))
    checks.append(("controller presents the interrupt while masked",
                   isr is not None and isr != 0,
                   "ISR_EL1=%s" % f.get("isr_masked")))

    # 500ms at 100Hz is ~50 ticks; allow for emulation jitter and the
    # busy-wait's own overhead.
    live = as_int(f.get("live_ticks"))
    want = HZ // 2
    checks.append(("ticks arrive at roughly the programmed rate",
                   live is not None and want * 0.5 <= live <= want * 1.6,
                   "live_ticks=%s (expected ~%d)" % (f.get("live_ticks"), want)))

    remask = as_int(f.get("remasked_ticks"))
    checks.append(("ticks stop when PSTATE.I is set again",
                   remask == 0, "remasked_ticks=%s" % f.get("remasked_ticks")))

    mmu_ticks = as_int(f.get("mmu_ticks"))
    checks.append(("interrupts survive MMU enable",
                   mmu_ticks is not None and mmu_ticks > 0,
                   "mmu_ticks=%s" % f.get("mmu_ticks")))

    checks.append(("a synchronous fault is still handled",
                   as_int(f.get("faults")) == 1,
                   "faults=%s" % f.get("faults")))

    after = as_int(f.get("after_fault_ticks"))
    checks.append(("ticks continue after a fault",
                   after is not None and after > 0,
                   "after_fault_ticks=%s" % f.get("after_fault_ticks")))

    checks.append(("no spurious interrupts",
                   as_int(f.get("spurious")) == 0,
                   "spurious=%s" % f.get("spurious")))
    checks.append(("no interrupts from unexpected sources",
                   as_int(f.get("unexpected")) == 0,
                   "unexpected=%s" % f.get("unexpected")))

    npass = nfail = 0
    for name, ok, detail in checks:
        if ok:
            print("  PASS  [%-6s] %-38s %s" % (board, name, detail))
            npass += 1
        else:
            print("  FAIL  [%-6s] %-38s %s" % (board, name, detail))
            nfail += 1

    return npass, nfail


def main():
    import baremetal_arm64 as bm

    if subprocess.run(["which", "qemu-system-aarch64"],
                      capture_output=True).returncode != 0:
        print("SKIP: qemu-system-aarch64 not installed")
        return 0

    # Every board with an interrupt controller that can actually be booted
    # here. jetson and raspi4 have controllers too, but no qemu machine models
    # them -- they are covered at register level by gic_base_test.py instead.
    boards = [b for b in sorted(bm.BOARDS)
              if bm.BOARDS[b]["irq"] and bm.BOARDS[b]["machine"]]
    if not boards:
        print("SKIP: no bootable board with an interrupt controller")
        return 0

    total_pass = total_fail = 0
    for board in boards:
        p, f = run_board(board)
        total_pass += p
        total_fail += f

    print("\narm64 interrupts across %d boards: %d pass, %d fail"
          % (len(boards), total_pass, total_fail))
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
