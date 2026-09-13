/* Microsoft x64 calling-convention conformance, executed.
 *
 * Built twice -- by Crust and by MinGW-w64 gcc, the reference implementation
 * used for comparison -- with PFX naming each build's functions. Every
 * function is also reachable through PFX##table(), so abi_checks.c can call
 * either build's functions from either build's checks; see
 * tests/test_windows_target.py (Win64ABIExecutionTests).
 */
#include <stdarg.h>
#define CAT2(a,b) a##b
#define CAT(a,b) CAT2(a,b)
#define F(name) CAT(PFX, name)
#include "abi_types.h"

/* 1. positional assignment: ints and doubles share one counter */
long long F(mix)(int a, double b, int c, double d, int e, double f, int g)
{ return a + (long long)(b * 10) + c * 100 + (long long)(d * 1000)
         + e * 10000LL + (long long)(f * 100000) + g * 1000000LL; }
/* 2. many args: position 4 onward on the stack, above the home space */
long long F(many)(int a, int b, int c, int d, int e, char f, short g,
                  long long h, int i, long j)
{ return a + b * 2 + c * 3 + d * 4 + e * 5 + f * 6 + g * 7 + h * 8 + i * 9
         + j * 10; }
double F(manyd)(float a, double b, float c, double d, float e, double f)
{ return a + b * 2 + c * 3 + d * 4 + e * 5 + f * 6; }
float F(retf)(float a, int k) { return a * (float)k; }
/* 3. sub-int values: the Win64 caller leaves upper bits undefined */
int F(take_sc)(signed char c) { return c; }
int F(take_us)(unsigned short s) { return s; }
signed char F(ret_sc)(int x) { return (signed char)x; }
unsigned short F(ret_us)(int x) { return (unsigned short)x; }
long F(ret_long)(long x) { return x * 3; }      /* LLP64: 4 bytes */
/* 4. structs of 1, 2, 4, 8 bytes travel as integers; others by reference */
struct S1 F(s1)(struct S1 a) { a.c = (char)(a.c + 1); return a; }
struct S2 F(s2)(struct S2 a) { a.s = (short)(a.s * 2); return a; }
struct S4 F(s4)(struct S4 a) { a.a = (char)(a.a + 1); a.b = (char)(a.b + 2); a.h = (short)(a.h + 3); return a; }
struct S8 F(s8)(struct S8 a) { a.x += 1; a.y += 2; return a; }
struct S3 F(s3)(struct S3 a) { a.c[0]++; a.c[1]++; a.c[2]++; return a; }
struct S12 F(s12)(struct S12 a) { a.x += 10; a.y += 20; a.z += 30; return a; }
struct S24 F(s24)(struct S24 a) { a.a *= 2; a.b *= 3; a.c *= 4; return a; }
/* The callee owns its by-reference copy, so its writes must not reach the
   caller. volatile, because an optimizer may otherwise drop stores to a
   parameter that is dead afterwards -- and then a caller that forgot to
   copy would pass unnoticed (mutation testing found exactly that). */
long long F(clobber24)(struct S24 a)
{
    volatile long long *p = &a.a;
    long long s = a.a + a.b + a.c;
    p[0] = -1; p[1] = -1; p[2] = -1;
    return s;
}
/* structs at stack positions (4+): an 8-byte one by value, a 24-byte one
   as a pointer in the slot */
long long F(late)(int a, int b, int c, int d, struct S8 e, struct S24 f, struct S3 g)
{ return a + b + c + d + e.x * 10 + e.y * 100 + f.a * 1000 + f.b * 10000
         + f.c * 100000 + g.c[2] * 1000000LL; }
/* 5. variadic: every argument an 8-byte slot, floating ones duplicated */
double F(vsum)(int n, ...)
{
    va_list ap; double t = 0; int i;
    va_start(ap, n);
    for (i = 0; i < n; i++)
        t += (i % 3 == 0) ? va_arg(ap, int)
           : (i % 3 == 1) ? va_arg(ap, double) : (double)va_arg(ap, long long);
    va_end(ap);
    return t;
}
double F(vnamed)(double a, int b, ...)
{
    va_list ap; double t = a + b;
    va_start(ap, b);
    t += va_arg(ap, double) * 10;
    t += va_arg(ap, int) * 100;
    va_end(ap);
    return t;
}
struct S24 F(vret)(int n, ...)     /* variadic *and* a hidden result pointer */
{
    va_list ap; struct S24 r;
    va_start(ap, n);
    r.a = va_arg(ap, long long); r.b = va_arg(ap, long long); r.c = n;
    va_end(ap);
    return r;
}
/* 6. calls back through a pointer from the other build */
long long F(apply)(long long (*fn)(int, int, int, int, int, char, short,
                                   long long, int, long), int k)
{ return fn(k, 2, 3, 4, 5, (char)6, (short)7, 8, 9, 10L); }

struct abi_table F(table_s) = {
    F(mix), F(many), F(manyd), F(retf), F(take_sc), F(take_us), F(ret_sc),
    F(ret_us), F(ret_long), F(s1), F(s2), F(s4), F(s8), F(s3), F(s12),
    F(s24), F(clobber24), F(late), F(vsum), F(vnamed), F(vret), F(apply)
};
struct abi_table *F(table)(void) { return &F(table_s); }
