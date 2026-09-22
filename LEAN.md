# Lean in Crust

Two things in this repository are checked by a proof kernel, and they are
different in kind. Keeping them apart is most of what this document is for.

## 1. Contract certification, at compile time

`shivyc/proofs.py` reads a SIMD contract, and when RosettaMath is available it
asks `crustproof.py` for a certificate: the contract becomes a term of the
Calculus of Constructions, and a proof is built and type-checked before the
compiler is allowed to drop a bounds check.

This runs on every compile, and it never touches the Lean binary.
`crustproof.py` imports `lean4.py`, which is a kernel written in Python and
named after Lean 4 rather than being it. Putting a 543 MB toolchain on the
compile path would be indefensible, and there is no `.lean` file here for it
to read.

```sh
make install_proofs     # clone RosettaMath as a sibling checkout
make check_proofs       # which kernel is live, and what it says
```

Absent RosettaMath, `proofs.backend()` reports `built-in`, contracts are read
by the compiler alone, and every uncertified case keeps its scalar tail. No
kernel is a supported configuration, not a broken one: a proof that does not
arrive degrades to the conservative path, never to a wrong answer.

## 2. Kernel verification, offline

RosettaMath's `crustos_eq.py` proves theorems about a Python *model* of
CrustOS -- `tick`, `sched`, `scheme_of`, `accepted` -- and exports them to
Lean 4, which checks them independently. That second kernel is the point: it
was written by other people in another language, and it agrees.

```sh
make install_lean       # a real Lean toolchain, for RosettaMath's own targets
make test_model         # the model against the shipped scheme layer
```

### The transcription problem

A theorem about a model is a theorem about a model. Nothing about proving
`accepted_bounded` makes `crustos/schemes.py` correct, and for a while the two
were not even in agreement: the model routed a bare `sys` to the `sys:` scheme
and the kernel routed it to nothing, because the model had no counterpart to
the kernel's `if idx <= 0` guard.

That gap was not carelessness. `hoare.py` refused a `return` inside an `if` or
a loop, so the model had to be written as an accumulator, and the guard was
lost in the rewrite -- the tool's expressiveness leaking into the
specification. `tests/test_crustos_model.py` now checks three things:

**Behaviour.** Both implementations run over a corpus of URLs and must agree.
The model side is not re-typed: `crustos_eq.build()` hands back the same
procedures that were compiled and proved, and the test evaluates *those terms*
through the kernel's evaluator. A copy of the model written out again in the
test would be a third transcription, and then there would be two gaps.

**Shape.** `scheme_of` and its model must have the same control-flow
skeleton -- statement kinds and nesting, names dropped. A corpus can miss an
input; a shape cannot miss a branch.

**Coverage.** Every function `crustos/schemes.py` exports is either modelled
or listed with a reason. A new one lands as a failure rather than as silence.

Known gaps are recorded in `EXPECTED_DIVERGENCES` and `EXPECTED_SHAPE_GAPS`,
and a test asserts each one *still* diverges. Closing one turns the file red,
so it cannot be done quietly.

### What is not covered

Both modelled functions in `crustos/schemes.py` now match the kernel
statement for statement -- the control-flow skeletons are compared, not just
the behaviour. Getting there took three additions to `hoare.py`, each a
rewrite in front of the lowering rather than a change to it: an early
`return` becomes a first-wins accumulator; `for x in xs` over a list becomes
the `while` it always was, with the variant supplied; `xs.append(x)` becomes
`xs = snoc(xs, x)`. What the model still cannot say is `-1`, so `SCHEME_NONE`
is `len(names)` and the kernel's `idx <= 0` is `idx == 0 or idx == len(url)`,
and those two paraphrases are the whole remaining distance for the scheme
layer.

