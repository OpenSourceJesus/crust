"""Tests for the Crust front end (a minimal Rust subset lowered to C).

Two layers are covered: pure translation (shivyc.crust.translate, which is
where line-number preservation and type mapping are checked) and end-to-end
compilation, where Rust functions and C functions share a translation unit and
call each other directly.
"""

import os
import subprocess
import sys
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

    def test_large_integer_const_becomes_define(self):
        # Values outside signed 32-bit cannot be C enum constants; #define
        # keeps them usable in later constant expressions.
        c = crust.translate(
            "const LAPIC_OFFSET: usize = 0xD800_0000;\n"
            "const IOAPIC_OFFSET: usize = LAPIC_OFFSET + 4096;\n"
            "fn f() -> usize { IOAPIC_OFFSET }\n")
        self.assertIn("#define LAPIC_OFFSET", c)
        self.assertIn("#define IOAPIC_OFFSET", c)
        self.assertNotIn("static const unsigned long LAPIC_OFFSET", c)

    def test_large_const_chain_compiles(self):
        self.assertEqual(_run("""
const LAPIC_OFFSET: usize = 0xD800_0000;
const IOAPIC_OFFSET: usize = LAPIC_OFFSET + 4096;
fn main() -> i32 {
    if IOAPIC_OFFSET == 0xD8001000 { 42 } else { 0 }
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

    def test_nonzero_repeat_expands_to_elements(self):
        c = crust.translate("fn f() { let a: [i32; 3] = [7; 3]; }")
        self.assertIn("{7, 7, 7}", c)

    def test_nonzero_repeat_uses_const_length(self):
        c = crust.translate(
            "const CAP: usize = 4;\nfn f() { let a: [i32; CAP] = [1; CAP]; }")
        self.assertIn("{1, 1, 1, 1}", c)

    def test_zero_repeat_still_zero_fills(self):
        c = crust.translate("fn f() { let a: [i32; 64] = [0; 64]; }")
        self.assertIn("{0}", c)

    def test_nonzero_repeat_needs_a_literal_length(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f(n: usize) { let a: [i32; 3] = [7; n]; }")

    def test_absurd_repeat_length_is_rejected(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f() { let a: [i32; 99999] = [7; 99999]; }")

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


class TestCrustEnumImpl(unittest.TestCase):
    """`impl` blocks on enums."""

    ENUM = """
enum Color { Red, Green, Blue }
impl Color {
    fn weight(&self) -> i32 {
        match *self {
            Color::Red => 1,
            Color::Green => 2,
            Color::Blue => 3,
        }
    }
}
"""

    def test_enum_method_lowers_like_a_struct_method(self):
        c = crust.translate(self.ENUM)
        self.assertIn("int Color_weight(Color *self)", c)

    def test_enum_method_end_to_end(self):
        self.assertEqual(_run(self.ENUM + """
fn main() -> i32 {
    let c: Color = Color::Blue;
    c.weight() * 14
}
""", suffix=".rs"), 42)


class TestCrustAssociatedConsts(unittest.TestCase):
    """`const` items inside `impl` blocks."""

    def test_associated_const_is_mangled(self):
        c = crust.translate("struct P { x: i32 }\n"
                            "impl P { const ZERO: i32 = 0; }")
        self.assertIn("enum { P_ZERO = 0 };", c)

    def test_associated_const_path_reference(self):
        self.assertEqual(_run("""
struct P { x: i32 }
impl P {
    const BASE: i32 = 40;
    fn get(&self) -> i32 { self.x }
}
fn main() -> i32 {
    let p: P = P { x: 2 };
    P::BASE + p.get()
}
""", suffix=".rs"), 42)

    def test_non_integer_associated_const(self):
        c = crust.translate("struct P { x: i32 }\n"
                            "impl P { const K: f64 = 0.5; }")
        self.assertIn("static const double P_K = 0.5;", c)


class TestCrustStr(unittest.TestCase):
    """`&str` and string literals."""

    def test_str_ref_is_const_char_pointer(self):
        c = crust.translate("fn f(s: &str) -> &str { s }")
        self.assertIn("const char *f(const char *s)", c)

    def test_bare_str_is_rejected(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("fn f(s: str) {}")
        self.assertIn("unsized", str(cm.exception))

    def test_len_lowers_to_strlen(self):
        c = crust.translate("fn f(s: &str) -> usize { s.len() }")
        self.assertIn("strlen(s)", c)
        self.assertIn("unsigned long strlen(const char *);", c)

    def test_str_end_to_end(self):
        self.assertEqual(_run("""
fn size(s: &str) -> i32 { s.len() as i32 }
fn main() -> i32 { size("hello") * 8 + 2 }
""", suffix=".rs"), 42)


class TestCrustSlices(unittest.TestCase):
    """`&[T]` slices as fat pointers."""

    def test_slice_struct_is_generated(self):
        c = crust.translate("fn f(xs: &[i32]) -> usize { xs.len() }")
        self.assertIn("struct crust_slice_int { int *ptr; "
                      "unsigned long len; };", c)

    def test_len_is_a_field(self):
        c = crust.translate("fn f(xs: &[i32]) -> usize { xs.len() }")
        self.assertIn("return xs.len;", c)

    def test_index_goes_through_the_data_pointer(self):
        c = crust.translate("fn f(xs: &[i32]) -> i32 { xs[0] }")
        self.assertIn("xs.ptr[0]", c)

    def test_full_slice_of_array_takes_its_length(self):
        c = crust.translate("fn f() { let a: [i32; 4] = [0; 4];"
                            " let s: &[i32] = &a[..]; }")
        self.assertIn("{a, 4}", c)

    def test_ranged_slice(self):
        c = crust.translate("fn f() { let a: [i32; 4] = [0; 4];"
                            " let s: &[i32] = &a[1..3]; }")
        self.assertIn("a + 1", c)

    def test_array_reference_is_not_a_slice(self):
        # `&[T; N]` is a reference to an array, not a slice.
        c = crust.translate("fn f(a: &[i32; 4]) -> i32 { a[0] }")
        self.assertNotIn("crust_slice", c)

    def test_slicing_a_raw_pointer_needs_an_end_bound(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f(p: *const i32) { let s: &[i32] = &p[..]; }")

    def test_unknown_slice_method_is_an_error(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f(xs: &[i32]) { xs.nope(); }")

    def test_slices_end_to_end(self):
        self.assertEqual(_run("""
fn sum(xs: &[i32]) -> i32 {
    let mut acc: i32 = 0;
    for i in 0..xs.len() {
        acc += xs[i];
    }
    acc
}
fn main() -> i32 {
    let a: [i32; 5] = [1, 2, 3, 4, 30];
    let all: &[i32] = &a[..];
    let mid: &[i32] = &a[1..3];
    sum(all) + sum(mid) + (all.len() as i32)
}
""", suffix=".rs"), 50)

    def test_slice_of_struct_elements(self):
        self.assertEqual(_run("""
struct P { x: i32 }
fn total(ps: &[P]) -> i32 {
    let mut acc: i32 = 0;
    for i in 0..ps.len() {
        acc += ps[i].x;
    }
    acc
}
fn main() -> i32 {
    let mut a: [P; 2] = [P { x: 20 }, P { x: 22 }];
    total(&a[..])
}
""", suffix=".rs"), 42)


class TestCrustOption(unittest.TestCase):
    """`Option<T>`, monomorphised into a tagged struct per instantiation."""

    FIND = """
fn find(xs: &[i32], want: i32) -> Option<i32> {
    for i in 0..xs.len() {
        if xs[i] == want { return Some(i as i32); }
    }
    None
}
"""

    def test_option_struct_is_generated(self):
        c = crust.translate("fn f() -> Option<i32> { None }")
        self.assertIn("struct crust_option_int { _Bool some; int value; };", c)

    def test_one_struct_per_element_type(self):
        c = crust.translate("fn f() -> Option<i32> { None }\n"
                            "fn g() -> Option<f64> { None }")
        self.assertIn("crust_option_int", c)
        self.assertIn("crust_option_double", c)

    def test_some_and_none_lowering(self):
        c = crust.translate("fn f(n: i32) -> Option<i32> "
                            "{ if n > 0 { Some(n) } else { None } }")
        self.assertIn("(crust_option_int){1, n}", c)
        self.assertIn("(crust_option_int){0}", c)

    def test_none_infers_from_the_annotation(self):
        c = crust.translate("fn f() { let x: Option<i64> = None; }")
        self.assertIn("crust_option_long x = (crust_option_long){0};", c)

    def test_none_without_context_is_an_error(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("fn f() { let x = None; }")
        self.assertIn("None", str(cm.exception))

    def test_none_infers_from_a_parameter(self):
        c = crust.translate("fn g(o: Option<i32>) {}\n"
                            "fn f() { g(None); }")
        self.assertIn("g((crust_option_int){0})", c)

    def test_nested_option_splits_the_shift_token(self):
        c = crust.translate("fn f() -> Option<Option<i32>> { None }")
        self.assertIn("crust_option_crust_option_int", c)

    def test_option_of_unit_is_rejected(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f() -> Option<()> { None }")

    def test_is_some_and_is_none(self):
        c = crust.translate("fn f(o: Option<i32>) -> bool "
                            "{ o.is_some() }")
        self.assertIn("return o.some;", c)
        c = crust.translate("fn f(o: Option<i32>) -> bool "
                            "{ o.is_none() }")
        self.assertIn("(!o.some)", c)

    def test_unwrap_or_is_inline(self):
        c = crust.translate("fn f(o: Option<i32>) -> i32 "
                            "{ o.unwrap_or(7) }")
        self.assertIn("o.some ? o.value : 7", c)

    def test_unwrap_emits_a_checked_helper(self):
        c = crust.translate("fn f(o: Option<i32>) -> i32 { o.unwrap() }")
        self.assertIn("if (!o.some) abort();", c)
        self.assertIn("void abort(void);", c)

    def test_unknown_option_method_is_an_error(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f(o: Option<i32>) { o.expect(); }")

    def test_option_end_to_end(self):
        self.assertEqual(_run(self.FIND + """
fn main() -> i32 {
    let a: [i32; 5] = [10, 20, 30, 40, 50];
    let xs: &[i32] = &a[..];
    let hit: Option<i32> = find(xs, 30);
    let miss: Option<i32> = find(xs, 99);
    hit.unwrap() * 10 + miss.unwrap_or(7)
}
""", suffix=".rs"), 27)

    def test_unwrap_on_none_aborts(self):
        # subprocess reports a signal as its negation; SIGABRT is 6.
        self.assertEqual(_run("""
fn get(n: i32) -> Option<i32> { if n > 0 { Some(n) } else { None } }
fn main() -> i32 { let x: Option<i32> = get(-1); x.unwrap() }
""", suffix=".rs"), -6)


class TestCrustIfLet(unittest.TestCase):
    """`if let` and `while let`."""

    def test_if_let_binds_and_scopes_the_temporary(self):
        c = crust.translate("fn f(o: Option<i32>) -> i32 "
                            "{ if let Some(v) = o { v } else { 0 } }")
        self.assertIn(".some)", c)
        self.assertIn("int v = ", c)

    def test_subject_is_evaluated_once(self):
        c = crust.translate("fn g() -> Option<i32> { None }\n"
                            "fn f() { if let Some(v) = g() { h(v); } }")
        # The prototype spells `g(void)`, so this counts call sites only.
        self.assertEqual(c.count("g()"), 1)

    def test_non_option_subject_is_an_error(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f(n: i32) { if let Some(v) = n { } }")

    def test_other_patterns_are_rejected(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f(o: Option<i32>) { if let Ok(v) = o { } }")

    def test_if_let_end_to_end(self):
        self.assertEqual(_run("""
fn get(n: i32) -> Option<i32> { if n > 0 { Some(n * 2) } else { None } }
fn main() -> i32 {
    let mut total: i32 = 0;
    if let Some(v) = get(20) { total += v; } else { total += 1; }
    if let Some(v) = get(-1) { total += v; } else { total += 2; }
    total
}
""", suffix=".rs"), 42)

    def test_while_let_end_to_end(self):
        self.assertEqual(_run("""
static mut N: i32 = 0;
fn next() -> Option<i32> {
    N += 1;
    if N < 4 { Some(N) } else { None }
}
fn main() -> i32 {
    let mut sum: i32 = 0;
    while let Some(v) = next() { sum += v; }
    sum
}
""", suffix=".rs"), 6)


class TestCrustResult(unittest.TestCase):
    """`Result<T, E>`, monomorphised like `Option<T>`."""

    BASE = """
enum Error { Overflow, Negative }
fn nonneg(n: i32) -> Result<i32, Error> {
    if n < 0 { Err(Error::Negative) } else { Ok(n) }
}
"""

    def test_result_struct_is_generated(self):
        c = crust.translate("fn f() -> Result<i32, i32> { Ok(1) }")
        self.assertIn("_Bool ok; int value; int error;", c)

    def test_distinct_instantiations_are_distinct_structs(self):
        c = crust.translate("fn f() -> Result<i32, i32> { Ok(1) }\n"
                            "fn g() -> Result<f64, i32> { Ok(1.0) }")
        self.assertIn("crust_result_int_e_int", c)
        self.assertIn("crust_result_double_e_int", c)

    def test_ok_and_err_use_designated_initializers(self):
        c = crust.translate("fn f(n: i32) -> Result<i32, i32> "
                            "{ if n > 0 { Ok(n) } else { Err(9) } }")
        self.assertIn(".ok = 1, .value = n", c)
        self.assertIn(".ok = 0, .error = 9", c)

    def test_ok_without_context_is_an_error(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f() { let x = Ok(1); }")

    def test_is_ok_and_is_err(self):
        c = crust.translate("fn f(r: Result<i32, i32>) -> bool "
                            "{ r.is_err() }")
        self.assertIn("(!r.ok)", c)

    def test_unwrap_and_unwrap_err_emit_checked_helpers(self):
        c = crust.translate("fn f(r: Result<i32, i32>) -> i32 "
                            "{ r.unwrap() + r.unwrap_err() }")
        self.assertIn("if (!r.ok) abort();", c)
        self.assertIn("if (r.ok) abort();", c)

    def test_ok_converts_to_option(self):
        c = crust.translate("fn f(r: Result<i32, i32>) -> Option<i32> "
                            "{ r.ok() }")
        self.assertIn("crust_option_int", c)

    def test_unknown_result_method_is_an_error(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f(r: Result<i32, i32>) { r.expect(); }")

    def test_result_end_to_end(self):
        self.assertEqual(_run(self.BASE + """
fn main() -> i32 {
    let good: Result<i32, Error> = nonneg(40);
    let bad: Result<i32, Error> = nonneg(-1);
    let mut t: i32 = good.unwrap();
    if bad.is_err() { t += 2; }
    t
}
""", suffix=".rs"), 42)


class TestCrustTryOperator(unittest.TestCase):
    """The `?` operator."""

    BASE = TestCrustResult.BASE

    def test_try_hoists_a_test_and_early_return(self):
        c = crust.translate(self.BASE + """
fn f(n: i32) -> Result<i32, Error> { let v: i32 = nonneg(n)?; Ok(v) }
""")
        self.assertIn("if (!", c)
        self.assertIn(".ok) return", c)

    def test_subject_is_evaluated_once(self):
        c = crust.translate(self.BASE + """
fn f(n: i32) -> Result<i32, Error> { Ok(nonneg(n)? + 1) }
""")
        self.assertEqual(c.count("nonneg(n)"), 1)

    def test_try_outside_a_result_fn_is_an_error(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate(self.BASE + "fn f(n: i32) -> i32 "
                                        "{ nonneg(n)? }")
        self.assertIn("Result", str(cm.exception))

    def test_try_rejects_mismatched_error_types(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("fn g() -> Result<i32, i32> { Ok(1) }\n"
                            "fn f() -> Result<i32, f64> { Ok(g()?) }")
        self.assertIn("convert", str(cm.exception))

    def test_try_on_a_plain_value_is_an_error(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f(n: i32) -> Result<i32, i32> { Ok(n?) }")

    def test_try_propagates_the_error(self):
        self.assertEqual(_run(self.BASE + """
fn chain(a: i32, b: i32) -> Result<i32, Error> {
    let x: i32 = nonneg(a)?;
    let y: i32 = nonneg(b)?;
    Ok(x + y)
}
fn main() -> i32 {
    let good: Result<i32, Error> = chain(20, 22);
    let bad: Result<i32, Error> = chain(20, -1);
    let mut t: i32 = good.unwrap();
    if bad.is_err() { t += 0; } else { t += 100; }
    t
}
""", suffix=".rs"), 42)

    def test_try_on_option(self):
        self.assertEqual(_run("""
fn head(xs: &[i32]) -> Option<i32> {
    if xs.len() == 0 { return None; }
    Some(xs[0])
}
fn twice(xs: &[i32]) -> Option<i32> {
    let h: i32 = head(xs)?;
    Some(h * 2)
}
fn main() -> i32 {
    let a: [i32; 2] = [21, 9];
    let s: &[i32] = &a[..];
    twice(s).unwrap_or(0)
}
""", suffix=".rs"), 42)

    def test_try_on_option_outside_an_option_fn_is_an_error(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn g() -> Option<i32> { None }\n"
                            "fn f() -> i32 { g()? }")


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


class TestCrustForEach(unittest.TestCase):
    """`for x in xs` over slices and arrays."""

    def test_slice_iteration_sums(self):
        self.assertEqual(_run("""
fn total(xs: &[i32]) -> i32 {
    let mut s: i32 = 0;
    for x in xs {
        s += x;
    }
    s
}
fn main() -> i32 {
    let a: [i32; 4] = [10, 15, 8, 9];
    total(&a[..])
}
""", suffix=".rs"), 42)

    def test_array_iteration_uses_its_own_length(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let a: [i32; 3] = [20, 14, 8];
    let mut s: i32 = 0;
    for v in a {
        s += v;
    }
    s
}
""", suffix=".rs"), 42)

    def test_iter_is_accepted_as_a_no_op(self):
        c = crust.translate(
            "fn f(xs: &[i32]) -> i32 { let mut s: i32 = 0; "
            "for x in xs.iter() { s += x; } s }")
        self.assertIn("for (unsigned long", c)

    def test_subject_is_evaluated_once(self):
        # The subject is bound to a temporary, so a call in that position runs
        # once per loop rather than once per iteration. (The prototype spells
        # it `get(void)`, so the only `get()` left is the single call site.)
        c = crust.translate("""
fn get() -> &[i32] { 0 }
fn f() { for x in get() { } }
""")
        self.assertEqual(c.count("get()"), 1)

    def test_break_and_continue_work_inside(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let a: [i32; 5] = [1, 2, 99, 3, 4];
    let mut s: i32 = 0;
    for v in a {
        if v == 99 { continue; }
        s += v;
    }
    s * 4
}
""", suffix=".rs"), 40)

    def test_iterating_a_raw_pointer_is_rejected(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f(p: *const i32) { for x in p { } }")

    def test_iterating_an_untypeable_expression_is_rejected(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f() { for x in nope { } }")


class TestCrustUnitStructs(unittest.TestCase):
    """`struct S;` -- no fields, and its own name is its value."""

    def test_unit_struct_lowers_with_a_placeholder_field(self):
        # An `impl` block is the evidence that claims `struct Marker;` as
        # Rust rather than a C forward declaration.
        c = crust.translate(
            "struct Marker;\n"
            "impl Marker { fn id(&self) -> i32 { 1 } }\n"
            "fn f() { let m: Marker = Marker; }")
        self.assertIn("struct Marker", c)
        self.assertIn("(Marker){0}", c)

    def test_rs_file_needs_no_impl_as_evidence(self):
        # In an all-Rust file there is no C to be ambiguous with.
        c = crust.translate("struct Marker;\nfn f() { let m: Marker = Marker; }",
                            path="prog.rs")
        self.assertIn("(Marker){0}", c)

    def test_unit_struct_takes_methods(self):
        self.assertEqual(_run("""
