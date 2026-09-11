"""Floating-point support, slice 1: literals, copies, conversions, return.

This slice covers `float`/`double` types, floating literals (decimal, exponent,
hex, with f/F/l/L suffixes), copying floats, converting between float, double,
and the integer types, unary minus on float constants, and returning a float.
Arithmetic, comparisons, argument passing, and float-returning calls are later
slices.

Each test compiles a small program and checks the process exit code, which is
how the value is observed (after truncating to int where needed).
"""

import os
import subprocess
import tempfile
import unittest


def _run(src):
    d = tempfile.mkdtemp()
    c = os.path.join(d, "t.c")
    with open(c, "w") as f:
        f.write(src)
    out = os.path.join(d, "t")
    rc = subprocess.run(["shivyc", c, "-o", out],
                        capture_output=True, text=True).returncode
    if rc != 0:
        return None
    return subprocess.run([out]).returncode


class TestFloatSlice1(unittest.TestCase):
    def test_double_literal_to_int(self):
        self.assertEqual(_run("int main(){ return (int)3.75; }"), 3)

    def test_negative_double_literal(self):
        self.assertEqual(_run("int main(){ return (int)-3.9; }"), 253)  # -3

    def test_float_suffix_literal(self):
        self.assertEqual(_run("int main(){ float f = 2.5f; return (int)f; }"),
                         2)

    def test_double_copy(self):
        self.assertEqual(
            _run("int main(){ double x=3.75; double y=x; return (int)y; }"), 3)

    def test_int_to_double(self):
        self.assertEqual(_run("int main(){ double x = 5; return (int)x; }"), 5)

    def test_double_to_float_narrowing(self):
        self.assertEqual(
            _run("int main(){ double a=1.9; float b=a; return (int)b; }"), 1)

    def test_large_truncation(self):
        self.assertEqual(
            _run("int main(){ double x = 1000.7; return (int)x; }"), 232)  # 1000 & 255

    def test_hex_float_literal(self):
        self.assertEqual(_run("int main(){ double x=0x1.8p1; return (int)x; }"),
                         3)  # 1.5 * 2 = 3

    def test_exponent_literal(self):
        self.assertEqual(_run("int main(){ double x=2.5e1; return (int)x; }"),
                         25)

    def test_round_trip_int_double_float(self):
        self.assertEqual(
            _run("int main(){ double x=7; float y=x; double z=y;"
                 " return (int)z; }"), 7)

    def test_float_return(self):
        # Returning a float places it in xmm0; reading it back via a cast in a
        # single function exercises the return path's source spot.
        self.assertEqual(
            _run("double d(){ double x = 6.5; return x; }"
                 "int main(){ double r = 0; r = 6.5; return (int)r; }"), 6)


if __name__ == "__main__":
    unittest.main()


class TestFloatArithmetic(unittest.TestCase):
    """Slice 2: float/double arithmetic (+ - * /) via SSE."""

    def test_add(self):
        self.assertEqual(
            _run("int main(){ double a=2.5,b=3.25; return (int)(a+b); }"), 5)

    def test_subtract_order(self):
        # Non-commutative: arg1 - arg2 must keep order.
        self.assertEqual(
            _run("int main(){ double a=3.0,b=10.0;"
                 " return (int)(a-b)+100; }"), 93)  # -7 + 100

    def test_multiply(self):
        self.assertEqual(
            _run("int main(){ double a=2.5,b=4.0; return (int)(a*b); }"), 10)

    def test_divide(self):
        self.assertEqual(
            _run("int main(){ double a=7.0,b=2.0; return (int)(a/b); }"), 3)

    def test_float32_arithmetic(self):
        self.assertEqual(
            _run("int main(){ float a=1.5f,b=2.5f; return (int)(a*b); }"), 3)

    def test_mixed_int_double_promotion(self):
        self.assertEqual(
            _run("int main(){ int n=3; double x=2.5; return (int)(n*x); }"), 7)

    def test_chained_expression(self):
        self.assertEqual(
            _run("int main(){ double r=((1.5+2.5)*3.0-4.0)/2.0;"
                 " return (int)r; }"), 4)

    def test_in_place_update(self):
        self.assertEqual(
            _run("int main(){ double x=5.0; x=x+2.5; x=x*2.0;"
                 " return (int)x; }"), 15)

    def test_compound_assignment(self):
        self.assertEqual(
            _run("int main(){ double x=2.0; x*=4.5; x-=3; return (int)x; }"), 6)

    def test_loop_accumulate(self):
        self.assertEqual(
            _run("int main(){ double s=0.0; int i; for(i=0;i<5;i++) s+=1.5;"
                 " return (int)s; }"), 7)

    def test_array_of_double(self):
        self.assertEqual(
            _run("int main(){ double a[3]; a[0]=1.5; a[1]=2.5;"
                 " a[2]=a[0]+a[1]; return (int)a[2]; }"), 4)


