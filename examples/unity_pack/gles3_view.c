/* Display a packed Unity scene (engine.c + data.c) via surfaceless
 * OpenGL ES 3.1 -- the headless twin of gles3_window.c.
 *
 *     python3 tools/unity_pack.py examples/unity_pack/MiniScene -o /tmp/upack
 *     gcc -O3 -c /tmp/upack/engine.c -o /tmp/upack/engine.o
 *     gcc -O0 -c /tmp/upack/data.c   -o /tmp/upack/data.o
 *     python3 crust examples/unity_pack/gles3_view.c \
 *         /tmp/upack/engine.o /tmp/upack/data.o \
 *         -I /tmp/upack -o build/unity_gles3_view -lEGL -lGLESv2
 *     EGL_PLATFORM=surfaceless ./build/unity_gles3_view [out.ppm]
 *
 * Or: examples/unity_pack/run_gles3.sh
 *
 * Renders after TICKS_BEFORE_DRAW ticks into a WIDTH x HEIGHT FBO, prints
 * it as ASCII (and writes a PPM when given a path) -- the same frame, the
 * same output, as gles2_view.c: the tests hold the two to identical
 * pixels. A `--gpu-handles` pack's handles are bound at SSBO binding 1.
 */
#include <EGL/egl.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include "gles3_render.h"

float Camera_main_pos_x __attribute__((weak)) = 0.f;
float Camera_main_pos_y __attribute__((weak)) = 0.f;
float Camera_main_pos_z __attribute__((weak)) = -10.f;
float Camera_main_orthographicSize __attribute__((weak)) = 3.f;
float Camera_main_nearClipPlane __attribute__((weak)) = 0.3f;
float Camera_main_farClipPlane __attribute__((weak)) = 1000.f;
float Camera_main_background_r __attribute__((weak)) = 0.f;
float Camera_main_background_g __attribute__((weak)) = 0.f;
float Camera_main_background_b __attribute__((weak)) = 0.f;

#define WIDTH  96
#define HEIGHT 64
#ifndef TICKS_BEFORE_DRAW
#define TICKS_BEFORE_DRAW 30
#endif

static unsigned char pixels[WIDTH * HEIGHT * 4];

static int init_egl(void)
{
    EGLint cfg_attribs[] = {
        EGL_SURFACE_TYPE,    EGL_PBUFFER_BIT,
        EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT,
        EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8,
        EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8,
        EGL_NONE
    };
    EGLint ctx_attribs[] = {
        EGL_CONTEXT_MAJOR_VERSION, 3,
        EGL_CONTEXT_MINOR_VERSION, 1,
        EGL_NONE
    };
    EGLDisplay dpy;
    EGLConfig cfg;
    EGLContext ctx;
    EGLint major, minor, num_config;

    dpy = eglGetDisplay(EGL_DEFAULT_DISPLAY);
    if (dpy == EGL_NO_DISPLAY) {
        printf("eglGetDisplay failed\n");
        return 0;
    }
    if (!eglInitialize(dpy, &major, &minor)) {
        printf("eglInitialize failed (0x%X) -- "
               "is EGL_PLATFORM=surfaceless set?\n", eglGetError());
        return 0;
    }
    if (!eglBindAPI(EGL_OPENGL_ES_API)) {
        printf("eglBindAPI failed\n");
        return 0;
    }
    if (!eglChooseConfig(dpy, cfg_attribs, &cfg, 1, &num_config)
        || num_config < 1) {
        printf("no EGLConfig with OpenGL ES 3\n");
        return 0;
    }
    ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctx_attribs);
    if (ctx == EGL_NO_CONTEXT) {
        printf("eglCreateContext (ES 3.1) failed\n");
        return 0;
    }
    if (!eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, ctx)) {
        printf("eglMakeCurrent failed\n");
        return 0;
    }
    printf("EGL %d.%d, GLES %s\n", (int)major, (int)minor,
           (const char *)glGetString(GL_VERSION));
    return 1;
}

/* The target's format: GL_RGBA4 by default; the tests compare viewers at
 * 8 bits a channel (-DFBO_FORMAT=0x8058, GL_RGBA8), where RGBA4 would
 * round a small difference away. */
#ifndef FBO_FORMAT
#define FBO_FORMAT GL_RGBA4
#endif

static int init_fbo(void)
{
    GLuint fbo, rbo;
    glGenFramebuffers(1, &fbo);
    glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    glGenRenderbuffers(1, &rbo);
    glBindRenderbuffer(GL_RENDERBUFFER, rbo);
    glRenderbufferStorage(GL_RENDERBUFFER, FBO_FORMAT, WIDTH, HEIGHT);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                              GL_RENDERBUFFER, rbo);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
        printf("framebuffer incomplete\n");
        return 0;
    }
    return 1;
}

static int write_ppm(const char *path)
{
    FILE *f = fopen(path, "wb");
    int y, x;
    if (!f) {
        printf("cannot open %s\n", path);
        return 0;
    }
    fprintf(f, "P6\n%d %d\n255\n", WIDTH, HEIGHT);
    for (y = HEIGHT - 1; y >= 0; y--) {
        for (x = 0; x < WIDTH; x++) {
            unsigned char *p = &pixels[(y * WIDTH + x) * 4];
            fputc(p[0], f);
            fputc(p[1], f);
            fputc(p[2], f);
        }
    }
    fclose(f);
    return 1;
}

static void print_ascii(void)
{
    int y, x;
    for (y = HEIGHT - 1; y >= 0; y--) {
        for (x = 0; x < WIDTH; x++) {
            unsigned char *p = &pixels[(y * WIDTH + x) * 4];
            int r = p[0], g = p[1], b = p[2];
            char c = '.';
            if (r + g + b > 24) {
                if (r >= g && r >= b)      c = 'R';
                else if (g >= r && g >= b) c = 'G';
                else                       c = 'B';
            }
            putchar(c);
        }
        putchar('\n');
    }
}

int main(int argc, char **argv)
{
    int t, ndraw;

    engine_apply_argv(argc, argv);
    if (!init_egl())
        return 1;
    if (!init_fbo())
        return 1;
    if (!g3_init())
        return 1;

    /* Let the player drift so it separates from the origin coin cluster. */
    for (t = 0; t < TICKS_BEFORE_DRAW; t++)
        engine_tick();

    ndraw = g3_draw(WIDTH, HEIGHT);
    if (ndraw < 1) {
        printf("engine_collect_draws returned %d\n", ndraw);
        return 1;
    }
    printf("draws=%d classes=%d textures=%d\n",
           ndraw, engine_class_count(), engine_texture_count());
    glFinish();
    glReadPixels(0, 0, WIDTH, HEIGHT, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
    print_ascii();
    if (argc > 1 && !write_ppm(argv[1]))
        return 1;
    return 0;
}