struct Tag;
impl Tag {
    fn value(&self) -> i32 { 42 }
}
fn main() -> i32 {
    let t: Tag = Tag;
    t.value()
}
""", suffix=".rs"), 42)

    def test_c_forward_declaration_is_not_claimed(self):
        # `struct X;` in C declares an incomplete type; the following C must
        # survive untouched rather than being read as a Rust unit struct.
        src = "struct Node;\nstruct Node { int v; };\nfn f() -> i32 { 1 }\n"
        out = crust.translate(src)
        self.assertIn("struct Node { int v; };", out)

    def test_incomplete_c_type_stays_incomplete(self):
        # The regression that motivated the evidence rule: claiming a C
        # forward declaration as a one-byte Rust type would make sizeof
        # wrongly succeed on an incomplete type.
        out = crust.translate(
            "struct S;\nfn f() -> i32 { 1 }\n"
            "int main(){ return sizeof(struct S); }\n")
        self.assertNotIn("_crust_unit", out)


class TestRpythonInclude(unittest.TestCase):
    """`#include "foo.py"` -- rpython modules lowered by tools/py2c.py."""

    KERNEL = (
        "def triple(n: int) -> int:\n"
        "    return n * 3\n"
    )

    def test_pure_kernel_is_callable_from_c(self):
        self.assertEqual(_run(
            '#include "k.py"\nint main(void) { return triple(14); }\n',
            extra={"k.py": self.KERNEL}), 42)

    def test_pure_kernel_is_callable_from_rust(self):
        self.assertEqual(_run(
            '#include "k.py"\n'
            'fn go() -> i32 { triple(14) }\n'
            'int main(void) { return go(); }\n',
            extra={"k.py": self.KERNEL}), 42)

    def test_runtime_module_links(self):
        # Uses lists/strings, so py2c's output needs shivyc_rt.c on the link
        # line; the include hook has to arrange that on its own.
        mod = (
            'def joined(n: int) -> str:\n'
            '    parts: "list[str]" = []\n'
            '    i = 0\n'
            '    while i < n:\n'
            '        parts.append("x")\n'
            '        i += 1\n'
            '    return ",".join(parts)\n'
        )
        self.assertEqual(_run(
            '#include "m.py"\n'
            'int main(void) { return (int)strlen(joined(4)); }\n',
            extra={"m.py": mod}), 7)          # "x,x,x,x"

    def test_cache_is_keyed_on_source_text(self):
        import shivyc.rpyinc as rpyinc
        a = rpyinc.cache_key("def f(n: int) -> int:\n    return n\n")
        b = rpyinc.cache_key("def f(n: int) -> int:\n    return n + 1\n")
        self.assertNotEqual(a, b)
        self.assertEqual(a, rpyinc.cache_key(
            "def f(n: int) -> int:\n    return n\n"))

    def test_all_three_languages_in_one_unit(self):
        self.assertEqual(_run(
            '#include "k.py"\n'
            '#include "v.rs"\n'
            'fn combined() -> i32 { triple(Pair::sum(&Pair { a: 3, b: 5 })) }\n'
            'int main(void) { return combined() + 18; }\n',
            extra={"k.py": self.KERNEL,
                   "v.rs": "struct Pair { a: i32, b: i32 }\n"
                           "impl Pair {\n"
                           "    fn sum(&self) -> i32 { self.a + self.b }\n"
                           "}\n"}), 42)


class TestCrustUnsafe(unittest.TestCase):
    """`unsafe { }` blocks -- the most common blocker in real Rust source."""

    def test_unsafe_block_as_a_statement(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut s: i32 = 0;
    unsafe {
        s = 42;
    }
    s
}
""", suffix=".rs"), 42)

    def test_unsafe_block_as_an_expression(self):
        self.assertEqual(_run("""
fn get(p: *const i32) -> i32 {
    let v: i32 = unsafe { p[1] };
    v
}
fn main() -> i32 {
    let a: [i32; 2] = [1, 42];
    get(&a[0])
}
""", suffix=".rs"), 42)

    def test_unsafe_block_can_return(self):
        self.assertEqual(_run("""
fn pick() -> i32 {
    unsafe {
        42
    }
}
fn main() -> i32 { pick() }
""", suffix=".rs"), 42)

    def test_unsafe_fn_still_parses(self):
        c = crust.translate("unsafe fn f(a: i32) -> i32 { a }")
        self.assertIn("int f(int a)", c)


