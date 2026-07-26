// Generics, monomorphised. Each distinct set of type arguments produces its
// own ordinary C struct or function -- no boxing, no tag, no vtable -- so an
// instantiation stays directly callable from C, exactly like every other
// Crust type.

int printf(const char *, ...);

struct Pair<T> {
    a: T,
    b: T,
}

impl<T> Pair<T> {
    fn new(a: T, b: T) -> Pair<T> {
        Pair { a: a, b: b }
    }

    fn sum(&self) -> T {
        self.a + self.b
    }

    fn largest(&self) -> T {
        if self.a > self.b { self.a } else { self.b }
    }
}

// A generic function. The type argument is inferred from what is passed.
fn max<T>(a: T, b: T) -> T {
    if a > b { a } else { b }
}

fn id<T>(x: T) -> T {
    x
}

fn main() {
    // `Pair<i32>` and `Pair<f64>` are two distinct C structs:
    //     struct Pair_int    { int a;    int b; };
    //     struct Pair_double { double a; double b; };
    let pi: Pair<i32> = Pair { a: 40, b: 2 };
    let pf: Pair<f64> = Pair::<f64>::new(1.5, 2.25);

    printf("Pair<i32>.sum     = %d\n", pi.sum());
    printf("Pair<i32>.largest = %d\n", pi.largest());
    printf("Pair<f64>.sum     = %g\n", pf.sum());

    // One `max` template, two instantiations, chosen by the argument types.
    printf("max(3, 9)         = %d\n", max(3, 9));
    printf("max(2.5, 1.5)     = %g\n", max(2.5, 1.5));

    // A turbofish where inference has nothing to work from, or to be explicit.
    printf("id::<i32>(7)      = %d\n", id::<i32>(7));
}
