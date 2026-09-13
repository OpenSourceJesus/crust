/* The Crust executable: runs every caller/callee pairing against the
 * reference DLL (abi_gcc.dll, built by MinGW gcc), makes direct calls into
 * it through rlink's import stubs, and checks register preservation.
 * Prints one line per group; exits 0 only if everything conforms. */
#include <stdio.h>
#include "abi_types.h"
struct abi_table *c_table(void);
struct abi_table *g_table(void);
int c_checks(struct abi_table *T, int k);
int g_checks(struct abi_table *T, int k);
int g_preserve(void (*cb)(void *), void *arg);
/* direct calls by name into the DLL */
long long g_many(int, int, int, int, int, char, short, long long, int, long);
long long g_late(int, int, int, int, struct S8, struct S24, struct S3);
struct S24 g_s24(struct S24);
struct S3 g_s3(struct S3);
double g_vsum(int, ...);
float g_retf(float, int);

long long hungry_sink;
/* Enough simultaneously live values to exhaust the seven Win64 scratch
 * registers, so the allocator must reach for rbx, rsi, rdi, r12-r15. */
void hungry(void *p)
{
    long long *v = (long long *)p;
    long long a = v[0], b = v[1], c = v[2], d = v[3], e = v[4], f = v[5];
    long long g = v[6], h = v[7], i = v[8], j = v[9], l = v[10], m = v[11];
    long long n = v[12];
    int it;
    for (it = 0; it < 10; it++) {
        a += b; b ^= c; c += d; d ^= e; e += f; f ^= g; g += h;
        h ^= i; i += j; j ^= l; l += m; m ^= n; n += a;
    }
    hungry_sink = a + b + c + d + e + f + g + h + i + j + l + m + n;
}

static int one(void) { return 1; }

int main(void)
{
    int k = one();
    int cc = c_checks(c_table(), k);
    int cg = c_checks(g_table(), k);
    int gc = g_checks(c_table(), k);
    int gg = g_checks(g_table(), k);
    int direct = 0, keep;
    long long vals[13];
    struct S8 a8; struct S24 a24; struct S3 a3; struct S24 r24;
    int i;
    for (i = 0; i < 13; i++) vals[i] = i * 7 + 1;
    a8.x = 1; a8.y = 2; a24.a = 3; a24.b = 4; a24.c = 5;
    a3.c[0] = 6; a3.c[1] = 7; a3.c[2] = 8;
    if (g_many(k, 2, 3, 4, 5, (char)6, (short)7, 8, 9, 10L) != 385) direct |= 1;
    if (g_late(k, 2, 3, 4, a8, a24, a3) != 10 + 10 + 200 + 3000 + 40000 + 500000 + 8000000) direct |= 2;
    r24 = g_s24(a24); if (r24.a != 6 || r24.b != 12 || r24.c != 20 || a24.a != 3) direct |= 4;
    a3 = g_s3(a3); if (a3.c[0] != 7 || a3.c[2] != 9) direct |= 8;
    if (g_vsum(4, k, 0.5, 10LL, 2) != 13.5) direct |= 16;
    if (g_retf(2.5f, 2) != 5.0f) direct |= 32;
    keep = g_preserve(hungry, vals);
    printf("crust->crust %d\ncrust->gcc %d\ngcc->crust %d\ngcc->gcc %d\n"
           "direct %d\npreserve %d\n", cc, cg, gc, gg, direct, keep);
    return (cc | cg | gc | gg | direct | keep) != 0;
}
