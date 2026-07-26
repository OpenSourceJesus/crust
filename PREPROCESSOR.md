# ShivyC preprocessor

The preprocessor (`shivyc/preproc.py`) was rewritten from a 66-line
include-only stub into a real C preprocessor, because compiling library code
like musl needs far more than `#include`.

## What it supports

* **`#include`** -- quoted and angle-bracket headers, plus macro-computed
  includes (`#include SOME_MACRO`).
* **`#define`** -- both object-like (`#define N 5`) and function-like
  (`#define ADD(a,b) ((a)+(b))`) macros, with:
  * **`#`** stringize,
  * **`##`** token paste,
  * **variadic** macros via `...` / `__VA_ARGS__`.
* **`#undef`**.
* **Conditionals**: `#if`, `#ifdef`, `#ifndef`, `#elif`, `#else`, `#endif`,
  arbitrarily nested, with a full integer **constant-expression evaluator**
  (ternary, `||`, `&&`, bitwise, equality/relational, shifts, additive,
  multiplicative, unary `+ - ! ~`, parentheses, C truncating division) and the
  **`defined`** operator (`defined X` and `defined(X)`).
* **`#error`** (reported); `#pragma`, `#line`, etc. ignored.
* **Backslash-newline line continuation** in directives (e.g. a `#if` spanning
  several physical lines, as in musl's `features.h`).

Macro expansion uses **hide sets**, so a macro is never re-expanded within its
own expansion -- `#define FOO FOO` and mutual recursion terminate, matching the
C standard's "painted blue" rule.

## Lexer changes this required

Tokenization happens *before* preprocessing, so the lexer had to learn things
the preprocessor depends on:

* **Integer constants** beyond plain decimal: hexadecimal (`0x1F`), binary
  (`0b101`), octal (`010`), and `u`/`l` suffixes (`10UL`). Previously
  `match_number_string` accepted only `str.isdigit()`, so `#define MASK 0x0F`
  silently lost its value. `shivyc/tokens.py:parse_c_int` parses these into
  values for codegen.
* **Logical-line tagging.** The lexer already spliced backslash-newline
  continuations, but the spliced tokens kept their original physical line
  numbers; each token now also carries a `logical_line` index so the
  preprocessor groups directives correctly. Physical positions are unchanged,
  so diagnostics still point at the real line.
* `##` and `...` are not lexer tokens (they are preprocessor-only); the
  preprocessor rejoins the adjacent `#`/`.` tokens the lexer emits.

## Tests

`tests/test_preproc.py` -- 24 tests that compile and run programs whose exit
code depends on correct preprocessing (object/function/variadic macros,
paste/stringize, all conditional forms, the expression evaluator, `defined`,
`#undef`, the recursion guard, line continuation) plus the lexer's
integer-constant support. The full suite is 155 tests.

## Where this leaves musl (measured, not assumed)

Running musl's `src/string/strlen.c` through the front-end
(tokenize -> preprocess -> parse) now **preprocesses cleanly**: real musl
headers (`features.h`, `stdint.h`, and the build-generated
`bits/alltypes.h`) expand to ~780 tokens with all conditionals resolved. The
preprocessor is no longer the blocker.

The *next* blockers are downstream and are **not** about macros:

1. **Lexer** -- *fixed.* `?` and `:` are now tokens (`question`/`colon`), so
   the ternary idioms in `alltypes.h` (e.g.
   `int __i[sizeof(long)==8?14:9]`) tokenize instead of failing. With this in
   place, musl's `src/string/strlen.c` compiles end-to-end through ShivyC and
   passes a differential test against the reference (it builds the
   byte-at-a-time path; the `#ifdef __GNUC__` word-at-a-time block is correctly
   excluded because ShivyC does not predefine `__GNUC__`).

2. **Parser / semantics** -- in progress. The following now work and are
   differential-tested against gcc where applicable:
   * **Conditional (ternary) expressions** `a ? b : c` (branch-correct, single
     arm evaluated) and comparison constant folding.
   * **Bitwise operators** `&`, `|`, `^` -- these were entirely absent before
     (the `|` and `^` tokens did not even exist), so this added the tokens, the
     correct precedence chain, IL/asm (`and`/`or`/`xor`), and folding.
   * **Bitfields** -- named and anonymous. Each field is masked to its declared
     width on write and read, with sign-extension for signed fields. Layout
     uses an own-storage-unit model (conforming, though not GCC-bit-packed --
     ShivyC structs are not ABI-packed regardless).
   * **GCC/C extension spellings** -- `__attribute__((...))`, `__restrict` /
     `restrict`, `__inline` / `inline`, `__extension__`, `_Noreturn`, etc. are
     accepted and ignored via a preprocessor prelude, so library headers parse.

   With these, **6 of 14 sampled musl `src/string/*.c` files now pass the full
   front-end** (tokenize -> preprocess -> parse -> IL): strlen, strcmp, strncmp,
   strcat, memcmp, strnlen. `strlen.c` compiles all the way to assembly and
   passes a differential test against the reference.

3. **The genuine remaining subsystems.** **Weak aliases are now done**:
   `__attribute__((weak, alias("target")))` (including musl's `__typeof`-based
   `weak_alias` macro) is recognized by a token-level pass
   (`shivyc/weak_alias.py`) and emitted as `.weak` / `.set` assembler
   directives. Verified on musl's `stpcpy.c`.

   Still not started, and deliberately not faked: `_Thread_local`/`__thread`
   TLS and `_Atomic`/atomics. **Inline `asm` is now supported for the subset
   the Minikraft unikernel actually uses** (see below). Smaller front-end
   items: `do`/`while` loops, designated initializers, compound/array
   initializers, and `sizeof`/casts on tagged types are now done; remaining are
   a few small parser gaps (see below) and some pointer cases of the
   conditional operator's result-type rule.

## Bare-metal / freestanding (Minikraft)

Toward compiling freestanding code, ShivyC now ships minimal `<stddef.h>` and
`<stdint.h>`, accepts pointer comparisons that differ only in qualifiers,
accepts a `(void)` parameter list in function *definitions*, supports
`do`/`while` loops, and supports brace (aggregate) initializers: arrays and
structs/unions, positional and designated (`.field` / `[index]`), partial
(zero-filled), inferred array size (`T a[] = {...}`), nested aggregates, and
scalar `= {x}`, in both automatic and static storage (static aggregates emit a
`.zero` block when all-zero, or an initialized data block otherwise). It also
supports a **Minikraft-scoped slice of inline assembly**
(`shivyc/il_cmds/asm.py`): bare side-effect templates (`mfence`, `hlt`, `sti`,
the empty barrier), port I/O with `a`/`=a` and `Nd` constraints, and a single
`m` memory operand (`lidt`), emitted verbatim inside an
`.att_syntax`/`.intel_syntax` toggle. This is intentionally not general
extended asm.

With these, 16 of the 23 sampled Minikraft kernel/lib files now compile fully
to assembly. Recent additions that unblocked files include compound bitwise/
shift assignment (`|= &= ^= <<= >>=`), variadic function prototypes
(`f(int, ...)`), the conditional operator's pointer result type
(pointer vs `NULL`/`void *`/compatible pointers), `switch`/`case`/`default`
(with fall-through), `enum`, `goto`/labels, adjacent string literal
concatenation (C phase 6, e.g. `"[INFO] " fmt`), and the `sizeof`/cast
tagged-type fix. A comment-only (token-less) translation unit, which
previously crashed the parser, now compiles to an empty object.

The remaining 2 of 23 files are not single-translation-unit compiler bugs: a
header that uses `UK_ASSERT` before its definition is included, which gcc only
accepts via a C89 implicit function declaration (`netdev_core.c`, a source
include-order quirk that ShivyC correctly rejects rather than silently papering
over); and a cross-translation-unit function reference (`thread.c`, which would
resolve at link time).

## Static initialization with address constants

A static-storage object may now be initialized with an address constant -- the
address of a function or of an externally-linked object -- which is emitted as
a relocation (`.quad symbol`) in the data section rather than as runtime code.
This covers function pointers in static structs (positional and designated),
ops-style vtables, arrays of function pointers, and a static pointer to an
external array, all differential-tested against gcc. (Internal-static-object
addresses and string-literal addresses are not yet handled, because their
final assembly names are assigned later in code generation; those still fall
back to the constant-initializer rules.) This unblocked `netlink_socket.c`,
bringing the sampled Minikraft kernel/lib files to 21 of 23 compiling fully to
assembly.

## Assorted C-conformance fixes

Several smaller gaps were closed, each differential-tested against gcc:

* A label may be followed by a declaration (`done: int x = 0;`), per C23/GCC,
  not only by a statement.
* Stray semicolons at file scope are accepted as empty declarations (common
  after a function-definition macro invocation, e.g. `REGISTER(x);`).
* The GNU `, ##__VA_ARGS__` paste deletes the preceding comma when the
  variadic arguments are empty (so `LOG("hi")` and `LOG("%d", n)` both work).
* Flexible array members (`struct { ...; T name[]; }`) are laid out with an
  offset but no size, and must be the last member.
* `__builtin_expect(x, c)` evaluates to `x` (used by `likely()`/`unlikely()`).
* The language-extension pre-pass no longer mis-reads a function-like macro
  definition (`#define likely(x) __builtin_expect(...)`) as an extension
  region; preprocessor-directive lines are skipped.

With these (plus flexible array members), 20 of the 23 sampled Minikraft
kernel/lib files compile fully to assembly.

## Variadic function bodies (`<stdarg.h>`)

ShivyC now supports variadic *bodies* (it already parsed `...` prototypes).
A variadic function receives all of its arguments on the stack, so the bundled
`<stdarg.h>` defines `va_list` as a moving `char *` over the 8-byte argument
slots: `va_start` asks the compiler (via a small `__builtin_va_start_addr()`
builtin) for the address of the first variadic argument -- which sits at
`[rbp + 16 + 8*named_count]` -- and `va_arg`/`va_end` are plain macros that
read the current slot and advance. This is internally consistent (ShivyC
generates both the variadic call and the body) rather than ABI-compatible with
externally-built variadic functions, which Minikraft does not use. All of
integer, pointer, char-promoted, and mixed varargs, multiple named parameters,
and passing a `va_list` to a helper (the vprintf pattern) are differential-
tested against gcc. This unblocked `printf.c`.

## Calling convention: stack-passed arguments

ShivyC previously aborted on any function with more than six integer
parameters (only the six SysV argument registers `rdi, rsi, rdx, rcx, r8, r9`
were supported). It now passes the seventh and later arguments on the stack per
the SysV AMD64 convention: the caller pushes them right-to-left (with 8 bytes
of padding when there is an odd number, to keep the stack 16-byte aligned at
the `call`) and cleans them up afterward, while the callee reads them at
`[rbp+16]`, `[rbp+24]`, ... A function that receives stack arguments is always
given a real `rbp` frame, and a call with more than six arguments is never
turned into a tail jump, so the feature composes correctly with the frameless/
tail-call (`-fstackless-calls`) and metamorphic (`-O4`) optimizations. All
cases are differential-tested against gcc (7-10 arguments, mixed operand
sizes, pointer arguments, indirect calls, recursion, and the optimization
paths). This eliminated a hard crash on `virtqueue.c` and is broadly required
for realistic C (including much of musl).

So "mostly more macro support" was the right call for the single biggest gap,
and that gap (plus the small lexer follow-ups) is now closed and tested -- but
it is not the last mile. The work now moves squarely into the parser and type
system, beginning with the conditional-expression grammar.

## Multi-translation-unit builds, AST caching, and the whole-program call graph

With everything that compiles as a single translation unit now working, the
next milestone is whole-program builds rather than more per-file features.

Multi-TU compilation and linking already work: `shivyc a.c b.c -o prog`
compiles each file to an object and links them, and cross-TU calls resolve at
link time (verified in both file orders). A new `-I <dir>` option adds include
search directories (previously only file-relative `"..."` and bundled `<...>`
headers were found), which is needed for realistic multi-file projects.

Of the two Minikraft files that did not compile, both turned out to be source
include-order bugs rather than compiler limits: `thread.c` calls
`register_interrupt_handler` without including `idt.h` (adding the include lets
ShivyC compile it to assembly), and `netdev_core.c` uses `UK_ASSERT` before
`assert.h` is included. gcc only accepts these via C89 implicit function
declarations, which ShivyC deliberately does not emulate. So ShivyC compiles
everything that is well-formed as a single translation unit.

To make repeated builds faster, parsed ASTs are cached on disk (under
`/tmp/shivyc-cache`, overridable with `SHIVYC_CACHE_DIR`, disabled with
`--no-cache`). The cache is keyed by a hash of the post-preprocessing token
stream, so it transparently incorporates the contents of every included
header; a cache hit skips the (relatively expensive, backtracking) parse. The
cache is best-effort -- any read/write/unpickle failure simply falls back to
parsing -- and verified to produce byte-identical assembly to a cold compile.
Making this correct required teaching the singleton `TokenKind` objects to
pickle by reference to their module-level instance, since the IL stage compares
them by identity. In practice the cache roughly halves single-file recompile
time and gives about a 3x speedup on a 23-file whole-program parse.

Finally, a cross-translation-unit call graph (`--print-call-graph`) parses every
input file (reusing the cache), lowers each to IL, folds indirect calls of
known functions into direct edges, and merges the per-TU graphs into one
whole-program graph: defined functions, call edges, address-taken functions,
and which TU defines each symbol. On the Minikraft kernel this is 222 functions
with 279 edges across 23 units, and it correctly resolves cross-TU references
(e.g. `register_interrupt_handler`, defined in `idt.c`, called from another
unit). This is the same `Call.direct_name` edge information the existing
single-TU metamorphic-reentrancy and `-O4` near-scratch analyses already use,
generalized to the whole program -- the groundwork for letting a final
optimization pass reason about the entire call graph. Building the graph is a
self-contained analysis: it never alters normal code generation, so it cannot
change the output of an ordinary compile.

## Wiring the whole-program graph into codegen: cross-TU safety

The first concrete use of the whole-program graph is making two existing
safety analyses sound across translation units. Both previously built their
call graph from a single TU's IL, so a cycle (or an address-taken function)
that travelled through another unit was invisible.

The metamorphic-reentrancy check refuses a `__metamorphic__` function that can
re-enter itself, because it returns through one static slot. The -O4
near-scratch optimization lets a function keep its locals in a static buffer
only if it can never be active twice at once (not self-reachable and not
address-taken). When either feature is active and more than one C file is
given, `main` now builds the whole-program graph once up front and passes it to
each file's compile.

The integration is deliberately conservative. For functions defined in the
current TU, the locally computed edges are kept (they reflect this unit's
tail-call lowering, where a tail-called callee does not keep its caller live);
the whole-program graph only contributes edges for functions defined in *other*
units, plus the program-wide address-taken set. For a single-file build the
whole-program graph equals the one TU, so nothing is added and code generation
is byte-for-byte unchanged -- and in fact the graph is not even built for a
single input. With multiple units, a metamorphic function that recurses through
another file is now correctly rejected, and a function that is recursive only
through another file no longer receives a static scratch buffer (verified by
the disappearance of its `<func>__scratch` storage from the generated
assembly).

## The first enabling cross-TU optimization: granting near-scratch

The previous step used the whole-program graph defensively -- to *refuse* an
unsound optimization. This step uses it offensively: to *grant* near-scratch
that a sound single-TU analysis must refuse.

The realization is that the old single-TU near-scratch rule was quietly
unsound. It granted the static buffer to any function that did not reach itself
through this unit's call edges -- but a function that calls a function defined
in *another* unit cannot be cleared from one TU alone, because that callee
might (directly or transitively) call back. The rule is now made sound: a
callee whose body we cannot see is "unknown", and a function whose call closure
reaches an unknown callee is treated as possibly re-entrant.

What counts as "unknown" depends on the analysis scope, and that is the whole
point. In a single TU, only that unit's own functions are known, so calling a
function from another file disqualifies near-scratch. With the whole-program
graph, every function defined anywhere in the program is known, so the same
function can be proven non-reentrant and *granted* the optimization. A
correctness-preserving spill-heavy function that calls a helper in another unit
gets no static buffer when its file is compiled alone, and gets one (verified
in the generated assembly, with results unchanged) when the program is compiled
together.

Two refinements keep this from being needlessly conservative. A cycle back to
the function through *known* functions still disqualifies it (this subsumes the
cross-TU recursion refusal from the previous step). And a function with
internal (`static`) linkage is eligible even when it calls unknown externals:
nothing outside its translation unit can name it, so an unknown external cannot
re-enter it (unless its address is taken, which already disqualifies it). The
remaining conservatism -- an externally-linked function that calls a genuinely
external symbol (a libc routine, an assembly stub) is still refused -- is sound
but could be relaxed later by trusting known-leaf externals or an explicit
"does not call back" annotation.

## Whole-program flag promotion into xmm15

The second offensive use of the whole-program graph promotes small global flags
into the dedicated `xmm15` cache *across* translation units. The single-TU
`-fsimd-pack-globals` feature already packs 1-8 bit `*_Nbit` file-scope flags
into `xmm15` (with the byte staying authoritative and hot/interrupt routines
reading the register instead of memory). On its own it can only pack flags
whose every use is in one unit, because each unit built its own bit layout and
kept a unit-local memory mirror.

With the whole-program graph, ShivyC collects every externally-linked `*_Nbit`
flag in the program and assigns them a single layout in sorted-name order. The
order is deterministic, so each unit computes the identical bit positions
without coordination, and the 8-byte memory mirror is emitted as a shared
common symbol that the linker merges into one object. A flag defined in one
unit and read in a hot function in another is then served from the same
`xmm15` bit in both, seeded once from `main`. A flag whose address is taken
anywhere in the program is excluded, since a write through the pointer would
bypass the register cache; the whole-program address-taken set makes that
check sound.

This is enabling, not merely safe: an externally-linked flag cannot be packed
from a single TU at all (the layout and mirror would not agree across units),
so the optimization only exists once the whole program is in view. The scope
is deliberately limited to external-linkage flags -- a unit-local `static`
flag is left to the per-TU mechanism in single-file builds, because giving
each unit's private flags a globally-unique bit (and seeding it from the unit
that defines it) would require per-unit startup code that does not yet exist.

