/* la.h -- fixed-size linear algebra for the C++ subset.
 *
 * The Eigen alternative described in EIGEN_DIRECTION.md, or rather the part
 * of it a library can be. Eigen's template metaprogramming does two jobs:
 * it carries sizes and scalar types in the type system, and it fuses
 * expressions so `A*x + b` allocates nothing. The first is ordinary generic
 * programming and this file does it. The second is what expression
 * templates are for, and it is *not* here -- it belongs to the compiler,
 * where ShivyCX's contract vectorizer already does the equivalent thing for
 * numpy. Eigen fuses in the type system because C++ gave a library author
 * nowhere else to do it. That constraint is not ours.
 *
 * So there are no traits, no `Scalar` typedef chains, no `XprType`, no
 * `nested_eval`, no `.eval()`, no lazy proxies. `A * x` computes a vector.
 *
 * WHY THE OPERATORS LOOK LIKE THIS
 *
 * Each one is a plain loop over the receiver's own storage, returning a
 * fresh value. That is not a naive first cut -- it is the shape ShivyCX
 * vectorizes. A `Vec<float,16>` is `struct { float d[16]; }`, so the
 * element count is a fact of the type; the operator returns by value, so
 * the destination arrives as System V's hidden sret pointer; and the loop
 * covers every element, so the local the source writes is elided. The whole
 * of `operator+` comes out as:
 *
 *     movups xmm1, [rsi + rax]      ; this->d
 *     movups xmm2, [rdx + rax]      ; o->d
 *     addps  xmm1, xmm2
 *     movups [rdi + rax], xmm1      ; the returned Vec, in place
 *
 * with no scalar remainder, no call, and no copy. Delegating to a separate
 * kernel function would be *worse*: the arrays would then belong to the
 * caller's frame and could not be proven at all.
 *
 * NO CONTRACTS ARE WRITTEN HERE
 *
 * ShivyCX's vectorizer runs off contract clauses, and it would be tempting
 * to write them into these operators. They must not be: a clause is a
 * ShivyCX extension, so a header carrying one is not C++ and will not
 * compile under g++ at all. They are *inferred* instead, under
 * `--contracts`, from the struct each operator takes -- a `Vec<float,16>`
 * lowers to `struct { float d[16]; }`, so the element count is on the page
 * and the divisibility follows from it. This file stays ordinary C++ that
 * happens to vectorize when ShivyCX compiles it, which is the whole
 * "size in the type becomes the proof in the compiler" claim, arrived at
 * without the header knowing anything about SIMD.
 *
 * FIXED SIZES ONLY
 *
 * Every type here stores its elements inline and owns nothing. That is not
 * incidental: a class that owns nothing is the one that gets a by-value
 * chain (`a + b + c`), and a fixed size is what gives the loop a literal
 * trip count. The two properties the subset rewards are the two properties
 * Eigen's fast path already has. A dynamically-sized matrix would have a
 * destructor, so a chain over one is refused -- correctly, since the copy
 * would make a second owner. Those get explicit destinations instead, and
 * are not in this file yet.
 *
 * TWO OF EIGEN'S FOOTGUNS ARE ABSENT, NOT FIXED
 *
 *   auto x = A * b;   is correct here. In Eigen it captures a proxy holding
 *                     references to temporaries and is the single most
 *                     reported bug in the library.
 *   A = A * B;        is safe here. In Eigen it silently corrupts unless
 *                     you write .eval() or .noalias(), because the lazy
 *                     product reads A while writing it.
 *
 * Both hazards are created entirely by the expression-template strategy.
 * There is nothing to alias when the operator returns a fresh value.
 *
 * LIMITS, STATED RATHER THAN DISCOVERED
 *
 *   - `a + b * c` regroups correctly, but every operand must be a plain
 *     name. Assign a call result to a local first.
 *   - `T * Vec` (scalar on the left) is not available: an overloaded
 *     operator is a member here, so its left operand has to be an object of
 *     the class. Write `v * s`.
 *   - Element counts that are not a multiple of the SIMD lane width still
 *     compute correctly; they just do not get the fallback-free loop.
 */

#ifndef CRUST_LA_H
#define CRUST_LA_H

/* ------------------------------------------------------------------ Vec */

