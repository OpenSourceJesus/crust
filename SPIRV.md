# SPIR-V back end — plan

Status: **Phase 1 (primitives) is implemented; the compiler back end is not started.**
`make test_spirv` runs the writer, validator and interpreter tests. This file is the working plan for
`--target spirv`: translating the GPU-safe subsets of C, Rust, C++ and C# into
SPIR-V so the same source can run on a GPU.

## 1. Goal and non-goals

**Goal.** Take a function written in the Crust subset of C, Rust, C++ or C#,
mark it as a compute kernel, and get a `.spv` module that a Vulkan driver
accepts — with no glslang, no LLVM, no `spirv-tools` in the build path, which is
how the wasm back end already works ([WASM.md](WASM.md)). The same source also
compiles natively, so it runs on the CPU with identical results (modulo the
float rules in §6.5).

**Non-goals for the first milestones.**

* Graphics stages (vertex / fragment / ray tracing). Compute first.
* The whole language. As everywhere in Crust, the subset **refuses** what it
  cannot lower, with the reason and the replacement in the diagnostic, and the
  refusals are tested as pinned behaviour.
* Beating a hand-tuned shader. Correct and predictable first; fast second.

## 2. What the repo already gives us

Findings from reading the tree (not assumptions):

| Fact | Where | Why it matters |
|---|---|---|
| Every front end lowers to plain C, then one IL | `README.md`; `tools/cs2cpp.py` → `tools/cpprust.py` → ShivyCX; `shivyc/crust.py` for Rust | **One SPIR-V back end on the IL serves all four languages.** No per-language SPIR-V work. |
| A non-native back end already exists and is the template | `shivyc/wasm.py`, `_make_asm_wasm` hook in `shivyc/asm_gen.py`, `Target.is_binary` / `output_ext` in `shivyc/targets/__init__.py`, the `is_binary` branch in `shivyc/main.py` | Binary writer, no registers, structured-control-flow problem, memory-model decisions — all solved once already and documented. |
| The wasm doc names the missing piece | WASM.md, "How control flow is encoded": a `br_table` dispatcher now, a relooper as "the obvious follow-up" | SPIR-V needs the follow-up (§6.2), not just the dispatcher. |
| Decoder next to encoder, plus a reference oracle | `shivyc/wasm_reader.py`, `tools/wasm2c.py`, `tools/wasm_difftest.py`, `tools/wasm_fuzz.py` | The verification pattern to copy (§8). |
| The IL is small | `shivyc/il_cmds/`: Add/Subtr/Mult/Div/Mod, shifts, bit ops, compares, Label/Jump/JumpZero/JumpNotZero, Return, Call, Set, AddrOf, ReadAt/SetAt, ReadRel/SetRel/AddrRel, LoadArg, LoadStructArg | A bounded list to lower. Variadics, `InlineAsm`, `setjmp` are simply refused. |
| Declarations-to-the-compiler already have an idiom | `assert io_thread in threads.left(core=0)` (`shivyc/thread_contracts.py`, `shivyc/extensions.py`) | Kernel declaration should follow it (§5), and it is language-independent because it lives at the C level. |
| Headers are owned, not borrowed | GLES2.md: `shivyc/include/` is the only include path | A `crust_gpu.h` with a **CPU fallback** fits the model (§7). |
| Memory-safety machinery with elision | `shivyc/memory_safety.py`, `memsafe_elide.py` | Optional bounds-checked buffer access that is proven away where possible (§6.3). |
| Whole-program call graph + inliner | `shivyc/callgraph.py`, `shivyc/inline.py` | Recursion check is nearly free. **But** the inliner only takes leaf functions that touch no memory — the GPU path needs a stronger one. |
| GPU work already exists, in GLSL | `UNITY_PACK_GPU.md`; GLES 3.1 bitonic-sort compute shader embedded in `examples/unity_pack/gles3_batch.h`; "GPU-driven culling" listed as the long-term path | Real first consumers, and a concrete first corpus. That doc also says Vulkan/SPIR-V "stays out until a second backend exists" — this plan is that second backend. |
| No SPIR-V tooling in this sandbox | `spirv-val`, `spirv-dis`, `glslang`, `vulkaninfo` all absent; `node`, `clang`, `gcc` present | We cannot lean on external validators. We ship our own reader/validator and a reference interpreter (§8). |
| Compiler modules are self-host friendly | `wasm_reader.py` header: "plain loops and instance attributes only, so the restricted-Python translator can lower this" | New modules follow the same restricted style. |

