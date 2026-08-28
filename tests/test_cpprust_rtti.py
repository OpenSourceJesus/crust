#!/usr/bin/env python3
"""test_cpprust_rtti -- the type descriptor, `dynamic_cast` and `typeid`.

Under `--rtti` every vtable is prefixed with a type descriptor laid out
field for field against py2c's `TypeInfoHdr` (shivyc_rt.h). That is the
whole feature: the vptr a polymorphic class already carries *becomes* the
descriptor pointer, so an object costs no more than it did, and both
languages can ask the same object the same question.

Three things are being pinned here, and they are not the same kind of
claim:

  * **Off by default, and identical when off.** `TestRttiIsOptIn` asserts
    that a translation without the flag is unchanged. This is the one that
    matters most: the descriptor is a cost, and a file that does not ask
    for it must not pay it. A regression here is invisible in every other
    test in this file, because they all pass the flag.

  * **The layout agrees with py2c's.** `TestDescriptorLayout` pins the
    field order against `shivyc_rt.h` rather than against itself. Nothing
    in the C++ half breaks if these drift -- the C++ half is self
    consistent either way -- so the test reads the other language's header
    and compares. Drift here is exactly the failure the digest work in
    CPPRPY.md would inherit, and it would show up as a wrong indirect call
    rather than as a diagnostic.

  * **The refusals.** `dynamic_cast` has four shapes this subset will not
    take, and for three of them real C++ agrees. A refusal is the contract
    as much as a lowering is, so they are tested as such.

    python3 tools/test_cpprust_rtti.py
    python3 tools/test_cpprust_rtti.py -v
    python3 tools/test_cpprust_rtti.py TestDynamicCastRefusals
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.cpprust as cpprust                       # noqa: E402


HIER = """\
class Shape {
public:
    int id;
    Shape(int i) { id = i; }
    virtual ~Shape() { }
    virtual int area() { return 0; }
};
class Square : public Shape {
public:
    int side;
    Square(int i, int s) : Shape(i) { side = s; }
    ~Square() { }
    int area() { return side * side; }
};
"""


class Base(unittest.TestCase):
    """Shared helpers. `lower` defaults the flag *on*, since almost every
    test here is about what the flag does; the one group that is about its
    absence passes `rtti=False` explicitly, which is also what makes that
    group readable."""

    def lower(self, src, rtti=True, **kw):
        return cpprust.translate(src, rtti=rtti, **kw)

    def refuses(self, src, *needles, **kw):
        try:
            out = self.lower(src, **kw)
        except cpprust.CppError as e:
            msg = e.args[0] if e.args else str(e)
            for n in needles:
                self.assertIn(n, msg)
            return msg
        self.fail("expected a refusal, got:\n%s" % out[-600:])

    def assertLowers(self, src, *needles, **kw):
        out = self.lower(src, **kw)
        for n in needles:
            pat = r"\s*".join(re.escape(p) for p in n.split())
            if not re.search(pat, out):
                self.fail("no match for %r in:\n%s" % (n, out[-800:]))
        return out


# ------------------------------------------------------------ opt-in

class TestRttiIsOptIn(Base):
    """The descriptor costs a static object per polymorphic class. A file
    that does not ask for it must come out exactly as it did before, or
    the flag is not a flag."""

    def test_no_descriptor_without_the_flag(self):
        out = self.lower(HIER, rtti=False)
        self.assertNotIn("_CppTypeInfo", out)
        self.assertNotIn("_cpp_isinstance", out)

    def test_vtable_holds_only_slots_without_the_flag(self):
        """The vtable struct is where the descriptor would go, so this is
        the narrowest place the difference shows."""
        out = self.lower(HIER, rtti=False)
        m = re.search(r"struct Shape_vtable \{([^}]*)\}", out)
        self.assertIsNotNone(m)
        self.assertNotIn("name", m.group(1))
        self.assertNotIn("objsize", m.group(1))

    def test_dynamic_cast_is_refused_and_names_the_flag(self):
        """Refusing without saying how to proceed is the failure mode this
        whole translator is written against."""
        self.refuses(HIER + "Square *f(Shape *s) "
                            "{ return dynamic_cast<Square *>(s); }",
                     "dynamic_cast", "--rtti", rtti=False)

    def test_typeid_is_refused_and_names_the_flag(self):
        self.refuses(HIER + "const char *f(Shape *s) "
                            "{ return typeid(*s).name(); }",
                     "typeid", "--rtti", rtti=False)


# ------------------------------------------------------- descriptor

class TestDescriptorLayout(Base):
    """The descriptor's whole purpose is to be the *same* descriptor py2c
    emits, so these read py2c's header rather than restating it."""

    @staticmethod
    def _members(body):
        """Member names of a C struct body, in order.

        Trailing comments are stripped first: both layouts annotate their
        rows (`const void *fields;  /* FieldDesc *; null from C++ */`), and
        a `;` inside one of those comments is not a member.
        """
        out = []
        for line in body.splitlines():
            line = re.sub(r"/\*.*?\*/", "", line).strip()
            if not line.endswith(";"):
                continue
            fn = re.search(r"\(\s*\*\s*(\w+)\s*\)", line)   # a fn pointer
            out.append(fn.group(1) if fn
                       else re.search(r"(\w+)\s*;$", line).group(1))
        return out

    @classmethod
    def _cpp_descriptor_fields(cls):
        m = re.search(r"typedef struct _CppTypeInfo \{(.*?)\} _CppTypeInfo;",
                      cpprust._RTTI_PRELUDE, re.S)
        assert m is not None
        return cls._members(m.group(1))

    def _py2c_typeinfohdr_fields(self):
        """The member names of `TypeInfoHdr`, in order, from py2c's own
        embedded runtime header. Read rather than hardcoded: a test that
        restates the layout cannot detect the layout changing."""
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "py2c.py")
        with open(p) as f:
            src = f.read()
        m = re.search(r"typedef struct TypeInfoHdr \{(.*?)\} TypeInfoHdr;",
                      src, re.S)
        self.assertIsNotNone(m, "py2c no longer defines TypeInfoHdr")
        return self._members(m.group(1))

    def test_field_order_matches_py2c(self):
        """The layouts have to agree member for member. `isinstance_of`
        only needs `{name, base}` at the front, but the rest is what a
        shared digest would later rely on, and a hole is how two sides
        drift apart quietly."""
        self.assertEqual(self._py2c_typeinfohdr_fields(),
                         self._cpp_descriptor_fields())

    def test_rows_match_the_prelude(self):
        """`_RTTI_ROWS` is prefixed onto every vtable struct and has to
        stay in step with the typedef it mirrors."""
        self.assertEqual(self._cpp_descriptor_fields(),
                         self._members("\n".join(cpprust._RTTI_ROWS)))

    def test_descriptor_precedes_the_slots(self):
        """Slots after the header, so a derived table is still prefix
        compatible with its base's and a dispatch site is unchanged."""
        out = self.assertLowers(HIER)
        m = re.search(r"struct Square_vtable \{([^}]*)\}", out)
        body = m.group(1)
        self.assertLess(body.index("name"), body.index("(*area)"))
        self.assertLess(body.index("objsize"), body.index("(*area)"))

    def test_base_link_points_at_the_base(self):
        out = self.assertLowers(HIER)
        self.assertRegex(out, r"Square__vtable = \{\s*\"Square\",\s*"
                              r"\(const struct _CppTypeInfo \*\)&Shape__vtable")

    def test_root_has_a_null_base(self):
        out = self.assertLowers(HIER)
        self.assertRegex(out, r"Shape__vtable = \{\s*\"Shape\",\s*0,")

    def test_object_layout_is_unchanged(self):
        """The point of the design: the vptr *is* the descriptor pointer,
        so no object grows a header. The struct bodies must be identical
        with the flag on and off."""
        on = self.lower(HIER, rtti=True)
        off = self.lower(HIER, rtti=False)
        for cls in ("Shape", "Square"):
            pat = r"struct %s \{[^}]*\}" % cls
            self.assertEqual(re.search(pat, on).group(0),
                             re.search(pat, off).group(0))


