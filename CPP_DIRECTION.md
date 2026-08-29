# A direction for C++ — the subset that says no

Crust's position paper. Why the C++ subset exists, what it believes, and why
"a smaller C++ that refuses things" is a direction for the language rather
than a retreat from it. Everything asserted about the subset here is
implemented in this tree and tested; the point of the paper is to say what
it adds up to.

---

## 1. The situation, honestly stated

Three different groups have reached the same conclusion about C++ from three
different directions, and it is worth noticing that none of them is wrong.

**The security establishment.** The ONCD report (Feb 2024), the NSA/CISA
memory-safety information sheets, and the corresponding EU guidance say,
in plain language, to migrate away from C and C++. Microsoft and Google both
put memory-safety at ~70% of their serious vulnerabilities. Whatever one
thinks of the framing, the underlying number is theirs, measured on their
own codebases, and it has not been rebutted — the WG21 responses to it were
papers about future plans, written quickly, several of them by Stroustrup in
the space of a few months. Nobody writes four hurried defenses of a language
that is fine.

**The safety-critical world got there decades earlier.** JSF AV C++ — the
standard the F-35 flies on — bans exceptions (`throw`, `try`, `catch`),
bans dynamic allocation after initialisation, restricts inheritance, and
treats most of the standard library as inadmissible. MISRA and AUTOSAR C++
make the same cuts. Read as a whole, these documents are a forty-year field
experiment whose result is: **the safe subset of C++ is small, and it is
nothing like the language WG21 keeps growing.** The most safety-critical
users of C++ have never once asked for a larger language.

**Working programmers, by revealed preference.** The fish-shell thread
(HN 34589687) is the honest version of the argument, and the strongest
points in it are not about the borrow checker. They are about **everything
around the language**: that setting up a project is "death by a thousand
cuts"; that the build and dependency story is CMake archaeology; that a
newcomer must learn every standard since C++11 because every codebase
stopped at a different one; that people who *want* to like C++ — "I would
like to be a better C++ developer" — walk away, and the ones who stay are
the ones already invested. That is the brain-drain mechanism, and it
operates regardless of whether TIOBE goes up or down.

Meanwhile the committee's answer is C++26, and the mood around it is the
tell. Each release adds features; none removes a hazard; "profiles" promise
subsetting later, by annotation, atop the whole language, with the whole
language still underneath. And the serious energy has visibly left the
building: Carbon, Val, Circle, Cpp2/cppfront are all *committee-adjacent
people* concluding that the fix will not come from inside. The successor
languages are the resignation letters.

## 2. The diagnosis: C++ has never once said no

Every hazard the reports count has the same root: C++ accumulates and never
subtracts. Exceptions and `-fno-exceptions` both exist, so every library
must work under both, so neither is dependable. RTTI is paid for by programs
that never use it, so it is turned off, so `dynamic_cast` cannot be relied
on either. Virtual inheritance, implicit conversions, UB-as-optimisation,
five overlapping initialisation syntaxes — each is somebody's compatibility
constraint and nobody's choice. The language's actual semantics is the
union of every decision it declined to make.

The successor projects mostly answer this with a *new language next to*
C++ (Carbon, Val) or a *new syntax over* it (Cpp2). Both are honest
approaches. But there is a third answer, and it is the one this tree has
been building without initially calling it that:

> **Take the subset the safety-critical world already proved out, give it
> the modern semantics C++ never delivered cleanly, and make everything
> outside it a compile-time refusal with an explanation — not undefined,
> not linted, refused.**

Not a successor language. A **successor discipline**, applied to the
language people already have, producing C anyone can read.

## 3. What this looks like when it is real

The claim that a subset can lead is empty unless the subset exists, takes
real code, and its refusals are as designed as its features. This one
exists. `tools/cpprust.py` lowers it to C99, its test surface is ~640 tests
across five suites plus differential runs against `g++` on identical
sources, and it eats real litehtml sources whole. Concretely, the design
positions and their status:

