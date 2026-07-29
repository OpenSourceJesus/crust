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


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_units(units, main_unit, suffix=".rs"):
    """Compile several units separately, link them, and return the exit status.

    Every other test compiles exactly one translation unit, which is why a
    whole class of bug was invisible: a name can be emitted correctly in each
    unit on its own and still collide when two of them are linked. The
    bundled core does exactly that -- its methods go into every unit that
    names a core type.

    `units` maps a name to source; `main_unit` is the one holding `main`.
    """
    workdir = tempfile.mkdtemp()
    objs = []
    for name, text in units.items():
        src = os.path.join(workdir, name + suffix)
        with open(src, "w") as f:
            f.write(text)
        obj = os.path.join(workdir, name + ".o")
        proc = subprocess.run(
            [sys.executable, "-m", "shivyc.main", "-c", src, "-o", obj],
            cwd=_ROOT, capture_output=True, text=True)
        assert proc.returncode == 0 and os.path.exists(obj), (
            "compiling %s failed: %s" % (name, proc.stderr or proc.stdout))
        objs.append(obj)
    out = os.path.join(workdir, "prog")
    link = subprocess.run(
        [sys.executable, "-m", "shivyc.main"] + objs + ["-o", out],
        cwd=_ROOT, capture_output=True, text=True)
    assert link.returncode == 0, (
        "linking failed: %s" % (link.stderr or link.stdout))
    return subprocess.run([out]).returncode


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

    def test_data_carrying_variant_lowers_to_a_tagged_union(self):
        c = crust.translate("enum E { A(i32), B }\nfn f() -> i32 { 1 }")
        self.assertIn("enum E_tag { E_A, E_B };", c)
        self.assertIn("struct E_A_data { int _0; };", c)
        self.assertIn("union", c)

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

    def test_statements_in_arm_are_allowed(self):
        # A block used as a value may run statements before its tail
        # expression; Rust code does that constantly. They are hoisted into
        # the enclosing statement's pending list, which is emitted just
        # before it -- exactly when they should run.
        self.assertEqual(_run("""
fn main() -> i32 {
    let v: i32 = if true { let a: i32 = 6; a * 7 } else { 0 };
    v
}
""", suffix=".rs"), 42)

    def test_block_without_a_tail_value_is_an_error(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f() -> i32 { let x: i32 = { let a: i32 = 1; };"
                            " x }")

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
        # `format!` is supported now; `asm!` is deliberately not -- dropping
        # inline assembly silently would change behaviour invisibly, and a
        # kernel is exactly where that matters.
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate('fn f() { asm!("nop"); }')
        self.assertIn("asm", str(cm.exception))

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


class TestCrustItemMacros(unittest.TestCase):
    """File-scope macro invocations are erased; calls inside bodies are not."""

    def test_global_asm_is_erased(self):
        c = crust.translate('global_asm!(\n    "\n    .global x\n"\n);\n'
                            'fn f() -> i32 { 1 }')
        self.assertNotIn(".global", c)
        self.assertIn("int f(void)", c)

    def test_item_macro_with_braces_is_erased(self):
        c = crust.translate("int_like!{ Foo, FooAtomic, usize, AtomicUsize }\n"
                            "fn f() -> i32 { 1 }")
        self.assertNotIn("int_like", c)

    def test_macro_call_inside_a_body_still_expands(self):
        # The erasure must be file-scope only.
        c = crust.translate('fn f() { let n: i32 = 1; println!("{}", n); }')
        self.assertIn("printf", c)

    def test_erasure_preserves_lines(self):
        c = crust.translate('global_asm!("x");\nfn f() -> i32 { 1 }')
        self.assertIn("int f(void) { return 1; }", c.split("\n")[1])


class TestCrustUsePathSeeding(unittest.TestCase):
    """Types are seeded across a crate by following `use crate::a::b`."""

    def _crate(self, files):
        import tempfile
        root = tempfile.mkdtemp(prefix="crust_crate_")
        for rel, text in files.items():
            full = os.path.join(root, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(text)
        return root

    def test_use_path_seeds_a_type_alias(self):
        root = self._crate({
            "lib.rs": "pub mod platform;\n",
            "platform/mod.rs": "pub mod types;\n",
            "platform/types.rs": "pub type pid_t = i32;\n",
            "header/mod.rs": ("use crate::platform::types::pid_t;\n"
                              "pub struct Cred { pid: pid_t }\n"),
        })
        src = open(os.path.join(root, "header/mod.rs")).read()
        c = crust.translate(src, path=os.path.join(root, "header/mod.rs"))
        self.assertIn("int pid;", c)

    def test_use_path_seeds_a_struct(self):
        root = self._crate({
            "lib.rs": "pub mod a;\n",
            "a/mod.rs": "pub struct Inner { v: i32 }\n",
            "b.rs": ("use crate::a::Inner;\n"
                     "fn get(x: *const Inner) -> i32 { x.v }\n"),
        })
        src = open(os.path.join(root, "b.rs")).read()
        c = crust.translate(src, path=os.path.join(root, "b.rs"))
        self.assertIn("int get(Inner *x)", c)

    def test_missing_use_target_is_not_fatal(self):
        c = crust.translate("use crate::nowhere::Thing;\n"
                            "fn f() -> i32 { 1 }",
                            path="/tmp/crust_no_such_crate/x.rs")
        self.assertIn("int f(void)", c)

    def test_unparsable_sibling_is_not_fatal(self):
        # Seeding must degrade, never fail the file that mentioned it.
        root = self._crate({
            "lib.rs": "pub mod broken;\n",
            "broken.rs": "pub struct Bad { ",
            "user.rs": "use crate::broken::Bad;\nfn f() -> i32 { 1 }\n",
        })
        src = open(os.path.join(root, "user.rs")).read()
        c = crust.translate(src, path=os.path.join(root, "user.rs"))
        self.assertIn("int f(void)", c)


class TestCrustExternGuessing(unittest.TestCase):
    """Prototype guessing is limited to genuine path calls."""

    def test_bare_unknown_call_gets_no_guess(self):
        # It may be declared by text pulled in later by `#include`, which the
        # preprocessor expands only after translation.
        c = crust.translate("fn f() -> i32 { labels(4) }")
        self.assertNotIn("extern void labels", c)

    def test_path_call_still_gets_a_prototype(self):
        c = crust.translate("fn f() { rmm::aarch64::init_mair(); }")
        self.assertIn("rmm_aarch64_init_mair", c)


class TestCrustDataEnums(unittest.TestCase):
    """Data-carrying enum variants, lowered to a tagged union."""

    def test_tuple_variant_roundtrip(self):
        self.assertEqual(_run("""
enum E { N(i32), Nothing }
fn get(e: E) -> i32 {
    match e {
        E::N(v) => v,
        E::Nothing => 0,
    }
}
fn main() -> i32 { get(E::N(42)) }
""", suffix=".rs"), 42)

    def test_struct_variant_roundtrip(self):
        self.assertEqual(_run("""
enum Shape { Rect { w: i32, h: i32 }, Empty }
fn area(s: Shape) -> i32 {
    match s {
        Shape::Rect { w, h } => w * h,
        Shape::Empty => 0,
    }
}
fn main() -> i32 { area(Shape::Rect { w: 6, h: 7 }) }
""", suffix=".rs"), 42)

    def test_multiple_payload_fields(self):
        self.assertEqual(_run("""
enum P { Two(i32, i32), None2 }
fn sum(p: P) -> i32 {
    match p {
        P::Two(a, b) => a + b,
        P::None2 => 0,
    }
}
fn main() -> i32 { sum(P::Two(40, 2)) }
""", suffix=".rs"), 42)

    def test_payload_free_variant_is_a_whole_value(self):
        # `E::B` must build `(E){.tag = E_B}`, not a bare tag constant.
        c = crust.translate("enum E { A(i32), B }\n"
                            "fn f() -> E { E::B }")
        self.assertIn("(E){.tag = E_B}", c)

    def test_field_renaming_in_a_struct_pattern(self):
        self.assertEqual(_run("""
enum S { P { x: i32 }, Q }
fn get(s: S) -> i32 {
    match s {
        S::P { x: got } => got,
        S::Q => 0,
    }
}
fn main() -> i32 { get(S::P { x: 42 }) }
""", suffix=".rs"), 42)

    def test_underscore_binding_is_not_declared(self):
        c = crust.translate("""
enum E { A(i32), B }
fn f(e: E) -> i32 { match e { E::A(_) => 1, E::B => 2 } }
""")
        self.assertNotIn("int _ =", c)

    def test_binding_does_not_leak_between_arms(self):
        # Each arm's bindings live in their own block.
        self.assertEqual(_run("""
enum E { A(i32), B(i32) }
fn f(e: E) -> i32 { match e { E::A(v) => v, E::B(v) => v * 2 } }
fn main() -> i32 { f(E::B(21)) }
""", suffix=".rs"), 42)

    def test_scrutinee_is_evaluated_once(self):
        c = crust.translate("""
enum E { A(i32), B }
fn make() -> E { E::B }
fn f() -> i32 { match make() { E::A(v) => v, E::B => 0 } }
""")
        # The prototype spells it `make(void)`, so the single remaining
        # `make()` is the one call site: the scrutinee is bound to a temporary
        # rather than re-evaluated per arm.
        self.assertEqual(c.count("make()"), 1)

    def test_exhaustiveness_still_checked(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("enum E { A(i32), B }\n"
                            "fn f(e: E) -> i32 { match e { E::A(v) => v } }")

    def test_wrong_arity_is_rejected(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("enum E { A(i32), B }\nfn f() -> E { E::A(1, 2) }")

    def test_destructuring_a_dataless_variant_is_rejected(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("enum E { A(i32), B }\n"
                            "fn f(e: E) -> i32 { match e { E::A(v) => v, "
                            "E::B(x) => x } }")


class TestCrustDerive(unittest.TestCase):
    """`#[derive(..)]` generates the same methods a hand-written impl would."""

    def test_clone_returns_a_copy(self):
        self.assertEqual(_run("""
#[derive(Clone, Copy)]
struct P { x: i32, y: i32 }
fn main() -> i32 { let a: P = P { x: 40, y: 2 }; let b: P = a.clone();
                   b.x + b.y }
""", suffix=".rs"), 42)

    def test_partial_eq_compares_fields(self):
        self.assertEqual(_run("""
#[derive(PartialEq)]
struct P { x: i32, y: i32 }
fn main() -> i32 {
    let a: P = P { x: 1, y: 2 };
    let b: P = P { x: 1, y: 2 };
    let c: P = P { x: 9, y: 2 };
    if a.eq(&b) { if a.eq(&c) { 0 } else { 42 } } else { 0 }
}
""", suffix=".rs"), 42)

    def test_default_zeroes(self):
        self.assertEqual(_run("""
#[derive(Default)]
struct P { x: i32, y: i32 }
fn main() -> i32 { let p: P = P::default(); 42 + p.x + p.y }
""", suffix=".rs"), 42)

    def test_debug_prints_fields(self):
        c = crust.translate("#[derive(Debug)]\nstruct P { x: i32 }\n"
                            "fn f(p: *const P) { p.debug(); }")
        self.assertIn("P_debug", c)
        self.assertIn("x: %d", c)

    def test_copy_and_eq_are_markers(self):
        c = crust.translate("#[derive(Copy, Eq)]\nstruct P { x: i32 }\n"
                            "fn f() -> i32 { 1 }")
        self.assertNotIn("P_copy", c)
        self.assertNotIn("P_eq", c)

    def test_hand_written_impl_wins(self):
        self.assertEqual(_run("""
#[derive(Clone)]
struct P { x: i32 }
impl P { fn clone(&self) -> P { P { x: 42 } } }
fn main() -> i32 { let a: P = P { x: 1 }; a.clone().x }
""", suffix=".rs"), 42)

    def test_non_scalar_field_skips_derivation(self):
        # A nested struct cannot be compared with `==`, and an unknown type
        # cannot be printed. Skipping is the only safe answer.
        c = crust.translate("""
struct Inner { v: i32 }
#[derive(PartialEq, Debug)]
struct Outer { i: Inner }
fn f() -> i32 { 1 }
""")
        self.assertNotIn("Outer_eq", c)

    def test_unknown_derive_is_ignored(self):
        c = crust.translate("#[derive(Hash, Serialize)]\nstruct P { x: i32 }\n"
                            "fn f() -> i32 { 1 }")
        self.assertIn("int f(void)", c)

    def test_derive_on_a_data_enum_is_accepted(self):
        # Accepted so the attribute does not fail the file; nothing generated.
        c = crust.translate("#[derive(Clone)]\nenum E { A(i32), B }\n"
                            "fn f() -> i32 { 1 }")
        self.assertIn("enum E_tag", c)


class TestOddSizedStructReturn(unittest.TestCase):
    """Returning a struct whose size is not a register width.

    SysV returns a struct of 3, 5, 6 or 7 bytes in a full eightbyte of RAX,
    but the backend was moving exactly `size` bytes and raising
    `NotImplementedError` from inside register naming -- a crash rather than a
    diagnostic. Any function returning such a struct hit it; `#[derive(Clone)]`
    just made it easy to reach.
    """

    def _sz(self, fields):
        return _run("struct S { %s };\n"
                    "struct S f(struct S *p) { return *p; }\n"
                    "int main(void) { struct S s; s.a = 42;"
                    " struct S r = f(&s); return r.a; }" % fields)

    def test_three_byte_struct(self):
        self.assertEqual(self._sz("char a; char b; char c;"), 42)

    def test_five_byte_struct(self):
        self.assertEqual(self._sz("char a; char b; char c; char d; char e;"),
                         42)

    def test_six_byte_struct(self):
        self.assertEqual(self._sz("int a; short b;"), 42)

    def test_ten_byte_struct(self):
        self.assertEqual(self._sz("int a; int b; short c;"), 42)

    def test_register_sized_still_works(self):
        self.assertEqual(self._sz("int a; int b;"), 42)


class TestNoInternalCrashes(unittest.TestCase):
    """A valid program must never produce a Python traceback.

    `tools/crustfuzz.py` probes these systematically; the cases pinned here
    are the ones it found, kept so they cannot regress silently.
    """

    def _compile(self, src):
        import subprocess, tempfile, os as _os
        d = tempfile.mkdtemp()
        p = _os.path.join(d, "t.c")
        with open(p, "w") as f:
            f.write(src)
        return subprocess.run(
            [sys.executable, "-m", "shivyc.main", "-c", p,
             "-o", _os.path.join(d, "t.o")],
            cwd=_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
            capture_output=True, text=True)

    def test_odd_struct_arg_passes_correctly(self):
        # Was a crash, then a reported limitation, now supported: the store
        # into the parameter's home is split into 2+1 byte chunks.
        self.assertEqual(_run("struct S { char a; char b; char c; };\n"
                              "int f(struct S s) { return s.a*100 + s.b*10"
                              " + s.c; }\n"
                              "int main(void) { struct S s; s.a=1; s.b=2;"
                              " s.c=3; return f(s); }"), 123)

    def test_seven_byte_struct_arg_passes_correctly(self):
        self.assertEqual(_run("struct S { char v[7]; };\n"
                              "int f(struct S s) { return s.v[0] + s.v[6]; }\n"
                              "int main(void) { struct S s; s.v[0]=40;"
                              " s.v[6]=2; return f(s); }"), 42)

    def test_five_and_six_byte_struct_args(self):
        self.assertEqual(_run("struct S { char a; char b; char c; char d;"
                              " char e; };\n"
                              "int f(struct S s) { return s.a + s.e; }\n"
                              "int main(void) { struct S s; s.a=40; s.e=2;"
                              " return f(s); }"), 42)

    def test_no_traceback_on_any_struct_size(self):
        for body in ("char a; char b; char c;", "char v[7];",
                     "int a; char b;", "long a; char b;"):
            r = self._compile("struct S { %s };\n"
                              "int f(struct S s) { return 0; }\n"
                              "int main(void) { struct S s; return f(s); }"
                              % body)
            self.assertNotIn("Traceback", r.stderr, body)

    def test_odd_struct_return_still_works(self):
        # Returning one *is* supported -- only passing by value is not.
        self.assertEqual(_run("struct S { char a; char b; char c; };\n"
                              "struct S f(struct S *p) { return *p; }\n"
                              "int main(void) { struct S s; s.a = 42;"
                              " struct S r = f(&s); return r.a; }"), 42)

    def test_register_sized_struct_arg_still_works(self):
        self.assertEqual(_run("struct S { int a; int b; };\n"
                              "int f(struct S s) { return s.a + s.b; }\n"
                              "int main(void) { struct S s; s.a = 40; s.b = 2;"
                              " return f(s); }"), 42)


class TestCrustPyList(unittest.TestCase):
    """`PyList<T>` -- the list an included rpython module builds.

    Its layout must match what `tools/py2c.py` emits for a typed list
    (`struct _tlist_int { int* data; long len; long cap; }`), because the two
    are passed to each other by a pointer cast with no conversion.
    """

    def test_layout_matches_py2c(self):
        c = crust.translate("fn f(p: *mut PyList<i32>) -> i64 { p.len() }")
        self.assertIn("struct PyList_int { int *data; long len; long cap; }", c)

    def test_element_type_is_monomorphised(self):
        c = crust.translate("""
fn a(p: *mut PyList<i32>) -> i64 { p.len() }
fn b(p: *mut PyList<f64>) -> i64 { p.len() }
""")
        self.assertIn("struct PyList_int", c)
        self.assertIn("struct PyList_double", c)

    def test_for_each_over_a_pylist_pointer(self):
        c = crust.translate("""
fn total(xs: *mut PyList<i32>) -> i32 {
    let mut s: i32 = 0;
    for x in xs { s += x; }
    s
}
""")
        self.assertIn("->data[", c)
        self.assertIn("->len", c)

    def test_for_each_over_a_pylist_value(self):
        c = crust.translate("""
fn total(xs: PyList<i32>) -> i32 {
    let mut s: i32 = 0;
    for x in xs { s += x; }
    s
}
""")
        self.assertIn(".data[", c)

    def test_plain_pointer_is_still_rejected(self):
        # Only a PyList pointer is iterable; a raw pointer has no length.
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f(p: *const i32) { for x in p { } }")

    def test_unused_pylist_costs_nothing(self):
        c = crust.translate("fn f() -> i32 { 1 }")
        self.assertNotIn("PyList", c)


class TestRpythonPrototypes(unittest.TestCase):
    """The libc prototypes a runtime-free rpython module gets."""

    def test_realloc_takes_two_arguments(self):
        # A one-argument `realloc` made any rpython module that grew a list
        # fail to compile -- and only when the module avoided the runtime, so
        # it went unnoticed until a list was passed to Rust.
        import shivyc.rpyinc as rpyinc
        protos = dict(rpyinc._LIBC_PROTOS)
        self.assertEqual(protos["realloc"],
                         "void *realloc(void *, unsigned long);")


class TestCrustArrayFields(unittest.TestCase):
    """A struct field whose type is an array.

    The Rust-vs-C struct-body test used to reject any body containing a `;`,
    on the grounds that C members end in one. A Rust array type carries a
    semicolon inside its brackets (`a: [i32; 4]`), so every struct with an
    array field read as C and was passed through untranslated -- silently,
    with no diagnostic, which is how it went unnoticed.
    """

    def test_array_field_is_translated(self):
        c = crust.translate("struct T { a: [i32; 4], n: i32 }\n"
                            "fn f() -> i32 { 1 }")
        self.assertIn("struct T { int a[4]; int n; }", c)

    def test_array_of_struct_field(self):
        c = crust.translate("struct I { v: i32 }\n"
                            "struct T { items: [I; 8], n: i32 }\n"
                            "fn f() -> i32 { 1 }")
        self.assertIn("I items[8]", c)

    def test_c_struct_is_still_c(self):
        c = crust.translate("struct C { int a; int b; };\n"
                            "fn g() -> i32 { 1 }")
        self.assertIn("struct C { int a; int b; };", c)

    def test_array_field_round_trips(self):
        self.assertEqual(_run("""
struct T { a: [i32; 4], n: i32 }
impl T {
    fn fill(&mut self) {
        for i in 0..4 { self.a[i] = i * 10; }
        self.n = 4;
    }
    fn sum(&self) -> i32 {
        let mut s: i32 = 0;
        for i in 0..self.n { s += self.a[i]; }
        s
    }
}
fn main() -> i32 { let mut t: T = T { a: [0; 4], n: 0 }; t.fill();
                   t.sum() + 12 }
""", suffix=".rs"), 72)

    def test_multi_dimensional_field(self):
        c = crust.translate("struct G { grid: [[i32; 3]; 3] }\n"
                            "fn f() -> i32 { 1 }")
        self.assertIn("grid", c)


class TestCrustResultAlias(unittest.TestCase):
    """`Result<T>` with one type argument."""

    def test_single_argument_result(self):
        c = crust.translate("fn f() -> Result<i32> { Ok(1) }")
        self.assertIn("crust_result_int_e_int", c)

    def test_alias_supplies_the_error_type(self):
        c = crust.translate("struct Error { code: i32 }\n"
                            "type Result<T> = core::result::Result<T, Error>;\n"
                            "fn f() -> Result<i32> { Ok(1) }")
        self.assertIn("crust_result_int_e_Error", c)

    def test_two_argument_form_still_works(self):
        c = crust.translate("fn f() -> Result<i32, i64> { Ok(1) }")
        self.assertIn("crust_result_int_e_long", c)

    def test_single_argument_result_runs(self):
        self.assertEqual(_run("""
fn half(n: i32) -> Result<i32> {
    if n % 2 == 0 { Ok(n / 2) } else { Err(1) }
}
fn main() -> i32 {
    let r: Result<i32> = half(84);
    if r.is_ok() { r.unwrap() } else { 0 }
}
""", suffix=".rs"), 42)


class TestCrustGenericAlias(unittest.TestCase):
    """A generic `type` alias is skipped, not fatal."""

    def test_generic_alias_does_not_fail_the_file(self):
        # Crust monomorphises, so a generic alias has nothing to bind to.
        # Failing the whole file over one would be wildly out of proportion.
        c = crust.translate("type Pair<T> = (T, T);\nfn f() -> i32 { 42 }")
        self.assertIn("int f(void) { return 42; }", c)

    def test_plain_alias_still_resolves(self):
        c = crust.translate("type Word = i64;\nfn f(x: Word) -> Word { x }")
        self.assertIn("long f(long x)", c)


class TestCrustSyncWrappers(unittest.TestCase):
    """The std wrappers the bundled core supplies."""

    def test_unsafe_cell_is_one_field(self):
        c = crust.translate("fn f(c: *mut UnsafeCell<i32>) -> i32 "
                            "{ c.read() }")
        self.assertIn("struct UnsafeCell_int { int value; }", c)

    def test_non_null_is_a_pointer(self):
        c = crust.translate("fn f(p: *mut NonNull<i32>) -> i32 { p.read() }")
        self.assertIn("struct NonNull_int { int *ptr; }", c)

    def test_mutex_roundtrip(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut m: Mutex<i32> = Mutex { value: 42 };
    m.read()
}
""", suffix=".rs"), 42)

    def test_once_runs_once(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut o: Once<i32> = Once { value: 0, done: false };
    o.call_once(42);
    o.call_once(7);
    o.get()
}
""", suffix=".rs"), 42)

    def test_mutex_does_not_synchronise(self):
        # Documented explicitly because the name promises otherwise: there is
        # no lock in the generated C at all.
        c = crust.translate("fn f(m: *mut Mutex<i32>) -> i32 { m.read() }")
        self.assertNotIn("lock(", c.replace("Mutex_int_lock", ""))

    def test_unused_wrappers_cost_nothing(self):
        c = crust.translate("fn f() -> i32 { 1 }")
        for name in ("Mutex", "RwLock", "Once", "NonNull", "UnsafeCell"):
            self.assertNotIn(name, c)


class TestCrustCfg(unittest.TestCase):
    """`#[cfg(..)]` selects one arm of a set of alternatives.

    Without this every gated arm was emitted, so a crate offering a 32-bit and
    a 64-bit constant produced two conflicting definitions of the same name.
    """

    def test_matching_arm_is_kept(self):
        c = crust.translate('#[cfg(target_pointer_width = "64")]\n'
                            'pub const W: i64 = 64;\n'
                            'fn f() -> i32 { 1 }')
        self.assertIn("64", c)

    def test_non_matching_arm_is_erased(self):
        c = crust.translate('#[cfg(target_pointer_width = "32")]\n'
                            'pub const W: i64 = 32;\n'
                            'fn f() -> i32 { 1 }')
        self.assertNotIn("W", c.replace("void", ""))

    def test_alternatives_do_not_collide(self):
        c = crust.translate('#[cfg(target_pointer_width = "32")]\n'
                            'pub const MAX: u64 = 0xFFFF_FFFF;\n'
                            '#[cfg(target_pointer_width = "64")]\n'
                            'pub const MAX: u64 = 0xFFFF_FFFF_FFFF_FFFF;\n'
                            'fn f() -> i32 { 1 }')
        self.assertEqual(c.count("#define MAX"), 1)
        self.assertIn("0xFFFFFFFFFFFFFFFF", c)

    def test_rejected_arm_leaves_no_rust_behind(self):
        # Dropping only the span would leave its Rust source in the output
        # for the C front end to choke on.
        c = crust.translate('#[cfg(target_arch = "aarch64")]\n'
                            'pub fn only_arm64(x: i32) -> i32 { x }\n'
                            'fn f() -> i32 { 1 }')
        self.assertNotIn("only_arm64", c)
        self.assertNotIn("-> i32", c)

    def test_line_numbers_survive_erasure(self):
        c = crust.translate('#[cfg(target_arch = "aarch64")]\n'
                            'pub fn gone() -> i32 { 1 }\n'
                            'fn f() -> i32 { 42 }')
        self.assertIn("int f(void) { return 42; }", c.split("\n")[2])

    def test_any_and_not(self):
        self.assertTrue(crust.cfg_allows(
            '#[cfg(any(target_arch = "x86", target_arch = "x86_64"))]'))
        self.assertTrue(crust.cfg_allows('#[cfg(not(target_os = "linux"))]'))
        self.assertFalse(crust.cfg_allows(
            '#[cfg(all(unix, target_arch = "aarch64"))]'))

    def test_unknown_key_is_false(self):
        # Treating an unknown predicate as true would select several arms of
        # a set of alternatives, which is the failure this exists to prevent.
        self.assertFalse(crust.cfg_allows('#[cfg(feature = "nope")]'))

    def test_item_without_cfg_is_kept(self):
        self.assertTrue(crust.cfg_allows("#[derive(Clone)]"))
        self.assertTrue(crust.cfg_allows(""))

    def test_cfg_gated_items_run(self):
        self.assertEqual(_run("""
#[cfg(target_pointer_width = "32")]
fn pick() -> i32 { 0 }
#[cfg(target_pointer_width = "64")]
fn pick() -> i32 { 42 }
fn main() -> i32 { pick() }
""", suffix=".rs"), 42)


class TestCrustAssociatedConsts(unittest.TestCase):
    """Associated consts reached through a generic's type parameter.

    This is the shape `rmm/src/page/flags.rs` uses: `PageFlags<A>` generic over
    the architecture, reading `A::ENTRY_FLAG_NO_EXEC` off a trait. Without
    substituting the type parameter in *path* position the path flattened to
    `A_ENTRY_FLAG_NO_EXEC`, naming a type that does not exist.
    """

    def test_const_through_a_type_parameter(self):
        c = crust.translate("""
trait A { const N: i32; }
struct X { v: i32 }
impl A for X { const N: i32 = 5; }
fn g<T: A>() -> i32 { T::N }
fn f() -> i32 { g::<X>() }
""")
        self.assertIn("return X_N;", c)
        self.assertNotIn("T_N", c)

    def test_one_instantiation_per_arch(self):
        c = crust.translate("""
trait Arch { const FLAG: u64; }
struct X8 { v: i32 }
struct A6 { v: i32 }
impl Arch for X8 { const FLAG: u64 = 1; }
impl Arch for A6 { const FLAG: u64 = 8; }
struct Flags<A> { data: u64 }
impl<A> Flags<A> {
    fn new() -> Flags<A> { Flags { data: A::FLAG } }
}
fn f() { let a: Flags<X8> = Flags::<X8>::new();
         let b: Flags<A6> = Flags::<A6>::new(); }
""")
        self.assertIn("X8_FLAG", c)
        self.assertIn("A6_FLAG", c)

    def test_arch_generic_flags_run(self):
        self.assertEqual(_run("""
trait Arch { const PRESENT: u64; const NX: u64; }
struct X86 { v: i32 }
impl Arch for X86 { const PRESENT: u64 = 1; const NX: u64 = 8; }
struct PageFlags<A> { data: u64 }
impl<A> PageFlags<A> {
    fn new() -> PageFlags<A> { PageFlags { data: A::PRESENT | A::NX } }
    fn present(&self) -> bool { (self.data & A::PRESENT) != 0 }
    fn bits(&self) -> u64 { self.data }
}
fn main() -> i32 {
    let p: PageFlags<X86> = PageFlags::<X86>::new();
    if p.present() { (p.bits() as i32) + 33 } else { 0 }
}
""", suffix=".rs"), 42)

    def test_plain_path_still_flattens(self):
        # A path whose head is not a type parameter is unaffected.
        c = crust.translate("fn f() { rmm::aarch64::init_mair(); }")
        self.assertIn("rmm_aarch64_init_mair", c)


class TestCrustTraitConstDefaults(unittest.TestCase):
    """Associated consts with defaults, inherited by an impl.

    rmm's `Arch` trait declares twenty of these, several derived from another
    (`const ENTRY_ADDRESS_SHIFT: usize = Self::PAGE_SHIFT;`). Without
    inheritance the symbol was referenced but never defined, so the file
    translated and then failed to link.
    """

    def test_default_is_inherited(self):
        self.assertEqual(_run("""
trait A { const N: i32 = 42; }
struct X { v: i32 }
impl A for X { }
fn main() -> i32 { X::N }
""", suffix=".rs"), 42)

    def test_impl_overrides_the_default(self):
        self.assertEqual(_run("""
trait A { const N: i32 = 1; }
struct X { v: i32 }
impl A for X { const N: i32 = 42; }
fn main() -> i32 { X::N }
""", suffix=".rs"), 42)

    def test_default_derived_from_another_const(self):
        self.assertEqual(_run("""
trait A { const N: i32; const M: i32 = Self::N; }
struct X { v: i32 }
impl A for X { const N: i32 = 42; }
fn main() -> i32 { X::M }
""", suffix=".rs"), 42)

    def test_two_impls_get_their_own_values(self):
        # The derived default must be computed per implementing type.
        self.assertEqual(_run("""
trait A { const SHIFT: u64; const SIZE: u64 = 1 << Self::SHIFT; }
struct P4 { v: i32 }
struct P64 { v: i32 }
impl A for P4 { const SHIFT: u64 = 12; }
impl A for P64 { const SHIFT: u64 = 16; }
fn main() -> i32 { ((P64::SIZE / P4::SIZE) as i32) * 2 + 10 }
""", suffix=".rs"), 42)

    def test_inherited_const_is_declared_before_use(self):
        # It has to reach the prelude: the appended block sits after every
        # function body, which is too late for anything that reads it.
        c = crust.translate("trait A { const N: i32 = 7; }\n"
                            "struct X { v: i32 }\nimpl A for X { }\n"
                            "fn f() -> i32 { X::N }")
        define = c.index("#define X_N")
        self.assertLess(define, c.index("return X_N"))

    def test_declaration_without_default_is_not_invented(self):
        # A trait const with no default must come from the impl; inventing
        # one would silently give every implementor the same value.
        c = crust.translate("trait A { const N: i32; }\n"
                            "struct X { v: i32 }\nimpl A for X { const N: i32 = 5; }\n"
                            "fn f() -> i32 { X::N }")
        self.assertEqual(c.count("X_N ="), 1)


class TestCrustCoreIntrinsics(unittest.TestCase):
    """`core` free functions with an exact C lowering.

    Not a standard library -- the handful of one-line helpers real Rust
    reaches for constantly. Measured across the Redox kernel and relibc:
    `slice::from_raw_parts` 25 uses, `ptr::null_mut` 21, `hint::spin_loop` 18,
    `cmp::min` 15. Each was otherwise an undefined symbol at link time.
    """

    def test_null_is_a_typed_pointer(self):
        c = crust.translate("fn f() -> *mut i32 "
                            "{ core::ptr::null_mut::<i32>() }")
        self.assertIn("((int *)0)", c)

    def test_import_spelling_does_not_matter(self):
        # A crate may import `core::ptr::null_mut`, `ptr::null_mut` or
        # `null_mut`; the call site follows whatever was imported.
        for call in ("core::ptr::null_mut::<i32>()", "ptr::null_mut::<i32>()",
                     "null_mut::<i32>()"):
            self.assertIn("0", crust.translate("fn f() -> *mut i32 { %s }"
                                               % call))

    def test_min_and_max(self):
        self.assertEqual(_run("""
fn main() -> i32 { core::cmp::min(60, 42) + core::cmp::max(0, 0) }
""", suffix=".rs"), 42)

    def test_from_raw_parts_is_a_real_slice(self):
        self.assertEqual(_run("""
fn total(xs: &[i32]) -> i32 { let mut s: i32 = 0; for x in xs { s += x; } s }
fn main() -> i32 {
    let a: [i32; 5] = [10, 20, 12, 99, 99];
    let s: &[i32] = core::slice::from_raw_parts(&a[0], 3);
    total(s)
}
""", suffix=".rs"), 42)

    def test_read_and_write(self):
        self.assertEqual(_run("""
fn main() -> i32 { let mut v: i32 = 5; core::ptr::write(&v, 42);
                   core::ptr::read(&v) }
""", suffix=".rs"), 42)

    def test_swap(self):
        self.assertEqual(_run("""
fn main() -> i32 { let mut a: i32 = 0; let mut b: i32 = 42;
                   core::mem::swap(&a, &b); a }
""", suffix=".rs"), 42)

    def test_copy_nonoverlapping_argument_order(self):
        # Rust puts the source first and counts elements; C's memcpy puts the
        # destination first and counts bytes. Getting either backwards
        # silently corrupts memory.
        self.assertEqual(_run("""
fn main() -> i32 {
    let src: [i32; 3] = [40, 2, 0];
    let mut dst: [i32; 3] = [0; 3];
    core::ptr::copy_nonoverlapping(&src[0], &dst[0], 3);
    dst[0] + dst[1]
}
""", suffix=".rs"), 42)

    def test_spin_loop_is_a_no_op(self):
        c = crust.translate("fn f() { core::hint::spin_loop(); }")
        self.assertNotIn("spin_loop", c)

    def test_local_definition_shadows_the_intrinsic(self):
        # `min` is an ordinary thing to define; silently replacing it with the
        # intrinsic would be a very confusing bug.
        self.assertEqual(_run("""
fn min(a: i32, b: i32) -> i32 { 42 }
fn main() -> i32 { min(1, 2) }
""", suffix=".rs"), 42)


class TestVariadicOverflowArgs(unittest.TestCase):
    """A variadic call with more arguments than the registers hold.

    ShivyC pushes every argument of a variadic call, because its own variadic
    callees read them all from the stack. A standard SysV callee -- glibc's
    printf family -- instead reads the first six integer arguments from
    registers and any overflow from the *top* of the stack. The code assumed
    no variadic call ever overflowed, which held until `write!` started
    generating `snprintf(buf, size, fmt, a, b, c, d)`: seven arguments, one
    past the six integer registers.

    The seventh was read from whatever sat at the top of the all-push block,
    which was the format pointer -- so it printed a plausible-looking garbage
    integer rather than failing.
    """

    def test_six_varargs(self):
        self.assertEqual(_run(
            'int printf(const char *, ...);\n'
            'int main(void) { char b[64]; return 0; }\n'
            'int unused(void) { printf("%d%d%d%d%d%d", 1,2,3,4,5,6);'
            ' return 0; }'), 0)

    def test_sixth_vararg_is_correct(self):
        self.assertEqual(_run("""
int snprintf(char *, unsigned long, const char *, ...);
int atoi(const char *);
int main(void) {
    char b[64];
    snprintf(b, 64, "%d", 42);
    return atoi(b);
}
"""), 42)

    def test_overflow_vararg_is_correct(self):
        # Seven arguments to snprintf: three fixed plus four varargs, so the
        # last is the first stack-passed one.
        self.assertEqual(_run("""
int snprintf(char *, unsigned long, const char *, ...);
int atoi(const char *);
int main(void) {
    char b[64];
    snprintf(b, 64, "%d%d%d%d", 1, 2, 3, 42);
    return atoi(b + 3);
}
"""), 42)

    def test_mixed_widths_overflow(self):
        self.assertEqual(_run("""
int snprintf(char *, unsigned long, const char *, ...);
int atoi(const char *);
int main(void) {
    char b[64];
    long big = 7;
    snprintf(b, 64, "%d%d%ld%d%d", 1, 2, big, 4, 42);
    return atoi(b + 4);
}
"""), 42)

    def test_non_variadic_seven_args_unaffected(self):
        self.assertEqual(_run("""
int f(int a,int b,int c,int d,int e,int g,int h){ return h; }
int main(void){ return f(1,2,3,4,5,6,42); }
"""), 42)


class TestCrustAssociatedTypes(unittest.TestCase):
    """`type Item;` on a trait, bound per impl.

    Redox uses these 45 times -- `type Target` for `Deref` (23) and
    `type Item` for `Iterator` (22). An associated type is a type alias
    attached to an impl, so it resolves at monomorphisation exactly as an
    associated const does.
    """

    def test_self_associated_type(self):
        c = crust.translate("""
trait Container { type Item; fn first(&self) -> Self::Item; }
struct B { v: i32 }
impl Container for B { type Item = i32; fn first(&self) -> Self::Item { self.v } }
""")
        self.assertIn("int B_first(B *self)", c)

    def test_through_a_generic_parameter(self):
        self.assertEqual(_run("""
trait Container { type Item; fn first(&self) -> Self::Item; }
struct B { v: i32 }
impl Container for B { type Item = i32; fn first(&self) -> Self::Item { self.v } }
fn head<T: Container>(c: T) -> T::Item { c.first() }
fn main() -> i32 { let b: B = B { v: 42 }; head(b) }
""", suffix=".rs"), 42)

    def test_each_impl_binds_its_own(self):
        c = crust.translate("""
trait C { type Item; fn first(&self) -> Self::Item; }
struct I { v: i32 }
struct F { v: f64 }
impl C for I { type Item = i32; fn first(&self) -> Self::Item { self.v } }
impl C for F { type Item = f64; fn first(&self) -> Self::Item { self.v } }
""")
        self.assertIn("int I_first(I *self)", c)
        self.assertIn("double F_first(F *self)", c)

    def test_target_naming_a_struct(self):
        # The `Deref` shape: an associated type naming another struct.
        self.assertEqual(_run("""
struct Inner { v: i32 }
struct W { inner: Inner }
impl W {
    type Target = Inner;
    fn deref(&self) -> *mut Self::Target { &self.inner }
}
fn main() -> i32 { let w: W = W { inner: Inner { v: 42 } }; w.deref().v }
""", suffix=".rs"), 42)

    def test_missing_associated_type_is_reported(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("""
trait C { type Item; }
struct B { v: i32 }
impl C for B { }
fn head<T: C>(c: T) -> T::Item { 0 }
fn f() { head(B { v: 1 }); }
""")

    def test_qualified_path_still_flattens(self):
        # `Self::Item` must be checked before the path handler, which would
        # otherwise flatten it to the name `Self_Item`.
        c = crust.translate("struct P { v: i32 }\n"
                            "fn f(p: *const self::P) -> i32 { p.v }")
        self.assertIn("int f(P *p)", c)

    def test_associated_type_emits_no_c(self):
        c = crust.translate("struct B { v: i32 }\n"
                            "impl B { type Item = i32; }\n"
                            "fn f() -> i32 { 1 }")
        self.assertNotIn("Item", c)


class TestCrustSyntaxGaps(unittest.TestCase):
    """Small parser gaps, each measured from real Redox source.

    None of these is a language feature so much as a spelling Crust had not
    seen. They were picked by ranking the actual translation errors rather
    than by guessing what might be missing.
    """

    def test_unsafe_impl(self):
        # `unsafe impl Send for X {}` -- the marker-trait spelling. The
        # `unsafe` is a promise to the borrow checker, which Crust lacks.
        c = crust.translate("struct X { v: i32 }\n"
                            "unsafe impl Send for X { }\n"
                            "fn f() -> i32 { 1 }")
        self.assertIn("int f(void)", c)

    def test_never_type(self):
        c = crust.translate("fn die() -> ! { loop { } }\nfn f() -> i32 { 1 }")
        self.assertIn("void die(void)", c)

    def test_fn_pointer_type(self):
        # Reuses the typedef machinery closures already generate, so the two
        # spellings produce the same C type.
        self.assertEqual(_run("""
fn twice(a: i32) -> i32 { a * 2 }
fn apply(cb: fn(i32) -> i32, v: i32) -> i32 { cb(v) }
fn main() -> i32 { apply(twice, 21) }
""", suffix=".rs"), 42)

    def test_fn_pointer_with_named_params(self):
        c = crust.translate("fn f(cb: fn(x: i32, y: i32) -> i32) -> i32 "
                            "{ cb(1, 2) }")
        self.assertIn("crust_fn_int_int_int", c)

    def test_tuple_destructuring_let(self):
        self.assertEqual(_run("""
fn divmod(a: i32, b: i32) -> (i32, i32) { (a / b, a % b) }
fn main() -> i32 { let (q, r) = divmod(47, 5); q * 4 + r * 3 }
""", suffix=".rs"), 42)

    def test_tuple_destructuring_ignores_underscore(self):
        c = crust.translate("fn f() { let (a, _, c) = (1, 2, 3); }")
        self.assertNotIn("int _ =", c)

    def test_tuple_destructuring_evaluates_once(self):
        c = crust.translate("""
fn pair() -> (i32, i32) { (1, 2) }
fn f() { let (a, b) = pair(); }
""")
        self.assertEqual(c.count("pair()"), 1)   # the prototype is `pair(void)`

    def test_tuple_arity_mismatch_is_reported(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f() { let (a, b, c) = (1, 2); }")

    def test_const_fn(self):
        # `const fn new(..)` is a function, not an associated constant. The
        # tell is what follows `const`: a constant is `const NAME:`.
        self.assertEqual(_run("""
struct S { v: i32 }
impl S {
    pub const fn new(inner: i32) -> S { S { v: inner } }
}
fn main() -> i32 { let s: S = S::new(42); s.v }
""", suffix=".rs"), 42)

    def test_associated_const_still_recognised(self):
        c = crust.translate("struct S { v: i32 }\nimpl S { const N: i32 = 5; }\n"
                            "fn f() -> i32 { S::N }")
        self.assertIn("S_N", c)

    def test_pub_crate_struct_field(self):
        self.assertEqual(_run("""
struct S { pub(crate) a: i32, pub b: i32 }
fn main() -> i32 { let s: S = S { a: 40, b: 2 }; s.a + s.b }
""", suffix=".rs"), 42)

    def test_block_expression(self):
        # Rust code writes `{ self.x }` to force a copy out of a packed field
        # before formatting it. In *tail* position a block is a scope rather
        # than a value, as it is in C, so this is tested where it is
        # unambiguously an expression.
        self.assertEqual(_run("""
struct S { x: i32 }
fn main() -> i32 { let s: S = S { x: 42 }; let v: i32 = { s.x }; v }
""", suffix=".rs"), 42)


class TestCrustCorePaths(unittest.TestCase):
    """A qualified path to a concrete core type."""

    def test_core_fmt_formatter(self):
        # `core::fmt::Formatter` flattens to a name nothing defines; its last
        # segment has to reach the demand-driven core loader instead.
        c = crust.translate("fn f(p: *mut core::fmt::Formatter) -> i64 "
                            "{ p.len() }")
        self.assertIn("Formatter_len", c)

    def test_short_fmt_path(self):
        c = crust.translate("fn f(p: *mut fmt::Formatter) -> i64 { p.len() }")
        self.assertIn("Formatter_len", c)

    def test_unknown_path_still_flattens(self):
        # Only a name core actually provides is pulled in; anything else keeps
        # the flattened spelling so the diagnostic names what was written.
        c = crust.translate("fn f(p: *const some::Unknown) -> i32 { 0 }")
        self.assertIn("some_Unknown", c)

    def test_local_path_type_still_wins(self):
        c = crust.translate("struct P { v: i32 }\n"
                            "fn f(p: *const self::P) -> i32 { p.v }")
        self.assertIn("int f(P *p)", c)


class TestCrustRefCounted(unittest.TestCase):
    """`Rc<T>` and `Arc<T>` -- counted, but not automatic."""

    def test_clone_and_release(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut a: Rc<i32> = Rc::<i32>::new(42);
    let mut b: Rc<i32> = a.clone();
    let v: i32 = b.get();
    b.release();
    a.release();
    v
}
""", suffix=".rs"), 42)

    def test_strong_count_tracks_clones(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut a: Rc<i32> = Rc::<i32>::new(1);
    let mut b: Rc<i32> = a.clone();
    let mut c: Rc<i32> = a.clone();
    let n: i64 = a.strong_count();
    b.release(); c.release(); a.release();
    (n as i32) * 14
}
""", suffix=".rs"), 42)

    def test_arc_is_rc_under_another_name(self):
        # No atomics: Crust has no threads for the refcount to race with.
        c = crust.translate("fn f() { let a: Arc<i32> = Arc::<i32>::new(1); }")
        self.assertNotIn("atomic", c.lower())

    def test_release_frees_at_zero(self):
        c = crust.translate("fn f() { let mut a: Rc<i32> = Rc::<i32>::new(1);"
                            " a.release(); }")
        self.assertIn("free(", c)

    def test_mutex_guard_resolves(self):
        c = crust.translate("fn f(g: *mut MutexGuard<i32>) -> i32 "
                            "{ g.get() }")
        self.assertIn("MutexGuard_int", c)


class TestCrust128Bit(unittest.TestCase):
    """`u128` / `i128` lower to a two-word struct.

    They have no C counterpart in this backend. The struct is the *right size*
    -- 16 bytes -- and so correct for storage, layout and ABI, which is how
    real code overwhelmingly uses them: relibc's `pub type c_longdouble =
    u128;` models C's `long double` and never computes with it.
    """

    def test_u128_is_sixteen_bytes(self):
        c = crust.translate("fn f(x: *mut u128) -> i32 { 1 }")
        self.assertIn("typedef struct crust_u128 { unsigned long lo; "
                      "unsigned long hi; } crust_u128;", c)

    def test_i128_is_signed_in_the_high_word(self):
        c = crust.translate("fn f(x: *mut i128) -> i32 { 1 }")
        self.assertIn("long hi; } crust_i128;", c)

    def test_size_is_right(self):
        self.assertEqual(_run("""
fn f(x: *mut u128) -> i32 { 1 }
int main(void) { return (int)sizeof(crust_u128) + 26; }
""", suffix=".rs"), 42)

    def test_unused_costs_nothing(self):
        c = crust.translate("fn f() -> i32 { 1 }")
        self.assertNotIn("crust_u128", c)

    def test_arithmetic_does_not_silently_truncate(self):
        # The point of a distinct struct: `a + b` on a 128-bit value fails to
        # compile rather than quietly doing 64-bit arithmetic. That is the
        # only honest option short of real 128-bit support.
        with self.assertRaises(Exception):
            _run("fn f(a: u128, b: u128) -> u128 { a + b }\n"
                 "fn main() -> i32 { 0 }", suffix=".rs")


class TestC128BitApproximation(unittest.TestCase):
    """The C front end's `__int128` is 64-bit, and that is a real divergence.

    `shivyc/preproc.py` defines `__int128` as `long long` so that glibc's
    internal typedefs compile. The comment there states the precondition: it
    is sound only where those types are never *computed* with. This test
    records what happens when they are, so the divergence is known rather
    than discovered.
    """

    def test_sizeof_diverges_from_gcc(self):
        # gcc reports 16. Pinned so a future change to real 128-bit support
        # shows up here rather than silently.
        self.assertEqual(_run("int main(void) "
                              "{ return (int)sizeof(__int128); }"), 8)

    def test_arithmetic_is_wrong(self):
        # gcc gives 2; the 64-bit approximation cannot. Asserting the *wrong*
        # answer is deliberate: it documents the limit and will fail loudly
        # if 128-bit arithmetic is ever implemented properly.
        got = _run("""
int main(void) {
    unsigned __int128 b = 0xFFFFFFFFFFFFFFFFULL;
    b = b * 3;
    return (int)(unsigned long long)(b >> 64);
}
""")
        self.assertNotEqual(got, 2)


class TestCrustBlockExpression(unittest.TestCase):
    """A block used as a value may run statements first."""

    def test_statements_then_value(self):
        self.assertEqual(_run("""
fn main() -> i32 { let v: i32 = { let a: i32 = 6; let b: i32 = 7; a * b }; v }
""", suffix=".rs"), 42)

    def test_statements_run_before_the_use(self):
        # The hoisted statements land in the pending list, which is emitted
        # immediately before the statement containing the block.
        c = crust.translate("fn f() -> i32 { let v: i32 = { let a: i32 = 2;"
                            " a * 21 }; v }")
        self.assertLess(c.index("int a = 2"), c.index("int v ="))

    def test_nested_blocks(self):
        self.assertEqual(_run("""
fn main() -> i32 { let v: i32 = { let a: i32 = { let b: i32 = 21; b }; a * 2 };
                   v }
""", suffix=".rs"), 42)

    def test_two_blocks_may_reuse_a_name(self):
        # The hoisted statements keep their own C scope. Emitting them bare
        # put every block-local at function scope, so two blocks that each
        # declared `a` collided with "redefinition of 'a'".
        self.assertEqual(_run("""
fn main() -> i32 {
    let x: i32 = { let a: i32 = 6; a };
    let y: i32 = { let a: i32 = 7; a };
    x * y
}
""", suffix=".rs"), 42)

    def test_block_local_is_not_visible_after(self):
        c = crust.translate("fn f() -> i32 { let v: i32 = { let a: i32 = 1;"
                            " a }; v }")
        # The declaration sits inside its own braces, not at function scope.
        self.assertIn("{ int a = 1;", c)


class TestCrustVecMacro(unittest.TestCase):
    """`vec![..]` builds a bundled `Vec<T>`."""

    def test_elements_are_pushed(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut v: Vec<i32> = vec![10, 20, 12];
    let mut s: i32 = 0;
    for i in 0..v.len() { s += v.get(i); }
    v.free_buf();
    s
}
""", suffix=".rs"), 42)

    def test_element_type_comes_from_the_first(self):
        c = crust.translate("fn f() { let v: Vec<f64> = vec![1.0, 2.0]; }")
        self.assertIn("Vec_double", c)

    def test_empty_vec_is_rejected(self):
        # Nothing to infer the element type from, exactly as in Rust.
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f() { let v: Vec<i32> = vec![]; }")


class TestCrustString(unittest.TestCase):
    """`String` -- a growable, always NUL-terminated buffer.

    Deliberately not rpython's `str`. py2c has a complete string runtime, but
    reaching for it means linking `shivyc_rt.c` and its arena into every unit
    that formats anything, and a kernel cannot use an arena before its heap
    exists. A kernel's string handling is short and bounded; this is that.
    """

    def test_push_str_and_compare(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut s: String = String::new();
    s.push_str("hello");
    s.push(32 as c_char);
    s.push_str("world");
    let ok: bool = s.eq_str("hello world");
    let n: i64 = s.len();
    s.free_buf();
    if ok { (n as i32) + 31 } else { 0 }
}
""", suffix=".rs"), 42)

    def test_growth_past_initial_capacity(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut s: String = String::new();
    for i in 0..100 { s.push(65 as c_char); }
    let n: i64 = s.len();
    s.free_buf();
    (n as i32) - 58
}
""", suffix=".rs"), 42)

    def test_always_nul_terminated(self):
        # `as_ptr` must hand C something it can use directly.
        self.assertEqual(_run("""
int strlen_c(const char *);
fn main() -> i32 {
    let mut s: String = String::new();
    s.push_str("abcdefghij");
    let n: i32 = strlen_c(s.as_ptr());
    s.free_buf();
    n + 32
}
int strlen_c(const char *p) { int n = 0; while (p[n]) n++; return n; }
""", suffix=".rs"), 42)

    def test_clear_resets(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut s: String = String::new();
    s.push_str("junk");
    s.clear();
    s.push_str("ok");
    let ok: bool = s.eq_str("ok");
    s.free_buf();
    if ok { 42 } else { 0 }
}
""", suffix=".rs"), 42)


