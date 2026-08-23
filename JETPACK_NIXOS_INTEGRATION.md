# Integrating jetpack-nixos

A companion to [DRMDEEP_DESIGN.md](DRMDEEP_DESIGN.md). Assessed 2026-08-22
against `anduril/jetpack-nixos` master.

---

## The constraint, first

jetpack-nixos does not support the Jetson Nano, and never will.

> The Jetson Nano, TX2, and TX1 devices are *not* supported, since support for
> them was dropped upstream in JetPack 5.

The Nano is a Tegra X1 (T210) and is frozen at JetPack 4.6.x / L4T r32.7.x.
jetpack-nixos begins at JetPack 5. Its supported set is Xavier AGX/NX (T194),
Orin AGX/NX/Nano (T234) and Thor AGX. This is upstream end-of-life, not a
missing feature.

Every Jetson constant in crust is Tegra X1:

| crust `jetson` profile | Tegra X1 (Nano) | Orin (T234) |
|---|---|---|
| CPU | Cortex-A57 | Cortex-A78AE |
| `RAM_BASE` | `0x80000000` | different |
| console | 16550 UART-A `0x70006000` | different |
| interrupt controller | **GICv2** `0x50041000`/`0x50042000` | **GICv3** |
| GPU | Maxwell GM20B, sm_53 | Ampere, sm_87 |

So there is no board in jetpack-nixos that crust's `baremetal_arm64.py`
`jetson` profile describes, and no profile in crust that jetpack-nixos could
flash. **A board-level integration does not exist to be built.**

`BOARDS.md` currently claims "Jetson Xavier / Orin — yes (same AArch64 path)".
That is defensible for *Linux userland* binaries, which only need AArch64 and
the generic syscall table. It is not true of the bare-metal profile, and the
row is worth splitting so the two claims are not read as one.

---

## What is actually separable

jetpack-nixos is two things bolted together, and only one of them is
board-locked:

1. **A board enablement layer** — flashing scripts, EDK2 UEFI firmware,
   ATF/OP-TEE, device trees, the vendor kernel. Board-locked. Useless here.
2. **A packaging layer** — a hermetic, pinned, reproducible Nix expression for
   an aarch64 CUDA toolchain, the nvgpu driver source, and everything around
   it. **Not board-locked.**

The second is worth having, and two of the three uses below need no Jetson
hardware at all — which matters given the container-only constraint we are
working under.

---

## Integration point A — a hermetic build environment

**This is the highest-value item, and it is the direct fix for the defect
class found in the drmdeep work.**

Defect 2 in `DRMDEEP_DESIGN.md` was that `drmdeep.py`'s gcc oracle silently
consumed `/usr/include/linux/errno.h` from Ubuntu's `linux-libc-dev`, making a
supposedly freestanding build depend on an ambient host package. The result
inverted the tool's central claim: ShivyCX was filed as having a compiler gap
for correctly *not* finding a header gcc should not have found either.

That is exactly the failure mode Nix exists to prevent. A Nix build has no
`/usr/include` at all, so the contaminated invocation fails loudly instead of
succeeding quietly, and the pinned compiler means the survey number is
reproducible across machines rather than a property of whoever ran it.

Note this needs **plain nixpkgs, not jetpack-nixos.** No CUDA, no Jetson, no
unfree licence. It is worth doing regardless of what happens with the rest of
this page, and it is the cheapest item on it:

```nix
# flake.nix — devshell for the drmdeep survey
devShells.default = pkgs.mkShell {
  packages = [ pkgs.gcc13 pkgs.python312 pkgs.binutils ];
};
```

The honest framing: this does not *fix* the missing `-nostdinc`; it makes the
absence of `-nostdinc` harmless, and it makes the fixed version's numbers
reproducible. Both changes should still land. Nix is the belt to that
suspenders — and specifically the part that survives someone re-introducing the
bug later.

---

## Integration point B — nvcc, to compile-verify `accel_cuda.cu`

`hostsim/accel_cuda.cu` carries this in its header:

> This file has never been compiled or run.

`tools/hostsim_test.py` reports the same, as a passing check:

```
  PASS  nvcc is absent, so the CUDA path is untested here
```

jetpack-nixos packages CUDA for aarch64 across JetPack 5/6/7
(`cudaPackages_11_4`, `cudaPackages_12_6`, CUDA 13 respectively). The question
is whether any of them can still target the Nano's GM20B, which is
**compute capability 5.3** (Maxwell).

The answer is version-dependent and closing:

| toolkit | JetPack | targets sm_53? |
|---|---|---|
| CUDA 11.4 | 5 | yes |
| CUDA 12.6 | 6 | yes (min is sm_50) |
| CUDA 13.x | 7 | **no** |

<!-- CUDA 13.0 removed offline compilation for Maxwell, Pascal and Volta;
     the minimum architecture is now Turing. -->

So `cudaPackages_12_6` — JetPack 6, an Orin package set, from a repository
that does not support the Nano — can still compile device code for the Nano's
GPU. The toolchain outlives the board support.

**And this runs in a container with no GPU.** `nvcc -arch=sm_53 -c` is an
offline compile; it needs no device. That converts the file's status from
"never compiled anywhere" to "compiles, checked in CI", which is a real change
in what the repository can claim.

What it does **not** give:

- `accel_selftest()` cannot run. It compares CUDA and C results exactly over
  generated frames, and that needs silicon. Compile-verification says the
  kernel is well-formed, not that it computes the same answers.
- The integer arithmetic in `infer_kernel` — the striding loop and the
  shared-memory reduction — is where a real bug would live, and it is
  precisely what compiling does not exercise.

The honest intermediate step, which *is* fully checkable here: extract the
kernel body into a header shared between `accel.c` and `accel_cuda.cu`, then
run it on the host with `blockDim`/`threadIdx` supplied by a serial loop. That
tests the reduction's arithmetic without a GPU and without duplicating the
templates. It is the same trick `hostsim` already uses on the rest of the
firmware.

`--cuda`'s existing refusal-rather-than-fallback behaviour should stay exactly
as it is. A Nix devshell that provides `nvcc` does not change the argument for
it.

---

## Integration point C — nvgpu as a second drmdeep corpus

jetpack-nixos ships a vendor kernel that includes **nvgpu**, the open-source
Tegra GPU driver. This is the only route in this whole exercise that reaches
actual Tegra GPU code — `GM20B`, `nvgpu` and `host1x` appear nowhere in the
crust tree today.

The natural shape is to point the repaired `drmdeep.py` at it: same
two-compiler methodology, same shim-gap versus ShivyCX-gap split, different
corpus. Where drm-kmod's generic layer gives portable algorithms with no Tegra
in them, nvgpu gives Tegra with no portability.

Three cautions, in order of severity:

1. **Version mismatch.** JP5/JP6 nvgpu targets Orin and Xavier. Nano nvgpu is
   in L4T r32.7.x, which jetpack-nixos does not package — it would have to
   come from NVIDIA's archive or OE4T directly. jetpack-nixos gets you *an*
   nvgpu, not the Nano's.
2. **nvgpu is not the drm generic layer.** It assumes a Linux kernel, DMA,
   IOMMU, interrupts, firmware blobs and command submission. `GPU.md` already
   rules out amdgpu and i915 for exactly these reasons, and that reasoning
   applies to nvgpu with equal force. Expect a survey result dominated by
   framework dependencies, not a shim shortfall.
3. **Sequencing.** Running a survey against a second corpus while the harness
   still miscounts the first would produce another unreproducible number.
   Stage 1 and 2 of the drmdeep plan come first.

Framed correctly this is still worth doing — as a *measurement* that tells you
how far off real Tegra GPU code is, which nothing in the tree currently
answers. Framed as a step toward driving a GM20B, it is not.

---

## What none of this provides

- **Nano board support.** Permanently unavailable from this source.
- **Anything about silicon.** Every caveat in `JETSON_NANO.md` stands
  unchanged. A hermetic toolchain and a compiling `.cu` are both still
  statements about build artifacts.
- **A running GPU.** No emulator here models a GM20B, armulator included.

---

## Suggested order

1. **nixpkgs devshell for the drmdeep survey** (Point A). No CUDA, no unfree,
   no hardware. Do this alongside Stage 1 of the drmdeep plan, so the repaired
   numbers are reproducible from the moment they are first published.
2. **Split the `BOARDS.md` Xavier/Orin row** into userland (plausible) and
   bare-metal (not applicable), and record the Nano/JetPack 5 EOL in
   `JETSON_NANO.md` so the question does not get re-asked.
3. **nvcc devshell + `nvcc -arch=sm_53 -c` in CI** (Point B), plus the shared
   kernel-body header so the arithmetic is testable without a GPU.
4. **nvgpu survey** (Point C), only after drmdeep Stages 1–2 land.

Items 1 and 2 are the ones that pay off immediately. Item 3 removes the
repository's most prominent "never compiled" caveat. Item 4 is a measurement,
and should be described as one.