## Cross-TU inlining of small leaf functions

The third offensive use of the whole-program graph is the canonical one:
inlining a tiny callee defined in one unit directly into a call site in
another. A single translation unit never has the body of a function defined in
another file, so this optimization only exists once the whole program is in
view. Bodies are captured while the whole-program graph is built (every unit is
parsed and lowered there anyway) and spliced into callers as an IL pass at -O4.

To keep the transform obviously sound, only a restricted shape is inlinable: a
leaf function (no calls) that touches no memory or globals -- only its
parameters, locals, and integer literals, so any reference to a value with
static storage duration disqualifies it -- with at most a couple dozen body
operations. It may, however, contain internal control flow: comparisons,
conditional and unconditional jumps, labels, loops, and several `return`
statements. Such a body is a pure function of its arguments, so splicing cannot
change observable behavior; it only removes the call. A callee that reads or
writes a global, or calls anything, is left as an ordinary call (verified: it
still appears as a `call`/tail `jmp`), as is any function over the size cap.

Splicing binds each parameter to a fresh copy of its argument -- so a callee
that reassigns a parameter cannot clobber the caller's value, and an argument
expression with a side effect is evaluated exactly once -- then clones the body
with every callee value remapped to a fresh one (literals re-registered in the
caller) and every label remapped to a fresh label, so that two call sites of the
same function (or a call site inside a loop) never share labels. Each `return`
is routed through a single fresh end label: it assigns the call's result and
jumps to the end, where the caller's code resumes. The pass runs before
tail-call lowering and the near-scratch and recursion analyses, so they all see
the simplified, call-free code; running it afterwards would be wrong, because
`return f(x)` is turned into a tail jump that drops the very return the inlined
body needs. It is gated on -O4 with more than one C file, so ordinary and
single-file builds are byte-for-byte unchanged. Results were differential-tested
against gcc across arithmetic, bitwise, shift, division, multi-argument,
type-converting, parameter-reassigning, side-effecting, and nested-call shapes,
and -- for control flow -- early returns, if/else, ternary, `&&`/`||`, `for`,
`while`, `do`/`while`, nested loops, `break`, `continue`, `switch`, and a
multi-step loop (Collatz step count), with the same function inlined at two
sites to confirm labels do not collide.

