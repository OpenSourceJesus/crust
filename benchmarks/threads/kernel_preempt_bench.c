/* kernel_preempt_bench.c - what does the register partition actually buy?
 *
 * Two threads preempt each other at a fixed tick rate for a fixed number of
 * switches. Everything is identical between the two builds except which
 * switcher sits in the EL1h IRQ vector slot:
 *
 *   partitioned  the ISR ShivyCX generated from the whole-program left/right
 *                partition -- saves only the running thread's footprint
 *   save-all     switcher_full_arm64.S -- saves all 31 general-purpose
 *                registers, which is what a scheduler without partition
 *                information has to do
 *
 * The measurement is work completed per switch: both threads spin in a tight
 * loop, and every cycle the ISR spends saving registers is a cycle they do
 * not. A cheaper switch therefore shows up as more loop iterations between
 * ticks. Reported as the total across both threads at a fixed switch count,
 * so the two runs are directly comparable.
 *
 * Also reported is elapsed CNTPCT, which gives cycles-per-switch. Under qemu
 * that is emulated time rather than silicon, so treat the ratio as indicative
 * and the *register counts* as exact -- the counts are a property of the
 * emitted code, the timing is a property of the emulator.
 *
 *     make bench-preempt
 */

void uart_init(void);
void uart_puts(char *s);
void uart_putdec(long v);
void uart_puthex(unsigned long v);

void irq_init(void);
void timer_start(int hz);
void irq_enable(void);
void irq_disable(void);
unsigned long ticks(void);
unsigned long timer_count(void);
unsigned long timer_freq(void);

void install_preempt_vectors(void);
void thread_launch(void *tcb);

extern unsigned long cur_tcb;
extern unsigned long next_tcb;

/* Enough for the save-all switcher: 24 bytes of header plus 31 registers. */
#define TCB_WORDS 48
static unsigned long tcb_left[TCB_WORDS];
static unsigned long tcb_right[TCB_WORDS];

#define STACK_WORDS 512
static unsigned long stack_left[STACK_WORDS];
static unsigned long stack_right[STACK_WORDS];

#define SPSR_EL1H_IRQ_ON 0x5

/* How many switches to measure over. Large enough that the fixed cost of
 * getting started does not dominate, small enough to finish under qemu. */
#ifndef TARGET_SWITCHES
#define TARGET_SWITCHES 4000
#endif

/* Tick rate. The interesting variable: at a low rate the switch cost is
 * amortised into nothing and the two builds are indistinguishable, which is
 * itself worth knowing. Raising it until switches are frequent relative to the
 * work between them is what exposes the difference. */
#ifndef TICK_HZ
#define TICK_HZ 1000
#endif

volatile long left_work;
volatile long right_work;
volatile long left_bad;
volatile long right_bad;
volatile long done;
volatile unsigned long t_start;
volatile unsigned long t_end;

void irq_ack_timer(void);

void timer_ack(void)
{
    irq_ack_timer();
}

/* The workers do the same shape of arithmetic as the correctness demo, so a
 * register the switcher failed to preserve still shows up as a mismatch --
 * a fast switcher that corrupts state is not a result worth having. */
void bench_report(void);

/* The completion check costs something, so it runs once every CHECK_MASK+1
 * iterations rather than every iteration. Both workers pay the same amount,
 * and the interval is large enough that the check is noise next to the loop
 * body -- otherwise the measurement would be dominated by the instrumentation
 * rather than by the switcher.
 */
#define CHECK_MASK 4095

void worker_left(void)
{
    long i = 0;
    for (;;) {
        long a = i + 1;
        long b = a * 3;
        long c = b - a;
        if (c != a * 2) {
            left_bad = left_bad + 1;
        }
        i = i + 1;
        left_work = i;
        if ((i & CHECK_MASK) == 0) {
            if (done) {
                for (;;) {
                }
            }
            if ((long)ticks() >= TARGET_SWITCHES) {
                done = 1;
                bench_report();
                for (;;) {
                }
            }
        }
    }
}

void worker_right(void)
{
    long j = 0;
    for (;;) {
        long x = j + 2;
        long y = x * 5;
        long z = y - x;
        if (z != x * 4) {
            right_bad = right_bad + 1;
        }
        j = j + 1;
        right_work = j;
        if ((j & CHECK_MASK) == 0) {
            if (done) {
                for (;;) {
                }
            }
        }
    }
}

static void tcb_init(unsigned long *tcb, unsigned long *stack,
                     void (*entry)(void))
{
    int i;
    for (i = 0; i < TCB_WORDS; i = i + 1) {
        tcb[i] = 0;
    }
    tcb[0] = (unsigned long)(stack + STACK_WORDS - 2);
    tcb[1] = (unsigned long)entry;
    tcb[2] = SPSR_EL1H_IRQ_ON;
}

/* kmain becomes the supervisor: it starts the two threads, then waits for the
 * switch count to reach the target. It is itself one of the two scheduled
 * contexts (the first tick saves kmain into the left TCB), so the wait loop is
 * preempted like anything else -- which is exactly what we want to measure. */
void kmain(void)
{
    uart_init();
    uart_puts("\n== preemptive switch benchmark ==\n");

    irq_init();
    install_preempt_vectors();

    tcb_init(tcb_left, stack_left, worker_left);
    tcb_init(tcb_right, stack_right, worker_right);
    cur_tcb = (unsigned long)tcb_left;
    next_tcb = (unsigned long)tcb_right;

    uart_puts("switch target: ");
    uart_putdec(TARGET_SWITCHES);
    uart_puts("\ntimer: ");
    uart_putdec(TICK_HZ);
    uart_puts(" Hz, CNTFRQ ");
    uart_putdec((long)timer_freq());
    uart_puts("\n\n");

    timer_start(TICK_HZ);
    t_start = timer_count();
    irq_enable();

    thread_launch(tcb_left);

    /* Not reached: thread_launch erets into worker_left and never returns. */
    uart_puts("thread_launch returned\n");
}

/* Called from the left worker once the target is met; see the loop above.
 * Reporting has to happen from a thread, because kmain never runs again. */
void bench_report(void)
{
    unsigned long cycles;
    long total;

    irq_disable();
    t_end = timer_count();
    cycles = t_end - t_start;
    total = left_work + right_work;

    uart_puts("RESULT switches=");
    uart_putdec((long)ticks());
    uart_puts(" work=");
    uart_putdec(total);
    uart_puts(" left=");
    uart_putdec(left_work);
    uart_puts(" right=");
    uart_putdec(right_work);
    uart_puts(" cycles=");
    uart_putdec((long)cycles);
    uart_puts(" corrupt=");
    uart_putdec(left_bad + right_bad);
    uart_puts("\nDONE\n");
}
