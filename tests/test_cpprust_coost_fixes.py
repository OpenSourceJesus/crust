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


class TestArrayElementReceivers(Base):
    """A method called on an element of an array of a class -- at file scope
    or in a block -- reached the C unlowered (`g[i].set(5)`), because only a
    declarator followed by `;`, `=`, `,` or `)` was recorded. A packed
    engine keeps a table per class exactly so (`static fastring T[N]`)."""

    def test_file_scope_and_local_arrays(self):
        self.assertRuns("""
class Box { public: int v; Box() { v = 0; } void set(int x) { v = x; }
            int get() { return v; } Box *self() { return this; } };
class Sel { public: int k; Sel() { k = 1; } int idx() { return k; } };
static Box g_boxes[4];
static int idx[2] = { 1, 3 };
static int pick(int k) { return idx[k]; }
int main(void) {
    Box local[3];
    Sel s;
    local[0].v = 0; local[1].v = 0; local[2].v = 0;
    g_boxes[0].set(5);
    g_boxes[idx[1]].set(7);
    g_boxes[pick(0)].set(9);
    local[s.idx()].set(4);
    local[2].v = 6;
    local[0].self()->set(8);
    int r = (g_boxes[0].get() == 5) + (g_boxes[3].get() == 7)
          + (g_boxes[1].get() == 9) + (local[1].get() == 4)
          + (local[2].get() == 6) + (local[0].get() == 8);
    return r == 6 ? 0 : 1;
}
""")


class TestLiteralSemicolons(Base):
    def test_initialiser_and_assignment_with_a_semicolon_in_a_literal(self):
        # The `T x = ..;` and `x = ..;` patterns ran to the first `;`,
        # inside the literal: `S a = mk("k=v;x=y");` was cut to `mk("k=v`.
        self.assertRuns("""
#include <stdlib.h>
#include <string.h>
class S { public: char *p; S() { p = 0; } S(const S &o) { p = strdup(o.p ? o.p : ""); }
          ~S() { free(p); }
          void operator=(const S &o) { free(p); p = strdup(o.p ? o.p : ""); } };
static S mk(const char *x) { S s; s.p = strdup(x); return s; }
int main(void) { S a = mk("k=v;x=y"); S b; b = mk("1;2");
    return (strcmp(a.p, "k=v;x=y") == 0 && strcmp(b.p, "1;2") == 0) ? 0 : 1; }
""")


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


class TestArrayOfObjects(Base):
    """`T a[N];` in a block passed through as plain C: no element was
    constructed or destroyed. coost's `tcp::Conn many[50];` then ran
    `connect()`, which closes the fd it holds first -- a garbage value, so
    it closed the test's own live sockets. `new T[n]` is refused for this
    very reason; the stack form slipped past."""

    ORDER = """
#include <string.h>
static char g_log[64];
static int g_n = 0;
static int g_id = 0;
class A {
public:
    int id;
    A() { id = g_id++; g_log[g_n++] = 'c'; }
    ~A() { g_log[g_n++] = (char)('0' + id); }
};
"""

    def test_constructed_in_order_destroyed_in_reverse(self):
        self.assertRuns(self.ORDER + """
static void f(void) { A a[3]; a[1].id = 1; }
int main(void) { f(); return strcmp(g_log, "ccc210") == 0 ? 0 : 1; }
""")

    def test_every_exit_path_destroys(self):
        self.assertRuns(self.ORDER + """
static int f(int k) {
    A a[2];
    for (int i = 0; i < 3; ++i) {
        A b[1];
        if (i == 0) continue;
        if (i == 1) break;
    }
    if (k) return 7;
    return 0;
}
int main(void) {
    // the array outside the loop, then the inner array made twice
    if (f(1) != 7) return 1;
    return strcmp(g_log, "ccc2c310") == 0 ? 0 : 2;
}
""")

    def test_array_beside_a_scalar(self):
        self.assertRuns(self.ORDER + """
static void f(void) { A x; A a[2]; }
int main(void) { f(); return strcmp(g_log, "ccc210") == 0 ? 0 : 1; }
""")

    def test_namespaced_class_with_a_resource(self):
        # the coost shape: close() on a garbage fd
        self.assertRuns("""
#include <stdlib.h>
static int g_live = 0;
namespace tcp {
class Conn {
public:
    int *p;
    Conn() { p = 0; }
    ~Conn() { this->close(); }
    void open() { this->close(); p = (int *)malloc(4); g_live++; }
    void close() { if (p) { free(p); p = 0; g_live--; } }
};
}
static int f(void) {
    tcp::Conn c[50];
    for (int i = 0; i < 50; ++i) c[i].open();
    return g_live;
}
int main(void) { return (f() == 50 && g_live == 0) ? 0 : 1; }
""")

    def test_plain_data_is_left_alone(self):
        out, _ = self.assertRuns("""
class P { public: int x; int y; };
int main(void) { P p[4]; p[0].x = 1; return p[0].x == 1 ? 0 : 1; }
""")
        self.assertNotIn("__cpp_ai", out)

    def test_file_scope_is_left_alone(self):
        # as for a scalar: no automatic construction or drop at file scope
        out = self.lower("""
class A { public: int v; A() { v = 1; } };
A g[2];
int main(void) { return 0; }
""")
        self.assertNotIn("__cpp_ai", out)

    def test_initializer_is_refused(self):
        self.assertRefused("""
class A { public: int v; A() { v = 1; } };
void f(void) { A a[2] = {}; }
""", "an array with an initializer")

    def test_multi_dimensional_is_refused(self):
        self.assertRefused("""
class A { public: int v; A() { v = 1; } };
void f(void) { A a[2][3]; }
""", "a multi-dimensional array")

    def test_several_declarators_are_refused(self):
        self.assertRefused("""
class A { public: int v; A() { v = 1; } };
void f(void) { A a[2], b[3]; }
""", "an array declared beside other names")

    def test_no_default_constructor_is_refused(self):
        self.assertRefused("""
class A { public: int v; A(int x) { v = x; } };
void f(void) { A a[2]; }
""", "has no constructor taking no arguments")