class TestCrustGenerics(unittest.TestCase):
    """Generics, monomorphised like Option/Result rather than boxed."""

    def test_generic_fn_infers_from_arguments(self):
        self.assertEqual(_run("""
fn id<T>(x: T) -> T { x }
fn main() -> i32 { id(42) }
""", suffix=".rs"), 42)

    def test_turbofish_selects_the_instantiation(self):
        c = crust.translate("fn id<T>(x: T) -> T { x }\n"
                            "fn f() -> i32 { id::<i32>(1) }")
        self.assertIn("id_int", c)

    def test_one_instantiation_per_type(self):
        c = crust.translate("""
fn id<T>(x: T) -> T { x }
fn f() -> i32 { id(1) }
fn g() -> f64 { id(1.0) }
fn h() -> i32 { id(2) }
""")
        self.assertIn("int id_int(int x)", c)
        self.assertIn("double id_double(double x)", c)
        # `id(1)` and `id(2)` share one instantiation.
        self.assertEqual(c.count("int id_int(int x)"), 1)

    def test_unused_generic_emits_nothing(self):
        c = crust.translate("fn unused<T>(x: T) -> T { x }\n"
                            "fn f() -> i32 { 1 }")
        self.assertNotIn("unused", c)

    def test_generic_struct_and_impl(self):
        self.assertEqual(_run("""
struct Pair<T> { a: T, b: T }
impl<T> Pair<T> {
    fn sum(&self) -> T { self.a + self.b }
}
fn main() -> i32 {
    let p: Pair<i32> = Pair { a: 40, b: 2 };
    p.sum()
}
""", suffix=".rs"), 42)

    def test_two_instantiations_are_distinct_types(self):
        c = crust.translate("""
struct Wrap<T> { v: T }
fn f() { let a: Wrap<i32> = Wrap { v: 1 }; let b: Wrap<f64> = Wrap { v: 1.0 }; }
""")
        self.assertIn("struct Wrap_int { int v; }", c)
        self.assertIn("struct Wrap_double { double v; }", c)

    def test_generic_struct_stays_a_plain_c_struct(self):
        # The point of monomorphising rather than boxing: an instantiation is
        # an ordinary struct C can build and pass with no conversion.
        c = crust.translate("struct Wrap<T> { v: T }\n"
                            "fn f() { let a: Wrap<i32> = Wrap { v: 1 }; }")
        self.assertNotIn("obj", c)
        self.assertIn("struct Wrap_int { int v; };", c)

    def test_associated_fn_via_turbofish(self):
        self.assertEqual(_run("""
struct Box2<T> { a: T, b: T }
impl<T> Box2<T> {
    fn make(a: T, b: T) -> Box2<T> { Box2 { a: a, b: b } }
    fn total(&self) -> T { self.a + self.b }
}
fn main() -> i32 {
    let p: Box2<i32> = Box2::<i32>::make(20, 22);
    p.total()
}
""", suffix=".rs"), 42)

    def test_uninferable_type_argument_is_rejected(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn make<T>() -> T { 0 }\nfn f() { make(); }")

    def test_ambiguous_generic_literal_is_rejected(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("struct W<T> { v: T }\nfn f() { let x = W { v: 1 }; }")

    def test_unknown_generic_names_the_problem(self):
        # Redox is full of these: a generic Crust has no source for. `Vec` is
        # no longer one, since the bundled core supplies it -- but a std type
        # core does not carry still is.
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("fn f(v: BTreeMap<i32>) -> i32 { 0 }")
        self.assertIn("no definition for generic type", str(cm.exception))

    def test_wrong_arity_is_rejected(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("struct P<T> { v: T }\n"
                            "fn f() { let x: P<i32, i32> = P { v: 1 }; }")


class TestCrustCore(unittest.TestCase):
    """The bundled minimal core: Vec<T>, Box<T>, Cell<T>, PhantomData<T>."""

    def test_vec_push_and_get(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut v: Vec<i32> = Vec::<i32>::new();
    v.push(40);
    v.push(2);
    let r: i32 = v.get(0) + v.get(1);
    v.free_buf();
    r
}
""", suffix=".rs"), 42)

    def test_vec_grows(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut v: Vec<i32> = Vec::<i32>::new();
    for i in 0..100 {
        v.push(i);
    }
    let n: i32 = v.len() as i32;
    v.free_buf();
    n - 58
}
""", suffix=".rs"), 42)

    def test_box_roundtrip(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut b: Box<i32> = Box::<i32>::new(42);
    let v: i32 = b.get();
    b.free_box();
    v
}
""", suffix=".rs"), 42)

    def test_vec_is_a_plain_c_struct(self):
        # The whole point: no boxing, so C can read it directly.
        c = crust.translate("fn f() { let v: Vec<i32> = Vec::<i32>::new(); }")
        self.assertIn("struct Vec_int { int *ptr;", c)

    def test_two_element_types_are_distinct(self):
        c = crust.translate("""
fn f() { let a: Vec<i32> = Vec::<i32>::new(); }
fn g() { let b: Vec<f64> = Vec::<f64>::new(); }
""")
        self.assertIn("struct Vec_int", c)
        self.assertIn("struct Vec_double", c)

    def test_unused_core_emits_nothing(self):
        # Seeding must be free for a unit that never mentions core.
        c = crust.translate("fn f() -> i32 { 1 }")
        self.assertNotIn("Vec", c)
        self.assertNotIn("malloc", c)

    def test_local_definition_wins(self):
        c = crust.translate("""
struct Vec<T> { only: T }
fn f() { let v: Vec<i32> = Vec { only: 1 }; }
""")
        self.assertIn("struct Vec_int { int only; };", c)
        self.assertNotIn("cap", c)

    def test_size_of_intrinsic(self):
        c = crust.translate("fn f() -> usize { size_of::<f64>() }")
        self.assertIn("sizeof(double)", c)

    def test_auto_ref_materialises_a_temporary(self):
        # `a.f().g()` -- the receiver is a value, so C cannot take its
        # address; Crust must bind it to a temporary rather than emit `&f()`.
        c = crust.translate("""
struct P { v: i32 }
impl P {
    fn make() -> P { P { v: 1 } }
    fn get(&self) -> i32 { self.v }
}
fn f() -> i32 { P::make().get() }
""")
        self.assertNotIn("&P_make()", c)
        self.assertIn("P_make()", c)


class TestCrustMacros(unittest.TestCase):
    """Built-in macros and `macro_rules!`."""

    def test_println_picks_specs_from_types(self):
        c = crust.translate("""
fn f() {
    let i: i32 = 1;
    let d: f64 = 1.0;
    let s: &str = "x";
    println!("{} {} {}", i, d, s);
}
""")
        self.assertIn('"%d %g %s\\n"', c)

    def test_braces_and_percent_are_escaped(self):
        c = crust.translate('fn f() { println!("{{}} 50%"); }')
        self.assertIn('"{} 50%%\\n"', c)

    def test_format_hint_overrides_the_type(self):
        c = crust.translate("fn f() { let n: i32 = 1; println!(\"{:x}\", n); }")
        self.assertIn("%x", c)

    def test_assert_aborts_on_failure(self):
        self.assertEqual(_run("fn main() -> i32 { assert!(1 > 2); 0 }",
                              suffix=".rs"), -6)      # SIGABRT

    def test_assert_passes_quietly(self):
        self.assertEqual(_run("""
fn main() -> i32 { assert!(1 < 2); assert_eq!(2, 2); assert_ne!(2, 3); 42 }
""", suffix=".rs"), 42)

    def test_debug_assert_is_compiled_out(self):
        self.assertEqual(_run(
            "fn main() -> i32 { debug_assert!(1 > 2); 42 }",
            suffix=".rs"), 42)

    def test_cfg_is_false(self):
        self.assertEqual(_run("""
fn main() -> i32 { if cfg!(feature = "nope") { 1 } else { 42 } }
""", suffix=".rs"), 42)

    def test_unsupported_macro_is_named(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate('fn f() { let v = format!("{}", 1); }')
        self.assertIn("format", str(cm.exception))

    def test_macro_rules_single_arg(self):
        self.assertEqual(_run("""
macro_rules! square { ($x:expr) => { ($x) * ($x) }; }
fn main() -> i32 { square!(6) + 6 }
""", suffix=".rs"), 42)

    def test_macro_rules_two_args(self):
        self.assertEqual(_run("""
macro_rules! maxof { ($a:expr, $b:expr) => { if $a > $b { $a } else { $b } }; }
fn main() -> i32 { maxof!(11, 42) }
""", suffix=".rs"), 42)

    def test_macro_rules_no_args(self):
        self.assertEqual(_run("""
macro_rules! answer { () => { 42 }; }
fn main() -> i32 { answer!() }
""", suffix=".rs"), 42)

    def test_macro_rules_picks_the_matching_rule(self):
        self.assertEqual(_run("""
macro_rules! pick {
    () => { 1 };
    ($a:expr) => { 40 };
    ($a:expr, $b:expr) => { $a + $b };
}
fn main() -> i32 { pick!() + pick!(9) + pick!(1, 0) }
""", suffix=".rs"), 42)

    def test_macro_argument_is_a_full_expression(self):
        # The capture must not stop at the comma inside `g(1, 2)`.
        self.assertEqual(_run("""
macro_rules! twice { ($x:expr) => { ($x) + ($x) }; }
fn g(a: i32, b: i32) -> i32 { a + b }
fn main() -> i32 { twice!(g(1, 20)) }
""", suffix=".rs"), 42)

    def test_no_matching_rule_is_reported(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("macro_rules! one { ($a:expr) => { $a }; }\n"
                            "fn f() -> i32 { one!(1, 2) }")

    def test_macro_definition_emits_no_c(self):
        c = crust.translate("macro_rules! unused { () => { 1 }; }\n"
                            "fn f() -> i32 { 2 }")
        self.assertNotIn("unused", c)


class TestCrustPaths(unittest.TestCase):
    """Qualified paths in type position."""

    def test_std_path_finds_the_bundled_type(self):
        c = crust.translate("fn f() { let b: alloc::boxed::Box<i32> = "
                            "Box::<i32>::new(1); }")
        self.assertIn("Box_int", c)

    def test_flattened_local_path_wins(self):
        c = crust.translate("struct mod_P { v: i32 }\n"
                            "fn f(p: *const mod::P) -> i32 { p.v }")
        self.assertIn("mod_P *p", c)

    def test_last_segment_fallback(self):
        c = crust.translate("struct P { v: i32 }\n"
                            "fn f(p: *const self::P) -> i32 { p.v }")
        self.assertIn("P *p", c)

    def test_c_const_pointer_is_not_read_as_a_const_item(self):
        # `*const self::P` -- `::` means a path, not a `NAME: type` annotation.
        c = crust.translate("struct P { v: i32 }\n"
                            "fn f(p: *const self::P) -> i32 { p.v }")
        self.assertIn("int f(P *p)", c)


class TestCrustTuples(unittest.TestCase):
    """Tuple types and expressions, monomorphised like slices."""

    def test_tuple_return_and_field_access(self):
        self.assertEqual(_run("""
fn divmod(a: i32, b: i32) -> (i32, i32) { (a / b, a % b) }
fn main() -> i32 {
    let d: (i32, i32) = divmod(47, 5);
    d.0 * 4 + d.1 * 3
}
""", suffix=".rs"), 42)

    def test_mixed_element_types(self):
        c = crust.translate("fn f() { let t: (i32, f64) = (1, 2.0); }")
        self.assertIn("struct crust_tuple_int_double { int _0; double _1; }", c)

    def test_one_element_parens_is_not_a_tuple(self):
        c = crust.translate("fn f() -> i32 { let x: (i32) = 1; x }")
        self.assertNotIn("crust_tuple", c)

    def test_unit_type_still_void(self):
        c = crust.translate("fn f() -> () { }")
        self.assertIn("void f(void)", c)

    def test_distinct_shapes_are_distinct_structs(self):
        c = crust.translate("""
fn f() { let a: (i32, i32) = (1, 2); }
fn g() { let b: (i32, f64) = (1, 2.0); }
""")
        self.assertIn("crust_tuple_int_int", c)
        self.assertIn("crust_tuple_int_double", c)


class TestCrustClosures(unittest.TestCase):
    """Non-capturing closures, lifted to plain functions."""

    def test_closure_is_called(self):
        self.assertEqual(_run("""
fn main() -> i32 { let f = |a: i32| a * 2; f(21) }
""", suffix=".rs"), 42)

    def test_two_parameters(self):
        self.assertEqual(_run("""
fn main() -> i32 { let f = |a: i32, b: i32| a + b; f(40, 2) }
""", suffix=".rs"), 42)

    def test_no_parameters(self):
        self.assertEqual(_run("fn main() -> i32 { let f = || 42; f() }",
                              suffix=".rs"), 42)

    def test_closure_lifts_to_a_static_function(self):
        c = crust.translate("fn f() { let g = |a: i32| a + 1; }")
        self.assertIn("static int _crust_closure1(int a)", c)

    def test_capturing_closure_is_rejected(self):
        # Crust has no environment; capturing must be an error, not a guess.
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("fn f() { let n: i32 = 1; let g = |a: i32| a + n; }")
        self.assertIn("captures", str(cm.exception))

    def test_unannotated_parameter_is_rejected(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f() { let g = |a| a + 1; }")


class TestCrustCKeywords(unittest.TestCase):
    """Rust identifiers that are C keywords."""

    def test_local_named_double_is_renamed(self):
        self.assertEqual(_run("""
fn main() -> i32 { let double: i32 = 21; double * 2 }
""", suffix=".rs"), 42)

    def test_parameter_named_int_is_renamed(self):
        self.assertEqual(_run("""
fn f(int: i32, register: i32) -> i32 { int + register }
fn main() -> i32 { f(40, 2) }
""", suffix=".rs"), 42)

    def test_loop_variable_named_short(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut t: i32 = 0;
    for short in 0..7 { t += short; }
    t * 2
}
""", suffix=".rs"), 42)