**Refusals are the contract.** Every rejection names the file and line,
says *why* the construct is outside the subset — the actual mechanism, not
"not supported" — and says what to write instead. Refusals are tested as
pinned behaviour, because for this compiler a refusal *is* the deliverable.
The CPPRUST.md guiding rule is the whole philosophy in one line: anything
the lowering cannot do **correctly** is reported, not approximated. When
tier-1 multiple inheritance was built here, the moment an unadjusted
interface cast was found to compile-and-dispatch-wrong, the conversion was
*refused* until the adjustment existed. Silent wrongness is the only
unforgivable output.

**Exceptions: refused, and replaced with the checked model.** `throw` is
out, permanently, in agreement with JSF/MISRA/AUTOSAR. In its place
(designed, CPPRPY.md §5): `try`/`except` with minipy's semantics — an error
is a value in a state slot, propagation is a checked flag after fallible
calls, destructors run because the error path reuses the *ordinary*
epilogue, fallibility is part of the function's signature, and an unhandled
error is a compile error rather than `terminate()`. No unwinder, no unwind
tables, bounded stack, every control edge visible in the generated C. It is
spelled `except`, not `catch`, precisely so a reviewer working to a
standard that bans `catch` can see at a glance it is not that thing. This
model is not hypothetical — it is the one minipy runs on today, including
`try/finally` and `with` built over it with zero new opcodes.

**RTTI: the descriptor, with a stated cost.** `dynamic_cast` (pointer form)
and `typeid` work under `--rtti`: the vtable is prefixed with a type
descriptor, the vptr a polymorphic class already carries *is* the
descriptor pointer, so the per-object cost is zero and the per-class cost
is one static structure. The cast is a base-chain walk whose length is
fixed at compile time — statable in a review, which is what the standards
actually demand. The reference form of `dynamic_cast` stays refused because
it throws, and there is nothing for it to do here.

**Inheritance: one layout base, any number of interfaces, no virtual
bases — ever.** Tier-1 MI is in: secondary bases carry no data, cost one
vptr at a compile-time offset, dispatch through per-class tables verified
byte-identical against g++. Virtual inheritance is refused as a *design
position*, with the reason in the diagnostic: a virtual base's offset
depends on the most-derived type, which turns field access into a runtime
table lookup and `dynamic_cast` into an unbounded search — the exact costs
this subset exists to not have. It is the clearest case in the language of
"expressible" beating "predictable", and refusing it is the subset having
an opinion rather than a gap.

**Memory: ownership by construction, and honesty about the rest.** The
subset's classes destroy deterministically; a C++ `~T()` and a Rust
`impl Drop for T` lower to the same `T_drop` symbol; owning types refuse to
be copied for the same reason they know how to be destroyed. This is not a
borrow checker and should not be marketed as one — the claim is smaller and
true: *the constructs that produce the classic UB are not accepted*, and
what remains lowers to C that a reviewer, a MISRA checker, and every
sanitizer ever written can see. The path to stronger guarantees runs
through the same discipline: refuse first, admit patterns back as they can
be proven.

**The output is C99 you can read.** No mangled soup: `Square_area`,
`Shape_drop`, a `struct` per class, a visible vtable. This is quietly a
safety feature — the trusted computing base is gcc or any C compiler the
certification people already accept, and the audit trail is a text file.
It is also the portability story: the same output runs on x86-64, ARM64
baremetal, RISC-V and WASM, which is more targets than most successor
languages ship.

**One object model across languages.** A C++ class here subclasses an
rpython class; the override is reached through the *other* language's
dispatch; `isinstance` walks one chain that crosses the boundary; a shared
class digest (`--decls` / `--emit-decls`) carries the layout both ways.
That is the answer to "but the ecosystem": interop is not an FFI
appendix, it is the same descriptor.

## 4. Why this is a *direction* and not a bunker

The strategic bet, stated plainly:

**Bet 1 — the subset is the language.** Ask what the ONCD report, JSF, and
the tired core-guidelines "best practices" lists have in common: each is an
attempt to describe a smaller C++ that behaves. They differ only in
enforcement — advice, checklists, annexes. A compiler that *refuses* is
the strongest possible enforcement, and the first mover that makes the
smaller language load-bearing gets to define it. WG26-era C++ cannot do
this, structurally: compatibility is its constitution. We are not bound by
it — cpprust already refuses most of C++ and translates litehtml anyway,
which is the existence proof that real code lives inside the subset.

**Bet 2 — safety by subtraction beats safety by addition.** Profiles,
contracts, hardened stdlib modes: additive safety, opt-in, forever
diluted by the unsafe majority around it. The historical evidence runs the
other way — every credible safety story in this space (JSF, MISRA, seL4's
C subset, SPARK against Ada) is a *subtraction* story. Subtraction also
compounds: because there are no exceptions, destructors need no unwinder;
because there are no virtual bases, casts are walks; because layout is
static, the whole object model fits one page of CPPRUST.md and a reviewer
can hold it in their head. Small is not the price of safety. Small *is*
the safety.

**Bet 3 — the tooling complaint is the adoption wedge.** Reread the HN
thread: the defection stories start with builds, not lifetimes. This
tree's answer is one Python file per direction, no CMake, C out — and the
Crust side already gives the `.cpp`/`.rs`/`.py` mix a single translation
story. "A C++ you can *start* in an afternoon" attacks the exact reason
the next generation is not showing up, and none of the committee's
roadmaps even acknowledges that front.

**Bet 4 — leadership is vacant.** That is the user-visible fact of
2023–2026: the founder writes defenses, the committee ships features into
indifference ("C++23 is like: who cares"), the talent builds successors,
and the migration mandates arrive anyway. Nobody is leading the *existing*
C++ community toward a defensible core. That is the open seat. Taking it
requires no permission — only a compiler whose opinions are tested, which
is what this repo has been accumulating one refusal at a time.

## 5. The near program

In the order the leverage runs, each with its acceptance test:

1. **`try`/`except`** (CPPRPY.md §5) — the flagship differentiator, since
   "error handling that JSF would accept" is a sentence no other C++ can
   say. Acceptance: differential semantics against the minipy model, and a
   demonstration that a fallible call which is not handled fails to
   *translate*.
2. **Tier-1 MI completion** — interface conversions for the remaining
   operand shapes (call results, array elements) via the existing symbol
   table, replacing today's refusal. Acceptance: the refusal tests flip to
   lowering tests, g++-differential.
3. **The py2c digest consumer** — `import pool_cpp` resolving a C++ class
   as a base. The hook is located: `load_xmod`'s not-found path falls back
   to `<module>.decls.json` and synthesizes the registry entry, marked
   external so emission skips it and calls lower to the digest's symbols.
   Acceptance: the mirror of `test_cpprpy_decls.py`'s boundary suite, run
   from the rpython side.
4. **`std::not_too_much`** — name the tiny library the subset actually
   admits (the existing `vector`/`string` cores), and publish the refusal
   list for the rest with reasons, the way CPPRUST.md already documents
   refusals. The standard library is where "lean reboot" is won or lost.
5. **A safety-audit mode** — `--audit` emitting, per translation unit, the
   list of properties the output C provably has (no unwinding, no dynamic
   dispatch beyond listed tables, no allocation past `main`, cast-cost
   bounds), machine-checkable against the generated C. This turns the
   design positions into evidence a certification process can consume,
   which is the artifact the JSF world actually buys.

## 6. What we do not claim

Because the credibility of a refusing compiler is that it also refuses to
oversell itself. This is not memory safety in the Rust sense; there is no
borrow checker, and unsafe patterns expressible in the subset remain
expressible. It is not ABI-compatible with existing compiled C++, and it
will never link against a `libstdc++` full of exceptions. It does not run
template metaprograms, and it never will — monomorphisation of the
container cores is the ceiling. Every one of these is a cut we would make
again, and saying so out loud is the difference between a direction and a
pitch.
