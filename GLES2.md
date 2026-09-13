# OpenGL ES 2.0

Crust compiles and runs real GLES2 against the system driver. A shader
pipeline -- vertex and fragment stages, a vertex buffer, a framebuffer object,
pixel readback -- goes through ShivyCX with no external compiler involved in
the C.

```sh
python3 crust examples/gles2/triangle.c -o build/gles2_triangle -lEGL -lGLESv2
EGL_PLATFORM=surfaceless ./build/gles2_triangle out.ppm
```

On Mesa 25.2 that prints a Gouraud-shaded triangle and writes a 64x32 PPM,
with no display, no X server and no `/dev/dri`.

## What was actually needed

Almost nothing in the compiler. The first thing tried was a hand-declared EGL
probe, and it compiled, linked and ran a GL 4.5 context on the first attempt.
ShivyCX was already capable of this; what was missing was the *headers*.

That is a consequence of the design rather than an oversight. Crust does not
read `/usr/include` -- every header a Crust program can see is one in
`shivyc/include`, which is why `#include <stdio.h>` works and
`-I /usr/include` does not (glibc's `stdio.h` uses `#if __GLIBC_USE (ISOC2X)`,
which the preprocessor rejects). So GLES2 support is two headers we own:

| file | what it is |
|---|---|
| `shivyc/include/GLES2/gl2.h` | the complete GLES2 API: 301 constants, all 142 entry points |
| `shivyc/include/EGL/egl.h` | EGL, scoped to what a surfaceless context needs |

Both differ from the Khronos originals in two deliberate ways. There is no
`<KHR/khrplatform.h>`: Khronos indirects every scalar through a `khronos_*_t`
typedef so one header can serve every word size, and Crust already knows its
target, so the types are spelled directly using the same LP64/LLP64 split as
`<stdint.h>`. And `GL_APICALL`/`GL_APIENTRY` expand to nothing, because they
exist to carry `dllimport` and `stdcall` on Win32 and the supported GL targets
are SysV-ABI ELF.

## No symbol loader

The usual bulk of this job -- a GLAD-style generated table of function
pointers filled in from `eglGetProcAddress` -- is not here, because on Linux
it buys nothing. libglvnd's `libGLESv2.so.2` exports all 358 GLES2 entry
points and `libGL.so.1` exports 3470, so plain `extern` declarations resolve
at link time like any other library. A loader becomes necessary the moment
this moves to a platform where that is not true, and that is the point at
which to write one, not before.

## Surfaceless, and why

`EGL_PLATFORM=surfaceless` gives a context with **no default framebuffer**. A
program must supply its own render target, which the example does with a
framebuffer object, and the result never reaches a screen.

That sounds like a limitation and is mostly the opposite. It means the output
is pixels in memory, so the whole path is checkable by a test rather than by
looking at it, and it runs in a container with no GPU. The environment
variable is required because libglvnd's build-time default platform is x11;
without it `eglInitialize` fails with `EGL_NOT_INITIALIZED` and the example
says so.

## Testing

```sh
python3 tools/gles2_header_test.py
python3 tools/gles2_header_test.py --mutate
```

Headers declaring somebody else's ABI are not self-checking. A constant with
the wrong value or a parameter of the wrong width compiles, links, and then
asks the driver to do something other than what the program said -- there is
no error to observe, because `glClear` with a wrong bit just clears the wrong
buffer. So three separate things are checked, because they fail in different
ways:

* **constants** -- every constant we share with the Khronos header is compared
  *by value*, by compiling both and printing the results, so a macro that
  expands differently than it reads is still caught;
* **prototypes** -- every entry point Khronos declares is declared by us with
  the same parameter types, and none is missing or invented;
* **render** -- the example is built twice, by gcc and by ShivyCX, both
  against our headers, and the two must produce byte-identical pixels.

Only the third runs a line of GL, and it is the only one that can catch a
disagreement between the compiler and the driver rather than between two
header files. It also refuses to pass on a blank image, since two blank
renders compare equal and prove nothing.

The suite is mutation-tested: `--mutate` injects a wrong constant value, a
constant wrongly defined in terms of another, a missing entry point, a
parameter of the wrong type, and a header reaching outside our include
directory, and requires all five to be caught. The first version of the
value mutation renamed the constant instead of changing it, so the value
comparison was never exercised and the run reported a miss -- which is the
argument for mutation testing in one line.

The constant and prototype checks need the Khronos headers (`libgles-dev`)
and the render check needs a software rasteriser; each skips with a stated
reason rather than passing quietly when its prerequisite is absent.

## Limits

State these rather than discover them later.

* **Hosted linking only.** Under `SHIVYC_RLINK=1` this does not work at all:

  ```
  rlink failed: undefined reference to: eglBindAPI, eglChooseConfig, ... glGetString
  ```

  `rlink` is static-only and resolves `-lGLESv2` by looking for
  `libGLESv2.a`, which does not exist on any Linux and never will, because
  libGLESv2 *is* the vendor driver. Making the self-hosted path work means
  teaching rlink dynamic linking -- `.interp`, `.dynamic` with `DT_NEEDED`,
  `.dynsym`, `.rela.plt`, and PLT/GOT synthesis. Well-bounded, and a
  different size of job from writing a header.

* **No window system.** `EGLNativeDisplayType` is an opaque pointer here.
  An X11 or GBM/DRM backend needs it to be the real type, and needs
  `egl.h` extended rather than reused. GBM/DRM is the interesting one,
  because it would meet the framebuffer work described in `tools/GPU.md`.

* **GLES2 only.** No GLES3, and `gl2ext.h` is not covered, so no extension
  entry points.

* **`shivyc/include` has no `dlfcn.h`**, so `dlopen`-based driver loading is
  not available as an alternative route.

## An unrelated defect found on the way

None of `shivyc/include/stdio.h`, `stdlib.h` or `string.h` defines `NULL`,
though C requires all three to; only `stddef.h` does. The example includes
`<stddef.h>` explicitly and says why, rather than papering over it. The fix
is a one-line guarded definition in each, but it touches headers every Crust
program uses, so it is called out here instead of being folded into this
change.
