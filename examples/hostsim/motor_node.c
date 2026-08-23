/* motor_node.c - a control loop, as an example of what the host path is for.
 *
 * A board driving a motor towards a target position: read the encoder, run a
 * PI controller, drive the output, report over the console.
 *
 * The control logic is portable and the console and timer calls are the same
 * seam baremetal64/ implements. The three motor calls are not: they are
 * satisfied by hostsim.c here, and a real board would need its own
 * implementation over a PWM peripheral and a quadrature counter, which does
 * not exist in this tree yet. So this builds for the host and *not* for a
 * Jetson -- tools/baremetal_arm64.py stops with
 *
 *     rlink: undefined reference to: sim_encoder_read, sim_motor_write,
 *            sim_target
 *
 * which is the right outcome. The seam is where the portability claim stops,
 * and a missing driver should fail at the link rather than be quietly
 * substituted.
 *
 * Nothing here is interesting as control theory. What it demonstrates is the
 * shape of a workload that instruction-level emulation cannot carry: several
 * of these running for simulated minutes, each stepping a plant model, with
 * the controlling process reading the results and plotting them. At armulator
 * speeds one board-second costs about a day.
 *
 *     python3 tools/hostsim_build.py examples/hostsim/motor_node.c
 *     python3 examples/hostsim/fleet_demo.py
 */

void uart_init(void);
void uart_puts(char *s);
void uart_putdec(long v);
void irq_init(void);
void irq_enable(void);
void timer_start(int hz);
unsigned long timer_count(void);
unsigned long timer_freq(void);

/* Set by the controlling process through sim_motor_*; see hostsim.c. The
 * plant lives outside the application, exactly as it does on a bench. */
long sim_encoder_read(void);
void sim_motor_write(long duty);
long sim_target(void);

/* Fixed point, 1000 = 1.0. A bare-metal control loop has no FPU it can
 * assume, and integer arithmetic keeps the two build targets bit-identical. */
#define KP 400L
#define KI 6L
#define DUTY_LIMIT 1000L
#define INTEGRAL_LIMIT 20000L

static long clamp(long v, long limit)
{
    if (v > limit) {
        return limit;
    }
    if (v < -limit) {
        return -limit;
    }
    return v;
}

void kmain(void)
{
    long integral = 0;
    unsigned long f;
    unsigned long period;
    unsigned long next;
    int steps = 0;

    uart_init();
    irq_init();
    timer_start(1000);
    irq_enable();

    uart_puts("[motor] control loop up at 1 kHz\n");

    f = timer_freq();
    period = f / 1000UL;            /* one millisecond */
    next = timer_count() + period;

    /* Run for a simulated ten seconds. On hardware this loop never ends; the
     * bound is here so the example terminates. */
    while (steps < 10000) {
        long position;
        long error;
        long duty;

        while (timer_count() < next) {
        }
        next = next + period;

        position = sim_encoder_read();
        error = sim_target() - position;

        integral = clamp(integral + error, INTEGRAL_LIMIT);
        duty = clamp((KP * error + KI * integral) / 1000L, DUTY_LIMIT);
        sim_motor_write(duty);

        if ((steps % 1000) == 0) {
            uart_puts("[motor] t=");
            uart_putdec((long)steps);
            uart_puts("ms pos=");
            uart_putdec(position);
            uart_puts(" err=");
            uart_putdec(error);
            uart_puts(" duty=");
            uart_putdec(duty);
            uart_puts("\n");
        }
        steps = steps + 1;
    }

    uart_puts("[motor] done\n");
}