`crustos/kernel.c` is not covered at all, and not for want of trying. It is
Rust and C, so there is no source for `hoare.py` to read. The modelled
`Context` (`current`, `nthreads`, `ticks`) has no counterpart in the kernel's
(`pid`, `prio`, `ticks`, `frames`, `state`, `entry`, `stack`, `reg_class`,
`switches`); its nearest real analogue is `used <= 16` on `Kernel`. So the
`tick` and `sched` theorems are about a model of nothing that ships. They are
honest proofs about a state machine and they are not proofs about this
scheduler, and the distance between those two claims is the whole reason this
section exists.

## 3. The IR lift

`shivyc/ilproof.py` is the seam between the two. It takes a function *after*
the front end -- the IL every Crust front end produces, C or Rust or rpython
-- rebuilds the structured control flow the front end flattened, and writes
it back out as Python in `hoare.py`'s dialect. The same `read_procedure`
compiles it, the same proof helpers apply, and the same `leanexport` sends
the result to Lean.

```sh
make test_ilproof       # the lift against the binary, and by the kernel
```

What it reaches today, from `crustos/kernel.c` through the real front end:

```
def elf_regs_for_class(cls: 'Nat') -> 'Nat':
    if (cls == 0):
        return 6
    if (cls == 1):
        return 10
    if (cls == 2):
        return 15
    return 23
```

That is real kernel code -- the scheduler sizes its register save/restore
from it -- lifted from IL, not transcribed. `by_every_bool` proves
`∀ cls, elf_regs_for_class cls <= 23` by splitting each guard, and Lean 4
accepts the export with no axioms. The false bound `<= 15` is refused. A
counted loop lifts with its invariant and variant derived, and the kernel
proves it terminates for every bound. Five example shapes are compiled with
the real compiler, run, and compared against the lifted terms through the
kernel's evaluator: the same discipline as the scheme layer, applied to
compiled code.

**What is claimed.** The lift is over Nat: subtraction is truncated where
C's goes negative, and nothing is said about overflow. A theorem about the
lifted function is a theorem about that model of the arithmetic. The lift
refuses rather than approximates: `goto`, `break`, `switch`, pointers,
structs, calls and `void` all come back as a `LiftError` naming the
construct.

A right shift by a literal lifts as division by the power of two --
`addr >> A::PAGE_SHIFT` is `addr // 4096` -- and for once the claim is not a
model: on an unsigned value the two are the same number, with no truncation
and no wrap. That reaches the kernel's two `frame_of` functions, the ones the
heap sizes itself from. `/` and `%` lift with it, with `x // 0` being 0 in the
model where C leaves it undefined. `divb` in the prelude is `modb`'s counter
incrementing where `modb` resets, so it needs no well-founded recursion
either, and Lean accepts it.

What it cannot yet do is *prove* anything about `frame_of`. The lifted
function is fine; `frame_of(8192) == 2` should be settled by computation and
is not, because `normalize` opens the definition under its binder, where
`divb addr 4096` has a free variable and cannot accelerate, and the step
function's `eqb _ 4096` unfolds `leb` four thousand levels deep. Closed
evaluation is not the problem -- `divb 131072 65536` is 0.66s -- the
unfolding order is, and that is a `lean4.py` matter, not a `hoare.py` one.

**What is not reached.** 10 of the kernel's 107 functions lift. The tally of
refusals is the roadmap, and it is nearly one item: 93 of 97 are `ReadAt`,
`AddrOf` and `SetAt` -- memory access. The kernel's stateful functions
(`schedule`, `spawn`, `sys_open`) read and write fields of `Kernel` and
`Context` through pointers, and lifting those means lifting struct access
into the record types `hoare.py` already has (`Context.with_ticks`,
`state_invariant`). That is the step that would connect this to the modelled
`Context` from the other direction, and it is the next one.

## 4. The loader

`crustos/elfcheck.py` is what `elf.c` asks before it maps an ELF: are the
`PT_LOAD`s ascending and disjoint, is the entry inside one, is the register
hint a class the scheduler sizes. It is rpython, it decides rather than
dereferences, and it is the first thing in the tree written *for* the proof
side rather than modelled after the fact -- every result is 1 or 0, every
loop counts up, every list is read only behind a length check, and
`reg_class_ok`'s guard is `<=` rather than `>` so that the guard is the
theorem. Linux runs an ELF with no such check; so did this loader until
`elf_load_path` learned to return `-6`.

