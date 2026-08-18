/* console_arm64.c - formatting on top of whatever the board's UART is.
 *
 * Everything here is written in terms of a single uart_putc(), which each
 * board's driver supplies: uart_arm64.c for a PL011 (virt, Raspberry Pi) and
 * uart_8250.c for a 16550-style Tegra UART (Jetson). Nothing in this file
 * knows which, and nothing in it touches a register.
 *
 * Splitting it out is what stops a second board meaning a second copy of
 * uart_puthex -- and a second place for a formatting bug to be fixed in only
 * one of them.
 */

void uart_putc(int c);

void uart_puts(char *s)
{
    while (*s) {
        /* A bare LF leaves the cursor in place on a serial terminal, so the
         * next line overprints this one. */
        if (*s == '\n') {
            uart_putc('\r');
        }
        uart_putc(*s);
        s = s + 1;
    }
}

void uart_puthex(unsigned long v)
{
    char digits[16];
    int i;
    int any;

    uart_putc('0');
    uart_putc('x');
    if (v == 0) {
        uart_putc('0');
        return;
    }
    i = 0;
    while (v != 0) {
        int nib = (int)(v & 0xF);
        if (nib < 10) {
            digits[i] = (char)('0' + nib);
        } else {
            digits[i] = (char)('a' + (nib - 10));
        }
        v = v >> 4;
        i = i + 1;
    }
    any = i;
    while (any > 0) {
        any = any - 1;
        uart_putc((int)digits[any]);
    }
}

void uart_putdec(long v)
{
    char digits[24];
    int i;

    if (v < 0) {
        uart_putc('-');
        v = -v;
    }
    if (v == 0) {
        uart_putc('0');
        return;
    }
    i = 0;
    while (v != 0) {
        digits[i] = (char)('0' + (int)(v % 10));
        v = v / 10;
        i = i + 1;
    }
    while (i > 0) {
        i = i - 1;
        uart_putc((int)digits[i]);
    }
}