class TestAbstractDescriptor(Base):
    """An abstract class never gets a vtable instance for a descriptor to
    sit in front of, but a derived class still names it as its base and
    `dynamic_cast<Abstract *>` is a fair question."""

    ABS = """\
class Shape { public: virtual ~Shape() { } virtual int area() = 0; };
class Square : public Shape {
public:
    int side;
    Square(int s) { side = s; }
    ~Square() { }
    int area() { return side * side; }
};
"""

    def test_abstract_class_gets_a_standalone_descriptor(self):
        out = self.assertLowers(self.ABS)
        self.assertIn("Shape__typeinfo", out)
        self.assertNotIn("Shape__vtable = {", out)

    def test_derived_links_to_the_standalone_one(self):
        out = self.assertLowers(self.ABS)
        self.assertRegex(out, r"Square__vtable = \{\s*\"Square\",\s*"
                              r"&Shape__typeinfo")

    def test_descriptor_follows_the_struct(self):
        """`objsize` is a `sizeof`, so the struct has to be complete
        first. Emitting it beside the vtable *type* -- which precedes the
        struct body -- compiled to an incomplete-type error."""
        out = self.assertLowers(self.ABS)
        self.assertLess(out.index("struct Shape {"),
                        out.index("Shape__typeinfo"))

    def test_cast_to_an_abstract_base_is_allowed(self):
        self.assertLowers(self.ABS + "Shape *f(Shape *s) "
                                     "{ return dynamic_cast<Shape *>(s); }",
                          "_cpp_dyncast", "&Shape__typeinfo")


