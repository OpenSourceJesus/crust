# Memory safety for unannotated C

Two tiers, over the same corpus. The **static** pass (`--check-memory`) proves
what it can at compile time and emits nothing, so it costs no runtime and needs
no test inputs -- but it only reports what it can prove, and it is deliberately
conservative. The **runtime** tier
(`--mem-safe`, below) instruments the build instead: it reports only what a
given run actually executed, but it reports it exactly, with a line number for
the access *and* for the allocation it belongs to.

They answer different questions and are meant to be used together: run
`--check-memory` on every build, `--mem-safe` under the test suite.

C's manual `malloc`/`free` is the classic source of **use-after-free** and
**double-free** bugs. Because ShivyCX sees the entire call graph, a Python pass
(`shivyc/memory_safety.py`) tracks every allocation, pointer copy (alias), and
free across the whole program and:

* **flags use-after-free** — a dereference (or pass to a callee that
  dereferences) of a pointer whose allocation has already been freed, *including
  through aliases and across function boundaries*;
* **flags double-free** — freeing an allocation that is already freed;
* **auto-frees** — when escape/region analysis proves an allocation is local
  with no remaining live reference, the compiler can insert the `free` for you,
  so the programmer may omit it.

This recovers much of Rust's ownership safety for ordinary C, driven by
whole-program reachability rather than by annotations — without Rust's wholesale
change of language.

## Usage

Report only (no code generated):

```
python3 -m shivyc.main examples/memory/dangling_alias.c --check-memory
```

Insert automatic frees during a normal compile:

```
python3 -m shivyc.main examples/memory/autofree_leak.c --auto-free -o leak
```

`--check-memory --auto-free` additionally lists the auto-free candidates without
modifying anything. From the repo root, `make check-memory` runs all four
examples.

## The examples

| file | what it shows | result |
|------|---------------|--------|
| `dangling_alias.c` | the canonical alias-outlives-free bug from the brief | use-after-free |
| `double_free.c` | the same allocation freed twice | double-free |
| `wrapper_uaf.c` | free in one helper, deref in another (whole-program) | use-after-free |
| `autofree_leak.c` | a leak with no escaping reference | auto-free inserts `free` |
| `memsafe_runtime.c` | every class the **runtime** tier detects | `--mem-safe`, see below |

## How it works

The pass runs on ShivyCX's IL — the same `Call.direct_name` / `Set` (alias) /
`ReadAt` & `SetAt` (dereference) commands the other whole-program analyses use,
so it sees aliasing as it actually flows through the generated code.

1. **Per-function dataflow.** A CFG is built from each function's IL. A
   forward, flow-sensitive analysis tracks a *may-points-to* map
   (pointer → set of abstract allocations) and each allocation's state
   (`allocated` / `maybe-freed` / `freed`), merging at control-flow joins.
   `malloc`/`calloc`/`strdup`/… create allocations; `free`/`kfree` free them;
   `Set` propagates aliases; `ReadAt`/`SetAt` are the use sites checked against
   the freed state.

2. **Whole-program summaries.** Functions are analyzed callees-first. Each gets
   a summary — *frees parameter i*, *dereferences parameter i*, *parameter i
   escapes*, *returns an owned allocation* — which is applied at call sites. This
   is what catches the `wrapper_uaf.c` bug and what avoids false positives when a
   function returns ownership (e.g. a `malloc` wrapper).

3. **Escape/region analysis → auto-free.** An allocation is auto-freeable when
   it is created locally and provably never escapes (not returned, not stored
   into a global or through a pointer, not passed to an escaping call) and is
   never already freed on any path. Such allocations are dead at function exit,
   so a `free` is inserted before each `return`.

## Honest limitations

* The analysis is flow-sensitive within a function and **summary-based** across
  functions; it is not fully field- or path-sensitive. Heap-to-heap aliasing
  through stored pointers is treated conservatively (as escape), which favors
  soundness of auto-free (never free something that might be live) over
  completeness (some real leaks are left alone).
* Points-to survives pointer arithmetic, so a use-after-free written as
  `p->field` or `a[i]` is reported. (It was not until the elision work needed
  the same information: the analysis lost the allocation at the first offset
  computation, so `*p` was caught and `p->key` was not.)
