/* vision_node.c - a Jetson-style node running an inference every frame.
 *
 * Waits for a frame, classifies it, and reports the result over the link. The
 * inference goes through the accelerator seam in hostsim/accel.h, which on a
 * real Jetson would be the GPU through the vendor runtime and on the host is
 * whatever the build was given -- plain C by default, CUDA with --cuda.
 *
 * This is the workload that motivates the host path. Thirty frames a second
 * of inference is not something an instruction emulator can carry: armulator
 * manages about 17,000 instructions a second, so one simulated second of this
 * would take roughly a day, and it models no GPU to run the inference on
 * anyway.
 *
 *     python3 tools/hostsim_build.py examples/hostsim/vision_node.c \
 *         -o /tmp/vision.so
 *     python3 examples/hostsim/vision_demo.py
 *
 * The frames come from the controlling process, which is the point: they can
 * be a recorded dataset, a generator, or a camera, and none of that has to be
 * modelled on the board.
 */

void uart_init(void);
void uart_puts(char *s);
void uart_putdec(long v);

int link_send(const char *data, unsigned long n);

void irq_init(void);
void irq_enable(void);
void timer_start(int hz);
unsigned long timer_count(void);
unsigned long timer_freq(void);

/* The accelerator seam. */
int accel_infer(const unsigned char *frame, long *score_out);
int accel_available(void);
const char *accel_backend(void);

/* Frames arrive from the controlling process. sim_frame_ready() is
 * non-blocking, because blocking would stall the virtual clock the
 * controller uses to feed us. */
int sim_frame_ready(void);
const unsigned char *sim_frame_data(void);
void sim_frame_consume(void);

#define FRAME_HZ 30

static unsigned long frames;
static unsigned long dropped;

/* "C <class> <score>" */
static unsigned long format_result(char *buf, int cls, long score)
{
    unsigned long i = 0;
    char digits[24];
    int n = 0;
    long v;

    buf[i++] = 'C';
    buf[i++] = ' ';
    buf[i++] = (char)('0' + (cls % 10));
    buf[i++] = ' ';

    v = score;
    if (v < 0) {
        buf[i++] = '-';
        v = -v;
    }
    if (v == 0) {
        digits[n++] = '0';
    }
    while (v > 0) {
        digits[n++] = (char)('0' + (v % 10));
        v /= 10;
    }
    while (n > 0) {
        buf[i++] = digits[--n];
    }
    return i;
}

void kmain(void)
{
    char message[64];
    unsigned long f;
    unsigned long period;
    unsigned long next;
    int budget = 3000;

    uart_init();
    irq_init();
    timer_start(1000);
    irq_enable();

    uart_puts("[vision] accelerator: ");
    uart_puts((char *)accel_backend());
    uart_puts(accel_available() ? " (hardware)\n" : " (software)\n");

    f = timer_freq();
    period = f / FRAME_HZ;
    next = timer_count() + period;

    while (budget > 0) {
        long score = 0;
        int cls;

        while (timer_count() < next) {
        }
        next = next + period;
        budget = budget - 1;

        /* A frame that has not arrived by its slot is a dropped frame, not
         * something to wait for. A camera does not pause for us. */
        if (!sim_frame_ready()) {
            dropped = dropped + 1;
            continue;
        }

        cls = accel_infer(sim_frame_data(), &score);
        sim_frame_consume();
        frames = frames + 1;

        link_send(message, format_result(message, cls, score));
    }

    uart_puts("[vision] frames=");
    uart_putdec((long)frames);
    uart_puts(" dropped=");
    uart_putdec((long)dropped);
    uart_puts("\n");
}