# --------------------------------------------------------- lowering

class TestDynamicCastLowering(Base):

    def test_pointer_form_lowers_to_the_helper(self):
        self.assertLowers(HIER + "Square *f(Shape *s) "
                                 "{ return dynamic_cast<Square *>(s); }",
                          "(Square *)_cpp_dyncast((void *)(s)")

    def test_helper_is_emitted(self):
        out = self.assertLowers(HIER + "Square *f(Shape *s) "
                                       "{ return dynamic_cast<Square *>(s); }")
        self.assertIn("_cpp_isinstance", out)
        # `static inline`, so an unused one draws no -Wunused-function and
        # the prelude can be emitted unconditionally.
        self.assertIn("static inline", out)

    def test_cast_inside_a_condition(self):
        """The lowering sits in expression position, which is where a
        pass that rewrote statements would have gone wrong."""
        self.assertLowers(HIER + "int f(Shape *s) { "
                                 "if (dynamic_cast<Square *>(s)) return 1; "
                                 "return 0; }",
                          "_cpp_dyncast")

    def test_two_casts_in_one_expression(self):
        self.assertLowers(
            HIER + "int f(Shape *a, Shape *b) { return "
                   "dynamic_cast<Square *>(a) && dynamic_cast<Square *>(b); }",
            "_cpp_dyncast")

    def test_cast_of_a_call_result(self):
        out = self.assertLowers(
            HIER + "Shape *mk(void);\n"
                   "Square *f(void) { return dynamic_cast<Square *>(mk()); }")
        self.assertIn("_cpp_dyncast((void *)(mk())", out)


class TestTypeidLowering(Base):

    def test_name_becomes_a_field_read(self):
        self.assertLowers(HIER + "const char *f(Shape *s) "
                                 "{ return typeid(*s).name(); }",
                          "->name")

    def test_deref_operand_uses_the_pointer_itself(self):
        """`typeid(*p)` asks about what `p` points at, so the address is
        `p` -- not `&*p`, which is the same address spelled twice."""
        out = self.assertLowers(HIER + "const char *f(Shape *s) "
                                       "{ return typeid(*s).name(); }")
        self.assertIn("(s))->name", out)
        self.assertNotIn("&(*s)", out)

    def test_object_operand_takes_its_address(self):
        out = self.assertLowers(HIER + "const char *f(Square q) "
                                       "{ return typeid(q).name(); }")
        self.assertIn("&(", out)

    def test_bare_typeid_yields_the_descriptor(self):
        """No `type_info` class behind it, so `typeid(a) == typeid(b)` is
        a pointer comparison and means the right thing."""
        self.assertLowers(HIER + "int f(Shape *a, Shape *b) "
                                 "{ return typeid(*a) == typeid(*b); }",
                          "_CppTypeInfo")


# --------------------------------------------------------- refusals

