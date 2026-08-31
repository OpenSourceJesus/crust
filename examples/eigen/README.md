# `la.h` — fixed-size linear algebra for the C++ subset

The library half of the Eigen alternative. See EIGEN_DIRECTION.md (and
issue #13) for why it is shaped this way; the short version is that Eigen's
template metaprogramming does two jobs, and only one of them belongs in a
library.

**Job A — sizes and scalar types in the type system.** `Matrix<double,4,4>`
exists so a 4×4 multiply has literal trip counts, inline storage and no
runtime dispatch. That is ordinary generic programming, and this file does
it with the subset's monomorphiser.

**Job B — fusing `A*x + b` so it allocates nothing.** That is what
expression templates are, and it is *not* here. Eigen does it in the type
system because C++ gave a library author nowhere else to do it. Crust has
somewhere else: `shivyc/simd_contracts.py`.

So there are no traits, no `Scalar` chains, no `XprType`, no `.eval()`, no
lazy proxies. `A * x` computes a vector.

## Using it

```cpp
#include "la.h"

Vec<float,16> a; Vec<float,16> b; Vec<float,16> c;
Vec<float,16> s = a + b * c;      // regroups by precedence
Vec<float,16> u = a.scaled(5.0f);
float dp = a.dot(b);

Mat<float,4,4> M; Vec<float,4> x;
Vec<float,4> y = M * x;
```

## Three ways to build it, all agreeing

```sh
g++ -std=c++11 -I. demo.cpp -o demo                        # the reference
python3 ../../tools/cpprust.py demo.cpp -o demo.c && gcc -O2 demo.c -o demo
python3 ../../tools/cpprust.py demo.cpp -o demo.c --contracts \
    && python3 -m shivyc.main demo.c -o demo               # from the repo root
```

All three return 51. `tools/test_la_header.py` pins that, and pins the
packed instructions separately — a kernel that quietly stopped vectorizing
would still return 51.

## The header contains no SIMD

No `assert`, no intrinsic, no lane width, no mention of a vector unit. The
ShivyCX contracts that turn the vectorizer on are **inferred**, under
`--contracts`, from the struct each operator takes: a `Vec<float,16>` lowers
to `struct { float d[16]; }`, so the element count is on the page and the
divisibility follows.

This is not a stylistic choice. A contract clause is a ShivyCX extension and
cpprust passes an author-written one through unconditionally, so a header
that *wrote* one would not be C++ and g++ would reject it outright. The
first two builds above are only possible because the header stays ordinary.

What comes out under ShivyCX is the whole of `operator+`:

```asm
movups xmm1, [rsi + rax]      ; this->d
movups xmm2, [rdx + rax]      ; o->d
addps  xmm1, xmm2
movups [rdi + rax], xmm1      ; the returned Vec, written in place
```

No scalar remainder, no call, no copy. The local the source names, its
zeroing constructor and the return copy are all elided — the operator
returns by value, so the destination arrives as the hidden sret pointer,
and the loop covers every element, which is what makes eliding the local
the same program.

Proven today: `+`, `-`, `*` elementwise, `scaled` (broadcast), `dot`
(reduction).

## Deliberate limits

* **Fixed sizes only.** Everything stores inline and owns nothing — which
  is exactly what earns the by-value chain (`a + b + c`) and the literal
  trip count. A dynamically-sized matrix would have a destructor, so a
  chain over one is refused, correctly: the copy would make a second owner.
  Those want explicit destinations (`mul(A, x, y)`) and are not here yet.
* **`M * x` stays scalar under ShivyCX.** Its inner loop is a reduction per
  row, not one pass over the arrays, so it is not a recognised kind. That
  is the next piece of *compiler* work — a blocked kernel for small fixed
  sizes — not library work. gcc vectorizes it on the other path.
* **Operands must be plain names.** `a + b * c` regroups correctly, but
  assign a call result to a local first.
* **No `s * v`**, only `v.scaled(s)` or `v * w`. An overloaded operator is
  a member here, so its left operand has to be an object of the class.
* **No decompositions.** LU, QR, Cholesky, `.solve()`. Most Eigen use is a
  small fraction of its surface; shipping that fraction honestly beats
  shipping a stub of the whole.

## Two of Eigen's footguns are absent rather than fixed

```cpp
auto x = A * b;   // correct here; in Eigen this captures a proxy holding
                  // references to temporaries — its most reported bug
A = A * B;        // safe here; in Eigen this silently corrupts without
                  // .eval() or .noalias(), since the lazy product reads A
                  // while writing it
```

Both hazards are created entirely by the expression-template strategy.
There is nothing to alias when an operator returns a fresh value.
