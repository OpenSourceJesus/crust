/* C that pulls in a Rust module with an ordinary #include. */
#include "vec2.rs"

int printf(const char *, ...);

/* A Rust function in this file, using the struct from the included .rs. */
fn centroid(pts: *const Vec2, n: usize) -> Vec2 {
    let mut acc: Vec2 = Vec2::zero();
    for i in 0..n {
        acc.x = acc.x + pts[i].x;
        acc.y = acc.y + pts[i].y;
    }
    acc.scale(1.0 / (n as f64));
    acc
}

/* C, calling both the Rust methods and the Rust free function directly. */
int main(void) {
    Vec2 pts[3];
    pts[0] = Vec2_new(0.0, 0.0);
    pts[1] = Vec2_new(6.0, 0.0);
    pts[2] = Vec2_new(0.0, 9.0);

    Vec2 c = centroid(pts, 3);
    printf("centroid  = (%g, %g)\n", c.x, c.y);
    printf("len2      = %g\n", Vec2_len2(&c));
    printf("dot(p1,p2)= %g\n", Vec2_dot(&pts[1], &pts[2]));
    return 0;
}
