/* Display a packed Unity scene (engine.c + data.c) via surfaceless GLES2.
 *
 *     python3 tools/unity_pack.py examples/unity_pack/MiniScene -o /tmp/upack
 *     # copy or -I this file's sibling engine_draw.h from the pack output
 *     gcc -O3 -c /tmp/upack/engine.c -o /tmp/upack/engine.o
 *     gcc -O0 -c /tmp/upack/data.c   -o /tmp/upack/data.o
 *     python3 crust examples/unity_pack/gles2_view.c \
 *         /tmp/upack/engine.o /tmp/upack/data.o \
 *         -I /tmp/upack -o build/unity_gles2_view -lEGL -lGLESv2
 *     EGL_PLATFORM=surfaceless ./build/unity_gles2_view
 *
 * Or: examples/unity_pack/run_gles2.sh
 *
 * Draws each engine_collect_draws() sprite as a coloured quad. Same FBO
 * readback path as examples/gles2/triangle.c — no window system.
 */

#include <EGL/egl.h>
#include <GLES2/gl2.h>
#include <stddef.h>
#ifdef __wasm__
#include <wasi.h>
#else
#include <stdio.h>
#include <stdlib.h>
#endif

#include "engine_draw.h"

#define WIDTH  96
#define HEIGHT 64
#define MAX_DRAWS 64
/* Two triangles per quad, 5 floats (xy + rgb) per vertex. */
#define MAX_FLOATS (MAX_DRAWS * 6 * 5)

/* Ortho covering MiniScene (-1..2) with margin. */
#define WORLD_LEFT   (-3.0f)
#define WORLD_RIGHT  ( 3.0f)
#define WORLD_BOTTOM (-2.0f)
#define WORLD_TOP    ( 3.0f)

#define TICKS_BEFORE_DRAW 30

static const char *VERT_SRC =
    "attribute vec2 a_pos;\n"
    "attribute vec3 a_color;\n"
    "varying vec3 v_color;\n"
    "void main() {\n"
    "    v_color = a_color;\n"
    "    gl_Position = vec4(a_pos, 0.0, 1.0);\n"
    "}\n";

static const char *FRAG_SRC =
    "precision mediump float;\n"
    "varying vec3 v_color;\n"
    "void main() {\n"
    "    gl_FragColor = vec4(v_color, 1.0);\n"
    "}\n";

static unsigned char pixels[WIDTH * HEIGHT * 4];
static GLfloat vert_buf[MAX_FLOATS];

static float world_to_ndc_x(float x)
{
    return 2.0f * (x - WORLD_LEFT) / (WORLD_RIGHT - WORLD_LEFT) - 1.0f;
}

static float world_to_ndc_y(float y)
{
    return 2.0f * (y - WORLD_BOTTOM) / (WORLD_TOP - WORLD_BOTTOM) - 1.0f;
}

static void emit_vert(int *ni, float x, float y, float r, float g, float b)
{
    int i = *ni;
    if (i + 5 > MAX_FLOATS)
        return;
    vert_buf[i] = x;
    vert_buf[i + 1] = y;
    vert_buf[i + 2] = r;
    vert_buf[i + 3] = g;
    vert_buf[i + 4] = b;
    *ni = i + 5;
}

/* Expand one axis-aligned sprite into two triangles in NDC. */
static void emit_quad(int *ni, const EngineDraw *d)
{
    float x0 = world_to_ndc_x(d->x - d->half_w);
    float x1 = world_to_ndc_x(d->x + d->half_w);
    float y0 = world_to_ndc_y(d->y - d->half_h);
    float y1 = world_to_ndc_y(d->y + d->half_h);
    float r = d->r, g = d->g, b = d->b;

    emit_vert(ni, x0, y0, r, g, b);
    emit_vert(ni, x1, y0, r, g, b);
    emit_vert(ni, x0, y1, r, g, b);

    emit_vert(ni, x1, y0, r, g, b);
    emit_vert(ni, x1, y1, r, g, b);
    emit_vert(ni, x0, y1, r, g, b);
}

static GLuint compile_stage(GLenum type, const char *src, const char *what)
{
    GLuint sh = glCreateShader(type);
    GLint ok = 0;

    glShaderSource(sh, 1, &src, NULL);
    glCompileShader(sh);
    glGetShaderiv(sh, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[1024];
        GLsizei len = 0;
        glGetShaderInfoLog(sh, (GLsizei)sizeof(log), &len, log);
        printf("%s shader failed:\n%s\n", what, log);
        return 0;
    }
    return sh;
}

