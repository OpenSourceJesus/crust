// mingine.rs -- the geometry and colour core of a tiny game engine, in Rust.
//
// Included straight into a C translation unit with `#include "mingine.rs"`,
// which shivyc/crust.py lowers to C before the lexer runs. Nothing here is
// behind an FFI boundary: `Rect_clip` below is called from C in mingine.c and
// from rpython-generated code, and it is the same direct call either way.
//
// Rust earns its place in this layer: the engine's clipping and colour maths
// is where ownership and method syntax actually read well, and where a bug is
// a corrupted framebuffer rather than a compile error.

struct Rect {
    x: i32,
    y: i32,
    w: i32,
    h: i32,
}

impl Rect {
    fn new(x: i32, y: i32, w: i32, h: i32) -> Rect {
        Rect { x: x, y: y, w: w, h: h }
    }

    fn right(&self) -> i32 {
        self.x + self.w
    }

    fn bottom(&self) -> i32 {
        self.y + self.h
    }

    fn is_empty(&self) -> i32 {
        if self.w <= 0 {
            return 1;
        }
        if self.h <= 0 {
            return 1;
        }
        0
    }

    fn contains(&self, px: i32, py: i32) -> i32 {
        if px < self.x {
            return 0;
        }
        if py < self.y {
            return 0;
        }
        if px >= self.right() {
            return 0;
        }
        if py >= self.bottom() {
            return 0;
        }
        1
    }

    // Clip this rectangle against another, in place. Returns 0 when nothing
    // survives, which is the caller's signal to draw nothing at all -- every
    // blit in the engine goes through here, so an off-screen sprite costs one
    // comparison rather than a loop over pixels that are thrown away.
    fn clip(&mut self, bounds: *const Rect) -> i32 {
        let mut x0: i32 = self.x;
        let mut y0: i32 = self.y;
        let mut x1: i32 = self.x + self.w;
        let mut y1: i32 = self.y + self.h;
        if x0 < bounds.x {
            x0 = bounds.x;
        }
        if y0 < bounds.y {
            y0 = bounds.y;
        }
        if x1 > bounds.x + bounds.w {
            x1 = bounds.x + bounds.w;
        }
        if y1 > bounds.y + bounds.h {
            y1 = bounds.y + bounds.h;
        }
        self.x = x0;
        self.y = y0;
        self.w = x1 - x0;
        self.h = y1 - y0;
        if self.is_empty() != 0 {
            return 0;
        }
        1
    }

    fn overlaps(&self, other: *const Rect) -> i32 {
        if self.right() <= other.x {
            return 0;
        }
        if other.x + other.w <= self.x {
            return 0;
        }
        if self.bottom() <= other.y {
            return 0;
        }
        if other.y + other.h <= self.y {
            return 0;
        }
        1
    }

    fn translate(&mut self, dx: i32, dy: i32) {
        self.x = self.x + dx;
        self.y = self.y + dy;
    }
}

// ---------------------------------------------------------------- colour --
// 32-bpp 0x00RRGGBB, matching the linear framebuffer the bare-metal side sets
// up in examples/rpython2c/mbos/vbe.c.

fn rgb(r: i32, g: i32, b: i32) -> i32 {
    let rr: i32 = clamp8(r);
    let gg: i32 = clamp8(g);
    let bb: i32 = clamp8(b);
    (rr << 16) | (gg << 8) | bb
}

fn clamp8(v: i32) -> i32 {
    if v < 0 {
        return 0;
    }
    if v > 255 {
        return 255;
    }
    v
}

fn red_of(c: i32) -> i32 {
    (c >> 16) & 255
}

fn green_of(c: i32) -> i32 {
    (c >> 8) & 255
}

fn blue_of(c: i32) -> i32 {
    c & 255
}

// Mix two colours, `t` running 0..256. Used for the sky gradient.
fn color_mix(a: i32, b: i32, t: i32) -> i32 {
    let u: i32 = 256 - t;
    let r: i32 = (red_of(a) * u + red_of(b) * t) >> 8;
    let g: i32 = (green_of(a) * u + green_of(b) * t) >> 8;
    let bl: i32 = (blue_of(a) * u + blue_of(b) * t) >> 8;
    rgb(r, g, bl)
}

fn color_scale(c: i32, num: i32, den: i32) -> i32 {
    if den <= 0 {
        return c;
    }
    rgb(red_of(c) * num / den,
        green_of(c) * num / den,
        blue_of(c) * num / den)
}

// -------------------------------------------------------------- entities --
// A sprite is a rectangle plus a velocity: enough for the bouncing demo in
// helloworld.c, and enough to show a Rust struct being stepped by C.

struct Sprite {
    body: Rect,
    dx: i32,
    dy: i32,
    color: i32,
    alive: i32,
}

impl Sprite {
    fn new(x: i32, y: i32, w: i32, h: i32, dx: i32, dy: i32, color: i32) -> Sprite {
        Sprite {
            body: Rect { x: x, y: y, w: w, h: h },
            dx: dx,
            dy: dy,
            color: color,
            alive: 1,
        }
    }

    // Advance one tick, bouncing off the walls of `bounds`.
    fn step(&mut self, bounds: *const Rect) {
        self.body.x = self.body.x + self.dx;
        self.body.y = self.body.y + self.dy;
        if self.body.x < bounds.x {
            self.body.x = bounds.x;
            self.dx = -self.dx;
        }
        if self.body.y < bounds.y {
            self.body.y = bounds.y;
            self.dy = -self.dy;
        }
        if self.body.x + self.body.w > bounds.x + bounds.w {
            self.body.x = bounds.x + bounds.w - self.body.w;
            self.dx = -self.dx;
        }
        if self.body.y + self.body.h > bounds.y + bounds.h {
            self.body.y = bounds.y + bounds.h - self.body.h;
            self.dy = -self.dy;
        }
    }

    fn hits(&self, other: *const Sprite) -> i32 {
        if self.alive == 0 {
            return 0;
        }
        if other.alive == 0 {
            return 0;
        }
        self.body.overlaps(&other.body)
    }
}
