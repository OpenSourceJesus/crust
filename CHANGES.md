# Lowering cpprust.py to C — py2c fixes for issue #16

brentharts/crust#16 proposes porting `tools/cpprust.py` to RPython so it can
be lowered to C and run natively. Measured rather than estimated: **py2c
already transpiles all 13,120 lines without refusing anything.** The work was
not rewriting cpprust into a subset -- it was that the C which came out did
not compile, and the reasons were nearly all in py2c.

`tools/rpy_census.py` (new) is what makes that a number rather than an
assertion. **76 gcc errors -> 7; 11 calls substituted with None -> 5.**

## The shape of it

One pattern accounted for most: py2c's type oracle and its emitter
disagreeing. `value_ctype` says a call yields an obj, the emitter emits a raw
`char*` or `long`, and the assignment between them does not compile --
`re.sub` (16 errors), `str.index` (7), `str.count` (5), and the mirror
direction for `AS_INT` over an already-long value.

Each fix belongs where the *disagreement* is. Teaching `value_ctype` that
`.index` is an int looked right and made it worse: that function also decides
how a fresh local is declared, so it declared `int` for names that elsewhere
held an obj -- seven errors traded for seventeen. What tells the two `.index`
lowerings apart is not the receiver's type (both are plain `obj` in the
generated C) but the helper py2c itself chose, so that is what the check
keys on.

## One was a wrong answer, not a missing feature

`pat.search(s, pos)` handed the engine `text + pos`, losing the text before
`pos`, so a lookbehind there could not see it:

    re.compile(r"(?<=;)x").search(";x", 1)
        CPython -> match at 1
        native  -> no match

cpprust windows its scans precisely so the lookbehind sees the character
before the window, so a self-hosted cpprust would have translated C++ subtly
wrong rather than failing. `crust_re_exec_from` starts the search at an offset
while keeping the whole subject visible; `len` doubles as CPython's `endpos`.
Differential fuzzer: 53,121 comparisons, 0 divergences.

## Files

* **`tools/rpy_census.py`** (new): the census over all three passes.
* **`RPYTHON_CPPRUST.md`** (new): the state, the method, and the remaining 7.
* **`runtime/crust_re.{c,h}`**: `crust_re_exec_from`; `crust_re_exec`
  delegates to it at 0. `tools/rpy_lib/crust_re_src.py` regenerated.
* **`tools/py2c.py`**: transitive closure captures through *values*, not just
  calls; nested tuple targets; the kwargs slot only for callees that declare
  `**kwargs`; `str_count`; `tuple()`; `os.path.normpath`; `_cre_sub_any` for a
  function replacement; a pre-scan so `.group()` lowers in a helper defined
  above the first `re.` call; pos/endpos on search/match/finditer; integer
  conditionals kept scalar across emitter, oracle and boxing.
* **`tools/cpprust.py`**: a list rather than a `bytearray` for the probe
  flags -- `bytearray` has no lowering, and the two are interchangeable for a
  flag array (verified element-for-element, timing a wash).
* **8 new agreement tests** under `tools/rpy_lib/`, plus
  `examples/rpython2c/closures/lifted_captures.py` in `make rpython`.

## Verification

Every fix ships a cpython-vs-native test that compiles the same source both
ways and diffs stdout, so a fix that compiles but computes something else
fails. All 8 pass. `make testminipy` fails only on `crust_re C++ frontend`,
which reproduces on a clean master with these changes stashed.

## What is NOT claimed

Nothing here says the result is faster: the binary does not link until the
last 7 errors are gone, so nothing has run. Also worth knowing before that
measurement is designed -- translation is already ~15s/file after PR #7, of
which ~4.5s is a clang subprocess a native build would not speed up; and
**ShivyCX has two bugs of its own** in code gcc compiles correctly (a
segfault on `m = pat.search(text, pos)`, and 42 where CPython and gcc get 65
on a `*args` program).

---

# cpprust: binary operators on owning classes, and four silent miscompiles

Six changes to `tools/cpprust.py`, found by translating a real codebase
(cpp-mcp) and pinned against litehtml. Four of them turn C that does not
mean anything into a diagnostic; two widen what translates.

## A type-level `(` is not a parameter list

