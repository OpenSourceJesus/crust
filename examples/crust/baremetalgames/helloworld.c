/* helloworld.c -- a scene drawn with the three-language mini engine.
 *
 *   #include "mingine.c"   the engine, which itself pulls in mingine.rs
 *                          (Rust geometry) and mingine.py (rpython rules)
 *   #include "mingine.py"  the rules layer again, directly -- the guard in
 *                          mingine.c means it is spliced once, and naming it
 *                          here documents that this file calls into it too
 *
 * Run hosted, it renders into a malloc'd buffer and prints the result as
 * ASCII art plus a checksum, so the whole three-language path is testable
 * without a display. On bare metal the same code writes to the framebuffer
 * that vbe.c brings up -- build with -DMINGINE_BAREMETAL and hand mg_attach
 * the address from gfx_init().
 */

#include "mingine.c"

#ifndef MINGINE_PY_INCLUDED
#define MINGINE_PY_INCLUDED
#include "mingine.py"
#endif

int printf(const char *, ...);
int fprintf(void *, const char *, ...);
extern void *stderr;
void *malloc(unsigned long);

#define SCREEN_W 96
#define SCREEN_H 48

/* Draw one frame of the scene. Every layer crosses a language boundary that
 * does not exist at runtime:
 *
 *   sky        rpython picks the gradient stops, Rust mixes them, C writes
 *   ground     Rust clipping, rpython brick pattern
 *   sprites    Rust struct stepping itself, C blitting it
 *   score      rpython scoring, C font rendering
 */
static void draw_scene(int tick, Sprite *ball, Sprite *foe) {
    int horizon = SCREEN_H / 2;
    int bob;
    int sun_x;
    int score;

    /* --- sky: two colours chosen here, blended by Rust in mg_gradient --- */
    mg_gradient(Rect_new(0, 0, SCREEN_W, horizon),
                rgb(30, 40, 90), rgb(160, 120, 90));

    /* --- sun: rpython's ease curve decides where it has risen to --- */
    sun_x = 8 + ease_in(tick, 40, SCREEN_W - 24);
    mg_disc(sun_x, horizon - 8, 5, rgb(255, 220, 120));

    /* --- ground: an rpython pattern painted through Rust clipping --- */
    mg_fill_xywh(0, horizon, SCREEN_W, SCREEN_H - horizon, rgb(18, 42, 24));
    mg_draw_bricks(11, 3, 8, 6, 4, horizon + 3, rgb(210, 120, 90));

    /* --- sprites: each steps itself in Rust, C draws it --- */
    mg_step_sprite(ball);
    mg_step_sprite(foe);
    if (Sprite_hits(ball, foe) != 0) {
        /* a hit tints both, using the Rust colour mixer */
        ball->color = color_mix(ball->color, rgb(255, 0, 0), 128);
    }
    mg_draw_sprite(ball);
    mg_draw_sprite(foe);

    /* --- a banner that bobs on rpython's triangle wave --- */
    bob = wave(tick, 2, 24);
    mg_text("HELLO", 6, 4 + bob, 1, rgb(255, 255, 255));
    mg_text("CRUST", 6, 12 + bob, 1, rgb(200, 220, 255));

    /* --- score: computed in rpython, drawn in C --- */
    score = score_for(1, 3, tick);
    mg_number(score, SCREEN_W - 30, 4, 1, rgb(255, 240, 160));
}

/* Print the framebuffer as ASCII, so a hosted run shows the picture. It goes
 * to stderr on purpose: stdout then carries only the three deterministic
 * summary lines, which makes this example a golden test as well as a demo. */
static void dump_ascii(void) {
    const char *ramp = " .:-=+*#%@";
    int x;
    int y;
    int v;
    for (y = 0; y < SCREEN_H; y += 2) {
        for (x = 0; x < SCREEN_W; x += 1) {
            v = mg_luma(x, y) * 9 / 255;
            if (v < 0) { v = 0; }
            if (v > 9) { v = 9; }
            fprintf(stderr, "%c", ramp[v]);
        }
        fprintf(stderr, "\n");
    }
}

int main(void) {
    unsigned int *fb;
    Sprite ball;
    Sprite foe;
    int tick;
    int seed;

    fb = (unsigned int *)malloc(SCREEN_W * SCREEN_H * 4);
    if (fb == 0) {
        return 1;
    }
    mg_attach(fb, SCREEN_W, SCREEN_H);
    mg_clear(rgb(0, 0, 0));

    /* rpython's RNG places the sprites, so the scene is reproducible */
    seed = lcg_next(20260801);
    ball = Sprite_new(rand_range(seed, 8, 40), 20, 6, 4,
                      level_speed(2), 1, rgb(240, 200, 60));
    seed = lcg_next(seed);
    foe = Sprite_new(rand_range(seed, 50, 80), 26, 5, 5,
                     -level_speed(1), 1, rgb(90, 180, 230));

    for (tick = 0; tick < 24; tick++) {
        draw_scene(tick, &ball, &foe);
    }

    dump_ascii();
    printf("ball  = %d,%d  foe = %d,%d\n",
           ball.body.x, ball.body.y, foe.body.x, foe.body.y);
    printf("score = %d\n", score_for(1, 3, 24));
    printf("pixels= %08x\n", mg_checksum());
    return 0;
}
