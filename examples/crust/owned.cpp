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
 */
struct Vec_int;
typedef struct Vec_int Vec_int;
static void Vec_int_free_buf(Vec_int *);

class VecGuard {
    Vec_int * held;
public:
    VecGuard(Vec_int * v) { held = v; }
    ~VecGuard() { if (held) { Vec_int_free_buf(held); held = 0; } }
    Vec_int * get() { return held; }
};
