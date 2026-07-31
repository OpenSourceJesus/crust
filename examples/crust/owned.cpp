/* A scope guard over a Crust `Vec<int>`.
 *
 * This is the thing the three-language mix could not do before. Crust has no
 * `Drop`: scope exit cannot run code, so every allocating type in its core
 * carries an explicit `free_buf` and the caller has to remember. C++ is the
 * one language here whose object model is built around deterministic
 * destruction, so the guard lives on this side while the Rust type keeps the
 * explicit API for callers that want it.
 *
 * The lowering makes this cheap rather than clever: a C++ method becomes
 * `Class_method(Class *this, ..)` and a Rust `impl` method becomes
 * `Type_method(Type *self, ..)`. Both are plain C functions over a struct, so
 * the guard calls Rust's `free_buf` directly -- no wrapper object, no
 * indirection, nothing marshalled.
 *
 * The guard holds a *pointer*, deliberately. `Vec_int` is defined later in
 * the unit (the `#include` is expanded before Crust lowers the Rust), so it is
 * an incomplete type here -- which is fine to point at and not to embed. That
 * is also the better design: a guard that borrows does not duplicate the
 * container's API.
 *
 * Locals declared in this file get `Type_new` at the declaration and
 * `Type_drop` on every exit from the scope -- the closing `}`, but also
 * `return`, `break` and `continue` (see tools/cpprust.py). The inner block
 * below is therefore not needed to make the destructor run; it is here
 * because `*out_released` has to be set *after* the guard releases, so that
 * the caller can observe the ordering.
 */
struct Vec_int;
typedef struct Vec_int Vec_int;
static void Vec_int_free_buf(Vec_int *);
static unsigned long Vec_int_len(Vec_int *);
static int Vec_int_get(Vec_int *, unsigned long);

class VecGuard {
    Vec_int * held;
public:
    VecGuard(Vec_int * v) { held = v; }
    ~VecGuard() { if (held) { Vec_int_free_buf(held); held = 0; } }
    Vec_int * get() { return held; }
};

/* Borrow `v` under the guard, sum its squares-buffer, then Drop at `}`. */
int run_guarded(Vec_int *v, unsigned long *out_len, int *out_released) {
    int sum = 0;
    {
        VecGuard g(v);
        Vec_int *p = g.get();
        unsigned long n = Vec_int_len(p);
        unsigned long i;
        *out_len = n;
        for (i = 0; i < n; i = i + 1) {
            sum = sum + Vec_int_get(p, i);
        }
    }
    /* Inner block closed: destructor ran and nulled `held`. */
    *out_released = 1;
    return sum;
}
