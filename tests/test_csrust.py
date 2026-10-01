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

import tools.cpprust as cpprust                              # noqa: E402
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
    assignments. `[Shared]` is the documented opt-in for refcounted
    aliasing (see `test_shared_alias_runs`); default classes stay
    single-owner values.
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

    def test_shared_aliases_through_refcount(self):
        # §1 (c): `[Shared]` is the opt-in for reference semantics. Assignment
        # aliases; both names see one object. Cycles may leak -- that is the
        # documented cost of the opt-in.
        src = ("[Shared]\n"
               "public class Node {\n"
               "    public int v;\n"
               "    public Node() { v = 0; }\n"
               "    public void set(int x) { v = x; }\n"
               "    public int get() { return v; }\n"
               "}\n"
               "public class Prog {\n"
               "    public int Run() {\n"
               "        Node a = new Node();\n"
               "        Node b = a;\n"
               "        b.set(7);\n"
               "        return a.get();\n"
               "    }\n"
               "}\n")
        c = lower(src)
        self.assertTrue(
            ("shared_ptr" in c) or ("Node_copy" in c) or ("use_count" in c),
            c[-600:])
        self.assertIn("Node_set", c)
        self.assertIn("Node_get", c)

    @needs_cc
    def test_shared_alias_runs(self):
        src = ("[Shared]\n"
               "public class Node {\n"
               "    public int v;\n"
               "    public Node() { v = 0; }\n"
               "    public void set(int x) { v = x; }\n"
               "    public int get() { return v; }\n"
               "}\n"
               "public class Prog {\n"
               "    public int Run() {\n"
               "        Node a = new Node();\n"
               "        Node b = a;\n"
               "        b.set(7);\n"
               "        return a.get();\n"
               "    }\n"
               "}\n")
        c = lower(src)
        self.assertEqual(
            run_c(c, "int main(void) { Prog p; return Prog_Run(&p); }"),
            7)


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
        # lowered now (a C++ reference); see TestRefOut for the behaviour
        lower("public class A { public void F(ref int x) { x = 1; } }\n")

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


class TestGenerics(unittest.TestCase):
    """C# generics are templates with no specialisation -- monomorphise."""

    def test_class_becomes_a_template(self):
        cpp = cs2cpp.translate(
            "public class Box<T> { public T v; }\n", "t.cs")
        self.assertIn("template<typename T> class Box", cpp)

    @needs_cc
    def test_monomorphised_box_runs(self):
        src = ("public class Box<T> {\n"
               "    public T v;\n"
               "    public Box(T x) { v = x; }\n"
               "    public T Get() { return v; }\n"
               "}\n"
               "public class Prog {\n"
               "    public int Run() {\n"
               "        Box<int> b = new Box<int>(7);\n"
               "        return b.Get();\n"
               "    }\n"
               "}\n")
        self.assertEqual(
            run_c(lower(src),
                  "int main(void) { Prog p; return Prog_Run(&p); }"),
            7)

    def test_list_is_vector(self):
        cpp = cs2cpp.translate(
            "public class A { public void F(List<int> xs) { } }\n", "t.cs")
        self.assertIn("std::vector<int>", cpp)

    def test_dictionary_is_map(self):
        cpp = cs2cpp.translate(
            "public class A { public void F(Dictionary<int, int> m) { } }\n",
            "t.cs")
        self.assertIn("std::map<", cpp)


class TestProperties(unittest.TestCase):

    def test_auto_property_desugars(self):
        cpp = cs2cpp.translate(
            "public class A { public int Count { get; set; } }\n", "t.cs")
        self.assertIn("get_Count", cpp)
        self.assertIn("set_Count", cpp)
        self.assertIn("_Count", cpp)

    @needs_cc
    def test_property_runs(self):
        src = ("public class Box {\n"
               "    public int Count { get; set; }\n"
               "    public Box() { }\n"
               "    public int Bump() {\n"
               "        this.Count = this.Count + 1;\n"
               "        return this.Count;\n"
               "    }\n"
               "}\n")
        c = lower(src)
        self.assertEqual(
            run_c(c, "int main(void) { Box b; Box_new(&b);"
                     " Box_set_Count(&b, 41); return Box_Bump(&b); }"),
            42)


class TestExcept(unittest.TestCase):

    def test_throw_becomes_raise(self):
        cpp = cs2cpp.translate(
            "public class A {\n"
            "    public int F(int x) {\n"
            "        if (x < 0) { throw 42; }\n"
            "        return x;\n"
            "    }\n"
            "}\n", "t.cs")
        self.assertIn("raise", cpp)
        self.assertIn("except", cpp)
        self.assertNotIn("throw", cpp)

    @needs_cc
    def test_raise_sets_the_flag(self):
        src = ("public class A {\n"
               "    public int F(int x) {\n"
               "        if (x < 0) { throw 42; }\n"
               "        return x * 2;\n"
               "    }\n"
               "}\n")
        c = lower(src)
        # Call through a C driver that checks the except flag after F.
        main = (
            "int main(void) {\n"
            "  A a; int r = A_F(&a, -1);\n"
            "  if (!_cpp_exc.flag) return 1;\n"
            "  if (_cpp_exc.val != 42) return 2;\n"
            "  (void)r; return 0;\n"
            "}\n")
        self.assertEqual(run_c(c, main), 0)


class TestSugar(unittest.TestCase):

    def test_this_dot_becomes_arrow(self):
        cpp = cs2cpp.translate(
            "public class A { public int n; public void F() { this.n = 1; } }\n",
            "t.cs")
        self.assertIn("this->n", cpp)

    def test_delegate_is_a_function_pointer(self):
        cpp = cs2cpp.translate("public delegate int D(int x);\n", "t.cs")
        self.assertIn("typedef int (*D)(int x);", cpp)

    def test_lambda_becomes_cpp_lambda(self):
        cpp = cs2cpp.translate(
            "public class A {\n"
            "    public int F() { return ((int x) => x + 1)(3); }\n"
            "}\n", "t.cs")
        self.assertIn("[](int x) { return x + 1; }", cpp)


class TestByteArrays(unittest.TestCase):
    """`byte[]` is `vector<unsigned char>`, and has to *compile* as one.

    `test_a_two_word_element_type_stays_whole` pins the C++ spelling; this
    pins the C. The two came apart: cpprust expanded `__cpp_ref(T)` only
    for a one-word `T`, so `__cpp_ref(unsigned char)` reached the C compiler
    as an unknown type and no method taking a `byte[]` built at all.
    """

    @needs_cc
    def test_a_byte_array_parameter_compiles_and_runs(self):
        c = lower("public class A {\n"
                  "    public int Sum(byte[] b) {\n"
                  "        int t = 0;\n"
                  "        foreach (var x in b) { t += x; }\n"
                  "        return t;\n"
                  "    }\n"
                  "    public int Run() {\n"
                  "        byte[] b = new byte[3];\n"
                  "        b[0] = 40; b[2] = 2;\n"
                  "        return Sum(b);\n"
                  "    }\n"
                  "}\n")
        self.assertEqual(run_c(c, "int main(void) { A a; return A_Run(&a); }"),
                         42)

    @needs_cc
    def test_new_array_has_its_length_and_is_zeroed(self):
        # C# `new T[n]` is n default values. The prelude's `vector(int)`
        # only reserves, so that spelling would have length 0.
        c = lower("public class A {\n"
                  "    public int Run() {\n"
                  "        int[] xs = new int[5];\n"
                  "        xs[2] = 7;\n"
                  "        int t = 0;\n"
                  "        foreach (var x in xs) { t += x; }\n"
                  "        return t * 10 + xs.Length;\n"
                  "    }\n"
                  "}\n")
        self.assertEqual(run_c(c, "int main(void) { A a; return A_Run(&a); }"),
                         75)


#: The example the feature was written against, moved into a method: the
#: statements were top-level, which is refused (`TestTopLevelStatements`).
PACKET = ("using System;\n"
          "using System.Runtime.InteropServices;\n"
          "\n"
          "[StructLayout(LayoutKind.Sequential, Pack = 1)]\n"
          "public struct PacketData\n"
          "{\n"
          "    public int Id;\n"
          "    public float Value;\n"
          "}\n"
          "\n"
          "public class Program\n"
          "{\n"
          "    public int Run()\n"
          "    {\n"
          "        PacketData packet = new PacketData { Id = 101, Value = 3.14f };\n"
          "        byte[] rawBytes = MemoryMarshal.AsBytes("
          "MemoryMarshal.CreateSpan(ref packet, 1)).ToArray();\n"
          "        PacketData back = MemoryMarshal.Read<PacketData>(rawBytes);\n"
          "\n"
          "        if (rawBytes.Length != 8) { return 1; }\n"
          # The bytes .NET produces on a little-endian machine: 101 as an
          # int, then 3.14f as its IEEE-754 bits, 0x4048F5C3.
          "        if (rawBytes[0] != 101 || rawBytes[1] != 0) { return 2; }\n"
          "        if (rawBytes[4] != 0xC3 || rawBytes[5] != 0xF5) { return 3; }\n"
          "        if (rawBytes[6] != 0x48 || rawBytes[7] != 0x40) { return 4; }\n"
          "        if (back.Id != 101 || back.Value != 3.14f) { return 5; }\n"
          "        return 0;\n"
          "    }\n"
          "}\n")

_RUN_PROGRAM = "int main(void) { Program p; return Program_Run(&p); }"

#: The same, after filling the stack below `main` with 0xAA. A fresh
#: process's stack is often already zero, so without this a struct that
#: was never zeroed reads as zero anyway and the zeroing tests pass whether
#: or not anything zeroes it -- which is how they first passed.
_RUN_ON_DIRTY_STACK = (
    "static void dirty(void) { volatile unsigned char junk[8192];"
    " volatile int i; for (i = 0; i < 8192; i++) { junk[i] = 0xAA; }"
    # The counter shares the top of the frame with the caller's next
    # locals, and it stops at 0x2000 -- low byte zero, which read as a
    # zeroed field. Left at garbage like everything else.
    " i = (int)0xAAAAAAAA; }\n"
    "int main(void) { Program p; dirty(); return Program_Run(&p); }")

_MM_HEAD = "using System.Runtime.InteropServices;\n"


