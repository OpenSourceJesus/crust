/* C++ destructors supplying the `Drop` that Crust does not have. */
#include "owned.cpp"

int printf(const char *, ...);

/* Rust builds the vector; its methods lower to the same C functions the C++
 * guard in owned.cpp calls (Vec_int_free_buf, Vec_int_len, Vec_int_get). */
fn seed(n: i32) -> Vec<i32> {
    let mut v: Vec<i32> = Vec::<i32>::new();
    for i in 0..n {
        v.push(i * i);
    }
    v
}

int main(void) {
    Vec_int owned = seed(5);
    unsigned long len = 0;
    int released = 0;
    int sum = run_guarded(&owned, &len, &released);

    printf("sum      = %d\n", sum);
    printf("len      = %lu\n", len);
    printf("released = %d\n", released);
    return 42;
}