## Dead-function elimination

Whole-program inlining frequently leaves a small `static` helper with no
remaining callers -- every direct call was spliced into the caller. After the
inlining pass, any unreachable internal-linkage function is dropped from the
output.

Only `static` (internal-linkage) functions are eliminated, and that restriction
is what makes the pass unconditionally sound with no closed-world assumption: a
static function is visible only inside its own translation unit, so *every* way
of reaching it lives in that same unit and is therefore visible to the analysis
-- a direct call, having its address taken (an IL `AddrOf`), being named in a
static initializer (a function-pointer table lowers to `.quad name` via a
`("sym", name, _)` entry), or being referenced from an inline-asm template. The
roots are every external function (so `main`, which is external, is always a
root), every address-taken function, and every function referenced by a static
initializer or an asm template; anything not reachable from a root through the
post-inlining direct-call edges is removed. An external function is always kept
-- another unit or a later-linked library may call it, and one unit cannot prove
otherwise.

This composes with inlining and the control-flow inliner: a branchy static
helper inlined at all of its sites is eliminated, and a dead chain collapses
transitively (a leaf inlined into a non-leaf is removed while the non-leaf, a
real call, stays). Verified against gcc that results are unchanged while the
eliminated definitions disappear from the assembly, and that statics reachable
only through a pointer table, only through inline asm, or via a taken address
are correctly retained. Gated on -O4 with more than one C file, so ordinary and
single-file builds are unchanged.

## Floating point, slice 1: types, literals, conversions

The first slice of floating-point support adds the `float` and `double` types
(`long double` is accepted as a synonym for `double` for now), floating
constants in the lexer (decimal, decimal-with-exponent, and hexadecimal forms,
each with optional f/F/l/L suffixes), and the code generation needed to move
floats around and convert between them and the integer types.

Floating constants required teaching the lexer to keep a numeric chunk together
across a '.' and across a '+'/'-' sign inside an exponent (so `3.75`, `1e+10`,
`0x1p-4`, and `.5` each lex as one token while `a.b`, `1+2`, and `p->q` are
unaffected). This matches the C pp-number rule, so `10.a` now correctly lexes as
one (invalid) constant rather than three tokens, as gcc does.

Code generation uses a deliberately simple, obviously-correct scheme: floating
values live in memory (never in the integer register allocator) and literals are
emitted to `.data` as their IEEE-754 bits; each floating operation loads its
operands into a fixed xmm scratch register, computes, and stores back. The
`Set` command gained a floating path handling float-to-float (including
`double`<->`float` via `cvtsd2ss`/`cvtss2sd`), `int`->float (`cvtsi2sd`/`ss`),
and float->`int` (`cvttsd2si`/`ss`, truncating toward zero as C requires).
Returning a float places it in `xmm0`. Constant folding of casts and of unary
minus was extended to fold floating constants (with C truncation toward zero for
float->int). Results were differential-tested against gcc across literals,
copies, every conversion direction, narrowing, negation, hex/exponent literals,
and round trips.

Not yet supported (later slices): floating arithmetic (`+ - * /`), comparisons,
passing floats as function arguments, and calls that return a float.

## Floating point, slice 2: arithmetic

The second slice adds `+`, `-`, `*`, and `/` on `float` and `double`, using the
same memory + xmm-scratch scheme as slice 1. The usual arithmetic conversions
were extended so a floating operand dominates (double over float over any
integer), and the `Add`/`Subtr`/`Mult` and `Div` IL commands gained a floating
path: load arg1 into the xmm scratch, apply the SSE op (`addsd`/`subsd`/`mulsd`/
`divsd`, or the `ss` forms for `float`) with arg2 as a memory operand, and store
the result. Subtraction and division stay correct despite being
non-commutative because the scratch holds arg1 first. Constant folding is
skipped for floating operands so every floating computation goes through the
SSE path, which matches gcc's rounding exactly. Compound assignment, mixed
int/float expressions, loops accumulating into a float, and arrays of `double`
all work as a result. Differential-tested against gcc across each operator,
operand order, 32-bit vs 64-bit, mixed promotion, chained expressions, and
in-place updates.

Still pending: floating comparisons (`< <= > >= == !=`, via `ucomisd`),
passing floats as function arguments (xmm0-7), and calls that return a float.

## Floating point, slice 3: comparisons

The third slice adds `< <= > >= == !=` on `float`/`double`, reusing the same
result-in-GPR pattern as integer comparisons but built on `ucomisd`/`ucomiss`.
The tricky part is NaN: an unordered result sets CF, ZF, and PF together. The
relational forms load operands so the test is "above"/"above-or-equal"
(`ja`/`jae`), both of which are false when unordered -- the correct ordered-
comparison result -- swapping operands for `<` and `<=`. Equality consults the
parity flag explicitly (`jp` then `jne`) so `NaN == NaN` is false and
`NaN != NaN` is true. Verified against gcc across all six operators, both
outcomes, mixed int/float operands, use inside `while`/ternary/`&&`/`||`, and
every NaN case.

Still pending: passing floats as function arguments (xmm0-7) and calls that
return a float -- the remaining ABI piece, which is what most of musl's `math`
needs.

## Floating point, slice 4: the calling convention (ABI)

The fourth slice wires floats into the SysV AMD64 calling convention: floating
arguments are passed in xmm0-7 and a floating value is returned in xmm0.
Crucially, integer and floating arguments fill their register sequences
independently -- `f(int a, double b, int c)` puts a in rdi, b in xmm0, c in
rsi -- so both `LoadArg` (callee side) and `Call` (caller side) now walk two
counters and classify each parameter by type. `LoadArg` moves a floating
parameter out of its xmm register into the parameter's memory home; `Call`
loads floating arguments into xmm0-7 and reads a floating return from xmm0.
Negation of a non-constant float (`-x`) is computed as `0 - x` in the scratch
register. Integer-only calls are unchanged. Differential-tested against gcc:
double/float parameters, returns, the mixed int/float sequence in both orders,
recursion through a float function, six float arguments, and call results used
within expressions.

With literals, conversions, arithmetic, comparisons, and the ABI in place, the
core of `<math.h>`-style code (scalar float parameters, returns, branches, and
expressions) now compiles. Known gaps: `long double` is still aliased to
`double`; floats spilled past the 8 xmm registers / mixed-class stack spill are
untested; and the `al` vector-count register is not set (only matters when
calling a variadic function with float args in registers, which ShivyC routes
via the stack anyway).