def run_shivyc(c_src, main):
    """Build `c_src` + `main` with Crust's own compiler and run it.

    Beside `run_c` (the host compiler) because the claims that matter for
    struct layout are about agreement: gcc and shivyc must give one source
    one layout, or serialised bytes depend on which of them built it."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tmp = tempfile.mkdtemp(prefix="csrust-shivyc-")
    try:
        path = os.path.join(tmp, "t.c")
        with open(path, "w") as f:
            f.write(c_src + "\n" + main + "\n")
        exe = os.path.join(tmp, "t")
        proc = subprocess.run(
            [sys.executable, "-m", "shivyc.main", "--no-cache", path,
             "-o", exe],
            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise AssertionError("shivyc: %s" % (proc.stdout + proc.stderr)
                                 .decode("utf-8", "replace")[-2000:])
        return subprocess.run([exe]).returncode
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _program(body, types=""):
    return (_MM_HEAD + types + "public class Program {\n"
            "    public int Run() {\n" + body + "\n    }\n}\n")


class TestBlittableSerialization(unittest.TestCase):
    """An unmanaged struct is its bytes, in C# and in the lowered C alike.

    So `MemoryMarshal` over one is a byte copy, and the claim worth pinning
    is not that it translates but that the bytes are the ones .NET would
    produce -- checked here byte by byte, and round-tripped.
    """

    @needs_cc
    def test_the_example_produces_dotnets_bytes(self):
        if sys.byteorder != "little":                        # pragma: no cover
            self.skipTest("expected bytes are little-endian")
        self.assertEqual(run_c(lower(PACKET), _RUN_PROGRAM), 0)

    def test_the_example_keeps_its_line_count(self):
        self.assertEqual(cs2cpp.translate(PACKET, "t.cs").count("\n"),
                         PACKET.count("\n"))

    @unittest.skipIf(sys.byteorder != "little", "little-endian bytes")
    def test_the_example_runs_under_shivyc(self):
        # gcc and shivyc must agree on the layout, or one source has two
        # byte formats. Pinned on Crust's own compiler as well as the host's.
        self.assertEqual(run_shivyc(lower(PACKET), _RUN_PROGRAM), 0)

    @needs_cc
    def test_write_into_an_allocated_buffer(self):
        src = _program(
            "        P p = new P { Id = 7, Value = 1.5f };\n"
            "        byte[] buf = new byte[Marshal.SizeOf<P>()];\n"
            "        MemoryMarshal.Write(buf, ref p);\n"
            "        P q = MemoryMarshal.Read<P>(buf);\n"
            "        if (buf.Length != 8) { return 1; }\n"
            "        return q.Id;",
            "public struct P { public int Id; public float Value; }\n")
        self.assertEqual(run_c(lower(src), _RUN_PROGRAM), 7)

    @needs_cc
    def test_write_takes_in_as_well_as_ref(self):
        src = _program(
            "        P p = new P { Id = 9 };\n"
            "        byte[] buf = new byte[4];\n"
            "        MemoryMarshal.Write(buf, in p);\n"
            "        return buf[0];",
            "public struct P { public int Id; }\n")
        self.assertEqual(run_c(lower(src), _RUN_PROGRAM), 9)

    @needs_cc
    def test_nested_structs_and_enums_round_trip(self):
        # 4 + 2 + 1 + 1 for the header, 8 + 8 after it: 24, with no
        # padding, which is also what .NET's sequential layout gives.
        src = _program(
            "        M m = new M { Seq = 1234567890123, X = 0.5 };\n"
            "        m.Head.K = (Kind)2;\n"
            "        m.Head.Len = 300;\n"
            "        byte[] b = MemoryMarshal.AsBytes("
            "MemoryMarshal.CreateSpan(ref m, 1)).ToArray();\n"
            "        M r = MemoryMarshal.Read<M>(b);\n"
            "        if (b.Length != 24) { return 1; }\n"
            "        if (r.Seq != 1234567890123) { return 2; }\n"
            "        if ((int)r.Head.K != 2 || r.Head.Len != 300) { return 3; }\n"
            "        return 0;",
            "public enum Kind { A, B, C }\n"
            "public struct H { public Kind K; public short Len;"
            " public byte Flags; public byte Pad; }\n"
            "public struct M { public H Head; public long Seq;"
            " public double X; }\n")
        self.assertEqual(run_c(lower(src), _RUN_PROGRAM), 0)

    @needs_cc
    def test_an_auto_property_is_serialised_through_its_field(self):
        src = _program(
            "        A a = new A { X = 4, Y = 6 };\n"
            "        byte[] b = MemoryMarshal.AsBytes("
            "MemoryMarshal.CreateSpan(ref a, 1)).ToArray();\n"
            "        A r = MemoryMarshal.Read<A>(b);\n"
            "        return r.X * 10 + r.Y + b.Length * 100;",
            "public struct A { public int X { get; set; } public int Y; }\n")
        self.assertEqual(run_c(lower(src), _RUN_PROGRAM) & 0xff,
                         (846) & 0xff)

    @needs_cc
    def test_a_short_buffer_aborts(self):
        # `ArgumentOutOfRangeException` in .NET. Unhandled, that ends the
        # process, which is what this does; the checked `except` model
        # would make every C# caller handle it, and none of them do.
        src = _program("        byte[] b = new byte[3];\n"
                       "        P q = MemoryMarshal.Read<P>(b);\n"
                       "        return 0;",
                       "public struct P { public int Id; }\n")
        self.assertNotEqual(run_c(lower(src), _RUN_PROGRAM), 0)

    def test_the_helpers_sit_at_file_scope_in_a_namespace(self):
        # Declared inside a namespace, `abort` came out as `Net_abort` and
        # failed to link.
        src = (_MM_HEAD + "namespace Net {\n"
               "public struct P { public int Id; }\n"
               "public class Program {\n"
               "    public int Run() {\n"
               "        byte[] b = new byte[4];\n"
               "        return MemoryMarshal.Read<P>(b).Id;\n"
               "    }\n"
               "}\n"
               "}\n")
        c = lower(src)
        self.assertNotIn("Net_abort", c)
        if _CC is not None:
            self.assertEqual(
                run_c(c, "int main(void) { Net_Program p;"
                         " return Net_Program_Run(&p); }"), 0)


class TestObjectInitializers(unittest.TestCase):

    @needs_cc
    def test_unmentioned_fields_are_zero(self):
        # In C# they are. Left uninitialised in C they are stack garbage,
        # and serialising the struct puts that garbage in the bytes.
        src = _program(
            "        var p = new P { Id = 5 };\n"
            "        byte[] b = MemoryMarshal.AsBytes("
            "MemoryMarshal.CreateSpan(ref p, 1)).ToArray();\n"
            "        return b[0] + b[4] + b[5] + b[6] + b[7];",
            "public struct P { public int Id; public float Value; }\n")
        self.assertEqual(run_c(lower(src), _RUN_ON_DIRTY_STACK), 5)

    @needs_cc
    def test_new_of_a_plain_struct_is_zeroed(self):
        # Wide on purpose. A four-byte struct lands in the alignment gap
        # beside gcc's stack canary, which `dirty` never writes, and read
        # as zero with the zeroing removed.
        src = _program("        P p = new P();\n"
                       "        return (int)(p.A | p.B | p.C | p.D) + p.Id + 3;",
                       "public struct P { public long A, B, C, D;"
                       " public int Id; }\n")
        self.assertEqual(run_c(lower(src), _RUN_ON_DIRTY_STACK), 3)

    def test_a_multi_line_initializer_keeps_the_line_count(self):
        src = _program("        P p = new P\n"
                       "        {\n"
                       "            Id = 11,\n"
                       "            Value = 2.0f,\n"
                       "        };\n"
                       "        return p.Id;",
                       "public struct P { public int Id; public float Value; }\n")
        cpp = cs2cpp.translate(src, "t.cs")
        self.assertEqual(cpp.count("\n"), src.count("\n"))
        if _CC is not None:
            self.assertEqual(run_c(lower(src), _RUN_PROGRAM), 11)

    @needs_cc
    def test_a_class_is_constructed_then_assigned(self):
        # Not zeroed: a class with a constructor runs it, and the
        # initializer's assignments come after, as in C#.
        src = ("public class C {\n"
               "    public int A; public int B;\n"
               "    public C() { A = 1; B = 2; }\n"
               "}\n" + _program("        C c = new C { B = 40 };\n"
                                "        return c.A + c.B;"))
        self.assertEqual(run_c(lower(src), _RUN_PROGRAM), 41)


class TestStructLayout(unittest.TestCase):
    """`[StructLayout]`: checked against the fields, and `Pack` carried over.

    `Sequential` is what a C struct already is. A `Pack` that changes the
    layout becomes `_Pragma("pack(push, N)")` .. `_Pragma("pack(pop)")`
    around the struct, on its own lines. That is only sound because shivyc
    honours packing as gcc does -- before it did, one source had two
    layouts -- so each layout claim here is run under both compilers.
    """

    def assert_refuses(self, src, *needles):
        TestRefusals.assert_refuses(self, src, *needles)

    def test_pack_with_no_padding_to_remove_is_accepted(self):
        c = lower("using System.Runtime.InteropServices;\n"
                  "[StructLayout(LayoutKind.Sequential, Pack = 1)]\n"
                  "public struct P { public int Id; public float Value; }\n")
        self.assertIn("struct P", c)
        self.assertNotIn("StructLayout", c)
        self.assertNotIn("pragma", c)

    def _packed_q(self, fields, body):
        return _program(
            "        Q q = new Q { " + body[0] + " };\n"
            "        byte[] b = MemoryMarshal.AsBytes("
            "MemoryMarshal.CreateSpan(ref q, 1)).ToArray();\n"
            "        Q r = MemoryMarshal.Read<Q>(b);\n" + body[1],
            "[StructLayout(LayoutKind.Sequential, Pack = 1)]\n"
            "public struct Q\n"
            "{\n" + fields + "}\n")

    def assert_both(self, src, want):
        c = lower(src)
        self.assertEqual(run_c(c, _RUN_PROGRAM), want, "gcc")
        self.assertEqual(run_shivyc(c, _RUN_PROGRAM), want, "shivyc")

    def test_pack_that_moves_a_field_is_emitted(self):
        # `Id` at 1, not 4: five bytes, the ones .NET gives.
        src = self._packed_q(
            "    public byte Tag;\n    public int Id;\n",
            ("Tag = 7, Id = 0x01020304",
             "        if (b.Length != 5) { return 1; }\n"
             "        if (b[0] != 7 || b[1] != 4 || b[4] != 1) { return 2; }\n"
             "        return r.Id == 0x01020304 && r.Tag == 7 ? 0 : 3;"))
        cpp = cs2cpp.translate(src, "t.cs")
        self.assertEqual(cpp.count("\n"), src.count("\n"))
        self.assertIn('_Pragma("pack(push, 1)")', cpp)
        self.assertIn('_Pragma("pack(pop)")', cpp)
        if _CC is not None and sys.byteorder == "little":
            self.assert_both(src, 0)

    @needs_cc
    def test_pack_that_removes_tail_padding_is_emitted(self):
        src = self._packed_q(
            "    public int Id;\n    public byte Tag;\n",
            ("Id = 9, Tag = 3", "        return b.Length * 10 + b[4];"))
        self.assert_both(src, 53)

    @needs_cc
    def test_pack_two(self):
        src = _program("        return Marshal.SizeOf<Q>();",
                       "[StructLayout(LayoutKind.Sequential, Pack = 2)]\n"
                       "public struct Q { public byte Tag; public int Id;"
                       " public double D; }\n")
        self.assert_both(src, 14)

    @needs_cc
    def test_a_packed_struct_inside_a_natural_one(self):
        # `In` is 5 bytes with alignment 1; `W` still aligns to 8: 16.
        # `In` is declared below `Q`, so its definition is moved up -- and
        # has to keep its `Pack` when it is (`TestDeclarationOrder`).
        src = _program(
            "        Q q = new Q { A = 1, W = 5 };\n"
            "        q.In.V = 300;\n"
            "        byte[] b = MemoryMarshal.AsBytes("
            "MemoryMarshal.CreateSpan(ref q, 1)).ToArray();\n"
            "        Q r = MemoryMarshal.Read<Q>(b);\n"
            "        if (r.In.V != 300 || r.W != 5) { return 99; }\n"
            "        return b.Length;",
            "public struct Q { public byte A; public In In; public long W; }\n"
            "[StructLayout(LayoutKind.Sequential, Pack = 1)]\n"
            "public struct In { public byte T; public int V; }\n")
        self.assert_both(src, 16)

    @needs_cc
    def test_size_is_checked_against_the_packed_layout(self):
        src = _program("        return Marshal.SizeOf<Q>();",
                       "[StructLayout(LayoutKind.Sequential, Pack = 1,"
                       " Size = 5)]\n"
                       "public struct Q { public byte T; public int V; }\n")
        self.assert_both(src, 5)
        self.assert_refuses(
            "[StructLayout(LayoutKind.Sequential, Pack = 1, Size = 8)]\n"
            "public struct Q { public byte T; public int V; }\n",
            "`Size = 8`", "(5)")

    def test_a_packed_struct_nested_in_a_class_is_refused(self):
        self.assert_refuses(
            "public class Outer {\n"
            "    [StructLayout(LayoutKind.Sequential, Pack = 1)]\n"
            "    public struct Q { public byte T; public int V; }\n"
            "}\n", "nested in `Outer`", "outside the class")

    def test_packing_an_owner_is_refused(self):
        self.assert_refuses(
            "[StructLayout(LayoutKind.Sequential, Pack = 1)]\n"
            "public struct Q { public byte T; public string S; }\n",
            "`Q.S`", "plain data")

    def test_explicit_layout_is_refused(self):
        self.assert_refuses(
            "[StructLayout(LayoutKind.Explicit)]\n"
            "public struct Q { public int Id; }\n", "`LayoutKind.Explicit`")

    def test_the_attribute_may_share_the_struct_line(self):
        c = lower("[StructLayout(LayoutKind.Sequential)] public struct Q"
                  " { public int Id; }\n")
        self.assertNotIn("StructLayout", c)

    def test_a_pack_hidden_in_a_combined_attribute_is_not_dropped(self):
        # A whole-line attribute is otherwise dropped unread, which would
        # lose the `Pack` without a word.
        self.assert_refuses(
            "[Serializable, StructLayout(LayoutKind.Sequential, Pack = 1)]\n"
            "public struct Q { public byte Tag; public int Id; }\n",
            "its own brackets")


class TestTopLevelStatements(unittest.TestCase):

    def test_refused_at_the_first_statement(self):
        src = ("using System;\n"
               "\n"
               "public struct P { public int Id; }\n"
               "\n"
               "P p = new P { Id = 1 };\n")
        msg = refusal(src)
        self.assertTrue(msg.startswith("test.cs:5:"), msg)
        self.assertIn("top-level statement", msg)

    def test_types_and_namespaces_alone_are_not_statements(self):
        c = lower("using System;\n"
                  "using X = System.Int32;\n"
                  "public delegate int D(int x);\n"
                  "namespace N {\n"
                  "    public struct P { public int Id; };\n"
                  "}\n")
        self.assertIn("N_P", c)


class TestPlainDataRefusals(unittest.TestCase):
    """What a byte copy cannot honestly do, refused in C# terms."""

    def assert_refuses(self, src, *needles):
        TestRefusals.assert_refuses(self, src, *needles)

    def test_a_reference_field_is_not_plain_data(self):
        self.assert_refuses(_program(
            "        Q q = new Q();\n"
            "        byte[] b = MemoryMarshal.AsBytes("
            "MemoryMarshal.CreateSpan(ref q, 1)).ToArray();\n"
            "        return 0;",
            "public struct Q { public int Id; public string Name; }\n"),
            "unmanaged struct", "`Q.Name`", "`string`")

    def test_a_class_is_not_plain_data(self):
        self.assert_refuses(_program(
            "        byte[] b = new byte[4];\n"
            "        Q q = MemoryMarshal.Read<Q>(b);\n"
            "        return 0;",
            "public class Q { public int Id; }\n"), "`Q` is a class")

    def test_a_struct_with_an_interface_carries_a_vtable(self):
        self.assert_refuses(_program(
            "        S s = new S();\n"
            "        byte[] b = MemoryMarshal.AsBytes("
            "MemoryMarshal.CreateSpan(ref s, 1)).ToArray();\n"
            "        return 0;",
            "public interface I { int G(); }\n"
            "public struct S : I { public int X;"
            " public virtual int G() { return X; } }\n"), "vtable")

    def test_read_needs_a_parameterless_constructor(self):
        self.assert_refuses(_program(
            "        byte[] b = new byte[4];\n"
            "        R r = MemoryMarshal.Read<R>(b);\n"
            "        return 0;",
            "public struct R { public int A;"
            " public R(int a) { A = a; } }\n"), "parameterless")

    def test_spans_are_refused(self):
        self.assert_refuses(_program(
            "        P p = new P();\n"
            "        Span<byte> s = MemoryMarshal.AsBytes("
            "MemoryMarshal.CreateSpan(ref p, 1));\n"
            "        return 0;",
            "public struct P { public int Id; }\n"), "`Span<T>`")

    def test_as_bytes_without_to_array_is_refused(self):
        self.assert_refuses(_program(
            "        P p = new P();\n"
            "        var s = MemoryMarshal.AsBytes("
            "MemoryMarshal.CreateSpan(ref p, 1));\n"
            "        return 0;",
            "public struct P { public int Id; }\n"), ".ToArray()")

    def test_a_longer_span_is_refused(self):
        self.assert_refuses(_program(
            "        P p = new P();\n"
            "        byte[] b = MemoryMarshal.AsBytes("
            "MemoryMarshal.CreateSpan(ref p, 2)).ToArray();\n"
            "        return 0;",
            "public struct P { public int Id; }\n"), "count of `1`")

    def test_other_memory_marshal_members_are_refused(self):
        self.assert_refuses(_program(
            "        byte[] b = new byte[4];\n"
            "        var x = MemoryMarshal.Cast<byte, int>(b);\n"
            "        return 0;"), "`MemoryMarshal.Cast`", "`MemoryMarshal.Read<T>")

    def test_a_call_site_ref_elsewhere_is_still_refused(self):
        # `CreateSpan(ref x, 1)` and a call to a method the program declares
        # with `ref` are the call-site `ref`s read; any other is refused.
        self.assert_refuses(
            "public class A { public void F() { int x = 0; Other.G(ref x); } }\n",
            "`ref`")

    def test_an_initializer_outside_a_declaration_is_refused(self):
        self.assert_refuses(
            "public struct P { public int Id; }\n"
            "public class A { public P Make() { return new P { Id = 1 }; } }\n",
            "object initializer", "Declare a local")

    def test_a_collection_initializer_is_refused(self):
        self.assert_refuses(_program(
            "        var xs = new List<int> { 1, 2 };\n"
            "        return 0;"), "Collection")

    def test_an_array_initializer_is_refused(self):
        self.assert_refuses(_program(
            "        byte[] b = new byte[] { 1, 2 };\n"
            "        return 0;"), "array initializer")

    def test_an_array_of_objects_is_refused(self):
        self.assert_refuses(
            "public class C { public int A; }\n" + _program(
                "        C[] cs = new C[3];\n"
                "        return 0;"), "`new C[n]`", "primitive")


