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


# ------------------------------------------------------------------ set

class TestStdSet(Base):
    """`std::set<T>`, a sorted array keyed by `__cpp_cmp`.

    `map` stores insertion-ordered and scans linearly because, when it was
    written, there was no way to ask whether one `K` sorted before another.
    `__cpp_cmp` is that way -- a three-way comparison, two `<`s for a
    scalar and a `compare` method for a class -- so `set` binary-searches
    and, more importantly, iterates in the order `std::set` promises.
    """

    def test_int_set_is_supplied_and_sorted(self):
        out = self.assertLowers("""
#include <set>
int f(void) {
    std::set<int> s;
    s.insert(5);
    s.insert(1);
    return s.size();
}
""", "set_int_insert", "set_int_new")
        # The ordering test is the binary search: a linear container would
        # not need one, so its presence is what says this is sorted.
        self.assertIn("lower_index", out)

    def test_scalar_ordering_is_a_plain_comparison(self):
        """`__cpp_cmp(int, ..)` is `<`, not a call to a method int has not got."""
        out = self.assertLowers("""
#include <set>
int f(void) { std::set<int> s; s.insert(2); return s.count(2); }
""")
        self.assertNotIn("int_compare", out)

    def test_scalar_ordering_does_not_subtract(self):
        """`a - b` overflows for wide or unsigned types and inverts the order."""
        out = self.assertLowers("""
#include <set>
int f(void) { std::set<int> s; s.insert(2); return s.count(2); }
""")
        self.assertIn("? -1 :", out)

    def test_string_set_orders_by_the_compare_method(self):
        """A class element dispatches to `T_compare`, which `string` now has."""
        self.assertLowers("""
#include <set>
#include <string>
int f(void) {
    std::set<std::string> s;
    std::string a("pear");
    s.insert(a);
    return s.size();
}
""", "string_compare")

    def test_element_without_compare_is_reported(self):
        """Not silently ordered by address, which would iterate arbitrarily."""
        self.refuses("""
#include <set>
class K {
public:
    int v;
    int equals(const K &o) { return v == o.v; }
};
int f(void) { std::set<K> s; K k; s.insert(k); return s.size(); }
""", "no `compare`", "int compare(const K &o)")

    def test_compare_alone_is_enough_for_an_element(self):
        """The point of three-way: no separate `equals` to keep in step."""
        self.assertLowers("""
#include <set>
class K {
public:
    int v;
    int compare(const K &o) { if (v < o.v) { return -1; }
                              if (o.v < v) { return 1; } return 0; }
};
int f(void) { std::set<K> s; K k; s.insert(k); return s.size(); }
""", "K_compare")

    def test_owning_element_is_destroyed_with_the_set(self):
        """`clear` runs the element destructor, so `set<string>` does not leak."""
        self.assertLowers("""
#include <set>
#include <string>
int f(void) {
    std::set<std::string> s;
    std::string a("fig");
    s.insert(a);
    return s.size();
}
""", "string_drop")

    def test_set_is_not_supplied_when_unnamed(self):
        """An unused template would still be monomorphised, so it is not added."""
        out = self.assertLowers("int f(void) { return 0; }")
        self.assertNotIn("lower_index", out)


# ------------------------------------------------------- <algorithm>

class TestStdAlgorithm(Base):
    """`lower_bound`/`upper_bound`/`binary_search` over a `T *` range.

    Free *function* templates, which the subset already monomorphised --
    but only from an explicit `f<T>(..)`, since argument deduction is not
    implemented. A range is a pair of pointers because that is what every
    container here already hands out.
    """

    def test_deduced_call_is_reported_not_blanked(self):
        """The bug this found: no instantiation blanked the body silently.

        The call then survived over a definition that had just been erased
        and failed at link time naming a symbol the source never wrote.
        """
        self.refuses("""
template<typename T>
T twice(T x) { return x + x; }
int f(void) { return twice(3); }
""", "no template arguments", "twice<T>(..)")

    def test_explicit_instantiation_still_works(self):
        """The deduction check must not catch the form that does work."""
        self.assertLowers("""
template<typename T>
T twice(T x) { return x + x; }
int f(void) { return twice<int>(3); }
""", "int twice_int(int x)", "return twice_int(3)")

    def test_unused_template_is_still_blanked(self):
        """A template the file never calls emits nothing, as in C++."""
        out = self.assertLowers("""
template<typename T>
T twice(T x) { return x + x; }
int f(void) { return 0; }
""")
        self.assertNotIn("twice", out)

    def test_scalar_range_lowers(self):
        self.assertLowers("""
#include <algorithm>
#include <vector>
int f(void) {
    std::vector<int> v;
    v.push_back(2);
    return (int)(std::lower_bound<int>(v.begin(), v.end(), 2) - v.begin());
}
""", "lower_bound_int")

    def test_class_element_takes_the_key_by_reference(self):
        """`__cpp_ref(T)` expanded per instantiation, once `T` is concrete.

        Before this it reached the C as `__cpp_ref(string)`, an unknown
        type name -- the class emitter expands it for a *method*, and a
        free template has no class to be expanded against.
        """
        self.assertLowers("""
#include <algorithm>
#include <set>
#include <string>
int f(void) {
    std::set<std::string> s;
    std::string q("fig");
    std::string *lo = s.begin();
    std::string *hi = s.end();
    return std::binary_search<std::string>(lo, hi, q);
}
""", "binary_search_string", "const string * v")

    def test_algorithm_alone_is_reported(self):
        """No class in the unit means the builtin expansion never runs."""
        self.refuses("""
#include <algorithm>
int f(void) {
    int a[4];
    return (int)(std::lower_bound<int>(a, a + 4, 2) - a);
}
""", "__cpp_cmp", "survived")


# --------------------------------------------- nested calls by reference

class TestNestedCallInRefArg(Base):
    """A method call inside an argument list of a by-reference call.

    Pre-existing, and not about `std` at all: rewriting a call that takes a
    reference *consumed* its arguments -- the scan resumed past the closing
    paren -- so a method call nested in one was never visited, and reached
    the C as `take(&a, a.get())`, which is not a function that exists. The
    fixed-point loop could not help: every pass made the same jump.
    """

    def test_nested_method_call_is_lowered(self):
        self.assertLowers("""
class Box {
public:
    int v;
    Box() { v = 7; }
    int get() { return v; }
};
void take(const Box &b, int k);
int f(void) {
    Box a;
    take(a, a.get());
    return 0;
}
""", "take(&a, Box_get(&a))")

    def test_plain_c_argument_still_gets_the_address(self):
        """The wait must be bounded: a receiver that will never be rewritten
        must not defer, or the loop runs out with no `&` inserted at all."""
        self.assertLowers("""
struct raw { int n; };
int rawget(struct raw *r);
class Box {
public:
    int v;
    Box() { v = 7; }
};
void take(const Box &b, int k);
int f(void) {
    Box a;
    struct raw r;
    take(a, rawget(&r));
    return 0;
}
""", "take(&a, rawget(&r))")

    def test_container_iterator_nested_in_an_algorithm_call(self):
        """What the fix was for: `s.begin()` inside `lower_bound<T>(..)`."""
        self.assertLowers("""
#include <algorithm>
#include <set>
#include <string>
int f(void) {
    std::set<std::string> s;
    std::string q("fig");
    return std::binary_search<std::string>(s.begin(), s.end(), q);
}
""", "set_string_begin(&s)", "set_string_end(&s)")


# ------------------------------------------------------------- sort

class TestStdSort(Base):
    """`sort` over a `T *` range, moving elements by representation."""

    def test_scalar_sort_lowers(self):
        self.assertLowers("""
#include <algorithm>
#include <vector>
int f(void) {
    std::vector<int> v;
    v.push_back(2);
    std::sort<int>(v.begin(), v.end());
    return 0;
}
""", "sort_int")

    def test_owning_element_moves_rather_than_assigns(self):
        """No copy constructor call and no destructor in the sort body.

        An element is relocated, not copied and destroyed, so it keeps its
        one owner -- which is what lets an owning type be sorted at all
        without `operator=`.
        """
        out = self.assertLowers("""
#include <algorithm>
#include <vector>
#include <string>
int f(void) {
    std::vector<std::string> v;
    std::string a("pear");
    v.push_back(a);
    std::sort<std::string>(v.begin(), v.end());
    return 0;
}
""", "sort_string")
        body = out[out.index("sort_string"):]
        body = body[:body.index("\n}")]
        self.assertNotIn("string_copy", body)
        self.assertNotIn("string_drop", body)

    def test_addr_builtin_spells_the_right_operand(self):
        """`__cpp_addr` is an address for a class and the value for a scalar."""
        out = self.assertLowers("""
#include <algorithm>
#include <vector>
int f(void) {
    std::vector<int> v;
    std::sort<int>(v.begin(), v.end());
    return 0;
}
""")
        self.assertNotIn("__cpp_addr", out)


# --------------------------------------------------------- sorted map

