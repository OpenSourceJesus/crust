/* GLES 3.1 renderer for a packed Unity scene -- shared by gles3_view.c
 * (headless, EGL + FBO readback) and gles3_window.c (GLFW window).
 *
 * The default viewer is OpenGL ES 3.1 (desktop: GL 4.3+ drivers expose it
 * too) because ES 3.1 is what has shader storage buffers: a pack made with
 * `--gpu-handles` exports engine_upload_handles(), and this renderer
 * uploads it every frame into the SSBO at binding 1 that
 * shaders/handles.glsl declares. GLES2 hardware keeps gles2_view.c /
 * gles2_window.c.
 *
 * Sprites draw exactly as the GLES2 viewer draws them -- the same quads,
 * blending, texture sampling and target format -- and the tests hold the
 * two to identical pixels.
 */
#ifndef GLES3_RENDER_H
#define GLES3_RENDER_H

#include <GLES3/gl31.h>
#include <stdint.h>
#include <stdio.h>
#include "engine_draw.h"

extern float Camera_main_pos_x;
extern float Camera_main_pos_y;
extern float Camera_main_orthographicSize;
extern float Camera_main_aspect; /* 0 → framebuffer aspect */
extern float Camera_main_rect_x;
extern float Camera_main_rect_y;
extern float Camera_main_rect_w;
extern float Camera_main_rect_h;
extern float Camera_main_background_r;
extern float Camera_main_background_g;
extern float Camera_main_background_b;

/* A pack without `--gpu-handles` has no handles to upload: these weak
 * defaults say so, and the engine's own definitions win when it has. */
int engine_handle_words(void) __attribute__((weak));
int engine_handle_words(void) { return 0; }
int engine_upload_handles(uint32_t *dst, int max_words) __attribute__((weak));
int engine_upload_handles(uint32_t *dst, int max_words)
{
    (void)dst;
    (void)max_words;
    return 0;
}

#ifndef MAX_DRAWS
#define MAX_DRAWS 512
#endif
#ifndef MAX_TEX
#define MAX_TEX 512
#endif
#ifndef MAX_HANDLE_WORDS
#define MAX_HANDLE_WORDS 65536
#endif
#define VERT_STRIDE 8
#define MAX_FLOATS (6 * VERT_STRIDE)

static const char *G3_VERT_SRC =
    "#version 310 es\n"
    "layout(location = 0) in vec2 a_pos;\n"
    "layout(location = 1) in vec4 a_color;\n"
    "layout(location = 2) in vec2 a_uv;\n"
    "out vec4 v_color;\n"
    "out vec2 v_uv;\n"
    "void main() {\n"
    "    v_color = a_color;\n"
    "    v_uv = a_uv;\n"
    "    gl_Position = vec4(a_pos, 0.0, 1.0);\n"
    "}\n";

static const char *G3_FRAG_SRC =
    "#version 310 es\n"
    "precision mediump float;\n"
    "in vec4 v_color;\n"
    "in vec2 v_uv;\n"
    "uniform sampler2D u_tex;\n"
    "layout(location = 0) out vec4 frag;\n"
    "void main() {\n"
    "    vec4 t = texture(u_tex, v_uv);\n"
    "    frag = vec4(t.rgb * v_color.rgb, t.a * v_color.a);\n"
    "}\n";

static GLfloat g3_vert[MAX_FLOATS];
static GLuint g3_tex[MAX_TEX];
static int g3_tex_n;
static GLuint g3_prog;
static GLint g3_u_tex;
static GLuint g3_vao;
static GLuint g3_vbo;
static GLuint g3_ssbo;
static int g3_handle_words;
static uint32_t g3_handles[MAX_HANDLE_WORDS];
static float g3_left, g3_right, g3_bottom, g3_top;

static void g3_refresh_camera_bounds(float aspect)
{
    float half_h = Camera_main_orthographicSize;
    float half_w = half_h * aspect;
    if (half_w < 0.01f)
        half_w = 0.01f;
    g3_left = Camera_main_pos_x - half_w;
    g3_right = Camera_main_pos_x + half_w;
    g3_bottom = Camera_main_pos_y - half_h;
    g3_top = Camera_main_pos_y + half_h;
}

/* CameraScript.HandleViewSize: letterbox Camera.rect so authored
 * Camera_main_aspect fits the current pixel size. Only runs when aspect,
 * orthographicSize, or pixel size change (baked rect assumes Player
 * Settings screen aspect — a resize otherwise vertically stretches). */