## 3. Architecture

```
foo.cs  ─▶ tools/cs2cpp.py ─▶ tools/cpprust.py ─┐
foo.cpp ─────────────────────▶ tools/cpprust.py ─┤
foo.rs  ─▶ shivyc/crust.py ──────────────────────┼─▶ C ─▶ IL ─┬─▶ x86-64 / arm64 / …   (CPU, unchanged)
foo.c   ─────────────────────────────────────────┘            └─▶ shivyc/spirv_gen.py ─▶ .spv   (new)
                                                                      │
                                              kernel subset check ◀───┤
                                              inline + call-graph ◀───┤
                                              structurizer ◀──────────┘
```

**Decision: a new back end on the IL, not a new front end and not GLSL
emission.**

* A separate C#/C++/Rust → SPIR-V front end would triple the work and drift
  from the CPU semantics.
* Emitting GLSL and shelling out to glslang breaks the "no external tools" rule
  that every other Crust target keeps.
* One back end on the IL means the CPU build and the GPU build of a kernel
  share a parser, a type checker and a layout engine, so they can be
  differentially tested (§8).

New pieces:

| File | Role |
|---|---|
| `shivyc/targets/__init__.py` | `SpirvTarget`: `name="spirv"`, `is_binary=True`, `output_ext=".spv"`; aliases `spirv`, `spv`, `vulkan` |
| `shivyc/spirv.py` | Module writer: word framing, ID allocation, the module-layout order the spec demands, an opcode table for the subset only |
| `shivyc/spirv_gen.py` | IL → SPIR-V lowering (kept out of `asm_gen.py`, which is already ~7.6k lines; `asm_gen.py` gets only the hook beside `_make_asm_wasm`) |
| `shivyc/spirv_struct.py` | CFG structurizer (§6.2) |
| `shivyc/spirv_reader.py` | Decoder, disassembler, and a validator for the subset we emit |
| `tools/spirv_interp.py` | Reference interpreter: runs a kernel over a dispatch grid on the CPU |
| `tools/spirv_difftest.py`, `tools/spirv_fuzz.py` | Kernel corpus and random kernels vs the native build |
| `shivyc/include/crust_gpu.h` | Intrinsics + CPU fallback (§7) |
| `tests/test_spirv_*.py`, `make test_spirv` | Pinned behaviour, refusals included |

## 4. The GPU subset

SPIR-V for Vulkan compute (`Shader` capability, `Logical` addressing) has no
general pointers, no heap, no recursion, no function pointers, and requires
structured control flow. The subset is what survives that, enforced as a pass
that runs before lowering and **refuses with a replacement**.

| Construct | In kernels | Notes |
|---|---|---|
| int/uint 8–64, `float`, `double`, `bool` | yes | 8/16/64-bit and `double` need capabilities; checked against the target environment (§6.4) |
| `if`, `while`, `for`, `do`, `switch`, `break`, `continue`, early `return` | yes | Structurizer (§6.2) |
| `goto` | only if it keeps the CFG reducible | Else refused |
| structs, fixed-size arrays | yes | Explicit offsets from the C layout (§6.4) |
| pointers | restricted | Must resolve to a buffer binding, a local, or workgroup memory (§6.3) |
| calls | yes | Non-recursive, whole-program, inlined by default |
| recursion, function pointers, virtual dispatch | **no** | Refused. Devirtualisation of a closed hierarchy is a later option |
| heap: `malloc`, `new`, `Vec`, `std::vector`, `List<T>`, `Dictionary`, `string` | **no** | Refused; replacement is a fixed-capacity array or a buffer parameter |
| libc, varargs, `setjmp`, inline asm, threads, I/O | **no** | Refused |
| `except` / `raise` (checked model) | later | Needs a defined meaning for "error" on a GPU; not in the MVP |
| `<math.h>` | yes | Maps to the `GLSL.std.450` extended instruction set; precision caveat in §6.5 |

