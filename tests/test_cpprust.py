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


_PAIR = """
template<typename K, typename V>
class Pair {
    K key;
    V val;
public:
    Pair(K k, V v) { key = k; val = v; }
    K first() { return key; }
    V second() { return val; }
};
"""


class TestCppMultiTemplate(unittest.TestCase):
    def test_two_parameters_monomorphise(self):
        out = cpprust.translate(_PAIR + "Pair<int, double> p;\n")
        self.assertIn("struct Pair_int_double { int key; double val; };", out)
        self.assertIn("Pair_int_double_new(Pair_int_double *this, "
                      "int k, double v)", out)
        self.assertIn("Pair_int_double p;", out)
        self.assertNotIn("Pair<", out)

    def test_distinct_instantiations_are_distinct_structs(self):
        out = cpprust.translate(
            _PAIR + "void f(void) { Pair<int, double> a; Pair<char, int> b; }")
        self.assertIn("struct Pair_int_double { int key; double val; };", out)
        self.assertIn("struct Pair_char_int { char key; int val; };", out)

    def test_method_calls_pick_the_right_instantiation(self):
        out = cpprust.translate(_PAIR + """
int f(void) {
    Pair<int, double> a(1, 2.0);
    Pair<char, int> b(65, 9);
    return a.first() + b.second();
}
""")
        self.assertIn("Pair_int_double_first(&a)", out)
        self.assertIn("Pair_char_int_second(&b)", out)

    def test_substitution_does_not_cascade(self):
        """`<A,B>` instantiated as `<B,char>` must not rewrite A to B to char.

        Substituting one parameter at a time would re-examine the text just
        produced, so field `A a;` would become `B a;` and then `char a;`,
        silently giving both fields the same type.
        """
        out = cpprust.translate("""
typedef int B;
template<typename A, typename B>
class Two { A a; B b; };
Two<B, char> x;
""")
        self.assertIn("struct Two_B_char { B a; char b; };", out)

    def test_nested_instantiation_as_argument(self):
        out = cpprust.translate("""
template<typename A, typename B>
class Pair { A x; B y; };
template<typename T>
class Holder { T v; };
Holder<Pair<int, char> > h;
""")
        self.assertIn("struct Pair_int_char { int x; char y; };", out)
        self.assertIn("struct Holder_Pair_int_char { Pair_int_char v; };", out)
        self.assertIn("Holder_Pair_int_char h;", out)

    def test_closing_angles_without_a_space(self):
        """`>>` closes two argument lists; it is not a shift here."""
        out = cpprust.translate("""
template<typename A, typename B>
class Pair { A x; B y; };
template<typename T>
class Holder { T v; };
Holder<Pair<int,char>> h;
""")
        self.assertIn("struct Holder_Pair_int_char { Pair_int_char v; };", out)
        self.assertIn("Holder_Pair_int_char h;", out)
        self.assertNotIn("Pair<", out)
        self.assertNotIn("Holder<", out)

    def test_non_type_parameter_substitutes_array_dimension(self):
        out = cpprust.translate("""
template<typename T, int N>
class Buf {
    T data[N];
public:
    int cap() { return N; }
};
Buf<int, 8> b;
""")
        self.assertIn("struct Buf_int_8 { int data[8]; };", out)
        self.assertIn("return 8;", out)

    def test_wrong_arity_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(_PAIR + "Pair<int> p;\n")
        self.assertIn("takes 2 template arguments", cm.exception.message)

    def test_default_argument_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(
                "template<typename A, typename B = int>\nclass P { A a; };\n")
        self.assertIn("default template argument", cm.exception.message)

    def test_parameter_pack_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(
                "template<typename... Ts>\nclass P { int a; };\n")
        self.assertIn("parameter pack", cm.exception.message)

    def test_duplicate_parameter_name_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(
                "template<typename T, typename T>\nclass P { T a; };\n")
        self.assertIn("duplicate", cm.exception.message)

    def test_argument_naming_a_later_class_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
