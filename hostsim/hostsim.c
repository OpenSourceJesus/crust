/* hostsim.c - run a crust bare-metal application natively on the host.
 *
 * This is not an emulator and does not pretend to be one. armulator executes
 * AArch64 instructions one at a time, which is what you want when the
 * question is "does this image boot" -- and which runs at roughly 17,000
 * instructions a second, some five orders of magnitude slower than the
 * hardware. That is fine for bringing up an MMU and hopeless for simulating
 * twenty boards driving motors and talking to each other.
 *
 * So this takes the other road. The application's C is compiled for the host
 * by the host compiler and runs at native speed; only the layer underneath it
 * -- console, timer, interrupt controller, MMU, exceptions -- is replaced by
 * the implementations here. The same idea as Zephyr's native_posix or NuttX's
 * sim target: keep the application, replace the hardware.
 *
 * What that buys and what it costs:
 *
 *   + native speed, so wall-clock simulation of many boards is possible
 *   + the host's debugger, sanitisers and profilers all work
 *   + CUDA, BLAS, matplotlib and sockets are reachable, because this is an
 *     ordinary host process
 *   - it does not execute a single ARM instruction, so it proves nothing
 *     about code generation, instruction selection or the boot sequence
 *   - anything that depends on the real memory map, on register-level device
 *     behaviour, or on AArch64 semantics is modelled here rather than run
 *
 * The two modes answer different questions and neither replaces the other.
 * Use armulator to prove the image is correct; use this to run the system.
 * tools/hostsim_difftest.py exists to check they still agree.
 *
 * Virtual time is driven from outside. The application's delay loops spin on
 * timer_count(), which blocks until the controlling process grants more time
 * with sim_step(). That makes runs deterministic and repeatable regardless of
 * host load, and it is what lets several boards be stepped in lockstep.
 */

#define _GNU_SOURCE
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

#include "hostsim.h"
#include "accel.h"

/* ------------------------------------------------------------------ */
/* Simulated board state                                              */
/* ------------------------------------------------------------------ */

#define UART_BUFFER 65536
#define NET_QUEUE   256
#define NET_MTU     512

struct sim_state {
    /* Console. Transmitted bytes accumulate for the controller to read. */
    char tx[UART_BUFFER];
    unsigned long tx_len;
    char rx[UART_BUFFER];
    unsigned long rx_head, rx_tail;

    /* Architected counter, in ticks of `freq`. Advanced only by sim_step. */
    unsigned long now;
    unsigned long deadline;
    unsigned long freq;

    /* Timer interrupt. */
    int timer_hz;
    int timer_running;
    unsigned long next_tick;
    unsigned long tick_count;
    unsigned long spurious_count;
    unsigned long unexpected_count;
    int irqs_enabled;

    /* Point-to-point link, for boards talking to each other or to something
     * off the bench. Framed as whole messages rather than a byte stream:
     * every transport these boards actually use -- CAN, a UDP datagram, a
     * length-prefixed frame over a UART -- delivers messages, and a byte
     * stream would leave every application reimplementing the framing. */
    char net_out[NET_QUEUE][NET_MTU];
    unsigned long net_out_len[NET_QUEUE];
    unsigned long net_out_head, net_out_tail;
    char net_in[NET_QUEUE][NET_MTU];
    unsigned long net_in_len[NET_QUEUE];
    unsigned long net_in_head, net_in_tail;
    unsigned long net_sent, net_received, net_dropped;

    /* Injected faults. Each is something that is a nuisance to arrange on a
     * bench and one call here: a sensor that stops updating, a link that
     * silently drops traffic, a console that garbles. */
    int encoder_stuck;
    long encoder_bias;
    int link_down;
    unsigned long link_drop_every;
    unsigned long link_counter;

    /* MMU. Nothing is translated here; the flag exists so the application's
     * reports read the same as they do on hardware. */
    int mmu_on;