class TestEnums(unittest.TestCase):
    """`Kind.A` is `Kind_A`, and `Kind` is a typedef of its underlying type.

    C puts every enum member in one file-wide namespace and leaves an
    enum's size to the compiler; C# scopes members to their type and fixes
    the size. The prefix answers the first, the typedef the second -- and
    the second is not cosmetic: `TestEnums.test_a_byte_enum_is_one_byte`
    is a serialised layout that moved with a four-byte enum.
    """

    def assert_refuses(self, src, *needles):
        TestRefusals.assert_refuses(self, src, *needles)

    @needs_cc
    def test_members_switch_and_compare(self):
        src = ("public enum Kind { A, B, C }\n" + _program(
            "        Kind k = Kind.C;\n"
            "        switch (k) {\n"
            "            case Kind.A: return 1;\n"
            "            case Kind.C: return 40 + (int)Kind.C;\n"
            "            default: return 2;\n"
            "        }"))
        self.assertEqual(run_c(lower(src), _RUN_PROGRAM), 42)

    def test_members_are_prefixed_by_their_type(self):
        cpp = cs2cpp.translate("public enum Color { Red, Green }\n"
                               "public enum Light { Red, Amber }\n", "t.cs")
        # Two `Red`s, which C could not hold under one name.
        self.assertIn("Color_Red", cpp)
        self.assertIn("Light_Red", cpp)
        self.assertIn("typedef int Color;", cpp)

    @needs_cc
    def test_explicit_values_and_sibling_references(self):
        # `B = A + 4` names a sibling bare, which is in scope in C#.
        src = ("public enum Kind\n"
               "{\n"
               "    A = 3,\n"
               "    B = A + 4,\n"
               "    C = 0x2,\n"
               "}\n" + _program("        return (int)Kind.C * 100"
                                " + (int)Kind.B;"))
        cpp = cs2cpp.translate(src, "t.cs")
        self.assertEqual(cpp.count("\n"), src.count("\n"))
        self.assertEqual(run_c(lower(src), _RUN_PROGRAM), 207)

    @needs_cc
    def test_flags_combine(self):
        src = ("[Flags] public enum Perm { None = 0, Read = 1, Write = 2 }\n"
               + _program("        Perm p = Perm.Read | Perm.Write;\n"
                          "        if ((p & Perm.Write) == 0) { return 99; }\n"
                          "        return (int)p;"))
        self.assertEqual(run_c(lower(src), _RUN_PROGRAM), 3)

    @needs_cc
    def test_parameters_returns_and_casts(self):
        src = ("public enum Kind { A, B, C }\n"
               "public class Program {\n"
               "    Kind Next(Kind k) {\n"
               "        if (k == Kind.C) { return Kind.A; }\n"
               "        return (Kind)((int)k + 1);\n"
               "    }\n"
               "    public int Run() { return (int)Next(Next(Kind.A)); }\n"
               "}\n")
        self.assertEqual(run_c(lower(src), _RUN_PROGRAM), 2)

    @needs_cc
    def test_a_nested_enum_is_hoisted(self):
        # cpprust has no nested enum: inside a struct it emitted
        # `enum Type;` as a member. The declaration moves out, collapsed
        # onto the class's line, and leaves its own lines blank.
        src = ("public class Packet {\n"
               "    public enum Type\n"
               "    {\n"
               "        Ping,\n"
               "        Ack = 7,\n"
               "    }\n"
               "    public Type t;\n"
               "    public void Mark() { t = Type.Ack; }\n"
               "}\n"
               "public class Program {\n"
               "    public int Run() {\n"
               "        Packet p = new Packet();\n"
               "        p.Mark();\n"
               "        Packet.Type x = Packet.Type.Ack;\n"
               "        if (p.t != x) { return 1; }\n"
               "        return (int)p.t;\n"
               "    }\n"
               "}\n")
        cpp = cs2cpp.translate(src, "t.cs")
        self.assertEqual(cpp.count("\n"), src.count("\n"))
        self.assertEqual(run_c(lower(src), _RUN_PROGRAM), 7)

    @needs_cc
    def test_in_a_namespace(self):
        src = ("namespace Net {\n"
               "    public enum Kind { A, B }\n"
               "    public class Program {\n"
               "        public int Run() {\n"
               "            Kind k = Kind.B;\n"
               "            return (int)k + (int)Net.Kind.B;\n"
               "        }\n"
               "    }\n"
               "}\n")
        self.assertEqual(
            run_c(lower(src), "int main(void) { Net_Program p;"
                              " return Net_Program_Run(&p); }"), 2)

    @needs_cc
    def test_a_byte_enum_is_one_byte(self):
        # 1 + 1 + 2: four bytes. A C enum here is four bytes wide on its
        # own, which made the struct eight and moved `N`.
        src = _program(
            "        H h = new H { K = Kind.C, N = 9 };\n"
            "        byte[] b = MemoryMarshal.AsBytes("
            "MemoryMarshal.CreateSpan(ref h, 1)).ToArray();\n"
            "        H r = MemoryMarshal.Read<H>(b);\n"
            "        if (r.K != Kind.C || r.N != 9) { return 1; }\n"
            "        return b.Length * 10 + b[0] + b[2];",
            "public enum Kind : byte { A, B, C }\n"
            "public struct H { public Kind K; public byte Pad;"
            " public short N; }\n")
        self.assertEqual(run_c(lower(src), _RUN_PROGRAM), 40 + 2 + 9)

    @needs_cc
    def test_an_enum_array_is_zeroed(self):
        src = ("public enum Kind : byte { A, B }\n" + _program(
            "        Kind[] ks = new Kind[3];\n"
            "        ks[2] = Kind.B;\n"
            "        return (int)ks[2] + ks.Length + (int)ks[0];"))
        self.assertEqual(run_c(lower(src), _RUN_ON_DIRTY_STACK), 4)

    @needs_cc
    def test_a_property_may_share_the_enum_name(self):
        src = ("public enum Color { Red, Green }\n"
               "public class Box { public Color Color { get; set; } }\n"
               + _program("        Box b = new Box();\n"
                          "        b.Color = Color.Green;\n"
                          "        return (int)b.Color;"))
        self.assertEqual(run_c(lower(src), _RUN_PROGRAM), 1)

    def test_a_field_sharing_the_enum_name_is_refused(self):
        self.assert_refuses("public enum Color { Red }\n"
                            "public class Box { public Color Color; }\n",
                            "`Color`", "{ get; set; }")

    def test_two_enums_of_one_name_are_refused(self):
        self.assert_refuses("namespace A { public enum Kind { X } }\n"
                            "namespace B { public enum Kind { Y } }\n",
                            "two enums are named `Kind`")

    def test_enum_methods_are_refused(self):
        self.assert_refuses("public enum Kind { A }\n" + _program(
            "        var k = Kind.Parse(\"A\");\n        return 0;"),
            "`Kind.Parse`")
        self.assert_refuses("public enum Kind { A }\n" + _program(
            "        string s = Kind.A.ToString();\n        return 0;"),
            "`ToString`")

    def test_a_constant_past_int_is_refused(self):
        self.assert_refuses(
            "public enum Big : uint { Top = 0xFFFFFFFF }\n", "`int` range")

    def test_a_non_integral_base_is_refused(self):
        self.assert_refuses("public enum Kind : float { A }\n",
                            "integral")


