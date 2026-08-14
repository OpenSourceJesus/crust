"""Pure-translation tests for the C++ subset front end (tools.cpprust)."""

import os
import unittest

import tools.cpprust as cpprust
import tools.cpp_auto as cpp_auto


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
            _PAIR + "void f(void) { Pair<int, double> a(1, 2.0); "
            "Pair<char, int> b(65, 9); }")
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

    def test_instantiation_inside_a_template(self):
        """`Outer<int>` asks for `Inner<int>` only once `T` is known.

        The recording scan blanks template bodies, so a nested use is
        invisible to it. The set is closed transitively instead:
        each instantiation's arguments are substituted into its own body and
        that is scanned in turn.
        """
        out = cpprust.translate("""
template<typename T>
class Inner { public: T v; Inner() { } T get() { return v; } };
template<typename T>
class Outer { public: Inner<T> i; Outer() { } T get() { return i.get(); } };
void f(void) { Outer<int> o; }
""")
        self.assertIn("struct Inner_int { int v; };", out)
        self.assertIn("struct Outer_int { Inner_int i; };", out)
        self.assertIn("Inner_int_get(&this->i)", out)

    def test_transitive_instantiation_per_argument(self):
        out = cpprust.translate("""
template<typename T>
class Inner { public: T v; Inner() { } };
template<typename T>
class Outer { public: Inner<T> i; Outer() { } };
void f(void) { Outer<int> a; Outer<char> b; }
""")
        self.assertIn("struct Inner_int { int v; };", out)
        self.assertIn("struct Inner_char { char v; };", out)

    def test_nested_instantiation_declared_below_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
template<typename T>
class Outer { public: Inner<T> i; Outer() { } };
template<typename T>
class Inner { public: T v; Inner() { } };
void f(void) { Outer<int> o; }
""")
        self.assertIn("declared below", cm.exception.message)

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
        # (message unchanged: a member still needs a *default* constructor)
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
        # The *definition*, not the prototype. Prototypes are hoisted above
        # every definition now -- a template instantiated over a class
        # declared below it needs that -- so a bare name matches the
        # declaration first, and slicing to the next `}` then gives an empty
        # body rather than the constructor.
        ctor = out[out.index("Square_new(Square *this, int n) {"):]
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
        # Against the constructor's definition: its prototype is hoisted, and
        # a prototype above the table is fine -- what must not happen is the
        # body installing a table that has not been defined yet.
        self.assertLess(out.index("Shape__vtable = {"),
                        out.index("static void Shape_new(Shape *this) {"))

    def test_virtual_call_dispatches(self):
        out = cpprust.translate(_SHAPE + """
int f(Shape *s) { return s->area(); }
""")
        self.assertIn(
            "((const struct Shape_vtable *)(s)->_vptr)->area(s)", out)

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
        body = out[out.index("Shape_describe(Shape *this) {"):]
        self.assertIn("_vptr)->area(this)", body[:body.index("}")])

    def test_destructors_chain_to_the_base(self):
        out = cpprust.translate("""
class B { int a; public: B() { a = 1; } ~B() { a = 0; } };
class D : public B { int b; public: D() { b = 2; } ~D() { b = 0; } };
""")
        # Slice from the *definition*: every member is prototyped first, so
        # the name occurs earlier without a body.
        drop = out[out.index("static void D_drop(D *this) {"):]
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
        self.assertIn("no constructor taking 0", cm.exception.message)

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
        self.assertIn("((const struct A_vtable *)(p)->_vptr)->f(p)", out)
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
        # Still an error, and now with the fix named: a member with no
        # body needs an out-of-line definition in the same translation.
        self.assertIn("declared but never defined", cm.exception.message)


_NODE = """
class Node {
    int v;
public:
    Node(int x) { v = x; }
    ~Node() { v = 0; }
    int get() { return v; }
};
"""


_HIER = """
class Base {
public:
    int id;
    Base() { id = 1; }
    virtual ~Base() { id = 0; }
    virtual int area() { return 1; }
};
class Derived : public Base {
public:
    int w;
    Derived() { w = 2; }
    ~Derived() { w = 0; }
    int area() { return 2; }
};
"""


_CHAIN = """
class Node {
    int v;
public:
    Node(int x) { v = x; }
    int get() { return v; }
    Node *self() { return this; }
};
class Owner {
    Node *held;
public:
    Owner() { held = 0; }
    Node *node() { return held; }
};
"""


_COPYABLE = """
class Buf {
public:
    int *p;
    Buf() { p = 0; }
    Buf(const Buf &o) { p = o.p; }
    ~Buf() { p = 0; }
};
"""

_OWNING = """
class Own {
public:
    int *p;
    Own() { p = 0; }
    ~Own() { p = 0; }
};
"""


_ASSIGNABLE = """
class Buf {
public:
    int *p;
    Buf() { p = 0; }
    Buf(const Buf &o) { p = o.p; }
    Buf &operator=(const Buf &o) { p = o.p; }
    ~Buf() { p = 0; }
};
"""


class TestCppByValue(unittest.TestCase):
    """An owning class cannot cross a call boundary by value.

    Both forms were silent. A by-value parameter is a struct copy no
    constructor ran for and no destructor will run for. A by-value return is
    worse: the local is destroyed on the way out, so the caller receives a
    copy of a released object -- a use-after-free.
    """

    def test_by_value_parameter_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(_OWNING + "void take(Own b) { b.p = 0; }")
        self.assertIn("by value", cm.exception.message)

    def test_by_value_return_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(_OWNING + "Own make(void) { Own t; return t; }")
        self.assertIn("released object", cm.exception.message)

    def test_by_reference_is_fine(self):
        out = cpprust.translate(_OWNING + "void take(Own &b) { b.p = 0; }")
        self.assertIn("void take(Own *b)", out)

    def test_pointer_return_is_fine(self):
        out = cpprust.translate(_OWNING + "Own *make(void);")
        self.assertIn("Own *make(void);", out)

    def test_non_owning_class_may_pass_by_value(self):
        # No destructor, so nothing owns anything and the copy is harmless.
        out = cpprust.translate("""
class Pod { public: int x; Pod() { x = 0; } };
Pod make(void);
void take(Pod p);
""")
        self.assertIn("void take(Pod p);", out)

    def test_a_local_with_arguments_is_not_a_declaration(self):
        # `Node n(1);` parses like a function returning `Node`; it is not.
        out = cpprust.translate(_NODE + "void f(void) { Node n(1); }")
        self.assertIn("Node_new(&n, 1);", out)


class TestCppAssignOperator(unittest.TestCase):
    """`operator=` is the one overload the subset supports."""

    def test_assignment_calls_the_operator(self):
        out = cpprust.translate(_ASSIGNABLE + """
void f(void) { Buf a; Buf b; b = a; }
""")
        self.assertIn("Buf__assign(&b, &a);", out)

    def test_operator_does_not_collide_with_a_method_named_assign(self):
        out = cpprust.translate("""
class S {
public:
    int v;
    S() { v = 0; }
    S &operator=(const S &o) { v = o.v; }
    ~S() { v = 0; }
    void assign(int k) { v = k; }
};
""")
        self.assertIn("static void S__assign(S *this, const S *o)", out)
        self.assertIn("static void S_assign(S *this, int k)", out)

    def test_other_operators_are_still_rejected(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(
                "class S { public: int v; S() { v=0; } "
                "int operator+(const S &o) { return v; } };")
        self.assertIn("operator+", cm.exception.message)

    def test_chained_assignment_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(_ASSIGNABLE + """
void f(void) { Buf a; Buf b; Buf c; c = b = a; }
""")
        self.assertIn("chained assignment", cm.exception.message)

    def test_without_the_operator_assignment_is_still_refused(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(_OWNING + "void f(void) { Own a; Own b; b = a; }")
        self.assertIn("operator=", cm.exception.message)


class TestCppMemberPrototypes(unittest.TestCase):
    def test_a_method_may_call_one_declared_below_it(self):
        # Members are emitted in declaration order, so without prototypes
        # this was an implicit declaration in C.
        out = cpprust.translate("""
class C {
public:
    int v;
    C() { v = 0; }
    int first() { return second(); }
    int second() { return v; }
};
""")
        self.assertIn("static int C_second(C *this);", out)
        self.assertLess(out.index("static int C_second(C *this);"),
                        out.index("static int C_first(C *this) {"))


class TestCppStd(unittest.TestCase):
    """`std::string` and `std::vector`, written in the subset itself.

    They are not special-cased anywhere in the lowering: they go through the
    same passes as user code, so if they translate, the subset is expressive
    enough to have written them.
    """

    def test_string_is_supplied_on_demand(self):
        out = cpprust.translate("void f(void) { std::string s; }")
        self.assertIn("struct string {", out)
        self.assertIn("void string_new(string *this)", out)

    def test_namespace_is_stripped(self):
        out = cpprust.translate("void f(void) { std::string s; }")
        self.assertNotIn("std::", out)

    def test_include_form_works_too(self):
        out = cpprust.translate("#include <string>\nvoid f(void) { string s; }")
        self.assertIn("struct string {", out)
        self.assertNotIn("#include <string>", out)

    def test_nothing_is_supplied_when_unused(self):
        out = cpprust.translate("int f(void) { return 1; }")
        self.assertNotIn("struct string", out)
        self.assertNotIn("struct vector", out)

    def test_vector_monomorphises_per_element_type(self):
        out = cpprust.translate("""