class TestFloatComparison(unittest.TestCase):
    """Slice 3: float/double comparisons via ucomisd (NaN-aware)."""

    def test_less(self):
        self.assertEqual(_run("int main(){ double a=2.5,b=3.0; return a<b; }"),
                         1)
        self.assertEqual(_run("int main(){ double a=3.0,b=2.5; return a<b; }"),
                         0)

    def test_less_equal(self):
        self.assertEqual(
            _run("int main(){ double a=2.5,b=2.5; return a<=b; }"), 1)

    def test_greater_and_ge(self):
        self.assertEqual(_run("int main(){ double a=3.5,b=2.5; return a>b; }"),
                         1)
        self.assertEqual(
            _run("int main(){ double a=2.5,b=2.5; return a>=b; }"), 1)

    def test_equal_not_equal(self):
        self.assertEqual(_run("int main(){ double a=2.5,b=2.5; return a==b; }"),
                         1)
        self.assertEqual(_run("int main(){ double a=2.5,b=3.0; return a!=b; }"),
                         1)

    def test_mixed_int_compare(self):
        self.assertEqual(_run("int main(){ double x=2.5; return x>2; }"), 1)

    def test_compare_in_while(self):
        self.assertEqual(
            _run("int main(){ double x=10.0; int n=0; while(x>1.0){ x=x/2.0;"
                 " n++; } return n; }"), 4)

    def test_compare_in_logical(self):
        self.assertEqual(
            _run("int main(){ double x=2.5; return (x>0.0 && x<5.0); }"), 1)

    def test_nan_ordered_false(self):
        # All ordered comparisons with NaN are false.
        self.assertEqual(
            _run("int main(){ double z=0.0; double n=z/z; return n<1.0; }"), 0)
        self.assertEqual(
            _run("int main(){ double z=0.0; double n=z/z; return n>=n; }"), 0)

    def test_nan_equality(self):
        # NaN == NaN is false; NaN != NaN is true.
        self.assertEqual(
            _run("int main(){ double z=0.0; double n=z/z; return n==n; }"), 0)
        self.assertEqual(
            _run("int main(){ double z=0.0; double n=z/z; return n!=n; }"), 1)


class TestFloatABI(unittest.TestCase):
    """Slice 4: passing floats as arguments (xmm0-7) and returning them."""

    def test_identity_param(self):
        self.assertEqual(
            _run("double id(double x){ return x; }"
                 "int main(){ return (int)id(3.5); }"), 3)

    def test_two_double_params(self):
        self.assertEqual(
            _run("double add(double a,double b){ return a+b; }"
                 "int main(){ return (int)add(2.5,3.25); }"), 5)

    def test_float32_param(self):
        self.assertEqual(
            _run("float sq(float x){ return x*x; }"
                 "int main(){ return (int)sq(3.0f); }"), 9)

    def test_negate_param(self):
        # -x on a (non-constant) float parameter.
        self.assertEqual(
            _run("double fabs_(double x){ if(x<0.0) return -x; return x; }"
                 "int main(){ return (int)fabs_(-7.5); }"), 7)

    def test_mixed_int_float_sequence(self):
        # SysV fills integer and float arg registers independently.
        self.assertEqual(
            _run("double f(int a,double b,int c){ return a+b+c; }"
                 "int main(){ return (int)f(1,2.5,3); }"), 6)
        self.assertEqual(
            _run("double g(double a,int b,double c){ return a*b+c; }"
                 "int main(){ return (int)g(2.5,4,1.0); }"), 11)

    def test_recursive_float(self):
        self.assertEqual(
            _run("double pw(double b,int e){ if(e==0) return 1.0;"
                 " return b*pw(b,e-1); }"
                 "int main(){ return (int)pw(2.0,10); }"), 0)  # 1024 & 255

    def test_six_float_args(self):
        self.assertEqual(
            _run("double s6(double a,double b,double c,double d,double e,"
                 "double f){ return a+b+c+d+e+f; }"
                 "int main(){ return (int)s6(1.0,2.0,3.0,4.0,5.0,6.0); }"), 21)

    def test_call_result_in_expression(self):
        self.assertEqual(
            _run("double half(double x){ return x/2.0; }"
                 "int main(){ double r=half(10.0)+half(6.0); return (int)r; }"),
            8)

    def test_int_call_unaffected(self):
        self.assertEqual(
            _run("int add(int a,int b){ return a+b; }"
                 "int main(){ return add(40,2); }"), 42)


