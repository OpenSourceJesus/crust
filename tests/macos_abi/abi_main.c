/* main for one caller/callee pairing: PFX names the checks to run. */
#define CAT2(a,b) a##b
#define CAT(a,b) CAT2(a,b)
int CAT(PFX, checks)(void);
int CAT(PFX, checks2)(int);
static int one(void) { return 1; }
int main(void) { int k = one(); return CAT(PFX, checks)() | (CAT(PFX, checks2)(k) << 12); }
