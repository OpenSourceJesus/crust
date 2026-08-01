/* mingine.c -- a tiny game engine in three languages, one translation unit.
 *
 *   #include "mingine.rs"   geometry, colour, sprites   (Rust, via crust.py)
 *   #include "mingine.py"   level rules, RNG, curves    (rpython, via py2c.py)
 *   ...and this file, C, owning the framebuffer.
 *
 * Each language does the part it is actually best at:
 *
 *   Rust     types with invariants -- a Rect that knows how to clip itself,
 *            a Sprite that knows how to bounce.
 *   rpython  integer rules -- level patterns, animation curves, scoring; the
 *            code you want to read six months later.
 *   C        raw memory -- a pointer to a linear framebuffer and a loop.
 *
 * There is no FFI anywhere. All three become one stream of C before the lexer
 * runs, so `Rect_clip` called from `mg_fill` is a direct call that inlines.
 *
 * The engine does not own its pixels. `mg_attach` takes whatever buffer the
 * caller has: malloc'd memory when hosted, or the Bochs-VBE linear framebuffer
 * that examples/rpython2c/mbos/vbe.c brings up on bare metal. Same engine,
 * same drawing code, either target.
 */

#ifndef MINGINE_C_INCLUDED
#define MINGINE_C_INCLUDED

#include "mingine.rs"

#ifndef MINGINE_PY_INCLUDED
#define MINGINE_PY_INCLUDED
#include "mingine.py"
#endif

/* ------------------------------------------------------------- surface -- */

typedef struct {
    unsigned int *pixels;   /* 32-bpp 0x00RRGGBB, row-major */
    int w;
    int h;
    Rect bounds;            /* the Rust rectangle every draw clips against */
} MgSurface;

static MgSurface mg_screen;

void mg_attach(unsigned int *pixels, int w, int h) {
    mg_screen.pixels = pixels;
    mg_screen.w = w;
    mg_screen.h = h;
    mg_screen.bounds = Rect_new(0, 0, w, h);
}

int mg_width(void)  { return mg_screen.w; }
int mg_height(void) { return mg_screen.h; }

/* ------------------------------------------------------------ plotting -- */

void mg_pixel(int x, int y, int color) {
    if (Rect_contains(&mg_screen.bounds, x, y) == 0) {
        return;
    }
    mg_screen.pixels[y * mg_screen.w + x] = (unsigned int)color;
}

void mg_clear(int color) {
    int n = mg_screen.w * mg_screen.h;
    int i;
    for (i = 0; i < n; i++) {
        mg_screen.pixels[i] = (unsigned int)color;
    }
}

/* Fill a rectangle. The clip happens once, in Rust, before the loop: an
 * entirely off-screen rectangle costs four comparisons instead of a pass over
 * pixels that get discarded. */
void mg_fill(Rect r, int color) {
    int x;
    int y;
    if (Rect_clip(&r, &mg_screen.bounds) == 0) {
        return;
    }
    for (y = r.y; y < r.y + r.h; y++) {
        for (x = r.x; x < r.x + r.w; x++) {
            mg_screen.pixels[y * mg_screen.w + x] = (unsigned int)color;
        }
    }
}

void mg_fill_xywh(int x, int y, int w, int h, int color) {
    mg_fill(Rect_new(x, y, w, h), color);
}

void mg_frame(Rect r, int thickness, int color) {
    mg_fill(Rect_new(r.x, r.y, r.w, thickness), color);
    mg_fill(Rect_new(r.x, r.y + r.h - thickness, r.w, thickness), color);
    mg_fill(Rect_new(r.x, r.y, thickness, r.h), color);
    mg_fill(Rect_new(r.x + r.w - thickness, r.y, thickness, r.h), color);
}

/* A vertical gradient, mixing in Rust and stepping in C. */
void mg_gradient(Rect r, int top, int bottom) {
    int y;
    int t;
    if (Rect_clip(&r, &mg_screen.bounds) == 0) {
        return;
    }
    for (y = 0; y < r.h; y++) {
        t = (y * 256) / r.h;
        mg_fill_xywh(r.x, r.y + y, r.w, 1, color_mix(top, bottom, t));
    }
}

/* Midpoint circle, filled. */
void mg_disc(int cx, int cy, int radius, int color) {
    int y;
    int x;
    int rr = radius * radius;
    for (y = -radius; y <= radius; y++) {
        for (x = -radius; x <= radius; x++) {
            if (x * x + y * y <= rr) {
                mg_pixel(cx + x, cy + y, color);
            }
        }
    }
}

/* ------------------------------------------------------------- sprites -- */

void mg_draw_sprite(Sprite *s) {
    if (s->alive == 0) {
        return;
    }
    mg_fill(s->body, s->color);
    /* a lighter top edge, so the shape reads as lit from above */
    mg_fill(Rect_new(s->body.x, s->body.y, s->body.w, 1),
            color_mix(s->color, rgb(255, 255, 255), 96));
}

void mg_step_sprite(Sprite *s) {
    Sprite_step(s, &mg_screen.bounds);
}

/* ------------------------------------------------------ level painting -- */
/* The pattern predicates come from rpython, the pixels from C, the clipping
 * from Rust. This function is the whole point of the example. */

void mg_draw_bricks(int cols, int rows, int tile_w, int tile_h,
                    int x0, int y0, int base_color) {
    int row;
    int col;
    int shade;
    int x;
    int y;
    for (row = 0; row < rows; row++) {
        for (col = 0; col < cols; col++) {
            if (is_brick(col, row, cols, rows) == 0) {
                continue;
            }
            x = tile_origin_x(col, tile_w, x0);
            y = tile_origin_y(row, tile_h, y0);
            shade = 200 + 55 * is_checker(col, row);
            mg_fill_xywh(x, y, tile_w - 1, tile_h - 1,
                         color_scale(base_color, shade, 255));
        }
    }
}

