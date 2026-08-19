/* bench_printf.c - the case the register partition is actually for.
 *
 * Every earlier benchmark used tight arithmetic loops, which is the easy case
 * and not the interesting one: a leaf loop barely touches callee-saved
 * registers, so there is little to partition. An IO path is the opposite. A
 * formatted write walks a call graph -- format, digit conversion, per-character
 * output, a busy-wait on the transmit FIFO -- and every value that has to
 * survive one of those calls needs a callee-saved home. That is what generates
 * real register pressure on AArch64.
 *
 * The left thread does IO. The right thread computes. They share nothing, so
 * the call graphs are disjoint and the partition has something to work with.
 *
 *     python3 -m shivyc.main benchmarks/threads/bench_printf.c \
 *         --emit-thread-switcher sw.s --target arm64
 */

/* The console, as the bare-metal image provides it. uart_puts walks the string
 * a character at a time and each uart_putc polls the PL011's FIFO, so this is
 * a genuine multi-level call graph rather than a stand-in for one. */
void uart_putc(int c);
void uart_puts(char *s);
void uart_puthex(unsigned long v);
void uart_putdec(long v);

int io_lines;
int io_chars;
long compute_acc;
long compute_iters;

/* A formatted-output routine in the shape a printf implementation has: a
 * format walk, a width decision per conversion, and several live values that
 * all have to survive the calls that emit characters. */
static void emit_field(char *label, long value, int hex)
{
    uart_puts(label);
    if (hex) {
        uart_puthex((unsigned long)value);
    } else {
        uart_putdec(value);
    }
    io_chars = io_chars + 1;
}

void io_thread(void)
{
    long seq = 0;
    for (;;) {
        /* Several values live across every call below -- seq, the derived
         * fields, and the accumulators. On a leaf function these would sit in
         * caller-saved scratch; here they must occupy x19-x28. */
        long a = seq + 1;
        long b = a * 3;
        long c = b - a;
        long d = c + seq;

        emit_field("seq=", seq, 0);
        emit_field(" a=", a, 0);
        emit_field(" b=", b, 1);
        emit_field(" c=", c, 0);
        emit_field(" d=", d, 1);
        uart_puts("\n");

        io_lines = io_lines + 1;
        seq = seq + 1;
    }
}

/* A pure computation thread: no calls, so almost nothing needs a callee-saved
 * home. Its footprint is small, which is exactly the asymmetry that makes the
 * split work here -- the IO side gets the registers it needs because the
 * compute side does not want them. */
void compute_thread(void)
{
    long i = 0;
    long acc = 0;
    for (;;) {
        long x = i * 7;
        long y = x + 3;
        acc = acc + (y - x);
        i = i + 1;
        compute_acc = acc;
        compute_iters = i;
    }
}

int main()
assert io_thread in threads.left( core=0 )
assert compute_thread in threads.right( core=0 )
{
    io_thread();
    compute_thread();
    return 0;
}
