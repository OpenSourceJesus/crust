/* uart_8250.c - 16550-style UART, for Tegra (Jetson Nano / TX1 / TX2 / Xavier).
 *
 * The first board in this tree whose console is not a PL011. The two are not
 * variations on a theme -- they are unrelated designs, and every assumption
 * the PL011 driver makes is wrong here:
 *
 *                        PL011                 16550 / Tegra
 *   data register        DR   at 0x00          THR  at 0x00
 *   "can I write?"       FR.TXFF, *set* when   LSR.THRE, *set* when
 *                        the FIFO is full      the holding register is empty
 *   baud rate            IBRD + FBRD, two      DLL + DLM, reachable only
 *                        plain registers       while LCR.DLAB is set
 *   register spacing     4 bytes, 32-bit       1 byte architecturally, but
 *                                              Tegra spaces them 4 apart
 *
 * The polarity difference is the dangerous one. `while (*fr & TXFF)` and
 * `while (!(*lsr & THRE))` are the same intent written inversely; copying the
 * PL011 loop shape here produces a driver that waits precisely when it should
 * write and writes precisely when it should wait.
 *
 * The register *spacing* is the other trap. A 16550's registers are one byte
 * apart in the original design, and much documentation is written that way,
 * but Tegra (like most SoCs that embed one on a 32-bit bus) puts them four
 * bytes apart. Using the unshifted offsets means LCR writes land on IIR/FCR
 * and the port is configured almost at random -- with no error, because every
 * address in the range decodes to some real register.
 *
 * Jetson Nano's debug console is UART-A at 0x70006000. UART-B/C/D sit at
 * 0x70006040, 0x70006200 and 0x70006300.
 */

#ifndef UART8250_BASE
#define UART8250_BASE   0x70006000
#endif

/* Distance between consecutive registers. Tegra uses 4; a plain ISA 16550
 * would use 1. Parameterised rather than assumed, because getting it wrong is
 * silent. */
#ifndef UART8250_SHIFT
#define UART8250_SHIFT  2
#endif

/* Input clock to the UART, used to derive the divisor. Tegra's UART-A runs
 * from a 408 MHz PLL divided down; U-Boot has usually already configured the
 * clock and the divisor by the time a kernel runs, which is why reprogramming
 * is optional here. */
#ifndef UART8250_CLK
#define UART8250_CLK    408000000
#endif

#ifndef UART8250_BAUD
#define UART8250_BAUD   115200
#endif

/* Register indices, before shifting. */
#define REG_RBR     0       /* read: received byte */
#define REG_THR     0       /* write: byte to transmit */
#define REG_DLL     0       /* divisor low, when LCR.DLAB is set */
#define REG_IER     1
#define REG_DLM     1       /* divisor high, when LCR.DLAB is set */
#define REG_IIR     2       /* read */
#define REG_FCR     2       /* write */
#define REG_LCR     3
#define REG_MCR     4
#define REG_LSR     5
#define REG_MSR     6

#define LCR_WLEN8   0x03    /* 8 data bits */
#define LCR_DLAB    0x80    /* divisor latch access */

#define FCR_ENABLE  0x01
#define FCR_CLR_RX  0x02
#define FCR_CLR_TX  0x04

#define MCR_DTR     0x01
#define MCR_RTS     0x02

#define LSR_DR      0x01    /* a byte has been received */
#define LSR_THRE    0x20    /* transmit holding register empty -- ready */
#define LSR_TEMT    0x40    /* transmitter completely idle */

static volatile unsigned int *u8250(int reg)
{
    return (volatile unsigned int *)(UART8250_BASE
                                     + ((unsigned long)reg << UART8250_SHIFT));
}

void uart_init(void)
{
    /* Interrupts off; this driver polls. */
    *u8250(REG_IER) = 0;

    /* Program the divisor. DLL and DLM are aliases of THR and IER, reachable
     * only while DLAB is set -- so DLAB must be cleared again afterwards or
     * every subsequent character write lands in the divisor latch and nothing
     * is ever transmitted. */
    {
        unsigned int div = (unsigned int)(UART8250_CLK
                                          / (16 * UART8250_BAUD));
        if (div == 0) {
            div = 1;
        }
        *u8250(REG_LCR) = LCR_DLAB;
        *u8250(REG_DLL) = div & 0xFF;
        *u8250(REG_DLM) = (div >> 8) & 0xFF;
        *u8250(REG_LCR) = LCR_WLEN8;        /* 8N1, and DLAB back to 0 */
    }

    /* Enable and clear both FIFOs. */
    *u8250(REG_FCR) = FCR_ENABLE | FCR_CLR_RX | FCR_CLR_TX;

    /* Assert DTR/RTS. Harmless with no flow control wired, and required by
     * anything that does honour it. */
    *u8250(REG_MCR) = MCR_DTR | MCR_RTS;
}

void uart_putc(int c)
{
    volatile unsigned int *lsr = u8250(REG_LSR);
    /* Note the polarity: wait while THRE is *clear*. The PL011 waits while
     * its flag is set. */
    while ((*lsr & LSR_THRE) == 0) {
    }
    *u8250(REG_THR) = (unsigned int)c & 0xFF;
}

/* ---- receive interrupts -------------------------------------------------
 * The 16550's IER is one register with a bit per source; ERBFI (bit 0) is
 * "enable received data available". There is no separate receive-timeout
 * interrupt to enable as there is on a PL011 -- the 16550's character timeout
 * is folded into the same interrupt and reported through IIR, so a single bit
 * covers both cases the PL011 needs two for.
 */
#define IER_ERBFI   0x01        /* received data available */

void uart_enable_rx_irq(void)
{
    *u8250(REG_IER) = IER_ERBFI;
}

void uart_disable_rx_irq(void)
{
    *u8250(REG_IER) = 0;
}

int uart_rx_pending(void)
{
    return (int)(*u8250(REG_LSR) & LSR_DR);
}

/* Drain the receive FIFO, returning how many bytes were taken. As on the
 * PL011, the FIFO must be emptied rather than read once: the interrupt stays
 * asserted while data remains, so a handler that takes a single byte
 * re-enters immediately and the machine livelocks. Reading RBR is what clears
 * the condition -- there is no separate acknowledge register. */
int uart_drain_rx(char *buf, int max)
{
    volatile unsigned int *lsr = u8250(REG_LSR);
    int n = 0;
    while ((*lsr & LSR_DR) != 0 && n < max) {
        buf[n] = (char)(*u8250(REG_RBR) & 0xFF);
        n = n + 1;
    }
    return n;
}

int uart_getc(void)
{
    volatile unsigned int *lsr = u8250(REG_LSR);
    while ((*lsr & LSR_DR) == 0) {
    }
    return (int)(*u8250(REG_RBR) & 0xFF);
}

/* Wait for the shift register to drain, not just the holding register. A
 * board that resets or powers down straight after printing loses the last
 * character or two otherwise -- THRE goes high as soon as the byte moves to
 * the shift register, well before it is on the wire. */
void uart_flush(void)
{
    volatile unsigned int *lsr = u8250(REG_LSR);
    while ((*lsr & LSR_TEMT) == 0) {
    }
}