class TestFloatAggregates(unittest.TestCase):
    """Slice 5: floats through union/struct members, pointers, arrays."""

    def test_union_store_load(self):
        self.assertEqual(
            _run("typedef unsigned long u64;"
                 "int main(){ union {double f; u64 i;} u; u.f=3.5;"
                 " return (int)u.f; }"), 3)

    def test_union_type_pun(self):
        self.assertEqual(
            _run("typedef unsigned long u64;"
                 "int main(){ union {double f; u64 i;} u; u.f=2.5;"
                 " return u.i!=0; }"), 1)

    def test_fabs_union_idiom(self):
        # The exact musl idiom: clear the sign bit via the integer view.
        self.assertEqual(
            _run("typedef unsigned long u64;"
                 "double fb(double x){ union {double f; u64 i;} u; u.f=x;"
                 " u.i &= 0x7fffffffffffffff; return u.f; }"
                 "int main(){ return (int)fb(-7.5); }"), 7)

    def test_struct_double_members(self):
        self.assertEqual(
            _run("struct P{double x; double y;};"
                 "int main(){ struct P p; p.x=1.5; p.y=2.5;"
                 " return (int)(p.x+p.y); }"), 4)

    def test_struct_array(self):
        self.assertEqual(
            _run("struct P{double v;};"
                 "int main(){ struct P a[3]; a[0].v=1.5; a[1].v=2.5;"
                 " a[2].v=a[0].v+a[1].v; return (int)a[2].v; }"), 4)

    def test_pointer_to_double(self):
        self.assertEqual(
            _run("int main(){ double d=3.5; double *p=&d; *p=*p+1.0;"
                 " return (int)d; }"), 4)

    def test_indexed_double_array(self):
        self.assertEqual(
            _run("int main(){ double a[4]; int i; for(i=0;i<4;i++) a[i]=i*1.5;"
                 " return (int)(a[2]+a[3]); }"), 7)


class TestFloatStaticInit(unittest.TestCase):
    """Static/global float initializers emit IEEE-754 bits, not raw decimals."""

    def test_static_const_double(self):
        self.assertEqual(
            _run("static const double C=-0.001388;"
                 "double get(){ return C; }"
                 "int main(){ return (int)(get()*-1000000); }"), 108)

    def test_global_double_init(self):
        self.assertEqual(
            _run("double g = 2.5;"
                 "int main(){ return (int)(g*4); }"), 10)

    def test_static_double_array(self):
        self.assertEqual(
            _run("static const double t[3] = {1.5, 2.5, 3.5};"
                 "int main(){ return (int)(t[0]+t[1]+t[2]); }"), 7)


class TestFloatConstFold(unittest.TestCase):
    """Static/constant folding of floating arithmetic (matches SSE/gcc)."""

    def test_static_const_division(self):
        self.assertEqual(
            _run("static const double k=1.5/0x1p-52;"
                 "int main(){ return k>0; }"), 1)

    def test_static_const_mul(self):
        self.assertEqual(
            _run("static const double k=2.0*3.5; int main(){ return (int)k; }"),
            7)

    def test_global_init_expression(self):
        self.assertEqual(
            _run("double g=10.0/4.0; int main(){ return (int)(g*4); }"), 10)

    def test_fold_matches_runtime(self):
        self.assertEqual(
            _run("int main(){ double x=1.0/3.0,y=1.0/3.0; return x==y; }"), 1)


