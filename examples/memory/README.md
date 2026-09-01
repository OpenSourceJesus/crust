# Memory safety for unannotated C

Two tiers, over the same corpus. The **static** pass (`--check-memory`) proves
what it can at compile time and emits nothing, so it costs no runtime and needs
no test inputs -- but it reasons about the IL, which carries no source line
numbers, so it reports at function granularity. The **runtime** tier
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
* Diagnostics are reported at function granularity (the IL carries no source
  line numbers), naming the function and the kind of error.
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

| level | what is instrumented |
|-------|----------------------|
| `--mem-safe` / `--mem-safe=all` | everything, including hand-written C |
| `--mem-safe=cpp` | only code lowered from the C++ subset, leaving already-verified C at full speed |

`cpprust.py --mem-safe` is the same tier reached from the C++ side.

## What it catches

| class | example in the fixture |
|-------|------------------------|
| heap/stack buffer overflow | `heap_overflow`, `far_overrun` |
| use after free | `use_after_free` |
| use after scope exit | a local whose address outlives its frame |
| double free | `double_free` |
| free of an interior or non-heap pointer | — |
| read of uninitialized memory | `uninit_read` (tracked per byte, not per object) |
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
  flagging those would drown the report. A far overrun is still caught, because
  the runtime looks for an object ending just below the address.
* Freed records are quarantined so use-after-free can name the free site. The
  table is finite (`CRUST_MS_MAX_REGIONS`); when it fills, the oldest retired
  record is recycled and a use-after-free on *that* allocation is no longer
  attributable.
* Definedness tracking needs a bit per byte from a fixed arena
  (`CRUST_MS_BITMAP_ARENA`, 4 MiB). If it fills, later objects keep bounds and
  liveness checks but lose uninitialized-read detection; the report says so.
* Checks are emitted where the macros appear. Hand-written C only gets them
  where they are written; the automatic path is `cpprust.py --mem-safe` for the
  C++ tier, and an IL-level pass for the C tier once IL commands carry source
  ranges (they do not yet -- the same gap that limits `--check-memory` to
  function-granularity reporting).
* Single-threaded: the region table has no locking.