class TestDeclarationOrder(unittest.TestCase):
    """A type may hold one declared below it, as in C#.

    C needs a by-value field's struct complete first, so the C++ half moves
    the struct definitions a holder needs above it (`any_order`, which
    `csrust` turns on): classes, structs, and container instantiations like
    `vector_Item`. Only the definition moves; method bodies stay where they
    were written. Each case runs under gcc and shivyc.
    """

    def assert_refuses(self, src, *needles):
        TestRefusals.assert_refuses(self, src, *needles)

    def assert_both(self, src, want):
        TestStructLayout.assert_both(self, src, want)

    @needs_cc
    def test_a_struct_holding_a_later_struct(self):
        self.assert_both(
            "public struct Q { public byte A; public In inner; }\n"
            "public struct In { public int V; }\n" + _program(
                "        Q q = new Q();\n"
                "        q.inner.V = 7;\n"
                "        return q.inner.V;"), 7)

    @needs_cc
    def test_a_class_holding_a_later_class(self):
        # A class field is owned, so stored by value: the same need.
        self.assert_both(
            "public class A { public B b; public int Get() { return b.x; } }\n"
            "public class B { public int x; }\n" + _program(
                "        A a = new A();\n"
                "        a.b.x = 5;\n"
                "        return a.Get();"), 5)

    @needs_cc
    def test_a_list_of_a_later_class(self):
        # `vector_Item` is held back until `Item` is complete, and `Inv`
        # holds it by value: its struct definition moves above `Inv`.
        self.assert_both(
            "using System.Collections.Generic;\n"
            "public class Inv { public List<Item> items = new List<Item>();"
            " public int K() { return 3; } }\n"
            "public class Item { public int w; }\n" + _program(
                "        Inv i = new Inv();\n"
                "        return i.K();"), 3)

    @needs_cc
    def test_a_chain_declared_out_of_order(self):
        # A needs B needs C, with C in the middle: dependencies first.
        self.assert_both(
            "public struct A { public B b; }\n"
            "public struct C { public int v; }\n"
            "public struct B { public C c; }\n" + _program(
                "        A a = new A();\n"
                "        a.b.c.v = 6;\n"
                "        return a.b.c.v;"), 6)

    @needs_cc
    def test_a_later_generic_holding_its_argument(self):
        self.assert_both(
            "public class Holder { public Box<Item> b;"
            " public int Get() { return b.v.w; } }\n"
            "public class Box<T> { public T v; }\n"
            "public class Item { public int w; }\n" + _program(
                "        Holder h = new Holder();\n"
                "        h.b.v.w = 9;\n"
                "        return h.Get();"), 9)

    def test_methods_stay_where_they_were_written(self):
        src = ("public struct Q\n"
               "{\n"
               "    public In inner;\n"
               "    public int Twice() { return inner.Get() * 2; }\n"
               "}\n"
               "public struct In\n"
               "{\n"
               "    public int V;\n"
               "    public int Get() { return V; }\n"
               "}\n" + _program("        Q q = new Q();\n"
                                "        q.inner.V = 4;\n"
                                "        return q.Twice();"))
        self.assertEqual(cs2cpp.translate(src, "t.cs").count("\n"),
                         src.count("\n"))
        if _CC is not None:
            self.assert_both(src, 8)

    @needs_cc
    def test_a_moved_struct_keeps_its_pack(self):
        # The `_Pragma` pair around `In` stays where `In` was written; the
        # moved definition carries its own. Without that it silently came
        # out 8 bytes under gcc -- the failure packing exists to prevent.
        self.assert_both(
            "public struct Q { public byte A; public In inner;"
            " public long W; }\n"
            "[StructLayout(LayoutKind.Sequential, Pack = 1)]\n"
            "public struct In { public byte T; public int V; }\n" + _program(
                "        return Marshal.SizeOf<In>() * 10"
                " + Marshal.SizeOf<Q>();"), 66)

    @needs_cc
    def test_a_moved_struct_does_not_take_the_holders_pack(self):
        self.assert_both(
            "[StructLayout(LayoutKind.Sequential, Pack = 1)]\n"
            "public struct Q { public byte A; public In inner; }\n"
            "public struct In { public byte T; public int V; }\n" + _program(
                "        return Marshal.SizeOf<In>() * 10"
                " + Marshal.SizeOf<Q>();"), 89)

    @needs_cc
    def test_shared_breaks_a_cycle(self):
        self.assert_both(
            "public class A { public B b; }\n"
            "[Shared]\n"
            "public class B { public int v; public A a; }\n" + _program(
                "        A x = new A();\n"
                "        return 1;"), 1)

    @needs_cc
    def test_a_method_local_of_a_later_class(self):
        # `Program` first is the usual C# file. Method bodies are emitted
        # with their class, so a later type they declare by value is moved
        # up as a field's would be.
        self.assert_both(
            "public class Program {\n"
            "    int Get(Row r) { return r.w; }\n"
            "    public int Run() {\n"
            "        Row r = new Row();\n"
            "        r.w = 4;\n"
            "        return Get(r);\n"
            "    }\n"
            "}\n"
            "public struct Row { public int w; }\n", 4)

    def test_a_later_local_with_a_base_is_refused(self):
        self.assert_refuses(
            "public class Program {\n"
            "    public int Run() { D d = new D(); return d.v; }\n"
            "}\n"
            "public class Base { public int k; }\n"
            "public class D : Base { public int v; }\n",
            "`Program` declares a `D`", "Declare `D` above `Program`")

    def test_in_order_code_is_unchanged(self):
        # Nothing to move, nothing moved: the output is what it was.
        cpp = cs2cpp.translate(PACKET, "t.cs")
        self.assertEqual(
            cpprust.translate(cpp, path="t.cs", clang=False),
            cpprust.translate(cpp, path="t.cs", clang=False, any_order=True))

    def test_a_later_type_with_a_base_is_refused(self):
        self.assert_refuses(
            "public class A { public D d; }\n"
            "public class Base { public int k; }\n"
            "public class D : Base { public int v; }\n",
            "`A.d`", "has a base", "Declare `D` above `A`")

    def test_a_cycle_is_refused(self):
        self.assert_refuses(
            "public class A { public B b; }\n"
            "public class B { public A a; }\n",
            "`A` -> `B` -> `A`", "[Shared]")

    def test_holding_itself_is_refused(self):
        self.assert_refuses("public struct A { public A a; }\n",
                            "`A` -> `A`")


_LIST_HEAD = "using System.Collections.Generic;\n"


