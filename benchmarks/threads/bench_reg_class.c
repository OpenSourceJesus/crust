/* Context-switch register-class levels for Crust-ELF hints.
   Measures how many GPRs a switcher would save at each reg_class.
   Classes: 0=minimal (rbx,rbp,r12-r15), 1=+r8-r11, 2=full GPR, 3=+xmm0-7 */
#include <stdio.h>

enum { REG_MIN = 0, REG_EXTRA = 1, REG_FULL = 2, REG_SIMD = 3 };

static int regs_for_class(int cls) {
    /* Counts match crustos switcher / thread_contracts philosophy. */
    if (cls == REG_MIN) return 6;       /* callee-saved only */
    if (cls == REG_EXTRA) return 10;    /* + volatile extras often live */
    if (cls == REG_FULL) return 15;     /* all GPRs except rsp */
    return 15 + 8;                      /* + xmm0-7 */
}

static long switch_cost(int cls, long rounds) {
    int n = regs_for_class(cls);
    volatile long pad[32];
    long i, acc = 0;
    for (i = 0; i < 32; i++)
        pad[i] = i;
    for (i = 0; i < rounds; i++) {
        int r;
        for (r = 0; r < n; r++)
            pad[r] = pad[r] + 1;        /* stand-in for save */
        for (r = 0; r < n; r++)
            acc += pad[r];              /* stand-in for restore */
    }
    return acc;
}

int main(int argc, char **argv) {
    long rounds = 2000000;
    int cls = REG_FULL;
    long a, b, c, d;
    (void)argc; (void)argv;
    a = switch_cost(REG_MIN, rounds);
    b = switch_cost(REG_EXTRA, rounds);
    c = switch_cost(REG_FULL, rounds);
    d = switch_cost(REG_SIMD, rounds);
    printf("reg_class_min=%d extra=%d full=%d simd=%d\n",
           regs_for_class(0), regs_for_class(1),
           regs_for_class(2), regs_for_class(3));
    return (int)((a ^ b ^ c ^ d) % 256);
}