class TestPeepholeStrideLiteral(unittest.TestCase):
    """A loop that walks a pointer, which the peephole strength-reduces.

    The pass rewrites `addr = base + i` into a pointer advanced by a constant
    stride, and registers that stride as a new literal. Spots are assigned to
    every literal *before* functions are emitted, so a literal created during
    codegen needs recording -- and `register_literal_var` was not doing it.
    The allocator then treated the stride as an ordinary value with no
    definition, gave it a register nothing ever wrote, and the loop advanced
    the pointer by whatever that register happened to hold.

    Reachable from plain C by any walk-a-string loop.
    """

    def test_walk_a_c_string(self):
        self.assertEqual(_run("""
int walk(char *p) { long i = 0; while (p[i] != (char)0) { i += 1; } return (int)i; }
int main(void) { return walk("hello") + 37; }
"""), 42)

    def test_walk_with_int_index(self):
        self.assertEqual(_run("""
int walk(char *p) { int i = 0; while (p[i]) { i++; } return i; }
int main(void) { return walk("abcdefg") + 35; }
"""), 42)

    def test_sum_an_array_in_a_loop(self):
        self.assertEqual(_run("""
int sum(int *a, int n) { int t = 0; long i = 0; while (i < n) { t += a[i]; i++; }
                         return t; }
int main(void) { int a[4]; a[0]=10; a[1]=20; a[2]=8; a[3]=4; return sum(a, 4); }
"""), 42)