## Floating point, slice 5: floats through aggregates and pointers

The relative memory commands (SetRel/ReadRel), which implement struct/union
member access, array indexing, and pointer dereference, previously moved every
value through a GPR scratch -- crashing on a floating value (which lives in
memory and has no GPR home). Both commands now take a floating path that moves
the value through the xmm scratch register instead. This unblocks the
union-type-punning idiom musl uses pervasively (e.g. clearing a double's sign
bit via its uint64 view in fabs), as well as structs of doubles, arrays of
double, and double pointers. A latent crash in get_reg_spot when there is no
array index (count is None) was also fixed. Differential-tested against gcc.

### Static float initializers (follow-on fix)

Emitting a static/global float initializer wrote the decimal value straight
into a `.quad`/`.int` directive (e.g. `.quad -0.001388`), which the assembler
rejects. Both the scalar (`add_data`) and aggregate (`add_data_block`) emission
paths now convert a Python float initializer to its IEEE-754 bit pattern; integer
initializers (Python ints) pass through untouched. This fixed every "bad asm"
case in the math sweep -- the `__sin`/`__cos`/`__tan` coefficient tables and
similar static double constants now assemble. Differential-tested against gcc
for scalar, global, and array-of-double initializers.

### Measured musl `src/math` impact

On a 60-file sample of `src/math`, clean codegen-and-assemble went from 0/60
before the floating-point work to 44/60 (73%). The remaining failures are not
floating-point-specific: compound literals `(union{...}){x}` used by the
as-uint/as-double type-pun macros (the largest group), a few static
initializers that need compile-time folding of a float division (`1.5/EPS`),
and one oversized `unsigned long long` hex literal.

## Compound literals (C99)

A parenthesized type-name followed by a brace initializer -- `(type){...}` --
is a compound literal: an anonymous object of that type, initialized from the
brace list, usable as an lvalue. The parser distinguishes it from a cast by
looking for a `{` after the `)`. The node creates a synthetic anonymous
variable (automatic storage inside a function, static at file scope) and reuses
the existing declaration-initialization machinery, then yields a direct lvalue.
This unblocks musl's pervasive type-pun macros `asuint64`/`asdouble`/`asuint`
(`((union{double _f; uint64_t _i;}){f})._i`). Differential-tested against gcc
for scalar, struct (positional and designated, trailing comma), array, and
both union-punning directions; ordinary casts are unaffected.

## Static float folding and wider integer literals

Two follow-on fixes cleared the remaining math front-end blockers:

Floating constant expressions are now folded at compile time (a `_float_const`
hook on the arithmetic operators). Python floats are IEEE-754 doubles, so
+,-,*,/ on `double` constants reproduce the SSE result exactly; the value is
re-rounded to the target type on emission. This lets static initializers like
`static const double toint = 1.5/EPS;` compile. Comparisons deliberately do not
fold (they keep their runtime, NaN-correct path).

Integer-literal typing now follows the C rules for unsigned types: a hex/octal
constant or one with a `u` suffix may take `unsigned int`/`unsigned long` when
it does not fit a signed type, so constants like `0xffffffffc0000000ULL` are
accepted instead of rejected as "too large".

### Measured musl `src/math` impact (cumulative)

On the 60-file sample: 0/60 before any of this work -> 44/60 after the
floating-point slices and the static-literal fix -> 55/60 after compound
literals -> 56/60 (93%) after static float folding and wide integer literals.
The three front-end blockers (compound literals, static float folding, wide
literals) are fully cleared. The few remaining non-clean files now fail in
*code generation* rather than parsing: three hit a float codegen bug
(`subsd` operand form) newly reachable now that they parse, and unsigned 64-bit
right shift of a high-bit value uses an arithmetic instead of logical shift.
These are codegen bugs, distinct from the parser blockers addressed here.

### Floating increment/decrement fix

`++`/`--` on a floating operand created its `1` step as an integer literal,
which reached the SSE add/subtract as an illegal immediate operand
(`subsd xmm0, 1`). The step is now registered as a float constant `1.0`
(materialized in memory). This was the last codegen bug in the 60-file
`src/math` sample, which now reaches 60/60 (100%) clean codegen-and-assemble.

## Full src/math sweep (232 files) and a robustness fix

A complete sweep of all 232 files in musl `src/math` gives 221/232 (95.3%)
clean codegen-and-assemble (the function-only files reach the harmless
missing-main linker stage). This is the whole-directory figure behind the
earlier 60-file sample; "clean" means it compiles and assembles, not that the
math has been numerically validated.

The 11 non-clean files are almost entirely `long double`: nine `*l.c` files
fail because long double is aliased to double and these use 80-bit constants or
long-double-specific behavior (out-of-range hex constants, an operand-form
mismatch in nextafterl, a non-assignable-type path in sqrtl). The remaining two
are fma.c and fmal.c, whose extended-precision arithmetic reaches an
unimplemented codegen path ("unexpected register size"). So fma.c is the single
genuinely-`double` file that does not compile; everything else is long double.

Robustness fix: an out-of-range floating constant (an 80-bit long-double hex
literal beyond the range of double) previously escaped as an uncaught
OverflowError traceback. It now raises a clean "floating constant out of range"
diagnostic. (The fma/fmal "unexpected register size" path still surfaces as an
internal error; it is an unimplemented-feature limitation, not addressed here.)

## Unsigned right shift + long double rejection

Two changes:

Right shift now selects the instruction by operand signedness: `shr` (logical)
for unsigned operands, `sar` (arithmetic) for signed. Previously it always
emitted `sar`, so an unsigned 64-bit value with its high bit set shifted
incorrectly (e.g. `0x8000000000000000UL >> 63` gave 0 instead of 1).
Differential-tested against gcc for signed/unsigned, immediate/variable counts.

long double (80-bit) is no longer silently aliased to double. It is now
rejected with the diagnostic "our compiler uses the x87 Floating Point Unit
(FPU) as an extra data cache, and we do not allow 80bit floating point math",
pointing at the offending line. To avoid failing merely because a header
defines an unused long double helper, the rejection is deferred: long double
use is recorded per function during IL generation and reported only for
functions that survive dead-function elimination (file-scope long double
objects are rejected immediately). A pure prototype, or an unused static long
double helper, therefore compiles; a long double variable or a reachable long
double computation is rejected.

### Effect on the src/math sweep

Rejecting long double (rather than aliasing it) lowers clean coverage from
221/232 to 150/232 (64.7%). The ~70-file difference is NOT all genuine long
double code: about nine are the long double implementations themselves
(__sinl, powl, ...), but most are ordinary double functions (cos, sin, round,
ceil, ...) that reference musl's FORCE_EVAL macro. That macro expands to a
sizeof-dispatched chain that *calls* the long double helper fp_force_evall in a
branch that is statically dead for a double argument; because we do not fold
the constant `sizeof` condition and delete the dead branch, that call survives
as reachable and taints the function. This is a conservative over-rejection
(it fails loudly rather than computing wrong), and recovering those files would
require dead-branch elimination on constant conditions.

## -f-long-double-as-double flag and the __SHIVYC__ predefine

The compiler now always predefines __SHIVYC__ (value 1), so source built for
this compiler can detect it with `#ifdef __SHIVYC__`.

By default long double is rejected (see above). The new -f-long-double-as-double
flag instead aliases long double to plain 64-bit double and emits a one-time
warning making clear that this compiler never supports the 80-bit extended
format -- neither ARM nor RISC-V have native hardware for it, and on Intel/AMD
this compiler repurposes the x87/SIMD registers as a spill cache for speed. The
flag exists so a user must explicitly opt in and understand the precision loss.

## -c (compile to object) flag

Added a `-c` flag: compile and assemble each input to a `.o` and stop before
linking (with `-o`, a single object is written to that name). ShivyC already
produced per-file object files internally before linking; `-c` simply exposes
them, which is the prerequisite for building libraries/archives (e.g. a musl
libc.a) and for any multi-step build. Verified that `-c` objects carry the
expected symbols and relink correctly into a working executable.

## Preprocessor/lexer robustness for real-world headers

Two lexer fixes needed to handle real headers (musl, CPython):

1. Invalid pp-tokens no longer abort lexing immediately. The lexer emits an
   `unrecognized` placeholder token instead; the preprocessor drops it in dead
   `#if` branches and renders it in `#error` text, and only if it survives into
   live code (checked after preprocessing) is the original "unrecognized token"
   diagnostic raised. So an odd token in a skipped branch or an `#error`
   message no longer breaks compilation.

