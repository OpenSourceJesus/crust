int printf(const char *fmt, ...);

int apply(int (*fn)(int), int v) { return fn(v); }

int main(void) {
    auto twice = [](int y) -> int { return y * 2; };
    printf("twice=%d\n", twice(21));
    printf("apply=%d\n", apply(twice, 5));
    printf("inline=%d\n", apply([](int z) -> int { return z + 100; }, 7));
    auto greet = []() -> void { printf("hi\n"); };
    greet();
    return 0;
}
