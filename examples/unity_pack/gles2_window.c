/* Live GLFW window for a packed Unity scene (engine.c + data.c).
 *
 * Uses OpenGL ES 2.0 via GLFW — a real on-screen window, not the
 * surfaceless FBO path in gles2_view.c.
 *
 *     examples/unity_pack/run_gles2_window.sh
 *     SCENE=.../SystemsScene ./examples/unity_pack/run_gles2_window.sh
 *
 * Arrow keys / WASD feed engine_input_axis_* (Input.GetAxis). Left click
 * feeds engine_pointer_* for authored uGUI Buttons. Escape or Q quits.
 * Time.deltaTime comes from the frame clock. SpriteRenderer quads
 * sample packed PNG textures (tint × texel). Window size is
 * Screen_width × Screen_height from Player Settings
 * (defaultScreenWidth / defaultScreenHeight). fullscreenMode 0/1 opens a
 * real monitor fullscreen window (FullScreenWindow / Exclusive); with
 * defaultIsNativeResolution the desktop video mode size is used.
 */

#define GLFW_INCLUDE_ES2
#include <GLFW/glfw3.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "engine_draw.h"

extern float Time_deltaTime;

/* Camera globals — strong defs from data.c when a Camera was authored. */
float Camera_main_pos_x __attribute__((weak)) = 0.f;
float Camera_main_pos_y __attribute__((weak)) = 0.f;
float Camera_main_pos_z __attribute__((weak)) = -10.f;
float Camera_main_orthographicSize __attribute__((weak)) = 3.f;
float Camera_main_nearClipPlane __attribute__((weak)) = 0.3f;
float Camera_main_farClipPlane __attribute__((weak)) = 1000.f;
float Camera_main_background_r __attribute__((weak)) = 0.05f;
float Camera_main_background_g __attribute__((weak)) = 0.05f;
float Camera_main_background_b __attribute__((weak)) = 0.08f;

/* Weak so MiniScene (no Input) still links; SystemsScene data.c wins. */
float engine_input_axis_Horizontal __attribute__((weak)) = 0.f;
float engine_input_axis_Vertical __attribute__((weak)) = 0.f;
int engine_keyboard_connected __attribute__((weak)) = 0;
int engine_keyboard_leftArrow __attribute__((weak)) = 0;
int engine_keyboard_rightArrow __attribute__((weak)) = 0;
int engine_keyboard_upArrow __attribute__((weak)) = 0;
int engine_keyboard_downArrow __attribute__((weak)) = 0;
float engine_pointer_x __attribute__((weak)) = 0.f;
float engine_pointer_y __attribute__((weak)) = 0.f;
int engine_pointer_down __attribute__((weak)) = 0;

/* Player Settings defaultScreenWidth/Height (data.c defines). */
extern int Screen_width;
extern int Screen_height;
extern int Screen_fullScreen;
extern int Screen_fullScreenNative;
extern int Screen_maximized;
extern const char engine_product_name[];

#define MAX_DRAWS 64
#define MAX_TEX 32
/* xy + rgba + uv */
#define VERT_STRIDE 8
#define MAX_FLOATS (6 * VERT_STRIDE)

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

static GLfloat vert_buf[MAX_FLOATS];
static GLuint prog;
static GLuint vbo;
static GLuint gl_tex[MAX_TEX];
static int tex_n;
static GLint u_tex_loc;
static int want_close;

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

static void emit_quad(int *ni, const EngineDraw *d)
{
    float hw = d->half_w, hh = d->half_h;
    float c = d->cos_z, s = d->sin_z;
    float r = d->r, g = d->g, b = d->b, a = d->a;
    float lx[4] = {-hw, hw, -hw, hw};
    float ly[4] = {-hh, -hh, hh, hh};
    float u[4] = {0.f, 1.f, 0.f, 1.f};
    float v[4] = {0.f, 0.f, 1.f, 1.f};
    float nx[4], ny[4];
    int i;

    for (i = 0; i < 4; i = i + 1) {
        float wx = d->x + c * lx[i] - s * ly[i];
        float wy = d->y + s * lx[i] + c * ly[i];
        nx[i] = world_to_ndc_x(wx);
        ny[i] = world_to_ndc_y(wy);
    }
    /* tris: 0-1-2 and 1-3-2 (same winding as axis-aligned path) */
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
        fprintf(stderr, "%s shader failed:\n%s\n", what, log);
        return 0;
    }
    return sh;
}

