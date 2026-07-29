/* Monomorphised generics over several element types, with a bounded
 * container. Measures whether an instantiation costs what a hand-written
 * version would. */
int printf(const char *, ...);

struct Ring<T> { buf: [T; 64], head: u64, len: u64 }

impl<T> Ring<T> {
    fn init(&mut self) { self.head = 0; self.len = 0; }
    fn push(&mut self, v: T) {
        self.buf[(self.head + self.len) & 63] = v;
        if self.len < 64 { self.len += 1; } else { self.head = (self.head + 1) & 63; }
    }
    fn at(&self, i: u64) -> T { self.buf[(self.head + i) & 63] }
}

fn churn_u64(r: *mut Ring<u64>, n: u64) -> u64 {
    let mut acc: u64 = 0;
    for i in 0..n {
        r.push(i);
        acc += r.at(i & 63);
    }
    acc
}

fn churn_i32(r: *mut Ring<i32>, n: u64) -> i32 {
    let mut acc: i32 = 0;
    for i in 0..n {
        r.push(i as i32);
        acc += r.at(i & 63);
    }
    acc
}

fn main() {
    let mut a: Ring<u64> = Ring { buf: [0; 64], head: 0, len: 0 };
    let mut b: Ring<i32> = Ring { buf: [0; 64], head: 0, len: 0 };
    a.init();
    b.init();
    printf("%lu %d\n", churn_u64(&a, 2000000), churn_i32(&b, 2000000));
}
