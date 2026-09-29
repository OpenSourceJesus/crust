"""Regressions found porting coost to the C++ subset.

Each test lowers a small program, compiles it with gcc and runs it: the
bugs here all produced C that either failed to compile or compiled and did
the wrong thing, so an output-shape assertion would not have caught most of
them. A program returns 0 when every check in it held.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.cpprust as cpprust                       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CPPRUST = os.path.join(ROOT, "tools", "cpprust.py")
GCC = shutil.which("gcc")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cpprust_coost_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lower(self, src):
        return cpprust.translate(src, path="t.cpp")

    def run_c(self, csrcs):
        """Compile the C sources together and run; returns (rc, compiler output)."""
        if GCC is None:
            self.skipTest("gcc not available")
        paths = []
        for k, c in enumerate(csrcs):
            p = os.path.join(self.tmp, "u%d.c" % k)
            with open(p, "w") as f:
                f.write(c)
            paths.append(p)
        exe = os.path.join(self.tmp, "a.out")
        cc = subprocess.run(
            [GCC, "-std=gnu11", "-Wall", "-Wno-unused-function",
             "-Werror=implicit-function-declaration",
             "-Werror=incompatible-pointer-types", "-Werror=return-type"]
            + paths + ["-o", exe],
            capture_output=True, text=True)
        if cc.returncode != 0:
            self.fail("C did not compile:\n%s\n%s" % (cc.stderr, "\n".join(csrcs)))
        return subprocess.run([exe]).returncode, cc.stderr

    def assertRuns(self, src):
        out = self.lower(src)
        rc, warnings = self.run_c([out])
        self.assertEqual(rc, 0, "program failed its checks:\n%s" % out)
        return out, warnings

    def assertRefused(self, src, fragment):
        with self.assertRaises(cpprust.CppError) as cm:
            self.lower(src)
        self.assertIn(fragment, cm.exception.message)


class TestDefaultArguments(Base):
    """A default on a prototype, or on a free function, passed straight
    through to C -- which rejects `int y = 2` in a parameter list -- and a
    free function with a body was split into two C functions of one name."""

    def test_free_function_prototype_and_definition(self):
        self.assertRuns("""
int twice(int x, int y = 2);
int twice(int x, int y) { return x * y; }
static int thrice(int x, int y = 3) { return x * y; }
int main(void) { return (twice(3) == 6 && twice(3, 4) == 12
                         && thrice(2) == 6) ? 0 : 1; }
""")

    def test_members_declared_in_class_and_defined_outside(self):
        self.assertRuns("""
namespace n { int add(int a, int b = 1, int c = 10); }
int n::add(int a, int b, int c) { return a + b + c; }
class S {
public:
    int v;
    S(int x = 4) { v = x; }
    int find(char c, int pos = 0) const;
    void bump(int by = 1);
    static int twice(int x, int k = 2) { return x * k; }
};
int S::find(char c, int pos) const { return c == 'a' ? pos : -1; }
void S::bump(int by) { v += by; }
int main(void) {
    S s; S t(7);
    s.bump(); t.bump(3);
    int r = (s.v == 5) + (t.v == 10) + (s.find('a') == 0) + (s.find('a', 3) == 3)
          + (S::twice(3) == 6) + (n::add(1) == 12) + (n::add(1, 2) == 13);
    return r == 7 ? 0 : 1;
}
""")

    def test_literals_in_defaults_are_not_split(self):
        self.assertRuns("""
#include <string.h>
static int cnt(const char *s = "a,=b", char c = '=') {
    int n = 0; for (; *s; s++) n += *s == c; return n; }
class T { public: int f(const char *s = "x,y", int k = 1) { return (int)strlen(s) + k; } };
int main(void) { T t; return (cnt() == 1 && cnt("==") == 2 && t.f() == 4
                              && t.f("ab") == 3) ? 0 : 1; }
""")

    def test_an_equals_literal_in_code_is_left_alone(self):
        # Once read as `if` with a defaulted parameter, which emitted
        # `if(s[2] == ')` -- an unterminated character literal.
        self.assertRuns("""
class K { public: int v; K() { v = 0; } };
static long dec(const unsigned char *s, char *x) {
    if (s[2] == '=') { if (s[3] == '=') return 1; return -1; }
    x[0] = '=';
    return 0;
}
int main(void) { K k; char b[2];
    return (dec((const unsigned char *)"ab==", b) == 1
            && dec((const unsigned char *)"abcd", b) == 0 && b[0] == '=') ? 0 : 1; }
""")

    def test_out_of_line_constructor_default_is_refused(self):
        self.assertRefused("""