`map<int, pair<string, function<void(const string &)>>> subs` was read as a
*method* whose return type was everything up to that paren, and the parse
then failed against its own trailing `>>> subs` -- naming neither the field
nor the real limit. Four places located a member's parameter list with a
bare `find("(")`; they now use `_decl_paren`, which counts `<>` and `[]`
nesting and steps over `operator<`, `<<` and `<=` first. An unbalanced `<`
falls back to the plain scan, so nothing that parsed before stops.

## `std::function` is refused by name

An unknown template passes through untouched on purpose, so the field above
reached the C front end spelled `function<void(const string &)>`. It is now
named, with a function pointer and a context parameter as the replacement.

Refused only where it is **stored** -- a field, or a template argument.
Taken by reference it is a borrow and passes through, which is what keeps
litehtml's `split_text(.., const std::function<..> &on_word, ..)` from
refusing every file that includes `html.h`. The first version of this check
was not so careful and cost 38 of 43 files.

## Default arguments on a declared-only member

A member *defined* in the file loses its defaults on the way to its
definition; one only **declared** here did not, so litehtml's `css_length.h`
emitted `void fromString(.., const string *predefs = _t(""), int defValue =
0);` -- and C has no default arguments. 70 instances across the tree, in a
header most of it includes. `_strip_default_args` cuts at the first `=`
that is not part of `==`, `<=`, `>=` or `!=`.

## Two `operator=` overloads collided

`litehtml::border` declares assignment from `border` and from `css_border`.
Both lowered to `border__assign` and the second redefined the first.
Ordinary methods already refuse this -- overloads resolve by argument
*count* -- and the move overload escapes it only by having a symbol of its
own. `operator=` skipped the check; it no longer does.

## Binary operators on a class that owns something

`operator+` was refused for a class with a destructor, on the grounds that a
by-value return of an owning class was not in the subset. It has not been
for some time: a returned bare local is moved out. The two spellings had
drifted apart for no reason left standing -- `buf plus(const buf &)` was
emitted and `buf operator+(const buf &)` refused with an identical body.

What is still owning-specific is the *chain*: `a + b + c` passes the first
result into the second by value, which for an owning class would make a
second owner. The by-value front door is therefore emitted only for a class
that owns nothing, and a chain over one is refused with its own diagnostic.

`string` gains `+` and `+=` on the back of this -- written in the subset,
like the rest of the supplied `std`. Verified under ASan and LSan: correct
output, no leaks, no double free.

A **literal** operand is materialised on either side, through the
one-argument constructor, which is what C++ does and what a converting
assignment already did here. That is also what makes `"lit" + s` work: in
C++ that is a *free* `operator+`, which this subset has no notion of, but
the member operator on the materialised temporary means the same thing. An
arbitrary expression is not materialised -- it could be an object of the
class this pass failed to name.

## An argument to a reference parameter needs an address

`fix_args` put an `&` on whatever was there, so `sink(a + b)` came out as
`sink(&a + b)` and `sink(b.substr(0,1))` as `sink(&string_substr(&b,0,1))`.
`_unaddressable_arg` names only the shapes it is sure of -- a literal, a
call result, a top-level operator result -- and leaves anything it cannot
classify exactly as before, because a false refusal here fails every caller.

Two false positives were caught against real litehtml and are pinned as
tests: a member chain, where the `-` of `->` is not an operator; and a
prototype's parameter list, which is not an argument list at all -- a
parameter carrying a default argument is what stopped it parsing as one.

A by-value parameter is unaffected: nothing needs an address, so
`sink(a + b)` with `void sink(string)` lowers like any other use.

## Files

* `tools/cpprust.py` -- the six changes above.
* `tests/test_cpprust.py` -- 22 tests: the refusals, the features, and both
  false-positive shapes.
* `tools/test_cpprust_extras.py` -- the owning-`operator+` guardrail
  repinned. It asserted the refusal that is now a feature; it now pins the
  single application working and the chain still refused.
* `CPPRUST.md` -- binary operators, `std::function`, the `operator=`
  collision, and the reference-argument rule.

## Verification

`tests/test_cpprust.py` 401 tests, `tools/test_cpprust_extras.py` 143,
`tools/test_std_move_lowering.py` 41. The two failures in the first are
present on unmodified `master` -- failure sets diffed before and after and
are identical.