    /* Exception injection. `taken` is a cumulative count, not a flag:
     * exc_arm64.c returns exc_count, which exc_expect() does not reset. */
    int expecting;
    int taken;
};

static struct sim_state sim;

/* Handshake between the application thread and the controlling process. */
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t app_cv = PTHREAD_COND_INITIALIZER;
static pthread_cond_t ctl_cv = PTHREAD_COND_INITIALIZER;
static pthread_t app_thread;
static int app_waiting;
static int app_done;
static int app_started;

/* ------------------------------------------------------------------ */
/* Faults                                                             */
/* ------------------------------------------------------------------ */

/* A bare-metal image tests its exception path by storing to an address with
 * nothing behind it and expecting to come back from the abort handler. There
 * is no abort handler here -- the host delivers SIGSEGV instead -- so the
 * fault is caught and control returns to the point exc_expect() marked.
 *
 * This means the recovery point is the exc_expect() call rather than the
 * instruction after the faulting store, so anything between the two is
 * skipped rather than resumed. For the "expect a fault, check it happened"
 * shape the images use, that is indistinguishable. It is not general
 * exception emulation and should not be mistaken for it.
 */
/* A bare-metal image tests its exception path by storing to an address with
 * nothing behind it and expecting to come back from the abort handler. On the
 * board the MMU raises a translation fault, the handler notes it and skips
 * the instruction. There is no MMU here, so the host raises SIGSEGV instead.
 *
 * The recovery is to map a page at the faulting address and return from the
 * handler, which re-executes the instruction -- now against real memory. What
 * the application observes is what it observes on hardware: the access was
 * taken as a fault, exc_taken() reports it, and execution continues. It works
 * for any faulting instruction without decoding one, which a skip-the-
 * instruction approach would need on x86-64.
 *
 * The scratch page is dropped again by the next exc_expect(), so an image that
 * faults twice at the same address faults twice here too.
 *
 * This is fault *injection*, not exception emulation. There is no ESR, no
 * FAR, no vector table and no exception level. An image that inspects those
 * belongs under armulator.
 */
#define FAULT_PAGE 4096UL

static volatile int fault_armed;
static void *fault_page;

static void drop_fault_page(void)
{
    if (fault_page) {
        munmap(fault_page, FAULT_PAGE);
        fault_page = NULL;
    }
}

static void on_fault(int signo, siginfo_t *info, void *context)
{
    (void)context;
    if (fault_armed && info != NULL && info->si_addr != NULL) {
        unsigned long addr = (unsigned long)info->si_addr;
        void *page = (void *)(addr & ~(FAULT_PAGE - 1));
        void *got = mmap(page, FAULT_PAGE, PROT_READ | PROT_WRITE,
                         MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
        if (got != MAP_FAILED) {
            drop_fault_page();
            fault_page = got;
            fault_armed = 0;
            sim.taken = sim.taken + 1;
            return;             /* re-execute against the new page */
        }
    }
    /* Not expected, or unrecoverable: crash with the usual diagnostics. */
    signal(signo, SIG_DFL);
    raise(signo);
}

static void install_fault_handler(void)
{
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_sigaction = on_fault;
    sa.sa_flags = SA_SIGINFO | SA_NODEFER;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGBUS, &sa, NULL);
}

/* ------------------------------------------------------------------ */
/* Console                                                            */
/* ------------------------------------------------------------------ */

void uart_init(void) { }

static void uart_putc_host(char c)
{
    if (sim.tx_len < UART_BUFFER) {
        sim.tx[sim.tx_len] = c;
        sim.tx_len = sim.tx_len + 1;
    }
}

void uart_puts(char *s)
{
    while (*s) {
        uart_putc_host(*s);
        s++;
    }
}

