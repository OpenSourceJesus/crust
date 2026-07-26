// A small vector type in Rust syntax, usable from C via `#include "vec2.rs"`.

struct Vec2 {
    x: f64,
    y: f64,
}

impl Vec2 {
    fn new(x: f64, y: f64) -> Vec2 {
        Vec2 { x: x, y: y }
    }

    fn zero() -> Vec2 {
        Vec2 { x: 0.0, y: 0.0 }
    }

    fn dot(&self, other: *const Vec2) -> f64 {
        self.x * other.x + self.y * other.y
    }

    fn len2(&self) -> f64 {
        self.dot(self)
    }

    fn scale(&mut self, k: f64) {
        self.x = self.x * k;
        self.y = self.y * k;
    }
}
