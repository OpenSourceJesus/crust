"""The fixed-size numeric types the Eigen alternative is built on.

Four things had to change in the lowering before a `Mat<T,R,C>` could be
written the way anyone actually writes one, and each is pinned here:

* a bare class name inside its own instantiated body (`Vec operator+(const
  Vec &)`), which used to reach C as an undeclared `Vec`;
* an array bound that is an *expression* (`T d[R * C]`), which used to come
  out half-substituted because the declarator was tokenised on `*`;
* an operator whose operand and result are a *different* class than the
  receiver (`Vec<T,R> Mat::operator*(const Vec<T,C> &)`), which used to
  fall through to a raw `*` on two structs;
* a run of operators mixing precedence (`A * x + b`), which used to be
  refused because the fold went left to right.

The refusals that stood before are pinned too, in `TestNumericRefusals`:
each of the last two now lowers only when it *can*, and an owning class
still gets the message it always did.

"""

import os, sys
import subprocess
import tempfile
import unittest
sys.path.append(os.path.split(__file__)[0])
import cpprust


VEC = """
template<typename T, int N>
class Vec {
public:
    T d[N];
    Vec() { int i = 0; for (i = 0; i < N; i = i + 1) { d[i] = 0; } }
    Vec operator+(const Vec &o) {
        Vec r; int i = 0;
        for (i = 0; i < N; i = i + 1) { r.d[i] = d[i] + o.d[i]; }
        return r;
    }
    Vec operator*(const Vec &o) {
        Vec r; int i = 0;
        for (i = 0; i < N; i = i + 1) { r.d[i] = d[i] * o.d[i]; }
        return r;
    }
};
"""

MAT = """
template<typename T, int R, int C>
class Mat {
public:
    T d[R * C];
    Mat() { int i = 0; for (i = 0; i < R * C; i = i + 1) { d[i] = 0; } }
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
};
"""


class Base(unittest.TestCase):
    def lower(self, src):
        return cpprust.translate(src)

    def refuses(self, src, needle):
        with self.assertRaises(cpprust.CppError) as cm:
            self.lower(src)
        self.assertIn(needle, cm.exception.message)


class TestInjectedClassName(Base):
    """`Vec` inside `Vec<T,N>` means `Vec<T,N>` -- C++ calls it the injected
    class name, and nothing in parameter substitution covers it."""

    def test_return_parameter_and_local(self):
        out = self.lower(VEC + "Vec<float,8> v;")
        # Every position the bare name appeared in: the return type, the
        # reference parameter, and the local the operator builds.
        self.assertIn("static Vec_float_8 Vec_float_8__binadd("
                      "Vec_float_8 *this, const Vec_float_8 *o)", out)
        self.assertIn("Vec_float_8 r;", out)
        # Nothing may still spell the bare template name.
        self.assertNotIn("Vec *", out)
        self.assertNotIn(" Vec ", out)

    def test_template_use_is_still_mangled_not_rewritten(self):
        """`Vec<T,N>` written out longhand must not become
        `Vec_float_8<float, 8>` -- the rewrite skips a name followed by
        `<`, leaving it to the ordinary use-mangling."""
        out = self.lower("""
template<typename T, int N>
class Vec {
public:
    T d[N];
    Vec() { d[0] = 0; }
    Vec<T,N> twin() { Vec<T,N> r; return r; }
};
Vec<int,4> v;
""")
        self.assertIn("Vec_int_4 Vec_int_4_twin(Vec_int_4 *this)", out)
        self.assertNotIn("Vec_int_4<", out)


class TestArrayBoundExpression(Base):
    """A non-type parameter inside an array bound, where the bound is an
    expression rather than a bare name."""

    def test_product_bound_is_fully_substituted(self):
        out = self.lower(MAT.replace("Vec<T,R>", "T").replace(
            "Vec<T,C> &v", "T &v").replace(
            "Vec<T,R> r; int i = 0; int j = 0;", "T r = 0; int i = 0;")
            .replace("""
            T acc = 0;
            for (j = 0; j < C; j = j + 1) {
                acc = acc + d[i * C + j] * v.d[j];
            }
            r.d[i] = acc;""", "            r = r + d[i];")
            + "Mat<float,4,3> m;")
        self.assertIn("float d[4 * 3];", out)
        self.assertNotIn("C]", out)

    def test_several_declarators_keep_their_own_bounds(self):
        """The suffix is taken off each declarator, not just the first."""
        out = self.lower("""
template<typename T, int N>
class Two {
public:
    T a[N * 2], b[N];
    Two() { a[0] = 0; b[0] = 0; }
};
Two<int,3> t;
""")
        self.assertIn("int a[3 * 2];", out)
        self.assertIn("int b[3];", out)

    def test_pointer_declarator_still_splits(self):
        """`int *p, q;` -- the star belongs to `p`, and taking the array
        suffix off first must not disturb that."""
        out = self.lower("""
class K {
public:
    int *p, q;
    K() { p = 0; q = 0; }
};
""")
        self.assertIn("int * p;", out)
        self.assertIn("int q;", out)