void uart_puthex(unsigned long v)
{
    char digits[17] = "0123456789abcdef";
    char buf[19];
    int i = 0;
    buf[i++] = '0';
    buf[i++] = 'x';
    if (v == 0) {
        buf[i++] = '0';
    } else {
        char tmp[16];
        int n = 0;
        while (v) {
            tmp[n++] = digits[v & 0xF];
            v >>= 4;
        }
        while (n) {
            buf[i++] = tmp[--n];
        }
    }
    buf[i] = 0;
    uart_puts(buf);
}

void uart_putdec(long v)
{
    char buf[24];
    int i = 0;
    int neg = 0;
    unsigned long u;
    if (v < 0) {
        neg = 1;
        u = (unsigned long)(-v);
    } else {
        u = (unsigned long)v;
    }
    if (u == 0) {
        buf[i++] = '0';
    } else {
        char tmp[24];
        int n = 0;
        while (u) {
            tmp[n++] = (char)('0' + (u % 10));
            u /= 10;
        }
        while (n) {
            buf[i++] = tmp[--n];
        }
    }
    buf[i] = 0;
    if (neg) {
        uart_puts("-");
    }
    uart_puts(buf);
}

int uart_rx_ready(void)
{
    int ready;
    pthread_mutex_lock(&lock);
    ready = sim.rx_head != sim.rx_tail;
    pthread_mutex_unlock(&lock);
    return ready;
}

/* Returns -1 when nothing has arrived, so a polling loop can spin without
 * blocking the virtual clock. A blocking read would deadlock: the controller
 * cannot feed the console while the application thread holds time. */
int uart_getc(void)
{
    int c = -1;
    pthread_mutex_lock(&lock);
    if (sim.rx_head != sim.rx_tail) {
        c = (unsigned char)sim.rx[sim.rx_tail];
        sim.rx_tail = (sim.rx_tail + 1) % UART_BUFFER;
    }
    pthread_mutex_unlock(&lock);
    return c;
}

/* ------------------------------------------------------------------ */
/* Link                                                               */
/* ------------------------------------------------------------------ */

/* Queue a message for whatever is on the other end. Returns 0 if it was
 * accepted, -1 if it was not -- a full queue or a downed link. Firmware that
 * ignores the return value will lose messages exactly as it would on a real
 * link, which is the point of returning it. */
int link_send(const char *data, unsigned long n)
{
    unsigned long next;
    int result = -1;
    if (n > NET_MTU) {
        return -1;
    }
    pthread_mutex_lock(&lock);

    sim.link_counter = sim.link_counter + 1;
    if (sim.link_down ||
        (sim.link_drop_every &&
         (sim.link_counter % sim.link_drop_every) == 0)) {
        sim.net_dropped = sim.net_dropped + 1;
        pthread_mutex_unlock(&lock);
        return -1;                  /* silently lost, as on a real link */
    }

    next = (sim.net_out_head + 1) % NET_QUEUE;
    if (next != sim.net_out_tail) {
        memcpy(sim.net_out[sim.net_out_head], data, n);
        sim.net_out_len[sim.net_out_head] = n;
        sim.net_out_head = next;
        sim.net_sent = sim.net_sent + 1;
        result = 0;
    }
    pthread_mutex_unlock(&lock);
    return result;
}

/* Take the next message, or -1 if none. Non-blocking, for the same reason
 * uart_getc is. */
long link_recv(char *out, unsigned long max)
{
    long n = -1;
    pthread_mutex_lock(&lock);
    if (sim.net_in_head != sim.net_in_tail) {
        unsigned long len = sim.net_in_len[sim.net_in_tail];
        if (len > max) {
            len = max;
        }
        memcpy(out, sim.net_in[sim.net_in_tail], len);
        sim.net_in_tail = (sim.net_in_tail + 1) % NET_QUEUE;
        sim.net_received = sim.net_received + 1;
        n = (long)len;
    }
    pthread_mutex_unlock(&lock);
    return n;
}

/* ------------------------------------------------------------------ */
/* MMU                                                                */
/* ------------------------------------------------------------------ */