class TestSortedMap(Base):
    """`map` moved onto `__cpp_cmp`: sorted, binary-searched, and owning.

    It was an unsorted array with a linear `find`, which also meant it
    iterated in *insertion* order -- quietly unlike `std::map`, so code
    that walked one and relied on the order was wrong with nothing
    reporting it.
    """

    def test_lookup_is_a_binary_search(self):
        out = self.assertLowers("""
#include <map>
int f(void) { std::map<int, int> m; m[1] = 2; return m.count(1); }
""")
        self.assertIn("lower_index", out)

    def test_key_class_needs_compare_not_equals(self):
        """The contract change: an order, not an equality."""
        self.refuses("""
#include <map>
class K { public: int v; K() { v = 0; } ~K() { } };
int f(void) { std::map<K, int> m; K k; return m.count(k); }
""", "no `compare`")

    def test_owning_key_and_value_are_both_destroyed(self):
        """`~map` freed only the array, leaking every owning key in it."""
        self.assertLowers("""
#include <map>
#include <string>
int f(void) {
    std::map<std::string, std::string> m;
    std::string k("a");
    std::string v("b");
    m[k] = v;
    return m.size();
}
""", "map_string_string_clear", "string_drop")

    def test_a_new_value_is_zeroed(self):
        """`std::map` value-initialises; `realloc` storage holds junk.

        Reading `m[absent]` gave whatever was in the block, and a
        `map<K,string>` would have destroyed a pointer nobody set.
        """
        self.assertLowers("""
#include <map>
int f(void) { std::map<int, int> m; return m[7]; }
""", "memset")

    def test_erase_shifts_by_representation(self):
        """Relocation, not assignment: an owning entry keeps its one owner
        and is not destroyed a second time on the way past."""
        self.assertLowers("""
#include <map>
#include <string>
int f(void) {
    std::map<std::string, int> m;
    std::string k("a");
    m[k] = 1;
    m.erase(k);
    return m.size();
}
""", "memmove")


# ------------------------------------------------- argument deduction

class TestPartialExplicitArguments(Base):
    """`align_up<64>(x)` -- some arguments spelled, the rest deduced.

    Explicit arguments bind to the *leading* parameters, so only the tail
    is deduced -- from a function parameter written `P name` or `P *name`
    against the argument in that position. The call is then rewritten the
    long way and the ordinary substitution runs, the same one code path
    the fully-bare deduction already takes.
    """

    def test_trailing_type_is_deduced_from_a_value(self):
        self.assertLowers("""
template<int A, typename X>
X align_up(X x) { return (x + (A - 1)) & ~(A - 1); }
int f(void) { int v = 123; return align_up<64>(v); }
""", "align_up_64_int(v)", "align_up_64_int(int x)")

    def test_an_untypeable_tail_keeps_the_arity_refusal(self):
        """If any missing argument cannot be typed, the call is left
        exactly as written and the arity check reports it as before."""
        self.refuses("""
template<int A, typename X>
X align_up(X x) { return x; }
int g(void);
int f(void) { return align_up<64>(g()); }
""", "template argument")


class TestFunctionTemplateOverloads(Base):
    """Function templates overload, and this pass keys them by name.

    coost's `god.h` has four `align_up`. Every one of them used to see
    every call: a call in one overload's body was read as a call to its
    siblings with too few arguments, and two overloads instantiating the
    same call lowered to one symbol, the second redefining the first.
    """

    def test_a_call_in_a_sibling_body_is_not_an_arity_error(self):
        """`align_up<A>((size_t)x)` names the *enclosing* template's own
        parameter; it cannot be instantiated until that template is."""
        self.assertLowers("""
template<int A, typename X>
X align_up(X x) { return (x + (A - 1)) & ~(A - 1); }
template<int A, typename X>
X* align_up(X* x) { return (X*)align_up<A>((unsigned long)x); }
int f(void) { int v = 4; return align_up<64>(v); }
""", "align_up_64_int(int x)")

    def test_value_and_pointer_overloads_do_not_collide(self):
        """Selection is on argument count and pointer-ness -- the whole of
        overload resolution these need. Without it both overloads emitted
        `align_up_64_int` and the second redefined the first."""
        out = self.lower("""
template<int A, typename X>
X align_up(X x) { return x; }
template<int A, typename X>
X* align_up(X* x) { return x; }
int f(void) { int v = 4; int *p = &v;
    int a = align_up<64>(v); int *b = align_up<64>(p);
    return a + *b; }
""")
        self.assertEqual(out.count("int align_up_64_int(int x)"), 1)
        self.assertEqual(out.count("int* align_up_64_int(int* x)"), 1)


class TestParameterPacks(Base):
    """A trailing type pack in a *free function* template.

    Unrolled per instantiation: `sum<A,B,C>` becomes an ordinary function
    whose body calls `sum<B, C>` -- a spelling that exists only in the
    substituted copy, so a worklist scans each new copy for further calls,
    to a fixpoint. Bounded, because every derived call has strictly fewer
    template arguments than the one it came from. A class-template pack
    (coost's `is_same` struct) stays refused.
    """

    def test_recursive_consume_unrolls_to_the_base_overload(self):
        out = self.lower("""
int sum() { return 0; }
template<typename X, typename ...V>
int sum(X x, V... v) { return x + sum(v...); }
int f(void) { return sum<int, int, int>(1, 2, 3); }
""")
        # Three instantiations, each one element shorter, bottoming out at
        # the plain nullary overload.
        self.assertIn("sum_int_int_int(1, 2, 3)", out)
        self.assertIn("sum_int_int(", out)
        self.assertIn("+ sum()", out)

    def test_forward_is_pass_through(self):
        """Every element parameter is spelled concretely, so there is no
        reference collapsing to preserve; `V&&` becomes by-value."""
        out = self.lower("""
void emit() { }
template<typename X, typename ...V>
void emit(X x, V&& ... v) { (void)x; emit(std::forward<V>(v)...); }
int f(void) { emit<int, int>(1, 2); return 0; }
""")
        self.assertIn("emit_int_int(int x, int __pk1)", out)
        self.assertIn("emit_int(__pk1)", out)
        self.assertNotIn("&&", out.split("int f(void)")[0]
                         .split("emit_int_int")[-1].split(")")[0])

    def test_an_empty_pack_erases_its_comma(self):
        """`f(x, v...)` with nothing in the pack is `f(x)` -- which is how
        the recursion reaches a plain overload."""
        out = self.lower("""
int last(int x) { return x; }
template<typename X, typename ...V>
int last(X x, V... v) { (void)x; return last(v...); }
int f(void) { return last<int, int>(1, 2); }
""")
        self.assertIn("return last(__pk1)", out)

    def test_too_few_arguments_names_at_least(self):
        self.refuses("""
template<typename X, typename Y, typename ...V>
int take(X x, Y y, V... v) { (void)x; (void)y; return 0; }
int f(void) { return take<int>(1); }
""", "at least 2")

    def test_a_class_template_pack_is_still_refused(self):
        self.refuses("""
template<typename ...T>
struct is_same { static const bool value = false; };
int f(void) { return is_same<int, int>::value; }
""", "parameter pack")


class TestConstructorTemporaries(Base):
    """`Cls(a, b).method()` -- a construction in expression position.

    Hoisted to just before the statement that contains it, which is the
    same move an inlined lambda body makes and carries the same soundness
    rule: only where the construction is evaluated exactly once and
    unconditionally.
    """

    def test_a_method_on_a_temporary_is_hoisted(self):
        out = self.lower("""
struct pt {
    int x; int y;
    pt(int a, int b) { x = a; y = b; }
    int sum() { return x + y; }
};
int f(void) { return pt(1, 2).sum(); }
""")
        self.assertIn("pt_new(&__cpp_tmp0, 1, 2)", out)
        self.assertIn("pt_sum(&__cpp_tmp0)", out)

    def test_a_ternary_branch_is_reported(self):
        """A branch may not be evaluated, so the temporary cannot be
        hoisted out of it. This used to pass through into C that did not
        compile."""
        self.refuses("""
struct pt { int x; pt(int a) { x = a; } };
int f(void) { int c = 1; pt q = c ? pt(3) : pt(5); return q.x; }
""", "may not be evaluated")

    def test_an_initializer_list_is_not_a_ternary(self):
        """`fastring() : fast::stream() {}` starts with a `:` and is not a
        conditional branch. Reading it as one refused 140 files."""
        out = self.lower("""
struct base { int b; base(int x) { b = x; } };
struct derived : public base {
    derived() : base(7) { }
};
int f(void) { derived d; return d._base.b; }
""")
        self.assertIn("base_new(&this->_base, 7)", out)


