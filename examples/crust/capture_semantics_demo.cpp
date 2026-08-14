int printf(const char *fmt, ...);
int main(void) {
    int x = 10;
    int y = 1;
    auto byval = [x](int k) -> int { return x + k; };
    auto byref = [&y](int k) -> int { return y + k; };
    x = 99;
    y = 99;
    printf("byval=%d (want 11)  byref=%d (want 100)\n", byval(1), byref(1));
    return 0;
}