class TestCrustModuleItems(unittest.TestCase):
    """`use` / `extern crate` erasure, and the `core::ffi` types.

    These matter out of proportion to their size: they translate cleanly (the
    text passes through untouched) and only fail later in the C front end, so
    any measurement that stops at `translate()` never sees them.
    """

    def test_use_is_erased(self):
        c = crust.translate("use core::mem;\nfn f() -> i32 { 1 }")
        self.assertNotIn("use core", c)

    def test_use_with_a_brace_group_is_erased(self):
        c = crust.translate("use core::ffi::{c_int, c_char};\n"
                            "fn f() -> i32 { 1 }")
        self.assertNotIn("use core", c)

    def test_pub_use_is_erased(self):
        c = crust.translate("pub use self::inner::Thing;\n"
                            "fn f() -> i32 { 1 }")
        self.assertNotIn("use self", c)

    def test_extern_crate_is_erased(self):
        c = crust.translate("extern crate alloc;\nfn f() -> i32 { 1 }")
        self.assertNotIn("extern crate", c)

    def test_erasure_preserves_line_numbers(self):
        # Erasure blanks in place rather than deleting, so a diagnostic still
        # points at the line the user wrote.
        c = crust.translate("use core::mem;\nuse core::ptr;\n"
                            "fn f() -> i32 { 1 }")
        lines = c.split("\n")
        self.assertIn("int f(void) { return 1; }", lines[2])

    def test_use_compiles_end_to_end(self):
        self.assertEqual(_run("""
use core::mem;
use core::ffi::{c_int, c_char};
fn main() -> i32 { 42 }
""", suffix=".rs"), 42)

    def test_ffi_types_map_to_c(self):
        c = crust.translate("fn f(a: c_int, b: c_ulong, s: *const c_char) "
                            "-> c_int { a }")
        self.assertIn("int f(int a, unsigned long b, char *s)", c)

    def test_ffi_types_run(self):
        self.assertEqual(_run("""
use core::ffi::c_int;
fn add(a: c_int, b: c_int) -> c_int { a + b }
fn main() -> i32 { add(40, 2) }
""", suffix=".rs"), 42)

    def test_use_in_a_c_file_is_untouched(self):
        # `use` is not a C keyword, so a C identifier called `use` must
        # survive: erasure only applies to a whole `use ...;` item line.
        c = crust.translate("int use_count(int use) { return use; }\n"
                            "fn f() -> i32 { 1 }")
        self.assertIn("int use_count(int use)", c)


