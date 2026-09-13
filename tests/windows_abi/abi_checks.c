/* The calling side: PFX##checks(T) exercises the functions in table T,
 * whichever build they came from. Each failed check sets one bit; 0 means
 * conformant. `k` is 1 at run time, so values are computed, not constant. */
#define CAT2(a,b) a##b
#define CAT(a,b) CAT2(a,b)
#include "abi_types.h"

int CAT(PFX, checks)(struct abi_table *T, int k)
{
    int bad = 0;
    struct S1 a1; struct S2 a2; struct S4 a4; struct S8 a8; struct S3 a3;
    struct S12 a12; struct S24 a24; struct S24 r24;
    a1.c = (char)(k * 40); a2.s = (short)(k * 1000); a4.a = (char)k;
    a4.b = (char)(k * 2); a4.h = (short)(k * 300); a8.x = k * 5; a8.y = k * 7;
    a3.c[0] = (char)k; a3.c[1] = (char)(k * 2); a3.c[2] = (char)(k * 3);
    a12.x = k; a12.y = 2 * k; a12.z = 3 * k;
    a24.a = k * 100000000000LL; a24.b = k * 3; a24.c = -k;

    if (T->mix(k, 2.5, 3, 4.5, 5, 6.25, 7) != 1 + 25 + 300 + 4500 + 50000 + 625000 + 7000000) bad |= 1 << 0;
    if (T->many(k, 2, 3, 4, 5, (char)6, (short)-7, 8000000000LL, 9, 10L)
        != 1 + 4 + 9 + 16 + 25 + 36 - 49 + 64000000000LL + 81 + 100) bad |= 1 << 1;
    if (T->manyd(0.5f, 1.25, 2.0f, 3.5, 4.0f, 0.125) != 0.5 + 2.5 + 6 + 14 + 20 + 0.75) bad |= 1 << 2;
    if (T->retf(1.5f, k * 3) != 4.5f) bad |= 1 << 3;
    if (T->take_sc((signed char)(k * 200)) != -56) bad |= 1 << 4;
    if (T->take_us((unsigned short)(k * 65535)) != 65535) bad |= 1 << 5;
    if (T->ret_sc(k * 300) != 44) bad |= 1 << 6;
    if (T->ret_us(k * 70000) != 70000 - 65536) bad |= 1 << 7;
    if (T->ret_long(k * -700000000L / 1) != -2100000000L) bad |= 1 << 8;
    a1 = T->s1(a1); if (a1.c != 41) bad |= 1 << 9;
    a2 = T->s2(a2); if (a2.s != 2000) bad |= 1 << 10;
    a4 = T->s4(a4); if (a4.a != 2 || a4.b != 4 || a4.h != 303) bad |= 1 << 11;
    a8 = T->s8(a8); if (a8.x != 6 || a8.y != 9) bad |= 1 << 12;
    a3 = T->s3(a3); if (a3.c[0] != 2 || a3.c[1] != 3 || a3.c[2] != 4) bad |= 1 << 13;
    a12 = T->s12(a12); if (a12.x != 11 || a12.y != 22 || a12.z != 33) bad |= 1 << 14;
    r24 = T->s24(a24);
    if (r24.a != 200000000000LL || r24.b != 9 || r24.c != -4) bad |= 1 << 15;
    if (T->clobber24(a24) != 100000000000LL + 3 - 1 || a24.a != 100000000000LL
        || a24.b != 3 || a24.c != -1) bad |= 1 << 16;
    if (T->late(k, 2, 3, 4, a8, a24, a3)
        != 1 + 2 + 3 + 4 + 60 + 900 + 100000000000000LL + 30000 - 100000 + 4000000) bad |= 1 << 17;
    if (T->vsum(7, k, 2.5, 3000000000LL, 4, 5.25, -6LL, 7) != 1 + 2.5 + 3000000000.0 + 4 + 5.25 - 6 + 7) bad |= 1 << 18;
    if (T->vnamed(0.5, 2, 3.25, 4) != 0.5 + 2 + 32.5 + 400) bad |= 1 << 19;
    r24 = T->vret(k * 9, 11LL, 22LL);
    if (r24.a != 11 || r24.b != 22 || r24.c != 9) bad |= 1 << 20;
    if (T->apply(T->many, k) != 1 + 4 + 9 + 16 + 25 + 36 + 49 + 64 + 81 + 100) bad |= 1 << 21;
    return bad;
}
