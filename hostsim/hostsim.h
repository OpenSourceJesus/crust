/* hostsim.h - the seam between a bare-metal application and its hardware.
 *
 * Everything above the line is what the application calls and what
 * baremetal64/ implements on real hardware; hostsim.c implements the same
 * names for the host. Everything below is what the controlling process calls
 * over ctypes to drive the simulation.
 */
#ifndef HOSTSIM_H
#define HOSTSIM_H

/* Provided by the application. */
void kmain(void);

/* -- the seam: same signatures as baremetal64/ ---------------------- */
void uart_init(void);
void uart_puts(char *s);
void uart_puthex(unsigned long v);
void uart_putdec(long v);

/* Console receive. uart_getc returns -1 when nothing has arrived. */
int uart_rx_ready(void);
int uart_getc(void);

/* Message link to other boards or to something off the bench.
 * link_send returns 0 if accepted; link_recv returns -1 if nothing waits. */
int link_send(const char *data, unsigned long n);
long link_recv(char *out, unsigned long max);

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

/* -- actuators and sensors, as seen by the application -------------- */
long sim_encoder_read(void);
void sim_motor_write(long duty);
long sim_target(void);

/* -- frames, as seen by the application ----------------------------- */
int sim_frame_ready(void);
const unsigned char *sim_frame_data(void);
void sim_frame_consume(void);

/* -- controller interface, called over ctypes ----------------------- */
void sim_push_frame(const unsigned char *data, unsigned long n);
unsigned long sim_frames_pushed(void);
unsigned long sim_frames_overwritten(void);
unsigned long sim_frame_size(void);
const char *sim_accel_backend(void);
int sim_accel_available(void);
int sim_accel_selftest(void);

unsigned long sim_link_pop(char *out, unsigned long max);
int sim_link_push(const char *data, unsigned long n);
unsigned long sim_link_sent(void);
unsigned long sim_link_received(void);
unsigned long sim_link_dropped(void);

void sim_fault_encoder_stuck(int on);
void sim_fault_encoder_bias(long counts);
void sim_fault_link_down(int on);
void sim_fault_link_drop_every(unsigned long n);

long sim_motor_duty(void);
void sim_set_encoder(long position);
void sim_set_target(long target);

void sim_init(unsigned long freq);
void sim_start(void);
unsigned long sim_step(unsigned long count);
int sim_finished(void);
unsigned long sim_uart_read(char *out, unsigned long max);
void sim_uart_feed(const char *data, unsigned long n);
unsigned long sim_ticks(void);
unsigned long sim_now(void);
int sim_mmu_on(void);

#endif
