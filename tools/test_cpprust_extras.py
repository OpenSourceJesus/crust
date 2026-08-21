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


# ------------------------------------------------- inherited field by name

class TestInheritedFieldNamed(Base):
    """Naming an inherited field of an owning type.

    From `litehtml/src/html_tag.cpp`:

        std::shared_ptr<element> html_tag::get_child(int idx) const
        { return m_children[idx]; }

    `m_children` is declared on the base, and field qualification rewrites
    the body to the path it actually lives at -- `this->_base.m_children`.
    The name walker had no step for that `_base` hop, because an inherited
    field is flattened into the derived class's own field table under its
    plain name and `_base` is not a declared field of anything. So an
    inherited field of an owning type could not be named at all, and every
    copy out of one was refused with "not an object of that type this pass
    can name" -- a diagnostic about the copy, for what was really a gap in
    the walk.
    """

    SRC = """
#include <vector>
#include <memory>

class elem { public: int v; };
class base_c { public: std::vector<std::shared_ptr<elem> > m_children; };
class derived_c : public base_c {
public:
    std::shared_ptr<elem> get_child(int idx);
};
std::shared_ptr<elem> derived_c::get_child(int idx) {
    std::shared_ptr<elem> ret = m_children[idx];
    return ret;
}
"""

    def test_copy_from_inherited_container_subscript(self):
        out = self.assertLowers(self.SRC, "shared_ptr_elem_copy")
        # The copy reads through the base hop and the container's own
        # `operator[]`, rather than subscripting the struct.
        self.assertIn("this->_base.m_children", out)
        self.assertIn("__index", out)

    def test_inherited_field_still_dropped(self):
        """The copy is an owner, so the local is still dropped."""
        out = self.lower(self.SRC)
        self.assertIn("shared_ptr_elem_drop", out)


class TestSmartPointerFieldNamed(Base):
    """Naming a field reached through a smart pointer.

    From `litehtml/src/document.cpp`:

        std::shared_ptr<element> child = el_ptr->m_children[i];

    `el_ptr->m_children` is a field of the *pointee*, not of the handle, so
    a walk that looked only in the handle's own fields stopped at the hop.
    `shared_ptr<T>` is how litehtml passes every element around, so this
    meant no field reached through one could be named, and copying out of
    one was refused. `operator->` already has a lowered form registered;
    the walker goes through it, which is the same step the call pass takes.
    """

    SRC = """
#include <vector>
#include <memory>

class leaf { public: int v; };
class holder { public: std::vector<leaf> items; };
void f(std::shared_ptr<holder>& p, int i) {
    leaf x = p->items[i];
    (void)x;
}
"""

    def test_copy_through_arrow(self):
        out = self.assertLowers(self.SRC, "__arrow")
        # The handle is already a pointer here, so it is passed straight to
        # `operator->` rather than having its address taken again.
        self.assertIn("shared_ptr_holder__arrow(p)", out)
        self.assertNotIn("shared_ptr_holder__arrow(&(p))", out)


class TestPointerLocalNamed(Base):
    """Naming a field through a pointer *local*.

    From `litehtml/src/html_tag.cpp`, after the attribute loop was indexed:

        const css_attribute_selector *attr = &selector.m_attrs[ai];
        selector_name = attr->val;

    A pointer *parameter* of class type was already registered, because
    reference lowering turns `const T &p` into `const T *p` and the body
    still has to name it. A pointer *local* was not, so binding one to an
    element and copying out of it was refused.

    Kept in `ptrvals` rather than `vals` deliberately: the copy and
    assignment handlers read `vals`, and `p = q` on two pointers is a
    pointer assignment, not a class one. Registering these in `vals` made
    the supplied containers' own `T *nd = (T *)realloc(..)` look like a
    class assignment and broke every container in the tree.
    """

    SRC = """
#include <vector>
#include <string>

class attr_t { public: std::string val; };
class holder { public: std::vector<attr_t> attrs; };

void f(holder& h, int i) {
    const attr_t *attr = &h.attrs[i];
    std::string selector_name;
    selector_name = attr->val;
    (void)selector_name;
}
"""

    def test_copy_through_pointer_local(self):
        out = self.assertLowers(self.SRC, "string__assign")
        self.assertIn("attr->val", out)

    def test_pointer_local_is_not_treated_as_a_class_object(self):
        """A pointer local is reassignable as a pointer, not copied."""
        out = self.assertLowers("""
class thing { public: int v; ~thing() { v = 0; } };
void f(thing *a, thing *b) {
    thing *p = a;
    p = b;
    (void)p;
}
""", "thing")
        # No copy call generated for `p = b`, and no drop for the pointer.
        self.assertNotIn("thing_copy(&p", out)
        self.assertNotIn("thing_drop(&p)", out)


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


