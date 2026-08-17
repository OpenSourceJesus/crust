# Metamorphic returns (`-fmetamorphic`, experimental)

This is an advanced, intentionally-unsafe option ported from the
"metamorphic return site" idea in the reference `arx86.py`. It is opt-in twice:
the `-fmetamorphic` flag enables the machinery, and the `__metamorphic__`
specifier marks which functions use it.

## What it does

A `__metamorphic__` function returns **without using the stack for the return
address**. Instead:

* Each function gets an 8-byte return slot in writable `.data` -- a separate
  page from any code. The function body stays in ordinary read-execute `.text`.
* Each caller writes its own return address into that slot and then **jumps**
  into the function (no `call`).
* The function returns by **jumping through the slot** (no `ret`).

So no return address for a metamorphic function is ever pushed or popped. The
slot is self-modified at run time, which is why it lives in writable data.

```asm
; in .data (writable, a separate page from the code):
helper__metaret:
    .quad 0                                  ; the return slot

; in .text (ordinary read-execute):
helper:
    ...                                      ; body, result in eax
    jmp QWORD PTR [rip + helper__metaret]    ; "return"

; at a call site:
    lea  r11, [rip + .Lret]
    mov  QWORD PTR [rip + helper__metaret], r11   ; patch the slot
    jmp  helper                                   ; jump, don't call
.Lret:
    ...                                           ; result is in eax
```

## Where the slot lives, and why not next to the code

An earlier version of this feature placed the slot in a writable+executable
`.mtext` section *immediately before* the function's entry label -- "memory near
the function". That was a mistake: the slot then shared a cache line with the
code the CPU was fetching, so the caller's store into it triggered a
self-modifying-code machine clear on **every call**. Measured, that layout ran
~two orders of magnitude slower than an ordinary `call`/`ret` (see the
`## Performance` section and `tools/metamorphic/`).

Moving the slot into ordinary writable `.data`, off the code page, removes that
stall entirely and brings the return to `call`/`ret` parity. It also means the
feature no longer needs a writable+executable segment at all: the body is plain
`R+X` text and the slot is plain `R+W` data, so `-fmetamorphic` no longer asks
the linker for RWX memory (and no longer trips the linker's RWX-segment
warning). The `-N`/OMAGIC linker mode the old design flirted with is likewise
unnecessary.

## Correctness and limitations

This feature is **experimental**. It is correct only within clear limits:

* **Not re-entrant.** There is a single static return slot per function, so a
  metamorphic function must not be active twice at once. The compiler builds
  the (direct) call graph and **refuses to compile** a metamorphic function
  that can reach itself -- directly or transitively -- with a clear error
  rather than emitting code that would corrupt the slot:

  ```
  error: metamorphic function 'fact' may be re-entered (recursion);
         not supported
  ```

* **Leaf-friendly.** The demo and tests use leaf metamorphic functions. A
  metamorphic function that itself makes ordinary calls is not exercised here.

* **Self-modifying data.** The return slot is self-modified at run time (the
  caller patches it before each jump). It lives in ordinary writable `.data`, so
  no writable+executable memory is involved -- the body is plain read-execute
  text. The remaining unsafety is the re-entrancy constraint above, not an RWX
  segment.

Without `-fmetamorphic`, the `__metamorphic__` specifier is ignored entirely
and ordinary call/ret code is generated, so a program behaves identically.

## Performance: getting the lowering right

The shape shown above -- an 8-byte return slot sitting in the same section, and
the same cache line, as the function's code, rewritten on every call -- is the
*worst* way to do this. A store into the cache line the CPU is currently
fetching instructions from triggers a self-modifying-code machine clear, and in
a tight loop that clear happens on every call. Measured on the microbenchmarks
in `benchmarks/metamorphic/` (self-hosted `rasm`+`rlink`, `rdtsc`-timed; the
generators and the full write-up are in `tools/metamorphic/`):

