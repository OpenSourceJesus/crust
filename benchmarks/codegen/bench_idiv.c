/* Scalar idiv-bound recurrence -- mirrors the lexer hash bottleneck.
   Fair peers: ShivyCX vs gcc -O0 / -O2. */
unsigned long HMOD = 1000003UL;

unsigned long word_hash(const unsigned char *p, int n) {
    unsigned long h = 0;
    int i;
    for (i = 0; i < n; i++)
        h = (h * 131UL + p[i]) % HMOD;
    return h;
}

int main(void) {
    unsigned char buf[64];
    int i, r;
    unsigned long acc = 0;
    for (i = 0; i < 64; i++)
        buf[i] = (unsigned char)(i * 17 + 3);
    for (r = 0; r < 200000; r++)
        acc += word_hash(buf, 64);
    return (int)(acc % 256);
}
