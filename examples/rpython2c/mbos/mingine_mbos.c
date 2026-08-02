/* mingine_mbos.c -- the three-language game engine, inside mbos.
 *
 * examples/baremetal/kernel_mingine.c runs the same engine as its own bootable
 * image, compiled by ShivyCX. This file runs it as a *program under mbos*,
 * compiled by gcc, driven from the shell and presented through the framebuffer
 * driver in vbe.c.
 *
 * That the two agree is the interesting part. Both render
 * examples/crust/baremetalgames/scene.c and report mg_checksum(); if the
 * numbers match, the same Rust + rpython + C source produced bit-identical
 * pixels through two entirely different compilers. test_mingine_mbos.py
 * asserts exactly that.
 *
 * How the non-C halves get here. ShivyCX splices `#include "mingine.rs"` and
 * `#include "mingine.py"` itself; gcc cannot. So the Makefile pre-lowers both
 * into a staging directory and copies mingine.c and scene.c in beside them --
 * the include *names* are unchanged, the contents are already C, and because
 * a quoted include resolves relative to the including file, the staged copies
 * are what mingine.c finds. No edits to the engine sources, and no second copy
 * of them to drift.
 *
 * The engine draws into a RAM buffer that scene.c owns, and this file hands it
 * to gfx_present(). That is the path gfx_present() was written for: writes to
 * VRAM are write-combined and fast, reads are not, so a frame is built in RAM
 * and blitted once.
 */
#include "mbos.h"

/* py2c lowers Python's floor-division and modulo semantics to calls into the
 * ShivyCX runtime. Only these two are reachable from mingine.py, so provide
 * them here rather than linking the whole runtime into the kernel for the sake
 * of twenty lines. Semantics copied from shivyc_rt.c: the result takes the
 * sign of the divisor, unlike C's truncating % and /.
 *
 * Except in the MBOS_RPYTHON build, which links the real shivyc_rt.c for its
 * generated render path and would then have two definitions of each. */
#ifndef MBOS_RPYTHON
long pymod(long a, long b) {
    long r;
    if (b == 0) return 0;
    r = a % b;
    if (r != 0 && ((r < 0) != (b < 0))) r += b;
    return r;
}

long pyfdiv(long a, long b) {
    long q;
    if (b == 0) return 0;
    q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0))) q -= 1;
    return q;
}
#endif /* MBOS_RPYTHON */

/* The staged copy: mingine.c, with mingine.rs and mingine.py already lowered
 * to C beside it. */
#include "scene.c"

/* ---- presentation ------------------------------------------------------ */

/* Nearest-neighbour upscale, centred, integer scale only so the pixels stay
 * square. Writing through gfx_pixel would be one bounds check and one stride
 * multiply per destination pixel; at 1920x1080 that is 2 million of each per
 * frame, so the row pointer is computed once per output row instead. */
static void present_scaled(void) {
    u32 fw = gfx_width();
    u32 fh = gfx_height();
    int sw = scene_width();
    int sh = scene_height();
    unsigned int *src = scene_buffer();
    int scale, ox, oy, x, y, i, j;

    scale = (int)(fw / (u32)sw);
    j = (int)(fh / (u32)sh);
    if (j < scale) scale = j;
    if (scale < 1) scale = 1;

    ox = ((int)fw - sw * scale) / 2;
    oy = ((int)fh - sh * scale) / 2;

    for (y = 0; y < sh; y++) {
        for (j = 0; j < scale; j++) {
            int dy = oy + y * scale + j;
            if (dy < 0 || dy >= (int)fh) continue;
            for (x = 0; x < sw; x++) {
                u32 c = src[y * sw + x];
                for (i = 0; i < scale; i++) {
                    int dx = ox + x * scale + i;
                    if (dx >= 0 && dx < (int)fw) gfx_pixel((u32)dx, (u32)dy, c);
                }
            }
        }
    }
}

/* ---- entry points used by the shell ------------------------------------ */

/* Render the deterministic scene and report the checksum. This is the one that
 * has to agree with the ShivyCX-built kernel and with the hosted run. */
unsigned int mingine_render(void) {
    return scene_render();
}

void mingine_present(void) {
    if (gfx_up()) present_scaled();
}

int mingine_width(void)   { return scene_width(); }
int mingine_height(void)  { return scene_height(); }
int mingine_ball_x(void)  { return scene_ball_x(); }
int mingine_ball_y(void)  { return scene_ball_y(); }
int mingine_foe_x(void)   { return scene_foe_x(); }
int mingine_foe_y(void)   { return scene_foe_y(); }
int mingine_score(void)   { return scene_score(); }