class TestFunctionalCasts(Base):
    """`uint64_t(1)` and `int(x)` -- a type written like a call.

    C has no such spelling, and it survived into the output: coost's
    `dtoa_milo.h` writes `l & (uint64_t(1) << 63)`, which the C front end
    read as a call to something named `uint64_t`.
    """

    def test_a_builtin_type_call_becomes_a_cast(self):
        out = self.lower("""
int f(void) { return int(3.7); }
""")
        self.assertIn("((int)(3.7))", out)

    def test_a_multiword_type_works(self):
        out = self.lower("""
typedef unsigned long long u64;
int f(void) { u64 l = 5; return (l & (u64(1) << 2)) != 0; }
""")
        self.assertIn("((unsigned long long)(1))", out)

    def test_a_function_pointer_declaration_is_not_a_cast(self):
        """`int (*g)(int) = ..` declares a function pointer. Read as a
        cast it became `((int)(*g))(int) = ..`, which broke every lambda
        binding in the suite."""
        out = self.lower("""
static int helper(int y) { return y * 2; }
void f(void) { int (*g)(int) = helper; (void)g; }
""")
        self.assertIn("int (*g)(int) = helper;", out)

    def test_a_constructor_call_is_not_a_cast(self):
        """A class name followed by parentheses is a construction, and
        rewriting it as a cast would be silently wrong."""
        out = self.lower("""
struct pt { int x; pt(int a) { x = a; } };
int f(void) { pt p(2); return p.x; }
""")
        self.assertNotIn("((pt)", out)


class TestConstructorCallReturns(Base):
    """`return Cls(a, b);` for a class with no destructor.

    An owning class is refused here -- there is nothing to move out of --
    but a non-owning one slipped through and reached the C front end
    verbatim. coost's `fast.h` has seventeen, one per `dp::_1` .. `dp::_9`.
    Rewritten into the declaration form the initialiser lowering already
    handles, rather than a second path that could drift from it.
    """

    def test_it_becomes_a_named_local(self):
        out = self.lower("""
struct pt { int x; int y; pt(int a, int b) { x = a; y = b; } };
pt mk(int a) { return pt(a, a * 2); }
int f(void) { pt p = mk(3); return p.x + p.y; }
""")
        self.assertIn("pt_new(&__cpp_ret0, a, a * 2)", out)
        self.assertIn("return __cpp_ret0;", out)
        self.assertNotIn("return pt(", out)

    def test_a_plain_call_is_untouched(self):
        """Only a *class* name is materialised; an ordinary function
        returning a value is left exactly as written."""
        out = self.lower("""
struct pt { int x; };
int side(int a) { return a; }
int f(void) { return side(2); }
""")
        self.assertIn("return side(2);", out)
        self.assertNotIn("__cpp_ret", out)


class TestStaticConstMembers(Base):
    """`static const int cap = 64;` is a class constant, not a field.

    C has no static data member. Treated as one, it put `static const int
    cap;` inside the struct -- which is not C -- moved the initialiser into
    the constructor, so every instance re-assigned a constant, and read
    every use through `this`. coost's vendored `dtoa_milo.h` declares seven
    of them, one defined in terms of another.
    """

    def test_it_becomes_a_file_scope_constant(self):
        out = self.lower("""
class box {
public:
    static const int cap = 64;
    int n;
    box() { n = cap; }
    int room() { return cap - n; }
};
int f(void) { box b; return b.room(); }
""")
        self.assertIn("static const int box_cap = 64;", out)
        self.assertNotIn("static const int cap;", out)
        self.assertIn("this->n = box_cap", out)
        self.assertNotIn("this->cap", out)

    def test_one_constant_may_use_another(self):
        out = self.lower("""
class box {
public:
    static const int cap = 64;
    static const int half = cap / 2;
    int n;
    box() { n = half; }
};
int f(void) { box b; return b.n; }
""")
        self.assertIn("static const int box_half = box_cap / 2;", out)


class TestGlobalScopeQualifier(Base):
    """`::free(p)` reaches the C library, not a member named `free`.

    coost's `system_allocator` has a static `free` whose body calls
    `::free(p)`. Namespace flattening first turned that into
    `::this->co_free(p)` -- invalid C *and* the wrong function. Stripping
    the `::` early fixed that and reintroduced it in another form: the bare
    name then resolved against the enclosing class, giving
    `this->co_free(p)` in a static method with no `this`. The marker is
    therefore carried through every name-resolving pass and removed last.
    """

    def test_a_global_call_is_not_a_member_call(self):
        out = self.lower("""
void *malloc(unsigned long);
void free(void *);
struct salloc {
    static void *alloc(unsigned long n) { return ::malloc(n); }
    static void free(void *p, unsigned long) { return ::free(p); }
};
int f(void) { void *p = salloc::alloc(8); salloc::free(p, 8); return 0; }
""")
        self.assertIn("{ return free(p); }", out)
        self.assertNotIn("this->", out)
        self.assertNotIn("::", out)

    def test_the_marker_never_reaches_the_output(self):
        out = self.lower("""
void free(void *);
int f(void) { void *p = 0; ::free(p); return 0; }
""")
        self.assertNotIn("__gsq__", out)


class TestRecursiveNonTypeTemplates(Base):
    """`copy<N>` calling `copy<N - 1>`, terminated by `copy<0>`.

    Three pieces have to agree: the argument arithmetic is evaluated, so
    `copy<4 - 1>` and `copy<3>` are one instantiation; the explicit
    specialisation is collected and emitted under the mangled name the
    general template's instantiations use; and the general template is not
    instantiated for the arguments the specialisation defines, which is
    what stops the recursion.
    """

    def test_the_whole_chain_is_emitted(self):
        out = self.lower("""
template<int N>
int sum_to() { return N + sum_to<N - 1>(); }
template<>
int sum_to<0>() { return 0; }
int f(void) { return sum_to<4>(); }
""")
        for n in ("sum_to_4", "sum_to_3", "sum_to_2", "sum_to_1", "sum_to_0"):
            self.assertIn(n, out)
        # The specialisation defines `sum_to_0`; the general template must
        # not also emit one, or the second redefines the first.
        self.assertEqual(out.count("int sum_to_0()"), 1)
        self.assertNotIn("template<>", out)
        self.assertNotIn("sum_to_-1", out)

    def test_arithmetic_arguments_are_one_instantiation(self):
        out = self.lower("""
template<int N>
int val() { return N; }
int f(void) { return val<2 + 1>() + val<3>(); }
""")
        self.assertIn("val_3", out)
        self.assertEqual(out.count("int val_3()"), 1)

    def test_a_name_in_the_argument_is_left_alone(self):
        """Only literal arithmetic is evaluated; a name could be a type."""
        out = self.lower("""
template<typename T>
T pick(T a) { return a; }
int f(void) { int x = 1; return pick<int>(x); }
""")
        self.assertIn("pick_int", out)

    def test_a_recursion_with_no_reachable_base_is_reported(self):
        self.refuses("""
template<int N>
int bad() { return bad<N + 1>(); }
int f(void) { return bad<1>(); }
""", "instantiated more than", "base case")


class TestConstexprConstructor(Base):
    """`constexpr fastring() noexcept : fast::stream() {}`.

    A constructor is recognised by its signature being exactly the class
    name, and `constexpr` sat in front of it -- so it was not recognised as
    a constructor at all. coost's `fast::stream` then appeared to have no
    default constructor, and every derived class's `: fast::stream()` was
    refused. Dropped beside `explicit` and `final`: the lowering emits an
    ordinary function either way.
    """

    def test_a_constexpr_default_constructor_is_a_constructor(self):
        self.assertLowers("""
class base {
public:
    int a;
    constexpr base() noexcept : a(0) { }
};
class derived : public base {
public:
    constexpr derived() noexcept : base() { }
};
int f(void) { derived d; return d._base.a; }
""", "base_new(&this->_base)")

    def test_constexpr_does_not_reach_the_output(self):
        out = self.lower("""
class point {
public:
    int x;
    constexpr point() noexcept : x(1) { }
    constexpr int get() const { return x; }
};
int f(void) { point p; return p.get(); }
""")
        self.assertNotIn("constexpr", out)


class TestExportMacroClassName(Base):
    """`class __coapi fastring : public fast::stream`.

    An export/visibility macro between the keyword and the name is the
    ordinary way a library marks a type for a shared build. Every class
    scan reads the name as the first word after `class`, so all of them
    collected the *macro*: coost's `fastring` was collected as `__coapi`,
    its members went missing, and its constructors' initializer lists never
    bound -- the failure surfaced far away, as a `std::move` operand that
    could not be named in a scope that was empty because the class holding
    it was never really there.
    """

    def test_the_class_is_collected_under_its_real_name(self):
        self.assertLowers("""
#define API

class API point {
public:
    int x;
    point() { x = 3; }
    int get() { return x; }
};
int f(void) { point p; return p.get(); }
""", "point_get", "point_new")

    def test_an_attribute_bodied_macro_counts_too(self):
        self.assertLowers("""
#define API __attribute__((visibility("default")))

class API point {
public:
    int x;
    point() { x = 3; }
    int get() { return x; }
};
int f(void) { point p; return p.get(); }
""", "point_get")

    def test_a_base_clause_still_binds(self):
        """The shape that actually broke: with the name misread, a derived
        class's base-init never bound."""
        out = self.lower("""
#define API

class base {
public:
    int b;
    base() { b = 1; }
};
class API derived : public base {
public:
    int d;
    derived() : base() { d = 2; }
};
int f(void) { derived x; return x.d + x._base.b; }
""")
        self.assertIn("derived_new", out)
        self.assertIn("base_new(&this->_base)", out)

    def test_an_unknown_second_word_is_left_alone(self):
        """Only a macro this unit defines, with an empty or attribute body.
        Anything else is not something this can identify, so it stays
        exactly where it was rather than being guessed at."""
        out = self.lower("""
class NotAMacro point {
public:
    int x;
};
int f(void) { point p; return p.x; }
""")
        self.assertIn("class NotAMacro point", out)