### Per-language surface

All four reduce to the same lowered C, so this is about which source shapes
become which kernel parameters.

| Language | Kernel | Buffer parameter | Notes |
|---|---|---|---|
| C | contract line (§5) | `T *p` with `len`/access contracts | Reuses the existing `assert len(p) >= N` contract grammar |
| Rust | `#[kernel(local_size = 64)]` | `&[T]` / `&mut [T]` (already lowered to `crust_slice_*` in `shivyc/crust.py`) | A slice is exactly "binding + runtime-array length". Best-fitting source form. `Vec`, `Box`, `Drop` types refused. |
| C++ subset | `[[crust::kernel(64)]]` or the contract line | `T *`, a span-like wrapper later | No `std::vector`, no virtual |
| C# subset | `[Kernel(LocalSize = 64)]` | `T[]` parameter | `cs2cpp` already carries attributes through (`[MaxInstances]`, `[StructLayout]`) and strips the rest, so the precedent exists. Arena classes (`[MaxInstances(N)]`, a reference is a `T*` into static slots) map naturally to one SSBO per arena. |

## 5. Declaring a kernel

Use the Crust idiom — a contract between the parameter list and the body — so
it works identically for every language:

```c
void saxpy(float *x, float *y, float a, unsigned n)
assert saxpy in gpu.compute(local_size=64)
assert x in gpu.buffer(binding=0, readonly)
assert y in gpu.buffer(binding=1)
assert len(x) >= 1
{
    unsigned i = gpu_global_id(0);
    if (i < n) y[i] = a * x[i] + y[i];
}
```

Per-language attributes (§4 table) are sugar that lowers to these lines. This
needs `shivyc/extensions.py` to learn `gpu.compute`, `gpu.buffer`,
`gpu.push` (push constants) and `gpu.shared` (workgroup memory), recorded the
same way `threads.left` is.

## 6. Back-end design

### 6.1 Module shape

One `OpEntryPoint GLCompute` per kernel, `OpExecutionMode LocalSize`, one
descriptor set, one binding per buffer parameter, scalars in a push-constant
block. Environments: `vulkan1.1` (baseline) and `vulkan1.2`; capabilities are
requested from what the kernel actually uses, never a fixed superset.

### 6.2 Control flow — the hard part

The Vulkan `Shader` capability requires structured control flow
(`OpSelectionMerge`, `OpLoopMerge`, one exit per construct, merge/continue
blocks that dominate correctly). The IL is an arbitrary CFG
(`Label` / `Jump` / `JumpZero` / `JumpNotZero`).

Do it in two steps, the same order the wasm back end took:

1. **Dispatcher first (correct for any CFG).** One `OpLoopMerge` loop whose
   body is an `OpSwitch` on a `state` variable, one case per basic block, each
   ending by setting `state` and continuing. This is the SPIR-V twin of the
   wasm `loop` + `br_table` encoding, and it is legal structured SPIR-V. It is
   slow on a GPU (every branch becomes a divergent switch) but it lets Phases 2
   and 4 proceed before the structurizer exists, and it stays as the fallback
   for CFGs the structurizer cannot handle.
2. **Real structurizer.** Dominator tree, natural-loop detection, reducibility
   check, then emission of `if`/`loop` with merge blocks. `break`/`continue`/
   early `return` need care: early return becomes a branch to a single function
   exit; a `break` out of nested constructs follows the structured-CF rules
   (the "Beyond Relooper" dominator-tree formulation is a good reference).
   Irreducible CFGs (a `goto` into a loop) fall back to the dispatcher or are
   refused, selectable by flag.

