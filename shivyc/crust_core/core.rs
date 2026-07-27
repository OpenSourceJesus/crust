// Crust's minimal `core` -- the handful of standard generic types that real
// Rust code cannot be read without.
//
// Crust monomorphises from source, so a generic with no template in the unit
// cannot be instantiated. Redox (and any real Rust) reaches constantly for
// `Vec<T>`, `Box<T>` and friends, which live in `alloc`/`core` and are written
// in a dialect far outside this subset -- traits, intrinsics, `unsafe`
// pointer arithmetic, allocator plumbing. So rather than trying to compile the
// real ones, Crust ships small honest equivalents *written in the Crust
// subset itself*, and seeds every translation unit with them.
//
// This is a reimplementation, not the standard library. What is here matches
// the shape and the common methods of the real types, so ordinary code reads
// and compiles the same way. What is not here -- iterators, traits, `Drop`,
// bounds checking, thread safety -- is absent rather than faked. A local
// definition of the same name always wins, so a unit that brings its own
// `Vec` is unaffected by any of this.
//
// Instantiation is demand-driven, so a unit that never mentions `Vec<T>` pays
// nothing for these templates: they are tokens in a table until something
// asks for one.

// ---------------------------------------------------------------------------
// Vec<T> -- a growable array.
//
// The layout is the same three words the real `Vec` uses (pointer, length,
// capacity), so it stays an ordinary C struct that C code can read directly.
// There is no bounds checking: `get` on an out-of-range index is undefined,
// exactly as indexing a raw C array would be. `Drop` does not exist in Crust,
// so the buffer is freed by an explicit `free_buf` rather than by scope exit.
// ---------------------------------------------------------------------------
struct Vec<T> {
    ptr: *mut T,
    len: usize,
    cap: usize,
}

impl<T> Vec<T> {
    fn new() -> Vec<T> {
        Vec { ptr: 0 as *mut T, len: 0, cap: 0 }
    }

    fn with_capacity(cap: usize) -> Vec<T> {
        let mut v: Vec<T> = Vec::<T>::new();
        v.reserve(cap);
        v
    }

    fn len(&self) -> usize {
        self.len
    }

    fn is_empty(&self) -> bool {
        self.len == 0
    }

    fn capacity(&self) -> usize {
        self.cap
    }

    // Grow to hold at least `want` elements, doubling so that repeated
    // `push` stays amortised O(1).
    fn reserve(&mut self, want: usize) {
        if want <= self.cap {
            return;
        }
        let mut cap: usize = self.cap;
        if cap == 0 {
            cap = 4;
        }
        while cap < want {
            cap = cap * 2;
        }
        self.ptr = realloc(self.ptr as *mut u8,
                           cap * size_of::<T>()) as *mut T;
        self.cap = cap;
    }

    fn push(&mut self, value: T) {
        self.reserve(self.len + 1);
        self.ptr[self.len] = value;
        self.len += 1;
    }

    fn get(&self, i: usize) -> T {
        self.ptr[i]
    }

    fn set(&mut self, i: usize, value: T) {
        self.ptr[i] = value;
    }

    fn last(&self) -> T {
        self.ptr[self.len - 1]
    }

    fn pop(&mut self) -> T {
        self.len -= 1;
        self.ptr[self.len]
    }

    fn clear(&mut self) {
        self.len = 0;
    }

    fn as_ptr(&self) -> *mut T {
        self.ptr
    }

    // Crust has no `Drop`, so releasing the buffer is explicit.
    fn free_buf(&mut self) {
        free(self.ptr as *mut u8);
        self.ptr = 0 as *mut T;
        self.len = 0;
        self.cap = 0;
    }
}

// ---------------------------------------------------------------------------
// PyList<T> -- the list an included rpython module builds.
//
// `tools/py2c.py` lowers a typed rpython list to
//
//     typedef struct _tlist_int { int* data; long len; long cap; } _tlist_int;
//
// which is the same three words in the same order as this type. That is the
// whole point: an rpython function returning `list[int]` hands back something
// Rust can walk directly, with a pointer cast and no copy.
//
// This is how Crust gets iteration over a built-up collection without having
// an iterator protocol. RPython already has one, and its output is a plain
// struct, so `for x in xs` over the result needs nothing new -- see
// `examples/crust/polylist.c`.
//
// The layout must not drift from py2c's. `len` and `cap` are `i64` rather
// than `usize` because py2c emits `long`, and a mismatch here would be a
// silent misread rather than a compile error.
// ---------------------------------------------------------------------------
struct PyList<T> {
    data: *mut T,
    len: i64,
    cap: i64,
}

impl<T> PyList<T> {
    fn len(&self) -> i64 {
        self.len
    }

    fn is_empty(&self) -> bool {
        self.len == 0
    }

    fn capacity(&self) -> i64 {
        self.cap
    }

    fn get(&self, i: i64) -> T {
        self.data[i]
    }

    fn set(&mut self, i: i64, value: T) {
        self.data[i] = value;
    }