static GLuint build_program(void)
{
    GLuint vs = compile_stage(GL_VERTEX_SHADER, VERT_SRC, "vertex");
    GLuint fs = compile_stage(GL_FRAGMENT_SHADER, FRAG_SRC, "fragment");
    GLuint p;
    GLint ok = 0;
    if (!vs || !fs)
        return 0;
    p = glCreateProgram();
    glAttachShader(p, vs);
    glAttachShader(p, fs);
    glBindAttribLocation(p, 0, "a_pos");
    glBindAttribLocation(p, 1, "a_color");
    glBindAttribLocation(p, 2, "a_uv");
    glLinkProgram(p);
    glGetProgramiv(p, GL_LINK_STATUS, &ok);
    glDeleteShader(vs);
    glDeleteShader(fs);
    if (!ok) {
        fprintf(stderr, "program link failed\n");
        return 0;
    }
    return p;
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

static void on_key(GLFWwindow *win, int key, int scancode, int action, int mods)
{
    (void)scancode;
    (void)mods;
    if (action == GLFW_PRESS && (key == GLFW_KEY_ESCAPE || key == GLFW_KEY_Q)) {
        want_close = 1;
        glfwSetWindowShouldClose(win, GLFW_TRUE);
    }
}

static void poll_input_axes(GLFWwindow *win)
{
    float hx = 0.f, vy = 0.f;
    if (glfwGetKey(win, GLFW_KEY_LEFT) == GLFW_PRESS
        || glfwGetKey(win, GLFW_KEY_A) == GLFW_PRESS)
        hx -= 1.f;
    if (glfwGetKey(win, GLFW_KEY_RIGHT) == GLFW_PRESS
        || glfwGetKey(win, GLFW_KEY_D) == GLFW_PRESS)
        hx += 1.f;
    if (glfwGetKey(win, GLFW_KEY_DOWN) == GLFW_PRESS
        || glfwGetKey(win, GLFW_KEY_S) == GLFW_PRESS)
        vy -= 1.f;
    if (glfwGetKey(win, GLFW_KEY_UP) == GLFW_PRESS
        || glfwGetKey(win, GLFW_KEY_W) == GLFW_PRESS)
        vy += 1.f;
    engine_input_axis_Horizontal = hx;
    engine_input_axis_Vertical = vy;

    /* Input System Keyboard.current — only the named key, no WASD aliases. */
    engine_keyboard_connected = 1;
    engine_keyboard_leftArrow =
        glfwGetKey(win, GLFW_KEY_LEFT) == GLFW_PRESS;
    engine_keyboard_rightArrow =
        glfwGetKey(win, GLFW_KEY_RIGHT) == GLFW_PRESS;
    engine_keyboard_upArrow =
        glfwGetKey(win, GLFW_KEY_UP) == GLFW_PRESS;
    engine_keyboard_downArrow =
        glfwGetKey(win, GLFW_KEY_DOWN) == GLFW_PRESS;

    /* uGUI Button — screen space, origin bottom-left (Unity).
     * glfwGetCursorPos is in window coordinates; map via window size, not
     * framebuffer (HiDPI would stretch hits and miss ColorBlock hover). */
    {
        double mx = 0.0, my = 0.0;
        int ww = 1, wh = 1;
        glfwGetCursorPos(win, &mx, &my);
        glfwGetWindowSize(win, &ww, &wh);
        if (ww < 1) ww = 1;
        if (wh < 1) wh = 1;
        engine_pointer_x = (float)(mx * (double)Screen_width / (double)ww);
        engine_pointer_y = (float)Screen_height
            - (float)(my * (double)Screen_height / (double)wh);
        engine_pointer_down =
            glfwGetMouseButton(win, GLFW_MOUSE_BUTTON_LEFT) == GLFW_PRESS;
    }
}

static void draw_one(const EngineDraw *d)
{
    int nfloats = 0;
    int tid;
    GLsizei stride = (GLsizei)(VERT_STRIDE * sizeof(GLfloat));

    emit_quad(&nfloats, d);
    tid = d->tex;
    if (tid < 0 || tid >= tex_n)
        return;
    glBindTexture(GL_TEXTURE_2D, gl_tex[tid]);
    glBindBuffer(GL_ARRAY_BUFFER, vbo);
    glBufferData(GL_ARRAY_BUFFER,
                 (GLsizeiptr)(nfloats * (int)sizeof(GLfloat)),
                 vert_buf, GL_DYNAMIC_DRAW);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, stride, (const void *)0);
    glEnableVertexAttribArray(1);
    glVertexAttribPointer(1, 4, GL_FLOAT, GL_FALSE, stride,
                          (const void *)(2 * sizeof(GLfloat)));
    glEnableVertexAttribArray(2);
    glVertexAttribPointer(2, 2, GL_FLOAT, GL_FALSE, stride,
                          (const void *)(6 * sizeof(GLfloat)));
    glDrawArrays(GL_TRIANGLES, 0, nfloats / VERT_STRIDE);
}

