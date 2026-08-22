# rpython FFI / ctypes bridge — change summary

Builds on `b224c42 "rpy 8bit quantized neural networks"`. Five files (three new).
**`tools/rpy_lib/rpy_ctypes.py` and the `examples/rpython2c/ffi/` directory are
new — they need `git add`.** Only the transpiler (`tools/py2c.py`) changed this
turn; the live ShivyCX compiler (`shivyc/*.py`) was untouched.

## What it does

A small, transpilable `ctypes` subset turns the common "load a library and call
its functions" pattern into direct, statically-linked C — the dynamic lookup is
resolved at transpile time, nothing is `dlopen`'d at runtime:

    import ctypes
    libm = ctypes.CDLL("libm.so.6")
    libm.pow.restype  = ctypes.c_double
    libm.pow.argtypes = [ctypes.c_double, ctypes.c_double]
    r = libm.pow(2.0, 10.0)        # -> `pow(2.0, 10.0)` in C
    f = libm.sqrt                  # bind the lookup to a local
    s = f(r)                       # -> `sqrt(...)` in C

py2c tracks the `CDLL` handle and each `lib.symbol` attribute as a compile-time
constant, emits a real prototype (`extern double pow(double, double);`), lowers
the calls to direct C calls (coercing args to the declared `argtypes`), drops
the `CDLL`/`restype`/`argtypes` statements (no code), and leaves the symbol to
the linker (ShivyCX links `-lc -lm`).

## Files

* **`tools/rpy_lib/rpy_ctypes.py`** (new): the ctypes subset — scalar type
  markers (`c_int`, `c_double`, `c_char_p`, ...) and `CDLL`. Under CPython it
  delegates to the real `ctypes`, so the same source cross-validates against the
  genuine dynamic-loading implementation.
* **`tools/py2c.py`**:
  - `_CTYPES_TYPEMAP` (ctypes marker -> C type).
  - `_scan_ctypes(tree)` (called from `run` after `collect_imports`): tracks
    `CDLL` handles, `lib.symbol` bindings, `restype`/`argtypes`, the symbols
    actually called, and the statement-ids that emit no C.
  - `ctypes_call_symbol` / `_emit_ctypes_call`: lower a tracked call to
    `symbol(args)`.
  - `ctypes_externs` + an emit hook in `emit_forward_decls` for the prototypes.
  - `value_ctype` returns a call's `restype`.
  - Config statements are skipped in `stmt`, `toplevel`, and
    `collect_module_globals`; bound FFI names are excluded from local hoisting
    (so no shadowing `obj` is declared).
  - Every hook is a no-op when no ctypes import is present.
* **`examples/rpython2c/ffi/ffi_math.py`** (new): libm `pow`/`sqrt`/`cbrt` via
  the subset; returns `35`.
* **`examples/rpython2c/ffi/README.md`** (new).
* **`Makefile`**: `ffi_math.py` added to the `rpython` and `testtorch` targets.

## Verification (no regressions)

* `ffi_math.py` returns **35** under all three: ShivyCX, gcc (`-lm`), and CPython
  (real ctypes) — same source.
* unit tests `FAILED (errors=29)` — unchanged
* `selfhost test` -> 3 OK — unchanged
* `make rpython` all pass (incl. ffi_math=35; simd_kernels=55, torch_mlp=4,
  torch_mlp_f32=4, quant_mlp=50, fusion=97, neural_net=199, ...)
* `make testtorch / testfast / testpromote / testpgo / testfuse` -> PASS
  (testtorch checks ffi_math on gcc **and** ShivyCX)
* gcc coverage 45/60 — unchanged

## Scope

Minimal by design: `CDLL`, scalar type markers, per-function
`restype`/`argtypes`, direct calls, and `f = lib.symbol` bindings. Struct/array
marshalling, callbacks, `byref`/pointer out-params, and `errno` are future work.

---

# Jetson Nano bare-metal bring-up — change summary

Builds on `ed86357 "https://github.com/crustos/armulator"`. Two files changed
in this tree plus one new tool; the companion changes are in **armulator**,
which needs its own commit (see the bottom of this file).

**`tools/jetson_armulator.py` is new — it needs `git add`.**

## What it does

