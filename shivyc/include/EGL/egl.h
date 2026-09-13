#ifndef _EGL_EGL_H
#define _EGL_EGL_H

/* EGL 1.5 for Crust -- the display/context half of the GLES2 support.
 *
 * Scoped on purpose. Upstream <EGL/egl.h> pulls <EGL/eglplatform.h>, which
 * reaches for <X11/Xlib.h> (or Windows.h, or ANativeWindow) to give
 * EGLNativeDisplayType a real type. Crust's first GLES2 target is the
 * surfaceless platform: no window system, no native display, render into a
 * framebuffer object and read the pixels back. So the native handles are
 * declared as opaque pointers and no window-system header is involved.
 *
 * That is a real limitation, stated here rather than discovered later: an
 * X11 or GBM/DRM backend needs those types to be the real ones, and will
 * need this header extended rather than reused as-is.
 *
 * Constant values are checked against the system EGL header by
 * tools/gles2_header_test.py when it is installed.
 *
 * Link with -lEGL. Getting a context is four calls:
 *
 *     EGLDisplay d = eglGetDisplay(EGL_DEFAULT_DISPLAY);
 *     eglInitialize(d, &major, &minor);
 *     eglBindAPI(EGL_OPENGL_ES_API);
 *     eglChooseConfig(d, cfg_attribs, &cfg, 1, &n);
 *     eglMakeCurrent(d, EGL_NO_SURFACE, EGL_NO_SURFACE,
 *                    eglCreateContext(d, cfg, EGL_NO_CONTEXT, ctx_attribs));
 *
 * and the program must run with EGL_PLATFORM=surfaceless in the environment,
 * because libglvnd's build-time default platform is x11.
 */

#include <stdint.h>

#define EGLAPI
#define EGLAPIENTRY
#define EGLAPIENTRYP *

typedef unsigned int EGLBoolean;
typedef unsigned int EGLenum;
typedef int          EGLint;

/* Handles. Opaque to the client in every EGL version; EGL 1.5 made that
 * explicit by spelling them void *. */
typedef void *EGLConfig;
typedef void *EGLContext;
typedef void *EGLDisplay;
typedef void *EGLSurface;
typedef void *EGLClientBuffer;
typedef void *EGLImage;
typedef void *EGLSync;

/* Native handles. Real types under a window system; nothing under the
 * surfaceless platform, which is the only one this header supports. */
typedef void     *EGLNativeDisplayType;
typedef uintptr_t EGLNativePixmapType;
typedef uintptr_t EGLNativeWindowType;

typedef intptr_t  EGLAttrib;
typedef uint64_t  EGLTime;

typedef void (*__eglMustCastToProperFunctionPointerType)(void);