void f(void) { std::vector<int> a; std::vector<char> b; }
""")
        self.assertIn("struct vector_int {", out)
        self.assertIn("struct vector_char {", out)

    def test_vector_brings_string_for_nesting(self):
        # `string` is declared above `vector`, so `vector<string>` would find
        # it complete -- the same declaration-order rule as any nesting.
        out = cpprust.translate("void f(void) { std::vector<int> v; }")
        self.assertLess(out.index("struct string {"),
                        out.index("class vector") if "class vector" in out
                        else out.index("struct vector_int {"))

    def test_string_has_copy_and_assignment(self):
        out = cpprust.translate("void f(void) { std::string s; }")
        self.assertIn("void string_copy(string *this,", out)
        self.assertIn("void string__assign(string *this,", out)

    def test_owning_element_type_is_refused_clearly(self):
        # `vector<T>` stores by assignment, which for a class with a
        # destructor would leave two owners.
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(
                "int f(void) { std::vector<std::string> v; return v.size(); }")
        self.assertIn("stores its elements by assignment",
                      cm.exception.message)
        self.assertIn("vector<string *>", cm.exception.message)


_ARR = """
class Arr {
public:
    int d[8];
    Arr() { d[0] = 0; }
    int &operator[](int i) { return d[i]; }
};
"""

_POINT = """
class Point {
public:
    int x;
    int y;
    Point() { x = 0; y = 0; }
    Point(int a) { x = a; y = a; }
    Point(int a, int b) { x = a; y = b; }
};
"""


class TestCppCtorOverload(unittest.TestCase):
    """Constructors are told apart by argument count.

    A call site is matched before types are known, so arity is the only
    thing there is to resolve on -- which is why two constructors of the
    same arity are refused rather than guessed between.
    """

    def test_each_arity_gets_its_own_symbol(self):
        out = cpprust.translate(_POINT + "void f(void) { Point p; }")
        self.assertIn("static void Point_new(Point *this)", out)
        self.assertIn("static void Point_new_1(Point *this, int a)", out)
        self.assertIn("static void Point_new_2(Point *this, int a, int b)",
                      out)

    def test_declaration_picks_by_argument_count(self):
        out = cpprust.translate(_POINT + """
void f(void) { Point p; Point q(5); Point r(2, 3); }
""")
        self.assertIn("Point_new(&p);", out)
        self.assertIn("Point_new_1(&q, 5);", out)
        self.assertIn("Point_new_2(&r, 2, 3);", out)

    def test_new_picks_the_matching_allocator(self):
        out = cpprust.translate(_POINT + """
void f(void) { Point *a = new Point(); Point *b = new Point(7, 8); }
""")
        self.assertIn("Point__alloc()", out)
        self.assertIn("Point__alloc_2(7, 8)", out)

    def test_single_constructor_keeps_the_plain_name(self):
        # Unchanged from before overloading existed.
        out = cpprust.translate(
            "class C { public: int v; C(int k) { v = k; } };"
            "void f(void) { C c(1); }")
        self.assertIn("static void C_new(C *this, int k)", out)
        self.assertIn("C_new(&c, 1);", out)

    def test_wrong_argument_count_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(_POINT + "void f(void) { Point p(1, 2, 3); }")
        self.assertIn("no constructor taking 3", cm.exception.message)

    def test_missing_default_constructor_is_error(self):
        # Previously this emitted a call with too few arguments and left the
        # C compiler to report it.
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(
                "class C { public: int v; C(int k) { v = k; } };"
                "void f(void) { C c; }")
        self.assertIn("no constructor taking 0", cm.exception.message)

    def test_base_overload_from_initializer_list(self):
        out = cpprust.translate(_POINT + """
class P3 : public Point { public: int z; P3(int a, int b) : Point(a, b) { z = 0; } };
void f(void) { P3 p(1, 2); }
""")
        self.assertIn("Point_new_2(&this->_base, a, b);", out)

    def test_string_takes_a_literal(self):
        out = cpprust.translate('void f(void) { std::string s("hi"); }')
        self.assertIn("string_new_1(&s,", out)


class TestCppSubscript(unittest.TestCase):
    """`operator[]` returns a reference, so `v[i] = x` assigns the element."""

    def test_subscript_lowers_to_a_dereference(self):
        out = cpprust.translate(_ARR + "int f(void) { Arr v; return v[2]; }")
        self.assertIn("(*Arr__index(&v, 2))", out)

    def test_subscript_is_an_lvalue(self):
        out = cpprust.translate(_ARR + "void f(void) { Arr v; v[2] = 42; }")
        self.assertIn("(*Arr__index(&v, 2)) = 42;", out)

    def test_operator_returns_the_address(self):
        out = cpprust.translate(_ARR)
        self.assertIn("static int * Arr__index(Arr *this, int i) "
                      "{ return &(this->d[i]); }", out)

    def test_by_value_subscript_is_error(self):
        # `v[i] = x` would assign to a copy.
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
class A { public: int d[4]; A() { d[0] = 0; } int operator[](int i) { return d[i]; } };
""")
        self.assertIn("has to return a reference", cm.exception.message)

    def test_subscript_through_a_member(self):
        out = cpprust.translate(_ARR + """
class H { public: Arr a; H() { } };
void f(void) { H h; h.a[0] = 7; }
""")
        self.assertIn("(*Arr__index(&h.a, 0)) = 7;", out)

    def test_plain_c_array_is_untouched(self):
        out = cpprust.translate(_ARR + """
int f(int *a) { int b[4]; b[0] = 1; return a[0] + b[0]; }
""")
        self.assertIn("return a[0] + b[0];", out)

    def test_pointer_field_indexing_is_untouched(self):
        # `T *p; p[i]` walks an array; it is not `operator[]` on what `p`
        # points at. Fields record their pointer-ness truthfully.
        out = cpprust.translate(_ARR + """
class Box { public: Arr *items; int n; Box() { items = 0; n = 0; }
            void put(int i) { items[i].d[0] = 1; } };
""")
        self.assertIn("this->items[i]", out)
        self.assertNotIn("Arr__index(this->items", out)

    def test_operator_index_is_not_read_as_a_lambda(self):
        # `operator[](int i) { .. }` has the shape of an empty capture list.
        out = cpprust.translate(_ARR)
        self.assertNotIn("_cpp_lambda", out)

    def test_container_subscript(self):
        out = cpprust.translate("""
void f(void) { std::vector<int> v; v.push_back(1); v[0] = 2; }
""")
        self.assertIn("(*vector_int__index(&v, 0)) = 2;", out)

    def test_subscript_result_can_be_a_receiver(self):
        out = cpprust.translate("""
int f(void) { std::vector<std::string*> v; return v[0]->size(); }
""")
        self.assertIn("string_size((*vector_string__index(&v, 0)))", out)


class TestCppOwningElements(unittest.TestCase):
    def test_owning_element_type_is_refused_with_the_alternative(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("int f(void) { std::vector<std::string> v; return v.size(); }")
        self.assertIn("stores its elements by assignment", cm.exception.message)
        self.assertIn("vector<string *>", cm.exception.message)

    def test_vector_of_pointers_works(self):
        out = cpprust.translate("""
void f(void) {
    std::vector<std::string*> v;
    std::string *a = new std::string("x");
    v.push_back(a);
    delete a;
}
""")
        self.assertIn("vector_string_push_back(&v, a)", out)
        self.assertIn("string_drop(a); free(a);", out)


_CALC = """
class Calc {
public:
    int acc;
    Calc() { acc = 0; }
    int add() { return acc; }
    int add(int a) { acc = acc + a; return acc; }
    int add(int a, int b) { acc = acc + a + b; return acc; }
};
"""


class TestCppMethodOverload(unittest.TestCase):
    """Methods overload by argument count, like constructors."""

    def test_each_arity_gets_its_own_symbol(self):
        out = cpprust.translate(_CALC + "void f(void) { Calc c; c.add(1); }")
        self.assertIn("static int Calc_add_0(Calc *this)", out)
        self.assertIn("static int Calc_add_1(Calc *this, int a)", out)
        self.assertIn("static int Calc_add_2(Calc *this, int a, int b)", out)

    def test_call_picks_by_argument_count(self):
        out = cpprust.translate(_CALC + """
void f(void) { Calc c; c.add(); c.add(1); c.add(2, 3); }
""")
        self.assertIn("Calc_add_0(&c)", out)
        self.assertIn("Calc_add_1(&c, 1)", out)
        self.assertIn("Calc_add_2(&c, 2, 3)", out)

    def test_single_method_keeps_the_plain_name(self):
        out = cpprust.translate("""
class C { public: int v; C() { v = 0; } int get() { return v; } };
void f(void) { C c; c.get(); }
""")
        self.assertIn("static int C_get(C *this)", out)

    def test_same_arity_overloads_are_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
class C { public: int v; C() { v = 0; }
          int f(int a) { return a; } int f(char b) { return b; } };
""")
        self.assertIn("take 1 argument", cm.exception.message)

    def test_virtual_overload_is_error(self):
        # A virtual method occupies one vtable slot.
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
class C { public: int v; C() { v = 0; }
          virtual int f() { return 1; } int f(int a) { return a; } };
""")
        self.assertIn("one vtable slot", cm.exception.message)

    def test_wrong_arity_on_an_overload_set_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(_CALC + "void f(void) { Calc c; c.add(1,2,3); }")
        self.assertIn("no overload taking 3", cm.exception.message)


class TestCppOwnVector(unittest.TestCase):
    """`ownvector<T>` copy-constructs and destroys its elements.

    It is separate from `vector<T>` because the two need different parameter
    conventions: a scalar element wants `push_back(T v)` and an owning one
    must not cross a call boundary by value at all.
    """

    def test_elements_are_copy_constructed(self):
        out = cpprust.translate("""
void f(void) { std::ownvector<std::string> v; std::string a("x");
               v.push_back(a); }
""")
        self.assertIn("string_copy(&", out)

    def test_elements_are_destroyed(self):
        out = cpprust.translate("""
void f(void) { std::ownvector<std::string> v; }
""")
        self.assertIn("string_drop(&", out)

    def test_builtins_dispatch_on_the_element_class(self):
        out = cpprust.translate("""
class E { public: int *p; E() { p = 0; } E(const E &o) { p = o.p; }
          ~E() { p = 0; } };
void f(void) { std::ownvector<E> v; E e; v.push_back(e); }
""")
        self.assertIn("E_copy(&", out)
        self.assertIn("E_drop(&", out)

    def test_element_without_a_copy_constructor_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
