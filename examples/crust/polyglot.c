/* Three languages, one translation unit, no FFI boundary anywhere.
 *
 *   #include "vec2.rs"        Rust    -- lowered by shivyc/crust.py
 *   #include "histogram.py"   rpython -- lowered by tools/py2c.py
 *   ... and the file itself is C, with Rust functions written inline.
 *
 * All of it becomes one stream of C before the lexer runs, so every call
 * below is a direct call: same IL, same register allocator, full inlining.
 */
#include "vec2.rs"
#include "histogram.py"

int printf(const char *, ...);

/* --- Rust, in the C file, calling the included rpython module. --- */
fn bar(count: i32, total: i32) -> i32 {
    scale_to_width(count, total, 40)
}

/* Rust using the struct from the included .rs module. */
fn spread(pts: *const Vec2, n: usize) -> f64 {
    let mut worst: f64 = 0.0;
    for i in 0..n {
        let d: f64 = Vec2_len2(&pts[i]);
        if d > worst {
            worst = d;
        }
    }
    worst
}

/* Rust calling both other languages in one expression. */
fn bucket_for_len2(v: *const Vec2, width: i32) -> i32 {
    bucket_of(Vec2_len2(v) as i32, width)
}

/* --- C, driving all three. --- */
int main(void) {
    Vec2 pts[4];
    pts[0] = Vec2_new(1.0, 0.0);
    pts[1] = Vec2_new(3.0, 4.0);
    pts[2] = Vec2_new(0.0, 2.0);
    pts[3] = Vec2_new(6.0, 8.0);

    int counts[4] = {12, 30, 6, 2};
    int total = 50;

    printf("buckets   = %d\n", bucket_count(0, 99, 25));
    printf("spread    = %g\n", spread(pts, 4));
    printf("bucket[1] = %d\n", bucket_for_len2(&pts[1], 10));

    for (int i = 0; i < 4; i++) {
        printf("  n=%2d  bar=%2d\n", counts[i], bar(counts[i], total));
    }
    return 0;
}