class TestLists(unittest.TestCase):
    """`List<T>` members, lowered only where the receiver is a `List`.

    `Add` and `Count` are ordinary names -- a user class may have its own --
    so the receiver's type is resolved first: a local or parameter above it
    in the method, a `foreach` variable, else a field; then field by field
    and element by element. Each case runs under gcc and shivyc.
    """

    def assert_refuses(self, src, *needles):
        TestRefusals.assert_refuses(self, src, *needles)

    def assert_both(self, src, want, main=None):
        c = lower(src)
        self.assertEqual(run_c(c, main or _RUN_PROGRAM), want, "gcc")
        self.assertEqual(run_shivyc(c, main or _RUN_PROGRAM), want, "shivyc")

    def run_list(self, body, want, types=""):
        self.assert_both(_LIST_HEAD + types + _program(body), want)

    @needs_cc
    def test_add_count_and_index(self):
        self.run_list("        List<int> xs = new List<int>();\n"
                      "        xs.Add(3); xs.Add(4); xs.Add(5);\n"
                      "        return xs.Count * 20 + xs[0] * 10 + xs[2];", 95)

    @needs_cc
    def test_foreach_over_a_var_list(self):
        # Did not translate before: `var` became `auto`, which hid the
        # container from the C++ half's range-for.
        self.run_list("        var xs = new List<int>();\n"
                      "        xs.Add(2); xs.Add(3);\n"
                      "        int t = 0;\n"
                      "        foreach (var x in xs) { t += x; }\n"
                      "        return t;", 5)

    @needs_cc
    def test_clear_insert_and_remove_at(self):
        # `Insert` at `Count` appends, as in C#.
        self.run_list("        var xs = new List<int>();\n"
                      "        xs.Add(9); xs.Clear();\n"
                      "        xs.Add(1); xs.Add(3);\n"
                      "        xs.Insert(1, 2); xs.Insert(3, 4);\n"
                      "        xs.RemoveAt(0);\n"
                      "        return xs[0] * 100 + xs[1] * 10 + xs[2]"
                      " + xs.Count - 3;", 234)

    @needs_cc
    def test_contains_index_of_and_remove(self):
        self.run_list("        var xs = new List<int>();\n"
                      "        xs.Add(5); xs.Add(6); xs.Add(7);\n"
                      "        bool r = xs.Remove(6);\n"
                      "        int a = xs.Contains(7) ? 1 : 0;\n"
                      "        int b = xs.Contains(6) ? 1 : 0;\n"
                      "        return (r ? 100 : 0) + a * 10 + b"
                      " + xs.IndexOf(7) * 20;", 130)

    @needs_cc
    def test_enum_elements(self):
        self.run_list("        var ks = new List<Kind>();\n"
                      "        ks.Add(Kind.C); ks.Add(Kind.A);\n"
                      "        return ks.IndexOf(Kind.A) * 10"
                      " + (ks.Contains(Kind.B) ? 1 : 0);", 10,
                      "public enum Kind { A, B, C }\n")

    @needs_cc
    def test_a_field_this_and_another_object(self):
        self.run_list(
            "        Bag b = new Bag();\n"
            "        b.Put(1); b.Put(5);\n"
            "        b.items.Add(8);\n"
            "        return b.N() * 10 + b.items[3] + b.items[4];", 64,
            "public class Bag {\n"
            "    public List<int> items = new List<int>();\n"
            "    public void Put(int v) { items.Add(v); this.items.Add(v + 1); }\n"
            "    public int N() { return items.Count; }\n"
            "}\n")

    @needs_cc
    def test_an_auto_property_list_is_the_objects_own(self):
        # The getter returns the list by value; through it, `Add` and an
        # index write would change a copy and be lost. They reach the
        # storage instead, as C#'s reference does.
        self.run_list(
            "        Bag b = new Bag();\n"
            "        b.Items.Add(1); b.Items.Add(2);\n"
            "        b.Items[1] = 9;\n"
            "        b.Put(20);\n"
            "        int t = 0;\n"
            "        foreach (var x in b.Items) { t += x; }\n"
            "        return t + b.Items.Count;", 33,
            "public class Bag {\n"
            "    public List<int> Items { get; set; }\n"
            "    public void Put(int v) { Items.Add(v); }\n"
            "}\n")

    @needs_cc
    def test_adding_a_new_object(self):
        # A temporary has no address to pass; it is named, then moved in.
        # With an initializer, the name makes it a declaration, which the
        # initializer lowering takes.
        self.run_list("        var rows = new List<Row>();\n"
                      "        rows.Add(new Row { w = 4 });\n"
                      "        rows.Add(new Row());\n"
                      "        int t = 0;\n"
                      "        foreach (var r in rows) { t += r.w; }\n"
                      "        return rows.Count * 10 + t;", 24,
                      "public class Row { public int w; }\n")

    @needs_cc
    def test_adding_a_call_result_with_program_first(self):
        self.assert_both(
            _LIST_HEAD +
            "public class Program {\n"
            "    Row Make(int v) { Row r = new Row(); r.w = v; return r; }\n"
            "    public int Run() {\n"
            "        var rows = new List<Row>();\n"
            "        rows.Add(Make(6));\n"
            "        return rows[0].w;\n"
            "    }\n"
            "}\n"
            "public class Row { public int w; }\n", 6)

    @needs_cc
    def test_foreach_through_a_member(self):
        self.run_list(
            "        var rows = new List<Row>();\n"
            "        rows.Add(new Row()); rows.Add(new Row());\n"
            "        int t = 0;\n"
            "        foreach (var r in rows) { r.cells.Add(1); t += r.cells.Count; }\n"
            "        return t;", 2,
            "public class Row { public List<int> cells = new List<int>(); }\n")

    @needs_cc
    def test_a_user_add_and_count_are_not_a_lists(self):
        self.run_list("        Calc c = new Calc();\n"
                      "        int v = c.Add(2, 3);\n"
                      "        return v * 10 + c.Count;", 51,
                      "public class Calc {\n"
                      "    public int Count;\n"
                      "    public int Add(int a, int b) { Count += 1; return a + b; }\n"
                      "}\n")

    @needs_cc
    def test_a_bad_index_aborts(self):
        # `ArgumentOutOfRangeException`, unhandled. The vector's own
        # `erase` would ignore it silently.
        c = lower(_LIST_HEAD + _program("        var xs = new List<int>();\n"
                                        "        xs.Add(1);\n"
                                        "        xs.RemoveAt(5);\n"
                                        "        return 0;"))
        self.assertNotEqual(run_c(c, _RUN_PROGRAM), 0)

    @needs_cc
    def test_a_var_array_is_walkable(self):
        self.assert_both(_program("        var a = new int[3];\n"
                                  "        a[1] = 4;\n"
                                  "        int t = 0;\n"
                                  "        foreach (var x in a) { t += x; }\n"
                                  "        return t + a.Length;"), 7)

    def test_other_members_are_refused(self):
        self.assert_refuses(_LIST_HEAD + _program(
            "        var xs = new List<int>();\n        xs.Sort();\n"
            "        return 0;"), "`List.Sort`", "`Add`, `Insert`")

    def test_contains_on_a_class_is_refused(self):
        self.assert_refuses(
            _LIST_HEAD + "public class Item { public int w; }\n" + _program(
                "        var xs = new List<Item>();\n"
                "        Item it = new Item();\n"
                "        return xs.Contains(it) ? 1 : 0;"),
            "`List<Item>.Contains`", "`Equals`")

    def test_linq_count_is_refused(self):
        self.assert_refuses(_LIST_HEAD + _program(
            "        var xs = new List<int>();\n        return xs.Count();"),
            "`Count()`", "property")


_IN = ("public struct In {\n"
       "    public int V;\n"
       "    public int Get() { return V; }\n"
       "    public static int Zero() { return 0; }\n"
       "}\n")


class TestTypeNamedFields(unittest.TestCase):
    """`public In In;` -- a member named after a type, resolved as C# does.

    In `In.X`, an instance `X` means the field and a static one the type
    (the "Color Color" rule). The C++ half, seeing a type name, never
    qualified such a field with `this`, so `In.Get()` reached C unchanged.
    Each case runs under gcc and shivyc -- whose parser also had to learn
    that a member named like a typedef does not hide it
    (`feature_tests/struct_member_typedef_name.c`).
    """

    def assert_both(self, src, want, main=None):
        TestLists.assert_both(self, src, want, main)

    def run_q(self, q, want):
        self.assert_both(_IN + q + _program("        Q q = new Q();\n"
                                            "        return q.F();"), want)

    @needs_cc
    def test_a_call_through_the_field(self):
        self.run_q("public class Q {\n"
                   "    public In In;\n"
                   "    public int F() { In.V = 4; return In.Get() * 2; }\n"
                   "}\n", 8)

    @needs_cc
    def test_read_write_and_assign(self):
        self.run_q("public class Q {\n"
                   "    public In In;\n"
                   "    public int F() {\n"
                   "        In.V = 5;\n"
                   "        In other = new In();\n"
                   "        other.V = 1;\n"
                   "        In = other;\n"
                   "        return In.V + In.Get();\n"
                   "    }\n"
                   "}\n", 2)

    @needs_cc
    def test_a_static_member_means_the_type(self):
        self.run_q("public class Q {\n"
                   "    public In In;\n"
                   "    public int F() { In.V = 3; return In.Zero() + In.V; }\n"
                   "}\n", 3)

    @needs_cc
    def test_a_local_of_the_same_name_shadows_the_field(self):
        self.run_q("public class Q {\n"
                   "    public In In;\n"
                   "    public int F() { In.V = 1; return G() + In.V; }\n"
                   "    int G() { In In = new In(); In.V = 9; return In.V; }\n"
                   "}\n", 10)

    @needs_cc
    def test_a_field_named_after_another_class(self):
        self.assert_both(
            "public class Node {\n"
            "    public int v;\n"
            "    public Node Next() { Node n = new Node(); n.v = v + 1; return n; }\n"
            "}\n"
            "public class Chain {\n"
            "    public Node Node;\n"
            "    public int F() { Node.v = 2; Node x = Node.Next(); return x.v; }\n"
            "}\n" + _program("        Chain c = new Chain();\n"
                             "        return c.F();"), 3)

    @needs_cc
    def test_a_static_call_through_a_type(self):
        # Not lowered at all before: `Type.Method()` reached C as written.
        self.assert_both(_IN + _program("        return In.Zero() + 3;"), 3)

    @needs_cc
    def test_assigning_new_to_a_plain_struct_zeroes_it(self):
        # `x = new T()`, assigned rather than declared: C# zeroes it, and
        # the expression form `T()` is not C for a struct with no
        # constructor.
        c = lower(_IN + _program("        In a = new In();\n"
                                 "        a.V = 5;\n"
                                 "        a = new In();\n"
                                 "        return a.V + 2;"))
        self.assertEqual(run_c(c, _RUN_ON_DIRTY_STACK), 2)
        self.assertEqual(run_shivyc(c, _RUN_PROGRAM), 2)


class TestLiterals(unittest.TestCase):
    """C# literal spellings that are not C's."""

    @needs_cc
    def test_a_float_suffix_on_an_integer(self):
        # `2f` is a C# float; in C it is an invalid suffix on an integer
        # constant. `cs2cpp.lower_float_literals`, which unity_pack uses
        # for script bodies as well, gives it a decimal point.
        src = _program("        float a = 2f;\n"
                       "        float b = 1.5f;\n"
                       "        return (int)(a * 10 + b * 2 + 0F);")
        self.assertIn("2.f", cs2cpp.translate(src, "t.cs"))
        c = lower(src)
        self.assertEqual(run_c(c, _RUN_PROGRAM), 23)
        self.assertEqual(run_shivyc(c, _RUN_PROGRAM), 23)

    def test_strings_comments_and_real_literals_are_left_alone(self):
        self.assertEqual(
            cs2cpp.lower_float_literals(
                'a = 2f + 1.5f + 1e2f; s = "2f"; // 3f\n'),
            'a = 2.f + 1.5f + 1e2f; s = "2f"; // 3f\n')