class S { public: S(int x = 1); int v; };
S::S(int x) { v = x; }
""", "cannot delegate")


class TestReferenceAddress(Base):
    """`&o` on a class reference parameter took the address of the lowered
    pointer, so `if (&o != this)` compared a stack slot with `this`: a
    self-assignment guard that never fired."""

    def test_self_assignment_guard_fires(self):
        self.assertRuns("""
#include <stdlib.h>
class B {
public:
    int *p;
    B() { p = (int *)malloc(4); *p = 1; }
    B(const B &o) { p = (int *)malloc(4); *p = *o.p; }
    ~B() { free(p); }
    void operator=(const B &o) { if (&o != this) { *p = *o.p; } }
    bool same(const B &o) { return &o == this; }
    bool me(const B &o) { return this == &o; }
};
int main(void) { B a; B b; *b.p = 5; a = b; a = a;
    return (*a.p == 5 && a.same(a) && !a.same(b) && a.me(a) && !a.me(b)) ? 0 : 1; }
""")

    def test_address_comparison_is_not_an_operator_call(self):
        self.assertRuns("""
class P {
public:
    int v;
    P() { v = 0; }
    bool operator!=(const P &o) const { return v != o.v; }
    bool operator==(const P &o) const { return v == o.v; }
    bool is(const P &o) { return &o == this; }
    bool isnt(const P &o) { return &o != this; }
};
int main(void) { P a; P b; return (a.is(a) && a.isnt(b) && !(a != b) && a == b) ? 0 : 1; }
""")

    def test_free_function(self):
        self.assertRuns("""
class B { public: int v; B() { v = 1; } };
static const B *g_last = 0;
bool same(const B &a, const B &b) { g_last = &a; return &a == &b; }
int main(void) { B x; B y; return (same(x, x) && !same(x, y) && g_last == &x) ? 0 : 1; }
""")


class TestCopyOfThis(Base):
    def test_copy_constructs_from_star_this(self):
        # `S c(*this)` named nothing and fell through to the one-argument
        # constructor, handing it a struct where it wanted a `size_t`.
        self.assertRuns("""
#include <stdlib.h>
#include <string.h>
class S {
public:
    char *p; size_t n;
    S() { p = 0; n = 0; }
    S(size_t cap) { p = (char *)malloc(cap + 1); n = 0; }
    S(const S &o) { n = o.n; p = (char *)malloc(n + 1); memcpy(p, o.p, n); }
    ~S() { free(p); }
    S clone() const { S c(*this); return c; }
};
int main(void) { S a(4); a.p[0] = 'x'; a.n = 1; S b = a.clone();
    return (b.n == 1 && b.p[0] == 'x') ? 0 : 1; }
""")


class TestCopyOfPointerParameter(Base):
    def test_copy_constructs_from_star_param(self):
        # `S c(*p)` with `p` a pointer parameter picked the one-argument
        # constructor: only pointer *locals* were resolved through `*`.
        self.assertRuns("""
#include <stdlib.h>
class S {
public:
    int *p;
    S() { p = (int *)malloc(4); *p = 0; }
    S(int v) { p = (int *)malloc(4); *p = v + 100; }
    S(const S &o) { p = (int *)malloc(4); *p = *o.p; }
    ~S() { free(p); }
};
static int dup(const S *q) { S c(*q); return *c.p; }
int main(void) { S a(1); *a.p = 7; return dup(&a) == 7 ? 0 : 1; }
""")


class TestStaticConstQualified(Base):
    def test_qualified_use_outside_the_class(self):
        self.assertRuns("""
#include <stddef.h>
class F { public: static const size_t npos = (size_t)-1; size_t find() { return npos; } };
namespace ns { class G { public: static const int k = 7; }; }
int main(void) { F f; return (f.find() == F::npos && ns::G::k == 7) ? 0 : 1; }
""")


class TestNamespaces(Base):
    def test_nested_and_reopened(self):
        # Flattened twice: `co_now_co_now_ms`.
        self.assertRuns("""
namespace co { namespace now { int ms(); } namespace sleep { void ms(int n); } }
namespace co { namespace now { int ms() { return 5; } }
               namespace sleep { void ms(int n) { (void)n; } } }
int main(void) { co::sleep::ms(1); return co::now::ms() == 5 ? 0 : 1; }
""")

    def test_libc_struct_used_in_a_namespace(self):
        # `struct timespec t;` was read as the namespace declaring
        # `timespec`, and became `struct clk_timespec`.
        self.assertRuns("""
