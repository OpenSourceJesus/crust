# GPU support

Where the `gpu:` scheme goes next, and what parts of
[drm-kmod](https://github.com/freebsd/drm-kmod) are worth taking.

## What we have already

mbos drives real graphics today via [vbe.c](examples/rpython2c/mbos/vbe.c): it
finds QEMU's std VGA on PCI (1234:1111), enables the memory BAR, programs a
32-bpp linear framebuffer through the Bochs DISPI registers, and hands the
console a pointer. Since the bare-metal work, that whole path — including the
`outw`/`inl` port I/O — compiles with ShivyCX, assembles with rasm and links
with rlink. `build/mbos_hires_own.elf` is a 1920x1080 kernel built with no
external toolchain.

CrustOS registers a `gpu:` scheme, but it is a stub.

So the gap is not "can we touch the hardware" — it is that we hardcode one
mode, one format, one allocation strategy, and one device.

## What drm-kmod actually offers us

The tree is 550 MB. `amdgpu` and `i915` are not on the table: they assume a
Linux kernel underneath (`linuxkpi` exists precisely to fake one), and they are
millions of lines of hardware-specific code that would need firmware blobs and
a working IOMMU, interrupt and DMA story we do not have.

What *is* interesting is that DRM's generic layer contains several files that
are essentially self-contained algorithms with only cosmetic ties to Linux:

| file | lines | what it gives us |
|---|---|---|
| `drm_rect.c` | 374 | clipping, scaling, rotation of rectangles |
| `drm_fourcc.c` | 458 | pixel-format descriptions (bpp, planes, chroma) |
| `drm_displayid.c` | 176 | DisplayID block parsing |
| `drm_dumb_buffers.c` | 151 | the "just give me a framebuffer" allocation path |
| `drm_blend.c` | 617 | alpha/blend-mode plumbing |
| `drm_color_mgmt.c` | 632 | gamma/CTM/degamma helpers |
| `drm_mm.c` | 1020 | a range allocator — VRAM management |
| `drm_buddy.c` | 1194 | buddy allocator for VRAM |
| `drm_modes.c` | 2776 | mode timing math: CVT, GTF, named modes |
| `drm_edid.c` | 7504 | EDID parsing — *what the monitor can actually do* |

`dummygfx/` (321 lines) is a minimal DRM driver skeleton and is a better
structural template for a `gpu:` scheme than any real driver.

## Measured feasibility, not a guess

`drm_rect.c` was compiled freestanding to find out how much Linux it really
needs. The answer: **a 67-line shim** (`tools/drm_shim/linux/`) covering

* four stub headers (`compiler.h`, `printk.h`, `device.h`, `dynamic_debug.h`),
* the scalar typedefs (`u8`..`s64`, `size_t`, `ssize_t`, `bool`, `va_list`),
* `min`/`max`/`clamp`/`swap`/`ARRAY_SIZE`,
* the attribute macros (`__printf`, `__iomem`, `__must_check`, …) as no-ops,
* `EXPORT_SYMBOL` as a no-op.

With that:

```sh
gcc -c -ffreestanding -nostdlib -I tools/drm_shim -I drm-kmod/include \
    drm-kmod/drivers/gpu/drm/drm_rect.c
# -> drm_rect_calc_hscale drm_rect_calc_vscale drm_rect_clip_scaled
#    drm_rect_debug_print drm_rect_intersect drm_rect_rotate drm_rect_rotate_inv
```

ShivyCX gets through the same file's own code and stops in
`include/uapi/drm/drm.h`, which pulls FreeBSD's `sys/ioccom.h` for the *ioctl*
definitions. That is the userspace ABI — a kernel that has no ioctl interface
does not need it, so the fix is another stub, not compiler work. **The blocker
is header shimming, not compiler capability.**

## The proposal

Take the algorithms, leave the framework.

1. **`tools/drmdeep.py`** — a survey tool in the shape of `tools/crustos.py`:
   walk `drivers/gpu/drm/*.c`, try each under the shim, and report what
   compiles, what links, and which missing symbol stops the rest. Same
   methodology that gave the honest "94 of 108 files" number for Redox, rather
   than a headline percentage.
2. **Adopt in dependency order**, each step standing alone:
   * `drm_fourcc` + `drm_rect` — formats and clipping. Immediately useful: the
     console's blitter currently assumes one format and no clipping.
   * `drm_mm` or `drm_buddy` — VRAM allocation, so the `gpu:` scheme can hand
     out buffers instead of owning one framebuffer.
   * `drm_edid` + `drm_modes` — **the real win.** Today mbos is told
     `-DMBOS_GFX_W=1920`. With EDID parsing it can read the monitor's
     capabilities over DDC/I2C and compute correct CVT timings, which is the
     difference between "we hardcoded a mode QEMU happens to accept" and
     "we do modesetting".
3. **A `gpu:` scheme over that**, modelled on `dummygfx`: `gpu:/modes` lists
   what EDID reported, `gpu:/fb0` is a buffer from the allocator, writes blit
   through `drm_rect` clipping. Schemes are URL-addressed, so this fits the
   CrustOS shape without a DRM ioctl layer.

## What this deliberately is not

Not a path to accelerated 3D, and not a path to real hardware. There is no
command submission, no GEM/TTM object lifetime, no fences, no interrupt
handling and no IOMMU. Radeon or Intel acceleration would need all of those
plus firmware, and honestly assessed, that is a different project rather than a
next step.

What it is: modesetting and buffer management built from the same code Linux
and FreeBSD use, compiled by our own toolchain, driving the framebuffer we
already bring up.

## Status

`tools/drmdeep.py` implements step 1. It builds every file in
`drivers/gpu/drm/` twice -- once with gcc, once with ShivyCX -- and separates
the two failure modes, because they call for different work:

* **shim gaps** (gcc rejects it too) -- a missing kernel header or type;
* **ShivyCX gaps** (gcc accepts it, we do not) -- a real compiler limitation.

```
$ python3 tools/drmdeep.py survey --priority --autostub -v
  ok    drm_rect.c                 7 symbols
  ok    drm_displayid.c            5 symbols
  shim  drm_fourcc.c               unknown type name 'spinlock_t'
  shim  drm_mm.c                   'drm_mm_interval_tree_augment' undeclared
  ...
  2 of 10 files compile        (550 lines, 12 symbols)
  8 need more shim             (gcc rejects them too)
  0 are ShivyCX gaps           (gcc accepts them)
```

**Zero ShivyCX gaps is the headline.** Every file that is portable enough for
gcc to build freestanding, our own compiler also builds -- `drm_rect.c` and
`drm_displayid.c` are upstream DRM sources, unmodified, compiled by ShivyCX and
assembled by rasm. What remains is shim work: the eight stragglers want
`spinlock_t` visible in one more header, an `ida` type, the interval-tree
macro, and similar. That is a bounded, mechanical exercise rather than a
research question.

`--autostub` generates placeholders for missing headers automatically (they
land in `tools/drm_shim/auto/`, which is not checked in). An empty placeholder
cannot silently corrupt anything: if the header really did define something the
code uses, the next compile fails on that identifier and the survey names it.
The hand-written headers in `tools/drm_shim/linux/` are the ones whose
*contents* matter -- `list.h` is a real intrusive-list implementation,
`bitfield.h` real mask arithmetic, `rbtree.h` and `ww_mutex.h` real struct
layouts because DRM embeds them by value.

### Honest notes on the shim

* `spinlock.h` and `ww_mutex.h` are **no-ops**. That is correct for a
  single-threaded kernel with no interrupts and wrong the moment either
  changes. Anything built on this inherits that assumption.
* `slab.h` declares `kmalloc`/`kfree` without defining them; the freestanding
  runtime supplies them (mbos: libmini, CrustOS: rlibc).
* `printk.h` discards every diagnostic. None of them affect what the algorithms
  compute, but a driver debugging session would want them pointed somewhere.
