/* Static trait dispatch: one implementation per type, resolved at
 * monomorphisation. Measures whether a trait call really costs a direct call. */
int printf(const char *, ...);

trait Op { fn apply(&self, x: u64) -> u64; }

struct AddK { k: u64 }
struct MulK { k: u64 }
struct XorK { k: u64 }

impl Op for AddK { fn apply(&self, x: u64) -> u64 { x + self.k } }
impl Op for MulK { fn apply(&self, x: u64) -> u64 { (x * self.k) & 0xFFFFFFFF } }
impl Op for XorK { fn apply(&self, x: u64) -> u64 { x ^ self.k } }

fn run<T: Op>(op: T, n: u64) -> u64 {
    let mut acc: u64 = 1;
    for _i in 0..n {
        acc = op.apply(acc);
    }
    acc
}

fn main() {
    let a: AddK = AddK { k: 7 };
    let m: MulK = MulK { k: 31 };
    let x: XorK = XorK { k: 0x5A5A };
    printf("%lu\n", run(a, 3000000) ^ run(m, 3000000) ^ run(x, 3000000));
}
