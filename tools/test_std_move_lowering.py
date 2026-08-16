#!/usr/bin/env python3
"""test_std_move_lowering -- statement-position `std::move`.

The inner loop for move construction and move assignment. Kept apart from
`test_cpprust_extras.py` while the feature is in flight; it folds in there
once it lands.

The shape comes from `tools/test_std_move.py`, which benchmarks a deep copy
against a move of a ~200MB buffer under g++ and clang++:

    HeavyBuffer(HeavyBuffer&& other) noexcept : size(other.size),
                                                data(other.data) {
        other.data = nullptr;
        other.size = 0;
    }
    HeavyBuffer target = std::move(source);

That is the whole feature in statement position: a declaration whose
initializer is a `std::move` of a named local.

The rule these tests exist to pin down is the one most likely to be got
wrong quietly, because the pass already contains machinery that does the
*other* thing:

    A C++ moved-from object is NOT dead. It is valid-but-unspecified, and
    it is STILL DESTROYED. The move constructor nulls the source so that
    destruction is harmless.

`_rewrite_scopes` already has `unwind(upto, moved=)`, which drops a local
out of a path's destructors -- that is Crust's move-out, used for
`return v;`, and it is the correct answer *there* because the object is
handed to the caller bitwise with no constructor involved. Reaching for it
here would give `std::move` Rust semantics: source dead, no drop. On the
benchmark's own class both answers happen to produce working code, because
`delete[] nullptr` is a no-op -- which is exactly why a test has to say
which one is meant.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.cpprust as cpprust                       # noqa: E402


BUF = """
class Buf {
public:
    int *d;
    Buf() { d = (int *)malloc(16); }
    Buf(const Buf &o) { d = (int *)malloc(16); d[0] = o.d[0]; }
    Buf(Buf &&o) { d = o.d; o.d = 0; }
    ~Buf() { free(d); }
};
"""

# Only a copy constructor. C++ overload resolution picks it for an rvalue
# when no move constructor exists, so `std::move` on one of these is a copy.
COPYONLY = """
class Cbuf {
public:
    int *d;
    Cbuf() { d = (int *)malloc(16); }
    Cbuf(const Cbuf &o) { d = (int *)malloc(16); d[0] = o.d[0]; }
    ~Cbuf() { free(d); }
};
"""


class Base(unittest.TestCase):

    def lower(self, src, **kw):
        return cpprust.translate(src, **kw)

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
        import re as _re
        out = self.lower(src, **kw)
        for n in needles:
            pat = r"\s*".join(_re.escape(p) for p in n.split())
            if not _re.search(pat, out):
                self.fail("no match for %r in:\n%s" % (n, out[-800:]))
        return out


# ------------------------------------------------------- the constructor

class TestMoveConstructorIsEmitted(Base):
    """`T(T &&o)` is a member like any other, and gets its own symbol.

    `T_move`, beside `T_copy`, for the same reason `T_copy` exists: every
    other constructor is `T_new`, so overloading is not available.
    """

    def test_move_ctor_emitted(self):
        self.assertLowers(BUF + "int go(void) { Buf a; return 0; }",
                          "void Buf_move(Buf *this, Buf *o)")

    def test_rvalue_ref_param_lowers_to_pointer(self):
        """`Buf &&o` is a reference, so it lowers like `Buf &o` does."""
        out = self.assertLowers(BUF + "int go(void) { Buf a; return 0; }",
                                "void Buf_move(Buf *this, Buf *o)")
        self.assertNotIn("&&", out)

    def test_copy_and_move_can_coexist(self):
        """Both, in one class. This used to be `more than one copy ctor`."""
        self.assertLowers(BUF + "int go(void) { Buf a; return 0; }",
                          "void Buf_copy(Buf *this, const Buf *o)",
                          "void Buf_move(Buf *this, Buf *o)")


# ------------------------------------------------------ move construction

class TestMoveConstruction(Base):
    """`T b = std::move(a);` and `T b(std::move(a));`."""

    def test_copy_init_calls_move(self):
        self.assertLowers(
            BUF + "int go(void) { Buf a; Buf b = std::move(a); return 0; }",
            "Buf_move(&b, &a);")

    def test_direct_init_calls_move(self):
        self.assertLowers(
            BUF + "int go(void) { Buf a; Buf b(std::move(a)); return 0; }",
            "Buf_move(&b, &a);")

    def test_no_copy_call_is_emitted(self):
        out = self.lower(
            BUF + "int go(void) { Buf a; Buf b = std::move(a); return 0; }")
        self.assertNotIn("Buf_copy(&b", out)


# ------------------------------------------- the rule worth getting right

class TestMovedFromIsStillDestroyed(Base):
    """A moved-from object is destroyed. This is C++, not Crust.

    The source stays live to the end of its scope and its destructor runs
    there, exactly as it would have without the move. The move constructor
    is what makes that harmless, by nulling the source -- which is the
    contract `HeavyBuffer` and `Buf` below both honour.

    If this ever regresses to Crust's move-out -- dropping `a` from the
    scope's destructors -- these are the tests that say so.
    """

    def test_source_is_still_dropped(self):
        out = self.assertLowers(
            BUF + "int go(void) { Buf a; Buf b = std::move(a); return 0; }",
            "Buf_move(&b, &a);")
        self.assertIn("Buf_drop(&a)", out)

    def test_both_objects_are_dropped(self):
        out = self.lower(
            BUF + "int go(void) { Buf a; Buf b = std::move(a); return 0; }")
        self.assertIn("Buf_drop(&a)", out)
        self.assertIn("Buf_drop(&b)", out)

    def test_drops_are_in_reverse_declaration_order(self):
        """C++ destroys in reverse order, and a move does not change it."""
        out = self.lower(
            BUF + "int go(void) { Buf a; Buf b = std::move(a); return 0; }")
        self.assertLess(out.index("Buf_drop(&b)"), out.index("Buf_drop(&a)"))


# --------------------------------------------------------- the fall-back

class TestNoMoveConstructorFallsBackToCopy(Base):
    """No move constructor means overload resolution picks the copy.

    `std::move` is a cast, not a call to anything: it produces an rvalue,
    and if the class offers only `T(const T &)` then that is what binds. So
    a class without a move constructor is copied, silently and correctly --
    which is also what makes adding `std::move` to existing sources safe.
    """

    def test_falls_back_to_copy_ctor(self):
        self.assertLowers(
            COPYONLY
            + "int go(void) { Cbuf a; Cbuf b = std::move(a); return 0; }",
            "Cbuf_copy(&b, &a);")

    def test_rule_of_three_still_refuses(self):
        """Neither constructor: still two owners and one resource."""
        src = """
