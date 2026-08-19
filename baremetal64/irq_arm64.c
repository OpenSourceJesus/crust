/* irq_arm64.c - timer tick and interrupt dispatch.
 *
 * The EL1 physical timer is PPI 30. It is private to each core, which is why
 * it needs no board knowledge: no device tree, no MMIO base, no interrupt
 * number to look up. That makes it the right first interrupt for a
 * bare-metal image, and the reason this file has no board-specific constant
 * in it beyond that ID.
 *
 * The path an interrupt takes, and every gate it must pass:
 *
 *   CNTP_CTL_EL0.ENABLE=1, IMASK=0     the timer will assert
 *          |
 *   GICD_ISENABLER bit 30              the distributor forwards it
 *          |
 *   GICC_CTLR=1, GICC_PMR > priority   the CPU interface presents it
 *          |
 *   PSTATE.I clear (irq_enable)        the core accepts it
 *          |
 *   VBAR_EL1 + 0x280                   EL1h IRQ vector -> exc_common
 *          |
 *   irq_dispatch                       acknowledge, handle, EOI
 *
 * Every one of those is silent when wrong: the machine simply never takes an
 * interrupt, with nothing to indicate which gate is shut. timer_selftest()
 * exists to tell them apart.
 */

/* The logical id the timer arrives as. On a GIC this is genuinely PPI 30; on
 * the Pi's BCM controller there are no interrupt ids at all, so its driver
 * reports the same number for the timer source. That keeps this file -- the
 * timer policy, the tick counting, the rearming -- free of any controller
 * knowledge, which is the point of the intc_* seam. */
#define TIMER_PPI   30

/* The UART's interrupt id. Unlike the timer this really is board-specific --
 * a UART is a shared peripheral, and where it lands differs per SoC:
 *
 *   qemu virt      GIC SPI 1  -> INTID 33
 *   Tegra X1       UART-A     -> INTID 68
 *   Pi 3           GPU 57, which has no INTID at all; bcm_irq_arm64.c reports
 *                  the same number configured here so this file stays free of
 *                  board knowledge
 *
 * Getting it wrong does not fail to build: the wrong SPI is enabled, the UART
 * never routes, and the console appears simply not to receive. */
#ifndef UART_IRQ
#define UART_IRQ    33
#endif

void uart_puts(char *s);
void uart_putc(int c);
void uart_puthex(unsigned long v);
void uart_putdec(long v);

void intc_init(void);
void intc_init_cpu(void);
void intc_enable_irq(int irq);
void intc_disable_irq(int irq);
int intc_acknowledge(void);
void intc_eoi(int iar);
int intc_is_spurious(int iar);
int intc_irq_of(int iar);

unsigned long timer_freq(void);
unsigned long timer_count(void);
unsigned long timer_ctl(void);
void timer_set_tval(unsigned long ticks);
void timer_enable(void);
void timer_disable(void);
void irq_enable(void);
void irq_disable(void);
unsigned long irq_pending(void);

void uart_enable_rx_irq(void);
void uart_disable_rx_irq(void);
int uart_drain_rx(char *buf, int max);

static unsigned long tick_interval;
static unsigned long tick_count;
static unsigned long spurious_count;
static unsigned long unexpected_count;
static int trace_ticks;
static unsigned long rx_chars;
static unsigned long rx_irqs;
static char rx_last;
static int echo_rx;

unsigned long ticks(void)
{
    return tick_count;
}

unsigned long spurious(void)
{
    return spurious_count;
}

unsigned long unexpected(void)
{
    return unexpected_count;
}

void timer_trace(int on)
{
    trace_ticks = on;
}

/* Start a periodic tick of `hz` interrupts per second. */
void timer_start(int hz)
{
    unsigned long freq = timer_freq();
    if (hz <= 0) {
        hz = 1;
    }
    tick_interval = freq / (unsigned long)hz;
    if (tick_interval == 0) {
        tick_interval = 1;
    }
    intc_enable_irq(TIMER_PPI);
    timer_set_tval(tick_interval);
    timer_enable();
}

