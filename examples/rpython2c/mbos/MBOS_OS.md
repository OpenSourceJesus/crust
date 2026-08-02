# mbos as an OS — interrupts, shell, heap

mbos started as a renderer: boot, parse HTML, paint it, halt. This document
covers the layer added on top of that — the parts that make it a machine you
can drive rather than a program that runs once — and the plan for the rest.

The destination is a self-hosted system: one that can run `py2c.py` under its
own Python interpreter, assemble the result with `rasm`, and link it with
`rlink`, all without a host. That is what makes the JIT-in-the-OS idea
tractable later, in the way TempleOS had it.

```
make            build build/mbos.elf
make run        boot in a QEMU window
make test       render self-test
make test-irq   timer + keyboard interrupts
make test-shell shell dispatch, line editing, history
make test-alloc heap: split, coalesce, rejection paths
make test-fs    ramdisk: module discovery, tar walk, corrupt archives
make test-gfx   graphics: caps, stride, mode negotiation to 3840x2160
make fourk      boot at 3840x2160 in a QEMU window
make check-rs   rustc over the Rust-authored modules
```

---

## Where the pieces live

| Layer | Files | State |
|---|---|---|
| Boot, long mode | `boot64.S` | works |
| Console, framebuffer | `console.c`, `font8x16.h` | works |
| Graphics (to 4K) | `vbe.c` | works |
| Interrupts | `idt.S`, `irq.c` | works |
| Keyboard | `kbd.c` | works |
| Shell | `shell.c`, `editbuf.rs` | works |
| Heap | `alloc.c`, `alloc.rs` | works |
| Ramdisk | `ramfs.c`, `tarfs.rs`, `fs/` | works |
| minipy | `tools/minipy/`, `tools/rpy_lib/rast.py` | not started |

The toolchain half is further along than the OS half, which is unusual and
worth keeping in mind: `rasm` assembles `boot64.S` byte-identically to GNU `as`
(59/59), and `rlink` links the whole kernel image matching GNU `ld` symbol for
symbol (16/16). The pieces needed to *build* a self-hosted system exist; the
pieces needed to *run* one are what is being filled in.

---

## Interrupts (`idt.S`, `irq.c`)

`idt.S` is `baremetal64/idt64.S` verbatim. `irq.c` is a port of the matching
`idt64.c` with minikraft's `console.h`/`string.h` dependencies removed, plus
the PIC remap and PIT programming that `examples/baremetal/kernel_irq.c` kept
in its own `app_main`. One entry point does the lot:

```c
irq_init(100);   /* IDT -> PIC remap -> 100 Hz timer -> keyboard -> sti */
```

Unhandled CPU exceptions panic with the vector name, error code, RIP, RSP and
RFLAGS on serial. The original silently returned, which for a fault means
re-faulting forever with nothing on the wire.

`kbd.c` is scancode set 1 with shift, caps lock, ctrl, and the `0xE0` extended
keys. The prefix and its code arrive as two separate interrupts, so the flag
persists across handler calls. The IRQ handler only reads port `0x60`,
translates, and pushes into a ring — all echo and editing happens in the
foreground, so no console work runs in interrupt context.

## Shell (`shell.c`, `editbuf.rs`)

The line editor and the dispatcher are separate on purpose. The editor turns
keystrokes into a finished line and knows nothing about what commands mean; the
dispatcher tokenizes into argv and walks a `{name, fn, help}` table. When minipy
lands, running a script is one more row in `CMDS` and a REPL is one more caller
of `shell_readline()` — neither needs the other to change.

Commands: `help` `echo` `clear` `ticks` `uptime` `ver` `mem` `memtest` `peek`
`reboot`.

`peek` and `mem` are not demos. They are how the next steps get debugged: an
interpreter that leaks shows up as a block count that only climbs.

**Known limit:** the editor does not wrap. Input longer than the console width
is refused rather than allowed to corrupt the row. At 128 columns there is
headroom, but a Python REPL will hit it, and the fix wants multi-row lines
(tracking a start row as well as a start column).