class Own {
public:
    int *d;
    Own() { d = (int *)malloc(16); }
    ~Own() { free(d); }
};
int go(void) { Own a; Own b = std::move(a); return 0; }
"""
        self.refuses(src, "Own")


# ----------------------------------------------------- move assignment

class TestMoveAssignment(Base):
    """`operator=(T &&)` lowers beside `operator=`, and `b = std::move(a)`."""

    ASSIGN = """
class Abuf {
public:
    int *d;
    Abuf() { d = (int *)malloc(16); }
    Abuf(const Abuf &o) { d = (int *)malloc(16); }
    Abuf &operator=(const Abuf &o) { d[0] = o.d[0]; }
    Abuf &operator=(Abuf &&o) { free(d); d = o.d; o.d = 0; }
    ~Abuf() { free(d); }
};
"""

    def test_move_assign_emitted(self):
        self.assertLowers(
            self.ASSIGN + "int go(void) { Abuf a; return 0; }",
            "void Abuf__moveassign(Abuf *this, Abuf *o)")

    def test_move_assign_called(self):
        self.assertLowers(
            self.ASSIGN
            + "int go(void) { Abuf a; Abuf b; b = std::move(a); return 0; }",
            "Abuf__moveassign(&b, &a);")

    def test_plain_assign_still_copies(self):
        self.assertLowers(
            self.ASSIGN
            + "int go(void) { Abuf a; Abuf b; b = a; return 0; }",
            "Abuf__assign(&b, &a);")

    def test_falls_back_to_copy_assign(self):
        """No `operator=(T &&)`: the const-ref overload binds the rvalue."""
        src = """
