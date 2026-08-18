/* gic_arm64.c - GICv2 interrupt controller for `qemu-system-aarch64 -M virt`.
 *
 * This is the AArch64 replacement for minikraft's 8259 PIC. The two solve the
 * same problem and share almost no structure:
 *
 *   - the PIC is programmed through two I/O ports with a fixed initialisation
 *     word sequence; the GIC is memory-mapped and register-per-function
 *   - the PIC has 15 usable lines; the GIC has up to 1020 interrupt IDs
 *   - the PIC needs an EOI to *it*; the GIC splits acknowledge and EOI, and
 *     the value read from the acknowledge register is the one that must be
 *     written back
 *
 * A GICv2 has two halves. The **distributor** (GICD) is global: it decides
 * which interrupts exist, their priority, and which CPU each targets. The
 * **CPU interface** (GICC) is per-core: it is what actually presents an
 * interrupt to this core, and what acknowledges and ends it.
 *
 * Interrupt IDs are split by origin:
 *   0-15    SGI, software-generated (inter-processor)
 *   16-31   PPI, private peripheral -- one per core. The EL1 physical timer
 *           is PPI 30, which is why it needs no device tree to find.
 *   32+     SPI, shared peripheral -- the UART and everything else
 *
 * Compiled by ShivyCX; every access is through a volatile pointer so the
 * register writes are not reordered or elided.
 */

/* Where the two blocks live is board-specific; everything below is not. A
 * GICv2 is a GICv2, so the same driver serves any board that has one and only
 * the bases move:
 *
 *   qemu virt      GICD 0x08000000   GICC 0x08010000
 *   Jetson (T210)  GICD 0x50041000   GICC 0x50042000
 *   Pi 4 (BCM2711) GICD 0xFF841000   GICC 0xFF842000
 *
 * The Pi 3 has no GIC at all -- see irq_none_arm64.c.
 *
 * The distributor-to-CPU-interface offset is not fixed by the architecture
 * (it is 0x10000 on virt and 0x1000 on Tegra), so the two are separate
 * parameters rather than a base plus a constant. Assuming a fixed offset
 * would put every CPU-interface write somewhere in the distributor's address
 * space, where it would be accepted and do something else entirely.
 */
#ifndef GICD_BASE
#define GICD_BASE   0x08000000
#endif
#ifndef GICC_BASE
#define GICC_BASE   0x08010000
#endif

/* Distributor registers, offsets from GICD_BASE. */
#define GICD_CTLR       0x000       /* global enable */
#define GICD_TYPER      0x004       /* how many interrupts this GIC supports */
#define GICD_IGROUPR    0x080       /* group 0 (secure) / 1 (non-secure) */
#define GICD_ISENABLER  0x100       /* set-enable, one bit per interrupt */
#define GICD_ICENABLER  0x180       /* clear-enable */
#define GICD_ICPENDR    0x280       /* clear-pending */
#define GICD_IPRIORITYR 0x400       /* one byte per interrupt */
#define GICD_ITARGETSR  0x800       /* one byte per interrupt: CPU mask */
#define GICD_ICFGR      0xC00       /* two bits per interrupt: level/edge */

/* CPU interface registers, offsets from GICC_BASE. */
#define GICC_CTLR       0x00        /* enable signalling to this core */
#define GICC_PMR        0x04        /* priority mask: only higher priority
                                     * (numerically lower) gets through */
#define GICC_BPR        0x08        /* binary point, for preemption grouping */
#define GICC_IAR        0x0C        /* acknowledge: read to claim an interrupt */
#define GICC_EOIR       0x10        /* end of interrupt: write the IAR value */

/* IAR returns this when there is nothing to service. Treating it as a real
 * interrupt id and EOIing it corrupts the controller's state. */
#define GICC_SPURIOUS   1023

static volatile unsigned int *gicd(unsigned long off)
{
    return (volatile unsigned int *)(GICD_BASE + off);
}

