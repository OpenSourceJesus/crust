"""Tests for the Crust front end (a minimal Rust subset lowered to C).

Two layers are covered: pure translation (shivyc.crust.translate, which is
where line-number preservation and type mapping are checked) and end-to-end
compilation, where Rust functions and C functions share a translation unit and
call each other directly.
"""

import os
import subprocess
import tempfile
import unittest

import shivyc.main
import shivyc.crust as crust
from shivyc.errors import error_collector


class _Args:
    show_reg_alloc_perf = False
    variables_on_stack = False
    simd_pack_globals = False
    stackless_calls = False
    metamorphic = False
    opt_level = 0

    def __init__(self, files, output_name):
        self.files = files
        self.output_name = output_name


def _run(source, suffix=".c"):
    workdir = tempfile.mkdtemp()
    src_path = os.path.join(workdir, "prog" + suffix)
    out_path = os.path.join(workdir, "prog")
    with open(src_path, "w") as f:
        f.write(source)
    args = _Args([src_path], [out_path])
    shivyc.main.get_arguments = lambda: args
    error_collector.show = lambda: True
    error_collector.clear()
    rc = shivyc.main.main()
    assert rc == 0, "compilation failed"
    return subprocess.run([out_path]).returncode


class TestCrustTranslation(unittest.TestCase):
    """Source-to-source behavior, independent of codegen."""

    def test_signature_mapping(self):
        c = crust.translate("fn f(a: i32, b: *mut u8) -> i64 { 0 }")
        self.assertIn("long f(int a, unsigned char *b)", c)

    def test_unit_return_is_void(self):
        self.assertIn("void f(void)", crust.translate("fn f() { }"))

    def test_tail_expression_becomes_return(self):
        c = crust.translate("fn f(x: i32) -> i32 { x + 1 }")
        self.assertIn("return (x + 1);", c)

    def test_no_tail_return_for_unit_fn(self):
        c = crust.translate("fn f(x: i32) { g(x) }")
        self.assertNotIn("return", c)

    def test_let_infers_from_annotation_and_literal(self):
        c = crust.translate("fn f() -> i32 { let a: u64 = 1; let b = 2; b }")
        self.assertIn("unsigned long a = 1;", c)
        self.assertIn("int b = 2;", c)

    def test_uninferable_let_is_an_error(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f() { let x = undeclared_thing.field; }")

    def test_loop_and_range_lowering(self):
        c = crust.translate("fn f() { loop { } for i in 0..=3 { } }")
        self.assertIn("while (1)", c)
        self.assertIn("i <= 3", c)

    def test_range_uses_wider_bound_type(self):
        c = crust.translate("fn f(n: usize) { for i in 0..n { } }")
        self.assertIn("unsigned long i = 0", c)

    def test_cast_lowering(self):
        c = crust.translate("fn f(x: i32) -> f64 { x as f64 }")
        self.assertIn("(double)(x)", c)

    def test_bang_is_bitwise_on_integers_logical_on_bool(self):
        c = crust.translate("fn f(a: i32, b: bool) -> i32 { !a } "
                            "fn g(b: bool) -> bool { !b }")
        self.assertIn("(~a)", c)
        self.assertIn("(!b)", c)

    def test_numeric_literal_forms(self):
        c = crust.translate("fn f() { let a: u64 = 1_000; let b = 0xff; "
                            "let d = 1.5f32; }")
        self.assertIn("1000", c)
        self.assertIn("0xff", c)
        self.assertIn("1.5f", c)

    def test_line_numbers_are_preserved(self):
        src = "int a;\nfn f() -> i32 {\n    let x: i32 = 1;\n    x\n}\nint b;\n"
        out = crust.translate(src)
        # The prototype prefix shares line 1, so every line index still holds.
        self.assertEqual(out.count("\n"), src.count("\n"))
        lines = out.split("\n")
        self.assertIn("int f(void)", lines[1])
        self.assertIn("int x = 1;", lines[2])
        self.assertIn("return x;", lines[3])
        self.assertIn("int b;", lines[5])

    def test_c_only_source_is_untouched(self):
        src = "int main(void) { return 0; }\n"
        self.assertEqual(crust.translate(src), src)

    def test_fn_inside_a_c_body_is_not_an_item(self):
        # `fn` at nonzero brace depth is not a top-level Rust item.
        src = "int main(void) { int fn = 1; return fn (0); }\n"
        self.assertEqual(crust.translate(src), src)

    def test_comments_and_strings_do_not_start_items(self):
        src = '/* fn f() {} */\nchar *s = "fn g() {}";\n'
        self.assertEqual(crust.translate(src), src)

    def test_forward_declarations_are_emitted(self):
        c = crust.translate("fn a() -> i32 { b() } fn b() -> i32 { 1 }")
        self.assertIn("int b(void);", c)


class TestCrustCompilation(unittest.TestCase):
    """End-to-end: Rust functions compiled and executed."""

    def test_rust_main_returns_value(self):
        self.assertEqual(_run("fn main() -> i32 { 7 }"), 7)

    def test_rust_main_unit_returns_zero(self):
        self.assertEqual(_run("fn main() { let x: i32 = 5; }"), 0)

    def test_arithmetic_and_let_mut(self):
        self.assertEqual(_run(
            "fn main() -> i32 { let mut x: i32 = 3; x += 4; x * 2 }"), 14)

    def test_while_loop(self):
        self.assertEqual(_run(
            "fn main() -> i32 {\n"
            "    let mut n: i32 = 0;\n"
            "    let mut i: i32 = 0;\n"
            "    while i < 5 { n += i; i += 1; }\n"
            "    n\n"
            "}"), 10)

    def test_for_range_and_break(self):
        self.assertEqual(_run(
            "fn main() -> i32 {\n"
            "    let mut n: i32 = 0;\n"
            "    for i in 0..100 { if i > 4 { break; } n += i; }\n"
            "    n\n"
            "}"), 10)

    def test_if_else_chain_as_tail(self):
        self.assertEqual(_run(
            "fn c(n: i32) -> i32 { if n < 0 { 1 } else if n == 0 { 2 } "
            "else { 3 } }\n"
            "fn main() -> i32 { c(-1) + c(0) * 3 + c(9) * 9 }"), 34)

    def test_recursion_and_forward_reference(self):
        self.assertEqual(_run(
            "fn main() -> i32 { fact(5) }\n"
            "fn fact(n: i32) -> i32 { if n < 2 { 1 } else { n * fact(n-1) } }"
        ), 120)

    def test_rust_calls_c_and_c_calls_rust(self):
        self.assertEqual(_run(
            "int triple(int x) { return x * 3; }\n"
            "fn quad(x: i32) -> i32 { triple(x) + x }\n"
            "int main(void) { return quad(5); }\n"), 20)

    def test_pointers_and_indexing(self):
        self.assertEqual(_run(
            "fn total(p: *const i32, n: usize) -> i32 {\n"
            "    let mut s: i32 = 0;\n"
            "    for i in 0..n { s += p[i]; }\n"
            "    s\n"
            "}\n"
            "int main(void) { int a[4] = {1,2,3,4}; return total(a, 4); }\n"),
            10)

    def test_pointer_deref_and_mutation(self):
        self.assertEqual(_run(
            "fn bump(p: *mut i32) { *p = *p + 9; }\n"
            "int main(void) { int v = 3; bump(&v); return v; }\n"), 12)

    def test_float_math(self):
        self.assertEqual(_run(
            "fn half(x: f64) -> f64 { x / 2.0 }\n"
            "fn main() -> i32 { half(9.0) as i32 }"), 4)

    def test_bool_and_logical_ops(self):
        self.assertEqual(_run(
            "fn both(a: bool, b: bool) -> bool { a && b }\n"
            "fn main() -> i32 { if both(true, true) { 1 } else { 0 } }"), 1)

    def test_dot_rs_file_extension(self):
        self.assertEqual(_run("fn main() -> i32 { 42 }", suffix=".rs"), 42)

    def test_syntax_error_is_reported(self):
        with self.assertRaises(AssertionError):
            _run("fn main() -> i32 { let x = ; }")


if __name__ == "__main__":
    unittest.main()