| lowering                                   | cyc/call | note |
|--------------------------------------------|----------|------|
| ordinary `call`/`ret`                      | ~2.7     | reference |
| slot in code's cache line, 8B write/call   | ~410     | the old `.mtext` layout -- SMC clear (removed) |
| slot moved off-page, 8B write/call         | ~4.4     | no SMC; the `jmp [slot]` load costs a little |
| **`jmp reg`, immediate patched + hoisted** | **~3.5** | fastest; near `call`/`ret` parity |
| `jmp reg`, immediate patched every call    | ~410     | SMC again -- the patch must be hoisted |

Three lessons drive the planned lowering:

1. **Put the return state off the code page.** A slot cache-line-adjacent to
   executing code is the entire ~150x penalty; a writable data page (or, for
   the register form below, an instruction patched once) removes it.

2. **Return through a register, not memory.** `jmp QWORD PTR [slot]` performs an
   8-byte load at the return; a narrower store into that slot cannot be
   store-forwarded to the wider load and stalls. The faster form loads the
   target into a register and does `jmp reg`. The address is written into the
   immediate of the `mov reg, imm32` that precedes the jump:

   ```asm
   site: mov  edx, 0x0000        ; BA 00 00 00 00 ; low 2 bytes are the target
         jmp  rdx                 ; register-indirect return, BTB-predicted
   ```

   Because the image is based low (every code address fits in two bytes), only
   the low two bytes of the immediate ever change.

3. **Hoist the patch out of the loop.** Patching that immediate on every call
   is self-modifying code again. But when the call graph shows the return site
   is loop-invariant -- the same chain runs every iteration -- the compiler can
   write all the immediates *once*, in the outermost caller's preamble, before
   the loop. The loop body is then pure register-indirect jumps to constants,
   with no store into code at all. This is what turns ~410 cyc/call into ~3.5.

## When it pays off

Even done perfectly, a metamorphic return only *ties* a balanced
`call`/`ret` in ordinary, shallow, stackful code: the CPU's return-stack buffer
(RSB) already predicts a matched `ret` essentially for free, and the stack
push/pop is L1-cheap. **In that regime, do not convert -- `call`/`ret` wins.**

There are two regimes where it is a real win:

* **Stackless / trampoline code** (`-fstackless-calls`), where a worker is
  *jumped* into rather than called, so no RSB entry is pushed. An ordinary
  `ret` there pops a stale RSB entry and mispredicts every time (~29 cyc/call
  in the benchmark); the metamorphic `jmp reg` is BTB-predicted and stays at
  ~3.6 -- about **8x faster** than the naive `push;ret` trampoline.

* **Call chains deeper than the RSB.** The RSB holds ~16-24 entries; past that,
  `call`/`ret`'s outermost returns are evicted and mispredict, while a
  monomorphic `jmp reg` is predicted at any depth. The crossover is around
  depth 22-24, and it widens fast:

  | chain depth | call/ret | metamorphic | speedup |
  |-------------|----------|-------------|---------|
  | 16          | ~29      | ~44         | 0.7x (call/ret wins) |
  | 24          | ~85      | ~64         | 1.3x |
  | 32          | ~254     | ~87         | 2.9x |
  | 64          | ~940     | ~171        | 5.5x |

  (cycles per outer iteration; `tools/metamorphic/bench_deep.py`.)

## Versus inlining

The usual way a compiler removes call/return overhead is **inlining**: paste the
callee into the caller and the `call`/`ret` disappears. It is the right tool for
small, hot, statically-known callees, and metamorphic returns are not a
replacement for it. But inlining is a technique that gets *worse* as functions
get larger:

* **Code-size and i-cache blow-up.** Inlining a large callee at many sites
  multiplies its body; deep inlining of a chain multiplies multiplicatively.
  Past a size threshold every compiler stops inlining precisely to avoid this.
* **It cannot cross certain boundaries** -- indirect/virtual calls, recursion,
  and separately-compiled units -- without whole-program information.
* **It does not deepen the RSB.** Where a call chain is deep because the
  *logic* is deep (interpreters, driver stacks, continuation chains), inlining
  either can't flatten it or would explode code size doing so, and the RSB
  mispredictions remain.

A metamorphic return is orthogonal and size-independent: it is a few bytes
regardless of how large the callee is, it works across indirect and
separately-compiled calls, and its advantage *appears* exactly in the deep-chain
case where inlining and the return predictor both give out. The two compose --
inline the small leaves, use metamorphic returns for the deep structural chain.