static GLuint build_program(void)
{
    GLuint vs = compile_stage(GL_VERTEX_SHADER, VERT_SRC, "vertex");
    GLuint fs = compile_stage(GL_FRAGMENT_SHADER, FRAG_SRC, "fragment");
    GLuint prog;
    GLint ok = 0;

    if (!vs || !fs)
        return 0;
    prog = glCreateProgram();
    glAttachShader(prog, vs);
    glAttachShader(prog, fs);
    glBindAttribLocation(prog, 0, "a_pos");
    glBindAttribLocation(prog, 1, "a_color");
    glLinkProgram(prog);
    glGetProgramiv(prog, GL_LINK_STATUS, &ok);
    if (!ok) {
        char log[1024];
        GLsizei len = 0;
        glGetProgramInfoLog(prog, (GLsizei)sizeof(log), &len, log);
        printf("link failed:\n%s\n", log);
        return 0;
    }
    glDeleteShader(vs);
    glDeleteShader(fs);
    return prog;
}

static int init_egl(void)
{
    EGLint cfg_attribs[] = {
        EGL_SURFACE_TYPE,    EGL_PBUFFER_BIT,
        EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
        EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8,
        EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8,
        EGL_NONE
    };
    EGLint ctx_attribs[] = { EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE };
    EGLDisplay dpy;
    EGLConfig cfg;
    EGLContext ctx;
    EGLint major = 0, minor = 0, num_config = 0;

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
        printf("no EGLConfig\n");
        return 0;
    }
    ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctx_attribs);
    if (ctx == EGL_NO_CONTEXT) {
        printf("eglCreateContext failed\n");
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

static int init_fbo(void)
{
    GLuint fbo, rbo;

    glGenFramebuffers(1, &fbo);
    glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    glGenRenderbuffers(1, &rbo);
    glBindRenderbuffer(GL_RENDERBUFFER, rbo);
    glRenderbufferStorage(GL_RENDERBUFFER, GL_RGBA4, WIDTH, HEIGHT);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                              GL_RENDERBUFFER, rbo);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
        printf("framebuffer incomplete\n");
        return 0;
    }
    return 1;
}

static int draw_scene(GLuint prog)
{
    EngineDraw draws[MAX_DRAWS];
    int ndraw;
    int nfloats = 0;
    int i;
    GLuint vbo;
    int nverts;

    ndraw = engine_collect_draws(draws, MAX_DRAWS);
    if (ndraw < 1) {
        printf("engine_collect_draws returned %d\n", ndraw);
        return 0;
    }
    printf("draws=%d classes=%d\n", ndraw, engine_class_count());

    for (i = 0; i < ndraw; i++)
        emit_quad(&nfloats, &draws[i]);

    nverts = nfloats / 5;
    glGenBuffers(1, &vbo);
    glBindBuffer(GL_ARRAY_BUFFER, vbo);
    glBufferData(GL_ARRAY_BUFFER,
                 (GLsizeiptr)(nfloats * (int)sizeof(GLfloat)),
                 vert_buf, GL_STATIC_DRAW);

    glViewport(0, 0, WIDTH, HEIGHT);
    glClearColor(0.0f, 0.0f, 0.0f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);

    glUseProgram(prog);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE,
                          (GLsizei)(5 * sizeof(GLfloat)), (const void *)0);
    glEnableVertexAttribArray(1);
    glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE,
                          (GLsizei)(5 * sizeof(GLfloat)),
                          (const void *)(2 * sizeof(GLfloat)));
    glDrawArrays(GL_TRIANGLES, 0, nverts);
    glFinish();
    return 1;
}

static void read_back(void)
{
    glReadPixels(0, 0, WIDTH, HEIGHT, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
}

#ifndef __wasm__
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
#endif

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
    GLuint prog;
    int t;

    if (!init_egl())
        return 1;
    if (!init_fbo())
        return 1;
    prog = build_program();
    if (!prog)
        return 1;

    /* Let the player drift so it separates from the origin coin cluster. */
    for (t = 0; t < TICKS_BEFORE_DRAW; t++)
        engine_tick();

    if (!draw_scene(prog))
        return 1;
    read_back();
    print_ascii();

#ifndef __wasm__
    if (argc > 1 && !write_ppm(argv[1]))
        return 1;
#else
    (void)argc;
    (void)argv;
#endif
    return 0;
}