`tools/litehtml_test.py`: baseline `0/43 ok, 1 translate-fail, 42
gcc-fail`; after the first four changes `0/43 ok, 38 translate-fail, 5
gcc-fail`. **The passing count did not move.** What moved is the failure
mode: the 70 default-argument errors are gone, and the `operator=`
collision that was a gcc `conflicting types` error is now a refusal, which
is why translate-fail rose. Honest diagnostics rather than invalid C, not
more files translating. The last two changes have not been run against it.

## Known, not fixed

A binary operator in a **constructor initializer list** is not checked at
all: `Base_new(&this->_base, "file://" + p)` is emitted with no diagnostic.
Same family as the argument-position bug above, different code path.

A reference parameter with a **default argument** in a free-function
declaration is mangled by the `T &r = e;` lowering, which reads it as a
reference declaration with an initialiser and swallows the following
parameter: `const string *delims = &("", int keep = 0)`. Identical on
`master`. Third bug in the default-argument family, which suggests one
deliberate pass rather than another point fix.

A **literal argument to a reference parameter** is now refused rather than
mangled, but C++ materialises a temporary there and this could too -- the
same mechanism the binary operators just got. It is the commonest shape in
ordinary C++ and the next thing worth building.

# cpprust: translating litehtml to C, in seconds rather than minutes

Fixes brentharts/crust#6. `tools/cpprust.py` hung on
`juce_litehtml/litehtml/src/document.cpp`: 1175 lines of C++, two and a half
minutes to reach a diagnostic. Three files changed, no new ones.

**Nothing about the translation changes.** Every file that translated before
translates now, every refusal is refused with the same sentence, and the C
that comes out is byte-for-byte what came out before -- verified by running
both trees over ten litehtml sources and diffing.

## Why it was slow

`document.cpp` is 1175 lines on disk and a little over a megabyte once its
headers are spliced in, which they must be: a class this file uses is only a
class if its declaration is in hand. Several passes were quadratic in that
size -- each rescanned or re-copied the whole unit once per thing it found.
At 40 KB nobody notices. At 1 MB it is minutes.

The fixes are all the same shape: do the sweep once instead of once per hit.

* **Monomorphisation.** `_monomorphise_uses` rewrote one `Name<..>` use per
  pass and rescanned the file for the next. `document.cpp` names some seven
  thousand uses, so the scan ran seven thousand times over a megabyte.
  Innermost uses in one scan cannot overlap and none contains another, so
  they are now all rewritten together and the pass count is the *nesting
  depth* -- two. **55s of 175s.**
* **Prefix slices.** `_func_return_type`, the struct-body lookback in
  `_rewrite_scopes`, and `_assign_target` each took `text[:idx]` and searched
  it end-anchored. Each pattern can only reach back to the nearest `;`, `{`,
  `}` or a run of `[\w*=\s]`, so each now gets that window via
  `search(text, lo, hi)` -- no copy, and the lookbehinds still see the
  character before it. **55s.**
* **Character walkers.** `_rewrite_scopes` and `_rewrite_calls` tried a dozen
  compiled patterns at every character. All of them start at a `*` or the
  first character of a word -- their lookbehinds say so -- so
  `_probe_positions` marks those offsets once and the walkers skip straight
  to copying at the rest. 23M regex attempts became 3M. **~5s.**
* **Blanking.** `_blank_like`, `_strip_comments`, `_blank_strings` and
  `_blank_braced` walked every character in Python. They now jump between
  openers and copy the runs between them whole. **~20s.**
* **Namespace flattening.** `_rename_in_blocks`, `_rename_in_qualified_defs`
  and the per-block rename in `resolve_namespaces` substituted one name at a
  time and re-blanked the body after each -- litehtml declares hundreds. One
  alternation pattern does all of them in one scan; this is exact, because at
  any position only the whole word sitting there can match. `_blank_like`
  call count dropped from 50539 to 2587.
* **Declarator scans.** `_DECLARATOR` and `_FIELD` are anchored on
  `(?<=[;{}:\n])|\A`, which the regex engine cannot see through, so it
  retried a backtracking nested quantifier at every offset.
  `_anchored_finditer` finds the boundaries first and matches only there.
  **~5s.**