class TestFunctionTemplates(Base):
    """`template<..>` on a function.

    From `litehtml/include/litehtml/context.h`, which every element file
    includes, and which was the single biggest blocker in the tree -- 22
    of 43 files:

        template<class T>
        void js_register_class(const char* className) {
            ...
            if (auto* ref { static_cast<typename T::js_object_ref*>(..) })
                delete ref;
        }

    The subset monomorphises *class* templates. This was not recognised
    as a template at all, so its body was lowered as ordinary code -- and
    a template's body is not ordinary code: `typename T::js_object_ref`
    names a type that exists only once `T` is known. The result was a
    diagnostic about `delete` in files that never call the function.

    An uninstantiated template emits nothing in C++, so it emits nothing
    here. Only `context.cpp` instantiates this one; for the other 22 the
    right answer was always to emit nothing at all.
    """

    def test_uninstantiated_function_template_emits_nothing(self):
        out = self.assertLowers("""
class Ctx {
public:
    int n;
    template<class T>
    void reg(const char *name) {
        if (auto* ref { static_cast<typename T::inner*>(get(name)) }) {
            delete ref;
        }
    }
    int get_n() { return n; }
};
void f(void) { Ctx c; use(c.get_n()); }
""", "Ctx_get_n")
        self.assertNotIn("reg", out)
        self.assertNotIn("template", out)

    def test_class_template_is_untouched(self):
        """`template<..> class X` is this pass's own business."""
        self.assertLowers("""
template<typename T>
class Box { public: T v; Box() { } T get() { return v; } };
void f(void) { Box<int> b; use(b.get()); }
""", "Box_int_get")

    def test_member_instantiation_is_monomorphised(self):
        """The litehtml shape: a member template, instantiated in a method.

        Substituting in place is what makes this cost nothing extra --
        what comes out is an ordinary member, and the class emitter gives
        it its `this` and mangles its name without knowing a template was
        ever involved.
        """
        out = self.assertLowers("""
struct Doc { int id; };
class Ctx {
public:
    int n;
    template<class T>
    void reg(const char *name) { n = sizeof(T); use(name); }
    void go() { reg<Doc>("Document"); }
};
""", "Ctx_reg_Doc(Ctx *this, const char *name)",
     "Ctx_reg_Doc(this, \"Document\")")
        self.assertNotIn("template", out)

    def test_free_instantiation_is_monomorphised(self):
        out = self.assertLowers("""
template<class T> int idof(T *p) { return p->v; }
struct A { int v; };
void f(A *a) { use(idof<A>(a)); }
""", "int idof_A(A *p)", "idof_A(a)")
        self.assertNotIn("template", out)

    def test_two_instantiations_give_two_functions(self):
        out = self.assertLowers("""
struct Doc { int id; };
struct El { int id; };
class Ctx {
public:
    int n;
    template<class T> void reg(const char *nm) { n = sizeof(T); use(nm); }
    void go() { reg<Doc>("D"); reg<El>("E"); }
};
""", "Ctx_reg_Doc", "Ctx_reg_El")
        self.assertIn("sizeof(Doc)", out)
        self.assertIn("sizeof(El)", out)

    def test_wrong_argument_count_is_reported(self):
        """Substituted by position, with no defaults to fall back on."""
        self.refuses("""
template<class T, class U> int both(T *a, U *b) { return 1; }
struct A { int v; };
void f(A *a) { use(both<A>(a, a)); }
""", "template argument")

    def test_qualified_argument_mangles_like_the_flattened_name(self):
        """`lh::Doc` gives `_lh_Doc`, which is what flattening calls it."""
        out = self.assertLowers("""
namespace lh { struct Doc { int id; }; }
template<class T> int idof(T *p) { return p->id; }
void f(lh::Doc *d) { use(idof<lh::Doc>(d)); }
""", "idof_lh_Doc")

    def test_declaration_only_function_template(self):
        """No body to hold back, and nothing to emit either."""
        out = self.assertLowers("""
template<class T> void reg(const char *n);
int f(void) { return 1; }
""", "int f(void)")
        self.assertNotIn("template", out)


