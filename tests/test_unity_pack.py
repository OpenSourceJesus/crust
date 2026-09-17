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
import math
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
        objs, analyses, _lights, _cams = unity_pack.load_project(SCENE)
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
        self.objs, self.an, _lights, _cams = unity_pack.load_project(SCENE)
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

    def test_emitted_c_passes_cpprust_subset_gate(self):
        """Hand-lowered engine.c must survive cpprust.translate (csrust gate)."""
        d = tempfile.mkdtemp(prefix="upack-")
        unity_pack.pack(SCENE, d)
        with open(os.path.join(d, "engine.c")) as f:
            engine = f.read()
        # Explicit re-check (pack already ran validate_emitted_c).
        unity_pack.validate_emitted_c(engine, "engine.c")

    def test_validate_emitted_c_refuses_throw(self):
        with self.assertRaises(unity_pack.PackError) as cm:
            unity_pack.validate_emitted_c(
                "void f(void) { throw 1; }\n", "bad.c")
        self.assertIn("subset", cm.exception.message)
        self.assertIn("throw", cm.exception.message)

    def test_engine_omits_unused_mathf(self):
        d = tempfile.mkdtemp(prefix="upack-")
        unity_pack.pack(SCENE, d)
        with open(os.path.join(d, "engine.c")) as f:
            engine = f.read()
        self.assertNotIn("Mathf_Abs", engine)
        self.assertIn("Time_deltaTime", engine)
        self.assertIn("_Coin_inst_array", engine)
        self.assertIn("engine_collect_draws", engine)
        self.assertIn("EngineDraw", engine)
        self.assertIn("engine_upload_positions", engine)
        self.assertTrue(os.path.isfile(os.path.join(d, "engine_draw.h")))
        self.assertTrue(os.path.isfile(os.path.join(d, "main.c")))
        self.assertTrue(os.path.isfile(
            os.path.join(d, "shaders", "shader_compiler_wasm.c")))
        with open(os.path.join(d, "engine_draw.h")) as f:
            hdr = f.read()
        self.assertIn("engine_collect_draws", hdr)
        self.assertIn("engine_upload_positions", hdr)
        self.assertIn("engine_tick", hdr)


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


class TestSoa(unittest.TestCase):
    """--soa: positions in contiguous float tables for GPU upload."""

    def test_soa_emits_pos_tables_not_struct_fields(self):
        d = tempfile.mkdtemp(prefix="upack-soa-")
        plan = unity_pack.pack(SCENE, d, soa=True)
        self.assertTrue(plan["soa"])
        self.assertEqual(plan["classes"]["Coin"]["soa_dims"], 2)
        names = [m[0] for m in plan["classes"]["Coin"]["members"]]
        self.assertNotIn("pos_x", names)
        with open(os.path.join(d, "data.c")) as f:
            data = f.read()
        self.assertIn("_Coin_pos[", data)
        self.assertIn("_Player_pos[", data)
        with open(os.path.join(d, "engine.c")) as f:
            engine = f.read()
        self.assertIn("SoA: one contiguous table", engine)
        self.assertIn("engine_upload_positions", engine)

    @needs_cc
    def test_soa_tick_still_moves_player(self):
        d = tempfile.mkdtemp(prefix="upack-soa-run-")
        unity_pack.pack(SCENE, d, soa=True)
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "#include \"engine_draw.h\"\n"
                "extern float _Player_pos[][2];\n"
                "int main(void) {\n"
                "  float before = _Player_pos[0][0];\n"
                "  engine_tick();\n"
                "  if (_Player_pos[0][0] <= before) return 2;\n"
                "  float buf[16];\n"
                "  int n = engine_upload_positions(buf, 16);\n"
                "  return n == engine_position_floats() ? 0 : 1;\n"
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
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"),
             "-I", d, "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_soa_vec4_pads_w_with_instance_id(self):
        d = tempfile.mkdtemp(prefix="upack-soa4-")
        plan = unity_pack.pack(SCENE, d, soa_vec4=True)
        self.assertTrue(plan["soa_vec4"])
        self.assertEqual(plan["classes"]["Coin"]["soa_dims"], 4)
        self.assertEqual(plan["classes"]["Coin"]["soa_logical"], 2)
        with open(os.path.join(d, "data.c")) as f:
            data = f.read()
        # CoinB is index 1 → { -1, 0, 0, 1 }
        self.assertIn("{ -1.0f, 0.0f, 0.0f, 1.0f }", data)
        glsl = os.path.join(d, "shaders", "soa_positions.glsl")
        self.assertTrue(os.path.isfile(glsl))
        with open(glsl) as f:
            text = f.read()
        self.assertIn("std430", text)
        self.assertIn("vec4 pos[]", text)
        self.assertIn("SOA_STRIDE 4", text)


@needs_cc
class TestGLES2View(unittest.TestCase):
    """Packed scene rendered through surfaceless GLES2 (needs libEGL)."""

    def test_gles2_view_ascii_has_sprites(self):
        d = tempfile.mkdtemp(prefix="upack-gles-")
        unity_pack.pack(SCENE, d)
        view = os.path.join(ROOT, "examples", "unity_pack", "gles2_view.c")
        exe = os.path.join(d, "view")
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
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, view,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"),
             "-I", d, "-lEGL", "-lGLESv2", "-lm"],
            capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest("cannot link GLES2 view: %s" % r.stderr[-400:])
        env = os.environ.copy()
        env["EGL_PLATFORM"] = "surfaceless"
        env["LIBGL_ALWAYS_SOFTWARE"] = "1"
        run = subprocess.run([exe], capture_output=True, text=True, env=env)
        if run.returncode != 0:
            self.skipTest("GLES run failed (no soft rasteriser?): %s"
                          % (run.stderr or run.stdout)[-400:])
        self.assertIn("draws=3", run.stdout)
        art = "\n".join(
            line for line in run.stdout.splitlines()
            if line and set(line) <= set(".RGB"))
        self.assertTrue(art, run.stdout[-500:])
        lit = sum(1 for ch in art if ch in "RGB")
        self.assertGreaterEqual(lit, 20, art)


