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

`accepted` agrees behaviourally but not structurally. The kernel writes `for
url in urls.split(",")` and `out.append(i)`; `hoare.py` has neither, because
its `for` must be over `range(n)` and a store-passing lowering has no
mutation. The model hoists the split and rebuilds the list with `snoc`.
Closing this means teaching `hoare.py` to iterate a list, which is the next
piece of work.

`crustos/kernel.c` is not covered at all, and not for want of trying. It is
Rust and C, so there is no source for `hoare.py` to read. The modelled
`Context` (`current`, `nthreads`, `ticks`) has no counterpart in the kernel's
(`pid`, `prio`, `ticks`, `frames`, `state`, `entry`, `stack`, `reg_class`,
`switches`); its nearest real analogue is `used <= 16` on `Kernel`. So the
`tick` and `sched` theorems are about a model of nothing that ships. They are
honest proofs about a state machine and they are not proofs about this
scheduler, and the distance between those two claims is the whole reason this
section exists.

Reaching it would mean a Hoare pass over Crust's own IR rather than over
Python source -- the compiler already has the control-flow graph the prover
would need. That is a larger project than anything above and is not started.