class TestHeterogeneousOperator(Base):
    """An operator whose operand -- and result -- is a different class."""

    def test_matrix_times_vector_lowers(self):
        out = self.lower(VEC + MAT + """
int f(void) {
    Mat<float,4,4> A; Vec<float,4> x;
    Vec<float,4> y = A * x;
    return (int)y.d[0];
}
""")
        self.assertIn("static Vec_float_4 Mat_float_4_4__binmul("
                      "Mat_float_4_4 *this, const Vec_float_4 *v)", out)
        self.assertIn("Vec_float_4 y = Mat_float_4_4__binmul(&A, &x);", out)

    def test_wrong_operand_class_is_not_lowered(self):
        """`A * A` names an operator taking a `Vec`. It must not lower to
        the `Vec` one -- the operand class is checked, not merely the
        operator's presence."""
        out = self.lower(VEC + MAT + """
int f(void) {
    Mat<float,4,4> A; Mat<float,4,4> B;
    return 0;
}
""")
        self.assertNotIn("Mat_float_4_4__binmul(&A, &B)", out)


class TestPrecedence(Base):
    """A run mixing `+` and `*` regroups instead of folding left."""

    def test_matrix_expression_regroups(self):
        out = self.lower(VEC + MAT + """
int f(void) {
    Mat<float,4,4> A; Vec<float,4> x; Vec<float,4> b;
    Vec<float,4> y = A * x + b;
    return (int)y.d[0];
}
""")
        self.assertIn("Vec_float_4__binadd_vv("
                      "Mat_float_4_4__binmul_vv(A, x), b)", out)

    def test_tighter_operator_binds_first(self):
        out = self.lower(VEC + """
int f(void) {
    Vec<int,4> a; Vec<int,4> b; Vec<int,4> c;
    Vec<int,4> y = a + b * c;
    return y.d[0];
}
""")
        self.assertIn("Vec_int_4__binadd_vv(a, Vec_int_4__binmul_vv(b, c))",
                      out)

    def test_equal_precedence_run_still_folds_left(self):
        """Deliberately unchanged: while every operator binds equally
        tightly the old fold is already right, so it keeps emitting what it
        always did rather than routing through the new door."""
        out = self.lower(VEC + """
int f(void) {
    Vec<int,4> a; Vec<int,4> b; Vec<int,4> c;
    Vec<int,4> y = a + b + c;
    return y.d[0];
}
""")
        self.assertIn("Vec_int_4__binadd_v(Vec_int_4__binadd(&a, &b), &c)",
                      out)


_OWNING = """
class Buf {
public:
    char *p;
    Buf() { p = 0; }
    ~Buf() { }
    Buf operator+(const Buf &o) { Buf r; return r; }
    Buf operator*(const Buf &o) { Buf r; return r; }
};
"""


class TestNumericRefusals(Base):
    """The refusals that still stand. A pass that grants new ground has to
    say where the ground ends, and for this compiler a refusal is the
    deliverable."""

    def test_owning_class_still_refuses_a_chain(self):
        self.refuses(_OWNING + """
int f(void) { Buf a; Buf b; Buf c; Buf s = a + b + c; return 0; }
""", "owns a resource")

    def test_owning_class_still_refuses_mixed_precedence(self):
        """The precedence path needs the by-value door, and an owning class
        has none -- so this reaches exactly the refusal it did before,
        rather than silently regrouping through a copy that would make a
        second owner."""
        self.refuses(_OWNING + """
int f(void) { Buf a; Buf b; Buf c; Buf s = a + b * c; return 0; }
""", "would group left, which is not what this expression means")

    def test_expression_operand_still_refuses(self):
        self.refuses(VEC + """
int f(void) {
    Vec<int,4> a; Vec<int,4> b;
    Vec<int,4> y = a + (b);
    return 0;
}
""", "has to be a plain name")