## Multi-call-site functions, threads, and the compatibility roadmap

The current single static slot -- and the single patched immediate in the
register form -- is correct only for **one logical caller active at a time**.
Two things break that, and the plan for each:

* **Multiple call sites needing different return targets.** A function reached
  from several places cannot share one hoisted immediate. Two options: emit a
  small dispatch on a caller id, or **specialise per call site** -- clone the
  callee once per site (`W$site0`, `W$site1`, ...) so each clone has its own
  loop-invariant return immediate and the hoisting still applies. Cloning
  trades code size for keeping every return a predicted constant jump; the
  compiler picks per site based on how hot the site is.

* **Threads.** A single static slot is shared state, so two threads in the same
  metamorphic function would corrupt each other's return address. Because this
  compiler can see the **entire** call graph -- OS, drivers, and user code, and
  every thread's entry point -- it can generate a **per-thread copy of each
  metamorphic function**: `A_thread0`, `A_thread1`, ... Each thread calls its
  own clones, so each has private return slots/immediates and there is no
  cross-thread contention and no re-entrancy between threads by construction.
  This makes metamorphic returns thread-safe without any runtime locking, at
  the cost of one function body per thread that uses it. Whole-program
  knowledge is what makes this tractable: the compiler knows exactly which
  functions each thread can reach and clones only those.

* **Recursion / re-entrancy within a thread** remains refused today (single
  slot). A per-thread slot *stack* -- a tiny near-function array indexed by a
  depth counter -- would lift that limit for bounded recursion; until then the
  call-graph check stands.

**Done (shipped).** The return slot now lives off the code page, in ordinary
writable `.data`, reached by `jmp [slot]`. This is the ~410 -> ~4 cyc/call
change, it removes the RWX segment, and it brings shallow metamorphic returns to
`call`/`ret` parity.

Planned lowering and compatibility work, in rough order:

1. Switch the return to a register-indirect `mov reg,imm ; jmp reg` whose
   immediate is patched, and hoist that patch out of loop-invariant loops. This
   is the further ~4 -> ~3.5 cyc/call step, and the form that unlocks the
   deep-chain wins over `call`/`ret`. It depends on (2).
2. A call-graph loop-invariance pass that hoists the immediate patches into the
   outermost invariant caller's preamble.
3. Per-call-site specialisation (cloning) for multi-site functions, and
   per-thread cloning driven by the whole-program thread graph.
4. Integration with `-O4` near-scratch storage (`NEAR_SCRATCH.md`) so the slots
   and any recursion stack live in the same per-function static region.
5. ARM64: the same idea lowers to patching the immediate of a `MOVZ`/`MOVK`
   pair before a `BR` register branch. The AArch64 back end is currently
   conservative about indirect calls (see `ARM64.md` / `RISCV64.md`), so this is
   future work.


## Interaction with stackless

A call to a metamorphic function returns to its call site, so it must never be
turned into a tail jump (which would drop the return). When both
`-fstackless-calls` (or `-O4`) and `-fmetamorphic` are active, the stackless
pass is told not to tail-eliminate calls to metamorphic callees. The two
features otherwise compose: verified combined returns the correct result.

## Relationship to `-O4`

`-O4` turns on whole-program stackless lowering and near-function scratch
storage (see `NEAR_SCRATCH.md`), which moves register spills off the stack into
a static per-function buffer. The metamorphic return slot is a related use of
per-function static storage -- self-modified state the caller patches -- but is
opt-in separately via `-fmetamorphic`. (It lives in writable `.data`; only
`-O4` near-scratch still asks the linker for a writable text segment.)

## Verification

* `helper(10) + helper(a)` returns 35 with `-fmetamorphic` and 35 without it
  (matching gcc), and the assembly shows the slot-based return and the
  patch-and-jump call sequence.
* Recursive metamorphic functions are refused at compile time.
* `tests/test_metamorphic_simd.py::TestMetamorphic` and
  `tests/general_tests/extensions/metamorphic.c` cover these.
