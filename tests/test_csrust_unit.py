#!/usr/bin/env python3
"""test_csrust_unit -- `ref`/`out`, and several C# files as one unit.

Futile's `ref`/`out` blockers were all calls into a method declared in another
file (`matrix.ApplyVector3FromLocalVector2(ref v, ..)`), which no one-file
translation can lower: the callee's signature is what says an `&` goes there.
So csrust takes several files (`csrust.translate_unit`, `csrust A.cs B.cs`).

    python3 -m unittest tests.test_csrust_unit
"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.cpprust as cpprust                              # noqa: E402
import tools.cs2cpp as cs2cpp                                # noqa: E402
import tools.cs2cpp_unit as cs2cpp_unit                      # noqa: E402
import tools.csrust as csrust                                # noqa: E402
from tests.test_csrust import run_c, run_shivyc, needs_cc    # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")


def src(*parts):
    p = os.path.join(FIX, *parts)
    with open(p) as f:
        return (p, f.read())


MAIN_46 = "int main(void) { return Entry_Go() == 46 ? 0 : 1; }"


class TestRefOutOneFile(unittest.TestCase):
    """What a method declared in the same file as its caller can take."""

    @classmethod
    def setUpClass(cls):
        p, text = src("refout", "SameFile.cs")
        cls.c = csrust.translate(text, path=p)

    @needs_cc
    def test_ref_local_field_out_pointer_and_int_all_write_the_callers(self):
        # ref to a local, ref to a field, out of an arena pointer (with a
        # member written through it), out of an int field: 4 + 42
        self.assertEqual(run_c(self.c, MAIN_46), 0)

    def test_runs_under_shivyc(self):
        self.assertEqual(run_shivyc(self.c, MAIN_46), 0)

    def test_out_of_an_arena_pointer_is_a_reference_to_the_pointer(self):
        # was `Layer* l`: a pointer by value, so the caller's stayed null
        self.assertIn("Layer* *l", self.c)
        self.assertIn("(*l)->n", self.c)


class TestUnit(unittest.TestCase):
    def sources(self, *names):
        return [src("unit", n) for n in names]

    def test_alone_the_caller_is_refused_at_its_ref(self):
        p, text = src("unit", "Caller.cs")
        with self.assertRaises(cs2cpp.CsError) as cm:
            csrust.translate(text, path="Caller.cs")
        self.assertIn("Caller.cs:21:", cm.exception.message)
        self.assertIn("`ref` parameters", cm.exception.message)

    @needs_cc
    def test_as_a_unit_cross_file_ref_and_out_run(self):
        c = csrust.translate_unit(self.sources("Callee.cs", "Caller.cs"))
        self.assertEqual(run_c(c, MAIN_46), 0)

    def test_as_a_unit_under_shivyc(self):
        c = csrust.translate_unit(self.sources("Callee.cs", "Caller.cs"))
        self.assertEqual(run_shivyc(c, MAIN_46), 0)

    @needs_cc
    def test_a_field_read_through_a_pointer_to_a_class_declared_below(self):
        """`Holder.Run` reads `layer.n`, and `Layer` is in the file listed
        second. cpprust used to emit each class's bodies right after its
        struct, so `this->layer->n` met an incomplete `Layer`. Now every struct
        definition comes before any body (`bodies_last`)."""
        c = csrust.translate_unit(self.sources("Caller.cs", "Callee.cs"))
        self.assertEqual(run_c(c, MAIN_46), 0)

    def test_the_same_under_shivyc(self):
        c = csrust.translate_unit(self.sources("Caller.cs", "Callee.cs"))
        self.assertEqual(run_shivyc(c, MAIN_46), 0)

    @needs_cc
    def test_a_base_class_is_put_above_its_subclass(self):
        a = ("A.cs", "public class Fancy : Base { public int Extra() { return Plain() + 100; } }\n"
                     "public static class Entry { public static int Go() { Fancy f = new Fancy(); return f.Extra(); } }\n")
        b = ("B.cs", "public class Base { public int Plain() { return 7; } }\n")
        c = csrust.translate_unit([a, b])         # the subclass's file first
        self.assertEqual(run_c(c, "int main(void) { return Entry_Go() == 107 ? 0 : 1; }"), 0)

    def test_an_error_names_the_file_and_line_it_was_written_on(self):
        a = ("A.cs", "public class A {\n  public int F() { return 1; }\n}\n")
        b = ("B.cs", "public class B {\n  public int G() { return 1; }\n"
                     "  public void H(object o) { }\n  public int K() { int x = 1 ?? 2; return x; }\n}\n")
        with self.assertRaises(cs2cpp.CsError) as cm:
            csrust.translate_unit([a, b])
        self.assertIn("B.cs:4:", cm.exception.message)
        self.assertNotIn("unit.cs", cm.exception.message)

    def test_a_duplicate_type_across_files_is_refused(self):
        with self.assertRaises(cs2cpp.CsError) as cm:
            csrust.translate_unit([("A.cs", "public class X { }\n"), ("B.cs", "public class X { }\n")])
        self.assertIn("`X` is declared twice", cm.exception.message)

    def test_a_cycle_of_base_classes_is_refused(self):
        with self.assertRaises(cs2cpp.CsError) as cm:
            csrust.translate_unit([("A.cs", "public class P : Q { }\n"), ("B.cs", "public class Q : P { }\n")])
        self.assertIn("cycle in base classes", cm.exception.message)


class TestBodiesLast(unittest.TestCase):
    """Two arena classes that read each other's fields, one derived from the
    other and virtual: no order of the two satisfies a body-after-struct
    layout. (Futile's FNode / FContainer.) Only bodies-last can compile it."""

    @classmethod
    def setUpClass(cls):
        p, text = src("unit", "Hierarchy.cs")
        cls.c = csrust.translate(text, path=p)

    MAIN = "int main(void) { return Entry_Go() == 14 ? 0 : 1; }"

    @needs_cc
    def test_a_cycle_with_a_base_and_virtuals_runs(self):
        # Node.Depth reads parent.lvl through a Cont*, Cont is declared
        # below it and derives from it: 9 + 5
        self.assertEqual(run_c(self.c, self.MAIN), 0)

    def test_a_cycle_with_a_base_and_virtuals_under_shivyc(self):
        self.assertEqual(run_shivyc(self.c, self.MAIN), 0)

    def test_every_struct_comes_before_every_body(self):
        lines = self.c.split("\n")
        last_struct = max(i for i, l in enumerate(lines) if l.startswith("struct ") and "{" in l)
        first_body = min(i for i, l in enumerate(lines)
                         if l.startswith("static ") and l.rstrip().endswith("}") and "(" in l
                         and ") {" in l)
        self.assertLess(last_struct, first_body)

    def test_cpprust_keeps_its_own_layout_unless_asked(self):
        cpp = "class A { public: B *b; int f() { return b->n; } }; class B { public: int n; };\n"

        def order(**kw):
            c = cpprust.translate(cpp, path="t.cpp", any_order=True, **kw)
            return c.index("struct B {"), c.index("static int A_f(A *this) {")
        s, b = order()
        self.assertGreater(s, b)            # default: A's body, then B's struct
        s, b = order(bodies_last=True)
        self.assertLess(s, b)               # asked: B's struct, then A's body


class TestArenaReceivers(unittest.TestCase):
    """Collections reached through an arena-typed local or static. By the time
    members are lowered an arena class is a pointer (`Box* b`), and neither the
    local-declaration finder nor the statics table allowed a `*` after the
    type -- so `b.items.Count` was not seen as a collection's and reached C as a
    member that does not exist. (No strings involved: `Dictionary<int, T>`.)"""

    SRC = ("using System.Collections.Generic;\n"
           "[MaxInstances(4)] public class Item { public int n; }\n"
           "[MaxInstances(2)] public class Box { public Dictionary<int, Item> byId = new Dictionary<int, Item>(); }\n"
           "public static class Reg { public static Box box; }\n"
           "public static class Entry { public static int Go() {\n"
           "  Box b = new Box(); Reg.box = b; Item i = new Item(); i.n = 7; b.byId[3] = i;\n"
           "  if (!b.byId.ContainsKey(3)) return 1;\n"
           "  if (!Reg.box.byId.ContainsKey(3)) return 2;\n"
           "  if (b.byId.Count != 1) return 3;\n"
           "  return Reg.box.byId[3].n; } }\n")

    @needs_cc
    def test_members_through_a_local_and_a_static_and_a_pointer_value(self):
        c = csrust.translate(self.SRC, path="t.cs")
        self.assertEqual(run_c(c, "int main(void) { return Entry_Go() == 7 ? 0 : 1; }"), 0)


class TestStaticClass(unittest.TestCase):
    @needs_cc
    def test_a_static_class_does_not_leave_its_modifier_on_the_first_member(self):
        # `static class R { static int n; }` gave `static static int R_n;`
        c = csrust.translate("public static class R { public static int n;\n"
                             "  public static int Go() { n = 3; return n; } }\n", path="t.cs")
        self.assertNotIn("static static", c)
        self.assertEqual(run_c(c, "int main(void) { return R_Go() == 3 ? 0 : 1; }"), 0)


class TestCommandLine(unittest.TestCase):
    """The command line and the API give the same C (it did not: `null` in a
    generated array was `NULL`, undeclared, only from the command line)."""

    @needs_cc
    def test_command_line_output_compiles_and_runs(self):
        tmp = tempfile.mkdtemp(prefix="csunit-")
        out = os.path.join(tmp, "o.c")
        fx = os.path.join(FIX, "unit")
        r = subprocess.run([sys.executable, os.path.join(HERE, "..", "tools", "csrust.py"),
                            os.path.join(fx, "Callee.cs"), os.path.join(fx, "Caller.cs"),
                            "-o", out], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(out) as f:
            self.assertEqual(run_c(f.read(), MAIN_46), 0)


class TestPlanning(unittest.TestCase):
    """cpprust's struct ordering, which the unit leans on."""

    @needs_cc
    def test_a_container_of_pointers_held_by_value_is_a_value(self):
        # `vector<Layer *>` was read as a pointer field because of the `*`
        # inside its template arguments, so it was never moved above `Pool`.
        text = ("public class Pool { public Layer[] pool = new Layer[2]; public int N() { return pool.Length; } }\n"
                "[MaxInstances(4)] public class Layer { public int n; }\n"
                "public static class Entry { public static int Go() { Pool p = new Pool(); return p.N(); } }\n")
        self.assertEqual(run_c(csrust.translate(text, path="t.cs"),
                               "int main(void) { return Entry_Go() == 2 ? 0 : 1; }"), 0)

    def test_by_value_names_looks_outside_the_template_arguments(self):
        self.assertEqual(cpprust._by_value_names("vector<Layer * >"), "vector<Layer *>")
        self.assertIsNone(cpprust._by_value_names("Layer *"))
        self.assertIsNone(cpprust._by_value_names("vector<int> *"))
        self.assertIsNone(cpprust._by_value_names("Layer &"))


if __name__ == "__main__":
    unittest.main()