class E { public: int *p; E() { p = 0; } ~E() { p = 0; } };
void f(void) { std::ownvector<E> v; E e; v.push_back(e); }
""")
        self.assertIn("no copy constructor", cm.exception.message)

    def test_scalar_element_is_refused(self):
        # `ownvector` copy-constructs and destroys each element, and a
        # scalar has neither. Stated at the instantiation now rather than
        # falling out of `__cpp_copy` refusing scalars -- the builtins have
        # to accept them, since `map<int, ..>` copies a scalar key.
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
void f(void) { std::ownvector<int> v; int k = 1; v.push_back(k); }
""")
        self.assertIn("Use `vector<int>`", cm.exception.message)

    def test_vector_diagnostic_names_ownvector(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(
                "int f(void) { std::vector<std::string> v; return v.size(); }")
        self.assertIn("ownvector<string>", cm.exception.message)

    def test_subscript_result_can_receive_a_call(self):
        # `v[i]` is a dereference, so it is addressable -- unlike a call
        # result, which is why this is allowed where `f().g()` is not.
        out = cpprust.translate("""
int f(void) { std::ownvector<std::string> v; return v[0].size(); }
""")
        self.assertIn("string_size(&(*ownvector_string__index(&v, 0)))", out)


class TestCppEmittedStorage(unittest.TestCase):
    def test_supplied_containers_are_static_inline(self):
        # An unused `static` function is a warning, and a program uses only
        # a few of a container's methods. `static inline` is not.
        out = cpprust.translate("void f(void) { std::string s; }")
        self.assertIn("static inline void string_new(string *this)", out)

    def test_user_classes_stay_plain_static(self):
        # User code should still hear about functions it never calls.
        out = cpprust.translate(
            "class C { public: int v; C() { v = 0; } };"
            "void f(void) { C c; }")
        self.assertIn("static void C_new(C *this)", out)
        self.assertNotIn("static inline void C_new", out)

    def test_allocators_only_for_the_arities_used(self):
        out = cpprust.translate(_POINT + """
void f(void) { Point *p = new Point(1, 2); }
""")
        self.assertIn("Point__alloc_2", out)
        self.assertNotIn("Point__alloc(", out)


class TestCppLambda(unittest.TestCase):
    """A lambda with no captures is exactly a function, so it becomes one."""

    def test_lambda_becomes_a_static_function(self):
        out = cpprust.translate("""
void f(void) { auto g = [](int y) -> int { return y * 2; }; }
""")
        self.assertIn("static int _cpp_lambda0(int y) { return y * 2; }", out)

    def test_auto_binding_becomes_a_function_pointer(self):
        out = cpprust.translate("""
void f(void) { auto g = [](int y) -> int { return y * 2; }; }
""")
        self.assertIn("int (*g)(int) = _cpp_lambda0;", out)

    def test_inline_lambda_argument(self):
        out = cpprust.translate("""
int apply(int (*fn)(int), int v);
int f(void) { return apply([](int z) -> int { return z + 1; }, 7); }
""")
        self.assertIn("apply(_cpp_lambda0, 7)", out)

    def test_void_lambda_needs_no_return_type_spelled(self):
        out = cpprust.translate("""
int puts(const char *s);
void f(void) { auto g = []() -> void { puts("hi"); }; }
""")
        self.assertIn("void (*g)(void) = _cpp_lambda0;", out)

    def test_definition_precedes_the_use(self):
        out = cpprust.translate("""
void f(void) { auto g = [](int y) -> int { return y; }; }
""")
        self.assertLess(out.index("static int _cpp_lambda0"),
                        out.index("(*g)(int)"))

    def test_capture_by_value_snapshots_at_the_definition(self):
        """`[x]` copies where the lambda is written, `[&x]` does not.

        The copy is a real local declared at that point, so a later change
        to `x` is not visible through it -- which is the whole difference
        between the two capture forms.
        """
        out = cpprust.translate("""
int f(void) { int x = 10; auto g = [x](int k) -> int { return x + k; };
              x = 99; return g(1); }
""")
        self.assertIn("int _cpp_cap_g_x = x;", out)
        self.assertIn("_cpp_cap_g_x + k", out)

    def test_capture_by_reference_sees_later_changes(self):
        out = cpprust.translate("""
int f(void) { int y = 1; auto g = [&y](int k) -> int { return y + k; };
              y = 99; return g(1); }
""")
        self.assertIn("y + k", out)
        self.assertNotIn("_cpp_cap_g_y", out)

    def test_mixed_captures(self):
        out = cpprust.translate("""
int f(void) { int a = 1; int b = 2;
              auto g = [a, &b](int k) -> int { return a + b + k; };
              return g(3); }
""")
        self.assertIn("int _cpp_cap_g_a = a;", out)
        self.assertIn("_cpp_cap_g_a + b + k", out)

    def test_capture_all_by_value_is_error(self):
        # `[=]` names nothing, and a by-value capture has to be declared.
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
void f(void) { int x = 1; auto g = [=](int y) -> int { return x + y; };
               int r = g(1); }
""")
        self.assertIn("`[=]`", cm.exception.message)

    def test_unfindable_capture_type_is_error(self):
        # The declaration is a function parameter of a different function,
        # so no unambiguous type is in scope to declare the copy with.
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
void f(void) { auto g = [nosuch](int y) -> int { return y; }; int r = g(1); }
""")
        self.assertIn("captured by value", cm.exception.message)

    def test_capture_by_reference_inlines_at_the_call(self):
        """No closure struct: the body goes where the call is.

        A capture would need the captured variable's type to become a field,
        and that type is an ordinary local this pass cannot see. Inlining
        makes the question go away -- the variables are simply in scope.
        """
        out = cpprust.translate("""
int f(void) { int t = 0; auto g = [&](int y) -> int { return t + y; }; return g(2); }
""")
        self.assertIn("do { int y = 2;", out)
        self.assertIn("t + y", out)
        self.assertNotIn("auto", out)

    def test_return_becomes_break_not_goto(self):
        # A lambda `return` leaves the lambda, not the function. `break` out
        # of `do { } while (0)` is a structured jump the destructor
        # unwinding already understands; `goto` is refused whenever anything
        # is live, which is most RAII code.
        out = cpprust.translate("""
int f(void) { int t = 1; auto g = [&](int y) -> int { return t + y; }; return g(2); }
""")
        self.assertIn("break; }", out)
        self.assertIn("} while (0);", out)
        self.assertNotIn("goto", out)

    def test_each_call_site_gets_its_own_expansion(self):
        out = cpprust.translate("""
int f(void) { int t = 0; auto g = [&](int y) -> int { return t + y; };
              return g(1) + g(2); }
""")
        self.assertIn("_cpp_lam0_r", out)
        self.assertIn("_cpp_lam1_r", out)

    def test_void_capturing_lambda(self):
        out = cpprust.translate("""
int puts(const char *s);
void f(void) { auto g = [&]() -> void { puts("hi"); }; g(); }
""")
        self.assertIn('puts("hi")', out)
        self.assertIn("(void)0", out)

    def test_capture_all_by_reference_is_supported(self):
        out = cpprust.translate("""
int f(void) { int a = 1; int b = 2; auto g = [&]() -> int { return a + b; };
              return g(); }
""")
        self.assertIn("a + b", out)

    def test_recursive_lambda_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
void f(void) { int x = 1; auto g = [&](int y) -> int { return g(y); }; int r = g(1); }
""")
        self.assertIn("cannot recurse", cm.exception.message)

    def test_call_in_a_loop_condition_is_error(self):
        # Hoisting the body before the statement would evaluate it once.
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
void f(void) { int x = 1; auto g = [&](int y) -> int { return x + y; };
               while (g(1) < 5) { x = x + 1; } }
""")
        self.assertIn("controlling expression", cm.exception.message)

    def test_call_in_a_short_circuit_operand_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
void f(void) { int x = 1; auto g = [&](int y) -> int { return x + y; };
               int r = (x > 0 && g(1) > 2); }
""")
        self.assertIn("operand of", cm.exception.message)

    def test_capturing_lambda_used_as_a_value_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
int h(int (*f)(int));
void f(void) { int x = 1; auto g = [&](int y) -> int { return x + y; };
               int r = h(g); }
""")
        self.assertIn("used as a value", cm.exception.message)

    def test_return_inside_a_loop_in_the_body_is_error(self):
        # `return` becomes `break`, which would leave only the inner loop.
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
void f(void) { int x = 1;
               auto g = [&](int y) -> int { while (y > 0) { return x; } return 0; };
               int r = g(1); }
""")
        self.assertIn("becomes `break`", cm.exception.message)

    def test_capturing_lambda_composes_with_destructors(self):
        out = cpprust.translate("""
class G { public: int v; G() { v = 7; } ~G() { v = 0; } int get() { return v; } };
int f(void) { G g; int b = 1; auto k = [&](int n) -> int { return b + n + g.get(); };
              return k(2); }
""")
        self.assertIn("G_get(&g)", out)
        self.assertIn("G_drop(&g);", out)

    def test_array_subscript_is_not_a_lambda(self):
        out = cpprust.translate("""
int f(int *a) { int b[4]; b[0] = 1; return a[0] + b[0]; }
""")
        self.assertIn("return a[0] + b[0];", out)


class TestCppCopy(unittest.TestCase):
    """Copying an owning object has to call a copy constructor, or be refused.

    A struct copy duplicates the representation and leaves two objects
    owning one resource, so both destructors run on it. That was silent:
    `T b = a;` was neither constructed nor dropped, `T b(a);` called the
    default constructor with an extra argument, and `b = a;` double-dropped.
    """

    def test_copy_constructor_gets_its_own_symbol(self):
        out = cpprust.translate(_COPYABLE)
        self.assertIn("static void Buf_copy(Buf *this, const Buf *o)", out)
        self.assertIn("static void Buf_new(Buf *this)", out)

    def test_copy_initialization_calls_it(self):
        out = cpprust.translate(_COPYABLE + "void f(void) { Buf a; Buf b = a; }")
        self.assertIn("Buf b; Buf_copy(&b, &a);", out)

    def test_copy_construction_calls_it(self):
        out = cpprust.translate(_COPYABLE + "void f(void) { Buf a; Buf c(a); }")
        self.assertIn("Buf c; Buf_copy(&c, &a);", out)
        self.assertNotIn("Buf_new(&c, a)", out)

    def test_the_copy_is_dropped_too(self):
        out = cpprust.translate(_COPYABLE + "void f(void) { Buf a; Buf b = a; }")
        # Reverse declaration order, and the copy is dropped at all -- it
        # previously fell out of the scope's live list entirely.
        self.assertIn("Buf_drop(&b); Buf_drop(&a);", out)

    def test_destructor_without_copy_constructor_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(_OWNING + "void f(void) { Own a; Own b = a; }")
        self.assertIn("no copy constructor", cm.exception.message)

    def test_assignment_to_an_owning_object_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(_COPYABLE + "void f(void) { Buf a; Buf b; b = a; }")
        self.assertIn("two objects owning one resource",
                      cm.exception.message)

    def test_plain_data_still_copies_bitwise(self):
        # No destructor: nothing owns anything, so the implicit copy is
        # exactly what C++ would do.
        out = cpprust.translate("""
