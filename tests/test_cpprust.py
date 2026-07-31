"""Pure-translation tests for the C++ subset front end (tools.cpprust)."""

import unittest

import tools.cpprust as cpprust


class TestCppClass(unittest.TestCase):
    def test_struct_and_methods(self):
        out = cpprust.translate("""
class VecGuard {
    int * held;
public:
    VecGuard(int * v) { held = v; }
    ~VecGuard() { if (held) { held = 0; } }
    int * get() { return held; }
};
""")
        self.assertIn("struct VecGuard { int * held; };", out)
        self.assertIn("VecGuard_new(VecGuard *this, int * v)", out)
        self.assertIn("VecGuard_drop(VecGuard *this)", out)
        self.assertIn("VecGuard_get(VecGuard *this)", out)
        self.assertIn("this->held = v;", out)

    def test_field_qualification(self):
        out = cpprust.translate("""
class Box {
    int x;
public:
    void set(int v) { x = v; }
};
""")
        self.assertIn("this->x = v;", out)


class TestCppTemplate(unittest.TestCase):
    def test_monomorphise_and_rewrite(self):
        out = cpprust.translate("""
template<typename T>
class Holder {
    T val;
public:
    Holder(T v) { val = v; }
    T get() { return val; }
};
Holder<int> x;
""")
        self.assertIn("struct Holder_int { int val; };", out)
        self.assertIn("Holder_int_new(Holder_int *this, int v)", out)
        self.assertIn("Holder_int x;", out)
        self.assertNotIn("Holder<int>", out)
        self.assertNotIn("template", out)


class TestCppReject(unittest.TestCase):
    def test_typeid_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("void f(void) { typeid(int); }")
        self.assertIn("typeid", cm.exception.message)
        self.assertIn("not in the C++ subset", cm.exception.message)

    def test_new_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("void f() { int *p = new int; }")
        self.assertIn("new", cm.exception.message)


class TestCppScope(unittest.TestCase):
    def test_ctor_at_decl_and_dtor_at_brace(self):
        out = cpprust.translate("""
class Guard {
    int * p;
public:
    Guard(int * v) { p = v; }
    ~Guard() { p = 0; }
};
void run(int *v) {
    Guard g(v);
    (void)v;
}
""")
        self.assertIn("Guard g; Guard_new(&g, v);", out)
        self.assertIn("Guard_drop(&g);", out)
        # Drop comes before the function's closing brace, after the body.
        body = out[out.index("void run"):]
        self.assertLess(body.index("(void)v;"), body.index("Guard_drop(&g);"))
        self.assertLess(body.index("Guard_drop(&g);"), body.rindex("}"))

    def test_inner_block_drop_before_return(self):
        out = cpprust.translate("""
class Guard {
    int * p;
public:
    Guard(int * v) { p = v; }
    ~Guard() { p = 0; }
};
int run(int *v) {
    int x;
    {
        Guard g(v);
        x = 1;
    }
    return x;
}
""")
        # Drop is tied to the inner `}`, so it runs before `return`.
        inner = out[out.index("int x;"):]
        self.assertLess(inner.index("Guard_drop(&g);"), inner.index("return x;"))

    def test_file_scope_decl_untouched(self):
        out = cpprust.translate("""
class Box {
    int x;
public:
    Box(int v) { x = v; }
    ~Box() { x = 0; }
};
Box g;
""")
        self.assertIn("Box g;", out)
        self.assertNotIn("Box_new(&g", out)
        self.assertNotIn("Box_drop(&g", out)


_COUNTER = """
class Counter {
    int n;
public:
    Counter() { n = 0; }
    void bump(int by) { n = n + by; }
    void twice(int by) { bump(by); bump(by); }
    int get() { return n; }
};
"""


