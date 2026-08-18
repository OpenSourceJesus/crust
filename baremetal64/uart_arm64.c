/* uart_arm64.c - PL011 UART driver for `qemu-system-aarch64 -M virt`.
 *
 * This is the AArch64 replacement for minikraft's VGA text console: on virt
 * there is no VGA, no BIOS, and no PS/2 controller. What there is, at a fixed
 * address in the device tree, is an ARM PL011 -- the same controller a
 * Raspberry Pi exposes as its primary UART, which is why this driver moves to
 * real hardware nearly unchanged (the Pi differs in base address and in
 * needing its clocks configured by firmware first).
 *
 * Compiled by ShivyCX itself, not by a cross gcc. Every access goes through a
 * volatile pointer so the polling loop below is a real loop: without volatile
 * a compiler is entitled to read FR once and spin forever on a stale value.
 *
 * Only uart_init() and uart_putc() live here. Everything above a single
 * character -- uart_puts, uart_puthex, uart_putdec -- is in console_arm64.c,
 * because none of it is PL011-specific and the Jetson's 8250 needs the same
 * formatting on top of a completely different register layout.
 */

/* Where the PL011 lives is the one thing that differs between boards, so it
 * is the one thing parameterised. The driver itself is identical: the Pi's
 * primary UART *is* a PL011, which is why the same code drives both.
 *
 *   qemu virt      0x09000000
 *   Pi 3 (BCM2837) 0x3F201000   peripherals at 0x3F000000
 *   Pi 4 (BCM2711) 0xFE201000   peripherals at 0xFE000000
 *
 * The Pi needs GPIO 14/15 switched to their ALT0 function before the UART is
 * connected to any pin; virt has no GPIO block at all. That is board setup,
 * not UART setup, so it lives in uart_gpio_init() which is a no-op unless
 * RASPI_GPIO_BASE is defined.
 */
#ifndef PL011_BASE
#define PL011_BASE  0x09000000
#endif

/* Register offsets, in bytes from the base. */
#define UART_DR     0x00        /* data */
#define UART_FR     0x18        /* flag */
#define UART_IBRD   0x24        /* integer baud rate divisor */
#define UART_FBRD   0x28        /* fractional baud rate divisor */
#define UART_LCRH   0x2C        /* line control */
#define UART_CR     0x30        /* control */
#define UART_IMSC   0x38        /* interrupt mask set/clear */
#define UART_MIS    0x40        /* masked interrupt status */
#define UART_ICR    0x44        /* interrupt clear */

/* Interrupt bits, the same layout in IMSC, MIS and ICR. */
#define INT_RX      0x010       /* receive */
#define INT_TX      0x020       /* transmit */
#define INT_RT      0x040       /* receive timeout: a partial FIFO has gone
                                 * stale. Without this, a byte that arrives
                                 * alone sits in the FIFO forever, because the
                                 * RX interrupt only fires at the trigger
                                 * level -- which is exactly what typing one
                                 * character at a time looks like. */

#define FR_TXFF     0x20        /* transmit FIFO full */
#define FR_RXFE     0x10        /* receive FIFO empty */

#define LCRH_FEN    0x10        /* enable FIFOs */
#define LCRH_WLEN8  0x60        /* 8 data bits */

#define CR_UARTEN   0x001       /* UART enable */
#define CR_TXE      0x100       /* transmit enable */
#define CR_RXE      0x200       /* receive enable */

static volatile unsigned int *uart_reg(unsigned long off)
{
    return (volatile unsigned int *)(PL011_BASE + off);
}

/* Route GPIO 14 and 15 to ALT0, which is where the PL011's TX and RX appear
 * on a Pi. Without this the UART runs but is wired to nothing, so it accepts
 * every byte and emits none -- indistinguishable from a dead console.
 *
 * GPFSEL1 holds three bits per pin for pins 10-19; 14 and 15 are fields 4 and
 * 5, so bits 12-14 and 15-17. ALT0 is 0b100.
 */