* Diagnostics name the use, the allocation, and the free by `file:line`. (They
  were function-granularity until IL commands gained source ranges; `ILCode.add`
  now stamps every command with the position of the construct being lowered.)
  Positions are exact to the statement, and to the sub-expression wherever a
  dereference or a call supplies its own.
* Auto-free **insertion** reuses a `free` reference already present in the
  translation unit; if a unit never frees anything, candidates are still
  reported but not inserted (there is no deallocator symbol to call).
* Conservative by construction: when the analysis cannot prove an allocation is
  dead, it does nothing — it never inserts a free that could create a
  double-free or free an escaping pointer.

---

# `--mem-safe`: runtime checks for test builds

Inspired by Fil-C, minus the ABI change. Fil-C gives every pointer a capability
(InvisiCaps: bounds plus allocation liveness) carried with the pointer, which is
why it needs the whole program rebuilt. Crust keeps the same three facts in a
side table keyed by address (`runtime/crust_memsafe.c`), so an instrumented
translation unit links against uninstrumented C with no ABI break. The cost is a
lookup per access instead of a register compare -- the right trade for a flag
whose entire purpose is to be on under `make test` and off in the release build.

Because the table stores the real size, bounds are **exact**: there are no
redzones to size, and an overflow is caught at the first byte past the end.

## Usage

```
shivyc --mem-safe examples/memory/memsafe_runtime.c -o memsafe && ./memsafe
```

The flag needs nothing else -- no `-I`, no `-D`, no runtime source on the
command line. It puts `CRUST_MEM_SAFE` on, and compiles and links the checking
runtime for you.

Rebuild the *identical source* without the flag and every check macro collapses
to the bare expression, the runtime is not linked at all, and the program runs
at full speed. That is the intended workflow: find the bugs under test, fix
them, ship unchecked.

| level | what is instrumented | how |
|-------|----------------------|-----|
| `--mem-safe` / `--mem-safe=all` | everything, including hand-written C | an IL pass, automatically |
| `--mem-safe=cpp` | only code lowered from the C++ subset | the CRUST_MS_* macros in the generated C |

`cpprust.py --mem-safe` is the `cpp` tier reached from the C++ side.

## Stack objects

Locals are registered once after the prologue and retired at every return, so a
local array overflow is reported against the variable by name and a pointer to
a dead frame is reported as a use-after-scope:

```
crust --mem-safe: stack buffer overflow
  at s1.c:6 in main
    object: 32 bytes, buf (stack)
    offset 36 into a 32-byte object: the write begins 4 bytes past the end
```

Registration goes *after* the prologue because a call clobbers the argument
registers; emitting it above the argument loads hands the runtime's own
arguments to the function and loses the real ones. The prologue is more than
`LoadArg`: a function taking a struct by value begins with `LoadStructArg` and
no `LoadArg` at all, and missing that produced a segfaulting binary.

Which objects get registered is not obvious either. `buf[i]` on an array names
the array object directly as `ReadRel`'s base and never takes its address, so
collecting only `AddrOf` operands found the format string and missed the
buffer. String literals need excluding for the opposite reason: they live in
`.rodata` and are never declared, so they are absent from the storage map that
filters out globals.

## The C tier: automatic instrumentation

`--mem-safe=all` needs no macros and no annotation. It rewrites the IL after
`make_il` (`shivyc/memsafe_il.py`), inserting a check before every dereference
and redirecting `malloc`/`calloc`/`realloc`/`free` to the tracking wrappers:

```
$ cat t.c
#include <stdlib.h>
int main(void) {
    int *a = malloc(4 * sizeof(int));
    a[0] = 1;
    a[4] = 99;
    free(a);
    return 0;
}
$ shivyc --mem-safe t.c -o t && ./t
crust --mem-safe: heap buffer overflow
  at t.c:5 in main
    write of 4 bytes at 0x10cef2b0
    object: 16 bytes, malloc (heap)
            allocated at t.c:3 in main
    offset 16 into a 16-byte object: the write begins at the first byte past the end
```

Working on the IL rather than the source is what makes it complete. `*p`,
`a[i]`, `s->field`, a compound assignment's implicit read-modify-write, a
dereference buried three macros deep -- all have been lowered to the same
handful of IL commands by the time the pass runs, so there is one place to
instrument instead of a dozen syntactic forms to chase.

Two design points worth knowing:

