#!/usr/bin/env python3
"""test_cpprust_except -- the checked error model: `except`, `raise`, `try`.

CPP_DIRECTION.md's item 1, and the sentence it exists for: **error handling
a standard that bans exceptions would accept**. An error is a value in a
one-word state slot; propagation is a flag check after every call to a
function declared `except`; a `raise` dispatches to the innermost handler
the same way a failed call does; and the error path is the *ordinary*
return path with a flag set -- which is why destructors run on it without
this pass knowing what a destructor is. No unwinder, no unwind tables, no
allocation, every control edge visible in the generated C.

Spelled `except`, not `catch`, on purpose: a reviewer working to JSF or
MISRA can see at a glance this is not the banned thing, and it is the same
mechanism the rpython half of a mixed unit runs on (minipy's
`exc_flag`/`exc_val`).

Three claims are pinned behaviorally -- lowered, compiled, run:

  * destructors fire on the error path (`TestDestructorsOnErrorPath`);
  * a raise goes to the *innermost* handler -- a raise inside a `try` is
    caught by that `try`, a re-raise in a handler goes one level out
    (`TestDispatch`), which the first implementation got wrong in both
    directions;
  * an unhandled error is a *compile-time* refusal, never a runtime
    terminate (`TestRefusals`).

    python3 tools/test_cpprust_except.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.cpprust as cpprust                       # noqa: E402


def _have_gcc():
    return shutil.which("gcc") is not None


class Base(unittest.TestCase):

    def lower(self, src, **kw):
        return cpprust.translate(src, path="t.cpp", **kw)

    def refuses(self, src, *needles):
        try:
            out = self.lower(src)
        except cpprust.CppError as e:
            msg = e.args[0] if e.args else str(e)
            for n in needles:
                self.assertIn(n, msg)
            return msg
        self.fail("expected a refusal, got:\n%s" % out[-600:])

    def run_c(self, src):
        d = tempfile.mkdtemp(prefix="cppexc-")
        try:
            c = os.path.join(d, "t.c")
            with open(c, "w") as f:
                f.write(self.lower(src))
            exe = os.path.join(d, "t")
            r = subprocess.run(["gcc", "-std=c11", "-w", "-o", exe, c],
                               capture_output=True, text=True)
            self.assertEqual(0, r.returncode, r.stderr[-800:])
            run = subprocess.run([exe], capture_output=True, text=True)
            self.assertEqual(0, run.returncode,
                             "crashed: %s" % run.stderr[-400:])
            return run.stdout
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ----------------------------------------------------------- lowering

class TestLowering(Base):

    SRC = """\
int parse(int x) except {
    if (x < 0) { raise 42; }
    return x * 2;
}
int use(void) {
    int r = 0;
    try { r = parse(3); } except (long e) { r = (int)e; }
    return r;
}
"""

    def test_prelude_emitted_when_used(self):
        out = self.lower(self.SRC)
        self.assertIn("_cpp_exc", out)
        self.assertIn("struct { int flag; long val; }", out)

    def test_prelude_absent_when_unused(self):
        self.assertNotIn("_cpp_exc",
                         self.lower("int f(int x) { return x + 1; }"))

    def test_except_keyword_leaves_the_signature(self):
        """The keyword is this pass's to consume; the C behind it is a
        plain function. (The word itself may appear in the emitted
        prelude's comment -- prose about the model -- so what is pinned
        is the syntactic position: no `) except` survives.)"""
        out = self.lower(self.SRC)
        self.assertNotIn(") except", out)
        self.assertIn("int parse(int x)", out)

    def test_raise_sets_flag_and_returns(self):
        out = self.lower(self.SRC)
        self.assertIn("_cpp_exc.flag = 1", out)
        self.assertIn("_cpp_exc.val = (long)(42)", out)

    def test_call_in_try_is_followed_by_a_check(self):
        out = self.lower(self.SRC)
        self.assertIn("if (_cpp_exc.flag) goto _cpp_h_", out)

    def test_handler_clears_the_flag(self):
        """A handled error is over. A flag that stayed set would turn the
        next fallible call's success into a phantom failure."""
        self.assertIn("_cpp_exc.flag = 0;", self.lower(self.SRC))


# --------------------------------------------------------- behavioral

@unittest.skipUnless(_have_gcc(), "gcc not available")
class TestBehavior(Base):

    def test_success_path_and_error_path(self):
        out = self.run_c("""#include <stdio.h>
int parse(int x) except {
    if (x < 0) { raise 42; }
    return x * 2;
}
int outer(int x) except {
    int v = parse(x);
    return v + 1;
}
int main(void) {
    int r;
    try {
        r = outer(5);
        printf("ok %d\\n", r);
        r = outer(-1);
        printf("never %d\\n", r);
    } except (long e) {
        printf("caught %ld\\n", e);
    }
    return 0;
}
""")
        self.assertEqual("ok 11\ncaught 42\n", out)

    def test_propagation_through_a_fallible_middle(self):
        """`b` and `c` have no `try`; being `except` themselves, the
        failed inner call becomes a poisoned return and the error arrives
        two frames up. `return a() + 1` is sound even though the value is
        garbage: the caller tests the flag before the value, which is the
        whole contract. (The fallible call bound to a local before the
        printf is not style -- an embedded `printf(c())` is refused, see
        TestRefusals.)"""
        out = self.run_c("""#include <stdio.h>
int a(void) except { raise 7; }
int b(void) except { return a() + 1; }
int c(void) except { return b() + 1; }
int main(void) {
    int v;
    try { v = c(); printf("v %d\\n", v); }
    except (long e) { printf("e %ld\\n", e); }
    return 0;
}
""")
        self.assertEqual("e 7\n", out)

    def test_fallible_call_embedded_in_another_call_is_refused(self):
        """The statement-level check runs after the statement -- too late
        once the enclosing call has consumed the poisoned value. The
        model refuses what it cannot check."""
        self.refuses("""#include <stdio.h>
int c(void) except { raise 7; }
int main(void) {
    try { printf("%d", c()); } except (long e) { }
    return 0;
}
""", "argument to another call", "bind it to a local")