#ifdef __cplusplus
extern "C" {
#endif

/* ---- Values ---------------------------------------------------------- */

#define EGL_VERSION_1_0                 1
#define EGL_VERSION_1_1                 1
#define EGL_VERSION_1_2                 1
#define EGL_VERSION_1_3                 1
#define EGL_VERSION_1_4                 1
#define EGL_VERSION_1_5                 1

#define EGL_FALSE                       0
#define EGL_TRUE                        1
#define EGL_DONT_CARE                   ((EGLint)-1)
#define EGL_NONE                        0x3038

#define EGL_DEFAULT_DISPLAY             ((EGLNativeDisplayType)0)
#define EGL_NO_CONTEXT                  ((EGLContext)0)
#define EGL_NO_DISPLAY                  ((EGLDisplay)0)
#define EGL_NO_SURFACE                  ((EGLSurface)0)

/* Errors. eglGetError() returns EGL_SUCCESS and clears only on a call that
 * did not fail; a failed call leaves the previous code in place. */
#define EGL_SUCCESS                     0x3000
#define EGL_NOT_INITIALIZED             0x3001
#define EGL_BAD_ACCESS                  0x3002
#define EGL_BAD_ALLOC                   0x3003
#define EGL_BAD_ATTRIBUTE               0x3004
#define EGL_BAD_CONFIG                  0x3005
#define EGL_BAD_CONTEXT                 0x3006
#define EGL_BAD_CURRENT_SURFACE         0x3007
#define EGL_BAD_DISPLAY                 0x3008
#define EGL_BAD_MATCH                   0x3009
#define EGL_BAD_NATIVE_PIXMAP           0x300A
#define EGL_BAD_NATIVE_WINDOW           0x300B
#define EGL_BAD_PARAMETER               0x300C
#define EGL_BAD_SURFACE                 0x300D
#define EGL_CONTEXT_LOST                0x300E

/* Config attributes. */
#define EGL_BUFFER_SIZE                 0x3020
#define EGL_ALPHA_SIZE                  0x3021
#define EGL_BLUE_SIZE                   0x3022
#define EGL_GREEN_SIZE                  0x3023
#define EGL_RED_SIZE                    0x3024
#define EGL_DEPTH_SIZE                  0x3025
#define EGL_STENCIL_SIZE                0x3026
#define EGL_CONFIG_CAVEAT               0x3027
#define EGL_CONFIG_ID                   0x3028
#define EGL_LEVEL                       0x3029
#define EGL_MAX_PBUFFER_HEIGHT          0x302A
#define EGL_MAX_PBUFFER_PIXELS          0x302B
#define EGL_MAX_PBUFFER_WIDTH           0x302C
#define EGL_NATIVE_RENDERABLE           0x302D
#define EGL_NATIVE_VISUAL_ID            0x302E
#define EGL_NATIVE_VISUAL_TYPE          0x302F
#define EGL_SAMPLES                     0x3031
#define EGL_SAMPLE_BUFFERS              0x3032
#define EGL_SURFACE_TYPE                0x3033
#define EGL_TRANSPARENT_TYPE            0x3034
#define EGL_TRANSPARENT_BLUE_VALUE      0x3035
#define EGL_TRANSPARENT_GREEN_VALUE     0x3036
#define EGL_TRANSPARENT_RED_VALUE       0x3037
#define EGL_BIND_TO_TEXTURE_RGB         0x3039
#define EGL_BIND_TO_TEXTURE_RGBA        0x303A
#define EGL_MIN_SWAP_INTERVAL           0x303B
#define EGL_MAX_SWAP_INTERVAL           0x303C
#define EGL_LUMINANCE_SIZE              0x303D
#define EGL_ALPHA_MASK_SIZE             0x303E
#define EGL_COLOR_BUFFER_TYPE           0x303F
#define EGL_RENDERABLE_TYPE             0x3040
#define EGL_MATCH_NATIVE_PIXMAP         0x3041
#define EGL_CONFORMANT                  0x3042

/* EGL_SURFACE_TYPE bits. */
#define EGL_PBUFFER_BIT                 0x0001
#define EGL_PIXMAP_BIT                  0x0002
#define EGL_WINDOW_BIT                  0x0004
#define EGL_VG_COLORSPACE_LINEAR_BIT    0x0020
#define EGL_VG_ALPHA_FORMAT_PRE_BIT     0x0040
#define EGL_MULTISAMPLE_RESOLVE_BOX_BIT 0x0200
#define EGL_SWAP_BEHAVIOR_PRESERVED_BIT 0x0400

/* EGL_RENDERABLE_TYPE bits. EGL_OPENGL_ES2_BIT is the one that matters
 * here: without it eglChooseConfig can hand back a config that will only
 * carry a GLES1 context. */
#define EGL_OPENGL_ES_BIT               0x0001
#define EGL_OPENVG_BIT                  0x0002
#define EGL_OPENGL_ES2_BIT              0x0004
#define EGL_OPENGL_BIT                  0x0008
#define EGL_OPENGL_ES3_BIT              0x0040

/* Color buffer types. */
#define EGL_RGB_BUFFER                  0x308E
#define EGL_LUMINANCE_BUFFER            0x308F

/* Caveats and transparency. */
#define EGL_SLOW_CONFIG                 0x3050
#define EGL_NON_CONFORMANT_CONFIG       0x3051
#define EGL_TRANSPARENT_RGB             0x3052

/* eglQueryString() targets. */
#define EGL_VENDOR                      0x3053
#define EGL_VERSION                     0x3054
#define EGL_EXTENSIONS                  0x3055
#define EGL_CLIENT_APIS                 0x308D

/* Client APIs, for eglBindAPI/eglQueryAPI. */
#define EGL_OPENGL_ES_API               0x30A0
#define EGL_OPENVG_API                  0x30A1
#define EGL_OPENGL_API                  0x30A2

/* Surface attributes. */
#define EGL_HEIGHT                      0x3056
#define EGL_WIDTH                       0x3057
#define EGL_LARGEST_PBUFFER             0x3058
#define EGL_TEXTURE_FORMAT              0x3080
#define EGL_TEXTURE_TARGET              0x3081
#define EGL_MIPMAP_TEXTURE              0x3082
#define EGL_MIPMAP_LEVEL                0x3083
#define EGL_RENDER_BUFFER               0x3086
#define EGL_COLORSPACE                  0x3087
#define EGL_ALPHA_FORMAT                0x3088
#define EGL_HORIZONTAL_RESOLUTION       0x3090
#define EGL_VERTICAL_RESOLUTION         0x3091
#define EGL_PIXEL_ASPECT_RATIO          0x3092
#define EGL_SWAP_BEHAVIOR               0x3093
#define EGL_MULTISAMPLE_RESOLVE         0x3099

#define EGL_BACK_BUFFER                 0x3084
#define EGL_SINGLE_BUFFER               0x3085

/* Context attributes. EGL_CONTEXT_CLIENT_VERSION must be 2 for GLES2; its
 * EGL 1.5 spelling EGL_CONTEXT_MAJOR_VERSION is the same token. */
#define EGL_CONTEXT_CLIENT_VERSION      0x3098
#define EGL_CONTEXT_MAJOR_VERSION       0x3098
#define EGL_CONTEXT_MINOR_VERSION       0x30FB
#define EGL_CONTEXT_OPENGL_PROFILE_MASK 0x30FD
#define EGL_CONTEXT_OPENGL_DEBUG        0x31B0
#define EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT 0x00000001

/* eglGetPlatformDisplay platforms. SURFACELESS is an extension token
 * (EGL_MESA_platform_surfaceless) rather than core EGL, which is why the
 * EGL_PLATFORM=surfaceless environment variable is the portable way in. */
#define EGL_PLATFORM_SURFACELESS_MESA   0x31DD
#define EGL_PLATFORM_GBM_KHR            0x31D7
#define EGL_PLATFORM_X11_KHR            0x31D5
#define EGL_PLATFORM_DEVICE_EXT         0x313F

#define EGL_NO_SYNC                     ((EGLSync)0)
#define EGL_NO_IMAGE                    ((EGLImage)0)
#define EGL_FOREVER                     0xFFFFFFFFFFFFFFFFULL

/* ---- Entry points ---------------------------------------------------- */

EGLint     eglGetError(void);

EGLDisplay eglGetDisplay(EGLNativeDisplayType display_id);
EGLDisplay eglGetPlatformDisplay(EGLenum platform, void *native_display,
                                 const EGLAttrib *attrib_list);
EGLBoolean eglInitialize(EGLDisplay dpy, EGLint *major, EGLint *minor);
EGLBoolean eglTerminate(EGLDisplay dpy);
const char *eglQueryString(EGLDisplay dpy, EGLint name);

EGLBoolean eglGetConfigs(EGLDisplay dpy, EGLConfig *configs,
                         EGLint config_size, EGLint *num_config);
EGLBoolean eglChooseConfig(EGLDisplay dpy, const EGLint *attrib_list,
                           EGLConfig *configs, EGLint config_size,
                           EGLint *num_config);
EGLBoolean eglGetConfigAttrib(EGLDisplay dpy, EGLConfig config,
                              EGLint attribute, EGLint *value);

EGLSurface eglCreatePbufferSurface(EGLDisplay dpy, EGLConfig config,
                                   const EGLint *attrib_list);
EGLSurface eglCreateWindowSurface(EGLDisplay dpy, EGLConfig config,
                                  EGLNativeWindowType win,
                                  const EGLint *attrib_list);
EGLBoolean eglDestroySurface(EGLDisplay dpy, EGLSurface surface);
EGLBoolean eglQuerySurface(EGLDisplay dpy, EGLSurface surface,
                           EGLint attribute, EGLint *value);
EGLBoolean eglSurfaceAttrib(EGLDisplay dpy, EGLSurface surface,
                            EGLint attribute, EGLint value);

EGLBoolean eglBindAPI(EGLenum api);
EGLenum    eglQueryAPI(void);

EGLContext eglCreateContext(EGLDisplay dpy, EGLConfig config,
                            EGLContext share_context,
                            const EGLint *attrib_list);
EGLBoolean eglDestroyContext(EGLDisplay dpy, EGLContext ctx);
EGLBoolean eglMakeCurrent(EGLDisplay dpy, EGLSurface draw, EGLSurface read,
                          EGLContext ctx);
EGLContext eglGetCurrentContext(void);
EGLSurface eglGetCurrentSurface(EGLint readdraw);
EGLDisplay eglGetCurrentDisplay(void);
EGLBoolean eglQueryContext(EGLDisplay dpy, EGLContext ctx, EGLint attribute,
                           EGLint *value);

EGLBoolean eglWaitGL(void);
EGLBoolean eglWaitNative(EGLint engine);
EGLBoolean eglWaitClient(void);
EGLBoolean eglSwapBuffers(EGLDisplay dpy, EGLSurface surface);
EGLBoolean eglSwapInterval(EGLDisplay dpy, EGLint interval);
EGLBoolean eglReleaseThread(void);

__eglMustCastToProperFunctionPointerType eglGetProcAddress(const char *procname);

#ifdef __cplusplus
}
#endif

#endif /* _EGL_EGL_H */