#include <time.h>
namespace clk {
struct Pt { int x; };
long now_s() { struct timespec t; clock_gettime(CLOCK_REALTIME, &t); return (long)t.tv_sec; }
struct Pt make(int x) { struct Pt p; p.x = x; return p; }
}
int main(void) { clk::Pt p = clk::make(3); return (p.x == 3 && clk::now_s() > 0) ? 0 : 1; }
""")


class TestCLinkage(Base):
    def test_inline_free_functions_link(self):
        # C99 `inline` provides no external definition, so every call
        # gcc did not inline was an undefined reference.
        out, _w = self.assertRuns("""
static inline int a1(int x) { return x + 1; }
inline static int a2(int x) { return x + 2; }
inline int a3(int x);
inline int a3(int x) { return x + 3; }
namespace m { inline int a4(int x) { return x + 4; } }
const char *s = "inline text";
int main(void) { return (a1(0) + a2(0) + a3(0) + m::a4(0) == 10 && s[0] == 'i') ? 0 : 1; }
""")
        self.assertIn('"inline text"', out)

    def test_pragma_once_is_dropped(self):
        out, warnings = self.assertRuns("#pragma once\nint main(void) { return 0; }\n")
        self.assertNotIn("pragma once", out)


class TestReturnPaths(Base):
    def test_no_dead_drops_after_a_final_return(self):
        # The scope-exit drops after the return block made gcc report
        # `control reaches end of non-void function` -- under
        # -fsanitize=undefined, at least, which is why the shape is checked
        # as well as the program run.
        out, _w = self.assertRuns("""
#include <stdlib.h>
class S { public: int *p; S() { p = (int *)malloc(4); *p = 7; }
          S(const S &o) { p = (int *)malloc(4); *p = *o.p; } ~S() { free(p); } };
static int f(void) { S s; return *s.p; }
static S g(void) { S c; S d(c); return d; }
int main(void) { S e = g(); return (f() == 7 && *e.p == 7) ? 0 : 1; }
""")
        import re as _re
        body = out[out.index("S g(void) {"):]
        body = body[:body.index("\nint main")]
        self.assertTrue(_re.search(r"return _cpp_ret\d+; \}\s*\}\s*$", body),
                        "dead drops after the return:\n" + body)

    def test_a_conditional_return_still_drops_on_fall_through(self):
        self.assertRuns("""
#include <stdlib.h>
static int g_live = 0;
class R { public: int *p; R() { p = (int *)malloc(4); g_live++; }
          R(const R &o) { p = (int *)malloc(4); g_live++; } ~R() { free(p); g_live--; } };
static void f1(int c) { R a; if (c) return; }
static void f2(int c) { R a; if (c) { R b; return; } }
static int f3(int c) { R a; if (c) { R b; return 2; } else return 3; }
static void f4(int c) { R a; switch (c) { case 1: { R b; return; } default: break; } }
static int f5(int c) { R a; while (c) { R b; return 5; } return 6; }
int main(void) { f1(0); f1(1); f2(0); f2(1); f3(0); f3(1); f4(0); f4(1); f5(0); f5(1);
                 return g_live == 0 ? 0 : 1; }
""")


class TestMoveAssignFromCall(Base):
    def test_rvalue_uses_move_assignment(self):
        self.assertRuns("""
#include <stdlib.h>
class J {
public:
    int *p;
    J() { p = 0; }
    J(J &&o) { p = o.p; o.p = 0; }
    ~J() { free(p); }
    void operator=(J &&o) { if (o.p != p) { free(p); p = o.p; o.p = 0; } }
};
J make(int v) { J r; r.p = (int *)malloc(sizeof(int)); *r.p = v; return r; }
int main(void) { J m; m = make(3); return *m.p == 3 ? 0 : 1; }
""")

    def test_lvalue_is_still_refused(self):
        # C++ rejects this too: only the move assignment exists.
        self.assertRefused("""
#include <stdlib.h>
class J { public: int *p; J() { p = 0; } J(J &&o) { p = o.p; o.p = 0; } ~J() { free(p); }
  void operator=(J &&o) { free(p); p = o.p; o.p = 0; } };
int main(void) { J a; J b; a = b; return 0; }
""", "has a destructor")


class TestFreeFunctionChains(Base):
    def test_plain_class_result_is_a_receiver(self):
        self.assertRuns("""
class V { public: int x; V() { x = 2; } int get() { return x; }
          V dbl() { V r; r.x = x * 2; return r; } };
V mk(void) { V v; return v; }
static V *g(void) { static V v; return &v; }
int main(void) { return (mk().get() == 2 && mk().dbl().get() == 4 && g()->get() == 2) ? 0 : 1; }
""")

    def test_owning_result_is_refused_not_passed_through(self):
        # Was left as `mk().get()` for the C front end to reject.
        self.assertRefused("""
