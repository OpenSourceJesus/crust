/* kernel_arm64.c - a bare-metal AArch64 kernel for `qemu-system-aarch64
 * -M virt`, exercising the whole spine: console, exceptions, MMU.
 *
 * Build and run:
 *     make baremetal-arm64
 *     python3 tools/baremetal_arm64.py examples/baremetal/kernel_arm64.c --run
 *
 * Everything below is compiled by ShivyCX and assembled by rasm; the only
 * external tool involved is `ld`.
 */

void uart_init(void);
void uart_puts(char *s);
void uart_puthex(unsigned long v);
void uart_putdec(long v);

void mmu_init(void);
void mmu_enable(void);
void mmu_report(void);
unsigned long read_sctlr(void);

void exc_expect(int n);
int exc_taken(void);

void irq_init(void);
void irq_enable(void);
void irq_disable(void);
void timer_start(int hz);
void timer_selftest(void);
unsigned long ticks(void);
unsigned long spurious(void);
unsigned long unexpected(void);
unsigned long timer_count(void);
unsigned long timer_freq(void);

/* Spin until `ms` milliseconds of counter time have passed. The counter runs
 * whether or not interrupts are enabled, so this measures real time either
 * way -- which is what makes it usable to show ticks starting and stopping. */
static void wait_ms(unsigned long ms)
{
    unsigned long f = timer_freq();
    unsigned long t = timer_count() + (f / 1000UL) * ms;
    while (timer_count() < t) {
    }
}

static int fib(int n)
{
    if (n < 2) {
        return n;
    }
    return fib(n - 1) + fib(n - 2);
}

void kmain(void)
{
    int i;
    unsigned long sctlr;

    uart_init();
    uart_puts("\n");
    uart_puts("+--------------------------------------+\n");
    uart_puts("|  crust: bare-metal AArch64 (EL1)     |\n");
    uart_puts("+--------------------------------------+\n");

    /* -- the compiler is working at all ------------------------------- */
    uart_puts("\n[compute] fib(0..12):");
    for (i = 0; i < 13; i = i + 1) {
        uart_puts(" ");
        uart_putdec((long)fib(i));
    }
    uart_puts("\n");

    /* -- exceptions ---------------------------------------------------
     * A store to an address with nothing behind it. With the MMU still off
     * this is an external abort from the bus, not a translation fault --
     * the same access reports differently once translation is on, which is
     * a neat way to see the MMU actually take effect.
     */
    uart_puts("\n[except] storing to 0xdeadbe000 with the MMU off\n");
    exc_expect(1);
    {
        volatile unsigned long *bad = (volatile unsigned long *)0xdeadbe000UL;
        *bad = 1;
    }
    uart_puts("[except] recovered, faults so far: ");
    uart_putdec((long)exc_taken());
    uart_puts("\n");

    /* -- MMU ----------------------------------------------------------- */
    uart_puts("\n[mmu] building a 1 GiB identity map\n");
    mmu_init();
    mmu_report();
    sctlr = read_sctlr();
    uart_puts("  SCTLR before = ");
    uart_puthex(sctlr);
    uart_puts("\n");

    mmu_enable();

    sctlr = read_sctlr();
    uart_puts("  SCTLR after  = ");
    uart_puthex(sctlr);
    uart_puts("\n  MMU=");
    uart_putdec((long)(sctlr & 1));
    uart_puts(" dcache=");
    uart_putdec((long)((sctlr >> 2) & 1));
    uart_puts(" icache=");
    uart_putdec((long)((sctlr >> 12) & 1));
    uart_puts("\n");

    /* -- memory still works, now through the page tables ---------------- */
    uart_puts("\n[mmu] summing an array through the MMU: ");
    {
        static unsigned long a[64];
        unsigned long sum = 0;
        for (i = 0; i < 64; i = i + 1) {
            a[i] = (unsigned long)(i * 3);
        }
        for (i = 0; i < 64; i = i + 1) {
            sum = sum + a[i];
        }
        uart_putdec((long)sum);
        uart_puts(" (expect 6048)\n");
    }

    /* -- and an unmapped access now faults differently ------------------ */
    uart_puts("\n[mmu] the same kind of bad store, now translated:\n");
    exc_expect(1);
    {
        volatile unsigned long *bad = (volatile unsigned long *)0x800000000UL;
        *bad = 1;
    }
    uart_puts("[mmu] recovered, faults total: ");
    uart_putdec((long)exc_taken());
    uart_puts("\n");

    /* -- interrupts -----------------------------------------------------
     * The EL1 physical timer is PPI 30: architectural, so it needs no device
     * tree and no board knowledge. Ticks are counted only while PSTATE.I is
     * clear, which is what shows they arrive through the vector table rather
     * than from anything else.
     */
    uart_puts("\n[irq] GIC + generic timer at 100 Hz\n");
    irq_init();
    timer_start(100);
    timer_selftest();

    uart_puts("  interrupts still masked, waiting 200ms: ");
    wait_ms(200);
    uart_putdec((long)ticks());
    uart_puts(" ticks (expect 0)\n");

    irq_enable();
    uart_puts("  unmasked, waiting 300ms: ");
    wait_ms(300);
    uart_putdec((long)ticks());
    uart_puts(" ticks (expect ~30)\n");

    irq_disable();
    uart_puts("  spurious=");
    uart_putdec((long)spurious());
    uart_puts(" unexpected=");
    uart_putdec((long)unexpected());
    uart_puts("\n");

    uart_puts("\n== all stages ok ==\n");
}
