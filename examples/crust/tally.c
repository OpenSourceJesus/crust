/* An rpython module that needs the py2c runtime, included into C.
 *
 * The include hook notices that the generated C references the runtime,
 * keeps its `#include "shivyc_rt.h"`, and queues shivyc_rt.c for linking.
 * Nothing here has to know that happened.
 */
#include "tally.py"
#include "vec2.rs"

/* shivyc's bundled <stdio.h> (pulled in by shivyc_rt.h) declares printf with
 * no prototype, so a double argument would be passed without %al set. Declare
 * it properly, as every other example in this directory does. */
int printf(const char *, ...);

/* A Rust function in the C file, calling the rpython module. */
fn label_width(n: i32) -> i32 {
    widest(labels(n))
}

int main(void) {
    Vec2 v = Vec2_new(3.0, 4.0);
    printf("labels = %s\n", labels(4));
    printf("widest = %d\n", label_width(12));
    printf("len2   = %g\n", Vec2_len2(&v));
    return 0;
}