2. Comment delimiters are detected from raw characters rather than matched
   symbol kinds. `match_symbol_kind_at` is greedy, so in `/*=...` the `*=`
   symbol hid the `*` and the `/*` was not recognized as a comment start (the
   `/*=====*/` banner comments in CPython's headers triggered this); similarly
   `//=` line comments. Both are now handled.

## Parser fixes for real-world C

- Postfix operators may now follow a function call: `f()->m`, `(*f())`, `f()[i]`,
  `f().m`, and chained calls `f()()`. Previously `parse_postfix` returned
  immediately after a call instead of continuing its loop, so any postfix after
  `()` raised "expected ';'".
- Function parameter names are now scoped to the parameter list, so a parameter
  whose name matches a typedef (e.g. `void f(int destructor)` where
  `destructor` is a typedef) no longer leaks out and shadows that typedef in
  the enclosing scope. Typedef resolution for parameter *types* still works
  because lookups fall through to outer scopes.

## Dynamic predefined macros __LINE__ and __FILE__

`__LINE__` and `__FILE__` are expanded in the preprocessor from each token's
own source position (they cannot be plain `#define`s since their value depends
on location). `__LINE__` becomes a number token with the line; `__FILE__` a
string token with the file name. A user `#define` of the same name takes
precedence. (Note: `__func__` is a separate, function-scoped feature and is
not yet implemented.)

## C99 __func__

`__func__` (and the GCC aliases `__FUNCTION__` / `__PRETTY_FUNCTION__`) resolve
to a string literal of the enclosing function's name. The parser tracks the
current function name while parsing its body and emits a String node for these
identifiers. Note: the on-disk AST cache keys on source, so use --no-cache when
re-compiling the same source after a compiler change.

## C11 anonymous struct/union members

A struct/union member that is itself an untagged struct/union with no
declarator now promotes its members into the enclosing type, so they are
accessible directly (e.g. CPython's PyObject: `obj->ob_refcnt` where
`ob_refcnt` lives in an anonymous struct inside an anonymous union). The
anonymous member occupies space normally; `set_members` flattens inner member
offsets (recursively, so arbitrarily nested anonymous members work) into the
parent's offset map. Verified vs gcc for both layout (sizeof, overlapping union
fields) and access.

## char array initialized by a string literal

`char s[] = "abc";` and `static char s[] = "abc";` now copy the literal's bytes
into the array's own storage (rather than pointing at a separate literal). The
array size is inferred from the string when the declarator is incomplete, a
larger declared array is zero-padded, and the static case emits the bytes as a
data-section constant. (A char *pointer* initialized by a string literal --
`static char *p = "abc";` -- is a separate case still pending.)

## __builtin_offsetof

Implemented `__builtin_offsetof(type, member-designator)` as a constant
expression yielding a size_t byte offset. The member designator supports
nested members and array subscripts (`a.b[3].c`) and resolves through C11
anonymous members. The result matches where ShivyC actually places members
(self-consistent with member access). ShivyC's own <stddef.h> and the musl
fork's <stddef.h> now define `offsetof` in terms of this builtin under
__SHIVYC__, so CPython's `offsetof(...)` static initializers (e.g.
tp_basicsize) compile.

## String literals and static-object addresses in static initializers

Two related static-initializer gaps were closed:

- A string literal initializing a pointer (scalar `static char *p = "x";`, an
  aggregate member like a PyTypeObject's `tp_name`, or an array element) now
  emits the string bytes under a stable label and initializes the pointer to
  that address. Previously this crashed.
- The address of a file-scope `static` (internal-linkage) object -- e.g.
  `&bool_as_number` in a PyTypeObject initializer -- is now a valid address
  constant. Internal-static objects get a stable, cached assembly label
  (`SymbolTable.asm_name`) used consistently at both the definition and the
  reference; functions keep their plain label.

## Include resolution: -I before bundled headers

For angle-bracket includes (`<stdio.h>`), `-I` directories are now searched
before ShivyC's bundled fallback headers (matching gcc's ordering), so a real
libc provided via `-I` (e.g. musl) takes precedence and its `FILE`/types no
longer clash with ShivyC's stub headers. Bundled headers remain the fallback
when no `-I` provides the header (standalone compilation is unchanged).

## Whole-program elimination of unused struct members

`-f-eliminate-unused-members` (off by default) removes struct members that are
never accessed in any translation unit, shrinking the struct. This lets one
generous struct definition stand in for hand-pruned #ifdef variants: the
compiler sees every use across all units and drops what nobody reads.

Because this changes layout, it is sound only when every observation of the
struct's bytes goes through a tracked `.`/`->` access. The analysis is
conservative -- a struct tag keeps all members if, anywhere in the program, its
address is taken, it is `sizeof`'d or `__builtin_offsetof`'d, it is initialized
positionally (designated initializers are fine), it is nested in another
struct/union or array, it is passed/returned by value, or it has anonymous
members. `--print-eliminated-members` reports what was removed. With the flag
off, behavior is exactly unchanged.

## Compile-time function contracts

A function definition may carry one or more precondition clauses between its
parameter list and its body, borrowing Python's `assert` syntax:

```c
void process_input(char *user_input)
    assert len(user_input) <= 16
{
    char buffer[16];
    strcpy(buffer, user_input);   /* would overflow for a longer input */
}

int main(void) {
    char *large_string = "ThisInputIsWayTooLongForTheBuffer";
    process_input(large_string);  /* rejected at compile time */
}
```

`extensions.py` recognizes these clauses in a pre-pass, strips them to plain C
(preserving byte offsets so later diagnostics line up), and records them as
per-argument bounds: `len(p) <= N` and `len(p) >= N` bound a string/array's
length, and `not len(p) % N` requires the length to be a multiple of `N`.

`contracts.py` then *checks* them. At each call site where an argument's length
is statically known, the bound is evaluated and a violation is reported as a
compile error naming the caller, the offending argument, the callee, and the
clause:

```
error: in the function `main`, the variable `char * large_string` is too large
to pass to the function `process_input` because of the contract:
`assert len(user_input) <= 16`
```

An argument's length is known when it is a string literal, or a local variable
proven to hold one (tracked from its string-literal initializer; the tracking
is invalidated when the variable is reassigned or has its address taken, so a
variable later pointed at a different string is judged by that string). The
analysis is deliberately one-sided: it reports a violation only when it can
prove one, and stays silent when a length is unknown, so it never rejects a
valid program. Functions without contract clauses are entirely unaffected.

## Register-allocator scratch-register spilling

The code generator hands each IL command a `get_reg` callback for the scratch
registers it needs to materialize temporaries. Previously, if every one of the
nine allocatable GPRs already held a value live across the command, `get_reg`
had nowhere to turn and raised `spill required for get_reg`, aborting
compilation. Real code reaches this under high register pressure (e.g. taking
the address of a variable while many others are live), which blocked compiling
cpython's core objects.

`get_reg` now spills when it must: it picks an allocatable register holding a
value live across the command that the command neither reads (an input) nor is
already using (a conflict, or a register spilled earlier in the same command),
saves that value to a reserved scratch slot, hands the register out, and
restores it immediately after the command. Because the value is live *across*
the command, the command never touches it, so save-before / restore-after
preserves it exactly. Slots are allocated lazily and reused across commands, so
a function that never exhausts its registers reserves nothing.

A scratch slot's home depends on the function. A normal (framed) function uses
an rbp-relative stack slot, which grows its frame. A frameless leaf function
uses the System V red zone (128 bytes below `rsp`, guaranteed safe for leaf
functions), so it stays frameless. An `-O4` near-scratch function uses its
static per-function buffer. The framelessness decision is made before code
generation and is independent of scratch usage, which keeps every function's
prologue/epilogue consistent. Since all allocatable registers are
caller-saved, any value live across a call is already forced to memory, so a
call site never needs a scratch spill -- spilling only arises at register-only
commands such as address-of and arithmetic.

A related codegen fix accompanies this: x86-64 cannot move an immediate wider
than a sign-extended 32 bits straight to memory (`mov QWORD PTR [m], imm64` is
invalid), which arose for values such as cpython's immortal refcount sentinel.
Such a store is now routed through a register. Both the spilling and the wide
immediate store are verified by differential testing against gcc under heavy
register pressure.

## Function-pointer-cast static initializers

A cast of an address constant to a pointer type is itself an address constant
(C11 6.6p9), so it is valid in a static initializer. The most common case is a
method table such as CPython's clinic-generated `PyMethodDef[]`, whose entries
look like `{"__len__", (PyCFunction)obj_len, METH_NOARGS, doc}`. The static
address-constant evaluator now unwraps such a cast (when its target type is a
pointer, which preserves the full address) and emits the underlying symbol as a
relocation, just as it already did for bare function names, `&object`, and
string literals. This unblocked compiling further CPython core objects.

## Variadic builtins, struct copy-init, and an extension-region fix

Three fixes that let more real-world C (CPython core objects compiled against
musl) parse and compile:

- **GCC-style variadic builtins.** musl's `<stdarg.h>` spells the macros with
  `__builtin_va_start`/`__builtin_va_end`/`__builtin_va_arg`/`__builtin_va_copy`.
  `__builtin_va_arg(ap, type)` is implemented as a real builtin (it parses a
  type-name operand, reads `*(type *)ap`, and advances `ap` by the 8-byte-
  aligned slot size); the other three are provided as prelude macros over the
  existing `__builtin_va_start_addr` mechanism. As before, every variadic
  argument is passed on the stack, which is self-consistent for an all-ShivyC
  program (e.g. CPython plus a ShivyC-built musl).