class TestArrayElementAssignment(Base):
    """`a[i] = x;` on an array of an owning class was a struct copy: two
    objects holding one buffer, freed twice at scope exit."""

    S = """
#include <stdlib.h>
#include <string.h>
static int g_live = 0;
class S {
public:
    char *p;
    S() { p = (char *)malloc(8); strcpy(p, "d"); g_live++; }
    S(const S &o) { p = (char *)malloc(8); strcpy(p, o.p); g_live++; }
    void operator=(const S &o) { strcpy(p, o.p); }
    ~S() { free(p); g_live--; }
};
"""

    def test_copy_assignment_calls_operator_eq(self):
        self.assertRuns(self.S + """
static int f(void) {
    S a[2]; S x; strcpy(x.p, "x");
    a[1] = x;
    return strcmp(a[1].p, "x") == 0 && a[1].p != x.p && g_live == 3;
}
int main(void) { return (f() && g_live == 0) ? 0 : 1; }
""")

    def test_index_is_evaluated_once(self):
        self.assertRuns(self.S + """
static int f(void) {
    S a[3]; S x; strcpy(x.p, "x"); int i = 0;
    a[i++] = x;
    return i == 1 && strcmp(a[0].p, "x") == 0 && strcmp(a[1].p, "d") == 0;
}
int main(void) { return (f() && g_live == 0) ? 0 : 1; }
""")

    def test_a_call_result_is_moved_in(self):
        self.assertRuns(self.S + """
static S make(void) { S r; strcpy(r.p, "m"); return r; }
static int f(void) {
    S a[2];
    a[0] = make();
    return strcmp(a[0].p, "m") == 0 && g_live == 2;
}
int main(void) { return (f() && g_live == 0) ? 0 : 1; }
""")

    def test_plain_data_stays_a_struct_copy(self):
        out, _ = self.assertRuns("""
class P { public: int x; int y; };
int main(void) { P a[2]; P q; q.x = 3; q.y = 4; a[1] = q;
                 return (a[1].x == 3 && a[1].y == 4) ? 0 : 1; }
""")
        self.assertNotIn("__cpp_el", out)

    def test_owning_class_without_operator_eq_is_refused(self):
        self.assertRefused("""
#include <stdlib.h>
class R { public: int *p; R() { p = (int *)malloc(4); } ~R() { free(p); } };
void f(void) { R a[2]; R x; a[0] = x; }
""", "owns a resource, so assigning to an element")


class TestArrayMembers(Base):
    """An array *member* of a class with a constructor or destructor was
    left as plain C: never constructed, never destroyed, never copied. Only
    `std::array` was refused (with advice to use `vector`); the general
    shape is now refused too, rather than lowered half-way."""

    def test_owning_element_is_refused(self):
        self.assertRefused("""
class S { public: int v; S() { v = 1; } ~S() { v = 0; } };
class H { public: S m[2]; };
int main(void) { H h; return 0; }
""", "member `m` is an array of S")

    def test_plain_data_member_array_is_fine(self):
        self.assertRuns("""
class P { public: int x; };
class H { public: P m[2]; int k; H() { k = 1; m[1].x = 5; } };
int main(void) { H h; return (h.k == 1 && h.m[1].x == 5) ? 0 : 1; }
""")