class TestCppMethodCalls(unittest.TestCase):
    def test_value_receiver_takes_address(self):
        out = cpprust.translate(_COUNTER + """
int f(void) {
    Counter a;
    a.bump(2);
    return a.get();
}
""")
        self.assertIn("Counter_bump(&a, 2);", out)
        self.assertIn("return Counter_get(&a);", out)

    def test_pointer_receiver_passed_through(self):
        out = cpprust.translate(_COUNTER + """
int f(Counter *p) {
    p->bump(2);
    return p->get();
}
""")
        self.assertIn("Counter_bump(p, 2);", out)
        self.assertIn("return Counter_get(p);", out)

    def test_implicit_this_between_methods(self):
        out = cpprust.translate(_COUNTER)
        self.assertIn("Counter_bump(this, by); Counter_bump(this, by);", out)

    def test_chain_through_class_typed_field(self):
        out = cpprust.translate(_COUNTER + """
class Pair {
    Counter a;
public:
    int total() { return a.get(); }
};
""")
        self.assertIn("Counter_get(&this->a)", out)

    def test_aggregate_body_gets_no_ctor_call(self):
        # A field declaration inside a struct body is not a local.
        out = cpprust.translate(_COUNTER + """
class Pair {
    Counter a;
public:
    int total() { return a.get(); }
};
""")
        self.assertIn("struct Pair { Counter a; };", out)

    def test_unknown_receiver_untouched(self):
        out = cpprust.translate(_COUNTER + """
int f(void) {
    struct Other o;
    return o.field;
}
""")
        self.assertIn("return o.field;", out)

    def test_template_method_call_monomorphised(self):
        out = cpprust.translate("""
template<typename T>
class Holder {
    T val;
public:
    Holder(T v) { val = v; }
    T get() { return val; }
};
int f(void) {
    Holder<int> h(3);
    return h.get();
}
""")
        self.assertIn("Holder_int_get(&h)", out)


class TestCppReferences(unittest.TestCase):
    def test_reference_param_becomes_pointer(self):
        out = cpprust.translate(_COUNTER + """
void addto(Counter &c, int k) { c.bump(k); }
""")
        self.assertIn("void addto(Counter *c, int k)", out)
        self.assertIn("Counter_bump(c, k);", out)

    def test_call_site_takes_address(self):
        out = cpprust.translate(_COUNTER + """
void addto(Counter &c, int k) { c.bump(k); }
void f(void) {
    Counter a;
    addto(a, 1);
}
""")
        self.assertIn("addto(&a, 1);", out)

    def test_prototype_is_not_treated_as_a_call(self):
        out = cpprust.translate(_COUNTER + """
void addto(Counter &c, int k);
""")
        self.assertIn("void addto(Counter *c, int k);", out)
        self.assertNotIn("&Counter", out)

    def test_pointer_argument_not_double_addressed(self):
        out = cpprust.translate(_COUNTER + """
void addto(Counter &c, int k) { c.bump(k); }
void f(Counter *p) {
    addto(*p, 1);
}
""")
        self.assertNotIn("addto(&*p", out)

    def test_reference_local_binds_address(self):
        out = cpprust.translate(_COUNTER + """
int f(void) {
    Counter a;
    Counter &r = a;
    return r.get();
}
""")
        self.assertIn("Counter *r = &(a);", out)
        self.assertIn("Counter_get(r)", out)

    def test_reference_return_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("class B { int v; public: B& self() "
                              "{ return *this; } };")
        self.assertIn("return type is not in the C++ subset",
                      cm.exception.message)


class TestCppArrayField(unittest.TestCase):
    def test_array_field_qualified(self):
        # The declarator suffix is not part of the field name, so uses of
        # `arr` in a method body still resolve to `this->arr`.
        out = cpprust.translate("""
class B {
    int arr[10];
public:
    int get() { return arr[0]; }
};
""")
        self.assertIn("struct B { int arr[10]; };", out)
        self.assertIn("return this->arr[0];", out)


_INNER = """
class Inner {
    int v;
public:
    Inner() { v = 7; }
    ~Inner() { v = 0; }
    int get() { return v; }
};
"""


