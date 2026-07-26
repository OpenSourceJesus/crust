// `for x in xs` over slices and arrays, unit structs, and the `[v; N]`
// repeat initializer -- the syntax Crust grew after the Option/Result pass.

int printf(const char *, ...);

const LANES: usize = 4;

// A unit struct: no fields, and its own name is its value. C has no empty
// struct, so this lowers to a one-byte placeholder nobody ever reads.
struct Celsius;

impl Celsius {
    fn unit(&self) -> &str {
        "C"
    }
}

// `for x in xs` walks a slice directly. The binding is a copy of the
// element, so this is `for x in xs.iter().copied()` in real Rust.
fn sum(xs: &[i32]) -> i32 {
    let mut total: i32 = 0;
    for x in xs {
        total += x;
    }
    total
}

// `.iter()` is accepted as a no-op, so the idiomatic spelling reads the same.
fn max(xs: &[i32]) -> i32 {
    let mut best: i32 = xs[0];
    for x in xs.iter() {
        if x > best {
            best = x;
        }
    }
    best
}

// A fixed-size array iterates too, taking its length from its own type.
fn spread() -> i32 {
    let bias: [i32; LANES] = [3; LANES];
    let mut total: i32 = 0;
    for b in bias {
        total += b;
    }
    total
}

// Nested: a slice of the array, iterated after the range loop built it.
fn running_max(xs: &[i32]) -> i32 {
    let mut seen: [i32; 8] = [0; 8];
    let mut n: usize = 0;
    for i in 0..xs.len() {
        if n < 8 {
            seen[n] = xs[i];
            n += 1;
        }
    }
    max(&seen[0..n])
}

fn main() {
    let temps: [i32; 6] = [12, 19, 4, 23, 7, 15];
    let unit: Celsius = Celsius;

    printf("sum         = %d\n", sum(&temps[..]));
    printf("max         = %d%s\n", max(&temps[..]), unit.unit());
    printf("spread      = %d\n", spread());
    printf("running_max = %d\n", running_max(&temps[..]));
}
