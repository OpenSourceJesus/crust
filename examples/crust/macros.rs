// Macros: the built-in control and printing macros, plus `macro_rules!`.
//
// `println!` reads its conversions from the *types* of its arguments, since
// Rust's `{}` carries none of its own. `assert!` and friends lower to a test
// and an `abort()`. A `macro_rules!` definition is kept as token slices and
// matched at the invocation site.

// A one-rule macro.
macro_rules! square {
    ($x:expr) => { ($x) * ($x) };
}

// Several rules, chosen by the shape of the invocation.
macro_rules! pick {
    () => { 0 };
    ($a:expr) => { $a };
    ($a:expr, $b:expr) => { if $a > $b { $a } else { $b } };
}

// A macro whose argument is a whole expression, commas and all.
macro_rules! twice {
    ($x:expr) => { ($x) + ($x) };
}

fn add(a: i32, b: i32) -> i32 {
    a + b
}

fn main() {
    let n: i32 = 6;
    let f: f64 = 1.5;
    let s: &str = "crust";

    // Conversions come from the argument types: %d, %g, %s.
    println!("n={} f={} s={}", n, f, s);
    println!("hex={:x} braces={{}} percent=100%", n);

    println!("square(6)    = {}", square!(6));
    println!("pick()       = {}", pick!());
    println!("pick(7)      = {}", pick!(7));
    println!("pick(3, 9)   = {}", pick!(3, 9));
    println!("twice(add)   = {}", twice!(add(1, 20)));

    // Checked at run time; a failure calls abort().
    assert!(n > 0);
    assert_eq!(square!(6), 36);
    assert_ne!(n, 0);

    // Compiled out, as in a release build.
    debug_assert!(n == 999);

    // Crust configures nothing in, so cfg! is false.
    if cfg!(feature = "unavailable") {
        println!("not reached");
    } else {
        println!("cfg!         = false");
    }
}
