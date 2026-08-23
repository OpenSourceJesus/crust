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
needs. The original answer was a 67-line shim. Carrying the same exercise
through `drm_fourcc`, `drm_displayid` and `drm_buddy`, and fixing the harness
defects noted below, grew it to **31 headers and about 1800 lines** in
`tools/drm_shim/linux/`, covering

* the scalar typedefs (`u8`..`s64`, `size_t`, `ssize_t`, `bool`, `va_list`),
* `min`/`max`/`clamp`/`swap`/`ARRAY_SIZE`, alignment and rounding,
* the attribute macros (`__printf`, `__iomem`, `__must_check`, …) as no-ops,
* real implementations where the algorithms depend on them: `rbtree.h` and
  `interval_tree_generic.h` (a full augmented red-black tree), `list.h`,
  `bitops.h`, `bits.h`, `log2.h`, `err.h`, `string.h` prototypes,
* types that DRM embeds *by value* and so cannot be forward-declared:
  `spinlock_t`, `struct kref`, `struct idr`, `wait_queue_head_t`,
  `struct delayed_work`, `struct llist_head`, `depot_stack_handle_t`,
* the no-op layer -- locking, work queues, wait queues, lockdep, `BUG_ON`.

That last group is where the honesty lives: no-op locking and never-running
work queues are correct for a single-threaded kernel with no interrupts, and
silently wrong the moment either changes. Each header says so at the point
where someone will read it.

With that:

```sh
gcc -c -nostdinc -ffreestanding -nostdlib -I tools/drm_shim -I drm-kmod/include \
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
  ok    drm_fourcc.c               9 symbols
  ok    drm_displayid.c            5 symbols
  ok    drm_buddy.c                9 symbols
  shim  drm_blend.c                field 'data' has incomplete type  (union hdmi_infoframe)
  shim  drm_edid.c                 implicit declaration of 'video_firmware_drivers_only'
  ...
  4 of 10 files compile        (2202 lines, 30 symbols)
  5 need more shim             (gcc rejects them too)
  1 are ShivyCX gaps           (gcc accepts them)

  ShivyCX gaps are the actionable ones:
    drm_mm.c                   line 372: expected '{'
```

`drm_rect.c`, `drm_fourcc.c`, `drm_displayid.c` and `drm_buddy.c` are upstream
DRM sources, unmodified, compiled by ShivyCX and assembled by rasm. That
covers the first rung of the adoption ladder above (`drm_fourcc` + `drm_rect`)
and half of the second (`drm_buddy`).

### The first real ShivyCX gap

Every previous run reported zero, and every apparent gap along the way turned
out to be the oracle being more permissive than the target, or the shim
depending on a gcc builtin. This one is neither.

`drm_mm.c:372` declares a function taking an enum by value:

```c
static struct drm_mm_node *
first_hole(struct drm_mm *mm, u64 start, u64 end, u64 size,
           enum drm_mm_insert_mode mode)
```

ShivyCX cannot parse an `enum` tag used as a parameter type. Reduced:

```c
enum mode { A, B };
static int f(enum mode m) { return (int)m; }   /* line 2: expected '{', found 'm' */
```

That is a compiler bug, not shim work, and it is the one item in this track
that belongs to ShivyCX rather than to the shim. `drm_mm` is otherwise
complete -- it compiles under gcc, augmented interval tree and all.

### drm_mm needed a real interval tree

`INTERVAL_TREE_DEFINE` was the last structural blocker, and unlike everything
else in the shim it could not be stubbed: `drm_mm` is a range allocator built
on an augmented red-black tree, where each node caches the maximum interval
end in its subtree so overlap queries can skip whole branches.

`linux/rbtree.h` is therefore a real implementation -- insert and erase with
colour rebalancing, augmentation callbacks threaded through the rotations, and
the cached-leftmost bookkeeping -- and `linux/interval_tree_generic.h` builds
the query macros on top.

**That breaks the assumption the rest of this survey rests on.** `drmdeep`
compiles files and counts symbols; it never runs a line. A no-op spinlock
cannot be subtly wrong, but a red-black tree that rebalances incorrectly
compiles perfectly and returns wrong answers at runtime.

