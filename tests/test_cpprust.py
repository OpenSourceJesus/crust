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
    def test_virtual_is_error(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("class B { virtual void f() {} };")
        self.assertIn("virtual", cm.exception.message)
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


if __name__ == "__main__":
    unittest.main()
