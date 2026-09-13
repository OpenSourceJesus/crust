#!/usr/bin/env python3
"""test_csrust -- the C# subset: what it lowers, and what it refuses.

CSHARP.md's milestones 1-3. Three claims are pinned behaviourally --
lowered, compiled with a C compiler, and *run*, because a translation that
merely produces C proves nothing about what the C does:

  * a class lowers to `Class_method(Class *this, ..)`, the same symbol shape
    cpprust gives a C++ class and shivyc gives a Rust `impl`, and the
    methods do what the C# said (`TestClassLowering`);
  * an `interface` is a pure-abstract base, so a call through one dispatches
    to the implementation (`TestInterfaces`);
  * everything outside the subset is refused *in C# terms, at a C# line*,
    before the C++ half ever runs (`TestRefusals`) -- which is the whole
    argument for checking before rewriting rather than after.

`TestSemantics` is the load-bearing one and is deliberately first. It pins
CSHARP.md §1: what a C# `class` is, given that it is a garbage-collected
reference type upstream and there is no garbage collector here. The answer
this suite pins is **single ownership**. If that decision is revisited, this
class is the thing to edit, and the rest of the suite should keep passing.

    python3 tools/test_csrust.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.cs2cpp as cs2cpp                                # noqa: E402
import tools.csrust as csrust                                # noqa: E402


def lower(src):
    """C# source in, C out."""
    return csrust.translate(src, path="test.cs")


def refusal(src):
    """The diagnostic a source outside the subset produces."""
    try:
        lower(src)
    except cs2cpp.CsError as e:
        return e.message
    except Exception as e:                                   # pragma: no cover
        raise AssertionError(
            "expected a C# refusal, got %s: %s" % (type(e).__name__, e))
    raise AssertionError("expected a refusal, got a translation")


_CC = shutil.which("gcc") or shutil.which("cc")


