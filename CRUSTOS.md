# CrustOS

A small **macro-kernel-shaped** operating system built from the parts of
[Redox](https://redox-os.org) that Crust can compile, plus the parts we supply
ourselves. Schemes and drivers live in the CrustOS address space (unlike Redox
userspace scheme daemons), so C drivers and rpython bookkeeping sit beside
Rust `rmm` without IPC.

```sh
python3 tools/crustos.py fetch           # clone the Redox kernel and relibc
python3 tools/crustos.py run             # compile, link, run (default guest ELF)
python3 tools/crustos.py run --elf path  # load a guest ELF (sets CRUSTOS_ELF)
```

```
CrustOS
  schemes      : 7
  heap offset  : 0xe0000000  (from vendor/kernel arch consts)
  opened       : 4 of 5 URLs
  first fd     : sys:/context
  frames       : 495 free of 512
  runnable     : 2 of 3
  switches     : 8
  ticks        : init=76 shell=72 idle=0
  ...
  elf load     : build/crustos/hello_guest.so -> 0
  elf pid      : 4
  elf image    : 16392 bytes, 4 PT_LOAD, dyn=1
  elf reg_class: 1 (10 regs to save)
  elf guest rc : 2
```

## What this is, and is not

**It is** a working model of a Redox-shaped kernel: URL-addressed schemes, a
context table, a physical frame allocator, a round-robin scheduler, a syscall
interface, and a hosted **static/PIE ELF loader** — built with no cargo, no
rustc, no LLVM and no nightly toolchain, linking **~89 object files compiled
from genuine Redox source**.

**It is not** an operating system you can boot. CrustOS runs hosted, as an
ordinary program. There is no bootloader, no bare-metal target, no interrupt
handling and no live MMU. Calling it an OS is a claim about *structure*, not
about capability. Hosted micros and ELF load times are **not** RedoxOS-on-QEMU
benchmarks; see [benchmarks/README.md](benchmarks/README.md) methodology.

## Macro-kernel direction

| Redox (micro) | CrustOS (macro-shaped) |
|---|---|
| Scheme daemons in userspace | Scheme routing + handlers in-kernel ([crustos/schemes.py](crustos/schemes.py)) |
| Rust-only drivers | C welcome when faster (e.g. future Haiku NVIDIA path via a `gpu:` scheme) |
| Full userspace ELF + IPC | Hosted ELF load + in-process guest for ET_DYN; ET_EXEC validate-only |

## Static ELF and Crust-ELF

[crustos/elf.c](crustos/elf.c) parses ELF64, maps `PT_LOAD` into a malloc'd
image, charges frames on a `Context`, and for **ET_DYN/PIC** guests marks the
image executable (`mprotect`) and calls the entry. Linux **ET_EXEC** static
binaries are loaded and described but not jumped to (absolute VAs).

**Crust-ELF register hints** (faster context switches):

- Prefer a `PT_NOTE` named `CRUSTOS` with `desc[0] = reg_class` (0..3).
- Also accepted: `e_flags` bits 8..9.
- Classes: 0 = minimal callee-saved (6), 1 = +extras (10), 2 = full GPR (15),
  3 = +xmm0-7 (23). The scheduler sizes its save/restore stand-in from this.

```sh
python3 tools/crust_elf_note.py guest.so --reg-class 1 -o guest_hinted.so
CRUSTOS_ELF=guest_hinted.so python3 tools/crustos.py run --elf guest_hinted.so
python3 benchmarks/crustos_elf/bench_elf_load.py
```

Example guest: [examples/crustos/hello_guest.c](examples/crustos/hello_guest.c).

## The three-way split

| part | language | why |
|---|---|---|
| `vendor/kernel`, `vendor/relibc` | Rust (upstream) | genuine Redox source, compiled by Crust |
| `crustos/schemes.py` | rpython | URL parsing, routing tables, listings (`gpu:` stub included) |
| `crustos/kernel.c` + `elf.c` | Rust + C | frames, contexts, scheduler, syscalls, ELF load |

**rpython** gets text and list work. **Rust** gets fixed layouts and hot loops.
**C** gets `main` and the ELF loader.

## Paging, in upstream's shape

`crustos/kernel.c` uses upstream's `PageFlags<A>` shape; Crust monomorphises
it per architecture. `kernel_heap_offset()` is upstream's, linked from
`vendor/kernel`.

## What comes from upstream Redox

Live survey snapshot (kernel + relibc, after `fetch`): see
[tools/crustos_survey_baseline.txt](tools/crustos_survey_baseline.txt) and
[tools/crustdeep_baseline.txt](tools/crustdeep_baseline.txt).

Approx. **94 of 108** translatable files compile to objects; **~89** link.
What survives is mostly `rmm` and arch consts. Greedy set-cover for remaining
files (via `tools/crustdeep.py`): match-binding, inline-asm, iterator-chain,
nested-generic, std-generic, …

### Why some objects are dropped

Duplicate arch symbols after path flattening, and unresolved refs into parts
of Redox that do not compile. `tools/crustos.py` selects with `nm`.

## Commands

| command | what it does |
|---|---|
| `fetch` | shallow-clone `redox-os/kernel` and `redox-os/relibc` into `vendor/` |
| `fetch --update` | pull existing checkouts |
| `survey` | how much of the source Crust translates, and what stops the rest |
| `survey --verify` | also compile, and report how many succeed |
| `survey --blockers` | rank the failure messages |
| `build` | compile subset + crustos; also build `hello_guest.so` |
| `build --upstream-only` | stop after the upstream objects |
| `run [--elf path]` | build, then run (default guest if present) |
| `clean` | remove `build/crustos` |

`CRUSTOS_VENDOR`, `CRUSTOS_BUILD`, and `CRUSTOS_ELF` override paths / guest.

## Benchmarks and compiler

```sh
python3 tools/crust_benchmarks.py           # feature suite
python3 tools/crust_benchmarks.py rpython   # includes OS-shaped micros
```

Codegen micros under `benchmarks/codegen/` (idiv, mul-pow2, struct) compare
ShivyCX to gcc `-O0`/`-O2`. ShivyCX now strength-reduces **multiply by a power
of two** to `shl`/`sal` (see [OPTIMIZATIONS.md](OPTIMIZATIONS.md)).

## Future: GPU and tile / AI hardware

- **GPU:** a `gpu:` scheme is registered; integrating C driver code (Haiku-style
  NVIDIA accel) is the pragmatic path vs Rust-only.
- **Tile / FPGA:** [examples/rpython2c/nn/](examples/rpython2c/nn/) is the CPU
  golden model (POD + fusion + SIMD contracts). A tile IR / HLS backend is
  not implemented yet; py2c remains the front-end seam for that work.

## Honest limits

Beyond "it does not boot":

**`Mutex` does not lock.** Replace before real concurrency.

**`#[cfg]` is a fixed target** in `shivyc/crust.py`.

**rpython needs the heap** — wrong for ISR / allocator paths.

**ELF guests:** ET_DYN run in-process after `mprotect`; ET_EXEC is
validate-only. Full Linux static ABI (syscalls to CrustOS) is not there yet.

**Redox coverage** is still a fraction of the tree; prefer
`crustdeep` set-cover over headline file percentages. See `CRUST.md`.
