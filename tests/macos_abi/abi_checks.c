/* Apple arm64 calling-convention conformance, executed.
 *
 * Built twice -- by Crust and by clang (-target arm64-apple-macos11, the
 * reference implementation of the ABI) -- with PFX naming each build's
 * functions, and linked in every caller/callee pairing; see
 * tests/test_macos_target.py (AppleABIExecutionTests) and
 * tools/macho_run.py, which runs the result under emulation.
 */
/* The calling side. PFX names these check functions; OPFX names the
 * callee build. Each check sets one bit on failure; 0 means conformant. */
#define CAT2(a,b) a##b
#define CAT(a,b) CAT2(a,b)
#define OCAT2(a,b) a##b
#define OCAT(a,b) OCAT2(a,b)
#define O(name) OCAT(OPFX, name)
long O(many)(int, int, int, int, int, int, int, int, int, char, short, int, long);
double O(manyd)(double, double, double, double, double, double, double, double, float, double);
int O(take_sc)(signed char); int O(take_us)(unsigned short); int O(take_b)(_Bool);
signed char O(ret_sc)(int); unsigned short O(ret_us)(int);
long O(vsum)(int n, ...);
int CAT(PFX, checks)(void) {
    int bad = 0;
    signed char sc = -5; unsigned short us = 65535; char c10 = 7; short s11 = -3;
    if (O(many)(1,2,3,4,5,6,7,8, 9, c10, s11, 4, 2L) != 36+90+700-3000+40000+200000) bad |= 1;
    if (O(manyd)(1,2,3,4,5,6,7,8, 1.5f, 2.0) != 36 + 15 + 200) bad |= 2;
    if (O(take_sc)(sc) != -5) bad |= 4;
    if (O(take_us)(us) != 65535) bad |= 8;
    if (O(take_b)(sc != 0) != 1) bad |= 16;
    if (O(ret_sc)(-7) != -7) bad |= 32;
    if (O(ret_us)(70000) != 70000 - 65536) bad |= 64;
    if (O(vsum)(4, 10, 20L, 30, 40L) != 100) bad |= 128;
    if (O(vsum)(0) != 0) bad |= 256;
    return bad;
}
double O(twof)(double, double, double, double, double, double, double, double, float, float, double);
long O(gap)(long, long, long, long, long, long, long, long, char, long, short, char, int);
int O(id_sc)(signed char); int O(id_uc)(unsigned char); int O(id_ss)(short);
signed char O(narrow_sc)(int); unsigned char O(narrow_uc)(int);
int CAT(PFX, checks2)(int k) {          /* k = 1 at run time */
    int bad = 0;
    long (*fp)(int, int, int, int, int, int, int, int, int, char, short, int, long) = O(many);
    signed char sc = (signed char)(k + 199);        /* 200 -> -56 */
    unsigned char uc = (unsigned char)(k * 255);     /* 255 */
    short ss = (short)(k * 40000);                   /* 40000 -> -25536 */
    if (O(twof)(1,1,1,1,1,1,1,1, 0.5f, 0.25f, 0.125) != 8 + 5 + 25 + 125) bad |= 1;
    if (O(gap)(1,1,1,1,1,1,1,1, (char)(k * 3), 2L, (short)(-k), (char)k, 3) != 8 + 30 + 200 - 1000 + 10000 + 300000) bad |= 2;
    if (O(id_sc)(sc) != -56) bad |= 4;
    if (O(id_uc)(uc) != 255) bad |= 8;
    if (O(id_ss)(ss) != -25536) bad |= 16;
    if (O(narrow_sc)(299 + k) != 45) bad |= 32;          /* (signed char)301 */
    if (O(narrow_uc)(100 + k) != (303 & 0xff)) bad |= 64;
    if (fp(1,2,3,4,5,6,7,8, 9, (char)7, (short)-3, 4, 2L) != 36+90+700-3000+40000+200000) bad |= 128;
    return bad;
}