class TestDynamicCastRefusals(Base):
    """Four shapes this subset will not take. Real C++ refuses three of
    them too; the fourth (the reference form) it accepts, and it is
    refused here because it is defined to throw."""

    def test_reference_form(self):
        self.refuses(HIER + "int f(Shape &s) "
                            "{ Square &q = dynamic_cast<Square &>(s); "
                            "return q.side; }",
                     "reference form", "no exceptions", "test the result")

    def test_value_form(self):
        self.refuses(HIER + "int f(Shape *s) "
                            "{ Square q = dynamic_cast<Square>(s); "
                            "return q.side; }",
                     "casts to a value", "Cast to `Square *`")

    def test_non_polymorphic_target(self):
        """C++ refuses this for the same reason: with no virtual method
        there is no descriptor and nothing to check."""
        self.refuses("class A { public: int x; };\n"
                     "class B : public A { public: int y; };\n"
                     "int f(A *p) { return dynamic_cast<B *>(p) ? 1 : 0; }",
                     "no virtual methods", "virtual destructor")

    def test_unknown_target_class(self):
        self.refuses(HIER + "int f(Shape *s) "
                            "{ return dynamic_cast<Nope *>(s) ? 1 : 0; }",
                     "not a class defined in this translation")


# ------------------------------------------------------------ gcc

def _have_gcc():
    try:
        subprocess.check_output(["gcc", "--version"],
                                stderr=subprocess.STDOUT)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


@unittest.skipUnless(_have_gcc(), "gcc not available")
class TestAgainstRealCpp(Base):
    """Translation succeeding is not the claim; the claim is that the C
    means what the C++ meant. Each of these lowers, compiles and runs, and
    compares against `g++` on the same source.

    `typeid(..).name()` is deliberately not compared: the standard leaves
    it implementation-defined and g++ returns the mangled spelling
    (`6Square`), so a comparison would pin g++'s choice rather than
    anything about this subset.
    """

    def _run(self, src, extra_cpp=""):
        d = tempfile.mkdtemp()
        cpp = os.path.join(d, "t.cpp")
        with open(cpp, "w") as f:
            f.write(src)
        c = os.path.join(d, "t.c")
        with open(c, "w") as f:
            f.write(self.lower(src))
        exe = os.path.join(d, "t")
        subprocess.check_output(["gcc", "-std=c11", "-w", "-o", exe, c],
                                stderr=subprocess.STDOUT)
        ours = subprocess.check_output([exe]).decode()
        ref_src = os.path.join(d, "r.cpp")
        with open(ref_src, "w") as f:
            f.write("#include <typeinfo>\n" + extra_cpp + src)
        rexe = os.path.join(d, "r")
        subprocess.check_output(["g++", "-std=c++11", "-w", "-o", rexe,
                                 ref_src], stderr=subprocess.STDOUT)
        return ours, subprocess.check_output([rexe]).decode()

    def test_downcast_succeeds_and_crosscast_fails(self):
        ours, ref = self._run("""#include <stdio.h>
class Shape { public: virtual ~Shape() { } virtual int area() { return 0; } };
class Square : public Shape {
public: int side; Square(int s) { side = s; } ~Square() { }
        int area() { return side * side; } };
class Circle : public Shape {
public: int r; Circle(int q) { r = q; } ~Circle() { }
        int area() { return 3 * r * r; } };
int main(void) {
    Shape *a = new Square(4);
    Shape *b = new Circle(5);
    Square *ok = dynamic_cast<Square *>(a);
    Square *no = dynamic_cast<Square *>(b);
    printf("%d %d %d\\n", a->area(), ok ? ok->side : -1, no ? 9 : -1);
    delete a; delete b;
    return 0;
}
""")
        self.assertEqual(ours, ref)

    def test_three_levels(self):
        """Down to the middle of a chain, up to its root, and a sibling
        that must fail -- the cases a one-level test cannot tell apart."""
        ours, ref = self._run("""#include <stdio.h>
class A { public: virtual ~A() { } virtual int k() { return 1; } };
class B : public A { public: int k() { return 2; } };
class C : public B { public: int k() { return 3; } };
int main(void) {
    A *p = new C();
    A *q = new B();
    B *asb = dynamic_cast<B *>(p);
    A *asa = dynamic_cast<A *>(p);
    C *bad = dynamic_cast<C *>(q);
    printf("%d %d %d\\n", asb ? asb->k() : -1, asa ? asa->k() : -1,
           bad ? 9 : -1);
    delete p; delete q;
    return 0;
}
""")
        self.assertEqual(ours, ref)

    def test_typeid_equality(self):
        """`name()` is implementation-defined, but equality is not."""
        ours, ref = self._run("""#include <stdio.h>
class A { public: virtual ~A() { } };
class B : public A { };
int main(void) {
    A *p = new B();
    A *q = new B();
    A *r = new A();
    printf("%d %d\\n", typeid(*p) == typeid(*q), typeid(*p) == typeid(*r));
    delete p; delete q; delete r;
    return 0;
}
""")
        self.assertEqual(ours, ref)

    def test_null_operand_is_not_a_match(self):
        """`_cpp_isinstance` guards the load, so a null pointer answers
        no rather than dereferencing offset zero."""
        ours, ref = self._run("""#include <stdio.h>
class A { public: virtual ~A() { } };
class B : public A { };
int main(void) {
    A *p = 0;
    printf("%d\\n", dynamic_cast<B *>(p) ? 1 : 0);
    return 0;
}
""")
        self.assertEqual(ours, ref)


