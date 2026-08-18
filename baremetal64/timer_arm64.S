/* timer_arm64.S - the system-register half of the ARM generic timer.
 *
 * ShivyCX has no inline assembly, so everything touching a system register
 * lives here, as with mmu_enable_arm64.S. timer_arm64.c does the arithmetic.
 *
 * The generic timer is architectural: it is part of the CPU, not a device on
 * a bus, so it needs no device tree entry and no MMIO mapping. That makes it
 * the natural first interrupt source for a bare-metal image -- unlike the
 * UART, whose interrupt number depends on the board.
 */

.section .text

/* unsigned long timer_freq(void) -- ticks per second. Fixed by the board;
 * on virt it is 62.5 MHz, on a Raspberry Pi 4 it is 54 MHz. Reading it rather
 * than assuming is what makes the same code work on both. */
.global timer_freq
timer_freq:
    mrs     x0, cntfrq_el0
    ret

/* unsigned long timer_count(void) -- the raw counter, monotonic since reset. */
.global timer_count
timer_count:
    isb                             /* the read may be speculated otherwise */
    mrs     x0, cntpct_el0
    ret

/* void timer_set_tval(unsigned long ticks)
 *
 * TVAL is a *countdown*: the hardware decrements it every tick and raises the
 * interrupt when it goes negative. Writing it again inside the handler is how
 * a periodic tick is produced -- there is no auto-reload. */
.global timer_set_tval
timer_set_tval:
    msr     cntp_tval_el0, x0
    isb
    ret

/* void timer_enable(void) -- CNTP_CTL_EL0.ENABLE, IMASK clear. */
.global timer_enable
timer_enable:
    mov     x0, #1
    msr     cntp_ctl_el0, x0
    isb
    ret

/* void timer_disable(void) */
.global timer_disable
timer_disable:
    msr     cntp_ctl_el0, xzr
    isb
    ret

/* unsigned long timer_ctl(void) -- ENABLE(0), IMASK(1), ISTATUS(2).
 * ISTATUS reads 1 while the condition is met, which is the direct way to see
 * a timer that is firing even if the GIC is not delivering it. */
.global timer_ctl
timer_ctl:
    mrs     x0, cntp_ctl_el0
    ret

/* void irq_enable(void) -- unmask IRQs at the PSTATE level.
 *
 * This is the last gate. An interrupt has to get past three: the timer's own
 * IMASK, the GIC (distributor enable, CPU interface enable, priority mask),
 * and PSTATE.I here. Boot masked everything via SPSR_EL2 = 0x3C5, so nothing
 * arrives until this runs -- which is deliberate, since the vectors are not
 * installed at that point. */
.global irq_enable
irq_enable:
    msr     daifclr, #2
    ret

/* void irq_disable(void) */
.global irq_disable
irq_disable:
    msr     daifset, #2
    ret

/* unsigned long irq_pending(void) -- ISR_EL1, so a test can see a pending
 * interrupt without having to take it. */
.global irq_pending
irq_pending:
    mrs     x0, isr_el1
    ret