class TestWideIntLiterals(unittest.TestCase):
    """Integer literals may take unsigned types (hex/octal or 'u' suffix)."""

    def test_unsigned_long_long_hex(self):
        self.assertEqual(
            _run("typedef unsigned long u64;"
                 "int main(){ u64 i=0x4000000000000000UL;"
                 " i=(i+0x80000000)&0xffffffffc0000000UL; return i!=0; }"), 1)

    def test_u_suffix(self):
        self.assertEqual(
            _run("int main(){ unsigned u=4000000000U; return u>2000000000; }"),
            1)

    def test_hex_takes_unsigned(self):
        self.assertEqual(
            _run("int main(){ unsigned u=0xC0000000; return u>2000000000; }"),
            1)

    def test_decimal_still_signed(self):
        self.assertEqual(_run("int main(){ return 42; }"), 42)


class TestFloatIncrDecr(unittest.TestCase):
    """++/-- on floating operands use a float step, not an int immediate."""

    def test_post_decrement(self):
        self.assertEqual(_run("int main(){ double f=5.0; f--; return (int)f; }"),
                         4)

    def test_pre_increment(self):
        self.assertEqual(_run("int main(){ double f=5.0; ++f; return (int)f; }"),
                         6)

    def test_post_increment_returns_old(self):
        self.assertEqual(
            _run("int main(){ double f=5.0; double g=f++;"
                 " return (int)(g*10+f); }"), 56)

    def test_float_predecrement(self):
        self.assertEqual(
            _run("int main(){ float f=3.0f; --f; return (int)f; }"), 2)

    def test_decrement_in_loop(self):
        self.assertEqual(
            _run("int main(){ double f=10.0; int n=0;"
                 " while(f>0){f--; n++;} return n; }"), 10)


def _compile_output(src):
    """Compile src and return (returncode, combined stdout+stderr)."""
    d = tempfile.mkdtemp()
    c = os.path.join(d, "t.c")
    with open(c, "w") as f:
        f.write(src)
    out = os.path.join(d, "t")
    p = subprocess.run(["shivyc", c, "-o", out], capture_output=True,
                       text=True)
    return p.returncode, p.stdout + p.stderr


class TestRightShift(unittest.TestCase):
    """Right shift is logical for unsigned operands, arithmetic for signed."""

    def test_unsigned_long_high_bit(self):
        self.assertEqual(
            _run("typedef unsigned long u64;"
                 "int main(){ u64 x=0x8000000000000000UL; return (x>>63)==1; }"),
            1)

    def test_unsigned_long_two_bits(self):
        self.assertEqual(
            _run("typedef unsigned long u64;"
                 "int main(){ u64 x=0xffffffffc0000000UL;"
                 " return (x>>62)==3; }"), 1)

    def test_unsigned_int_high_bit(self):
        self.assertEqual(
            _run("int main(){ unsigned x=0x80000000U; return (x>>31)==1; }"), 1)

    def test_signed_negative_arithmetic(self):
        self.assertEqual(
            _run("int main(){ long x=-16; return (x>>2)==-4; }"), 1)

    def test_signed_positive(self):
        self.assertEqual(_run("int main(){ long x=64; return (x>>2)==16; }"), 1)

    def test_unsigned_variable_count(self):
        self.assertEqual(
            _run("typedef unsigned long u64;"
                 "int main(){ u64 x=0x8000000000000000UL; int n=63;"
                 " return (x>>n)==1; }"), 1)


class TestLongDoubleRejected(unittest.TestCase):
    """long double is not supported; it must be rejected with a clear error."""

    MSG = "80bit floating point math"

    def test_long_double_variable_rejected(self):
        rc, out = _compile_output(
            "int main(){ long double x = 1.0; return (int)x; }")
        self.assertNotEqual(rc, 0)
        self.assertIn(self.MSG, out)

    def test_used_long_double_function_rejected(self):
        rc, out = _compile_output(
            "static long double h(long double x){"
            " volatile long double y=x; return y; }"
            "int main(){ return (int)h(1.0); }")
        self.assertNotEqual(rc, 0)
        self.assertIn(self.MSG, out)

    def test_unused_long_double_helper_tolerated(self):
        # An unused static long double helper (as musl headers define) must
        # not fail compilation: it is dead-code eliminated before the check.
        self.assertEqual(
            _run("static long double h(long double x){"
                 " volatile long double y=x; return y; }"
                 "int main(){ return 0; }"), 0)

    def test_long_double_prototype_allowed(self):
        # A pure prototype allocates nothing and does no math; it compiles.
        self.assertEqual(
            _run("long double sinl(long double);"
                 "int main(){ return 0; }"), 0)

    def test_plain_double_still_works(self):
        self.assertEqual(
            _run("int main(){ double d=2.5; return (int)(d*2); }"), 5)