void mmu_init(void) { }

void mmu_enable(void) { sim.mmu_on = 1; }

unsigned long read_sctlr(void)
{
    /* The values the images print, so their output is comparable with a real
     * run. Nothing here depends on them. */
    return sim.mmu_on ? 0xc5183dUL : 0xc50838UL;
}

void mmu_report(void)
{
    uart_puts("  SCTLR = ");
    uart_puthex(read_sctlr());
    uart_puts(sim.mmu_on ? "  MMU=1 (host: no translation)\n"
                         : "  MMU=0\n");
}

/* ------------------------------------------------------------------ */
/* Exceptions                                                         */
/* ------------------------------------------------------------------ */

void exc_expect(int n)
{
    (void)n;
    sim.expecting = 1;
    /* Drop any page mapped by a previous recovery, so an image that faults
     * twice at the same address faults twice. */
    drop_fault_page();
    fault_armed = 1;
}

int exc_taken(void)
{
    return sim.taken;
}

/* ------------------------------------------------------------------ */
/* Timer and interrupts                                               */
/* ------------------------------------------------------------------ */

unsigned long timer_freq(void) { return sim.freq; }

/* Block until the controller has granted enough virtual time. This is the
 * only place the application thread yields, which is what makes the schedule
 * deterministic: the application runs flat out between grants. */
unsigned long timer_count(void)
{
    unsigned long value;
    pthread_mutex_lock(&lock);
    while (sim.now >= sim.deadline && !app_done) {
        app_waiting = 1;
        pthread_cond_signal(&ctl_cv);
        pthread_cond_wait(&app_cv, &lock);
    }
    /* Consume the grant. `now` must lag `deadline` until the application
     * asks for the time, or the wait condition above is true the instant it
     * wakes and it can never make progress. */
    sim.now = sim.deadline;
    app_waiting = 0;
    value = sim.now;
    pthread_mutex_unlock(&lock);
    return value;
}

void timer_start(int hz)
{
    sim.timer_hz = hz;
    sim.timer_running = 1;
    sim.next_tick = sim.now + (hz > 0 ? sim.freq / (unsigned long)hz : 0);
}

void timer_selftest(void) { }

void irq_init(void) { }
void irq_enable(void) { sim.irqs_enabled = 1; }
void irq_disable(void) { sim.irqs_enabled = 0; }

unsigned long ticks(void) { return sim.tick_count; }
unsigned long spurious(void) { return sim.spurious_count; }
unsigned long unexpected(void) { return sim.unexpected_count; }

/* ------------------------------------------------------------------ */
/* Controller interface, called over ctypes                           */
/* ------------------------------------------------------------------ */

static void *app_entry(void *arg)
{
    (void)arg;
    install_fault_handler();
    kmain();
    pthread_mutex_lock(&lock);
    app_done = 1;
    app_waiting = 1;
    pthread_cond_signal(&ctl_cv);
    pthread_mutex_unlock(&lock);
    return NULL;
}

void sim_init(unsigned long freq)
{
    memset(&sim, 0, sizeof(sim));
    sim.freq = freq ? freq : 19200000UL;
    app_waiting = 0;
    app_done = 0;
    app_started = 0;
}

void sim_start(void)
{
    if (app_started) {
        return;
    }
    app_started = 1;
    pthread_create(&app_thread, NULL, app_entry, NULL);
    /* Let it run until it first asks for time. */
    pthread_mutex_lock(&lock);
    while (!app_waiting && !app_done) {
        pthread_cond_wait(&ctl_cv, &lock);
    }
    pthread_mutex_unlock(&lock);
}

/* Advance virtual time by `count` counter ticks and let the application run
 * until it asks for more. Returns the number of timer interrupts that fell in
 * the interval. */