Enforcement: the validator in `spirv_reader.py` checks the structured-CF rules
independently of the emitter, so a structurizer bug is caught by us, not by a
driver crash.

### 6.3 Pointers and memory — decide once

C is full of pointers; Logical-addressing SPIR-V has none. Three strategies,
**pluggable behind one interface** in `spirv_gen.py`:

| Strategy | Idea | Pros | Cons |
|---|---|---|---|
| **typed** (MVP default) | Every pointer-typed IL value is resolved statically to *(buffer binding \| local \| workgroup var) + element index*; emit `OpAccessChain` | Idiomatic, fast, portable Vulkan 1.1 | Needs provenance analysis; ambiguous pointers are **refused** |
| **linear** | One `uint[]` SSBO as linear memory; a pointer is a 32-bit byte offset, exactly the wasm back end's model (shadow stack, static data) | All of C works; reuses the wasm design | Sub-word access via shift/mask; no per-buffer typing; needs a per-invocation stack region; slower |
| **bda** | `PhysicalStorageBuffer64` + `bufferDeviceAddress`: real 64-bit pointers | Closest to C, pointer arithmetic via `OpConvertPtrToU` / `OpConvertUToPtr` | Needs Vulkan 1.2 + the feature; not every device has it |

Recommended order: **typed → linear fallback → bda opt-in**
(`--gpu-memory=typed|linear|bda`). Typed matches the real corpus (kernels over
buffers); linear is the safety net that makes "any C" run, as the wasm back end
did first.

Pointer handling falls out of the same rule: provenance resolves only if the
callee is inlined into the kernel, which is why the stronger inliner (§3
table) is a prerequisite for typed.

**Bounds checking** (optional `--gpu-bounds-check`): runtime-array length comes
from `OpArrayLength`; the existing `memory_safety.py` / `memsafe_elide.py`
machinery can emit checks and prove many of them away from `assert len(p) >= N`
contracts. Without it, behaviour on out-of-range access is whatever the driver
does (robust-buffer-access or undefined).

### 6.4 Types and layout

* **Layout is a contract, not a coincidence.** `UNITY_PACK_GPU.md` already
  records the std140 trap (`vec3` arrays pad to 16 bytes, so `float[N][3]` does
  not match). Emit explicit `Offset` / `ArrayStride` decorations computed from
  the C layout, and require scalar-block-layout compatibility (core in Vulkan
  1.2, `VK_EXT_scalar_block_layout` before). A struct that is not expressible is
  refused. A layout test compares our offsets to `offsetof` the way
  `tools/gles2_header_test.py` compares constants.
* **Pointers are not values.** In `typed` mode a "pointer" is an
  (object, index) pair resolved at compile time; in `linear` it is a `u32`.
  Compare the wasm back end carrying pointers as i64-holding-32-bits so
  `sizeof(void*)` stays 8 in the front end.
* **Narrow types.** `char`/`short` in buffers need `StorageBuffer8/16BitAccess`;
  otherwise pack into `uint` and shift/mask. `long long`/`double` need `Int64`/
  `Float64`. Capability set is derived, then checked against the chosen
  environment, with a refusal if unavailable.
* **Signedness.** SPIR-V integers are signless; the lowering picks
  `OpSDiv`/`OpUDiv`, `OpSRem` (C `%`), `OpShiftRightArithmetic`/`Logical` from the
  C type.

### 6.5 Numerics that need a stated decision

State these now, the way WASM.md states `trunc_sat`:

* **Shift counts**: SPIR-V leaves a count ≥ width undefined. Mask the count, as
  `cs2cpp` already does for C#.
* **Divide / mod by zero**: undefined in SPIR-V; keep C's "undefined" but
  guarantee no GPU hang (a defined result is cheap insurance; decide which).
* **Float → int on overflow/NaN**: pick saturating, matching the wasm choice,
  and register it as a known XFAIL against x86-64 in the difftest.