## Files

* **`tools/cpprust.py`**: `_iter_template_uses` (generator; `_find_template_use`
  keeps its signature on top of it) and the batched `_monomorphise_uses`;
  `_probe_positions` + `_PROBE_START`; windowed `_func_return_type`,
  `opens_aggregate`, `_assign_target` (now takes `(look, at)` rather than a
  slice); opener-jumping `_strip_comments` and `_blank_strings`.
* **`tools/cpp_auto.py`**: `_anchored_finditer` + `_DECL_ANCHOR`;
  `_flatten_pattern` and `_sub_flattened`; opener-jumping `_blank_like` and
  `_blank_braced`.
* **`tests/test_cpprust.py`**: `TestTranslationScales` -- five tests that pin
  the scaling properties rather than a wall-clock budget, so a slow machine
  cannot make one fail spuriously.
* **`CPPRUST.md`**: a "What a translation costs" section stating the rule the
  passes are held to.
* **`.gitignore`**: `.litehtml_out/` and `.litehtml_cache/`, which
  `tools/litehtml_test.py` generates.

## Verification

* `document.cpp`: **2m29s -> 15s** (~10x), same diagnostic, same output file.
* `python3 tools/litehtml_test.py --stage translate --no-cache`:
  **41/43 ok, 2 translate-fail, 0 gcc-fail** -- the same two files, with the
  same two messages, as before the change.
* Ten litehtml sources translated on both trees: **byte-identical C**, and
  byte-identical diagnostics for the two that are refused.
* `tests/test_cpprust.py` + `test_cpprust_extras` + `test_crust`: 907 tests,
  the same 2 pre-existing failures as `master`
  (`test_by_value_return_of_a_borrowed_object_is_still_an_error`,
  `test_helper_evaluates_this_once`).
* `tools/test_cpprust_extras.py` 65 OK, `tools/test_std_move_lowering.py`
  41 OK, `tools/test_cpp2rust.py` 3 errors -- all unchanged from `master`.
* Every rewritten helper was differential-tested against the old
  implementation: tens of thousands of fuzzed inputs plus the whole litehtml
  corpus, zero differences.

## What was left alone

`clang_auto_types` (~4.5s: a subprocess and a JSON AST) is real external
work, not a scanning bug. The `using namespace` rename in
`resolve_namespaces` has the same one-name-at-a-time shape but does not
register on litehtml, which qualifies explicitly, so it was not touched.

---

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

# hostsim: running firmware at native speed

Builds on `82f281f`. A second way to run a bare-metal image, for the questions
instruction-level emulation is too slow to answer. Nothing in armulator
changes; this is entirely additive and no existing path is altered.

**All new, all needing `git add`: `hostsim/`, `examples/hostsim/`,
`tools/hostsim.py`, `tools/hostsim_build.py`, `tools/hostsim_test.py`,
`tools/hostsim_difftest.py`, `HOSTSIM.md`.**

## Why

armulator executes AArch64 one instruction at a time and manages about
**17,000 instructions a second** — roughly 80,000x slower than a Jetson. That
is the right tool for "does this image boot", and hopeless for "do twenty
boards driving motors and talking to each other behave".

Measured on `examples/baremetal/kernel_arm64.c`, same program both ways:

| | armulator | hostsim |
|---|---|---|
| full run | 17.0 s | **0.0043 s** |
| versus real time | ~80,000x slower | 120–1000x faster |
| 16 boards in lockstep | — | ~100,000 board-ms/sec |

**About 4,000x.** Enough that a parameter sweep or a fault-injection campaign
is worth setting up.

## What it is

The application's C is compiled for the host by `gcc -O3` and runs at native
speed on its own thread inside a shared object; only the layer underneath --
console, timer, interrupt counters, MMU flags, motor and sensor values -- is
replaced, by `hostsim/hostsim.c`. The same idea as Zephyr's `native_posix` or
NuttX's simulator target: keep the application, replace the hardware.

Virtual time is driven from outside. Delay loops spin on `timer_count()`,
which blocks until the controller grants more with `step()`. Nothing advances
on its own, so runs are bit-for-bit repeatable regardless of host load, and
several boards can share one clock.