## Heap (`alloc.c`, `alloc.rs`)

A 1 MiB static arena, covered with no gaps by a list of blocks held in address
order. First-fit with splitting; freeing merges with the neighbour on either
side. Sizes and offsets are multiples of 16 and the arena base is aligned, so
every pointer is aligned and no block needs a leading pad.

```c
void *kmalloc(size_t n);
void *kzalloc(size_t n);      /* + zero */
void  kfree(void *p);         /* NULL is a no-op; a bad pointer is reported */
```

`kheap_verify()` audits the covering invariant — address order, no gaps, no two
adjacent free blocks, sums to the arena size — and `mem check` runs it from the
shell.

Double frees and out-of-arena pointers are refused and reported rather than
accepted. Accepting a double free would merge two live blocks and surface the
damage somewhere unrelated.

**Not interrupt-safe.** Nothing calls it from a handler today: the timer only
increments a counter, the keyboard only pushes into a compile-time-sized ring.
That has to be revisited when preemption arrives.

**Static arena, not the memory map.** Deliberate for now — the heap is
available before anything else initialises. Step 4 brings in the Multiboot
memory map, at which point this becomes the early-boot fallback.

## Ramdisk (`ramfs.c`, `tarfs.rs`)

Everything in `fs/` is tarred into `build/initrd.tar` at build time and handed
to the kernel as a Multiboot module (`qemu -initrd`). `ramfs.c` finds the
module through the Multiboot info structure, walks the archive, and builds a
file table. `ls` and `cat` read it from the shell.

Plain ustar, no compression — the kernel has no inflate and does not need one.

**Nothing is copied.** The table points into the module image where the
bootloader left it, so a 1 MiB ramdisk costs a few hundred bytes of heap rather
than a second megabyte. That is safe because the ramdisk is read-only by
construction, which is also why there is no write path here to get wrong.

`tarfs.rs` holds the header parsing: fixed-offset fields of NUL-or-space
terminated octal. That is parsing that fails quietly — an unterminated field
runs into the next one, a digit outside 0-7 gets silently accepted, a size that
overflows wraps into something plausible. Each of those is a bounds or range
check, so it belongs on the checked side. `ramfs.c` keeps the pointer
arithmetic and the Multiboot layout.

Both the ustar magic *and* the header checksum are verified. The magic says
this looks like a header; the checksum says it is one. Without the second, a
walk that lost alignment would read file data as a header and keep going.

A ramdisk arrives from outside the kernel, so `make test-fs` boots deliberately
corrupted archives — a flipped header byte, a non-octal digit in the size
field, an archive cut in half — and requires each to be refused with a message
rather than faulted on. Booting with no ramdisk at all is also a normal
outcome, not an error.

The test compares the guest's view against the host's `tarfile` module: same
names, same sizes, same file count, and `cat` output matching the source file
line for line. That last one is the real check on the data pointer — a stride
bug still produces plausible entries, but only a correct walk lands the data on
the right byte.

## Graphics (`vbe.c`)

QEMU's std VGA exposes the Bochs display interface (DISPI) on ports
0x1CE/0x1CF, so a linear-framebuffer mode can be set with no real-mode BIOS
calls. mbos now runs up to **3840x2160** at 32bpp.

Three things the driver learned:

**Capabilities are queried, not assumed.** Setting the `GETCAPS` bit makes the
geometry registers report maxima instead of the current mode; the VRAM register
is a count of 64 KiB units. On QEMU's std VGA that comes back as a maximum of
16000x12000 with 16 MiB of memory.

**Geometry limits and memory limits are different limits**, and the second is
the one that bites. 3840x2160 is well inside 16000x12000 but needs 31.6 MiB, so
a 16 MiB device accepts the mode and then scans out past the end of VRAM.
`gfx_mode_fits()` checks both, and an impossible mode is refused with a
diagnostic naming the limit that was hit, falling back to VGA text:

