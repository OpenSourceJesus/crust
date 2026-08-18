/* kernel_echo.c - an interrupt-driven serial echo.
 *
 * The first non-timer interrupt in this tree. A timer is the easy case: it is
 * private to the core and needs no routing. A UART is a shared peripheral, and
 * how it reaches the core differs completely by board --
 *
 *   virt    GIC SPI 33, routed by the distributor
 *   Pi 3    GPU 57: enabled in the legacy controller at 0x3F00B200, then
 *           routed to a core by the ARM local block at 0x40000000, and
 *           arriving as an undifferentiated "GPU" bit that must be decoded
 *           a second time
 *
 * -- but none of that appears below, because it is behind the intc_* seam.
 *
 *     make baremetal-echo            (virt)
 *     make baremetal-echo-raspi      (Pi 3)
 *
 * Type at the console; characters come back through the interrupt handler.
 * The timer runs at the same time, so the two sources can be seen sharing the
 * vector.
 */

void uart_init(void);
void uart_puts(char *s);
void uart_putdec(long v);

void irq_init(void);
void irq_enable(void);
void timer_start(int hz);
void rx_start(int echo);
unsigned long ticks(void);
unsigned long rx_count(void);
unsigned long rx_interrupts(void);
unsigned long unexpected(void);

void kmain(void)
{
    unsigned long last_report = 0;

    uart_init();
    uart_puts("\n== interrupt-driven echo ==\n");

    irq_init();
    timer_start(100);
    rx_start(1);                    /* 1 = echo each byte from the handler */
    irq_enable();

    uart_puts("type something; ^A x quits qemu\n\n");

    /* Nothing here polls the UART. Every character below arrives because the
     * controller raised an interrupt and the handler echoed it. */
    for (;;) {
        unsigned long t = ticks();
        if (t - last_report >= 500) {   /* every ~5s at 100Hz */
            last_report = t;
            uart_puts("\n[");
            uart_putdec((long)t);
            uart_puts(" ticks, ");
            uart_putdec((long)rx_count());
            uart_puts(" chars in ");
            uart_putdec((long)rx_interrupts());
            uart_puts(" rx interrupts, ");
            uart_putdec((long)unexpected());
            uart_puts(" unexpected]\n");
        }
    }
}