[`tools/drm_shim_rbtree_test.py`](tools/drm_shim_rbtree_test.py) closes that
gap for these two headers, and only these two. It compiles the shim into a
native binary and runs it, re-checking after every operation that no red node
has a red parent, that black heights agree, that in-order traversal is sorted,
that the cached leftmost really is the minimum, that every subtree summary
equals the real maximum below it, and that interval queries return exactly
what brute-force search returns. Default is 50,000 randomised operations; it
is mutation-tested against four deliberate breakages.

It earned its keep immediately: the first draft of the augmentation walked
from the inserted node to the root, which misses nodes that a rotation
*demotes* off that path. Every file still compiled. The test caught it in
under 2000 operations.

### What that number is measured against

An earlier version of this section reported 2 of 10, but it was not
reproducible from a clean checkout, and the harness behind it was more
permissive than the target. Four things were wrong, and are now fixed:

* **The shim was committed flat** in `tools/drm_shim/` while every DRM source
  includes it as `<linux/NAME.h>`. None of the hand-written headers was ever
  loaded; `--autostub` silently replaced each with an empty placeholder,
  including `list.h` and `bitfield.h`, whose contents are the whole point.
* **The gcc oracle read host headers.** `-ffreestanding -nostdlib` does not
  remove the host include path (`-nostdlib` affects linking, and these are
  `-c` compiles), so `<linux/errno.h>` came from the distribution's
  `linux-libc-dev`. Files appeared to build freestanding while depending on an
  ambient host package, and ShivyCX -- which has no host include path -- was
  recorded as having a *compiler gap* for correctly failing where gcc should
  have failed too.
* **`-w` hid implicit declarations.** gcc 13 treats a call to an undeclared
  function as a warning, so `ERR_PTR()` with no declaration in scope compiled
  cleanly, with gcc assuming `int ERR_PTR()` and truncating a 64-bit pointer.
  That is wrong code counted as a success. `-w` also defeats `-Werror=`
  regardless of ordering, so it is no longer passed at all.
* **gcc's host-OS predefines leaked in.** `-nostdinc` removes the host's
  *headers*; it does not touch `__linux__` and `__unix__`, which gcc defines
  from the host triplet. drm-kmod is a FreeBSD port and branches on
  `#ifdef __linux__ … #elif defined(__FreeBSD__)` throughout `drm_print.h`, so
  gcc silently compiled the Linux branch while ShivyCX, predefining neither,
  compiled no branch at all and found `DRM_DEBUG_KMS` undefined. The survey
  now passes `-D__linux__ -D__unix__` explicitly to both compilers: not
  because a bare-metal target is Unix, but because whatever the oracle
  assumes, the compiler under test has to assume too.

A fourth problem was in the shim rather than the harness. `linux/printk.h`
defined `drm_info`, `drm_warn`, `drm_err`, `drm_WARN_ON` and the `DRM_*`
family -- names belonging to `<drm/drm_print.h>`, which is included later. So
upstream's real definitions won, ours were dead code, and gcc emitted a
redefinition warning on every file. Those no-ops were also *masking* the
`__linux__` problem above: they satisfied `DRM_DEBUG_KMS` for both compilers
and hid the fact that upstream's definition was unreachable for one of them.
`linux/printk.h` now defines only the Linux `printk` API.

The numbers above are taken with `-nostdinc`, gcc's own freestanding headers
handed back explicitly, and implicit declarations promoted to errors. Every
"ShivyCX gap" observed while fixing this turned out to be the oracle being
more lenient than the target -- with one exception, which was ours: the shim
originally used `__builtin_ffsll` and `__builtin_popcount`, which ShivyCX does
not implement. Those are now plain C loops, because a shim that manufactures a
ShivyCX gap out of its own code and reports it against the DRM sources is
worse than useless.

[`tools/drm_shim_test.py`](tools/drm_shim_test.py) locks all of it down --
reachability, hermeticity, strictness, guard idempotence, that no autostub
ever shadows a hand-written header, that no shim header defines a `drm_*`
symbol, and that the survey sets every host-OS predefine gcc would otherwise
supply on its own. It is mutation-tested (`--mutate`) and
needs no drm-kmod checkout, so it runs anywhere gcc does. The reachability
check earns its keep beyond the original defect: it compiles each header
*alone*, which caught three include cycles the survey could not see, because
survey sources always reach `kernel.h` first.

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
