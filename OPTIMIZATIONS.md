# ShivyCX code-generation optimizations

ShivyCX is a textbook `-O0` code generator: it lowers each IL command in
isolation. That leaves a large, mechanical gap against `gcc -O2`. This document
records the optimizations added to narrow that gap, measured on the C-subset
lexer kernel in `examples/rpython2c/compiler/`.

All optimizations are correctness-preserving and can be disabled with
`--no-peephole` (for the peephole pass) to A/B their effect.

## 1. Index strength reduction for byte-sized elements

`get_size` (`shivyc/tree/utils.py`) used to emit `index * sizeof(elem)` for
every `p[i]`. For `char`/byte arrays `sizeof(elem) == 1`, so this produced a
useless `imul $1` on *every* character access — pervasive in lexers and all
string code. `get_size` now returns the index unscaled when the element size is
1, eliminating the multiply.

## 2. Compare-and-branch fusion (`shivyc/peephole.py`)

A comparison feeding directly into a conditional jump used to compile as:

```
mov  r, 1
cmp  a, b
jcc  L          ; materialize a 0/1 boolean
mov  r, 0
L:
cmp  r, 0       ; ...then test it
je   target
```

i.e. ~6 instructions per loop/`if` test. The peephole detects a `_GeneralCmp`
whose boolean result is consumed solely by the next `JumpZero`/`JumpNotZero`
and fuses them into a single compare-and-branch:

```
cmp  a, b
jge  target     ; one cmp, one jcc -- matches gcc
```

Details and guards:

- The comparison gains a `fuse = (label, negate)` marker. When set, its
  `make_asm` emits `cmp; jcc` and skips the boolean; `outputs()` becomes empty
  and `targets()` reports the branch label so the register allocator's liveness
  analysis still sees the control-flow edge. (Omitting `targets()` corrupts
  liveness and lets the allocator wrongly coalesce a loop-carried value with a
  per-iteration temporary — caught in testing.)
- Each comparison subclass carries both its true-condition jump and its negated
  jump (signed and unsigned), so `JumpZero` (branch when the comparison is
  false) is handled correctly.
- Fusion is skipped when the first operand is a literal (the comparison codegen
  swaps literal-first operands, which would invert an ordering test) and for
  floating-point comparisons.

## 3. Arithmetic identities (`shivyc/peephole.py`)

`x * 1`, `1 * x`, `x + 0`, `0 + x`, and `x - 0` become a plain copy; these arise
from address arithmetic and generic code. Implemented as IL rewrites to `Set`,
so the register coalescer usually removes them entirely.

## 4. Loop-invariant code motion (`shivyc/peephole.py`)

A computation whose operands do not change across a loop is hoisted into a
preheader so it runs once instead of every iteration. In the lexer this removes
the per-iteration reload of the loop-invariant global modulus `HMOD` (gcc does
the same); the divisor is loaded once before the loop and kept in a register.

The pass is deliberately conservative:

- It only fires on a structured loop (a single backward edge, that edge being
  the label's only jump-predecessor, no nested back-edges in range).
- It refuses to hoist out of a loop containing a **call or a store**, so a hoisted
  read cannot observe a stale value. Plain loads are permitted inside the loop
  (they do not modify memory) but are never themselves hoisted.
- It hoists only pure, non-trapping commands (`Set`, address arithmetic, integer
  add/sub/mul, bitwise ops, shifts) whose operands are all defined outside the
  loop and whose result is defined exactly once in the loop. `Div`/`Mod` are
  excluded (they can trap), as are dereferencing loads (which could fault if the
  loop runs zero times).

Verified to leave nested loops, zero-trip loops, and loops that mutate the read
value via a call or store unchanged.

## Comparison codegen fix (literal-first operands)

While adding fusion we found a latent bug in the base comparison codegen: when
the first operand is a literal (e.g. `5 < x`), the operands are swapped so the
immediate is not the `cmp` destination, but the conditional jump was not
adjusted, inverting ordering comparisons. The comparison now reports whether the
swap happened and selects the reversed-comparison jump accordingly (and the
negated-reversed jump for the fused path). All six relations were verified for
signed, unsigned, var-first and literal-first operands against gcc.

## 5. Induction-variable strength reduction (`shivyc/peephole.py`)

An address recomputed from the loop counter every iteration
(`addr = base + scale*i + off`) is replaced by a pointer that is computed once
in a preheader and advanced by a constant stride each iteration. The index
recompute, sign-extend, and add-base sequence collapses to a single `add`,
matching what gcc does for array traversals:

```
    ; before:                    ; after:
    mov   %esi,%ecx              mov   (%rdi),%dl
    add   %r9d,%ecx              ...
    movslq %ecx,%rcx             add   $1,%rdi      ; advance pointer
    add   %rdi,%rcx
    mov   (%rcx),%dl
```

The pass detects a basic induction variable (`t = i + c; i = t`, c literal),
then traces each `ReadAt`/`SetAt` address back through `Set`/`Add`/`Subtr`/`Mult`
to confirm it is an affine function of exactly one IV with a compile-time
stride. It is gated for safety:

- single-basic-block loop body (no internal labels or jumps other than early
  exits and the back-edge), so the pointer advance executes once per iteration;
- the address and the chain's intermediate temps are used only to compute that
  one access (so the chain can be moved to the preheader);
- the pointer is advanced through a temporary (`t = p + stride; p = t`) rather
  than in place, because ShivyCX's liveness pass mishandles a read-and-write of
  the same value and would let the allocator clobber the live pointer.

Like gcc -O2, this assumes the affine index does not signed-overflow, so that
sign-extension is linear. Verified against gcc on int/double/char arrays, an
index with an invariant offset, a scaled index (`a[i*2]`), a reverse traversal
(`a[n-1-i]`, negative stride), a store loop, two parallel pointers, nested
loops, zero-trip loops, and a non-unit IV step.

## 6. Multiply by power of two → left shift (`shivyc/peephole.py`)

Integer `x * 2^k` becomes `x << k` (`LBitShift` / `sal`). Unlike division,
signed and unsigned agree on left shift, so both are rewritten. Floating-point
multiplies are left alone. Measured on `benchmarks/codegen/bench_mul_pow2.c`:
ShivyCX emits `sal`/`shr` with **zero** `imul`/`idiv` for the power-of-two
scale loops.

## Result

| backend  | speedup vs CPython                   |
|----------|--------------------------------------|
| CPython  | 1.0x                                 |
| ShivyCX  | ~18.6x -> ~20.9x -> ~21.8x           |
| gcc -O2  | ~51x                                 |

IV strength reduction does not move the lexer number further: `word_hash` is a
serial `idiv`-bound recurrence (`h = (h*K + byte) % M`), and the address
arithmetic it eliminates already overlapped with the division latency. On an
address-bound loop where nothing else is the bottleneck the same pass is worth
~1.7x and matches gcc -O2.

## Remaining gap

Both compilers emit the same `idiv` for the modulo (the divisor is a runtime
global, so neither strength-reduces it); on this kernel that division latency,
not codegen, is now the dominant term.

### On SIMD / contracts for this kernel

The hash accumulator is a serial recurrence (`h = (h*K + byte) % M`), so the
hot loop is not vectorizable, and no data-parallel divisibility contract (of the
kind the SSE element-wise path uses) applies to a scanner. Realistic SIMD for
lexing means simdjson-style character-class/boundary finding, which is an
algorithmic rewrite rather than a codegen pass. The gap here is scalar codegen
quality, addressed by the optimizations above.