class Pod { public: int x; Pod() { x = 0; } };
void f(void) { Pod a; Pod b = a; }
""")
        self.assertIn("b = a;", out)

    def test_scalar_assignment_is_untouched(self):
        out = cpprust.translate(_COPYABLE + """
void f(void) { int k; k = 3; Buf a; a.p = 0; }
""")
        self.assertIn("k = 3;", out)
        self.assertIn("a.p = 0;", out)

    def test_same_arity_overloads_are_error(self):
        # Overloads are resolved by argument count, so two constructors of
        # the same arity have nothing left to choose between them.
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(
                "class P { public: int x; P(int v) { x=v; } "
                "P(char c) { x=c; } };")
        self.assertIn("same arity", cm.exception.message.replace(
            "two constructors take 1 argument", "same arity"))

    def test_copy_constructor_is_not_the_overload_that_is_rejected(self):
        out = cpprust.translate(_COPYABLE)
        self.assertIn("Buf_copy", out)

    def test_copy_from_an_unnameable_expression_is_error(self):
        # A copy constructor but no destructor, so returning by value is
        # allowed -- what fails is that the source of the copy is not an
        # object this pass can name.
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
class Pod { public: int x; Pod() { x = 0; } Pod(const Pod &o) { x = o.x; } };
Pod make(void);
void f(void) { Pod b = make(); }
""")
        self.assertIn("not an object of that type", cm.exception.message)

    def test_copy_constructor_installs_the_vptr(self):
        # A copied object still has to dispatch.
        out = cpprust.translate("""
class B {
public:
    int v;
    B() { v = 0; }
    B(const B &o) { v = o.v; }
    virtual ~B() { v = 1; }
    virtual int get() { return v; }
};
""")
        self.assertIn("static void B_copy(B *this, const B *o) "
                      "{((B *)this)->_vptr", out)


class TestCppChainedReceivers(unittest.TestCase):
    """The result of a call can be the receiver of the next one.

    Each step is emitted into an expression that becomes the next step's
    receiver, so no temporary is needed -- which matters because this is
    expression position and C has no statement expression.
    """

    def test_two_link_chain(self):
        out = cpprust.translate(_CHAIN + """
int f(void) { Owner o; return o.node()->get(); }
""")
        self.assertIn("return Node_get(Owner_node(&o));", out)

    def test_three_link_chain(self):
        out = cpprust.translate(_CHAIN + """
int f(void) { Owner o; return o.node()->self()->get(); }
""")
        self.assertIn("return Node_get(Node_self(Owner_node(&o)));", out)

    def test_chain_inside_an_argument(self):
        out = cpprust.translate(_CHAIN + """
int take(int k);
int f(void) { Owner o; return take(o.node()->get()); }
""")
        self.assertIn("take(Node_get(Owner_node(&o)))", out)

    def test_free_function_chain_is_untouched(self):
        """The case the subset always had to leave alone.

        A chain only ever starts from a symbol that resolves to a class, so
        plain C -- a free function returning a struct pointer -- still comes
        through exactly as written.
        """
        out = cpprust.translate(_CHAIN + """
struct Ops { int (*init)(int); };
struct Ops *get_ops(void);
int g(int x) { return get_ops()->init(x); }
""")
        self.assertIn("return get_ops()->init(x);", out)

    def test_unknown_method_ends_the_chain(self):
        out = cpprust.translate(_CHAIN + """
int f(void) { Owner o; return o.node()->nosuch(); }
""")
        self.assertIn("Owner_node(&o)->nosuch()", out)

    def test_value_return_cannot_be_a_receiver(self):
        # C cannot take the address of a function result, and spilling would
        # need a statement.
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
class Inner { public: int n; Inner() { n = 0; } int get() { return n; } };
class Outer { public: Inner in; Outer() { } Inner val() { return in; } };
int f(void) { Outer o; return o.val().get(); }
""")
        self.assertIn("returned by value", cm.exception.message)


_VCHAIN = """
class Shape {
public:
    int id;
    Shape(int i) { id = i; }
    virtual int area() { return 1; }
    virtual Shape *twin() { return this; }
};
class Square : public Shape {
public:
    int side;
    Square(int i, int s) : Shape(i) { side = s; }
    int area() { return side * side; }
};
class Factory {
public:
    int n;
    Factory() { n = 0; }
    Shape *make() { n = n + 1; return 0; }
};
"""


class TestCppVirtualChainEvaluation(unittest.TestCase):
    """A virtual call must not evaluate its receiver twice.

    The plain dispatch form names the receiver once to reach the vptr and
    once as the argument. That is fine for a name, but a call receiver would
    run twice -- `f.make()->area()` would build two objects.
    """

    def test_call_receiver_dispatches_through_a_helper(self):
        out = cpprust.translate(_VCHAIN + """
int f(void) { Factory k; return k.make()->area(); }
""")
        self.assertIn("Shape__vcall_area(Factory_make(&k))", out)
        # The factory is named exactly once.
        self.assertEqual(out.count("Factory_make(&k)"), 1)

    def test_helper_evaluates_this_once(self):
        out = cpprust.translate(_VCHAIN + """
int f(void) { Factory k; return k.make()->area(); }
""")
        self.assertIn("static int Shape__vcall_area(Shape *this) { return "
                      "((const struct Shape_vtable *)this->_vptr)"
                      "->area(this); }", out)

    def test_named_receiver_keeps_the_plain_form(self):
        out = cpprust.translate(_VCHAIN + "int f(Shape *s) { return s->area(); }")
        self.assertIn("((const struct Shape_vtable *)(s)->_vptr)->area(s)", out)

    def test_no_helper_when_nothing_chains(self):
        out = cpprust.translate(_VCHAIN + "int f(Shape *s) { return s->area(); }")
        self.assertNotIn("__vcall", out)

    def test_helper_is_emitted_by_the_declaring_class_only(self):
        out = cpprust.translate(_VCHAIN + """
int f(void) { Factory k; return k.make()->area(); }
""")
        self.assertNotIn("Square__vcall_area", out)

    def test_return_this_upcasts_to_the_declared_base(self):
        out = cpprust.translate("""
class Base { public: int id; Base() { id = 0; } virtual Base *me() { return this; } };
class Derived : public Base { public: int w; Derived() { w = 0; } Base *me() { return this; } };
""")
        self.assertIn("static Base * Derived_me(Derived *this) "
                      "{ return (Base *)this; }", out)


class TestCppVirtualDtor(unittest.TestCase):
    """A destructor is a vtable slot like any other virtual."""

    def test_destructor_gets_a_slot(self):
        out = cpprust.translate(_HIER + "void f(void) { Base b; }")
        self.assertIn("void (*__dtor)(Base *this);", out)

    def test_derived_table_keeps_the_slot_layout(self):
        out = cpprust.translate(_HIER + "void f(void) { Derived d; }")
        # Same slots, same order, same `this` type as the base's table --
        # that is what lets a `Base *` dispatch through a derived table.
        self.assertIn("struct Base_vtable { int (*area)(Base *this); "
                      "void (*__dtor)(Base *this); };", out)
        self.assertIn("struct Derived_vtable { int (*area)(Base *this); "
                      "void (*__dtor)(Base *this); };", out)

    def test_override_goes_through_a_thunk_that_converts_this(self):
        out = cpprust.translate(_HIER + "void f(void) { Derived d; }")
        self.assertIn("static void Derived__thunk___dtor(Base *this) "
                      "{ Derived_drop((Derived *)this); }", out)

    def test_slot_implementation_is_drop_not_the_slot_name(self):
        out = cpprust.translate(_HIER + "void f(void) { Base b; }")
        self.assertIn("&Base_drop", out)
        self.assertNotIn("Base___dtor", out)

    def test_derived_without_the_keyword_still_overrides(self):
        # `~Derived()` is not marked `virtual`; it overrides anyway.
        out = cpprust.translate(_HIER + "void f(void) { Derived d; }")
        self.assertIn("Derived__thunk___dtor", out)

    def test_implicit_derived_destructor_fills_the_slot(self):
        out = cpprust.translate("""
class B { public: int v; B() { v = 0; } virtual ~B() { v = 1; } };
class D : public B { public: int w; D() { w = 0; } };
void f(void) { D d; }
""")
        # D declares no destructor, but its epilogue has to chain to B's, so
        # it has one -- and the slot points at it.
        self.assertIn("static void D_drop(D *this)", out)
        self.assertIn("D__thunk___dtor", out)

    def test_destructor_slot_is_not_callable_as_a_method(self):
        out = cpprust.translate(_HIER + "void f(Base *b) { delete b; }")
        self.assertNotIn("Base___dtor(", out)

    def test_by_value_local_still_drops_statically(self):
        # The static type is known, so no dispatch is needed.
        out = cpprust.translate(_HIER + "void f(void) { Derived d; }")
        self.assertIn("Derived_drop(&d);", out)


class TestCppInheritedFields(unittest.TestCase):
    """A base field lives inside `_base`, not at the derived class's top.

    Both of these emitted C that did not compile: an unqualified name in a
    derived method, and a direct `.id` on a derived object.
    """

    def test_inherited_field_in_a_derived_method(self):
        out = cpprust.translate("""
class Base { public: int id; Base() { id = 1; } };
class Derived : public Base {
public:
    int w;
    Derived() { w = 2; }
    int show() { return id + w; }
};
""")
        self.assertIn("return this->_base.id + this->w;", out)

    def test_inherited_field_through_a_pointer(self):
        out = cpprust.translate("""