## Note on the approach

The original suggestion was to compile natively and have the result talk to
armulator. Measuring the seam first showed that is not worth doing: it is only
about 20 functions, and reimplementing them in C is ~400 lines, whereas
calling back into armulator's Python device models would cost a Python call
per MMIO access and give back most of the speed.

For the same reason the RPython/`py2c.py` translation of armulator is not
needed here. The fast path does not want armulator's device models; it wants a
small hand-written C layer plus a differential test proving the two still
agree.

MMIO interception was considered and rejected for now. The drivers reach
registers through a `static volatile unsigned int *` accessor, so intercepting
them would need an x86-64 instruction decoder in a `SIGSEGV` handler. The seam
is drawn at values instead -- `sim_motor_write(duty)`, not a PWM register --
which is right for system questions and wrong for driver questions. Drivers
should be tested against armulator, where the registers exist.

## Files

* **`hostsim/hostsim.c`, `hostsim/hostsim.h`**: the host implementation of the
  seam, plus the controller interface, a message link, and injectable faults.
* **`tools/hostsim_build.py`**: compiles an application plus the backend into
  a shared object. `-l` links host libraries, which is the route to CUDA or
  BLAS.
* **`tools/hostsim.py`**: `Sim` (one board) and `Fleet` (several in lockstep,
  with message routing).
* **`examples/hostsim/motor_node.c`**: a PI control loop.
* **`examples/hostsim/sensor_node.c`**: reports over the link, takes commands
  from it, and stops on a console keystroke.
* **`examples/hostsim/fleet_demo.py`**: three boards, a plant model in Python,
  two injected faults and a matplotlib plot.
* **`tools/hostsim_test.py`**: 23 checks. Needs only gcc.
* **`tools/hostsim_difftest.py`**: runs the same image both ways and compares.
* **`HOSTSIM.md`**, plus links from `BOARDS.md` and `BAREMETAL_ARM64.md`.

## Verification

* `tools/hostsim_test.py` -> **23 pass, 0 fail**
* `tools/hostsim_difftest.py` -> **8 pass, 0 fail** — hostsim and armulator
  agree on the computation, the fault count (2), the tick counts, spurious and
  unexpected counts, and completion
* mutation: making the host timer double-count drops the difftest to 7/8 with
  a diagnostic naming model drift as the likely cause
* regressions unchanged: `armulator_boards_test.py` 16/0,
  `board_machine_test.py` 20/0, armulator 1423/0

Injected faults are checked end to end rather than only at the API: with the
link forced down, the controller counts 7 drops and the firmware's own
`lost=7` counter agrees -- the error reaches application code instead of being
swallowed by the model.

### Bugs found while building it

* `sim_uart_feed` filled a receive buffer **no firmware function could read**
  — dead code until `uart_getc`/`uart_rx_ready` were added.
* The controller ran ahead of the simulation: `sim_step` waited on an
  `app_waiting` flag the application had set on its *previous* block, so it
  returned immediately. Timer interrupts were then credited against granted
  rather than consumed time, giving 51 ticks where 30 were expected.
* A stuck encoder froze at zero rather than at its current value, because the
  held reading was captured lazily on the next read instead of when the fault
  was injected. Found by `hostsim_test.py`, not by hand.
* The first fault-recovery attempt used `sigsetjmp` inside `exc_expect()`,
  whose frame is dead by the time the fault arrives. Replaced by a `SIGSEGV`
  handler that maps a page at the faulting address and returns, re-executing
  the instruction — which needs no instruction decoding.
* The fleet demo claimed integral windup to saturation; the firmware's
  integral clamp prevents it. Replaced with a stall detector, which is what
  the run actually shows.

## Still not done

* **No ARM is executed**, so code generation, instruction selection, the boot
  sequence and the vectors are untested on this path.
* **No MMU**: `mmu_enable()` sets a flag. No translation, no ESR, no FAR.
* **No register-level device behaviour.** The drivers in `baremetal64/` are
  not used on this path at all.
* **`examples/hostsim/motor_node.c` does not build for a board** — it needs a
  PWM and quadrature driver that does not exist in this tree, and fails at the
  link rather than being quietly substituted.