void uart_gpio_init(void)
{
#ifdef RASPI_GPIO_BASE
    volatile unsigned int *gpfsel1 =
        (volatile unsigned int *)(RASPI_GPIO_BASE + 0x04);
    volatile unsigned int *gppud =
        (volatile unsigned int *)(RASPI_GPIO_BASE + 0x94);
    volatile unsigned int *gppudclk0 =
        (volatile unsigned int *)(RASPI_GPIO_BASE + 0x98);
    unsigned int sel = *gpfsel1;
    int i;

    sel = sel & ~(unsigned int)(7 << 12);
    sel = sel & ~(unsigned int)(7 << 15);
    sel = sel | (unsigned int)(4 << 12);        /* GPIO14 -> ALT0 (TXD0) */
    sel = sel | (unsigned int)(4 << 15);        /* GPIO15 -> ALT0 (RXD0) */
    *gpfsel1 = sel;

    /* Disable pull-up/down on both pins. The sequence is fixed by the
     * BCM2835 datasheet: write the control, wait 150 cycles, write the clock
     * mask, wait again, then clear both. The waits are why this cannot be
     * collapsed -- the hardware latches on a delay, not on a handshake. */
    *gppud = 0;
    for (i = 0; i < 150; i = i + 1) {
    }
    *gppudclk0 = (1u << 14) | (1u << 15);
    for (i = 0; i < 150; i = i + 1) {
    }
    *gppud = 0;
    *gppudclk0 = 0;
#endif
}

void uart_init(void)
{
    volatile unsigned int *cr = uart_reg(UART_CR);

    uart_gpio_init();

    /* Disable while reconfiguring; changing LCRH with the UART enabled is
     * undefined. qemu tolerates it, real PL011 silicon does not. */
    *cr = 0;

    /* qemu ignores the baud divisors (there is no real wire to clock), but
     * they are set anyway so the same code drives hardware. These give
     * 115200 baud from a 24 MHz UARTCLK, which is what the virt machine and
     * a Pi both use: 24000000 / (16 * 115200) = 13.02. */
    *uart_reg(UART_IBRD) = 13;
    *uart_reg(UART_FBRD) = 1;

    *uart_reg(UART_LCRH) = LCRH_FEN | LCRH_WLEN8;
    *uart_reg(UART_IMSC) = 0;           /* poll; no interrupts yet */
    *cr = CR_UARTEN | CR_TXE | CR_RXE;
}

/* Ask the PL011 to interrupt on receive. Both RX and RT are needed: RX fires
 * when the FIFO reaches its trigger level, RT when a partially filled FIFO has
 * been idle. Enabling only RX gives a console that appears dead until enough
 * characters arrive at once to cross the threshold. */
void uart_enable_rx_irq(void)
{
    *uart_reg(UART_ICR) = 0x7FF;            /* clear anything stale first */
    *uart_reg(UART_IMSC) = INT_RX | INT_RT;
}

void uart_disable_rx_irq(void)
{
    *uart_reg(UART_IMSC) = 0;
}

/* Nonzero if this UART is the thing currently asserting. */
int uart_rx_pending(void)
{
    return (int)(*uart_reg(UART_MIS) & (INT_RX | INT_RT));
}

/* Drain the receive FIFO, returning how many bytes were taken. The FIFO must
 * be emptied, not merely read once: the interrupt stays asserted while data
 * remains, so a handler that reads a single byte and returns re-enters
 * immediately and the machine livelocks. */
int uart_drain_rx(char *buf, int max)
{
    volatile unsigned int *fr = uart_reg(UART_FR);
    int n = 0;
    while ((*fr & FR_RXFE) == 0 && n < max) {
        buf[n] = (char)(*uart_reg(UART_DR) & 0xFF);
        n = n + 1;
    }
    /* Clear the RX and receive-timeout conditions once the FIFO is empty. */
    *uart_reg(UART_ICR) = INT_RX | INT_RT;
    return n;
}

void uart_putc(int c)
{
    volatile unsigned int *fr = uart_reg(UART_FR);
    while (*fr & FR_TXFF) {
    }
    *uart_reg(UART_DR) = (unsigned int)c;
}