class TestCrustFormatMacro(unittest.TestCase):
    """`format!` renders into a fresh `String`.

    Sized with the standard C idiom: `snprintf` with a NULL destination
    reports how many characters the result *would* take, so the buffer is
    reserved once at exactly the right size and written once -- no guess and
    no grow-and-retry loop.

    The result owns its buffer and there is no `Drop`, so the caller must
    `free_buf()` it, like every other allocating type in the bundled core.
    """

    def test_renders_values(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut s: String = format!("n={} f={:.1} s={}", 7, 2.5, "hi");
    let ok: bool = s.eq_str("n=7 f=2.5 s=hi");
    s.free_buf();
    if ok { 42 } else { 0 }
}
""", suffix=".rs"), 42)

    def test_length_is_exact(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut s: String = format!("{}{}{}{}{}{}", 1, 2, 3, 4, 5, 6);
    let n: i64 = s.len();
    s.free_buf();
    (n as i32) + 36
}
""", suffix=".rs"), 42)

    def test_empty_format_is_an_empty_string_not_null(self):
        # `String::new()` leaves the buffer null, so the reserve must happen
        # even when nothing is written -- `as_ptr()` printed "(null)".
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut s: String = format!("");
    let ok: bool = s.eq_str("");
    s.free_buf();
    if ok { 42 } else { 0 }
}
""", suffix=".rs"), 42)

    def test_braces_and_percent_are_literal(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut s: String = format!("{{}} 100%");
    let ok: bool = s.eq_str("{} 100%");
    s.free_buf();
    if ok { 42 } else { 0 }
}
""", suffix=".rs"), 42)

    def test_no_arguments(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut s: String = format!("plain");
    let n: i64 = s.len();
    s.free_buf();
    (n as i32) + 37
}
""", suffix=".rs"), 42)


class TestCoreInternalLinkage(unittest.TestCase):
    """Bundled core methods have internal linkage.

    A core type is emitted into *every* translation unit that names it, so
    external linkage means two such units collide at link time -- and worse, a
    crate with its own type of the same name collides with the bundled one.
    relibc has its own `String`, and linking CrustOS against it failed with
    "duplicate symbol String_as_ptr".

    Within a single unit the local definition correctly wins, so the collision
    is invisible until two units are linked together.
    """

    def _core_defs(self, src):
        import re
        c = crust.translate(src)
        return re.findall(r"^\s*(static\s+)?[A-Za-z_][\w *]*?"
                          r"(String|Vec_int)_\w+\s*\([^;]*\)\s*\{",
                          c, re.M)

    def test_every_definition_is_static(self):
        # The whole `impl` block arrives as one blob, so prefixing the string
        # only reached the first method -- `String_new` was static while
        # `String_push` stayed external.
        src = ("fn f() { let mut s: String = String::new();"
               " s.push(65 as c_char); s.push_str(\"x\"); }")
        defs = self._core_defs(src)
        self.assertTrue(defs)
        self.assertTrue(all(d[0].strip() == "static" for d in defs),
                        [d for d in defs if d[0].strip() != "static"])

    def test_generic_core_definitions_are_static_too(self):
        defs = self._core_defs("fn f() { let v: Vec<i32> = Vec::<i32>::new(); }")
        self.assertTrue(all(d[0].strip() == "static" for d in defs))

    def test_prototype_matches_the_definition(self):
        # A `static` definition with an extern prototype is a linkage error in
        # its own right.
        c = crust.translate("fn f() { let mut s: String = String::new();"
                            " s.push(65 as c_char); }")
        proto = [x for x in c.split(";")
                 if "String_push(String" in x and "{" not in x]
        self.assertTrue(proto)
        self.assertIn("static", proto[0])

    def test_user_function_is_not_made_static(self):
        # Only core-provided methods get internal linkage; the unit's own
        # functions are its linkage surface.
        c = crust.translate("fn visible(a: i32) -> i32 { a }")
        self.assertIn("int visible(int a)", c)
        self.assertNotIn("static int visible", c)


class TestMultiUnitLinking(unittest.TestCase):
    """Two translation units linked together.

    Every other test compiles one unit, and a name can be emitted correctly in
    each unit on its own and still collide when they are linked. That is
    exactly what happened: the bundled core emits its methods into every unit
    that names a core type, and with external linkage two such units failed
    with "duplicate symbol String_as_ptr". Within either unit alone the output
    was perfectly correct.
    """

    def test_two_units_both_using_string(self):
        self.assertEqual(_run_units({
            "helper": """