- **Struct/union copy-initialization.** A struct local may be initialized from
  a struct-valued expression -- a compound literal, another struct, or a
  function result -- not only from a brace list. Such an initializer is now
  lowered exactly like struct assignment instead of being rejected as "not of
  assignable type."

- **Extension-region detection.** The contract pre-pass pairs a `name(...)` and
  inspects the text up to the body `{`. A bare function *call* nested in an
  enclosing parenthesized expression (e.g. `if (!track && maybe_tracked(o)) {`)
  no longer trips this: the scan now bails as soon as an unmatched `)` appears,
  since a real definition header is paren-balanced.

## Returning small structs by value (SysV AMD64)

A function returning a struct of 9..16 bytes now returns it in the RAX:RDX
register pair: the low eightbyte in RAX, the high one in RDX. The `Return`
command loads the two halves out of the struct's memory home, and the call
site stores RAX:RDX back into the result's memory. (A struct of 8 bytes or
fewer already returned in RAX.) Returns larger than 16 bytes -- which require
the hidden-pointer `sret` convention -- are not yet supported and raise a clear
error rather than miscompiling. The implementation is ABI-correct in both
directions: a ShivyC-compiled callee interoperates with a gcc-compiled caller
and vice versa, verified by mixed compilation. This was the gating feature for
CPython's `tupleobject.c`, whose `_PyStackRef`-style 16-byte returns now
compile.

## Returning large structs by value (sret) and two codegen fixes

Three changes that, together, let CPython's `floatobject.c` compile (it returns
a 24-byte struct by value and exercises wide struct copies and negation under
heavy register pressure):

- **Struct return larger than 16 bytes (SysV memory class, "sret").** A struct
  that does not fit in RAX:RDX is returned through a hidden pointer: the caller
  allocates the result storage and passes its address as an implicit first
  integer argument (in RDI), which naturally shifts the real arguments to RSI
  onward; the callee writes the struct through that pointer and returns the
  pointer in RAX. This is implemented at the IL level -- the call site prepends
  the storage address and treats the call as returning nothing in registers,
  and `return X` inside such a function stores the value through the hidden
  pointer -- so it reuses the ordinary argument-passing and pointer-store
  machinery. Verified ABI-correct in both directions against gcc. Combined with
  the earlier RAX:RDX work, structs of any size now return by value.

- **Wide struct-copy offset bug.** The routine that copies a block of memory in
  8/4/2/1-byte chunks reassigned its running source and destination spots on
  each iteration, so the offset compounded and the third and later chunks
  landed past where they belonged. Any copy larger than 16 bytes (struct
  returns, struct assignment) was corrupted; each chunk is now addressed from
  the original base by an absolute offset.

- **Memory-to-memory move in unary negation.** Integer `-x`/`~x` emitted a
  direct memory-to-memory `mov` when the value and its destination were both
  spilled to the stack, which x86 does not allow. The copy now goes through a
  scratch register (the `neg`/`not` itself may still operate in place on
  memory).

## Address of a static object's member, and struct-by-value arguments

Two features that let CPython's `listobject.c` compile:

- **`&OBJ.member...` as a static address constant.** The address of a (possibly
  nested) member of a static or external object is a compile-time address
  constant -- a linker symbol plus a byte offset. The static-initializer
  evaluator now folds such forms, accumulating member offsets down to the named
  base object. This covers CPython's clinic tables, e.g. `&_Py_ID(key)` (a
  deeply nested member of the external `_PyRuntime`) and
  `&_kwtuple.ob_base.ob_base` (a member of a function-local static). The emitted
  relocation is `symbol+offset`.

- **Passing structs by value as arguments.** Mirroring the struct-return work:
  a struct of 9..16 bytes (INTEGER class) is passed in two consecutive integer
  registers, all-or-nothing; a larger struct, or one that does not fit the
  remaining registers, is passed on the stack as ceil(size/8) eightbytes. The
  callee copies the incoming registers or stack slots into the parameter's
  memory home (a new LoadStructArg IL command); the caller loads the struct's
  eightbytes into the argument registers or pushes them. Verified ABI-correct
  in both directions against gcc for register- and stack-passed structs,
  including structs mixed with scalar arguments. Combined with the return-value
  work, structs of any size now pass both into and out of functions by value.

## Compiling longobject.c: language fixes and a register-allocator speedup

Four changes. The first three let CPython's `longobject.c` parse and compile;
the last makes compiling large functions practical.

- **C11 `static_assert` / `_Static_assert`** are accepted (as prelude macros
  that expand to nothing, so `static_assert(cond, msg);` becomes an empty
  statement). The assertion is not enforced -- acceptable when compiling code a
  conforming compiler has already validated.

- **Constant folding of `!`.** Logical-not previously always emitted code, so
  `!(constant)` was never itself a constant. It now folds when its operand is a
  constant, which lets it appear in constant contexts such as array sizes --
  notably CPython's `Py_BUILD_ASSERT_EXPR`, `sizeof(char[1 - 2*!(cond)])`.

- **Null pointer constant to a function pointer.** `NULL` (i.e. `(void *)0`)
  may initialize or be assigned to a function pointer. The cast checker
  previously rejected this because the "incompatible pointer" case was tested
  before the null-pointer-constant case; the null constant is now accepted for
  any pointer target. Genuinely incompatible pointer conversions still error.

- **Register-allocator coalescing made near-quadratic.** Two hot steps were
  badly super-linear on large functions. The freeze step built and sorted all
  O(V^2) node pairs on every call (cubic overall); it now scans preference
  edges directly with the same low-degree-first heuristic. The coalesce step
  repeatedly rescanned conflict lists with linear membership tests and rebuilt
  conflict sets; it now caches each node's conflict set once per pass for O(1)
  membership (the merge decisions are unchanged). Together these cut a
  synthetic 400-statement function from a >90 s timeout to under 7 s, and
  compiling `longobject.c` dropped from about 14 minutes to about 3.5, with
  byte-identical output.

## Compiling dictobject.c: comments no longer confuse the extension scan

The contract/extension pre-pass scans for `name(...)` function headers on the
raw source. A function-name-like token inside a comment -- e.g. CPython's
`/* Uncomment to check the dict content in _PyDict_CheckConsistency() */` --
was matched as a definition header, and the "region" scan then ran from that
comment across `#if 0` blocks and macro bodies until the next `{`, swept up an
`assert`, and failed on the stray `*/`. The scan now runs on a copy of the
source with comment and string/char-literal contents blanked to spaces
(offsets and line numbers preserved), so tokens inside comments or strings are
never mistaken for definitions. With this, `dictobject.c` compiles.

## Inline-asm syscall support (musl): correct constraints + register bindings

Three changes let ShivyC compile musl's syscall layer (`arch/x86_64/syscall_arch.h`
plus `src/internal/syscall.h`), which is included by most of musl:

1. **Inline-asm constraint mapping fixed (correctness bug).** The asm IL command
   previously moved every input operand except `a`/`m` into RDX, so `D` (rdi),
   `S` (rsi), and `r` were silently miscompiled -- a `write(2)` syscall emitted
   `mov rdx,rdi; mov rdx,rsi; mov rdx,rdx` and printed nothing. Each constraint
   letter (a/b/c/d/S/D) now maps to its register, `r` honors a bound register
   (below), and every target register is reported by `clobber()` so operand
   sources never sit in a register about to be overwritten. Unsupported
   constraints raise rather than miscompile.

2. **`register` keyword** added (treated like `auto`).

3. **`register T v __asm__("reg")` bindings.** A GCC register-asm declaration
   pins a variable to a hardware register; musl binds r10/r8/r9 this way for
   syscall args 4/5/6. The register name flows decl_nodes.Root.asm_regs ->
   DeclInfo.asm_reg -> ILValue.asm_reg, and an `r`-constrained asm operand whose
   value carries asm_reg is placed in that register. The declarator scanner
   (`_find_decl_end`) now stops before an `asm(...)` clause instead of swallowing
   it as declarator tokens.

Also: **C99 `[static N]` / `[const N]` array parameter hints** now parse (the
qualifiers are skipped before the size expression), needed by syscall.h's
`__procfdname(char buf[static 15+3*sizeof(int)], ...)`.

Result: a musl src sweep over errno/internal/prng/stdio/unistd/time went from
66 pass / 176 fail to ~171 pass / 35 fail; the dominant `register __asm__` parse
blocker (≈173 files) is eliminated. `src/unistd/write.c` and `src/stdlib/strtol.c`
now compile. Differential-tested vs gcc (the r10/r8/r9 sum returns 27 under both).

## Pointer-arithmetic address constants in static initializers