class TestDirectivesAreNotCode(Base):
    """A `#define`'s replacement text is not an expression.

    From `litehtml/include/litehtml/os_types.h`:

        #define t_to_string(val)   std::to_string(val)

    read as a call handing a `string` over by value -- the macro's own
    parameter `val` resolving against an unrelated local of that name
    elsewhere in the file. That refusal fired on 22 of 43 litehtml
    sources, every one for a line no compiler evaluates here.

    Blanked only in the *scan*; the directives still reach the output,
    where ShivyCX expands them.
    """

    def test_macro_body_is_not_read_as_a_call(self):
        out = self.assertLowers("""
#include <string>
#define t_to_string(val) to_string(val)
void f(void) { string val("x"); use(&val); }
""", "#define t_to_string")

    def test_multiline_macro_is_not_read_as_code(self):
        self.assertLowers("""
#include <string>
#define two_step(val) do { \\
        to_string(val); \\
    } while (0)
void f(void) { string val("x"); use(&val); }
""", "#define two_step")

    def test_a_real_by_value_owning_argument_is_constructed(self):
        """The check has to keep working on actual code.

        No longer a refusal: `string` has a copy constructor, so the
        argument is copy-constructed into the parameter the callee will
        destroy -- which is what C++ does. What is still refused is the
        same call for a type with no copy constructor.
        """
        self.assertLowers("""
#include <string>
void consume(string s);
void f(void) { string v("x"); consume(v); }
""", "string_copy(&")


class TestClangFallback(Base):
    """`auto` that no written spelling can answer.

    The textual pass reads types from how they are written, which is
    exact where a spelling exists and reports where none does. Four
    litehtml files fail there -- a ternary in `context.cpp`, iterator
    arithmetic in `box.cpp`, `str.find(..)` in `style.cpp`,
    `text.substr(..)` in `stylesheet.cpp`.

    Where this pass reports, a C++ compiler already knows the answer.
    So if `clang++` is installed its answer is asked for, from the
    original file, before the report is raised.

    Nothing is approximated, which is what keeps this inside the guiding
    rule: clang either says what the type is or it does not, and if it
    does not the original diagnostic stands unchanged. The tests below
    are skipped where clang is absent -- which is itself the point worth
    testing, since the fallback must not change what a machine without
    clang does.
    """

    def setUp(self):
        if not cpp_auto.clang_available():
            self.skipTest("clang++ not installed")

    def _lower_file(self, src):
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "t.cpp")
        with open(p, "w") as f:
            f.write(src)
        return cpprust.translate(src, path=p)

    def test_ternary_is_deduced(self):
        out = self._lower_file("""
struct Node { int v; int get(); };
Node *lookup(int k);
void use(int x);
void f(void) {
    auto a = lookup(1) ? lookup(2) : lookup(3);
    use(a->v);
}
""")
        self.assertIn("Node * a", out)

    def test_scalar_expression_is_deduced(self):
        out = self._lower_file("""
int base(void);
void use(int x);
void f(void) { auto n = base() + 1; use(n); }
""")
        self.assertIn("int n", out)

    def test_without_clang_the_diagnostic_is_unchanged(self):
        """The fallback must not be load-bearing.

        A machine with no clang has to behave exactly as before, so this
        forces the unavailable path and asserts the original message.
        """
        saved = cpp_auto._CLANG_OK
        cpp_auto._CLANG_OK = False
        try:
            self.refuses("""
struct Node { int v; };
Node *lookup(int k);
void f(void) { auto a = lookup(1) ? lookup(2) : lookup(3); use(a->v); }
""", "`auto` cannot deduce")
        finally:
            cpp_auto._CLANG_OK = saved

    def test_an_unspellable_answer_is_refused(self):
        """clang answers in C++'s terms, and some answers name nothing here.

        `iterator` is the case that matters: a nested typedef, arriving
        spelled bare. Emitting `iterator i = ..` into C declares a
        variable of a type nothing defines -- worse than the diagnostic it
        replaced, since the error moves to the C front end and stops
        naming `auto`. So an answer is taken only if this translation
        already knows the name.
        """
        self.assertFalse(cpp_auto._spellable("iterator", set(), {}))
        self.assertTrue(cpp_auto._spellable("int", set(), {}))
        self.assertTrue(cpp_auto._spellable("Node *", set(["Node"]), {}))
        self.assertTrue(cpp_auto._spellable("unsigned long", set(), {}))

    def test_a_name_declared_twice_is_not_guessed_between(self):
        """`box.cpp` declares `i` four times, with different iterator
        types. Keyed by name, that is ambiguous, and ambiguous is
        reported rather than resolved to whichever came first."""
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "amb.cpp")
        with open(p, "w") as f:
            f.write("int mk(void); double dk(void);\n"
                    "void f(void) { auto i = mk(); use(i); }\n"
                    "void g(void) { auto i = dk(); use(i); }\n")
        got = cpp_auto.clang_auto_types(p)
        self.assertNotIn("i", got)

    def test_a_type_the_subset_cannot_spell_is_not_taken(self):
        """A nested `iterator` is not a spelling this subset has.

        Taking clang's word for it would only move the error somewhere
        less informative, so the `auto` diagnostic stands.
        """
        self.assertIsNone(cpp_auto._from_cxx_spelling(
            "basic_string<char>::iterator"))
        self.assertIsNone(cpp_auto._from_cxx_spelling(
            "(lambda at t.cpp:3:5)"))
        self.assertEqual(cpp_auto._from_cxx_spelling("std::string"), "string")