template<typename T, int N>
class Vec {
public:
    T d[N];

    Vec() { int i = 0; for (i = 0; i < N; i = i + 1) { d[i] = 0; } }

    /* Elementwise. Each returns a fresh Vec, which is the destination the
     * vectorizer writes into. */
    Vec operator+(const Vec &o)
    {
        Vec r; int i = 0;
        for (i = 0; i < N; i = i + 1) { r.d[i] = d[i] + o.d[i]; }
        return r;
    }

    Vec operator-(const Vec &o)
    {
        Vec r; int i = 0;
        for (i = 0; i < N; i = i + 1) { r.d[i] = d[i] - o.d[i]; }
        return r;
    }

    /* Elementwise product (Eigen spells this `.cwiseProduct`; there is no
     * ambiguity to avoid here, since a Vec is not a matrix). */
    Vec operator*(const Vec &o)
    {
        Vec r; int i = 0;
        for (i = 0; i < N; i = i + 1) { r.d[i] = d[i] * o.d[i]; }
        return r;
    }

    /* Scalar broadcast. The scalar is splatted across the lanes once. */
    Vec scaled(T s) {
        Vec r; int i = 0;
        for (i = 0; i < N; i = i + 1) { r.d[i] = d[i] * s; }
        return r;
    }

    /* Reductions. These return a scalar, so there is no destination to
     * write and the lane accumulator is summed horizontally at the end. */
    T dot(const Vec &o) {
        T acc = 0; int i = 0;
        for (i = 0; i < N; i = i + 1) { acc = acc + d[i] * o.d[i]; }
        return acc;
    }

    T sum() {
        T acc = 0; int i = 0;
        for (i = 0; i < N; i = i + 1) { acc = acc + d[i]; }
        return acc;
    }

    /* Written out rather than `dot(*this)`: a reference parameter is a
     * pointer here, and `*this` is a value, so the subset has nothing to
     * take the address of. The loop is the same one `dot` runs. */
    T squaredNorm() {
        T acc = 0; int i = 0;
        for (i = 0; i < N; i = i + 1) { acc = acc + d[i] * d[i]; }
        return acc;
    }

    void setAll(T v) { int i = 0; for (i = 0; i < N; i = i + 1) { d[i] = v; } }
    int size() { return N; }
};

/* ------------------------------------------------------------------ Mat */

template<typename T, int R, int C>
class Mat {
public:
    /* Row-major, inline. `R * C` is substituted and folded at
     * monomorphisation, so the struct is a plain fixed array. */
    T d[R * C];

    Mat() { int i = 0; for (i = 0; i < R * C; i = i + 1) { d[i] = 0; } }

    Mat operator+(const Mat &o)
    {
        Mat r; int i = 0;
        for (i = 0; i < R * C; i = i + 1) { r.d[i] = d[i] + o.d[i]; }
        return r;
    }

    Mat operator-(const Mat &o)
    {
        Mat r; int i = 0;
        for (i = 0; i < R * C; i = i + 1) { r.d[i] = d[i] - o.d[i]; }
        return r;
    }

    Mat scaled(T s) {
        Mat r; int i = 0;
        for (i = 0; i < R * C; i = i + 1) { r.d[i] = d[i] * s; }
        return r;
    }

    /* Matrix times vector. The operand and the result are a different class
     * than the receiver, which the lowering allows: the receiver decides
     * the symbol and the operand type is checked against the declaration.
     *
     * Not a recognised SIMD kind -- the inner loop is a reduction per row,
     * not one pass over the arrays -- so this stays scalar under ShivyCX
     * and is left to gcc's vectorizer under gcc. A blocked kernel for small
     * fixed sizes is the next piece of compiler work, not library work.
     */
    Vec<T,R> operator*(const Vec<T,C> &v) {
        Vec<T,R> r; int i = 0; int j = 0;
        for (i = 0; i < R; i = i + 1) {
            T acc = 0;
            for (j = 0; j < C; j = j + 1) {
                acc = acc + d[i * C + j] * v.d[j];
            }
            r.d[i] = acc;
        }
        return r;
    }

    T at(int i, int j) { return d[i * C + j]; }
    void put(int i, int j, T v) { d[i * C + j] = v; }
    int rows() { return R; }
    int cols() { return C; }
};

#endif