template<typename T>
class Holder { T v; };
template<typename A, typename B>
class Pair { A x; B y; };
Holder<Pair<int,char> > h;
""")
        self.assertIn("declared below it", cm.exception.message)

    def test_instantiation_inside_a_template_is_error(self):
        """Not discoverable, so reported rather than left dangling.

        The recording scan blanks template bodies, because there `Inner<T>`
        is the pattern. So this instantiation is only revealed after `T` is
        substituted, by which time the class list is fixed -- emitting the
        name anyway would reference a struct that is never defined.
        """
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
template<typename T>
class Inner { T v; };
template<typename T>
class Outer { Inner<T> i; };
Outer<int> o;
""")
        self.assertIn("instantiated from inside another template",
                      cm.exception.message)

    def test_relational_operator_is_not_a_template_use(self):
        """A template name in a comparison must not swallow the expression."""
        out = cpprust.translate(_PAIR + """
int f(int Pair_x, int b) { return Pair_x < b; }
""")
        self.assertIn("return Pair_x < b;", out)

    def test_destructors_run_for_each_instantiation(self):
        out = cpprust.translate("""
template<typename K, typename V>
class E {
    K k;
    V v;
public:
    E() { k = 0; }
    ~E() { k = 0; }
};
void f(void) { E<int, char> a; E<char, int> b; }
""")
        self.assertIn("E_char_int_drop(&b);", out)
        self.assertIn("E_int_char_drop(&a);", out)
        # Reverse declaration order: the later object drops first.
        self.assertLess(out.index("E_char_int_drop(&b);"),
                        out.index("E_int_char_drop(&a);"))


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


_NODE = """
class Node {
    int v;
public:
    Node(int x) { v = x; }
    ~Node() { v = 0; }
    int get() { return v; }
};
"""


class TestCppNewDelete(unittest.TestCase):
    """`new` lowers to a helper, `delete` lowers in place.

    `new T(..)` sits in expression position and C has no statement
    expression, so allocate-construct-yield has to be a function. `delete`
    is a statement, so it needs no helper.
    """

    def test_new_calls_the_alloc_helper(self):
        out = cpprust.translate(_NODE + """
void f(void) { Node *p = new Node(5); }
""")
        self.assertIn("Node *p = Node__alloc(5);", out)

    def test_alloc_helper_constructs_and_returns(self):
        out = cpprust.translate(_NODE + "void f(void) { Node *p = new Node(5); }")
        self.assertIn("static Node *Node__alloc(int x) {", out)
        self.assertIn("malloc(sizeof(Node))", out)
        self.assertIn("Node_new(p, x);", out)

    def test_alloc_does_not_construct_through_a_failed_malloc(self):
        # No exceptions in the subset, so `new` yields null and the caller
        # checks -- constructing through null would fault instead.
        out = cpprust.translate(_NODE + "void f(void) { Node *p = new Node(5); }")
        self.assertIn("if (p) { Node_new(p, x); }", out)

    def test_new_without_parentheses(self):
        out = cpprust.translate("""
class Bare { public: int v; };
void f(void) { Bare *b = new Bare; }
""")
        self.assertIn("Bare *b = Bare__alloc();", out)

    def test_delete_drops_then_frees(self):
        out = cpprust.translate(_NODE + "void f(Node *p) { delete p; }")
        self.assertIn("Node_drop(p);", out)
        self.assertIn("free(p);", out)
        self.assertLess(out.index("Node_drop(p);"), out.index("free(p);"))

    def test_delete_is_a_no_op_on_null(self):
        out = cpprust.translate(_NODE + "void f(Node *p) { delete p; }")
        self.assertIn("if (p)", out)

    def test_delete_survives_an_else_branch(self):
        # A bare block would leave a stray `;` before the `else`.
        out = cpprust.translate(_NODE + """
void f(Node *p, int c) { if (c) delete p; else p->v = 2; }
""")
        self.assertIn("while (0); else", out)

    def test_delete_without_a_destructor_is_just_free(self):
        out = cpprust.translate("""
class Bare { public: int v; };
void f(Bare *b) { delete b; }
""")
        self.assertIn("free(b)", out)
        self.assertNotIn("Bare_drop", out)

    def test_delete_this(self):
        out = cpprust.translate("""
class N { public: int v; N() { v = 0; } ~N() { v = 1; } void kill() { delete this; } };
""")
        self.assertIn("N_drop(this);", out)
        self.assertIn("free(this);", out)

    def test_heap_pointer_gets_no_scope_drop(self):
        # A pointer is not an automatic object; C++ leaks this too.
        out = cpprust.translate(_NODE + "void f(void) { Node *p = new Node(1); }")
        self.assertNotIn("Node_drop(&p)", out)

    def test_malloc_and_free_are_declared(self):
        out = cpprust.translate(_NODE + "void f(void) { Node *p = new Node(1); }")
        self.assertIn("void *malloc(unsigned long);", out)
        self.assertIn("void free(void *);", out)

    def test_no_prelude_when_the_heap_is_unused(self):
        out = cpprust.translate(_NODE + "int f(void) { Node n(1); return n.get(); }")
        self.assertNotIn("malloc", out)

    def test_alloc_helper_only_for_classes_that_need_it(self):
        out = cpprust.translate(_NODE + """
class Other { public: int v; Other() { v = 0; } };
void f(void) { Node *p = new Node(1); }
""")
        self.assertIn("Node__alloc", out)
        self.assertNotIn("Other__alloc", out)

    def test_template_instantiation_allocates(self):
        out = cpprust.translate("""
template<typename T>
class Box { T v; public: Box(T x) { v = x; } };
void f(void) { Box<int> *b = new Box<int>(3); }
""")
        self.assertIn("static Box_int *Box_int__alloc(int x)", out)
        self.assertIn("Box_int *b = Box_int__alloc(3);", out)

    def test_new_in_a_method_body(self):
        out = cpprust.translate("""
class Node { public: int v; Node(int x) { v = x; } };
class Maker {
    int seed;
public:
    Maker() { seed = 2; }
    Node *make() { return new Node(seed); }
};
""")
        self.assertIn("return Node__alloc(this->seed);", out)

    def test_keyword_in_a_literal_is_not_an_allocation(self):
        out = cpprust.translate(_NODE + """
int puts(const char *s);
void f(void) { puts("new Node and delete it"); }
""")
        self.assertIn('puts("new Node and delete it");', out)
        self.assertNotIn("malloc", out)

    def test_new_of_a_non_class_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(_NODE + "void f(void) { int *p = new int; }")
        self.assertIn("not a class", cm.exception.message)

    def test_array_new_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(_NODE + "void f(void) { Node *p = new Node[4]; }")
        self.assertIn("array `new`", cm.exception.message)

    def test_array_delete_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(_NODE + "void f(Node *p) { delete[] p; }")
        self.assertIn("delete[]", cm.exception.message)

    def test_deleting_a_value_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(_NODE + "void f(void) { Node n(1); delete n; }")
        self.assertIn("not a pointer", cm.exception.message)

    def test_new_of_an_abstract_class_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
