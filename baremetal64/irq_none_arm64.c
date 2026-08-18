/* irq_none_arm64.c - the IRQ dispatch used on boards with no GICv2.
 *
 * exc_arm64.c calls irq_dispatch() from the IRQ vector slots, so the symbol
 * must exist even where there is no interrupt controller this tree knows how
 * to drive. The BCM2837 in a Raspberry Pi 3 is exactly that case: it has a
 * bespoke BCM interrupt controller at 0x3F00B200 plus per-core "ARM local"
 * registers at 0x40000000, and nothing resembling a GIC. Compiling
 * gic_arm64.c for it would not fail to build -- it would write to addresses
 * that decode to something else entirely, which is far worse than not
 * building it.
 *
 * So this file stands in. Nothing enables an interrupt source on such a
 * board, so in practice nothing calls it; if something does, it is counted
 * rather than silently ignored, because an IRQ arriving with no controller to
 * acknowledge it will re-assert immediately and the machine will livelock in
 * the vector. A count that climbs is the visible symptom of that.
 */

static unsigned long stray_irqs;

unsigned long stray_irq_count(void)
{
    return stray_irqs;
}

void irq_dispatch(void)
{
    stray_irqs = stray_irqs + 1;
}
