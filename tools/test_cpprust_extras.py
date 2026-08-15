#!/usr/bin/env python3
"""test_cpprust_extras -- fast tests for the C++ features in flight.

`tools/litehtml_test.py` is the acceptance test: it lowers the real
litehtml sources and compiles the result. It is also slow -- minutes per
run -- because it splices every header of a real project for every file.
That is the wrong loop to develop against.

This is the inner loop. Each test is a *distilled* version of one shape
found in litehtml, cut down to the few lines that actually exercise the
gap, so the whole file runs in about a second. When a test here passes and
the corresponding litehtml file still fails, the distillation was
incomplete -- and that difference is itself worth knowing, so the docstring
of each test names the file it came from.

Tests are grouped by the feature being added, and each group carries a
short note on *why* the shape is refused today. As a feature lands, its
group stays: this file is the regression net for work that
`tests/test_cpprust.py` has not absorbed yet.

    python3 tools/test_cpprust_extras.py            # all
    python3 tools/test_cpprust_extras.py -v         # names as they run
    python3 tools/test_cpprust_extras.py Cast       # one group
    python3 tools/test_cpprust_extras.py --failing  # only the open ones
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.cpprust as cpprust                       # noqa: E402
import tools.cpp_auto as cpp_auto                     # noqa: E402


class Base(unittest.TestCase):
    """Shared helpers.

    `lower` returns the C. `refuses` asserts that a shape is *reported*
    rather than mistranslated -- which is as much a part of the contract
    as a successful lowering, so the open gaps below assert on the
    diagnostic and flip to asserting on the output when they land.
    """

    def lower(self, src, **kw):
        return cpprust.translate(src, **kw)

    def refuses(self, src, *needles, **kw):
        try:
            out = self.lower(src, **kw)
        except (cpprust.CppError, cpp_auto.AutoError) as e:
            msg = e.args[0] if e.args else str(e)
            for n in needles:
                self.assertIn(n, msg)
            return msg
        self.fail("expected a refusal, got:\n%s" % out[-600:])

    def assertLowers(self, src, *needles, **kw):
        """Assert the lowering succeeds and its output contains `needles`.

        Matching is whitespace-tolerant: `Thing *t` and `Thing * t` are
        the same declaration, and which one comes out depends on which
        path emitted it. Output spacing is not part of the contract, so a
        test that pins it is testing the wrong thing -- and would have to
        be edited every time an unrelated emission changed.
        """
        import re as _re
        out = self.lower(src, **kw)
        for n in needles:
            pat = r"\s*".join(_re.escape(p) for p in n.split())
            if not _re.search(pat, out):
                self.fail("no match for %r in:\n%s" % (n, out[-800:]))
        return out


# ---------------------------------------------------------------- casts

class TestCastDeduction(Base):
    """`auto` through a named cast.

    From `litehtml/include/litehtml/context.h`, which every element file
    includes -- so this one shape fails about a quarter of the tree:

        if (auto* ref { static_cast<typename T::js_object_ref*>(
                            JS_GetOpaque(value, T::jsClassID)) })
            delete ref;

    A named cast is the *most* written a type can be: it is spelled in the
    angle brackets, in the source, at the point of use. Nothing has to be
    inferred. Deducing from it is squarely inside what this pass claims to
    do, which is read types from how they are written.
    """

    def test_static_cast_deduces_target_type(self):
        self.assertLowers("""
struct Thing { int v; };
void f(void *p) {
    auto t = static_cast<Thing *>(p);
    t->v = 1;
}
""", "Thing * t")

    def test_reinterpret_and_const_cast(self):
        self.assertLowers("""
struct Thing { int v; };
void f(void *p, const Thing *cp) {
    auto a = reinterpret_cast<Thing *>(p);
    auto b = const_cast<Thing *>(cp);
    a->v = b->v;
}
""", "Thing * a", "Thing * b")

    def test_c_style_cast_deduces(self):
        self.assertLowers("""
struct Thing { int v; };
void f(void *p) {
    auto t = (Thing *)p;
    t->v = 1;
}
""", "Thing * t")

    def test_cast_to_scalar(self):
        self.assertLowers("""
