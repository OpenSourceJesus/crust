/* bcm_irq_arm64.c - interrupt controller for the BCM2836/2837 (Raspberry Pi
 * 2 and 3), implementing the same intc_* seam as gic_arm64.c.
 *
 * The Pi is the one board here with no GIC. Broadcom's arrangement predates
 * the ARM interrupt controller being a given, and it is split in two:
 *
 *   ARM local peripherals, 0x40000000
 *       Added by the BCM2836 when the Pi went multi-core. Per-core registers
 *       for the generic timer, mailboxes, and which core a GPU interrupt goes
 *       to. This is where the timer lives, so it is what this file mostly
 *       drives.
 *
 *   legacy interrupt controller, 0x3F00B200
 *       Inherited from the single-core BCM2835. Three enable registers and
 *       three pending registers covering the GPU peripherals (UART, DMA,
 *       system timer, and so on), with no per-core notion at all.
 *
 * Two differences from a GIC shape the code below.
 *
 * **There is no acknowledge, and no end-of-interrupt.** A GIC hands you an id
 * from IAR, moves it to "active", and expects that id written back to EOIR
 * before it will present another at that priority. Here you read a *source
 * bitmap* saying what is currently asserted, and the interrupt goes away only
 * when you quiet the device itself. For the timer that means rearming
 * CNTP_TVAL_EL0 -- which irq_arm64.c already does, because a GIC's timer needs
 * it too. So intc_eoi() has nothing to do, and says so rather than pretending.
 *
 * **There are no interrupt ids.** The source register is a bitmap, not a
 * number. intc_irq_of() therefore reports the timer as 30 -- the id it has on
 * a GIC -- so that irq_arm64.c stays free of controller knowledge. That is a
 * deliberate fiction, and the only one in this file.
 *
 * Not implemented: the legacy controller's GPU sources. Nothing here enables
 * a peripheral interrupt yet, so the registers are defined and left alone
 * rather than half-driven.
 */

/* ARM local peripherals. Note this is a *different* base from the SoC's
 * peripheral block: it sits at 0x40000000 on both the BCM2836 and BCM2837 and
 * does not move with the peripheral base the way the UART and GPIO do. */
#ifndef BCM_LOCAL_BASE
#define BCM_LOCAL_BASE      0x40000000
#endif

/* Legacy (GPU) interrupt controller, inside the peripheral block. */
#ifndef BCM_IRQ_BASE
#define BCM_IRQ_BASE        0x3F00B200
#endif

/* Legacy controller registers, offsets from BCM_IRQ_BASE. The two banks cover
 * GPU interrupts 0-31 and 32-63; "basic" is a handful of ARM-side sources
 * plus summary bits. */
#define IRQ_BASIC_PENDING   0x00
#define IRQ_PENDING1        0x04    /* GPU 0-31 */
#define IRQ_PENDING2        0x08    /* GPU 32-63 */
#define IRQ_FIQ_CONTROL     0x0C
#define IRQ_ENABLE1         0x10    /* GPU 0-31 */
#define IRQ_ENABLE2         0x14    /* GPU 32-63 */
#define IRQ_ENABLE_BASIC    0x18
#define IRQ_DISABLE1        0x1C
#define IRQ_DISABLE2        0x20
#define IRQ_DISABLE_BASIC   0x24

/* The PL011 is GPU interrupt 57, i.e. bit 25 of the second bank. (The
 * mini-UART is 29, in the first bank -- a different device on the same pins,
 * and a common source of confusion.) */
#define GPU_IRQ_UART        57

/* Logical id reported for the UART, matching the GIC's SPI 33 on virt so that
 * irq_arm64.c needs no board-specific interrupt number. Same convention as
 * TIMER_IRQ_ID below. */
#ifndef UART_IRQ
#define UART_IRQ            33
#endif
#define UART_IRQ_ID         UART_IRQ

static volatile unsigned int *irq_reg(unsigned long off)
{
    return (volatile unsigned int *)(BCM_IRQ_BASE + off);
}

/* ARM local registers, offsets from BCM_LOCAL_BASE. */
#define LOCAL_CONTROL       0x000
#define LOCAL_PRESCALER     0x008
#define LOCAL_GPU_ROUTING   0x00C   /* which core GPU IRQ/FIQ goes to */
#define LOCAL_TIMER_IRQCNTL 0x040   /* per core: +4 each */
#define LOCAL_MAILBOX_IRQ   0x050
#define LOCAL_IRQ_SOURCE    0x060   /* per core: +4 each */
#define LOCAL_FIQ_SOURCE    0x070

/* Bits in both TIMER_IRQCNTL and IRQ_SOURCE. The four timers of the generic
 * timer each get a bit; in IRQCNTL the low four enable them as IRQs and bits
 * 4-7 as FIQs instead.
 *
 * Which one the EL1 physical timer appears as depends on the security state:
 * secure EL1 raises CNTPSIRQ, non-secure raises CNTPNSIRQ. The boot stub sets
 * SCR_EL3.NS on the way down, so this image runs non-secure and the timer is
 * CNTPNSIRQ. Enabling the wrong one produces a timer that counts, asserts,
 * and is never routed to the core -- with nothing to indicate why.
 */
#define TIMER_CNTPSIRQ      0x01    /* secure physical */
#define TIMER_CNTPNSIRQ     0x02    /* non-secure physical  <- ours */
#define TIMER_CNTHPIRQ      0x04    /* hypervisor */
#define TIMER_CNTVIRQ       0x08    /* virtual */

