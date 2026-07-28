# CrustOS

A small operating system built from the parts of [Redox](https://redox-os.org)
that Crust can compile, plus the parts we supply ourselves.

```sh
python3 tools/crustos.py fetch    # clone the Redox kernel and relibc
python3 tools/crustos.py run      # compile the compatible subset, link, run
```

```
CrustOS
  schemes      : 6
  heap offset  : 0xe0000000  (from vendor/kernel arch consts)
  opened       : 4 of 5 URLs
  first fd     : sys:/context
  frames       : 495 free of 512
  runnable     : 2 of 3
  switches     : 8
  ticks        : init=16 shell=12 idle=0
  x86_64 kpage : 0x8000000000000003 present=1
  arm64 upage  : 0x40000000000043
  heap frame   : 917504
  read(0,64)   : 64
  close(0)     : 0
  bad fd       : -1
```

## What this is, and is not

**It is** a working model of a Redox-shaped kernel: URL-addressed schemes, a
context table, a physical frame allocator, a round-robin scheduler and a
syscall interface — built with no cargo, no rustc, no LLVM and no nightly
toolchain, linking **88 object files compiled from genuine Redox source**.

**It is not** an operating system you can boot. CrustOS runs hosted, as an
ordinary program. There is no bootloader, no bare-metal target, no interrupt
handling and no MMU work. Calling it an OS is a claim about *structure*, not
about capability, and it is worth keeping that distinction sharp: what has
been demonstrated is that this toolchain can compile real kernel source and
build a coherent program out of it, not that the result runs on hardware.

## The three-way split

| part | language | why |
|---|---|---|
| `vendor/kernel`, `vendor/relibc` | Rust (upstream) | genuine Redox source, compiled by Crust |
| `crustos/schemes.py` | rpython | URL parsing, routing tables, listings |
| `crustos/kernel.c` | Rust + C | frame allocator, contexts, scheduler, syscalls |

The split is not arbitrary. Each half is written in the language that makes it
short:

**rpython** gets the text and list work. Scheme routing is string parsing and
table lookup — exactly what the Crust subset is worst at, having no string
type and no iterator protocol. `tools/py2c.py` lowers a typed rpython list to
three words (`{ int* data; long len; long cap; }`), which the Rust side walks
directly as a `PyList<i32>` with no copy and no conversion at the boundary. So
the collection is *built* where collections are cheap and *consumed* where the
machine is close, and neither side pays for the crossing — after lowering,
there isn't one.

**Rust** gets the fixed layouts and the hot loops: a bitmap frame allocator,
the context array, the scheduler, and syscall dispatch through a
data-carrying enum. No allocator, no boxing, no GC anywhere near it.

**C** gets `main`.

## Paging, in upstream's shape

`rmm/src/page/flags.rs` writes its page flags generically over the
architecture, reading constants off a trait:

```rust
impl<A: Arch> PageFlags<A> {
    pub fn new() -> Self {
        Self::from_data(A::ENTRY_FLAG_DEFAULT_PAGE | A::ENTRY_FLAG_NO_EXEC)
    }
}
```

`crustos/kernel.c` uses the same shape, and Crust monomorphises it: one
implementation of the flag logic, one instantiation per architecture, every
constant resolved at compile time with no dispatch. The `x86_64 kpage` and
`arm64 upage` lines above come from the same source code compiled twice.

The `heap frame` line is that logic applied to a genuinely upstream value —
`kernel_heap_offset()` from `vendor/kernel/src/arch/x86/consts.rs`, shifted by
the architecture's own `PAGE_SHIFT`.

Upstream's own `PageFlags<A>` still does not instantiate here, because it
reaches for parts of `core` that Crust has no source for. What has been shown
is that the *pattern* compiles, which is the part that was in doubt.

## What comes from upstream Redox

Currently **93 of 106** translatable files compile to objects, of which 88 can
be linked together. What survives is mostly the memory manager and the
architecture definitions:

- `rmm/src/page/` — page table entries, flags, mappers, table walking. These
  compile but export little, because a generic impl only produces code once
  something instantiates it; see the section above.
- `rmm/src/allocator/frame/` — the buddy and bump frame allocators
- `src/arch/*/consts.rs` — memory layout constants. `kernel_heap_offset()` in
  the output above is upstream's, called from our `main`.
- `src/arch/*/paging.rs`, `ipi.rs`, syscall constants
- `src/sync/wait_queue.rs`, `src/memory/kernel_mapper.rs`
- relibc's `auxv_defs`, `limits`, various header modules

Most of these contribute constants, layouts and helpers rather than entry
points — which is what a kernel takes from a memory manager anyway.

### Why some objects are dropped

Five of the 93 cannot join the link, for two reasons that both follow from
compiling files that were never meant to be one program:

- **Duplicate symbols.** Redox ships one implementation per architecture, and
  Crust flattens module paths, so the `aarch64` and `riscv64` versions of a
  function end up with the same symbol. The first is kept and later duplicates
  dropped — arbitrary, but the alternative is linking nothing.
- **Unresolved references** into parts of Redox that do not compile
  (`KernelMapper_lock`, `rmm_aarch64_init_mair`). Those objects are dropped
  rather than left to fail the link with a message that names the symbol but
  not the file.

`tools/crustos.py` does this selection with `nm`, and `--verbose` reports what
was dropped and why.

## Commands

| command | what it does |
|---|---|
| `fetch` | shallow-clone `redox-os/kernel` and `redox-os/relibc` into `vendor/` |
| `fetch --update` | pull existing checkouts |
| `survey` | how much of the source Crust translates, and what stops the rest |
| `survey --verify` | also compile, and report how many produce an object |
| `survey --blockers` | rank the failure messages |
| `build` | compile the compatible subset and link it with `crustos/` |
| `build --upstream-only` | stop after the upstream objects |
| `run` | build, then run |
| `clean` | remove `build/crustos` |

`CRUSTOS_VENDOR` and `CRUSTOS_BUILD` override the directories. Passing source
trees as arguments uses those instead of `vendor/`.

## Honest limits

Beyond "it does not boot", four things are worth naming:

**`Mutex` does not lock.** The bundled core supplies `Mutex<T>` and
`RwLock<T>` so that upstream source parses, and they do no synchronisation at
all. That is defensible only because Crust has no threads — no spawn, no
atomics, no memory model — so there is nothing for a lock to protect against.
The moment real concurrency exists they become dangerous and must be
*replaced*, not extended.

**`#[cfg]` is evaluated against a fixed target** (x86_64, 64-bit, little
endian, redox/unix/relibc). There is no way to change it yet; it lives in
`CFG` in `shivyc/crust.py`.

**rpython cannot run before the heap does.** py2c's lists are malloc-backed
and its runtime has an arena, so the scheme layer is fine for bookkeeping and
would be entirely wrong for an allocator or an interrupt handler. `crustos/`
keeps that line, but nothing enforces it.

**The subset is about a sixth of Redox.** 117 of 615 files translate, 99
compile. The rest need traits with associated types, iterator chains,
`async`, inline assembly, and a real `core`. See `CRUST.md` for the current
blocker analysis, and `tools/crustdeep.py` for the measurement behind it.
