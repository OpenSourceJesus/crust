"""Pure-translation tests for Crust scope-exit Drop helpers."""

import unittest

import shivyc.crust as crust


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


if __name__ == "__main__":
    unittest.main()
