/* Virtual dispatch through a base pointer: the vtable plus the thunk.
 *
 * The matched pair to cpp_methods.cpp -- the same arithmetic, the same
 * iteration count, the same runtime alternation between two objects, the same
 * call through a pointer. The single difference is that `mix` is `virtual`
 * here, so the gap between the two numbers is dispatch and nothing else.
 *
 * Two things are being paid for, and they are worth naming separately. One is
 * the indirect call itself, which any vtable scheme pays. The other is the
 * thunk: the slot`s `this` is the class that first declared the method, the
 * implementation takes its own, and a small forwarding function converts
 * between them. Casting the function pointer instead would remove that hop
 * and is what a C++ compiler emits, but it is undefined behaviour in C, and
 * the point of this lowering is that its output is C. This benchmark is what
 * that decision costs.
 *
 * The alternation is what makes the number mean anything. A call site that
 * only ever sees one type lets an optimiser prove the target and devirtualise,
 * which measures the optimiser rather than the dispatch -- and is not why
 * anyone writes `virtual` in the first place.
 */
int printf(const char *, ...);

class Op {
    unsigned long k;
    unsigned long m;
public:
    Op(unsigned long a, unsigned long b) { k = a; m = b; }
    unsigned long getk() { return k; }
    unsigned long getm() { return m; }
    virtual unsigned long mix(unsigned long x) { return x; }
    unsigned long step(unsigned long x) { return mix(x) & 0xFFFFFFFF; }
};

class AddMix : public Op {
public:
    AddMix(unsigned long a, unsigned long b) : Op(a, b) { }
    unsigned long mix(unsigned long x) { return (x + getk()) ^ getm(); }
};

class XorMix : public Op {
public:
    XorMix(unsigned long a, unsigned long b) : Op(a, b) { }
    unsigned long mix(unsigned long x) { return (x ^ getk()) + getm(); }
};

int main(void) {
    AddMix a(7, 0x5A5A);
    XorMix b(31, 0x3C3C);
    Op *p;
    unsigned long acc;
    unsigned long i;
    acc = 1;
    for (i = 0; i < 60000000; i = i + 1) {
        if ((i & 1) == 0) {
            p = (Op *)&a;
        } else {
            p = (Op *)&b;
        }
        acc = p->step(acc);
    }
    printf("%lu\n", acc);
    return 0;
}