# --------------------------------------------------- multiple inheritance

IFACE = """\
class Drawable { public: virtual ~Drawable() { } virtual int draw() = 0; };
class Named { public: virtual ~Named() { } virtual int tag() = 0; };
class Shape {
public:
    int id;
    Shape(int i) { id = i; }
    virtual ~Shape() { }
    virtual int area() { return 0; }
};
class Square : public Shape, public Drawable {
public:
    int side;
    Square(int i, int s) : Shape(i) { side = s; }
    ~Square() { }
    int area() { return side * side; }
    int draw() { return 100 + side; }
};
"""


class TestInterfaceInheritance(Base):
    """Tier-1 MI: the first base is the layout base, the rest are
    interfaces reached through a vptr of their own at a fixed offset."""

    def test_one_vptr_per_interface_after_the_layout_base(self):
        self.assertLowers(IFACE, "struct Square { Shape _base; "
                                 "const struct Drawable_vtable "
                                 "*_vptr_Drawable; int side; };")

    def test_interface_gets_a_table_of_its_own(self):
        self.assertLowers(IFACE, "Square__vtable_Drawable")

    def test_thunk_steps_back_to_the_whole_object(self):
        """The slot hands over a `Drawable *` pointing at the vptr field;
        the implementation wants the object, which is that address less the
        field's offset."""
        self.assertLowers(IFACE, "offsetof(struct Square, _vptr_Drawable)")

    def test_constructor_installs_the_interface_vptr(self):
        """A `Drawable *` taken before this ran would dispatch through an
        uninitialised word."""
        out = self.assertLowers(IFACE)
        self.assertRegex(out, r"Square_new\([^)]*\)\s*\{[^}]*"
                              r"_vptr_Drawable = &Square__vtable_Drawable")

    def test_cast_from_a_value_is_adjusted(self):
        self.assertLowers(IFACE + "int f(void) { Square q(1, 2); "
                                  "Drawable *d = (Drawable *)&q; "
                                  "return d->draw(); }",
                          "_vptr_Drawable)")

    def test_cast_from_a_pointer_is_adjusted(self):
        self.assertLowers(IFACE + "int f(Square *p) { "
                                  "Drawable *d = (Drawable *)p; "
                                  "return d->draw(); }",
                          "_vptr_Drawable)")

    def test_several_interfaces_on_one_class(self):
        src = IFACE.replace(
            "class Square : public Shape, public Drawable {",
            "class Square : public Shape, public Drawable, public Named {"
        ).replace("int draw() { return 100 + side; }",
                  "int draw() { return 100 + side; } int tag() { return id; }")
        out = self.assertLowers(src)
        self.assertIn("_vptr_Drawable", out)
        self.assertIn("_vptr_Named", out)

    def test_inherited_interface_gets_a_table_per_class(self):
        """A derived class that overrides an interface method must get its
        own table. Sharing the base's meant one object answered `draw()`
        two different ways depending on which pointer you asked through --
        the layout base gave the override, the interface gave the base."""
        out = self.assertLowers(IFACE + """\
class Tiny : public Square {
public:
    Tiny(int i) : Square(i, 1) { }
    ~Tiny() { }
    int draw() { return 300 + id; }
};
""")
        self.assertIn("Tiny__vtable_Drawable", out)
        self.assertIn("offsetof(struct Tiny, _base._vptr_Drawable)", out)


