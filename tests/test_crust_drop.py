"""Tests for Crust scope-exit Drop (auto free_buf / free_box)."""

import unittest

import shivyc.crust as crust
from tests.test_crust import _run


class TestOwningFree(unittest.TestCase):
    def test_vec_is_owning(self):
        p = crust.Parser([], crust.Unit())
        # Seed a Vec_int instance the way demand_seed would.
        p.unit.instances["Vec_int"] = ("Vec", [crust.INT])
        p.unit.methods[("Vec_int", "free_buf")] = type(
            "I", (), {"mangled": "Vec_int_free_buf", "name": "free_buf"})()
        self.assertEqual(p.owning_free(crust.RustCType("Vec_int")), "free_buf")
        self.assertIsNone(p.owning_free(crust.RustCType("Vec_int", ptr=1)))
        self.assertIsNone(p.owning_free(crust.INT))

    def test_string_is_owning(self):
        p = crust.Parser([], crust.Unit())
        self.assertEqual(p.owning_free(crust.RustCType("String")), "free_buf")

    def test_box_is_owning(self):
        p = crust.Parser([], crust.Unit())
        p.unit.instances["Box_int"] = ("Box", [crust.INT])
        self.assertEqual(p.owning_free(crust.RustCType("Box_int")), "free_box")

    def test_live_register_requires_method(self):
        p = crust.Parser([], crust.Unit())
        p.scope_push("block")
        p.unit.instances["Vec_int"] = ("Vec", [crust.INT])
        # No methods entry yet -- register is a no-op.
        p.live_register("v", crust.RustCType("Vec_int"))
        self.assertEqual(p.live[-1]["items"], [])
        p.unit.methods[("Vec_int", "free_buf")] = type(
            "I", (), {"mangled": "Vec_int_free_buf", "name": "free_buf"})()
        p.live_register("v", crust.RustCType("Vec_int"))
        self.assertEqual(len(p.live[-1]["items"]), 1)
        self.assertEqual(p.live[-1]["items"][0][0], "v")
        self.assertEqual(p.live[-1]["items"][0][1], "Vec_int_free_buf")

    def test_scope_push_tracks_kind(self):
        p = crust.Parser([], crust.Unit())
        self.assertEqual(p.live[0]["kind"], "file")
        p.scope_push("func")
        p.scope_push("loop")
        self.assertEqual(p.live_frame_index(("loop",)), 2)
        self.assertEqual(p.live_frame_index(("func",)), 1)
        p.scope_pop()
        self.assertEqual(p.live_frame_index(("loop",)), None)


class TestDropEmission(unittest.TestCase):
    def test_vec_dropped_without_explicit_free(self):
        out = crust.translate("""
fn f() -> i32 {
    let mut v: Vec<i32> = Vec::<i32>::new();
    v.push(1);
    v.len() as i32
}
""")
        self.assertIn("Vec_int_free_buf(&v);", out)
        self.assertEqual(out.count("Vec_int_free_buf(&v);"), 1)

    def test_return_spills_before_drop(self):
        out = crust.translate("""
fn f() -> i32 {
    let mut v: Vec<i32> = Vec::<i32>::new();
    v.push(7);
    return v.get(0);
}
""")
        body = out[out.index("int f(void) {"):]
        spill = body.index("_crust_opt")
        free = body.index("Vec_int_free_buf(&v);")
        ret = body.index("return ", free)
        self.assertLess(spill, free)
        self.assertLess(free, ret)

    def test_move_out_skips_drop(self):
        out = crust.translate("""
fn give() -> Vec<i32> {
    let mut v: Vec<i32> = Vec::<i32>::new();
    v.push(1);
    v
}
""")
        body = out[out.index("Vec_int give"):out.index("static Vec_int Vec_int_new")]
        self.assertNotIn("Vec_int_free_buf(&v);", body)

    def test_simple_move_zeros_source(self):
        out = crust.translate("""
fn f() -> i32 {
    let mut a: Vec<i32> = Vec::<i32>::new();
    a.push(1);
    let b = a;
    b.get(0)
}
""")
        self.assertIn("memset(&a, 0, sizeof(a));", out)
        self.assertIn("Vec_int_free_buf(&b);", out)
        self.assertNotIn("Vec_int_free_buf(&a);", out)

    def test_break_drops_loop_local(self):
        out = crust.translate("""
fn f() -> i32 {
    let mut s: i32 = 0;
    loop {
        let mut v: Vec<i32> = Vec::<i32>::new();
        v.push(1);
        s = s + v.get(0);
        break;
    }
    s
}
""")
        self.assertIn("Vec_int_free_buf(&v);", out)
        # free before break
        loop = out[out.index("while (1)"):]
        self.assertLess(loop.index("Vec_int_free_buf(&v);"),
                        loop.index("break;"))

    def test_end_to_end_no_explicit_free(self):
        self.assertEqual(_run("""
fn main() -> i32 {
    let mut v: Vec<i32> = Vec::<i32>::new();
    v.push(40);
    v.push(2);
    v.get(0) + v.get(1)
}
""", suffix=".rs"), 42)

    def test_end_to_end_move_and_give(self):
        self.assertEqual(_run("""
fn give() -> Vec<i32> {
    let mut v: Vec<i32> = Vec::<i32>::new();
    v.push(42);
    v
}
fn main() -> i32 {
    let w = give();
    w.get(0)
}
""", suffix=".rs"), 42)


if __name__ == "__main__":
    unittest.main()