@unittest.skipUnless(_have_gcc(), "gcc not available")
class TestDestructorsOnErrorPath(Base):
    """The flagship property, and the reason `raise` is lowered *before*
    the return pass rather than into a goto of its own: a raise becomes an
    ordinary return with a flag set, and ordinary returns already run every
    destructor (`{ int _cpp_ret0 = ..; Buf_drop(&b); return _cpp_ret0; }`).
    C++ exceptions need an unwinder for this sentence; the checked model
    gets it from the existing epilogue."""

    def test_dtor_runs_when_raising(self):
        out = self.run_c("""#include <stdio.h>
class Buf {
public:
    int n;
    Buf(int k) { n = k; printf("ctor %d\\n", n); }
    ~Buf() { printf("dtor %d\\n", n); }
};
int work(int x) except {
    Buf b(7);
    if (x < 0) { raise 9; }
    return x;
}
int main(void) {
    try { work(-1); printf("never\\n"); }
    except (long e) { printf("caught %ld\\n", e); }
    return 0;
}
""")
        self.assertEqual("ctor 7\ndtor 7\ncaught 9\n", out)

    def test_dtor_runs_when_propagating(self):
        """Same property one frame up: the middle function's local is
        destroyed as its poisoned return passes through."""
        out = self.run_c("""#include <stdio.h>
class Buf {
public:
    int n;
    Buf(int k) { n = k; }
    ~Buf() { printf("dtor %d\\n", n); }
};
int inner(void) except { raise 3; }
int mid(void) except {
    Buf b(11);
    int v = inner();
    return v;
}
int main(void) {
    try { mid(); } except (long e) { printf("caught %ld\\n", e); }
    return 0;
}
""")
        self.assertEqual("dtor 11\ncaught 3\n", out)


@unittest.skipUnless(_have_gcc(), "gcc not available")
class TestDispatch(Base):
    """A raise and a failed call take the same route: the innermost
    handler. Both directions of the first implementation's mistake are
    pinned -- it returned out of the function past an enclosing `try`, and
    it refused a re-raise whose outer handler was standing right there."""

    def test_raise_inside_try_is_caught_by_that_try(self):
        out = self.run_c("""#include <stdio.h>
int f(void) except {
    try { raise 5; } except (long e) { printf("here %ld\\n", e); }
    return 1;
}
int main(void) {
    int r = 0;
    try { r = f(); } except (long e) { printf("wrong level\\n"); }
    printf("r %d\\n", r);
    return 0;
}
""")
        self.assertEqual("here 5\nr 1\n", out)

    def test_reraise_goes_one_level_out(self):
        out = self.run_c("""#include <stdio.h>
void step(int x) except {
    if (x == 2) { raise 20; }
    if (x == 3) { raise 30; }
    printf("step %d\\n", x);
}
int main(void) {
    int i = 1;
    while (i < 5) {
        try {
            try { step(i); }
            except (long e) {
                printf("inner %ld\\n", e);
                if (e == 30) { raise; }
            }
        } except (long o) { printf("outer %ld\\n", o); }
        i = i + 1;
    }
    return 0;
}
""")
        self.assertEqual("step 1\ninner 20\ninner 30\nouter 30\nstep 4\n",
                         out)

    def test_bindingless_handler(self):
        out = self.run_c("""#include <stdio.h>
int f(void) except { raise 1; }
int main(void) {
    try { f(); } except { printf("handled\\n"); }
    return 0;
}
""")
        self.assertEqual("handled\n", out)


# ----------------------------------------------------------- refusals

class TestRefusals(Base):
    """An unhandled error is a compile error, not a terminate() -- which
    is the property `noexcept` never gave anyone, and the half of the
    model that makes the other half safe to rely on."""

    def test_unhandled_fallible_call(self):
        self.refuses("int parse(int x) except { raise 1; }\n"
                     "int main(void) { int v = parse(3); return v; }",
                     "nothing to handle a failure", "compile error",
                     "not a terminate()")

    def test_raise_with_nowhere_to_go(self):
        self.refuses("int f(int x) { raise 1; }",
                     "nowhere for the error to go")

    def test_class_local_inside_try(self):
        """The handler is reached by a jump that leaves the block early;
        a scope-end destructor would be skipped on exactly the path it
        matters most."""
        self.refuses(
            "class Buf { public: int n; Buf(int k) { n = k; } ~Buf() { } };\n"
            "int g(int x) except { raise 2; }\n"
            "int main(void) {\n"
            "    try { Buf b(1); g(3); } except (long e) "
            "{ return (int)e; }\n"
            "    return 0;\n}",
            "declared inside a `try` block", "Declare it before the `try`")

    def test_try_without_except(self):
        self.refuses("int main(void) { try { int x = 1; } return 0; }",
                     "`try` without an `except`")

    def test_non_word_payload_binding(self):
        self.refuses("int f(void) except { raise 1; }\n"
                     "int main(void) { try { f(); } "
                     "except (char *msg) { } return 0; }",
                     "one machine word")

    def test_catch_names_the_replacement(self):
        self.refuses("int main(void) { try { int x = 1; } "
                     "catch (int e) { } return 0; }",
                     "`try` without an `except`")

    def test_throw_names_the_replacement(self):
        self.refuses("int f(int x) { if (x) throw 1; return 0; }",
                     "checked model", "`raise`")


if __name__ == "__main__":
    unittest.main(verbosity=2)