class TestIndexOperatorCollision(Base):
    """A const/non-const `operator[]` pair lowered to one `T__index` symbol
    with no diagnostic -- invalid C where `operator=` in the same shape was
    already refused. coost's `fast::stream` declares exactly this pair."""

    def test_the_pair_is_refused_not_collided(self):
        self.refuses("""
class buf {
public:
    char _p[16];
    char& operator[](unsigned long i) { return _p[i]; }
    const char& operator[](unsigned long i) const { return _p[i]; }
};
int f(void) { buf b; b[0] = 'x'; return 0; }
""", "two `operator[]` overloads", "keep the non-const one")

    def test_one_operator_still_lowers(self):
        self.assertLowers("""
class buf {
public:
    char _p[16];
    char& operator[](unsigned long i) { return _p[i]; }
};
int f(void) { buf b; b[0] = 'x'; return 0; }
""", "buf__index")


class TestMacroBodyTemplates(Base):
    """A `#define` body is not code.

    coost's `DEF_has_method(f)` macro holds a whole function template whose
    name pastes with `##f`; the collector read `##f(` as a template named
    `f` and then refused the author's own `int f()` as a bare call to it.
    The monomorphiser's scan now goes through `_blank_directives`, which
    existed for exactly this hazard but was never applied to this pass.
    """

    def test_a_template_inside_a_define_is_not_collected(self):
        self.assertLowers("""
#define DEF_thing(f) \\
template<typename _T_> \\
int has_##f() { return 0; }
int f(void) { return 1; }
""", "int f(void) { return 1; }")

    def test_a_class_inside_a_define_is_not_collected(self):
        """coost's `DEF_has_method(f)` macro holds a whole class, nested
        struct and all. The class collector read them as real classes and
        the emitter rewrote *inside the macro*, breaking its backslash
        continuation chain -- so the tail stopped being a `#define` body
        and reached the C front end as code."""
        out = self.lower("""
#define DEF_has(f) \\
struct _has_##f { \\
    struct _R_ { int _[2]; }; \\
    int value; \\
}

struct point { int x; };
int f(void) { struct point p; p.x = 1; return p.x; }
""")
        self.assertIn("#define DEF_has(f)", out)
        self.assertIn("struct _R_ { int _[2]; }; \\", out)

    def test_a_deleted_member_inside_a_define_is_not_a_declaration(self):
        """coost's `DISALLOW_COPY_AND_ASSIGN(T)` spells `T(const T&) =
        delete;` on a continuation line. `resolve_defaulted` matched it,
        then cut "back to the start of the member" -- through the `#define`
        head and twelve unrelated defines, to a typedef's semicolon
        fourteen lines up. The orphaned last continuation then reached the
        delete handler as a *statement*."""
        out = self.lower("""
typedef unsigned long long u64;

#define MAX_U64 ((u64) ~((u64)0))

#define DISALLOW_COPY(T) \\
    T(const T&) = delete; \\
    void operator=(const T&) = delete

int f(void) { u64 x = MAX_U64; return (int)(x & 1); }
""")
        self.assertIn("#define DISALLOW_COPY(T)", out)
        self.assertIn("#define MAX_U64", out)
        self.assertIn("typedef unsigned long long u64;", out)


class TestTemplateArgDeduction(Base):
    """`sort(v.begin(), v.end())` without spelling `<int>`.

    Deliberately narrow: a range is a pair of pointers, so typing the
    *first* argument types the call. A deduced call is rewritten to spell
    its arguments the long way and the ordinary substitution runs on that,
    so there is one code path for both forms rather than two that can
    drift apart.
    """

    def test_deduced_from_a_container_iterator(self):
        self.assertLowers("""
#include <algorithm>
#include <vector>
int f(void) {
    std::vector<int> v;
    std::sort(v.begin(), v.end());
    return 0;
}
""", "sort_int")

    def test_deduced_from_an_array(self):
        """An array decays to `T *`, so it types the call too."""
        self.assertLowers("""
#include <algorithm>
#include <vector>
int f(void) {
    std::vector<int> keep;
    int a[4];
    std::sort(a, a + 4);
    return 0;
}
""", "sort_int")

    def test_deduced_from_a_declared_pointer_local(self):
        self.assertLowers("""
#include <algorithm>
#include <vector>
#include <string>
int f(void) {
    std::vector<std::string> v;
    std::string *lo = v.begin();
    std::string *hi = v.end();
    std::sort(lo, hi);
    return 0;
}
""", "sort_string")

    def test_map_iterator_is_not_deduced(self):
        """`map::begin()` is a `pair<K,V> *`, not a `K *`.

        Deducing `K` from it would be wrong rather than merely
        unsupported, which is why `map` is left out of the containers
        deduction reads through.
        """
        self.refuses("""
#include <algorithm>
#include <map>
int f(void) {
    std::map<int,int> m;
    return (int)(std::lower_bound(m.begin(), m.end(), 1) - m.begin());
}
""", "could not be deduced")

    def test_an_untypeable_argument_is_reported(self):
        """A call result has no declaration to read, so it is refused."""
        self.refuses("""
#include <algorithm>
#include <vector>
int *mystery(void);
int f(void) {
    std::vector<int> v;
    std::sort(mystery(), mystery() + 4);
    return 0;
}
""", "could not be deduced")

    def test_a_non_pointer_parameter_is_not_deduced(self):
        """Deduction reads a parameter written `T *` and nothing else."""
        self.refuses("""
template<typename T>
T twice(T x) { return x + x; }
int f(void) { return twice(3); }
""", "could not be deduced")

    def test_explicit_arguments_still_work(self):
        """The long form is unchanged -- and is what deduction produces."""
        self.assertLowers("""
#include <algorithm>
#include <vector>
int f(void) {
    std::vector<int> v;
    std::sort<int>(v.begin(), v.end());
    return 0;
}
""", "sort_int")


# ------------------------------------------------- priority_queue

class TestPriorityQueue(Base):
    """A max-heap in an array, sifted by hole rather than by swapping."""

    def test_scalar_queue_lowers(self):
        self.assertLowers("""
#include <queue>
int f(void) {
    std::priority_queue<int> q;
    q.push(3);
    q.pop();
    return q.size();
}
""", "priority_queue_int_push", "priority_queue_int_pop")

    def test_element_class_needs_compare(self):
        """Ordering is `__cpp_cmp`, so a class element supplies `compare`."""
        self.refuses("""
#include <queue>
class K { public: int v; K() { v = 0; } ~K() { } };
int f(void) {
    std::priority_queue<K> q;
    K k;
    q.push(k);
    return q.size();
}
""", "no `compare`")

    def test_owning_element_is_relocated_not_assigned(self):
        """Sifting moves by representation, so no copy or destroy per step.

        Two live copies of an owning object never exist at once, which is
        what lets an owning element be heaped without `operator=`.
        """
        out = self.assertLowers("""
#include <queue>
#include <string>
int f(void) {
    std::priority_queue<std::string> q;
    std::string a("pear");
    q.push(a);
    return q.size();
}
""", "priority_queue_string_push")
        # The definition, not the prototype: take the occurrence that is
        # followed by a body.
        at = out.index("priority_queue_string_pop(priority_queue_string "
                       "*this) {")
        body = out[at:out.index("\n", at)]
        self.assertNotIn("string_copy", body)

    def test_abandoned_queue_frees_its_elements(self):
        """`~priority_queue` clears before freeing the block."""
        self.assertLowers("""
#include <queue>
#include <string>
int f(void) {
    std::priority_queue<std::string> q;
    std::string a("pear");
    q.push(a);
    return q.size();
}
""", "priority_queue_string_clear", "string_drop")


# --------------------------------------------------- reference returns

class TestScalarRefReturn(Base):
    """`int &get()` -- documented as rejected, but only classes were checked.

    A reference return of a built-in type reached the C as `int_&get`,
    which is not an identifier, with no diagnostic at all. Found while
    writing `priority_queue::top()`.
    """

    def test_scalar_reference_return_is_reported(self):
        self.refuses("""
class B {
public:
    int v;
    B() { v = 1; }
    int &get() { return v; }
};
int f(void) { B b; return b.get(); }
""", "int&", "not in the C++ subset")

    def test_operator_index_may_still_return_one(self):
        """It is required there: a by-value subscript would make `v[i] = x`
        write to a copy."""
        self.assertLowers("""
#include <vector>
int f(void) {
    std::vector<int> v;
    v.push_back(1);
    return v[0];
}
""", "vector_int__index")