class Base { public: int id; Base() { id = 1; } };
class Derived : public Base { public: int w; Derived() { w = 2; } };
int g(Derived *d) { return d->id; }
""")
        self.assertIn("return d->_base.id;", out)

    def test_field_two_levels_up(self):
        out = cpprust.translate("""
class A { public: int a; A() { a = 1; } };
class B : public A { public: int b; B() { b = 2; } };
class C : public B { public: int c; C() { c = 3; } int all() { return a + b + c; } };
""")
        self.assertIn("this->_base._base.a", out)
        self.assertIn("this->_base.b", out)

    def test_own_field_shadows_an_inherited_one(self):
        out = cpprust.translate("""
class Base { public: int v; Base() { v = 1; } };
class Derived : public Base {
public:
    int v;
    Derived() { v = 2; }
    int get() { return v; }
};
""")
        self.assertIn("return this->v;", out)
        self.assertNotIn("return this->_base.v;", out)


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

    def test_delete_dispatches_through_a_virtual_destructor(self):
        """`delete base_ptr` has to reach the derived destructor.

        The destructor occupies a vtable slot like any other virtual, so a
        base pointer dispatches to the most derived one. Without this, the
        base destructor ran and the derived part leaked -- the exact bug
        `virtual ~T()` is written to prevent.
        """
        out = cpprust.translate(_HIER + "void f(Base *b) { delete b; }")
        self.assertIn("->_vptr)->__dtor(b);", out)
        self.assertIn("free(b);", out)

    def test_new_derived_upcasts_to_a_base_pointer(self):
        # C does not convert `Derived *` to `Base *` on its own; the base is
        # the first member, so the cast is address-preserving.
        out = cpprust.translate(_HIER + "void f(void) { Base *b = new Derived(); }")
        self.assertIn("Base *b = (Base *)Derived__alloc();", out)

    def test_new_of_the_same_class_is_not_cast(self):
        out = cpprust.translate(_HIER + "void f(void) { Base *b = new Base(); }")
        self.assertIn("Base *b = Base__alloc();", out)

    def test_upcast_on_a_later_assignment(self):
        out = cpprust.translate(_HIER + "void f(void) { Base *b; b = new Derived(); }")
        self.assertIn("b = (Base *)Derived__alloc();", out)

    def test_unrelated_class_is_not_cast(self):
        # Only a proven ancestor is cast to; anything else stays a real
        # mismatch for the C compiler to report.
        out = cpprust.translate(_HIER + """
class Other { public: int z; Other() { z = 0; } };
void f(void) { Other *o = new Derived(); }
""")
        self.assertIn("Other *o = Derived__alloc();", out)

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


class TestCppAuto(unittest.TestCase):
    """C++11 `auto`, resolved to a written type before anything reads types.

    A textual deduction, not a type checker: everything downstream reads
    types by their spelling, so `auto` has to become one. What has a spelling
    nearby resolves; what does not is reported rather than guessed.
    """

    CLASSES = ("class A { public: int v; A() { v = 7; } int g() "
               "{ return v; } };\nint mk(void);\n")

    def _decl(self, body):
        out = cpprust.translate(self.CLASSES + "int f(void) {%s}" % body,
                                path="t.cpp")
        return out

    def test_class_construction_becomes_direct_initialisation(self):
        # `auto a = A();` is written as copy-initialisation but means direct
        # initialisation -- C++17 elides the temporary, and the direct form
        # is the only one this subset lowers.
        out = self._decl(" auto a = A(); return a.g(); ")
        self.assertIn("A_new(&a);", out)

    def test_literals(self):
        for init, want in ((" 3 ", "int n"), (" 1.5 ", "double n"),
                           (' "hi" ', "const char * n"), (" 'x' ", "char n"),
                           (" true ", "bool n")):
            out = self._decl(" auto n =%s; return 0; " % init)
            self.assertIn(want, out)

    def test_new_deduces_a_pointer(self):
        out = self._decl(" auto p = new A(); return p->v; ")
        self.assertIn("A * p", out)

    def test_function_and_method_return_types(self):
        self.assertIn("int r", self._decl(" auto r = mk(); return r; "))
        self.assertIn("int z", self._decl(" A a; auto z = a.g(); return z; "))

    def test_subscript_deduces_through_the_template_parameter(self):
        out = cpprust.translate("""
template<typename T> class vec {
    T *d; int n;
public:
    vec() { d = 0; n = 0; }
    int size() { return n; }
    T &operator[](int i) { return d[i]; }
};
int f(void) { vec<int> v; auto e = v[0]; return e; }
""", path="t.cpp")
        self.assertIn("int e = ", out)

    def test_undeducible_forms_are_reported(self):
        for body, why in (
                (" auto x = 1 + 2 * 3; return x; ", "compound"),
                (" auto x = mystery(); return 0; ", "mystery"),
                (" auto x = nosuch; return 0; ", "nosuch")):
            with self.assertRaises(cpprust.CppError) as cm:
                self._decl(body)
            self.assertIn("auto", cm.exception.message)


class TestCppRangeFor(unittest.TestCase):
    """C++11 range-`for`, rewritten to the index loop it stands for."""

    VEC = """
template<typename T> class vec {
    T *d; int n; int cap;
public:
    vec() { d = 0; n = 0; cap = 0; }
    ~vec() { free(d); }
    int size() { return n; }
    T &operator[](int i) { return d[i]; }
};
"""

    def test_reference_form_aliases_the_element(self):
        # A reference means the name *is* the element, so writing through it
        # writes to the container. Done by substitution, which is exact.
        out = cpprust.translate(
            "int f(void) { int a[3]; for (auto &x : a) { x = 1; } return 0; }",
            path="t.cpp")
        self.assertIn("a[_cpp_it0] = 1;", out)

    def test_value_form_declares_a_copy(self):
        out = cpprust.translate(
            "int f(void) { int a[3]; int s = 0; "
            "for (auto x : a) { s = s + x; } return s; }", path="t.cpp")
        self.assertIn("int x = a[_cpp_it0];", out)

    def test_container_uses_size_and_subscript(self):
        out = cpprust.translate(
            self.VEC + "int f(void) { vec<int> v; int s = 0; "
            "for (auto x : v) { s = s + x; } return s; }", path="t.cpp")
        self.assertIn("vec_int_size(&v)", out)

    def test_two_loops_get_distinct_counters(self):
        # The blanked copy has to be rebuilt after the first rewrite; reading
        # a stale one substituted nothing in the second loop.
        out = cpprust.translate(
            "int f(void) { int a[3]; for (auto &x : a) { x = 1; } "
            "for (auto &y : a) { y = 2; } return 0; }", path="t.cpp")
        self.assertIn("a[_cpp_it0] = 1;", out)
        self.assertIn("a[_cpp_it1] = 2;", out)

    def test_an_unwalkable_range_is_reported(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(
                "class C { public: int v; };\n"
                "int f(void) { C c; for (auto x : c) { } return 0; }",
                path="t.cpp")
        self.assertIn("size()", cm.exception.message)


class TestCppNamespaces(unittest.TestCase):
    """`namespace N { .. }` and `N::x` flattened to `N_x`.

    The same thing Crust does with Rust paths, and for the same reason: C has
    one namespace, so a qualified name has to become an unqualified one.
    """

    def test_qualified_names_are_flattened(self):
        out = cpprust.translate("""
namespace geo {
    class Point { public: int x; int y; int sum() { return x + y; } };
    int twice(int v) { return v * 2; }
}
int f(void) { geo::Point p; p.x = 3; p.y = 4; return geo::twice(p.sum()); }
""", path="t.cpp")
        self.assertIn("geo_Point", out)
        self.assertIn("geo_twice", out)

    def test_members_are_not_prefixed(self):
        # Only what the namespace declares. Prefixing members renamed
        # `Point::x` to `geo_x` and broke every use of it.
        out = cpprust.translate("""
namespace geo { class Point { public: int x; int sum() { return x; } }; }
int f(void) { geo::Point p; p.x = 1; return p.sum(); }
""", path="t.cpp")
        self.assertNotIn("geo_x", out)
        self.assertIn("p.x", out)

    def test_nested_namespaces_join(self):
        out = cpprust.translate("""
namespace a { namespace b { int mk(int n) { return n; } } }
int f(void) { return a::b::mk(1); }
""", path="t.cpp")
        self.assertIn("a_b_mk", out)


class TestCppSmartPointers(unittest.TestCase):
    """`unique_ptr` and `shared_ptr`, written in the subset and supplied.

    Like `string` and `vector`: every feature they need is one the subset
    already claims, so if they compile the claim holds. `unique_ptr` declares
    no copy constructor, which means the existing Rule of Three refusal *is*
    its move-only semantics, for free.
    """

    THING = ("class Thing { public: int v; Thing() { v = 0; } "
             "~Thing() { } };\n")

    def test_unique_ptr_is_supplied_on_demand(self):
        out = cpprust.translate(
            "#include <memory>\n" + self.THING +
            "int f(void) { std::unique_ptr<Thing> u(new Thing()); "
            "return u.get()->v; }", path="t.cpp")
        self.assertIn("unique_ptr_Thing_reset", out)

    def test_unique_ptr_runs_the_element_destructor(self):
        # Plain `delete p` inside a template frees without calling the
        # element's destructor, because `T` is not known to be a class when
        # the body is parsed. `__cpp_drop` is resolved per instantiation.
        out = cpprust.translate(
            "#include <memory>\n" + self.THING +
            "int f(void) { std::unique_ptr<Thing> u(new Thing()); return 0; }",
            path="t.cpp")
        self.assertIn("Thing_drop", out)

    def test_copying_a_unique_ptr_is_refused(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(
                "#include <memory>\n" + self.THING +
                "int f(void) { std::unique_ptr<Thing> a(new Thing()); "
                "std::unique_ptr<Thing> b = a; return 0; }", path="t.cpp")
        self.assertIn("copy constructor", cm.exception.message)

    def test_shared_ptr_has_a_copy_constructor_and_a_count(self):
        out = cpprust.translate(
            "#include <memory>\n" + self.THING +
            "int f(void) { std::shared_ptr<Thing> a(new Thing()); "
            "std::shared_ptr<Thing> b(a); return (int)b.use_count(); }",
            path="t.cpp")
        self.assertIn("shared_ptr_Thing_copy", out)
        self.assertIn("shared_ptr_Thing_use_count", out)

    def test_the_header_alone_supplies_nothing(self):
        # An unused template would still be monomorphised.
        out = cpprust.translate(
            "#include <memory>\nclass C { public: int v; };\n"
            "int f(void) { C c; return c.v; }", path="t.cpp")
        self.assertNotIn("unique_ptr", out)


class TestCppDeclarationHoisting(unittest.TestCase):
    """Class names and prototypes precede every definition.

    A template instantiated over a class defined *below* it emitted
    `struct Box_Thing { Thing * bp; };` above `struct Thing;`. General, not
    specific to the supplied templates -- a user template hit it identically.
    """

    SRC = """
