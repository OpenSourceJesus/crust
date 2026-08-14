/* A C++ class that *owns* Crust values, rather than borrowing one.
 *
 * `examples/crust/owned.cpp` is the borrowing shape: the guard holds a
 * `Vec_int *` and frees it at scope exit. It had to, because the `#include`
 * is expanded before Crust emits the Rust type, so `Vec_int` was an
 * incomplete type here and could only be pointed at.
 *
 * That is no longer true. Crust places its prelude above this include and
 * emits a `#line` directive so the original numbering resumes, so both types
 * below are complete -- no forward declarations, no `struct Vec_int;` by
 * hand. And Crust hands the preprocessor the list of types it lowered that
 * own something, so this pass knows a `Vec_int` member has to be destroyed.
 *
 * Note what is *not* written here: no destructor. Both members own something,
 * so the class gets an implicit one that frees each of them, in reverse
 * declaration order -- C++'s rule, and deliberately not Rust's, which frees
 * fields in declaration order. Each side follows its own source language;
 * what they share is the symbol. `Res_drop` below is emitted by Crust from
 * `impl Drop for Res`, and is exactly what a `~Res()` would have lowered to.
 *
 * Owning also means the copy rules apply: `Tally b = a;` is refused here,
 * naming the Rule of Three, because the struct copy would leave two objects
 * holding one buffer.
 */

class Tally {
public:
    Vec_int samples;                  /* a Crust `Vec<i32>`, by value  */
    Res mark;                         /* a Crust type with `impl Drop` */

    void start(int tag) {
        mark.id = tag;
        samples = Vec_int_new();
    }
    void add(int v) { Vec_int_push(&samples, v); }
    unsigned long count() { return Vec_int_len(&samples); }
    int total() {
        int sum = 0;
        unsigned long i = 0;
        while (i < Vec_int_len(&samples)) {
            sum = sum + Vec_int_get(&samples, i);
            i = i + 1;
        }
        return sum;
    }
};

/* Both members are released at the closing brace, with no destructor in
 * sight -- and on the early `return` too, which the unwinding covers. */
int collect(int tag, int *out_count) {
    Tally t;
    t.start(tag);
    t.add(3);
    t.add(4);
    t.add(5);
    *out_count = (int)t.count();
    return t.total();
}