def run_c(csrc, main):
    """Compile lowered C plus a `main`, run it, return its exit status."""
    tmp = tempfile.mkdtemp(prefix="csrust-")
    try:
        path = os.path.join(tmp, "t.c")
        with open(path, "w") as f:
            f.write(csrc + "\n" + main + "\n")
        exe = os.path.join(tmp, "t")
        proc = subprocess.run([_CC, "-w", "-o", exe, path],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise AssertionError(
                "generated C did not compile:\n%s\n--- source ---\n%s"
                % (proc.stderr.decode("utf-8", "replace"), csrc))
        return subprocess.run([exe]).returncode
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


needs_cc = unittest.skipIf(_CC is None, "no C compiler")


class TestSemantics(unittest.TestCase):
    """CSHARP.md §1: what a C# `class` is.

    Upstream, `class` is a GC'd reference type: `a = b` aliases, and both
    names see one object. There is no garbage collector here and there is
    not going to be one, so that meaning is not available and the subset has
    to pick a different one *and say so*. It picks single ownership.

    The point of pinning it in a test rather than a document is that the
    decision determines what `=` means, and every later pass reads
    assignments. If this is ever changed to refcounted reference semantics
    (the `[Shared]` opt-in), these are the assertions that should change
    with it -- and the failure of the rest of the suite is the bill.
    """

    def test_class_is_a_value_with_one_owner(self):
        # Not a pointer, not a handle: the lowered field is the struct.
        c = lower("public class Box { public int n; }\n"
                  "public class Holder { public Box b; }\n")
        self.assertIn("struct Holder { Box b; }", re.sub(r"\s+", " ", c))

    def test_destruction_is_at_scope_exit_not_at_a_collector(self):
        c = lower("public class R {\n"
                  "    public int n;\n"
                  "    ~R() { n = 0; }\n"
                  "}\n"
                  "public class U { public void F() { R r = new R(); } }\n")
        # The destructor is called, by name, on the ordinary exit path.
        self.assertIn("R_drop", c)

    def test_shared_is_reserved_and_refused_rather_than_ignored(self):
        # The opt-in named in §1 is not built. It must not silently lower as
        # if it were absent -- that would be the "silently-wrong stub" this
        # project's TRANSPILER.md rules out.
        msg = refusal("[Shared]\npublic class N { public int n; }\n")
        self.assertIn("Shared", msg)
        self.assertIn("not built yet", msg)


class TestClassLowering(unittest.TestCase):

    def test_symbol_shape_matches_cpprust_and_crust(self):
        c = lower("public class Counter {\n"
                  "    private int n;\n"
                  "    public Counter() { n = 0; }\n"
                  "    public void Add(int v) { n += v; }\n"
                  "    public int Get() { return n; }\n"
                  "}\n")
        # The shared lowering: a method is `Class_method(Class *this, ..)`.
        # This is the thing that lets a C# class, a C++ class and a Rust
        # `impl` meet in one translation unit with no shim.
        self.assertIn("Counter_Add(Counter *this, int v)", c)
        self.assertIn("Counter_Get(Counter *this)", c)
        self.assertIn("Counter_new(Counter *this)", c)

    def test_line_numbers_survive(self):
        # A diagnostic from any later pass has to name a line in the `.cs`.
        # Every rewrite in cs2cpp is either length-preserving or confined to
        # one line, so the counts match exactly.
        src = ("using System;\n"
               "\n"
               "public class A {\n"
               "    public int n;\n"
               "}\n")
        self.assertEqual(cs2cpp.translate(src, "t.cs").count("\n"),
                         src.count("\n"))

    def test_csharp_long_is_sixty_four_bits(self):
        # C# fixes `long` at 64 bits; C does not. Passing it through would
        # be a silent change of meaning on any target where C's `long` is 32.
        c = lower("public class A { public long n; }\n")
        self.assertIn("long long n", c)

    @needs_cc
    def test_it_runs(self):
        c = lower("public class Counter {\n"
                  "    private int n;\n"
                  "    public Counter() { n = 0; }\n"
                  "    public void Add(int v) { n += v; }\n"
                  "    public int Get() { return n; }\n"
                  "}\n")
        self.assertEqual(
            run_c(c, "int main(void) { Counter c; Counter_new(&c);"
                     " Counter_Add(&c, 20); Counter_Add(&c, 22);"
                     " return Counter_Get(&c); }"),
            42)


class TestInterfaces(unittest.TestCase):

    SRC = ("public interface IShape {\n"
           "    int Area();\n"
           "}\n"
           "public class Square : IShape {\n"
           "    private int side;\n"
           "    public Square(int s) { side = s; }\n"
           "    public virtual int Area() { return side * side; }\n"
           "}\n")

    def test_interface_becomes_a_pure_abstract_base(self):
        c = lower(self.SRC)
        self.assertIn("struct IShape_vtable", c)
        # Laid out first, so an upcast is a cast -- the property CPPRPY.md's
        # shared object model rests on.
        self.assertIn("struct Square { IShape _base;", re.sub(r"\s+", " ", c))

    @needs_cc
    def test_dispatch_through_the_interface(self):
        c = lower(self.SRC)
        self.assertEqual(
            run_c(c, "int main(void) { Square s; Square_new(&s, 7);"
                     " IShape *p = (IShape *)&s;"
                     " return p->_vptr->Area(p); }"),
            49)


class TestArrays(unittest.TestCase):
    """A C# array carries its length; a C array does not.

    So `T[]` is `vector<T>` rather than `T*`. Lowering it to a bare pointer
    would make `.Length` unanswerable and `foreach` unimplementable, and the
    subset would have to refuse both -- which is a worse answer than
    choosing the type that already has them.
    """

    def test_array_is_a_vector(self):
        cpp = cs2cpp.translate(
            "public class A { public void F(int[] xs) { } }\n", "t.cs")
        self.assertIn("std::vector<int>", cpp)

    def test_jagged_arrays_nest(self):
        cpp = cs2cpp.translate(
            "public class A { public void F(int[][] xs) { } }\n", "t.cs")
        self.assertIn("std::vector<std::vector<int>>", cpp)

    def test_jagged_arrays_nest_past_two(self):
        # Two levels worked with a regex and three did not: a generic
        # argument list nests, and the character class that keeps a pattern
        # from running away also keeps it from matching the nested case, so
        # `byte[][][]` silently came out with two dimensions and a stray
        # `[]`. Pinned at three because two is the depth that passed while
        # broken.
        cpp = cs2cpp.translate(
            "public class A { public void F(byte[][][] g) { } }\n", "t.cs")
        self.assertIn(
            "std::vector<std::vector<std::vector<unsigned char>>>", cpp)

    def test_a_two_word_element_type_stays_whole(self):
        # `byte` is one word and `unsigned char` is two. Mapping types
        # before this pass left the `unsigned` outside the `vector<..>` it
        # belonged in.
        cpp = cs2cpp.translate(
            "public class A { public void F(byte[] b) { } }\n", "t.cs")
        self.assertIn("std::vector<unsigned char>", cpp)

    def test_indexing_is_not_an_array_type(self):
        # Only an *empty* `[]` is a type marker. `a[0]` is an index and
        # `new int[5]` is an allocation; neither is ever written empty.
        cpp = cs2cpp.translate(
            "public class A { public int F(int[] a) { return a[0]; } }\n",
            "t.cs")
        self.assertIn("return a[0];", cpp)

    def test_length_is_size(self):
        cpp = cs2cpp.translate(
            "public class A { public int F(int[] a) { return a.Length; } }\n",
            "t.cs")
        self.assertIn("a.size()", cpp)

    def test_foreach_walks_one(self):
        c = lower("public class Sum {\n"
                  "    public int total;\n"
                  "    public void AddAll(int[] xs) {\n"
                  "        foreach (var x in xs) { total += x; }\n"
                  "    }\n"
                  "}\n")
        self.assertIn("Sum_AddAll(Sum *this, vector_int xs)", c)


class TestRefusals(unittest.TestCase):
    """Every refusal names a C# construct at a C# line.

    This is the argument for the whole cs2cpp/cpprust split. The checks run
    against C# text before anything is rewritten, so the author is never
    shown a diagnostic about generated C++ they did not write. A refusal
    that leaks through to the C++ half is a bug in this file's checks, and
    `csrust.py` says as much when it happens.
    """

    def assert_refuses(self, src, *needles):
        msg = refusal(src)
        self.assertTrue(msg.startswith("test.cs:"),
                        "diagnostic does not name a C# line: %r" % msg)
        for n in needles:
            self.assertIn(n, msg)
        # A refusal with no replacement in it is a bug report filed against
        # the user. Every one of these has to say what to write instead.
        self.assertTrue(len(msg) > 80, "refusal gives no replacement: %r" % msg)

    def test_async(self):
        self.assert_refuses(
            "public class A { public async void F() { } }\n", "`async`")

    def test_await(self):
        self.assert_refuses(
            "public class A { public void F() { await g(); } }\n", "`await`")

    def test_yield(self):
        self.assert_refuses(
            "public class A { public void F() { yield return 1; } }\n",
            "`yield`")

    def test_linq(self):
        self.assert_refuses(
            "public class A { public void F() { var q = from x in xs; } }\n",
            "LINQ")

    def test_dynamic(self):
        self.assert_refuses(
            "public class A { public dynamic d; }\n", "`dynamic`")

    def test_char_is_not_c_char(self):
        # The one refusal that exists because passing it through would be
        # *silently* wrong rather than loudly wrong.
        self.assert_refuses(
            "public class A { public char c; }\n", "`char`", "UTF-16")

    def test_ref_parameters(self):
        self.assert_refuses(
            "public class A { public void F(ref int x) { } }\n", "`ref`")

    def test_multidimensional_arrays(self):
        self.assert_refuses(
            "public class A { public void F(int[,] g) { } }\n", "jagged")

    def test_string_interpolation(self):
        self.assert_refuses(
            'public class A { public void F() { var s = $"x{1}"; } }\n',
            "interpolation")

    def test_file_scoped_namespace(self):
        self.assert_refuses("namespace N;\npublic class A { }\n",
                            "file-scoped")

    def test_a_keyword_in_a_string_is_not_a_keyword(self):
        # The blanked-copy discipline, pinned: this must translate.
        c = lower('public class A {\n'
                  '    public void F() { Log("await the result"); }\n'
                  '}\n')
        self.assertIn("A_F(A *this)", c)

    def test_a_keyword_in_a_comment_is_not_a_keyword(self):
        c = lower("public class A {\n"
                  "    // async is not used here\n"
                  "    public void F() { }\n"
                  "}\n")
        self.assertIn("A_F(A *this)", c)


if __name__ == "__main__":
    unittest.main(verbosity=2)
