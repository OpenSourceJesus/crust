# Crust Metamorphic Returns

Self-hosted toolchain throughout: `rasm` assembles, `rlink` links a
freestanding static ELF at base `0x1000` (every code address < `0x4000`, so a
return address fits in two bytes). Timing is `rdtsc`, medians of several runs;
every variant checks all lowerings computed the same accumulator
(`92129705088`). Run: `python3 run_metaret.py`, `python3 bench_deep.py`.

The first pass used `jmp QWORD PTR [slot]` and concluded narrow writes were
useless. That was an artefact of the memory-indirect jump. The correct shape is
a REGISTER-indirect jump with the target patched into a `mov` immediate:

    site: mov edx, 0x0000   // BA 00 00 00 00 ; low 2 bytes patched
          jmp rdx

No load at the jump, so nothing to store-forward; the write can be as narrow as
the address needs. This section supersedes the earlier store-forwarding claim.

## 1. Slot vs immediate, per-call vs hoisted (single leaf)

| lowering | cyc/call |
|---|---|
| call / ret | ~2.6 |
| SLOT_PC  `jmp [slot]`, 8B write per call | ~4.3 |
| SLOT_HO  `jmp [slot]`, write hoisted | ~4.3 |
| IMM_PC   `jmp rdx`, patch immediate per call | ~410 |
| IMM_HO   `jmp rdx`, patch immediate hoisted | ~3.5 |

* `jmp reg` + hoisted patch (IMM_HO, 3.5) beats the memory-slot method
  (SLOT_HO, 4.3): it pays neither the load nor any forwarding.
* Patching the instruction stream on every call (IMM_PC, 410) is a
  self-modifying-code machine clear -- same disaster as a slot sitting in the
  code's cache line. Hoisting the write out of the loop is mandatory.

## 2. Nested chain A->B->C->D, patches hoisted into A

Only D works; B, C pass through; A accumulates.

| lowering | cyc/iter |
|---|---|
| NCALL   call/ret x4 (RSB-balanced) | ~6.3 |
| NSRET   push;jmp / ret (stackless, RSB desynced) | ~76 |
| NMETA_PC  metamorphic, patch per call x3 | ~790 |
| NMETA_HO  metamorphic, all patches hoisted to A | ~9.7 |

Hoisting is ~80x over per-call patching (790->9.7) and ~8x over the naive
stackless push;ret trampoline (76->9.7). But at shallow depth it does NOT beat
balanced call/ret: the RSB predicts all four returns and the stack traffic is
L1-cheap.

## 3. Where metamorphic actually beats call/ret: chains deeper than the RSB

The return-stack buffer holds ~16-24 entries. Past that, call/ret's outermost
returns are evicted and mispredict (~15-20 cyc each); a monomorphic `jmp rdx`
is BTB-predicted at any depth. Sweeping chain depth (patches hoisted):

| depth | call/ret cyc | meta_HO cyc | winner |
|---|---|---|---|
| 4  |   7.8 |  11.3 | call/ret |
| 16 |  29.1 |  43.5 | call/ret |
| 20 |  36.7 |  53.6 | call/ret |
| 24 |  84.1 |  64.0 | meta |
| 32 | 254.5 |  86.8 | meta (2.9x) |
| 48 | 567.1 | 127.4 | meta (4.5x) |
| 64 | 939.5 | 170.5 | meta (5.5x) |

Crossover at depth ~22-24, matching a ~16-24-entry RSB. Beyond it call/ret
scales super-linearly (mispredicts) while metamorphic stays linear.

## Bottom line for the compiler

* Lower a metamorphic return as `mov reg,imm ; jmp reg`, never `jmp [mem]`.
* When the call graph shows the return sites are loop-invariant, hoist every
  immediate patch out of the loop into the outermost caller's preamble. This is
  the difference between ~400 cyc/call (SMC per call) and ~3.5 cyc/call.
* The narrow low-address write is what makes hoisted patches cheap to set up
  and keeps them to 2 bytes; it does not by itself speed the steady state.
* Payoff regimes: (a) stackless/trampoline code, where balanced call/ret is off
  the table and metamorphic is ~8x the push;ret alternative; (b) call chains
  deeper than the RSB, where metamorphic beats even balanced call/ret, 3-5x by
  depth 32-64. In shallow, stackful code, call/ret still wins -- don't convert
  it.

## Files
* `gen_immret.py`   -- section 1 (slot vs immediate, per-call vs hoisted)
* `gen_nested.py`   -- section 2 (nested chain, hoisting into A)
* `bench_deep.py`   -- section 3 (depth sweep, self-contained runner)
* `gen_metaret_variants.py`, `gen_trampoline.py` -- earlier slot-based studies
* `rbuild.py`       -- assemble with rasm + link with rlink at a chosen base
* `run_metaret.py`  -- build/run the slot-era benchmarks with medians