# ------------------------------------------------------ line numbers

class TestDiagnosticLineNumbers(Base):
    """A reported line has to be a line the author can open.

    Two bugs compounded here. The supplied `std` prelude is hundreds of
    lines of `string`, `vector` and `map` prepended above the source, and
    counting from the start of the buffer named line 6 as line 197. On top
    of that, each `#include <vector>` was *deleted*, shifting everything
    below it up by one -- so the more headers a file used, the further off
    the number got.
    """

    def _line_of(self, src, *needles):
        msg = self.refuses(src, *needles)
        mm = __import__("re").search(r":(\d+):", msg)
        self.assertIsNotNone(mm, "no line number in: %s" % msg)
        return int(mm.group(1))

    def test_line_is_the_authors_not_the_preludes(self):
        """One header: the prelude is above, the count starts below it."""
        self.assertEqual(self._line_of("""#include <vector>
class B {
public:
    int v;
    B() { v = 1; }
    int &get() { return v; }
};
int f(void) { B b; return b.get(); }
""", "not in the C++ subset"), 6)

    def test_each_dropped_include_keeps_its_line(self):
        """Three headers, so the old deletion bug would report line 3."""
        self.assertEqual(self._line_of("""#include <vector>
#include <string>
#include <map>
class B {
public:
    int v;
    B() { v = 1; }
    int &get() { return v; }
};
int f(void) { B b; return b.get(); }
""", "not in the C++ subset"), 8)

    def test_line_survives_monomorphisation_above_it(self):
        """Why a marker and not a recorded offset.

        Instantiating a template replaces its body with one copy per use,
        so the number of lines above the author's code depends on what the
        file asks for. A fixed offset would be wrong by however much that
        added; the marker moves with the text below it.
        """
        self.assertEqual(self._line_of("""#include <vector>
#include <map>
#include <set>
int g(void) {
    std::vector<int> a;
    std::map<int,int> b;
    std::set<int> c;
    return 0;
}
class B {
public:
    int v;
    B() { v = 1; }
    int &get() { return v; }
};
int f(void) { B b; return b.get(); }
""", "not in the C++ subset"), 14)

    def test_the_marker_does_not_reach_the_c(self):
        """It is this module's bookkeeping, not part of the output."""
        out = self.assertLowers("""#include <vector>
int f(void) { std::vector<int> v; v.push_back(1); return v[0]; }
""")
        self.assertNotIn("__crust_src_origin__", out)


class TestMemberParseLineNumbers(Base):
    """Class-body parse failures name the member's line.

    `_split_members` already received the line the class was declared on,
    so a member's line is that plus the newlines above it in the body. It
    runs before class emission, where text and source still correspond
    exactly -- which is the boundary that decides whether a line number can
    be trusted at all here.
    """

    def _line_of(self, src, *needles):
        msg = self.refuses(src, *needles)
        mm = __import__("re").search(r":(\d+):", msg)
        self.assertIsNotNone(mm, "no line number in: %s" % msg)
        return int(mm.group(1))

    def test_unparsable_member_names_its_line(self):
        self.assertEqual(self._line_of("""#include <vector>
class B {
public:
    int v;
    B() { v = 1; }
    zzz;
};
int f(void) { B b; return b.v; }
""", "cannot parse member"), 6)

    def test_the_line_is_the_members_not_the_classs(self):
        """A class declared on 2 with a bad member on 9 reports 9."""
        self.assertEqual(self._line_of("""#include <vector>
class B {
public:
    int a;
    int b;
    int c;
    int d;
    B() { a = 1; }
    zzz;
};
int f(void) { B x; return x.a; }
""", "cannot parse member"), 9)


# ------------------------------------------- stack/queue/array/optional

class TestContainerBatch(Base):
    """Four small containers, each with one design question of its own."""

    def test_stack_indexes_from_the_top(self):
        """`s[0]` and `top()` must agree, so index 0 counts down."""
        self.assertLowers("""
#include <stack>
int f(void) {
    std::stack<int> s;
    s.push(1);
    return s[0];
}
""", "stack_int_push", "stack_int__index")

    def test_queue_is_a_head_index_not_a_ring(self):
        """A wrapped range cannot be handed out as a pointer pair, which is
        the iteration every other container here offers."""
        self.assertLowers("""
#include <queue>
int f(void) {
    std::queue<int> q;
    q.push(1);
    q.pop();
    return q.size();
}
""", "queue_int_begin", "queue_int_end")

    def test_owning_queue_compacts_by_relocation(self):
        """Reclaiming popped space moves elements rather than copying them."""
        self.assertLowers("""
#include <queue>
#include <string>
int f(void) {
    std::queue<std::string> q;
    std::string a("x");
    q.push(a);
    q.pop();
    return q.size();
}
""", "memmove", "string_drop")

    def test_array_refuses_an_owning_element(self):
        """The bug this caught: `array<string,3>` segfaulted.

        Elements live in a plain array *member*, which this subset neither
        constructs nor destroys, so the first `fill` copy-constructed over
        garbage and followed a wild pointer. Refused now, pointing at
        `vector`, which does construct and destroy what it holds.
        """
        self.refuses("""
#include <array>
#include <string>
int f(void) {
    std::array<std::string, 3> a;
    std::string k("x");
    a.fill(k);
    return a.size();
}
""", "array<string, 3>", "Use `vector<string>`")

    def test_array_of_plain_data_is_fine(self):
        """A class with neither constructor nor destructor owns nothing and
        is exactly what `std::array` holds."""
        self.assertLowers("""
#include <array>
class P { public: int x; int y; };
int f(void) {
    std::array<P, 3> a;
    a[0].x = 1;
    return a[0].x;
}
""", "array_P_3")

    def test_optional_holds_its_value_behind_a_pointer(self):
        """A `T` member would be constructed and destroyed with the
        container, which is exactly what an optional must not do -- an
        empty one holds nothing, and `reset()` would then be a second
        destruction of what the epilogue also destroys."""
        out = self.assertLowers("""
#include <optional>
#include <string>
int f(void) {
    std::optional<std::string> o;
    std::string k("x");
    o.set(k);
    return o.has_value();
}
""", "optional_string_reset", "string_drop")
        self.assertIn("malloc", out)

    def test_optional_value_is_null_when_empty(self):
        self.assertLowers("""
#include <optional>
int f(void) {
    std::optional<int> o;
    return o.value() == 0;
}
""", "optional_int_value")


class TestMoreAlgorithms(Base):
    """`find`, `count`, `reverse`, `fill`, `min_element`, `max_element`."""

    def test_find_asks_equality_not_ordering(self):
        """`std::find` needs only `==`, so requiring `compare` would refuse
        a class that reasonably has equality and no order."""
        self.assertLowers("""
#include <algorithm>
#include <vector>
class K {
public:
    int v;
    K() { v = 0; }
    ~K() { }
    K(const K &o) { v = o.v; }
    int equals(const K &o) { return v == o.v; }
};
int f(void) {
    std::vector<K> v;
    K k;
    return (int)(std::find(v.begin(), v.end(), k) - v.begin());
}
""", "K_equals")

    def test_reverse_swaps_by_representation(self):
        """No copy or destroy per swap, so an owning element keeps its
        one owner and needs no `operator=`."""
        out = self.assertLowers("""
#include <algorithm>
#include <vector>
#include <string>
int f(void) {
    std::vector<std::string> v;
    std::reverse(v.begin(), v.end());
    return 0;
}
""", "reverse_string")
        at = out.index("void reverse_string(")
        self.assertNotIn("string_copy", out[at:out.index("\n", at)])

    def test_min_element_chains_onto_its_result(self):
        """The bug this caught: `min_element(..)->c_str()` was left as
        written, because a chain only ever started from a *symbol* that
        resolved to a class and a call result is not one."""
        self.assertLowers("""
#include <algorithm>
#include <vector>
#include <string>
int f(void) {
    std::vector<std::string> v;
    return (int)std::min_element(v.begin(), v.end())->size();
}
""", "string_size(min_element_string(")


class TestDeclarationShadowing(Base):
    """Deduction reads the *nearest* declaration above the call.

    `re.search` returns the first match in the file, and the supplied
    templates sit above the author's code with ordinary local names in
    them -- so `T *lo` inside `reverse` was found for a call whose `lo` was
    the author's `string *`, and the call deduced a type named `T`.
    """

    def test_a_prelude_local_does_not_shadow_the_authors(self):
        self.assertLowers("""
#include <algorithm>
#include <vector>
#include <string>
int f(void) {
    std::vector<std::string> v;
    std::string *lo = v.begin();
    std::string *hi = v.end();
    std::sort(lo, hi);
    return 0;
}
""", "sort_string")