void f(double d) { auto n = static_cast<int>(d); use(n); }
""", "int n")

    def test_dynamic_cast_still_reported(self):
        """`dynamic_cast` needs RTTI, which the subset has no story for.

        Deducing the type would be easy and the lowering still wrong, so
        it stays refused -- the diagnostic is the feature.
        """
        self.refuses("""
struct A { virtual int f(); };
struct B : public A { int f(); };
void f(A *a) { auto b = dynamic_cast<B *>(a); }
""", "dynamic_cast")


class TestBraceInitAuto(Base):
    """`auto x { e }` -- brace initialisation.

    Also from `context.h`, and in the same statement as the cast above:
    `auto* ref { static_cast<..>(..) }` and `auto proto { JS_NewObject(..) }`.

    Today `_AUTO_DECL` requires an `=`, so the braced form is not
    recognised as a declaration at all and `auto` survives into the output.
    The two spellings mean the same thing for everything this subset
    lowers, exactly as the doc already says of a constructor's initializer
    list.
    """

    def test_brace_init_with_call(self):
        self.assertLowers("""
int mk(void);
void f(void) { auto n { mk() }; use(n); }
""", "int n")

    def test_brace_init_with_star(self):
        self.assertLowers("""
struct Thing { int v; };
void f(void *p) {
    auto* ref { static_cast<Thing *>(p) };
    ref->v = 1;
}
""", "Thing * ref")

    def test_brace_init_class_construction(self):
        self.assertLowers("""
class A { public: int v; A() { v = 0; } int get() { return v; } };
void f(void) { auto a { A() }; use(a.get()); }
""", "A_new(&a)")

    def test_empty_braces_is_reported(self):
        """`auto x {};` has nothing to deduce from, in C++ either."""
        self.refuses("void f(void) { auto x {}; }", "auto")


class TestDeleteInCondition(Base):
    """The whole `context.h` statement, end to end.

    A declaration inside an `if` condition, initialised by a cast, whose
    name is then the operand of `delete`. This is the shape that fails
    eleven litehtml files, and it is the acceptance test for the two
    groups above.
    """

    def test_declaration_in_if_condition(self):
        out = self.assertLowers("""
class Ref { public: int v; ~Ref() { v = 0; } };
void f(void *p) {
    if (auto *ref = static_cast<Ref *>(p)) { delete ref; }
}
""", "Ref_drop")
        self.assertNotIn("auto", out)

    def test_brace_declaration_in_if_condition(self):
        out = self.assertLowers("""
class Ref { public: int v; ~Ref() { v = 0; } };
void f(void *p) {
    if (auto* ref { static_cast<Ref *>(p) }) { delete ref; }
}
""", "Ref_drop")
        self.assertNotIn("auto", out)


# ------------------------------------------------------------ range-for

class TestRangeForMember(Base):
    """A range-`for` over a member expression.

    From `litehtml/src/css_selector.cpp`:

        for (auto &attr : m_right.m_attrs) { .. }

    The doc requires the range to be *a name*, because the length is read
    from how the range is written. A member of `this` is written just as
    plainly -- `m_right.m_attrs` names a field of a field, and both have
    declared types -- so the restriction is narrower than the reason for
    it.
    """

    def test_range_for_over_field_of_field(self):
        self.assertLowers("""
class Attrs {
public:
    int d[8]; int n;
    int size() { return n; }
    int &operator[](int i) { return d[i]; }
};
class Right { public: Attrs m_attrs; };
class Sel {
public:
    Right m_right;
    void go() { for (auto &a : m_right.m_attrs) { use(a); } }
};
""", "Attrs_size(&this->m_right.m_attrs)",
     "Attrs__index(&this->m_right.m_attrs,")

    def test_range_for_over_this_member(self):
        self.assertLowers("""
class Attrs {
public:
    int d[8]; int n;
    int size() { return n; }
    int &operator[](int i) { return d[i]; }
};
class Sel {
public:
    Attrs m_attrs;
    void go() { for (auto &a : m_attrs) { use(a); } }
};
""", "Attrs_size(&this->m_attrs)", "Attrs__index(&this->m_attrs,")

    def test_range_for_over_unknown_chain_is_still_reported(self):
        """A chain whose steps have no declared type is still guessing."""
        self.refuses("""
