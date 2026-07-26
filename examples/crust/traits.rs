// Traits, dispatched statically.
//
// `impl Trait for Type` lowers to the same `Type_method` free function an
// inherent `impl` produces, so a trait call is an ordinary direct call: no
// vtable, no function pointer, no indirection, and fully inlinable. Combined
// with monomorphisation, a bounded generic resolves its calls at instantiation
// time -- `total<T: Metric>` compiled for `Disk` calls `Disk_bytes` directly.

int printf(const char *, ...);

trait Metric {
    fn bytes(&self) -> i64;
    fn label(&self) -> &str;

    // A default method: any impl that does not override it gets this body
    // generated with `Self` bound to the implementing type.
    fn kib(&self) -> i64 {
        self.bytes() / 1024
    }
}

// A supertrait: `Device` requires `Metric`, and inherits its defaults.
trait Device: Metric {
    fn id(&self) -> i32;

    fn summary(&self) -> i64 {
        self.kib() + (self.id() as i64)
    }
}

struct Disk {
    id: i32,
    sectors: i64,
}

struct Ram {
    id: i32,
    pages: i64,
}

impl Metric for Disk {
    fn bytes(&self) -> i64 { self.sectors * 512 }
    fn label(&self) -> &str { "disk" }
}

impl Device for Disk {
    fn id(&self) -> i32 { self.id }
}

impl Metric for Ram {
    fn bytes(&self) -> i64 { self.pages * 4096 }
    fn label(&self) -> &str { "ram" }
    // overrides the default
    fn kib(&self) -> i64 { self.pages * 4 }
}

impl Device for Ram {
    fn id(&self) -> i32 { self.id }
}

// A trait bound on a generic. Monomorphisation gives one instantiation per
// concrete type, each with its calls resolved directly.
fn report<T: Device>(d: T) -> i64 {
    d.bytes() + d.summary()
}

fn main() {
    let d: Disk = Disk { id: 1, sectors: 2048 };
    let r: Ram = Ram { id: 2, pages: 256 };

    printf("%s: bytes=%ld kib=%ld\n", d.label(), d.bytes(), d.kib());
    printf("%s: bytes=%ld kib=%ld\n", r.label(), r.bytes(), r.kib());
    printf("disk summary = %ld\n", d.summary());
    printf("ram  summary = %ld\n", r.summary());
    printf("report(disk) = %ld\n", report(d));
    printf("report(ram)  = %ld\n", report(r));
}
