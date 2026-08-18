/* exc_arm64.c - the C side of the AArch64 exception vectors.
 *
 * vectors_arm64.S saves state and calls exc_handler with the slot number and
 * the three registers that describe what happened:
 *
 *   ESR_EL1  - Exception Syndrome. Bits [31:26] are the exception class (EC),
 *              which says *what kind* of fault; the low bits (ISS) say more,
 *              and their meaning depends on the class.
 *   FAR_EL1  - Fault Address, for the classes that have one. For anything
 *              else it holds a stale value from an earlier fault, so it is
 *              only printed when the class actually defines it.
 *   ELR_EL1  - the instruction to return to. For a synchronous fault this is
 *              the faulting instruction itself, which is what makes it the
 *              useful number: it points straight at the bug.
 *
 * Compiled by ShivyCX, like the rest of the AArch64 OS pieces.
 */

void uart_puts(char *s);
void uart_puthex(unsigned long v);
void uart_putdec(long v);

/* Slot names, in the order vectors_arm64.S numbers them. */
static char *slot_name(int kind)
{
    if (kind == 0) { return "EL1t synchronous"; }
    if (kind == 1) { return "EL1t IRQ"; }
    if (kind == 2) { return "EL1t FIQ"; }
    if (kind == 3) { return "EL1t SError"; }
    if (kind == 4) { return "EL1h synchronous"; }
    if (kind == 5) { return "EL1h IRQ"; }
    if (kind == 6) { return "EL1h FIQ"; }
    if (kind == 7) { return "EL1h SError"; }
    if (kind == 8) { return "EL0 aarch64 synchronous"; }
    if (kind == 9) { return "EL0 aarch64 IRQ"; }
    if (kind == 10) { return "EL0 aarch64 FIQ"; }
    if (kind == 11) { return "EL0 aarch64 SError"; }
    return "EL0 aarch32";
}

static char *ec_name(int ec)
{
    if (ec == 0x00) { return "unknown"; }
    if (ec == 0x0E) { return "illegal execution state"; }
    if (ec == 0x15) { return "SVC from aarch64"; }
    if (ec == 0x18) { return "trapped MSR/MRS"; }
    if (ec == 0x07) { return "SIMD/FP access trapped (CPACR_EL1)"; }
    if (ec == 0x20) { return "instruction abort, lower EL"; }
    if (ec == 0x21) { return "instruction abort, same EL"; }
    if (ec == 0x22) { return "PC alignment fault"; }
    if (ec == 0x24) { return "data abort, lower EL"; }
    if (ec == 0x25) { return "data abort, same EL"; }
    if (ec == 0x26) { return "SP alignment fault"; }
    if (ec == 0x2C) { return "trapped FP exception"; }
    if (ec == 0x30) { return "breakpoint, lower EL"; }
    if (ec == 0x31) { return "breakpoint, same EL"; }
    if (ec == 0x3C) { return "BRK instruction"; }
    return "other";
}

/* Whether FAR_EL1 is meaningful for this exception class. Printing it
 * unconditionally is worse than not printing it: a stale address from a
 * previous fault looks exactly like a real one. */
static int ec_has_far(int ec)
{
    if (ec == 0x20) { return 1; }
    if (ec == 0x21) { return 1; }
    if (ec == 0x22) { return 1; }
    if (ec == 0x24) { return 1; }
    if (ec == 0x25) { return 1; }
    if (ec == 0x26) { return 1; }
    return 0;
}

/* Data/instruction abort ISS: the low six bits are the fault status code. */
static char *dfsc_name(int dfsc)
{
    int level = dfsc & 3;
    if (dfsc >= 0x04 && dfsc <= 0x07) {
        if (level == 0) { return "translation fault, level 0"; }
        if (level == 1) { return "translation fault, level 1"; }
        if (level == 2) { return "translation fault, level 2"; }
        return "translation fault, level 3";
    }
    if (dfsc >= 0x08 && dfsc <= 0x0B) { return "access flag fault"; }
    if (dfsc >= 0x0C && dfsc <= 0x0F) { return "permission fault"; }
    if (dfsc == 0x10) { return "external abort"; }
    if (dfsc == 0x21) { return "alignment fault"; }
    return "other fault status";
}

/* Set by a test before it deliberately faults; see exc_expect(). When
 * nonzero, a synchronous fault is reported and then *skipped over* rather
 * than halting, by advancing the return address past the faulting
 * instruction. Every AArch64 instruction is four bytes, which is what makes
 * this legal here and impossible on x86. */
static int exc_expected;
static int exc_count;

void exc_expect(int n)
{
    exc_expected = n;
}

int exc_taken(void)
{
    return exc_count;
}

/* Slot kinds that are interrupts rather than faults. An IRQ is not an error:
 * it must be serviced and returned from, not reported and halted on. Getting
 * this wrong is not subtle -- the first timer tick would print a fault dump
 * and stop the machine -- but the *shape* of it matters, because an IRQ slot
 * carries no ESR worth decoding and no faulting address. */
static int is_irq_kind(int kind)
{
    if (kind == 1 || kind == 5 || kind == 9 || kind == 13) {
        return 1;               /* IRQ, in each of the four groups */
    }
    return 0;
}

void irq_dispatch(void);

/* Returning normally resumes at ELR_EL1; the assembly stub does the eret. */
unsigned long exc_handler(int kind, unsigned long esr, unsigned long far,
                          unsigned long elr)
{
    int ec = (int)((esr >> 26) & 0x3F);
    int dfsc = (int)(esr & 0x3F);

    if (is_irq_kind(kind)) {
        /* Not counted as an exception: exc_taken() is what the fault tests
         * assert on, and a timer running underneath them would make those
         * counts drift. */
        irq_dispatch();
        return 0;
    }

    exc_count = exc_count + 1;

    uart_puts("\n*** exception: ");
    uart_puts(slot_name(kind));
    uart_puts("\n    ESR = ");
    uart_puthex(esr);
    uart_puts("  EC = ");
    uart_puthex((unsigned long)ec);
    uart_puts(" (");
    uart_puts(ec_name(ec));
    uart_puts(")\n");

    if (ec == 0x24 || ec == 0x25 || ec == 0x20 || ec == 0x21) {
        uart_puts("    ");
        uart_puts(dfsc_name(dfsc));
        uart_puts("\n");
    }
    if (ec_has_far(ec)) {
        uart_puts("    FAR = ");
        uart_puthex(far);
        uart_puts("\n");
    }
    uart_puts("    ELR = ");
    uart_puthex(elr);
    uart_puts("\n");

    if (exc_expected) {
        exc_expected = 0;
        uart_puts("    (expected; skipping the faulting instruction)\n");
        /* 1 tells the vector stub to advance ELR_EL1 by one instruction. */
        return 1;
    }

    uart_puts("    unhandled -- halting\n");
    for (;;) {
    }
    return 0;
}
