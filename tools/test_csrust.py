#!/usr/bin/env python3
"""test_csrust — C# subset skeleton: class, methods, refusals, sugar.

Mirrors tools/test_cpprust_except.py: lower via cs2cpp+cpprust, compile, run.

    python3 tools/test_csrust.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.cpprust as cpprust  # noqa: E402
import tools.cs2cpp as cs2cpp  # noqa: E402
import tools.csrust as csrust  # noqa: E402


def _have_gcc():
    return shutil.which("gcc") is not None


_COUNTER = """
class Counter {
    int n;
    public Counter() { n = 0; }
    public void bump(int by) { n = n + by; }
    public void twice(int by) { bump(by); bump(by); }
    public int get() { return n; }
}
"""


class Base(unittest.TestCase):

    def lower(self, src, **kw):
        return csrust.translate(src, path="t.cs", **kw)

    def normalize(self, src):
        return cs2cpp.normalize(src, path="t.cs")

    def refuses(self, src, *needles):
        try:
            out = self.lower(src)
        except (cs2cpp.CsError, cpprust.CppError) as e:
            msg = getattr(e, "message", None) or (e.args[0] if e.args else str(e))
            for n in needles:
                self.assertIn(n, msg)
            return msg
        self.fail("expected refusal, got:\n%s" % out[-600:])

    def run_c(self, src):
        d = tempfile.mkdtemp(prefix="csrust-")
        try:
            c = os.path.join(d, "t.c")
            with open(c, "w") as f:
                f.write(self.lower(src))
            exe = os.path.join(d, "t")
            r = subprocess.run(
                ["gcc", "-std=c11", "-w", "-o", exe, c],
                capture_output=True, text=True)
            self.assertEqual(0, r.returncode, r.stderr[-800:])
            run = subprocess.run([exe], capture_output=True, text=True)
            self.assertEqual(0, run.returncode,
                             "crashed: %s" % run.stderr[-400:])
            return run.stdout
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestCounter(Base):

    def test_normalize_adds_semicolon_and_sections(self):
        out = self.normalize(_COUNTER)
        self.assertIn("public:", out)
        self.assertTrue(out.rstrip().endswith(";") or "};" in out)
        self.assertIn("~Counter()", out)

    def test_method_calls_lower(self):
        out = self.lower(_COUNTER + """
int f() {
    Counter a = new Counter();
    a.bump(2);
    return a.get();
}
""")
        self.assertIn("Counter_bump", out)
        self.assertIn("Counter_get", out)

    @unittest.skipUnless(_have_gcc(), "gcc required")
    def test_runs(self):
        src = _COUNTER + """
int main(void) {
    Counter a = new Counter();
    a.twice(3);
    return a.get() == 6 ? 0 : 1;
}
"""
        self.run_c(src)


class TestRefusals(Base):

    def test_async(self):
        self.refuses("async int f() { return 1; }", "async/await", "not in the C# subset")

    def test_linq_where(self):
        self.refuses(
            "int f(List<int> xs) { return xs.Where(x => x > 0); }",
            "LINQ", "Where")

    def test_yield(self):
        self.refuses(
            "int f() { yield return 1; }",
            "yield return")

    def test_multidim(self):
        self.refuses("void f() { int[,] a; }", "multidimensional")


class TestSugar(Base):

    def test_var_foreach(self):
        cpp = self.normalize("""
int sum(std::vector<int> xs) {
    var total = 0;
    foreach (var x in xs) { total = total + x; }
    return total;
}
""")
        self.assertIn("auto total", cpp)
        self.assertIn("for (auto x : xs)", cpp)

    def test_auto_property(self):
        cpp = self.normalize("""
class Box {
    public int Count { get; set; }
}
""")
        self.assertIn("get_Count", cpp)
        self.assertIn("set_Count", cpp)


class TestInterface(Base):

    def test_interface_pure(self):
        cpp = self.normalize("""
interface IFoo {
    int get();
}
class Foo : IFoo {
    public int get() { return 1; }
}
""")
        self.assertIn("virtual int get()", cpp)
        self.assertIn("= 0", cpp)
        self.assertIn("public IFoo", cpp)


class TestSharedRuns(Base):

    @unittest.skipUnless(_have_gcc(), "gcc required")
    def test_shared_alias_runs(self):
        src = """
[Shared] class Node {
    public int v;
    public Node() { v = 0; }
    public void set(int x) { v = x; }
    public int get() { return v; }
}
int main(void) {
    Node a = new Node();
    Node b = a;
    b.set(7);
    return a.get() == 7 ? 0 : 1;
}
"""
        self.run_c(src)


class TestVirtualRuns(Base):

    @unittest.skipUnless(_have_gcc(), "gcc required")
    def test_override_dispatch(self):
        src = """
class Base {
    public virtual int f() { return 1; }
}
class Child : Base {
    public override int f() { return 2; }
}
int main(void) {
    Child c = new Child();
    return c.f() == 2 ? 0 : 1;
}
"""
        self.run_c(src)


class TestExcept(Base):

    def test_throw_becomes_raise(self):
        out = self.lower("""
int parse(int x) {
    if (x < 0) { throw 42; }
    return x * 2;
}
""")
        self.assertIn("_cpp_exc", out)
        self.assertIn("except", self.normalize("""
int parse(int x) {
    if (x < 0) { throw 42; }
    return x * 2;
}
"""))


class TestCli(Base):

    def test_one_line_class_strips_public(self):
        out = self.lower(
            "class A { public int x; public A(){ x = 1; } "
            "public int get(){ return x; } }\n"
            "int f(){ A a = new A(); return a.get(); }\n")
        self.assertNotIn("public ", out)
        self.assertIn("A_get", out)


class TestMoreRefusals(Base):

    def test_extension_method(self):
        self.refuses(
            "static int Len(this string s) { return 0; }",
            "extension method")

    def test_static_class(self):
        self.refuses("static class Util { }", "static class")


class TestPreprocInclude(unittest.TestCase):
    """`#include \"x.cs\"` goes through csrust in the preprocessor."""

    @unittest.skipUnless(_have_gcc(), "gcc required")
    def test_c_includes_cs(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        d = tempfile.mkdtemp(prefix="csinc-")
        try:
            with open(os.path.join(d, "counter.cs"), "w") as f:
                f.write(_COUNTER + """
int entry() {
    Counter a = new Counter();
    a.twice(3);
    return a.get();
}
""")
            with open(os.path.join(d, "prog.c"), "w") as f:
                f.write('#include "counter.cs"\n'
                        "int main(void) { return entry() == 6 ? 0 : 1; }\n")
            out = os.path.join(d, "prog")
            r = subprocess.run(
                [sys.executable, "-m", "shivyc.main",
                 os.path.join(d, "prog.c"), "-o", out],
                cwd=root, capture_output=True, text=True)
            self.assertEqual(
                0, r.returncode, r.stderr[-1000:] or r.stdout[-1000:])
            run = subprocess.run([out], capture_output=True, text=True)
            self.assertEqual(0, run.returncode, run.stderr)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