**The bare-metal Jetson image boots.** It had never been booted anywhere: no
qemu machine models a Tegra, so `--board jetson --run` refuses, and the image
was verified at register level only. It now runs to completion under
[armulator](https://github.com/crustos/armulator), whose Cortex-A57 core is the
Nano's actual core:

    $ python3 tools/jetson_armulator.py
    [mmu] summing an array through the MMU: 6048 (expect 6048)
    ...
      unmasked, waiting 300ms: 30 ticks (expect ~30)
      spurious=0 unexpected=0

    == all stages ok ==
    [jetson-armulator] 300000 instructions, halted=False fault_loop=False
    [jetson-armulator] OK

That is the same result qemu produces for `-M virt`, on a board qemu cannot
model at all.

## The bug booting it caught

`baremetal64/mmu_arm64.c` **could never have worked on a Jetson.** It hardcoded
level-1 entries 0 and 1 with `RAM_BASE` at `0x40000000` — correct for virt and
for the Pi. The Nano's DRAM is at `0x80000000`, level-1 entry **2**, so the
image mapped neither the code it was executing nor the vector table it would
fault into.

The failure mode is undiagnosable from inside: `ESR = 0x86000005`, an
instruction abort with a level-1 translation fault whose `FAR` is the vector
address itself, looping forever with nothing left that could report it. No
register-level test would have caught it, because every individual write was
correct — only booting the thing exposes it.

## Files

* **`baremetal64/mmu_arm64.c`**: the identity map is now built from
  `RAM_BASE`, `PERIPH_BASE` and `PERIPH_SIZE`, supplied per board.
  - new `map_gigabyte()` fills one level-2 table and hangs it off the level-1
    entry for that gigabyte;
  - attributes are chosen **per 2 MiB block**, because on the Pi 3 RAM starts
    at `0` and the BCM peripherals are at `0x3F000000` — the same gigabyte, so
    one attribute for the whole gigabyte is wrong either way;
  - the peripheral window may **straddle a gigabyte**, because the Pi 3's ARM
    *local* peripherals (which route the generic timer) are at `0x40000000`,
    in the next gigabyte from the BCM ones;
  - up to three gigabytes are mapped, deduplicated. That is also the ceiling
    the linker script's 16 KiB of table space allows.
* **`tools/baremetal_arm64.py`**: each board profile gains `RAM_BASE`,
  `PERIPH_BASE` and `PERIPH_SIZE` defines. virt `0x40000000`/`0x0`; raspi3
  `0x0`/`0x3F000000` with a window reaching past `0x40000000`; raspi4
  `0x0`/`0xFE000000`; jetson `0x80000000`/`0x50000000`.
* **`tools/jetson_armulator.py`** (new): builds the image, loads its `PT_LOAD`
  segments into armulator's `JetsonNanoA64`, streams the console, and exits
  nonzero on a vector-table fault loop or missing expected output — usable as
  a CI check. Runs in slices and stops on the expected text, since once timer
  interrupts are live the parked halt loop is entered and left on every tick
  and `Board.run`'s self-branch detection never fires.
* **Docs**: `JETSON_NANO.md` (no longer "never booted"; still never run on
  hardware), `BOARDS.md`, `BAREMETAL_ARM64.md` (the MMU section).

## Verification (no regressions)

Baselines were taken from a clean tree before any change.

* `tools/irq_timer_test.py` → **22 pass, 0 fail** — unchanged from baseline
* `tools/board_tools_test.py` → **12 pass, 0 fail** — unchanged
* `tools/rlink_script_test.py` → **19 pass, 0 fail** — unchanged
* `tools/gic_base_test.py`, `tools/uart_8250_test.py` → all pass — unchanged
* bare-metal boots: `kernel_arm64.c` on virt → `== all stages ok ==`;
  `kernel_raspi.c` and `kernel_raspi_irq.c` on raspi3 → `== pi ok ==`,
  `== pi interrupts ok ==`; `kernel_arm64.c` on jetson under armulator →
  `== all stages ok ==`
* armulator → **1398 pass** (1371 baseline + 27 new), 0 fail

`examples/baremetal/kernel_irq.c` fails to build (`unable to read included
file "console.h"`). This is **pre-existing** — confirmed against a stashed
baseline — and unrelated.

### A regression caught mid-change

The first version of the MMU rewrite mapped one gigabyte for RAM and one for
peripherals, and **broke raspi3**: 22 pass → 11 pass, 1 fail, with a level-1
translation fault at `FAR = 0x40000060`. The Pi 3's ARM local peripherals are
in gigabyte 1, which the old hardcoded `l1[1]` had covered by accident. Fixed
by letting the window straddle the boundary. Recorded because the two Pi
kernels booting by hand was not sufficient coverage — the diff against a
stashed baseline is what caught it.

## Companion changes in armulator

These live in https://github.com/crustos/armulator and are needed for the boot
above to work at all:

* **`armulator/peripherals/uart_8250.py`** (new): `Uart8250` / `TegraUart`.
  The Jetson board's console was a **PL011 standing in for a 16550**. They
  share offset 0 for the data register and disagree about everything after it,
  so a driver writes its first byte successfully and then hangs forever
  polling `LSR.THRE` at `0x14`, which a PL011 answers with zero.
* **`armulator/armv8/generic_timer.py`** (new): the EL1 physical timer.
  `CNTPCT_EL0` did not exist and read as zero forever, so any delay loop hung.
  Delivered as PPI 30.
* **`armulator/boards/__init__.py`**: Jetson `GIC_ADDRESS` corrected from
  `0x50041000` to `0x50040000`. The former is the *distributor* address being
  used as the GIC base, which displaced every GIC register by `0x1000` — and
  unmapped offsets read back as zero rather than faulting, so the distributor
  simply never enabled.
* **`tests/test_jetson_console.py`** (new): 27 tests over the console, the GIC
  base and the timer.

An independent cross-check worth noting: `tools/uart_8250_test.py` here and
armulator's new UART model were written from the TRM without reference to each
other, and agree that 115200 baud off Tegra's 408 MHz UART clock is divisor
`0xDD`.

## Still not done

* **Never run on physical hardware.** armulator models the CPU, GIC, timer and
  console — not the SoC. No clock and reset controller, memory controller,
  PMIC, display or USB.
* Two fidelity gaps: armulator does not fault on unmapped physical addresses
  with the MMU off (this image counts 1 fault where qemu counts 2), and the
  GIC keeps one line per interrupt ID rather than per core, so PPI 30 on a
  cluster is driven by the primary.
* `--board` vs `--machine` in `baremetal_arm64.py` is still a foot-gun:
  `--machine raspi3` is accepted, silently leaves the profile at `virt`, and
  fails with an interrupt-controller link error that points nowhere near the
  actual mistake.
* raspi4's GIC-400 is still `irq: False` — present on the BCM2711, never
  exercised.

# Board fidelity: CLI, the Pi 4's GIC, and two emulator approximations

Builds on `bf4f89f` (crust) and `c2ee196` (armulator). Clears the four items
left open at the end of the previous section.

**New in this tree: `tools/board_machine_test.py`, `tools/armulator_boards_test.py`.
New in armulator: `tests/test_board_fidelity.py`. All three need `git add`.**

## 1. `--board` and `--machine` can no longer be silently confused

The root cause was that `"virt"` was both the default value *and* the sentinel
for "not given", so an explicit `--machine virt` was indistinguishable from no
flag at all. Both now default to `None`.

`--machine raspi3` used to be accepted, silently leave the profile at virt,
and fail at link time with `undefined reference to: intc_raw_source` — which
points at the interrupt controller when the mistake was the flag. And
`raspi3` is not a qemu machine name anyway; qemu's is `raspi3b`.

    $ python3 tools/baremetal_arm64.py app.c --run --machine raspi3
    --machine 'raspi3' is a board name, not a qemu machine.
      Use --board raspi3, whose qemu machine is 'raspi3b'.        # exit 2

`jetson` and `raspi4` get the variant that says no qemu machine models them.
A real mismatch (`--board raspi3 --machine virt`) warns and names the linker
script, so a silent console is explicable. Missing flag values now print
`--machine needs a value` instead of an `IndexError` traceback.

## 2. The Pi 4's GIC-400 is wired up and exercised

`raspi4` had `irq: False` because nothing could run it. It now carries
`GICD_BASE=0xFF841000`, `GICC_BASE=0xFF842000`, `UART_IRQ=153` and
`intc: gic_arm64.c`, and boots under armulator's `RaspberryPi4A64`: MMU on,
fault recovered, 30 ticks in 300 ms, `spurious=0`, `== all stages ok ==`.
The existing `PERIPH_SIZE` already reached the GIC at `0xFF84xxxx`, so the
MMU window needed no change.

That boot immediately caught a bug introduced in the previous section: it
reported the Jetson's 19.2 MHz. **The BCM2711 clocks its architected timer at
54 MHz.** `CNTFRQ_EL0` had been hardcoded as an architectural constant when it
is a board property — firmware derives its tick period from it, so every delay
was being scaled by 2.8x with nothing reporting an error.

## 3. Unmapped physical addresses abort (fidelity gap 1)

`MemoryControllerHub` returned zero for unclaimed reads and discarded
unclaimed writes. Firmware that walks off its own map therefore looked fine
here and would fail on hardware.

Unclaimed accesses now raise a synchronous external abort, fault status
`0b010000`. The Jetson image's deliberate bad store reports
`ESR = 0x96000050`, "external abort" — byte-identical to qemu — and its fault
count went from 1 to **2**, matching what qemu reports for the same code.

Deliberately scoped to the **MMU-off** path. With translation on, an address
outside the tables already faults as a translation fault, which is the more
specific report; and firmware routinely identity-maps a whole gigabyte of
peripheral space, so aborting there would punish it for peripherals armulator
does not model — a different thing from the firmware being wrong. Opt-in per
board (`FAULT_ON_UNMAPPED`), off for the ARMv6 boards whose tests rely on the
permissive behaviour.

## 4. PPIs are banked per core (fidelity gap 2)

`lines`, `pending`, `active` and `enabled` were flat arrays, so a cluster's
four timers appeared as one interrupt and whichever core fired last determined
what every core saw. New `BankedInterruptState` banks interrupt IDs below
`SPI_BASE` — the 16 SGIs and 16 PPIs, which the architecture specifies as
banked — while SPIs stay shared, since one device drives one line.

Arming only core 2's timer on a four-core cluster now asserts only core 2's
PPI 30; the other three stay low.

## Files

* **`tools/baremetal_arm64.py`**: `None` sentinels for `--machine`/`--cpu`;
  board names rejected as machines; mismatch warning; missing-value handling;
  `raspi4` GIC defines and `irq: True`; usage text explaining the distinction.
* **`tools/jetson_armulator.py`**: `--board {jetson,raspi4}`. Name kept — it
  is historical, and the docstring says so.
* **`tools/board_machine_test.py`** (new): 20 checks over the CLI.
* **`tools/armulator_boards_test.py`** (new): boots both no-qemu boards and
  checks timer rate, the unmapped-store abort, and interrupt counts. Skips
  cleanly when armulator is absent.
* **Docs**: `BOARDS.md`, `BAREMETAL_ARM64.md`, `RASPI.md`, `JETSON_NANO.md`.

### armulator

* **`armulator/armv6/memory_controller_hub.py`**: `fault_on_unmapped`,
  `is_mapped(address, size)`. An access straddling the end of a region counts
  as unmapped, since the far half would silently read zero.
* **`armulator/armv8/arm_v8.py`**: `_check_physical_address` raises the
  external abort.
* **`armulator/peripherals/gic400.py`**: `BankedInterruptState`; `set_line`
  takes a `cpu`; candidate selection and SGI dispatch read the bank of the
  core being asked about rather than the selected one.
* **`armulator/boards/__init__.py`**: `TIMER_FREQUENCY` and
  `FAULT_ON_UNMAPPED` per board; `sample_timer` drives every core's own PPI.
* **`tests/test_board_fidelity.py`** (new): 25 tests.
* **Docs**: `JETSON.md`, `README.md`.

## Verification

* armulator → **1423 pass** (1398 + 25 new), 0 fail
* `tools/board_machine_test.py` → **20 pass, 0 fail**
* `tools/armulator_boards_test.py` → **16 pass, 0 fail** (jetson + raspi4)
* `tools/irq_timer_test.py` → 22 pass, 0 fail — unchanged
* `tools/board_tools_test.py` → 12 pass, 0 fail — unchanged
* `tools/rlink_script_test.py` → 19 pass, 0 fail — unchanged
* `tools/gic_base_test.py`, `tools/uart_8250_test.py` → pass — unchanged
* qemu boots: `kernel_arm64` on virt, `kernel_raspi` and `kernel_raspi_irq` on
  raspi3 → all reach their OK lines

Every fix was mutation-tested rather than trusted green: reverting the Pi 4
frequency fails 2 tests, primary-only timer sampling 1, disabling unmapped
aborts 4, unbanking the PPIs 5, and reverting the `--machine` validation 7 of
20. All caught.

### One existing test was modified

Banking `enabled` broke `test_a_broadcast_sgi_reaches_every_target`. Its
fixture wrote `GICD_ISENABLER` once, with `current_cpu` left at 1 from a
preceding loop, so core 0 never had the SGI enabled — it only passed because
the old model shared the array. `GICD_ISENABLER0` covers the SGIs and PPIs and
is banked per core in GICv2, so the write moved inside the per-core loop.
Flagged because it is a test being changed to match new code, and deserves a
second opinion.

## Still not done

* **Never run on physical hardware.** armulator models the CPU, GIC,
  architected timer and console — not the SoC. No clock and reset controller,
  memory controller, PMIC, display or USB.
* **`UART_IRQ=153` (Pi 4) and INTID 68 (Jetson) are unexercised.** The timer
  arrives as PPI 30, so nothing drives either UART's SPI. Both come from
  documentation.
* **`priority`, `targets` and `config` are still shared** across cores. They
  are banked below `SPI_BASE` on real hardware too; only the state that
  mattered for the reported bug was split.