void go(void) { for (auto &a : thing.parts) { use(a); } }
""", "not something this pass can walk")


class TestScopedTypeInForHead(Base):
    """A plain `for` misread as a range-`for`.

    From `litehtml/src/el_before_after.cpp`:

        for (tstring::size_type i = 0; i < txt.length(); i++)

    The `:` of a qualified name `tstring::size_type` is being taken for
    the `:` of a range-`for`, so an ordinary indexed loop is reported as
    an unwalkable range. Same family as the `delete_property` bug: a
    pattern matching where it should not. A `::` is never a range colon.
    """

    def test_qualified_type_in_for_head(self):
        out = self.assertLowers("""
void f(void) {
    for (size_t i = 0; i < 4; i++) { use(i); }
}
""", "for (")
        self.assertNotIn("_cpp_it", out)

    def test_scoped_type_in_for_head_is_not_a_range_for(self):
        self.assertLowers("""
typedef struct tstring { int n; } tstring;
void f(tstring *txt) {
    for (tstring::size_type i = 0; i < 4; i = i + 1) { use(i); }
}
""", "for (")


# ------------------------------------------------------------- includes

class TestAngleIncludeOfProjectHeader(Base):
    """`#include <litehtml/html.h>` -- a project header in angle brackets.

    litehtml includes its own headers both ways. Quoted includes are
    spliced; angle ones are deliberately left for the C front end, on the
    grounds that this pass is not the authority on the include path.

    That reasoning holds for `<string.h>` and not for a header sitting in
    a directory the caller passed with `--incdir`: if it is found there,
    it is this project's, and leaving it unspliced means the classes it
    declares are never lowered -- which is how `num_cvt.cpp` translates
    clean and then emits C naming a struct nobody defined.

    The conservative rule that keeps both: splice an angle include only
    when it resolves under an `--incdir`, and leave it alone otherwise.
    """

    def test_angle_include_under_incdir_is_spliced(self):
        import tempfile
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "lh"), exist_ok=True)
        with open(os.path.join(d, "lh", "thing.h"), "w") as f:
            f.write("class Thing { public: int v; int get() { return v; } };\n")
        out = self.lower("""
#include <lh/thing.h>
int f(void) { Thing t; return t.get(); }
""", incdirs=[d])
        self.assertIn("Thing_get", out)

    def test_unresolvable_angle_include_is_left_alone(self):
        out = self.lower("""
#include <stdio.h>
int f(void) { return 0; }
""")
        self.assertIn("#include <stdio.h>", out)


class TestCxxSpelledCHeaders(Base):
    """`<cstdint>` and friends.

    C++ spells the C headers without the `.h` and with a leading `c`.
    They survive into the lowered C, where gcc stops at the first one.
    The subset already has the precedent -- it pulls in `<stdbool.h>`
    when a file writes `bool` -- so mapping the spelling back is the same
    move.
    """

    def test_cstdint_becomes_stdint(self):
        out = self.lower("#include <cstdint>\nint f(void) { return 0; }\n")
        self.assertIn("<stdint.h>", out)
        self.assertNotIn("<cstdint>", out)

    def test_cstring_becomes_string_h(self):
        out = self.lower("#include <cstring>\nint f(void) { return 0; }\n")
        self.assertIn("<string.h>", out)

    def test_cxx_only_header_is_not_mapped(self):
        """`<string>` is std::string, not `<string.h>` -- a different thing."""
        out = self.lower("#include <string>\nint f(void) { return 0; }\n")
        self.assertNotIn("<string.h>", out)


# ------------------------------------------------------------ namespaces

class TestForwardDeclaredClass(Base):
    """A class declared here and defined somewhere else.

    From `litehtml/include/litehtml/types.h`, which forward-declares
    `class element;` and then builds `std::shared_ptr<element>` and
    `std::vector<std::shared_ptr<element>>` on it. That is legal C++ --
    a `shared_ptr<T>` holds a `T *`, and a pointer to an incomplete type
    is fine -- and legal C too, provided the struct tag is declared. It
    was not, so a file could translate clean and then emit

        static inline void shared_ptr_litehtml_element_new_1(
            shared_ptr_litehtml_element *this, litehtml_element *q);

    naming a type nothing declares. Exactly the shape the gcc stage of
    `litehtml_test.py` exists to catch: translation had no complaint.
    """

    def test_instantiation_over_forward_declared_class(self):
        out = self.assertLowers("""