* **NaN comparisons**: C's `!=` is unordered; use `OpFUnordNotEqual` and the
  ordered variants for the rest.
* **FMA contraction**: Vulkan may fuse multiply-add. A `--gpu-strict-fp` flag
  decorates arithmetic `NoContraction` for bit-parity runs.
* **Transcendentals**: `sin`/`cos`/`pow`/`exp` on GPUs are not bit-identical to
  libm. Difftest compares floats within a stated ULP bound and compares
  integer/bit results exactly.

## 7. Intrinsics and the CPU fallback

`shivyc/include/crust_gpu.h` (an owned header, like the GLES2 ones):

```c
unsigned gpu_global_id(int dim);   unsigned gpu_local_id(int dim);
unsigned gpu_group_id(int dim);    unsigned gpu_num_groups(int dim);
void     gpu_barrier(void);        /* workgroup barrier + memory barrier */
/* atomics: gpu_atomic_add / min / max / cas / exchange, on buffers and shared */
```

Under `--target spirv` these are recognised and lowered to `BuiltIn` inputs /
`OpControlBarrier` / `OpAtomic*`. Under any native target the header supplies a
**sequential CPU implementation** (a loop over the dispatch grid; barriers handled by
running phases between barrier points), so the **same source** is the oracle for
its own GPU build. Barriers inside divergent control flow are refused (a
validity rule on the GPU and a footgun on the CPU).

## 8. Verification

No `spirv-val` here, and we should not depend on one existing anywhere. Three
independent layers, plus optional real-driver layers that skip with a stated
reason when absent (the `gles2_header_test.py` convention).

1. **Our validator** (`spirv_reader.py`): binary decode, ID defined-before-use
   and dominance, type rules, storage-class rules, structured-CF rules, "no
   recursion", capability/extension consistency. Also round-trip: encode →
   decode → re-encode is byte-identical (`wasm_roundtrip.py` model).
2. **Reference interpreter** (`tools/spirv_interp.py`): executes our emitted
   subset over a dispatch grid with real workgroup/barrier semantics.
3. **Difftest vs native** (`tools/spirv_difftest.py`): each corpus kernel is
   built twice — SPIR-V run in the interpreter, and the same C through the
   CPU fallback built by ShivyCX *and* by gcc as the second oracle — and the
   output buffers must match (exact for ints, ULP-bounded for floats). Refuses
   to pass on all-zero output (the blank-image rule from GLES2.md).
4. **Fuzz** (`tools/spirv_fuzz.py`): random kernels, patterned on
   `tools/wasm_fuzz.py` / `tools/crustfuzz.py`.
5. **Mutation tests** for the validator and the layout check (inject a wrong
   offset, a missing merge block, a bad capability) — the GLES2 `--mutate`
   lesson.
6. **Optional**: `spirv-val` / `spirv-dis` if installed; **Mesa lavapipe**
   (software Vulkan) for end-to-end runs in CI; real GPUs on request.

## 9. Phases

Each phase has an exit criterion that is a test, not a feeling.