class TestLowerBody(unittest.TestCase):
    """`cs2cpp.lower_body`: the C# language families, under an object model.

    unity_pack lowers each script method body with it before its Unity API
    rewrites, under `packed_model` (a reference is an instance index);
    `translate` runs it under `OWNED`. The golden corpus
    (`tools/unity_pack_golden.py`) checks the packed output end to end, but
    only for what its projects happen to use -- the packed `this` appears in
    one case -- so the families are pinned here directly.
    """

    PACKED = cs2cpp.packed_model(True)

    def test_packed_null_is_the_index_sentinel(self):
        self.assertEqual(
            cs2cpp.lower_body("if (c != null && d == null) {}", self.PACKED),
            "if (c != -1 && d == -1) {}")

    def test_without_objects_null_is_left_alone(self):
        self.assertEqual(
            cs2cpp.lower_body("if (c != null) {}", cs2cpp.packed_model(False)),
            "if (c != null) {}")

    def test_packed_booleans_and_this(self):
        self.assertEqual(
            cs2cpp.lower_body("hp = this.max; ok = true; Add(this); f = false;",
                              self.PACKED),
            "hp = max; ok = 1; Add(i); f = 0;")

    def test_owned_keeps_booleans_this_and_null(self):
        # csrust has `bool`, lowers `this.` to `this->`, and `null` to NULL,
        # each elsewhere in `translate`.
        text = "hp = this.max; ok = true; if (c == null) {} x = 2f;"
        self.assertEqual(cs2cpp.lower_body(text, cs2cpp.OWNED),
                         "hp = this.max; ok = true; if (c == null) {} x = 2.f;")

    def test_packed_string_locals(self):
        # `lower_local_types`: unity_pack runs it after its Unity rewrites,
        # which read some declarations as C# wrote them.
        # The packed model declares a marker, which unity_pack's last pass
        # turns into owned storage (a coost fastring) -- not `const char *`,
        # which pointed into scratch buffers later concatenations reused.
        self.assertEqual(
            cs2cpp.lower_local_types('string p = "string s";', self.PACKED),
            '%s p = "string s";' % cs2cpp.PACKED_STRING_LOCAL)
        self.assertEqual(
            cs2cpp.lower_local_types("string p = q;", cs2cpp.OWNED),
            "string p = q;")

    def test_packed_byte_arrays(self):
        model = cs2cpp.packed_model(True, byte_arrays=True)
        self.assertEqual(
            cs2cpp.lower_byte_arrays(
                "byte[] b = f(); n = b.Length + b[2];", model),
            "ByteArray b = f(); n = b.length + b.data[2];")
        # Without the engine's struct, `byte[]` stays the subset's array.
        self.assertEqual(
            cs2cpp.lower_byte_arrays("byte[] b = f();", self.PACKED),
            "byte[] b = f();")

    def test_packed_string_concatenation(self):
        # Each `+` with a string on its left becomes the engine's typed
        # helper, the operand's kind from `scalar_kind`. A literal further
        # along starts a concatenation of its own in the same pass, and a
        # later pass joins the pieces -- nested differently from a strict
        # left fold, and the same string, since concatenation associates.
        self.assertEqual(
            cs2cpp.lower_string_concat(
                'p = "hp=" + 3 + " x=" + x + " c=" + \'c\';', self.PACKED),
            'p = _str_plus_s(_str_plus_s(_str_plus_i("hp=", (3)), '
            '(_str_plus_f(" x=", (x)))), (_str_plus_c(" c=", (\'c\'))));')
        self.assertEqual(
            cs2cpp.lower_string_concat(
                "p = Application_dataPath() + name;", self.PACKED,
                string_idents={"name"}),
            "p = _str_plus_s(Application_dataPath(), (name));")
        # No helper, no rewrite: csrust's `+` is its own business.
        self.assertEqual(
            cs2cpp.lower_string_concat('p = "a" + 1;', cs2cpp.OWNED),
            'p = "a" + 1;')

    def test_scalar_kind(self):
        kind = lambda e, ids=None: cs2cpp.scalar_kind(e, self.PACKED, ids)
        self.assertEqual([kind('"s"'), kind("'c'"), kind("-12"), kind("x"),
                          kind("StreamReader_ReadLine(r)"), kind("(n)", {"n"})],
                         ["s", "c", "i", "f", "s", "s"])

    def test_packed_collections(self):
        model = cs2cpp.packed_model(True, elem_type=lambda t: {
            "Enemy": "int", "double": "float"}.get(t, t))
        text, names = cs2cpp.lower_list_types(
            "List<Enemy> es = new List<Enemy>(); var ds = new List<double>();",
            model)
        self.assertEqual(text, "std::vector<int> es; var ds = std::vector<float>();")
        self.assertEqual(names, {"es"})
        self.assertEqual(
            cs2cpp.lower_list_members_named(
                "es.Add(e); n = es.Count; es.Clear();", names),
            "es.push_back(e); n = es.size(); es.clear();")
        text, names = cs2cpp.lower_map_types(
            "Dictionary<string, int> hp = new Dictionary<string, int>();",
            cs2cpp.packed_model(True))
        self.assertEqual(text, "std::map<string, int> hp;")

    def test_string_keys_survive(self):
        # A replacement that copies an operand reads it from the text, not
        # the blanked scan it matched on: a string key used to come out as
        # spaces (`"Blaster Shoot"` -> `"             "`).
        out = cs2cpp.lower_map_members_named(
            'hp.Add("Blaster Shoot", 3); ok = hp.ContainsKey("a b");',
            {"hp"}, {"hp": "std::string"})
        self.assertEqual(
            out, '{ std::string __dk = "Blaster Shoot"; hp[__dk] = 3; }; '
                 'ok = (hp.count("a b") != 0);')
        self.assertEqual(
            cs2cpp.lower_map_string_index(
                'x = hp["Blaster Shoot"];', r"(?<![.\w])hp", self.PACKED),
            'x = (*_engine_map_at_si(hp, "Blaster Shoot"));')

    def test_csrust_and_unity_share_list_spellings(self):
        self.assertEqual(cs2cpp.LIST_METHODS,
                         {"Add": "push_back", "Clear": "clear", "Count": "size"})
        self.assertIn(cs2cpp.LIST_METHODS["Add"], cs2cpp._LIST_MEMBERS["Add"][1])

    def _fields(self, text, handles=None):
        return cs2cpp.lower_packed_fields(
            text, "Coin", {"hp", "speed", "target"}, {"MAX"},
            handles or {}, self.PACKED)

    def test_packed_field_reads_and_writes(self):
        self.assertEqual(
            self._fields("hp = hp + MAX; speed += 2.f; hp++; --hp;"),
            "Coin_set_hp(i, Coin_get_hp(i) + Coin_MAX); "
            "Coin_set_speed(i, Coin_get_speed(i) + 2.f); "
            "Coin_set_hp(i, Coin_get_hp(i) + 1); "
            "Coin_set_hp(i, Coin_get_hp(i) - 1);")

    def test_two_writes_on_one_line(self):
        # unity_pack rewrote a write's prefix and closed the paren at the end
        # of the line: one `)` for two writes, and C that did not compile.
        self.assertEqual(self._fields("hp = 1; speed = f(2, 3);"),
                         "Coin_set_hp(i, 1); Coin_set_speed(i, f(2, 3));")

    def test_comparisons_are_reads(self):
        self.assertEqual(self._fields("if (hp == 0 || hp <= MAX) {}"),
                         "if (Coin_get_hp(i) == 0 || Coin_get_hp(i) <= Coin_MAX) {}")

    def test_a_handle_field(self):
        # `target` indexes another class's instances: reached through that
        # class's slot. (No golden case uses one yet.)
        self.assertEqual(
            self._fields("d = target.hp;", {"target": "Enemy"}),
            "d = Enemy_AT(Coin_get_target(i)).hp;")

    def test_field_names_in_strings_stay(self):
        self.assertEqual(self._fields('Debug_Log_s("hp is low");'),
                         'Debug_Log_s("hp is low");')

    def _collections(self, text):
        owner = cs2cpp.PackedClass(
            "Bag", "Bag", static_lists=[("all", "int")],
            inst_lists=[("items", "int")], inst_maps=[("tags", "string", "int")],
            field_types={"board": "Board", "hp": ""})
        board = cs2cpp.PackedClass(
            "Board", "Board", static_lists=[("pieces", "int")],
            inst_maps=[("cells", "int", "int")])
        model = cs2cpp.packed_model(True, elem_type=lambda t: {
            "string": "std::string"}.get(t, t))
        return cs2cpp.lower_packed_collections(text, owner, [owner, board], model)

    def test_packed_instance_collections_alias_their_slot(self):
        self.assertEqual(
            self._collections("items.Add(1); n = items.Count;"),
            "std::vector<int> &items = Bag_items[i];\n"
            "items.push_back(1); n = items.size();")

    def test_another_class_static_list(self):
        self.assertEqual(self._collections("Board.pieces.Add(3); k = Board.pieces.Count;"),
                         "Board_pieces.push_back(3); k = Board_pieces.size();")

    def test_another_class_instance_map(self):
        # A field the plan says holds a Board, and an unknown receiver: taken.
        self.assertEqual(self._collections("c = board.cells[2];"),
                         "c = Board_cells[board][2];")
        self.assertEqual(self._collections("c = b.cells[2];"),
                         "c = Board_cells[b][2];")

    def test_a_receiver_of_another_type_is_left_alone(self):
        # unity_pack rewrote any `x.cells`, whatever `x` was.
        self.assertEqual(
            self._collections("Grid g = MakeGrid(); c = g.cells[2];"),
            "Grid g = MakeGrid(); c = g.cells[2];")

    def test_a_string_keyed_instance_map(self):
        self.assertEqual(
            self._collections('tags.Add("a", 1); t = tags["a"];'),
            "std::map<std::string, int> &tags = Bag_tags[i];\n"
            # The `Add` expansion's own indexer goes through the helper
            # too: it returns the slot's address, so the write lands.
            '{ std::string __dk = "a"; (*_engine_map_at_si(tags, __dk)) = 1; }; '
            't = (*_engine_map_at_si(tags, "a"));')

    def test_strings_and_comments_are_not_code(self):
        # unity_pack's regexes rewrote these too: `"is this true"` came out
        # `"is i 1"`. Matched on a blanked scan, they are left as written.
        text = 's = "is this true == null"; // this false\nok = true;'
        self.assertEqual(cs2cpp.lower_body(text, self.PACKED),
                         's = "is this true == null"; // this false\nok = 1;')


class TestBindings(unittest.TestCase):
    """`cs2cpp.lower_bindings`: a library's API as a table, applied here.

    unity_pack declares UnityEngine's this way; the table is its knowledge,
    the rewriting is cs2cpp's, with the same boundaries for every entry.
    """

    B = cs2cpp.Binding

    def test_forms(self):
        B = self.B
        table = [B("File.Exists", "File_Exists", namespaces=("System.IO",)),
                 B("Application.dataPath", "Application_dataPath", "getter"),
                 B("Time.time", "Time_time", "value"),
                 B("print", "Debug_Log", "callee"),
                 B("Application.Quit", "Application_Quit",
                   no_args="Application_Quit(0)")]
        self.assertEqual(
            cs2cpp.lower_bindings(
                "a = System.IO.File.Exists(p); b = Application.dataPath; "
                "c = Time.time; print (c); Application.Quit(); "
                "Application.Quit(2);", table),
            "a = File_Exists(p); b = Application_dataPath(); "
            "c = Time_time; Debug_Log (c); Application_Quit(0); "
            "Application_Quit(2);")

    def test_boundaries(self):
        # unity_pack's one-off patterns: `Time.time` by plain text replace
        # took the front of `Time.timeScale`, and `File\\.Exists` had no left
        # boundary, so it took the back of `MyFile.Exists`.
        B = self.B
        self.assertEqual(
            cs2cpp.lower_bindings(
                's = Time.timeScale; t = MyFile.Exists(p); u = "Time.time";',
                [B("Time.time", "Time_time", "value"),
                 B("File.Exists", "File_Exists")]),
            's = Time.timeScale; t = MyFile.Exists(p); u = "Time.time";')

    def test_callee_needs_a_call(self):
        self.assertEqual(
            cs2cpp.lower_bindings("int print = 1; print(2);",
                                  [self.B("print", "Debug_Log", "callee")]),
            "int print = 1; Debug_Log(2);")


class TestResidualCSharp(unittest.TestCase):
    """`cs2cpp.residual_csharp`: the C# a lowered body still holds.

    unity_pack stubs (and reports) a method whose body keeps any; the
    language questions are asked here, and the engine says only what is
    its own C.
    """

    M = cs2cpp.packed_model(True)

    def left(self, text, **kw):
        r = cs2cpp.residual_csharp(text, self.M, **kw)
        return r[1] if r else None

    def test_leftover_csharp_is_named(self):
        self.assertEqual(self.left("Renderer[] rs = f();"), "Renderer[] rs")
        self.assertEqual(self.left("x = Get<Foo>(1);"), "Get<Foo>(")
        self.assertEqual(self.left("int k = 1;\nOn(() => { go(); });"),
                         "On(() => { go(); });")
        self.assertEqual(self.left("End();"), "End(")
        self.assertEqual(self.left("x = Unknown.DoThing(1);"), "Unknown.DoThing(")
        self.assertEqual(self.left("v = Vector3.zero;"), "Vector3.zero")
        self.assertEqual(self.left("f().Go;"), ").G")

    def test_lowered_c_is_not_csharp(self):
        self.assertIsNone(self.left(
            "Coin_set_hp(i, Enemy_AT(Coin_get_target(i)).hp);\n"
            "items.push_back(3); n = items.size();\n"
            'Debug_Log_s("see Objects (Scripts)/Player.cs");'))

    def test_the_engine_says_what_is_its_own(self):
        body = "Matrix4x4 m = Get_m(i);\nfloat a = m.m00;\nv = Vector2Int(1, 2);"
        self.assertIsNotNone(self.left(body))
        self.assertIsNone(self.left(body, known_types={"Matrix4x4"},
                                    value_ctors=("Vector2Int",)))

    def test_the_instance_accessor_comes_from_the_model(self):
        # Under a model without one, `X_AT(..).f` is just a chained call.
        self.assertIsNone(self.left("a = Enemy_AT(k).hp;"))
        self.assertIsNotNone(cs2cpp.residual_csharp("a = Enemy_AT(k).hp;",
                                                    cs2cpp.OWNED))


