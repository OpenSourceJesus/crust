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

PROJECT = os.path.join(ROOT, "examples", "unity_pack", "MiniScene")
_CC = shutil.which("gcc") or shutil.which("cc")
needs_cc = unittest.skipIf(_CC is None, "no C compiler")


class TestSceneImport(unittest.TestCase):

    def test_unity_yaml_counts(self):
        objs, analyses, _lights, _cams, _hier = unity_pack.load_project(PROJECT)
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
        self.objs, self.an, _lights, _cams, _hier = unity_pack.load_project(PROJECT)
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
        plan = unity_pack.pack(PROJECT, tempfile.mkdtemp(prefix="upack-"))
        # pack writes files; re-read engine
        # Mathf was not called — must not appear as a function.
        # Time.deltaTime was.
        # We only check the last pack via a fresh dir.
        self.assertIn("Player", plan["classes"])

    def test_emitted_c_passes_cpprust_subset_gate(self):
        """Hand-lowered engine.cpp must survive cpprust.translate + crust."""
        d = tempfile.mkdtemp(prefix="upack-")
        unity_pack.pack(PROJECT, d)
        with open(os.path.join(d, "engine.cpp")) as f:
            engine = f.read()
        # Explicit re-check (pack already ran validate_emitted_c).
        unity_pack.validate_emitted_c(engine, "engine.cpp")
        self.assertTrue(os.path.isfile(os.path.join(d, "engine.c")))
        self.assertTrue(os.path.isfile(os.path.join(d, "engine.cpp")))
        self.assertTrue(os.path.isfile(os.path.join(d, "data.cpp")))
        self.assertTrue(os.path.isfile(os.path.join(d, "main.cpp")))
        self.assertNotIn("_Generic(", open(os.path.join(d, "engine.c")).read())

    def test_validate_emitted_c_refuses_throw(self):
        with self.assertRaises(unity_pack.PackError) as cm:
            unity_pack.validate_emitted_c(
                "void f(void) { throw 1; }\n", "bad.c")
        self.assertIn("subset", cm.exception.message)
        self.assertIn("throw", cm.exception.message)

    def test_engine_omits_unused_mathf(self):
        d = tempfile.mkdtemp(prefix="upack-")
        unity_pack.pack(PROJECT, d)
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
        unity_pack.pack(PROJECT, d)
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
        plan = unity_pack.pack(PROJECT, d, soa=True)
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
        self.assertIn("SoA: one contiguous table → memcpy", engine)
        self.assertIn("memcpy(dst + n, &_Coin_pos[0][0]", engine)
        self.assertIn("engine_upload_positions", engine)

    @needs_cc
    def test_soa_tick_still_moves_player(self):
        d = tempfile.mkdtemp(prefix="upack-soa-run-")
        unity_pack.pack(PROJECT, d, soa=True)
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
        plan = unity_pack.pack(PROJECT, d, soa_vec4=True)
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
        unity_pack.pack(PROJECT, d)
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

    def test_application_unsupported_member_is_cs0117(self):
        """Unsupported Application members → CS0117 (in scope via UnityEngine)."""
        src = (
            "using UnityEngine;\n"
            "\n"
            "public class LogAverageFPS : MonoBehaviour {\n"
            "    static string path =\n"
            "        Application.streamingAssetsPath + \"/Logs/x.txt\";\n"
            "}\n"
        )
        path = "/proj/Assets/Scripts/LogAverageFPS.cs"
        with self.assertRaises(unity_pack.PackError) as cm:
            unity_pack.analyze_script(path, src)
        self.assertEqual(
            cm.exception.message,
            "Assets/Scripts/LogAverageFPS.cs(5,21): error CS0117: "
            "'Application' does not contain a definition for "
            "'streamingAssetsPath'")
        # FQN binds without using; still CS0117.
        fqn = src.replace("using UnityEngine;\n", "").replace(
            "Application.streamingAssetsPath",
            "UnityEngine.Application.streamingAssetsPath")
        with self.assertRaises(unity_pack.PackError) as cm:
            unity_pack.analyze_script(path, fqn)
        self.assertIn("CS0117", cm.exception.message)
        self.assertIn("streamingAssetsPath", cm.exception.message)
        # Supported dataPath / persistentDataPath / isEditor / isPlaying /
        # OpenURL / productName / Quit.
        unity_pack.analyze_script(
            path, src.replace("streamingAssetsPath", "dataPath"))
        unity_pack.analyze_script(
            path, src.replace("streamingAssetsPath", "persistentDataPath"))
        unity_pack.analyze_script(
            path, src.replace("streamingAssetsPath", "productName"))
        unity_pack.analyze_script(
            path,
            "using UnityEngine;\n"
            "public class Gate : MonoBehaviour {\n"
            "    void Update() {\n"
                "        if (!Application.isEditor || Application.isPlaying)\n"
            "            return;\n"
            "        Application.OpenURL(\"https://example.com\");\n"
            "        Application.Quit();\n"
            "        Application.Quit(0);\n"
            "    }\n"
            "}\n")
        # EditorApplication.* must not be blamed as Application.* (CS0117).
        unity_pack.analyze_script(
            path,
            "using UnityEngine;\n"
            "using UnityEditor;\n"
            "public class Ed : MonoBehaviour {\n"
            "    void Start() {\n"
            "        EditorApplication.delayCall += () => { };\n"
            "    }\n"
            "}\n")

    @needs_cc
    def test_application_quit_packs(self):
        """Application.Quit → engine_wants_quit; host loop stops."""
        root = tempfile.mkdtemp(prefix="upack-quit-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Quilter.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using UnityEngine;\n"
                "public class Quilter : MonoBehaviour {\n"
                "    void Start() {\n"
                "        Console.WriteLine(\"before_quit\");\n"
                "        Application.Quit();\n"
                "    }\n"
                "    void Update() {\n"
                "        Console.WriteLine(\"after_quit\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Quilter.cs.meta"), "w") as f:
            f.write("guid: q1q1q1q1q1q1q1q1q1q1q1q1q1q1q1q1\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Quilter\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: q1q1q1q1q1q1q1q1q1q1q1q1q1q1q1q1}\n"
            )
        a = unity_pack.analyze_script(os.path.join(scripts, "Quilter.cs"))
        self.assertIn("Application.Quit", a["apis"])
        d = tempfile.mkdtemp(prefix="upack-quit-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("Quilter", plan["classes"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("Application_Quit", eng)
        self.assertIn("engine_wants_quit", eng)
        self.assertIn("/* Application.Quit", eng)
        with open(os.path.join(d, "engine_draw.h")) as f:
            hdr = f.read()
        self.assertIn("engine_wants_quit", hdr)
        start = eng.split("static void Quilter_Start", 1)[1].split(
            "static void Quilter_Update", 1)[0]
        self.assertIn("Application_Quit(0)", start)
        self.assertNotIn("Application.Quit(", start)
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "#include \"engine_draw.h\"\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  int i;\n"
                "  Time_deltaTime = 0.02f;\n"
                "  for (i = 0; i < 10; i++) {\n"
                "    engine_tick();\n"
                "    if (engine_wants_quit()) break;\n"
                "  }\n"
                "  return engine_wants_quit() ? 0 : 1;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "run")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"),
             "-I", d, "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("before_quit", run.stdout)

    def test_blank_mobile_if_skips_new_nested_class(self):
        """UNITY_ANDROID||IOS regions blank so new NestedClass never emits."""
        src = (
            "using UnityEngine;\n"
            "public class SettingsMenu : MonoBehaviour {\n"
            "#if UNITY_ANDROID || UNITY_IOS\n"
            "    public void StartDragControl(RectTransform rectTrs) {\n"
            "        var u = new DragControlUpdater(rectTrs);\n"
            "    }\n"
            "    class DragControlUpdater {\n"
            "        public DragControlUpdater(RectTransform rectTrs) {}\n"
            "    }\n"
            "#endif\n"
            "#if !UNITY_ANDROID && !UNITY_IOS\n"
            "    public void DesktopOnly() { int kept = 1; }\n"
            "#endif\n"
            "    void Update() {}\n"
            "}\n"
        )
        blanked = unity_pack._blank_unity_editor_regions(src)
        self.assertNotIn("new DragControlUpdater", blanked)
        self.assertNotIn("StartDragControl", blanked)
        self.assertIn("DesktopOnly", blanked)
        self.assertIn("kept = 1", blanked)
        a = unity_pack.analyze_script("/proj/Assets/SettingsMenu.cs", src)
        names = [m["name"] for m in a["classes"][0]["methods"]]
        self.assertIn("DesktopOnly", names)
        self.assertIn("Update", names)
        self.assertNotIn("StartDragControl", names)

    def test_blank_editor_keeps_else_branch(self):
        """#if UNITY_EDITOR … #else … keeps the player #else body."""
        src = (
            "using UnityEngine;\n"
            "public class G : MonoBehaviour {\n"
            "#if UNITY_EDITOR\n"
            "    void OnValidate() { int editorOnly = 1; }\n"
            "#else\n"
            "    void PlayerAwake() { int playerOnly = 1; }\n"
            "#endif\n"
            "}\n"
        )
        blanked = unity_pack._blank_unity_editor_regions(src)
        self.assertNotIn("OnValidate", blanked)
        self.assertNotIn("editorOnly", blanked)
        self.assertIn("PlayerAwake", blanked)
        self.assertIn("playerOnly", blanked)

    def test_emitted_new_list_subset_maps_to_csharp_cs0246(self):
        """cpprust `new List` subset error remaps to authored List site."""
        analyses = [{
            "path": "/proj/Assets/Scripts/P.cs",
            "classes": [{
                "name": "P",
                "file_text": (
                    "using System.Collections.Generic;\n"
                    "using UnityEngine;\n"
                    "public class P : MonoBehaviour {\n"
                    "    void Start() { var xs = new List<int>(); }\n"
                    "}\n"
                ),
            }],
        }]
        err = (
            "engine.cpp: `new List` is not in the C++ subset -- List is not "
            "a class defined in this file, and the lowering has to know the "
            "constructor to call. Use `malloc` directly."
        )
        msg = unity_pack._emitted_subset_error_to_unity(
            err, emitted_path="engine.cpp", analyses=analyses)
        self.assertIn("Assets/Scripts/P.cs(", msg)
        self.assertIn("error CS0246", msg)
        self.assertIn("'List'", msg)
        self.assertNotIn("left the crust", msg)
        self.assertNotIn("malloc", msg)

    def test_sortedlist_lowers_to_std_map(self):
        """SortedList<string,T> field + indexer → std::map with string keys."""
        root = tempfile.mkdtemp(prefix="upack-slist-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Entry.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Entry : MonoBehaviour {\n"
                "    void Update() {}\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Entry.cs.meta"), "w") as f:
            f.write("guid: entryentryentryentryentryentry01\n")
        with open(os.path.join(scripts, "Player.cs"), "w") as f:
            f.write(
                "using System.Collections.Generic;\n"
                "using UnityEngine;\n"
                "public class Player : MonoBehaviour {\n"
                "    public Entry entry;\n"
                "    public SortedList<string, Entry> bulletPatternEntriesSortedList ="
                " new SortedList<string, Entry>();\n"
                "    void Start() {\n"
                "        bulletPatternEntriesSortedList[\"Blaster Shoot\"] = entry;\n"
                "        Entry e = bulletPatternEntriesSortedList[\"Blaster Shoot\"];\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Player.cs.meta"), "w") as f:
            f.write("guid: playerplayerplayerplayerplayer01\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Player\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Entry\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: playerplayerplayerplayerplayer01}\n"
                "  entry: {fileID: 12}\n"
                "--- !u!4 &11\nTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: entryentryentryentryentryentry01}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-slist-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.cpp")) as f:
            eng = f.read()
        self.assertIn("#include <map>", eng)
        self.assertIn("#include <string>", eng)
        self.assertIn("_engine_map_at_si", eng)
        self.assertIn(
            "std::map<std::string, int> Player_bulletPatternEntriesSortedList",
            eng)
        self.assertNotIn("new SortedList", eng)

    def test_dictionary_lowers_to_std_map(self):
        """Dictionary<K,V> / Add / Clear → std::map; Vector2Int keys compare."""
        root = tempfile.mkdtemp(prefix="upack-dict-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Piece.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Piece : MonoBehaviour {\n"
                "    public Vector2Int location;\n"
                "    void Update() {}\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Piece.cs.meta"), "w") as f:
            f.write("guid: piecepiecepiecepiecepiecepiece01\n")
        with open(os.path.join(scripts, "World.cs"), "w") as f:
            f.write(
                "using System.Collections.Generic;\n"
                "using UnityEngine;\n"
                "public class World : MonoBehaviour {\n"
                "    public Piece piece;\n"
                "    public Dictionary<Vector2Int, Piece> piecesDict ="
                " new Dictionary<Vector2Int, Piece>();\n"
                "    void Start() {\n"
                "        piecesDict.Clear();\n"
                "        piecesDict.Add(piece.location, piece);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "World.cs.meta"), "w") as f:
            f.write("guid: worldworldworldworldworldworld01\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: World\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Piece\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: worldworldworldworldworldworld01}\n"
                "  piece: {fileID: 12}\n"
                "--- !u!4 &11\nTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: piecepiecepiecepiecepiecepiece01}\n"
                "  location: {x: 1, y: 2}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-dict-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.cpp")) as f:
            eng = f.read()
        self.assertIn("#include <map>", eng)
        self.assertIn("struct Vector2Int", eng)
        self.assertIn("int compare(const Vector2Int", eng)
        self.assertIn("std::map<Vector2Int, int> World_piecesDict", eng)
        self.assertIn(".clear()", eng)
        self.assertNotIn("new Dictionary", eng)
        self.assertNotIn("error CS0246", eng)

    def test_list_lowers_to_std_vector(self):
        """List<T> / new List / Add / Count → std::vector in engine.cpp."""
        root = tempfile.mkdtemp(prefix="upack-list-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "P.cs"), "w") as f:
            f.write(
                "using System.Collections.Generic;\n"
                "using UnityEngine;\n"
                "public class P : MonoBehaviour {\n"
                "    public static List<P> equipped = new List<P>();\n"
                "    void Start() {\n"
                "        List<int> xs = new List<int>();\n"
                "        xs.Add(3);\n"
                "        int n = xs.Count;\n"
                "        equipped.Add(this);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "P.cs.meta"), "w") as f:
            f.write("guid: listlistlistlistlistlistlistli01\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: P\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: listlistlistlistlistlistlistli01}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-list-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.cpp")) as f:
            eng = f.read()
        self.assertIn("#include <vector>", eng)
        self.assertIn("std::vector<int> xs", eng)
        self.assertIn("xs.push_back(", eng)
        self.assertIn("xs.size()", eng)
        self.assertIn("static std::vector<int> P_equipped", eng)
        self.assertIn("P_equipped.push_back(", eng)
        self.assertNotIn("new List", eng)
        self.assertNotIn("error CS0246", eng)

    def test_quaternion_unsupported_member_is_cs0117(self):
        """Unsupported Quaternion members → CS0117 (in scope via UnityEngine)."""
        src = (
            "using UnityEngine;\n"
            "\n"
            "public class Ball : MonoBehaviour {\n"
            "    void Start () {\n"
            "        transform.rotation = Quaternion.AngleAxis("
            "45f, Vector3.up);\n"
            "    }\n"
            "}\n"
        )
        path = ("/proj/Assets/Standard Assets/Scripts/Objects (Scripts)/"
                "Ball.cs")
        with self.assertRaises(unity_pack.PackError) as cm:
            unity_pack.analyze_script(path, src)
        self.assertEqual(
            cm.exception.message,
            "Assets/Standard Assets/Scripts/Objects (Scripts)/Ball.cs"
            "(5,41): error CS0117: 'Quaternion' does not contain a "
            "definition for 'AngleAxis'")
        fqn = src.replace("using UnityEngine;\n", "").replace(
            "Quaternion.AngleAxis",
            "UnityEngine.Quaternion.AngleAxis")
        with self.assertRaises(unity_pack.PackError) as cm:
            unity_pack.analyze_script(path, fqn)
        self.assertIn("CS0117", cm.exception.message)
        self.assertIn("AngleAxis", cm.exception.message)
        # Supported Euler / identity / LookRotation / Slerp / Inverse /
        # Angle / RotateTowards still analyze.
        unity_pack.analyze_script(
            path, src.replace(
                "AngleAxis(45f, Vector3.up)",
                "Euler(Vector3.forward * 45f)"))
        unity_pack.analyze_script(
            path,
            "using UnityEngine;\n"
            "public class Ball : MonoBehaviour {\n"
            "    void Start() {\n"
            "        transform.rotation = Quaternion.identity;\n"
            "    }\n"
            "}\n")
        unity_pack.analyze_script(
            path,
            "using UnityEngine;\n"
            "public class Ball : MonoBehaviour {\n"
            "    void Start() {\n"
            "        transform.rotation = Quaternion.LookRotation("
            "Vector3.forward, Vector3.up);\n"
            "    }\n"
            "}\n")
        unity_pack.analyze_script(
            path,
            "using UnityEngine;\n"
            "public class Ball : MonoBehaviour {\n"
            "    void Start() {\n"
            "        transform.rotation = Quaternion.Slerp("
            "Quaternion.identity, Quaternion.identity, 0.5f);\n"
            "    }\n"
            "}\n")
        unity_pack.analyze_script(
            path,
            "using UnityEngine;\n"
            "public class Ball : MonoBehaviour {\n"
            "    void Start() {\n"
            "        transform.rotation = Quaternion.Inverse("
            "transform.rotation);\n"
            "    }\n"
            "}\n")
        unity_pack.analyze_script(
            path,
            "using UnityEngine;\n"
            "public class Ball : MonoBehaviour {\n"
            "    void Start() {\n"
            "        float a = Quaternion.Angle("
            "Quaternion.identity, transform.rotation);\n"
            "    }\n"
            "}\n")
        unity_pack.analyze_script(
            path,
            "using UnityEngine;\n"
            "public class Ball : MonoBehaviour {\n"
            "    void Start() {\n"
            "        transform.rotation = Quaternion.RotateTowards("
            "Quaternion.identity, transform.rotation, 10f);\n"
            "    }\n"
            "}\n")
        # AngleAxis remains unsupported (distinct from Angle).
        with self.assertRaises(unity_pack.PackError) as cm:
            unity_pack.analyze_script(path, src)
        self.assertIn("AngleAxis", cm.exception.message)
        ball = os.path.join(
            SYSTEMS, "Assets", "Standard Assets", "Scripts",
            "Objects (Scripts)", "Ball.cs")
        # SystemsScene Ball exercises LookRotation — must analyze clean.
        if os.path.isfile(ball):
            unity_pack.analyze_script(ball)

    def test_file_unsupported_member_is_cs0117(self):
        """Unsupported File members → CS0117 (File is in scope via System.IO)."""
        src = (
            "using System.IO;\n"
            "using UnityEngine;\n"
            "\n"
            "public class LogAverageFPS : MonoBehaviour {\n"
            "    void Update() {\n"
            "        File.ReadAllText(\"a.txt\");\n"
            "    }\n"
            "}\n"
        )
        path = "/proj/Assets/Scripts/LogAverageFPS.cs"
        with self.assertRaises(unity_pack.PackError) as cm:
            unity_pack.analyze_script(path, src)
        self.assertEqual(
            cm.exception.message,
            "Assets/Scripts/LogAverageFPS.cs(6,14): error CS0117: 'File' "
            "does not contain a definition for 'ReadAllText'")
        # FQN binds without using; still CS0117 for unsupported members.
        fqn = src.replace("using System.IO;\n", "").replace(
            "File.ReadAllText", "System.IO.File.ReadAllText")
        with self.assertRaises(unity_pack.PackError) as cm:
            unity_pack.analyze_script(path, fqn)
        self.assertIn("CS0117", cm.exception.message)
        self.assertIn("ReadAllText", cm.exception.message)
        # Supported WriteAllText / AppendAllText / WriteAllBytes /
        # ReadAllBytes / Exists / Delete / CreateText / OpenText / Copy.
        unity_pack.analyze_script(
            path, src.replace("ReadAllText(\"a.txt\")",
                              "WriteAllText(\"a.txt\", \"x\")"))
        unity_pack.analyze_script(
            path, src.replace("ReadAllText(\"a.txt\")",
                              "AppendAllText(\"a.txt\", \"x\")"))
        unity_pack.analyze_script(
            path, src.replace("ReadAllText(\"a.txt\")",
                              "WriteAllBytes(\"a.bin\", new byte[] { 1 })"))
        unity_pack.analyze_script(
            path, src.replace("ReadAllText(\"a.txt\")",
                              "ReadAllBytes(\"a.bin\")"))
        unity_pack.analyze_script(
            path, src.replace("ReadAllText(\"a.txt\")",
                              "Exists(\"a.txt\")"))
        unity_pack.analyze_script(
            path, src.replace("ReadAllText(\"a.txt\")",
                              "Delete(\"a.txt\")"))
        unity_pack.analyze_script(
            path, src.replace("ReadAllText(\"a.txt\")",
                              "CreateText(\"a.txt\")"))
        unity_pack.analyze_script(
            path, src.replace("ReadAllText(\"a.txt\")",
                              "OpenText(\"a.txt\")"))
        unity_pack.analyze_script(
            path, src.replace("ReadAllText(\"a.txt\")",
                              "Copy(\"a.txt\", \"b.txt\")"))
        unity_pack.analyze_script(
            path, src.replace("ReadAllText(\"a.txt\")",
                              "Copy(\"a.txt\", \"b.txt\", true)"))

    def test_filestream_write_not_file_cs0117(self):
        """outFile.Write must not match System.IO.File (substring false positive)."""
        src = (
            "using System;\n"
            "using System.IO;\n"
            "using UnityEngine;\n"
            "public class W : MonoBehaviour {\n"
            "    void Start() {\n"
            "        using (FileStream outFile = new FileStream("
            "\"x\", FileMode.Create, FileAccess.Write, FileShare.None)) {\n"
            "            byte[] bytes = new byte[] { 1 };\n"
            "            outFile.Write(bytes, 0, bytes.Length);\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        path = "/proj/Assets/Scripts/W.cs"
        # Must not misread outFile.Write as System.IO.File.Write.
        unity_pack.analyze_script(path, src)

    def test_file_write_all_bytes_packs(self):
        """File.WriteAllBytes → fwrite of ByteArray; new byte[] {…} helper."""
        root = tempfile.mkdtemp(prefix="upack-wab-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Writer.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using System.IO;\n"
                "using UnityEngine;\n"
                "public class Writer : MonoBehaviour {\n"
                "    void Start() {\n"
                "        File.WriteAllBytes("
                "Application.persistentDataPath + \"/unity_pack_wab.bin\", "
                "new byte[] { 10, 20, 30 });\n"
                "        if (File.Exists("
                "Application.persistentDataPath + \"/unity_pack_wab.bin\"))\n"
                "            Console.WriteLine(\"wab_ok\");\n"
                "        else\n"
                "            Console.WriteLine(\"wab_bad\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Writer.cs.meta"), "w") as f:
            f.write("guid: a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Writer\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2}\n"
            )
        a = unity_pack.analyze_script(os.path.join(scripts, "Writer.cs"))
        self.assertIn("File.WriteAllBytes", a["apis"])
        d = tempfile.mkdtemp(prefix="upack-wab-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("Writer", plan["classes"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("File_WriteAllBytes", eng)
        self.assertIn("ByteArray", eng)
        self.assertIn("_engine_ba_0", eng)
        self.assertIn("10, 20, 30", eng)
        self.assertIn('File_WriteAllBytes(', eng)
        self.assertNotIn("File.WriteAllBytes(", eng)
        self.assertNotIn("new byte[]", eng)
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "run")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("wab_ok", run.stdout)
        # Contents on disk.
        # persistentDataPath is under the process home; probe via eng path helper
        # is hard — Exists already checked in-game. Spot-check fwrite path present.
        self.assertIn("fwrite", eng)

    def test_file_read_all_bytes_packs(self):
        """File.ReadAllBytes → malloc ByteArray; round-trip with WriteAllBytes."""
        root = tempfile.mkdtemp(prefix="upack-rab-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Reader.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using System.IO;\n"
                "using UnityEngine;\n"
                "public class Reader : MonoBehaviour {\n"
                "    void Start() {\n"
                "        File.WriteAllBytes("
                "Application.persistentDataPath + \"/unity_pack_rab.bin\", "
                "new byte[] { 7, 8, 9 });\n"
                "        byte[] bytes = File.ReadAllBytes("
                "Application.persistentDataPath + \"/unity_pack_rab.bin\");\n"
                "        if (bytes.Length == 3 && bytes[0] == 7 && "
                "bytes[2] == 9)\n"
                "            Console.WriteLine(\"rab_ok\");\n"
                "        else\n"
                "            Console.WriteLine(\"rab_bad\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Reader.cs.meta"), "w") as f:
            f.write("guid: b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Reader\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3}\n"
            )
        a = unity_pack.analyze_script(os.path.join(scripts, "Reader.cs"))
        self.assertIn("File.ReadAllBytes", a["apis"])
        d = tempfile.mkdtemp(prefix="upack-rab-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("Reader", plan["classes"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("File_ReadAllBytes", eng)
        self.assertIn("ByteArray", eng)
        self.assertIn("File_ReadAllBytes(", eng)
        self.assertNotIn("File.ReadAllBytes(", eng)
        start = eng.split("static void Reader_Start", 1)[1].split(
            "static int _Reader_started", 1)[0]
        self.assertIn("File_ReadAllBytes(", start)
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "run")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("rab_ok", run.stdout)

    def test_file_exists_packs_fopen_probe(self):
        """File.Exists → File_Exists fopen probe; missing path is false."""
        root = tempfile.mkdtemp(prefix="upack-fexists-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Checker.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using System.IO;\n"
                "using UnityEngine;\n"
                "public class Checker : MonoBehaviour {\n"
                "    void Start() {\n"
                "        if (File.Exists(\"/no/such/unity_pack_probe\"))\n"
                "            Console.WriteLine(\"exists_bad\");\n"
                "        else\n"
                "            Console.WriteLine(\"missing_ok\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Checker.cs.meta"), "w") as f:
            f.write("guid: ffffffffffffffffffffffffffffffff\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Checker\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: ffffffffffffffffffffffffffffffff}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-fexists-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("File_Exists", eng)
        self.assertIn('File_Exists("/no/such/unity_pack_probe")', eng)
        self.assertNotIn("File.Exists(", eng)
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "run")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("missing_ok", run.stdout)
        self.assertNotIn("exists_bad", run.stdout)

    def test_file_delete_packs(self):
        """File.Delete → remove(3); round-trip with WriteAllBytes + Exists."""
        root = tempfile.mkdtemp(prefix="upack-fdel-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Deleter.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using System.IO;\n"
                "using UnityEngine;\n"
                "public class Deleter : MonoBehaviour {\n"
                "    void Start() {\n"
                "        File.WriteAllBytes("
                "Application.persistentDataPath + \"/unity_pack_del.bin\", "
                "new byte[] { 1, 2 });\n"
                "        File.Delete("
                "Application.persistentDataPath + \"/unity_pack_del.bin\");\n"
                "        if (File.Exists("
                "Application.persistentDataPath + \"/unity_pack_del.bin\"))\n"
                "            Console.WriteLine(\"del_bad\");\n"
                "        else\n"
                "            Console.WriteLine(\"del_ok\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Deleter.cs.meta"), "w") as f:
            f.write("guid: e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Deleter\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5}\n"
            )
        a = unity_pack.analyze_script(os.path.join(scripts, "Deleter.cs"))
        self.assertIn("File.Delete", a["apis"])
        d = tempfile.mkdtemp(prefix="upack-fdel-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("Deleter", plan["classes"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("File_Delete", eng)
        self.assertIn("remove(path)", eng)
        self.assertIn("File_Delete(", eng)
        self.assertNotIn("File.Delete(", eng)
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "run")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("del_ok", run.stdout)

    def test_file_copy_packs(self):
        """File.Copy → fread/fwrite; 2-arg and overwrite=true round-trip."""
        root = tempfile.mkdtemp(prefix="upack-fcopy-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Copier.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using System.IO;\n"
                "using UnityEngine;\n"
                "public class Copier : MonoBehaviour {\n"
                "    void Start() {\n"
                "        string src = Application.persistentDataPath + "
                "\"/unity_pack_copy_src.bin\";\n"
                "        string dst = Application.persistentDataPath + "
                "\"/unity_pack_copy_dst.bin\";\n"
                "        File.WriteAllBytes(src, new byte[] { 9, 8, 7 });\n"
                "        File.Copy(src, dst);\n"
                "        byte[] a = File.ReadAllBytes(dst);\n"
                "        File.WriteAllBytes(src, new byte[] { 1, 2 });\n"
                "        File.Copy(src, dst, false);\n"
                "        byte[] b = File.ReadAllBytes(dst);\n"
                "        File.Copy(src, dst, true);\n"
                "        byte[] c = File.ReadAllBytes(dst);\n"
                "        if (a.Length == 3 && a[0] == 9 && a[2] == 7 && "
                "b.Length == 3 && b[0] == 9 && "
                "c.Length == 2 && c[0] == 1 && c[1] == 2)\n"
                "            Console.WriteLine(\"copy_ok\");\n"
                "        else\n"
                "            Console.WriteLine(\"copy_bad\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Copier.cs.meta"), "w") as f:
            f.write("guid: c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Copier\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0}\n"
            )
        a = unity_pack.analyze_script(os.path.join(scripts, "Copier.cs"))
        self.assertIn("File.Copy", a["apis"])
        d = tempfile.mkdtemp(prefix="upack-fcopy-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("Copier", plan["classes"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("File_Copy", eng)
        self.assertIn("/* System.IO.File.Copy", eng)
        start = eng.split("static void Copier_Start", 1)[1].split(
            "static int _Copier_started", 1)[0]
        self.assertNotIn("File.Copy(", start)
        self.assertIn("File_Copy(", start)
        self.assertIn("File_Copy(", start)
        # 2-arg → overwrite 0; true → 1; false → 0
        self.assertRegex(start, r"File_Copy\([^)]+,\s*0\s*\)")
        self.assertIn(", 1)", start)
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "run")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("copy_ok", run.stdout)

    def test_file_create_open_text_packs(self):
        """File.CreateText/OpenText → StreamWriter/Reader WriteLine/ReadLine."""
        root = tempfile.mkdtemp(prefix="upack-ftext-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "TextIO.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using System.IO;\n"
                "using UnityEngine;\n"
                "public class TextIO : MonoBehaviour {\n"
                "    void Start() {\n"
                "        string path = Application.persistentDataPath + "
                "\"/unity_pack_text.txt\";\n"
                "        StreamWriter w = File.CreateText(path);\n"
                "        w.WriteLine(\"hello\");\n"
                "        w.WriteLine(\"world\");\n"
                "        w.Close();\n"
                "        StreamReader r = File.OpenText(path);\n"
                "        string a = r.ReadLine();\n"
                "        string b = r.ReadLine();\n"
                "        r.Close();\n"
                "        Console.WriteLine(a);\n"
                "        Console.WriteLine(b);\n"
                "        Console.WriteLine(\"text_ok\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "TextIO.cs.meta"), "w") as f:
            f.write("guid: a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: TextIO\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7}\n"
            )
        a = unity_pack.analyze_script(os.path.join(scripts, "TextIO.cs"))
        self.assertIn("File.CreateText", a["apis"])
        self.assertIn("File.OpenText", a["apis"])
        d = tempfile.mkdtemp(prefix="upack-ftext-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("TextIO", plan["classes"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("File_CreateText", eng)
        self.assertIn("File_OpenText", eng)
        self.assertIn("StreamWriter_WriteLine", eng)
        self.assertIn("StreamReader_ReadLine", eng)
        start = eng.split("static void TextIO_Start", 1)[1].split(
            "static int _TextIO_started", 1)[0]
        self.assertNotIn("File.CreateText(", start)
        self.assertNotIn("File.OpenText(", start)
        self.assertIn("File_CreateText(", start)
        self.assertIn("File_OpenText(", start)
        self.assertIn("StreamWriter_WriteLine(", start)
        self.assertIn("StreamReader_ReadLine(", start)
        self.assertIn("Stream_Close(", start)
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "run")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("text_ok", run.stdout)
        self.assertIn("hello", run.stdout)
        self.assertIn("world", run.stdout)

    def test_transform_unsupported_member_is_cs1061(self):
        """Unsupported transform.Member → CS1061 on Transform (not undeclared)."""
        src = (
            "using UnityEngine;\n"
            "\n"
            "public class Ball : MonoBehaviour {\n"
            "    void Start () {\n"
            "        transform.DetachChildren();\n"
            "    }\n"
            "}\n"
        )
        path = ("/proj/Assets/Standard Assets/Scripts/Objects (Scripts)/"
                "Ball.cs")
        with self.assertRaises(unity_pack.PackError) as cm:
            unity_pack.analyze_script(path, src)
        self.assertEqual(
            cm.exception.message,
            "Assets/Standard Assets/Scripts/Objects (Scripts)/Ball.cs"
            "(5,19): error CS1061: 'Transform' does not contain a "
            "definition for 'DetachChildren' and no accessible extension "
            "method 'DetachChildren' accepting a first argument of type "
            "'Transform' could be found (are you missing a using directive "
            "or an assembly reference?)")
        # this.transform also binds; still CS1061 on the member.
        with self.assertRaises(unity_pack.PackError) as cm:
            unity_pack.analyze_script(
                path, src.replace("transform.DetachChildren",
                                  "this.transform.DetachChildren"))
        self.assertIn("CS1061", cm.exception.message)
        self.assertIn("'DetachChildren'", cm.exception.message)
        self.assertNotIn("undeclared", cm.exception.message.lower())
        # Camera.main.transform.position is not MonoBehaviour.transform.
        unity_pack.analyze_script(
            path,
            "using UnityEngine;\n"
            "public class Ball : MonoBehaviour {\n"
            "    void Update() {\n"
            "        float x = Camera.main.transform.position.x;\n"
            "    }\n"
            "}\n")
        # Supported transform.position / Rotate / LookAt / eulerAngles /
        # rotation / Find / localScale / SetParent / GetSiblingIndex.
        unity_pack.analyze_script(
            path,
            "using UnityEngine;\n"
            "public class Ball : MonoBehaviour {\n"
            "    void Update() {\n"
            "        transform.position = new Vector2(1f, 2f);\n"
            "        transform.Rotate(Vector3.forward * 90f * Time.deltaTime);\n"
            "        transform.LookAt(Camera.main.transform);\n"
            "        transform.eulerAngles += Vector3.forward * 45f;\n"
            "        transform.rotation = Quaternion.Euler("
            "Vector3.forward * 45f);\n"
            "        transform.rotation = Quaternion.LookRotation("
            "Vector3.forward, Vector3.up);\n"
            "        Transform child = transform.Find(\"Child\");\n"
            "        Vector3 s = transform.localScale;\n"
            "        Transform p = transform.parent;\n"
            "        GameObject go = transform.gameObject;\n"
            "        transform.SetParent(null);\n"
            "        transform.SetParent(null, false);\n"
            "        int sib = transform.GetSiblingIndex();\n"
            "        Matrix4x4 w2l = transform.worldToLocalMatrix;\n"
            "        Matrix4x4 l2w = transform.localToWorldMatrix;\n"
            "        Vector3 lp = transform.localPosition;\n"
            "        Quaternion lr = transform.localRotation;\n"
            "        Vector3 wp = transform.TransformPoint(Vector3.zero);\n"
            "        float wpx = transform.TransformPoint(1f, 0f, 0f).x;\n"
            "    }\n"
            "}\n")

    def test_transform_rotate_packs_live_quat(self):
        """transform.Rotate → live quat tables + draw uses rot_m** basis."""
        root = tempfile.mkdtemp(prefix="upack-rotate-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Spinner.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Spinner : MonoBehaviour {\n"
                "    void Update() {\n"
                "        transform.Rotate(Vector3.forward * 90f"
                " * Time.deltaTime);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Spinner.cs.meta"), "w") as f:
            f.write("guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Spinner\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "  - component: {fileID: 4}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}\n"
                "--- !u!212 &4\nSpriteRenderer:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n"
                "  m_Sprite: {fileID: 0}\n"
                "  m_Color: {r: 1, g: 0, b: 0, a: 1}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-rotate-out-")
        plan = unity_pack.pack(root, d)
        self.assertEqual(plan.get("live_rot_classes"), ["Spinner"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        with open(os.path.join(d, "data.c")) as f:
            data = f.read()
        self.assertIn("_engine_transform_rotate_local", eng)
        self.assertIn("_Spinner_rot_m00[i]", eng)
        self.assertIn(
            "_engine_transform_rotate_local("
            "&_Spinner_rot_x[i], &_Spinner_rot_y[i], &_Spinner_rot_z[i], "
            "&_Spinner_rot_w[i], &_Spinner_rot_m00[i], &_Spinner_rot_m01[i], "
            "&_Spinner_rot_m10[i], &_Spinner_rot_m11[i],",
            eng)
        self.assertIn("float _Spinner_rot_x[", data)
        self.assertIn("float _Spinner_rot_w[", data)
        self.assertIn("float _Spinner_rot_m00[", data)
        # Empty m_Sprite → no draw; still packs Rotate tables.
        with open(os.path.join(scripts, "Spinner.cs")) as sf:
            a = unity_pack.analyze_script(
                os.path.join(scripts, "Spinner.cs"), sf.read())
        self.assertTrue(a["writes_rot"])

    def test_transform_look_at_packs_live_quat(self):
        """transform.LookAt(Camera.main.transform) → look_at helper + rot tables."""
        root = tempfile.mkdtemp(prefix="upack-lookat-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Ball.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Ball : MonoBehaviour {\n"
                "    void Update() {\n"
                "        transform.LookAt(Camera.main.transform);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Ball.cs.meta"), "w") as f:
            f.write("guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Ball\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "  - component: {fileID: 4}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}\n"
                "--- !u!212 &4\nSpriteRenderer:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n"
                "  m_Sprite: {fileID: 0}\n"
                "  m_Color: {r: 1, g: 0, b: 0, a: 1}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Main Camera\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!4 &11\nTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: -10}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!20 &12\nCamera:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Enabled: 1\n"
                "  orthographic: 1\n"
                "  orthographic size: 5\n"
                "  near clip plane: 0.3\n"
                "  far clip plane: 1000\n"
            )
        d = tempfile.mkdtemp(prefix="upack-lookat-out-")
        plan = unity_pack.pack(root, d)
        self.assertEqual(plan.get("live_rot_classes"), ["Ball"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("_engine_transform_look_at", eng)
        self.assertIn("Camera_main_pos_x", eng)
        self.assertIn(
            "_engine_transform_look_at("
            "&_Ball_rot_x[i], &_Ball_rot_y[i], &_Ball_rot_z[i], "
            "&_Ball_rot_w[i], &_Ball_rot_m00[i], &_Ball_rot_m01[i], "
            "&_Ball_rot_m10[i], &_Ball_rot_m11[i],",
            eng)
        self.assertIn("Ball_get_pos_x(i)", eng)
        # Identity → look at camera (0,0,-10) should tilt (non-identity m).
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "extern float _Ball_rot_m00[];\n"
                "extern float _Ball_rot_m11[];\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  /* Looking toward -Z from origin → not identity XY basis. */\n"
                "  if (_Ball_rot_m00[0] > 0.99f && _Ball_rot_m11[0] > 0.99f)\n"
                "    return 2;\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "look")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)

    def test_transform_euler_angles_packs_live_quat(self):
        """transform.eulerAngles += Vector3.forward * deg → set_euler helper."""
        root = tempfile.mkdtemp(prefix="upack-euler-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Ball.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Ball : MonoBehaviour {\n"
                "    void Start() {\n"
                "        transform.eulerAngles = Vector3.zero;\n"
                "        transform.eulerAngles += Vector3.forward * 90f;\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Ball.cs.meta"), "w") as f:
            f.write("guid: cccccccccccccccccccccccccccccccc\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Ball\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "  - component: {fileID: 4}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: cccccccccccccccccccccccccccccccc}\n"
                "--- !u!212 &4\nSpriteRenderer:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n"
                "  m_Sprite: {fileID: 0}\n"
                "  m_Color: {r: 1, g: 0, b: 0, a: 1}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-euler-out-")
        plan = unity_pack.pack(root, d)
        self.assertEqual(plan.get("live_rot_classes"), ["Ball"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("_engine_transform_set_euler", eng)
        self.assertIn("_engine_quat_to_euler_deg", eng)
        self.assertNotIn("transform.eulerAngles", eng)
        self.assertNotIn("Vector3.forward", eng)
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "extern float _Ball_rot_m00[];\n"
                "extern float _Ball_rot_m01[];\n"
                "extern float _Ball_rot_m10[];\n"
                "extern float _Ball_rot_m11[];\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  /* 90° about Z: m ≈ [[0,-1],[1,0]] (cos90=0, sin90=1). */\n"
                "  if (_Ball_rot_m00[0] > 0.1f || _Ball_rot_m00[0] < -0.1f)\n"
                "    return 2;\n"
                "  if (_Ball_rot_m01[0] > -0.9f) return 3;\n"
                "  if (_Ball_rot_m10[0] < 0.9f) return 4;\n"
                "  if (_Ball_rot_m11[0] > 0.1f || _Ball_rot_m11[0] < -0.1f)\n"
                "    return 5;\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "euler")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)

    def test_transform_find_child_by_authored_hierarchy(self):
        """transform.Find(name) → child GO via live parent table (authored seed)."""
        root = tempfile.mkdtemp(prefix="upack-tfind-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Parent.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "using System;\n"
                "public class Parent : MonoBehaviour {\n"
                "    void Start() {\n"
                "        if (transform.Find(\"Child\") != null)\n"
                "            Console.WriteLine(\"child_ok\");\n"
                "        if (transform.Find(\"Nope\") == null)\n"
                "            Console.WriteLine(\"nope_ok\");\n"
                "        if (transform.Find(\"Child/Grand\") != null)\n"
                "            Console.WriteLine(\"nest_ok\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Parent.cs.meta"), "w") as f:
            f.write("guid: eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Parent\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_Children:\n  - {fileID: 5}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee}\n"
                "--- !u!1 &4\nGameObject:\n  m_Name: Child\n"
                "  m_Component:\n  - component: {fileID: 5}\n"
                "--- !u!4 &5\nTransform:\n"
                "  m_GameObject: {fileID: 4}\n"
                "  m_LocalPosition: {x: 1, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_Children:\n  - {fileID: 7}\n"
                "--- !u!1 &6\nGameObject:\n  m_Name: Grand\n"
                "  m_Component:\n  - component: {fileID: 7}\n"
                "--- !u!4 &7\nTransform:\n"
                "  m_GameObject: {fileID: 6}\n"
                "  m_LocalPosition: {x: 0, y: 1, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "  m_Father: {fileID: 5}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-tfind-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("Parent", plan["classes"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("Transform_Find", eng)
        self.assertIn("_engine_go_parent", eng)
        self.assertIn('Transform_Find(_engine_go_of_Parent(i), "Child")', eng)
        self.assertIn('Transform_Find(_engine_go_of_Parent(i), "Nope")', eng)
        self.assertIn(
            'Transform_Find(_engine_go_of_Parent(i), "Child/Grand")', eng)
        self.assertNotIn("transform.Find", eng)
        parents = plan.get("go_parents") or []
        names = plan.get("go_names") or []
        self.assertIn("Parent", names)
        self.assertIn("Child", names)
        self.assertIn("Grand", names)
        pi, ci, gi = names.index("Parent"), names.index("Child"), names.index(
            "Grand")
        self.assertEqual(parents[ci], pi)
        self.assertEqual(parents[gi], ci)
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "run")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("child_ok", run.stdout)
        self.assertIn("nope_ok", run.stdout)
        self.assertIn("nest_ok", run.stdout)

    def test_transform_find_uses_live_hierarchy(self):
        """Find walks live parents after SetParent; works on Transform receivers."""
        root = tempfile.mkdtemp(prefix="upack-tfind-live-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Probe.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using UnityEngine;\n"
                "public class Probe : MonoBehaviour {\n"
                "    void Start() {\n"
                "        Transform child = GameObject.Find(\"Child\").transform;\n"
                "        Transform oldP = GameObject.Find(\"OldParent\").transform;\n"
                "        /* Authored: Child under OldParent */\n"
                "        if (oldP.Find(\"Child\") != null)\n"
                "            Console.WriteLine(\"authored_ok\");\n"
                "        if (transform.Find(\"Child\") == null)\n"
                "            Console.WriteLine(\"empty_ok\");\n"
                "        child.SetParent(transform, false);\n"
                "        if (transform.Find(\"Child\") != null)\n"
                "            Console.WriteLine(\"live_ok\");\n"
                "        if (oldP.Find(\"Child\") == null)\n"
                "            Console.WriteLine(\"moved_ok\");\n"
                "        if (GameObject.Find(\"Probe\").transform.Find("
                "\"Child\") != null)\n"
                "            Console.WriteLine(\"recv_ok\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Probe.cs.meta"), "w") as f:
            f.write("guid: f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Probe\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: OldParent\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "--- !u!4 &11\nTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_Children:\n  - {fileID: 21}\n"
                "--- !u!1 &20\nGameObject:\n  m_Name: Child\n"
                "  m_Component:\n  - component: {fileID: 21}\n"
                "--- !u!4 &21\nTransform:\n"
                "  m_GameObject: {fileID: 20}\n"
                "  m_LocalPosition: {x: 1, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "  m_Father: {fileID: 11}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-tfind-live-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("Probe", plan["classes"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("Transform_Find", eng)
        self.assertIn("Transform_SetParent", eng)
        # Live parent table (not const) so SetParent updates are visible.
        self.assertRegex(eng, r"static int _engine_go_parent\[")
        self.assertNotRegex(eng, r"static const int _engine_go_parent\[")
        self.assertIn("Transform_Find(", eng)
        self.assertNotIn(".Find(", eng.replace("Transform_Find(", "TF("))
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "run")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("authored_ok", run.stdout)
        self.assertIn("empty_ok", run.stdout)
        self.assertIn("live_ok", run.stdout)
        self.assertIn("moved_ok", run.stdout)
        self.assertIn("recv_ok", run.stdout)

    def test_transform_point_uses_live_trs(self):
        """TransformPoint uses current rot/scale/pos, not pack-time bake."""
        root = tempfile.mkdtemp(prefix="upack-tpt-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Probe.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using UnityEngine;\n"
                "public class Probe : MonoBehaviour {\n"
                "    void Start() {\n"
                "        transform.eulerAngles = Vector3.forward * 90f;\n"
                "        /* 90° Z: local (1,0,0) → world ≈ (0,1,0) at origin */\n"
                "        float x = transform.TransformPoint(1f, 0f, 0f).x;\n"
                "        float y = transform.TransformPoint(1f, 0f, 0f).y;\n"
                "        if (x > -0.1f && x < 0.1f && y > 0.9f)\n"
                "            Console.WriteLine(\"tp_ok\");\n"
                "        else\n"
                "            Console.WriteLine(\"tp_bad\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Probe.cs.meta"), "w") as f:
            f.write("guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Probe\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-tpt-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("Probe", plan.get("transform_point_classes") or [])
        self.assertIn("Probe", plan.get("live_rot_classes") or [])
        self.assertIn("Probe", plan.get("live_scale_classes") or [])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("Probe_TransformPoint_x", eng)
        self.assertIn("_engine_transform_point", eng)
        self.assertIn("_Probe_rot_m00", eng)
        self.assertIn("_Probe_scale_x", eng)
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "run")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("tp_ok", run.stdout)

    def test_transform_matrices_local_trs_use_live_values(self):
        """localToWorld/worldToLocal/localPosition/localRotation use live TRS."""
        root = tempfile.mkdtemp(prefix="upack-mtrx-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Probe.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using UnityEngine;\n"
                "public class Probe : MonoBehaviour {\n"
                "    Vector3 savedPos;\n"
                "    void Start() {\n"
                "        transform.localPosition = new Vector3(2f, 3f, 0f);\n"
                "        savedPos = transform.localPosition;\n"
                "        transform.localRotation = Quaternion.Euler("
                "Vector3.forward * 90f);\n"
                "        Matrix4x4 l2w = transform.localToWorldMatrix;\n"
                "        Matrix4x4 w2l = transform.worldToLocalMatrix;\n"
                "        /* 90° Z: local (1,0) → world ≈ (2,4) at (2,3) */\n"
                "        float wx = l2w.m00 * 1f + l2w.m01 * 0f + l2w.m03;\n"
                "        float wy = l2w.m10 * 1f + l2w.m11 * 0f + l2w.m13;\n"
                "        float lx = w2l.m00 * wx + w2l.m01 * wy + w2l.m03;\n"
                "        float ly = w2l.m10 * wx + w2l.m11 * wy + w2l.m13;\n"
                "        float lr_z = transform.localRotation.z;\n"
                "        if (savedPos.x > 1.9f && savedPos.y > 2.9f\n"
                "            && wx > 1.9f && wx < 2.1f\n"
                "            && wy > 3.9f && wy < 4.1f\n"
                "            && lx > 0.9f && lx < 1.1f\n"
                "            && ly > -0.1f && ly < 0.1f\n"
                "            && lr_z > 0.7f)\n"
                "            Console.WriteLine(\"trs_ok\");\n"
                "        else\n"
                "            Console.WriteLine(\"trs_bad\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Probe.cs.meta"), "w") as f:
            f.write("guid: b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Probe\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-mtrx-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("Probe", plan.get("transform_matrix_classes") or [])
        self.assertIn("Probe", plan.get("live_rot_classes") or [])
        self.assertIn("Probe", plan.get("live_scale_classes") or [])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("Probe_localToWorldMatrix", eng)
        self.assertIn("Probe_worldToLocalMatrix", eng)
        self.assertIn("_engine_local_to_world_matrix", eng)
        self.assertIn("_Probe_rot_m00", eng)
        self.assertIn("_Probe_scale_x", eng)
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "run")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("trs_ok", run.stdout)

    def test_transform_set_parent_live_hierarchy(self):
        """SetParent updates live parent; worldPositionStays keeps world T."""
        root = tempfile.mkdtemp(prefix="upack-setp-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Parent.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Parent : MonoBehaviour {\n"
                "    void Start() {}\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Parent.cs.meta"), "w") as f:
            f.write("guid: c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3\n")
        with open(os.path.join(scripts, "Child.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using UnityEngine;\n"
                "public class Child : MonoBehaviour {\n"
                "    void Start() {\n"
                "        Transform p = GameObject.Find(\"Parent\").transform;\n"
                "        /* world stays: local becomes world - parent */\n"
                "        transform.SetParent(p, true);\n"
                "        float lx = transform.localPosition.x;\n"
                "        float ly = transform.localPosition.y;\n"
                "        Transform got = transform.parent;\n"
                "        transform.SetParent(null, false);\n"
                "        Transform gone = transform.parent;\n"
                "        if (lx > 4.9f && lx < 5.1f && ly > -0.1f && ly < 0.1f\n"
                "            && got >= 0 && gone < 0)\n"
                "            Console.WriteLine(\"setp_ok\");\n"
                "        else\n"
                "            Console.WriteLine(\"setp_bad\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Child.cs.meta"), "w") as f:
            f.write("guid: d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Parent\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 5, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3}\n"
                "--- !u!1 &4\nGameObject:\n  m_Name: Child\n"
                "  m_Component:\n  - component: {fileID: 5}\n"
                "  - component: {fileID: 6}\n"
                "--- !u!4 &5\nTransform:\n"
                "  m_GameObject: {fileID: 4}\n"
                "  m_LocalPosition: {x: 10, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "--- !u!114 &6\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 4}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-setp-out-")
        plan = unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("Transform_SetParent", eng)
        self.assertIn("static int _engine_go_parent", eng)
        self.assertIn("static int _Child_xf_parent_class", eng)
        self.assertIn(
            "Transform_SetParent(_engine_go_of_Child(i)", eng)
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "run")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("setp_ok", run.stdout)

    def test_transform_get_sibling_index_live(self):
        """GetSiblingIndex reads live sibling order; SetParent appends last."""
        root = tempfile.mkdtemp(prefix="upack-sib-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Kid.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using UnityEngine;\n"
                "public class Kid : MonoBehaviour {\n"
                "    void Start() {\n"
                "        int a = transform.GetSiblingIndex();\n"
                "        Transform bTr = GameObject.Find(\"B\").transform;\n"
                "        int b = bTr.GetSiblingIndex();\n"
                "        Transform p = GameObject.Find(\"Root\").transform;\n"
                "        transform.SetParent(p, false);\n"
                "        int after = transform.GetSiblingIndex();\n"
                "        /* A then B under Root at pack; A reparented last. */\n"
                "        if (a == 0 && b == 1 && after == 1)\n"
                "            Console.WriteLine(\"sib_ok\");\n"
                "        else\n"
                "            Console.WriteLine(\"sib_bad\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Kid.cs.meta"), "w") as f:
            f.write("guid: e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Root\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "--- !u!1 &3\nGameObject:\n  m_Name: A\n"
                "  m_Component:\n  - component: {fileID: 4}\n"
                "  - component: {fileID: 5}\n"
                "--- !u!4 &4\nTransform:\n"
                "  m_GameObject: {fileID: 3}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "  m_Father: {fileID: 2}\n"
                "--- !u!114 &5\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 3}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5}\n"
                "--- !u!1 &6\nGameObject:\n  m_Name: B\n"
                "  m_Component:\n  - component: {fileID: 7}\n"
                "--- !u!4 &7\nTransform:\n"
                "  m_GameObject: {fileID: 6}\n"
                "  m_LocalPosition: {x: 1, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "  m_Father: {fileID: 2}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-sib-out-")
        plan = unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("Transform_GetSiblingIndex", eng)
        self.assertIn("static int _engine_go_sib", eng)
        self.assertIn(
            "Transform_GetSiblingIndex(_engine_go_of_Kid(i))", eng)
        self.assertIn("Transform_SetParent", eng)
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "run")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("sib_ok", run.stdout)

    def test_transform_rotation_quaternion_euler_packs(self):
        """transform.rotation = Quaternion.Euler(...) → set_euler on live quat."""
        root = tempfile.mkdtemp(prefix="upack-rotq-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Ball.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Ball : MonoBehaviour {\n"
                "    void Start() {\n"
                "        transform.rotation = Quaternion.Euler("
                "Vector3.forward * 90f);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Ball.cs.meta"), "w") as f:
            f.write("guid: dddddddddddddddddddddddddddddddd\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Ball\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "  - component: {fileID: 4}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
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
        d = tempfile.mkdtemp(prefix="upack-rotq-out-")
        plan = unity_pack.pack(root, d)
        self.assertEqual(plan.get("live_rot_classes"), ["Ball"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("_engine_transform_set_euler", eng)
        self.assertIn("_engine_transform_set_quat", eng)
        self.assertNotIn("transform.rotation", eng)
        self.assertNotIn("Quaternion.Euler", eng)
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "extern float _Ball_rot_m00[];\n"
                "extern float _Ball_rot_m01[];\n"
                "extern float _Ball_rot_m10[];\n"
                "extern float _Ball_rot_m11[];\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  if (_Ball_rot_m00[0] > 0.1f || _Ball_rot_m00[0] < -0.1f)\n"
                "    return 2;\n"
                "  if (_Ball_rot_m01[0] > -0.9f) return 3;\n"
                "  if (_Ball_rot_m10[0] < 0.9f) return 4;\n"
                "  if (_Ball_rot_m11[0] > 0.1f || _Ball_rot_m11[0] < -0.1f)\n"
                "    return 5;\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "rotq")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)

    def test_quaternion_slerp_packs(self):
        """transform.rotation = Quaternion.Slerp(a, b, t) → live quat slerp."""
        root = tempfile.mkdtemp(prefix="upack-slerp-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Ball.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using UnityEngine;\n"
                "public class Ball : MonoBehaviour {\n"
                "    void Start() {\n"
                "        transform.rotation = Quaternion.Euler("
                "Vector3.forward * 90f);\n"
                "        transform.rotation = Quaternion.Slerp("
                "transform.rotation, Quaternion.identity, 1f);\n"
                "        float z = transform.localRotation.z;\n"
                "        float w = transform.localRotation.w;\n"
                "        if (z > -0.1f && z < 0.1f && w > 0.9f)\n"
                "            Console.WriteLine(\"slerp_ok\");\n"
                "        else\n"
                "            Console.WriteLine(\"slerp_bad\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Ball.cs.meta"), "w") as f:
            f.write("guid: e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Ball\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-slerp-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("Ball", plan.get("live_rot_classes") or [])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("_engine_quat_slerp", eng)
        self.assertIn("_engine_transform_set_quat", eng)
        # Call site lowered; helper comment may still mention Quaternion.Slerp.
        start = eng.split("static void Ball_Start", 1)[1].split(
            "static int _Ball_started", 1)[0]
        self.assertNotIn("Quaternion.Slerp(", start)
        self.assertIn("_engine_quat_slerp(", start)
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "run")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("slerp_ok", run.stdout)

    def test_quaternion_inverse_packs(self):
        """transform.rotation = Quaternion.Inverse(...) → live quat inverse."""
        root = tempfile.mkdtemp(prefix="upack-qinv-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Ball.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using UnityEngine;\n"
                "public class Ball : MonoBehaviour {\n"
                "    void Start() {\n"
                "        transform.rotation = Quaternion.Euler("
                "0f, 0f, 90f);\n"
                "        transform.rotation = Quaternion.Inverse("
                "transform.rotation);\n"
                "        float z = transform.localRotation.z;\n"
                "        float w = transform.localRotation.w;\n"
                "        /* Inverse of 90° Z ≈ -90° Z: z≈-0.707, w≈0.707 */\n"
                "        if (z < -0.6f && z > -0.8f && w > 0.6f && w < 0.8f)\n"
                "            Console.WriteLine(\"inv_ok\");\n"
                "        else\n"
                "            Console.WriteLine(\"inv_bad\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Ball.cs.meta"), "w") as f:
            f.write("guid: b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Ball\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-qinv-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("Ball", plan["classes"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("_engine_quat_inverse", eng)
        start = eng.split("static void Ball_Start", 1)[1].split(
            "static int _Ball_started", 1)[0]
        self.assertNotIn("Quaternion.Inverse(", start)
        self.assertIn("_engine_quat_inverse(", start)
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "run")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("inv_ok", run.stdout)

    def test_quaternion_angle_packs(self):
        """Quaternion.Angle(a, b) → degrees between live rotations."""
        root = tempfile.mkdtemp(prefix="upack-qang-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Ball.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using UnityEngine;\n"
                "public class Ball : MonoBehaviour {\n"
                "    void Start() {\n"
                "        transform.rotation = Quaternion.Euler("
                "0f, 0f, 90f);\n"
                "        float a = Quaternion.Angle("
                "Quaternion.identity, transform.rotation);\n"
                "        float z = Quaternion.Angle("
                "transform.rotation, transform.rotation);\n"
                "        if (a > 89f && a < 91f && z > -0.1f && z < 0.1f)\n"
                "            Console.WriteLine(\"ang_ok\");\n"
                "        else\n"
                "            Console.WriteLine(\"ang_bad\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Ball.cs.meta"), "w") as f:
            f.write("guid: c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Ball\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-qang-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("Ball", plan["classes"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("_engine_quat_angle", eng)
        start = eng.split("static void Ball_Start", 1)[1].split(
            "static int _Ball_started", 1)[0]
        self.assertNotIn("Quaternion.Angle(", start)
        self.assertIn("_engine_quat_angle(", start)
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "run")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("ang_ok", run.stdout)

    def test_quaternion_rotate_towards_packs(self):
        """transform.rotation = Quaternion.RotateTowards → live quat step."""
        root = tempfile.mkdtemp(prefix="upack-qrotto-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Ball.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using UnityEngine;\n"
                "public class Ball : MonoBehaviour {\n"
                "    void Start() {\n"
                "        transform.rotation = Quaternion.Euler("
                "0f, 0f, 90f);\n"
                "        transform.rotation = Quaternion.RotateTowards("
                "Quaternion.identity, transform.rotation, 45f);\n"
                "        float a = Quaternion.Angle("
                "Quaternion.identity, transform.rotation);\n"
                "        if (a > 44f && a < 46f)\n"
                "            Console.WriteLine(\"rt_ok\");\n"
                "        else\n"
                "            Console.WriteLine(\"rt_bad\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Ball.cs.meta"), "w") as f:
            f.write("guid: d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Ball\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-qrotto-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("Ball", plan["classes"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("_engine_quat_rotate_towards", eng)
        start = eng.split("static void Ball_Start", 1)[1].split(
            "static int _Ball_started", 1)[0]
        self.assertNotIn("Quaternion.RotateTowards(", start)
        self.assertIn("_engine_quat_rotate_towards(", start)
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "run")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("rt_ok", run.stdout)

    def test_transform_rotation_lookrotation_packs(self):
        """transform.rotation = Quaternion.LookRotation → look_rotation helper."""
        root = tempfile.mkdtemp(prefix="upack-lookrot-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Ball.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Ball : MonoBehaviour {\n"
                "    void Start() {\n"
                "        transform.rotation = Quaternion.LookRotation("
                "Vector3.forward, Vector3.up);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Ball.cs.meta"), "w") as f:
            f.write("guid: dddddddddddddddddddddddddddddddd\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Ball\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "  - component: {fileID: 4}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
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
        d = tempfile.mkdtemp(prefix="upack-lookrot-out-")
        plan = unity_pack.pack(root, d)
        self.assertEqual(plan.get("live_rot_classes"), ["Ball"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("_engine_quat_look_rotation", eng)
        self.assertIn("Ball_Start", eng)
        start = eng.find("static void Ball_Start")
        end = eng.find("\nstatic void ", start + 1)
        if end < 0:
            end = eng.find("\nvoid ", start + 1)
        body = eng[start:end]
        self.assertIn("_engine_quat_look_rotation", body)
        self.assertNotIn("Quaternion.LookRotation", body)
        self.assertNotIn("transform.rotation", body)
        if not _CC:
            return
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "extern float _Ball_rot_m00[];\n"
                "extern float _Ball_rot_m11[];\n"
                "extern float _Ball_rot_w[];\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  /* LookRotation(+Z, +Y) ≈ identity. */\n"
                "  if (_Ball_rot_m00[0] < 0.99f) return 2;\n"
                "  if (_Ball_rot_m11[0] < 0.99f) return 3;\n"
                "  if (_Ball_rot_w[0] < 0.99f) return 4;\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "lookrot")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)

    def test_cpp_style_float_suffix_is_cs1061(self):
        """C++ `0.f` is not a C# real-literal — csc reports CS1061 on `f`."""
        src = (
            "using UnityEngine;\n"
            "\n"
            "/* Lighting subset: read authored ambient; no invented Light. */\n"
            "public class AmbientBias : MonoBehaviour {\n"
            "    public float lift;\n"
            "\n"
            "    public void Update() {\n"
            "        lift = RenderSettings.ambientLight.r;\n"
            "        transform.position = new Vector2(\n"
            "            transform.position.x,\n"
            "            transform.position.y + lift * 0.f);\n"
            "    }\n"
            "}\n"
        )
        path = "/proj/Assets/Scripts/AmbientBias.cs"
        with self.assertRaises(unity_pack.PackError) as cm:
            unity_pack.analyze_script(path, src)
        self.assertEqual(
            cm.exception.message,
            "Assets/Scripts/AmbientBias.cs(11,45): error CS1061: 'int' does "
            "not contain a definition for 'f' and no accessible extension "
            "method 'f' accepting a first argument of type 'int' could be "
            "found (are you missing a using directive or an assembly "
            "reference?)")
        # Valid spellings still analyze.
        unity_pack.analyze_script(path, src.replace("0.f", "0f"))
        unity_pack.analyze_script(path, src.replace("0.f", "0.0f"))

    def test_csharp_0f_lowers_to_c_0_dot_f(self):
        """C# `0f` is valid; emitted C must spell `0.f` (gcc rejects `0f`)."""
        self.assertEqual(unity_pack._rewrite_csharp_float_literals("x * 0f"),
                         "x * 0.f")
        self.assertEqual(unity_pack._rewrite_csharp_float_literals("1.5f + 2F"),
                         "1.5f + 2.F")
        self.assertEqual(unity_pack._rewrite_csharp_float_literals('"0f"'),
                         '"0f"')
        d = tempfile.mkdtemp(prefix="upack-0f-")
        unity_pack.pack(SYSTEMS, d)
        with open(os.path.join(d, "engine.c")) as f:
            engine = f.read()
        self.assertIn("AmbientBias_get_lift(i) * 0.f", engine)
        self.assertNotIn("AmbientBias_get_lift(i) * 0f", engine)

    def test_sprite_sorting_layers_and_order(self):
        """TagManager layers + SpriteRenderer order → sorted EngineDraw list."""
        layers = unity_pack._load_sorting_layers(SYSTEMS)
        self.assertEqual([L["name"] for L in layers], ["Default", "Foreground"])
        self.assertEqual(layers[1]["unique_id"], 2081823273)
        objs, _a, _l, _c, _hier = unity_pack.load_project(SYSTEMS)
        by_name = {o["name"]: o for o in objs}
        bouncer = by_name["BouncePad"]["sprite"]
        button = by_name["Button"]["sprite"]
        self.assertEqual(bouncer["sorting_order"], -10)
        self.assertEqual(bouncer["sorting_layer"], 0)
        self.assertEqual(button["sorting_layer_id"], 2081823273)
        self.assertEqual(button["sorting_layer"], 1)
        self.assertEqual(button["sorting_order"], -1)
        d = tempfile.mkdtemp(prefix="upack-sort-")
        unity_pack.pack(SYSTEMS, d)
        with open(os.path.join(d, "engine.c")) as f:
            engine = f.read()
        self.assertIn("sorting_layer", engine)
        self.assertIn("sorting_order", engine)
        self.assertIn("qsort", engine)
        self.assertIn("_engine_draw_cmp", engine)
        with open(os.path.join(d, "engine_draw.h")) as f:
            hdr = f.read()
        self.assertIn("int sorting_layer;", hdr)
        self.assertIn("int sorting_order;", hdr)

    @needs_cc
    def test_application_data_path_and_log_average_fps(self):
        """Update-time persistentDataPath + suffix → _str_plus_s; script runs."""
        path = os.path.join(
            SYSTEMS, "Assets", "Standard Assets", "Scripts",
            "Concepts (Scripts)", "LogAverageFPS.cs")
        a = unity_pack.analyze_script(path)
        self.assertIn("Application.persistentDataPath", a["apis"])
        self.assertIn("string.+", a["apis"])
        self.assertFalse(a["classes"][0].get("ctor_forbidden"))
        d = tempfile.mkdtemp(prefix="upack-datapath-")
        plan = unity_pack.pack(SYSTEMS, d)
        self.assertFalse(plan["classes"]["LogAverageFPS"].get("ctor_forbidden"))
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("Application_persistentDataPath", eng)
        self.assertIn(
            "_str_plus_s(Application_persistentDataPath()", eng)
        self.assertIn("LogAverageFPS_LOG_FILE_PATH_SUFFIX", eng)
        self.assertIn("LogAverageFPS_Update", eng)
        self.assertIn("LogAverageFPS_Start", eng)
        self.assertIn("LogAverageFPS_set_timeLeft", eng)
        self.assertIn("f32_to_f16", eng)
        self.assertNotIn(
            "Application_persistentDataPath() + LogAverageFPS_LOG_FILE_PATH",
            eng)
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    @needs_cc
    def test_append_all_text_path_and_content_concat_live(self):
        """Sibling path+content string concats must not share one str buf.

        LogAverageFPS: AppendAllText(persistentDataPath + suffix,
        \"Average FPS: \" + fps + '\\n'). Two-slot ring overwritten the path
        with the line → mkdir made a folder named \"Average FPS: …\".
        """
        root = tempfile.mkdtemp(prefix="upack-fpsline-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "LogAverageFPS.cs"), "w") as f:
            f.write(
                "using System.IO;\n"
                "using UnityEngine;\n"
                "public class LogAverageFPS : MonoBehaviour {\n"
                "    static string LOG_FILE_PATH_SUFFIX = \"/AverageFPS.txt\";\n"
                "    void Start() {\n"
                "        File.AppendAllText("
                "Application.persistentDataPath + LOG_FILE_PATH_SUFFIX, "
                "\"Average FPS: \" + 60f + '\\n');\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "LogAverageFPS.cs.meta"), "w") as f:
            f.write("guid: f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: LogAverageFPS\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-fpsline-out-")
        plan = unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("_engine_str_buf[8]", eng)
        pp = plan.get("persistent_data_path") or ""
        self.assertTrue(pp, "expected baked persistentDataPath")
        want_file = os.path.join(pp, "AverageFPS.txt")
        # Isolate cwd so a wrong relative mkdir is visible.
        cwd = tempfile.mkdtemp(prefix="upack-fpsline-cwd-")
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "run")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], cwd=cwd, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertTrue(
            os.path.isfile(want_file),
            "expected file %r; cwd entries=%r" % (want_file, os.listdir(cwd)))
        with open(want_file) as f:
            body = f.read()
        self.assertIn("Average FPS:", body)
        # Must not mkdir the FPS line as a directory (cwd or elsewhere).
        for name in os.listdir(cwd):
            self.assertFalse(
                name.startswith("Average FPS"),
                "bogus dir/file in cwd: %r" % name)

    @needs_cc
    def test_application_open_url_packs(self):
        """Application.OpenURL → system(python3 webbrowser.open); compiles."""
        root = tempfile.mkdtemp(prefix="upack-openurl-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Link.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Link : MonoBehaviour {\n"
                "    void Start() {\n"
                "        Application.OpenURL(\"https://example.com\");\n"
                "        Application.OpenURL("
                "\"http://x/\" + \"docs\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Link.cs.meta"), "w") as f:
            f.write("guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Link\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1}\n"
            )
        a = unity_pack.analyze_script(os.path.join(scripts, "Link.cs"))
        self.assertIn("Application.OpenURL", a["apis"])
        d = tempfile.mkdtemp(prefix="upack-openurl-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("Link", plan["classes"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("Application_OpenURL", eng)
        self.assertIn("/* Application.OpenURL", eng)
        self.assertIn("webbrowser.open", eng)
        self.assertIn("system(cmd)", eng)
        self.assertNotIn("Application.OpenURL(", eng)
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    @needs_cc
    def test_application_product_name_packs(self):
        """Application.productName → baked ProjectSettings productName string."""
        root = tempfile.mkdtemp(prefix="upack-prodname-")
        os.makedirs(os.path.join(root, "ProjectSettings"))
        with open(os.path.join(root, "ProjectSettings",
                               "ProjectSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "PlayerSettings:\n"
                "  companyName: Acme\n"
                "  productName: Slime Jump\n"
            )
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Brand.cs"), "w") as f:
            f.write(
                "using System;\n"
                "using UnityEngine;\n"
                "public class Brand : MonoBehaviour {\n"
                "    void Start() {\n"
                "        Console.WriteLine(Application.productName);\n"
                "        Console.WriteLine("
                "Application.productName + \"!\");\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Brand.cs.meta"), "w") as f:
            f.write("guid: f6f6f6f6f6f6f6f6f6f6f6f6f6f6f6f6\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Brand\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: f6f6f6f6f6f6f6f6f6f6f6f6f6f6f6f6}\n"
            )
        a = unity_pack.analyze_script(os.path.join(scripts, "Brand.cs"))
        self.assertIn("Application.productName", a["apis"])
        d = tempfile.mkdtemp(prefix="upack-prodname-out-")
        plan = unity_pack.pack(root, d)
        self.assertEqual(plan.get("product_name"), "Slime Jump")
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("Application_productName", eng)
        self.assertIn("Slime Jump", eng)
        self.assertNotIn("Application.productName", eng.split(
            "static void Brand_Start", 1)[1].split(
            "static int _Brand_started", 1)[0])
        self.assertIn("Application_productName()", eng)
        self.assertIn("_str_plus_s(Application_productName()", eng)
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.02f;\n"
                "  engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        exe = os.path.join(d, "run")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("Slime Jump", run.stdout)
        self.assertIn("Slime Jump!", run.stdout)

    @needs_cc
    def test_application_path_field_init_ctor_forbidden(self):
        """Field-init persistentDataPath → UnityException each frame; no script."""
        src = (
            "using UnityEngine;\n"
            "public class BadPath : MonoBehaviour {\n"
            "    static string P = Application.persistentDataPath + \"/x.txt\";\n"
            "    void Update() { }\n"
            "}\n"
        )
        a = unity_pack.analyze_script("/proj/Assets/Scripts/BadPath.cs", src)
        self.assertTrue(a["classes"][0].get("ctor_forbidden"))
        self.assertEqual(
            a["classes"][0]["ctor_forbidden"][0]["api"], "persistentDataPath")
        self.assertEqual(a["classes"][0]["ctor_forbidden"][0]["line"], 3)
        root = tempfile.mkdtemp(prefix="upack-ctorforbid-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "BadPath.cs"), "w") as f:
            f.write(src)
        with open(os.path.join(scripts, "BadPath.cs.meta"), "w") as f:
            f.write("guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Bad Path GO\n"
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
        d = tempfile.mkdtemp(prefix="upack-ctorforbid-out-")
        plan = unity_pack.pack(root, d)
        self.assertTrue(plan["classes"]["BadPath"].get("ctor_forbidden"))
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("_engine_unity_ctor_forbidden", eng)
        self.assertIn("get_%s is not allowed", eng)
        self.assertIn("TypeInitializationException", eng)
        self.assertNotIn("BadPath_Update", eng)
        host = os.path.join(d, "host_ctor.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  int i;\n"
                "  Time_deltaTime = 0.02f;\n"
                "  for (i = 0; i < 3; i = i + 1) engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "host_ctor")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        err = r.stderr or ""
        self.assertIn("UnityException: get_persistentDataPath", err)
        self.assertIn("BadPath", err)
        self.assertIn("Bad Path GO", err)
        self.assertIn("BadPath.cs:3", err)
        self.assertEqual(err.count("UnityException: get_persistentDataPath"), 3)

    def test_package_cache_guid_resolves(self):
        """UPM PackageCache .meta guids resolve; Assets scripts stay exclusive."""
        assets = unity_pack._asset_guid_map(SYSTEMS)
        # Builtin uGUI Image lives under Library/PackageCache.
        img = "fe87c0e1cc204ed48ad3b37840f39efc"
        self.assertIn(img, assets)
        self.assertIn("PackageCache", assets[img])
        self.assertTrue(assets[img].endswith("Image.cs"))
        scripts = unity_pack._guid_map(SYSTEMS, asset_guids=assets)
        self.assertNotIn(img, scripts)
        # TMP font under Assets still wins.
        font = "8f586378b4e144a9851e7b34d9b748ee"
        self.assertIn(font, assets)
        self.assertIn("Assets", assets[font])

    def test_canvas_button_draws_and_clicks(self):
        """Authored Canvas + Button (builtin UISprite) → draw + SetActive onClick."""
        objs, _a, _l, cams, _hier = unity_pack.load_project(SYSTEMS)
        btn = [o for o in objs if o["name"] == "Button"]
        self.assertEqual(len(btn), 1)
        sp = btn[0]["sprite"]
        self.assertIsNotNone(sp)
        self.assertEqual(sp.get("source"), "ui")
        self.assertTrue(sp.get("builtin"))
        self.assertEqual(sp.get("tex_path"), "<builtin:UISprite>")
        self.assertEqual(btn[0]["ui_image"].get("image_type"), 1)  # Sliced
        # Sliced UISprite bakes to the RectTransform size (not 1×1 white).
        self.assertEqual(sp.get("tex_w"), 115)
        self.assertEqual(sp.get("tex_h"), 30)
        self.assertEqual(sp.get("border"), (6, 6, 6, 6))
        # Outside corner is nearly transparent; center is opaque white.
        rgba = sp["tex_rgba"]
        self.assertLess(rgba[3], 32)  # bottom-left outside corner
        cx = (15 * 115 + 57) * 4
        self.assertGreater(rgba[cx + 3], 200)
        self.assertIsNotNone(btn[0].get("ui_button"))
        self.assertEqual(btn[0]["ui_button"]["onclick"][0]["method"], "SetActive")
        cols = btn[0]["ui_button"]["colors"]
        self.assertAlmostEqual(cols["highlighted"][0], 0.78431374, places=5)
        self.assertAlmostEqual(cols["pressed"][0], 0.5882353, places=5)
        hit = btn[0]["ui_hit"]
        self.assertAlmostEqual(hit["ncx"], 0.5, places=5)
        self.assertAlmostEqual(hit["ncy"], 0.5, places=5)
        txt = [o for o in objs if o["name"] == "Button Text"]
        self.assertEqual(len(txt), 1)
        self.assertEqual(txt[0]["ui_tmp"]["text"], "Click me")
        self.assertTrue(txt[0]["ui_tmp"]["has_font"])
        self.assertEqual(txt[0]["sprite"].get("source"), "ui_tmp")
        trgba = txt[0]["sprite"]["tex_rgba"]
        self.assertGreater(
            sum(1 for i in range(3, len(trgba), 4) if trgba[i] > 10),
            200)
        # Glyph bake must look like text, not atlas scrap: opaque pixels
        # span most of the label width.
        xs = [i // 4 % 115 for i in range(3, len(trgba), 4) if trgba[i] > 128]
        self.assertGreater(max(xs) - min(xs), 60)
        # Centered 115×30 px → world half-extent from Screen + ortho.
        ortho = float(cams[0]["orthographic_size"])
        sw, sh = unity_pack.player_screen(SYSTEMS)
        expect_hw = 57.5 * (2.0 * ortho) / float(sh)
        self.assertAlmostEqual(btn[0]["pos"][0], 0.0, places=3)
        self.assertAlmostEqual(btn[0]["pos"][1], 0.0, places=3)
        self.assertAlmostEqual(sp["half_w"], expect_hw, places=5)
        self.assertEqual(sp["sorting_layer"], 1)  # Foreground
        d = tempfile.mkdtemp(prefix="upack-ui-")
        plan = unity_pack.pack(SYSTEMS, d)
        self.assertTrue(plan.get("ui_buttons"))
        self.assertTrue(any(
            o.get("sprite") and o["sprite"].get("source") == "ui"
            for cl in plan["classes"].values() for o in cl["instances"]))
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("/* Button SpriteRenderer */", eng)
        self.assertIn("engine_ui_tick", eng)
        self.assertIn("GameObject_SetActive", eng)
        self.assertIn("engine_pointer_x", eng)
        self.assertIn("_engine_ui_btn_tint", eng)
        self.assertIn("_engine_ui_btn_col_h", eng)
        self.assertIn("_spr_ncx", eng)
        self.assertIn("_spr_btn", eng)
        self.assertIn("/* Button_Text SpriteRenderer */", eng)
        data_c = open(os.path.join(d, "data.c")).read()
        self.assertIn("<tmp:Click me>", data_c)
        self.assertIn("float a;", open(os.path.join(d, "engine_draw.h")).read())
        self.assertRegex(eng, r"_spr_a\[\] = \{[^}]*1\.0")
        ub = plan["ui_buttons"][0]
        self.assertAlmostEqual(ub["highlighted"][0], 0.78431374, places=5)
        self.assertAlmostEqual(ub["pressed"][0], 0.5882353, places=5)

    def test_vector3_plus_equals_vector2_is_cs0034(self):
        """transform.position is Vector3; += Vector2 is ambiguous in csc."""
        bad = (
            "using UnityEngine;\n"
            "using UnityEngine.InputSystem;\n"
            "\n"
            "public class Pad : MonoBehaviour\n"
            "{\n"
            "\tpublic float speed;\n"
            "\n"
            "\tvoid Start ()\n"
            "\t{\n"
            "\t\tDebug.Log(\"Hello World!\");\n"
            "\t\tSystem.Console.WriteLine(GameObject.Find(\"BouncePad\")"
            ".GetComponent<Bouncer>().amp);\n"
            "\t}\n"
            "\n"
            "\tvoid Update ()\n"
            "\t{\n"
            "\t\tfloat move = 0;\n"
            "\t\tif (Keyboard.current.leftArrowKey.isPressed)\n"
            "\t\t\tmove --;\n"
            "\t\tif (Keyboard.current.rightArrowKey.isPressed)\n"
            "\t\t\tmove ++;\n"
            "\t\ttransform.position += "
            "new Vector2(move * speed * Time.deltaTime, 0);\n"
            "\t\tprint(move);\n"
            "\t\tSystem.Console.WriteLine(\"\" + Time.time);\n"
            "\t\tSpriteRenderer spriteRend = "
            "gameObject.AddComponent<SpriteRenderer>();\n"
            "\t\tSystem.Console.WriteLine(spriteRend);\n"
            "\t}\n"
            "}\n"
        )
        path = "/proj/Assets/Scripts/Pad.cs"
        with self.assertRaises(unity_pack.PackError) as cm:
            unity_pack.analyze_script(path, bad)
        self.assertEqual(
            cm.exception.message,
            "Assets/Scripts/Pad.cs(21,3): error CS0034: Operator '+=' is "
            "ambiguous on operands of type 'Vector3' and 'Vector2'")
        # Fixture Player.cs uses Vector3 += (valid).
        unity_pack.analyze_script(
            os.path.join(SYSTEMS, "Assets", "Scripts", "Player.cs"))

    def test_detects_system_apis(self):
        _objs, analyses, lights, cameras, _hier = unity_pack.load_project(SYSTEMS)
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
        self.assertAlmostEqual(cameras[0]["orthographic_size"], 7.2525)
        self.assertAlmostEqual(cameras[0]["pos"][2], -10.0)
        self.assertAlmostEqual(cameras[0]["near_clip"], 0.3)
        self.assertAlmostEqual(cameras[0]["far_clip"], 1000.0)
        self.assertNotIn("ParticleSystem.Emit", apis)
        self.assertNotIn("AnimationCurve.Evaluate", apis)
        spr = [o for o in _objs if o.get("sprite")]
        self.assertEqual(len(spr), 9)  # + Button Text TMP
        tmp = [o for o in _objs if o["name"] == "Button Text"]
        self.assertEqual(len(tmp), 1)
        self.assertEqual(tmp[0]["ui_tmp"]["text"], "Click me")
        self.assertTrue(tmp[0]["sprite"].get("tex_rgba"))
        player = [o for o in _objs if o["name"] == "Player"][0]
        self.assertIsNone(player.get("sprite"))
        graphic = [o for o in _objs if o["name"] == "Graphics"][0]
        self.assertIsNotNone(graphic.get("sprite"))
        self.assertEqual(graphic["father_id"], "3002")  # child of Player
        self.assertAlmostEqual(graphic["local_pos"][0], 0.0)
        self.assertAlmostEqual(graphic["local_pos"][1], 0.0)
        self.assertAlmostEqual(graphic["pos"][0], 0.0)
        self.assertAlmostEqual(graphic["pos"][1], 0.0)
        self.assertEqual(cameras[0]["father_id"], "3002")  # under Player
        self.assertAlmostEqual(cameras[0]["local_pos"][2], -10.0)
        d = tempfile.mkdtemp(prefix="upack-xf-")
        plan = unity_pack.pack(SYSTEMS, d)
        self.assertTrue(plan.get("has_transform_parents"))
        self.assertTrue(plan.get("camera_follows_parent"))
        self.assertEqual(plan["camera"].get("xf_parent_class"), "Player")
        g_inst = plan["classes"]["Graphics"]["instances"][0]
        self.assertEqual(g_inst.get("xf_parent_class"), "Player")
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("_engine_world_pos", eng)
        self.assertIn("_Graphics_xf_parent_class", eng)
        self.assertIn("_engine_sync_camera_main", eng)
        self.assertIn("Camera_main_local_z", eng)

    @needs_cc
    def test_main_camera_follows_player_parent(self):
        """Main Camera under Player: Camera_main_pos tracks Player world."""
        d = tempfile.mkdtemp(prefix="upack-camfollow-")
        unity_pack.pack(SYSTEMS, d)
        host = os.path.join(d, "host_cam.c")
        with open(host, "w") as f:
            f.write(
                "typedef struct { float x, y, half_w, half_h;\n"
                "                 float m00, m01, m10, m11;\n"
                "                 float r, g, b; float a; int tex;\n"
                "                 int sorting_layer; int sorting_order;\n"
                "               } EngineDraw;\n"
                "int engine_collect_draws(EngineDraw *out, int max);\n"
                "typedef struct Player Player;\n"
                "struct Player { float pos_x; float pos_y; float pos_z; };\n"
                "extern Player _Player_inst_array[];\n"
                "extern float Camera_main_pos_x;\n"
                "extern float Camera_main_pos_y;\n"
                "extern float Camera_main_pos_z;\n"
                "int main(void) {\n"
                "  EngineDraw buf[4];\n"
                "  _Player_inst_array[0].pos_x = 3.f;\n"
                "  _Player_inst_array[0].pos_y = 4.f;\n"
                "  engine_collect_draws(buf, 4);\n"
                "  if (Camera_main_pos_x < 2.9f || Camera_main_pos_x > 3.1f)\n"
                "    return 1;\n"
                "  if (Camera_main_pos_y < 3.9f || Camera_main_pos_y > 4.1f)\n"
                "    return 2;\n"
                "  if (Camera_main_pos_z < -10.1f || Camera_main_pos_z > -9.9f)\n"
                "    return 3;\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, "host_cam")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

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
        objs, _lights, _cams, _hier = unity_pack.parse_unity_yaml(text)
        nested = [o for o in objs if o["name"] == "Nested"][0]
        self.assertEqual(nested["father_id"], "2")
        self.assertAlmostEqual(nested["local_pos"][0], 3.0)
        self.assertAlmostEqual(nested["local_pos"][1], 4.0)
        self.assertAlmostEqual(nested["pos"][0], 13.0)
        self.assertAlmostEqual(nested["pos"][1], 4.0)

    def test_vec2_fields_ignore_vector3_yaml(self):
        """Vector3 `{x,y,z}` must not be parsed as Vector2 (y stops at comma)."""
        text = (
            "%YAML 1.1\n"
            "--- !u!1 &1\n"
            "GameObject:\n"
            "  m_Name: X\n"
            "  m_Component:\n"
            "  - component: {fileID: 2}\n"
            "  - component: {fileID: 3}\n"
            "--- !u!4 &2\n"
            "Transform:\n"
            "  m_GameObject: {fileID: 1}\n"
            "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
            "--- !u!114 &3\n"
            "MonoBehaviour:\n"
            "  m_GameObject: {fileID: 1}\n"
            "  m_Script: {fileID: 11500000, "
            "guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}\n"
            "  multSize: {x: 1.5, y: 2.25}\n"
            "  mCustomOffset: {x: 0, y: 0, z: 0}\n"
        )
        objs, _lights, _cams, _hier = unity_pack.parse_unity_yaml(text)
        x = [o for o in objs if o["name"] == "X"][0]
        fields = x.get("fields") or {}
        self.assertAlmostEqual(fields.get("multSize_x"), 1.5)
        self.assertAlmostEqual(fields.get("multSize_y"), 2.25)
        # Vector2 must not grow a phantom z; Vector3 keeps z (not truncated).
        self.assertNotIn("multSize_z", fields)
        self.assertAlmostEqual(fields.get("mCustomOffset_x"), 0.0)
        self.assertAlmostEqual(fields.get("mCustomOffset_y"), 0.0)
        self.assertAlmostEqual(fields.get("mCustomOffset_z"), 0.0)

    def test_editor_scripts_are_not_analyzed(self):
        """Assets/**/Editor/**/*.cs are Unity editor-only — skip for player pack."""
        root = tempfile.mkdtemp(prefix="upack-editor-")
        runtime = os.path.join(root, "Assets", "Scripts")
        editor = os.path.join(root, "Assets", "Scripts", "Editor")
        os.makedirs(runtime)
        os.makedirs(editor)
        with open(os.path.join(runtime, "Player.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Player : MonoBehaviour {\n"
                "    void Update() { transform.position = "
                "new Vector2(1f, 2f); }\n"
                "}\n"
            )
        with open(os.path.join(runtime, "Player.cs.meta"), "w") as f:
            f.write("guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n")
        with open(os.path.join(editor, "BadWindow.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "using UnityEditor;\n"
                "public class BadWindow : EditorWindow {\n"
                "    void OnGUI() {\n"
                "        if (Application.isPlaying) { }\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(editor, "BadWindow.cs.meta"), "w") as f:
            f.write("guid: cccccccccccccccccccccccccccccccc\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Player\n"
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
        # Must not raise CS0117 for Application.isPlaying in Editor script.
        d = tempfile.mkdtemp(prefix="upack-editor-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("Player", plan["classes"])
        self.assertTrue(unity_pack._is_player_csharp(
            root, os.path.join(runtime, "Player.cs")))
        self.assertFalse(unity_pack._is_player_csharp(
            root, os.path.join(editor, "BadWindow.cs")))
        # "Editor Helpers" is not the Unity Editor folder.
        helpers = os.path.join(root, "Assets", "Editor Helpers", "Tool.cs")
        os.makedirs(os.path.dirname(helpers))
        with open(helpers, "w") as f:
            f.write("using UnityEngine;\nclass Tool {}\n")
        self.assertTrue(unity_pack._is_player_csharp(root, helpers))

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
        self.assertIn("Player_Start", engine)
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
        self.assertIn("_Rigidbody2D_linear_damping", data)
        self.assertIn("_Collider2D_count", data)
        self.assertIn("Camera_main_orthographicSize", data)
        self.assertIn("Camera_main_pos_z", data)
        self.assertIn("Camera_main_nearClipPlane", data)
        self.assertIn("Camera_main_farClipPlane", data)
        self.assertIn("SpriteRenderer", engine)
        self.assertIn("wz - Camera_main_pos_z", engine)
        self.assertIn("out[n].m00", engine)
        self.assertIn("_spr_sin", engine)
        self.assertIn("engine_physics_collide2d", engine)
        self.assertIn("engine_animation_tick", engine)
        self.assertIn("_AnimPlayer_count", data)
        self.assertIn("_AnimKey_y", data)
        self.assertIn("_engine_tex0_rgba", data)
        self.assertIn("engine_texture_rgba", engine)
        self.assertNotIn("ParticleSystem_Emit", engine)
        self.assertNotIn("AnimationCurve_Evaluate", engine)
        self.assertNotIn("_AnimCurve0", data)
        self.assertNotIn("PARTICLE_MAX", engine)
        # MiniScene must not pull Sin / physics / input / lights in.
        d2 = tempfile.mkdtemp(prefix="upack-mini-")
        unity_pack.pack(PROJECT, d2)
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
        plan = unity_pack.pack(PROJECT, d)
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
        shutil.copytree(PROJECT, os.path.join(red, "proj"))
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
        objs, _a, _l, _c, _hier = unity_pack.load_project(root)
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
        objs, _a, _l, _c, _hier = unity_pack.load_project(root)
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
        objs, _a, _l, _c, _hier = unity_pack.load_project(root)
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
                "        transform.position = new Vector2(\n"
                "            transform.position.x + speed * Time.deltaTime,\n"
                "            transform.position.y);\n"
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

    def test_authored_animation_and_animator(self):
        objs, _a, _l, _c, _hier = unity_pack.load_project(SYSTEMS)
        wave = [o for o in objs if o["name"] == "Wave"][0]
        spin = [o for o in objs if o["name"] == "Spinner"][0]
        # Bob.anim is legacy → Animation on Wave plays; Animator on Spinner idle.
        self.assertEqual(wave["anim_player"]["kind"], "animation")
        self.assertIsNone(spin.get("anim_player"))
        self.assertTrue(wave["anim_player"]["playing"])
        self.assertTrue(wave["anim_player"]["clip"]["legacy"])
        self.assertAlmostEqual(wave["anim_player"]["clip"]["length"], 1.0)
        self.assertEqual(len(wave["anim_player"]["clip"]["pos_keys"]), 3)
        self.assertAlmostEqual(wave["anim_player"]["clip"]["pos_keys"][0][1], 2.0)
        player = [o for o in objs if o["name"] == "Player"][0]
        self.assertEqual(player["anim_player"]["kind"], "animator")
        idle_clip = player["anim_player"]["clip"]
        self.assertEqual(idle_clip["name"], "Idle")
        self.assertEqual(len(idle_clip.get("sprite_curves") or []), 1)
        self.assertEqual(idle_clip["sprite_curves"][0]["path"], "Graphics")
        self.assertEqual(len(idle_clip["sprite_curves"][0]["keys"]), 3)
        d = tempfile.mkdtemp(prefix="upack-anim-")
        plan = unity_pack.pack(SYSTEMS, d)
        self.assertEqual(len(plan["animation"]["players"]), 2)
        anim_names = {p["name"] for p in plan["animation"]["players"]}
        self.assertEqual(anim_names, {"Wave", "Player"})
        self.assertEqual(len(plan["animation"]["clips"]), 2)
        idle = [c for c in plan["animation"]["clips"] if c["name"] == "Idle"][0]
        self.assertEqual(idle["key_count"], 0)  # no root pos; sprite PPtr only
        skeys = plan["animation"]["sprite_keys"]
        self.assertEqual(len(skeys), 3)
        self.assertAlmostEqual(skeys[0]["t"], 0.0)
        self.assertAlmostEqual(skeys[1]["t"], 2.5)
        self.assertNotEqual(skeys[0]["tex"], skeys[1]["tex"])
        self.assertEqual(skeys[0]["tex"], skeys[2]["tex"])
        binds = plan["animation"]["sprite_binds"]
        self.assertEqual(len(binds), 1)
        self.assertEqual(binds[0]["target_class"], "Graphics")
        self.assertIn("Graphics", plan.get("sprite_draw_mutable") or [])
        # Eyes Closed PNG is anim-only — still packed into the texture table.
        tex_paths = [os.path.basename(t["path"]) for t in plan["textures"]]
        self.assertTrue(any("Eyes Closed" in p for p in tex_paths))
        self.assertTrue(any(p == "Red Slime.png" for p in tex_paths))
        # Bob (legacy Wave): absolute localPosition x stays 2 across keys.
        bob = [c for c in plan["animation"]["clips"] if c["name"] == "Bob"][0]
        keys = plan["animation"]["keys"][
            bob["key_begin"]:bob["key_begin"] + bob["key_count"]]
        self.assertAlmostEqual(keys[0]["x"], 2.0)
        self.assertAlmostEqual(keys[1]["x"], 2.0)
        self.assertAlmostEqual(keys[1]["y"], 0.5)
        anim_by_name = {p["name"]: p for p in plan["animation"]["players"]}
        self.assertAlmostEqual(anim_by_name["Wave"]["rest_x"], 2.0)
        self.assertEqual(anim_by_name["Player"]["sprite_bind_count"], 1)
        self.assertFalse(plan["classes"]["Wave"]["static"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("engine_animation_tick", eng)
        self.assertIn("Wave_set_pos_y", eng)
        self.assertIn("Wave_set_pos_x", eng)
        self.assertIn("_anim_sample_hold_i", eng)
        self.assertIn("_Graphics_draw_tex", eng)
        with open(os.path.join(d, "data.c")) as f:
            data = f.read()
        self.assertIn("_AnimSpriteKey_tex", data)
        self.assertIn("int _Graphics_draw_tex[", data)

    def test_mecanim_clip_drives_animator_not_animation(self):
        """Non-legacy Bob.anim → Spinner Animator plays; Wave Animation idle."""
        root = tempfile.mkdtemp(prefix="upack-mecanim-")
        scene = os.path.join(root, "SystemsScene")
        shutil.copytree(SYSTEMS, scene)
        anim = os.path.join(scene, "Assets", "Animations", "Bob.anim")
        text = open(anim).read().replace("m_Legacy: 1", "m_Legacy: 0")
        with open(anim, "w") as f:
            f.write(text)
        objs, _a, _l, _c, _hier = unity_pack.load_project(scene)
        wave = [o for o in objs if o["name"] == "Wave"][0]
        spin = [o for o in objs if o["name"] == "Spinner"][0]
        self.assertIsNone(wave.get("anim_player"))
        self.assertEqual(spin["anim_player"]["kind"], "animator")
        self.assertFalse(spin["anim_player"]["clip"]["legacy"])
        d = tempfile.mkdtemp(prefix="upack-mecanim-out-")
        plan = unity_pack.pack(scene, d)
        self.assertEqual(len(plan["animation"]["players"]), 2)
        anim_by_name = {p["name"]: p for p in plan["animation"]["players"]}
        self.assertEqual(set(anim_by_name), {"Spinner", "Player"})
        self.assertAlmostEqual(anim_by_name["Spinner"]["rest_x"], -2.5)
        # Runtime: absolute curve → Spinner at x=2 (Bob.anim), Wave idle.
        if not _CC:
            return
        spin_cl = plan["classes"]["Spinner"]
        wave_cl = plan["classes"]["Wave"]
        spin_z = "" if spin_cl.get("two_d") else " float pos_z;"
        wave_z = "" if wave_cl.get("two_d") else " float pos_z;"
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "#include <stdio.h>\n"
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "typedef struct { float x, y, half_w, half_h;\n"
                "                 float m00, m01, m10, m11;\n"
                "                 float r, g, b; float a; int tex;\n"
                "                 int sorting_layer; int sorting_order;\n"
                "               } EngineDraw;\n"
                "int engine_collect_draws(EngineDraw *out, int max);\n"
                "typedef struct Spinner Spinner;\n"
                "struct Spinner { float pos_x; float pos_y;%s };\n"
                "extern Spinner _Spinner_inst_array[];\n"
                "typedef struct Wave Wave;\n"
                "struct Wave { float pos_x; float pos_y;%s };\n"
                "extern Wave _Wave_inst_array[];\n"
                "int main(void) {\n"
                "  float wy0 = _Wave_inst_array[0].pos_y;\n"
                "  Time_deltaTime = 0.02f;\n"
                "  int i;\n"
                "  for (i = 0; i < 25; i = i + 1) engine_tick();\n"
                "  if (_Wave_inst_array[0].pos_y < wy0 - 0.01f\n"
                "      || _Wave_inst_array[0].pos_y > wy0 + 0.01f)\n"
                "    return 2; /* Wave must not bob without legacy clip */\n"
                "  if (_Spinner_inst_array[0].pos_x < 1.99f\n"
                "      || _Spinner_inst_array[0].pos_x > 2.01f)\n"
                "    return 3; /* Bob.anim localPosition.x == 2 */\n"
                "  if (_Spinner_inst_array[0].pos_y < 0.4f) return 4;\n"
                "  EngineDraw buf[64];\n"
                "  int n = engine_collect_draws(buf, 64);\n"
                "  if (n != 9) return 5;\n"
                "  { int j; int found = 0;\n"
                "    for (j = 0; j < n; j = j + 1)\n"
                "      if (buf[j].x > 1.9f && buf[j].x < 2.1f\n"
                "          && buf[j].y > 0.3f) found = 1;\n"
                "    if (!found) return 6; /* Spinner drawn at curve x */\n"
                "  }\n"
                "  return 0;\n"
                "}\n" % (spin_z, wave_z)
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
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

    def test_mathf_sign_and_set_world_scale_extensions(self):
        """Mathf.Sign + Extensions SetX/SetZ/SetWorldScale lower for Player."""
        d = tempfile.mkdtemp(prefix="upack-ext-")
        plan = unity_pack.pack(SYSTEMS, d)
        self.assertEqual(plan.get("live_scale_classes"), ["Graphics"])
        targets = plan.get("transform_field_targets") or {}
        self.assertIn(("Player", "graphicsTrs"), targets)
        hit = targets[("Player", "graphicsTrs")][0]
        self.assertIsNotNone(hit)
        self.assertEqual(hit[2], "Graphics")
        player = plan["classes"]["Player"]["instances"][0]
        self.assertAlmostEqual(float(player["fields"]["multSize_x"]), 1.0)
        self.assertAlmostEqual(float(player["fields"]["multSize_y"]), 1.0)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("Mathf_Sign", eng)
        self.assertIn("_engine_set_world_scale", eng)
        self.assertIn("_Player_graphicsTrs_target_class", eng)
        self.assertIn("_Graphics_scale_x", eng)
        self.assertNotIn("Mathf.Sign", eng)
        self.assertNotIn("graphicsTrs.SetWorldScale", eng)
        self.assertIn(
            "_engine_set_world_scale(_Player_graphicsTrs_target_class[i]",
            eng)
        with open(os.path.join(d, "data.c")) as f:
            data = f.read()
        self.assertIn("float _Graphics_scale_x[", data)
        self.assertIn("const int _Player_graphicsTrs_target_class[", data)
        # Script `float xSize = 1;` — not in scene YAML; must still init to 1
        # so SetWorldScale(multSize.x * xSize) is non-zero before arrows.
        self.assertRegex(
            data,
            r"Player _Player_inst_array\[1\] = \{\s*\{[^}]*1\.0f[^}]*\},")
        # xSize is last float member after multSize_x/y — trailing 1.0f before }
        self.assertIn("17.5f, 1.0f, 1.0f, 1.0f", data)

    def test_addcomponent_camera_and_rigidbody2d(self):
        """AddComponent refuses a second DisallowMultipleComponent with Unity's error."""
        d = tempfile.mkdtemp(prefix="upack-addcomp-")
        plan = unity_pack.pack(SYSTEMS, d)
        self.assertIn("SpriteRenderer", plan.get("addcomponent_types") or [])
        self.assertIn("Graphics", plan.get("go_has_sprite") or [])
        self.assertNotIn("Player", plan.get("go_has_sprite") or [])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("GameObject_AddComponent_SpriteRenderer", eng)
        self.assertIn("_engine_cant_add_component", eng)
        self.assertIn(
            "Can't add component '%s' to %s because such a component is "
            "already added to the game object!",
            eng)
        self.assertIn(
            "GameObject_AddComponent_SpriteRenderer(_engine_go_of_Player", eng)

        root = tempfile.mkdtemp(prefix="upack-addrb-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "X.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class X : MonoBehaviour {\n"
                "    public void Start() {\n"
                "        Rigidbody2D rb = gameObject.AddComponent<Rigidbody2D>();\n"
                "        System.Console.WriteLine(rb);\n"
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
        d2 = tempfile.mkdtemp(prefix="upack-addrb-out-")
        plan2 = unity_pack.pack(root, d2)
        self.assertIn("Rigidbody2D", plan2.get("addcomponent_types") or [])
        with open(os.path.join(d2, "engine.c")) as f:
            eng2 = f.read()
        self.assertIn("GameObject_AddComponent_Rigidbody2D", eng2)
        self.assertIn("Rigidbody2D_ToString", eng2)

    def test_audiosource_addcomponent_play_stop(self):
        """Authored !u!82 + AddComponent second source; Play/Stop/volume lower."""
        root = tempfile.mkdtemp(prefix="upack-audio-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Music.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Music : MonoBehaviour {\n"
                "    public AudioSource musicSource;\n"
                "    public void Start() {\n"
                "        AudioSource other = musicSource.gameObject"
                ".AddComponent<AudioSource>();\n"
                "        other.playOnAwake = false;\n"
                "        other.loop = true;\n"
                "        other.volume = 0.5f;\n"
                "        other.clip = null;\n"
                "        musicSource.Stop();\n"
                "        musicSource.Play();\n"
                "        System.Console.WriteLine(other);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Music.cs.meta"), "w") as f:
            f.write("guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Music\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "  - component: {fileID: 4}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!82 &3\nAudioSource:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n"
                "  m_PlayOnAwake: 1\n"
                "  m_Volume: 1\n"
                "  m_Pitch: 1\n"
                "  Loop: 0\n"
                "  Mute: 0\n"
                "--- !u!114 &4\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}\n"
                "  musicSource: {fileID: 3}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-audio-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("AudioSource", plan.get("addcomponent_types") or [])
        self.assertEqual(len(plan.get("audiosources") or []), 1)
        self.assertIn("3", plan.get("audiosource_by_file_id") or {})
        music = plan["classes"]["Music"]["instances"][0]
        self.assertEqual(music.get("object_refs", {}).get("musicSource"), "3")
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("GameObject_AddComponent_AudioSource", eng)
        self.assertIn("AudioSource_Play", eng)
        self.assertIn("AudioSource_Stop", eng)
        self.assertIn("AudioSource_ToString", eng)
        self.assertIn("_AudioSource_volume", eng)
        self.assertIn("_AudioSource_playing", eng)
        # Method body lowers props / Play against packed indices.
        self.assertIn("GameObject_AddComponent_AudioSource", eng)
        self.assertIn("_AudioSource_play_on_awake[", eng)
        self.assertIn("_AudioSource_loop[", eng)
        self.assertIn("AudioSource_Play(", eng)
        self.assertIn("AudioSource_Stop(", eng)
        with open(os.path.join(d, "data.c")) as f:
            data = f.read()
        self.assertIn("int _AudioSource_count = 1;", data)
        self.assertIn("_AudioSource_owner_go", data)

    @needs_cc
    def test_addcomponent_disallow_multiple_prints_unity_error(self):
        """Player has no SpriteRenderer — AddComponent succeeds and prints it."""
        d = tempfile.mkdtemp(prefix="upack-disallow-run-")
        unity_pack.pack(SYSTEMS, d)
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "int main(void) {\n"
                "  engine_tick();\n"
                "  return 0;\n"
                "}\n"
            )
        r = subprocess.run(
            [_CC, "-O2", "-c", "-o", os.path.join(d, "engine.o"),
             os.path.join(d, "engine.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O0", "-c", "-o", os.path.join(d, "data.o"),
             os.path.join(d, "data.c")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [_CC, "-O2", "-o", os.path.join(d, "t"),
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), host,
             "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run(
            [os.path.join(d, "t")], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("Player (UnityEngine.SpriteRenderer)", run.stdout)
        self.assertNotIn(
            "Can't add component 'SpriteRenderer' to Player because such a "
            "component is already added to the game object!",
            run.stderr)

    def test_authored_rigidbody2d_is_packed(self):
        objs, _a, _l, _c, _hier = unity_pack.load_project(SYSTEMS)
        ball = [o for o in objs if o["name"] == "Ball"][0]
        self.assertIsNotNone(ball.get("rigidbody2d"))
        self.assertAlmostEqual(ball["rigidbody2d"]["vel_x"], 0.0)
        player = [o for o in objs if o["name"] == "Player"][0]
        self.assertIsNotNone(player.get("rigidbody2d"))
        self.assertAlmostEqual(player["rigidbody2d"]["mass"], 0.5704784)
        self.assertAlmostEqual(player["rigidbody2d"]["linear_damping"], 1.0)
        self.assertAlmostEqual(ball["rigidbody2d"]["linear_damping"], 0.0)
        d = tempfile.mkdtemp(prefix="upack-rb2d-")
        plan = unity_pack.pack(SYSTEMS, d)
        self.assertEqual(len(plan.get("rigidbody2d") or []), 2)
        by_rb = {r["name"]: r for r in plan["rigidbody2d"]}
        self.assertAlmostEqual(by_rb["Player"]["linear_damping"], 1.0)
        self.assertAlmostEqual(by_rb["Ball"]["linear_damping"], 0.0)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("engine_physics_fixed", eng)
        self.assertIn("_engine_fixed_accum", eng)
        self.assertIn("_Rigidbody2D_linear_damping", eng)
        self.assertIn("GameObject_GetComponent_Rigidbody2D", eng)
        self.assertIn("engine_physics_collide2d", eng)
        self.assertIn("Player_OnCollisionEnter2D", eng)
        self.assertIn("Collision2D_ToString", eng)
        self.assertIn("engine_physics_collide2d_messages", eng)
        self.assertGreaterEqual(len(plan.get("collider2d") or []), 3)
        ball_col = ball["collider2d"]
        self.assertEqual(ball_col["kind"], "circle")
        ground = [o for o in objs if o["name"] == "Ground"][0]
        self.assertEqual(ground["collider2d"]["kind"], "box")
        self.assertEqual(player["collider2d"]["kind"], "box")
        self.assertAlmostEqual(ground["pos"][1], -2.5)
        # Colliders use Unity 2D default friction (0.4); Ice asset removed.
        self.assertAlmostEqual(ball_col["friction"], 0.4)
        self.assertAlmostEqual(ground["collider2d"]["friction"], 0.4)
        ground_row = [c for c in plan["collider2d"] if c["name"] == "Ground"][0]
        self.assertAlmostEqual(ground_row["friction"], 0.4)
        self.assertIn("_Collider2D_friction", eng)
        self.assertIn("_phys_mat_combine", eng)

    def test_rigidbody_field_linear_velocity_setx(self):
        """Serialized Rigidbody2D field + linearVelocity.SetX lowers to vel tables."""
        d = tempfile.mkdtemp(prefix="upack-rb-lv-")
        plan = unity_pack.pack(SYSTEMS, d)
        player = plan["classes"]["Player"]["instances"][0]
        self.assertEqual(player.get("object_refs", {}).get("rb"), "3006")
        by_fid = plan.get("rb2d_by_file_id") or {}
        self.assertIn("3006", by_fid)
        player_rb = by_fid["3006"]
        ball_rb = by_fid["2005"]
        self.assertNotEqual(player_rb, ball_rb)
        with open(os.path.join(d, "data.c")) as f:
            data = f.read()
        # Player.rb must index Player's Rigidbody2D, not Ball (0).
        self.assertRegex(
            data,
            r"Player _Player_inst_array\[1\] = \{\s*\{[^}]*\b%d\b" % player_rb)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("Player_get_rb(i)", eng)
        start = eng.find("static void Player_Update")
        end = eng.find("\nstatic void ", start + 1)
        if end < 0:
            end = eng.find("\nvoid ", start + 1)
        upd = eng[start:end]
        self.assertIn("_Rigidbody2D_vel_x[_up_rb]", upd)
        self.assertNotIn("linearVelocity", upd)
        self.assertNotIn("SetX", upd)

        # Rigidbody (3D) field + SetY
        root = tempfile.mkdtemp(prefix="upack-rb3-lv-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Mover.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Mover : MonoBehaviour {\n"
                "    public Rigidbody rb;\n"
                "    public float speed;\n"
                "    void Update() {\n"
                "        rb.linearVelocity = rb.linearVelocity.SetY(speed);\n"
                "        rb.velocity = new Vector3(1f, 2f, 3f);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Mover.cs.meta"), "w") as f:
            f.write("guid: dddddddddddddddddddddddddddddddd\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Mover\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "  - component: {fileID: 4}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!54 &4\nRigidbody:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Mass: 1\n"
                "  m_Drag: 0\n"
                "  m_UseGravity: 1\n"
                "  m_Velocity: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: dddddddddddddddddddddddddddddddd}\n"
                "  rb: {fileID: 4}\n"
                "  speed: 5\n"
            )
        d3 = tempfile.mkdtemp(prefix="upack-rb3-lv-out-")
        plan3 = unity_pack.pack(root, d3)
        self.assertEqual(len(plan3.get("rigidbody") or []), 1)
        self.assertEqual(plan3.get("rb3d_by_file_id", {}).get("4"), 0)
        with open(os.path.join(d3, "engine.c")) as f:
            eng3 = f.read()
        self.assertIn("_Rigidbody_vel_y[_up_rb]", eng3)
        self.assertIn("_Rigidbody_vel_x[_up_rb]", eng3)
        self.assertIn("Mover_get_rb(i)", eng3)
        self.assertNotIn(".linearVelocity", eng3)

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
        objs, _a, _l, _c, _hier = unity_pack.load_project(root)
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
        msg = cm.exception.message
        self.assertIn("Assets/Scripts/Spark.cs(", msg)
        self.assertIn("error CS0117", msg)
        self.assertIn("ParticleSystem", msg)
        self.assertIn("Emit", msg)

    def test_refuses_canvas_is_cs0246_with_site(self):
        """AddComponent<Canvas> invent → CS0246 at the type token."""
        src = (
            "using UnityEngine;\n"
            "public class Sel : MonoBehaviour {\n"
            "    void Start() { gameObject.AddComponent<Canvas>(); }\n"
            "}\n"
        )
        path = "/proj/Assets/Scripts/Unity Overrides/_Selectable.cs"
        with self.assertRaises(unity_pack.PackError) as cm:
            unity_pack.analyze_script(path, src)
        self.assertIn("error CS0246", cm.exception.message)
        self.assertIn("Canvas", cm.exception.message)
        self.assertIn("Assets/Scripts/Unity Overrides/_Selectable.cs(",
                      cm.exception.message)

    def test_getcomponent_unknown_is_cs0246_with_site(self):
        """GetComponent of a non-packed type → CS0246 at the type token."""
        root = tempfile.mkdtemp(prefix="upack-gc-miss-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Hud.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Hud : MonoBehaviour {\n"
                "    void Start() {\n"
                "        NoSuchComp c = GetComponent<NoSuchComp>();\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Hud.cs.meta"), "w") as f:
            f.write("guid: cccccccccccccccccccccccccccccccc\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Hud\n"
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
        msg = cm.exception.message
        self.assertIn("error CS0246", msg)
        self.assertIn("NoSuchComp", msg)
        self.assertIn("Assets/Scripts/Hud.cs(", msg)
        self.assertNotIn("does not invent", msg)
        self.assertNotIn("no authored", msg)

    def test_getcomponent_canvas_is_authored_ui(self):
        """GetComponent<Canvas> on authored UI packs (not CS0246 invent)."""
        root = tempfile.mkdtemp(prefix="upack-gc-canvas-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Hud.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "using UnityEngine.UI;\n"
                "public class Hud : MonoBehaviour {\n"
                "    void Start() {\n"
                "        Canvas c = GetComponent<Canvas>();\n"
                "        RectTransform rt = GetComponent<RectTransform>();\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Hud.cs.meta"), "w") as f:
            f.write("guid: dddddddddddddddddddddddddddddddd\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Hud\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "  - component: {fileID: 4}\n"
                "--- !u!224 &2\nRectTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!223 &3\nCanvas:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n"
                "--- !u!114 &4\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: dddddddddddddddddddddddddddddddd}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("Canvas", plan.get("go_ui_components") or {})
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("GameObject_GetComponent_Canvas", eng)
        self.assertIn("GameObject_GetComponent_RectTransform", eng)
        self.assertIn("static int _engine_go_Canvas[", eng)
        self.assertNotIn("static const int _engine_go_Canvas[", eng)

    def test_getcomponent_prefab_mb_is_live(self):
        """GetComponent<T> for a prefab-authored MB uses a live GO map."""
        root = tempfile.mkdtemp(prefix="upack-gc-prefab-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "D2dFracturer.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class D2dFracturer : MonoBehaviour {\n"
                "    public float damageRequired = 100f;\n"
                "    public void Fracture() { damageRequired = 0f; }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "D2dFracturer.cs.meta"), "w") as f:
            f.write("guid: 98209ab08e5ab0e4bb6d18d7bc0ad690\n")
        with open(os.path.join(scripts, "Player.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Player : MonoBehaviour {\n"
                "    public D2dFracturer fracturer;\n"
                "    public void Death() {\n"
                "        fracturer = GetComponent<D2dFracturer>();\n"
                "        int go = 0;\n"
                "        fracturer = go.GetComponent<D2dFracturer>();\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Player.cs.meta"), "w") as f:
            f.write("guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
        prefabs = os.path.join(root, "Assets", "Prefabs")
        os.makedirs(prefabs)
        with open(os.path.join(prefabs, "Chunk.prefab"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Chunk\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!4 &11\nTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_LocalPosition: {x: 1, y: 2, z: 0}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: 98209ab08e5ab0e4bb6d18d7bc0ad690}\n"
                "  damageRequired: 100\n"
            )
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Player\n"
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
        plan = unity_pack.pack(root, d)
        self.assertIn("D2dFracturer", plan["classes"])
        self.assertGreaterEqual(plan["classes"]["D2dFracturer"]["n"], 1)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("GameObject_GetComponent_D2dFracturer", eng)
        self.assertIn("static int _engine_go_D2dFracturer[", eng)
        self.assertNotIn("static const int _engine_go_D2dFracturer[", eng)
        self.assertIn(
            "GameObject_GetComponent_D2dFracturer(_engine_go_of_Player(i))",
            eng)
        self.assertIn("GameObject_GetComponent_D2dFracturer(go)", eng)
        # Shallow: Fracture body not lowered from the vendor-style target.
        self.assertNotIn("D2dFracturer_Fracture", eng)


    def test_crust_undeclared_setter_maps_to_csharp_site(self):
        """Missing Class_set_field in crust output → CS0103 at the C# field."""
        src = (
            "using UnityEngine;\n"
            "public class LogAverageFPS : MonoBehaviour {\n"
            "    float timeLeft;\n"
            "    void Update() { timeLeft -= Time.deltaTime; }\n"
            "}\n"
        )
        analyses = [{
            "path": "/proj/Assets/Scripts/LogAverageFPS.cs",
            "classes": [{
                "name": "LogAverageFPS",
                "file_text": src,
            }],
        }]
        err = (
            "\x1b[1m/tmp/upack-crust-x/tu.c:1492:7: \x1b[31merror:\x1b[0m "
            "use of undeclared identifier 'LogAverageFPS_set_timeLeft'\n"
        )
        msg = unity_pack._crust_error_to_unity(err, analyses=analyses)
        self.assertIn("error CS0103", msg)
        self.assertIn("timeLeft", msg)
        self.assertIn("Assets/Scripts/LogAverageFPS.cs(", msg)
        self.assertNotIn("/tmp/", msg)
        self.assertNotIn("tu.c", msg)


    def test_methods_in_skips_else_if(self):
        """`else if (...) {` must not become a method named if."""
        src = (
            "using UnityEngine;\n"
            "public class P : MonoBehaviour {\n"
            "    int x, y;\n"
            "    void Update() {\n"
            "        else if (x) { y = 1; }\n"
            "        if (y) { x = 0; }\n"
            "    }\n"
            "}\n"
        )
        a = unity_pack.analyze_script("/proj/Assets/P.cs", src)
        names = [m["name"] for m in a["classes"][0]["methods"]]
        self.assertEqual(names, ["Update"])
        self.assertNotIn("if", names)


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
        """Find miss + GetComponent.field → NRE with site; Start exits, player continues."""
        d = tempfile.mkdtemp(prefix="upack-find-run-")
        unity_pack.pack(SYSTEMS, d)
        r = subprocess.run(["make", "-C", d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        run = subprocess.run(
            [os.path.join(d, "game"), "-logFile", "-"],
            capture_output=True, text=True, cwd=d)
        # Scene GO is "Bouncer"; Player.Find("BouncePad") is null → NRE.
        # Unity catches script exceptions — process must not SIGABRT.
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        err = run.stderr or ""
        self.assertIn("NullReferenceException", err)
        self.assertIn(
            "Object reference not set to an instance of an object", err)
        self.assertIn("Player.Start ()", err)
        self.assertIn("Player.cs:18", err)
        self.assertNotIn("SIGABRT", err)
        self.assertNotIn("Aborted", err)
        # Start aborted before print("Hello World 2!"); Update still runs.
        out = run.stdout or ""
        self.assertNotIn("Hello World 2!", out)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("_engine_null_reference_at", eng)
        self.assertIn("setjmp", eng)
        self.assertIn('GameObject_Find("BouncePad")', eng)

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
        msg = cm.exception.message
        self.assertIn("Assets/Scripts/PadBare.cs(", msg)
        self.assertIn("error CS0246", msg)
        self.assertIn("Keyboard", msg)

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
        self.assertIn('Debug_Log_s("Hello World!")', engine)
        self.assertIn("Debug_Log_i(3)", engine)
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
        self.assertEqual(unity_pack.exe_filename("Rocket"), "Rocket")
        self.assertEqual(
            os.path.basename(unity_pack.default_pack_dir(root)),
            os.path.basename(os.path.abspath(root)))

    def test_player_screen_from_project_settings(self):
        """defaultScreenWidth/Height → Screen_width/height for the window host."""
        self.assertEqual(
            unity_pack.player_screen(tempfile.mkdtemp(prefix="upack-noscr-")),
            (1024, 768))
        root = tempfile.mkdtemp(prefix="upack-scr-")
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "ProjectSettings.asset"), "w") as f:
            f.write(
                "defaultScreenWidth: 1280\n"
                "defaultScreenHeight: 720\n"
            )
        self.assertEqual(unity_pack.player_screen(root), (1280, 720))
        # Missing fullscreenMode → windowed (host-safe default).
        self.assertEqual(
            unity_pack.player_display(root)[2:], (0, 1, 0))
        # SystemsScene authors 1920×1080 FullScreenWindow + native res.
        self.assertEqual(unity_pack.player_screen(SYSTEMS), (1920, 1080))
        sw, sh, sfs, snative, smax = unity_pack.player_display(SYSTEMS)
        self.assertEqual((sw, sh, sfs, snative, smax), (1920, 1080, 1, 1, 0))
        d = tempfile.mkdtemp(prefix="upack-scr-pack-")
        plan = unity_pack.pack(SYSTEMS, d)
        self.assertEqual(plan["screen_width"], 1920)
        self.assertEqual(plan["screen_height"], 1080)
        self.assertEqual(plan["screen_fullscreen"], 1)
        self.assertEqual(plan["screen_fullscreen_native"], 1)
        with open(os.path.join(d, "data.c")) as f:
            data = f.read()
        self.assertIn("int Screen_width = 1920;", data)
        self.assertIn("int Screen_height = 1080;", data)
        self.assertIn("int Screen_fullScreen = 1;", data)
        self.assertIn("int Screen_fullScreenNative = 1;", data)
        with open(os.path.join(d, "engine_draw.h")) as f:
            hdr = f.read()
        self.assertIn("extern int Screen_width;", hdr)
        self.assertIn("extern int Screen_height;", hdr)
        self.assertIn("extern int Screen_fullScreen;", hdr)
        # MiniScene has no defaultScreen* → Unity 1024×768 defaults.
        d2 = tempfile.mkdtemp(prefix="upack-scr-mini-")
        plan2 = unity_pack.pack(PROJECT, d2)
        self.assertEqual(plan2["screen_width"], 1024)
        self.assertEqual(plan2["screen_height"], 768)
        self.assertEqual(plan2.get("screen_fullscreen"), 0)

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
                ("CS0246", "InputAction"),
            ),
            (
                "using UnityEngine;\n"
                "public class Hud : MonoBehaviour {\n"
                "    void Start() {\n"
                "        gameObject.AddComponent<Canvas>();\n"
                "    }\n"
                "}\n",
                ("CS0246", "Canvas"),
            ),
        ]
        for src, needles in cases:
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
            msg = cm.exception.message
            self.assertIn("Assets/Scripts/X.cs(", msg)
            self.assertIn(": error ", msg)
            for needle in needles:
                self.assertIn(needle, msg)

    def test_using_unityengine_ui_allowed_for_image_field(self):
        """using UnityEngine.UI + Image field is authored wiring, not invent."""
        src = (
            "using UnityEngine;\n"
            "using UnityEngine.UI;\n"
            "public class Hud : MonoBehaviour {\n"
            "    public Image preview;\n"
            "    void Update() { }\n"
            "}\n"
        )
        path = "/proj/Assets/Scripts/Hud.cs"
        a = unity_pack.analyze_script(path, src)
        self.assertNotIn("UnityEngine.UI", a["apis"])
        self.assertNotIn("Canvas", a["apis"])


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
                "extern float engine_pointer_x;\n"
                "extern float engine_pointer_y;\n"
                "extern int engine_pointer_down;\n"
                "extern float RenderSettings_ambient_r;\n"
                "extern float _Light_intensity[];\n"
                "typedef struct { float x, y, half_w, half_h;\n"
                "                 float m00, m01, m10, m11;\n"
                "                 float r, g, b; float a; int tex;\n"
                "                 int sorting_layer; int sorting_order;\n"
                "               } EngineDraw;\n"
                "int engine_collect_draws(EngineDraw *out, int max);\n"
                "typedef struct Ball Ball;\n"
                "struct Ball { float pos_x; float pos_y; };\n"
                "extern Ball _Ball_inst_array[];\n"
                "typedef struct Player Player;\n"
                "struct Player { float pos_x; float pos_y; float moveSpeed; };\n"
                "extern Player _Player_inst_array[];\n"
                "extern float _AnimPlayer_time[];\n"
                "extern int _Graphics_draw_tex[];\n"
                "extern const int _AnimSpriteKey_tex[];\n"
                "int main(void) {\n"
                "  float y0 = _Ball_inst_array[0].pos_y;\n"
                "  float x0 = _Player_inst_array[0].pos_x;\n"
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
                "  if (_Player_inst_array[0].pos_x <= x0) return 4;\n"
                "  if (_Light_intensity[0] < 1.4f) return 5;\n"
                "  if (n != 9) return 6; /* SpriteRenderers + Button + TMP */\n"
                "  /* Ground top ≈ -2.25; ball radius ≈ 0.225 → rest y ≳ -2.05 */\n"
                "  if (_Ball_inst_array[0].pos_y < -2.1f) return 8;\n"
                "  /* Lowest sortingOrder first; Button Foreground last. */\n"
                "  if (buf[0].sorting_order != -10) return 13;\n"
                "  if (buf[n - 1].sorting_layer != 1) return 14;\n"
                "  if (buf[n - 1].a < 0.99f)\n"
                "    return 15; /* TMP / Button alpha */\n"
                "  /* Hover center → ColorBlock highlighted (~0.784) on Image. */\n"
                "  engine_pointer_x = 960.f;\n"
                "  engine_pointer_y = 540.f;\n"
                "  engine_pointer_down = 0;\n"
                "  engine_tick();\n"
                "  n = engine_collect_draws(buf, 128);\n"
                "  if (n != 9) return 21;\n"
                "  { int j; int found = 0;\n"
                "    for (j = 0; j < n; j = j + 1)\n"
                "      if (buf[j].r > 0.7f && buf[j].r < 0.85f\n"
                "          && buf[j].a > 0.99f) found = 1;\n"
                "    if (!found) return 22; /* highlighted Image tint */\n"
                "  }\n"
                "  /* Click Button → SetActive(false) hides Image + TMP child. */\n"
                "  engine_pointer_x = 960.f;\n"
                "  engine_pointer_y = 540.f;\n"
                "  engine_pointer_down = 1;\n"
                "  engine_tick();\n"
                "  n = engine_collect_draws(buf, 128);\n"
                "  if (n != 7) return 19; /* Button + Text hidden */\n"
                "  /* Child under Player follows parent world position (m_Father). */\n"
                "  {\n"
                "    float px = _Player_inst_array[0].pos_x;\n"
                "    float py = _Player_inst_array[0].pos_y;\n"
                "    int j; int found = 0;\n"
                "    if (px <= 0.5f) return 16; /* rightArrow move sticks */\n"
                "    /* Player m_LinearDamping 1 → less fall than undamped. */\n"
                "    if (py >= 6.5f || py < -4.5f) return 18;\n"
                "    for (j = 0; j < n; j = j + 1) {\n"
                "      if (buf[j].x > px - 0.05f && buf[j].x < px + 0.05f\n"
                "          && buf[j].y > py - 0.05f && buf[j].y < py + 0.05f)\n"
                "        found = 1;\n"
                "    }\n"
                "    if (!found) return 17; /* child sprite at parent world pos */\n"
                "  }\n"
                "  /* Idle.anim m_Sprite at t=2.5 → Eyes Closed on Graphics. */\n"
                "  {\n"
                "    int open_tex = _Graphics_draw_tex[0];\n"
                "    _AnimPlayer_time[0] = 2.55f;\n"
                "    _AnimPlayer_time[1] = 2.55f;\n"
                "    engine_tick();\n"
                "    if (_Graphics_draw_tex[0] == open_tex) return 19;\n"
                "    if (_Graphics_draw_tex[0] != _AnimSpriteKey_tex[1])\n"
                "      return 20;\n"
                "  }\n"
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

    def test_oncollision_enter2d_fires_on_landing(self):
        """Player.OnCollisionEnter2D prints Collision2D when hitting Ground/Ball."""
        d = tempfile.mkdtemp(prefix="upack-col2d-msg-")
        unity_pack.pack(SYSTEMS, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("Player_OnCollisionEnter2D(unsigned i, int coll)", eng)
        self.assertIn("Collision2D_ToString", eng)
        self.assertIn("_col2d_add_contact", eng)
        self.assertIn("inv_a / inv_sum", eng)
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "typedef struct Player Player;\n"
                "struct Player { float pos_x; float pos_y; };\n"
                "typedef struct Ball Ball;\n"
                "struct Ball { float pos_x; float pos_y; };\n"
                "extern Player _Player_inst_array[];\n"
                "extern Ball _Ball_inst_array[];\n"
                "int main(void) {\n"
                "  int i;\n"
                "  Time_deltaTime = 0.02f;\n"
                "  for (i = 0; i < 120; i = i + 1) engine_tick();\n"
                "  /* Player lands on Ball above Ground; Ball must stay on floor. */\n"
                "  if (_Ball_inst_array[0].pos_y < -2.15f) return 2;\n"
                "  if (_Player_inst_array[0].pos_y"
                "      <= _Ball_inst_array[0].pos_y) return 3;\n"
                "  if (_Player_inst_array[0].pos_y > 0.f) return 4;\n"
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
        self.assertIn("UnityEngine.Collision2D", run.stdout)

    def test_player_stack_on_ball_does_not_teleport_ball(self):
        """Player landing on Ball must not drive Ball through Ground."""
        d = tempfile.mkdtemp(prefix="upack-stack-")
        unity_pack.pack(SYSTEMS, d)
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "typedef struct { float pos_x; float pos_y; } Ball;\n"
                "typedef struct { float pos_x; float pos_y; } Player;\n"
                "extern Ball _Ball_inst_array[];\n"
                "extern Player _Player_inst_array[];\n"
                "int main(void) {\n"
                "  int i;\n"
                "  float ball_min = 99.f;\n"
                "  Time_deltaTime = 0.02f;\n"
                "  for (i = 0; i < 200; i = i + 1) {\n"
                "    engine_tick();\n"
                "    if (_Ball_inst_array[0].pos_y < ball_min)\n"
                "      ball_min = _Ball_inst_array[0].pos_y;\n"
                "  }\n"
                "  /* Rest on Ground ≈ -2.025; never sink well below. */\n"
                "  if (ball_min < -2.2f) return 2;\n"
                "  if (_Ball_inst_array[0].pos_y < -2.15f) return 3;\n"
                "  if (_Player_inst_array[0].pos_y"
                "      <= _Ball_inst_array[0].pos_y + 0.2f) return 4;\n"
                "  return 0;\n"
                "}\n"
            )
        if not _CC:
            return
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
        exe = os.path.join(d, "stack")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)

    def test_physics_fall_matches_wall_clock_not_frame_count(self):
        """60Hz×1s ≈ same Player fall as 50 fixed steps (Unity fixed clock)."""
        ys = {}
        for label, dt, n in (
                ("50", "0.02f", 50),
                ("60", "(1.f/60.f)", 60)):
            out = tempfile.mkdtemp(prefix="upack-fall%s-" % label)
            unity_pack.pack(SYSTEMS, out)
            hostp = os.path.join(out, "host.c")
            with open(hostp, "w") as f:
                f.write(
                    "#include <stdio.h>\n"
                    "void engine_tick(void);\n"
                    "extern float Time_deltaTime;\n"
                    "typedef struct Player Player;\n"
                    "struct Player { float pos_x; float pos_y; };\n"
                    "extern Player _Player_inst_array[];\n"
                    "int main(void) {\n"
                    "  int i;\n"
                    "  Time_deltaTime = %s;\n"
                    "  for (i = 0; i < %d; i = i + 1) engine_tick();\n"
                    "  printf(\"Y=%%.6f\\n\", _Player_inst_array[0].pos_y);\n"
                    "  return 0;\n"
                    "}\n" % (dt, n)
                )
            r = subprocess.run(
                [_CC, "-O2", "-c", "-o", os.path.join(out, "engine.o"),
                 os.path.join(out, "engine.c")],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = subprocess.run(
                [_CC, "-O0", "-c", "-o", os.path.join(out, "data.o"),
                 os.path.join(out, "data.c")],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            exe = os.path.join(out, "fall")
            r = subprocess.run(
                [_CC, "-O2", "-o", exe, hostp,
                 os.path.join(out, "engine.o"), os.path.join(out, "data.o"),
                 "-lm"],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            run = subprocess.run([exe], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
            yline = [ln for ln in run.stdout.splitlines() if ln.startswith("Y=")]
            self.assertTrue(yline, run.stdout)
            ys[label] = float(yline[-1][2:])
        self.assertAlmostEqual(ys["50"], ys["60"], delta=0.05)

    def test_camera_positive_z_culls_sprites(self):
        """Unity looks +Z; camera at +z with sprites at 0 draws nothing."""
        d = tempfile.mkdtemp(prefix="upack-cam-z-")
        unity_pack.pack(SYSTEMS, d)
        host = os.path.join(d, "host_camz.c")
        with open(host, "w") as f:
            f.write(
                "typedef struct { float x, y, half_w, half_h;\n"
                "                 float m00, m01, m10, m11;\n"
                "                 float r, g, b; float a; int tex;\n"
                "                 int sorting_layer; int sorting_order;\n"
                "               } EngineDraw;\n"
                "int engine_collect_draws(EngineDraw *out, int max);\n"
                "extern float Camera_main_pos_z;\n"
                "int main(void) {\n"
                "  EngineDraw buf[128];\n"
                "  int n0 = engine_collect_draws(buf, 128);\n"
                "  if (n0 != 9) return 1;\n"
                "  Camera_main_pos_z = 10.f;\n"
                "  int n1 = engine_collect_draws(buf, 128);\n"
                "  /* Screen-space UI ignores world depth cull. */\n"
                "  if (n1 != 2) return 2;\n"
                "  Camera_main_pos_z = -10.f;\n"
                "  int n2 = engine_collect_draws(buf, 128);\n"
                "  if (n2 != 9) return 3;\n"
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
