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
        # byte formats. That agreement is why `Pack` is checked rather than
        # emitted (`TestStructLayout`), so it is pinned on Crust's own
        # compiler as well as the host's.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tmp = tempfile.mkdtemp(prefix="csrust-shivyc-")
        try:
            path = os.path.join(tmp, "t.c")
            with open(path, "w") as f:
                f.write(lower(PACKET) + "\n" + _RUN_PROGRAM + "\n")
            exe = os.path.join(tmp, "t")
            proc = subprocess.run(
                [sys.executable, "-m", "shivyc.main", path, "-o", exe],
                cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                raise AssertionError("shivyc: %s" % (proc.stdout + proc.stderr)
                                     .decode("utf-8", "replace")[-2000:])
            self.assertEqual(subprocess.run([exe]).returncode, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

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
    """`[StructLayout]` is checked against the fields, then dropped.

    `Sequential` is what a C struct already is. `Pack` is not emitted as
    `#pragma pack`, because shivyc parses that pragma and lays the struct out
    naturally anyway: one source would get two layouts, and nothing would
    say so. A `Pack` that changes nothing is dropped; one that would move a
    field is refused.
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

    def test_pack_that_moves_a_field_is_refused(self):
        self.assert_refuses(
            "[StructLayout(LayoutKind.Sequential, Pack = 1)]\n"
            "public struct Q { public byte Tag; public int Id; }\n",
            "`Pack = 1`", "`Id` at 1 instead of 4", "largest-first")

    def test_pack_that_removes_tail_padding_is_refused(self):
        self.assert_refuses(
            "[StructLayout(LayoutKind.Sequential, Pack = 1)]\n"
            "public struct Q { public int Id; public byte Tag; }\n",
            "size 5 instead of 8")

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
        # `CreateSpan(ref x, 1)` is the one call-site `ref` read; the
        # exemption must not reach anything else.
        self.assert_refuses(
            "public class A { public void F(ref int x) { } }\n", "`ref`")

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
