int printf(const char *fmt, ...);
class Calc {
public:
    int acc;
    Calc() { acc = 0; }
    int add() { return acc; }
    int add(int a) { acc = acc + a; return acc; }
    int add(int a, int b) { acc = acc + a + b; return acc; }
};
class Base { public: int v; Base() { v = 1; } virtual int get() { return v; } };
class Derived : public Base { public: Derived() { } int get() { return v * 2; } };
int main(void) {
    Calc c;
    printf("%d %d %d\n", c.add(1), c.add(2, 3), c.add());
    Base *b = new Derived();
    printf("virt=%d\n", b->get());
    delete b;
    return 0;
}