class TestCrustModuleItemForms(unittest.TestCase):
    """The `use` / `mod` / visibility forms real Rust actually writes."""

    def test_nested_brace_group_is_erased(self):
        # A regex cannot balance these; the scanner must.
        c = crust.translate("use core::{cell::Cell, ops::{Deref, DerefMut}};\n"
                            "fn f() -> i32 { 1 }")
        self.assertNotIn("Deref", c)

    def test_multiline_use_is_erased(self):
        c = crust.translate("use crate::{\n    a::b,\n    c::d,\n};\n"
                            "fn f() -> i32 { 1 }")
        self.assertNotIn("a::b", c)

    def test_mod_declaration_is_erased(self):
        c = crust.translate("pub mod aligned_box;\nfn f() -> i32 { 1 }")
        self.assertNotIn("aligned_box", c)

    def test_inline_module_is_not_erased(self):
        # `mod x { .. }` has contents; only the bare declaration is erased.
        c = crust.translate("mod x { }\nfn f() -> i32 { 1 }")
        self.assertIn("mod x", c)

    def test_use_inside_a_string_survives(self):
        c = crust.translate('fn f() -> &str { "use core::mem;" }')
        self.assertIn("use core::mem;", c)

    def test_find_mod_decls(self):
        names = crust.find_mod_decls(
            "pub mod types;\nmod helpers;\npub(crate) mod io;\n"
            "mod inline { }\nfn f() -> i32 { 1 }\n")
        self.assertEqual(names, ["types", "helpers", "io"])

    def test_find_mod_decls_ignores_comments_and_strings(self):
        self.assertEqual(
            crust.find_mod_decls('// mod ghost;\nfn f() -> i32 { 1 }\n'), [])
        self.assertEqual(
            crust.find_mod_decls('fn f() -> &str { "mod ghost;" }\n'), [])

    def test_resolve_mod_path_rs_then_mod_rs(self):
        work = tempfile.mkdtemp()
        try:
            with open(os.path.join(work, "types.rs"), "w") as f:
                f.write("struct T { x: i32 }\n")
            os.makedirs(os.path.join(work, "helpers"))
            with open(os.path.join(work, "helpers", "mod.rs"), "w") as f:
                f.write("struct H { y: i32 }\n")
            parent = os.path.join(work, "user.rs")
            with open(parent, "w") as f:
                f.write("mod types;\n")
            self.assertEqual(
                crust.resolve_mod_path("types", parent),
                os.path.join(work, "types.rs"))
            self.assertEqual(
                crust.resolve_mod_path("helpers", parent),
                os.path.join(work, "helpers", "mod.rs"))
            self.assertIsNone(crust.resolve_mod_path("missing", parent))
        finally:
            for root, dirs, files in os.walk(work, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                for name in dirs:
                    os.rmdir(os.path.join(root, name))
            os.rmdir(work)


class TestCrustModSiblingTypes(unittest.TestCase):
    """`mod name;` seeds type definitions from sibling .rs files."""

    def test_translate_sees_sibling_struct_fields(self):
        work = tempfile.mkdtemp()
        try:
            with open(os.path.join(work, "types.rs"), "w") as f:
                f.write("pub struct Foo { x: i32 }\n")
            user = os.path.join(work, "user.rs")
            with open(user, "w") as f:
                f.write("mod types;\n"
                        "fn f(p: Foo) -> i32 { p.x }\n")
            with open(user) as f:
                src = f.read()
            c = crust.translate(src, path=user)
            self.assertIn("struct Foo", c)
            self.assertIn("int x", c)
            self.assertIn("return p.x;", c)
            self.assertNotIn("mod types", c)
        finally:
            for name in ("types.rs", "user.rs"):
                try:
                    os.remove(os.path.join(work, name))
                except OSError:
                    pass
            os.rmdir(work)

    def test_sibling_struct_compiles(self):
        self.assertEqual(_run("""
mod types;
fn main() -> i32 {
    let p: Foo = Foo { x: 40 };
    p.x + 2
}
""", suffix=".rs", extra={
            "types.rs": "pub struct Foo { x: i32 }\n",
        }), 42)

    def test_sibling_enum_compiles(self):
        self.assertEqual(_run("""
mod kinds;
fn main() -> i32 {
    match Kind::B {
        Kind::A => 1,
        Kind::B => 42,
    }
}
""", suffix=".rs", extra={
            "kinds.rs": "pub enum Kind { A, B }\n",
        }), 42)

    def test_mod_rs_layout(self):
        work = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(work, "types"))
            with open(os.path.join(work, "types", "mod.rs"), "w") as f:
                f.write("pub struct Foo { x: i32 }\n")
            user = os.path.join(work, "user.rs")
            src = ("mod types;\n"
                   "fn f(p: Foo) -> i32 { p.x }\n")
            with open(user, "w") as f:
                f.write(src)
            c = crust.translate(src, path=user)
            self.assertIn("struct Foo { int x; }", c)
        finally:
            for root, dirs, files in os.walk(work, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                for name in dirs:
                    os.rmdir(os.path.join(root, name))
            os.rmdir(work)

    def test_compile_object_with_mod_types(self):
        """crustos-style per-file `-c` succeeds when types come from a mod."""
        work = tempfile.mkdtemp()
        try:
            with open(os.path.join(work, "types.rs"), "w") as f:
                f.write("pub struct Foo { x: i32 }\n")
            user = os.path.join(work, "user.rs")
            with open(user, "w") as f:
                f.write("mod types;\n"
                        "fn use_foo(p: Foo) -> i32 { p.x }\n")
            obj = os.path.join(work, "user.o")
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            proc = subprocess.run(
                [sys.executable, "-m", "shivyc.main", "-c", user, "-o", obj],
                cwd=root, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertTrue(os.path.exists(obj))
        finally:
            import shutil
            shutil.rmtree(work, ignore_errors=True)


class TestCrustTypeAliases(unittest.TestCase):
    """`type Name = T;` as a typedef."""

    def test_local_alias_translates(self):
        c = crust.translate("type pid_t = i32;\nfn f(p: pid_t) -> pid_t { p }\n")
        self.assertIn("typedef int pid_t;", c)
        self.assertIn("int f(int p)", c)

    def test_pub_alias(self):
        c = crust.translate("pub type ssize_t = i64;\nfn f() -> i32 { 1 }\n")
        self.assertIn("typedef long ssize_t;", c)

    def test_sibling_alias_compiles(self):
        self.assertEqual(_run("""
mod types;
fn main() -> i32 {
    let p: pid_t = 40;
    p + 2
}
""", suffix=".rs", extra={
            "types.rs": "pub type pid_t = i32;\n",
        }), 42)

    def test_alias_to_struct(self):
        self.assertEqual(_run("""
type Handle = Foo;
struct Foo { x: i32 }
fn main() -> i32 {
    let h: Handle = Foo { x: 42 };
    h.x
}
""", suffix=".rs"), 42)


class TestCrustOpaquePaths(unittest.TestCase):
    """Qualified path types with no definition become incomplete structs."""

    def test_path_type_is_forward_declared(self):
        c = crust.translate(
            "fn f(p: &crate::percpu::PercpuBlock) { }\n")
        self.assertIn("struct crate_percpu_PercpuBlock;", c)
        self.assertIn("typedef struct crate_percpu_PercpuBlock "
                      "crate_percpu_PercpuBlock;", c)
        self.assertIn("crate_percpu_PercpuBlock *", c)

    def test_path_type_compiles(self):
        self.assertEqual(_run("""
fn take(_p: &crate::sync::Token) { }
fn main() -> i32 {
    // Never constructed; only the incomplete type is needed for -c.
    42
}
""", suffix=".rs"), 42)


class TestCrustExternPathCalls(unittest.TestCase):
    """Unknown path calls get an extern prototype for per-file -c."""

    def test_path_call_emits_extern(self):
        c = crust.translate(
            "fn init() { rmm::aarch64::init_mair(); }\n")
        self.assertIn("extern void rmm_aarch64_init_mair(void);", c)
        self.assertIn("rmm_aarch64_init_mair();", c)

    def test_path_call_compiles(self):
        # -c only; the symbol is undefined at link time.
        work = tempfile.mkdtemp()
        try:
            src = os.path.join(work, "paging.rs")
            with open(src, "w") as f:
                f.write("fn init() { rmm::aarch64::init_mair(); }\n")
            obj = os.path.join(work, "paging.o")
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            proc = subprocess.run(
                [sys.executable, "-m", "shivyc.main", "-c", src, "-o", obj],
                cwd=root, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        finally:
            import shutil
            shutil.rmtree(work, ignore_errors=True)


class TestCrustAtomics(unittest.TestCase):
    """Bundled AtomicU32 / AtomicUsize stubs."""

    def test_atomic_static_init(self):
        c = crust.translate(
            "const N: u32 = !0;\n"
            "static LOCK: AtomicU32 = AtomicU32::new(N);\n"
            "fn f() -> i32 { 1 }\n")
        self.assertIn("struct AtomicU32", c)
        self.assertIn("static AtomicU32 LOCK = { N };", c)
        self.assertIn("#define N 0xFFFFFFFFu", c)

    def test_atomic_static_runs(self):
        self.assertEqual(_run("""
const NO_PROCESSOR: u32 = !0;
static LOCK_OWNER: AtomicU32 = AtomicU32::new(NO_PROCESSOR);
static LOCK_COUNT: AtomicUsize = AtomicUsize::new(0);
fn main() -> i32 { 42 }
""", suffix=".rs"), 42)

    def test_atomic_methods_run(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut a: AtomicU32 = AtomicU32::new(40);
    let prev: u32 = a.fetch_add(2, Ordering::Relaxed);
    (prev + a.load(Ordering::Relaxed)) as i32 - 40
}
""", suffix=".rs"), 42)

    def test_const_generic_struct_lowers(self):
        # Const-only generics are erased; the struct is a plain C type, and
        # `impl Foo<false>` methods still typecheck.
        c = crust.translate(
            "struct Foo<const RW: bool> { v: i32 }\n"
            "impl Foo<false> {\n"
            "    fn get() -> Self { Foo { v: 1 } }\n"
            "}\n")
        self.assertIn("struct Foo { int v; }", c)
        self.assertIn("Foo Foo_get(void)", c)


class TestCrustVisibility(unittest.TestCase):
    """`pub`, `pub(crate)`, and `pub unsafe extern "C"`."""

    def test_pub_crate_function(self):
        c = crust.translate("pub(crate) fn f() -> i32 { 1 }")
        self.assertIn("int f(void)", c)

    def test_pub_unsafe_extern_c_function(self):
        # The scan text has string literals blanked, so the modifier matcher
        # cannot look for the literal spelling of `extern "C"`.
        c = crust.translate('pub unsafe extern "C" fn go() -> i32 { 7 }')
        self.assertIn("int go(void)", c)

    def test_pub_in_path_struct(self):
        c = crust.translate("pub(in crate::x) struct S { v: i32 }\n"
                            "fn f() -> i32 { 1 }")
        self.assertIn("struct S { int v; }", c)

    def test_visibility_forms_run(self):
        self.assertEqual(_run("""
pub(crate) fn a() -> i32 { 40 }
pub unsafe extern "C" fn b() -> i32 { 2 }
fn main() -> i32 { a() + b() }
""", suffix=".rs"), 42)


class TestCrustLifetimes(unittest.TestCase):
    """Lifetimes are dropped in the lexer; char literals are not."""

    def test_lifetime_in_generics(self):
        c = crust.translate("fn f<'a>(x: *const i32) -> i32 { 1 }")
        self.assertIn("int f(int *x)", c)

    def test_lifetime_on_a_reference(self):
        c = crust.translate("fn f<'a>(x: &'a i32) -> i32 { 1 }")
        self.assertIn("int f(int *x)", c)

    def test_char_literal_still_works(self):
        self.assertEqual(_run("""
fn main() -> i32 { let c: char = 'A'; (c as i32) - 23 }
""", suffix=".rs"), 42)

    def test_escaped_char_literal(self):
        self.assertEqual(_run("""
fn main() -> i32 { let c: char = '\\n'; (c as i32) + 32 }
""", suffix=".rs"), 42)

    def test_lifetime_does_not_swallow_the_file(self):
        # The bug this guards: blanking `'a` as a char literal ran to the next
        # quote and destroyed the item structure, so nothing was found at all.
        c = crust.translate("struct L<'a, T> { v: *const T }\n"
                            "fn g() -> i32 { 1 }")
        self.assertIn("int g(void)", c)