class Sbuf {
public:
    int *d;
    Sbuf() { d = (int *)malloc(16); }
    Sbuf(const Sbuf &o) { d = (int *)malloc(16); }
    Sbuf &operator=(const Sbuf &o) { d[0] = o.d[0]; }
    ~Sbuf() { free(d); }
};
int go(void) { Sbuf a; Sbuf b; b = std::move(a); return 0; }
"""
        self.assertLowers(src, "Sbuf__assign(&b, &a);")


# ------------------------------------------------------------ guardrails

class TestExpressionPosition(Base):
    """`std::move` where there is no declaration to construct into.

    A GNU statement expression is what makes this possible:
    `({ T t; T_move(&t, &a); t; })` declares the temporary, moves into it,
    and yields it -- which is what a C++ compiler does with a materialised
    temporary, written out. It is an extension rather than ISO C, but gcc,
    clang and ShivyCX all implement it, so the output stays one file.
    """

    def test_return_move_materialises(self):
        out = self.assertLowers(
            BUF + "Buf mk(void) { Buf a; return std::move(a); }",
            "Buf _cpp_mv0;", "Buf_move(&_cpp_mv0, &a);")
        self.assertIn("({", out)

    def test_return_move_runs_before_the_drop(self):
        """The spill is the statement the move needed.

        `return` already evaluates its operand into a temporary before the
        destructors run, because C++ evaluates the operand first. That
        ordering is exactly what a move requires: the source is still alive
        when it is moved from, and its own drop then finds the husk.
        """
        out = self.lower(BUF + "Buf mk(void) { Buf a; return std::move(a); }")
        self.assertLess(out.index("Buf_move(&_cpp_mv0, &a)"),
                        out.index("Buf_drop(&a)"))

    def test_source_is_still_dropped(self):
        out = self.lower(BUF + "Buf mk(void) { Buf a; return std::move(a); }")
        self.assertIn("Buf_drop(&a)", out)

    def test_temporary_is_not_dropped(self):
        """The temporary is yielded by value: the caller owns what it holds.

        Destroying it would destroy the resource the caller just received.
        Only the source's drop belongs in this function.
        """
        out = self.lower(BUF + "Buf mk(void) { Buf a; return std::move(a); }")
        self.assertNotIn("Buf_drop(&_cpp_mv0)", out)

    def test_falls_back_to_copy(self):
        """No move constructor: the copy binds the rvalue here too."""
        self.assertLowers(
            COPYONLY + "Cbuf mk(void) { Cbuf a; return std::move(a); }",
            "Cbuf_copy(&_cpp_mv0, &a);")

    def test_unnameable_operand_is_refused(self):
        """The operand still has to be something this pass can name."""
        self.refuses(
            BUF + "Buf mk(void) { Buf a; return std::move(f()); }",
            "std::move")


class TestBareMoveIsNotStdMove(Base):
    """`move` unqualified is somebody's method, not `std::move`.

    `std::` is stripped rather than resolved, so the qualifier has to be
    read *before* that happens. A project with its own `move` -- a layout
    engine moving a box, say -- must not have its calls rewritten.
    """

    def test_user_method_named_move_is_untouched(self):
        src = """
class Box {
public:
    int x;
    Box() { x = 0; }
    void move(int dx) { x = x + dx; }
};
int go(void) { Box b; b.move(3); return 0; }
"""
        self.assertLowers(src, "Box_move(&b, 3);")

    def test_free_function_named_move_is_untouched(self):
        src = """
int move(int v) { return v + 1; }
int go(void) { return move(2); }
"""
        self.assertLowers(src, "move(2)")


class TestUniquePtrMoves(Base):
    """`unique_ptr` is move-only, and now actually moves.

    It is supplied source in this subset rather than special-cased, so it
    gained a move constructor the same way any user class would. The point
    worth pinning is that this did *not* make it copyable: move-only means
    a move constructor and no copy constructor, and the Rule of Three
    refusal is still what enforces the second half.
    """

    SRC = """
#include <memory>
class Thing { public: int v; Thing() { v = 1; } };
"""

    def test_move_construction(self):
        self.assertLowers(
            self.SRC + "int go(void) {"
            " std::unique_ptr<Thing> a(new Thing());"
            " std::unique_ptr<Thing> b(std::move(a)); return 0; }",
            "unique_ptr_Thing_move(&b, &a);")

    def test_move_assignment(self):
        self.assertLowers(
            self.SRC + "int go(void) {"
            " std::unique_ptr<Thing> a(new Thing());"
            " std::unique_ptr<Thing> b(new Thing());"
            " b = std::move(a); return 0; }",
            "unique_ptr_Thing__moveassign(&b, &a);")

    def test_injected_class_name_is_monomorphised(self):
        """`unique_ptr<T> &&o`, not a bare `unique_ptr &&o`.

        The supplied templates spell the injected class name with its
        arguments, because the bare name is not what substitution rewrites.
        Written bare, the parameter came out `unique_ptr *o` -- a type
        nothing defines -- and the body read `o.up` through it.
        """
        out = self.lower(
            self.SRC + "int go(void) {"
            " std::unique_ptr<Thing> a(new Thing());"
            " std::unique_ptr<Thing> b(std::move(a)); return 0; }")
        self.assertIn(
            "unique_ptr_Thing_move(unique_ptr_Thing *this, "
            "unique_ptr_Thing *o)", out)

    def test_copying_is_still_refused(self):
        """Move-only: the move landed, the copy refusal did not move."""
        self.refuses(
            self.SRC + "int go(void) {"
            " std::unique_ptr<Thing> a(new Thing());"
            " std::unique_ptr<Thing> b(a); return 0; }",
            "no copy constructor")

    def test_both_are_still_dropped(self):
        """The moved-from `unique_ptr` is nulled, and destroyed anyway."""
        out = self.lower(
            self.SRC + "int go(void) {"
            " std::unique_ptr<Thing> a(new Thing());"
            " std::unique_ptr<Thing> b(std::move(a)); return 0; }")
        self.assertIn("unique_ptr_Thing_drop(&a)", out)
        self.assertIn("unique_ptr_Thing_drop(&b)", out)


class TestContainerMoves(Base):
    """A move-only element in a supplied container.

    Three things had to meet for this: a `push_back(__cpp_rref(T))` overload
    told apart from the copying one by the `std::move` at the call site
    rather than by arity; `__cpp_movein`, which is to `__cpp_copy` what a
    move constructor is to a copy one; and the copying members being
    *deleted* for an element that cannot be copied, as C++ deletes them,
    rather than the whole instantiation being refused.
    """

    RES = """