/* IRQ_SOURCE also reports non-timer sources. */
#define SOURCE_MAILBOX0     0x10
#define SOURCE_GPU          0x100   /* something from the legacy controller */
#define SOURCE_PMU          0x200

/* The logical id reported for the timer, matching the GIC's PPI 30 so that
 * irq_arm64.c needs no board knowledge. */
#define TIMER_IRQ_ID        30
#define SPURIOUS_ID         1023

static volatile unsigned int *local_reg(unsigned long off)
{
    return (volatile unsigned int *)(BCM_LOCAL_BASE + off);
}

/* Which core we are. The Pi starts all four at the entry point and the boot
 * stub parks all but core 0, so this is 0 in practice -- but the timer
 * registers are genuinely per core, and hardcoding 0 would be a trap for
 * whoever brings up the others. */
static int core_id(void)
{
    return 0;
}

void intc_init(void)
{
    /* Route the local timer to the core rather than to a FIQ, and give it no
     * prescaling: the generic timer is clocked from the crystal and its
     * frequency is what CNTFRQ_EL0 reports. Writing a prescaler here would
     * make CNTFRQ a lie. */
    *local_reg(LOCAL_CONTROL) = 0;
    *local_reg(LOCAL_PRESCALER) = 0x80000000;

    /* Nothing enabled yet; intc_enable_irq turns the timer on. */
    *local_reg(LOCAL_TIMER_IRQCNTL + (unsigned long)(core_id() * 4)) = 0;
}

/* A GIC needs per-core CPU-interface setup; this controller has none, since
 * the per-core registers *are* the interface. */
void intc_init_cpu(void)
{
}

void intc_enable_irq(int irq)
{
    unsigned long off = LOCAL_TIMER_IRQCNTL + (unsigned long)(core_id() * 4);
    if (irq == TIMER_IRQ_ID) {
        unsigned int v = *local_reg(off);
        *local_reg(off) = v | TIMER_CNTPNSIRQ;
        return;
    }
    if (irq == UART_IRQ_ID) {
        /* A GPU interrupt has to pass *two* controllers on this board, which
         * is the whole difference from the timer. The legacy controller
         * decides whether GPU 57 is asserted at all; the ARM local block
         * decides which core sees the aggregate GPU signal. Enabling only the
         * first leaves the interrupt asserted at the legacy controller and
         * invisible to every core -- no error, and IRQ_SOURCE stays zero. */
        *irq_reg(IRQ_ENABLE2) = 1u << (GPU_IRQ_UART - 32);
        /* Route GPU IRQ (low 2 bits) and GPU FIQ (bits 2-3) to core 0. */
        *local_reg(LOCAL_GPU_ROUTING) = 0;
    }
}

void intc_disable_irq(int irq)
{
    unsigned long off = LOCAL_TIMER_IRQCNTL + (unsigned long)(core_id() * 4);
    if (irq == TIMER_IRQ_ID) {
        unsigned int v = *local_reg(off);
        *local_reg(off) = v & ~(unsigned int)TIMER_CNTPNSIRQ;
        return;
    }
    if (irq == UART_IRQ_ID) {
        *irq_reg(IRQ_DISABLE2) = 1u << (GPU_IRQ_UART - 32);
    }
}

/* No priorities on this controller. */
void intc_set_priority(int irq, int prio)
{
}

int intc_num_irqs(void)
{
    return 32;
}

/* "Acknowledge" is a read of the source bitmap. Nothing changes state, which
 * is why an interrupt left unhandled here re-enters immediately rather than
 * being held active as a GIC would. */
int intc_acknowledge(void)
{
    unsigned int src =
        *local_reg(LOCAL_IRQ_SOURCE + (unsigned long)(core_id() * 4));
    if (src & TIMER_CNTPNSIRQ) {
        return TIMER_IRQ_ID;
    }
    if (src & SOURCE_GPU) {
        /* The local block only says "some GPU interrupt"; which one comes
         * from the legacy controller's pending banks. This is the second
         * level of decode a GIC does not have. */
        if (*irq_reg(IRQ_PENDING2) & (1u << (GPU_IRQ_UART - 32))) {
            return UART_IRQ_ID;
        }
        return (int)(*irq_reg(IRQ_PENDING2) & 0xFF) + 256;
    }
    if (src == 0) {
        return SPURIOUS_ID;
    }
    /* Something real but not ours: report the raw bitmap, offset clear of the
     * timer id so irq_arm64.c counts it as unexpected rather than as a tick. */
    return (int)(src & 0xFF) + 256;
}

int intc_is_spurious(int id)
{
    return id == SPURIOUS_ID;
}

int intc_irq_of(int id)
{
    return id;
}

/* Nothing to do: the interrupt is deasserted by quieting the device, which
 * for the timer means rearming CNTP_TVAL_EL0. irq_arm64.c does that already,
 * because a GIC's timer needs it too. */
void intc_eoi(int id)
{
}

/* The raw source bitmap, for tests that want to see the controller state
 * directly rather than infer it from a tick count. */
unsigned long intc_raw_source(void)
{
    return (unsigned long)
        *local_reg(LOCAL_IRQ_SOURCE + (unsigned long)(core_id() * 4));
}

unsigned long intc_raw_pending2(void)
{
    return (unsigned long)*irq_reg(IRQ_PENDING2);
}

unsigned long intc_raw_timer_control(void)
{
    return (unsigned long)
        *local_reg(LOCAL_TIMER_IRQCNTL + (unsigned long)(core_id() * 4));
}