class Thing;
template<typename T>
class Ptr {
public:
    T *p;
    Ptr() { p = 0; }
    Ptr(T *q) { p = q; }
    T *get() { return p; }
};
void f(void) { Ptr<Thing> a; use(a.get()); }
""", "struct Thing;", "typedef struct Thing Thing;")
        self.assertNotIn("class Thing", out)

    def test_defined_class_is_not_declared_twice(self):
        """A class both declared and defined here already had a tag."""
        out = self.assertLowers("""
class Thing;
class Thing { public: int v; int get() { return v; } };
void f(void) { Thing t; use(t.get()); }
""", "Thing_get")
        self.assertEqual(out.count("typedef struct Thing Thing;"), 1)

    def test_by_value_member_of_incomplete_class_is_left_to_the_c_front_end(self):
        """Hoisting the *name* must not invent a definition.

        A by-value member still needs a complete type, and this pass does
        not pretend to supply one: it emits the tag and nothing else, so
        `struct Holder { Thing t; };` reaches the C front end, which
        reports `field 't' has incomplete type` and names the field. That
        is a loud, precise diagnostic rather than a silent miscompile, so
        the guiding rule is satisfied without a refusal here -- the same
        reasoning the doc already applies to a trailing `const` and to a
        member declared but never defined.
        """
        out = self.assertLowers("""
class Thing;
class Holder { public: Thing t; };
void f(void) { Holder h; use(&h); }
""", "struct Thing;", "struct Holder { Thing t; };")
        self.assertNotIn("struct Thing {", out)


class TestConditionals(Base):
    """`#ifdef` resolved while headers are spliced.

    From `litehtml/include/litehtml/os_types.h`, which gives `tstring`
    two definitions under `#ifndef LITEHTML_UTF8`. Spliced without
    evaluating the conditional, both reach one translation, and templates
    were monomorphised over both -- a `vector_wstring` beside the real
    one, over a type the subset does not supply.

    What the evaluator does with a condition it *cannot* decide is the
    half that matters: the block is passed through untouched, for the C
    front end to resolve as it always did. This only ever narrows what
    reaches the rest of the pass, and only where the answer is not in
    doubt.
    """

    def test_ifndef_takes_the_else_branch_when_defined(self):
        out = self.lower("""
#ifndef USE_NARROW
typedef int wide_t;
#else
typedef char narrow_t;
#endif
""", defines=["USE_NARROW"], incdirs=["."])
        self.assertIn("narrow_t", out)
        self.assertNotIn("wide_t", out)

    def test_ifndef_takes_the_first_branch_when_not_defined(self):
        out = self.lower("""
#ifndef USE_NARROW
typedef int wide_t;
#else
typedef char narrow_t;
#endif
""", incdirs=["."])
        self.assertIn("wide_t", out)
        self.assertNotIn("narrow_t", out)

    def test_defined_disjunction_is_decided(self):
        """`defined(A) || defined(B)` is a chain of the one test this pass
        does answer. litehtml puts most of os_types.h inside one."""
        out = self.lower("""
#if defined( WIN32 ) || defined( _WIN32 ) || defined( WINCE )
typedef int windows_t;
#else
typedef int posix_t;
#endif
""", incdirs=["."])
        self.assertIn("posix_t", out)
        self.assertNotIn("windows_t", out)

    def test_undecidable_condition_is_passed_through(self):
        """A comparison needs a value, and this pass has none."""
        out = self.lower("""
#if _MSC_VER < 1900
typedef int kept_t;
#endif
""", incdirs=["."])
        self.assertIn("kept_t", out)
        self.assertIn("#if _MSC_VER < 1900", out)

    def test_mixed_operators_are_not_guessed_at(self):
        """`A || B && C` needs precedence; evaluation order is a guess."""
        out = self.lower("""