static void frame(GLFWwindow *win)
{
    EngineDraw draws[MAX_DRAWS];
    int ndraw, i;
    int fbw, fbh;

    poll_input_axes(win);
    engine_tick();
    glfwGetFramebufferSize(win, &fbw, &fbh);
    refresh_camera_bounds(fbh > 0 ? (float)fbw / (float)fbh : 1.0f);
    ndraw = engine_collect_draws(draws, MAX_DRAWS);

    glViewport(0, 0, fbw, fbh);
    glClearColor(Camera_main_background_r, Camera_main_background_g,
                 Camera_main_background_b, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);

    glUseProgram(prog);
    glActiveTexture(GL_TEXTURE0);
    glUniform1i(u_tex_loc, 0);
    for (i = 0; i < ndraw; i++)
        draw_one(&draws[i]);

    glfwSwapBuffers(win);
    glfwPollEvents();
}

int main(int argc, char **argv)
{
    GLFWwindow *win;
    GLFWmonitor *monitor = NULL;
    int win_w, win_h;
    double prev, now;

    engine_apply_argv(argc, argv);

    if (!glfwInit()) {
        fprintf(stderr, "glfwInit failed\n");
        return 1;
    }
    glfwWindowHint(GLFW_CLIENT_API, GLFW_OPENGL_ES_API);
    glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 2);
    glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 0);
    win_w = Screen_width;
    win_h = Screen_height;
    if (Screen_fullScreen) {
        /* Unity FullScreenWindow / ExclusiveFullScreen → monitor window. */
        monitor = glfwGetPrimaryMonitor();
        if (monitor && Screen_fullScreenNative) {
            const GLFWvidmode *mode = glfwGetVideoMode(monitor);
            if (mode) {
                /* Match desktop bits so borderless FS does not mode-switch. */
                glfwWindowHint(GLFW_RED_BITS, mode->redBits);
                glfwWindowHint(GLFW_GREEN_BITS, mode->greenBits);
                glfwWindowHint(GLFW_BLUE_BITS, mode->blueBits);
                glfwWindowHint(GLFW_REFRESH_RATE, mode->refreshRate);
                win_w = mode->width;
                win_h = mode->height;
                Screen_width = win_w;
                Screen_height = win_h;
            }
        }
    } else if (Screen_maximized) {
        glfwWindowHint(GLFW_MAXIMIZED, GLFW_TRUE);
    }
    win = glfwCreateWindow(win_w, win_h, engine_product_name, monitor, NULL);
    if (!win) {
        fprintf(stderr, "glfwCreateWindow failed (need a display + GLES)\n");
        glfwTerminate();
        return 1;
    }
    glfwMakeContextCurrent(win);
    glfwSwapInterval(1);
    glfwSetKeyCallback(win, on_key);

    printf("GLES %s\n", (const char *)glGetString(GL_VERSION));
    printf("draws classes=%d textures=%d — arrows (Keyboard), Escape/Q\n",
           engine_class_count(), engine_texture_count());

    prog = build_program();
    if (!prog) {
        glfwDestroyWindow(win);
        glfwTerminate();
        return 1;
    }
    u_tex_loc = glGetUniformLocation(prog, "u_tex");
    glGenBuffers(1, &vbo);
    if (!upload_textures()) {
        fprintf(stderr, "texture upload failed\n");
        return 1;
    }

    prev = glfwGetTime();
    while (!glfwWindowShouldClose(win) && !want_close) {
        now = glfwGetTime();
        Time_deltaTime = (float)(now - prev);
        if (Time_deltaTime > 0.05f)
            Time_deltaTime = 0.05f;
        if (Time_deltaTime < 0.0f)
            Time_deltaTime = 0.0f;
        prev = now;
        frame(win);
    }

    if (tex_n > 0)
        glDeleteTextures(tex_n, gl_tex);
    glDeleteBuffers(1, &vbo);
    glDeleteProgram(prog);
    glfwDestroyWindow(win);
    glfwTerminate();
    return 0;
}
