/* Non-virtual method calls through a pointer: the direct-call baseline.
 *
 * The claim this checks is the one the lowering is built on: a C++ method
 * becomes `Class_method(Class *this, ..)`, the same shape a Rust `impl`
 * method lowers to, so calling it should cost a direct call and nothing
 * else -- no receiver object, no indirection, no marshalling.
 *
 * This is the baseline half of a matched pair with cpp_dispatch.cpp. The two
 * are deliberately the same program down to the branch: same arithmetic, same
 * iteration count, same alternation between two objects selected at runtime,
 * the same call through a pointer. The single difference is that `mix` is
 * non-virtual here and `virtual` there, so the gap between the two numbers is
 * dispatch and not loop shape, cache behaviour or branch prediction.
 */
int printf(const char *, ...);

class Mixer {
    unsigned long k;
    unsigned long m;
public:
    Mixer(unsigned long a, unsigned long b) { k = a; m = b; }
    unsigned long mix(unsigned long x) { return (x + k) ^ m; }
    unsigned long step(unsigned long x) { return mix(x) & 0xFFFFFFFF; }
};

int main(void) {
    Mixer a(7, 0x5A5A);
    Mixer b(31, 0x3C3C);
    Mixer *p;
    unsigned long acc;
    unsigned long i;
    acc = 1;
    for (i = 0; i < 60000000; i = i + 1) {
        if ((i & 1) == 0) {
            p = &a;
        } else {
            p = &b;
        }
        acc = p->step(acc);
    }
    printf("%lu\n", acc);
    return 0;
}