    fn last(&self) -> T {
        self.data[self.len - 1]
    }

    fn as_ptr(&self) -> *mut T {
        self.data
    }

    // The buffer belongs to the rpython runtime, which allocated it with
    // malloc; freeing it here is correct but must not be done twice.
    fn free_buf(&mut self) {
        free(self.data as *mut u8);
        self.data = 0 as *mut T;
        self.len = 0;
        self.cap = 0;
    }
}

// ---------------------------------------------------------------------------
// Box<T> -- a single heap-allocated value.
//
// Without `Drop` this is a thin, explicit wrapper: `new` allocates, `get`
// reads, `free_box` releases. It exists mainly so that a signature written
// `Box<T>` in real code has something to resolve to.
// ---------------------------------------------------------------------------
struct Box<T> {
    ptr: *mut T,
}

impl<T> Box<T> {
    fn new(value: T) -> Box<T> {
        let p: *mut T = malloc(size_of::<T>()) as *mut T;
        p[0] = value;
        Box { ptr: p }
    }

    fn get(&self) -> T {
        self.ptr[0]
    }

    fn set(&mut self, value: T) {
        self.ptr[0] = value;
    }

    fn as_ptr(&self) -> *mut T {
        self.ptr
    }

    fn free_box(&mut self) {
        free(self.ptr as *mut u8);
        self.ptr = 0 as *mut T;
    }
}

// ---------------------------------------------------------------------------
// Cell<T> -- a value in a struct field that is written through a shared
// reference. Crust has no borrow checker, so this carries no interior
// mutability machinery; it is a named box for a value, present so code
// spelled with it resolves.
// ---------------------------------------------------------------------------
struct Cell<T> {
    value: T,
}

impl<T> Cell<T> {
    fn new(value: T) -> Cell<T> {
        Cell { value: value }
    }

    fn get(&self) -> T {
        self.value
    }

    fn set(&mut self, value: T) {
        self.value = value;
    }
}

// ---------------------------------------------------------------------------
// PhantomData<T> -- a zero-sized marker. Real Rust uses it to tie a type
// parameter to a struct that does not otherwise mention it; here it exists so
// that such a struct still parses. It lowers to the same one-byte placeholder
// any unit struct does.
// ---------------------------------------------------------------------------
struct PhantomData<T> {
    _marker: u8,
}

// ---------------------------------------------------------------------------
// AtomicU32 / AtomicUsize / Ordering -- stubs for kernel-style code.
//
// No real concurrency: load/store/compare_exchange/fetch_add are ordinary
// reads and writes. They exist so `static LOCK: AtomicU32 = AtomicU32::new(0)`
// and the common methods typecheck. A local definition always wins.
// ---------------------------------------------------------------------------
enum Ordering {
    Relaxed,
    Acquire,
    Release,
    AcqRel,
    SeqCst,
}

struct AtomicU32 {
    value: u32,
}

impl AtomicU32 {
    fn new(value: u32) -> AtomicU32 {
        AtomicU32 { value: value }
    }

    fn load(&self, _order: Ordering) -> u32 {
        self.value
    }

    fn store(&mut self, value: u32, _order: Ordering) {
        self.value = value;
    }

    fn compare_exchange(&mut self, current: u32, new: u32,
                        _success: Ordering, _failure: Ordering) -> u32 {
        if self.value == current {
            self.value = new;
            current
        } else {
            self.value
        }
    }

    fn compare_exchange_weak(&mut self, current: u32, new: u32,
                             success: Ordering, failure: Ordering) -> u32 {
        self.compare_exchange(current, new, success, failure)
    }

    fn fetch_add(&mut self, value: u32, _order: Ordering) -> u32 {
        let prev: u32 = self.value;
        self.value = self.value + value;
        prev
    }

    fn fetch_sub(&mut self, value: u32, _order: Ordering) -> u32 {
        let prev: u32 = self.value;
        self.value = self.value - value;
        prev
    }
}

struct AtomicUsize {
    value: usize,
}

impl AtomicUsize {
    fn new(value: usize) -> AtomicUsize {
        AtomicUsize { value: value }
    }

    fn load(&self, _order: Ordering) -> usize {
        self.value
    }

    fn store(&mut self, value: usize, _order: Ordering) {
        self.value = value;
    }

    fn compare_exchange(&mut self, current: usize, new: usize,
                        _success: Ordering, _failure: Ordering) -> usize {
        if self.value == current {
            self.value = new;
            current
        } else {
            self.value
        }
    }

    fn compare_exchange_weak(&mut self, current: usize, new: usize,
                             success: Ordering, failure: Ordering) -> usize {
        self.compare_exchange(current, new, success, failure)
    }

    fn fetch_add(&mut self, value: usize, _order: Ordering) -> usize {
        let prev: usize = self.value;
        self.value = self.value + value;
        prev
    }

    fn fetch_sub(&mut self, value: usize, _order: Ordering) -> usize {
        let prev: usize = self.value;
        self.value = self.value - value;
        prev
    }
}