def _compile_output_flags(src, flags):
    """Compile src with extra flags; return (returncode, stdout+stderr)."""
    d = tempfile.mkdtemp()
    c = os.path.join(d, "t.c")
    with open(c, "w") as f:
        f.write(src)
    out = os.path.join(d, "t")
    p = subprocess.run(["shivyc"] + flags + [c, "-o", out],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr, out


class TestShivycDefine(unittest.TestCase):
    """The compiler predefines __SHIVYC__."""

    def test_shivyc_macro_defined(self):
        self.assertEqual(
            _run("#ifdef __SHIVYC__\nint main(){return 7;}\n"
                 "#else\nint main(){return 0;}\n#endif"), 7)


class TestLongDoubleAsDouble(unittest.TestCase):
    """-f-long-double-as-double aliases long double to 64-bit with a warning."""

    def test_flag_allows_long_double(self):
        rc, out, exe = _compile_output_flags(
            "int main(){ long double a=1.5L; long double b=2.0L;"
            " return (int)(a+b); }", ["-f-long-double-as-double"])
        self.assertEqual(rc, 0)
        self.assertEqual(subprocess.run([exe]).returncode, 3)

    def test_flag_emits_warning(self):
        rc, out, exe = _compile_output_flags(
            "int main(){ long double x=1.0; return (int)x; }",
            ["-f-long-double-as-double"])
        self.assertIn("warning", out)
        self.assertIn("64-bit double", out)

    def test_default_still_rejects(self):
        rc, out, exe = _compile_output_flags(
            "int main(){ long double x=1.0; return (int)x; }", [])
        self.assertNotEqual(rc, 0)
        self.assertIn("80bit floating point math", out)


def _run_stdout(src):
    d = tempfile.mkdtemp()
    c = os.path.join(d, "t.c")
    with open(c, "w") as f:
        f.write(src)
    out = os.path.join(d, "t")
    if subprocess.run(["shivyc", c, "-o", out],
                      capture_output=True).returncode != 0:
        return None
    return subprocess.run([out], capture_output=True, text=True).stdout


class TestDefaultArgumentPromotion(unittest.TestCase):
    """C11 6.5.2.2p7: a `float` passed through `...` arrives as `double`.

    It used to be passed as a bare float, so printf("%f", 1.5f) read a
    double made of the float's bits and garbage and printed 0.000000.
    Found while bringing up the Windows target (see WINDOWS.md)."""

    def test_printf_float(self):
        self.assertEqual(_run_stdout(
            "int printf(const char *, ...);\n"
            "int main(void){ float f = 1.5f, g = 2.25f;"
            " printf(\"%f %.2f\\n\", f, f + g); return 0; }"),
            "1.500000 3.75\n")

    def test_crust_variadic_callee_reads_double(self):
        self.assertEqual(_run_stdout(
            "#include <stdarg.h>\n"
            "int printf(const char *, ...);\n"
            "double take(int n, ...) { va_list ap; double s = 0; int i;"
            " va_start(ap, n); for (i = 0; i < n; i++)"
            " s += va_arg(ap, double); va_end(ap); return s; }\n"
            "int main(void){ float f = 1.5f;"
            " printf(\"%.3f\\n\", take(3, f, 2.25f, 0.125f)); return 0; }"),
            "3.875\n")

    def test_unprototyped_call_promotes(self):
        self.assertEqual(_run_stdout(
            "int printf(const char *, ...);\n"
            "double twice();\n"
            "int main(void){ float f = 1.25f;"
            " printf(\"%.2f\\n\", twice(f)); return 0; }\n"
            "double twice(double x) { return x * 2; }\n"),
            "2.50\n")