class TestCppMembers(unittest.TestCase):
    def test_member_constructed_by_containing_ctor(self):
        out = cpprust.translate(_INNER + """
class Outer {
    Inner a;
public:
    Outer() { }
};
""")
        self.assertIn("Inner_new(&this->a);", out)

    def test_implicit_ctor_and_dtor_synthesised(self):
        # Outer declares neither, but its member needs both.
        out = cpprust.translate(_INNER + """
class Outer {
    Inner a;
public:
    int peek() { return a.get(); }
};
""")
        self.assertIn("static void Outer_new(Outer *this) { Inner_new(&this->a); }",
                      out)
        self.assertIn("static void Outer_drop(Outer *this) { Inner_drop(&this->a); }",
                      out)

    def test_members_dropped_in_reverse_order(self):
        out = cpprust.translate(_INNER + """
class Outer {
    Inner a;
    Inner b;
public:
    ~Outer() { }
};
""")
        drop = out[out.index("Outer_drop"):]
        self.assertLess(drop.index("Inner_drop(&this->b);"),
                        drop.index("Inner_drop(&this->a);"))

    def test_initializer_list_not_emitted_verbatim(self):
        out = cpprust.translate(_INNER + """
class Outer {
    int k;
public:
    Outer(int n) : k(n) { }
};
""")
        self.assertIn("static void Outer_new(Outer *this, int n)", out)
        self.assertIn("this->k = n;", out)
        self.assertNotIn(": k(n)", out)

    def test_initializer_list_supplies_member_ctor_args(self):
        out = cpprust.translate("""
class I { int v; public: I(int k) { v = k; } };
class O {
    I a;
public:
    O(int n) : a(n) { }
};
""")
        self.assertIn("I_new(&this->a, n);", out)

    def test_member_without_default_ctor_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("class I { int v; public: I(int k) { v = k; } };"
                              "class O { I a; public: int f() { return 1; } };")
        self.assertIn("no default constructor", cm.exception.message)

    def test_unknown_initializer_name_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("class O { int k; public: O() : nope(1) { } };")
        self.assertIn("not a member", cm.exception.message)

    def test_pointer_member_not_constructed(self):
        out = cpprust.translate(_INNER + """
class Outer {
    Inner * p;
public:
    Outer() { p = 0; }
};
""")
        self.assertNotIn("Inner_new(&this->p)", out)


class TestCppLiterals(unittest.TestCase):
    def test_keyword_inside_string_is_not_rejected(self):
        out = cpprust.translate('void f(void) { puts("new item"); }')
        self.assertIn('puts("new item");', out)

    def test_real_keyword_still_rejected(self):
        with self.assertRaises(cpprust.CppError):
            cpprust.translate("void f(void) { int *p = new int; }")


_GUARD = """
class G {
    int p;
public:
    G() { p = 1; }
    ~G() { p = 0; }
    int get() { return p; }
};
"""


class TestCppUnwind(unittest.TestCase):
    def test_return_drops_before_returning(self):
        out = cpprust.translate(_GUARD + """
int f(int n) {
    G a;
    if (n) { return 1; }
    return 0;
}
""")
        body = out[out.index("int f(int n)"):]
        # The early path spills, drops, then returns.
        self.assertIn("{ int _cpp_ret0 = (1); G_drop(&a); return _cpp_ret0; }",
                      body)

    def test_return_value_spilled_before_drop(self):
        # `return a.get();` must evaluate before the destructor runs.
        out = cpprust.translate(_GUARD + """
int f(void) {
    G a;
    return a.get();
}
""")
        body = out[out.index("int f(void)"):]
        self.assertIn("_cpp_ret0 = (G_get(&a));", body)
        spill = body.index("_cpp_ret0 = (G_get(&a));")
        self.assertLess(spill, body.index("G_drop(&a);"))
        self.assertLess(body.index("G_drop(&a);"), body.index("return _cpp_ret0"))

    def test_return_temporary_uses_function_return_type(self):
        out = cpprust.translate(_GUARD + """
unsigned long f(void) {
    G a;
    return a.get();
}
""")
        self.assertIn("unsigned long _cpp_ret0 =", out)

    def test_void_return_needs_no_temporary(self):
        out = cpprust.translate(_GUARD + """
void f(int n) {
    G a;
    if (n) { return; }
}
""")
        self.assertIn("{ G_drop(&a); return; }", out)
        self.assertNotIn("_cpp_ret", out)

    def test_nested_return_unwinds_outward(self):
        out = cpprust.translate(_GUARD + """
int f(int n) {
    G a;
    {
        G b;
        if (n) { return 1; }
    }
    return 0;
}
""")
        early = out[out.index("if (n)"):]
        self.assertLess(early.index("G_drop(&b);"), early.index("G_drop(&a);"))

    def test_break_and_continue_stop_at_the_loop(self):
        out = cpprust.translate(_GUARD + """
int f(int n) {
    G outer;
    int i;
    for (i = 0; i < n; i = i + 1) {
        G in;
        if (i == 1) { continue; }
        if (i == 2) { break; }
    }
    return 0;
}
""")
        loop = out[out.index("for (i = 0"):]
        self.assertIn("{ G_drop(&in); continue; }", loop)
        self.assertIn("{ G_drop(&in); break; }", loop)
        # `outer` lives past the loop, so neither exit may drop it.
        self.assertNotIn("G_drop(&outer); continue;", out)
        self.assertNotIn("G_drop(&outer); break;", out)

    def test_break_binds_to_switch_not_loop(self):
        out = cpprust.translate(_GUARD + """
int f(int n) {
    int i;
    for (i = 0; i < n; i = i + 1) {
        G in;
        switch (i) {
            case 1: { G s; break; }
        }
    }
    return 0;
}
""")
        sw = out[out.index("switch (i)"):]
        self.assertIn("{ G_drop(&s); break; }", sw)
        self.assertNotIn("G_drop(&in); break;", sw)

    def test_goto_with_pending_destructor_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(_GUARD + """
int f(int n) {
    G a;
    if (n) { goto done; }
done:
    return 0;
}
""")
        self.assertIn("goto done", cm.exception.message)

    def test_goto_without_live_object_is_allowed(self):
        out = cpprust.translate(_GUARD + """
int f(int n) {
    if (n) { goto done; }
done:
    return 0;
}
""")
        self.assertIn("goto done;", out)