class TestCodeSub(unittest.TestCase):
    """`cs2cpp.code_sub`: `re.sub` that rewrites code, not what it prints."""

    def test_like_re_sub_in_code(self):
        self.assertEqual(
            cs2cpp.code_sub(r"(\w+)\s*=\s*(\d+)", r"\2 := \1", "x = 3; y = 4;"),
            re.sub(r"(\w+)\s*=\s*(\d+)", r"\2 := \1", "x = 3; y = 4;"))
        self.assertEqual(
            cs2cpp.code_sub(r"a(\d)", lambda m: "<%s>" % m.group(1), "a1 a2",
                            count=1), "<1> a2")

    def test_strings_and_comments_are_left_alone(self):
        self.assertEqual(
            cs2cpp.code_sub(r"transform\.position\.x", "P_get_x(i)",
                            'a = transform.position.x; '
                            's = "transform.position.x"; // transform.position.x'),
            'a = P_get_x(i); s = "transform.position.x"; // transform.position.x')

    def test_a_literal_is_still_read(self):
        # A rewrite that needs a string's contents gets them.
        self.assertEqual(
            cs2cpp.code_sub(r'Find\("([^"]*)"\)', r"Find_\1()", 'g = Find("Enemy");'),
            "g = Find_Enemy();")


class TestDigest(unittest.TestCase):
    """C# joins the same --emit-decls digest as C++ / rpython (CPPRPY.md)."""

    def test_emit_decls_names_the_class(self):
        import json
        tmp = tempfile.mkdtemp(prefix="csdecls-")
        try:
            src = os.path.join(tmp, "shape.cs")
            with open(src, "w") as f:
                f.write("public class Shape {\n"
                        "    public virtual int Area() { return 0; }\n"
                        "}\n")
            out_c = os.path.join(tmp, "shape.c")
            decls = os.path.join(tmp, "shape.decls.json")
            proc = subprocess.run(
                [sys.executable, "tools/csrust.py", src, "-o", out_c,
                 "--emit-decls", decls],
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            self.assertTrue(os.path.isfile(decls))
            with open(decls) as f:
                data = json.load(f)
            # Digest shape matches cpprust: a list/dict of class records.
            blob = json.dumps(data)
            self.assertIn("Shape", blob)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)


