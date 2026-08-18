/* kernel_raspi_irq.c - timer interrupts on a Raspberry Pi 3, which has no GIC.
 *
 * The interrupt is routed by the BCM2837's ARM local peripherals at
 * 0x40000000 rather than by an ARM interrupt controller, but nothing below
 * mentions that: the controller sits behind the intc_* seam, so this is the
 * same code that would run on virt or a Jetson.
 *
 *     make baremetal-raspi-irq
 */

void uart_init(void);
void uart_puts(char *s);
void uart_putdec(long v);
void uart_puthex(unsigned long v);

void irq_init(void);
void timer_start(int hz);
void timer_stop(void);
void irq_enable(void);
void irq_disable(void);
unsigned long ticks(void);
unsigned long spurious(void);
unsigned long unexpected(void);
unsigned long timer_count(void);
unsigned long timer_freq(void);

/* Raw controller state, for showing what the BCM side actually looks like. */
unsigned long intc_raw_source(void);
unsigned long intc_raw_timer_control(void);

static void wait_ms(unsigned long ms)
{
    unsigned long f = timer_freq();
    unsigned long t0 = timer_count();
    while (timer_count() - t0 < (f / 1000) * ms) {
    }
}

void kmain(void)
{
    unsigned long f;

    uart_init();
    uart_puts("\n== Raspberry Pi 3: timer interrupts, no GIC ==\n");

    irq_init();
    f = timer_freq();
    uart_puts("CNTFRQ_EL0        = ");
    uart_putdec((long)f);
    uart_puts(" Hz\n");

    timer_start(1000);
    uart_puts("TIMER_IRQCNTL     = ");
    uart_puthex(intc_raw_timer_control());
    uart_puts("  (bit 1 = CNTPNSIRQ, the non-secure physical timer)\n");

    /* Masked: the controller asserts, but PSTATE.I holds it off. Reading the
     * source register here is what distinguishes "routed and pending" from
     * "never delivered" -- a tick count of zero looks the same either way. */
    irq_disable();
    wait_ms(100);
    uart_puts("\n[masked]   ticks      = ");
    uart_putdec((long)ticks());
    uart_puts("\n           IRQ_SOURCE = ");
    uart_puthex(intc_raw_source());
    uart_puts("  (nonzero: asserted, waiting on PSTATE.I)\n");

    irq_enable();
    wait_ms(100);
    uart_puts("\n[unmasked] ticks in 100ms at 1000Hz = ");
    uart_putdec((long)ticks());
    uart_puts("\n");

    uart_puts("           spurious=");
    uart_putdec((long)spurious());
    uart_puts(" unexpected=");
    uart_putdec((long)unexpected());
    uart_puts("\n");

    timer_stop();
    irq_disable();
    uart_puts("\n== pi interrupts ok ==\n");
}