* **Callees are synthesized.** A `Call` with `direct_name` set never
  materializes its function pointer, so the pass can conjure a callee from a
  ctype alone -- the runtime functions need not be declared in the source or
  entered in the symbol table. (`--auto-free` predates this and has to scavenge
  an existing `free` reference out of the IL, which is why it silently does
  nothing in a unit that never frees anything.)
* **Pointers escaping to uninstrumented code are marked defined.** When a
  pointer is passed to a function this build does not instrument -- libc, a
  unit compiled without the flag -- that callee's writes are invisible to the
  shadow, so every later read would be reported as uninitialized. The pass
  emits `crust_ms_il_escape` for such arguments. This costs some detection and
  buys trustworthiness: a false report on every `strcpy` buffer would get the
  flag switched off, and then it catches nothing at all. Bounds and liveness
  are unaffected.

A translation unit that already carries macro checks is left alone, so a file
generated by `cpprust.py --mem-safe` and then compiled with `--mem-safe=all`
gets one check per access rather than two.

## Proving checks away

Crust sees the whole call graph, so it can prove a share of accesses safe at
compile time and emit nothing for them. Fil-C cannot: its capability test is
per-pointer at run time, with no whole-program view to prove anything away.

Three rules (`shivyc/memsafe_elide.py`):

**Rule 1 -- redundancy.** If the identical address was already checked in this
basic block at least as wide, the second check must reach the same verdict.
A read-modify-write is the common case: `a[i] = i; s += a[i];` recomputes the
address into a fresh ILValue, so the two accesses look unrelated until you
number the values. This halves the checks in such a loop, and needs no
whole-program information at all.

**Rule 2 -- provably in bounds.** A *constant* offset into an allocation of
statically known size that is provably live is in bounds by arithmetic. Every
struct field access through `malloc(sizeof T)` has this shape. The static pass
supplies the temporal half -- which allocation the pointer targets and whether
it is still live -- from its fixpoint dataflow; the spatial half is arithmetic.

Rule 2 **downgrades rather than removes**, and the reason is worth stating. A
write check is not a pure predicate: it also records which bytes are now
defined, and that is the only way the runtime tells an uninitialized read from
an initialized one. Removing a proved write made `x->next = h` invisible to the
shadow, so reading `h->next` on the next line was reported as uninitialized --
a false positive manufactured by the optimization meant to reduce noise. A
proved write therefore becomes a bare `crust_ms_mark_init`: the bounds and
liveness work goes, the bookkeeping stays. For the same reason a proved *read*
is never elided, since consulting definedness is all it has left to do.

**Rule 3 -- loop ranges.** `a[i]` inside `for (i = 0; i < N; i++)` is in bounds
on every iteration when `N * scale` fits the allocation. The two halves of the
range come from different places, and both are needed:

* the **upper** bound from the loop guard, credited only to blocks the guard
  actually dominates -- `i < n` on one arm of a branch says nothing on the
  other;
* the **lower** bound from how the counter is written. If every assignment to
  it anywhere in the function is a non-negative constant or an increase by a
  non-negative constant, it never goes below zero on any path. One unexplained
  assignment disqualifies the value, which is what makes it safe without any
  control-flow reasoning.

Rule 3 also needs the base pointer, and the `malloc` that produced it is
usually *before* the loop, in another block. Values assigned exactly once in
the whole function hold the same thing wherever they are live, so their origin
is carried across blocks; anything reassigned (`p = p->next`) is excluded and
stays block-local.

**Hoisting.** A proved write in a counted loop writes one contiguous run over
the whole loop, so a single shadow update in the preheader replaces one per
iteration -- and once bounds and liveness are proved away, that update is the
entire remaining cost. The update has to climb the whole loop nest: an inner
loop's preheader still sits inside the outer loop, and stopping at one level
left it running once per outer iteration. It is emitted only when the write
runs on *every* iteration (its block must dominate the latch), so a write under
an `if` inside the loop is not covered.

The precision cost is that a loop exiting early leaves bytes marked defined
that were never written -- a missed report rather than a false one, the same
direction the escape rule already trades in.

Measured on a 2M-iteration array read-modify-write loop, overhead went **31x →
15x → 12x → 1.1x**, and a struct-field loop from 29x to 1.0x as the rules and hoisting landed. The build reports what it did:

```
--mem-safe: hot.c: 0 check(s) emitted, 1 removed, 0 downgraded (100% avoided),
2 allocator call(s) redirected, 0 stack object(s) tracked, 1 shadow update(s)
hoisted out of loops
```