#include <vector>
class Res {
public:
    int *d;
    Res() { d = (int *)malloc(16); }
    Res(Res &&o) { d = o.d; o.d = 0; }
    ~Res() { free(d); }
};
"""

    def test_push_back_selects_the_move_overload(self):
        self.assertLowers(
            self.RES + "int go(void) { std::vector<Res> v; Res r;"
            " v.push_back(std::move(r)); return 0; }",
            "vector_Res_push_back__move(&v, &r);")

    def test_no_temporary_is_materialised(self):
        """A move overload takes the source by reference.

        `push_back(T &&)` lowers to `push_back(T *)`, so the call wants the
        address of the source. Materialising a temporary here would hand it
        a statement expression's result, whose address cannot be taken.
        """
        out = self.lower(
            self.RES + "int go(void) { std::vector<Res> v; Res r;"
            " v.push_back(std::move(r)); return 0; }")
        self.assertNotIn("_cpp_mv", out)

    def test_source_is_still_dropped(self):
        out = self.lower(
            self.RES + "int go(void) { std::vector<Res> v; Res r;"
            " v.push_back(std::move(r)); return 0; }")
        self.assertIn("Res_drop(&r)", out)

    def test_scalar_element_gets_no_move_overload(self):
        """`__cpp_rref(int)` is plain `int`, so both overloads would be one.

        There is nothing to move about a scalar, so the move overload is not
        emitted at all rather than colliding with the copying one.
        """
        out = self.lower(
            "#include <vector>\n"
            "int go(void) { std::vector<int> v; v.push_back(3); return 0; }")
        self.assertNotIn("push_back__move", out)

    def test_vector_of_unique_ptr(self):
        """The shape `CPPRUST.md` used to list as inexpressible."""
        self.assertLowers(
            "#include <vector>\n#include <memory>\n"
            "class Thing { public: int v; Thing() { v = 1; } };\n"
            "int go(void) { std::vector<std::unique_ptr<Thing> > w;"
            " std::unique_ptr<Thing> p(new Thing());"
            " w.push_back(std::move(p)); return 0; }",
            "vector_unique_ptr_Thing_push_back__move(&w, &p);")

    def test_nested_instantiation_is_emitted_first(self):
        """`unique_ptr<Thing>` before `vector<unique_ptr_Thing>`.

        The deferral is transitive: `unique_ptr<Thing>` waits for `Thing`, a
        user class declared below both templates, so the vector over it has
        to wait too. Reading only the template names missed the middle step,
        and the vector was emitted while its element was still an unknown
        name -- which cost it the knowledge that the element cannot be
        copied, and the copying `push_back` was emitted and then refused.
        """
        out = self.lower(
            "#include <vector>\n#include <memory>\n"
            "class Thing { public: int v; Thing() { v = 1; } };\n"
            "int go(void) { std::vector<std::unique_ptr<Thing> > w;"
            " std::unique_ptr<Thing> p(new Thing());"
            " w.push_back(std::move(p)); return 0; }")
        self.assertLess(
            out.index("void unique_ptr_Thing_move("),
            out.index("void vector_unique_ptr_Thing_push_back__move("))

    def test_copying_a_move_only_element_is_reported(self):
        """A deleted member is an error at the *call*, as in C++.

        Dropping it silently would turn a diagnostic into an undefined
        symbol from the C front end, which is the failure mode this whole
        pass exists to avoid.
        """
        self.refuses(
            self.RES + "int go(void) { std::vector<Res> v; Res r;"
            " v.push_back(r); return 0; }",
            "deleted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