```sh
make test_elfcheck      # the validator, four ways, and the loader on real ELFs
```

RosettaMath's `elfcheck_eq.py` is the same four functions for the kernel,
with two theorems Lean 4 accepts with no axioms:

```
reg_class_sized : ∀ cls, reg_class_ok cls == 1 → cls <= 3
accept_sized    : ∀ v m e cls, accept_image v m e cls == 1 → cls <= 3
```

The second composes with `elf_regs_bounded` from section 3: an image the
loader accepts has a class the scheduler sizes, and that sizing is `<= 23`.
It is the first theorem here that spans two functions of the shipped kernel.

Both are guard chains and `by_every_bool` settles them, after two changes to
how it picks a guard. It now splits the innermost open guard first, so a
guard written in terms of another -- `accept_image` tests
`eqb (reg_class_ok cls) 0`, and `reg_class_ok` is itself an `ite` on
`leb cls 3` -- computes once the inner one is decided. And a guard that
*computes* to a literal after earlier splits is written in, not split:
splitting it would ask for a proof of the branch computation rules out, and
there is none. Both are general; `elf_regs_bounded` and `crustos_eq` are
unchanged under them.

`tests/test_elfcheck_model.py` checks the validator the way
`test_crustos_model.py` checks the scheme layer -- behaviour over a corpus
through the kernel's evaluator, shape, coverage, Lean -- and two ways it
does not. The same file is compiled by ShivyCX through `#include`, as
`kernel.c` takes it, and compared with itself run as Python. And `elf.c` with
the validator wired in is run on a ShivyCX-built ELF and on copies with
corrupted headers: overlapping loads, descending loads, an entry in no load,
an entry one past the end. Each is refused before anything is mapped. Before
the validator, every one of them loaded, and for the entry a megabyte past a
16 KB image `elf_run_guest_fn` would have jumped there.

**What is claimed, and what is not.** The theorems are about the register
class. The theorems the loader wants -- accepted implies no two loads
overlap, accepted implies the entry is inside a load -- are facts about the
loops in `loads_ordered` and `entry_in_load`, and those need the
loop-invariant machinery `crustos_eq.py` spends sixty lines on for
`accepted`. Both loops are modelled, with their invariants and variants, and
are in the Lean export with their `result <= 1` proved -- the first
postconditions in this tree to go through a loop that `return`s early.
That took two things in `hoare.py`: the lowering's `_return_value` had been
reserved against being *read* as well as written, so no invariant could
bound it; and there was no weak-head step for `fst`/`snd` over `mk`, which
is the one reduction a post-pass claim needs. `early_return_bound` is the
recipe -- entry by splitting the pre-loop guards, preservation by splitting
the body's guards and then `_returned`, exit by splitting the final
`_returned` -- and `leanos/memmap.py`'s three early-return loops go through
it unchanged. The overlap theorem is the next step. The
corpus and the four corrupted ELFs are what stand in for it meanwhile, and
the tamper that swaps `<` for `<=` on the entry bound -- the loader's
off-by-one -- is caught by the corpus on three rows.

Addresses are `i64` in the code and Nat in the model, so `vaddr + memsz`
wraps past 2**63 in one and not the other. A user-space image is nowhere near
it, and it is the one distance between this file and its model.

## 5. Rust as a proof source

The IL lift in section 3 reaches Rust only as far as the IL keeps what a
proof needs, and for idiomatic Rust that is not far. A `match` is lowered to a
`switch` the lift sees as `goto`s, a tail `if` expression leaves a dangling
return, `&&` becomes jumps, and a `u32`'s width is gone. Crust has the Rust
structure at parse time, so the plan is a *source-level* lift from Rust into
`hoare.py`'s dialect, with the model generated from the file that ships. That
removes the transcription this document spends so long guarding against.