The loop body ends up with no runtime calls at all. The runtime also caches its
last region lookup, which matters for everything that is *not* proved away:
accesses cluster hard (a loop walks one array, a function works on one object)
and the one-entry cache was worth 15x → 12x on its own before hoisting landed.

Real programs see much less. The repo's own examples avoid 0-14% of their
checks, because they work through stack objects and pointers whose provenance
the pass cannot see. The dramatic numbers are for heap arrays walked by counted
loops, which is what the rules were built for.

Soundness is the whole game here, because a wrong proof is silent: the build
succeeds, the program runs, and a real bug goes unreported, which looks exactly
like a clean program. Nothing is carried across a basic block boundary (IL
values are not SSA), value numbers are reissued on every definition, and any
call invalidates every liveness fact, since a callee may free anything.
`tests/test_mem_safe_elide.py` re-tests every detection class through code
shaped to trigger elision.

## What it catches

| class | example in the fixture |
|-------|------------------------|
| heap/stack buffer overflow | `heap_overflow`, `far_overrun` |
| use after free | `use_after_free` |
| use after scope exit | a local whose address outlives its frame |
| double free | `double_free` |
| free of an interior or non-heap pointer | — |
| read of uninitialized memory | `uninit_read` (tracked per byte, not per object) |
| heap/stack buffer underflow | an access before an object's base |
| null dereference | — |
| leaks still live at exit | `leak` |

## Why the messages look like this

```
crust --mem-safe: heap buffer overflow
  at examples/memory/memsafe_runtime.c:22 in heap_overflow
    while evaluating `a[4]`
    write of 4 bytes at 0x34f1a2b0
    object: 16 bytes, malloc (heap)
            spans 0x34f1a2a0 .. 0x34f1a2b0
            allocated at examples/memory/memsafe_runtime.c:19 in heap_overflow
    offset 16 into a 16-byte object: the write begins at the first byte past the end
```

Unchecked, the same program gets you `free(): double free detected in tcache 2`
and an abort with no location at all. Naming the allocation site as well as the
access site is most of the value: the bug is usually at the allocation, and the
access is only where it surfaced.

A run reports **every** distinct error rather than dying on the first, since one
test run should surface every bug it can reach. Repeated firings of the same
site are collapsed into a count, so a bug inside a loop reads as one bug.
`crust_ms_set_halt_on_error(1)` restores stop-at-first.

The process exit status is forced to 1 when anything was reported. Without that
`make test` stays green while the report scrolls past.

## Honest limitations

* An address in **no** tracked object is not an error. Under partial
  instrumentation most pointers point into memory owned by uninstrumented C, and
  flagging those would drown the report. An overrun is still caught, because the
  runtime looks for an object ending just below the address -- and an underflow
  too, by looking for one starting just above it. (`a[-2]` was invisible until
  that second search existed: it lands in no region, so the rule above let it
  through, it corrupted the allocator's metadata, and the program died later
  inside `free()` with no location.)
* Freed records are quarantined so use-after-free can name the free site. The
  table is finite (`CRUST_MS_MAX_REGIONS`); when it fills, the oldest retired
  record is recycled and a use-after-free on *that* allocation is no longer
  attributable.
* Definedness tracking needs a bit per byte from a fixed arena
  (`CRUST_MS_BITMAP_ARENA`, 4 MiB). If it fills, later objects keep bounds and
  liveness checks but lose uninitialized-read detection; the report says so.
* Globals are not registered, so an overflow of a global array is only caught
  if it runs into a tracked object. Heap and stack objects are covered.
* Definedness is tracked for local **arrays** only. A scalar or struct is
  assigned wholesale by `Set` and a parameter arrives via `LoadArg`; neither is
  a memory access, so neither is instrumented, and registering those as
  undefined reported a false uninitialized read on the first use of every
  by-value parameter. Their bounds and scope are still checked.
* Uninitialized-read detection stops at the boundary with uninstrumented code
  (see the escape rule above).
* Checks the compiler cannot prove away are still uniform. Expect roughly 15x
  on an array read-modify-write loop and 30x where nothing is provable.
* Rule 2 (below) only fires on *constant* offsets. `a[i]` with a runtime index
  needs a loop range analysis, which does not exist yet and is where the next
  large win is.
* Single-threaded: the region table has no locking.
