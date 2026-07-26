int printf(const char *, ...);

/* --- Rust syntax --- */
fn gcd(a: i64, b: i64) -> i64 {
    let mut x: i64 = a;
    let mut y: i64 = b;
    while y != 0 {
        let t: i64 = y;
        y = x % y;
        x = t;
    }
    x
}

fn classify(n: i32) -> i32 {
    if n < 0 {
        -1
    } else if n == 0 {
        0
    } else {
        1
    }
}

fn sum_to(n: u32) -> u64 {
    let mut acc: u64 = 0;
    for i in 1..=n {
        acc += i as u64;
    }
    acc
}

fn dot(a: *const f64, b: *const f64, n: usize) -> f64 {
    let mut s: f64 = 0.0;
    for i in 0..n {
        s += a[i] * b[i];
    }
    s
}

/* --- C syntax, calling into the Rust functions with no FFI --- */
int main(void) {
    double u[3] = {1.0, 2.0, 3.0};
    double v[3] = {4.0, 5.0, 6.0};
    printf("gcd(1071, 462) = %ld\n", gcd(1071, 462));
    printf("classify(-7)   = %d\n", classify(-7));
    printf("sum_to(100)    = %lu\n", sum_to(100));
    printf("dot(u, v)      = %g\n", dot(u, v, 3));
    return 0;
}