```
[gfx] requested mode exceeds the device: 3840x2160 needs 31 MiB,
      device has 16 MiB, max 16000x12000
[con] text console
```

**Stride is not width.** The scanline stride is the device's *virtual* width,
which it may round up for alignment. The old code indexed `fb[y * width + x]`,
which is right for the common cases and quietly shears the image when it is
not. The driver now takes `DISPI_VIRT_WIDTH` from the hardware.

`gfx_present()` blits a caller-owned RAM buffer of exactly width x height
pixels. That is the path a game should draw through: writes to VRAM are
write-combined and fast, reads are not. `gfx_scroll()` is the one place the
kernel still reads VRAM, and it is why console scrolling is the slowest thing
at hi-res -- a one-line scroll at 4K moves ~32 MiB each way. A RAM back buffer
would fix both that and give double buffering; it is the obvious next step.

Shell: `gfx` reports mode, stride, VRAM and device maxima; `gfxtest` draws
colour bars, a gradient, and corner markers.

`make test-gfx` covers all of it, including reading the corner markers back out
of a screenshot -- with a wrong stride the right-hand markers walk diagonally
down the screen instead of sitting on the edge, which is precisely the failure
the old code would have produced.

### Correction to the README

`examples/rpython2c/mbos/README.md` says hi-res needs `-device
VGA,vgamem_mb=64`. That is true past roughly 2560x1600, but **1920x1080 needs
only 7.9 MiB and runs fine on the 16 MiB std device** -- `make test-gfx`
asserts it. 64 MiB is genuinely required at 4K.

---

## Writing parts of the kernel in Rust

Some modules are written in Crust's Rust subset and have two toolchains pointed
at them, doing different jobs:

- **rustc checks.** `make check-rs` runs `--emit=metadata` over every `.rs`
  module. It produces nothing the kernel uses; it is a gate.
- **crust.py compiles.** `gen_rs.py` calls `crust.translate()` to lower `.rs`
  to C, which gcc builds into the image as an ordinary object. No FFI, no
  runtime, no libc — a lowered `EditBuf_insert` is just a C function.

`gen_rs.py` also emits a header by splitting crust's output at the declaration
line, so the prototypes the C side calls through are exactly the ones the Rust
produced. The two cannot drift.

`CRUST.md` documents `#include "vec2.rs"`, which works when ShivyCX is the
compiler. mbos is built with gcc, hence the separate generation step.

**What goes on which side:** if a function can be checked without a machine, it
belongs in Rust. `editbuf.rs` has the buffer and history-ring index arithmetic;
`shell.c` keeps the drawing. `alloc.rs` has the block bookkeeping; `alloc.c`
owns the arena and the offset-to-pointer translation.

**Subset constraints:** both toolchains must accept the file, which means no
slices, iterators, `Option`, or traits. Flat arrays and explicit index math —
which is the error-prone style, and the reason a checker on it is worth
anything.

### What the Rust gate actually catches

Measured, not assumed. Two bugs injected into `editbuf.rs`:

| Bug | rustc | gcc |
|---|---|---|
| `fn left(&self)` mutating `self.pos` | **error** | cannot express the concept |
| `self.buf[256]` on a `[u8; 256]` | passed | warning, exit 0 |

rustc 1.75 did not reject the constant out-of-bounds index — its bounds
checking is a runtime one, and const-propagation did not fire through the
struct field.

The checkers are complementary, not redundant. Generated objects therefore
compile with `RSWARN` (`-Werror=array-bounds` and friends), which makes gcc's
half binding.

Rust here buys aliasing and mutability discipline, which is real and is exactly
what C cannot give you. It is **not** a compile-time bounds checker. Worth
knowing before relying on the Rust side feeling safe.

---

## Remaining steps