_KERNEL = """
void *malloc(unsigned long);

class Kern {
public:
    int tag;
    Kern() { tag = 0; }
    void vadd(float *a, float *b, float *o, int n)
    assert not len(a) %% 4
    assert len(a) >= 4
    {
        int i = 0;
        for (i = 0; i < n; i = i + 1) { o[i] = a[i] + b[i]; }
    }
};

int main() {
    Kern k;
    float *a = (float *)malloc(64 * 4);
    float *b = (float *)malloc(64 * 4);
    float *o = (float *)malloc(64 * 4);
    int i = 0;
    for (i = 0; i < 64; i = i + 1) { a[i] = 1.0f; b[i] = 2.0f; }
    k.vadd(a, b, o, 64);
    return (int)o[0];
}
""" % ()


class TestMethodContracts(Base):
    """ShivyCX contract clauses on a *method*.

    They already worked on a free function -- this file does not read one,
    so they passed through untouched -- and stopped at the class boundary,
    which is exactly where a numeric library lives. A contract sits between
    the parameter list and the body, which is where a constructor's
    initializer list sits, so the tail parser met them first.
    """

    def test_clauses_survive_the_method_rewrite(self):
        out = self.lower(_KERNEL)
        # Between the lowered parameter list and the body, with the
        # parameter names intact: `this` is prepended, nothing is renamed.
        self.assertIn("static void Kern_vadd(Kern *this, float *a, "
                      "float *b, float *o, int n)", out)
        self.assertIn("assert not len(a) % 4", out)
        self.assertIn("assert len(a) >= 4", out)

    def test_trailing_const_is_not_eaten(self):
        out = self.lower("""
class K {
public:
    int t;
    K() { t = 0; }
    int go(int *p, int n)
    assert not len(p) % 4
    const
    { int i = 0; int s = 0; for (i = 0; i < n; i = i + 1) { s = s + p[i]; }
      return s; }
};
""")
        self.assertIn("assert not len(p) % 4", out)
        self.assertNotIn("const\n", out.split("assert not len(p) % 4")[1][:40])

    def test_a_contract_on_a_field_is_refused(self):
        """A contract is proven at the call site from the argument passed,
        so it can only speak about parameters. A field has no call site to
        be proven at, and a clause naming one could never be proven --
        which is safe but silently useless, so it is reported."""
        self.refuses("""
class V {
public:
    float d[64];
    void go(int n)
    assert not len(d) % 4
    { int i = 0; for (i = 0; i < n; i = i + 1) { d[i] = 0; } }
};
""", "which is not a parameter of it")


def _have(tool):
    try:
        subprocess.check_output([tool, "--version"],
                                stderr=subprocess.STDOUT)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


@unittest.skipUnless(_have("gcc") and _have("g++"), "gcc/g++ not available")
class TestAgainstRealCpp(Base):
    """Lowering is not the claim; the claim is that the C means what the
    C++ meant. Each of these compiles both ways and compares exit codes.

    The grouping test is the one that matters: a left-to-right fold of
    `a + b * c` gives 25 and the correct grouping gives 17, so agreeing
    with g++ here is evidence about precedence rather than about arithmetic
    in general.
    """

    def _both(self, src):
        d = tempfile.mkdtemp()
        c = os.path.join(d, "t.c")
        with open(c, "w") as f:
            f.write(self.lower(src))
        exe = os.path.join(d, "t")
        subprocess.check_output(["gcc", "-std=c11", "-w", "-o", exe, c],
                                stderr=subprocess.STDOUT)
        cpp = os.path.join(d, "t.cpp")
        with open(cpp, "w") as f:
            f.write(src)
        rexe = os.path.join(d, "r")
        subprocess.check_output(["g++", "-std=c++11", "-w", "-o", rexe, cpp],
                                stderr=subprocess.STDOUT)
        return (subprocess.call([exe]), subprocess.call([rexe]))

    def test_grouping_matches(self):
        ours, ref = self._both(VEC + """
int main() {
    Vec<int,4> a; Vec<int,4> b; Vec<int,4> c;
    int i = 0;
    for (i = 0; i < 4; i = i + 1) { a.d[i] = 2; b.d[i] = 3; c.d[i] = 5; }
    Vec<int,4> y = a + b * c;
    return y.d[0];
}
""")
        self.assertEqual(ours, ref)
        self.assertEqual(ours, 17)          # not 25

    def test_matrix_expression_matches(self):
        ours, ref = self._both(VEC + MAT + """
int main() {
    Mat<float,4,4> A; Vec<float,4> x; Vec<float,4> b;
    int i = 0;
    for (i = 0; i < 16; i = i + 1) { A.d[i] = 1.0f; }
    for (i = 0; i < 4; i = i + 1) { x.d[i] = 2.0f; b.d[i] = 1.0f; }
    Vec<float,4> y = A * x + b;
    return (int)y.d[0];
}
""")
        self.assertEqual(ours, ref)
        self.assertEqual(ours, 9)