unsigned long sim_step(unsigned long count)
{
    unsigned long fired = 0;
    pthread_mutex_lock(&lock);
    if (app_done) {
        pthread_mutex_unlock(&lock);
        return 0;
    }
    sim.deadline = sim.deadline + count;
    /* Clear the flag before waking the application, or the wait below sees
     * the flag it set on the *previous* block, returns immediately, and the
     * controller runs ahead of the simulation it is supposed to be driving. */
    app_waiting = 0;
    pthread_cond_signal(&app_cv);
    while (!app_waiting && !app_done) {
        pthread_cond_wait(&ctl_cv, &lock);
    }

    /* Timer interrupts are counted against `now` -- the time the application
     * has actually consumed -- and only once it has stopped for more. Counting
     * against `deadline` instead credits ticks for time that has been granted
     * but not yet reached, which shows up as a tick count comfortably higher
     * than the elapsed period can account for. */
    if (sim.timer_running && sim.timer_hz > 0) {
        unsigned long period = sim.freq / (unsigned long)sim.timer_hz;
        while (period > 0 && sim.next_tick <= sim.now) {
            if (sim.irqs_enabled) {
                sim.tick_count = sim.tick_count + 1;
                fired = fired + 1;
            }
            sim.next_tick = sim.next_tick + period;
        }
    }
    pthread_mutex_unlock(&lock);
    return fired;
}

int sim_finished(void)
{
    int done;
    pthread_mutex_lock(&lock);
    done = app_done;
    pthread_mutex_unlock(&lock);
    return done;
}

/* Copy out everything transmitted since the last call. */
unsigned long sim_uart_read(char *out, unsigned long max)
{
    unsigned long n;
    pthread_mutex_lock(&lock);
    n = sim.tx_len < max ? sim.tx_len : max;
    memcpy(out, sim.tx, n);
    sim.tx_len = 0;
    pthread_mutex_unlock(&lock);
    return n;
}

/* Push bytes into the console receive path, as a terminal would. */
void sim_uart_feed(const char *data, unsigned long n)
{
    unsigned long i;
    pthread_mutex_lock(&lock);
    for (i = 0; i < n; i++) {
        unsigned long next = (sim.rx_head + 1) % UART_BUFFER;
        if (next != sim.rx_tail) {
            sim.rx[sim.rx_head] = data[i];
            sim.rx_head = next;
        }
    }
    pthread_mutex_unlock(&lock);
}

/* ------------------------------------------------------------------ */
/* Actuators and sensors                                              */
/* ------------------------------------------------------------------ */

/* Deliberately not a model of a motor. The plant -- inertia, friction, the
 * encoder's noise and quantisation -- belongs to the controlling process,
 * where it can be written in numpy and changed without recompiling. All that
 * lives here is the pair of values the application reads and writes, which is
 * what the wire between a board and a motor driver actually is.
 *
 * A real board reaches these through a PWM peripheral and a quadrature
 * counter; the seam is drawn at the value rather than the register because
 * this path is for the system question, not the driver question. Test the
 * driver against armulator, where the registers exist. */
static long motor_duty;
static long encoder_position;
static long encoder_held;
static long motor_target;

/* The injected sensor faults live here rather than in the plant model,
 * because they are faults in the *sensor*, not in the mechanism: a stuck
 * encoder still reads plausibly while the shaft turns, which is precisely
 * what makes it hard to diagnose and worth testing against. */
long sim_encoder_read(void)
{
    if (sim.encoder_stuck) {
        return encoder_held + sim.encoder_bias;
    }
    return encoder_position + sim.encoder_bias;
}
void sim_motor_write(long duty) { motor_duty = duty; }
long sim_target(void) { return motor_target; }

long sim_motor_duty(void) { return motor_duty; }
void sim_set_encoder(long position) { encoder_position = position; }
void sim_set_target(long target) { motor_target = target; }

/* -- link, from the controller's side ------------------------------- */