_SHAPE = """
class Shape {
    int tag;
public:
    Shape() { tag = 1; }
    virtual int area() { return 0; }
    int describe() { return area() + tag; }
};
class Square : public Shape {
    int w;
public:
    Square(int n) : w(n) { }
    int area() { return w * w; }
};
"""


class TestCppInheritance(unittest.TestCase):
    def test_base_is_the_first_member(self):
        # Layout is what makes an upcast a plain pointer cast.
        out = cpprust.translate(_SHAPE)
        self.assertIn("struct Square { Shape _base; int w; };", out)

    def test_vptr_is_first_in_the_root(self):
        out = cpprust.translate(_SHAPE)
        self.assertIn(
            "struct Shape { const struct Shape_vtable *_vptr; int tag; };",
            out)

    def test_derived_ctor_calls_base_then_installs_vtable(self):
        out = cpprust.translate(_SHAPE)
        ctor = out[out.index("Square_new(Square *this"):]
        ctor = ctor[:ctor.index("}")]
        self.assertLess(ctor.index("Shape_new(&this->_base);"),
                        ctor.index("Square__vtable"))

    def test_override_goes_through_a_thunk(self):
        out = cpprust.translate(_SHAPE)
        self.assertIn(
            "static int Square__thunk_area(Shape *this) "
            "{ return Square_area((Square *)this); }", out)
        self.assertIn("Square__vtable = { &Square__thunk_area };", out)

    def test_vtable_declared_before_the_ctor_uses_it(self):
        out = cpprust.translate(_SHAPE)
        self.assertLess(out.index("Shape__vtable = {"),
                        out.index("static void Shape_new"))

    def test_virtual_call_dispatches(self):
        out = cpprust.translate(_SHAPE + """
int f(Shape *s) { return s->area(); }
""")
        self.assertIn(
            "((const struct Shape_vtable *)s->_vptr)->area(s)", out)

    def test_inherited_method_upcasts(self):
        out = cpprust.translate(_SHAPE + """
int f(void) {
    Square q(2);
    return q.describe();
}
""")
        self.assertIn("Shape_describe(((Shape *)&q))", out)

    def test_non_virtual_caller_still_dispatches_virtually(self):
        # `describe` is not virtual but its call to `area` is.
        out = cpprust.translate(_SHAPE)
        body = out[out.index("Shape_describe(Shape *this)"):]
        self.assertIn("_vptr)->area(this)", body[:body.index("}")])

    def test_destructors_chain_to_the_base(self):
        out = cpprust.translate("""
class B { int a; public: B() { a = 1; } ~B() { a = 0; } };
class D : public B { int b; public: D() { b = 2; } ~D() { b = 0; } };
""")
        drop = out[out.index("D_drop(D *this)"):]
        self.assertIn("B_drop(&this->_base);", drop[:drop.index("}")])

    def test_implicit_dtor_chains_to_the_base(self):
        out = cpprust.translate("""
class B { int a; public: B() { a = 1; } ~B() { a = 0; } };
class D : public B { int b; public: D() { b = 2; } };
""")
        self.assertIn("static void D_drop(D *this) { B_drop(&this->_base); }",
                      out)

    def test_base_ctor_args_from_initializer_list(self):
        out = cpprust.translate("""
class B { int a; public: B(int k) { a = k; } };
class D : public B { public: D(int n) : B(n) { } };
""")
        self.assertIn("B_new(&this->_base, n);", out)

    def test_base_without_default_ctor_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("class B { int a; public: B(int k) { a = k; } };"
                              "class D : public B { public: D() { } };")
        self.assertIn("no default constructor", cm.exception.message)

    def test_abstract_class_cannot_be_instantiated(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("class A { public: virtual int f() = 0; };"
                              "\nint g(void) { A a; return 0; }")
        self.assertIn("cannot be instantiated", cm.exception.message)

    def test_abstract_class_emits_no_vtable_instance(self):
        out = cpprust.translate("class A { public: virtual int f() = 0; };")
        self.assertIn("struct A_vtable", out)
        self.assertNotIn("A__vtable = {", out)

    def test_pure_virtual_dispatches_from_a_base_pointer(self):
        out = cpprust.translate("""
class A { public: virtual int f() = 0; };
class C : public A { int v; public: C() { v = 3; } int f() { return v; } };
int use(A *p) { return p->f(); }
""")
        self.assertIn("((const struct A_vtable *)p->_vptr)->f(p)", out)
        self.assertIn("C__vtable = { &C__thunk_f };", out)

    def test_multiple_inheritance_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("class X { int a; }; class Y { int b; };"
                              " class Z : public X, public Y { int c; };")
        self.assertIn("multiple inheritance", cm.exception.message)

    def test_undefined_base_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("class D : public Missing { int a; };")
        self.assertIn("not defined above it", cm.exception.message)

    def test_virtual_declaration_without_body_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("class A { public: virtual int f(); };")
        self.assertIn("without a body", cm.exception.message)


class TestCppComments(unittest.TestCase):
    """Prose must never change what the lowering does.

    Both of these produced no diagnostic -- just objects that were silently
    never constructed -- so they are worth pinning down.
    """

    def test_word_struct_in_a_comment(self):
        # The struct-body check scans backwards; on raw text, prose
        # mentioning "struct" made every later brace look like a struct body.
        out = cpprust.translate("""
class B { int a; public: B() { a = 1; } };
/* a struct pointer */
void f(void) { B b; }
""")
        self.assertIn("B b; B_new(&b);", out)

    def test_apostrophe_in_a_comment(self):
        # An apostrophe used to open a string literal, swallowing every
        # brace up to the next one.
        out = cpprust.translate("""
class B { int a; public: B() { a = 1; } };
/* the class's table and its base's slots */
void f(void) { B b; }
""")
        self.assertIn("B b; B_new(&b);", out)

    def test_apostrophe_does_not_swallow_method_calls(self):
        out = cpprust.translate("""
class B { int a; public: B() { a = 1; } int get() { return a; } };
/* what the caller's code does */
int f(void) { B b; return b.get(); }
""")
        self.assertIn("B_get(&b)", out)

    def test_real_string_literals_still_respected(self):
        # Blanking comments must not blank strings: a brace in a literal is
        # still not a brace.
        out = cpprust.translate("""
class B { int a; public: B() { a = 1; } };
void f(void) { puts("} not a brace {"); B b; }
""")
        self.assertIn('puts("} not a brace {");', out)
        self.assertIn("B b; B_new(&b);", out)


    def test_array_of_class_pointers_not_a_scalar_symbol(self):
        # `B *arr[2]` declares an array, not a `B *`. Registering `arr` as a
        # receiver would rewrite `arr->get()` into something that is not what
        # the author wrote. Subscripted receivers are simply not resolved.
        out = cpprust.translate("""
class B { int a; public: int get() { return a; } };
int f(void) {
    B *arr[2];
    B one;
    return one.get();
}
""")
        self.assertIn("B *arr[2];", out)
        self.assertIn("B_get(&one)", out)


if __name__ == "__main__":
    unittest.main()
