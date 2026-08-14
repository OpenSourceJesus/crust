/* Declarations only, in the style of the other bundled fallback headers: the
 * definitions come from libm on the link line.
 *
 * `tools/py2c.py` emits `#include <math.h>` into its runtime header, and its
 * MATH_FUNCS set routes those names through as native `double` arithmetic
 * rather than boxing them. Without a stub here, a program that pulls in the
 * rpython runtime -- or any C that reaches for `sqrt` -- failed to compile
 * unless the caller happened to pass `-I/usr/include`.
 *
 * The `float`/`long double` variants are declared too, so `sqrtf` resolves,
 * but note that this compiler's `long double` is its `double`.
 */

double      acos(double);
double      acosh(double);
double      asin(double);
double      asinh(double);
double      atan(double);
double      atan2(double, double);
double      atanh(double);
double      cbrt(double);
double      ceil(double);
double      copysign(double, double);
double      cos(double);
double      cosh(double);
double      erf(double);
double      erfc(double);
double      exp(double);
double      exp2(double);
double      expm1(double);
double      fabs(double);
double      fdim(double, double);
double      floor(double);
double      fma(double, double, double);
double      fmax(double, double);
double      fmin(double, double);
double      fmod(double, double);
double      frexp(double, int *);
double      hypot(double, double);
double      ldexp(double, int);
double      lgamma(double);
long long   llrint(double);
long long   llround(double);
double      log(double);
double      log10(double);
double      log1p(double);
double      log2(double);
double      logb(double);
long        lrint(double);
long        lround(double);
double      modf(double, double *);
double      nan(const char *);
double      nearbyint(double);
double      nextafter(double, double);
double      pow(double, double);
double      remainder(double, double);
double      remquo(double, double, int *);
double      rint(double);
double      round(double);
double      scalbln(double, long);
double      scalbn(double, int);
double      sin(double);
double      sinh(double);
double      sqrt(double);
double      tan(double);
double      tanh(double);
double      tgamma(double);
double      trunc(double);

float       acosf(float);
float       asinf(float);
float       atanf(float);
float       atan2f(float, float);
float       cbrtf(float);
float       ceilf(float);
float       copysignf(float, float);
float       cosf(float);
float       coshf(float);
float       expf(float);
float       fabsf(float);
float       floorf(float);
float       fmaxf(float, float);
float       fminf(float, float);
float       fmodf(float, float);
float       hypotf(float, float);
float       logf(float);
float       log2f(float);
float       log10f(float);
float       powf(float, float);
float       roundf(float);
float       sinf(float);
float       sinhf(float);
float       sqrtf(float);
float       tanf(float);
float       tanhf(float);
float       truncf(float);

/* `long double` is `double` here, so these are the same functions under
 * different names rather than a wider format. */
long double acosl(long double);
long double asinl(long double);
long double atanl(long double);
long double ceill(long double);
long double cosl(long double);
long double expl(long double);
long double fabsl(long double);
long double floorl(long double);
long double fmodl(long double);
long double logl(long double);
long double powl(long double, long double);
long double roundl(long double);
long double sinl(long double);
long double sqrtl(long double);
long double tanl(long double);
long double truncl(long double);

int         __fpclassifyd(double);
int         __isnand(double);
int         __isinfd(double);

#define HUGE_VAL  (1e10000)
#define HUGE_VALF (1e10000f)
#define INFINITY  (1e10000f)
#define M_E        2.7182818284590452354
#define M_LOG2E    1.4426950408889634074
#define M_LOG10E   0.43429448190325182765
#define M_LN2      0.69314718055994530942
#define M_LN10     2.30258509299404568402
#define M_PI       3.14159265358979323846
#define M_PI_2     1.57079632679489661923
#define M_PI_4     0.78539816339744830962
#define M_1_PI     0.31830988618379067154
#define M_2_PI     0.63661977236758134308
#define M_2_SQRTPI 1.12837916709551257390
#define M_SQRT2    1.41421356237309504880
#define M_SQRT1_2  0.70710678118654752440