class Shape { public: virtual int area() = 0; };
void f(void) { Shape *s = new Shape(); }
""")
        self.assertIn("pure virtual", cm.exception.message)

    def test_delete_through_a_virtual_destructor_is_error(self):
        """The vtable carries methods only, so a `_drop` cannot dispatch.

        Deleting through a base pointer would run the base destructor and
        leave the derived part untouched -- the exact bug `virtual ~T()` is
        written to prevent -- so it is reported rather than mistranslated.
        """
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
class B { public: int v; B() { v = 0; } virtual ~B() { v = 1; } };
void f(B *b) { delete b; }
""")
        self.assertIn("virtual destructor", cm.exception.message)

    def test_delete_of_an_untyped_expression_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(_NODE + "void f(void *q) { delete (Node *)q; }")
        self.assertIn("cannot tell what type", cm.exception.message)


class TestCppFieldAccess(unittest.TestCase):
    """A member read or write has to follow the same pointer-ness as a call.

    `_lower_refs` turns `T &c` into `T *c`, so a `.` the author wrote is now
    applied to a pointer. Only method *calls* were being rewritten, so
    `c.v = 1` came out unchanged and the generated C did not compile.
    """

    def test_reference_param_field_becomes_arrow(self):
        out = cpprust.translate(_COUNTER + """
void addto(Counter &c, int k) { c.n = c.n + k; }
""")
        self.assertIn("c->n = c->n + k;", out)

    def test_value_receiver_keeps_the_dot(self):
        out = cpprust.translate(_COUNTER + """
int f(void) { Counter c; return c.n; }
""")
        self.assertIn("return c.n;", out)

    def test_reference_local_field_becomes_arrow(self):
        out = cpprust.translate(_COUNTER + """
void f(void) { Counter c; Counter &r = c; r.n = 3; }
""")
        self.assertIn("r->n = 3;", out)

    def test_chain_mixes_operators_per_step(self):
        out = cpprust.translate("""
class Inner { public: int n; Inner() { n = 0; } };
class Outer { public: Inner in; Outer() { } };
void f(Outer &o) { o.in.n = 5; }
""")
        self.assertIn("o->in.n = 5;", out)

    def test_field_nested_in_a_by_reference_argument(self):
        # The call is emitted whole, arguments included, so the main scan
        # never reaches inside them and re-running the pass re-copies the
        # same text. The chain has to be fixed where it is copied.
        out = cpprust.translate("""
class Inner { public: int n; Inner() { n = 0; } };
class Outer { public: Inner in; Outer() { } };
void bump(Inner &i) { i.n = i.n + 1; }
void f(Outer &o) { bump(o.in); }
""")
        self.assertIn("bump(&o->in);", out)

    def test_array_field_through_a_reference(self):
        out = cpprust.translate("""
class B { public: int arr[4]; B() { arr[0] = 1; } };
int f(B &b) { return b.arr[2]; }
""")
        self.assertIn("return b->arr[2];", out)

    def test_plain_c_struct_access_is_untouched(self):
        out = cpprust.translate(_COUNTER + """
struct P { int x; };
int f(struct P *p, struct P q) { return p->x + q.x; }
""")
        self.assertIn("return p->x + q.x;", out)

    def test_unknown_member_is_left_alone(self):
        # Not a field of the class, so nothing is known about it; the C
        # compiler is the right place for that error.
        out = cpprust.translate(_COUNTER + """
void f(Counter &c) { c.nosuch = 1; }
""")
        self.assertIn("c.nosuch = 1;", out)

    def test_rewriting_is_idempotent(self):
        # `_rewrite_calls` runs to a fixed point, so a chain it has already
        # converted must convert to itself on the next pass.
        src = _COUNTER + "void f(Counter &c) { c.n = 1; }\n"
        once = cpprust.translate(src)
        self.assertIn("c->n = 1;", once)
        self.assertNotIn("c->->n", once)
        self.assertNotIn("c.n = 1;", once)


