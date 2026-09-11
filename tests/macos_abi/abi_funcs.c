/* Apple arm64 calling-convention conformance, executed.
 *
 * Built twice -- by Crust and by clang (-target arm64-apple-macos11, the
 * reference implementation of the ABI) -- with PFX naming each build's
 * functions, and linked in every caller/callee pairing; see
 * tests/test_macos_target.py (AppleABIExecutionTests) and
 * tools/macho_run.py, which runs the result under emulation.
 */
#define CAT2(a,b) a##b
#define CAT(a,b) CAT2(a,b)
#define F(name) CAT(PFX, name)
/* 1. named args past the 8th integer register: Apple packs them on the
      stack at natural size/alignment. */
long F(many)(int a, int b, int c, int d, int e, int f, int g, int h,
             int i9, char c10, short s11, int i12, long l13)
{ return a + b + c + d + e + f + g + h + i9 * 10 + c10 * 100 + s11 * 1000
         + i12 * 10000 + l13 * 100000; }
/* 2. 9th+ FP args. */
double F(manyd)(double a, double b, double c, double d, double e, double f,
                double g, double h, float f9, double d10)
{ return a + b + c + d + e + f + g + h + f9 * 10 + d10 * 100; }
/* 3. sub-int args: Apple makes the *caller* extend to 32 bits. */
int F(take_sc)(signed char c) { return c; }
int F(take_us)(unsigned short s) { return s; }
int F(take_b)(_Bool b) { return b; }
/* 4. sub-int returns. */
signed char F(ret_sc)(int x) { return (signed char)x; }
unsigned short F(ret_us)(int x) { return (unsigned short)x; }
/* 5. two stack floats pack into 4-byte slots; char then long forces a gap. */
double F(twof)(double a, double b, double c, double d, double e, double f,
               double g, double h, float x, float y, double z)
{ return a + b + c + d + e + f + g + h + x * 10 + y * 100 + z * 1000; }
long F(gap)(long a, long b, long c, long d, long e, long f, long g, long h,
            char c9, long l10, short s11, char c12, int i13)
{ return a+b+c+d+e+f+g+h + c9 * 10 + l10 * 100 + s11 * 1000 + c12 * 10000L + i13 * 100000L; }
/* 6. identity on computed sub-int values: extension of a *runtime* value. */
int F(id_sc)(signed char c) { return c; }
int F(id_uc)(unsigned char c) { return c; }
int F(id_ss)(short s) { return s; }
signed char F(narrow_sc)(int x) { return (signed char)(x + 1); }
unsigned char F(narrow_uc)(int x) { return (unsigned char)(x * 3); }

#include <stdarg.h>
long F(vsum)(int n, ...) {
    va_list ap; long t = 0; int i;
    va_start(ap, n);
    for (i = 0; i < n; i++) t += (i % 2 == 0) ? va_arg(ap, int) : va_arg(ap, long);
    va_end(ap);
    return t;
}
