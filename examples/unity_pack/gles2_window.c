/* Live GLFW window for a packed Unity scene (engine.c + data.c).
 *
 * Uses OpenGL ES 2.0 via GLFW — a real on-screen window, not the
 * surfaceless FBO path in gles2_view.c.
 *
 *     examples/unity_pack/run_gles2_window.sh
 *
 * Close the window (or press Escape) to quit. Time.deltaTime is updated
 * from the frame clock so the player keeps moving.
 */

#define GLFW_INCLUDE_ES2
#include <GLFW/glfw3.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "engine_draw.h"

extern float Time_deltaTime;

#define WIN_W 800
#define WIN_H 600
#define MAX_DRAWS 64
#define MAX_FLOATS (MAX_DRAWS * 6 * 5)

#define WORLD_LEFT   (-3.0f)
#define WORLD_RIGHT  ( 3.0f)
#define WORLD_BOTTOM (-2.0f)
#define WORLD_TOP    ( 3.0f)

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

static GLfloat vert_buf[MAX_FLOATS];
static GLuint prog;
static GLuint vbo;
static int want_close;

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

static void on_key(GLFWwindow *win, int key, int scancode, int action, int mods)
{
    (void)scancode;
    (void)mods;
    if (action == GLFW_PRESS && (key == GLFW_KEY_ESCAPE || key == GLFW_KEY_Q)) {
        want_close = 1;
        glfwSetWindowShouldClose(win, GLFW_TRUE);
    }
}

static void frame(GLFWwindow *win)
{
    EngineDraw draws[MAX_DRAWS];
    int ndraw, nfloats = 0, i, nverts;
    int fbw, fbh;

    engine_tick();
    ndraw = engine_collect_draws(draws, MAX_DRAWS);
    for (i = 0; i < ndraw; i++)
        emit_quad(&nfloats, &draws[i]);
    nverts = nfloats / 5;

    glfwGetFramebufferSize(win, &fbw, &fbh);
    glViewport(0, 0, fbw, fbh);
    glClearColor(0.05f, 0.05f, 0.08f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);

    glBindBuffer(GL_ARRAY_BUFFER, vbo);
    glBufferData(GL_ARRAY_BUFFER,
                 (GLsizeiptr)(nfloats * (int)sizeof(GLfloat)),
                 vert_buf, GL_DYNAMIC_DRAW);

    glUseProgram(prog);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE,
                          (GLsizei)(5 * sizeof(GLfloat)), (const void *)0);
    glEnableVertexAttribArray(1);
    glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE,
                          (GLsizei)(5 * sizeof(GLfloat)),
                          (const void *)(2 * sizeof(GLfloat)));
    if (nverts > 0)
        glDrawArrays(GL_TRIANGLES, 0, nverts);

    glfwSwapBuffers(win);
    glfwPollEvents();
}

int main(void)
{
    GLFWwindow *win;
    double prev, now;

    if (!glfwInit()) {
        fprintf(stderr, "glfwInit failed\n");
        return 1;
    }
    glfwWindowHint(GLFW_CLIENT_API, GLFW_OPENGL_ES_API);
    glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 2);
    glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 0);
    win = glfwCreateWindow(WIN_W, WIN_H, "Crust unity_pack / MiniScene",
                           NULL, NULL);
    if (!win) {
        fprintf(stderr, "glfwCreateWindow failed (need a display + GLES)\n");
        glfwTerminate();
        return 1;
    }
    glfwMakeContextCurrent(win);
    glfwSwapInterval(1);
    glfwSetKeyCallback(win, on_key);

    printf("GLES %s\n", (const char *)glGetString(GL_VERSION));
    printf("draws classes=%d — Escape/Q to quit\n", engine_class_count());

    prog = build_program();
    if (!prog) {
        glfwDestroyWindow(win);
        glfwTerminate();
        return 1;
    }
    glGenBuffers(1, &vbo);

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

    glDeleteBuffers(1, &vbo);
    glDeleteProgram(prog);
    glfwDestroyWindow(win);
    glfwTerminate();
    return 0;
}