class TestUnorderedAliases(Base):
    """`unordered_map`/`unordered_set` are the ordered ones renamed.

    Nothing here hashes, and nothing in this subset can write `hash<T>`
    generically, so a separate copy would have the unordered interface and
    the ordered behaviour. Aliasing says that rather than hiding it.
    """

    def test_unordered_map_is_map(self):
        self.assertLowers("""
#include <unordered_map>
int f(void) {
    std::unordered_map<int, int> m;
    m[1] = 2;
    return m.size();
}
""", "map_int_int_new")

    def test_unordered_set_is_set(self):
        self.assertLowers("""
#include <unordered_set>
int f(void) {
    std::unordered_set<int> s;
    s.insert(1);
    return s.size();
}
""", "set_int_insert")


class TestSwapAccumulateCopy(Base):
    """`swap`, `accumulate`, `copy`."""

    def test_swap_takes_pointers(self):
        """`std::swap` takes references, which cannot be spelled here.

        A `T &` parameter is lowered to `T *` only for a *class*, so
        `swap(int &, int &)` would keep its `&`; and `__cpp_ref(T)` gives a
        scalar by value, which is what a swap cannot have. Pointers are the
        one spelling that works for both.
        """
        self.assertLowers("""
#include <algorithm>
#include <vector>
int f(void) {
    std::vector<int> keep;
    int a = 1;
    int b = 2;
    std::swap(&a, &b);
    return a;
}
""", "swap_int(&a, &b)")

    def test_swap_moves_by_representation(self):
        """No copy or destroy, so an owning element keeps its one owner."""
        out = self.assertLowers("""
#include <algorithm>
#include <string>
#include <vector>
int f(void) {
    std::vector<int> keep;
    std::string x("a");
    std::string y("b");
    std::swap(&x, &y);
    return 0;
}
""", "swap_string")
        at = out.index("void swap_string(")
        self.assertNotIn("string_copy", out[at:out.index("\n", at)])

    def test_accumulate_and_copy(self):
        self.assertLowers("""
#include <algorithm>
#include <vector>
int f(void) {
    std::vector<int> v;
    std::vector<int> w;
    std::copy(v.begin(), v.end(), w.begin());
    return std::accumulate(v.begin(), v.end(), 0);
}
""", "accumulate_int", "copy_int")


class TestDeductionIgnoresThePrelude(Base):
    """Deduction never reads a declaration above the author's first line.

    Searching backwards for the nearest declaration was not enough on its
    own: `swap` declares a parameter `T *a`, above everything, so a call
    whose `a` was the author's `int a[4]` found the template's parameter
    and deduced a type literally named `T`. The prelude is now out of
    range entirely.
    """

    def test_a_template_parameter_does_not_answer_for_a_local(self):
        self.assertLowers("""
#include <algorithm>
#include <vector>
int f(void) {
    std::vector<int> keep;
    int a[4];
    std::sort(a, a + 4);
    return 0;
}
""", "sort_int")


class TestStringSubstringSearch(Base):
    """`find_str`, not an overload of `find`.

    `std::string` overloads `find` on `char` and `const char *`, which are
    the same arity -- and this subset resolves overloads by argument
    *count*, before types are known, so the two cannot be told apart.
    """

    def test_substring_search_lowers(self):
        self.assertLowers("""
#include <string>
int f(void) {
    std::string s("hello");
    return s.find_str("ell");
}
""", "string_find_str")

    def test_char_find_still_works_alongside_it(self):
        self.assertLowers("""
#include <string>
int f(void) {
    std::string s("hello");
    return s.find('e');
}
""", "string_find")


class TestRangeWriteSafety(Base):
    """`fill`/`copy` of an owning element need a constructed destination.

    Both destroy each destination before constructing over it, which is
    what assignment would have done and is right for a container's range.
    Handed raw storage they destroy garbage and follow whatever the bytes
    were -- a segfault, reproduced before this landed, and the same hazard
    `array<T,N>` of an owning element was refused for.
    """

    def test_owning_copy_into_raw_storage_is_refused(self):
        self.refuses("""
#include <algorithm>
#include <vector>
#include <string>
int f(void) {
    std::vector<std::string> v;
    std::string *raw = (std::string *)malloc(sizeof(std::string) * 2);
    std::copy(v.begin(), v.end(), raw);
    return 0;
}
""", "destroying each one", "not visibly a container's own range")

    def test_the_explicit_form_is_checked_too(self):
        """`copy<string>(..)` skips deduction but not this."""
        self.refuses("""
#include <algorithm>
#include <vector>
#include <string>
int f(void) {
    std::vector<std::string> v;
    std::string *raw = (std::string *)malloc(sizeof(std::string));
    std::copy<std::string>(v.begin(), v.end(), raw);
    return 0;
}
""", "destroying each one")

    def test_a_container_range_is_accepted(self):
        self.assertLowers("""
#include <algorithm>
#include <vector>
#include <string>
int f(void) {
    std::vector<std::string> v;
    std::vector<std::string> w;
    std::copy(v.begin(), v.end(), w.begin());
    return 0;
}
""", "copy_string")

    def test_one_level_of_alias_is_followed(self):
        """`T *dst = w.begin();` is the ordinary way to name a range, and
        refusing it would fire the check mostly on correct code."""
        self.assertLowers("""
#include <algorithm>
#include <vector>
#include <string>
int f(void) {
    std::vector<std::string> v;
    std::vector<std::string> w;
    std::string *dst = w.begin();
    std::copy(v.begin(), v.end(), dst);
    return 0;
}
""", "copy_string")

    def test_a_scalar_element_may_go_anywhere(self):
        """Plain data has nothing to destroy, so `__cpp_drop` is a no-op on
        it and raw storage is a perfectly good destination."""
        self.assertLowers("""
#include <algorithm>
#include <vector>
int f(void) {
    std::vector<int> v;
    int raw[4];
    std::fill(raw, raw + 4, 7);
    std::copy(v.begin(), v.end(), raw);
    return 0;
}
""", "fill_int", "copy_int")

    def test_fill_over_an_owning_range_is_accepted(self):
        self.assertLowers("""
#include <algorithm>
#include <vector>
#include <string>
int f(void) {
    std::vector<std::string> v;
    std::string k("x");
    std::fill(v.begin(), v.end(), k);
    return 0;
}
""", "fill_string")


class TestNumericHeader(Base):
    """`<numeric>` as its own header, now that it is more than one
    function."""

    def test_the_header_supplies_them(self):
        self.assertLowers("""
#include <numeric>
#include <vector>
int f(void) {
    std::vector<int> v;
    std::iota(v.begin(), v.end(), 1);
    return std::accumulate(v.begin(), v.end(), 0);
}
""", "iota_int", "accumulate_int")

    def test_accumulate_still_answers_to_its_name(self):
        """It lived in `<algorithm>` here until this header existed.

        A file that included only that one and called it would otherwise
        have stopped compiling, with a link error naming a function the
        author *did* write -- so the name is probed for as well as the
        header.
        """
        self.assertLowers("""
#include <algorithm>
#include <vector>
int f(void) {
    std::vector<int> v;
    return std::accumulate(v.begin(), v.end(), 0);
}
""", "accumulate_int")

    def test_partial_sum_and_adjacent_difference(self):
        self.assertLowers("""
#include <numeric>
#include <vector>
int f(void) {
    std::vector<int> v;
    std::partial_sum(v.begin(), v.end(), v.begin());
    std::adjacent_difference(v.begin(), v.end(), v.begin());
    return 0;
}
""", "partial_sum_int", "adjacent_difference_int")

    def test_a_string_element_accumulates(self):
        """`string` has `operator+` now, so there is a `+` for the supplied
        body to use. This used to be refused, and the refusal was right at
        the time: no binary operator was in the subset at all."""
        self.assertLowers("""
#include <numeric>
#include <vector>
#include <string>
int f(void) {
    std::vector<std::string> v;
    std::string z("");
    std::string s = std::accumulate(v.begin(), v.end(), z);
    return s.size();
}
""", "accumulate_string", "string__augadd")

    def test_a_class_without_the_operator_is_reported_against_the_call(self):
        """Not against `sum += *it`, a line inside a supplied template the
        author never wrote and cannot act on."""
        msg = self.refuses("""
#include <numeric>
#include <vector>
class point {
public:
    int x;
    point() { x = 0; }
};
int f(void) {
    std::vector<point> v;
    point z;
    point s = std::accumulate(v.begin(), v.end(), z);
    return s.x;
}
""", "combines elements with `+`", "does not declare `operator+`")
        self.assertNotIn("sum +=", msg)