class TestJoinedShivyCXPath(Base):
    """C++ source -> cpprust -> ShivyCX, with the contract proven and the
    kernel vectorized at the far end.

    This is the claim EIGEN_DIRECTION.md §8 listed as *unverified*: the two
    halves had each been run, but a contract had never travelled from a C++
    method all the way to a proof, because it could not be written on a
    method at all. Both halves plus the join are asserted here so the claim
    stops resting on inference.

    The receiver is what made the join non-trivial. A method lowers to
    `Kern_vadd(Kern *this, float *a, ..)`, and the prover required *every*
    pointer argument to trace back to a `malloc` -- which `this` never
    does. `_arg_layout` now counts a pointer as a kernel array only when it
    points at an arithmetic element, so `this` is an ordinary opaque
    argument that still occupies its System V register.
    """

    def _asm(self, src):
        d = tempfile.mkdtemp()
        c = os.path.join(d, "t.c")
        with open(c, "w") as f:
            f.write(self.lower(src))
        s = os.path.join(d, "t.s")
        rc = subprocess.call(
            ["python3", "-m", "shivyc.main", c, "-S", "-o", s],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(rc, 0)
        with open(s) as f:
            return f.read()

    def test_contract_from_a_cpp_method_is_proven_and_vectorized(self):
        asm = self._asm(_KERNEL)
        body = asm.split("Kern_vadd:")[1].split("ret")[0]
        # Packed float add, and the receiver did not displace the arrays:
        # this=rdi, a=rsi, b=rdx, o=rcx, n=r8d.
        self.assertIn("addps", body)
        self.assertIn("[rsi + rax]", body)
        self.assertIn("[rdx + rax]", body)
        self.assertIn("[rcx + rax]", body)
        # The point of the contract: no scalar remainder after the loop.
        self.assertNotIn("addss", body)

    def test_it_still_computes_the_right_answer(self):
        d = tempfile.mkdtemp()
        c = os.path.join(d, "t.c")
        with open(c, "w") as f:
            f.write(self.lower(_KERNEL))
        exe = os.path.join(d, "t")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        rc = subprocess.call(["python3", "-m", "shivyc.main", c, "-o", exe],
                             cwd=root, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        self.assertEqual(rc, 0)
        self.assertEqual(subprocess.call([exe]), 3)     # 1.0 + 2.0


_AUTO = """
template<typename T, int N>
void la_add(T o[N], const T a[N], const T b[N]) {
    int i = 0;
    for (i = 0; i < N; i = i + 1) { o[i] = a[i] + b[i]; }
}
int main() {
    float x[16]; float y[16]; float z[16];
    int i = 0;
    for (i = 0; i < 16; i = i + 1) { x[i] = 1.0f; y[i] = 2.0f; }
    la_add<float,16>(z, x, y);
    return (int)z[0];
}
"""


class TestContractsAreOptIn(Base):
    """The default path emits none, so gcc still builds."""

    def test_no_clauses_without_the_flag(self):
        out = cpprust.translate(_AUTO)
        self.assertNotIn("assert", out)


class ContractBase(Base):
    """Auto-contracts are opt-in, so these lower with the flag on.

    They are a ShivyCX extension and gcc cannot parse one, so inserting
    them unasked broke every gcc build of a file with a fixed-size array
    parameter -- code that never asked for contracts at all. Written
    contracts were always safe because writing one is already a statement
    that ShivyCX is the target; an *inferred* one is not.
    """

    def lower(self, src):
        return cpprust.translate(src, contracts=True)


class TestAutoContracts(ContractBase):
    """The size in the type becoming the proof in the compiler.

    `Mat<float,4,4>` already says the element count is 16, and 16 is a
    multiple of the four floats an SSE lane holds -- so making the author
    write `assert not len(d) % 4` would be asking them to restate the
    template argument. Ported from `tools/py2c.py::_auto_contracts`, which
    infers the same clauses on the rpython side from `x: "f32[256]"`.
    """

    def test_clauses_are_inferred_from_the_template_argument(self):
        out = self.lower(_AUTO)
        self.assertNotIn("assert", _AUTO)          # none in the source
        self.assertIn("assert not len(o) % 4", out)
        self.assertIn("assert len(a) >= 4", out)
        self.assertIn("assert not len(b) % 4", out)

    def test_a_bound_that_does_not_fill_a_lane_infers_nothing(self):
        """Six floats is neither a multiple of four nor big enough to
        promise anything. That is exactly the case the scalar remainder
        exists for, so there is nothing to say."""
        out = self.lower(_AUTO.replace("16", "6").replace("float,6", "float,6"))
        self.assertNotIn("assert", out)

    def test_a_prototype_gets_no_clauses(self):
        """Contracts belong on the definition. A declaration with no body
        must be left alone, or the C stops parsing."""
        out = self.lower("""
void kern(float a[16]);
void kern(float a[16]) { a[0] = 1.0f; }
""")
        self.assertIn("void kern(float a[16]);", out)
        self.assertEqual(out.count("assert not len(a) % 4"), 1)

    def test_a_written_contract_is_not_overridden(self):
        """The author said something more specific than this pass can
        infer, so it says nothing."""
        out = self.lower("""
void kern(float a[16])
assert len(a) >= 64
{ a[0] = 1.0f; }
""")
        self.assertIn("assert len(a) >= 64", out)
        self.assertNotIn("assert not len(a) % 4", out)


class TestAutoContractProof(ContractBase):
    """The inferred clauses proven at the far end, over a *stack* array.

    Two things had to give. The prover only resolved `malloc`, so a fixed
    size array -- which is what a fixed-size matrix is -- could never be
    proven; it now reads the bound off the `AddrOf` that decays the array,
    which is if anything the easier proof, since an array's size is part of
    its type rather than a literal to read back out of a call.

    And the synthesizer took "the last pointer argument is the output" as a
    convention. It held for every kernel written by hand and is not a
    property of C: `la_add(out, a, b)` compiled, vectorized, and computed
    `b = out + a`. The destination is now walked back from the store.
    """

    def _shivyc(self, src, args):
        d = tempfile.mkdtemp()
        c = os.path.join(d, "t.c")
        with open(c, "w") as f:
            f.write(self.lower(src))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        target = os.path.join(d, "t")
        rc = subprocess.call(
            ["python3", "-m", "shivyc.main", c] + args + ["-o", target],
            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(rc, 0)
        return target

    def test_a_stack_array_call_site_is_proven(self):
        s = self._shivyc(_AUTO, ["-S"])
        with open(s) as f:
            asm = f.read()
        body = asm.split("la_add_float_16:")[1].split("ret")[0]
        self.assertIn("addps", body)
        self.assertNotIn("addss", body)         # no scalar remainder

    def test_output_first_still_computes_the_right_thing(self):
        """The regression the convention would have caused. `la_add(o, a,
        b)` with o first: 1.0 + 2.0 = 3.0, and the wrong destination gave
        0."""
        exe = self._shivyc(_AUTO, [])
        self.assertEqual(subprocess.call([exe]), 3)


_STRUCT_FIELD = """
void la_add_f32_16(float o[16], const float a[16], const float b[16]) {
    int i = 0;
    for (i = 0; i < 16; i = i + 1) { o[i] = a[i] + b[i]; }
}
template<typename T, int N>
class Vec {
public:
    T d[N];
    Vec() { int i = 0; for (i = 0; i < N; i = i + 1) { d[i] = 0; } }
};
int main() {
    Vec<float,16> a; Vec<float,16> b; Vec<float,16> c;
    int i = 0;
    for (i = 0; i < 16; i = i + 1) { a.d[i] = 1.0f; b.d[i] = 2.0f; }
    la_add_f32_16(c.d, a.d, b.d);
    return (int)c.d[0];
}
"""


class TestStructFieldArrays(ContractBase):
    """A fixed-size array that is a *member* of a local struct.

    A fixed-size matrix is a struct with an inline array, so `a.d` is the
    shape every call in such a library has. The prover knew `malloc` and a
    bare array; a member reaches it as `AddrRel(&a, offset)`, which it read
    as an unknown pointer and refused. The member is now found by its byte
    offset in the struct's layout -- by offset rather than by name because
    the name is not on the command, and the offset is what the address
    actually is.
    """

    def _shivyc(self, src, args):
        d = tempfile.mkdtemp()
        c = os.path.join(d, "t.c")
        with open(c, "w") as f:
            f.write(self.lower(src))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        target = os.path.join(d, "t")
        rc = subprocess.call(
            ["python3", "-m", "shivyc.main", c] + args + ["-o", target],
            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(rc, 0)
        return target

    def test_a_member_array_call_site_is_proven(self):
        s = self._shivyc(_STRUCT_FIELD, ["-S"])
        with open(s) as f:
            body = f.read().split("la_add_f32_16:")[1].split("ret")[0]
        self.assertIn("addps", body)
        self.assertNotIn("addss", body)

    def test_it_computes_the_right_answer(self):
        self.assertEqual(subprocess.call([self._shivyc(_STRUCT_FIELD, [])]), 3)


class TestKernelDelegationLimit(Base):
    """The library wants a class method delegating to a raw-pointer kernel
    template, and that does not work yet.

    Function templates are monomorphised *before* classes are, so a
    `la_add<T,N>(..)` inside `Vec<T,N>::operator+` was instantiated while
    `T` and `N` were still literally those letters -- emitting
    `la_add_T_N(T o[N], ..)`, a function whose parameter type C has never
    heard of, with no diagnostic at all. Pinned as a refusal until the pass
    grows the second run that would let the call wait.
    """

    def test_kernel_template_called_from_a_class_template_is_refused(self):
        self.refuses("""
template<typename T, int N>
void la_add(T o[N], const T a[N], const T b[N]) {
    int i = 0;
    for (i = 0; i < N; i = i + 1) { o[i] = a[i] + b[i]; }
}
template<typename T, int N>
class Vec {
public:
    T d[N];
    Vec() { d[0] = 0; }
    Vec operator+(const Vec &o) { Vec r; la_add<T,N>(r.d, d, o.d); return r; }
};
Vec<float,16> v;
""", "whose parameters are not substituted yet")

    def test_a_concrete_kernel_still_works(self):
        """The workaround the diagnostic names: instantiate the kernel at
        file scope and call that."""
        out = self.lower("""
void la_add_f32_16(float o[16], const float a[16], const float b[16]) {
    int i = 0;
    for (i = 0; i < 16; i = i + 1) { o[i] = a[i] + b[i]; }
}
template<typename T, int N>
class Vec {
public:
    T d[N];
    Vec() { d[0] = 0; }
    Vec operator+(const Vec &o) {
        Vec r; la_add_f32_16(r.d, d, o.d); return r;
    }
};
Vec<float,16> v;
""")
        self.assertIn("la_add_f32_16(r.d, this->d, o->d);", out)


class TestContractsWithReferenceParams(Base):
    """A contract on a method that takes a reference parameter.

    Contracts sit between the parameter list and the body, which is the one
    place nothing else does -- so every pass that finds a function by
    looking for `)` immediately before `{` walked back into the tail of
    `assert not len(o) % 4` and gave up. The cost landed on the *body*: the
    enclosing function's parameters could not be found, so `o.d` was never
    rewritten to `o->d` and the C front end reported a member access on
    something that is not a structure.

    It went unnoticed because the first contract-bearing method took raw
    pointers. Every operator in a numeric library takes `const Vec &`, so
    contracts and reference parameters were mutually exclusive -- exactly
    the combination the library needs.
    """

    SRC = """
template<typename T, int N>
class Vec {
public:
    T d[N];
    Vec() { int i = 0; for (i = 0; i < N; i = i + 1) { d[i] = 0; } }
    Vec operator+(const Vec &o)
    assert not len(o) %% 4
    {
        Vec r; int i = 0;
        for (i = 0; i < N; i = i + 1) { r.d[i] = d[i] + o.d[i]; }
        return r;
    }
};
int main() {
    Vec<float,16> a; Vec<float,16> b;
    int i = 0;
    for (i = 0; i < 16; i = i + 1) { a.d[i] = 1.0f; b.d[i] = 2.0f; }
    Vec<float,16> c = a + b;
    return (int)c.d[0];
}
""" % ()

    def test_the_body_still_gets_its_reference_lowering(self):
        out = self.lower(self.SRC)
        self.assertIn("o->d[i]", out)
        self.assertNotIn("o.d[i]", out)
        self.assertIn("assert not len(o) % 4", out)

    def test_the_same_body_without_a_contract_is_unchanged(self):
        """The two spellings must agree: adding a contract may not change
        what the body lowers to."""
        with_c = self.lower(self.SRC)
        without = self.lower(self.SRC.replace(
            "    assert not len(o) %% 4\n" % (), "").replace(
            "    assert not len(o) % 4\n", ""))
        self.assertIn("o->d[i]", without)
        self.assertEqual(
            with_c.replace("\nassert not len(o) % 4\n", ""),
            without)


_CTOR_KERNEL = """
template<typename T, int N>
class Vec {
public:
    T d[N];
    Vec() { int i = 0; for (i = 0; i < N; i = i + 1) { d[i] = 0; } }
    Vec operator+(const Vec &o)
    assert not len(o) %% 4
    {
        Vec r; int i = 0;
        for (i = 0; i < N; i = i + 1) { r.d[i] = d[i] + o.d[i]; }
        return r;
    }
};
int main() {
    Vec<float,16> a; Vec<float,16> b;
    int i = 0;
    for (i = 0; i < 16; i = i + 1) { a.d[i] = 1.0f; b.d[i] = 2.0f; }
    Vec<float,16> c = a + b;
    return (int)c.d[0];
}
""" % ()


class TestConstructingKernel(Base):
    """An `operator+` returning by value *is* the kernel.

    This is the shape the library is built on, and it needed three things
    that were not there. A function returning a struct takes a hidden
    destination pointer (System V's sret), so the IL has one more argument
    than the source signature -- and that hidden argument is the output,
    which is why the operator looked like inputs with nowhere to write. The
    loop then writes a *local*, copied out on return; since the trip count
    equals the element count, every element is assigned before the copy, so
    writing the destination directly is the same program with the copy
    elided. And a constructing kernel has two stores, only one of which has
    the element type.

    What comes out is the whole operator as a fallback-free packed loop:
    the local, its zeroing constructor and the return copy are all gone.
    """

    def _shivyc(self, args):
        d = tempfile.mkdtemp()
        c = os.path.join(d, "t.c")
        with open(c, "w") as f:
            f.write(self.lower(_CTOR_KERNEL))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        target = os.path.join(d, "t")
        rc = subprocess.call(
            ["python3", "-m", "shivyc.main", c] + args + ["-o", target],
            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(rc, 0)
        return target

    def test_the_operator_is_synthesized_as_a_packed_loop(self):
        with open(self._shivyc(["-S"])) as f:
            body = f.read().split("Vec_float_16__binadd:")[1].split("\n\tret")[0]
        self.assertIn("addps", body)
        self.assertNotIn("addss", body)          # no scalar remainder
        # sret destination in rdi, receiver in rsi, operand in rdx.
        self.assertIn("[rdi + rax]", body)
        self.assertIn("[rsi + rax]", body)
        self.assertIn("[rdx + rax]", body)
        # The local, its constructor and the return copy are elided.
        self.assertNotIn("call", body)

    def test_it_computes_the_right_answer(self):
        self.assertEqual(subprocess.call([self._shivyc([])]), 3)


_SMALL_VALUE = """
template<typename T, int N>
class Vec {
public:
    T d[N];
    Vec() { int i = 0; for (i = 0; i < N; i = i + 1) { d[i] = 0; } }
    Vec operator+(const Vec &o) {
        Vec r; int i = 0;
        for (i = 0; i < N; i = i + 1) { r.d[i] = d[i] + o.d[i]; }
        return r;
    }
};
int main() {
    Vec<float,4> a; Vec<float,4> b;
    int i = 0;
    for (i = 0; i < 4; i = i + 1) { a.d[i] = 1.0f; b.d[i] = 2.0f; }
    Vec<float,4> c = a + b;
    return (int)(c.d[0] + c.d[1] + c.d[2] + c.d[3]);
}
"""


_MATVEC = """
template<typename T, int N>
class Vec {
public:
    T d[N];
    Vec() { int i = 0; for (i = 0; i < N; i = i + 1) { d[i] = 0; } }
};
template<typename T, int R, int C>
class Mat {
public:
    T d[R * C];
    Mat() { int i = 0; for (i = 0; i < R * C; i = i + 1) { d[i] = 0; } }
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
};
int main() {
    Mat<float,8,8> M; Vec<float,8> x;
    int i = 0;
    for (i = 0; i < 64; i = i + 1) { M.d[i] = 1.0f; }
    for (i = 0; i < 8; i = i + 1) { x.d[i] = 2.0f; }
    Vec<float,8> y = M * x;
    return (int)(y.d[0] + y.d[7]);
}
"""


class TestSmallValueReturn(ContractBase):
    """A value type of 16 bytes or less is returned in *registers*.

    System V hands back an aggregate that size in rax/rdx or xmm0/xmm1,
    with no hidden destination pointer at all -- and `Vec<float,4>` is
    exactly 16. The constructing-kernel work modelled every struct return
    as sret, so for this size it invented an argument that is not there and
    shifted each register by one. That assembled cleanly and jumped into
    whatever the caller had left in rdi.

    Segfaulting was luck. The same mistake on a kernel handed a writable
    address would have corrupted it silently, which is the failure this
    compiler exists to make impossible -- so the boundary gets its own
    test rather than riding on the larger sizes the other cases use.
    """

    #: A matvec whose *result* is exactly 16 bytes. This is the shape that
    #: actually broke: `Mat<float,4,4> * Vec<float,4>` returns a
    #: register-returned aggregate, and the synthesized kernel wrote
    #: through the hidden pointer it assumed was in rdi.
    SMALL_MATVEC = _MATVEC.replace("8,8", "4,4").replace(
        "float,8", "float,4").replace("i < 64", "i < 16").replace(
        "i < 8;", "i < 4;").replace("y.d[7]", "y.d[3]")

    def test_a_sixteen_byte_matvec_result_is_correct(self):
        """Without the size guard this segfaults: the kernel writes to
        whatever the caller happened to leave in rdi."""
        self.assertEqual(self._run(self.SMALL_MATVEC), 16)   # 4*2 + 4*2

    def _run(self, src):
        d = tempfile.mkdtemp()
        c = os.path.join(d, "t.c")
        with open(c, "w") as f:
            f.write(self.lower(src))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        exe = os.path.join(d, "t")
        rc = subprocess.call(["python3", "-m", "shivyc.main", c, "-o", exe],
                             cwd=root, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        self.assertEqual(rc, 0)
        return subprocess.call([exe])

    def test_a_sixteen_byte_result_is_correct(self):
        d = tempfile.mkdtemp()
        c = os.path.join(d, "t.c")
        with open(c, "w") as f:
            f.write(self.lower(_SMALL_VALUE))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        exe = os.path.join(d, "t")
        rc = subprocess.call(["python3", "-m", "shivyc.main", c, "-o", exe],
                             cwd=root, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        self.assertEqual(rc, 0)
        self.assertEqual(subprocess.call([exe]), 12)   # 4 lanes of 3.0

    def test_gcc_agrees(self):
        d = tempfile.mkdtemp()
        c = os.path.join(d, "t.c")
        with open(c, "w") as f:
            f.write(cpprust.translate(_SMALL_VALUE))
        exe = os.path.join(d, "t")
        subprocess.check_output(["gcc", "-std=c11", "-w", "-o", exe, c],
                                stderr=subprocess.STDOUT)
        self.assertEqual(subprocess.call([exe]), 12)


class TestMatVec(ContractBase):
    """Matrix times vector -- a reduction per row, not one pass.

    None of the element-wise kinds fit, so it is classified from the shape
    of the arguments rather than from the loop nest: three fixed-size value
    structs where one holds exactly `rows * cols` elements and the others
    hold `rows` and `cols` is a `Mat<T,R,C>::operator*(const Vec<T,C> &)`
    and essentially nothing else. The body is still checked, so a function
    that merely had those three sizes is not silently replaced.
    """

    def _shivyc(self, asm=False):
        d = tempfile.mkdtemp()
        c = os.path.join(d, "t.c")
        with open(c, "w") as f:
            f.write(self.lower(_MATVEC))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        target = os.path.join(d, "t.s" if asm else "t")
        out = subprocess.run(
            ["python3", "-m", "shivyc.main", c]
            + (["-S"] if asm else []) + ["-o", target],
            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(out.returncode, 0, out.stdout.decode()[-1500:])
        return target, out.stdout.decode()

    def test_it_is_proven_and_packed(self):
        s, log = self._shivyc(asm=True)
        self.assertIn("'Mat_float_8_8__binmul': contracts proven", log)
        with open(s) as f:
            body = f.read().split("\nMat_float_8_8__binmul:")[1] \
                           .split("\n\tret")[0]
        self.assertIn("mulps", body)
        self.assertIn("addps", body)
        # The row's lanes are summed horizontally and one scalar stored.
        self.assertIn("shufps", body)
        self.assertIn("movss", body)

    def test_it_computes_the_right_answer(self):
        exe, _ = self._shivyc()
        self.assertEqual(subprocess.call([exe]), 32)   # 8*2 + 8*2

    def test_gcc_agrees(self):
        d = tempfile.mkdtemp()
        c = os.path.join(d, "t.c")
        with open(c, "w") as f:
            f.write(cpprust.translate(_MATVEC))
        exe = os.path.join(d, "t")
        subprocess.check_output(["gcc", "-std=c11", "-w", "-o", exe, c],
                                stderr=subprocess.STDOUT)
        self.assertEqual(subprocess.call([exe]), 32)


if __name__ == "__main__":
    unittest.main()