`static const int32_t *const p = table + 128;` (musl's ctype tables) now folds
to a link-time `symbol+offset` relocation. `_static_addr_const` gained a
Plus/Minus case: it resolves one operand as an address constant and the other
as an integer constant, scaling the addend by the pointee size (array element
or pointed-to size). Handles `ARRAY + n`, `n + ARRAY`, and `PTR - n`.
Previously these fell through to runtime IL emission at file scope and crashed
with `KeyError: None` (cur_func is None outside a function). Fixes
__ctype_tolower_loc.c / __ctype_toupper_loc.c / prng/random.c. Differential-
tested vs gcc.

A related file-scope crash remained for non-constant static initializers that
are not address constants — e.g. `int f(void); static int x = f();` with no
prior function definition leaving `cur_func` unset. `DeclInfo.do_init` and
`do_aggregate_init` now evaluate those on a probe IL (`<static-init-probe>`),
so the existing “non-constant initializer for variable with static storage
duration” diagnostic fires instead of `KeyError: None`. `ILCode.add` also
rejects a null `cur_func` with an internal `CompilerError` rather than a bare
dictionary miss.

## Extended inline-asm output constraints (musl atomics)

ShivyC's inline asm now supports the full operand model musl's atomics use
(arch/x86_64/atomic_arch.h: a_cas/a_swap/a_fetch_add/a_store/a_and/a_inc/...):

- **`=m` memory output**: realized as the operand's address (like an `m`
  input); the asm writes through it, no register copy-back. In the IL command
  the address counts as an *input* for liveness (it is read, not defined).
- **`=r` register output** and any allocator-chosen register operand.
- **matching constraints (`"0"`, `"1"`, ...)**: the operand shares the
  referenced operand's register (in/out operands like a_swap's `"0"(v)`).
- **multiple memory operands** (e.g. a_inc's `=m(*p)` + `m(*p)`).

The IL command now does a single deterministic operand-to-register assignment
(`_assign`, shared by clobber/abs_spot_pref/make_asm): fixed letters and
`register __asm__` bindings first, then fresh registers for `r`/`=r`, then
matching constraints, then address-staging registers for memory operands, from
a 9-register pool (rbx excluded to avoid callee-save). Pre-asm loads are emitted
through a parallel-move scheduler (`_emit_parallel`) that orders moves so none
overwrites a register another still needs, breaking cycles via a scratch
register -- this fixed a real bug where a memory operand's address-staging
register collided with an input argument's register (a_cas wrote the wrong
location). Every atomic op is differential-tested against gcc.

Result: the core atomic-using files compile -- malloc/mallocng/malloc.c,
thread/__lock.c, stdio, ctype. A sweep over errno/internal/prng/stdio/unistd/
time went from 167 pass / 31 fail to 237 pass / 18 fail; traceback crashes
dropped from 19 to 4.

## Small musl bug cluster: compound-literal postfix, pointer-diff qualifiers, as-operator symbol names

Three bounded fixes (all differential-tested vs gcc):

1. **Postfix operators on compound literals.** `(size_t[3]){0,a,b}[whence]` and
   `(struct P){..}.m` now parse. `parse_cast` returned the CompoundLiteral
   directly, bypassing postfix parsing; the postfix-operator loop is now a
   shared `_parse_postfix_ops` applied to compound literals too. (musl
   fmemopen.c / open_memstream.c / open_wmemstream.c)

2. **Pointer difference across qualifiers.** `char * - const char *` was
   rejected (the check used full `compatible()`); it now compares the
   unqualified pointed-to types (C11 6.5.6p3). (musl strcspn.c)