class TestInterfaceRefusals(Base):

    def test_data_carrying_secondary_base(self):
        """Only the first base is a struct prefix, so a second base's
        fields would have no storage to sit in."""
        self.refuses("class X { public: int a; virtual ~X() { } };\n"
                     "class Y { public: int b; virtual ~Y() { } };\n"
                     "class Z : public X, public Y { public: int c; };",
                     "has data members")

    def test_secondary_base_with_no_virtuals(self):
        self.refuses("class X { public: int a; virtual ~X() { } };\n"
                     "class Y { public: int f() { return 1; } };\n"
                     "class Z : public X, public Y { public: int c; };",
                     "no virtual methods")

    def test_virtual_inheritance(self):
        """A design position rather than a gap: a virtual base's offset
        depends on the most-derived type, which is the property the rest of
        this lowering is built on not needing."""
        self.refuses("class X { public: virtual int f() { return 1; } };\n"
                     "class Z : public virtual X { public: int c; };",
                     "`virtual` inheritance", "runtime table lookup")

    def test_conversion_from_a_call_result(self):
        """No named source, so no offset to adjust by -- and an unadjusted
        pointer would dispatch through the wrong table silently."""
        self.refuses(IFACE + "Square *mk(void);\n"
                             "int f(void) { Drawable *d = (Drawable *)mk(); "
                             "return d->draw(); }",
                     "cannot convert this to the secondary base",
                     "Assign it to a typed local first")

    def test_implicit_conversion_without_a_cast(self):
        self.refuses(IFACE + "int f(void) { Square q(1, 2); "
                             "Drawable *d = &q; return d->draw(); }",
                     "cannot convert this to the secondary base")


@unittest.skipUnless(_have_gcc(), "gcc not available")
class TestInterfaceAgainstRealCpp(TestAgainstRealCpp):
    """Dispatch through an interface, compared against g++ on the same
    source. Translating is not the claim; answering the same is."""

    def test_dispatch_through_each_interface(self):
        ours, ref = self._run("""#include <stdio.h>
class Drawable { public: virtual ~Drawable() { } virtual int draw() = 0; };
class Named { public: virtual ~Named() { } virtual int tag() = 0; };
class Shape { public: int id; Shape(int i) { id = i; }
              virtual ~Shape() { } virtual int area() { return 0; } };
class Square : public Shape, public Drawable, public Named {
public: int side; Square(int i, int s) : Shape(i) { side = s; } ~Square() { }
        int area() { return side * side; }
        int draw() { return 100 + side; }
        int tag() { return 200 + id; } };
int use(Drawable *d) { return d->draw(); }
int main(void) {
    Square sq(7, 4);
    Square *sp = &sq;
    Drawable *d1 = (Drawable *)&sq;
    Drawable *d2 = (Drawable *)sp;
    Named *nm = (Named *)&sq;
    printf("%d %d %d %d %d\\n", sq.area(), d1->draw(), d2->draw(),
           nm->tag(), use((Drawable *)sp));
    return 0;
}
""")
        self.assertEqual(ours, ref)

    def test_override_reached_through_an_inherited_interface(self):
        """The bug this caught: `Tiny` overrode `draw`, but inherited the
        interface through its layout base and so kept `Square`'s table.
        Asking through the interface gave 101; asking g++ gave 305."""
        ours, ref = self._run("""#include <stdio.h>
class Drawable { public: virtual ~Drawable() { } virtual int draw() = 0; };
class Shape { public: int id; Shape(int i) { id = i; } virtual ~Shape() { } };
class Square : public Shape, public Drawable {
public: int side; Square(int i, int s) : Shape(i) { side = s; } ~Square() { }
        int draw() { return 100 + side; } };
class Tiny : public Square {
public: Tiny(int i) : Square(i, 1) { } ~Tiny() { }
        int draw() { return 300 + id; } };
int main(void) {
    Tiny t(5);
    Drawable *d = (Drawable *)&t;
    printf("%d\\n", d->draw());
    return 0;
}
""")
        self.assertEqual(ours, ref)


if __name__ == "__main__":
    unittest.main(verbosity=2)
