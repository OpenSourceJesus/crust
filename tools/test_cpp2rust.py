#!/usr/bin/env python3
"""Tests for tools/cpp2rust.py.

Three kinds, and the third is the point of the tool.

**Shape** tests assert things about the emitted Rust that have to hold for
the check to mean anything -- that an owning class has no `Clone`, that
fields come out in reverse declaration order, that a skeleton never names a
variable it did not declare.

**Differential** tests run cpprust and cpp2rust over the same source and
assert they *agree*. Where cpprust refuses on ownership grounds the raised
Rust must contain a move followed by a read, and where cpprust is happy it
must not. Two independent roads to the same answer is the whole idea; a case
where they diverge is worth knowing about either way, so it fails here.

**rustc** tests hand the output to the borrow checker, and are skipped when
rustc is not installed rather than failing -- the raising is useful without
it, and a machine without a Rust toolchain should still be able to run the
rest of this file.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cpp2rust
import cpprust


HAVE_RUSTC = shutil.which("rustc") is not None


def raise_src(src, **kw):
    """Raise a source string, writing it out so the `auto` fallback can see it."""
    d = tempfile.mkdtemp(prefix="cpp2rust.test.")
    path = os.path.join(d, "t.cpp")
    with open(path, "w") as f:
        f.write(src)
    return cpp2rust.raise_to_rust(src, path, clang=False, **kw)


def lower_src(src, **kw):
    """Lower the same source with cpprust. Returns None if it refused."""
    d = tempfile.mkdtemp(prefix="cpprust.test.")
    path = os.path.join(d, "t.cpp")
    with open(path, "w") as f:
        f.write(src)
    try:
        return cpprust.translate(src, path, clang=False, **kw)
    except cpprust.CppError:
        return None


OWNING = """\
void *malloc(unsigned long);
void free(void *);
class Owner {
public:
    int *p;
    Owner() { p = (int *)malloc(16); }
    ~Owner() { free(p); }
    int head() { return p[0]; }
};
"""

COPYABLE = """\
void *malloc(unsigned long);
void free(void *);
class Owner {
public:
    int *p;
    Owner() { p = (int *)malloc(16); }
    Owner(const Owner &o) { p = (int *)malloc(16); }
    ~Owner() { free(p); }
    int head() { return p[0]; }
};
"""

USE_AFTER_COPY = """
int printf(const char *, ...);
int main(void) {
    Owner a;
    Owner b(a);
    printf("%d %d\\n", a.head(), b.head());
    return 0;
}
"""


class Shape(unittest.TestCase):

    def test_owning_class_has_drop_and_no_clone(self):
        """A destructor and no copy constructor is the Rule of Three shape.

        In Rust that is `impl Drop` with no `Clone`, which makes a copy a
        move -- and the move is what rustc goes on to object to.
        """
        rust = raise_src(OWNING)
        self.assertIn("impl Drop for Owner", rust)
        self.assertNotIn("impl Clone for Owner", rust)

    def test_copy_constructor_supplies_clone(self):
        rust = raise_src(COPYABLE)
        self.assertIn("impl Drop for Owner", rust)
        self.assertIn("impl Clone for Owner", rust)

    def test_class_owning_nothing_is_cloneable(self):
        """No destructor anywhere in it means C++ copies it bitwise."""
        rust = raise_src("class P { public: int x; int get() { return x; } };")
        self.assertNotIn("impl Drop for P", rust)
        self.assertIn("Clone", rust)

    def test_fields_are_reversed(self):
        """C++ destroys members in reverse declaration order; Rust drops in
        declaration order. The reversal is what makes the Rust model the C++
        rather than the Crust side of that disagreement."""
        rust = raise_src(
            "class T { public: int *a; int *b; ~T() { } };")
        ia, ib = rust.index("pub a:"), rust.index("pub b:")
        self.assertLess(ib, ia, "fields should be emitted in reverse order")

    def test_base_is_dropped_last(self):
        """C++ destroys the base after every member, so it goes last."""
        rust = raise_src(
            "class B { public: int t; ~B() { } };\n"
            "class D : public B { public: int *m; ~D() { } };")
        body = rust[rust.index("pub struct D"):]
        body = body[:body.index("}")]
        self.assertLess(body.index("pub m:"), body.index("pub base:"))

    def test_owning_external_type_gets_drop(self):
        """A Crust type handed over on --owning owns something, so a class
        holding one by value owns something too -- with no destructor
        written anywhere in the file."""
        rust = raise_src(
            "class Tally { public: Vec_int samples; void add(int v) { } };",
            owning={"Vec_int": "Vec_int_free_buf"})
        self.assertIn("impl Drop for Vec_int", rust)
        self.assertIn("impl Drop for Tally", rust)
        self.assertNotIn("impl Clone for Tally", rust)

    def test_pointer_member_is_raw_not_reference(self):
        """A C++ `T *` carries no lifetime and may be null. Inventing a `&T`
        for one would have rustc check a claim the source never made."""
        rust = raise_src("class V { public: int *p; };")
        self.assertIn("*mut i32", rust)

    def test_reference_parameter_is_borrowed(self):
        """A reference *is* non-null and non-reseatable, which is what a Rust
        reference claims -- so this one is borrowed and gets checked."""
        rust = raise_src("class V { public: void take(const V &o) { } };")
        self.assertIn("&V", rust)

    def test_virtuals_become_a_trait(self):
        rust = raise_src(
            "class S { public: int t; virtual int area() { return 0; } };")
        self.assertIn("pub trait S_virt", rust)
        self.assertIn("fn area", rust)

    def test_base_with_fields_and_nonvirtuals_is_accepted(self):
        """Cpp2Rust (PLDI'26) lists a base with fields or non-virtual methods
        as unsupported. Crust's subset has one in `dispatch.cpp`, so refusing
        it here would refuse a file cpprust already lowers."""
        rust = raise_src(
            "class S { int tag; public: virtual int area() { return 0; }\n"
            "  int describe() { return area() + tag; } };")
        self.assertIn("pub struct S", rust)
        self.assertIn("pub trait S_virt", rust)


class Skeletons(unittest.TestCase):

    def test_skeleton_declares_what_it_uses(self):
        """The invariant that keeps erasure honest.

        A dropped statement makes the check quieter, which is a cost. A
        surviving statement naming a variable that was never declared makes
        rustc report this pass's own bug as a finding about the source, which
        is not allowed. So: every name mentioned must be declared above it.
        """
        rust = raise_src(OWNING + USE_AFTER_COPY, mode="ownership")
        body = rust[rust.index("pub fn __own_main"):]
        declared = set(re.findall(r"let mut (\w+) =", body))
        used = set(re.findall(r"let _ = &(?:mut )?(\w+);", body))
        used |= set(re.findall(r"drop\((\w+)\)", body))
        self.assertTrue(used <= declared,
                        "skeleton names %s but declares %s"
                        % (sorted(used - declared), sorted(declared)))

    def test_method_bodies_are_not_free_functions(self):
        """A method defined inside a class is not a free function, and
        reading it as one produced a skeleton over a body whose receiver
        nothing had declared."""
        rust = raise_src(OWNING + USE_AFTER_COPY, mode="ownership")
        self.assertNotIn("__own_head", rust)
        self.assertIn("__own_main", rust)

    def test_plain_declaration_is_kept(self):
        """`Owner a;` declares the variable every copy below it names."""
        rust = raise_src(OWNING + USE_AFTER_COPY, mode="ownership")
        self.assertIn("let mut a = Owner::new();", rust)

    def test_read_survives_an_erased_statement(self):
        """`printf("%d", a.head())` cannot move anything, so the call goes --
        but C++ reads `a` there, so the read has to stay or the move above it
        has nothing to collide with."""
        rust = raise_src(OWNING + USE_AFTER_COPY, mode="ownership")
        body = rust[rust.index("pub fn __own_main"):]
        self.assertLess(body.index("let mut b = a;"),
                        body.index("let _ = &a;"))


class Differential(unittest.TestCase):
    """cpprust and cpp2rust should reach the same answer by different roads."""

    def moves_then_reads(self, rust, var):
        """Does the skeleton move `var` and then read it? That is what rustc
        reports as `borrow of moved value`."""
        body = rust[rust.index("pub fn __own_main"):]
        move = re.search(r"let mut \w+ = %s;" % re.escape(var), body)
        if not move:
            return False
        return re.search(r"let _ = &%s;" % re.escape(var),
                         body[move.end():]) is not None

    def test_rule_of_three_refused_by_both(self):
        src = OWNING + USE_AFTER_COPY
        self.assertIsNone(lower_src(src), "cpprust should refuse this")
        self.assertTrue(self.moves_then_reads(
            raise_src(src, mode="ownership"), "a"),
            "cpprust refused but the raised Rust has nothing to object to")

    def test_copyable_accepted_by_both(self):
        src = COPYABLE + USE_AFTER_COPY
        self.assertIsNotNone(lower_src(src), "cpprust should accept this")
        rust = raise_src(src, mode="ownership")
        self.assertFalse(self.moves_then_reads(rust, "a"),
                         "cpprust accepted but the raised Rust moves `a`")
        self.assertIn("a.clone()", rust)


@unittest.skipUnless(HAVE_RUSTC, "rustc is not installed")
class Rustc(unittest.TestCase):
    """The second opinion itself. Skipped without a Rust toolchain."""

    def check(self, rust):
        code, err = cpp2rust.check_with_rustc(rust, "<test>")
        return code, err

    def test_owning_class_typechecks(self):
        code, err = self.check(raise_src(OWNING))
        self.assertEqual(code, 0, err)

    def test_use_after_move_is_reported(self):
        code, err = self.check(raise_src(OWNING + USE_AFTER_COPY,
                                         mode="ownership"))
        self.assertNotEqual(code, 0, "rustc accepted a double free")
        self.assertIn("E0382", err)

    def test_copyable_is_accepted(self):
        code, err = self.check(raise_src(COPYABLE + USE_AFTER_COPY,
                                         mode="ownership"))
        self.assertEqual(code, 0, err)


class Operators(unittest.TestCase):
    """The eight member kinds that used to be dropped on the floor."""

    def test_subscript_returns_a_borrow(self):
        """`T &operator[](int)` returning `&mut T` with an elided lifetime is
        what makes the borrow checker responsible for the result -- the one
        claim this pass could not previously back."""
        rust = raise_src(
            "class A { public: int d[4]; int &operator[](int i)"
            " { return d[i]; } };")
        self.assertIn("fn index_op(&mut self, i: i32) -> &mut i32", rust)

    def test_assignment_operator_is_kept(self):
        rust = raise_src(
            "class A { public: int *p; ~A() { }\n"
            "  A &operator=(const A &o) { return *this; } };")
        self.assertIn("fn assign_op", rust)

    def test_comparison_operator_is_named(self):
        rust = raise_src(
            "class P { public: int x; int operator==(const P &o)"
            " { return 0; } };")
        self.assertIn("fn cmp_eq", rust)

    def test_anonymous_union_is_reported_not_flattened(self):
        """Flattening would give each member of one storage its own drop --
        a double free this pass would have invented rather than found."""
        with self.assertRaises(cpp2rust.RustError) as cm:
            raise_src("class L { union { float v; int p; }; public: int q; };")
        self.assertIn("double free", cm.exception.message)


class Heap(unittest.TestCase):

    NODE = """
class Node { public: int v; Node() { v = 0; } ~Node() { } };
"""

    def test_new_becomes_a_box(self):
        rust = raise_src(self.NODE + """
int main(void) { Node *a = new Node(); delete a; return 0; }
""", mode="ownership")
        self.assertIn("Box::new(Node::new())", rust)
        self.assertIn("drop(a);", rust)

    def test_double_delete_is_a_move(self):
        """`delete p` moves the box out, so a second one is a use of a moved
        value -- the double free reported as the thing it is."""
        rust = raise_src(self.NODE + """
int main(void) { Node *a = new Node(); delete a; delete a; return 0; }
""", mode="ownership")
        body = rust[rust.index("pub fn __own_main"):]
        self.assertEqual(body.count("drop(a);"), 2,
                         "both deletes should survive, so rustc sees the "
                         "second as a use of a moved value")

    def test_use_after_delete_survives(self):
        rust = raise_src(self.NODE + """
int printf(const char *, ...);
int main(void) { Node *a = new Node(); delete a; printf("%d", a->v); return 0; }
""", mode="ownership")
        body = rust[rust.index("pub fn __own_main"):]
        self.assertLess(body.index("drop(a);"), body.index("let _ = &a;"))


class FreeFunctions(unittest.TestCase):

    def test_signature_is_emitted(self):
        rust = raise_src(
            "class A { public: int x; };\n"
            "int f(A *p, int k) { return k; }")
        self.assertIn("pub fn f(p: *mut A, k: i32) -> i32", rust)

    def test_parameters_are_declared_in_the_skeleton(self):
        """A class-typed parameter is live for the whole body. With an empty
        parameter list every statement reading one was erased by the guard,
        and the function was checked for nothing."""
        rust = raise_src(
            "class A { public: int *p; ~A() { } int get() { return 0; } };\n"
            "int f(A a) { return a.get(); }", mode="ownership")
        self.assertIn("pub fn __own_f(a: A)", rust)
        body = rust[rust.index("pub fn __own_f"):]
        # Read through the fallback rather than as a statement-position
        # call, since `return a.get();` is a return. Either way `a` is live
        # here, which is the whole point of declaring it.
        self.assertIn("&a", body)

    def test_variadic_prototype_is_skipped(self):
        rust = raise_src(
            "class A { public: int x; };\n"
            "int printf(const char *f, ...);")
        self.assertNotIn("pub fn printf", rust)


if __name__ == "__main__":
    if not HAVE_RUSTC:
        sys.stderr.write(
            "note: rustc not found -- the borrow-checking tests are skipped, "
            "and only the shape and differential tests run.\n")
    unittest.main(verbosity=2)