class TestBinaryArithmeticOperators(Base):
    """`operator+` and friends, in the one case that has an honest lowering.

    A binary operator hands back a new object *by value*, which this subset
    cannot do for a class that owns something -- the local is destroyed on
    the way out and the caller gets a copy of a released object. For a
    class that owns nothing the struct copy is exactly what C++ does, so
    that is where the operator is available and the other case is reported.
    """

    def test_plain_data_class_gets_the_operator(self):
        self.assertLowers("""
class vec2 {
public:
    int x;
    vec2() { x = 0; }
    vec2 operator+(const vec2 &o) { vec2 r; r.x = x + o.x; return r; }
};
int f(void) {
    vec2 a;
    vec2 b;
    vec2 c = a + b;
    return c.x;
}
""", "vec2__binadd(&a, &b)")

    def test_an_owning_class_lowers(self):
        """This used to be refused, and the refusal was right at the time.

        A by-value return of an owning class was not in the subset at all.
        It is now -- a returned bare local is moved out -- so `buf r; ..;
        return r;` under `operator+` lowers exactly as the same body under
        an ordinary method name already did. `r` is left out of the drops
        on the return path and `c` is dropped once.
        """
        self.assertLowers("""
class buf {
public:
    char *p;
    buf() { p = 0; }
    ~buf() { free(p); }
    buf operator+(const buf &o) { buf r; return r; }
};
int f(void) { buf a; buf b; buf c = a + b; return 0; }
""", "buf c = buf__binadd(&a, &b)")

    def test_an_owning_chain_is_still_reported(self):
        """A *run* needs the by-value front door, which an owning class
        does not get: passing one by value would make a second owner of the
        same buffer with no copy constructor run.
        """
        self.refuses("""
class buf {
public:
    char *p;
    buf() { p = 0; }
    ~buf() { free(p); }
    buf operator+(const buf &o) { buf r; return r; }
};
int f(void) { buf a; buf b; buf c; buf d = a + b + c; return 0; }
""", "owns a resource")

    def test_scalars_are_untouched(self):
        """The receiver has to resolve to a class with the operator, so
        ordinary arithmetic is left exactly as written."""
        self.assertLowers("""
class vec2 {
public:
    int x;
    vec2() { x = 0; }
    vec2 operator+(const vec2 &o) { vec2 r; r.x = x + o.x; return r; }
};
int f(void) {
    int i = 3;
    int j = 4;
    return i + j;
}
""", "return i + j")

    def test_compound_assignment_is_not_eaten(self):
        """`a += b` is a different lowering, and the `+` must not be taken
        for the binary one."""
        self.assertLowers("""
class vec2 {
public:
    int x;
    vec2() { x = 0; }
    vec2 operator+(const vec2 &o) { vec2 r; r.x = x + o.x; return r; }
    void operator+=(const vec2 &o) { x = x + o.x; }
};
int f(void) {
    vec2 a;
    vec2 b;
    a += b;
    return a.x;
}
""", "vec2__augadd(&a, &b)")

    def test_dereference_and_multiply_are_told_apart(self):
        """`operator*` is spelled the same either way; the difference on
        the page is whether it takes an operand."""
        self.assertLowers("""
class num {
public:
    int v;
    num() { v = 0; }
    num operator*(const num &o) { num r; r.v = v * o.v; return r; }
};
int f(void) {
    num a;
    num b;
    num c = a * b;
    return c.v;
}
""", "num__binmul(&a, &b)")


class TestCtorCallInMethodBody(Base):
    """A parenthesised constructor call inside a method body.

    It was not lowered: `vec2 r(5);` in a method reached the C unchanged,
    so `r` was never declared and the call was not a call. The cause was
    the declaration pattern's argument group, which could run past its own
    closing paren -- so for a method whose *return type* is a class, it
    matched from the function's own name to the first `;` inside the body,
    swallowing the real declaration. Only a class return type put the
    header in range, which is why `T name(args)` worked everywhere else and
    the gap looked like a general limitation.
    """

    def test_ctor_call_in_a_method_body_is_lowered(self):
        self.assertLowers("""
class vec2 {
public:
    int x;
    vec2() { x = 0; }
    vec2(int a) { x = a; }
    vec2 mk() { vec2 r(5); return r; }
};
int f(void) { vec2 a; return a.mk().x; }
""", "vec2 r; vec2_new_1(&r, 5);")

    def test_a_nested_call_in_the_arguments_still_works(self):
        """Braces are excluded from the argument group rather than parens,
        because an argument may legitimately contain a call -- and the
        balance check is what keeps that from over-matching again."""
        self.assertLowers("""
int dbl(int v);
class vec2 {
public:
    int x;
    vec2() { x = 0; }
    vec2(int a) { x = a; }
    vec2 mk() { vec2 r(dbl(2)); return r; }
};
int f(void) { vec2 a; return a.mk().x; }
""", "vec2_new_1(&r, dbl(2))")

    def test_the_no_argument_form_does_lower(self):
        self.assertLowers("""
class vec2 {
public:
    int x;
    vec2() { x = 0; }
    vec2 mk() { vec2 r; r.x = 5; return r; }
};
int f(void) { vec2 a; return a.mk().x; }
""", "vec2 r; vec2_new(&r);")


class TestChainedBinaryOperators(Base):
    """`a + b + c`, through a by-value front door.

    The left operand of the second `+` is the *result* of the first, and C
    cannot take the address of a function result -- the same wall a
    by-value method return hits in a chain. So each operator gets a variant
    taking its left operand by value, which a call to the ordinary form can
    be passed straight into. Safe precisely because a binary operator is
    only available to a class that owns nothing, so the by-value parameter
    is a struct copy with nothing to construct or destroy.
    """

    _VEC = """
class vec2 {
public:
    int x;
    vec2() { x = 0; }
    vec2(int a) { x = a; }
    vec2 operator+(const vec2 &o) { vec2 r(x + o.x); return r; }
    vec2 operator-(const vec2 &o) { vec2 r(x - o.x); return r; }
    vec2 operator*(const vec2 &o) { vec2 r(x * o.x); return r; }
};
"""

    def test_a_run_chains_left_to_right(self):
        self.assertLowers(self._VEC + """
int f(void) {
    vec2 a(1);
    vec2 b(2);
    vec2 c(3);
    vec2 s = a + b + c;
    return s.x;
}
""", "vec2__binadd_v(vec2__binadd(&a, &b), &c)")

    def test_subtraction_keeps_its_grouping(self):
        """`c - b - a` is `(c - b) - a`. Right-associating it would be a
        different number, so this is the shape worth pinning."""
        self.assertLowers(self._VEC + """
int f(void) {
    vec2 a(1);
    vec2 b(2);
    vec2 c(3);
    vec2 d = c - b - a;
    return d.x;
}
""", "vec2__binsub_v(vec2__binsub(&c, &b), &a)")

    def test_mixed_precedence_is_reported(self):
        """`a + b * c` would chain to `(a + b) * c`, which is the wrong
        grouping -- so it is refused rather than computed."""
        self.refuses(self._VEC + """
int f(void) {
    vec2 a(1);
    vec2 b(2);
    vec2 c(3);
    vec2 s = a + b * c;
    return s.x;
}
""", "different precedence", "temporary")

    def test_equal_precedence_may_mix(self):
        """`+` and `-` bind equally, so left to right is correct."""
        self.assertLowers(self._VEC + """
int f(void) {
    vec2 a(1);
    vec2 b(2);
    vec2 c(3);
    vec2 e = a + b - c;
    return e.x;
}
""", "vec2__binsub_v(vec2__binadd(&a, &b), &c)")

    def test_a_parenthesised_operand_is_reported(self):
        """And the diagnostic does not suggest parentheses, which do not
        help: an operand has to be a plain name either way."""
        msg = self.refuses(self._VEC + """
int f(void) {
    vec2 a(1);
    vec2 b(2);
    vec2 c(3);
    vec2 s = a + (b * c);
    return s.x;
}
""", "has to be a plain name")
        self.assertIn("temporary", msg)

    def test_the_by_value_wrapper_is_only_for_class_returns(self):
        """An operator returning a scalar (`int operator+`) has nothing to
        chain, so no wrapper is emitted for it."""
        out = self.assertLowers("""
class S {
public:
    int v;
    S() { v = 0; }
    int operator+(const S &o) { return v + o.v; }
};
int f(void) { S a; S b; return a + b; }
""", "S__binadd(&a, &b)")
        self.assertNotIn("S__binadd_v", out)


