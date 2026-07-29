/* Multiply / divide by powers of two -- strength-reduction hotspot.
   ShivyCX should turn unsigned x*8 / x/16 into shifts; gcc -O0 often still
   emits imul/idiv. */
unsigned long scale_up(unsigned long x, int n) {
    int i;
    for (i = 0; i < n; i++)
        x = x * 8UL;
    return x;
}

unsigned long scale_down(unsigned long x, int n) {
    int i;
    for (i = 0; i < n; i++)
        x = x / 16UL;
    return x;
}

int main(void) {
    unsigned long a = 1, b = 1UL << 40;
    int r;
    for (r = 0; r < 500000; r++) {
        a = scale_up(a + (unsigned long)r, 3) ^ scale_down(b + (unsigned long)r, 2);
        b = scale_down(a, 1) + scale_up(b, 1);
    }
    return (int)((a ^ b) % 256);
}
