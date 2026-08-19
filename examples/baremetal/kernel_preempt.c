/* kernel_preempt.c - two register-partitioned threads preempting each other
 * on real (emulated) hardware.
 *
 * This is where the whole chain meets: ShivyCX computes each thread's register
 * footprint from the call graph, splits the file into left/right budgets,
 * re-runs allocation constrained to each, and emits a timer ISR that saves
 * only the running side's registers. That ISR is installed in the EL1h IRQ
 * vector slot, the generic timer drives it, and the two threads run.
 *
 *     make baremetal-preempt
 *
 * What to look for in the output: both counters advance, which means control
 * really is alternating, and neither thread's arithmetic is ever wrong, which
 * means the specialized save/restore preserved everything that mattered. A
 * switcher that saved too few registers would not crash -- it would corrupt a
 * value occasionally, depending where the tick landed, which is why each
 * thread checks its own invariant rather than just counting.
 *
 * The threads are `worker_left` and `worker_right`. They never call the
 * scheduler; they are interrupted by it.
 */

void uart_init(void);
void uart_puts(char *s);
void uart_putdec(long v);
void uart_puthex(unsigned long v);
unsigned long ticks(void);
unsigned long timer_count(void);
unsigned long timer_freq(void);

void irq_init(void);
void timer_start(int hz);
void irq_enable(void);

void install_preempt_vectors(void);
void thread_launch(void *tcb);

/* Defined by the generated switcher (switcher.preempt.s). */
extern unsigned long cur_tcb;
extern unsigned long next_tcb;

/* TCB layout, matching what the generated ISR reads and writes:
 *   +0 sp   +8 ELR_EL1 (resume address)   +16 SPSR_EL1   +24.. saved regs
 * Sized generously: the ISR only touches the slots its footprint needs. */
#define TCB_WORDS 48
static unsigned long tcb_left[TCB_WORDS];
static unsigned long tcb_right[TCB_WORDS];

#define STACK_WORDS 512
static unsigned long stack_left[STACK_WORDS];
static unsigned long stack_right[STACK_WORDS];

/* EL1h, with D/A/I/F clear so the thread runs with interrupts enabled --
 * without that the first tick would never be taken and the second thread
 * would never start. */
#define SPSR_EL1H_IRQ_ON  0x5

/* Per-thread state. Kept in globals rather than locals so each thread's
 * working set is what the partition sees. */
volatile long left_ticks;
volatile long left_acc;
volatile long left_bad;

volatile long right_ticks;
volatile long right_acc;
volatile long right_bad;

/* Each worker keeps a running sum and re-derives it, so a corrupted register
 * shows up as a mismatch rather than as silence. */
void worker_left(void)
{
    long i = 0;
    long next_report = 20000;
    for (;;) {
        long a = i + 1;
        long b = a * 3;
        long c = b - a;
        if (c != a * 2) {
            left_bad = left_bad + 1;
        }
        left_acc = left_acc + c;
        i = i + 1;
        left_ticks = i;

        /* Reporting lives in a thread rather than in kmain, because kmain
         * never runs again once the first thread is launched -- the scheduler
         * is symmetric from that point and every switch is an eret from the
         * ISR.
         *
         * It also puts a UART call inside the left thread's call graph, which
         * is the case the partition is actually meant for: an IO path is deep
         * enough to have a real register footprint, unlike the call-free
         * bodies in bench_threads.c. */
        if (i >= next_report) {
            next_report = i + 20000;
            uart_puts("  left=");
            uart_putdec(left_ticks);
            uart_puts("  right=");
            uart_putdec(right_ticks);
            uart_puts("  ticks=");
            uart_putdec((long)ticks());
            uart_puts("  switches=");
            uart_putdec((long)ticks());
            uart_puts("  corrupt(l/r)=");
            uart_putdec(left_bad);
            uart_puts("/");
            uart_putdec(right_bad);
            uart_puts("\n");
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
        right_acc = right_acc + z;
        j = j + 1;
        right_ticks = j;
    }
}

/* The board hook the generated ISR calls. Rearming the timer is what
 * deasserts it; the controller EOI is handled inside irq_ack_timer(). */
void irq_ack_timer(void);

void timer_ack(void)
{
    irq_ack_timer();
}

static void tcb_init(unsigned long *tcb, unsigned long *stack,
                     void (*entry)(void))
{
    int i;
    for (i = 0; i < TCB_WORDS; i = i + 1) {
        tcb[i] = 0;
    }
    /* Stacks grow down; leave the top 16-byte aligned, which AAPCS64
     * requires and which `stp x, y, [sp, #-16]!` in any prologue assumes. */
    tcb[0] = (unsigned long)(stack + STACK_WORDS - 2);
    tcb[1] = (unsigned long)entry;
    tcb[2] = SPSR_EL1H_IRQ_ON;
}

void kmain(void)
{
    uart_init();
    uart_puts("\n== register-partitioned preemptive threads ==\n");

    irq_init();
    install_preempt_vectors();

    tcb_init(tcb_left, stack_left, worker_left);
    tcb_init(tcb_right, stack_right, worker_right);

    /* The ISR switches cur <-> next on every tick. */
    cur_tcb = (unsigned long)tcb_left;
    next_tcb = (unsigned long)tcb_right;

    uart_puts("left  TCB = ");
    uart_puthex((unsigned long)tcb_left);
    uart_puts("\nright TCB = ");
    uart_puthex((unsigned long)tcb_right);
    uart_puts("\n");

    timer_start(1000);
    irq_enable();

    uart_puts("launching left thread; the timer will preempt into right\n\n");

    /* From here the scheduler is symmetric: kmain never runs again, and the
     * left worker does the reporting. */
    thread_launch(tcb_left);

    /* Not reached. */
    uart_puts("thread_launch returned -- this should not happen\n");
}
