/* Data-carrying enums dispatched by `match` -- the tagged union lowering.
 * Measures the cost of the tag switch and the payload reads. */
int printf(const char *, ...);

enum Op {
    Add(u64),
    Mul(u64),
    Shift { by: u64, mask: u64 },
    Nop,
}

fn apply(acc: u64, op: Op) -> u64 {
    match op {
        Op::Add(k) => acc + k,
        Op::Mul(k) => (acc * k) & 0xFFFFFFFF,
        Op::Shift { by, mask } => (acc >> by) & mask,
        Op::Nop => acc,
    }
}

fn run(n: u64) -> u64 {
    let mut acc: u64 = 1;
    for i in 0..n {
        let sel: u64 = i & 3;
        if sel == 0 { acc = apply(acc, Op::Add(7)); }
        else if sel == 1 { acc = apply(acc, Op::Mul(31)); }
        else if sel == 2 { acc = apply(acc, Op::Shift { by: 1, mask: 0xFFFF }); }
        else { acc = apply(acc, Op::Nop); }
    }
    acc
}

fn main() { printf("%lu\n", run(3000000)); }