SYSTEMS = os.path.join(ROOT, "examples", "unity_pack", "SystemsScene")


class TestSystems(unittest.TestCase):
    """Authored systems subset — see UNITY_PACK_SYSTEMS.md."""

    def test_detects_system_apis(self):
        _objs, analyses, lights, cameras = unity_pack.load_project(SYSTEMS)
        apis = set()
        for a in analyses:
            apis |= a["apis"]
        self.assertIn("Time.time", apis)
        self.assertIn("Mathf.Sin", apis)
        self.assertIn("Keyboard.current", apis)
        self.assertIn("Debug.Log", apis)
        self.assertIn("print", apis)
        self.assertIn("GameObject.Find", apis)
        self.assertIn("GetComponent", apis)
        self.assertNotIn("Input.GetAxis", apis)
        self.assertIn("RenderSettings.ambientLight", apis)
        self.assertEqual(len(lights), 1)
        self.assertAlmostEqual(lights[0]["intensity"], 1.5)
        self.assertEqual(len(cameras), 1)
        self.assertTrue(cameras[0]["main"])
        self.assertAlmostEqual(cameras[0]["orthographic_size"], 3.0)
        self.assertAlmostEqual(cameras[0]["pos"][2], -10.0)
        self.assertAlmostEqual(cameras[0]["near_clip"], 0.3)
        self.assertAlmostEqual(cameras[0]["far_clip"], 1000.0)
        self.assertNotIn("ParticleSystem.Emit", apis)
        self.assertNotIn("AnimationCurve.Evaluate", apis)
        spr = [o for o in _objs if o.get("sprite")]
        self.assertEqual(len(spr), 3)  # Bouncer, Ball, Pad — not Shade
        stick = [o for o in _objs if o["name"] == "Stick"][0]
        self.assertAlmostEqual(stick["sprite"]["rot_z"], math.pi / 4, places=5)
        self.assertAlmostEqual(stick["sprite"]["cos_z"], math.cos(math.pi / 4),
                               places=5)
        self.assertAlmostEqual(stick["rot"][2], 0.3826834, places=5)
        self.assertAlmostEqual(stick["rot"][3], 0.9238795, places=5)
        # Rig at (0, 0.5) + local (1.5, 0) → world (1.5, 0.5)
        self.assertEqual(stick["father_id"], "6002")
        self.assertAlmostEqual(stick["local_pos"][0], 1.5)
        self.assertAlmostEqual(stick["local_pos"][1], 0.0)
        self.assertAlmostEqual(stick["pos"][0], 1.5)
        self.assertAlmostEqual(stick["pos"][1], 0.5)
        self.assertEqual(len([o for o in _objs if o["name"] == "Rig"]), 0)

    def test_prefab_m_transform_parent_composes_world(self):
        """PrefabInstance.m_TransformParent parents stripped Transforms."""
        text = (
            "--- !u!1 &1\n"
            "GameObject:\n"
            "  m_Name: Anchor\n"
            "  m_Component:\n"
            "  - component: {fileID: 2}\n"
            "--- !u!4 &2\n"
            "Transform:\n"
            "  m_GameObject: {fileID: 1}\n"
            "  m_LocalPosition: {x: 10, y: 0, z: 0}\n"
            "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
            "  m_Father: {fileID: 0}\n"
            "--- !u!1001 &50\n"
            "PrefabInstance:\n"
            "  m_Modification:\n"
            "    m_TransformParent: {fileID: 2}\n"
            "    m_Modifications:\n"
            "    - target: {fileID: 99, guid: abcd, type: 3}\n"
            "      propertyPath: m_LocalPosition.x\n"
            "      value: 3\n"
            "      objectReference: {fileID: 0}\n"
            "    - target: {fileID: 99, guid: abcd, type: 3}\n"
            "      propertyPath: m_LocalPosition.y\n"
            "      value: 4\n"
            "      objectReference: {fileID: 0}\n"
            "    - target: {fileID: 99, guid: abcd, type: 3}\n"
            "      propertyPath: m_LocalPosition.z\n"
            "      value: 0\n"
            "      objectReference: {fileID: 0}\n"
            "--- !u!1 &60\n"
            "GameObject:\n"
            "  m_Name: Nested\n"
            "  m_Component:\n"
            "  - component: {fileID: 61}\n"
            "  - component: {fileID: 62}\n"
            "--- !u!4 &61 stripped\n"
            "Transform:\n"
            "  m_GameObject: {fileID: 60}\n"
            "  m_PrefabInstance: {fileID: 50}\n"
            "--- !u!114 &62\n"
            "MonoBehaviour:\n"
            "  m_GameObject: {fileID: 60}\n"
            "  m_Script: {fileID: 11500000, guid: deadbeefdeadbeefdeadbeefdeadbeef}\n"
        )
        objs, _lights, _cams = unity_pack.parse_unity_yaml(text)
        nested = [o for o in objs if o["name"] == "Nested"][0]
        self.assertEqual(nested["father_id"], "2")
        self.assertAlmostEqual(nested["local_pos"][0], 3.0)
        self.assertAlmostEqual(nested["local_pos"][1], 4.0)
        self.assertAlmostEqual(nested["pos"][0], 13.0)
        self.assertAlmostEqual(nested["pos"][1], 4.0)

    def test_emits_opt_in_stubs_not_invented_components(self):
        d = tempfile.mkdtemp(prefix="upack-sys-")
        unity_pack.pack(SYSTEMS, d)
        with open(os.path.join(d, "engine.c")) as f:
            engine = f.read()
        with open(os.path.join(d, "data.c")) as f:
            data = f.read()
        self.assertIn("Mathf_Sin", engine)
        self.assertIn("engine_physics_fixed", engine)
        self.assertIn("Time_time = Time_time + Time_deltaTime", engine)
        self.assertIn("Keyboard_current", engine)
        self.assertIn("Keyboard_leftArrowKey_isPressed", engine)
        self.assertIn("Debug_Log", engine)
        self.assertIn("Pad_Start", engine)
        self.assertIn('GameObject_Find("BouncePad")', engine)
        self.assertIn("GameObject_GetComponent_Bouncer", engine)
        self.assertIn("Bouncer_get_amp", engine)
        self.assertIn("Object_ToString", engine)
        self.assertIn("GameObject_GetComponent_Rigidbody2D", engine)
        self.assertIn("engine_keyboard_connected", data)
        self.assertIn("engine_keyboard_leftArrow", data)
        self.assertIn("RenderSettings_ambient_r", data)
        self.assertIn("_Light_intensity", data)
        self.assertIn("1.5f", data)
        self.assertIn("Physics2D_gravity_y", data)
        self.assertIn("_Rigidbody2D_vel_x", data)
        self.assertIn("_Collider2D_count", data)
        self.assertIn("Camera_main_orthographicSize", data)
        self.assertIn("Camera_main_pos_z", data)
        self.assertIn("Camera_main_nearClipPlane", data)
        self.assertIn("Camera_main_farClipPlane", data)
        self.assertIn("SpriteRenderer", engine)
        self.assertIn("oz - Camera_main_pos_z", engine)
        self.assertIn("out[n].cos_z", engine)
        self.assertIn("_spr_sin", engine)
        self.assertIn("engine_physics_collide2d", engine)
        self.assertIn("_engine_tex0_rgba", data)
        self.assertIn("engine_texture_rgba", engine)
        self.assertNotIn("ParticleSystem_Emit", engine)
        self.assertNotIn("AnimationCurve_Evaluate", engine)
        self.assertNotIn("_AnimCurve0", data)
        self.assertNotIn("PARTICLE_MAX", engine)
        # MiniScene must not pull Sin / physics / input / lights in.
        d2 = tempfile.mkdtemp(prefix="upack-mini-")
        unity_pack.pack(SCENE, d2)
        with open(os.path.join(d2, "engine.c")) as f:
            mini = f.read()
        with open(os.path.join(d2, "data.c")) as f:
            mini_data = f.read()
        self.assertNotIn("Mathf_Sin", mini)
        self.assertNotIn("Physics2D_gravity", mini)
        self.assertNotIn("Keyboard_current", mini)
        self.assertNotIn("_Light_intensity", mini_data)

    def test_sprite_png_pixels_are_packed(self):
        """Editing the referenced PNG changes packed texture bytes."""
        d = tempfile.mkdtemp(prefix="upack-tex-")
        plan = unity_pack.pack(SCENE, d)
        self.assertGreaterEqual(len(plan.get("textures") or []), 1)
        tex = plan["textures"][0]
        self.assertEqual(tex["w"], 8)
        self.assertEqual(tex["h"], 8)
        # Default fixture is opaque white.
        self.assertEqual(tex["rgba"][0:4], b"\xff\xff\xff\xff")
        with open(os.path.join(d, "data.c")) as f:
            data = f.read()
        self.assertIn("255, 255, 255, 255", data)

        # Recolor the project PNG and re-pack — bytes must follow.
        red = tempfile.mkdtemp(prefix="upack-red-")
        import shutil
        shutil.copytree(SCENE, os.path.join(red, "proj"))
        proj = os.path.join(red, "proj")
        png = os.path.join(proj, "Assets", "Sprites", "quad.png")
        # 8x8 opaque red
        w, h, _old = unity_pack._load_png_rgba(png)
        import struct, zlib

        def chunk(tag, body):
            return (struct.pack(">I", len(body)) + tag + body
                    + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff))

        raw = b""
        for _y in range(h):
            raw += b"\x00" + (b"\xff\x00\x00\xff" * w)
        open(png, "wb").write(
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b"")
        )
        d2 = tempfile.mkdtemp(prefix="upack-tex2-")
        plan2 = unity_pack.pack(proj, d2)
        self.assertEqual(plan2["textures"][0]["rgba"][0:4], b"\xff\x00\x00\xff")
        with open(os.path.join(d2, "data.c")) as f:
            data2 = f.read()
        self.assertIn("255, 0, 0, 255", data2)

    def test_sprite_world_size_uses_pixels_per_unit(self):
        """Larger PNG at the same PPU/scale draws a larger half-extent."""
        root = tempfile.mkdtemp(prefix="upack-ppu-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Mark.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Mark : MonoBehaviour {\n"
                "    public void Update() { }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Mark.cs.meta"), "w") as f:
            f.write("guid: dddddddddddddddddddddddddddddddd\n")
        spr = os.path.join(root, "Assets", "Sprites")
        os.makedirs(spr)
        import struct, zlib

        def write_png(path, w, h):
            def chunk(tag, body):
                return (struct.pack(">I", len(body)) + tag + body
                        + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff))
            raw = b""
            for _y in range(h):
                raw += b"\x00" + (b"\xff\xff\xff\xff" * w)
            open(path, "wb").write(
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9))
                + chunk(b"IEND", b"")
            )

        write_png(os.path.join(spr, "small.png"), 8, 8)
        write_png(os.path.join(spr, "big.png"), 16, 16)
        with open(os.path.join(spr, "small.png.meta"), "w") as f:
            f.write(
                "guid: 11111111111111111111111111111111\n"
                "TextureImporter:\n"
                "  spritePixelsToUnits: 8\n"
            )
        with open(os.path.join(spr, "big.png.meta"), "w") as f:
            f.write(
                "guid: 22222222222222222222222222222222\n"
                "TextureImporter:\n"
                "  spritePixelsToUnits: 8\n"
            )
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Small\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "  - component: {fileID: 4}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: dddddddddddddddddddddddddddddddd}\n"
                "--- !u!212 &4\nSpriteRenderer:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n"
                "  m_Sprite: {fileID: 21300000, "
                "guid: 11111111111111111111111111111111, type: 3}\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Big\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "  - component: {fileID: 13}\n"
                "--- !u!4 &11\nTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_LocalPosition: {x: 2, y: 0, z: 0}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: dddddddddddddddddddddddddddddddd}\n"
                "--- !u!212 &13\nSpriteRenderer:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Enabled: 1\n"
                "  m_Sprite: {fileID: 21300000, "
                "guid: 22222222222222222222222222222222, type: 3}\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
            )
        objs, _a, _l, _c = unity_pack.load_project(root)
        by_name = {o["name"]: o for o in objs}
        small = by_name["Small"]["sprite"]
        big = by_name["Big"]["sprite"]
        # (8/8)*1/2 = 0.5 ; (16/8)*1/2 = 1.0
        self.assertAlmostEqual(small["half_w"], 0.5)
        self.assertAlmostEqual(small["half_h"], 0.5)
        self.assertAlmostEqual(big["half_w"], 1.0)
        self.assertAlmostEqual(big["half_h"], 1.0)
        self.assertEqual(small["pixels_per_unit"], 8.0)
        self.assertEqual(big["pixels_per_unit"], 8.0)

    def test_pixels_per_unit_defaults_to_100(self):
        self.assertEqual(
            unity_pack._pixels_per_unit("/no/such/sprite.png"), 100.0)

    def test_dangling_sprite_guid_does_not_draw(self):
        """Placeholder / missing asset guids are not invent-drawn."""
        root = tempfile.mkdtemp(prefix="upack-dang-spr-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Mark.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Mark : MonoBehaviour {\n"
                "    public void Update() { }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Mark.cs.meta"), "w") as f:
            f.write("guid: dddddddddddddddddddddddddddddddd\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Mark\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "  - component: {fileID: 4}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: dddddddddddddddddddddddddddddddd}\n"
                "--- !u!212 &4\nSpriteRenderer:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n"
                "  m_Sprite: {fileID: 21300000, "
                "guid: 11111111111111111111111111111111, type: 3}\n"
                "  m_Color: {r: 1, g: 0, b: 0, a: 1}\n"
            )
        objs, _a, _l, _c = unity_pack.load_project(root)
        self.assertIsNone(objs[0].get("sprite"))

    def test_sprite_renderer_without_sprite_does_not_draw(self):
        root = tempfile.mkdtemp(prefix="upack-empty-spr-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Mark.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Mark : MonoBehaviour {\n"
                "    public void Update() { }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Mark.cs.meta"), "w") as f:
            f.write("guid: dddddddddddddddddddddddddddddddd\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Mark\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "  - component: {fileID: 4}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: dddddddddddddddddddddddddddddddd}\n"
                "--- !u!212 &4\nSpriteRenderer:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n"
                "  m_Sprite: {fileID: 0}\n"
                "  m_Color: {r: 1, g: 0, b: 0, a: 1}\n"
            )
        objs, _a, _l, _c = unity_pack.load_project(root)
        self.assertIsNone(objs[0].get("sprite"))
        d = tempfile.mkdtemp(prefix="upack-empty-spr-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            engine = f.read()
        self.assertIn("no authored SpriteRenderers", engine)

    def test_no_default_draws_without_sprite_renderer(self):
        """Bare MonoBehaviour GameObjects are not invent-drawn."""
        root = tempfile.mkdtemp(prefix="upack-nodraw-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Ghost.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Ghost : MonoBehaviour {\n"
                "    public float speed;\n"
                "    public void Update() {\n"
                "        transform.position += new Vector2(speed * Time.deltaTime, 0);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Ghost.cs.meta"), "w") as f:
            f.write("guid: cccccccccccccccccccccccccccccccc\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Ghost\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: cccccccccccccccccccccccccccccccc}\n"
                "  speed: 1\n"
            )
        d = tempfile.mkdtemp(prefix="upack-nodraw-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            engine = f.read()
        self.assertIn("no authored SpriteRenderers", engine)
        self.assertNotIn("out[n].half_w", engine)

    def test_refuses_addcomponent_rigidbody(self):
        root = tempfile.mkdtemp(prefix="upack-refuse-rb-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "X.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class X : MonoBehaviour {\n"
                "    public void Start() { gameObject.AddComponent<Rigidbody2D>(); }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "X.cs.meta"), "w") as f:
            f.write("guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: X\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}\n"
            )
        with self.assertRaises(unity_pack.PackError) as cm:
            unity_pack.pack(root, tempfile.mkdtemp())
        self.assertIn("Rigidbody2D", cm.exception.message)

    def test_authored_rigidbody2d_is_packed(self):
        objs, _a, _l, _c = unity_pack.load_project(SYSTEMS)
        ball = [o for o in objs if o["name"] == "HeavyBall"][0]
        self.assertIsNotNone(ball.get("rigidbody2d"))
        self.assertAlmostEqual(ball["rigidbody2d"]["vel_x"], 0.5)
        d = tempfile.mkdtemp(prefix="upack-rb2d-")
        plan = unity_pack.pack(SYSTEMS, d)
        self.assertEqual(len(plan.get("rigidbody2d") or []), 1)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("engine_physics_fixed", eng)
        self.assertIn("GameObject_GetComponent_Rigidbody2D", eng)
        self.assertIn("engine_physics_collide2d", eng)
        self.assertGreaterEqual(len(plan.get("collider2d") or []), 3)
        ball_col = [o for o in objs if o["name"] == "HeavyBall"][0]["collider2d"]
        self.assertEqual(ball_col["kind"], "circle")
        ground = [o for o in objs if o["name"] == "Ground"][0]
        self.assertEqual(ground["collider2d"]["kind"], "box")
        self.assertAlmostEqual(ground["pos"][1], -2.5)
        # Authored Ice on ball/ground; BouncePad keeps Unity 2D default 0.4.
        self.assertAlmostEqual(ball_col["friction"], 0.05)
        self.assertAlmostEqual(ground["collider2d"]["friction"], 0.05)
        pad = [o for o in objs if o["name"] == "BouncePad"][0]
        self.assertAlmostEqual(pad["collider2d"]["friction"], 0.4)
        ground_row = [c for c in plan["collider2d"] if c["name"] == "Ground"][0]
        self.assertAlmostEqual(ground_row["friction"], 0.05)
        self.assertIn("_Collider2D_friction", eng)
        self.assertIn("_phys_mat_combine", eng)

    def test_default_and_authored_physics_materials_3d(self):
        root = tempfile.mkdtemp(prefix="upack-mat3d-")
        mats = os.path.join(root, "Assets", "Mats")
        os.makedirs(mats)
        with open(os.path.join(mats, "Bouncy.physicMaterial"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!134 &13400000\nPhysicMaterial:\n"
                "  m_Name: Bouncy\n"
                "  dynamicFriction: 0.3\n"
                "  staticFriction: 0.4\n"
                "  bounciness: 0.8\n"
                "  frictionCombine: 0\n"
                "  bounceCombine: 3\n"
            )
        with open(os.path.join(mats, "Bouncy.physicMaterial.meta"), "w") as f:
            f.write("guid: b2c3d4e5f60718293a4b5c6d7e8f901a\n")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Cube.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Cube : MonoBehaviour {\n"
                "    public void Update() { }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Cube.cs.meta"), "w") as f:
            f.write("guid: cccccccccccccccccccccccccccccccc\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Cube\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "  - component: {fileID: 4}\n"
                "  - component: {fileID: 5}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 2, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: cccccccccccccccccccccccccccccccc}\n"
                "--- !u!54 &4\nRigidbody:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Mass: 1\n"
                "  m_UseGravity: 1\n"
                "--- !u!65 &5\nBoxCollider:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n"
                "  m_IsTrigger: 0\n"
                "  m_Material: {fileID: 13400000, "
                "guid: b2c3d4e5f60718293a4b5c6d7e8f901a, type: 2}\n"
                "  m_Center: {x: 0, y: 0, z: 0}\n"
                "  m_Size: {x: 1, y: 1, z: 1}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Floor\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!4 &11\nTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 1}\n"
                "--- !u!65 &12\nBoxCollider:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Enabled: 1\n"
                "  m_IsTrigger: 0\n"
                "  m_Center: {x: 0, y: 0, z: 0}\n"
                "  m_Size: {x: 10, y: 0.5, z: 10}\n"
            )
        objs, _a, _l, _c = unity_pack.load_project(root)
        cube = [o for o in objs if o["name"] == "Cube"][0]
        floor = [o for o in objs if o["name"] == "Floor"][0]
        self.assertAlmostEqual(cube["collider3d"]["dynamic_friction"], 0.3)
        self.assertAlmostEqual(cube["collider3d"]["static_friction"], 0.4)
        self.assertAlmostEqual(cube["collider3d"]["bounciness"], 0.8)
        self.assertAlmostEqual(floor["collider3d"]["dynamic_friction"], 0.6)
        self.assertAlmostEqual(floor["collider3d"]["static_friction"], 0.6)
        d = tempfile.mkdtemp(prefix="upack-mat3d-out-")
        plan = unity_pack.pack(root, d)
        self.assertEqual(len(plan.get("collider3d") or []), 2)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("engine_physics_collide3d", eng)
        self.assertIn("_Collider3D_dynamic_friction", eng)
        self.assertIn("_Collider3D_static_friction", eng)

    def test_refuses_invented_particle_system(self):
        src = (
            "using UnityEngine;\n"
            "public class Spark : MonoBehaviour {\n"
            "    public void Update() {\n"
            "        ParticleSystem.Emit(0f, 0f);\n"
            "    }\n"
            "}\n"
        )
        root = tempfile.mkdtemp(prefix="upack-refuse-ps-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Spark.cs"), "w") as f:
            f.write(src)
        with open(os.path.join(scripts, "Spark.cs.meta"), "w") as f:
            f.write("guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Spark\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}\n"
            )
        with self.assertRaises(unity_pack.PackError) as cm:
            unity_pack.pack(root, tempfile.mkdtemp(prefix="upack-out-"))
        self.assertIn("ParticleSystem", cm.exception.message)

    def test_ast_find_getcomponent_chain(self):
        """cpprust paren/angle parse of Find().GetComponent<T>().field."""
        src = (
            'GameObject.Find("BouncePad").GetComponent<Bouncer>().amp'
        )
        chains = unity_pack._ast_find_getcomponent_chains(src)
        self.assertEqual(len(chains), 1)
        self.assertEqual(chains[0]["find_args"].strip('"'), "BouncePad")
        self.assertEqual(chains[0]["component"], "Bouncer")
        self.assertEqual(chains[0]["field"], "amp")

    def test_wrap_log_gameobject_tostring(self):
        """Printing a Find result uses Object.ToString (name), not the index."""
        src = 'Console_WriteLine(GameObject_Find("BouncePad"));'
        out = unity_pack._wrap_log_gameobject_tostring(src)
        self.assertEqual(
            out,
            'Console_WriteLine(Object_ToString(GameObject_Find("BouncePad")));')
        # Idempotent
        self.assertEqual(out, unity_pack._wrap_log_gameobject_tostring(out))
        dbg = 'Debug_Log(GameObject_Find("X"));'
        self.assertEqual(
            unity_pack._wrap_log_gameobject_tostring(dbg),
            'Debug_Log(Object_ToString(GameObject_Find("X")));')

    @needs_cc
    def test_find_unknown_name_returns_minus_one_at_runtime(self):
        """Find name lookup is runtime-only — unknown names pack and yield -1."""
        root = tempfile.mkdtemp(prefix="upack-find-rt-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "X.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using UnityEngine;\n"
                "public class X : MonoBehaviour {\n"
                "    public void Start() {\n"
                "        Console.WriteLine(GameObject.Find(\"Nope\"));\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "X.cs.meta"), "w") as f:
            f.write("guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Only\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
            self.assertIn('GameObject_Find("Nope")', eng)
            self.assertIn("Object_ToString", eng)
        r = subprocess.run(["make", "-C", d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        run = subprocess.run([os.path.join(d, "game")],
                             capture_output=True, text=True, cwd=d)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        # Unity prints "null" for a missing Object — not the packed -1 index.
        self.assertNotIn("-1", run.stdout)
        self.assertIn("null", run.stdout)

    @needs_cc
    def test_console_writeline_gameobject_prints_name(self):
        """Unity Object.ToString → name (UnityEngine.GameObject) on Console."""
        root = tempfile.mkdtemp(prefix="upack-go-tostring-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "X.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using UnityEngine;\n"
                "public class X : MonoBehaviour {\n"
                "    public void Start() {\n"
                "        Console.WriteLine(GameObject.Find(\"BouncePad\"));\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "X.cs.meta"), "w") as f:
            f.write("guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: BouncePad\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-go-tostring-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
            self.assertIn(
                'Console_WriteLine(Object_ToString(GameObject_Find("BouncePad")))',
                eng)
            self.assertIn("%s (UnityEngine.GameObject)", eng)
        r = subprocess.run(["make", "-C", d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        run = subprocess.run([os.path.join(d, "game")],
                             capture_output=True, text=True, cwd=d)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("BouncePad (UnityEngine.GameObject)", run.stdout)

    @needs_cc
    def test_find_getcomponent_runs(self):
        d = tempfile.mkdtemp(prefix="upack-find-run-")
        unity_pack.pack(SYSTEMS, d)
        r = subprocess.run(["make", "-C", d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        run = subprocess.run(
            [os.path.join(d, "game"), "-logFile", "-"],
            capture_output=True, text=True, cwd=d)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        # Pad Start: Find("BouncePad").GetComponent<Bouncer>().amp
        self.assertIn("0.5", run.stdout)

    def test_refuses_keyboard_without_inputsystem_using(self):
        """Bare Keyboard is not a global — needs InputSystem using or FQN."""
        root = tempfile.mkdtemp(prefix="upack-kb-scope-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "PadBare.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class PadBare : MonoBehaviour {\n"
                "    public void Update() {\n"
                "        if (Keyboard.current.leftArrowKey.isPressed) {}\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "PadBare.cs.meta"), "w") as f:
            f.write("guid: cccccccccccccccccccccccccccccccc\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: PadBare\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: cccccccccccccccccccccccccccccccc}\n"
            )
        with self.assertRaises(unity_pack.PackError) as cm:
            unity_pack.pack(root, tempfile.mkdtemp(prefix="upack-out-"))
        self.assertIn("Keyboard", cm.exception.message)
        self.assertIn("InputSystem", cm.exception.message)

    @needs_cc
    def test_debug_log_and_print_go_to_player_log(self):
        """Debug.Log / print → Unity Player.log path; not stdout unless -logFile -."""
        root = tempfile.mkdtemp(prefix="upack-log-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "ProjectSettings.asset"), "w") as f:
            f.write(
                "PlayerSettings:\n"
                "  companyName: CrustTest\n"
                "  productName: TalkerLog\n"
            )
        with open(os.path.join(scripts, "Talker.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Talker : MonoBehaviour {\n"
                "    public void Start() { Debug.Log(\"Hello World!\"); }\n"
                "    public void Update() { print(3); }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Talker.cs.meta"), "w") as f:
            f.write("guid: eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Talker\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-log-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            engine = f.read()
        self.assertIn("unity3d", engine)  # Linux path fragment in #else
        self.assertIn("CrustTest", engine)
        self.assertIn("TalkerLog", engine)
        self.assertIn("Debug_Log_s", engine)
        self.assertIn("Talker_Start", engine)
        self.assertIn('Debug_Log("Hello World!")', engine)
        self.assertIn("Debug_Log(3)", engine)
        r = subprocess.run(["make", "-C", d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        run = subprocess.run([os.path.join(d, "game")],
                             capture_output=True, text=True, cwd=d)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertNotIn("Hello World!", run.stdout)
        self.assertIn("draws=", run.stdout)
        self.assertFalse(
            os.path.isfile(os.path.join(d, "Player.log")),
            "must not write Player.log in cwd")
        log_path = unity_pack.unity_player_log_path("CrustTest", "TalkerLog")
        self.assertTrue(os.path.isfile(log_path), log_path)
        with open(log_path) as f:
            log = f.read()
        self.assertIn("Hello World!", log)
        self.assertEqual(log.count("Hello World!"), 1)
        self.assertGreaterEqual(log.count("3\n"), 60)

        # -logFile - mirrors Unity: Debug.Log goes to stdout.
        run2 = subprocess.run(
            [os.path.join(d, "game"), "-logFile", "-"],
            capture_output=True, text=True, cwd=d)
        self.assertEqual(run2.returncode, 0, run2.stderr or run2.stdout)
        self.assertIn("Hello World!", run2.stdout)

    @needs_cc
    def test_console_writeline_goes_to_stdout(self):
        root = tempfile.mkdtemp(prefix="upack-con-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "ProjectSettings.asset"), "w") as f:
            f.write(
                "PlayerSettings:\n"
                "  companyName: CrustTest\n"
                "  productName: ConsoleTalk\n"
            )
        with open(os.path.join(scripts, "Talker.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using UnityEngine;\n"
                "public class Talker : MonoBehaviour {\n"
                "    public void Start() {\n"
                "        Console.WriteLine(\"term\");\n"
                "        Debug.Log(\"file\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Talker.cs.meta"), "w") as f:
            f.write("guid: ffffffffffffffffffffffffffffffff\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Talker\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: ffffffffffffffffffffffffffffffff}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-con-out-")
        unity_pack.pack(root, d)
        r = subprocess.run(["make", "-C", d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        run = subprocess.run([os.path.join(d, "game")],
                             capture_output=True, text=True, cwd=d)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("term", run.stdout)
        self.assertNotIn("file", run.stdout)
        log_path = unity_pack.unity_player_log_path("CrustTest", "ConsoleTalk")
        with open(log_path) as f:
            self.assertIn("file", f.read())

    @needs_cc
    def test_string_plus_int_prints_digits_not_pointer_math(self):
        """C# \"\" + 1 → \"1\"; must not emit C pointer arithmetic."""
        root = tempfile.mkdtemp(prefix="upack-strcat-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Talker.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using UnityEngine;\n"
                "public class Talker : MonoBehaviour {\n"
                "    public void Start() {\n"
                "        Console.WriteLine(\"\" + 1);\n"
                "        Console.WriteLine(\"n=\" + 2);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Talker.cs.meta"), "w") as f:
            f.write("guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Talker\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-strcat-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            engine = f.read()
        self.assertIn("_str_plus", engine)
        self.assertNotIn('Console_WriteLine("" + 1)', engine)
        r = subprocess.run(["make", "-C", d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        run = subprocess.run([os.path.join(d, "game")],
                             capture_output=True, text=True, cwd=d)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        lines = [ln for ln in run.stdout.splitlines() if ln.strip()]
        self.assertTrue(any(ln == "1" for ln in lines), run.stdout)
        self.assertTrue(any(ln == "n=2" for ln in lines), run.stdout)

    def test_player_identity_from_project_settings(self):
        root = tempfile.mkdtemp(prefix="upack-id-")
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "ProjectSettings.asset"), "w") as f:
            f.write("companyName: Acme\nproductName: Rocket\n")
        c, p = unity_pack.player_identity(root)
        self.assertEqual(c, "Acme")
        self.assertEqual(p, "Rocket")
        c2, p2 = unity_pack.player_identity(
            tempfile.mkdtemp(prefix="upack-noid-"))
        self.assertEqual(c2, "DefaultCompany")
        self.assertTrue(p2.startswith("upack-noid-"))

    def test_keyboard_fqn_without_using_ok(self):
        root = tempfile.mkdtemp(prefix="upack-kb-fqn-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "PadFqn.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class PadFqn : MonoBehaviour {\n"
                "    public void Update() {\n"
                "        if (UnityEngine.InputSystem.Keyboard.current"
                ".leftArrowKey.isPressed) {}\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "PadFqn.cs.meta"), "w") as f:
            f.write("guid: dddddddddddddddddddddddddddddddd\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: PadFqn\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: dddddddddddddddddddddddddddddddd}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            engine = f.read()
        self.assertIn("Keyboard_current", engine)
        self.assertIn("Keyboard_leftArrowKey_isPressed", engine)

    def test_refuses_input_action_and_ui_invent(self):
        cases = [
            (
                "using UnityEngine;\n"
                "using UnityEngine.InputSystem;\n"
                "public class Act : MonoBehaviour {\n"
                "    public InputAction move;\n"
                "    public void Update() { move.ReadValue<float>(); }\n"
                "}\n",
                "InputAction",
            ),
            (
                "using UnityEngine;\n"
                "using UnityEngine.UI;\n"
                "public class Hud : MonoBehaviour {\n"
                "    public void Update() { }\n"
                "}\n",
                "UnityEngine.UI",
            ),
            (
                "using UnityEngine;\n"
                "public class L : MonoBehaviour {\n"
                "    public void Start() { gameObject.AddComponent<Light>(); }\n"
                "}\n",
                "Light",
            ),
        ]
        for src, needle in cases:
            root = tempfile.mkdtemp(prefix="upack-refuse-")
            scripts = os.path.join(root, "Assets", "Scripts")
            os.makedirs(scripts)
            with open(os.path.join(scripts, "X.cs"), "w") as f:
                f.write(src)
            with open(os.path.join(scripts, "X.cs.meta"), "w") as f:
                f.write("guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n")
            scene = os.path.join(root, "Assets", "Scenes")
            os.makedirs(scene)
            with open(os.path.join(scene, "S.unity"), "w") as f:
                f.write(
                    "%YAML 1.1\n"
                    "--- !u!1 &1\nGameObject:\n  m_Name: X\n"
                    "  m_Component:\n  - component: {fileID: 2}\n"
                    "  - component: {fileID: 3}\n"
                    "--- !u!4 &2\nTransform:\n"
                    "  m_GameObject: {fileID: 1}\n"
                    "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                    "--- !u!114 &3\nMonoBehaviour:\n"
                    "  m_GameObject: {fileID: 1}\n"
                    "  m_Script: {fileID: 11500000, "
                    "guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}\n"
                )
            with self.assertRaises(unity_pack.PackError) as cm:
                unity_pack.pack(root, tempfile.mkdtemp(prefix="upack-out-"))
            self.assertIn(needle, cm.exception.message)


@needs_cc
class TestSystemsRuns(unittest.TestCase):

    def test_make_game_links(self):
        d = tempfile.mkdtemp(prefix="upack-sys-make-")
        unity_pack.pack(SYSTEMS, d)
        r = subprocess.run(["make", "-C", d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        run = subprocess.run([os.path.join(d, "game")],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("draws=", run.stdout)

    def test_tick_animates_and_physics(self):
        d = tempfile.mkdtemp(prefix="upack-sys-run-")
        unity_pack.pack(SYSTEMS, d)
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "extern float Time_time;\n"
                "extern int engine_keyboard_connected;\n"
                "extern int engine_keyboard_rightArrow;\n"
                "extern float RenderSettings_ambient_r;\n"
                "extern float _Light_intensity[];\n"
                "typedef struct { float x, y, half_w, half_h;\n"
                "                 float cos_z, sin_z;\n"
                "                 float r, g, b; int tex; } EngineDraw;\n"
                "int engine_collect_draws(EngineDraw *out, int max);\n"
                "typedef struct Ball Ball;\n"
                "struct Ball { float pos_x; float pos_y; };\n"
                "extern Ball _Ball_inst_array[];\n"
                "typedef struct Pad Pad;\n"
                "struct Pad { float pos_x; float pos_y; float speed; };\n"
                "extern Pad _Pad_inst_array[];\n"
                "int main(void) {\n"
                "  float y0 = _Ball_inst_array[0].pos_y;\n"
                "  float x0 = _Pad_inst_array[0].pos_x;\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_keyboard_connected = 1;\n"
                "  engine_keyboard_rightArrow = 1;\n"
                "  RenderSettings_ambient_r = 0.5f;\n"
                "  int i;\n"
                "  for (i = 0; i < 50; i = i + 1) engine_tick();\n"
                "  EngineDraw buf[128];\n"
                "  int n = engine_collect_draws(buf, 128);\n"
                "  if (Time_time < 0.9f) return 2;\n"
                "  if (_Ball_inst_array[0].pos_y >= y0) return 3;\n"
                "  if (_Pad_inst_array[0].pos_x <= x0) return 4;\n"
                "  if (_Light_intensity[0] < 1.4f) return 5;\n"
                "  if (n != 3) return 6; /* SpriteRenderer on Bouncer+Ball+Pad */\n"
                "  { int j; int found = 0;\n"
                "    for (j = 0; j < n; j = j + 1) {\n"
                "      if (buf[j].cos_z > 0.7f && buf[j].cos_z < 0.72f\n"
                "          && buf[j].sin_z > 0.7f && buf[j].sin_z < 0.72f)\n"
                "        found = 1;\n"
                "    }\n"
                "    if (!found) return 7; /* Stick m_LocalRotation 45deg */\n"
                "  }\n"
                "  /* Ground top ≈ -2.25; ball radius ≈ 0.225 → rest y ≳ -2.05 */\n"
                "  if (_Ball_inst_array[0].pos_y < -2.1f) return 8;\n"
                "  return 0;\n"
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
        exe = os.path.join(d, "host")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)

    def test_camera_positive_z_culls_sprites(self):
        """Unity looks +Z; camera at +z with sprites at 0 draws nothing."""
        d = tempfile.mkdtemp(prefix="upack-cam-z-")
        unity_pack.pack(SYSTEMS, d)
        host = os.path.join(d, "host_camz.c")
        with open(host, "w") as f:
            f.write(
                "typedef struct { float x, y, half_w, half_h;\n"
                "                 float cos_z, sin_z;\n"
                "                 float r, g, b; int tex; } EngineDraw;\n"
                "int engine_collect_draws(EngineDraw *out, int max);\n"
                "extern float Camera_main_pos_z;\n"
                "int main(void) {\n"
                "  EngineDraw buf[128];\n"
                "  int n0 = engine_collect_draws(buf, 128);\n"
                "  if (n0 != 3) return 1;\n"
                "  Camera_main_pos_z = 10.f;\n"
                "  int n1 = engine_collect_draws(buf, 128);\n"
                "  if (n1 != 0) return 2;\n"
                "  Camera_main_pos_z = -10.f;\n"
                "  int n2 = engine_collect_draws(buf, 128);\n"
                "  if (n2 != 3) return 3;\n"
                "  return 0;\n"
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
        exe = os.path.join(d, "host_camz")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