static volatile unsigned int *gicc(unsigned long off)
{
    return (volatile unsigned int *)(GICC_BASE + off);
}

/* How many interrupt IDs this GIC implements. TYPER's low five bits give
 * (N/32 - 1), so the count is 32 * (field + 1), capped at 1020. */
int intc_num_irqs(void)
{
    unsigned int typer = *gicd(GICD_TYPER);
    int n = 32 * (int)((typer & 0x1F) + 1);
    if (n > 1020) {
        n = 1020;
    }
    return n;
}

void intc_init(void)
{
    int n = intc_num_irqs();
    int i;

    /* Distributor off while it is reconfigured. */
    *gicd(GICD_CTLR) = 0;

    /* Disable and clear every SPI. PPIs and SGIs (0-31) are per-core and are
     * left to intc_init_cpu; their enable register is banked per core, so
     * writing it here would configure only this one anyway. */
    for (i = 32; i < n; i = i + 32) {
        *gicd(GICD_ICENABLER + (unsigned long)(i / 32) * 4) = 0xFFFFFFFF;
        *gicd(GICD_ICPENDR + (unsigned long)(i / 32) * 4) = 0xFFFFFFFF;
    }

    /* Middle priority for every SPI, and target CPU 0. A priority of 0xFF is
     * the *lowest*, and GICC_PMR below masks anything at or below its own
     * value -- so leaving priorities at 0xFF with a PMR of 0xFF delivers
     * nothing at all, which looks exactly like a controller that is not
     * working. */
    for (i = 32; i < n; i = i + 4) {
        *gicd(GICD_IPRIORITYR + (unsigned long)i) = 0xA0A0A0A0;
        *gicd(GICD_ITARGETSR + (unsigned long)i) = 0x01010101;
    }

    /* Enable group 0 forwarding. */
    *gicd(GICD_CTLR) = 1;
}

/* Per-core setup: this half is banked, so every core runs it for itself. */
void intc_init_cpu(void)
{
    int i;

    /* Disable all PPIs/SGIs on this core, then clear any pending. */
    *gicd(GICD_ICENABLER) = 0xFFFFFFFF;
    *gicd(GICD_ICPENDR) = 0xFFFFFFFF;

    for (i = 0; i < 32; i = i + 4) {
        *gicd(GICD_IPRIORITYR + (unsigned long)i) = 0xA0A0A0A0;
    }

    /* Let everything numerically below 0xF0 through. */
    *gicc(GICC_PMR) = 0xF0;
    *gicc(GICC_BPR) = 0x07;
    *gicc(GICC_CTLR) = 1;
}

void intc_enable_irq(int irq)
{
    unsigned long reg = (unsigned long)(irq / 32) * 4;
    unsigned int bit = 1U << (unsigned int)(irq % 32);
    *gicd(GICD_ISENABLER + reg) = bit;
}

void intc_disable_irq(int irq)
{
    unsigned long reg = (unsigned long)(irq / 32) * 4;
    unsigned int bit = 1U << (unsigned int)(irq % 32);
    *gicd(GICD_ICENABLER + reg) = bit;
}

void intc_set_priority(int irq, int prio)
{
    volatile unsigned char *p =
        (volatile unsigned char *)(GICD_BASE + GICD_IPRIORITYR
                                   + (unsigned long)irq);
    *p = (unsigned char)prio;
}

/* Claim the pending interrupt. The returned value must be handed back to
 * intc_eoi unchanged -- it carries the source CPU id in its high bits for an
 * SGI, and masking those off before the EOI leaves the interrupt active
 * forever. */
int intc_acknowledge(void)
{
    return (int)(*gicc(GICC_IAR));
}

void intc_eoi(int iar)
{
    *gicc(GICC_EOIR) = (unsigned int)iar;
}

int intc_is_spurious(int iar)
{
    return ((iar & 0x3FF) == GICC_SPURIOUS) ? 1 : 0;
}

int intc_irq_of(int iar)
{
    return iar & 0x3FF;
}
