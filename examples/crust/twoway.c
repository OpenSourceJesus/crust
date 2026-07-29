/* Two-way lists across the rpython/Rust boundary.
 *
 * rpython builds a list, Rust walks it *and writes back into it*, and C sees
 * the result. Nothing is copied in either direction: py2c's typed list is
 * three words, `PyList<T>` in the bundled core has the same layout, and the
 * mutating methods are py2c's own helpers.
 *
 * Those helpers are `static`, which is exactly why this works: an
 * `#include "x.py"` puts the generated C in *this* translation unit.
 */
#include "sieve.py"

int printf(const char *, ...);

/* Rust compacts the list in place: keep the odd entries, drop the rest.
 *
 * Note that the scratch list is one rpython handed us, not one built here. A
 * hand-constructed `PyList` has `cap == 0`, and py2c's `push` grows by
 * doubling -- `cap = cap * 2` stays zero, so the first write lands in a
 * zero-sized allocation. Lists must come from the rpython side. */
fn keep_odd(xs: *mut PyList<i32>, scratch: *mut PyList<i32>) -> i64 {
    let n: i64 = xs.len();
    scratch.clear();
    for x in xs {
        if x % 2 == 1 {
            scratch.push(x);
        }
    }
    xs.clear();
    for k in scratch {
        xs.push(k);
    }
    n - xs.len()
}

fn sum(xs: *mut PyList<i32>) -> i32 {
    let mut s: i32 = 0;
    for x in xs {
        s += x;
    }
    s
}

int main(void) {
    _tlist_int *primes = sieve(30);
    printf("primes    = %ld\n", primes->len);
    printf("sum       = %d\n", sum((PyList_int *)primes));
    _tlist_int *scratch = sieve(30);
    printf("dropped   = %ld\n",
           keep_odd((PyList_int *)primes, (PyList_int *)scratch));
    printf("odd count = %ld\n", primes->len);
    printf("odd sum   = %d\n", sum((PyList_int *)primes));
    return 0;
}
