/* Virtual dispatch from the C++ subset, driven from C, with Rust doing the
 * arithmetic: three front ends in one translation unit, no shims.
 *
 * The C++ syntax lives in dispatch.cpp because the include hook lowers that
 * file and never sees this one. What arrives here is plain C functions over
 * struct pointers -- the same shape a Rust `impl` lowers to, which is why
 * `total` below can take the results directly. */
#include "dispatch.cpp"

int printf(const char *, ...);

/* Rust reduces what the C++ side produced. */
fn total(a: i32, b: i32, c: i32) -> i32 {
    a + b + c
}

int main(void) {
    int r[9];
    int i;
    for (i = 0; i < 9; i = i + 1) {
        r[i] = 0;
    }
    run_dispatch(r);

    printf("area         = %d %d\n", r[0], r[1]);
    printf("via base     = %d %d\n", r[2], r[3]);
    printf("describe     = %d %d\n", r[4], r[5]);
    printf("scaled(10)   = %d %d\n", r[6], r[7]);
    printf("inherited    = %d\n", r[8]);
    printf("total        = %d\n", total(r[0], r[1], r[8]));
    return 42;
}