**5. minipy in-kernel.** `MINIPY.md` has minipy shelling out to `python3` to
turn a `.py` file into a JSON AST. On bare metal there is no `python3` to shell
out to, so this is structural, not a porting detail. The groundwork exists:
`tools/rpy_lib/rast.py` is a de-eval'd pymetaterp Python parser written
explicitly as the first step toward an RPython port. Finishing that port is the
long pole for everything downstream.

**6. Self-hosting.** Run `py2c.py` under in-kernel minipy, then feed its output
to `rasm` and `rlink` — both of which already provably produce a correct kernel
image from this tree.

### rasm can now assemble everything in the tree

`rasm` used to reject `idt.S` with `unsupported mnemonic: ISR_NOERR` — it had
no `.macro` support, which was the one known blocker on step 6. That is fixed:
`tools/rpy_lib/rasm_macro.py` expands `.macro`/`.endm` and `.rept`/`.endr`
before parsing, and `make test-rasm-macro` covers it. See RASM.md.

One documented divergence remains, unrelated to macros. On `idt.S` rasm's
instruction stream matches GNU `as` exactly — 258 instructions, same mnemonics,
same operands — but its `.text` is 27 bytes larger, because rasm keeps a
relocation for branches to a `.global` symbol while `as` resolves same-section
branches and relaxes nine of them to rel8. Functionally identical once linked.
The mbos build still uses gcc for `idt.o` so that `rlink_baremetal_test.py` can
keep comparing symbol addresses against an `ld`-built reference.

---

## A ShivyCX-built game kernel

Separate from mbos, and the most direct exercise of the self-hosting path so
far: `examples/baremetal/kernel_mingine.c` is a bootable image in which
**every instruction came from ShivyCX** — `OS pieces linked in: (none)`.

One translation unit, three languages, no FFI: C, plus Rust spliced in from
`mingine.rs` by `shivyc/crust.py`, plus rpython lowered from `mingine.py` by
`py2c.py`. It brings up a Bochs-VBE framebuffer over PCI, renders the shared
scene in `examples/crust/baremetalgames/scene.c`, and blits it to the screen.

`make test-mingine` compiles that same `scene.c` twice — hosted as a process,
and as the kernel — and requires the two to agree:

```
scene 320x200 frames 24     ball 105,114
foe 178,144                 score 594
pixels 41fcd6e7             <- identical in both
```

The checksum is the point. "It booted and something appeared" is compatible
with a great many bugs; "every pixel of the final frame hashes the same in both
worlds" is not.

### Three bugs this found in the bare-metal boot path

All in minikraft's 64-bit stub, all latent because nothing had booted an image
that needed them.

1. **No Multiboot AOUT kludge.** `MB_FLAGS` was `0`, so `qemu -kernel` read the
   ELF class, found ELFCLASS64, and refused with *"Cannot load x86-64 image,
   give a 32bit one"*. Every `--image` build was unbootable this way. Fixed by
   setting bit 16 and adding the five address fields, with `_load_start` /
   `_load_end` / `_bss_end` now defined in `kernel64.ld`. mbos's own `boot64.S`
   had solved this and documented why; minikraft's had not.

2. **Only 1 GiB identity-mapped.** QEMU puts the Bochs-VBE framebuffer up
   around `0xFD000000`, so writing a pixel page-faulted. Now maps 4 GiB with
   four PDs, the same as mbos.

3. **`ENABLE_LOGGING` never defined.** minikraft wraps every
   `console_putchar` / `console_puts` body in `#ifdef ENABLE_LOGGING`, so the
   console linked and did nothing. A "prints to VGA/serial" demo booted
   silently. Now defined in `_BASE_CFLAGS`.

Also fixed: `shivycx_baremetal.py` copies the app into a temp directory before
compiling, which silently broke any `#include "..."` the app made relative to
itself, so a multi-file application could not build. The original directory is
now on the include path.

The kernel carries its own COM1 routines rather than using minikraft's console.
That keeps the image dependent on no OS piece at all, which is what lets the
test assert the whole thing is ShivyCX's output.
