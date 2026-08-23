# drmdeep: repairing the survey before extending it

A design note for the DRM-salvage track described in [GPU.md](tools/GPU.md).

Measured on crust `b570d34` (2026-08-22) against drm-kmod
`b81715b` (2026-08-19), gcc 13.3.0 on Ubuntu 24.04, no GPU and no
CUDA toolkit present.

---

## Summary

`GPU.md` reports the survey's result as:

```
  2 of 10 files compile        (550 lines, 12 symbols)
  8 need more shim             (gcc rejects them too)
  0 are ShivyCX gaps           (gcc accepts them)
```

and draws the conclusion that **zero ShivyCX gaps is the headline** — that
what remains is bounded, mechanical shim work rather than compiler work.

That conclusion is probably right. But it is not currently *supported*, because
the committed tree does not reproduce the number, and the harness that produced
it was measuring something other than what it claims.

A clean checkout gives:

```
  0 of 10 files compile        (0 lines, 0 symbols)
  10 need more shim            (gcc rejects them too)
  0 are ShivyCX gaps           (gcc accepts them)
```

Three defects are responsible, and they compound: each one hides the next.
None of them is in the DRM sources or in ShivyCX. All three are in the shim and
the harness.

The proposal below is therefore **not** "write more shim headers". It is: fix
the harness so its numbers mean what they say, add the regression tests that
would have caught this, and only then resume adoption — starting with a single
missing typedef that is the first error in **50 of the 76 files** in the
generic layer.

---

## Defect 1 — the hand-written shim is at the wrong path

`drm_rect.c` opens with:

```c
#include <linux/errno.h>
#include <linux/export.h>
#include <linux/kernel.h>
```

`drmdeep.includes()` passes `-I tools/drm_shim`, so the preprocessor looks for
`tools/drm_shim/`**`linux/`**`kernel.h`. The hand-written headers are committed
flat, at `tools/drm_shim/kernel.h`.

Every one of them is therefore unreachable. `--autostub` then generates an
empty placeholder for each miss, so `list.h` — a real intrusive-list
implementation — is silently replaced by a two-line stub, and likewise
`bitfield.h`, `rbtree.h` and `ww_mutex.h`, which `GPU.md` explicitly names as
the headers *whose contents matter*.

This is visible in the survey's own output. From a clean checkout it reports:

```
  generated 56 placeholder headers: linux/bitfield.h linux/bitops.h ...
```

`linux/bitfield.h` is hand-written. The tool is stubbing over its own shim and
reporting that as progress.