void timer_stop(void)
{
    timer_disable();
}

unsigned long rx_count(void)
{
    return rx_chars;
}

unsigned long rx_interrupts(void)
{
    return rx_irqs;
}

int rx_last_char(void)
{
    return (int)rx_last;
}

/* Turn on receive interrupts: at the UART itself, then at the controller.
 * Both are needed and neither reports the other missing. */
void rx_start(int echo)
{
    echo_rx = echo;
    uart_enable_rx_irq();
    intc_enable_irq(UART_IRQ);
}

void rx_stop(void)
{
    intc_disable_irq(UART_IRQ);
    uart_disable_rx_irq();
}

/* Acknowledge one timer tick without doing any dispatch.
 *
 * The generated preemptive switcher (see SHIVYCX.md, register-partitioned
 * threads) replaces irq_dispatch entirely: it saves the running thread's
 * footprint itself and erets into the other thread. But it still has to quiet
 * the timer, and how differs per board -- rearming CNTP_TVAL_EL0 is what
 * deasserts the timer, and the controller then needs its own end-of-interrupt
 * (a real write on a GIC, nothing at all on the Pi's BCM controller). Both
 * live behind the intc_* seam, so this one function covers every board.
 */
void irq_ack_timer(void)
{
    int iar = intc_acknowledge();
    timer_set_tval(tick_interval);
    tick_count = tick_count + 1;
    intc_eoi(iar);
}

void irq_init(void)
{
    intc_init();
    intc_init_cpu();
}

/* Called from exc_common for every IRQ-class exception. */
void irq_dispatch(void)
{
    int iar = intc_acknowledge();
    int irq = intc_irq_of(iar);

    if (intc_is_spurious(iar)) {
        /* No EOI: the spurious id was never a real interrupt, and ending it
         * would decrement the controller's active count for something that
         * was never active. */
        spurious_count = spurious_count + 1;
        return;
    }

    if (irq == UART_IRQ) {
        /* Drain the whole FIFO. The UART asserts while any data remains, so
         * reading a single byte and returning re-enters the handler
         * immediately and the machine livelocks -- the same failure shape as
         * forgetting to rearm the timer, from the opposite cause. */
        char buf[32];
        int n = uart_drain_rx(buf, 32);
        int i;
        rx_irqs = rx_irqs + 1;
        for (i = 0; i < n; i = i + 1) {
            rx_chars = rx_chars + 1;
            rx_last = buf[i];
            if (echo_rx) {
                uart_putc((int)buf[i]);
            }
        }
    } else if (irq == TIMER_PPI) {
        /* Rearm first. TVAL does not auto-reload, so a handler that EOIs
         * without rewriting it takes exactly one tick and then goes quiet --
         * which looks like the GIC dropping interrupts rather than a timer
         * that was never restarted. */
        timer_set_tval(tick_interval);
        tick_count = tick_count + 1;
        if (trace_ticks) {
            uart_puts("  tick ");
            uart_putdec((long)tick_count);
            uart_puts("\n");
        }
    } else {
        unexpected_count = unexpected_count + 1;
    }

    intc_eoi(iar);
}

/* Report the state of each gate an interrupt has to pass, so a machine that
 * takes no interrupts can be diagnosed rather than guessed at. */
void timer_selftest(void)
{
    unsigned long ctl = timer_ctl();
    uart_puts("  timer freq   = ");
    uart_putdec((long)timer_freq());
    uart_puts(" Hz\n  CNTP_CTL     = ");
    uart_puthex(ctl);
    uart_puts("  (enable=");
    uart_putdec((long)(ctl & 1));
    uart_puts(" imask=");
    uart_putdec((long)((ctl >> 1) & 1));
    uart_puts(" istatus=");
    uart_putdec((long)((ctl >> 2) & 1));
    uart_puts(")\n  ISR_EL1      = ");
    uart_puthex(irq_pending());
    uart_puts("\n");
}