template<typename T> class Box { T *bp; public: Box() { bp = 0; } T *get() { return bp; } };
class Thing { public: int v; Thing() { v = 1; } };
int f(void) { Box<Thing> b; return b.get() ? 1 : 0; }
"""

    def test_a_template_over_a_later_class_orders_correctly(self):
        out = cpprust.translate(self.SRC, path="t.cpp")
        self.assertLess(out.index("struct Thing;"),
                        out.index("struct Box_Thing {"))

    def test_struct_definitions_stay_where_they_were(self):
        # Only names and prototypes hoist. A by-value member needs its
        # member's *definition* above it, so moving one would move them all.
        out = cpprust.translate(self.SRC, path="t.cpp")
        self.assertLess(out.index("struct Box_Thing {"),
                        out.index("struct Thing { int v; };"))

    def test_a_local_with_a_new_argument_is_not_a_declaration(self):
        # `Holder h(new Thing())` read as a function declaration returning
        # `Holder`, so the by-value check refused a perfectly good local.
        out = cpprust.translate(
            "class Thing { public: int v; Thing() { v = 1; } };\n"
            "class H { Thing *p; public: H(Thing *q) { p = q; } "
            "~H() { } Thing *get() { return p; } };\n"
            "int f(void) { H h(new Thing()); return h.get()->v; }",
            path="t.cpp")
        self.assertIn("H_new(&h,", out)


class TestCppPointerOperators(unittest.TestCase):
    """`operator->` and `operator*` -- the two a smart pointer needs."""

    PTR = """
class Ptr {
    int *p;
public:
    Ptr() { p = 0; }
    Ptr(int *q) { p = q; }
    int *operator->() { return p; }
    int &operator*() { return *p; }
};
"""

    def test_star_is_an_lvalue(self):
        # Lowered like `operator[]`: the function yields the address and the
        # dereference is written back, so `*p = x` assigns through.
        out = cpprust.translate(
            self.PTR + "int f(int *q) { Ptr a(q); *a = 5; return *a; }",
            path="t.cpp")
        self.assertIn("(*Ptr__star(&a)) = 5;", out)

    def test_arrow_hands_back_a_plain_pointer(self):
        out = cpprust.translate("""
class Node { public: int v; Node() { v = 0; } };
class Handle {
    Node *n;
public:
    Handle() { n = 0; }
    Node *operator->() { return n; }
};
int f(void) { Handle h; return h->v; }
""", path="t.cpp")
        self.assertIn("Handle__arrow(&h)->v", out)

    def test_this_is_not_rewritten(self):
        # `this->` has the same shape. Rewriting pointers turned every field
        # access inside the class into a call to its own `operator->`.
        out = cpprust.translate(
            self.PTR + "int f(int *q) { Ptr a(q); return *a; }", path="t.cpp")
        self.assertNotIn("Ptr__arrow(this)", out)

    def test_a_genuine_pointer_is_ordinary_member_access(self):
        # `Ptr *p; p->x` means a member of `Ptr` in C++, not the operator.
        out = cpprust.translate("""
