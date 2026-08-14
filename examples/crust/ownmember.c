/* C++ classes owning Crust values, and Rust's own `Drop`, in one unit.
 *
 * `impl Drop for Res` lowers to `Res_drop(Res *)`, which is the same symbol
 * `~Res()` would lower to on the C++ side. That is what lets the C++ class in
 * ownmember.cpp hold a `Res` by value and have it destroyed properly: the
 * member epilogue calls the Rust destructor directly, with no wrapper and
 * nothing marshalled.
 */
#include "ownmember.cpp"

int printf(const char *, ...);

/* A Rust type with a destructor. Scope exit calls it on the Rust side; a C++
 * member epilogue calls the very same function on the other. */
struct Res { id: i32 }

impl Drop for Res {
    fn drop(&mut self) {
        printf("  Res_drop tag=%d\n", self.id);
    }
}

/* A Rust struct that owns a `Vec` without writing any `Drop` of its own: the
 * field glue gives it one, and it is transitive through `Wrap`. */
struct Bag { items: Vec<i32> }
struct Wrap { bag: Bag }

fn build(n: i32) -> i32 {
    let mut w: Wrap = Wrap { bag: Bag { items: Vec::<i32>::new() } };
    let mut i: i32 = 0;
    while i < n {
        w.bag.items.push(i * i);
        i += 1;
    }
    w.bag.items.len() as i32
}

/* Passing an owning value across a call boundary is a move, as in Rust: the
 * callee drops it, and the caller must not drop it again. */
fn consume(v: Vec<i32>) -> i32 {
    v.len() as i32
}

fn moved(n: i32) -> i32 {
    let mut v: Vec<i32> = Vec::<i32>::new();
    let mut i: i32 = 0;
    while i < n {
        v.push(i);
        i += 1;
    }
    consume(v)
}

int main(void) {
    int count = 0;
    int total = collect(7, &count);

    printf("total    = %d\n", total);
    printf("count    = %d\n", count);
    printf("built    = %d\n", build(4));
    printf("moved    = %d\n", moved(6));
    return 42;
}