fn helper_len(text: *const c_char) -> i32 {
    let mut s: String = String::new();
    s.push_str(text);
    let n: i32 = s.len() as i32;
    s.free_buf();
    n
}
""",
            "main": """
int helper_len(const char *);
fn main() -> i32 {
    let mut s: String = String::new();
    s.push_str("abcdefg");
    let mine: i32 = s.len() as i32;
    s.free_buf();
    mine + helper_len("abcdefghij") + 25
}
""",
        }, "main"), 42)

    def test_two_units_both_using_a_core_generic(self):
        # `Vec_int_new` is emitted in every unit that names `Vec<i32>`.
        self.assertEqual(_run_units({
            "helper": """
fn helper_sum() -> i32 {
    let mut v: Vec<i32> = Vec::<i32>::new();
    v.push(20);
    v.push(1);
    let t: i32 = v.get(0) + v.get(1);
    v.free_buf();
    t
}
""",
            "main": """
int helper_sum(void);
fn main() -> i32 {
    let mut v: Vec<i32> = Vec::<i32>::new();
    v.push(21);
    let mine: i32 = v.get(0);
    v.free_buf();
    mine + helper_sum()
}
""",
        }, "main"), 42)

    def test_unit_with_its_own_string_type(self):
        # relibc has its own `String`, and it collided with the bundled one.
        # A local definition wins within its unit; the other unit still gets
        # the bundled type, and the two must not clash.
        self.assertEqual(_run_units({
            "theirs": """
struct String { n: i32 }
impl String {
    fn as_ptr(&self) -> i32 { self.n }
}
fn theirs_value() -> i32 {
    let s: String = String { n: 20 };
    s.as_ptr()
}
""",
            "main": """
int theirs_value(void);
fn main() -> i32 {
    let mut s: String = String::new();
    s.push_str("xx");
    let mine: i32 = s.len() as i32;
    s.free_buf();
    mine + theirs_value() + 20
}
""",
        }, "main"), 42)

    def test_user_functions_stay_linkable(self):
        # Only core-provided methods get internal linkage; a unit's own
        # functions are its linkage surface and must remain callable.
        self.assertEqual(_run_units({
            "lib": "fn twice(a: i32) -> i32 { a * 2 }\n",
            "main": ("int twice(int);\n"
                     "fn main() -> i32 { twice(21) }\n"),
        }, "main"), 42)