/* Take one message the application has sent, or 0 if there is none. */
unsigned long sim_link_pop(char *out, unsigned long max)
{
    unsigned long n = 0;
    pthread_mutex_lock(&lock);
    if (sim.net_out_head != sim.net_out_tail) {
        n = sim.net_out_len[sim.net_out_tail];
        if (n > max) {
            n = max;
        }
        memcpy(out, sim.net_out[sim.net_out_tail], n);
        sim.net_out_tail = (sim.net_out_tail + 1) % NET_QUEUE;
    }
    pthread_mutex_unlock(&lock);
    return n;
}

/* Deliver a message to the application. Returns 0 on success, -1 if the
 * queue is full -- which the caller should treat as backpressure rather
 * than ignore. */
int sim_link_push(const char *data, unsigned long n)
{
    unsigned long next;
    int result = -1;
    if (n > NET_MTU) {
        return -1;
    }
    pthread_mutex_lock(&lock);
    next = (sim.net_in_head + 1) % NET_QUEUE;
    if (next != sim.net_in_tail) {
        memcpy(sim.net_in[sim.net_in_head], data, n);
        sim.net_in_len[sim.net_in_head] = n;
        sim.net_in_head = next;
        result = 0;
    }
    pthread_mutex_unlock(&lock);
    return result;
}

unsigned long sim_link_sent(void) { return sim.net_sent; }
unsigned long sim_link_received(void) { return sim.net_received; }
unsigned long sim_link_dropped(void) { return sim.net_dropped; }

/* -- injected faults ------------------------------------------------ */

/* Freeze at the value the sensor holds *now*, rather than at whatever it
 * happens to be read as later: a sensor that seizes does so at a particular
 * moment, and capturing lazily on the next read means it freezes at zero if
 * nothing has read it yet. */
void sim_fault_encoder_stuck(int on)
{
    if (on && !sim.encoder_stuck) {
        encoder_held = encoder_position;
    }
    sim.encoder_stuck = on;
}
void sim_fault_encoder_bias(long counts) { sim.encoder_bias = counts; }
void sim_fault_link_down(int on) { sim.link_down = on; }
void sim_fault_link_drop_every(unsigned long n) { sim.link_drop_every = n; }

/* -- frames, for a vision workload ---------------------------------- */

/* One frame of slack, not a queue. A frame that has not been consumed by the
 * time the next arrives is overwritten and the old one is gone, which is what
 * a camera DMAing into a double buffer actually does -- and it is why
 * vision_node.c counts dropped frames instead of assuming it sees them all. */
static unsigned char frame_buffer[ACCEL_FRAME];
static int frame_pending;
static unsigned long frames_pushed;
static unsigned long frames_overwritten;

int sim_frame_ready(void) { return frame_pending; }
const unsigned char *sim_frame_data(void) { return frame_buffer; }
void sim_frame_consume(void) { frame_pending = 0; }

void sim_push_frame(const unsigned char *data, unsigned long n)
{
    unsigned long i;
    pthread_mutex_lock(&lock);
    if (frame_pending) {
        frames_overwritten = frames_overwritten + 1;
    }
    for (i = 0; i < ACCEL_FRAME; i++) {
        frame_buffer[i] = i < n ? data[i] : 0;
    }
    frame_pending = 1;
    frames_pushed = frames_pushed + 1;
    pthread_mutex_unlock(&lock);
}

unsigned long sim_frames_pushed(void) { return frames_pushed; }
unsigned long sim_frames_overwritten(void) { return frames_overwritten; }
unsigned long sim_frame_size(void) { return ACCEL_FRAME; }

/* Exposed so the controller can report and check the backend without the
 * firmware having to print it. */
const char *sim_accel_backend(void) { return accel_backend(); }
int sim_accel_available(void) { return accel_available(); }
int sim_accel_selftest(void) { return accel_selftest(); }

unsigned long sim_ticks(void) { return sim.tick_count; }
unsigned long sim_now(void) { return sim.now; }
int sim_mmu_on(void) { return sim.mmu_on; }
