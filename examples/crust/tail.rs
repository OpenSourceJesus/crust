// The diffuse tail: qualified paths in type position, tuple types, and
// closures -- plus Rust identifiers that happen to be C keywords.

int printf(const char *, ...);

// A tuple type. Each distinct shape becomes its own C struct, monomorphised
// on demand exactly like a slice, with positional fields `_0`, `_1`, ...
fn divmod(a: i32, b: i32) -> (i32, i32) {
    (a / b, a % b)
}

fn stats(xs: &[i32]) -> (i32, i32, f64) {
    let mut lo: i32 = xs[0];
    let mut hi: i32 = xs[0];
    let mut sum: i32 = 0;
    for x in xs {
        if x < lo { lo = x; }
        if x > hi { hi = x; }
        sum += x;
    }
    (lo, hi, (sum as f64) / (xs.len() as f64))
}

// A qualified path in type position. `alloc::boxed::Box` resolves to the
// bundled core `Box`, because the last segment names a type this unit knows.
fn boxed_sum(n: i32) -> i32 {
    let mut b: alloc::boxed::Box<i32> = Box::<i32>::new(n);
    b.set(b.get() + 2);
    // Scope exit calls `free_box`; no explicit free needed.
    b.get()
}

// `double`, `int` and `register` are ordinary Rust names and C keywords, so
// Crust renames them on the way out rather than emitting C that will not
// parse.
fn keywords(int: i32, register: i32) -> i32 {
    let double: i32 = int + register;
    double
}

fn main() {
    let d: (i32, i32) = divmod(47, 5);
    printf("divmod   = (%d, %d)\n", d.0, d.1);

    let data: [i32; 6] = [8, 3, 9, 1, 7, 4];
    let s: (i32, i32, f64) = stats(&data[..]);
    printf("stats    = (%d, %d, %g)\n", s.0, s.1, s.2);

    printf("boxed    = %d\n", boxed_sum(40));
    printf("keywords = %d\n", keywords(40, 2));

    // Non-capturing closures lift to plain static functions, so the value is
    // an ordinary C function pointer.
    let twice = |a: i32| a * 2;
    let plus = |a: i32, b: i32| a + b;
    let konst = || 7;
    printf("closures = %d %d %d\n", twice(21), plus(40, 2), konst());
}