3. **Symbol names colliding with GNU-as operators.** A C identifier spelled
   exactly like an `as` Intel-syntax operator (shr, shl, and, or, xor, not,
   mod, eq, ne, lt, le, gt, ge, offset) cannot be emitted as a bare label --
   `as` reads `[shr]` as the shift operator. (gcc avoids this only via AT&T
   syntax.) `spots.mangle_symbol` renames these to `__shivyc_sym_<name>`
   consistently at every definition, reference, and `.global/.weak/.set/.comm`
   directive; the map is idempotent and touches only those names. (musl
   qsort.c's `static inline shr()`)

Also note: musl source must be compiled with `-D_XOPEN_SOURCE=700` (its real
build flag); without it the public `syscall()` prototype in unistd.h collides
with syscall.h's `#define syscall(...)` -- this is a build-flag requirement,
not a compiler bug (gcc fails identically without the flag).

Deferred (larger features, not bounded bugs): VLAs (`uint32_t big[bufsize]` in
vfprintf.c) and wide string/char literals (`L"..."` typed as char[] not
wchar_t[]; wcstod/wcstol/vfwscanf).

## Nested and anonymous-member designated initializers

`_flatten_init` handled only a single member designator per item, so a nested
designator (`.a.b`, `.a[2].c`) or a member promoted from a C11 anonymous
union/struct crashed with StopIteration. It now walks the full designator chain
from the aggregate, taking each member offset from `get_offset` (which resolves
both direct and anonymous-promoted members) and each array index from the
element size. (musl's sigaction reaches `.sa_sigaction` via a macro that expands
to the nested designator `.__sa_handler.sa_sigaction`.) Differential-tested vs
gcc. (timer_create.c still has a separate, later codegen issue.)

## Wide string and character literals (L"..." / L'...')

The lexer recognized the `L` prefix but discarded its wide-ness, so `L"..."`
was typed as `char[]` (incompatible with `wchar_t *`). Now the lexer flags an
L-prefixed string/char token (`Token.wide`); a wide `String` node is typed as
`wchar_t[N]` (wchar_t is int, 4 bytes), and its literal is emitted with `.int`
(4-byte) elements rather than `.byte`. `L'x'` already had the right type
(wchar_t == int == a plain char constant's promoted type), so only string
literals needed work. The `String` node reads its wide flag via getattr so
pickled ASTs cached before this change still load. Differential-tested vs gcc
(element values, sizeof, null terminator). Unblocks musl's wcstol.c and
vfwscanf.c (wcstod.c remains only on the by-design long-double rejection).

## Call-argument marshalling: hazard-free parallel moves (+ inline-asm literal fix)

Surfaced while building a partial libc.a from ShivyC-compiled musl. Two codegen
bugs fixed (both differential-tested vs gcc):

1. **Function-call argument moves clobbered each other.** `emit_reg_moves`
   emitted the integer-argument moves in naive order, relying on abs_spot_pref
   to make them no-ops. For a "shift" pattern -- e.g. passing `(const, a, b, c)`
   where a/b/c already sit in the next argument registers (exactly musl's
   `__syscall_cp(SYS_write, fd, buf, count, 0,0,0)` from `write()`) -- the
   constant was loaded into an argument register before the value already there
   was relocated, so every argument collapsed to the constant. Integer arg moves
   now go through a parallel-move scheduler (`_emit_parallel_int_moves`) that
   orders moves hazard-free and breaks cycles via a scratch register (r11/r10/
   rax). This made musl's real `write()` work end to end.

2. **Inline asm with an immediate input operand crashed the compiler.** The
   move-scheduler dedup key called `str()` on the source spot, but a LiteralSpot
   (e.g. `"a"(1L)`) returns its int from `__str__`; the key now uses `asm_str`.

## Partial libc.a milestone

ShivyC compiled 151/153 musl functions across string/ctype/stdlib/multibyte
(the 2 failures are the by-design long-double rejection) into a static libc.a.
A ShivyC-compiled program linked statically (with a minimal hand-written
`_start`, no glibc/crt) against this archive runs correctly: pure-computation
functions (strlen/memcpy/memcmp) and -- after the call-marshalling fix -- musl's
syscall-based `write()` both work. This validates the codegen/ABI end to end in
a real linked binary. (errno-setting on the syscall *error* path still needs
musl's TLS startup, a known boundary; the success path is fine.)

## Minimal TLS startup + 3-argument main

### Thread-pointer (TLS) bring-up
A stripped startup makes musl's `errno` and thread-local storage work without
the full `__libc_start_main`. On x86-64, `__set_thread_area(p)` is
`arch_prctl(ARCH_SET_FS=0x1002, p)` (syscall 158); `TP_ADJ(p)==p` and
`__pthread_self()` reads `fs:0`. So the startup points the thread pointer at a
`struct pthread`-sized block (sizeof 200; `self` at +0, `errno_val` at +52)
whose `self` field points to itself, reserving a zeroed area below the thread
pointer for `__thread` data. With this, a ShivyC-compiled program linked
against the partial libc.a:
- prints via musl `write()` (success path),
- sets and reads `errno` correctly on the syscall ERROR path (EBADF), with no
  crash -- the dereference of `&__pthread_self()->errno_val` now resolves,
- receives argc/argv/envp.

The startup (`crt_shivyc.c`) and `_start` (`start_tls.s`) are saved with the
demo (shivyc-musl-tls-demo.zip). Not yet done: initializing `__thread` data
from the ELF PT_TLS image (`__copy_tls`), and the full
auxv/init-array/ssp/poll startup.

### 3-argument main
`main` may now be declared `int main(int, char**, char**)` (the common POSIX
`envp` extension), in addition to 0- or 2-argument forms; the third parameter
is validated as char**. Differential-tested; envp is passed through correctly
by the minimal startup.

## CPython core bring-up: conditional struct operands + void/function-pointer conversion

Surfaced while compiling typeobject.c (CPython's keystone, 13k lines):

1. **Conditional operator with struct/union operands** (boolean_exprs.py). C11
   allows `cond ? structA : structB` when both have the same struct/union type;
   the result is a non-lvalue so qualifiers are dropped. ShivyC's result-type
   logic required equal const-qualification, rejecting CPython's
   `entry->value ? PyStackRef_FromPyObjectNew(...) : PyStackRef_NULL` (the latter
   is a `const _PyStackRef` global). Now compares struct operands unqualified
   and yields the unqualified struct type.

2. **void* <-> function-pointer conversion** (utils.py set_type). A GCC
   extension (used by CPython and musl) lets a void pointer convert to/from a
   function pointer and be compared against one. ShivyC allowed void*<->object*
   but not void*<->function*; both directions are now accepted.

Both differential-tested vs gcc. With these, typeobject.c advances well past
its earlier walls.

## Extension scanner: ignore preprocessor-directive continuation lines

The contract/extension pre-pass blanked comments and strings but recognized
only the first physical line of a `#define` as a directive. A function-like
name used on a backslash-continuation line of a macro (CPython's
`#define PyUnicodeError_Check(PTR) \` then `PyObject_TypeCheck((PTR), ...)`) was
mistaken for a function-definition header, aborting the scan with "unexpected
text in extension region". The pre-pass now blanks whole preprocessor
directives including their `\` continuations. Unblocks Objects/exceptions.c.

### CPython core compile status (so far)
Compile with ShivyC: boolobject, rangeobject, tupleobject, floatobject,
listobject, dictobject, longobject, **typeobject** (the type-system keystone,
13k lines), **object**, **descrobject**, **unicodectype**. Remaining core walls
identified: pystate.c needs `_Alignof(type)` (C11); obmalloc.c hits a
preprocessor self-reference subtlety; structseq.c a parse issue.

## _Alignof (C11 alignment operator)

Added the `_Alignof` keyword. `_Alignof(type-name)` (C11) and `_Alignof(expr)`
(the GCC __alignof__ form) yield a compile-time size_t alignment. A new
`CType.alignment()` reports the natural ABI alignment: scalars -> their size
(capped at 8), arrays -> element alignment, struct/union -> max member
alignment. Note ShivyC lays out struct members packed (no inter-member
padding), but `_Alignof` deliberately returns the true ABI alignment, which is
what callers use to align allocations (over-aligning is always safe). Unblocks
Python/pystate.c's `_Alignof(PyInterpreterState)`. Differential-tested vs gcc.

## structseq.c: use musl PUBLIC headers for CPython (not internal build headers)

structseq.c failed with "expected expression, got '/'" on
`hidden / sizeof(PyObject *)`. Root cause was the include configuration, not a
compiler bug: CPython was being compiled with musl's INTERNAL build include
paths (`-I$M/src/internal`, `-I$M/obj/src/internal`, `-I$M/src/include`), which
define helper macros like `hidden`/`weak` for musl's own build. CPython has a
local variable named `hidden`, so `Py_ssize_t hidden = ...` expanded to
`Py_ssize_t __attribute__((__visibility__("hidden"))) = ...` -- an invalid
declaration. gcc fails identically with those paths.

Fix: compile CPython against musl's PUBLIC headers only:
    -D_XOPEN_SOURCE=700 -I$M/arch/x86_64 -I$M/arch/generic -I$M/obj/include -I$M/include
(drop the src/internal, obj/src/internal, and src/include paths -- those are
for building musl itself). Previously-compiling core files still compile with
the public set; structseq.c now compiles too.

## _PyRuntime static initializer: array-member decay & (&E)->member address constants

CPython's giant `_PyRuntime`/`initial` static initializer (pystate.c, via
pycore_runtime_init.h) hit "non-constant initializer for variable with static
storage duration". Bisecting the exact failing element (temporary stderr
instrumentation at the error site printing the offending offset, ctype, node
type, and head-chain) showed two address-constant forms the static evaluator
did not recognize:

1. A *bare* member access `OBJ.m1.m2...` whose member type is an array. Like a
   bare array identifier, this decays to the member's address (C11 6.3.2.1p3),
   so it is an address constant (symbol + offset). `_static_addr_const` now
   handles `ObjMember` whose resolved member ctype `is_array()`, returning
   `("sym", name, offset)`. Non-array members stay an rvalue load (fall through).

2. `(&E)->member`, i.e. taking the address of a static member and re-accessing
   through `->`. Semantically identical to `E.member` (the `->` dereferences the
   address `&E` produces). `_static_member_ref` now traverses `ObjPtrMember`
   when its head (after unwrapping parens) is `&E`: it resolves `E` as a
   location and adds the member offset within E's type. The whole chain
   `(&_PyRuntime.x.y.z)->a.b` thus resolves to a symbol+offset constant.

With both, pystate.c compiles: the `initial` template is emitted as a ~377 KB
`.data` object with ~1084 relocations (`&PyLong_Type` for each small-int's
ob_type, the allocator function pointers, and the self-referential member
addresses), all constant-folded. Differential-tested vs gcc (exit 17 and 19);
regression tests in tests/test_static_addr_init.py.

## Multi-character character constants (and skipped-group lexer tolerance)

ShivyC hard-errored on any character constant with more than one character
("multiple characters in character constant"). This blocked bytes_methods.c:
including Objects/stringlib/find_max_char.h pulls in `# error C 'size_t' size
should be either 4 or 8!` in a `#else` branch. On x86-64 `SIZEOF_SIZE_T == 8`,
so that branch is SKIPPED -- but ShivyC lexes the whole file into tokens before
the preprocessor evaluates conditionals, so the `'size_t'` apostrophes were
lexed as a (malformed) char constant and raised before the inactive group was
discarded.

Fix: multi-character constants are valid C (C11 6.4.4.4p10) with an
implementation-defined value, so the lexer no longer rejects them (the
empty-constant `''` error is kept). The parser packs the bytes big-endian into
the low 32 bits as a signed int, matching gcc: `'ab' == 0x6162`,
`'abcd' == 0x61626364`. This both supports real multi-char constants and makes
the lexer tolerant of `'...'` text in skipped conditional groups. Differential-
tested vs gcc; regression tests in tests/test_multichar_const.py;
tests/feature_tests/error_string.c updated (the two multi-char cases were stale).

## Self-referential macro inside another macro's argument (hide-set fix)

methodobject.c failed with "use of undeclared identifier '_PyObject_CAST'" from
the body of `#define PyCFunction_GET_CLASS(func) PyCFunction_GET_CLASS(_PyObject_CAST(func))`
-- but only where it was used inside another macro, e.g.
`Py_VISIT(PyCFunction_GET_CLASS(m))`.

Root cause: `_subst`/`_gather_args` stripped hide sets from macro arguments
(`p.tok` instead of `p`) and applied the new hide set by REPLACING rather than
unioning. So when an argument was expanded (e.g. `CLS(5)` -> a blue-painted
`CLS(((int)((5))))`) and that result was reused/substituted, the blue paint on
the self-referential call was lost; on rescan it was re-expanded, and the
accumulating hide sets contaminated the sibling `_PyObject_CAST` token with its
own name, so it was treated as already-expanded and left as a bare identifier.

Fix (Prosser's algorithm): arguments are now collected and expanded as `_PP`
objects so each token keeps its hide set, and `_subst` UNIONS the new hide set
HS' onto every result token (`hsadd`) instead of replacing. Body tokens start
with empty hide and receive HS'; argument tokens retain their accumulated hide
(keeping blue paint) and also receive HS'. Differential-tested vs gcc;
regression tests in tests/test_selfref_macro_arg.py. methodobject.c now gets
past this point (next wall: objimpl.h:198 void-return, unrelated).

## Transpile-friendliness + speed: removing deepcopy from the parser

Groundwork for a future source-to-C transpiler (and a direct speed win):

1. **Eliminated copy.deepcopy in the parser's backtracking.** The parser's
   log_error() context manager backed up the symbol table with copy.deepcopy on
   every speculative parse. Profiling showed deepcopy dominated compile time
   (~2/3 of runtime). The symbol table is a list of {str: bool} scope dicts --
   keys and values are immutable -- so a shallow per-scope dict copy is a fully
   correct backup. Added SimpleSymbolTable.snapshot()/restore() (explicit, with
   no library deepcopy, so a transpiler can emit a plain loop of map copies) and
   switched log_error() to use them, preserving the table's object identity.
   Results (pypy3, --no-cache): synthetic 2.9k-line file 5.40s -> 4.04s (~25%);
   real Objects/rangeobject.c 43.6s -> 11.5s (~3.8x; the win scales with symbol
   table size, which is large on header-heavy CPython input). Output is
   byte-identical before/after (cmp), and the 528-test suite stays green.

2. **Type hints + monomorphic locals.** Added `from __future__ import
   annotations` and function/local type hints to shivyc/parser/utils.py and
   shivyc/spots.py, and confirmed locals hold a single type throughout (so a
   transpiler can assign one C type per variable). mypy validates the
   annotations. This is incremental groundwork; the same pattern extends to the
   remaining modules.
