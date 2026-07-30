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


if __name__ == "__main__":
    unittest.main()
