/* A GLES2 triangle, rendered with no window system.
 *
 * Native:
 *     python3 crust examples/gles2/triangle.c -o build/gles2_triangle \
 *         -lEGL -lGLESv2
 *     EGL_PLATFORM=surfaceless ./build/gles2_triangle out.ppm
 *
 * WASM (soft GLES host, no browser):
 *     python3 crust --target wasm examples/gles2/triangle.c \
 *         -o build/gles2_triangle.wasm
 *     node tools/gles2_wasm_run.js build/gles2_triangle.wasm
 *
 * Everything here goes through Crust's own <EGL/egl.h> and <GLES2/gl2.h>;
 * no system GL header is read, because the compiler has no system include
 * path. On native the entry points come from libEGL and libGLESv2 at link
 * time; under --target wasm they become `env` imports filled by the host.
 *
 * Surfaceless means there is no default framebuffer to draw into -- the
 * context is current with EGL_NO_SURFACE, so the program must supply its own
 * render target. That is the framebuffer object below. It also means the
 * result never reaches a screen, which is the point: the output is pixels in
 * memory, so the whole path is checkable by a test rather than by looking at
 * it.
 *
 * If EGL_PLATFORM is not set, libglvnd uses its build-time default platform
 * (x11 on a typical distribution) and eglInitialize fails with
 * EGL_NOT_INITIALIZED when no display is reachable. The wasm host stubs EGL
 * and does not need the variable.
 */

#include <EGL/egl.h>
#include <GLES2/gl2.h>
/* C says <stdio.h> defines NULL; Crust's does not yet, so ask for it
 * directly rather than depend on a header that happens to drag it in. */
#include <stddef.h>
#ifdef __wasm__
/* stdio fprintf/fopen become wasm imports with a broken varargs ABI;
 * wasi.h gives a real printf and putchar inside the module. */
#include <wasi.h>
#else
#include <stdio.h>
#include <stdlib.h>
#endif

#define WIDTH  64
#define HEIGHT 32

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

/* Interleaved: two position floats then three colour floats per vertex. */
static const GLfloat VERTS[] = {
    -0.9f, -0.9f,   1.0f, 0.0f, 0.0f,
     0.9f, -0.9f,   0.0f, 1.0f, 0.0f,
     0.0f,  0.9f,   0.0f, 0.0f, 1.0f
};

static unsigned char pixels[WIDTH * HEIGHT * 4];

/* Compile one shader stage, reporting the driver's log rather than just a
 * failure: a GLSL error here is the most likely thing to go wrong, and the
 * log is the only thing that says what. */
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
        printf("%s shader failed to compile:\n%s\n", what, log);
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
    /* Bind before linking: GLES2 has no layout qualifiers, and leaving the
     * locations to the driver would make the attribute indices below a
     * guess about this particular implementation. */
    glBindAttribLocation(prog, 0, "a_pos");
    glBindAttribLocation(prog, 1, "a_color");
    glLinkProgram(prog);
    glGetProgramiv(prog, GL_LINK_STATUS, &ok);
    if (!ok) {
        char log[1024];
        GLsizei len = 0;
        glGetProgramInfoLog(prog, (GLsizei)sizeof(log), &len, log);
        printf("program failed to link:\n%s\n", log);
        return 0;
    }
    glDeleteShader(vs);
    glDeleteShader(fs);
    return prog;
}

/* A GLES2 context with no surface. Returns 0 on failure. */
static int init_egl(void)
{
    EGLint cfg_attribs[] = {
        EGL_SURFACE_TYPE,    EGL_PBUFFER_BIT,
        EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
        EGL_RED_SIZE,        8,
        EGL_GREEN_SIZE,      8,
        EGL_BLUE_SIZE,       8,
        EGL_ALPHA_SIZE,      8,
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
        printf("eglBindAPI failed (0x%X)\n", eglGetError());
        return 0;
    }
    if (!eglChooseConfig(dpy, cfg_attribs, &cfg, 1, &num_config)
        || num_config < 1) {
        printf("no matching EGLConfig\n");
        return 0;
    }
    ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctx_attribs);
    if (ctx == EGL_NO_CONTEXT) {
        printf("eglCreateContext failed (0x%X)\n", eglGetError());
        return 0;
    }
    if (!eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, ctx)) {
        printf("eglMakeCurrent failed (0x%X)\n", eglGetError());
        return 0;
    }
    printf("EGL %d.%d, GLES %s\n", (int)major, (int)minor,
           (const char *)glGetString(GL_VERSION));
    return 1;
}

/* Our own render target, since a surfaceless context has none. */
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

static void draw(GLuint prog)
{
    GLuint vbo;

    glGenBuffers(1, &vbo);
    glBindBuffer(GL_ARRAY_BUFFER, vbo);
    glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)sizeof(VERTS), VERTS,
                 GL_STATIC_DRAW);

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
    glDrawArrays(GL_TRIANGLES, 0, 3);
    glFinish();
}

/* GLES2 guarantees readback in exactly one format: RGBA/UNSIGNED_BYTE. */
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
    /* GL's origin is bottom-left, PPM's is top-left. */
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

    if (!init_egl())
        return 1;
    if (!init_fbo())
        return 1;
    prog = build_program();
    if (!prog)
        return 1;

    draw(prog);
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