class TestOwningArgScope(Base):
    """The owning-argument check has to know which `val` it is looking at.

    `locals_` was a flat, file-wide map from name to owning class, so one
    `string val` anywhere made *every* `val` in the translation a
    `string`. quickjs.h has

        static js_force_inline JSValue JS_NewBool(JSContext *ctx,
                                                  JS_BOOL val)
        { return JS_MKVAL(JS_TAG_BOOL, (val != 0)); }

    and litehtml has a `string val` of its own elsewhere, so the pass
    refused a parameter that is an `int`. The name is the same; the
    variable is not.

    A declaration now counts only where it sits in the same top-level
    declaration as the use.
    """

    def test_same_name_in_another_function_is_not_confused(self):
        self.assertLowers("""
#include <string>
int mkval(int val) { return JS_MKVAL(TAG, val); }
void f(void) { string val("x"); use(&val); }
""", "mkval")

    def test_the_real_case_is_still_refused(self):
        """Handing an owning local over by value is still the bug it was."""
        self.refuses("""
#include <string>
void f(void) { string v("x"); consume(v); }
""", "hands over")

    def test_two_owning_locals_of_the_same_name(self):
        """Each function's own declaration is the one that applies."""
        self.refuses("""
#include <string>
void g(void) { string val("y"); consume(val); }
void f(void) { string val("x"); use(&val); }
""", "hands over")


class TestQualifiedNameFlattening(Base):
    """`N::x` becomes `N_x` only for names flattening actually renamed.

    The qualification says which namespace to look in, not what the name
    became -- and this pass deliberately does not rename everything a
    namespace holds. A typedef keeps its name so the generated C stays
    readable.

    litehtml writes `litehtml::tstring` in fourteen places while the
    typedef in `os_types.h` stays `tstring`, so every qualified use
    became `litehtml_tstring` and the declaration did not follow. Every
    file reaching those headers translated clean and then failed to
    compile on a type that appears nowhere -- around 35 of 43 sources,
    and the reason the gcc stage exists.
    """

    def test_qualified_typedef_keeps_its_name(self):
        out = self.assertLowers("""
#include <string>
namespace lh {
    typedef std::string tstring;
    lh::tstring pick(void);
}
""", "string lh_pick")
        self.assertNotIn("lh_tstring", out)

    def test_qualified_class_is_still_flattened(self):
        """A name flattening *did* rename still gets the prefix."""
        out = self.assertLowers("""
namespace lh {
    class Thing { public: int v; int get() { return v; } };
}
void f(void) { lh::Thing t; use(t.get()); }
""", "lh_Thing")

    def test_qualified_function_is_still_flattened(self):
        self.assertLowers("""
namespace lh { int helper(int a) { return a; } }
void f(void) { use(lh::helper(1)); }
""", "lh_helper")


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