static void g3_handle_view_size(int pixel_w, int pixel_h)
{
    static int cached_w = -1, cached_h = -1;
    static float cached_aspect = -1.f, cached_ortho = -1.f;
    float aspect = Camera_main_aspect;
    float ortho = Camera_main_orthographicSize;
    float screen_aspect, rw, rh;

    if (pixel_w < 1)
        pixel_w = 1;
    if (pixel_h < 1)
        pixel_h = 1;
    if (pixel_w == cached_w && pixel_h == cached_h
        && aspect == cached_aspect && ortho == cached_ortho)
        return;
    cached_w = pixel_w;
    cached_h = pixel_h;
    cached_aspect = aspect;
    cached_ortho = ortho;
    if (aspect < 1e-6f)
        return;
    screen_aspect = (float)pixel_w / (float)pixel_h;
    rw = aspect / screen_aspect;
    if (rw > 1.f)
        rw = 1.f;
    rh = screen_aspect / aspect;
    if (rh > 1.f)
        rh = 1.f;
    Camera_main_rect_w = rw;
    Camera_main_rect_h = rh;
    Camera_main_rect_x = 0.5f - rw * 0.5f;
    Camera_main_rect_y = 0.5f - rh * 0.5f;
}

static float g3_ndc_x(float x)
{
    return 2.0f * (x - g3_left) / (g3_right - g3_left) - 1.0f;
}

static float g3_ndc_y(float y)
{
    return 2.0f * (y - g3_bottom) / (g3_top - g3_bottom) - 1.0f;
}

static void g3_emit_vert(int *ni, float x, float y, float r, float g, float b,
                         float a, float u, float v)
{
    int i = *ni;
    if (i + VERT_STRIDE > MAX_FLOATS)
        return;
    g3_vert[i] = x;
    g3_vert[i + 1] = y;
    g3_vert[i + 2] = r;
    g3_vert[i + 3] = g;
    g3_vert[i + 4] = b;
    g3_vert[i + 5] = a;
    g3_vert[i + 6] = u;
    g3_vert[i + 7] = v;
    *ni = i + VERT_STRIDE;
}

/* One sprite as two triangles in NDC (full localRotation XY basis). */
static void g3_emit_quad(int *ni, const EngineDraw *d)
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
        nx[i] = g3_ndc_x(wx);
        ny[i] = g3_ndc_y(wy);
    }
    g3_emit_vert(ni, nx[0], ny[0], r, g, b, a, u[0], v[0]);
    g3_emit_vert(ni, nx[1], ny[1], r, g, b, a, u[1], v[1]);
    g3_emit_vert(ni, nx[2], ny[2], r, g, b, a, u[2], v[2]);

    g3_emit_vert(ni, nx[1], ny[1], r, g, b, a, u[1], v[1]);
    g3_emit_vert(ni, nx[3], ny[3], r, g, b, a, u[3], v[3]);
    g3_emit_vert(ni, nx[2], ny[2], r, g, b, a, u[2], v[2]);
}

static GLuint g3_compile_stage(GLenum type, const char *src, const char *what)
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

static int g3_upload_textures(void)
{
    int i;
    g3_tex_n = engine_texture_count();
    if (g3_tex_n > MAX_TEX)
        g3_tex_n = MAX_TEX;
    if (g3_tex_n < 1)
        return 1;
    glGenTextures(g3_tex_n, g3_tex);
    for (i = 0; i < g3_tex_n; i++) {
        int w = engine_texture_width(i);
        int h = engine_texture_height(i);
        const unsigned char *rgba = engine_texture_rgba(i);
        if (!rgba || w < 1 || h < 1)
            return 0;
        glBindTexture(GL_TEXTURE_2D, g3_tex[i]);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA,
                     GL_UNSIGNED_BYTE, rgba);
    }
    return 1;
}

/* Program, vertex array, textures and -- for a `--gpu-handles` pack --
 * the handle SSBO. Returns 0 on failure (the reason is printed). */
