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
 * Draws each engine_collect_draws() sprite as a textured quad (PNG × tint).
 * Same FBO readback path as examples/gles2/triangle.c — no window system.
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
#define MAX_DRAWS 64
#define MAX_TEX 32
#define VERT_STRIDE 8
#define MAX_FLOATS (6 * VERT_STRIDE)

#define TICKS_BEFORE_DRAW 30

static const char *VERT_SRC =
    "attribute vec2 a_pos;\n"
    "attribute vec4 a_color;\n"
    "attribute vec2 a_uv;\n"
    "varying vec4 v_color;\n"
    "varying vec2 v_uv;\n"
    "void main() {\n"
    "    v_color = a_color;\n"
    "    v_uv = a_uv;\n"
    "    gl_Position = vec4(a_pos, 0.0, 1.0);\n"
    "}\n";

static const char *FRAG_SRC =
    "precision mediump float;\n"
    "varying vec4 v_color;\n"
    "varying vec2 v_uv;\n"
    "uniform sampler2D u_tex;\n"
    "void main() {\n"
    "    vec4 t = texture2D(u_tex, v_uv);\n"
    "    gl_FragColor = vec4(t.rgb * v_color.rgb, t.a * v_color.a);\n"
    "}\n";

static unsigned char pixels[WIDTH * HEIGHT * 4];
static GLfloat vert_buf[MAX_FLOATS];
static GLuint gl_tex[MAX_TEX];
static int tex_n;
static GLint u_tex_loc;
static float world_left, world_right, world_bottom, world_top;

static void refresh_camera_bounds(float aspect)
{
    float half_h = Camera_main_orthographicSize;
    float half_w = half_h * aspect;
    if (half_w < 0.01f)
        half_w = 0.01f;
    world_left = Camera_main_pos_x - half_w;
    world_right = Camera_main_pos_x + half_w;
    world_bottom = Camera_main_pos_y - half_h;
    world_top = Camera_main_pos_y + half_h;
}

static float world_to_ndc_x(float x)
{
    return 2.0f * (x - world_left) / (world_right - world_left) - 1.0f;
}

static float world_to_ndc_y(float y)
{
    return 2.0f * (y - world_bottom) / (world_top - world_bottom) - 1.0f;
}

static void emit_vert(int *ni, float x, float y, float r, float g, float b,
                      float a, float u, float v)
{
    int i = *ni;
    if (i + VERT_STRIDE > MAX_FLOATS)
        return;
    vert_buf[i] = x;
    vert_buf[i + 1] = y;
    vert_buf[i + 2] = r;
    vert_buf[i + 3] = g;
    vert_buf[i + 4] = b;
    vert_buf[i + 5] = a;
    vert_buf[i + 6] = u;
    vert_buf[i + 7] = v;
    *ni = i + VERT_STRIDE;
}

/* Expand one sprite into two triangles in NDC (full localRotation XY basis). */
static void emit_quad(int *ni, const EngineDraw *d)
{
    float hw = d->half_w, hh = d->half_h;
    float m00 = d->m00, m01 = d->m01, m10 = d->m10, m11 = d->m11;
    float r = d->r, g = d->g, b = d->b, a = d->a;
    float lx[4] = {-hw, hw, -hw, hw};
    float ly[4] = {-hh, -hh, hh, hh};
    float u[4] = {0.f, 1.f, 0.f, 1.f};
    float v[4] = {0.f, 0.f, 1.f, 1.f};
    float nx[4], ny[4];
    int i;

    for (i = 0; i < 4; i = i + 1) {
        float wx = d->x + m00 * lx[i] + m01 * ly[i];
        float wy = d->y + m10 * lx[i] + m11 * ly[i];
        nx[i] = world_to_ndc_x(wx);
        ny[i] = world_to_ndc_y(wy);
    }
    emit_vert(ni, nx[0], ny[0], r, g, b, a, u[0], v[0]);
    emit_vert(ni, nx[1], ny[1], r, g, b, a, u[1], v[1]);
    emit_vert(ni, nx[2], ny[2], r, g, b, a, u[2], v[2]);

    emit_vert(ni, nx[1], ny[1], r, g, b, a, u[1], v[1]);
    emit_vert(ni, nx[3], ny[3], r, g, b, a, u[3], v[3]);
    emit_vert(ni, nx[2], ny[2], r, g, b, a, u[2], v[2]);
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
    glBindAttribLocation(prog, 2, "a_uv");
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

static int upload_textures(void)
{
    int i;
    tex_n = engine_texture_count();
    if (tex_n > MAX_TEX)
        tex_n = MAX_TEX;
    if (tex_n < 1)
        return 1;
    glGenTextures(tex_n, gl_tex);
    for (i = 0; i < tex_n; i++) {
        int w = engine_texture_width(i);
        int h = engine_texture_height(i);
        const unsigned char *rgba = engine_texture_rgba(i);
        if (!rgba || w < 1 || h < 1)
            return 0;
        glBindTexture(GL_TEXTURE_2D, gl_tex[i]);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA,
                     GL_UNSIGNED_BYTE, rgba);
    }
    return 1;
}

static int draw_scene(GLuint prog)
{
    EngineDraw draws[MAX_DRAWS];
    int ndraw;
    int i;
    GLuint vbo;
    GLsizei stride = (GLsizei)(VERT_STRIDE * sizeof(GLfloat));

    ndraw = engine_collect_draws(draws, MAX_DRAWS);
    if (ndraw < 1) {
        printf("engine_collect_draws returned %d\n", ndraw);
        return 0;
    }
    printf("draws=%d classes=%d textures=%d\n",
           ndraw, engine_class_count(), engine_texture_count());

    refresh_camera_bounds((float)WIDTH / (float)HEIGHT);
    glGenBuffers(1, &vbo);
    glBindBuffer(GL_ARRAY_BUFFER, vbo);

    glViewport(0, 0, WIDTH, HEIGHT);
    glClearColor(Camera_main_background_r, Camera_main_background_g,
                 Camera_main_background_b, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);

    glUseProgram(prog);
    glActiveTexture(GL_TEXTURE0);
    glUniform1i(u_tex_loc, 0);
    for (i = 0; i < ndraw; i++) {
        int nfloats = 0;
        int tid = draws[i].tex;
        if (tid < 0 || tid >= tex_n)
            continue;
        emit_quad(&nfloats, &draws[i]);
        glBindTexture(GL_TEXTURE_2D, gl_tex[tid]);
        glBufferData(GL_ARRAY_BUFFER,
                     (GLsizeiptr)(nfloats * (int)sizeof(GLfloat)),
                     vert_buf, GL_DYNAMIC_DRAW);
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, stride,
                              (const void *)0);
        glEnableVertexAttribArray(1);
        glVertexAttribPointer(1, 4, GL_FLOAT, GL_FALSE, stride,
                              (const void *)(2 * sizeof(GLfloat)));
        glEnableVertexAttribArray(2);
        glVertexAttribPointer(2, 2, GL_FLOAT, GL_FALSE, stride,
                              (const void *)(6 * sizeof(GLfloat)));
        glDrawArrays(GL_TRIANGLES, 0, nfloats / VERT_STRIDE);
    }
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

    engine_apply_argv(argc, argv);

    if (!init_egl())
        return 1;
    if (!init_fbo())
        return 1;
    prog = build_program();
    if (!prog)
        return 1;
    u_tex_loc = glGetUniformLocation(prog, "u_tex");
    if (!upload_textures()) {
        printf("texture upload failed\n");
        return 1;
    }

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
