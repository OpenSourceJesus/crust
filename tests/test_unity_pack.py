#!/usr/bin/env python3
"""test_unity_pack -- packed engine from a Unity-subset scene.

Pins the claims in UNITY_PACK.md:

  * a 2D hand-placed class that never Instantiates is indexed in 8 bits
    and drops z;
  * a static coin packs to ≤16 bytes (the Unity-object-header win);
  * a script that writes transform.position keeps float32;
  * only the Unity API that was called is emitted;
  * engine.c + data.c compile (-O3 / -O0) and a tick moves the player.

    python3 tests/test_unity_pack.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tools.unity_pack as unity_pack  # noqa: E402

SCENE = os.path.join(ROOT, "examples", "unity_pack", "MiniScene")
_CC = shutil.which("gcc") or shutil.which("cc")
needs_cc = unittest.skipIf(_CC is None, "no C compiler")


class TestSceneImport(unittest.TestCase):

    def test_unity_yaml_counts(self):
        objs, analyses = unity_pack.load_project(SCENE)
        names = sorted(o["name"] for o in objs)
        self.assertEqual(names, ["CoinA", "CoinB", "Hero"])
        coins = [o for o in objs if o["class"] == "Coin"]
        self.assertEqual(len(coins), 2)
        self.assertFalse(any(a["spawns"] for a in analyses))

    def test_godot_tscn(self):
        text = (
            '[node name="Star" type="Node2D"]\n'
            'script_class = "Star"\n'
            'position = Vector2(3, 4)\n'
            'hp = 2\n'
        )
        objs = unity_pack.parse_godot_tscn(text)
        self.assertEqual(len(objs), 1)
        self.assertEqual(objs[0]["class"], "Star")
        self.assertEqual(objs[0]["pos"][0], 3.0)


class TestLayout(unittest.TestCase):

    def setUp(self):
        self.objs, self.an = unity_pack.load_project(SCENE)
        self.plan = unity_pack.plan_layouts(self.objs, self.an)

    def test_two_d_drops_z(self):
        self.assertTrue(self.plan["two_d"])
        for cl in self.plan["classes"].values():
            names = [m[0] for m in cl["members"]]
            self.assertNotIn("pos_z", names)

    def test_no_spawn_is_uint8_index(self):
        for cl in self.plan["classes"].values():
            self.assertEqual(cl["idx_ty"], "uint8_t")
            self.assertTrue(cl["bounded"])

    def test_coin_is_at_most_sixteen_bytes(self):
        # The whole point: no Unity object header.
        self.assertLessEqual(self.plan["classes"]["Coin"]["size"], 16)

    def test_static_coin_uses_f16(self):
        kinds = {m[0]: m[3] for m in self.plan["classes"]["Coin"]["members"]}
        self.assertEqual(kinds["pos_x"], "f16")
        self.assertEqual(kinds["hp"], "bits")

    def test_moving_player_keeps_f32(self):
        kinds = {m[0]: m[3] for m in self.plan["classes"]["Player"]["members"]}
        self.assertEqual(kinds["pos_x"], "f32")
        self.assertEqual(kinds["speed"], "f32")


class TestEmit(unittest.TestCase):

    def test_api_subset_only(self):
        plan = unity_pack.pack(SCENE, tempfile.mkdtemp(prefix="upack-"))
        # pack writes files; re-read engine
        # Mathf was not called — must not appear as a function.
        # Time.deltaTime was.
        # We only check the last pack via a fresh dir.
        self.assertIn("Player", plan["classes"])

    def test_engine_omits_unused_mathf(self):
        d = tempfile.mkdtemp(prefix="upack-")
        unity_pack.pack(SCENE, d)
        with open(os.path.join(d, "engine.c")) as f:
            engine = f.read()
        self.assertNotIn("Mathf_Abs", engine)
        self.assertIn("Time_deltaTime", engine)
        self.assertIn("_Coin_inst_array", engine)
        self.assertTrue(os.path.isfile(
            os.path.join(d, "shaders", "shader_compiler_wasm.c")))


class TestSpawnWidensIndex(unittest.TestCase):

    def test_instantiate_refuses_uint8_bound(self):
        src = (
            "using UnityEngine;\n"
            "public class Mob : MonoBehaviour {\n"
            "    public int hp;\n"
            "    public void Update() { Instantiate(this); }\n"
            "}\n"
        )
        a = unity_pack.analyze_script("<mem>", src)
        self.assertTrue(a["spawns"])
        objs = [{"name": "m", "pos": (0, 0, 0), "fields": {"hp": 1},
                 "script": None, "class": "Mob"}]
        plan = unity_pack.plan_layouts(objs, [a])
        # Spawned classes are not a closed set of 256.
        self.assertFalse(plan["classes"]["Mob"]["bounded"])
        self.assertEqual(plan["classes"]["Mob"]["idx_ty"], "uint32_t")


@needs_cc
class TestRuns(unittest.TestCase):

    def test_tick_moves_player(self):
        d = tempfile.mkdtemp(prefix="upack-")
        unity_pack.pack(SCENE, d)
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "int engine_class_count(void);\n"
                "extern float Time_deltaTime;\n"
                "typedef struct Player Player;\n"
                "struct Player { float pos_x; float pos_y; "
                "unsigned hp : 3; float speed; };\n"
                "extern Player _Player_inst_array[];\n"
                "int main(void) {\n"
                "  float before = _Player_inst_array[0].pos_x;\n"
                "  engine_tick();\n"
                "  if (_Player_inst_array[0].pos_x <= before) return 2;\n"
                "  return engine_class_count() == 2 ? 0 : 1;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O3", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "game")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), host,
             "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