class TestCppLiteralsAndComments(unittest.TestCase):
    """A rewrite must not reach inside a string literal or a comment.

    Every body-level pass here is a regex over source text, and a regex
    cannot tell a field named `key` from the word `key` in
    `printf("key=%d", key)`. Rewriting the literal changes what the program
    prints; a `//` comment carried into a member declaration comments out
    the generated code that follows it. Neither produced a diagnostic.
    """

    def test_field_name_in_a_literal_is_not_qualified(self):
        out = cpprust.translate("""
int printf(const char *fmt, ...);
class C {
    int key;
public:
    C() { key = 1; }
    void show() { printf("key=%d\\n", key); }
};
""")
        self.assertIn('printf("key=%d\\n", this->key);', out)
        self.assertNotIn('"this->key', out)

    def test_method_name_in_a_literal_gets_no_implicit_this(self):
        out = cpprust.translate("""
int puts(const char *s);
class C {
    int n;
public:
    C() { n = 0; }
    int helper(void) { return n; }
    int go(void) { puts("call helper() now"); return helper(); }
};
""")
        self.assertIn('puts("call helper() now")', out)
        self.assertIn("return C_helper(this);", out)

    def test_template_parameter_in_a_literal_is_not_substituted(self):
        out = cpprust.translate("""
int puts(const char *s);
template<typename T>
class C { T v; public: void go() { puts("T is the parameter"); } };
C<int> c;
""")
        self.assertIn('puts("T is the parameter")', out)

    def test_reference_spelling_in_a_literal_is_not_lowered(self):
        out = cpprust.translate("""
int puts(const char *s);
class Counter { public: int v; Counter() { v = 0; } };
void g(void) { puts("Counter &c is a reference"); }
""")
        self.assertIn('puts("Counter &c is a reference")', out)

    def test_line_comment_in_a_class_body_does_not_reach_the_output(self):
        # A member is emitted onto one line, so a `//` carried through from
        # the class body commented out the declaration after it -- the
        # generated C was broken, with no diagnostic from here.
        out = cpprust.translate("""
class D {
    int a;
public:
    // set it
    void set(int v) { a = v; }
};
""")
        self.assertNotIn("//", out)
        self.assertIn("static void D_set(D *this, int v)", out)

    def test_block_comment_in_a_class_body_does_not_reach_the_output(self):
        out = cpprust.translate("""
class D {
    int a;
public:
    /* set it */
    void set(int v) { a = v; }
};
""")
        self.assertNotIn("set it", out)
        self.assertIn("static void D_set(D *this, int v)", out)

    def test_field_name_in_a_comment_is_not_qualified(self):
        out = cpprust.translate("""
class D {
    int a;
public:
    D() { a = 1; /* a starts at one */ }
};
""")
        self.assertNotIn("this->a starts", out)

    def test_escaped_quote_and_char_literal_survive(self):
        out = cpprust.translate("""
int puts(const char *s);
class C {
    int n;
public:
    C() { n = 0; }
    int f(void) { puts("say \\"n\\" now"); return n + 'n'; }
};
""")
        self.assertIn('puts("say \\"n\\" now")', out)
        self.assertIn("return this->n + 'n';", out)


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