| Phase | Deliverable | Exit criterion |
|---|---|---|
| **0. Lock** | This plan agreed; §10 decisions made; a corpus of ~10 kernels written (saxpy, prefix-sum step, histogram, bitonic-sort step lifted from `gles3_batch.h`, a culling test, matmul tile) | Corpus builds natively and passes its own CPU-fallback self-check |
| **1. Primitives** ✅ | `shivyc/spirv.py` writer, `shivyc/spirv_reader.py` decode/disasm/validate, `tools/spirv_interp.py`, hand-built `tools/spirv_examples.py` (`vecadd`, `saxpy_loop`, `reduce`) | **Done:** the three examples round-trip, validate and run; `tests/test_spirv_prims.py` (48 tests, incl. validator mutation tests) via `make test_spirv` |
| **2. Skeleton back end** | `SpirvTarget`, `spirv_gen.py` for scalars + typed buffers + builtins, **dispatcher** control flow, contract parsing | `saxpy` and the elementwise corpus pass difftest |
| **3. Control flow + calls** | Structurizer, reducibility check, strong inliner, recursion refusal | Loops / `break` / `continue` / early return kernels pass; 1000 fuzz kernels agree with native |
| **4. Data** | Structs and local arrays with explicit offsets, int8/16/64, `double`, provenance analysis, optional bounds checks, `linear` fallback | Layout test green; refusal tests pinned; the SoA tables from `unity_pack` read correctly |
| **5. Front ends** | Rust `#[kernel]` + slices, C++ attribute, C# `[Kernel]` + arena→SSBO | The same algorithm written in all four languages produces identical `.spv` behaviour in difftest |
| **6. GPU features** | Workgroup shared memory, barriers, atomics, subgroup ops, specialization constants | Reduction, histogram, scan kernels pass; divergent-barrier refusal pinned |
| **7. Host + integration** | A Vulkan host example (owned `vulkan.h` subset, `-lvulkan` hosted linking, as with `-lGLESv2`), lavapipe CI run; unity_pack GPU sort/cull kernels written in Crust | `make test_spirv_vk` passes on lavapipe; unity_pack GPU path runs from Crust source instead of embedded GLSL |

Later, not scheduled: a GLSL ES 3.10 emitter from the same structured form so
GLES 3.1 hosts (the current unity_pack target) can still use it — GLES cannot
ingest SPIR-V — and a WGSL emitter for browsers, since WebGPU takes WGSL, not
SPIR-V. Both come cheaply once §6.2 exists, and they are the reason to keep the
structurizer independent of SPIR-V encoding.

## 10. Decisions and recommended defaults

| # | Question | Recommendation | Why |
|---|---|---|---|
| 1 | Target environment | **Vulkan compute** (`Shader`), baseline Vulkan 1.1 | The repo's own hardware targets (Raspberry Pi, Jetson Nano) are Vulkan platforms; OpenCL-style `Kernel` SPIR-V would be easier (pointers, no structured CF) but is not what those devices run. *Confirm per device.* |
| 2 | Pointer strategy | typed → linear → bda | §6.3 |
| 3 | Kernel declaration | contract line + per-language sugar | §5; reuses `extensions.py`, language-independent |
| 4 | Control flow | dispatcher first, then structurizer, dispatcher kept as fallback | §6.2; same path wasm took |
| 5 | Module placement | new `spirv.py` / `spirv_gen.py`, not more inline code in `asm_gen.py` | Size of `asm_gen.py` (~7.6k lines); the wasm code living there is a cost, not a model |
| 6 | Float parity | ULP-bounded compare by default, `--gpu-strict-fp` available | §6.5 |
| 7 | Scope of first language | **C and Rust first** (the lowest-friction mappings), C++ and C# once the subset check exists | C# and C++ both bring heap types that the subset refuses; Rust slices map one-to-one onto bindings |

## 11. Risks

* **Structurizer correctness** is the biggest technical risk; mitigated by the
  independent validator, fuzzing, and the dispatcher fallback.
* **Provenance analysis** may refuse more programs than hoped; mitigated by
  the `linear` fallback. Measure the refusal rate on the corpus early.
* **Driver variance**: a module our validator accepts may still be rejected or
  mis-run by a driver. Keep lavapipe in CI and request a real-GPU pass per
  phase.
* **Inliner growth**: aggressive inlining of memory-touching functions is new
  territory for `shivyc/inline.py` and touches the CPU path if not gated;
  gate it on the spirv target.
* **Scope creep into graphics.** Held off deliberately.

## 12. First concrete steps

1. Agree §10, in particular #1 and #7.
2. Write the Phase 0 corpus under `examples/gpu/` with CPU-fallback self-checks.
3. Land `shivyc/spirv.py` + `spirv_reader.py` + `tools/spirv_interp.py` with a
   hand-built `vecadd` test before touching the compiler proper.
4. Add `SpirvTarget` and the `is_binary` hook; get `saxpy` through end to end.