def _run_main(cs, opt="-O0"):
    """Lower a C# program whose `static int Main()` is the check, build it
    at @opt and return Main's result."""
    c = lower(cs)
    tmp = tempfile.mkdtemp(prefix="csrust-")
    try:
        path = os.path.join(tmp, "t.c")
        with open(path, "w") as f:
            f.write(c + "\nint main(void) { return Program_Main(); }\n")
        exe = os.path.join(tmp, "t")
        proc = subprocess.run([_CC, "-w", opt, "-o", exe, path, "-lm"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise AssertionError("generated C did not compile:\n%s\n%s"
                                 % (proc.stderr.decode("utf-8", "replace"), c))
        return subprocess.run([exe], timeout=20).returncode
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_cc
class TestMonoParity(unittest.TestCase):
    """Programs checked against Mono: each `Main` returns 0 under `mono`, and
    must here. Each one was found returning something else, or not
    compiling, by running the same source under both."""

    def test_static_methods_and_fields(self):
        # static calls went through a `this` that a static method lacks,
        # and `static int n;` sat inside the struct
        self.assertEqual(_run_main("""
public class Counter {
    public static int Count;
    public static readonly int R = 4;
    public Counter() { Count++; }
    public static int Twice() { return Count * 2; }
}
public class Program {
    static int Fib(int n) { return n < 2 ? n : Fib(n - 1) + Fib(n - 2); }
    public static int Main() {
        Counter a = new Counter(); Counter b = new Counter();
        if (Counter.Count != 2) return 1;
        Counter.Count += 3; if (Counter.Twice() != 10) return 2;
        if (Counter.R != 4) return 3;
        if (Fib(15) != 610) return 4;
        return 0;
    }
}
"""), 0)

    def test_fields_start_at_zero(self):
        # a constructor that set one field left the others as stack garbage;
        # so did a class with no constructor once it held a List; and a
        # class with initializers but no constructor was zeroed in place of
        # constructed, losing them
        self.assertEqual(_run_main("""
using System.Collections.Generic;
public class P { public int a; public long b; public bool c; public double d;
                 public P(int x) { a = x; } }
public class M { public int a; public List<int> xs = new List<int>(); }
public class Q { public int A { get; set; } public int n; public Q(int k) { n = k; } }
public class W { public int w = 3; public long k = 5; }
public class Program {
    public static int Main() {
        P p = new P(5);
        if (p.a != 5 || p.b != 0 || p.c || p.d != 0.0) return 1;
        M m = new M(); if (m.a != 0) return 2;
        Q q = new Q(1); if (q.A != 0) return 3;
        W w = new W(); if (w.w != 3 || w.k != 5) return 4;
        return 0;
    }
}
"""), 0)

    def test_properties_with_accessors(self):
        self.assertEqual(_run_main("""
public class C {
    private int _n;
    private int w = 3;
    public int N {
        get { return _n; }
        set { _n = value * 2; }
    }
    public int W { get => w; private set => w = value; }
    public int Area => w * 2;
    public int A { get; set; } = 7;
}
public class Program {
    public static int Main() {
        C c = new C();
        c.N = 3; if (c.N != 6) return 1;
        if (c.W != 3 || c.Area != 6) return 2;
        if (c.A != 7) return 3;
        c.A++; c.A += 2; --c.A; if (c.A != 9) return 4;
        c.N += 1; if (c.N != 14) return 5;
        if (c.N == 13) return 6;
        return 0;
    }
}
"""), 0)

    def test_integer_arithmetic_wraps_and_shifts_mask_under_O2(self):
        # C# wraps and masks shift counts; C leaves both undefined, and gcc
        # -O2 folded `1 << 33` to 0 and turned a wrapping loop infinite
        src = """
public class Program {
    public static int Main() {
        int big = 2147483647; int w = big + 1;
        if (w != -2147483648) return 1;
        int one = 1; int n = 33; long L = 1; byte bt = 1;
        if ((one << n) != 2) return 2;
        if ((L << 65) != 2) return 3;
        if ((bt << n) != 2) return 4;
        if ((one << n + 1) != 4) return 5;
        int v = 1; v <<= n; if (v != 2) return 6;
        if ((-64 >> n) != -32) return 7;
        int c = 0;
        for (int i = 2147483600; i > 0; i += 100) c++;
        if (c != 1) return 8;
        return 0;
    }
}
"""
        for opt in ("-O0", "-O2"):
            self.assertEqual(_run_main(src, opt), 0, opt)

    def test_unbraced_foreach(self):
        self.assertEqual(_run_main("""
using System.Collections.Generic;
public class Program {
    public static int Main() {
        List<int> xs = new List<int>(); xs.Add(3); xs.Add(4);
        int s = 0;
        foreach (int v in xs) s += v;
        return s == 7 ? 0 : 1;
    }
}
"""), 0)

    def test_default_parameter_on_a_static_method(self):
        self.assertEqual(_run_main("""
public class Program {
    static int F(int a, int b = 5) { return a + b; }
    public static int Main() { return (F(1) == 6 && F(1, 2) == 3) ? 0 : 1; }
}
"""), 0)


class TestRefusedInCSharpTerms(unittest.TestCase):
    """Each reached the C compiler as invalid C; each is now refused, in C#
    terms, at its C# line."""

    def check(self, body, fragment, line):
        msg = refusal(body)
        self.assertIn(fragment, msg)
        self.assertIn("test.cs:%d:" % line, msg)

    def test_string(self):
        self.check("public class P {\n  public int F() { string s = \"a\"; return 0; }\n}\n",
                   "`string` is not in the C# subset", 2)

    def test_base_call(self):
        self.check("public class A { public virtual int F() { return 1; } }\n"
                   "public class B : A { public override int F() { return base.F(); } }\n",
                   "`base.F`", 2)

    def test_is(self):
        self.check("public class A { }\npublic class P {\n"
                   "  public bool F(A a) { return a is A; } }\n", "`is`", 3)

    def test_named_arguments(self):
        self.check("public class P { static int F(int a) { return a; }\n"
                   "  static int G() { return F(a: 1); } }\n", "named arguments", 2)

    def test_generic_method(self):
        self.check("public class P {\n  static T Id<T>(T x) { return x; } }\n",
                   "generic method `Id<..>`", 2)

    def test_slicing_into_a_base(self):
        self.check("public class A { }\npublic class B : A { }\npublic class P {\n"
                   "  public void F() { A a = new B(); } }\n",
                   "would hold only the `A` part", 4)

    def test_a_static_with_an_initializer_is_lowered(self):
        # run before first use by the class's __cctor (TestStaticInit)
        lower("public class P {\n  public static int[] T = new int[3];\n}\n")


@needs_cc
class TestMonoParityII(unittest.TestCase):
    """Part II, checked against Mono the same way. A throw in C# is an
    abort here: the program ends either way, and nothing reads a value that
    is not there."""

    DICT = """
using System.Collections.Generic;
public class Program {
    public static int Main() {
        Dictionary<int, int> d = new Dictionary<int, int>();
        %s
    }
}
"""

    def test_dictionary_members(self):
        self.assertEqual(_run_main(self.DICT % """
        d[1] = 10; d[2] = 20; d[1] = 11;
        if (d.Count != 2 || d[1] != 11) return 1;
        if (!d.ContainsKey(2) || d.ContainsKey(3)) return 2;
        if (!d.Remove(2) || d.Remove(2)) return 3;
        d[1] += 3; d[1]++; if (d[1] != 15) return 4;
        d.Add(7, 1); if (d[7] != 1) return 5;
        d.Clear(); if (d.Count != 0) return 6;
        return 0;"""), 0)

    def test_dictionary_missing_key_fails(self):
        # a read, a compound assignment, and a duplicate `Add` all throw in
        # C#; through `operator[]` the first two were silently 0
        for body in ("int v = d[7]; return 3;", "d[5] += 3; return 3;",
                     "d.Add(1, 1); d.Add(1, 2); return 3;"):
            self.assertNotIn(_run_main(self.DICT % body), (0, 3), body)

    def test_interface_parameters_borrow(self):
        self.assertEqual(_run_main("""
public interface IShape { int Area(); int Grow(int k); }
public class Sq : IShape { public int s; public Sq(int x) { s = x; }
    public int Area() { return s * s; } public int Grow(int k) { s += k; return s; } }
public class Program {
    static int Bump(IShape a, int k) { return a.Grow(k); }
    static int Twice(IShape a) { return Bump(a, 1) + a.Area(); }
    public static int Main() {
        Sq q = new Sq(3);
        if (Bump(q, 2) != 5 || q.s != 5) return 1;
        if (Twice(q) != 6 + 36) return 2;
        return 0;
    }
}
"""), 0)

    def test_field_initializers_and_base_constructor(self):
        self.assertEqual(_run_main("""
public class Inner { public int v; public Inner(int x) { v = x; } }
public class A { public int a; public A(int x) { a = x; } }
public class B : A {
    public Inner p = new Inner(5);
    public int b;
    public B(int x) : base(x + 1) { b = x + p.v; }
}
public class Outer {
    public class Nested { public int v; public Nested(int x) { v = x; } }
    public Nested n = new Nested(3);
}
public class Program {
    public static int Main() {
        B o = new B(2);
        if (o.a != 3 || o.b != 7) return 1;
        Outer t = new Outer(); if (t.n.v != 3) return 2;
        return 0;
    }
}
"""), 0)

    def test_new_of_nested_generics(self):
        self.assertEqual(_run_main("""
public struct Box<T> { public T v; }
public struct Pair<A, B> { public A a; public B b; }
public class Cell<T> { public T v; public Cell() { } }
public class Program {
    public static int Main() {
        Box<Box<int>> bb = new Box<Box<int>>(); bb.v.v = 5;
        Pair<int, Box<long>> pr = new Pair<int, Box<long>>(); pr.b.v = 7;
        Cell<Cell<int>> cc = new Cell<Cell<int>>(); cc.v = new Cell<int>(); cc.v.v = 4;
        return (bb.v.v == 5 && pr.b.v == 7 && cc.v.v == 4) ? 0 : 1;
    }
}
"""), 0)

    def test_lists_of_lists_and_dictionaries_of_lists(self):
        self.assertEqual(_run_main("""
using System.Collections.Generic;
public class Program {
    public static int Main() {
        List<List<int>> g = new List<List<int>>();
        g.Add(new List<int>()); g.Add(new List<int>());
        g[0].Add(7); g[1].Add(8); g[1].Add(9);
        int s = 0; foreach (List<int> row in g) foreach (int x in row) s += x;
        if (s != 24 || g[1][1] != 9) return 1;
        g.RemoveAt(0); if (g.Count != 1 || g[0].Count != 2) return 2;
        Dictionary<int, List<int>> d = new Dictionary<int, List<int>>();
        d[1] = new List<int>(); d[1].Add(5);
        if (d[1].Count != 1 || d[1][0] != 5) return 3;
        return 0;
    }
}
"""), 0)


class TestRefusedInCSharpTermsII(unittest.TestCase):
    def check(self, body, fragment, line):
        msg = refusal(body)
        self.assertIn(fragment, msg)
        self.assertIn("test.cs:%d:" % line, msg)

    def test_dictionary_member_not_lowered(self):
        self.check("using System.Collections.Generic;\npublic class P { public int F() {\n"
                   "  Dictionary<int, int> d = new Dictionary<int, int>();\n"
                   "  return d.Keys.Count; } }\n", "`Dictionary.Keys`", 4)

    def test_interface_local(self):
        self.check("public interface I { int F(); }\npublic class C : I { public int F() { return 1; } }\n"
                   "public class P { public int G() { C c = new C(); I i = c; return 0; } }\n",
                   "`I i`: an interface-typed", 3)

    def test_constructor_chaining_to_this(self):
        self.check("public class A { public int a;\n  public A(int x) { a = x; }\n"
                   "  public A() : this(4) { } }\n", "`: this(..)`", 3)



@needs_cc
class TestRefOut(unittest.TestCase):
    """`ref` / `out` parameters, checked against Mono: the callee writes the
    caller's local, struct, array element or field."""

    def test_ref_and_out(self):
        self.assertEqual(_run_main("""
public struct P { public int x; public int y; }
public class Box { public int v; }
public class Program {
    static void Swap(ref int a, ref int b) { int t = a; a = b; b = t; }
    static bool TryHalf(int n, out int half) { if (n % 2 != 0) { half = 0; return false; } half = n / 2; return true; }
    static void Bump(ref P p) { p.x += 1; p.y += 2; }
    static void Twice(ref int a) { Inc(ref a); Inc(ref a); }
    static void Inc(ref int a) { a++; }
    static void Set(ref int a) { a = 7; }
    public static int Main() {
        int a = 1, b = 2;
        Swap(ref a, ref b);
        if (a != 2 || b != 1) return 1;
        int h;
        if (!TryHalf(10, out h) || h != 5) return 2;
        if (TryHalf(7, out h) || h != 0) return 3;
        P p = new P(); Bump(ref p); if (p.x != 1 || p.y != 2) return 4;
        int c = 0; Twice(ref c); if (c != 2) return 5;
        int[] arr = new int[3]; Set(ref arr[1]); if (arr[1] != 7) return 6;
        Box bx = new Box(); Inc(ref bx.v); if (bx.v != 1) return 7;
        return 0;
    }
}
"""), 0)



@needs_cc
class TestStaticInit(unittest.TestCase):
    """Static field initializers and a static constructor, run before first
    use by a guarded `__cctor` -- they were refused (C has no code at
    startup). Checked against Mono."""

    def test_initializers_then_static_constructor(self):
        self.assertEqual(_run_main("""
using System.Collections.Generic;
public class Counter {
    public static int made;
    public static List<int> log = new List<int>();
    public static int start = Seed() * 2;
    static int Seed() { return 21; }
    static Counter() { log.Add(start); }
    public int id;
    public Counter() { made++; id = made; log.Add(id); }
}
public class Registry {
    public static int[] table = new int[4];
    public static int Get(int i) { return table[i]; }
}
public class Program {
    public static int Main() {
        if (Counter.start != 42) return 1;
        Counter a = new Counter(); Counter b = new Counter();
        if (Counter.made != 2) return 2;
        if (Counter.log.Count != 3 || Counter.log[0] != 42 || Counter.log[2] != 2) return 3;
        Registry.table[2] = 5;
        if (Registry.Get(2) != 5 || Registry.table.Length != 4) return 4;
        return 0;
    }
}
"""), 0)


@needs_cc
class TestSharedSameLine(unittest.TestCase):
    """`[Shared] public class Node` -- the attribute on the class's line was
    recognised (the class became shared) but not stripped, and reached the
    C. Aliasing through it, checked against Mono."""

    def test_same_line_attribute_and_aliasing(self):
        self.assertEqual(_run_main("""
[Shared] public class Node { public int v; public Node next; public Node(int x) { v = x; } }
public class Program {
    public static int Main() {
        Node a = new Node(1); Node b = new Node(2); a.next = b;
        Node x = b; x.v = 9;
        return a.next.v == 9 ? 0 : 1;
    }
}
"""), 0)



@needs_cc
class TestArenaClasses(unittest.TestCase):
    """`[MaxInstances(N)] class T`: reference semantics from an arena. A
    reference is a plain pointer -- assignment copies it, null is 0, ==
    compares references -- and `new` takes the next of N static slots;
    `T__arena_reset()` releases them all, and an (N+1)th live one aborts.
    The graph idioms of Unity-2D-Destruction's Delaunay library, checked
    against Mono."""

    GRAPH = 'using System.Collections.Generic;\npublic class MaxInstancesAttribute : System.Attribute {\n    public MaxInstancesAttribute(int n) {}\n}\n[MaxInstances(16)]\npublic class Node {\n    public int v;\n    public Node next;\n    public Node prev;\n    public Node(int x) { v = x; }\n    public void LinkAfter(Node n) { n.next = next; n.prev = this; if (next != null) next.prev = n; next = n; }\n}\npublic class Program {\n    static Node First(Node n) { while (n.prev != null) n = n.prev; return n; }\n    public static int Main() {\n        Node a = new Node(1); Node b = new Node(2); Node c = new Node(3);\n        a.LinkAfter(c); a.LinkAfter(b);                    // a -> b -> c\n        int s = 0; Node p = a;\n        while (p != null) { s = s * 10 + p.v; p = p.next; }\n        if (s != 123) return 1;\n        if (c.prev != b || b.prev != a || a.prev != null) return 2;\n        Node alias = b; alias.v = 9;\n        if (a.next.v != 9) return 3;\n        if (First(c) != a) return 4;\n        List<Node> l = new List<Node>(); l.Add(a); l.Add(c);\n        if (l[1].prev.v != 9) return 5;\n        l.Remove(a);\n        if (l.Count != 1 || l[0] != c) return 6;\n        Node[] arr = new Node[3]; arr[1] = b;\n        if (arr[0] != null || arr[1].v != 9) return 7;\n        Node q = a.next.next; if (q != c) return 8;\n        return 0;\n    }\n}\n'

    def test_graph_idioms_match_mono(self):
        self.assertEqual(_run_main(self.GRAPH), 0)

    def test_the_attribute_class_is_dropped(self):
        out = lower(self.GRAPH)
        self.assertNotIn("MaxInstancesAttribute", out)
        self.assertIn("Node__arena[16]", out)

    def test_exhaustion_aborts_and_reset_releases(self):
        src = """
public class MaxInstancesAttribute : System.Attribute { public MaxInstancesAttribute(int n) {} }
[MaxInstances(3)]
public class Cell { public int v; public Cell(int x) { v = x; } }
public class Program {
    public static int Fill(int k) { int s = 0; for (int i = 0; i < k; i++) { Cell c = new Cell(i); s += c.v; } return s; }
}
"""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "cap.c"), "w") as f:
            f.write(lower(src))
        with open(os.path.join(d, "host.c"), "w") as f:
            f.write('#include <stdio.h>\n#include <signal.h>\n#include <stdlib.h>\n'
                    '#include <unistd.h>\n#include "cap.c"\n'
                    'static void on_abort(int s) { (void)s; write(1, "aborted\\n", 8); _exit(0); }\n'
                    'int main(void) { signal(SIGABRT, on_abort);\n'
                    '  printf("fill %d\\n", Program_Fill(3)); Cell__arena_reset();\n'
                    '  printf("again %d\\n", Program_Fill(3)); fflush(stdout);\n'
                    '  Program_Fill(4); printf("NOT REACHED\\n"); return 1; }\n')
        exe = os.path.join(d, "host")
        r = subprocess.run([_CC, "-w", "-o", exe, os.path.join(d, "host.c")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        out = subprocess.run([exe], capture_output=True, text=True, timeout=30).stdout
        self.assertEqual(out.split(), ["fill", "3", "again", "3", "aborted"])


@needs_cc
class TestArenaPool(unittest.TestCase):
    """A static pool of arena references -- `static Stack<Edge> _pool = new
    Stack<Edge>()`, the Delaunay library's: its initializer is run by the
    class's __cctor (the type's `*` hid the field from that pass)."""

    # KNOWN GAP: `.Count` on a *static* `List<Edge>` of arena references is
    # not lowered yet (`vector_Edge_P` has no member `Count`); the same
    # members on a local list work (TestArenaClasses). The initializer
    # itself is run -- what this commit fixed.
    @unittest.expectedFailure
    def test_static_list_of_references(self):
        self.assertEqual(_run_main("""
using System.Collections.Generic;
public class MaxInstancesAttribute : System.Attribute { public MaxInstancesAttribute(int n) {} }
[MaxInstances(8)]
public class Edge {
    static List<Edge> _pool = new List<Edge>();
    public int id;
    public static Edge Create(int i) { Edge e; if (_pool.Count > 0) { e = _pool[_pool.Count - 1]; _pool.RemoveAt(_pool.Count - 1); } else { e = new Edge(); } e.id = i; return e; }
    public void Dispose() { _pool.Add(this); }
}
public class Program {
    public static int Main() {
        Edge a = Edge.Create(1); a.Dispose();
        Edge b = Edge.Create(2);
        return (b == a && b.id == 2) ? 0 : 1;      // the pooled object, reused
    }
}
"""), 0)