#if defined(A) || defined(B) && defined(C)
typedef int kept_t;
#endif
""", incdirs=["."])
        self.assertIn("#if defined(A) || defined(B) && defined(C)", out)

    def test_define_in_one_header_decides_a_later_one(self):
        import tempfile
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "opt.h"), "w") as f:
            f.write("#define USE_NARROW 1\n")
        with open(os.path.join(d, "types.h"), "w") as f:
            f.write("#ifndef USE_NARROW\ntypedef int wide_t;\n"
                    "#else\ntypedef char narrow_t;\n#endif\n")
        out = self.lower('#include "opt.h"\n#include "types.h"\n',
                         incdirs=[d], basedir=d)
        self.assertIn("narrow_t", out)
        self.assertNotIn("wide_t", out)

    def test_include_in_a_dead_branch_is_not_spliced(self):
        import tempfile
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "never.h"), "w") as f:
            f.write("class NeverSpliced { public: int v; };\n")
        out = self.lower('#ifdef NOPE\n#include "never.h"\n#endif\n',
                         incdirs=[d], basedir=d)
        self.assertNotIn("NeverSpliced", out)

    def test_include_guard_resolves(self):
        out = self.lower("""
#ifndef MY_GUARD_H
#define MY_GUARD_H
typedef int guarded_t;
#endif
""", incdirs=["."])
        self.assertIn("guarded_t", out)

    def test_nested_conditional(self):
        out = self.lower("""
#ifdef OUTER
#ifdef INNER
typedef int both_t;
#else
typedef int outer_only_t;
#endif
#endif
""", defines=["OUTER"], incdirs=["."])
        self.assertIn("outer_only_t", out)
        self.assertNotIn("both_t", out)


class TestAliasInTypedefTarget(Base):
    """An alias built on another alias.

    From litehtml, which spells nearly every container this way:

        typedef std::string             tstring;      // os_types.h
        typedef std::vector<tstring>    string_vector;

    The alias substitution skipped each typedef *declaration* whole, so
    the `tstring` on the right-hand side survived and the template was
    monomorphised over the alias rather than over what it names --
    `vector_litehtml_tstring`, a struct over a type nothing declared.

    Only the declared name needs holding back (rewriting
    `typedef vector_int int_vector;` in place would give
    `typedef vector_int vector_int;`); everything else in a typedef is a
    type like any other.
    """

    def test_alias_on_the_right_is_resolved(self):
        out = self.assertLowers("""
#include <vector>
#include <string>
typedef std::string tstring;
typedef std::vector<tstring> string_vector;
class Holder { public: string_vector v; };
void f(void) { Holder h; use(&h); }
""", "vector_string")
        self.assertNotIn("vector_tstring", out)

    def test_namespaced_alias_on_the_right_is_resolved(self):
        out = self.assertLowers("""
#include <vector>
#include <string>
namespace n {
    typedef std::string tstring;
    typedef std::vector<tstring> string_vector;
    class Holder { public: string_vector v; };
}
void f(void) { n::Holder h; use(&h); }
""", "vector_string")
        self.assertNotIn("vector_tstring", out)
        self.assertNotIn("vector_n_tstring", out)

    def test_alias_keeps_its_own_name(self):
        """The declared name still is not rewritten to its target."""
        out = self.assertLowers("""
#include <vector>
typedef std::vector<int> int_vector;
void f(void) { int_vector v; v.push_back(1); }
""", "int_vector")
        self.assertNotIn("typedef vector_int vector_int;", out)


class TestFlattenCollision(Base):
    """From `litehtml/src/html.cpp`.

    Flattening `litehtml::join_string` gives `litehtml_join_string`, which
    the file already declares, so the two would become one symbol. The
    refusal is right in general. What is worth checking is whether the
    collision is *real* -- a declaration and a definition of the same
    entity are not two entities, and litehtml declares
    `litehtml_join_string` in a header and defines `litehtml::join_string`
    in the namespace.
    """

    def test_genuine_collision_is_still_reported(self):
        self.refuses("""
int n_x(void) { return 1; }
namespace n { int x(void) { return 2; } }
""", "one symbol")


def _main():
    argv = [a for a in sys.argv[1:] if a != "--failing"]
    if "--failing" in sys.argv[1:]:
        print("(--failing: run the whole file; open gaps are the failures)")
    unittest.main(argv=[sys.argv[0]] + argv, verbosity=2)


if __name__ == "__main__":
    _main()
