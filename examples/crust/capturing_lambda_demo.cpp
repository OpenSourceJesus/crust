int printf(const char *fmt, ...);

class Guard {
public:
    int v;
    Guard() { v = 7; printf("guard in\n"); }
    ~Guard() { printf("guard out\n"); }
    int get() { return v; }
};

int main(void) {
    int total = 0;
    int scale = 3;
    auto add = [&](int v) -> int { total = total + v * scale; return total; };
    int a = add(1);
    int b = add(2);
    printf("a=%d b=%d total=%d\n", a, b, total);

    Guard g;
    int base = 100;
    auto bump = [&](int k) -> int {
        if (k < 0) { return base; }
        return base + k + g.get();
    };
    printf("bump=%d neg=%d\n", bump(5), bump(-1));

    auto shout = [&]() -> void { printf("total is %d\n", total); };
    shout();
    return 0;
}