* The difftest compares 8 extracted facts. MMIO interception would widen that
  to every register write, and is the obvious next step if this path is going
  to carry driver work as well as system work.
* **Not run on physical hardware.**

# hostsim: a socket bridge, and an accelerator seam

Builds on the previous section. Two additions, both to the host path only;
armulator and every existing path are untouched.

**New: `tools/hostsim_net.py`, `hostsim/accel.c`, `hostsim/accel.h`,
`hostsim/accel_cuda.cu`, `examples/hostsim/vision_node.c`,
`examples/hostsim/vision_demo.py`. All need `git add`.**

## 1. A fleet can join a real dev-ops test

`SocketBridge` connects a `Fleet` to a TCP service. It is an *endpoint*, not a
special case: it exposes the same `link_pop_all` and `link_push` a board does,
so a router addresses it identically and the service on the far end never
learns the boards are simulated. `Fleet` gained `endpoints=` and a
`participants` property; `broadcast` includes endpoints.

Messages are framed with a four-byte big-endian length, because link traffic
is whole messages and TCP is not. `Newline` is provided for line-oriented
peers and `codec=` takes anything else. `SocketBridge.listen()` accepts a
peer, `connect()` dials one, and `EchoService` is a throwaway peer for tests.

Bridges are polled, never blocked on, so a slow peer cannot stall the virtual
clock. The cost is that a bridged run is not deterministic the way a closed
fleet is -- documented, with the advice to assert on what was exchanged and to
drain first.

## 2. An accelerator seam, for the Jetson AI case

A Jetson exists for the GPU beside the CPU, and firmware running an inference
per frame is precisely what armulator cannot study: about a day per simulated
second, and no GPU modelled to run the inference on.

`hostsim/accel.h` is the seam -- one call taking a frame and returning a
classification, which is its shape on a real board too. `accel.c` is plain C
and is always built and tested. `accel_cuda.cu` is the same arithmetic as a
kernel, selected with `--cuda`. Everything is integer so the two must agree
*exactly*; `accel_selftest()` runs both over generated frames and counts
disagreements.

**The CUDA path has never been compiled or run.** There is no GPU and no CUDA
toolkit in the environment this was written in. `accel_cuda.cu` carries a
prominent warning saying so, and `--cuda` refuses outright when `nvcc` is
missing rather than falling back -- a silent fallback would look exactly like
a GPU build that was merely slow.

Frames come from the controlling process, so they can be a dataset, a
generator or a camera. There is one frame of slack rather than a queue: an
uncollected frame is overwritten as a camera DMAing into a double buffer
would, which is why `vision_node.c` counts dropped frames.

## Verification

* `tools/hostsim_test.py` -> **52 pass, 0 fail** (was 23)
* `tools/hostsim_difftest.py` -> 8 pass, 0 fail — unchanged
* `examples/hostsim/vision_demo.py` -> three nodes, 240 inferences each,
  **599 reports sent and 599 received over real TCP**, stable across runs,
  in ~0.6 s of wall clock
* regressions unchanged: armulator 1423/0, `armulator_boards_test.py` 16/0,
  `board_machine_test.py` 20/0, `irq_timer_test.py` 22/0,
  `board_tools_test.py` 12/0, qemu boots on virt and raspi3

### Found while building it

* The demo first reported **597 of 599** reports received. Not a bug in the
  bridge: reports were still in the outbound buffer and in the socket when the
  run ended. A drain phase fixed it, and the episode is documented, because
  needing to drain before asserting is a real integration concern rather than
  a simulation artefact.
* A framing test of mine asserted that eight bytes of a nine-byte message
  should decode. The codec was right and the test was wrong; it now checks
  both sides of the boundary.

## Still not done

* **The CUDA kernel is unverified.** The C reference is tested; the kernel has
  never run. `accel_selftest()` is the check to run first on real hardware.
* **A bridged run is not reproducible** in the way a closed fleet is, since
  real network timing is involved.
* **`SocketBridge` handles one peer.** No reconnection, no TLS, no
  multiplexing; a closed peer is reported and pushes fail rather than raising.
* Everything from the previous section still stands: no ARM executed, no MMU,
  no register-level device behaviour, never run on physical hardware.