class Ptr { public: int v; int *operator->() { return &v; } };
int f(Ptr *p) { return p->v; }
""", path="t.cpp")
        self.assertIn("p->v", out)

    def test_smart_pointers_expose_both(self):
        out = cpprust.translate(
            "#include <memory>\n"
            "class T { public: int v; T() { v = 0; } ~T() { } };\n"
            "int f(void) { std::unique_ptr<T> u(new T()); u->v = 1; "
            "return (*u).v; }", path="t.cpp")
        self.assertIn("unique_ptr_T__arrow", out)

    def test_other_overloads_are_still_reported(self):
        # `+=` and the comparisons are supported now; the stream operators
        # are not.
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(
                "class A { public: int v; A operator<<(int n) "
                "{ return *this; } };", path="t.cpp")
        self.assertIn("operator<<", cm.exception.message)


class TestCppNamespaceHazards(unittest.TestCase):
    """Flattening is name-mangling, not lookup, so the ways it could quietly
    change meaning are reported rather than resolved."""

    def test_a_flattened_name_colliding_with_a_global_is_reported(self):
        # Both become `geo_twice`. The C front end would report the
        # redefinition, but the *call sites* merge before that.
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
int geo_twice(int n) { return n; }
namespace geo { int twice(int n) { return n * 2; } }
int f(void) { return geo::twice(1); }
""", path="t.cpp")
        self.assertIn("one symbol", cm.exception.message)

    def test_ambiguous_using_namespace_is_reported(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
namespace a { int mk(int n) { return n; } }
namespace b { int mk(int n) { return n * 2; } }
using namespace a;
using namespace b;
int f(void) { return mk(1); }
""", path="t.cpp")
        self.assertIn("ambiguous", cm.exception.message)

    def test_one_using_namespace_still_resolves(self):
        out = cpprust.translate("""
namespace a { int mk(int n) { return n; } }
using namespace a;
int f(void) { return mk(1); }
""", path="t.cpp")
        self.assertIn("a_mk(1)", out)


class TestCppDefaulted(unittest.TestCase):
    """`= default` and `= delete`."""

    def test_defaulted_destructor_becomes_an_empty_body(self):
        # Rewritten rather than dropped, so `virtual` stays attached -- it
        # decides whether the class gets a vtable slot. The member epilogue
        # is appended to whatever body a destructor has, so an empty one
        # gives exactly the destructor the compiler would have written.
        out = cpprust.translate("""
class B {
public:
    int v;
    B() { v = 1; }
    virtual ~B() = default;
    virtual int g() { return v; }
};
int f(void) { B b; return b.g(); }
""", path="t.cpp")
        self.assertIn("B_drop", out)

    def test_defaulted_constructor_is_accepted(self):
        out = cpprust.translate("""
class A { public: int v; A() = default; int g() { return v; } };
int f(void) { A a; return a.g(); }
""", path="t.cpp")
        self.assertIn("A_g", out)

    def test_deleted_member_is_dropped(self):
        # A deleted copy constructor leaves a class with a destructor and no
        # copy constructor, which the Rule of Three already refuses to copy.
        out = cpprust.translate("""
class N {
    int *p;
public:
    N() { p = 0; }
    N(const N &o) = delete;
    ~N() { }
};
int f(void) { N a; return 0; }
""", path="t.cpp")
        self.assertNotIn("N_copy", out)


class TestCppOutOfLineDefinitions(unittest.TestCase):
    """Members declared in a class and defined under a qualified name.

    This is how C++ projects are laid out, and the lowering needs both halves
    in one place because it emits a class and its bodies together. The
    definitions are lifted out of the file, keyed by class, name and arity,
    and attached to the member they belong to before anything is emitted.
    """

    SPLIT = """
class A {
    int v;
public:
    A();
    A(int n);
    ~A();
    int get() const;
    void set(int n);
};

int gone = 0;

A::A() { v = 0; }
A::A(int n) { v = n; }
A::~A() { gone = gone + 1; }
int A::get() const { return v; }
void A::set(int n) { v = n; }
"""

    def test_bodies_are_attached_to_the_declarations(self):
        out = cpprust.translate(
            self.SPLIT + "int f(void) { A a(5); a.set(7); return a.get(); }",
            path="t.cpp")
        self.assertIn("static int A_get(A *this) { return this->v; }", out)

    def test_the_destructor_is_matched_through_its_tilde(self):
        # It is written `~A` where it is defined and recorded as `A` on the
        # member, so the key has to be put back together.
        out = cpprust.translate(self.SPLIT + "int f(void) { A a; return 0; }",
                                path="t.cpp")
        self.assertIn("A_drop(A *this) { gone = gone + 1; }", out)

    def test_overloaded_constructors_match_on_arity(self):
        out = cpprust.translate(self.SPLIT + "int f(void) { A a(5); return 0; }",
                                path="t.cpp")
        self.assertIn("A_new(A *this) { this->v = 0; }", out)
        self.assertIn("this->v = n;", out)

    def test_a_trailing_const_is_dropped(self):
        # It constrains what the body may do; `this` is a pointer either way,
        # and the C front end checks the body regardless.
        out = cpprust.translate(self.SPLIT + "int f(void) { A a; return a.get(); }",
                                path="t.cpp")
        self.assertIn("static int A_get(A *this)", out)

    def test_declared_and_never_defined_is_reported(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("class A { public: int f(); };", path="t.cpp")
        self.assertIn("declared but never defined", cm.exception.message)

    def test_a_qualified_call_inside_a_body_is_not_a_definition(self):
        # Only at brace depth zero: `Foo::bar()` inside a body is a call, and
        # matching it would tear the middle out of a function.
        out = cpprust.translate("""
class A { public: int v; A() { v = 1; } int g() { return v; } };
int f(void) { A a; return A::g(&a) ? 1 : a.g(); }
""", path="t.cpp")
        self.assertIn("A_g", out)

    def test_bodies_are_emitted_after_file_scope_names_they_read(self):
        # The author wrote them below `gone`; emitting at the class would put
        # them above it, and a header spliced in at the top makes that worse.
        out = cpprust.translate(self.SPLIT + "int f(void) { A a; return 0; }",
                                path="t.cpp")
        self.assertLess(out.index("int gone = 0;"),
                        out.index("gone = gone + 1;"))


class TestCppHeaderExpansion(unittest.TestCase):
    """`#include "x.h"` is spliced by this pass, so a class and its
    definitions meet in one translation."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "a.h"), "w") as f:
            f.write("class A { int v; public: A(); int g(); };\n")
        with open(os.path.join(self.dir, "b.h"), "w") as f:
            f.write('#include "a.h"\nint helper(void);\n')

    def test_a_header_supplies_the_declarations(self):
        out = cpprust.translate(
            '#include "a.h"\nA::A() { v = 3; }\nint A::g() { return v; }\n'
            'int f(void) { A a; return a.g(); }\n',
            path="a.cpp", basedir=self.dir)
        self.assertIn("static int A_g(A *this) { return this->v; }", out)

    def test_a_header_is_spliced_once(self):
        # Which is what an include guard does, and saves understanding either
        # `#pragma once` or the `#ifndef` idiom.
        out = cpprust.translate(
            '#include "a.h"\n#include "b.h"\n'
            'A::A() { v = 3; }\nint A::g() { return v; }\n'
            'int f(void) { A a; return a.g(); }\n',
            path="a.cpp", basedir=self.dir)
        self.assertEqual(out.count("struct A { int v; };"), 1)

    def test_a_missing_header_is_left_alone(self):
        # It may be one the C front end can resolve; this pass is not the
        # authority on the include path.
        out = cpprust.translate(
            '#include "nosuch.h"\nint f(void) { return 0; }\n',
            path="a.cpp", basedir=self.dir)
        self.assertIn('#include "nosuch.h"', out)

    def test_angle_bracket_includes_are_untouched(self):
        out = cpprust.translate(
            '#include <stdio.h>\nint f(void) { return 0; }\n',
            path="a.cpp", basedir=self.dir)
        self.assertIn("#include <stdio.h>", out)


class TestCppVirtualOnAValue(unittest.TestCase):
    """A virtual call through a value receiver."""

    def test_the_receiver_is_parenthesised(self):
        # The receiver may already be `&c`, and `&c->_vptr` parses as
        # `&(c->_vptr)` -- the address of the pointer rather than the
        # pointer. Dispatching on a value emitted that and did not compile.
        out = cpprust.translate("""
class C { int n; public: C() { n = 1; } virtual int scaled(int k) { return n * k; } };
int f(void) { C c; return c.scaled(2); }
""", path="t.cpp")
        self.assertIn("(&c)->_vptr", out)


class TestCppTypeAliases(unittest.TestCase):
    """`typedef X Y;` and `using Y = X;` resolved to what they name."""

    VEC = """
template<typename T> class vec {
    T *d; int n;
public:
    vec() { d = 0; n = 0; }
    int size() { return n; }
    T &operator[](int i) { return d[i]; }
};
"""

    def test_a_typedef_container_can_be_walked(self):
        # This is the shape that dominated the benchmark: a member declared
        # with an alias, walked by a range-`for`.
        out = cpprust.translate(self.VEC + """
typedef vec<int> ints;
class Bag { ints items; public: int total(); };
int Bag::total() { int s = 0; for (auto &x : items) { s = s + x; } return s; }
""", path="t.cpp")
        self.assertIn("vec_int_size(&this->items)", out)

    def test_a_using_alias_becomes_a_typedef(self):
        # C has only the typedef.
        out = cpprust.translate(self.VEC + """
using ints = vec<int>;
class Bag { ints items; public: int size(); };
int Bag::size() { return items.size(); }
""", path="t.cpp")
        self.assertNotIn("using ints", out)
        self.assertIn("vec_int_size", out)

    def test_a_nested_template_argument_is_matched(self):
        # `vec< vec<int> > x;` -- the declaration scan excluded nested angle
        # brackets, so a member of a container of containers was invisible.
        self.assertEqual(
            cpp_auto._declared_types("vec< vec<int> > items;").get("items"),
            "vec< vec<int> >")

    def test_a_self_naming_typedef_is_left_alone(self):
        # `typedef struct X X;` names itself. Substituting it prepends
        # `struct` once per round, and the C Crust emits for its own types is
        # full of them -- one pass produced `struct struct struct .. Vec_int`.
        self.assertEqual(
            cpp_auto._scan_typedefs("typedef struct Vec_int Vec_int;"), {})

    def test_a_class_scoped_typedef_is_not_taken_globally(self):
        # litehtml's are named `ptr` and `vector`; collecting those flatly
        # would make every `vector` mean `box::vector`, including the
        # supplied template of that name.
        got = cpp_auto._scan_typedefs(
            "typedef vec<int> ints;\nclass box { typedef vec<int> vector; };")
        self.assertIn("ints", got)
        self.assertNotIn("vector", got)

    def test_a_template_parameter_shadows_an_alias(self):
        # Inside the template the parameter is what the name means.
        out = cpprust.translate("""
typedef int B;
template<typename A, typename B>
class Two { A a; B b; };
Two<B, char> x;
""", path="t.cpp")
        self.assertIn("struct Two_B_char { B a; char b; };", out)


class TestCppIncludePath(unittest.TestCase):
    """Headers found on a search path, not just beside the source."""

    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp()
        self.inc = os.path.join(self.root, "include")
        self.src = os.path.join(self.root, "src")
        os.makedirs(self.inc)
        os.makedirs(self.src)
        with open(os.path.join(self.inc, "a.h"), "w") as f:
            f.write("class A { int v; public: A(); int g(); };\n")

    def test_a_header_on_the_search_path_resolves(self):
        # A project whose headers live in `include/` rather than beside the
        # source -- which is most of them.
        out = cpprust.translate(
            '#include "a.h"\nA::A() { v = 4; }\nint A::g() { return v; }\n'
            'int f(void) { A a; return a.g(); }\n',
            path="a.cpp", basedir=self.src, incdirs=[self.inc])
        self.assertIn("static int A_g(A *this) { return this->v; }", out)


class TestCppNamespaceReopening(unittest.TestCase):
    """A namespace may be reopened, which a project with one per header does."""

    def test_reopening_is_not_a_collision(self):
        # A name this pass produced from an earlier block of the same
        # namespace is the same entity; only one the author spelled `N_x` is
        # a genuine collision.
        out = cpprust.translate("""
namespace n { class A { public: int v; }; }
namespace n { int twice(int x) { return x * 2; } }
int f(void) { n::A a; a.v = 3; return n::twice(a.v); }
""", path="t.cpp")
        self.assertIn("n_twice", out)

    def test_a_real_collision_is_still_reported(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""
int n_twice(int x) { return x; }
namespace n { int twice(int x) { return x * 2; } }
int f(void) { return n::twice(1); }
""", path="t.cpp")
        self.assertIn("one symbol", cm.exception.message)


class TestCppExplicit(unittest.TestCase):
    """`explicit` constrains implicit conversion, which this never performs."""

    def test_an_explicit_constructor_is_accepted(self):
        out = cpprust.translate("""
class A { int v; public: explicit A(int n); int g(); };
A::A(int n) { v = n; }
int A::g() { return v; }
int f(void) { A a(5); return a.g(); }
""", path="t.cpp")
        self.assertIn("A_new(A *this, int n)", out)


class TestCppMap(unittest.TestCase):
    """`<map>`, written in the subset like the other containers.

    The iterator is a **pointer**. That is the whole design: `it->first`,
    `++it`, `it != m.end()` and `*it` are then plain C on a plain pointer, and
    none of `operator++`, `operator!=` or an iterator class has to exist. It
    costs a linear `find`, which is the honest trade for a container written
    in a subset with no comparison operator to order keys by.
    """

    def test_int_keys(self):
        out = cpprust.translate("""#include <map>
int f(void) {
    std::map<int, int> m;
    m[3] = 30;
    m[7] = 70;
    return m[3] + m[7] + m.size();
}
""", path="t.cpp")
        self.assertIn("map_int_int__index", out)

    def test_the_iterator_is_a_pointer(self):
        out = cpprust.translate("""#include <map>
int f(void) {
    std::map<int, int> m;
    int t = 0;
    for (auto it = m.begin(); it != m.end(); ++it) { t = t + it->second; }
    return t;
}
""", path="t.cpp")
        self.assertIn("pair_int_int * it = ", out)

    def test_a_method_return_is_substituted_per_instantiation(self):
        # `map<int,int>::begin()` reads `pair<K,V> *` in the template source.
        # Taking that literally asked for a class called `pair_K_V`.
        out = cpprust.translate("""#include <map>
int f(void) { std::map<int, int> m; auto it = m.begin(); return it == m.end(); }
""", path="t.cpp")
        self.assertNotIn("pair_K_V", out)

    def test_class_keys_compare_through_equals(self):
        out = cpprust.translate("""#include <map>
#include <string>
int f(void) {
    std::map<std::string, int> t;
    std::string a("x");
    t[a] = 1;
    return t.find(a) == t.end() ? 0 : t[a];
}
""", path="t.cpp")
        self.assertIn("string_equals", out)

    def test_a_user_key_class_is_reported(self):
        # A *user* key class is refused, and the reason is ordering rather
        # than the missing `equals`: the supplied templates are spliced above
        # the file, so when `map<K, ..>` is emitted `K` is not a class this
        # pass has seen yet, and `__cpp_ref(K)` picks the by-value spelling.
        # The diagnostic is still accurate and actionable; a key class that
        # works has to be one of the supplied ones (`string`) for now.
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("""#include <map>
class K { public: int v; K() { v = 0; } ~K() { } };
int f(void) { std::map<K, int> m; K k; return m.count(k); }
""", path="t.cpp")
        self.assertIn("by value", cm.exception.message)

    def test_the_header_alone_supplies_nothing(self):
        out = cpprust.translate(
            "#include <map>\nint f(void) { return 0; }", path="t.cpp")
        self.assertNotIn("class map", out)


class TestCppElementBuiltins(unittest.TestCase):
    """`__cpp_copy` / `__cpp_drop` / `__cpp_eq` / `__cpp_ref`.

    A template body is textual, so it can spell `T` but not `T_copy`. These
    are the hook that lets a container say "copy an element" once and have it
    mean the right thing per instantiation.
    """

    TPL = """
template<typename T> class holder {
    T a; T b;
public:
    holder() { }
    int same() { return __cpp_eq(T, a, b); }
};
"""

    def test_eq_on_a_scalar_is_an_operator(self):
        out = cpprust.translate(
            self.TPL + "int f(void) { holder<int> h; return h.same(); }",
            path="t.cpp")
        self.assertIn("((this->a) == (this->b))", out)

    def test_eq_on_a_class_goes_through_equals(self):
        out = cpprust.translate("""
class S { public: int v; S() { v = 0; } int equals(const S &o) { return v == o.v; } };
""" + self.TPL + "int f(void) { holder<S> h; return h.same(); }", path="t.cpp")
        self.assertIn("S_equals", out)

    def test_copy_and_drop_accept_scalars(self):
        # They used to refuse, which is what made `ownvector<int>` an error.
        # A `map<int, ..>` copies a scalar key, so they have to accept one.
        out = cpprust.translate("""
template<typename T> class cell {
    T v;
public:
    cell() { }
    void put(T x) { __cpp_copy(T, v, x); }
    void drop() { __cpp_drop(T, v); }
};
int f(void) { cell<int> c; c.put(4); c.drop(); return 0; }
""", path="t.cpp")
        self.assertIn("(this->v) = (x)", out)

    def test_ownvector_of_a_scalar_is_steered_to_vector(self):
        # The guidance that used to fall out of `__cpp_copy` refusing
        # scalars, stated where it belongs now.
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(
                "void f(void) { std::ownvector<int> v; int k = 1; "
                "v.push_back(k); }", path="t.cpp")
        self.assertIn("Use `vector<int>`", cm.exception.message)


class TestCppScalarReferences(unittest.TestCase):
    """`int &x` -- a reference is a pointer the source need not spell, and
    that is as true of a scalar as of a class."""

    def test_a_scalar_reference_parameter_is_lowered(self):
        out = cpprust.translate("""
class A { public: int v; A() { v = 0; } void set(const int &n) { v = n; } };
int f(void) { A a; int k = 5; a.set(k); return a.v; }
""", path="t.cpp")
        self.assertIn("A_set(A *this, const int *n)", out)
        self.assertIn("(*n)", out)

    def test_the_call_site_takes_the_address(self):
        out = cpprust.translate("""
class A { public: int v; A() { v = 0; } void set(const int &n) { v = n; } };
int f(void) { A a; int k = 5; a.set(k); return a.v; }
""", path="t.cpp")
        self.assertIn("A_set(&a, &k)", out)


class TestCppCompoundAssignment(unittest.TestCase):
    """`operator+=` and friends, lowered like `operator=`."""

    M = """
class M {
public:
    int a;
    M() { a = 0; }
    void operator+=(const M &o) { a = a + o.a; }
    void operator-=(const M &o) { a = a - o.a; }
};
"""

    def test_it_becomes_a_call(self):
        out = cpprust.translate(
            self.M + "int f(void) { M x; M y; y.a = 5; x += y; return x.a; }",
            path="t.cpp")
        self.assertIn("M__augadd(&x, &y);", out)

    def test_each_operator_gets_its_own_symbol(self):
        # Spelled out rather than punctuated: the symbol has to be a C
        # identifier, and `__augsub` reads back to the operator it came from.
        out = cpprust.translate(
            self.M + "int f(void) { M x; M y; x -= y; return x.a; }",
            path="t.cpp")
        self.assertIn("M__augsub(&x, &y);", out)

    def test_a_chained_compound_assignment_is_reported(self):
        # The result is dropped, so there is nothing to assign onward.
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(
                self.M + "int f(void) { M x; M y; M z; x += y = z; return 0; }",
                path="t.cpp")
        self.assertIn("chained assignment", cm.exception.message)

    def test_a_scalar_compound_assignment_is_untouched(self):
        out = cpprust.translate(
            "int f(void) { int n = 1; n += 2; return n; }", path="t.cpp")
        self.assertIn("n += 2;", out)


class TestCppTemplateParameterNames(unittest.TestCase):
    """A template parameter is not a name its namespace declares."""

    def test_template_class_t_is_not_flattened(self):
        # `template<class T>` reads exactly like a class declaration to the
        # scan that collects what a namespace declares. litehtml has one, and
        # flattening turned every `T` in the template into `litehtml_T` --
        # including the `operator T()` that made it visible.
        out = cpp_auto.resolve_namespaces("""
namespace n {
    template<class T> class holder {
        T val;
    public:
        holder() { }
        T get() { return val; }
    };
    class Other { public: int v; };
}
int f(void) { n::Other o; return o.v; }
""", "t.cpp")
        self.assertIn("n_holder", out)
        self.assertIn("n_Other", out)
        self.assertNotIn("n_T", out)

    def test_typename_spelling_too(self):
        out = cpp_auto.resolve_namespaces("""
namespace n {
    template<typename T> class holder { T val; public: T get() { return val; } };
}
int f(void) { return 0; }
""", "t.cpp")
        self.assertNotIn("n_T", out)


class TestCppConversionOperator(unittest.TestCase):
    """`operator T()` gets its own diagnostic.

    It is not one more overload to add but a different kind of thing: it
    applies wherever the compiler decides a conversion is wanted, and this
    pass reads types from how they are written.
    """

    def test_the_declaration_lowers_to_a_method(self):
        # Refusing the declaration refused forty files over two call sites:
        # litehtml has exactly one conversion operator, in a header every
        # file includes. What is limited is where the call can be inserted.
        out = cpprust.translate(
            "class A { public: int v; A() { v = 0; } "
            "operator int() { return v; } };" "\n"
            "int f(void) { A a; return a.v; }", path="t.cpp")
        self.assertIn("A__conv(A *this) { return this->v; }", out)

    def test_it_applies_where_the_target_type_is_written(self):
        out = cpprust.translate(
            "class A { public: int v; A() { v = 3; } "
            "operator int() { return v; } };" "\n"
            "int f(void) { A a; int w = a; int z; z = a; return w + z; }",
            path="t.cpp")
        self.assertIn("int w = A__conv(&a);", out)
        self.assertIn("z = A__conv(&a);", out)

    def test_a_missing_overload_still_reads_as_one(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(
                "class A { public: int v; A operator<<(int n) "
                "{ return *this; } };", path="t.cpp")
        self.assertIn("operator<<", cm.exception.message)
        self.assertNotIn("conversion", cm.exception.message)


class TestCppDeclarationScanAnchors(unittest.TestCase):
    """A member after an access label on its own line."""

    def test_a_field_after_a_private_label_is_seen(self):
        # The declaration scan anchored on `;{}(,` only, so `private:` on its
        # own line hid every field that followed -- which is how litehtml
        # declares `props_map m_properties;`, and why every deduction from
        # `m_properties.find(..)` failed.
        got = cpp_auto._declared_types(
            "class S {\nprivate:\n\tmap<string, int>\t\tm_props;\n};")
        self.assertEqual(got.get("m_props"), "map<string, int>")

    def test_cpp_ref_does_not_hide_a_declaration(self):
        # `__cpp_ref(T)` is a type spelled like a call, and the declarator
        # scan reads a parameter list as having no parentheses in it.
        n, m, f, tp = cpp_auto._scan_classes(cpp_auto._blank_like(
            "template<typename K,typename V> class map { public: "
            "pair<K,V> *find(__cpp_ref(K) k) { return 0; } };"))
        self.assertEqual(m.get("map", {}).get("find"), "pair<K,V> *")


class TestCppComparisonOperators(unittest.TestCase):
    """`operator==` and friends. Unlike an assignment the *result* is the
    point, so the declared return type is kept."""

    P = """
class P {
public:
    int v;
    P() { v = 0; }
    int operator==(const P &o) { return v == o.v; }
    int operator!=(const P &o) { return v != o.v; }
    int operator<(const P &o) { return v < o.v; }
    int operator<=(const P &o) { return v <= o.v; }
};
"""

    def test_equality_becomes_a_call(self):
        out = cpprust.translate(
            self.P + "int f(void) { P a; P b; return a == b; }", path="t.cpp")
        self.assertIn("P__cmpeq(&a, &b)", out)

    def test_longest_spelling_wins(self):
        # `<=` must not be read as `<`.
        out = cpprust.translate(
            self.P + "int f(void) { P a; P b; return a <= b; }", path="t.cpp")
        # The class declares `operator<` too, so `P__cmplt` exists -- what
        # matters is which one the *call site* picked.
        call = out[out.index("int f(void)"):]
        self.assertIn("P__cmple(&a, &b)", call)
        self.assertNotIn("P__cmplt", call)

    def test_scalar_comparisons_are_untouched(self):
        out = cpprust.translate(
            "int f(void) { int a = 1; int b = 2; return a == b; }",
            path="t.cpp")
        self.assertIn("a == b", out)

    def test_a_template_use_is_not_a_comparison(self):
        # `vec<int> v;` has a `<` in it and no class-typed local in front.
        out = cpprust.translate("""
template<typename T> class vec { T *d; public: vec() { d = 0; } };
int f(void) { vec<int> v; return 0; }
""", path="t.cpp")
        self.assertIn("vec_int v;", out)

    def test_an_unnameable_right_hand_side_is_reported(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate(
                self.P + "int mk(void);\n"
                "int f(void) { P a; return a == mk; }", path="t.cpp")
        self.assertIn("not an object of type P", cm.exception.message)


class TestCppBool(unittest.TestCase):
    """`bool` is a keyword in C++ and a header in C."""

    def test_bool_pulls_in_the_header(self):
        out = cpprust.translate(
            "class A { public: bool b; A() { b = true; } };\n"
            "int f(void) { A a; return a.b ? 1 : 0; }", path="t.cpp")
        self.assertIn("#include <stdbool.h>", out)

    def test_true_and_false_alone_are_enough(self):
        # A file may use the literals without ever writing the type.
        out = cpprust.translate(
            "int f(void) { int x = true; return x; }", path="t.cpp")
        self.assertIn("#include <stdbool.h>", out)

    def test_a_file_that_includes_it_is_left_alone(self):
        # Redefining would clash with the real header.
        out = cpprust.translate(
            "#include <stdbool.h>\nint f(void) { bool x = true; return x; }",
            path="t.cpp")
        self.assertEqual(out.count("stdbool.h"), 1)

    def test_a_file_without_bool_gets_nothing(self):
        out = cpprust.translate("int f(void) { return 0; }", path="t.cpp")
        self.assertNotIn("stdbool", out)
