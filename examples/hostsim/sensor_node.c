/* sensor_node.c - a board that reports over a link and takes commands.
 *
 * Reads its encoder once a millisecond, and every 100 ms sends a reading to
 * whatever is on the other end of the link. Commands arriving on the link
 * change the target; a 'q' on the console stops it.
 *
 * The point is the failure handling, not the arithmetic. link_send() can
 * fail -- a full queue, a downed link -- and this counts those failures and
 * reports them, which is the behaviour worth testing against an injected
 * fault. Firmware that ignores the return value loses messages silently, and
 * a simulation that cannot drop messages will never reveal that.
 *
 *     python3 tools/hostsim_build.py examples/hostsim/sensor_node.c \
 *         -o /tmp/sensor.so
 */

void uart_init(void);
void uart_puts(char *s);
void uart_putdec(long v);
int uart_getc(void);

int link_send(const char *data, unsigned long n);
long link_recv(char *out, unsigned long max);

void irq_init(void);
void irq_enable(void);
void timer_start(int hz);
unsigned long timer_count(void);
unsigned long timer_freq(void);

long sim_encoder_read(void);
void sim_motor_write(long duty);
long sim_target(void);
void sim_set_target(long target);

#define REPORT_EVERY 100        /* milliseconds */

static unsigned long lost;
static unsigned long commands;

/* "R <position>" -- small enough to build without a formatter. */
static unsigned long format_report(char *buf, long value)
{
    unsigned long i = 0;
    long v = value;
    char digits[24];
    int n = 0;

    buf[i++] = 'R';
    buf[i++] = ' ';
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

static long parse_number(const char *s, long n)
{
    long value = 0;
    long i = 0;
    int negative = 0;
    while (i < n && (s[i] == ' ' || s[i] == 'T')) {
        i++;
    }
    if (i < n && s[i] == '-') {
        negative = 1;
        i++;
    }
    while (i < n && s[i] >= '0' && s[i] <= '9') {
        value = value * 10 + (s[i] - '0');
        i++;
    }
    return negative ? -value : value;
}

void kmain(void)
{
    char message[64];
    unsigned long f;
    unsigned long period;
    unsigned long next;
    int ms = 0;
    int running = 1;

    uart_init();
    irq_init();
    timer_start(1000);
    irq_enable();

    uart_puts("[sensor] up, reporting every 100ms\n");

    f = timer_freq();
    period = f / 1000UL;
    next = timer_count() + period;

    while (running && ms < 20000) {
        long position;
        long got;
        int c;

        while (timer_count() < next) {
        }
        next = next + period;

        position = sim_encoder_read();
        sim_motor_write((sim_target() - position) / 4);

        /* Commands from the other end of the link. */
        got = link_recv(message, sizeof(message));
        if (got > 0) {
            if (message[0] == 'T') {
                sim_set_target(parse_number(message, got));
                commands = commands + 1;
            }
        }

        /* A 'q' on the console stops us, which is what makes the receive
         * path worth having: it is how an operator intervenes. */
        c = uart_getc();
        if (c == 'q') {
            running = 0;
        }

        if ((ms % REPORT_EVERY) == 0) {
            unsigned long n = format_report(message, position);
            if (link_send(message, n) != 0) {
                lost = lost + 1;
            }
        }
        ms = ms + 1;
    }

    uart_puts("[sensor] stopping: commands=");
    uart_putdec((long)commands);
    uart_puts(" lost=");
    uart_putdec((long)lost);
    uart_puts("\n");
}