class TestByValueReceiverChain(Base):
    """`o.make().get()` -- a method called on a by-value return.

    The other half of the wall `a + b + c` hit: C cannot take the address
    of a function result. Same way out, too -- a variant of the method
    taking its receiver by value, emitted only for the names a source
    actually chains onto, and only for a class that owns nothing.
    """

    _SRC = """
class inner {
public:
    int v;
    inner() { v = 0; }
    inner(int a) { v = a; }
    int get() { return v; }
    int plus(int k) { return v + k; }
};
class outer {
public:
    int n;
    outer() { n = 5; }
    inner make() { inner r(n * 2); return r; }
};
"""

    def test_a_value_return_can_receive_a_call(self):
        self.assertLowers(self._SRC + """
int f(void) { outer o; return o.make().get(); }
""", "inner__byval_get_0(outer_make(&o))")

    def test_arguments_are_forwarded(self):
        """The variant repeats the method's own parameter list, which meant
        recording it -- only the reference *positions* were kept before,
        enough to fix up a call but not to declare a forwarder."""
        self.assertLowers(self._SRC + """
int f(void) { outer o; return o.make().plus(3); }
""", "inner__byval_plus_1(outer_make(&o), 3)")

    def test_an_owning_return_is_still_refused(self):
        """A struct copy of an owning receiver would leave two objects
        holding one resource, so no variant exists for it."""
        self.refuses("""
class buf {
public:
    char *p;
    buf() { p = 0; }
    ~buf() { free(p); }
    int get() { return 1; }
};
class mk {
public:
    mk() { }
    buf make() { buf r; return r; }
};
int f(void) { mk m; return m.make().get(); }
""", "owns a resource")

    def test_a_virtual_call_says_why_it_cannot(self):
        """And does not claim the class owns a resource, which would send
        the author looking for one it has not got."""
        msg = self.refuses("""
class shp { public: shp() { } virtual int area() { return 1; } };
class fac { public: fac() { } shp mk() { shp r; return r; } };
int f(void) { fac k; return k.mk().area(); }
""", "through the vtable")
        self.assertNotIn("owns a resource", msg)

    def test_the_variant_is_only_emitted_for_chained_names(self):
        """One per method unconditionally would leave unused static
        functions all over the output."""
        out = self.assertLowers(self._SRC + """
int f(void) { outer o; return o.make().get(); }
""")
        self.assertNotIn("inner__byval_plus", out)


class TestCommaSeparatedFields(Base):
    """`int x, y;` -- one declaration, several declarators.

    Found by compiling a documentation example rather than by reading the
    code. It was parsed as a single field named `y` of type `int x,`, so
    `x` was not a field at all and a method body using it emitted a bare
    `x` naming nothing.
    """

    def test_two_names_become_two_fields(self):
        self.assertLowers("""
class vec2 {
public:
    int x, y;
    vec2() { x = 1; y = 2; }
    int sum() { return x + y; }
};
int f(void) { vec2 v; return v.sum(); }
""", "struct vec2 { int x; int y; }", "this->x + this->y")

    def test_a_star_belongs_to_its_declarator(self):
        """`int *p, q;` makes `p` a pointer and `q` an int, as C says.

        Carrying the star into the base type made `q` a pointer too, so a
        body adding it to an int silently did pointer arithmetic.
        """
        self.assertLowers("""
class T {
public:
    int *p, q;
    T() { p = 0; q = 4; }
};
int f(void) { T t; return t.q; }
""", "int * p; int q;")

    def test_a_template_comma_is_not_a_declarator_break(self):
        """`map<int, int> m;` is one field. The declarator splitter tracks
        angle brackets, which `_split_top` deliberately does not -- it is
        used for call arguments, where `<` is as often a comparison."""
        self.assertLowers("""
#include <map>
class T {
public:
    std::map<int, int> m;
    T() { }
};
int f(void) { T t; return t.m.size(); }
""", "map_int_int m;")

    def test_an_array_declarator_keeps_its_dimension(self):
        self.assertLowers("""
class T {
public:
    int arr[3], n;
    T() { n = 6; }
};
int f(void) { T t; return t.n; }
""", "int arr[3]; int n;")


class TestDeductionScope(Base):
    """Deduction reads only the scopes still open at the call.

    Searching backwards for the nearest declaration is right *within* one
    scope, but a file has many. A local in an unrelated function above the
    call, or in an `if` block earlier in the same function, was nearer than
    the global the call actually meant -- and answered, deducing
    `sort_string` for an `int *`. Wrong rather than declined, which is the
    worse of the two failures.

    The rule is one line of C++: a brace region that opened and closed
    before the call is out of scope; one still open encloses it.
    """

    def test_another_functions_local_does_not_answer(self):
        self.assertLowers("""
#include <algorithm>
#include <vector>
#include <string>
int *data;
void earlier(void) {
    std::vector<std::string> v;
    std::string *data = v.begin();
}
int f(void) {
    static int store[4];
    data = store;
    std::sort(data, data + 4);
    return 0;
}
""", "sort_int")

    def test_a_closed_inner_block_does_not_answer(self):
        """The nested case: same function, but the block has ended."""
        self.assertLowers("""
#include <algorithm>
#include <vector>
#include <string>
int *p;
int f(void) {
    static int store[2];
    if (store[0] > 0) {
        std::vector<std::string> inner;
        std::string *p = inner.begin();
    }
    p = store;
    std::sort(p, p + 2);
    return 0;
}
""", "sort_int")

    def test_a_local_still_shadows_a_global(self):
        """Narrowing what is visible must not lose the nearest-wins rule
        inside what remains."""
        self.assertLowers("""
#include <algorithm>
#include <vector>
#include <string>
std::string *data;
int f(void) {
    std::vector<int> w;
    int *data = w.begin();
    std::sort(data, data + 2);
    return 0;
}
""", "sort_int")

    def test_an_enclosing_block_is_still_visible(self):
        """A scope still open at the call encloses it, however deep."""
        self.assertLowers("""
#include <algorithm>
#include <vector>
int f(void) {
    std::vector<int> w;
    int *p = w.begin();
    if (w.size() > 0) {
        for (int k = 0; k < 1; k = k + 1) {
            std::sort(p, p + 2);
        }
    }
    return 0;
}
""", "sort_int")


class TestPostEmissionLineNumbers(Base):
    """Diagnostics raised *after* class emission name the author's line.

    The anchors used to be one, above the author's first line, and class
    emission broke the count: generated C does not have the same number of
    lines the class was written on, so everything below the first class had
    shifted. A wrapper locating these was written and removed once for
    exactly that reason -- it reported the copy below as line 8.

    Now an anchor is re-placed after every class, naming the line its brace
    is on, so the count is exact again from there down however much the
    emitter added.
    """

    _B = """
class B {
public:
    int v;
    B() { v = 1; }
    ~B() { }
};
"""

    def _line_of(self, src, *needles):
        msg = self.refuses(src, *needles)
        mm = __import__("re").search(r":(\d+):", msg)
        self.assertIsNotNone(mm, "no line number in: %s" % msg)
        return int(mm.group(1))

    def test_rule_of_three_names_the_copy_line(self):
        """The exact case that reported 8 for a copy on 10."""
        self.assertEqual(self._line_of("""#include <vector>
class B {
public:
    int v;
    B() { v = 1; }
    ~B() { }
};
int f(void) {
    B a;
    B c = a;
    return c.v;
}
""", "no copy constructor"), 10)

    def test_the_count_survives_several_classes(self):
        """Each class shifts the text by its own difference, so the error
        compounds without a re-anchor after every one."""
        self.assertEqual(self._line_of("""#include <vector>
class A { public: int a; A() { a = 1; } };
class B {
public:
    int v;
    B() { v = 1; }
    ~B() { }
};
class C { public: int c; C() { c = 3; } };
int f(void) {
    A x;
    C y;
    B a;
    B c = a;
    return c.v + x.a + y.c;
}
""", "no copy constructor"), 14)

    def test_the_count_survives_template_instantiation(self):
        """`vector`, `map`, `string` and `pair` all emit above the call."""
        self.assertEqual(self._line_of("""#include <vector>
#include <string>
#include <map>
class B {
public:
    int v;
    B() { v = 1; }
    ~B() { }
};
int f(void) {
    std::vector<int> a;
    std::map<std::string, int> m;
    std::string k("x");
    m[k] = 1;
    B p;
    B q = p;
    return q.v;
}
""", "no copy constructor"), 16)

    def test_a_call_rewriting_diagnostic_is_located_too(self):
        """The other pass of the thirty."""
        self.assertEqual(self._line_of("""#include <vector>
class inner {
public:
    int v;
    inner() { v = 0; }
    ~inner() { }
    int get() { return v; }
};
class outer {
public:
    outer() { }
    inner make() { inner r; return r; }
};
int f(void) {
    outer o;
    return o.make().get();
}
""", "owns a resource"), 16)

    def test_no_anchor_reaches_the_c(self):
        """A re-anchor per class, so stripping only the origin one left the
        rest behind as stray typedefs."""
        out = self.assertLowers("""#include <vector>
class A { public: int a; A() { a = 1; } };
class C { public: int c; C() { c = 3; } };
int f(void) { A x; C y; return x.a + y.c; }
""")
        self.assertNotIn("__crust_src_line_", out)
        self.assertNotIn("typedef int ;", out)


def _main():
    argv = [a for a in sys.argv[1:] if a != "--failing"]
    if "--failing" in sys.argv[1:]:
        print("(--failing: run the whole file; open gaps are the failures)")
    unittest.main(argv=[sys.argv[0]] + argv, verbosity=2)


if __name__ == "__main__":
    _main()