`GPU.md` describes the shim as living in `tools/drm_shim/linux/`, so the
documentation is correct and the tree is wrong. `git log` shows the files were
added flat in a single commit (`16abb37`, "Raspberry Pi and Jetson Nano - part
VI - baremetal") and never moved, which means **the committed state has never
reproduced its own documented result.** The numbers in `GPU.md` came from a
working tree that was correct and a commit that was not.

**Fix:** `git mv tools/drm_shim/*.h tools/drm_shim/linux/`.

With only that change, `drm_rect.c` and `drm_displayid.c` pass gcc — exactly
the two files `GPU.md` names — and the remaining eight fail on specific,
nameable types rather than on structural nonsense:

| before the move | after the move |
|---|---|
| `expected ':', ',', ';', '}' or '__attribute__' before '*'` | `unknown type name 'depot_stack_handle_t'` |
| `unknown type name 'u8'` | `'GFP_KERNEL' undeclared` |
| `field 'ww_ctx' has incomplete type` | `unknown type name 'rb'` |

The first column is what a missing `kernel.h` looks like. The second is a real
shim gap.

---

## Defect 2 — the gcc oracle is contaminated by host headers

This is the more serious one, because it does not merely suppress the result —
it inverts the tool's central claim.

`drmdeep.build_gcc()` invokes:

```
gcc -c -ffreestanding -nostdlib -fno-pic -w -D__KERNEL__ -I ... 
```

`-ffreestanding` changes what gcc *assumes about the environment*. `-nostdlib`
affects **linking**, and these are `-c` compiles. Neither removes gcc's default
include search path. So `#include <linux/errno.h>` resolves against
`/usr/include/linux/errno.h`, shipped by Ubuntu's `linux-libc-dev`.

Demonstrated directly:

```sh
$ gcc -c -ffreestanding -nostdlib ... drm_rect.c     # rc=0
$ gcc -c -nostdinc -ffreestanding -nostdlib ... drm_rect.c
drm_rect.c:24:10: fatal error: linux/errno.h: No such file or directory
```

Two consequences.

**The two compilers are not being asked the same question.** ShivyCX has no
host include path, so it correctly cannot find `linux/errno.h`. gcc can. The
survey observes "gcc accepted it, ShivyCX did not" and classifies that as
`FAIL_SHIVYC` — *a ShivyCX gap worth fixing*. It is the opposite: ShivyCX is
being more faithful to the freestanding target than the oracle is. After
Defect 1 is fixed, this misclassification is exactly what appears:

```
  2 are ShivyCX gaps           (gcc accepts them)
    drm_rect.c                 unable to read included file
    drm_displayid.c            unable to read included file
```

Both are `linux/errno.h` and `linux/string.h`. Neither is a compiler
limitation.

**The "compiles" count is not a freestanding claim.** A file that builds only
because it borrowed the host distribution's kernel UAPI headers has not been
shown to build for mbos or CrustOS, which have no `/usr/include`. The number
that matters for a `gpu:` scheme is the `-nostdinc` number.

**Fix:** add `-nostdinc` to `build_gcc()`, and add `-I` for gcc's own
freestanding headers (`stddef.h`, `stdarg.h`) via
`$(gcc -print-file-name=include)` if any file needs them.

This will make the reported numbers *worse*, which is the point. The honest
baseline, measured with `-nostdinc` and the shim relocated, across all 76 files
in `drivers/gpu/drm/`:

```
  0 of 76 compile
  top blockers:
    50  unknown type name 'depot_stack_handle_t'
     4  field 'refcount' has incomplete type
     3  field 'lock' has incomplete type
     3  unknown type name 'rwlock_t'
     2  'ENOENT' undeclared
```

> Caveat on that run: it used **empty** autostub placeholders, whereas
> `drmdeep.autostub()` writes `#include <linux/kernel.h>` into each one. The
> richer body is the more favourable configuration and would likely compile
> some files, but it could not be measured — see Defect 3. The 0-of-76 figure
> is a floor, not the tool's true post-fix score.

---

## Defect 3 — `kernel.h`'s include guard does not cover its own includes

`tools/drm_shim/kernel.h` closes its guard at line 44, then continues for
another 19 lines:

```c
#define EXPORT_SYMBOL_GPL(s)
#endif                              /* <- guard ends here */
/* limits the allocators compare against */
#define U8_MAX   0xffU
...
#include <linux/errno.h>            /* <- outside the guard */
...
#include <linux/string.h>           /* <- outside the guard */
```

Today this is invisible, because Defect 1 means `kernel.h` is never included at
all. Fix Defect 1 alone and it becomes visible as noise — a single missing
header reported five times, once per re-inclusion:

```
tools/drm_shim/linux/kernel.h:58:10: error: unable to read included file
tools/drm_shim/linux/kernel.h:58:10: error: unable to read included file
tools/drm_shim/linux/kernel.h:58:10: error: unable to read included file
...
```

Fix Defect 1 *and* run `--autostub`, and it becomes fatal. The placeholder for
`errno.h` includes `kernel.h`; `kernel.h`'s guard does not protect line 58; so
`kernel.h` includes `errno.h` again, forever:

```
In file included from tools/drm_shim/linux/kernel.h:58,
                 from tools/drm_shim/auto/linux/errno.h:2,
                 from tools/drm_shim/linux/kernel.h:58,
                 from tools/drm_shim/auto/linux/errno.h:2,
                 ...
```

This is why the favourable configuration in Defect 2 could not be measured: the
run does not terminate usefully.

**Fix:** move the `#endif` to the end of the file. One line.

Note the interaction, because it is the reason all this went unnoticed for a
release: Defect 1 hides Defect 3 entirely, and hides Defect 2 for eight of the
ten priority files. Defect 2 then partially hides Defect 1, by letting two
files compile anyway and making the tree look half-working rather than
inert. No single defect is subtle; the stack of three is.

---

## The payoff: one typedef

Once the harness is honest, the distribution of blockers is remarkably
concentrated. `depot_stack_handle_t` is the **first** error in 50 of 76 files.

It is a single `typedef u32` from `<linux/stackdepot.h>`, reached through
`dma-resv.h` → `ww_mutex.h`. It is used only by `KMEMLEAK`/`KASAN` debug
plumbing that a freestanding build does not want and never calls. It is not an
algorithm, a lock, or an allocator — it is a debug cookie.

An autostub for `stackdepot.h` is empty, which is why it does not currently
help: the type must actually be *declared*, not merely have its header exist.
That is a two-line hand-written shim header, and it is the single highest-value
change available in this track.

It will not make 50 files compile — it clears the first error, and the next one
appears. But it is the difference between a blocker list dominated by one
uninformative repeat and a blocker list that describes the real shape of the
work.

---

## Proposed work, in order

Each stage stands alone and is verifiable in a container with gcc and no
hardware, which is the whole appeal of this track relative to the Jetson GPU
work.

**Stage 1 — make the harness honest.** Three small changes:
`git mv` the shim into `linux/`; add `-nostdinc` to `build_gcc()`; move the
`#endif` in `kernel.h`. Expected outcome: the reported score *drops*, and
`GPU.md` is updated to the new number with the old one noted as having been
measured against host headers.

**Stage 2 — lock it down with tests**, in the style of
`tools/uart_8250_test.py` and `tools/gic_base_test.py`, both of which are
mutation-tested. Three checks, each of which would have caught one defect:

| check | catches |
|---|---|
| every `tools/drm_shim/linux/*.h` is reachable as `<linux/NAME.h>` from a one-line probe TU | Defect 1 |
| a probe TU that includes only `<linux/errno.h>` fails without the shim on the include path | Defect 2 |
| every shim header is idempotent — including it twice produces the same token stream | Defect 3 |
| no file in `auto/` shares a name with one in `linux/` | recurrence of 1 |

The last is the cheap general guard: autostub generating a placeholder for a
header we hand-wrote is *always* a bug, and the tool should say so loudly
rather than proceeding.

**Stage 3 — hand-write `stackdepot.h`,** then re-run `blockers` and let the
next tier of causes appear. Repeat while the blockers stay concentrated. Stop
when they fan out into a long tail, which is the signal that the remaining
files are genuinely entangled with the kernel rather than merely
under-shimmed.

**Stage 4 — only then** resume the `GPU.md` adoption ladder
(`drm_fourcc` + `drm_rect`, then `drm_mm`/`drm_buddy`, then
`drm_edid` + `drm_modes`).

---

## What this would and would not establish

It would establish that a named set of upstream DRM algorithm files compile,
unmodified, through ShivyCX and rasm, for a freestanding target, with a shim
whose contents are known and whose reachability is tested.

It would **not** establish that they compute correctly. Nothing here executes a
single one of these functions — `drmdeep` compiles and counts symbols. A file
that compiles under a no-op `spinlock.h` and a `printk.h` that discards
everything has been shown to be *syntactically* portable and nothing more.
`drm_rect`'s clipping math and `drm_edid`'s parsing both have obvious unit
tests against known inputs, and neither exists.

That gap is worth naming now rather than at Stage 4, because "2 of 10 files
compile" reads like more progress than it is, and the honest replacement should
not repeat the trick in the other direction.

It also has no bearing on the Jetson. This track targets the mbos/CrustOS
framebuffer, which today is QEMU's std VGA over Bochs DISPI on x86. Nothing in
`drivers/gpu/drm/`'s generic layer touches a Tegra, and the Nano's GM20B does
not appear anywhere in the crust tree.

---

## Reproducing the measurements

```sh
python3 tools/drmdeep.py fetch
python3 tools/drmdeep.py survey --priority --autostub -v     # 0 of 10

mkdir -p tools/drm_shim/linux && cp tools/drm_shim/*.h tools/drm_shim/linux/
rm -rf tools/drm_shim/auto
python3 tools/drmdeep.py survey --priority --autostub -v     # 2 pass gcc

gcc -c -nostdinc -ffreestanding -D__KERNEL__ \
    -I tools/drm_shim -I vendor/drm-kmod/include \
    -I vendor/drm-kmod/include/uapi \
    vendor/drm-kmod/drivers/gpu/drm/drm_rect.c -o /tmp/r.o
# fatal error: linux/errno.h: No such file or directory
```
