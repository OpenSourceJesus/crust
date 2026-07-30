/* C++ destructors supplying the `Drop` that Crust does not have. */
#include "owned.cpp"

int printf(const char *, ...);

/* Rust builds the vector; its methods lower to the same C functions the C++
 * guard above calls. */
fn seed(n: i32) -> Vec<i32> {
    let mut v: Vec<i32> = Vec::<i32>::new();
    for i in 0..n {
        v.push(i * i);
    }
    v
}

fn total(v: *mut Vec<i32>) -> i32 {
    let mut s: i32 = 0;
    for i in 0..v.len() {
        s += v.get(i);
    }
    s
}

int main(void) {
    Vec_int owned = seed(5);

    /* The guard borrows it. `VecGuard_drop` is what a destructor call lowers
     * to; a C++ front end with scope tracking would emit it automatically. */
    VecGuard g;
    VecGuard_new(&g, &owned);

    printf("sum      = %d\n", total(VecGuard_get(&g)));
    printf("len      = %lu\n", Vec_int_len(&owned));

    VecGuard_drop(&g);              /* frees the buffer, clears the pointer */
    printf("released = %d\n", VecGuard_get(&g) == 0);
    return 42;
}