#include <stdlib.h>
class S { public: int *p; S() { p = (int *)malloc(4); } ~S() { free(p); } int get() { return 1; } };
S mk(void) { S s; return s; }
int main(void) { return mk().get(); }
""", "owns a resource")


class TestSeparateTranslationUnits(Base):
    """A class declared in a header and defined in one `.cpp`, used from
    another. The defining unit emitted its out-of-line members `static`,
    and the using unit neither rewrote calls to members it only saw
    declared nor gave a declared `static` member the right prototype."""

    HEADER = """
#include <stdlib.h>
#include <string.h>
class str {
  public:
    str() { _p = 0; _n = 0; }
    str(const char *s) { _n = strlen(s); _p = (char *)malloc(_n + 1); memcpy(_p, s, _n + 1); }
    str(int n, char c);
    ~str() { free(_p); }
    int find(char c, size_t pos) const;
    static int count(const char *s, char c);
  private:
    char *_p; size_t _n;
};
"""
    DEFS = """#include "a.h"
str::str(int n, char c) { _n = n; _p = (char *)malloc(n + 1); memset(_p, c, n); _p[n] = 0; }
int str::find(char c, size_t pos) const {
    for (size_t i = pos; i < _n; i++) if (_p[i] == c) return (int)i;
    return -1;
}
int str::count(const char *s, char c) { int n = 0; for (; *s; s++) n += *s == c; return n; }
"""
    USE = """#include "a.h"
int main(void) {
    str x("hello");
    str y(3, 'z');
    return (x.find('l', 0) == 2 && y.find('z', 1) == 1 && str::count("lol", 'l') == 2) ? 0 : 1;
}
"""

    def test_two_units_link(self):
        for name, text in (("a.h", self.HEADER), ("a.cpp", self.DEFS),
                           ("b.cpp", self.USE)):
            with open(os.path.join(self.tmp, name), "w") as f:
                f.write(text)
        outs = []
        for name in ("a.cpp", "b.cpp"):
            c = os.path.join(self.tmp, name[:-4] + ".out.c")
            r = subprocess.run([sys.executable, CPPRUST,
                                os.path.join(self.tmp, name), "-o", c,
                                "--no-clang"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(c) as f:
                outs.append(f.read())
        self.assertIn("\nint str_find(str *this, char c, size_t pos) {", outs[0])
        self.assertIn("\nint str_count(const char *s, char c);", outs[1].replace("char* s", "char *s"))
        rc, _w = self.run_c(outs)
        self.assertEqual(rc, 0)

    def test_implicit_members_after_an_out_of_line_one_stay_static(self):
        # `Holder` gets an implicit copy constructor and assignment from its
        # `Buf` member. They are emitted after the member loop, and used to
        # inherit the out-of-line flag of the last member -- so both units
        # defined `Holder_copy` externally and the link failed.
        hdr = """
#include <stdlib.h>
class Buf {
  public:
    int *p;
    Buf() { p = (int *)malloc(4); *p = 0; }
    Buf(const Buf &o) { p = (int *)malloc(4); *p = *o.p; }
    ~Buf() { free(p); }
    void operator=(const Buf &o) { *p = *o.p; }
};
class Holder {
  public:
    Buf b;
    int get();
};
"""
        a = """#include "h.h"
int Holder::get() { return *b.p; }
int copy_a(Holder *h) { Holder c(*h); return c.get(); }
"""
        b = """#include "h.h"
int copy_a(Holder *h);
int main(void) { Holder h; *h.b.p = 4; Holder k(h); k = h;
                 return (copy_a(&h) == 4 && k.get() == 4) ? 0 : 1; }
"""
        for name, text in (("h.h", hdr), ("a.cpp", a), ("b.cpp", b)):
            with open(os.path.join(self.tmp, name), "w") as f:
                f.write(text)
        outs = []
        for name in ("a.cpp", "b.cpp"):
            c = os.path.join(self.tmp, name[:-4] + ".out.c")
            r = subprocess.run([sys.executable, CPPRUST,
                                os.path.join(self.tmp, name), "-o", c,
                                "--no-clang"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(c) as f:
                outs.append(f.read())
        for out in outs:
            self.assertIn("static void Holder_copy(", out)
        rc, _w = self.run_c(outs)
        self.assertEqual(rc, 0)

    def test_class_bodies_stay_static(self):
        # A member defined in the class is implicitly inline: every unit
        # that includes the header gets its own copy.
        out = self.lower(self.HEADER + "int f(void) { str s; return 0; }\n")
        self.assertIn("static void str_new(str *this)", out)


if __name__ == "__main__":
    unittest.main()
