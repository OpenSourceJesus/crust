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

**What is not reached.** 8 of the kernel's 107 functions lift. The tally of
refusals is the roadmap, and it is nearly one item: 92 of 99 are `ReadAt`,
`AddrOf` and `SetAt` -- memory access. The kernel's stateful functions
(`schedule`, `spawn`, `sys_open`) read and write fields of `Kernel` and
`Context` through pointers, and lifting those means lifting struct access
into the record types `hoare.py` already has (`Context.with_ticks`,
`state_invariant`). That is the step that would connect this to the modelled
`Context` from the other direction, and it is the next one.