static int g3_init(void)
{
    GLuint vs, fs;
    GLint ok = 0;
    GLsizei stride = (GLsizei)(VERT_STRIDE * sizeof(GLfloat));

    vs = g3_compile_stage(GL_VERTEX_SHADER, G3_VERT_SRC, "vertex");
    fs = g3_compile_stage(GL_FRAGMENT_SHADER, G3_FRAG_SRC, "fragment");
    if (!vs || !fs)
        return 0;
    g3_prog = glCreateProgram();
    glAttachShader(g3_prog, vs);
    glAttachShader(g3_prog, fs);
    glLinkProgram(g3_prog);
    glGetProgramiv(g3_prog, GL_LINK_STATUS, &ok);
    if (!ok) {
        char log[1024];
        GLsizei len = 0;
        glGetProgramInfoLog(g3_prog, (GLsizei)sizeof(log), &len, log);
        printf("link failed:\n%s\n", log);
        return 0;
    }
    glDeleteShader(vs);
    glDeleteShader(fs);
    g3_u_tex = glGetUniformLocation(g3_prog, "u_tex");

    glGenVertexArrays(1, &g3_vao);
    glBindVertexArray(g3_vao);
    glGenBuffers(1, &g3_vbo);
    glBindBuffer(GL_ARRAY_BUFFER, g3_vbo);
    glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)sizeof(g3_vert), NULL,
                 GL_DYNAMIC_DRAW);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, stride, (const void *)0);
    glEnableVertexAttribArray(1);
    glVertexAttribPointer(1, 4, GL_FLOAT, GL_FALSE, stride,
                          (const void *)(2 * sizeof(GLfloat)));
    glEnableVertexAttribArray(2);
    glVertexAttribPointer(2, 2, GL_FLOAT, GL_FALSE, stride,
                          (const void *)(6 * sizeof(GLfloat)));

    if (!g3_upload_textures()) {
        printf("texture upload failed\n");
        return 0;
    }

    g3_handle_words = engine_handle_words();
    if (g3_handle_words > MAX_HANDLE_WORDS) {
        printf("handles: %d words, more than MAX_HANDLE_WORDS (%d)\n",
               g3_handle_words, MAX_HANDLE_WORDS);
        return 0;
    }
    if (g3_handle_words > 0) {
        glGenBuffers(1, &g3_ssbo);
        glBindBuffer(GL_SHADER_STORAGE_BUFFER, g3_ssbo);
        glBufferData(GL_SHADER_STORAGE_BUFFER,
                     (GLsizeiptr)(g3_handle_words * (int)sizeof(uint32_t)),
                     NULL, GL_DYNAMIC_DRAW);
        glBindBufferBase(GL_SHADER_STORAGE_BUFFER, 1, g3_ssbo);
        printf("handles: %d words at SSBO binding 1\n", g3_handle_words);
    }
    return 1;
}

/* This frame's handles into the SSBO (the engine moves them every tick). */
static void g3_upload_handles(void)
{
    int n;
    if (g3_handle_words < 1)
        return;
    n = engine_upload_handles(g3_handles, g3_handle_words);
    glBindBuffer(GL_SHADER_STORAGE_BUFFER, g3_ssbo);
    glBufferSubData(GL_SHADER_STORAGE_BUFFER, 0,
                    (GLsizeiptr)(n * (int)sizeof(uint32_t)), g3_handles);
}

/* Clear to the camera background and draw every sprite. Returns the draw
 * count (0: nothing to draw).
 *
 * CameraScript.HandleViewSize seeds Camera_main_aspect / Camera_main_rect_*;
 * letterbox like gles2_window so Screen Space Camera UI matches Unity.
 */
static int g3_draw(int width, int height)
{
    EngineDraw draws[MAX_DRAWS];
    int ndraw;
    int i;
    int vx, vy, vw, vh;
    float aspect;

    ndraw = engine_collect_draws(draws, MAX_DRAWS);
    aspect = Camera_main_aspect;
    if (aspect < 1e-6f)
        aspect = height > 0 ? (float)width / (float)height : 1.0f;
    g3_refresh_camera_bounds(aspect);
    g3_upload_handles();
    /* Recompute letterbox for this framebuffer (HandleViewSize). */
    g3_handle_view_size(width, height);
    /* Camera.rect letterbox: black bars outside the authored pixel rect. */
    vx = (int)(Camera_main_rect_x * (float)width + 0.5f);
    vy = (int)(Camera_main_rect_y * (float)height + 0.5f);
    vw = (int)(Camera_main_rect_w * (float)width + 0.5f);
    vh = (int)(Camera_main_rect_h * (float)height + 0.5f);
    if (vw < 1)
        vw = 1;
    if (vh < 1)
        vh = 1;
    glViewport(0, 0, width, height);
    glClearColor(0.f, 0.f, 0.f, 1.f);
    glClear(GL_COLOR_BUFFER_BIT);
    glViewport(vx, vy, vw, vh);
    glClearColor(Camera_main_background_r, Camera_main_background_g,
                 Camera_main_background_b, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glUseProgram(g3_prog);
    glBindVertexArray(g3_vao);
    glBindBuffer(GL_ARRAY_BUFFER, g3_vbo);
    glActiveTexture(GL_TEXTURE0);
    glUniform1i(g3_u_tex, 0);
    for (i = 0; i < ndraw; i++) {
        int nfloats = 0;
        int tid = draws[i].tex;
        if (tid < 0 || tid >= g3_tex_n)
            continue;
        g3_emit_quad(&nfloats, &draws[i]);
        glBindTexture(GL_TEXTURE_2D, g3_tex[tid]);
        glBufferSubData(GL_ARRAY_BUFFER, 0,
                        (GLsizeiptr)(nfloats * (int)sizeof(GLfloat)), g3_vert);
        glDrawArrays(GL_TRIANGLES, 0, nfloats / VERT_STRIDE);
    }
    return ndraw < 0 ? 0 : ndraw;
}

#endif