class TestGlobalScopeInLiterals(Base):
    """The global-scope `::` was stripped with a `re.sub` over the raw text,
    so literals lost it too: `"::1"` lowered to `"1"`, and coost's
    `srv.start("::", 80)` would have listened on IPv4 without a word. The
    `constexpr` strip beside it had the same flaw."""

    def test_literals_keep_their_colons(self):
        self.assertRuns("""
#include <string.h>
int main(void) {
    const char *a = "::1";
    const char *b = "[::1]:81";
    const char *k = "constexpr";
    char c = ':';
    return (strcmp(a, "::1") == 0 && strcmp(b, "[::1]:81") == 0
            && strcmp(k, "constexpr") == 0 && c == ':') ? 0 : 1;
}
""")

    def test_code_and_macro_bodies_are_still_rewritten(self):
        # the case the rewrite exists for: a class with its own `free`
        # reaching the C library's, in a body and in a macro
        self.assertRuns("""
#include <stdlib.h>
#define RELEASE(p) ::free(p)
static int g = 0;
class M { public: void free(void *p) { ::free(p); g++; } };
constexpr int K = 2;
int main(void) {
    M m; m.free(malloc(4));
    void *q = malloc(4); RELEASE(q);
    return (g == 1 && K == 2) ? 0 : 1;
}
""")


class TestConstructorReturnInABranch(Base):
    """`if (c) return T(..);` without braces. The return was rewritten into
    two statements, so the `if` guarded only the declaration: the `return`
    ran unconditionally, a following `else` lost its `if`, and the
    temporary joined the enclosing scope -- every later exit destroyed a
    temporary another branch had never constructed. coost's `http.h` had to
    brace these by hand."""

    F = """
#include <stdlib.h>
#include <string.h>
static int g_live = 0;
class F {
public:
    char *p;
    F() { p = 0; g_live++; }
    F(const char *s) { p = strdup(s); g_live++; }
    F(const F &o) { p = o.p ? strdup(o.p) : 0; g_live++; }
    ~F() { free(p); g_live--; }
};
"""

    def test_if_else_and_loop_bodies(self):
        out, _ = self.assertRuns(self.F + """
static F pick(int k) {
    if (k == 0) return F();
    else if (k == 1) return F("one");
    for (;;) return F("loop");
}
int main(void) {
    int ok = 1;
    { F a = pick(0); ok &= a.p == 0; }
    { F b = pick(1); ok &= b.p && strcmp(b.p, "one") == 0; }
    { F c = pick(2); ok &= c.p && strcmp(c.p, "loop") == 0; }
    return (ok && g_live == 0) ? 0 : 1;
}
""")
        # no branch destroys another branch's temporary
        self.assertNotIn("F_drop(&__cpp_ret0); return _cpp_ret1", out)

    def test_plain_data_class_in_a_method(self):
        # the coost shape: a method returning a class by value from a guard
        self.assertRuns("""
class P { public: int v; P() { v = 0; } P(int x) { v = x; } };
class Q {
public:
    int k;
    P get(int d) const { if (d == 0) return P(); return P(d + k); }
};
int main(void) { Q q; q.k = 1; return (q.get(0).v == 0 && q.get(2).v == 3) ? 0 : 1; }
""")


class TestStaticMembers(Base):
    """`static` members beyond `static const`. A bare call from a static
    method went through a `this` that does not exist, one from an instance
    method passed `this` as an extra argument, and a mutable static data
    member was left inside the struct or made a constant."""

    def test_calls_and_data(self):
        self.assertRuns("""
class Base { public: static int twice(int x) { return 2 * x; } };
class P : public Base {
public:
    static int calls;
    static int total;
    static int hist[4];
    static const int cap = 8;
    static int Fib(int n) { calls++; return n < 2 ? n : Fib(n - 1) + Fib(n - 2); }
    int k;
    int G() { hist[1] = 3; return Fib(5) + k + twice(1); }
};
int P::calls = 0;
int P::total;
int P::hist[4];
int main(void) {
    P p; p.k = 1;
    int a = P::Fib(10);
    int b = p.G();
    int c = p.Fib(3);
    P::total += 4;
    return (a == 55 && b == 8 && c == 2 && P::calls > 100 && P::hist[1] == 3
            && P::total == 4 && P::cap == 8) ? 0 : 1;
}
""")

    def test_static_with_initializer_is_assignable(self):
        self.assertRuns("""
class C { public: static int n = 5; static void bump() { n++; } };
int main(void) { C::bump(); C::bump(); return C::n == 7 ? 0 : 1; }
""")
