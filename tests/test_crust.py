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


def _run(source, suffix=".c", extra=None):
    """Compile `source` and return its exit status.

    `extra` maps auxiliary file names (e.g. an included `.rs`) to contents,
    written alongside the main source so quoted includes resolve.
    """
    workdir = tempfile.mkdtemp()
    for name, text in (extra or {}).items():
        with open(os.path.join(workdir, name), "w") as f:
            f.write(text)
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
        src = ("int a;\n"
               "fn f() -> i32 {\n"
               "    let x: i32 = 1;\n"
               "    x\n"
               "}\n"
               "int b;\n")
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


PT = """
struct Point {
    x: f64,
    y: f64,
}

impl Point {
    fn new(x: f64, y: f64) -> Point {
        Point { x: x, y: y }
    }
    fn norm2(&self) -> f64 {
        self.x * self.x + self.y * self.y
    }
    fn scale(&mut self, k: f64) {
        self.x = self.x * k;
        self.y = self.y * k;
    }
    fn origin() -> Point {
        Point { x: 0.0, y: 0.0 }
    }
}
"""


class TestCrustStructs(unittest.TestCase):
    """`struct` items and their lowering."""

    def test_struct_is_hoisted_and_typedefed(self):
        c = crust.translate("struct P { x: i32, y: i32 }")
        self.assertIn("struct P { int x; int y; };", c)
        self.assertIn("typedef struct P P;", c)

    def test_c_struct_is_not_claimed(self):
        src = ("struct P { int x; int y; };\n"
               "fn f() -> i32 { 1 }\n")
        out = crust.translate(src)
        self.assertIn("struct P { int x; int y; };", out)
        # untouched: still exactly one definition, the C one
        self.assertEqual(out.count("struct P {"), 1)

    def test_struct_field_of_struct_type(self):
        c = crust.translate("struct Inner { v: i32 }\n"
                            "struct Outer { a: Inner, b: i32 }")
        # dependency order: Inner must be defined before Outer
        self.assertLess(c.index("struct Inner { int v; };"),
                        c.index("struct Outer {"))

    def test_recursive_struct_by_value_is_rejected(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("struct N { next: N }")

    def test_struct_literal_lowers_to_compound_literal(self):
        c = crust.translate("struct P { x: i32 }\n"
                            "fn f() -> P { P { x: 3 } }")
        self.assertIn("(P){.x = 3}", c)

    def test_missing_field_in_literal_is_an_error(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("struct P { x: i32, y: i32 }\n"
                            "fn f() -> P { P { x: 1 } }")

    def test_unknown_field_in_literal_is_an_error(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("struct P { x: i32 }\n"
                            "fn f() -> P { P { z: 1 } }")

    def test_unknown_field_access_is_an_error(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("struct P { x: i32 }\n"
                            "fn f(p: P) -> i32 { p.z }")

    def test_struct_literal_not_parsed_in_condition_position(self):
        # `if p.x > 0 { ... }` -- the brace opens a block, not a literal.
        c = crust.translate(
            "struct P { x: i32 }\n"
            "fn f(p: P) -> i32 { if p.x > 0 { 1 } else { 2 } }")
        self.assertIn("if ((p.x > 0))", c)

    def test_attributes_are_skipped(self):
        c = crust.translate("#[derive(Copy)]\nstruct P { x: i32 }")
        self.assertIn("struct P { int x; };", c)


class TestCrustImpl(unittest.TestCase):
    """`impl` blocks, method lowering and call sites."""

    def test_method_is_mangled_with_self_pointer(self):
        c = crust.translate(PT)
        self.assertIn("double Point_norm2(Point *self)", c)

    def test_self_field_access_uses_arrow(self):
        self.assertIn("self->x", crust.translate(PT))

    def test_associated_fn_has_no_self_param(self):
        c = crust.translate(PT)
        self.assertIn("Point Point_origin(void)", c)

    def test_method_call_auto_refs_receiver(self):
        c = crust.translate(PT + "\nfn f(p: Point) -> f64 { p.norm2() }")
        self.assertIn("Point_norm2(&p)", c)

    def test_method_call_on_pointer_does_not_double_ref(self):
        c = crust.translate(PT + "\nfn f(p: *mut Point) -> f64 "
                                 "{ p.norm2() }")
        self.assertIn("Point_norm2(p)", c)

    def test_path_call_of_associated_fn(self):
        c = crust.translate(PT + "\nfn f() -> Point { Point::origin() }")
        self.assertIn("Point_origin()", c)

    def test_self_type_alias(self):
        c = crust.translate(
            "struct P { x: i32 }\n"
            "impl P { fn id(&self) -> Self { Self { x: 1 } } }")
        self.assertIn("P P_id(P *self)", c)

    def test_unknown_method_is_an_error(self):
        with self.assertRaises(crust.CrustError):
            crust.translate(PT + "\nfn f(p: Point) -> f64 { p.nope() }")

    def test_method_on_untyped_receiver_is_an_error(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f(p: *mut i32) -> i32 { (*p).norm2() }")

    def test_struct_and_impl_end_to_end(self):
        # norm2 = 25; after scale(2.0) it is 100; total 125.
        self.assertEqual(_run(PT + """
fn main() -> i32 {
    let mut p: Point = Point::new(3.0, 4.0);
    let n: f64 = p.norm2();
    p.scale(2.0);
    (n + p.norm2()) as i32
}
""", suffix=".rs"), 125)

    def test_method_chaining_through_fields(self):
        self.assertEqual(_run("""
struct Inner { v: i32 }
struct Outer { a: Inner }
impl Inner { fn get(&self) -> i32 { self.v } }
fn main() -> i32 {
    let o: Outer = Outer { a: Inner { v: 9 } };
    o.a.get()
}
""", suffix=".rs"), 9)

    def test_c_can_call_rust_methods(self):
        self.assertEqual(_run(PT + """
int main(void) {
    Point p = Point_new(3.0, 4.0);
    return (int)Point_norm2(&p);
}
"""), 25)


class TestCrustRsInclude(unittest.TestCase):
    """`#include "foo.rs"` from C."""

    VEC = """
struct Vec2 { x: f64, y: f64 }
impl Vec2 {
    fn new(x: f64, y: f64) -> Vec2 { Vec2 { x: x, y: y } }
    fn dot(&self, o: *const Vec2) -> f64 { self.x * o.x + self.y * o.y }
    fn len2(&self) -> f64 { self.dot(self) }
}
"""

    def test_c_includes_rs_and_calls_it(self):
        self.assertEqual(_run(
            '#include "vec2.rs"\n'
            "int main(void) { Vec2 a = Vec2_new(3.0, 4.0); "
            "return (int)Vec2_len2(&a); }\n",
            extra={"vec2.rs": self.VEC}), 25)

    def test_rust_in_includer_uses_included_struct(self):
        self.assertEqual(_run(
            '#include "vec2.rs"\n'
            "fn scaled(k: f64) -> Vec2 { Vec2::new(k, k * 2.0) }\n"
            "int main(void) { Vec2 a = Vec2_new(3.0, 4.0); "
            "Vec2 b = scaled(1.0); return (int)Vec2_dot(&a, &b); }\n",
            extra={"vec2.rs": self.VEC}), 11)

    def test_leading_directive_is_not_clobbered(self):
        # The prelude must not be prefixed onto a `#` line.
        out = crust.translate('#include "x.rs"\nfn f() -> i32 { 1 }\n')
        self.assertTrue(out.split("\n")[0].lstrip().startswith("#include"))

    def test_include_is_left_for_the_preprocessor(self):
        out = crust.translate('#include "x.rs"\nfn f() -> i32 { 1 }\n')
        self.assertIn('#include "x.rs"', out)

    def test_error_in_included_rs_names_that_file_and_line(self):
        # The diagnostic must point into the .rs module, not the includer.
        workdir = tempfile.mkdtemp()
        with open(os.path.join(workdir, "bad.rs"), "w") as f:
            f.write("struct P { x: i32 }\n"
                    "impl P {\n"
                    "    fn get(&self) -> i32 {\n"
                    "        self.nosuchfield\n"
                    "    }\n"
                    "}\n")
        main_c = os.path.join(workdir, "prog.c")
        with open(main_c, "w") as f:
            f.write('#include "bad.rs"\nint main(void){ return 0; }\n')
        args = _Args([main_c], [os.path.join(workdir, "prog")])
        shivyc.main.get_arguments = lambda: args
        messages = []
        error_collector.show = lambda: messages.extend(
            str(e) for e in error_collector.issues) or True
        error_collector.clear()
        self.assertNotEqual(shivyc.main.main(), 0)
        joined = " ".join(messages)
        self.assertIn("bad.rs", joined)
        self.assertIn("line 4", joined)
        self.assertIn("nosuchfield", joined)

    def test_commented_out_include_is_ignored(self):
        self.assertEqual(crust.find_rs_includes('// #include "a.rs"\n'), [])
        self.assertEqual(crust.find_rs_includes('#include "a.rs"\n'),
                         ['"a.rs"'])


class TestCrustEnums(unittest.TestCase):
    """`enum` items and their lowering."""

    def test_enum_lowers_to_c_enum_with_prefixed_members(self):
        c = crust.translate("enum Color { Red, Green, Blue }")
        self.assertIn("enum Color { Color_Red, Color_Green, Color_Blue };", c)
        self.assertIn("typedef enum Color Color;", c)

    def test_explicit_discriminants_are_kept(self):
        c = crust.translate("enum E { A, B = 5, C }")
        self.assertIn("E_B = 5", c)

    def test_variant_path_resolves(self):
        c = crust.translate("enum Color { Red }\n"
                            "fn f() -> Color { Color::Red }")
        self.assertIn("return Color_Red;", c)

    def test_c_enum_is_not_claimed(self):
        # A C enum declaration ends in `;`; a Rust one never does.
        src = "enum Level { LOW, HIGH = 9 };\nfn f() -> i32 { 1 }\n"
        out = crust.translate(src)
        self.assertIn("enum Level { LOW, HIGH = 9 };", out)
        self.assertNotIn("Level_LOW", out)

    def test_data_carrying_variant_is_rejected(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("enum E { A(i32) }")

    def test_enum_round_trip(self):
        self.assertEqual(_run("""
enum Color { Red, Green = 5, Blue }
fn main() -> i32 { Color::Green as i32 }
""", suffix=".rs"), 5)


class TestCrustMatch(unittest.TestCase):
    """`match` lowering to `switch`."""

    def test_match_lowers_to_switch(self):
        c = crust.translate("fn f(n: i32) -> i32 { match n "
                            "{ 0 => 1, _ => 2 } }")
        self.assertIn("switch (n)", c)
        self.assertIn("case 0:", c)
        self.assertIn("default:", c)

    def test_or_patterns_become_stacked_cases(self):
        c = crust.translate("fn f(n: i32) -> i32 { match n "
                            "{ 1 | 2 => 1, _ => 2 } }")
        self.assertIn("case 1: case 2:", c)

    def test_arms_do_not_fall_through(self):
        c = crust.translate("fn f(n: i32) { match n { 0 => g(), _ => h() } }")
        self.assertEqual(c.count("break;"), 2)

    def test_tail_match_arms_return(self):
        c = crust.translate("fn f(n: i32) -> i32 { match n "
                            "{ 0 => 7, _ => 8 } }")
        self.assertIn("return 7;", c)
        self.assertIn("return 8;", c)

    def test_exhaustive_enum_match_needs_no_wildcard(self):
        c = crust.translate("enum E { A, B }\n"
                            "fn f(e: E) -> i32 { match e "
                            "{ E::A => 1, E::B => 2 } }")
        self.assertIn("case E_A:", c)

    def test_non_exhaustive_enum_match_is_an_error(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("enum E { A, B, C }\n"
                            "fn f(e: E) -> i32 { match e "
                            "{ E::A => 1, E::B => 2 } }")
        self.assertIn("`C`", str(cm.exception))

    def test_wildcard_satisfies_exhaustiveness(self):
        c = crust.translate("enum E { A, B, C }\n"
                            "fn f(e: E) -> i32 { match e "
                            "{ E::A => 1, _ => 2 } }")
        self.assertIn("default:", c)

    def test_binding_pattern_is_rejected(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f(n: i32) -> i32 { match n "
                            "{ other => other } }")

    def test_match_end_to_end(self):
        self.assertEqual(_run("""
enum Color { Red, Green, Blue }
fn score(c: Color) -> i32 {
    match c {
        Color::Red => 1,
        Color::Green | Color::Blue => { 20 }
    }
}
fn main() -> i32 { score(Color::Red) + score(Color::Blue) }
""", suffix=".rs"), 21)


class TestCrustConsts(unittest.TestCase):
    """`const` and `static` items."""

    def test_integer_const_lowers_to_enum_constant(self):
        # An enum constant is a C constant expression; `static const` is not.
        c = crust.translate("const MAX: i32 = 100;\nfn f() -> i32 { MAX }")
        self.assertIn("enum { MAX = 100 };", c)

    def test_integer_const_can_size_an_array(self):
        self.assertEqual(_run("""
const N: usize = 4;
fn main() -> i32 {
    let mut a: [i32; N] = [0; N];
    a[3] = 42;
    a[3]
}
""", suffix=".rs"), 42)

    def test_non_integer_const_stays_an_object(self):
        c = crust.translate("const K: f64 = 1.5;\nfn f() -> f64 { K }")
        self.assertIn("static const double K = 1.5;", c)

    def test_array_literal(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let a: [i32; 3] = [10, 15, 17];
    a[0] + a[1] + a[2]
}
""", suffix=".rs"), 42)

    def test_nonzero_repeat_initializer_is_rejected(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f() { let a: [i32; 3] = [7; 3]; }")

    def test_static_mut_lowers_to_static(self):
        c = crust.translate("static mut N: i64 = 0;\nfn f() -> i64 { N }")
        self.assertIn("static long N = 0;", c)

    def test_c_const_is_not_claimed(self):
        src = "const int MAX = 100;\nfn f() -> i32 { 1 }\n"
        out = crust.translate(src)
        self.assertIn("const int MAX = 100;", out)
        self.assertNotIn("static const int MAX", out)

    def test_const_is_usable_before_its_definition(self):
        self.assertEqual(_run("""
fn main() -> i32 { LIMIT / 2 }
const LIMIT: i32 = 84;
""", suffix=".rs"), 42)

    def test_local_const(self):
        self.assertEqual(_run(
            "fn main() -> i32 { const K: i32 = 9; K }", suffix=".rs"), 9)


class TestCrustTupleStructs(unittest.TestCase):
    """Tuple structs and positional field access."""

    def test_fields_are_named_positionally(self):
        c = crust.translate("struct Wrap(i32, f64);")
        self.assertIn("struct Wrap { int _0; double _1; };", c)

    def test_construction_and_access(self):
        c = crust.translate("struct W(i32);\n"
                            "fn f() -> i32 { let w: W = W(5); w.0 }")
        self.assertIn("(W){._0 = 5}", c)
        self.assertIn("return w._0;", c)

    def test_wrong_arity_is_an_error(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("struct W(i32, i32);\nfn f() { let w = W(1); }")

    def test_tuple_struct_end_to_end(self):
        self.assertEqual(_run("""
struct Pair(i32, i32);
fn sum(p: Pair) -> i32 { p.0 + p.1 }
fn main() -> i32 { sum(Pair(17, 25)) }
""", suffix=".rs"), 42)


class TestCrustIfExpression(unittest.TestCase):
    """`if` in expression position."""

    def test_if_expression_becomes_ternary(self):
        c = crust.translate("fn f(b: bool) -> i32 { let v: i32 = "
                            "if b { 1 } else { 2 }; v }")
        self.assertIn("? 1 : 2", c)

    def test_else_if_chain(self):
        c = crust.translate("fn f(n: i32) -> i32 { let v: i32 = "
                            "if n < 0 { 1 } else if n == 0 { 2 } "
                            "else { 3 }; v }")
        self.assertEqual(c.count("?"), 2)

    def test_missing_else_is_an_error(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f(b: bool) -> i32 "
                            "{ let v: i32 = if b { 1 }; v }")

    def test_statements_in_arm_are_an_error(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f(b: bool) -> i32 { let v: i32 = "
                            "if b { let q: i32 = 1; q } else { 2 }; v }")

    def test_if_statement_still_works(self):
        # A bare `if` as a statement must not be treated as an expression.
        c = crust.translate("fn f(b: bool) { if b { g(); } }")
        self.assertIn("if (b)", c)
        self.assertNotIn("?", c)

    def test_if_expression_end_to_end(self):
        self.assertEqual(_run("""
fn pick(n: i32) -> i32 { if n > 0 { n * 2 } else { 0 } }
fn main() -> i32 { pick(21) + pick(-5) }
""", suffix=".rs"), 42)


class TestEnumLiteralSpotRegression(unittest.TestCase):
    """Backend fix: a symbolic literal must not be range-compared as an int.

    Storing an enum constant into a struct member crashed the register
    allocator with a TypeError. This is plain C and independent of Crust.
    """

    def test_enum_constant_stored_to_struct_member(self):
        self.assertEqual(_run(
            "enum Level { LOW, HIGH = 9 };\n"
            "struct P { int x; };\n"
            "int main(void) { struct P p; p.x = HIGH; return p.x - 9; }\n"),
            0)


if __name__ == "__main__":
    unittest.main()