void mg_draw_pyramid(int cols, int rows, int tile_w, int tile_h,
                     int x0, int y0, int color) {
    int row;
    int col;
    for (row = 0; row < rows; row++) {
        for (col = 0; col < cols; col++) {
            if (is_pyramid(col, row, cols) == 0) {
                continue;
            }
            mg_fill_xywh(tile_origin_x(col, tile_w, x0),
                         tile_origin_y(row, tile_h, y0),
                         tile_w - 1, tile_h - 1,
                         color_scale(color, 128 + row * 30, 255));
        }
    }
}

/* ---------------------------------------------------------------- text -- */
/* A 5x7 font, one bit per pixel, enough for a score line. Only the glyphs the
 * demo needs -- a real engine would pull in font8x16.h from mbos. */

static const unsigned char mg_font[39][7] = {
    {0x1E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x1E},  /* 0 */
    {0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E},  /* 1 */
    {0x1E, 0x01, 0x01, 0x1E, 0x10, 0x10, 0x1F},  /* 2 */
    {0x1E, 0x01, 0x01, 0x0E, 0x01, 0x01, 0x1E},  /* 3 */
    {0x11, 0x11, 0x11, 0x1F, 0x01, 0x01, 0x01},  /* 4 */
    {0x1F, 0x10, 0x10, 0x1E, 0x01, 0x01, 0x1E},  /* 5 */
    {0x0E, 0x10, 0x10, 0x1E, 0x11, 0x11, 0x0E},  /* 6 */
    {0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08},  /* 7 */
    {0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E},  /* 8 */
    {0x0E, 0x11, 0x11, 0x0F, 0x01, 0x01, 0x0E},  /* 9 */
    {0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11},  /* A */
    {0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E},  /* B */
    {0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E},  /* C */
    {0x1C, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1C},  /* D */
    {0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F},  /* E */
    {0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10},  /* F */
    {0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F},  /* G */
    {0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11},  /* H */
    {0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E},  /* I */
    {0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C},  /* J */
    {0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11},  /* K */
    {0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F},  /* L */
    {0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11},  /* M */
    {0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11},  /* N */
    {0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E},  /* O */
    {0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10},  /* P */
    {0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D},  /* Q */
    {0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11},  /* R */
    {0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E},  /* S */
    {0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04},  /* T */
    {0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E},  /* U */
    {0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04},  /* V */
    {0x11, 0x11, 0x11, 0x15, 0x15, 0x15, 0x0A},  /* W */
    {0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11},  /* X */
    {0x11, 0x11, 0x0A, 0x04, 0x04, 0x04, 0x04},  /* Y */
    {0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F},  /* Z */
    {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00},  /* space */
    {0x00, 0x00, 0x00, 0x00, 0x00, 0x0C, 0x0C},  /* . */
    {0x00, 0x00, 0x00, 0x1F, 0x00, 0x00, 0x00},  /* - */
};

static int mg_glyph(int ch) {
    if (ch >= '0' && ch <= '9') { return ch - '0'; }
    if (ch >= 'A' && ch <= 'Z') { return 10 + (ch - 'A'); }
    if (ch >= 'a' && ch <= 'z') { return 10 + (ch - 'a'); }
    if (ch == '.') { return 37; }
    if (ch == '-') { return 38; }
    return 36;
}

void mg_char(int ch, int x, int y, int scale, int color) {
    int row;
    int col;
    int bits;
    int g = mg_glyph(ch);
    for (row = 0; row < 7; row++) {
        bits = (int)mg_font[g][row];
        for (col = 0; col < 5; col++) {
            if ((bits >> (4 - col)) & 1) {
                mg_fill_xywh(x + col * scale, y + row * scale, scale, scale,
                             color);
            }
        }
    }
}

void mg_text(const char *s, int x, int y, int scale, int color) {
    int i = 0;
    while (s[i] != 0) {
        mg_char((int)s[i], x + i * 6 * scale, y, scale, color);
        i++;
    }
}

void mg_number(int value, int x, int y, int scale, int color) {
    char buf[12];
    int n = 0;
    int i;
    int v = value;
    if (v < 0) {
        mg_char('-', x, y, scale, color);
        x = x + 6 * scale;
        v = -v;
    }
    if (v == 0) {
        buf[n] = '0';
        n = 1;
    }
    while (v > 0) {
        buf[n] = (char)('0' + (v % 10));
        v = v / 10;
        n++;
    }
    for (i = 0; i < n; i++) {
        mg_char((int)buf[n - 1 - i], x + i * 6 * scale, y, scale, color);
    }
}

/* ------------------------------------------------------------ readback -- */
/* Hosted builds have no screen. These let the demo prove it drew the right
 * thing: a checksum for regression testing, and an ASCII rendering so a human
 * can see the picture in a terminal. On bare metal neither is called -- the
 * pixels are already on the display. */

unsigned int mg_checksum(void) {
    unsigned int h = 2166136261u;
    int n = mg_screen.w * mg_screen.h;
    int i;
    for (i = 0; i < n; i++) {
        h = h ^ mg_screen.pixels[i];
        h = h * 16777619u;
    }
    return h;
}

int mg_luma(int x, int y) {
    unsigned int c;
    if (Rect_contains(&mg_screen.bounds, x, y) == 0) {
        return 0;
    }
    c = mg_screen.pixels[y * mg_screen.w + x];
    return (red_of((int)c) * 30 + green_of((int)c) * 59
            + blue_of((int)c) * 11) / 100;
}

#endif /* MINGINE_C_INCLUDED */
