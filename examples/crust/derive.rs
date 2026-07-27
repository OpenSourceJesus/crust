// `#[derive(..)]` generates the same free functions a hand-written `impl`
// would, so derived methods dispatch statically and cost nothing extra.
//
//     #[derive(Clone)] struct P { .. }   ->   P P_clone(P *self);

int printf(const char *, ...);

#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
struct Point {
    x: i32,
    y: i32,
}

// An explicit `impl` always wins over a derived method of the same name.
#[derive(Clone, Default)]
struct Counter {
    n: i32,
}

impl Counter {
    fn clone(&self) -> Counter {
        Counter { n: self.n * 10 }
    }
}

fn main() {
    let a: Point = Point { x: 40, y: 2 };
    let b: Point = a.clone();
    let z: Point = Point::default();

    printf("clone    = %d\n", b.x + b.y);
    printf("eq(a, b) = %d\n", a.eq(&b));
    printf("default  = %d %d\n", z.x, z.y);
    printf("debug    = ");
    a.debug();
    printf("\n");

    // `Counter::clone` is the hand-written one, not the derived one.
    let c: Counter = Counter { n: 4 };
    printf("override = %d\n", c.clone().n);
    let d: Counter = Counter::default();
    printf("zeroed   = %d\n", d.n);
}
