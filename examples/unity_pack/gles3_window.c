/* Live GLFW window for a packed Unity scene (engine.c + data.c).
 *
 * Uses OpenGL ES 3.1 via GLFW (GL 4.3+ desktop drivers provide it) -- a
 * real on-screen window; gles3_view.c is its headless twin, and both draw
 * through gles3_render.h. ES 3.1 is what has shader storage buffers: a
 * `--gpu-handles` pack's handles are uploaded every frame to SSBO binding
 * 1 (shaders/handles.glsl). GLES2-only hardware: gles2_window.c
 * (UNITY_PACK_GLES2=1).
 *
 *     examples/unity_pack/run_gles3_window.sh
 *     PROJECT=.../SystemsScene ./examples/unity_pack/run_gles3_window.sh
 *
 * Arrow keys / WASD feed engine_input_axis_* (Input.GetAxis). Left click
 * feeds engine_pointer_* for authored uGUI Buttons. Quit only via
 * Application.Quit (engine_wants_quit) or the window close control — no
 * Escape/Q host shortcut. Time.deltaTime comes from the frame clock.
 * SpriteRenderer quads sample packed PNG textures (tint × texel). Window
 * size is Screen_width × Screen_height from Player Settings
 * (defaultScreenWidth / defaultScreenHeight). fullscreenMode 0/1 opens a
 * real monitor fullscreen window (FullScreenWindow / Exclusive); with
 * defaultIsNativeResolution the desktop video mode size is used.
 */

#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "gles3_render.h"

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
float Camera_main_aspect __attribute__((weak)) = 0.f; /* 0 → framebuffer */
float Camera_main_rect_x __attribute__((weak)) = 0.f;
float Camera_main_rect_y __attribute__((weak)) = 0.f;
float Camera_main_rect_w __attribute__((weak)) = 1.f;
float Camera_main_rect_h __attribute__((weak)) = 1.f;

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

/* Unity scenes can emit far more than a handful of SpriteRenderers / uGUI
 * Images (Slime Jump Main Menu alone is ~160). Truncating here drops later
 * draws — e.g. Main Menu background never appears while early TMP labels do.
 */
/* xy + rgba + uv */

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

static void frame(GLFWwindow *win)
{
    int fbw, fbh, ww, wh;

    glfwGetWindowSize(win, &ww, &wh);
    glfwGetFramebufferSize(win, &fbw, &fbh);
    /* Unity Screen tracks the game view; keep pointer / uGUI in sync. */
    if (ww > 0 && wh > 0) {
        Screen_width = ww;
        Screen_height = wh;
    }
    /* Letterbox before tick so uGUI hits use the same Camera.rect as draw. */
    if (fbw > 0 && fbh > 0)
        g3_handle_view_size(fbw, fbh);
    else if (ww > 0 && wh > 0)
        g3_handle_view_size(ww, wh);
    poll_input_axes(win);
    engine_tick();
    if (fbw > 0 && fbh > 0)
        g3_draw(fbw, fbh);
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
    glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 3);
    glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 1);
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
        fprintf(stderr, "glfwCreateWindow failed (need a display + "
                "OpenGL ES 3.1; GLES2-only: UNITY_PACK_GLES2=1)\n");
        glfwTerminate();
        return 1;
    }
    glfwMakeContextCurrent(win);
    glfwSwapInterval(1);

    printf("GLES %s\n", (const char *)glGetString(GL_VERSION));
    printf("draws classes=%d textures=%d — arrows (Keyboard); "
           "quit via Application.Quit or window close\n",
           engine_class_count(), engine_texture_count());

    if (!g3_init()) {
        glfwDestroyWindow(win);
        glfwTerminate();
        return 1;
    }

    prev = glfwGetTime();
    while (!glfwWindowShouldClose(win) && !engine_wants_quit()) {
        now = glfwGetTime();
        Time_deltaTime = (float)(now - prev);
        if (Time_deltaTime > 0.05f)
            Time_deltaTime = 0.05f;
        if (Time_deltaTime < 0.0f)
            Time_deltaTime = 0.0f;
        prev = now;
        frame(win);
    }

    if (g3_tex_n > 0)
        glDeleteTextures(g3_tex_n, g3_tex);
    glDeleteBuffers(1, &g3_vbo);
    glDeleteVertexArrays(1, &g3_vao);
    glDeleteProgram(g3_prog);
    glfwDestroyWindow(win);
    glfwTerminate();
    return 0;
}
