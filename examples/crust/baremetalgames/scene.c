/* scene.c -- one deterministic mingine scene, drawn into a caller-owned buffer.
 *
 * helloworld.c is the demo: it mallocs a surface and dumps ASCII. This file is
 * the same scene with every trace of the host removed -- no malloc, no stdio,
 * no libc at all -- so that the *identical* code can be compiled twice by
 * ShivyCX and run in two places:
 *
 *   scene_host.c            hosted, prints the checksum to stdout
 *   ../../baremetal/kernel_mingine.c
 *                           a bootable kernel, prints it to serial and blits
 *                           the pixels to a Bochs-VBE framebuffer
 *
 * If the two checksums agree, the whole three-language path -- Rust geometry
 * from mingine.rs, rpython rules from mingine.py, C blitting here -- produced
 * bit-identical pixels through a compiler that is on its way to compiling
 * itself. That is the property worth testing; the picture is a bonus.
 *
 * Everything is deterministic on purpose: the RNG is seeded with a constant,
 * the frame count is fixed, and nothing reads a clock. A checksum is only a
 * useful oracle if the same input can never produce two answers.
 */
#ifndef MINGINE_SCENE_C
#define MINGINE_SCENE_C

#include "mingine.c"

#ifndef MINGINE_PY_INCLUDED
#define MINGINE_PY_INCLUDED
#include "mingine.py"
#endif

#define SCENE_W 320
#define SCENE_H 200
#define SCENE_FRAMES 24
#define SCENE_SEED 20260801

/* The surface. A static array rather than an allocation, because the
 * bare-metal side has a heap only if someone writes one, and this needs to
 * work before that is true. */
static unsigned int scene_pixels[SCENE_W * SCENE_H];

static Sprite scene_ball;
static Sprite scene_foe;

/* One frame. Every layer crosses a language boundary that does not exist at
 * runtime: rpython picks the numbers, Rust does the geometry, C writes pixels. */
static void scene_frame(int tick) {
    int horizon;
    int bob;
    int sun_x;
    int score;

    horizon = SCENE_H / 2;

    /* sky: two colours chosen here, blended by Rust in mg_gradient */
    mg_gradient(Rect_new(0, 0, SCENE_W, horizon),
                rgb(30, 40, 90), rgb(160, 120, 90));

    /* sun: rpython's ease curve decides how far it has risen */
    sun_x = 20 + ease_in(tick, SCENE_FRAMES * 2, SCENE_W - 80);
    mg_disc(sun_x, horizon - 26, 18, rgb(255, 220, 120));

    /* ground: an rpython brick pattern painted through Rust clipping */
    mg_fill_xywh(0, horizon, SCENE_W, SCENE_H - horizon, rgb(18, 42, 24));
    mg_draw_bricks(11, 4, 28, 18, 12, horizon + 10, rgb(210, 120, 90));
    mg_draw_pyramid(7, 4, 18, 12, SCENE_W / 2 - 60, horizon + 4,
                    rgb(120, 90, 160));

    /* sprites: each steps itself in Rust, C draws it */
    mg_step_sprite(&scene_ball);
    mg_step_sprite(&scene_foe);
    if (Sprite_hits(&scene_ball, &scene_foe) != 0) {
        scene_ball.color = color_mix(scene_ball.color, rgb(255, 0, 0), 128);
    }
    mg_draw_sprite(&scene_ball);
    mg_draw_sprite(&scene_foe);

    /* banner bobbing on rpython's triangle wave */
    bob = wave(tick, 2, 24);
    mg_text("CRUST", 12, 14 + bob, 3, rgb(255, 255, 255));
    mg_text("MBOS", 12, 44 + bob, 2, rgb(200, 220, 255));

    /* score computed in rpython, drawn in C */
    score = score_for(1, 3, tick);
    mg_number(score, SCENE_W - 90, 12, 2, rgb(255, 240, 160));
}

/* Draw the whole animation into scene_pixels and return the checksum of the
 * final frame. Callers get the buffer from scene_buffer(). */
unsigned int scene_render(void) {
    int tick;
    int seed;

    mg_attach(scene_pixels, SCENE_W, SCENE_H);
    mg_clear(rgb(0, 0, 0));

    /* rpython's RNG places the sprites, so the scene is reproducible */
    seed = lcg_next(SCENE_SEED);
    scene_ball = Sprite_new(rand_range(seed, 30, 140), 90, 22, 16,
                            level_speed(2), 1, rgb(240, 200, 60));
    seed = lcg_next(seed);
    scene_foe = Sprite_new(rand_range(seed, 180, 260), 120, 18, 18,
                           -level_speed(1), 1, rgb(90, 180, 230));

    for (tick = 0; tick < SCENE_FRAMES; tick++) {
        scene_frame(tick);
    }
    return mg_checksum();
}

unsigned int *scene_buffer(void) { return scene_pixels; }
int scene_width(void)  { return SCENE_W; }
int scene_height(void) { return SCENE_H; }

/* Where the sprites ended up. Reported alongside the checksum because if the
 * two builds ever disagree, knowing whether the *simulation* or only the
 * *rasterisation* diverged is the first thing worth knowing. */
int scene_ball_x(void) { return scene_ball.body.x; }
int scene_ball_y(void) { return scene_ball.body.y; }
int scene_foe_x(void)  { return scene_foe.body.x; }
int scene_foe_y(void)  { return scene_foe.body.y; }
int scene_score(void)  { return score_for(1, 3, SCENE_FRAMES); }

#endif /* MINGINE_SCENE_C */