The first step is in: Crust reads contracts. `#[requires]`, `#[ensures]`
(with `result` and `old(..)`), `#[invariant]` and `#[variant]`, in Creusot's
and Prusti's syntax, are checked at runtime today (see "Contracts" in
`CRUST.md`). The same clauses are what the lift will hand to
`read_procedure` as `ensures` and loop annotations. The runtime check keeps
the corpus discipline available before any proof exists: a contract the
tests break is a contract no proof will establish.

The second step is in too: `shivyc/rustproof.py` lifts a Rust function from
its source, contracts included, into `hoare.py`'s fragment.

```sh
make test_rustproof     # the lift, the model against the binary, the proofs
```

`leanos/regs.rs` is `elf_regs_for_class` as Rust writes it: a `match`, with
the bound stated on the function.

```rust
#[ensures(result <= 23)]
#[ensures(result >= 6)]
pub fn elf_regs_for_class(cls: u32) -> u32 {
    match cls { 0 => 6, 1 => 10, 2 => 15, _ => 23 }
}
```

It lifts to what a person would write, the same fragment the IL lift
produces from the C, with its postconditions beside it:

```
def elf_regs_for_class(cls: 'Nat') -> 'Nat':
    if (cls == 0):
        return 6
    elif (cls == 1):
        return 10
    elif (cls == 2):
        return 15
    return 23
```

`by_every_bool` proves both postconditions for every `cls`, and refuses the
false bound `<= 15`. Lean 4 accepts the export with no axioms. The Rust model
and the C model lifted from `crustos/kernel.c` agree on every class. This is
the first function in the tree whose model and theorem are both read off the
file that ships, so there is no transcription for a shape test to guard.

**What the lift reaches.**
- Unsigned integers (as Nat) and `bool`.
- `let`, assignment and compound assignment.
- `if` and `match` as statements, values or tails. `match` covers literals,
  `|`, ranges, `_`, bindings and guards, lowered to an `if`/`elif` chain.
- Early `return`.
- `for i in a..b` and `a..=b`, as a fold over `range(b - a)` shifted by `a`.
  Here Nat's truncating `-` is exact: an empty range runs no times.
- `while` with `#[invariant]` and `#[variant]`, which become the fragment's
  loop annotations.
- Calls to other functions in the file, lifted with it and handed over as
  signatures.
- `>>`/`<<` by a literal, widening `as`, `&&`, `||`, `!`, and `==>` in
  contracts.

**What is claimed.**
- Unsigned arithmetic is Nat, the same model as the IL lift's. The difference
  is that the Rust type's width is still known here, which is what will let
  overflow and underflow become obligations of their own. That is the next
  step.
- `#[requires]` becomes the fragment's leading `assert`s and `#[ensures]` its
  postconditions. These are the clauses Crust checks at runtime, so a
  contract the tests break is one no proof will establish.
- In an `ensures`, a parameter means its value at entry, which is `old(x)`. A
  clause naming a `mut` parameter outside `old` is refused rather than read
  one way or the other.

**What is refused, by name:** signed integers, references, fields, methods,
indexing, `loop`/`break`, macros, recursion, bitwise `&`/`|`/`^`, narrowing
`as`, and quantified clauses. The refusals are the roadmap, as the IL lift's
tally is.

**A known gap.** `class_covers` in `regs.rs` composes the two functions: the
class `class_for_regs(n)` picks is sized for at least `n` registers, for
every `n <= 23`. It lifts, but `by_every_bool` cannot prove it. The
postcondition needs `n <= 6` to give `6 >= n`, which is reasoning about the
range a guard establishes, and splitting then computing does not do that.
`tests/test_rustproof.py` asserts the proof still fails, so closing the gap
cannot happen quietly. Meanwhile the compiled function, with its `ensures`
checked at runtime, runs over every `n` its `requires` admits.
