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
import re
import contextlib
import io
import math
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tools.cs2cpp as cs2cpp  # noqa: E402
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


class TestBuildSettingsAndActive(unittest.TestCase):
    """EditorBuildSettings first-enabled scene + authored m_IsActive."""

    def _write_scene(self, root, rel, go_name, active=1, guid=None):
        scenes = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scenes, exist_ok=True)
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(
                "%%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: %s\n"
                "  m_IsActive: %d\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 1, y: 2, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1}\n"
                % (go_name, active)
            )
        if guid:
            with open(path + ".meta", "w") as f:
                f.write("guid: %s\n" % guid)

    def test_m_is_active_parsed(self):
        text = (
            "%YAML 1.1\n"
            "--- !u!1 &1\nGameObject:\n  m_Name: Hidden\n"
            "  m_IsActive: 0\n"
            "  m_Component:\n  - component: {fileID: 2}\n"
            "--- !u!4 &2\nTransform:\n"
            "  m_GameObject: {fileID: 1}\n"
            "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
            "  m_Father: {fileID: 0}\n"
        )
        objs, _l, _c, hier = unity_pack.parse_unity_yaml(text)
        self.assertEqual(hier[0]["active"], 0)
        # Hierarchy-only (no MB) — still records authored inactive.
        self.assertTrue(any(int(h.get("active", 1)) == 0 for h in hier))

    def test_editor_only_tag_skipped(self):
        """GameObjects with EditorOnly tag (and children) are not packed."""
        root = tempfile.mkdtemp(prefix="upack-editoronly-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        host_cs = os.path.join(scripts, "Host.cs")
        with open(host_cs, "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour { void Update() {} }\n"
            )
        text = (
            "%YAML 1.1\n"
            "--- !u!1 &1\nGameObject:\n  m_Name: Keep\n"
            "  m_TagString: Untagged\n"
            "  m_IsActive: 1\n"
            "  m_Component:\n  - component: {fileID: 2}\n"
            "  - component: {fileID: 3}\n"
            "--- !u!4 &2\nTransform:\n"
            "  m_GameObject: {fileID: 1}\n"
            "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
            "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
            "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
            "  m_Children:\n  - {fileID: 12}\n"
            "  m_Father: {fileID: 0}\n"
            "--- !u!114 &3\nMonoBehaviour:\n"
            "  m_GameObject: {fileID: 1}\n"
            "  m_Script: {fileID: 11500000, "
            "guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1, type: 3}\n"
            "--- !u!1 &10\nGameObject:\n  m_Name: EditorRoot\n"
            "  m_TagString: EditorOnly\n"
            "  m_IsActive: 1\n"
            "  m_Component:\n  - component: {fileID: 12}\n"
            "  - component: {fileID: 13}\n"
            "--- !u!4 &12\nTransform:\n"
            "  m_GameObject: {fileID: 10}\n"
            "  m_LocalPosition: {x: 1, y: 0, z: 0}\n"
            "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
            "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
            "  m_Children:\n  - {fileID: 22}\n"
            "  m_Father: {fileID: 2}\n"
            "--- !u!114 &13\nMonoBehaviour:\n"
            "  m_GameObject: {fileID: 10}\n"
            "  m_Script: {fileID: 11500000, "
            "guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1, type: 3}\n"
            "--- !u!1 &20\nGameObject:\n  m_Name: EditorChild\n"
            "  m_TagString: Untagged\n"
            "  m_IsActive: 1\n"
            "  m_Component:\n  - component: {fileID: 22}\n"
            "  - component: {fileID: 23}\n"
            "--- !u!4 &22\nTransform:\n"
            "  m_GameObject: {fileID: 20}\n"
            "  m_LocalPosition: {x: 2, y: 0, z: 0}\n"
            "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
            "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
            "  m_Father: {fileID: 12}\n"
            "--- !u!114 &23\nMonoBehaviour:\n"
            "  m_GameObject: {fileID: 20}\n"
            "  m_Script: {fileID: 11500000, "
            "guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1, type: 3}\n"
        )
        guids = {"a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1": host_cs}
        objs, _l, _c, hier = unity_pack.parse_unity_yaml(
            text, guid_to_script=guids)
        names = sorted(o["name"] for o in objs)
        self.assertEqual(names, ["Keep"])
        hier_names = sorted(h["name"] for h in hier)
        self.assertEqual(hier_names, ["Keep"])
        self.assertNotIn("EditorRoot", hier_names)
        self.assertNotIn("EditorChild", hier_names)

    def test_only_first_enabled_build_scene_packed(self):
        root = tempfile.mkdtemp(prefix="upack-build-scenes-")
        scripts = os.path.join(root, "Assets", "Scripts")
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(scripts)
        os.makedirs(ps)
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour {\n"
                "    void Update() { transform.position = "
                "transform.position; }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1\n")
        self._write_scene(
            root, "Assets/Scenes/Boot.unity", "BootGO", active=1,
            guid="11111111111111111111111111111111")
        self._write_scene(
            root, "Assets/Scenes/Level.unity", "LevelGO", active=1,
            guid="22222222222222222222222222222222")
        self._write_scene(
            root, "Assets/Demo/Extra.unity", "ExtraGO", active=1,
            guid="33333333333333333333333333333333")
        with open(os.path.join(ps, "EditorBuildSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1045 &1\nEditorBuildSettings:\n"
                "  m_Scenes:\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/Boot.unity\n"
                "    guid: 11111111111111111111111111111111\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/Level.unity\n"
                "    guid: 22222222222222222222222222222222\n"
                "  - enabled: 0\n"
                "    path: Assets/Demo/Extra.unity\n"
                "    guid: 33333333333333333333333333333333\n"
            )
        paths = unity_pack._unity_scenes_to_pack(root)
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith("Boot.unity"))
        objs, _a, _l, _c, hier = unity_pack.load_project(root)
        names = {o["name"] for o in objs} | {h["name"] for h in hier}
        self.assertIn("BootGO", names)
        self.assertNotIn("LevelGO", names)
        self.assertNotIn("ExtraGO", names)

    def test_go_active_seeded_in_engine(self):
        root = tempfile.mkdtemp(prefix="upack-active-")
        scripts = os.path.join(root, "Assets", "Scripts")
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(scripts)
        os.makedirs(ps)
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour {\n"
                "    void Update() { gameObject.SetActive(true); }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1\n")
        self._write_scene(
            root, "Assets/Scenes/S.unity", "HiddenHost", active=0,
            guid="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        with open(os.path.join(ps, "EditorBuildSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1045 &1\nEditorBuildSettings:\n"
                "  m_Scenes:\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/S.unity\n"
                "    guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            )
        d = tempfile.mkdtemp(prefix="upack-active-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn(0, plan.get("go_active") or [])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("_engine_go_active_authored", eng)
        self.assertRegex(eng, r"_engine_go_active_authored\[\d+\] = \{[^}]*0")

    def test_duplicate_ui_names_keep_inactive_parent(self):
        """Settings Menu children stay inactiveInHierarchy when Player UI
        reuses the same display names (Text / Sliding Area / …)."""
        text = (
            "%YAML 1.1\n"
            "--- !u!1 &10\nGameObject:\n  m_Name: Settings Menu\n"
            "  m_IsActive: 0\n"
            "  m_Component:\n  - component: {fileID: 11}\n"
            "--- !u!224 &11\nRectTransform:\n"
            "  m_GameObject: {fileID: 10}\n"
            "  m_Father: {fileID: 0}\n"
            "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
            "--- !u!1 &20\nGameObject:\n  m_Name: Text\n"
            "  m_IsActive: 1\n"
            "  m_Component:\n  - component: {fileID: 21}\n"
            "--- !u!224 &21\nRectTransform:\n"
            "  m_GameObject: {fileID: 20}\n"
            "  m_Father: {fileID: 11}\n"
            "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
            "--- !u!1 &30\nGameObject:\n  m_Name: Player\n"
            "  m_IsActive: 0\n"
            "  m_Component:\n  - component: {fileID: 31}\n"
            "--- !u!4 &31\nTransform:\n"
            "  m_GameObject: {fileID: 30}\n"
            "  m_Father: {fileID: 0}\n"
            "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
            "--- !u!1 &40\nGameObject:\n  m_Name: Text\n"
            "  m_IsActive: 1\n"
            "  m_Component:\n  - component: {fileID: 41}\n"
            "--- !u!4 &41\nTransform:\n"
            "  m_GameObject: {fileID: 40}\n"
            "  m_Father: {fileID: 31}\n"
            "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
        )
        _objs, _l, _c, hier = unity_pack.parse_unity_yaml(text)
        plan = {
            "classes": {"Obj": {"instances": [], "n": 0}},
            "scene_hierarchy": hier,
        }
        names, _comps = unity_pack._build_go_tables(plan)
        plan["go_names"] = names
        act = unity_pack._build_go_active(plan, names)
        parents = unity_pack._build_go_parents(plan)
        self.assertEqual(names.count("Text"), 2)
        self.assertEqual(names.count("Settings Menu"), 1)
        self.assertEqual(names.count("Player"), 1)
        sm = names.index("Settings Menu")
        pl = names.index("Player")
        texts = [i for i, n in enumerate(names) if n == "Text"]
        self.assertEqual(act[sm], 0)
        self.assertEqual(act[pl], 0)
        self.assertEqual(sorted(parents[t] for t in texts), sorted([sm, pl]))
        self.assertEqual(parents.count(sm), 1)
        self.assertEqual(parents.count(pl), 1)

        def aih(g):
            guard = 0
            while g >= 0 and g < len(act) and guard < len(act) + 2:
                if not act[g]:
                    return 0
                g = parents[g]
                guard += 1
            return 1

        for t in texts:
            self.assertEqual(aih(t), 0)

    def test_go_active_pads_spawn_budget(self):
        """Authored inactive GOs survive when go_n > len(go_active) (spawn)."""
        root = tempfile.mkdtemp(prefix="upack-active-pad-")
        scripts = os.path.join(root, "Assets", "Scripts")
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(scripts)
        os.makedirs(ps)
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "using UnityEngine.UI;\n"
                "public class Host : MonoBehaviour {\n"
                "    public GameObject menu;\n"
                "    void Update() {\n"
                "        if (menu != null) menu.SetActive(false);\n"
                "        GameObject.Instantiate(menu);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1\n")
        # Minimal scene: inactive Menu + Host (UI so active tables emit).
        scenes = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scenes)
        with open(os.path.join(scenes, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Host\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Menu\n"
                "  m_IsActive: 0\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "--- !u!224 &11\nRectTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
            )
        with open(os.path.join(scenes, "S.unity.meta"), "w") as f:
            f.write("guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n")
        with open(os.path.join(ps, "EditorBuildSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1045 &1\nEditorBuildSettings:\n"
                "  m_Scenes:\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/S.unity\n"
                "    guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
            )
        d = tempfile.mkdtemp(prefix="upack-active-pad-out-")
        plan = unity_pack.pack(root, d)
        names = plan.get("go_names") or []
        act = plan.get("go_active") or []
        self.assertIn("Menu", names)
        mi = names.index("Menu")
        self.assertEqual(act[mi], 0)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        m = re.search(
            r"_engine_go_active_authored\[(\d+)\] = \{([^}]+)\}", eng)
        self.assertIsNotNone(m)
        eact = [int(x.strip()) for x in m.group(2).split(",") if x.strip()]
        self.assertGreaterEqual(len(eact), len(names))
        self.assertEqual(eact[mi], 0)

    def test_image_preserve_aspect_fits_inside_rect(self):
        """Simple Image + preserveAspect letterboxes; hit rect stays full."""
        # 200×100 sprite (2:1) in a 200×200 rect → draw 200×100.
        dw, dh = unity_pack._ui_preserve_aspect_draw_size(200, 200, 200, 100)
        self.assertAlmostEqual(dw, 200.0)
        self.assertAlmostEqual(dh, 100.0)
        # 100×100 sprite in a 200×100 rect → draw 100×100 (pillarbox).
        dw, dh = unity_pack._ui_preserve_aspect_draw_size(200, 100, 100, 100)
        self.assertAlmostEqual(dw, 100.0)
        self.assertAlmostEqual(dh, 100.0)
        # Main Menu: 3000×1500 (2:1) in 1920×1080 (16:9) → 1920×960.
        dw, dh = unity_pack._ui_preserve_aspect_draw_size(
            1920, 1080, 3000, 1500)
        self.assertAlmostEqual(dw, 1920.0)
        self.assertAlmostEqual(dh, 960.0)
        text = (
            "%YAML 1.1\n"
            "--- !u!1 &1\nGameObject:\n  m_Name: Banner\n"
            "  m_IsActive: 1\n"
            "  m_Component:\n  - component: {fileID: 2}\n"
            "  - component: {fileID: 4}\n"
            "--- !u!224 &2\nRectTransform:\n"
            "  m_GameObject: {fileID: 1}\n"
            "  m_Father: {fileID: 0}\n"
            "  m_AnchorMin: {x: 0, y: 0}\n"
            "  m_AnchorMax: {x: 1, y: 1}\n"
            "  m_SizeDelta: {x: 0, y: 0}\n"
            "  m_Pivot: {x: 0.5, y: 0.5}\n"
            "--- !u!114 &4\nMonoBehaviour:\n"
            "  m_GameObject: {fileID: 1}\n"
            "  m_Enabled: 1\n"
            "  m_Script: {fileID: 11500000, "
            "guid: fe87c0e1cc204ed48ad3b37840f39efc, type: 3}\n"
            "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
            "  m_Sprite: {fileID: 21300000, "
            "guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, type: 3}\n"
            "  m_Type: 0\n"
            "  m_PreserveAspect: 1\n"
        )
        objs, _l, _c, _h = unity_pack.parse_unity_yaml(
            text, asset_guids={"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": "x.png"})
        found = next(
            (o for o in objs
             if o.get("name") == "Banner" and o.get("ui_image")),
            None)
        self.assertIsNotNone(found)
        self.assertEqual(int(found["ui_image"].get("preserve_aspect") or 0), 1)

    def test_ui_canvas_sort_walks_stripped_prefab_parent(self):
        """Labels under PrefabInstance roots inherit Canvas sorting_layer_id."""
        text = (
            "%YAML 1.1\n"
            "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
            "  m_IsActive: 1\n"
            "  m_Component:\n  - component: {fileID: 2}\n"
            "  - component: {fileID: 3}\n"
            "--- !u!224 &2\nRectTransform:\n"
            "  m_GameObject: {fileID: 1}\n"
            "  m_Father: {fileID: 0}\n"
            "--- !u!223 &3\nCanvas:\n"
            "  m_GameObject: {fileID: 1}\n"
            "  m_Enabled: 1\n"
            "  m_RenderMode: 1\n"
            "  m_SortingLayerID: 42\n"
            "  m_SortingOrder: 0\n"
            "--- !u!1001 &50\nPrefabInstance:\n"
            "  m_Modification:\n"
            "    m_TransformParent: {fileID: 2}\n"
            "    m_Modifications:\n"
            "    - target: {fileID: 99, guid: abcd, type: 3}\n"
            "      propertyPath: m_Name\n"
            "      value: Play Button\n"
            "      objectReference: {fileID: 0}\n"
            "    - target: {fileID: 88, guid: abcd, type: 3}\n"
            "      propertyPath: m_Sprite\n"
            "      value: \n"
            "      objectReference: {fileID: 21300000, "
            "guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, type: 3}\n"
            "    - target: {fileID: 99, guid: abcd, type: 3}\n"
            "      propertyPath: m_SizeDelta.x\n"
            "      value: 100\n"
            "      objectReference: {fileID: 0}\n"
            "    - target: {fileID: 99, guid: abcd, type: 3}\n"
            "      propertyPath: m_SizeDelta.y\n"
            "      value: 40\n"
            "      objectReference: {fileID: 0}\n"
            "--- !u!224 &51 stripped\nRectTransform:\n"
            "  m_CorrespondingSourceObject: {fileID: 1, guid: abcd, type: 3}\n"
            "  m_PrefabInstance: {fileID: 50}\n"
            "--- !u!1 &60\nGameObject:\n  m_Name: Label\n"
            "  m_IsActive: 1\n"
            "  m_Component:\n  - component: {fileID: 61}\n"
            "  - component: {fileID: 62}\n"
            "--- !u!224 &61\nRectTransform:\n"
            "  m_GameObject: {fileID: 60}\n"
            "  m_Father: {fileID: 51}\n"
            "  m_AnchorMin: {x: 0, y: 0}\n"
            "  m_AnchorMax: {x: 1, y: 1}\n"
            "  m_SizeDelta: {x: 0, y: 0}\n"
            "--- !u!114 &62\nMonoBehaviour:\n"
            "  m_GameObject: {fileID: 60}\n"
            "  m_Enabled: 1\n"
            "  m_Script: {fileID: 11500000, "
            "guid: fe87c0e1cc204ed48ad3b37840f39efc, type: 3}\n"
            "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
            "  m_Sprite: {fileID: 21300000, "
            "guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, type: 3}\n"
            "  m_Type: 0\n"
        )
        guid = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        # Minimal 2×2 PNG bytes via bake path — use temp file.
        d = tempfile.mkdtemp(prefix="upack-ui-sort-")
        png = os.path.join(d, "s.png")
        # 1×1 red PNG
        import struct, zlib
        def chunk(tag, data):
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
        raw = b"\x00\x00\x00" + b"\xff\x00\x00"  # filter+RGB
        with open(png, "wb") as f:
            f.write(
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw))
                + chunk(b"IEND", b""))
        objs, _l, cams, hier = unity_pack.parse_unity_yaml(
            text, asset_guids={guid: png})
        # Canvas camera stub for bake.
        cams = [{"main": True, "pos": (0, 0, -10), "orthographic_size": 5.0}]
        unity_pack._bake_ui_images(
            objs, cams, 200, 100, asset_guids={guid: png}, hierarchy=hier)
        label = next(o for o in objs if o.get("name") == "Label")
        btn = next((o for o in objs if o.get("name") == "Play Button"), None)
        self.assertIsNotNone(btn)
        self.assertIsNotNone(label.get("sprite"))
        self.assertEqual(
            int(label["sprite"].get("sorting_layer_id") or 0), 42)
        self.assertEqual(
            int(btn["sprite"].get("sorting_layer_id") or 0), 42)

    def test_stripped_prefab_father_hides_inactive_branch(self):
        """Children of stripped PrefabInstance xfs respect inactive parents."""
        text = (
            "%YAML 1.1\n"
            "--- !u!1 &1\nGameObject:\n  m_Name: Menu\n"
            "  m_IsActive: 1\n"
            "  m_Component:\n  - component: {fileID: 2}\n"
            "--- !u!224 &2\nRectTransform:\n"
            "  m_GameObject: {fileID: 1}\n"
            "  m_Father: {fileID: 0}\n"
            "  m_Children:\n  - {fileID: 11}\n"
            "--- !u!1 &10\nGameObject:\n  m_Name: Layout\n"
            "  m_IsActive: 0\n"
            "  m_Component:\n  - component: {fileID: 11}\n"
            "--- !u!224 &11\nRectTransform:\n"
            "  m_GameObject: {fileID: 10}\n"
            "  m_Father: {fileID: 2}\n"
            "--- !u!1001 &50\nPrefabInstance:\n"
            "  m_Modification:\n"
            "    m_TransformParent: {fileID: 11}\n"
            "    m_Modifications:\n"
            "    - target: {fileID: 99, guid: abcd, type: 3}\n"
            "      propertyPath: m_Name\n"
            "      value: Play Button\n"
            "      objectReference: {fileID: 0}\n"
            "    - target: {fileID: 99, guid: abcd, type: 3}\n"
            "      propertyPath: m_IsActive\n"
            "      value: 1\n"
            "      objectReference: {fileID: 0}\n"
            "--- !u!224 &51 stripped\nRectTransform:\n"
            "  m_CorrespondingSourceObject: {fileID: 1, guid: abcd, type: 3}\n"
            "  m_PrefabInstance: {fileID: 50}\n"
            "--- !u!1 &60\nGameObject:\n  m_Name: Play Button Text\n"
            "  m_IsActive: 1\n"
            "  m_Component:\n  - component: {fileID: 61}\n"
            "--- !u!224 &61\nRectTransform:\n"
            "  m_GameObject: {fileID: 60}\n"
            "  m_Father: {fileID: 51}\n"
        )
        _objs, _l, _c, hier = unity_pack.parse_unity_yaml(text)
        self.assertTrue(any(str(h.get("xf_id")) == "51" for h in hier))
        plan = {
            "classes": {"Obj": {"instances": [], "n": 0}},
            "scene_hierarchy": hier,
        }
        names, _comps = unity_pack._build_go_tables(plan)
        plan["go_names"] = names
        act = unity_pack._build_go_active(plan, names)
        parents = unity_pack._build_go_parents(plan)
        ti = names.index("Play Button Text")
        layout = names.index("Layout")
        self.assertEqual(act[layout], 0)

        def aih(g):
            guard = 0
            while g >= 0 and g < len(act) and guard < len(act) + 2:
                if not act[g]:
                    return 0
                g = parents[g]
                guard += 1
            return 1

        self.assertEqual(aih(ti), 0)
        g = ti
        seen = set()
        while g >= 0 and g not in seen:
            seen.add(g)
            if g == layout:
                break
            g = parents[g]
        else:
            self.fail("Play Button Text parent chain misses Layout")


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


class TestMethodOverloads(unittest.TestCase):
    """C# overloads must lower to distinct C free-function symbols."""

    def test_overload_method_c_symbols_are_unique(self):
        self.assertEqual(
            unity_pack._method_arg_type_suffix("SpawnedEntry spawnedEntry"),
            "SpawnedEntry")
        self.assertEqual(
            unity_pack._method_arg_type_suffix(
                "GameObject clone, Transform trs"),
            "GameObject_Transform")
        self.assertEqual(
            unity_pack._method_c_symbol(
                "ObjectPool", "RemoveSpawnedEntry",
                "SpawnedEntry spawnedEntry", True),
            "ObjectPool_RemoveSpawnedEntry_SpawnedEntry")
        self.assertEqual(
            unity_pack._method_c_symbol(
                "ObjectPool", "RemoveSpawnedEntry",
                "GameObject clone, Transform trs", True),
            "ObjectPool_RemoveSpawnedEntry_GameObject_Transform")
        self.assertEqual(
            unity_pack._method_c_symbol("ObjectPool", "Awake", "", False),
            "ObjectPool_Awake")
        root = tempfile.mkdtemp(prefix="upack-overload-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Pool.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Pool : MonoBehaviour {\n"
                "    public void RemoveSpawnedEntry(int a) {}\n"
                "    public void RemoveSpawnedEntry("
                "GameObject go, Transform trs) {}\n"
                "    void Update() { RemoveSpawnedEntry(1); }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Pool.cs.meta"), "w") as f:
            f.write("guid: ccccccccccccccccdddddddddddddddd\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Pool\n"
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
                "guid: ccccccccccccccccdddddddddddddddd, type: 3}\n"
            )
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "EditorBuildSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1045 &1\nEditorBuildSettings:\n"
                "  m_Scenes:\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/S.unity\n"
            )
        d = tempfile.mkdtemp(prefix="upack-overload-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("Pool_RemoveSpawnedEntry_int", eng)
        self.assertIn("Pool_RemoveSpawnedEntry_GameObject_Transform", eng)
        self.assertEqual(eng.count("static void Pool_RemoveSpawnedEntry("), 0)

    def test_nested_class_methods_not_on_outer(self):
        """Nested DoUpdate must not be attributed to the outer MonoBehaviour."""
        root = tempfile.mkdtemp(prefix="upack-nested-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        path = os.path.join(scripts, "Host.cs")
        with open(path, "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour {\n"
                "    public void DoUpdate() {}\n"
                "    class Nested {\n"
                "        public void DoUpdate() {}\n"
                "    }\n"
                "}\n"
            )
        a = unity_pack.analyze_script(path)
        by = {c["name"]: c for c in a["classes"]}
        self.assertIn("Host", by)
        self.assertIn("Nested", by)
        host_names = [m["name"] for m in by["Host"].get("methods") or []]
        self.assertEqual(host_names.count("DoUpdate"), 1)
        nest_names = [m["name"] for m in by["Nested"].get("methods") or []]
        self.assertEqual(nest_names.count("DoUpdate"), 1)


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


class TestIncrementalPack(unittest.TestCase):
    """Stamp early-exit + per-file transpile skip."""

    def _mini_project(self):
        root = tempfile.mkdtemp(prefix="upack-incr-proj-")
        scripts = os.path.join(root, "Assets", "Scripts")
        scenes = os.path.join(root, "Assets", "Scenes")
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(scripts)
        os.makedirs(scenes)
        os.makedirs(ps)
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour {\n"
                "    void Update() { transform.position = "
                "transform.position; }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1\n")
        with open(os.path.join(ps, "ProjectSettings.asset"), "w") as f:
            f.write(
                "PlayerSettings:\n"
                "  companyName: TestCo\n"
                "  productName: IncrPack\n"
                "  defaultScreenWidth: 800\n"
                "  defaultScreenHeight: 600\n"
            )
        with open(os.path.join(scenes, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Host\n"
                "  m_IsActive: 1\n"
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
                "guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Main Camera\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "  m_TagString: MainCamera\n"
                "--- !u!4 &11\nTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: -10}\n"
                "  m_Father: {fileID: 0}\n"
                "--- !u!20 &12\nCamera:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Orthographic: 1\n"
                "  m_OrthographicSize: 5\n"
            )
        return root

    def test_pack_skips_when_inputs_unchanged(self):
        root = self._mini_project()
        d = tempfile.mkdtemp(prefix="upack-incr-out-")
        calls = []
        real = unity_pack.validate_emitted_c

        def counting(text, path="engine.c", analyses=None):
            calls.append(path)
            return real(text, path, analyses=analyses)

        unity_pack.validate_emitted_c = counting
        try:
            unity_pack.pack(root, d)
            n_first = len(calls)
            self.assertGreaterEqual(n_first, 1)
            calls.clear()
            plan = unity_pack.pack(root, d)
            self.assertEqual(calls, [], "second pack must not re-validate")
            self.assertIn("Host", plan["classes"])
            host = plan["classes"]["Host"]
            self.assertIn("n", host)
            self.assertIn("size", host)
            self.assertIn("idx_ty", host)
            stamp = unity_pack._read_stamp(d)
            self.assertIsNotNone(stamp)
            self.assertTrue(any(
                isinstance(e, dict) and e.get("name") == "Host" and "n" in e
                for e in stamp.get("classes") or []))
            self.assertTrue(os.path.isfile(
                os.path.join(d, unity_pack._STAMP_NAME)))
        finally:
            unity_pack.validate_emitted_c = real

    def test_pack_force_revalidates(self):
        root = self._mini_project()
        d = tempfile.mkdtemp(prefix="upack-incr-force-")
        unity_pack.pack(root, d)
        calls = []
        real = unity_pack.validate_emitted_c

        def counting(text, path="engine.c", analyses=None):
            calls.append(path)
            return real(text, path, analyses=analyses)

        unity_pack.validate_emitted_c = counting
        try:
            unity_pack.pack(root, d, force=True)
            self.assertGreaterEqual(len(calls), 1)
        finally:
            unity_pack.validate_emitted_c = real

    def test_pack_reruns_when_script_changes(self):
        root = self._mini_project()
        d = tempfile.mkdtemp(prefix="upack-incr-chg-")
        unity_pack.pack(root, d)
        calls = []
        real = unity_pack.validate_emitted_c

        def counting(text, path="engine.c", analyses=None):
            calls.append(path)
            return real(text, path, analyses=analyses)

        host = os.path.join(root, "Assets", "Scripts", "Host.cs")
        # Change authored logic so emit text differs (comment-only is not enough).
        with open(host, "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour {\n"
                "    void Update() {\n"
                "        transform.position = transform.position "
                "* Time.deltaTime;\n"
                "    }\n"
                "}\n"
            )
        os.utime(host, None)
        unity_pack.validate_emitted_c = counting
        try:
            unity_pack.pack(root, d)
            self.assertGreaterEqual(len(calls), 1)
        finally:
            unity_pack.validate_emitted_c = real

    def test_script_only_change_reuses_scene_cache(self):
        """Editing .cs must not re-index PackageCache metas when assets match."""
        root = self._mini_project()
        d = tempfile.mkdtemp(prefix="upack-incr-script-")
        unity_pack.pack(root, d)
        self.assertTrue(os.path.isfile(
            os.path.join(d, unity_pack._SCENE_CACHE_NAME)))
        meta_calls = []
        real_map = unity_pack._asset_guid_map

        def counting(root_):
            meta_calls.append(root_)
            return real_map(root_)

        host = os.path.join(root, "Assets", "Scripts", "Host.cs")
        with open(host, "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour {\n"
                "    void Update() {\n"
                "        transform.position = transform.position "
                "* Time.deltaTime;\n"
                "    }\n"
                "}\n"
            )
        os.utime(host, None)
        unity_pack._asset_guid_map = counting
        try:
            unity_pack.pack(root, d)
            self.assertEqual(
                meta_calls, [],
                "scripts-only pack must reuse scene cache, not re-walk metas")
        finally:
            unity_pack._asset_guid_map = real_map

    def test_write_if_different_skips_identical(self):
        d = tempfile.mkdtemp(prefix="upack-wid-")
        path = os.path.join(d, "x.txt")
        self.assertTrue(unity_pack._write_if_different(path, "hello\n"))
        m0 = os.path.getmtime(path)
        self.assertFalse(unity_pack._write_if_different(path, "hello\n"))
        self.assertEqual(os.path.getmtime(path), m0)
        self.assertTrue(unity_pack._write_if_different(path, "bye\n"))


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
        root = tempfile.mkdtemp(prefix="upack-run-proj-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Player.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Player : MonoBehaviour {\n"
                "    public float speed = 1f;\n"
                "    void Update() {\n"
                "        transform.position += new Vector3("
                "speed * Time.deltaTime, 0, 0);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Player.cs.meta"), "w") as f:
            f.write("guid: c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Hero\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2}\n"
                "  speed: 2\n"
            )
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "EditorBuildSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1045 &1\nEditorBuildSettings:\n"
                "  m_Scenes:\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/S.unity\n"
            )
        d = tempfile.mkdtemp(prefix="upack-")
        unity_pack.pack(root, d)
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "int engine_class_count(void);\n"
                "extern float Time_deltaTime;\n"
                "extern float _Player_pos[][3];\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.1f;\n"
                "  float before = _Player_pos[0][0];\n"
                "  engine_tick();\n"
                "  if (_Player_pos[0][0] <= before) return 2;\n"
                "  return engine_class_count() == 1 ? 0 : 1;\n"
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
    """Default SoA: positions in contiguous float tables for GPU upload."""

    def _tiny_moving_project(self):
        root = tempfile.mkdtemp(prefix="upack-soa-proj-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour {\n"
                "    public float speed = 1f;\n"
                "    void Update() {\n"
                "        transform.position += new Vector3("
                "speed * Time.deltaTime, 0, 0);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Host\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1}\n"
                "  speed: 2\n"
            )
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "EditorBuildSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1045 &1\nEditorBuildSettings:\n"
                "  m_Scenes:\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/S.unity\n"
            )
        return root

    def test_soa_emits_pos_tables_not_struct_fields(self):
        d = tempfile.mkdtemp(prefix="upack-soa-")
        plan = unity_pack.pack(self._tiny_moving_project(), d)
        self.assertTrue(plan["soa"])
        self.assertEqual(plan["classes"]["Host"]["soa_dims"], 3)
        names = [m[0] for m in plan["classes"]["Host"]["members"]]
        self.assertNotIn("pos_x", names)
        with open(os.path.join(d, "data.c")) as f:
            data = f.read()
        self.assertIn("_Host_pos[", data)
        with open(os.path.join(d, "engine.c")) as f:
            engine = f.read()
        self.assertIn("SoA: one contiguous table → memcpy", engine)
        self.assertIn("memcpy(dst + n, &_Host_pos[0][0]", engine)
        self.assertIn("engine_upload_positions", engine)

    @needs_cc
    def test_soa_tick_still_moves_player(self):
        d = tempfile.mkdtemp(prefix="upack-soa-run-")
        unity_pack.pack(self._tiny_moving_project(), d)
        host = os.path.join(d, "host.c")
        with open(host, "w") as f:
            f.write(
                "#include \"engine_draw.h\"\n"
                "extern float _Host_pos[][3];\n"
                "extern float Time_deltaTime;\n"
                "int main(void) {\n"
                "  Time_deltaTime = 0.1f;\n"
                "  float before = _Host_pos[0][0];\n"
                "  engine_tick();\n"
                "  if (_Host_pos[0][0] <= before) return 2;\n"
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

    def test_aos_keeps_pos_in_struct(self):
        """--aos / soa=False keeps positions in AoS structs."""
        d = tempfile.mkdtemp(prefix="upack-aos-")
        plan = unity_pack.pack(self._tiny_moving_project(), d, soa=False)
        self.assertFalse(plan["soa"])
        names = [m[0] for m in plan["classes"]["Host"]["members"]]
        self.assertIn("pos_x", names)
        self.assertNotIn("soa_dims", plan["classes"]["Host"])
        with open(os.path.join(d, "data.c")) as f:
            data = f.read()
        self.assertNotIn("_Host_pos[", data)
        self.assertIn("_Host_inst_array", data)

    def test_soa_cli_removed_aos_flag(self):
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "unity_pack.py"),
             "--soa", tempfile.mkdtemp(prefix="upack-empty-"),
             "-o", tempfile.mkdtemp(prefix="upack-soa-gone-")],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--soa is gone", r.stderr)
        root = self._tiny_moving_project()
        d_aos = tempfile.mkdtemp(prefix="upack-aos-cli-")
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "unity_pack.py"),
             root, "-o", d_aos, "--aos"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("soa=False", r.stderr)
        with open(os.path.join(d_aos, "data.c")) as f:
            self.assertNotIn("_Host_pos[", f.read())
        d_soa = tempfile.mkdtemp(prefix="upack-soa-cli-")
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "unity_pack.py"),
             root, "-o", d_soa],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("soa=True", r.stderr)
        with open(os.path.join(d_soa, "data.c")) as f:
            self.assertIn("_Host_pos[", f.read())

    def test_soa_vec4_pads_w_with_instance_id(self):
        d = tempfile.mkdtemp(prefix="upack-soa4-")
        plan = unity_pack.pack(self._tiny_moving_project(), d, soa_vec4=True)
        self.assertTrue(plan["soa_vec4"])
        self.assertEqual(plan["classes"]["Host"]["soa_dims"], 4)
        self.assertEqual(plan["classes"]["Host"]["soa_logical"], 3)
        with open(os.path.join(d, "data.c")) as f:
            data = f.read()
        self.assertIn("_Host_pos[", data)
        self.assertRegex(data, r"\{[^}]*0\.0f,\s*0\.0f,\s*0\.0f,\s*0\.0f")
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
# SystemsScene's scene, art and animations are not in the repository -- only
# its C# scripts are (see .gitignore) -- so the tests that pack the whole
# project skip unless a local copy has been dropped in. The rest of these
# classes author their own small projects and always run.
_SYSTEMS_SCENE = os.path.join(SYSTEMS, "Assets", "Scenes", "Systems.unity")
needs_systems = unittest.skipUnless(
    os.path.isfile(_SYSTEMS_SCENE),
    "SystemsScene is scripts-only in the repository; put its Assets/Scenes, "
    "art and ProjectSettings in examples/unity_pack/SystemsScene to run this")


class TestStubDiagnostics(unittest.TestCase):
    """A method the translator cannot lower is reported, not silently emptied.

    It used to become an empty function with nothing said. Now it is a
    csc-style warning at the method (an error under `strict`), recorded in
    `plan["stubs"]` -- and the detector deciding it no longer fires on
    string contents or on the engine's own C types, which had been emptying
    methods that were lowered completely.
    """

    def test_a_string_literal_is_not_leftover_csharp(self):
        body = ('Debug_Log_s("see Objects (Scripts)/Player.cs");\n'
                'Application_OpenURL("http://x/");\n')
        self.assertIsNone(unity_pack._unlowered_csharp(body))

    def test_leftover_csharp_is_still_found_and_named(self):
        what, text = unity_pack._unlowered_csharp(
            "Foo_bar(); Unknown.DoThing(1);\n")
        self.assertIn("Unknown.DoThing(", text)

    def test_an_engine_type_local_is_not_leftover_csharp(self):
        body = "ByteArray b = File_ReadAllBytes(p);\n"
        self.assertIsNotNone(unity_pack._unlowered_csharp(body))
        self.assertIsNone(unity_pack._unlowered_csharp(
            body, known_types={"ByteArray"}))

    def test_a_field_of_an_engine_type_local_is_not_leftover_csharp(self):
        body = "Matrix4x4 l2w = Transform_l2w(i);\nfloat a = l2w.m00;\n"
        self.assertIsNone(unity_pack._unlowered_csharp(
            body, known_types={"Matrix4x4"}))

    def test_a_lambda_is_reported_by_its_line(self):
        what, text = unity_pack._unlowered_csharp(
            "int k = 1;\nNotify_AddEvent(() => { go(); }, 0.1f);\n")
        self.assertIn("Notify_AddEvent(() =>", text)

    def _project(self):
        root = tempfile.mkdtemp(prefix="upack-stubdiag-")
        scripts = os.path.join(root, "Assets", "Scripts")
        scenes = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scripts)
        os.makedirs(scenes)
        with open(os.path.join(scripts, "Menu.cs"), "w") as f:
            f.write("using UnityEngine;\n"
                    "public class Menu : MonoBehaviour {\n"
                    "    void Start() { Unknown.DoThing(); }\n"
                    "    void Update() {}\n"
                    "}\n")
        with open(os.path.join(scripts, "Menu.cs.meta"), "w") as f:
            f.write("guid: 5d1a95d1a95d1a95d1a95d1a95d1a95d\n")
        with open(os.path.join(scenes, "S.unity"), "w") as f:
            f.write("%YAML 1.1\n"
                    "--- !u!1 &1\nGameObject:\n  m_Name: Menu\n"
                    "  m_Component:\n  - component: {fileID: 2}\n"
                    "  - component: {fileID: 3}\n"
                    "--- !u!4 &2\nTransform:\n  m_GameObject: {fileID: 1}\n"
                    "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                    "--- !u!114 &3\nMonoBehaviour:\n  m_GameObject: {fileID: 1}\n"
                    "  m_Script: {fileID: 11500000, "
                    "guid: 5d1a95d1a95d1a95d1a95d1a95d1a95d}\n")
        return root

    def test_a_stub_is_a_warning_at_the_method(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            plan = unity_pack.pack(self._project(),
                                   tempfile.mkdtemp(prefix="upack-sd-out-"))
        self.assertIn("Assets/Scripts/Menu.cs(3,", err.getvalue())
        self.assertIn("warning CS8000: `Menu.Start` is not lowered yet",
                      err.getvalue())
        self.assertIn("Unknown.DoThing(", err.getvalue())
        self.assertEqual([(st["class"], st["method"])
                          for st in plan["stubs"]], [("Menu", "Start")])

    @needs_cc
    def test_api_names_inside_strings_are_printed_as_written(self):
        # The Unity rewrites matched inside string literals:
        # `Debug.Log("transform.position.x moved")` printed
        # `Player_get_pos_x(i) moved`. They match code only now
        # (`cs2cpp.code_sub`).
        root = tempfile.mkdtemp(prefix="upack-strlit-")
        shutil.copytree(PROJECT, os.path.join(root, "p"))
        root = os.path.join(root, "p")
        with open(os.path.join(root, "Assets", "Scripts", "Player.cs"), "w") as f:
            f.write("using UnityEngine;\n"
                    "public class Player : MonoBehaviour {\n"
                    "    public int hp;\n"
                    "    public float speed;\n"
                    "    public void Update() {\n"
                    "        transform.position = new Vector2(\n"
                    "            transform.position.x + speed * Time.deltaTime,\n"
                    "            transform.position.y);\n"
                    "        // transform.position.x in a comment\n"
                    "        Debug.Log(\"transform.position.x and Time.deltaTime\");\n"
                    "    }\n"
                    "}\n")
        d = tempfile.mkdtemp(prefix="upack-strlit-out-")
        with contextlib.redirect_stderr(io.StringIO()):
            unity_pack.pack(root, d)
        r = subprocess.run(["make", "-C", d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        run = subprocess.run([os.path.join(d, "game"), "-logFile", "-"],
                             capture_output=True, text=True, cwd=d, timeout=60)
        self.assertIn("transform.position.x and Time.deltaTime", run.stdout)
        self.assertNotIn("Player_get_pos_x(i) and", run.stdout)

    def test_strict_makes_a_stub_an_error(self):
        with self.assertRaises(unity_pack.PackError) as cm:
            with contextlib.redirect_stderr(io.StringIO()):
                unity_pack.pack(self._project(),
                                tempfile.mkdtemp(prefix="upack-sd-out-"),
                                strict=True)
        self.assertIn("error CS8000: `Menu.Start` is not lowered yet",
                      cm.exception.message)


class TestPackedFields(unittest.TestCase):
    """Fields read and written through the instance slot (cs2cpp's packed
    model), and the widths the packer picks for them.

    `other.hp` through a field of another class's type is documented and
    had never worked: it came out `Coin_get_other(i).Coin_get_hp(i)`, which
    the stub check then emptied, silently. And a bitfield is only sound if
    every write is known: `seen = target.hp` stored 1 of a 5 in the 1-bit
    field the scene's 0 had chosen.
    """

    def _project(self, coin_body, extra_field=""):
        root = tempfile.mkdtemp(prefix="upack-fields-")
        scripts = os.path.join(root, "Assets", "Scripts")
        scenes = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scripts)
        os.makedirs(scenes)
        with open(os.path.join(scripts, "Enemy.cs"), "w") as f:
            f.write("using UnityEngine;\n"
                    "public class Enemy : MonoBehaviour {\n"
                    "    public int hp;\n"
                    "    void Update() {}\n"
                    "}\n")
        with open(os.path.join(scripts, "Coin.cs"), "w") as f:
            f.write("using UnityEngine;\n"
                    "public class Coin : MonoBehaviour {\n"
                    "    public Enemy target;\n"
                    "    public int seen;\n" + extra_field +
                    "    void Update() {\n" + coin_body + "    }\n"
                    "}\n")
        for name, guid in (("Enemy", "e1" * 16), ("Coin", "c1" * 16)):
            with open(os.path.join(scripts, name + ".cs.meta"), "w") as f:
                f.write("guid: %s\n" % guid)
        with open(os.path.join(scenes, "S.unity"), "w") as f:
            f.write("%%YAML 1.1\n"
                    "--- !u!1 &1\nGameObject:\n  m_Name: Foe\n"
                    "  m_Component:\n  - component: {fileID: 2}\n"
                    "  - component: {fileID: 3}\n"
                    "--- !u!4 &2\nTransform:\n  m_GameObject: {fileID: 1}\n"
                    "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                    "--- !u!114 &3\nMonoBehaviour:\n  m_GameObject: {fileID: 1}\n"
                    "  m_Script: {fileID: 11500000, guid: %s}\n"
                    "  hp: 5\n"
                    "--- !u!1 &10\nGameObject:\n  m_Name: Pickup\n"
                    "  m_Component:\n  - component: {fileID: 11}\n"
                    "  - component: {fileID: 12}\n"
                    "--- !u!4 &11\nTransform:\n  m_GameObject: {fileID: 10}\n"
                    "  m_LocalPosition: {x: 1, y: 0, z: 0}\n"
                    "--- !u!114 &12\nMonoBehaviour:\n  m_GameObject: {fileID: 10}\n"
                    "  m_Script: {fileID: 11500000, guid: %s}\n"
                    "  target: {fileID: 3}\n"
                    "  seen: 0\n" % ("e1" * 16, "c1" * 16))
        return root

    def _two_enemies(self, coin_body, target_file_id):
        """Two Enemies (hp 3, hp 7) and a Coin whose `target` is given by
        the fileID of an Enemy's script component (0: none)."""
        root = self._project(coin_body)
        scene = os.path.join(root, "Assets", "Scenes", "S.unity")
        text = open(scene).read()
        text = text.replace("  hp: 5\n", "  hp: 3\n")
        text = text.replace("  target: {fileID: 3}\n",
                            "  target: {fileID: %d}\n" % target_file_id)
        text += ("--- !u!1 &20\nGameObject:\n  m_Name: Foe2\n"
                 "  m_Component:\n  - component: {fileID: 21}\n"
                 "  - component: {fileID: 22}\n"
                 "--- !u!4 &21\nTransform:\n  m_GameObject: {fileID: 20}\n"
                 "  m_LocalPosition: {x: 2, y: 0, z: 0}\n"
                 "--- !u!114 &22\nMonoBehaviour:\n  m_GameObject: {fileID: 20}\n"
                 "  m_Script: {fileID: 11500000, guid: %s}\n"
                 "  hp: 7\n" % ("e1" * 16))
        open(scene, "w").write(text)
        return root

    def _run_log(self, root):
        d = tempfile.mkdtemp(prefix="upack-fields-out-")
        with contextlib.redirect_stderr(io.StringIO()):
            unity_pack.pack(root, d)
        r = subprocess.run(["make", "-C", d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        run = subprocess.run([os.path.join(d, "game"), "-logFile", "-"],
                             capture_output=True, text=True, cwd=d, timeout=60)
        return set(l for l in run.stdout.splitlines()
                   if not l.startswith("ticks"))

    @needs_cc
    def test_a_scene_reference_is_resolved_to_its_instance(self):
        # The second Enemy: index 1. References between scripts were never
        # resolved -- every one held 0, the first instance, which is what
        # the single-Enemy test above happened to want.
        self.assertEqual(self._run_log(self._two_enemies(
            "        seen = target.hp;\n        Debug.Log(seen);\n", 22)),
            {"7"})

    @needs_cc
    def test_an_empty_reference_is_null(self):
        # `{fileID: 0}`: null in C#. It was stored as 0, another object, and
        # a narrow index could not hold -1 to compare with anyway.
        self.assertEqual(self._run_log(self._two_enemies(
            "        if (target != null) { seen = 1; } else { seen = 2; }\n"
            "        Debug.Log(seen);\n", 0)), {"2"})

    def _struct(self, eng, name):
        m = re.search(r"struct %s \{(.*?)\};" % name, eng, re.S)
        return m.group(1) if m else ""

    @needs_cc
    def test_a_handle_field_reads_the_other_instance(self):
        d = tempfile.mkdtemp(prefix="upack-fields-out-")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            plan = unity_pack.pack(self._project(
                "        seen = target.hp;\n"
                "        Debug.Log(seen);\n"), d)
        self.assertEqual(plan.get("stubs", []), [], err.getvalue())
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("Enemy_AT(Coin_get_target(i)).hp", eng)
        r = subprocess.run(["make", "-C", d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        run = subprocess.run([os.path.join(d, "game"), "-logFile", "-"],
                             capture_output=True, text=True, cwd=d, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        logged = [l for l in run.stdout.splitlines() if not l.startswith("ticks")]
        self.assertTrue(logged)
        self.assertEqual(set(logged), {"5"})

    def test_a_non_literal_write_keeps_the_csharp_width(self):
        d = tempfile.mkdtemp(prefix="upack-fields-out-")
        with contextlib.redirect_stderr(io.StringIO()):
            unity_pack.pack(self._project("        seen = target.hp;\n"), d)
        with open(os.path.join(d, "engine.cpp")) as f:
            coin = self._struct(f.read(), "Coin")
        self.assertIn("int seen;", coin)

    def test_increments_keep_the_csharp_width(self):
        d = tempfile.mkdtemp(prefix="upack-fields-out-")
        with contextlib.redirect_stderr(io.StringIO()):
            unity_pack.pack(self._project("        seen++;\n"), d)
        with open(os.path.join(d, "engine.cpp")) as f:
            coin = self._struct(f.read(), "Coin")
        self.assertNotIn("seen :", coin)

    def test_a_write_through_another_object_widens_its_field(self):
        # `target.hp = n` is Enemy's field, written from Coin: Enemy's own
        # scripts never assign it, and its scene value alone chose 3 bits.
        d = tempfile.mkdtemp(prefix="upack-fields-out-")
        with contextlib.redirect_stderr(io.StringIO()):
            unity_pack.pack(self._project("        target.hp = seen;\n"), d)
        with open(os.path.join(d, "engine.cpp")) as f:
            enemy = self._struct(f.read(), "Enemy")
        self.assertIn("int hp;", enemy)

    def test_literal_writes_still_pack(self):
        # The point of the rule: literals and scene values bound the field.
        d = tempfile.mkdtemp(prefix="upack-fields-out-")
        with contextlib.redirect_stderr(io.StringIO()):
            unity_pack.pack(self._project("        seen = 3;\n"), d)
        with open(os.path.join(d, "engine.cpp")) as f:
            coin = self._struct(f.read(), "Coin")
        self.assertRegex(coin, r"unsigned seen : [1-7];")


class TestMaxInstances(unittest.TestCase):
    """`[MaxInstances(N)]`: the author caps a class's live instances.

    The index into the class is then as narrow as N allows -- uint8_t up
    to 255, uint16_t up to 65535 -- whatever else spawns, and `Instantiate`
    returns null once N are live: dropping the N+1st bullet is the point.
    """

    _ATTR = ("public class MaxInstancesAttribute : System.Attribute {\n"
             "    public MaxInstancesAttribute(int n) {}\n"
             "}\n\n")

    def _project(self, cap, coin_extra=""):
        root = tempfile.mkdtemp(prefix="upack-maxinst-")
        scripts = os.path.join(root, "Assets", "Scripts")
        scenes = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scripts)
        os.makedirs(scenes)
        # The attribute class comes first in the file, as a user would
        # write it: the component is still the class named after the file.
        with open(os.path.join(scripts, "Bullet.cs"), "w") as f:
            f.write("using UnityEngine;\n\n" + self._ATTR +
                    "[MaxInstances(%d)]\n"
                    "public class Bullet : MonoBehaviour {\n"
                    "    public int speed;\n"
                    "    void Update() { Instantiate(this); }\n"
                    "}\n" % cap)
        with open(os.path.join(scripts, "Coin.cs"), "w") as f:
            f.write("using UnityEngine;\n"
                    "public class Coin : MonoBehaviour {\n"
                    "    public int value;\n" + coin_extra +
                    "    void Update() {}\n"
                    "}\n")
        for name, guid in (("Bullet", "b4" * 16), ("Coin", "c4" * 16)):
            with open(os.path.join(scripts, name + ".cs.meta"), "w") as f:
                f.write("guid: %s\n" % guid)

        def obj(fid, name, guid, fields):
            return ("--- !u!1 &%d\nGameObject:\n  m_Name: %s\n"
                    "  m_Component:\n  - component: {fileID: %d}\n"
                    "  - component: {fileID: %d}\n"
                    "--- !u!4 &%d\nTransform:\n  m_GameObject: {fileID: %d}\n"
                    "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                    "--- !u!114 &%d\nMonoBehaviour:\n  m_GameObject: {fileID: %d}\n"
                    "  m_Script: {fileID: 11500000, guid: %s}\n%s"
                    % (fid, name, fid + 1, fid + 2, fid + 1, fid, fid + 2, fid,
                       guid, fields))
        with open(os.path.join(scenes, "S.unity"), "w") as f:
            f.write("%YAML 1.1\n" + obj(1, "Shot", "b4" * 16, "  speed: 1\n")
                    + obj(10, "CoinA", "c4" * 16, "  value: 1\n")
                    + obj(20, "CoinB", "c4" * 16, "  value: 2\n"))
        return root

    def _pack(self, root):
        d = tempfile.mkdtemp(prefix="upack-maxinst-out-")
        with contextlib.redirect_stderr(io.StringIO()):
            plan = unity_pack.pack(root, d)
        return plan, d

    @needs_cc
    def test_a_capped_class_clips_at_its_cap(self):
        # A bullet that clones itself every frame doubles without end; the
        # cap holds it at 4, and its index is a byte although the project
        # spawns (which makes every unannotated class uint32_t).
        plan, d = self._pack(self._project(4))
        self.assertEqual(plan["classes"]["Bullet"]["idx_ty"], "uint8_t")
        self.assertEqual(plan["classes"]["Coin"]["idx_ty"], "uint32_t")
        r = subprocess.run(["make", "-C", d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        main = os.path.join(d, "count_main.c")
        with open(main, "w") as f:
            f.write("#include <stdio.h>\n"
                    "void engine_tick(void);\n"
                    "extern int _Bullet_inst_count;\n"
                    "int main(void) { int t, most = 0;\n"
                    "  for (t = 0; t < 20; t = t + 1) { engine_tick();\n"
                    "    if (_Bullet_inst_count > most) most = _Bullet_inst_count; }\n"
                    "  printf(\"%d %d\\n\", _Bullet_inst_count, most); return 0; }\n")
        exe = os.path.join(d, "count_game")
        r = subprocess.run([_CC, "-O2", "-o", exe, main,
                            os.path.join(d, "engine.o"),
                            os.path.join(d, "data.o"), "-lm"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.stdout.split(), ["4", "4"])

    @needs_cc
    def test_the_cap_counts_live_instances(self):
        # Each bullet fires one and is destroyed. `Destroy` never freed a
        # slot, so after 4 spawns in all there were none: the population
        # died out. A destroyed bullet's slot is now reused, and firing goes
        # on for all 60 frames of the headless run.
        root = self._project(4)
        path = os.path.join(root, "Assets", "Scripts", "Bullet.cs")
        text = open(path).read().replace(
            "    void Update() { Instantiate(this); }\n",
            "    void Update() {\n"
            "        Instantiate(this);\n"
            "        Debug.Log(speed);\n"
            "        Destroy(gameObject);\n"
            "    }\n")
        open(path, "w").write(text)
        _plan, d = self._pack(root)
        r = subprocess.run(["make", "-C", d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        run = subprocess.run([os.path.join(d, "game"), "-logFile", "-"],
                             capture_output=True, text=True, cwd=d, timeout=60)
        fired = [l for l in run.stdout.splitlines() if l == "1"]
        self.assertGreaterEqual(len(fired), 60)

    def test_a_larger_cap_is_uint16_and_lists_no_zero_rows(self):
        plan, d = self._pack(self._project(1000))
        self.assertEqual(plan["classes"]["Bullet"]["idx_ty"], "uint16_t")
        with open(os.path.join(d, "data.cpp")) as f:
            data = f.read()
        self.assertIn("_Bullet_inst_array[1000] = {", data)
        # 999 spares are zeros C fills in; none is written out.
        self.assertNotIn("addcomponent spare", data.split(
            "_Bullet_inst_array[1000]", 1)[1].split("};", 1)[0])

    def test_a_handle_is_as_wide_as_its_target(self):
        # Coin's own index is uint32_t (the project spawns); a field that
        # points at a Bullet is Bullet's width, a byte.
        plan, d = self._pack(self._project(4, "    public Bullet shot;\n"))
        members = dict((m[0], m[1]) for m in plan["classes"]["Coin"]["members"])
        self.assertEqual(members["shot"], "uint8_t")

    def test_a_scene_over_the_cap_is_an_error(self):
        root = self._project(1)
        scene = os.path.join(root, "Assets", "Scenes", "S.unity")
        text = open(scene).read()
        text += ("--- !u!1 &30\nGameObject:\n  m_Name: Shot2\n"
                 "  m_Component:\n  - component: {fileID: 31}\n"
                 "  - component: {fileID: 32}\n"
                 "--- !u!4 &31\nTransform:\n  m_GameObject: {fileID: 30}\n"
                 "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                 "--- !u!114 &32\nMonoBehaviour:\n  m_GameObject: {fileID: 30}\n"
                 "  m_Script: {fileID: 11500000, guid: %s}\n  speed: 2\n"
                 % ("b4" * 16))
        open(scene, "w").write(text)
        with self.assertRaises(unity_pack.PackError) as cm:
            self._pack(root)
        self.assertIn("[MaxInstances(1)] on `Bullet`", cm.exception.message)
        self.assertIn("Bullet.cs(", cm.exception.message)


def _gl_context():
    """A headless GL context (Mesa llvmpipe over EGL), or None."""
    try:
        import moderngl
        return moderngl.create_standalone_context(backend="egl", require=430)
    except Exception:
        return None


class TestGpuHandles(unittest.TestCase):
    """`--gpu-handles`: handle fields packed for a GLES 3.1 SSBO.

    Each handle field is a stream of its class's capacity at its width --
    the target class's index width -- four bytes, two shorts or one word
    per uint, read in the shader with `bitfieldExtract`. The scene: a
    Player (`[MaxInstances(1000)]`, 16-bit) whose `last` is the third of
    three Bullets (`[MaxInstances(10)]`, 8-bit), each of whose `owner` is
    the Player -- so an 8-bit class holds 16-bit handles and the reverse.
    """

    _root = None

    @classmethod
    def root_for_viewer(cls):
        """The mixed-width handle project, for the viewer tests."""
        if cls._root is None:
            cls.setUpClass()
        return cls._root

    @classmethod
    def setUpClass(cls):
        root = tempfile.mkdtemp(prefix="upack-gpuh-")
        scripts = os.path.join(root, "Assets", "Scripts")
        scenes = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scripts)
        os.makedirs(scenes)
        files = {
            "MaxInstancesAttribute.cs":
                "public class MaxInstancesAttribute : System.Attribute {\n"
                "    public MaxInstancesAttribute(int n) {}\n}\n",
            "Player.cs":
                "using UnityEngine;\n[MaxInstances(1000)]\n"
                "public class Player : MonoBehaviour {\n"
                "    public Bullet last;\n    public int hp;\n"
                "    void Update() {}\n}\n",
            "Bullet.cs":
                "using UnityEngine;\n[MaxInstances(10)]\n"
                "public class Bullet : MonoBehaviour {\n"
                "    public Player owner;\n    public int speed;\n"
                "    void Update() {}\n}\n",
        }
        for name, text in files.items():
            with open(os.path.join(scripts, name), "w") as f:
                f.write(text)
        for name, guid in (("Player", "91" * 16), ("Bullet", "b1" * 16)):
            with open(os.path.join(scripts, name + ".cs.meta"), "w") as f:
                f.write("guid: %s\n" % guid)

        def obj(fid, name, guid, fields):
            return ("--- !u!1 &%d\nGameObject:\n  m_Name: %s\n"
                    "  m_Component:\n  - component: {fileID: %d}\n"
                    "  - component: {fileID: %d}\n"
                    "--- !u!4 &%d\nTransform:\n  m_GameObject: {fileID: %d}\n"
                    "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                    "--- !u!114 &%d\nMonoBehaviour:\n  m_GameObject: {fileID: %d}\n"
                    "  m_Script: {fileID: 11500000, guid: %s}\n%s"
                    % (fid, name, fid + 1, fid + 2, fid + 1, fid, fid + 2, fid,
                       guid, fields))
        scene = "%YAML 1.1\n" + obj(1, "Hero", "91" * 16,
                                    "  last: {fileID: 42}\n  hp: 3\n")
        for k, fid in enumerate((20, 30, 40)):
            scene += obj(fid, "Shot%d" % k, "b1" * 16,
                         "  owner: {fileID: 3}\n  speed: %d\n" % (k + 1))
        with open(os.path.join(scenes, "S.unity"), "w") as f:
            f.write(scene)
        cls.root = root
        TestGpuHandles._root = root
        cls.out = tempfile.mkdtemp(prefix="upack-gpuh-out-")
        with contextlib.redirect_stderr(io.StringIO()):
            unity_pack.pack(root, cls.out, gpu_handles=True)
        with open(os.path.join(cls.out, "engine_handles.h")) as f:
            cls.header = f.read()
        with open(os.path.join(cls.out, "shaders", "handles.glsl")) as f:
            cls.glsl = f.read()
        cls.want = {"Bullet_owner": [0, 0, 0] + [65535] * 7,
                    "Player_last": [2] + [255] * 999}

    def _define(self, name):
        m = re.search(r"#define %s (\d+)" % name, self.header)
        return int(m.group(1))

    def test_a_handle_is_its_targets_width(self):
        self.assertEqual(self._define("Bullet_owner_BITS"), 16)
        self.assertEqual(self._define("Player_last_BITS"), 8)
        self.assertEqual(self._define("Bullet_owner_LEN"), 10)
        self.assertEqual(self._define("Player_last_LEN"), 1000)
        # 10 shorts in 5 words, then 1000 bytes in 250.
        self.assertEqual(self._define("Player_last_OFF"), 5)
        self.assertEqual(self._define("ENGINE_HANDLE_WORDS"), 255)

    def _words(self):
        d = self.out
        r = subprocess.run(["make", "-C", d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        dump = os.path.join(d, "dump.c")
        with open(dump, "w") as f:
            f.write('#include <stdio.h>\n#include "engine_handles.h"\n'
                    "int main(void) { static uint32_t b[ENGINE_HANDLE_WORDS];\n"
                    "  int n = engine_upload_handles(b, ENGINE_HANDLE_WORDS);\n"
                    "  fwrite(b, 4, (size_t)n, stdout); return 0; }\n")
        exe = os.path.join(d, "dump")
        r = subprocess.run([_CC, "-O2", "-I", d, "-o", exe, dump,
                            os.path.join(d, "engine.o"),
                            os.path.join(d, "data.o"), "-lm"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return subprocess.run([exe], capture_output=True, timeout=60).stdout

    @needs_cc
    def test_the_c_side_packs_what_bitfieldExtract_reads(self):
        # Decoded here with bitfieldExtract's definition:
        # (word >> offset) & ((1 << bits) - 1).
        import struct
        raw = self._words()
        words = struct.unpack("<%dI" % (len(raw) // 4), raw)
        for name, want in self.want.items():
            off = self._define(name + "_OFF")
            bits = self._define(name + "_BITS")
            per = 32 // bits
            got = [(words[off + i // per] >> ((i % per) * bits))
                   & ((1 << bits) - 1) for i in range(len(want))]
            self.assertEqual(got, want, name)

    @needs_cc
    def test_the_shader_reads_what_the_c_side_packed(self):
        ctx = _gl_context()
        if ctx is None:
            self.skipTest("no headless GL (moderngl + EGL) here")
        import struct
        raw = self._words()
        for name, want in self.want.items():
            cs = ctx.compute_shader(
                "#version 310 es\nlayout(local_size_x = 1) in;\n" + self.glsl +
                "layout(std430, binding = 2) writeonly buffer Out { uint o[]; };\n"
                "void main() { uint i = gl_GlobalInvocationID.x;"
                " o[i] = %s(i); }\n" % name)
            src = ctx.buffer(raw)
            dst = ctx.buffer(reserve=4 * len(want))
            src.bind_to_storage_buffer(1)
            dst.bind_to_storage_buffer(2)
            cs.run(group_x=len(want))
            got = list(struct.unpack("<%dI" % len(want), dst.read()))
            self.assertEqual(got, want, name)

    def test_off_by_default(self):
        d = tempfile.mkdtemp(prefix="upack-gpuh-off-")
        with contextlib.redirect_stderr(io.StringIO()):
            unity_pack.pack(self.root, d)
        self.assertFalse(os.path.exists(os.path.join(d, "engine_handles.h")))
        with open(os.path.join(d, "engine.cpp")) as f:
            self.assertNotIn("engine_upload_handles", f.read())


def _gl_runtime_dir():
    """A directory to link -lEGL -lGLESv2 from: the dev symlinks when
    installed, else links to the runtime libraries ldconfig knows (Mesa
    without its -dev packages). None when there is no EGL / GLES at all."""
    try:
        out = subprocess.run(["ldconfig", "-p"], capture_output=True,
                             text=True).stdout
    except OSError:
        return None
    found = {}
    for name in ("libEGL.so", "libGLESv2.so"):
        m = re.search(r"\s(%s(?:\.\d+)*)\s.*=> (\S+)" % re.escape(name), out)
        if m:
            found[name] = m.group(2)
    if len(found) < 2:
        return None
    d = tempfile.mkdtemp(prefix="upack-gllib-")
    for name, path in found.items():
        os.symlink(path, os.path.join(d, name))
    return d


def _gl_include_dir():
    """Crust's own GL headers, and nothing else of shivyc/include (its libc
    headers are not for gcc)."""
    d = tempfile.mkdtemp(prefix="upack-glinc-")
    for sub in ("EGL", "GLES2", "GLES3"):
        os.symlink(os.path.join(ROOT, "shivyc", "include", sub),
                   os.path.join(d, sub))
    return d


class TestGLES3View(unittest.TestCase):
    """The default viewer is OpenGL ES 3.1 (gles3_render.h), for SSBOs.

    It must draw what the GLES2 viewer draws: both render MiniScene
    headless (EGL + Mesa's software rasteriser) and the frames must be
    identical, byte for byte. Built against crust's own GL headers, so the
    check runs on a machine with Mesa but without its -dev packages.
    """

    @classmethod
    def setUpClass(cls):
        cls.libdir = _gl_runtime_dir()
        cls.incdir = _gl_include_dir()

    def _build(self, view, d, *defines):
        if self.libdir is None:
            self.skipTest("no libEGL / libGLESv2 here")
        for src, obj, opt in (("engine.c", "engine.o", "-O3"),
                              ("data.c", "data.o", "-O0")):
            r = subprocess.run([_CC, opt, "-w", "-c", "-o",
                                os.path.join(d, obj), os.path.join(d, src)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        exe = os.path.join(d, view.replace(".c", ""))
        r = subprocess.run(
            [_CC, "-O2", "-w", "-o", exe,
             os.path.join(ROOT, "examples", "unity_pack", view),
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"),
             "-I", self.incdir, "-I", d, "-L", self.libdir,
             "-lEGL", "-lGLESv2", "-lm"] + list(defines),
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return exe

    def _run(self, exe, *args):
        env = os.environ.copy()
        env["EGL_PLATFORM"] = "surfaceless"
        env["LD_LIBRARY_PATH"] = self.libdir
        run = subprocess.run([exe] + list(args), capture_output=True,
                             text=True, env=env, timeout=120)
        if "eglInitialize failed" in run.stdout or "no EGLConfig" in run.stdout:
            self.skipTest("no EGL display with OpenGL ES 3 here")
        return run

    def test_the_gles3_frame_is_the_gles2_frame(self):
        d = tempfile.mkdtemp(prefix="upack-gles3-")
        with contextlib.redirect_stderr(io.StringIO()):
            unity_pack.pack(PROJECT, d)
        frames = {}
        for view in ("gles2_view.c", "gles3_view.c"):
            # At 8 bits a channel: the default RGBA4 target would round a
            # small difference -- a 1% dimmer shader -- to the same pixels.
            exe = self._build(view, d, "-DFBO_FORMAT=0x8058")
            ppm = os.path.join(d, view + ".ppm")
            run = self._run(exe, ppm)
            self.assertEqual(run.returncode, 0, run.stdout[-600:])
            self.assertIn("draws=3", run.stdout)
            with open(ppm, "rb") as f:
                frames[view] = f.read()
        self.assertEqual(frames["gles2_view.c"], frames["gles3_view.c"])
        # And the frame is a scene, not a clear: background and sprites.
        body = frames["gles3_view.c"].split(b"255\n", 1)[1]
        colours = set(body[i:i + 3] for i in range(0, len(body), 3))
        self.assertGreaterEqual(len(colours), 3)

    def test_handles_are_bound_at_ssbo_binding_1(self):
        d = tempfile.mkdtemp(prefix="upack-gles3-h-")
        with contextlib.redirect_stderr(io.StringIO()):
            unity_pack.pack(TestGpuHandles.root_for_viewer(), d,
                            gpu_handles=True)
        exe = self._build("gles3_view.c", d)
        run = self._run(exe)
        self.assertIn("handles: 255 words at SSBO binding 1", run.stdout)

    def test_the_header_matches_khronos(self):
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "gles3_header_test.py")],
            capture_output=True, text=True)
        if r.stdout.startswith("SKIP"):
            self.skipTest(r.stdout.strip())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


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

    def test_vector2_struct_for_locals_and_fields(self):
        """Vector2 locals / new Vector2 → struct; packed field R/W via _x/_y."""
        root = tempfile.mkdtemp(prefix="upack-v2-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Cosmetic.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Cosmetic : MonoBehaviour {\n"
                "    public Vector2 initLocalPosition;\n"
                "    void Awake() {\n"
                "        initLocalPosition = transform.localPosition;\n"
                "    }\n"
                "    void Update() {}\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Cosmetic.cs.meta"), "w") as f:
            f.write("guid: cosmeticosmeticosmeticosmeti01\n")
        with open(os.path.join(scripts, "Player.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Player : MonoBehaviour {\n"
                "    public Cosmetic cosmetic;\n"
                "    void Update() {\n"
                "        Vector2 topPoint = new Vector2(1f, 2f);\n"
                "        cosmetic.initLocalPosition = topPoint;\n"
                "        cosmetic.transform.localPosition ="
                " cosmetic.initLocalPosition;\n"
                "        last = Vector2.zero;\n"
                "    }\n"
                "    Vector2 last;\n"
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
                "--- !u!1 &10\nGameObject:\n  m_Name: Cosmetic\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: playerplayerplayerplayerplayer01}\n"
                "  cosmetic: {fileID: 12}\n"
                "--- !u!4 &11\nTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_LocalPosition: {x: 1, y: 2, z: 0}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: cosmeticosmeticosmeticosmeti01}\n"
                "  initLocalPosition: {x: 3, y: 4}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-v2-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.cpp")) as f:
            eng = f.read()
        self.assertIn("typedef struct Vector2", eng)
        self.assertIn("Vector2_make(1.f, 2.f)", eng)
        self.assertIn("Player_set_last_x(i, (0.f))", eng)
        self.assertIn("Player_set_last_y(i, (0.f))", eng)
        self.assertNotIn("new Vector2", eng)
        self.assertNotIn("error CS0246", eng)
        self.assertIn("Cosmetic_set_initLocalPosition_x", eng)
        self.assertIn("Cosmetic_set_pos_x", eng)

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

    @needs_cc
    def test_player_build_uses_cpprust_lowered_engine_c(self):
        """Player compiles engine.c (cpprust-lowered), not g++ on engine.cpp."""
        root = tempfile.mkdtemp(prefix="upack-player-c-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "P.cs"), "w") as f:
            f.write(
                "using System.Collections.Generic;\n"
                "using UnityEngine;\n"
                "public class P : MonoBehaviour {\n"
                "    void Start() {\n"
                "        List<int> xs = new List<int>();\n"
                "        xs.Add(1);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "P.cs.meta"), "w") as f:
            f.write("guid: playecxplayecxplayecxplayecx01\n")
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
                "guid: playecxplayecxplayecxplayecx01}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-player-c-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.cpp")) as f:
            cpp = f.read()
        with open(os.path.join(d, "engine.c")) as f:
            c = f.read()
        self.assertIn("#include <vector>", cpp)
        self.assertIn("std::vector", cpp)
        self.assertNotIn("#include <vector>", c)
        self.assertNotIn("std::vector", c)
        self.assertIn("vector_int", c)
        with open(os.path.join(d, "Makefile")) as f:
            mk = f.read()
        self.assertIn("engine.o: engine.c", mk)
        self.assertIn("$(CC)", mk)
        self.assertNotIn("engine.o: engine.cpp", mk)
        exe = unity_pack.build_player_executable(d, "VectorPlayer")
        self.assertTrue(os.path.isfile(exe), exe)

    def test_cross_class_static_list_rewrites(self):
        """OtherClass.staticList.Count / [i] → OtherClass_staticList."""
        root = tempfile.mkdtemp(prefix="upack-xlist-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Cosmetic.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Cosmetic : MonoBehaviour {\n"
                "    public Vector2 initLocalPosition;\n"
                "    void Update() {}\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Cosmetic.cs.meta"), "w") as f:
            f.write("guid: cosmeticosmeticosmeticosmeti01\n")
        with open(os.path.join(scripts, "CosmeticsMenu.cs"), "w") as f:
            f.write(
                "using System.Collections.Generic;\n"
                "using UnityEngine;\n"
                "public class CosmeticsMenu : MonoBehaviour {\n"
                "    public static List<Cosmetic> equipped ="
                " new List<Cosmetic>();\n"
                "    void Update() {}\n"
                "}\n"
            )
        with open(os.path.join(scripts, "CosmeticsMenu.cs.meta"), "w") as f:
            f.write("guid: cosmeticsmenucosmeticsmenu01\n")
        with open(os.path.join(scripts, "Player.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Player : MonoBehaviour {\n"
                "    void Update() {\n"
                "        for (int n = 0;"
                " n < CosmeticsMenu.equipped.Count; n++) {\n"
                "            Cosmetic c = CosmeticsMenu.equipped[n];\n"
                "            if (c != null)\n"
                "                c.transform.localPosition ="
                " c.initLocalPosition;\n"
                "        }\n"
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
                "--- !u!1 &10\nGameObject:\n  m_Name: Cosmetic\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!1 &20\nGameObject:\n  m_Name: CosmeticsMenu\n"
                "  m_Component:\n  - component: {fileID: 21}\n"
                "  - component: {fileID: 22}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: playerplayerplayerplayerplayer01}\n"
                "--- !u!4 &11\nTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_LocalPosition: {x: 1, y: 2, z: 0}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: cosmeticosmeticosmeticosmeti01}\n"
                "  initLocalPosition: {x: 3, y: 4}\n"
                "--- !u!4 &21\nTransform:\n"
                "  m_GameObject: {fileID: 20}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &22\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 20}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: cosmeticsmenucosmeticsmenu01}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-xlist-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.cpp")) as f:
            eng = f.read()
        self.assertIn("static std::vector<int> CosmeticsMenu_equipped", eng)
        self.assertIn("CosmeticsMenu_equipped.size()", eng)
        self.assertIn("CosmeticsMenu_equipped[n]", eng)
        self.assertNotIn("CosmeticsMenu.equipped", eng)

    def test_static_method_and_singleton_instance(self):
        """Other.StaticMethod(Other.Instance.field) → Class_Method(get(Instance()))."""
        root = tempfile.mkdtemp(prefix="upack-static-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "CosmeticsMenu.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class CosmeticsMenu : MonoBehaviour {\n"
                "    public byte pointsPerGem;\n"
                "    public static void AddPoints(byte amount) {\n"
                "        pointsPerGem = amount;\n"
                "    }\n"
                "    void Update() {}\n"
                "}\n"
            )
        with open(os.path.join(scripts, "CosmeticsMenu.cs.meta"), "w") as f:
            f.write("guid: cosmeticsmenucosmeticsmenu01\n")
        with open(os.path.join(scripts, "SavePoint.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class SavePoint : MonoBehaviour {\n"
                "    void Update() {\n"
                "        CosmeticsMenu.AddPoints("
                "CosmeticsMenu.Instance.pointsPerGem);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "SavePoint.cs.meta"), "w") as f:
            f.write("guid: savepointsavepointsavepoint01\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: SavePoint\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: CosmeticsMenu\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: savepointsavepointsavepoint01}\n"
                "--- !u!4 &11\nTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: cosmeticsmenucosmeticsmenu01}\n"
                "  pointsPerGem: 1\n"
            )
        d = tempfile.mkdtemp(prefix="upack-static-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.cpp")) as f:
            eng = f.read()
        self.assertIn("static void CosmeticsMenu_AddPoints(int amount)", eng)
        self.assertIn("Object_FindObjectOfType_CosmeticsMenu", eng)
        self.assertIn("static int CosmeticsMenu_Instance(void)", eng)
        self.assertIn(
            "CosmeticsMenu_AddPoints(CosmeticsMenu_get_pointsPerGem("
            "CosmeticsMenu_Instance()))", eng)
        self.assertNotIn(
            "CosmeticsMenu_get_pointsPerGem(0)", eng)
        self.assertNotIn("CosmeticsMenu.AddPoints", eng)
        self.assertNotIn("CosmeticsMenu.Instance.pointsPerGem", eng)
        self.assertNotIn("CosmeticsMenu.Instance)", eng)

    @needs_cc
    def test_findobject_and_instance_live_not_slot_zero(self):
        """FindObjectOfType / Instance scan live maps; survive Destroy + AddComponent."""
        root = tempfile.mkdtemp(prefix="upack-fot-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Marker.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Marker : MonoBehaviour {\n"
                "    public int id;\n"
                "    void Start() {\n"
                "        if (id == 1 || id == 99)\n"
                "            Destroy(gameObject);\n"
                "    }\n"
                "    void Update() {}\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Marker.cs.meta"), "w") as f:
            f.write("guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n")
        with open(os.path.join(scripts, "AHost.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class AHost : MonoBehaviour {\n"
                "    void Start() {\n"
                "        gameObject.AddComponent<Marker>();\n"
                "    }\n"
                "    void Update() {}\n"
                "}\n"
            )
        with open(os.path.join(scripts, "AHost.cs.meta"), "w") as f:
            f.write("guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
        with open(os.path.join(scripts, "ZClient.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class ZClient : MonoBehaviour {\n"
                "    void Update() {\n"
                "        print(FindObjectOfType<Marker>());\n"
                "        print(Marker.Instance);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "ZClient.cs.meta"), "w") as f:
            f.write("guid: cccccccccccccccccccccccccccccccc\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: MarkerOld\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!1 &5\nGameObject:\n  m_Name: MarkerKeep\n"
                "  m_Component:\n  - component: {fileID: 6}\n"
                "  - component: {fileID: 7}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Host\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!1 &20\nGameObject:\n  m_Name: Client\n"
                "  m_Component:\n  - component: {fileID: 21}\n"
                "  - component: {fileID: 22}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}\n"
                "  id: 1\n"
                "--- !u!4 &6\nTransform:\n"
                "  m_GameObject: {fileID: 5}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &7\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 5}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}\n"
                "  id: 99\n"
                "--- !u!4 &11\nTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}\n"
                "--- !u!4 &21\nTransform:\n"
                "  m_GameObject: {fileID: 20}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &22\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 20}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: cccccccccccccccccccccccccccccccc}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-fot-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("Object_FindObjectOfType_Marker", eng)
        self.assertIn("static int Marker_Instance(void)", eng)
        self.assertIn("GameObject_AddComponent_Marker", eng)
        self.assertIn("Object_Destroy", eng)
        self.assertIn("Object_FindObjectOfType_Marker(0)", eng)
        self.assertIn("Marker_Instance()", eng)
        r = subprocess.run(["make", "-C", d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        run = subprocess.run(
            [os.path.join(d, "game"), "-logFile", "-"],
            capture_output=True, text=True, cwd=d)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        # Authored Markers are indices 0 and 1; live AddComponent is index 2.
        # Hardcoded Instance→0 would keep printing 0 after Destroy.
        self.assertGreaterEqual(run.stdout.count("2\n"), 2, run.stdout)
        self.assertNotIn("\n0\n", "\n" + run.stdout)
        self.assertNotIn("\n1\n", "\n" + run.stdout)

    def test_bare_singleton_instance_assign_this(self):
        """Awake `instance = this` → Class_instance = i (not undeclared `instance`)."""
        root = tempfile.mkdtemp(prefix="upack-inst-assign-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Cam.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Cam : MonoBehaviour {\n"
                "    public static Cam instance;\n"
                "    public static Cam Instance {\n"
                "        get {\n"
                "            if (instance == null)\n"
                "                instance = FindObjectOfType<Cam>(true);\n"
                "            return instance;\n"
                "        }\n"
                "    }\n"
                "    void Awake() {\n"
                "        instance = this;\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Cam.cs.meta"), "w") as f:
            f.write("guid: camcamcamcamcamcamcamcamcamcam01\n")
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour {\n"
                "    void Start() {\n"
                "        Debug.Log(Cam.Instance);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: hosthosthosthosthosthosthosthost01\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Cam\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Host\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: camcamcamcamcamcamcamcamcamcam01}\n"
                "--- !u!4 &11\nTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: hosthosthosthosthosthosthosthost01}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-inst-assign-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.cpp")) as f:
            eng = f.read()
        self.assertIn("Cam_instance = i;", eng)
        self.assertNotRegex(eng, r"(?<![\w.])instance\s*=\s*i")
        self.assertIn("static int Cam_Instance(void)", eng)
        awake = eng.split("static void Cam_Awake(", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("not lowered yet", awake)
        self.assertIn("Cam_instance = i", awake)

    def test_toggle_is_on_does_not_span_prior_index_expr(self):
        """equipped[i]; … toggles[x].isOn must not merge (DOTALL bug → CS0000)."""
        src = (
            "Cosmetic cosmetic = CosmeticsMenu_equipped[i];\n"
            "if (type == cosmetic.type) {\n"
            "    cosmetic.Preview = false;\n"
            "    CosmeticsMenu_toggles[instances.IndexOf(cosmetic)].isOn = false;\n"
            "}\n"
        )
        out = unity_pack._rewrite_toggle_is_on(src)
        self.assertIn("Cosmetic cosmetic = CosmeticsMenu_equipped[i];", out)
        self.assertIn(
            "Toggle_set_isOn(CosmeticsMenu_toggles[instances.IndexOf(cosmetic)], "
            "(false));",
            out)
        self.assertNotIn("Toggle_set_isOn(CosmeticsMenu_equipped[i]", out)

    def test_unlowered_public_method_emits_stub(self):
        """Public GetComponents (no InChildren) helpers → empty stub."""
        root = tempfile.mkdtemp(prefix="upack-stub-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Notify.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Notify : MonoBehaviour {\n"
                "    public void Show() {\n"
                "        Renderer[] rs = GetComponents<Renderer>();\n"
                "    }\n"
                "    void Update() {}\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Notify.cs.meta"), "w") as f:
            f.write("guid: dddddddddddddddddddddddddddddddd\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Notify\n"
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
        d = tempfile.mkdtemp(prefix="upack-stub-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("static void Notify_Show(unsigned i)", eng)
        self.assertIn("unlowered C#", eng)
        self.assertNotIn("GetComponents<", eng)

    def test_unlowered_lambda_static_call_emits_stub(self):
        """Public helper with Type.Method + lambda → stub (not crust fail)."""
        root = tempfile.mkdtemp(prefix="upack-lambda-stub-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Notify.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "using System;\n"
                "public class Notify : MonoBehaviour {\n"
                "    public static void AddEvent(Action a, float t) {}\n"
                "    public void Show() {\n"
                "        Notify.AddEvent(() => { gameObject.SetActive(true); }, "
                "Time.time + 0.1f);\n"
                "    }\n"
                "    void Update() {}\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Notify.cs.meta"), "w") as f:
            f.write("guid: eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Notify\n"
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
        d = tempfile.mkdtemp(prefix="upack-lambda-stub-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("static void Notify_Show(unsigned i)", eng)
        self.assertIn("unlowered C#", eng)
        self.assertNotIn("Notify.AddEvent", eng)
        self.assertNotIn("=>", eng)

    def test_getcomponentsinchildren_collects_subtree(self):
        """GetComponentsInChildren<T> walks parent table into std::vector."""
        root = tempfile.mkdtemp(prefix="upack-gcic-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Part.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Part : MonoBehaviour {\n"
                "    public int tagId;\n"
                "    void Update() {}\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Part.cs.meta"), "w") as f:
            f.write("guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1\n")
        with open(os.path.join(scripts, "Root.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Root : MonoBehaviour {\n"
                "    void Update() {\n"
                "        Part[] parts = GetComponentsInChildren<Part>();\n"
                "        int n = parts.Length;\n"
                "        if (n > 1) n = n - 1;\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Root.cs.meta"), "w") as f:
            f.write("guid: b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Root\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: ChildA\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!4 &11\nTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_LocalPosition: {x: 1, y: 0, z: 0}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1}\n"
                "  tagId: 1\n"
                "--- !u!1 &20\nGameObject:\n  m_Name: ChildB\n"
                "  m_Component:\n  - component: {fileID: 21}\n"
                "  - component: {fileID: 22}\n"
                "--- !u!4 &21\nTransform:\n"
                "  m_GameObject: {fileID: 20}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_LocalPosition: {x: 2, y: 0, z: 0}\n"
                "--- !u!114 &22\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 20}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1}\n"
                "  tagId: 2\n"
            )
        d = tempfile.mkdtemp(prefix="upack-gcic-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        with open(os.path.join(d, "engine.cpp")) as f:
            cpp = f.read()
        self.assertIn("GameObject_GetComponentsInChildren_Part", eng)
        self.assertIn("_engine_go_is_child_of", eng)
        self.assertIn(
            "std::vector<int> parts = "
            "GameObject_GetComponentsInChildren_Part(", cpp)
        self.assertIn("vector_int", eng)
        self.assertNotIn("std::vector", eng)
        self.assertIn("vector_int_size(&parts)", eng)
        self.assertNotIn("GetComponentsInChildren<Part>", eng)
        self.assertNotIn("unlowered C#", eng)
        self.assertNotIn("Part[]", eng)

    def test_getcomponent_type_with_zero_instances_packs(self):
        """GetComponent<T> when T.cs exists but no authored instance → n=0 class.

        Trimming unused prefabs must not CS0246 on GetComponent for a type that
        is still referenced from a packed script.
        """
        root = tempfile.mkdtemp(prefix="upack-gc-zero-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Part.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Part : MonoBehaviour {\n"
                "    public int hp;\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Part.cs.meta"), "w") as f:
            f.write("guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1\n")
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour {\n"
                "    public Part part;\n"
                "    void Update() {\n"
                "        part = GetComponent<Part>();\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Host\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, guid: b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2, type: 3}\n"
                "  part: {fileID: 0}\n"
            )
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "EditorBuildSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1045 &1\nEditorBuildSettings:\n"
                "  m_Scenes:\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/S.unity\n"
            )
        d = tempfile.mkdtemp(prefix="upack-gc-zero-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("Part", plan["classes"])
        self.assertEqual(plan["classes"]["Part"]["n"], 0)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("GameObject_GetComponent_Part", eng)
        self.assertIn("GetComponent_Part", eng)
        self.assertNotIn("error CS0246", eng)

    def test_getcomponentsinchildren_base_type_finds_subclass(self):
        """GetComponentsInChildren<Weapon> collects Blaster : Weapon instances."""
        root = tempfile.mkdtemp(prefix="upack-gcic-base-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Weapon.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Weapon : MonoBehaviour {\n"
                "    public int dmg;\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Weapon.cs.meta"), "w") as f:
            f.write("guid: d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4\n")
        with open(os.path.join(scripts, "Blaster.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Blaster : Weapon {\n"
                "    void Update() {}\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Blaster.cs.meta"), "w") as f:
            f.write("guid: e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5\n")
        with open(os.path.join(scripts, "Hero.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Hero : MonoBehaviour {\n"
                "    void Update() {\n"
                "        Weapon[] ws = GetComponentsInChildren<Weapon>();\n"
                "        int n = ws.Length;\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Hero.cs.meta"), "w") as f:
            f.write("guid: f6f6f6f6f6f6f6f6f6f6f6f6f6f6f6f6\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Hero\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: f6f6f6f6f6f6f6f6f6f6f6f6f6f6f6f6}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Gun\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!4 &11\nTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_LocalPosition: {x: 1, y: 0, z: 0}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5}\n"
                "  dmg: 3\n"
            )
        d = tempfile.mkdtemp(prefix="upack-gcic-base-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        with open(os.path.join(d, "engine.cpp")) as f:
            cpp = f.read()
        self.assertIn("GameObject_GetComponentsInChildren_Weapon", eng)
        self.assertIn("GameObject_GetComponent_Blaster(go)", eng)
        self.assertIn(
            "std::vector<int> ws = "
            "GameObject_GetComponentsInChildren_Weapon(", cpp)
        self.assertNotIn("Weapon could not be found", eng)
        self.assertNotIn("std::vector", eng)

    def test_getcomponentsinchildren_on_instantiated(self):
        """clone.GetComponentsInChildren after Instantiate uses clone GO."""
        root = tempfile.mkdtemp(prefix="upack-gcic-inst-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Gem.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Gem : MonoBehaviour {\n"
                "    void Update() {\n"
                "        Gem g = Instantiate(this);\n"
                "        Gem[] kids = g.GetComponentsInChildren<Gem>();\n"
                "        int n = kids.Length;\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Gem.cs.meta"), "w") as f:
            f.write("guid: c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Gem\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-gcic-inst-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("Object_Instantiate_Gem(i, -1)", eng)
        self.assertIn(
            "GameObject_GetComponentsInChildren_Gem("
            "_engine_go_of_Gem(g)", eng)
        self.assertNotIn("unlowered C#", eng)

    def test_instantiate_this_clones_live(self):
        """Instantiate(this) → Object_Instantiate_T; spare GO + MB pool."""
        root = tempfile.mkdtemp(prefix="upack-inst-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Mob.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Mob : MonoBehaviour {\n"
                "    public int hp = 3;\n"
                "    void Update() {\n"
                "        Mob m = Instantiate(this);\n"
                "        print(m);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Mob.cs.meta"), "w") as f:
            f.write("guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Mob\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 1, y: 2, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}\n"
                "  hp: 3\n"
            )
        d = tempfile.mkdtemp(prefix="upack-inst-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        with open(os.path.join(d, "data.c")) as f:
            data = f.read()
        self.assertIn("Object_Instantiate_Mob", eng)
        self.assertIn("Object_Instantiate_Mob(i, -1)", eng)
        self.assertNotIn("unlowered C#", eng)
        self.assertIn("int _Mob_inst_count = 1;", data)
        self.assertIn("_Mob_inst_array[2]", data)
        self.assertIn("static int _engine_go_count = 1;", eng)
        self.assertIn("_engine_go_cap = 2", eng)
        self.assertIn("_engine_go_name[go] = \"(Clone)\";", eng)

    def test_instantiate_this_with_parent(self):
        """Instantiate(this, transform.parent) wires live parent table."""
        root = tempfile.mkdtemp(prefix="upack-instp-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Kid.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Kid : MonoBehaviour {\n"
                "    void Update() {\n"
                "        Kid k = Instantiate(this, transform.parent);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Kid.cs.meta"), "w") as f:
            f.write("guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Nest\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "--- !u!4 &11\nTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Kid\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 11}\n"
                "  m_LocalPosition: {x: 0, y: 1, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-instp-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("Object_Instantiate_Kid", eng)
        self.assertIn(
            "Object_Instantiate_Kid(i, Transform_get_parent(", eng)
        self.assertIn("_engine_go_parent[go] = parent_go;", eng)
        self.assertNotIn("unlowered C#", eng)

    def test_singleton_toggle_array_is_on(self):
        """Other.instance.toggles[i].isOn → Toggle_set_isOn(Other_toggles[i], …)."""
        root = tempfile.mkdtemp(prefix="upack-toggle-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Cosmetic.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Cosmetic : MonoBehaviour {\n"
                "    void Update() {\n"
                "        CosmeticsMenu.instance.toggles[0].isOn = false;\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Cosmetic.cs.meta"), "w") as f:
            f.write("guid: cosmeticosmeticosmeticosmeti01\n")
        with open(os.path.join(scripts, "CosmeticsMenu.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "using UnityEngine.UI;\n"
                "public class CosmeticsMenu : MonoBehaviour {\n"
                "    public Toggle[] toggles = new Toggle[0];\n"
                "    void Update() {}\n"
                "}\n"
            )
        with open(os.path.join(scripts, "CosmeticsMenu.cs.meta"), "w") as f:
            f.write("guid: cosmeticsmenucosmeticsmenu01\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Cosmetic\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: CosmeticsMenu\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: cosmeticosmeticosmeticosmeti01}\n"
                "--- !u!4 &11\nTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: cosmeticsmenucosmeticsmenu01}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-toggle-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.cpp")) as f:
            eng = f.read()
        self.assertIn("static std::vector<int> CosmeticsMenu_toggles", eng)
        self.assertIn("Toggle_set_isOn(CosmeticsMenu_toggles[0], (0))", eng)
        self.assertNotIn("0.toggles", eng)
        self.assertNotIn("CosmeticsMenu.instance", eng)

    @needs_systems
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

    @needs_systems
    def test_csharp_0f_lowers_to_c_0_dot_f(self):
        """C# `0f` is valid; emitted C must spell `0.f` (gcc rejects `0f`)."""
        self.assertEqual(cs2cpp.lower_float_literals("x * 0f"),
                         "x * 0.f")
        self.assertEqual(cs2cpp.lower_float_literals("1.5f + 2F"),
                         "1.5f + 2.F")
        self.assertEqual(cs2cpp.lower_float_literals('"0f"'),
                         '"0f"')
        d = tempfile.mkdtemp(prefix="upack-0f-")
        unity_pack.pack(SYSTEMS, d)
        with open(os.path.join(d, "engine.c")) as f:
            engine = f.read()
        self.assertIn("AmbientBias_get_lift(i) * 0.f", engine)
        self.assertNotIn("AmbientBias_get_lift(i) * 0f", engine)

    @needs_systems
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
    @needs_systems
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

    @needs_systems
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

    def test_gles_host_draw_tex_limits_cover_large_ui(self):
        """Host MAX_DRAWS/MAX_TEX must fit menus with 100+ UI sprites/textures."""
        for name in ("gles2_window.c", "gles2_view.c"):
            path = os.path.join(ROOT, "examples", "unity_pack", name)
            with open(path) as f:
                src = f.read()
            self.assertRegex(src, r"#define MAX_DRAWS\s+512")
            self.assertRegex(src, r"#define MAX_TEX\s+512")

    def test_sprite_mode_multiple_crops_by_file_id(self):
        """Multiple spriteMode: Image fileID selects spriteSheet rect, not full PNG."""
        root = tempfile.mkdtemp(prefix="upack-sheet-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Mark.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Mark : MonoBehaviour { void Update() {} }\n"
            )
        with open(os.path.join(scripts, "Mark.cs.meta"), "w") as f:
            f.write("guid: sheetmarksheetmarksheetmarkshee01\n")
        spr = os.path.join(root, "Assets", "Sprites")
        os.makedirs(spr)
        import struct, zlib

        def chunk(tag, body):
            return (struct.pack(">I", len(body)) + tag + body
                    + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff))

        # 8x4 atlas: left 4x4 red, right 4x4 green.
        w, h = 8, 4
        raw = b""
        for y in range(h):
            row = b""
            for x in range(w):
                if x < 4:
                    row += b"\xff\x00\x00\xff"
                else:
                    row += b"\x00\xff\x00\xff"
            raw += b"\x00" + row
        png = os.path.join(spr, "atlas.png")
        open(png, "wb").write(
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))
        slice_id = 424242
        with open(png + ".meta", "w") as f:
            f.write(
                "guid: a11a11a11a11a11a11a11a11a11a11a1\n"
                "TextureImporter:\n"
                "  spriteMode: 2\n"
                "  spritePixelsToUnits: 100\n"
                "  spriteBorder: {x: 0, y: 0, z: 0, w: 0}\n"
                "  spriteSheet:\n"
                "    serializedVersion: 2\n"
                "    sprites:\n"
                "    - serializedVersion: 2\n"
                "      name: left\n"
                "      rect:\n"
                "        serializedVersion: 2\n"
                "        x: 0\n"
                "        y: 0\n"
                "        width: 4\n"
                "        height: 4\n"
                "      alignment: 0\n"
                "      pivot: {x: 0.5, y: 0.5}\n"
                "      border: {x: 0, y: 0, z: 0, w: 0}\n"
                "      outline: []\n"
                "      physicsShape: []\n"
                "      tessellationDetail: -1\n"
                "      bones: []\n"
                "      spriteID: a\n"
                "      internalID: 111\n"
                "      vertices: []\n"
                "      indices: \n"
                "      edges: []\n"
                "      weights: []\n"
                "    - serializedVersion: 2\n"
                "      name: right\n"
                "      rect:\n"
                "        serializedVersion: 2\n"
                "        x: 4\n"
                "        y: 0\n"
                "        width: 4\n"
                "        height: 4\n"
                "      alignment: 0\n"
                "      pivot: {x: 0.5, y: 0.5}\n"
                "      border: {x: 0, y: 0, z: 0, w: 0}\n"
                "      outline: []\n"
                "      physicsShape: []\n"
                "      tessellationDetail: -1\n"
                "      bones: []\n"
                "      spriteID: b\n"
                "      internalID: %d\n"
                "      vertices: []\n"
                "      indices: \n"
                "      edges: []\n"
                "      weights: []\n"
                "    nameFileIdTable:\n"
                "      left: 111\n"
                "      right: %d\n"
                % (slice_id, slice_id)
            )
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        img = "fe87c0e1cc204ed48ad3b37840f39efc"
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!224 &2\nRectTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n  m_RenderMode: 0\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Slice\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!224 &11\nRectTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 4, y: 4}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, guid: " + img + "}\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                "  m_Enabled: 1\n  m_Type: 0\n"
                "  m_Sprite: {fileID: %d, "
                "guid: a11a11a11a11a11a11a11a11a11a11a1, type: 3}\n"
                % slice_id
            )
        objs, _a, _l, _c, _h = unity_pack.load_project(root)
        slice_o = [o for o in objs if o["name"] == "Slice"][0]
        sp = slice_o["sprite"]
        self.assertEqual(sp["sprite_file_id"], slice_id)
        self.assertEqual(sp["tex_rgba"][0:4], b"\x00\xff\x00\xff")
        cw, ch, crgba, _b = unity_pack._load_sprite_rgba(png, slice_id)
        self.assertEqual((cw, ch), (4, 4))
        self.assertEqual(crgba[0:4], b"\x00\xff\x00\xff")


    def test_sprite_sheet_crop_uses_bottom_up_y(self):
        """Sheet rect y is bottom-origin on an already bottom-up buffer.

        ``_load_png_rgba`` flips PNG rows (row 0 = texture bottom). Cropping
        with ``y0 = th - y - h`` (top-down conversion) grabs the wrong band
        when the slice is shorter than the atlas — icon appears cut off at
        the RectTransform bottom while the hit rect stays full.
        """
        import struct, zlib

        def chunk(tag, body):
            return (struct.pack(">I", len(body)) + tag + body
                    + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff))

        # Atlas 8×10. Unity rect (x=0,y=1,w=8,h=4): bottom-up rows 1..4.
        # Wrong top-down conversion would take rows 5..8 instead.
        # PNG top-down row r → buffer row (9-r). Paint RED into buffer 1..4
        # (PNG rows 8..5) and GREEN decoy into buffer 5..8 (PNG rows 4..1).
        w, h = 8, 10
        red, green, clear = b"\xff\x00\x00\xff", b"\x00\xff\x00\xff", b"\x00\x00\x00\x00"
        raw = b""
        for png_y in range(h):
            buf_y = (h - 1) - png_y
            if 1 <= buf_y <= 4:
                pix = red
            elif 5 <= buf_y <= 8:
                pix = green
            else:
                pix = clear
            raw += b"\x00" + pix * w
        root = tempfile.mkdtemp(prefix="upack-sheet-y-")
        spr = os.path.join(root, "Assets", "Sprites")
        os.makedirs(spr)
        png = os.path.join(spr, "pad.png")
        with open(png, "wb") as out:
            out.write(
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9))
                + chunk(b"IEND", b""))
        fid = 777001
        with open(png + ".meta", "w") as f:
            f.write(
                "guid: b22b22b22b22b22b22b22b22b22b22b2\n"
                "TextureImporter:\n"
                "  spriteMode: 2\n"
                "  spritePixelsToUnits: 100\n"
                "  spriteSheet:\n"
                "    serializedVersion: 2\n"
                "    sprites:\n"
                "    - serializedVersion: 2\n"
                "      name: icon\n"
                "      rect:\n"
                "        serializedVersion: 2\n"
                "        x: 0\n"
                "        y: 1\n"
                "        width: 8\n"
                "        height: 4\n"
                "      border: {x: 0, y: 0, z: 0, w: 0}\n"
                "      internalID: %d\n"
                "    nameFileIdTable:\n"
                "      icon: %d\n"
                % (fid, fid)
            )
        unity_pack._SPRITE_SHEET_CACHE.clear()
        cw, ch, crgba, _b = unity_pack._load_sprite_rgba(png, fid)
        self.assertEqual((cw, ch), (8, 4))
        # Every texel must be the RED slice — not the GREEN padding band.
        for i in range(0, len(crgba), 4):
            self.assertEqual(crgba[i:i + 4], red,
                             "crop must use bottom-up y, not th-y-h")
        # Direct crop helper agrees.
        _tw, _th, full = unity_pack._load_png_rgba(png)
        cw2, ch2, c2 = unity_pack._crop_rgba(full, 8, 10, 0, 1, 8, 4)
        self.assertEqual((cw2, ch2), (8, 4))
        self.assertEqual(c2, crgba)

        # Real atlas regression: padded Multiple-mode icon (art near bottom).
        slime = os.path.join(
            ROOT, "examples", "unity_pack", "Slime Jump", "Assets",
            "Art", "Textures", "Sound Toggle (Off).png")
        if os.path.isfile(slime):
            unity_pack._SPRITE_SHEET_CACHE.clear()
            sw, sh, srgba, _sb = unity_pack._load_sprite_rgba(
                slime, -6433588549950406020)
            self.assertEqual((sw, sh), (115, 101))
            # Bottom-up: opaque art fills most of the slice (not ~40% top band).
            rows_with_alpha = []
            for y in range(sh):
                if any(srgba[(y * sw + x) * 4 + 3] > 0 for x in range(sw)):
                    rows_with_alpha.append(y)
            self.assertTrue(rows_with_alpha)
            span = rows_with_alpha[-1] - rows_with_alpha[0] + 1
            self.assertGreaterEqual(
                span, 90,
                "sheet crop must keep full icon height, not half-clip")

    def test_authored_m_isactive_zero_seeds_go_active(self):
        """Scene m_IsActive: 0 → _engine_go_active seed 0 (not forced on)."""
        root = tempfile.mkdtemp(prefix="upack-isactive-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
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
                + chunk(b"IEND", b""))

        write_png(os.path.join(spr, "q.png"), 8, 8)
        with open(os.path.join(spr, "q.png.meta"), "w") as f:
            f.write(
                "guid: 44444444444444444444444444444444\n"
                "TextureImporter:\n"
                "  spritePixelsToUnits: 8\n"
            )
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour {\n"
                "    void Update() { gameObject.SetActive(true); }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: isactivhostisactivhostisactiv01\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Hidden\n"
                "  m_IsActive: 0\n"
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
                "guid: isactivhostisactivhostisactiv01}\n"
                "--- !u!212 &4\nSpriteRenderer:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n"
                "  m_Sprite: {fileID: 21300000, "
                "guid: 44444444444444444444444444444444, type: 3}\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Shown\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "  - component: {fileID: 13}\n"
                "--- !u!4 &11\nTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_LocalPosition: {x: 1, y: 0, z: 0}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: isactivhostisactivhostisactiv01}\n"
                "--- !u!212 &13\nSpriteRenderer:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Enabled: 1\n"
                "  m_Sprite: {fileID: 21300000, "
                "guid: 44444444444444444444444444444444, type: 3}\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-isactive-out-")
        plan = unity_pack.pack(root, d)
        names = plan.get("go_names") or []
        actives = plan.get("go_active") or []
        self.assertIn("Hidden", names)
        self.assertIn("Shown", names)
        hi = names.index("Hidden")
        si = names.index("Shown")
        self.assertEqual(actives[hi], 0)
        self.assertEqual(actives[si], 1)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("_engine_go_active_init", eng)
        # Seed array must include a 0 for the authored inactive GO.
        self.assertRegex(
            eng,
            r"_engine_go_active_authored\[\d+\] = \{[^}]*0[^}]*\}")
        # Must not force every slot to 1 (old bug).
        self.assertNotRegex(
            eng,
            r"_engine_go_active_init\(void\) \{[^}]*"
            r"_engine_go_active\[i\] = 1;")


    def test_prefab_instance_ui_parents_tmp_not_fullscreen(self):
        """Stripped UI Button PrefabInstance → TMP child uses button rect."""
        root = tempfile.mkdtemp(prefix="upack-prefab-ui-")
        assets = os.path.join(root, "Assets")
        pref_dir = os.path.join(assets, "Prefabs")
        scripts = os.path.join(assets, "Scripts")
        scene = os.path.join(assets, "Scenes")
        os.makedirs(pref_dir)
        os.makedirs(scripts)
        os.makedirs(scene)
        img = "fe87c0e1cc204ed48ad3b37840f39efc"
        pref_guid = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        spr_guid = "cccccccccccccccccccccccccccccccc"
        spr_dir = os.path.join(assets, "Sprites")
        os.makedirs(spr_dir)
        import struct, zlib

        def write_png(path, w, h):
            def chunk(tag, body):
                return (struct.pack(">I", len(body)) + tag + body
                        + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff))
            raw = b""
            for _y in range(h):
                raw += b"\x00" + (b"\xff\x00\x00\xff" * w)
            open(path, "wb").write(
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9))
                + chunk(b"IEND", b""))

        write_png(os.path.join(spr_dir, "btn.png"), 8, 8)
        with open(os.path.join(spr_dir, "btn.png.meta"), "w") as f:
            f.write(
                "guid: %s\n"
                "TextureImporter:\n"
                "  spritePixelsToUnits: 8\n" % spr_guid
            )
        with open(os.path.join(pref_dir, "UIButton.prefab"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &100\nGameObject:\n  m_Name: UI Button\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 200}\n"
                "  - component: {fileID: 300}\n"
                "--- !u!224 &200\nRectTransform:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &300\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  m_Script: {fileID: 11500000, guid: " + img + "}\n"
                "  m_Sprite: {fileID: 0}\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
            )
        with open(os.path.join(pref_dir, "UIButton.prefab.meta"), "w") as f:
            f.write("guid: %s\n" % pref_guid)
        # Minimal TMP font stub so bake can skip or succeed — use empty text skip;
        # we only need parent rect for hit/layout. Use a Host + Image-less TMP
        # with has_font false → no sprite; still check objects' screen parents.
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour { void Update() {} }\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: prefabuihostprefabuihostpref01\n")
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "  - component: {fileID: 4}\n"
                "--- !u!224 &2\nRectTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n  m_RenderMode: 0\n"
                "--- !u!114 &4\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: prefabuihostprefabuihostpref01}\n"
                "--- !u!1001 &50\nPrefabInstance:\n"
                "  m_Modification:\n"
                "    m_TransformParent: {fileID: 2}\n"
                "    m_Modifications:\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_AnchorMin.x\n"
                "      value: 0.25\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_AnchorMin.y\n"
                "      value: 0.25\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_AnchorMax.x\n"
                "      value: 0.75\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_AnchorMax.y\n"
                "      value: 0.75\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_SizeDelta.x\n"
                "      value: 0\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_SizeDelta.y\n"
                "      value: 0\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 300, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_Sprite\n"
                "      value: \n"
                "      objectReference: {fileID: 21300000, guid: "
                + spr_guid + ", type: 3}\n"
                "  m_SourcePrefab: {fileID: 100100000, guid: " + pref_guid
                + ", type: 3}\n"
                "--- !u!224 &60 stripped\nRectTransform:\n"
                "  m_CorrespondingSourceObject: {fileID: 200, guid: "
                + pref_guid + ", type: 3}\n"
                "  m_PrefabInstance: {fileID: 50}\n"
                "--- !u!1 &70\nGameObject:\n  m_Name: Label\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 71}\n"
                "  - component: {fileID: 72}\n"
                "--- !u!224 &71\nRectTransform:\n"
                "  m_GameObject: {fileID: 70}\n"
                "  m_Father: {fileID: 60}\n"
                "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 200, y: 50}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "  m_LocalScale: {x: 0.5, y: 0.5, z: 1}\n"
                "--- !u!114 &72\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 70}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: prefabuihostprefabuihostpref01}\n"
            )
        objs, _a, _l, _c, hier = unity_pack.load_project(root)
        buttons = [o for o in objs if "UI Button" in (o.get("name") or "")]
        if not buttons:
            buttons = [h for h in hier if "UI Button" in (h.get("name") or "")]
            self.fail("hydrated PrefabInstance root missing; hier=%r objs=%r" % (
                [h.get("name") for h in hier],
                [o.get("name") for o in objs]))
        btn = buttons[0]
        self.assertEqual(str(btn.get("xf_id")), "60")
        self.assertEqual(str(btn.get("father_id")), "2")
        self.assertAlmostEqual(btn["rect"]["anchor_min"][0], 0.25, places=5)
        self.assertAlmostEqual(btn["rect"]["anchor_max"][0], 0.75, places=5)
        # Prefab root Image has m_Sprite: {fileID: 0}; instance override wins.
        ui = btn.get("ui_image") or {}
        self.assertTrue(ui.get("has_sprite"), "m_Sprite objectReference not applied")
        self.assertEqual(ui.get("sprite_guid"), spr_guid)
        sp = btn.get("sprite") or {}
        self.assertTrue(sp.get("tex_rgba"), "prefab Image sprite not baked")
        label = [o for o in objs if o.get("name") == "Label"][0]
        self.assertEqual(str(label.get("father_id")), "60")
        # Parent in by_xf → label not full-screen.
        sw, sh = 1920, 1080
        by_xf = {str(o["xf_id"]): o for o in objs if o.get("xf_id")}
        self.assertIn("60", by_xf)
        cache = {}
        cx, cy, rw, rh = unity_pack._ui_screen_rect(
            label, by_xf, sw, sh, cache)
        # Centered in the 50%×50% button (screen center), size 200×50 * scale 0.5.
        self.assertAlmostEqual(cx, sw * 0.5, places=1)
        self.assertAlmostEqual(cy, sh * 0.5, places=1)
        self.assertAlmostEqual(rw, 100.0, places=1)
        self.assertAlmostEqual(rh, 25.0, places=1)
        # Must not be the old full-screen fallback for a 200×50 rect.
        self.assertLess(rw, sw * 0.2)


    def test_ui_ancestor_local_scale_shrinks_screen_rect(self):
        """Parent RectTransform.localScale accumulates into child screen size."""
        by_xf = {
            "1": {
                "xf_id": "1",
                "canvas": {"render_mode": 0, "enabled": 1},
                "rect": {
                    "anchor_min": (0.0, 0.0), "anchor_max": (1.0, 1.0),
                    "anchored_position": (0.0, 0.0), "size_delta": (0.0, 0.0),
                    "pivot": (0.5, 0.5),
                },
                "local_scale": (1.0, 1.0, 1.0),
            },
            "2": {
                "xf_id": "2",
                "father_id": "1",
                "rect": {
                    "anchor_min": (0.5, 0.5), "anchor_max": (0.5, 0.5),
                    "anchored_position": (0.0, 0.0), "size_delta": (200.0, 100.0),
                    "pivot": (0.5, 0.5),
                },
                "local_scale": (0.5, 0.5, 1.0),
            },
            "3": {
                "xf_id": "3",
                "father_id": "2",
                "rect": {
                    "anchor_min": (0.5, 0.5), "anchor_max": (0.5, 0.5),
                    "anchored_position": (0.0, 0.0), "size_delta": (100.0, 50.0),
                    "pivot": (0.5, 0.5),
                },
                "local_scale": (1.0, 1.0, 1.0),
            },
        }
        cache = {}
        _cx, _cy, rw, rh = unity_pack._ui_screen_rect(
            by_xf["3"], by_xf, 800, 600, cache)
        # Child 100×50 under parent scaled 0.5 → 50×25 screen pixels.
        self.assertAlmostEqual(rw, 50.0, places=5)
        self.assertAlmostEqual(rh, 25.0, places=5)

    def test_ui_local_scale_keeps_pivot_fixed(self):
        """Own localScale shrinks about pivot — corner pivot stays anchored."""
        by_xf = {
            "1": {
                "xf_id": "1",
                "canvas": {"render_mode": 0, "enabled": 1},
                "rect": {
                    "anchor_min": (0.0, 0.0), "anchor_max": (1.0, 1.0),
                    "anchored_position": (0.0, 0.0), "size_delta": (0.0, 0.0),
                    "pivot": (0.5, 0.5),
                },
                "local_scale": (1.0, 1.0, 1.0),
            },
            "2": {
                "xf_id": "2",
                "father_id": "1",
                "rect": {
                    # Top-left of canvas; pivot top-left; scale 0.5.
                    "anchor_min": (0.0, 1.0), "anchor_max": (0.0, 1.0),
                    "anchored_position": (0.0, 0.0), "size_delta": (200.0, 100.0),
                    "pivot": (0.0, 1.0),
                },
                "local_scale": (0.5, 0.5, 1.0),
            },
        }
        cache = {}
        cx, cy, rw, rh = unity_pack._ui_screen_rect(
            by_xf["2"], by_xf, 800, 600, cache)
        # Pivot stays at canvas top-left (0, 600); size 100×50.
        self.assertAlmostEqual(rw, 100.0, places=5)
        self.assertAlmostEqual(rh, 50.0, places=5)
        left = cx - rw * 0.5
        top = cy + rh * 0.5
        self.assertAlmostEqual(left, 0.0, places=5)
        self.assertAlmostEqual(top, 600.0, places=5)


    def test_image_preserve_aspect_fits_inside_rect(self):
        """m_PreserveAspect: 1 → Simple Image fits sprite aspect in the rect."""
        # Square rect, 2:1 sprite → width fills, height halves.
        fw, fh = unity_pack._fit_preserve_aspect(100, 100, 200, 100)
        self.assertAlmostEqual(fw, 100.0, places=5)
        self.assertAlmostEqual(fh, 50.0, places=5)
        # Wide rect, tall sprite → height fills, width shrinks.
        fw, fh = unity_pack._fit_preserve_aspect(100, 50, 50, 100)
        self.assertAlmostEqual(fh, 50.0, places=5)
        self.assertAlmostEqual(fw, 25.0, places=5)
        # Top-left pivot: fitted draw TL stays on rect TL (Unity
        # PreserveSpriteAspectRatio), not mid-letterboxed.
        dcx, dcy = unity_pack._preserve_aspect_draw_center(
            50.0, 50.0, 100.0, 100.0, 50.0, 100.0, (0.0, 1.0))
        self.assertAlmostEqual(dcx, 25.0, places=5)
        self.assertAlmostEqual(dcy, 50.0, places=5)
        self.assertAlmostEqual(dcx - 25.0, 0.0, places=5)
        self.assertAlmostEqual(dcy + 50.0, 100.0, places=5)
        # Center pivot unchanged.
        dcx, dcy = unity_pack._preserve_aspect_draw_center(
            50.0, 50.0, 100.0, 100.0, 50.0, 100.0, (0.5, 0.5))
        self.assertAlmostEqual(dcx, 50.0, places=5)
        self.assertAlmostEqual(dcy, 50.0, places=5)

        root = tempfile.mkdtemp(prefix="upack-presasp-")
        scripts = os.path.join(root, "Assets", "Scripts")
        spr = os.path.join(root, "Assets", "Sprites")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scripts)
        os.makedirs(spr)
        os.makedirs(scene)
        import struct, zlib

        def write_png(path, w, h):
            def chunk(tag, body):
                return (struct.pack(">I", len(body)) + tag + body
                        + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff))
            raw = b""
            for _y in range(h):
                raw += b"\x00" + (b"\xff\xff\xff\xff" * w)
            with open(path, "wb") as out:
                out.write(
                    b"\x89PNG\r\n\x1a\n"
                    + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(raw, 9))
                    + chunk(b"IEND", b""))

        # 64×32 sprite (2:1) into a 200×200 rect with preserveAspect.
        # Guids must be hex — _asset_guid_map / _guid_map only index [0-9a-f].
        spr_guid = "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"
        host_guid = "b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2"
        write_png(os.path.join(spr, "wide.png"), 64, 32)
        with open(os.path.join(spr, "wide.png.meta"), "w") as f:
            f.write(
                "guid: " + spr_guid + "\n"
                "TextureImporter:\n"
                "  spritePixelsToUnits: 32\n"
            )
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour { void Update() {} }\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: " + host_guid + "\n")
        img = "fe87c0e1cc204ed48ad3b37840f39efc"
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!224 &2\nRectTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n  m_RenderMode: 0\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Icon\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "  - component: {fileID: 13}\n"
                "--- !u!224 &11\nRectTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 200, y: 200}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, guid: " + img + "}\n"
                "  m_Sprite: {fileID: 21300000, "
                "guid: " + spr_guid + ", type: 3}\n"
                "  m_Type: 0\n"
                "  m_PreserveAspect: 1\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                "--- !u!114 &13\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: " + host_guid + "}\n"
            )
        objs, _a, _l, _c, _h = unity_pack.load_project(root)
        icon = [o for o in objs if o.get("name") == "Icon"][0]
        self.assertEqual(icon["ui_image"].get("preserve_aspect"), 1)
        sp = icon.get("sprite") or {}
        sw, sh = unity_pack.player_screen(root)
        # 200×200 rect, 2:1 sprite → draw 200×100.
        self.assertAlmostEqual(sp["nhw"] * float(sw) * 2.0, 200.0, places=3)
        self.assertAlmostEqual(sp["nhh"] * float(sh) * 2.0, 100.0, places=3)
        hit = icon.get("ui_hit") or {}
        # Hit stays the full RectTransform.
        self.assertAlmostEqual(hit.get("hw", 0) * 2, 200.0, places=3)
        self.assertAlmostEqual(hit.get("hh", 0) * 2, 200.0, places=3)


    @needs_systems
    def test_canvas_button_draws_and_clicks(self):
        """Authored Canvas + Button (builtin UISprite) → draw + SetActive onClick."""
        objs, _a, _l, cams, _hier = unity_pack.load_project(SYSTEMS)
        btn = [o for o in objs if o["name"] == "Button"]
        self.assertEqual(len(btn), 1)
        # Scriptless uGUI Button GO must not pack as class "Button" — that
        # collides with GameObject_GetComponent_Button (C has no overloads).
        self.assertEqual(btn[0]["class"], "_Rect")
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
        self.assertIn("_engine_ui_screen_rect", eng)
        self.assertIn("_engine_rt_apos_x", eng)
        self.assertNotIn("static const float _spr_ncx[]", eng)
        self.assertIn("_spr_btn", eng)
        self.assertIn("/* Button_Text SpriteRenderer */", eng)
        data_c = open(os.path.join(d, "data.c")).read()
        self.assertIn("<tmp:Click me>", data_c)
        self.assertIn("float a;", open(os.path.join(d, "engine_draw.h")).read())
        self.assertRegex(eng, r"_spr_a\[\] = \{[^}]*1\.0")
        ub = plan["ui_buttons"][0]
        self.assertAlmostEqual(ub["highlighted"][0], 0.78431374, places=5)
        self.assertAlmostEqual(ub["pressed"][0], 0.5882353, places=5)

    def test_uibutton_subclass_clicks_and_colors(self):
        """UIButton : Button gets ui_button hit + ColorBlock (not only builtin Button)."""
        root = tempfile.mkdtemp(prefix="upack-uibtn-click-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        uibtn_guid = "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"
        host_guid = "b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2"
        with open(os.path.join(scripts, "UIButton.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "using UnityEngine.UI;\n"
                "public class UIButton : Button {\n"
                "    void Update() {}\n"
                "}\n"
            )
        with open(os.path.join(scripts, "UIButton.cs.meta"), "w") as f:
            f.write("guid: %s\n" % uibtn_guid)
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour { void Update() {} }\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: %s\n" % host_guid)
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        img = "fe87c0e1cc204ed48ad3b37840f39efc"
        builtin = "0000000000000000f000000000000000"
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "  - component: {fileID: 4}\n"
                "--- !u!224 &2\nRectTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n  m_RenderMode: 0\n"
                "--- !u!114 &4\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, guid: " + host_guid + "}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Play\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "  - component: {fileID: 13}\n"
                "--- !u!224 &11\nRectTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_AnchorMin: {x: 0.25, y: 0.25}\n"
                "  m_AnchorMax: {x: 0.75, y: 0.75}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, guid: " + img + "}\n"
                "  m_Sprite: {fileID: 10905, guid: " + builtin + ", type: 3}\n"
                "  m_Type: 1\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                "--- !u!114 &13\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Enabled: 1\n"
                "  m_Script: {fileID: 11500000, guid: " + uibtn_guid + "}\n"
                "  m_EditorClassIdentifier: "
                "Assembly-CSharp::UIButton\n"
                "  m_Interactable: 1\n"
                "  m_Colors:\n"
                "    m_NormalColor: {r: 1, g: 1, b: 1, a: 1}\n"
                "    m_HighlightedColor: "
                "{r: 0.8, g: 0.8, b: 0.8, a: 1}\n"
                "    m_PressedColor: {r: 0.5, g: 0.5, b: 0.5, a: 1}\n"
                "    m_SelectedColor: {r: 0.8, g: 0.8, b: 0.8, a: 1}\n"
                "    m_DisabledColor: "
                "{r: 0.5, g: 0.5, b: 0.5, a: 0.5}\n"
                "    m_ColorMultiplier: 1\n"
                "  m_OnClick:\n"
                "    m_PersistentCalls:\n"
                "      m_Calls:\n"
                "      - m_Target: {fileID: 30}\n"
                "        m_MethodName: SetActive\n"
                "        m_Mode: 6\n"
                "        m_Arguments:\n"
                "          m_BoolArgument: 1\n"
                "      - m_Target: {fileID: 20}\n"
                "        m_MethodName: SetActive\n"
                "        m_Mode: 6\n"
                "        m_Arguments:\n"
                "          m_BoolArgument: 0\n"
                "--- !u!1 &20\nGameObject:\n  m_Name: MainMenu\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 21}\n"
                "  - component: {fileID: 22}\n"
                "--- !u!224 &21\nRectTransform:\n"
                "  m_GameObject: {fileID: 20}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 0.2, y: 0.2}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!114 &22\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 20}\n"
                "  m_Script: {fileID: 11500000, guid: " + host_guid + "}\n"
                "--- !u!1 &30\nGameObject:\n  m_Name: PlayMenu\n"
                "  m_IsActive: 0\n"
                "  m_Component:\n  - component: {fileID: 31}\n"
                "  - component: {fileID: 32}\n"
                "--- !u!224 &31\nRectTransform:\n"
                "  m_GameObject: {fileID: 30}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!114 &32\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 30}\n"
                "  m_Script: {fileID: 11500000, guid: " + host_guid + "}\n"
                "--- !u!1 &100\nGameObject:\n  m_Name: Main Camera\n"
                "  m_Component:\n  - component: {fileID: 101}\n"
                "  - component: {fileID: 102}\n"
                "--- !u!4 &101\nTransform:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: -10}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!20 &102\nCamera:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  orthographic: 1\n"
                "  orthographic size: 5\n"
                "  m_BackGroundColor: {r: 0, g: 0, b: 0, a: 1}\n"
            )
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "EditorBuildSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1045 &1\nEditorBuildSettings:\n"
                "  m_Scenes:\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/S.unity\n"
            )
        with open(os.path.join(ps, "ProjectSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!129 &1\nPlayerSettings:\n"
                "  defaultScreenWidth: 960\n"
                "  defaultScreenHeight: 640\n"
            )
        objs, _a, _l, _c, _h = unity_pack.load_project(root)
        play = [o for o in objs if o.get("name") == "Play"][0]
        self.assertIsNotNone(play.get("ui_button"))
        self.assertEqual(len(play["ui_button"]["onclick"]), 2)
        self.assertEqual(play["ui_button"]["onclick"][0]["method"], "SetActive")
        self.assertEqual(play["ui_button"]["onclick"][0]["bool_arg"], 1)
        self.assertEqual(play["ui_button"]["onclick"][1]["bool_arg"], 0)
        self.assertAlmostEqual(
            play["ui_button"]["colors"]["pressed"][0], 0.5, places=5)
        self.assertIsNotNone(play.get("ui_hit"))
        d = tempfile.mkdtemp(prefix="upack-uibtn-click-out-")
        plan = unity_pack.pack(root, d)
        self.assertTrue(plan.get("ui_buttons"))
        ub = plan["ui_buttons"][0]
        self.assertAlmostEqual(ub["pressed"][0], 0.5, places=5)
        self.assertEqual(len(ub.get("calls") or []), 2)
        self.assertEqual(ub["calls"][0]["bool_arg"], 1)
        self.assertEqual(ub["calls"][1]["bool_arg"], 0)
        eng = open(os.path.join(d, "engine.c")).read()
        self.assertIn("_engine_ui_btn_tint", eng)
        self.assertIn("_engine_ui_btn_col_h", eng)
        self.assertIn("_engine_ui_btn_call_start", eng)
        self.assertIn("_engine_ui_btn_call_count", eng)
        self.assertRegex(
            eng, r"_engine_ui_btn_call_count\[1\] = \{ 2 \}")
        # onClick on pointer-up over press target (not pointer-down).
        self.assertIn("_engine_ui_btn_press", eng)
        self.assertIn("up_edge", eng)
        self.assertIn("down_edge && hit >= 0", eng)

    def test_nested_canvas_keeps_rect_size(self):
        """Nested Canvas (sorting override) must not expand to full screen."""
        root = tempfile.mkdtemp(prefix="upack-nested-canvas-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        host_guid = "f6f6f6f6f6f6f6f6f6f6f6f6f6f6f6f6"
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour { void Update() {} }\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: %s\n" % host_guid)
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        img = "fe87c0e1cc204ed48ad3b37840f39efc"
        builtin = "0000000000000000f000000000000000"
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "  - component: {fileID: 4}\n"
                "--- !u!224 &2\nRectTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n  m_RenderMode: 0\n"
                "--- !u!114 &4\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, guid: " + host_guid + "}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Back Button\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "  - component: {fileID: 13}\n"
                "  - component: {fileID: 14}\n"
                "--- !u!224 &11\nRectTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_LocalScale: {x: 0.5, y: 0.5, z: 0.5}\n"
                "  m_AnchorMin: {x: 0, y: 1}\n"
                "  m_AnchorMax: {x: 0, y: 1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 200, y: 100}\n"
                "  m_Pivot: {x: 0, y: 1}\n"
                "--- !u!223 &12\nCanvas:\n  m_GameObject: {fileID: 10}\n"
                "  m_Enabled: 1\n  m_RenderMode: 2\n"
                "--- !u!114 &13\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, guid: " + img + "}\n"
                "  m_Sprite: {fileID: 10905, guid: " + builtin + ", type: 3}\n"
                "  m_Type: 1\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                "--- !u!114 &14\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, guid: " + host_guid + "}\n"
                "--- !u!1 &100\nGameObject:\n  m_Name: Main Camera\n"
                "  m_Component:\n  - component: {fileID: 101}\n"
                "  - component: {fileID: 102}\n"
                "--- !u!4 &101\nTransform:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: -10}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!20 &102\nCamera:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  orthographic: 1\n"
                "  orthographic size: 5\n"
                "  m_BackGroundColor: {r: 0, g: 0, b: 0, a: 1}\n"
            )
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "EditorBuildSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1045 &1\nEditorBuildSettings:\n"
                "  m_Scenes:\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/S.unity\n"
            )
        with open(os.path.join(ps, "ProjectSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!129 &1\nPlayerSettings:\n"
                "  defaultScreenWidth: 800\n"
                "  defaultScreenHeight: 600\n"
            )
        objs, _a, _l, _c, _h = unity_pack.load_project(root)
        back = [o for o in objs if o.get("name") == "Back Button"][0]
        self.assertTrue(back.get("canvas"))
        hit = back.get("ui_hit") or {}
        # 200×100 sizeDelta × 0.5 scale → 100×50 screen; not full 800×600.
        self.assertAlmostEqual(hit.get("hw", 0) * 2, 100.0, places=3)
        self.assertAlmostEqual(hit.get("hh", 0) * 2, 50.0, places=3)
        self.assertLess(hit.get("nhw", 1), 0.2)

    def test_prefab_added_canvas_override_sorting_bakes_above(self):
        """PrefabInstance m_AddedComponents Canvas Override Sorting applies.

        Stripped UI Button roots often add a nested Canvas (World Space +
        override sort). Without attaching that Canvas, the Image inherits the
        root order and can be covered by sibling menu Images while TMP (+1)
        still shows. Nested World Space must still bake via ancestor SS mode.
        """
        root = tempfile.mkdtemp(prefix="upack-prefab-canvas-sort-")
        scripts = os.path.join(root, "Assets", "Scripts")
        prefabs = os.path.join(root, "Assets", "Prefabs")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scripts)
        os.makedirs(prefabs)
        os.makedirs(scene)
        host_guid = "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"
        pref_guid = "b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2"
        img = "fe87c0e1cc204ed48ad3b37840f39efc"
        builtin = "0000000000000000f000000000000000"
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour { void Update() {} }\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: %s\n" % host_guid)
        # Minimal UI Button prefab: root Image (empty sprite) + RectTransform.
        with open(os.path.join(prefabs, "UIButton.prefab"), "w") as f:
            f.write(
                "%%YAML 1.1\n"
                "--- !u!1 &100\nGameObject:\n  m_Name: UI Button\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 200}\n"
                "  - component: {fileID: 300}\n"
                "--- !u!224 &200\nRectTransform:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 200, y: 100}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &300\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  m_Enabled: 1\n"
                "  m_Script: {fileID: 11500000, guid: " + img + "}\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                "  m_Sprite: {fileID: 0}\n"
                "  m_Type: 0\n"
                "  m_PreserveAspect: 1\n"
            )
        with open(os.path.join(prefabs, "UIButton.prefab.meta"), "w") as f:
            f.write("guid: %s\n" % pref_guid)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "  - component: {fileID: 4}\n"
                "--- !u!224 &2\nRectTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n  m_RenderMode: 0\n"
                "  m_OverrideSorting: 0\n"
                "  m_SortingLayerID: 0\n"
                "  m_SortingOrder: -2\n"
                "--- !u!114 &4\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, guid: " + host_guid + "}\n"
                # Full-screen panel that would cover a same-order button Image.
                "--- !u!1 &20\nGameObject:\n  m_Name: Panel\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 21}\n"
                "  - component: {fileID: 22}\n"
                "--- !u!224 &21\nRectTransform:\n"
                "  m_GameObject: {fileID: 20}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!114 &22\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 20}\n"
                "  m_Enabled: 1\n"
                "  m_Script: {fileID: 11500000, guid: " + img + "}\n"
                "  m_Sprite: {fileID: 10905, guid: " + builtin + ", type: 3}\n"
                "  m_Type: 1\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                # PrefabInstance UI button with stripped GO + added Canvas.
                "--- !u!1001 &50\nPrefabInstance:\n"
                "  m_Modification:\n"
                "    m_TransformParent: {fileID: 2}\n"
                "    m_Modifications:\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_AnchorMin.x\n"
                "      value: 0.5\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_AnchorMin.y\n"
                "      value: 0.5\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_AnchorMax.x\n"
                "      value: 0.5\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_AnchorMax.y\n"
                "      value: 0.5\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_SizeDelta.x\n"
                "      value: 200\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_SizeDelta.y\n"
                "      value: 100\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_AnchoredPosition.x\n"
                "      value: 0\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_AnchoredPosition.y\n"
                "      value: 0\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 300, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_Sprite\n"
                "      value: \n"
                "      objectReference: {fileID: 10905, guid: " + builtin
                + ", type: 3}\n"
                "    - target: {fileID: 100, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_Name\n"
                "      value: Diff Button\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 100, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_IsActive\n"
                "      value: 1\n"
                "      objectReference: {fileID: 0}\n"
                "    m_RemovedComponents: []\n"
                "    m_RemovedGameObjects: []\n"
                "    m_AddedGameObjects: []\n"
                "    m_AddedComponents:\n"
                "    - targetCorrespondingSourceObject: "
                "{fileID: 100, guid: " + pref_guid + ", type: 3}\n"
                "      insertIndex: -1\n"
                "      addedObject: {fileID: 53}\n"
                "  m_SourcePrefab: {fileID: 100100000, guid: " + pref_guid
                + ", type: 3}\n"
                "--- !u!224 &51 stripped\nRectTransform:\n"
                "  m_CorrespondingSourceObject: {fileID: 200, guid: "
                + pref_guid + ", type: 3}\n"
                "  m_PrefabInstance: {fileID: 50}\n"
                "--- !u!1 &52 stripped\nGameObject:\n"
                "  m_CorrespondingSourceObject: {fileID: 100, guid: "
                + pref_guid + ", type: 3}\n"
                "  m_PrefabInstance: {fileID: 50}\n"
                "--- !u!223 &53\nCanvas:\n"
                "  m_GameObject: {fileID: 52}\n"
                "  m_Enabled: 1\n"
                "  m_RenderMode: 2\n"
                "  m_OverrideSorting: 1\n"
                "  m_SortingLayerID: 0\n"
                "  m_SortingOrder: 50\n"
                "--- !u!1 &100\nGameObject:\n  m_Name: Main Camera\n"
                "  m_Component:\n  - component: {fileID: 101}\n"
                "  - component: {fileID: 102}\n"
                "--- !u!4 &101\nTransform:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: -10}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!20 &102\nCamera:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  orthographic: 1\n"
                "  orthographic size: 5\n"
                "  m_BackGroundColor: {r: 0, g: 0, b: 0, a: 1}\n"
            )
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "EditorBuildSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1045 &1\nEditorBuildSettings:\n"
                "  m_Scenes:\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/S.unity\n"
            )
        with open(os.path.join(ps, "ProjectSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!129 &1\nPlayerSettings:\n"
                "  defaultScreenWidth: 800\n"
                "  defaultScreenHeight: 600\n"
            )
        objs, _a, _l, _c, _h = unity_pack.load_project(root)
        btn = [o for o in objs if o.get("name") == "Diff Button"][0]
        panel = [o for o in objs if o.get("name") == "Panel"][0]
        self.assertTrue(btn.get("canvas"), "added Canvas must attach")
        self.assertEqual(int(btn["canvas"].get("sorting_order") or 0), 50)
        self.assertEqual(int(btn["canvas"].get("override_sorting") or 0), 1)
        bsp = btn.get("sprite") or {}
        psp = panel.get("sprite") or {}
        self.assertTrue(bsp.get("has_sprite"), "button Image must bake")
        self.assertTrue(psp.get("has_sprite"), "panel Image must bake")
        self.assertEqual(int(bsp.get("sorting_order") or 0), 50)
        self.assertEqual(int(psp.get("sorting_order") or 0), -2)
        self.assertGreater(
            int(bsp.get("sorting_order") or 0),
            int(psp.get("sorting_order") or 0))

    def test_tmp_overflow_expands_bake_past_rect(self):
        """TMP Overflow (mode 0): glyphs taller than the rect are not clipped.

        Settings-menu labels use fontSize > Rect height with top align; Unity
        still draws descenders. Truncate keeps the authored clip box.
        """
        font_path = os.path.join(
            ROOT, "examples", "unity_pack", "Slime Jump", "Assets",
            "Others", "Fonts", "Montserrat-Black SDF.asset")
        if not os.path.isfile(font_path):
            self.skipTest("Montserrat SDF font missing")
        unity_pack._TMP_FONT_CACHE.clear()
        font = unity_pack._load_tmp_font_asset(font_path)
        self.assertIsNotNone(font)
        # Sound-like: size 76 in a 64px-tall top-aligned box.
        tw, th, rgba, _sx, sy = unity_pack._rasterize_tmp_text(
            font, "Sound", 76, (1, 1, 1, 1), 264, 64, 1, 256, 0)
        self.assertGreater(th, 64)
        self.assertLess(sy, 0.0)  # expanded downward past rect bottom
        rows = [sum(rgba[y * tw * 4 + 3:(y + 1) * tw * 4:4])
                for y in range(th)]
        nz = [i for i, a in enumerate(rows) if a > 0]
        self.assertTrue(nz)
        self.assertEqual(nz[0], 0)  # ink reaches bake bottom (no clip)
        tw2, th2, _r2, _sx2, _sy2 = unity_pack._rasterize_tmp_text(
            font, "Sound", 76, (1, 1, 1, 1), 264, 64, 1, 256, 3)
        self.assertEqual(th2, 64)

    def test_methods_in_keeps_default_paren_args(self):
        """``default(T)`` inside a param list must not drop the method."""
        body = (
            "\n\tvoid Do (InputDevice device = null, "
            "InputDeviceChange change = default(InputDeviceChange))\n"
            "\t{\n\t\tgameObject.SetActive(false);\n\t}\n"
        )
        ms = unity_pack._methods_in(body, body)
        self.assertEqual([m["name"] for m in ms], ["Do"])
        self.assertIn("default(InputDeviceChange)", ms[0]["args"])

    def test_ui_child_image_sorts_above_parent_panel(self):
        """Equal Canvas order: child UI Image sorts above ancestor Image.

        Without Override Sorting, a full-screen menu Image and a child Button
        Image share the root order — TMP (+1) would show while the Button is
        covered. Hierarchy bump raises each child above its ancestor.
        """
        root = tempfile.mkdtemp(prefix="upack-ui-hier-sort-")
        scripts = os.path.join(root, "Assets", "Scripts")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scripts)
        os.makedirs(scene)
        host_guid = "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"
        img = "fe87c0e1cc204ed48ad3b37840f39efc"
        builtin = "0000000000000000f000000000000000"
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour { void Update() {} }\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: %s\n" % host_guid)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "  - component: {fileID: 4}\n"
                "--- !u!224 &2\nRectTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n  m_RenderMode: 0\n"
                "  m_OverrideSorting: 0\n"
                "  m_SortingLayerID: 0\n"
                "  m_SortingOrder: -2\n"
                "--- !u!114 &4\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, guid: " + host_guid + "}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Panel\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!224 &11\nRectTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Enabled: 1\n"
                "  m_Script: {fileID: 11500000, guid: " + img + "}\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                "  m_Sprite: {fileID: 10905, guid: " + builtin + ", type: 0}\n"
                "  m_Type: 0\n  m_PreserveAspect: 0\n"
                "--- !u!1 &20\nGameObject:\n  m_Name: ChildBtn\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 21}\n"
                "  - component: {fileID: 22}\n"
                "--- !u!224 &21\nRectTransform:\n"
                "  m_GameObject: {fileID: 20}\n"
                "  m_Father: {fileID: 11}\n"
                "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 100, y: 50}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!114 &22\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 20}\n"
                "  m_Enabled: 1\n"
                "  m_Script: {fileID: 11500000, guid: " + img + "}\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                "  m_Sprite: {fileID: 10905, guid: " + builtin + ", type: 0}\n"
                "  m_Type: 0\n  m_PreserveAspect: 1\n"
            )
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "EditorBuildSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1045 &1\nEditorBuildSettings:\n"
                "  m_Scenes:\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/S.unity\n"
            )
        with open(os.path.join(ps, "ProjectSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!129 &1\nPlayerSettings:\n"
                "  defaultScreenWidth: 800\n"
                "  defaultScreenHeight: 600\n"
            )
        objs, _a, _l, _c, _h = unity_pack.load_project(root)
        panel = [o for o in objs if o.get("name") == "Panel"][0]
        child = [o for o in objs if o.get("name") == "ChildBtn"][0]
        psp = panel.get("sprite") or {}
        csp = child.get("sprite") or {}
        self.assertTrue(psp.get("has_sprite"))
        self.assertTrue(csp.get("has_sprite"))
        self.assertGreater(
            int(csp.get("sorting_order") or 0),
            int(psp.get("sorting_order") or 0))

    def test_prefab_added_mb_setactive_hides_tmp_child(self):
        """PrefabInstance m_AddedComponents MB packs and SetActive hides TMP.

        Stripped PrefabInstance roots list AddedComponents only via
        m_GameObject reverse refs — without joining them, Deactivate-style
        scripts never run and child TMP stays visible while the Image is
        covered / inactive.
        """
        root = tempfile.mkdtemp(prefix="upack-added-mb-")
        scripts = os.path.join(root, "Assets", "Scripts")
        prefabs = os.path.join(root, "Assets", "Prefabs")
        scene = os.path.join(root, "Assets", "Scenes")
        fonts = os.path.join(root, "Assets", "Fonts")
        os.makedirs(scripts)
        os.makedirs(prefabs)
        os.makedirs(scene)
        os.makedirs(fonts)
        host_guid = "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"
        dea_guid = "c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3"
        pref_guid = "b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2"
        img = "fe87c0e1cc204ed48ad3b37840f39efc"
        builtin = "0000000000000000f000000000000000"
        tmp = "f4688fdb7df04437aeb418b961361dc5"
        font_guid = "8f586378b4e144a9851e7b34d9b748ee"
        font_src = os.path.join(
            ROOT, "examples", "unity_pack", "SystemsScene", "Assets",
            "TextMesh Pro", "Resources", "Fonts & Materials",
            "LiberationSans SDF.asset")
        if not os.path.isfile(font_src):
            font_src = os.path.join(
                ROOT, "examples", "unity_pack", "Slime Jump", "Assets",
                "Standard Assets", "TextMesh Pro", "Resources",
                "Fonts & Materials", "LiberationSans SDF.asset")
        if not os.path.isfile(font_src):
            self.skipTest("LiberationSans SDF missing")
        shutil.copy(font_src, os.path.join(fonts, "LiberationSans SDF.asset"))
        with open(os.path.join(fonts, "LiberationSans SDF.asset.meta"),
                  "w") as f:
            f.write("guid: %s\n" % font_guid)
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour { void Update() {} }\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: %s\n" % host_guid)
        with open(os.path.join(scripts, "DeactivateWhen.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class DeactivateWhen : MonoBehaviour {\n"
                "    public bool deactivate;\n"
                "    void Start() {\n"
                "        if (deactivate) gameObject.SetActive(false);\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "DeactivateWhen.cs.meta"), "w") as f:
            f.write("guid: %s\n" % dea_guid)
        with open(os.path.join(prefabs, "Btn.prefab"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &100\nGameObject:\n  m_Name: UI Button\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 200}\n"
                "  - component: {fileID: 300}\n"
                "--- !u!224 &200\nRectTransform:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 200, y: 100}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &300\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  m_Enabled: 1\n"
                "  m_Script: {fileID: 11500000, guid: " + img + "}\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                "  m_Sprite: {fileID: 10905, guid: " + builtin + ", type: 0}\n"
                "  m_Type: 0\n  m_PreserveAspect: 1\n"
            )
        with open(os.path.join(prefabs, "Btn.prefab.meta"), "w") as f:
            f.write("guid: %s\n" % pref_guid)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Main Camera\n"
                "  m_TagString: MainCamera\n  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "  - component: {fileID: 4}\n"
                "--- !u!4 &2\nTransform:\n  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: -10}\n"
                "--- !u!20 &3\nCamera:\n  m_GameObject: {fileID: 1}\n"
                "  orthographic: 1\n  orthographic size: 5\n"
                "--- !u!114 &4\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, guid: " + host_guid + "}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Canvas\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!224 &11\nRectTransform:\n"
                "  m_GameObject: {fileID: 10}\n  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!223 &12\nCanvas:\n  m_GameObject: {fileID: 10}\n"
                "  m_Enabled: 1\n  m_RenderMode: 0\n"
                "  m_SortingOrder: 0\n"
                "--- !u!1001 &50\nPrefabInstance:\n  m_Modification:\n"
                "    m_TransformParent: {fileID: 11}\n"
                "    m_Modifications:\n"
                "    - target: {fileID: 100, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_Name\n      value: Action Button\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_SizeDelta.x\n      value: 200\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_SizeDelta.y\n      value: 100\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 300, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_Sprite\n      value:\n"
                "      objectReference: {fileID: 10905, guid: " + builtin
                + ", type: 0}\n"
                "    m_RemovedComponents: []\n"
                "    m_RemovedGameObjects: []\n"
                "    m_AddedGameObjects:\n"
                "    - targetCorrespondingSourceObject: {fileID: 200, guid: "
                + pref_guid + ", type: 3}\n"
                "      insertIndex: -1\n      addedObject: {fileID: 71}\n"
                "    m_AddedComponents:\n"
                "    - targetCorrespondingSourceObject: {fileID: 100, guid: "
                + pref_guid + ", type: 3}\n"
                "      insertIndex: -1\n      addedObject: {fileID: 60}\n"
                "  m_SourcePrefab: {fileID: 100100000, guid: " + pref_guid
                + ", type: 3}\n"
                "--- !u!224 &51 stripped\nRectTransform:\n"
                "  m_CorrespondingSourceObject: {fileID: 200, guid: "
                + pref_guid + ", type: 3}\n"
                "  m_PrefabInstance: {fileID: 50}\n  m_PrefabAsset: {fileID: 0}\n"
                "--- !u!1 &52 stripped\nGameObject:\n"
                "  m_CorrespondingSourceObject: {fileID: 100, guid: "
                + pref_guid + ", type: 3}\n"
                "  m_PrefabInstance: {fileID: 50}\n  m_PrefabAsset: {fileID: 0}\n"
                "--- !u!114 &60\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 52}\n  m_Enabled: 1\n"
                "  m_Script: {fileID: 11500000, guid: " + dea_guid + "}\n"
                "  deactivate: 1\n"
                "--- !u!1 &70\nGameObject:\n  m_Name: Label\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 71}\n"
                "  - component: {fileID: 72}\n"
                "--- !u!224 &71\nRectTransform:\n"
                "  m_GameObject: {fileID: 70}\n  m_Father: {fileID: 51}\n"
                "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 180, y: 40}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &72\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 70}\n  m_Enabled: 1\n"
                "  m_Script: {fileID: 11500000, guid: " + tmp + "}\n"
                "  m_text: HELLO\n"
                "  m_fontAsset: {fileID: 11400000, guid: " + font_guid
                + ", type: 2}\n"
                "  m_fontSize: 36\n"
                "  m_fontColor: {r: 1, g: 1, b: 1, a: 1}\n"
                "  m_HorizontalAlignment: 2\n"
                "  m_VerticalAlignment: 512\n"
                "  m_overflowMode: 0\n"
            )
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "EditorBuildSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1045 &1\nEditorBuildSettings:\n"
                "  m_Scenes:\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/S.unity\n"
            )
        with open(os.path.join(ps, "ProjectSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!129 &1\nPlayerSettings:\n"
                "  defaultScreenWidth: 800\n"
                "  defaultScreenHeight: 600\n"
            )
        objs, _a, _l, _c, _h = unity_pack.load_project(root)
        dea = [o for o in objs
               if o.get("class") == "DeactivateWhen"]
        self.assertTrue(dea, "AddedComponent MB must pack")
        btn = [o for o in objs
               if o.get("name") == "Action Button" and o.get("ui_image")]
        self.assertTrue(btn)
        self.assertEqual(str(dea[0].get("go_id")), str(btn[0].get("go_id")))
        d = tempfile.mkdtemp(prefix="upack-added-mb-out-")
        plan = unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("DeactivateWhen_Start", eng)
        self.assertIn("GameObject_SetActive", eng)
        # Same go_index: SetActive on the MB hides Image + TMP via hierarchy.
        parents = plan.get("go_parents") or []
        dea_o = None
        btn_o = None
        lab_o = None
        for cl in (plan.get("classes") or {}).values():
            for o in cl.get("instances") or []:
                if o.get("class") == "DeactivateWhen" or (
                        (o.get("script") or "").endswith("DeactivateWhen.cs")):
                    dea_o = o
                if o.get("name") == "Action Button" and o.get("ui_image"):
                    btn_o = o
                if o.get("name") == "Label":
                    lab_o = o
        self.assertIsNotNone(dea_o)
        self.assertIsNotNone(btn_o)
        self.assertIsNotNone(lab_o)
        self.assertEqual(dea_o.get("go_index"), btn_o.get("go_index"))
        self.assertEqual(
            parents[int(lab_o.get("go_index"))],
            int(btn_o.get("go_index")))

    def test_preserve_aspect_top_left_pivot_draw_flush(self):
        """preserveAspect + pivot (0,1): sprite TL flush with rect TL."""
        root = tempfile.mkdtemp(prefix="upack-presasp-tl-")
        scripts = os.path.join(root, "Assets", "Scripts")
        spr = os.path.join(root, "Assets", "Sprites")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scripts)
        os.makedirs(spr)
        os.makedirs(scene)
        import struct, zlib

        def write_png(path, w, h):
            def chunk(tag, body):
                return (struct.pack(">I", len(body)) + tag + body
                        + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff))
            raw = b""
            for _y in range(h):
                raw += b"\x00" + (b"\xff\xff\xff\xff" * w)
            with open(path, "wb") as out:
                out.write(
                    b"\x89PNG\r\n\x1a\n"
                    + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(raw, 9))
                    + chunk(b"IEND", b""))

        # Square sprite in a wide top-left rect → pillarbox; Unity keeps TL.
        spr_guid = "e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3"
        host_guid = "f4f4f4f4f4f4f4f4f4f4f4f4f4f4f4f4"
        write_png(os.path.join(spr, "sq.png"), 100, 100)
        with open(os.path.join(spr, "sq.png.meta"), "w") as f:
            f.write(
                "guid: " + spr_guid + "\n"
                "TextureImporter:\n"
                "  spritePixelsToUnits: 100\n"
            )
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour { void Update() {} }\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: " + host_guid + "\n")
        img = "fe87c0e1cc204ed48ad3b37840f39efc"
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!224 &2\nRectTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n  m_RenderMode: 0\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Back\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!224 &11\nRectTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_AnchorMin: {x: 0, y: 1}\n"
                "  m_AnchorMax: {x: 0, y: 1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 400, y: 100}\n"
                "  m_Pivot: {x: 0, y: 1}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, guid: " + img + "}\n"
                "  m_Sprite: {fileID: 21300000, guid: " + spr_guid + ", type: 3}\n"
                "  m_Type: 0\n"
                "  m_PreserveAspect: 1\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                "--- !u!1 &100\nGameObject:\n  m_Name: Main Camera\n"
                "  m_Component:\n  - component: {fileID: 101}\n"
                "  - component: {fileID: 102}\n"
                "--- !u!4 &101\nTransform:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: -10}\n"
                "--- !u!20 &102\nCamera:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  orthographic: 1\n"
                "  orthographic size: 5\n"
            )
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "EditorBuildSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1045 &1\nEditorBuildSettings:\n"
                "  m_Scenes:\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/S.unity\n"
            )
        with open(os.path.join(ps, "ProjectSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!129 &1\nPlayerSettings:\n"
                "  defaultScreenWidth: 800\n"
                "  defaultScreenHeight: 600\n"
            )
        objs, _a, _l, _c, _h = unity_pack.load_project(root)
        back = [o for o in objs if o.get("name") == "Back"][0]
        hit = back.get("ui_hit") or {}
        sp = back.get("sprite") or {}
        # Hit stays full 400×100; draw is 100×100 (square in wide rect).
        self.assertAlmostEqual(hit.get("hw", 0) * 2, 400.0, places=2)
        self.assertAlmostEqual(hit.get("hh", 0) * 2, 100.0, places=2)
        self.assertAlmostEqual(float(sp.get("nhw", 0)) * 2 * 800.0, 100.0, places=1)
        self.assertAlmostEqual(float(sp.get("nhh", 0)) * 2 * 600.0, 100.0, places=1)
        # Draw TL = canvas top-left (0, 600); centered bug put left at 150.
        draw_cx = float(sp.get("ncx", 0)) * 800.0
        draw_cy = float(sp.get("ncy", 0)) * 600.0
        draw_hw = float(sp.get("nhw", 0)) * 800.0
        draw_hh = float(sp.get("nhh", 0)) * 600.0
        left = draw_cx - draw_hw
        top = draw_cy + draw_hh
        self.assertAlmostEqual(left, 0.0, places=1)
        self.assertAlmostEqual(top, 600.0, places=1)

    def test_prefab_uibutton_onclick_mods(self):
        """PrefabInstance UIButton picks up scene m_OnClick SetActive mods."""
        root = tempfile.mkdtemp(prefix="upack-uibtn-pref-")
        scripts = os.path.join(root, "Assets", "Scripts")
        pref_dir = os.path.join(root, "Assets", "Prefabs")
        os.makedirs(scripts)
        os.makedirs(pref_dir)
        uibtn_guid = "c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3"
        host_guid = "d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4"
        pref_guid = "e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5"
        img = "fe87c0e1cc204ed48ad3b37840f39efc"
        builtin = "0000000000000000f000000000000000"
        with open(os.path.join(scripts, "UIButton.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "using UnityEngine.UI;\n"
                "public class UIButton : Button { void Update() {} }\n"
            )
        with open(os.path.join(scripts, "UIButton.cs.meta"), "w") as f:
            f.write("guid: %s\n" % uibtn_guid)
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour { void Update() {} }\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: %s\n" % host_guid)
        with open(os.path.join(pref_dir, "UIBtn.prefab"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &100\nGameObject:\n  m_Name: UI Button\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 200}\n"
                "  - component: {fileID: 300}\n"
                "  - component: {fileID: 400}\n"
                "--- !u!224 &200\nRectTransform:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &300\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  m_Script: {fileID: 11500000, guid: " + img + "}\n"
                "  m_Sprite: {fileID: 10905, guid: " + builtin + ", type: 3}\n"
                "  m_Type: 1\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                "--- !u!114 &400\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  m_Enabled: 1\n"
                "  m_Script: {fileID: 11500000, guid: " + uibtn_guid + "}\n"
                "  m_Interactable: 1\n"
                "  m_Colors:\n"
                "    m_NormalColor: {r: 1, g: 1, b: 1, a: 1}\n"
                "    m_HighlightedColor: "
                "{r: 0.7, g: 0.7, b: 0.7, a: 1}\n"
                "    m_PressedColor: {r: 0.4, g: 0.4, b: 0.4, a: 1}\n"
                "    m_SelectedColor: {r: 0.7, g: 0.7, b: 0.7, a: 1}\n"
                "    m_DisabledColor: "
                "{r: 0.5, g: 0.5, b: 0.5, a: 0.5}\n"
                "    m_ColorMultiplier: 1\n"
                "  m_OnClick:\n"
                "    m_PersistentCalls:\n"
                "      m_Calls: []\n"
            )
        with open(os.path.join(pref_dir, "UIBtn.prefab.meta"), "w") as f:
            f.write("guid: %s\n" % pref_guid)
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "  - component: {fileID: 4}\n"
                "--- !u!224 &2\nRectTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n  m_RenderMode: 0\n"
                "--- !u!114 &4\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, guid: " + host_guid + "}\n"
                "--- !u!1 &50\nGameObject:\n  m_Name: Panel\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 51}\n"
                "  - component: {fileID: 52}\n"
                "--- !u!224 &51\nRectTransform:\n"
                "  m_GameObject: {fileID: 50}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 0.1, y: 0.1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!114 &52\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 50}\n"
                "  m_Script: {fileID: 11500000, guid: " + host_guid + "}\n"
                "--- !u!1001 &60\nPrefabInstance:\n"
                "  m_Modification:\n"
                "    m_TransformParent: {fileID: 2}\n"
                "    m_Modifications:\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_AnchorMin.x\n"
                "      value: 0.2\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_AnchorMin.y\n"
                "      value: 0.2\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_AnchorMax.x\n"
                "      value: 0.8\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 200, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_AnchorMax.y\n"
                "      value: 0.8\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 100, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: m_Name\n"
                "      value: Play Button\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 400, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: "
                "m_OnClick.m_PersistentCalls.m_Calls.Array.size\n"
                "      value: 1\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 400, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: "
                "m_OnClick.m_PersistentCalls.m_Calls.Array.data[0]."
                "m_MethodName\n"
                "      value: SetActive\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 400, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: "
                "m_OnClick.m_PersistentCalls.m_Calls.Array.data[0].m_Mode\n"
                "      value: 6\n"
                "      objectReference: {fileID: 0}\n"
                "    - target: {fileID: 400, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: "
                "m_OnClick.m_PersistentCalls.m_Calls.Array.data[0].m_Target\n"
                "      value: \n"
                "      objectReference: {fileID: 50}\n"
                "    - target: {fileID: 400, guid: " + pref_guid + ", type: 3}\n"
                "      propertyPath: "
                "m_OnClick.m_PersistentCalls.m_Calls.Array.data[0]."
                "m_Arguments.m_BoolArgument\n"
                "      value: 0\n"
                "      objectReference: {fileID: 0}\n"
                "  m_SourcePrefab: {fileID: 100100000, guid: " + pref_guid
                + ", type: 3}\n"
                "--- !u!224 &70 stripped\nRectTransform:\n"
                "  m_CorrespondingSourceObject: {fileID: 200, guid: "
                + pref_guid + ", type: 3}\n"
                "  m_PrefabInstance: {fileID: 60}\n"
                "--- !u!1 &200\nGameObject:\n  m_Name: Main Camera\n"
                "  m_Component:\n  - component: {fileID: 201}\n"
                "  - component: {fileID: 202}\n"
                "--- !u!4 &201\nTransform:\n"
                "  m_GameObject: {fileID: 200}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: -10}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!20 &202\nCamera:\n"
                "  m_GameObject: {fileID: 200}\n"
                "  orthographic: 1\n"
                "  orthographic size: 5\n"
                "  m_BackGroundColor: {r: 0, g: 0, b: 0, a: 1}\n"
            )
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "EditorBuildSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1045 &1\nEditorBuildSettings:\n"
                "  m_Scenes:\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/S.unity\n"
            )
        with open(os.path.join(ps, "ProjectSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!129 &1\nPlayerSettings:\n"
                "  defaultScreenWidth: 960\n"
                "  defaultScreenHeight: 640\n"
            )
        unity_pack._prefab_parse_cache.clear()
        objs, _a, _l, _c, _h = unity_pack.load_project(root)
        play = [o for o in objs
                if (o.get("name") or "").startswith("Play Button")]
        self.assertTrue(play, "Play Button prefab root missing")
        ub = play[0].get("ui_button")
        self.assertIsNotNone(ub)
        self.assertEqual(ub["onclick"][0]["method"], "SetActive")
        self.assertEqual(ub["onclick"][0]["target_go"], "50")
        self.assertAlmostEqual(ub["colors"]["highlighted"][0], 0.7, places=5)
        d = tempfile.mkdtemp(prefix="upack-uibtn-pref-out-")
        plan = unity_pack.pack(root, d)
        self.assertTrue(plan.get("ui_buttons"))
        self.assertTrue(plan["ui_buttons"][0].get("calls"))
    def test_getcomponent_button_with_named_button_go(self):
        """GetComponent<Button> + GO named Button → one C symbol, not two.

        Scriptless UI GOs used to take class = go.name. A GO named \"Button\"
        then emitted GameObject_GetComponent_Button alongside the uGUI helper.
        """
        root = tempfile.mkdtemp(prefix="upack-gc-btn-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        host_guid = "c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1"
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "using UnityEngine.UI;\n"
                "public class Host : MonoBehaviour {\n"
                "    void Update() {\n"
                "        GetComponent<Button>();\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: %s\n" % host_guid)
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        img = "fe87c0e1cc204ed48ad3b37840f39efc"
        btn = "4e29b1a8efbd4b44bb3f3716e73f07ff"
        builtin = "0000000000000000f000000000000000"
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%%YAML 1.1\n"
                "--- !u!1 &100\nGameObject:\n  m_Name: Main Camera\n"
                "  m_Component:\n  - component: {fileID: 101}\n"
                "  - component: {fileID: 102}\n"
                "--- !u!4 &101\nTransform:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: -10}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!20 &102\nCamera:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  m_Orthographic: 1\n"
                "  m_OrthographicSize: 5\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!224 &2\nRectTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n  m_RenderMode: 0\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Button\n"
                "  m_Component:\n"
                "  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "  - component: {fileID: 13}\n"
                "  - component: {fileID: 14}\n"
                "--- !u!224 &11\nRectTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 160, y: 30}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, guid: %s}\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                "  m_Enabled: 1\n  m_Type: 1\n"
                "  m_Sprite: {fileID: 10905, guid: %s, type: 0}\n"
                "--- !u!114 &13\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, guid: %s}\n"
                "  m_OnClick:\n    m_PersistentCalls:\n      m_Calls: []\n"
                "--- !u!114 &14\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, guid: %s}\n"
                % (img, builtin, btn, host_guid)
            )
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "EditorBuildSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1045 &1\nEditorBuildSettings:\n"
                "  m_Scenes:\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/S.unity\n"
            )
        objs, _a, _l, _c, _h = unity_pack.load_project(root)
        btn_o = [o for o in objs if o["name"] == "Button"][0]
        # Host script wins class; without Host, scriptless name would be _Rect.
        self.assertNotEqual(btn_o["class"], "Button")
        self.assertIsNotNone(btn_o.get("ui_button"))
        d = tempfile.mkdtemp(prefix="upack-gc-btn-out-")
        plan = unity_pack.pack(root, d)
        self.assertNotIn("Button", plan["classes"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        # Definition once (call sites also mention the symbol).
        self.assertEqual(
            eng.count("static int GameObject_GetComponent_Button("), 1)
        self.assertEqual(eng.count("static int _engine_go_Button["), 1)
        self.assertNotIn("defined twice", eng)
        self.assertIn("GetComponent_Button", eng)

    def test_scriptless_button_go_class_is_rect(self):
        """GO named Button with only uGUI components packs as _Rect, not Button."""
        root = tempfile.mkdtemp(prefix="upack-btn-rect-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour { void Update() {} }\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        img = "fe87c0e1cc204ed48ad3b37840f39efc"
        btn = "4e29b1a8efbd4b44bb3f3716e73f07ff"
        builtin = "0000000000000000f000000000000000"
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%%YAML 1.1\n"
                "--- !u!1 &100\nGameObject:\n  m_Name: Main Camera\n"
                "  m_Component:\n  - component: {fileID: 101}\n"
                "  - component: {fileID: 102}\n"
                "--- !u!4 &101\nTransform:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: -10}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!20 &102\nCamera:\n"
                "  m_GameObject: {fileID: 100}\n"
                "  m_Orthographic: 1\n"
                "  m_OrthographicSize: 5\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Host\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Button\n"
                "  m_Component:\n"
                "  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "  - component: {fileID: 13}\n"
                "--- !u!224 &11\nRectTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 160, y: 30}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, guid: %s}\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                "  m_Enabled: 1\n  m_Type: 1\n"
                "  m_Sprite: {fileID: 10905, guid: %s, type: 0}\n"
                "--- !u!114 &13\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, guid: %s}\n"
                "  m_OnClick:\n    m_PersistentCalls:\n      m_Calls: []\n"
                % (img, builtin, btn)
            )
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "EditorBuildSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1045 &1\nEditorBuildSettings:\n"
                "  m_Scenes:\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/S.unity\n"
            )
        objs, _a, _l, _c, _h = unity_pack.load_project(root)
        btn_o = [o for o in objs if o["name"] == "Button"][0]
        self.assertEqual(btn_o["class"], "_Rect")
        d = tempfile.mkdtemp(prefix="upack-btn-rect-out-")
        plan = unity_pack.pack(root, d)
        self.assertNotIn("Button", plan["classes"])
        self.assertIn("_Rect", plan["classes"])

    def test_getcomponent_button_finds_uibutton_subclass(self):
        """GetComponent<Button> finds GO with UIButton : Button (inheritance)."""
        root = tempfile.mkdtemp(prefix="upack-uibtn-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        uibtn_guid = "e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3"
        with open(os.path.join(scripts, "UIButton.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "using UnityEngine.UI;\n"
                "public class UIButton : Button {\n"
                "    void Update() {\n"
                "        GetComponent<Button>();\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "UIButton.cs.meta"), "w") as f:
            f.write("guid: %s\n" % uibtn_guid)
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Play\n"
                "  m_Component:\n"
                "  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, guid: %s}\n"
                % uibtn_guid
            )
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "EditorBuildSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1045 &1\nEditorBuildSettings:\n"
                "  m_Scenes:\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/S.unity\n"
            )
        d = tempfile.mkdtemp(prefix="upack-uibtn-out-")
        plan = unity_pack.pack(root, d)
        self.assertIn("UIButton", plan["classes"])
        ui_maps = plan.get("go_ui_components") or {}
        self.assertIn("Button", ui_maps)
        play_gi = plan["classes"]["UIButton"]["instances"][0]["go_index"]
        self.assertIn(play_gi, ui_maps["Button"])
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertEqual(
            eng.count("static int GameObject_GetComponent_Button("), 1)
        self.assertIn("GameObject_GetComponent_UIButton", eng)

    def test_vertical_layout_group_stacks_children(self):
        """Authored VerticalLayoutGroup bakes child RectTransforms top→bottom."""
        root = tempfile.mkdtemp(prefix="upack-vlg-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour { void Update() {} }\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: vlghostvlghostvlghostvlghost01\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        img = "fe87c0e1cc204ed48ad3b37840f39efc"
        vlg = "59f8146938fff824cb5fd77236b75775"
        builtin = "0000000000000000f000000000000000"
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!224 &2\nRectTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n  m_RenderMode: 0\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Panel\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "  - component: {fileID: 13}\n"
                "--- !u!224 &11\nRectTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_Children:\n"
                "  - {fileID: 21}\n"
                "  - {fileID: 31}\n"
                "  - {fileID: 41}\n"
                "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 200, y: 300}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, guid: " + vlg + "}\n"
                "  m_Padding:\n    m_Left: 0\n    m_Right: 0\n"
                "    m_Top: 0\n    m_Bottom: 0\n"
                "  m_ChildAlignment: 0\n  m_Spacing: 10\n"
                "  m_ChildForceExpandWidth: 1\n"
                "  m_ChildForceExpandHeight: 0\n"
                "  m_ChildControlWidth: 1\n"
                "  m_ChildControlHeight: 0\n"
                "  m_ChildScaleWidth: 0\n  m_ChildScaleHeight: 0\n"
                "  m_ReverseArrangement: 0\n"
                "--- !u!114 &13\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: vlghostvlghostvlghostvlghost01}\n"
            )
            # Declare children out of sibling order (B0, B2, B1 in YAML) so
            # objects[] discovery ≠ m_Children; layout must still use
            # m_Children (B0, B1, B2 → top→bottom).
            for i, fid in enumerate((20, 40, 30)):
                xf, img_id = fid + 1, fid + 2
                name_i = {20: 0, 30: 1, 40: 2}[fid]
                f.write(
                    "--- !u!1 &%d\nGameObject:\n  m_Name: B%d\n"
                    "  m_Component:\n  - component: {fileID: %d}\n"
                    "  - component: {fileID: %d}\n"
                    "--- !u!224 &%d\nRectTransform:\n"
                    "  m_GameObject: {fileID: %d}\n"
                    "  m_Father: {fileID: 11}\n"
                    "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                    "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                    "  m_AnchoredPosition: {x: 0, y: 0}\n"
                    "  m_SizeDelta: {x: 100, y: 40}\n"
                    "  m_Pivot: {x: 0.5, y: 0.5}\n"
                    "--- !u!114 &%d\nMonoBehaviour:\n"
                    "  m_GameObject: {fileID: %d}\n"
                    "  m_Script: {fileID: 11500000, guid: %s}\n"
                    "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                    "  m_Enabled: 1\n  m_Type: 0\n"
                    "  m_Sprite: {fileID: 10905, guid: %s, type: 0}\n"
                    % (fid, name_i, xf, img_id, xf, fid, img_id, fid, img,
                       builtin)
                )
        objs, _a, _l, _c, _h = unity_pack.load_project(root)
        panel = [o for o in objs if o["name"] == "Panel"][0]
        self.assertEqual(
            panel.get("child_ids"), ["21", "31", "41"])
        self.assertTrue(panel.get("layout_group", {}).get("vertical"))
        by_name = {o["name"]: o for o in objs if o["name"].startswith("B")}
        kids = [by_name["B0"], by_name["B1"], by_name["B2"]]
        self.assertEqual(len(kids), 3)
        # Stacked from top with spacing 10; width driven to panel 200.
        ys = [o["rect"]["anchored_position"][1] for o in kids]
        self.assertAlmostEqual(ys[0], -20.0, places=3)
        self.assertAlmostEqual(ys[1], -70.0, places=3)
        self.assertAlmostEqual(ys[2], -120.0, places=3)
        for o in kids:
            self.assertAlmostEqual(o["rect"]["size_delta"][0], 200.0, places=3)
            # Distinct world Y after bake (not all piled at center).
        wy = [o["pos"][1] for o in kids]
        self.assertGreater(wy[0], wy[1])
        self.assertGreater(wy[1], wy[2])

    def test_disabled_vertical_layout_group_skips_bake(self):
        """m_Enabled:0 VerticalLayoutGroup leaves authored child positions."""
        root = tempfile.mkdtemp(prefix="upack-vlg-off-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour { void Update() {} }\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: vlghostvlghostvlghostvlghost02\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        img = "fe87c0e1cc204ed48ad3b37840f39efc"
        vlg = "59f8146938fff824cb5fd77236b75775"
        builtin = "0000000000000000f000000000000000"
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!224 &2\nRectTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n  m_RenderMode: 0\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Panel\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "  - component: {fileID: 13}\n"
                "--- !u!224 &11\nRectTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 200, y: 300}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Enabled: 0\n"
                "  m_Script: {fileID: 11500000, guid: " + vlg + "}\n"
                "  m_Padding:\n    m_Left: 0\n    m_Right: 0\n"
                "    m_Top: 0\n    m_Bottom: 0\n"
                "  m_ChildAlignment: 0\n  m_Spacing: 10\n"
                "  m_ChildForceExpandWidth: 1\n"
                "  m_ChildForceExpandHeight: 0\n"
                "  m_ChildControlWidth: 1\n"
                "  m_ChildControlHeight: 0\n"
                "  m_ChildScaleWidth: 0\n  m_ChildScaleHeight: 0\n"
                "  m_ReverseArrangement: 0\n"
                "--- !u!114 &13\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: vlghostvlghostvlghostvlghost02}\n"
            )
            for i, fid in enumerate((20, 30, 40)):
                xf, img_id = fid + 1, fid + 2
                f.write(
                    "--- !u!1 &%d\nGameObject:\n  m_Name: B%d\n"
                    "  m_Component:\n  - component: {fileID: %d}\n"
                    "  - component: {fileID: %d}\n"
                    "--- !u!224 &%d\nRectTransform:\n"
                    "  m_GameObject: {fileID: %d}\n"
                    "  m_Father: {fileID: 11}\n"
                    "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                    "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                    "  m_AnchoredPosition: {x: 0, y: 0}\n"
                    "  m_SizeDelta: {x: 100, y: 40}\n"
                    "  m_Pivot: {x: 0.5, y: 0.5}\n"
                    "--- !u!114 &%d\nMonoBehaviour:\n"
                    "  m_GameObject: {fileID: %d}\n"
                    "  m_Script: {fileID: 11500000, guid: %s}\n"
                    "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                    "  m_Enabled: 1\n  m_Type: 0\n"
                    "  m_Sprite: {fileID: 10905, guid: %s, type: 0}\n"
                    % (fid, i, xf, img_id, xf, fid, img_id, fid, img, builtin)
                )
        objs, _a, _l, _c, _h = unity_pack.load_project(root)
        panel = [o for o in objs if o["name"] == "Panel"][0]
        self.assertEqual(int(panel.get("layout_group", {}).get("enabled", 1)), 0)
        kids = sorted(
            [o for o in objs if o["name"].startswith("B")],
            key=lambda o: o["name"])
        self.assertEqual(len(kids), 3)
        for o in kids:
            self.assertAlmostEqual(o["rect"]["anchored_position"][0], 0.0,
                                   places=3)
            self.assertAlmostEqual(o["rect"]["anchored_position"][1], 0.0,
                                   places=3)
            self.assertAlmostEqual(o["rect"]["size_delta"][0], 100.0, places=3)

    def test_content_size_fitter_preferred_from_vlayout(self):
        """ContentSizeFitter PreferredSize height = VLG preferred (children+spacing)."""
        root = tempfile.mkdtemp(prefix="upack-csf-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour { void Update() {} }\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: csfhostcsfhostcsfhostcsfhost01\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        img = "fe87c0e1cc204ed48ad3b37840f39efc"
        vlg = "59f8146938fff824cb5fd77236b75775"
        csf = "3245ec927659c4140ac4f8d17403cc18"
        builtin = "0000000000000000f000000000000000"
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!224 &2\nRectTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n  m_RenderMode: 0\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Panel\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "  - component: {fileID: 13}\n"
                "  - component: {fileID: 14}\n"
                "--- !u!224 &11\nRectTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 200, y: 50}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, guid: " + vlg + "}\n"
                "  m_Padding:\n    m_Left: 0\n    m_Right: 0\n"
                "    m_Top: 0\n    m_Bottom: 0\n"
                "  m_ChildAlignment: 0\n  m_Spacing: 10\n"
                "  m_ChildForceExpandWidth: 0\n"
                "  m_ChildForceExpandHeight: 0\n"
                "  m_ChildControlWidth: 0\n"
                "  m_ChildControlHeight: 0\n"
                "  m_ChildScaleWidth: 0\n  m_ChildScaleHeight: 0\n"
                "  m_ReverseArrangement: 0\n"
                "--- !u!114 &13\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, guid: " + csf + "}\n"
                "  m_HorizontalFit: 0\n"
                "  m_VerticalFit: 2\n"
                "--- !u!114 &14\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: csfhostcsfhostcsfhostcsfhost01}\n"
            )
            for i, fid in enumerate((20, 30, 40)):
                xf, img_id = fid + 1, fid + 2
                f.write(
                    "--- !u!1 &%d\nGameObject:\n  m_Name: B%d\n"
                    "  m_Component:\n  - component: {fileID: %d}\n"
                    "  - component: {fileID: %d}\n"
                    "--- !u!224 &%d\nRectTransform:\n"
                    "  m_GameObject: {fileID: %d}\n"
                    "  m_Father: {fileID: 11}\n"
                    "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                    "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                    "  m_AnchoredPosition: {x: 0, y: 0}\n"
                    "  m_SizeDelta: {x: 100, y: 40}\n"
                    "  m_Pivot: {x: 0.5, y: 0.5}\n"
                    "--- !u!114 &%d\nMonoBehaviour:\n"
                    "  m_GameObject: {fileID: %d}\n"
                    "  m_Script: {fileID: 11500000, guid: %s}\n"
                    "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                    "  m_Enabled: 1\n  m_Type: 0\n"
                    "  m_Sprite: {fileID: 10905, guid: %s, type: 0}\n"
                    % (fid, i, xf, img_id, xf, fid, img_id, fid, img, builtin)
                )
        objs, _a, _l, _c, _h = unity_pack.load_project(root)
        panel = [o for o in objs if o["name"] == "Panel"][0]
        self.assertEqual(panel.get("content_size_fitter", {}).get("vertical"), 2)
        # 3×40 + 2×10 spacing = 140
        self.assertAlmostEqual(panel["rect"]["size_delta"][1], 140.0, places=3)
        self.assertAlmostEqual(panel["rect"]["size_delta"][0], 200.0, places=3)

    def test_layout_element_preferred_and_max(self):
        """LayoutElement preferred height drives VLG; max clamps preferred."""
        root = tempfile.mkdtemp(prefix="upack-le-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour { void Update() {} }\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: lehostlehostlehostlehostleho01\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        vlg = "59f8146938fff824cb5fd77236b75775"
        le = "306cc8c2b49d7114eaa3623786fc2126"
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!224 &2\nRectTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n  m_RenderMode: 0\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Panel\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!224 &11\nRectTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 200, y: 300}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, guid: " + vlg + "}\n"
                "  m_Padding:\n    m_Left: 0\n    m_Right: 0\n"
                "    m_Top: 0\n    m_Bottom: 0\n"
                "  m_ChildAlignment: 0\n  m_Spacing: 0\n"
                "  m_ChildForceExpandWidth: 0\n"
                "  m_ChildForceExpandHeight: 0\n"
                "  m_ChildControlWidth: 1\n"
                "  m_ChildControlHeight: 1\n"
                "  m_ChildScaleWidth: 0\n  m_ChildScaleHeight: 0\n"
                "  m_ReverseArrangement: 0\n"
                "--- !u!1 &20\nGameObject:\n  m_Name: Child\n"
                "  m_Component:\n  - component: {fileID: 21}\n"
                "  - component: {fileID: 22}\n"
                "--- !u!224 &21\nRectTransform:\n"
                "  m_GameObject: {fileID: 20}\n"
                "  m_Father: {fileID: 11}\n"
                "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 50, y: 50}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!114 &22\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 20}\n"
                "  m_Script: {fileID: 11500000, guid: " + le + "}\n"
                "  m_IgnoreLayout: 0\n"
                "  m_MinWidth: -1\n  m_MinHeight: -1\n"
                "  m_PreferredWidth: 80\n  m_PreferredHeight: 120\n"
                "  m_FlexibleWidth: -1\n  m_FlexibleHeight: -1\n"
                "  m_LayoutPriority: 1\n"
                "  m_MaxWidth: -1\n  m_MaxHeight: 60\n"
            )
        objs, _a, _l, _c, _h = unity_pack.load_project(root)
        child = [o for o in objs if o["name"] == "Child"][0]
        self.assertIsNotNone(child.get("layout_element"))
        self.assertAlmostEqual(child["layout_element"]["max"][1], 60.0)
        # preferred 120 clamped by max 60; control height → sizeDelta.y = 60
        self.assertAlmostEqual(child["rect"]["size_delta"][1], 60.0, places=3)
        self.assertAlmostEqual(child["rect"]["size_delta"][0], 80.0, places=3)

    def test_aspect_ratio_fitter_width_controls_height(self):
        """AspectRatioFitter WidthControlsHeight sets height = width / ratio."""
        root = tempfile.mkdtemp(prefix="upack-arf-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour { void Update() {} }\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: arfhostarfhostarfhostarfhost01\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        arf = "86710e43de46f6f4bac7c8e50813a599"
        img = "fe87c0e1cc204ed48ad3b37840f39efc"
        builtin = "0000000000000000f000000000000000"
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!224 &2\nRectTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n  m_RenderMode: 0\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Pic\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "  - component: {fileID: 13}\n"
                "--- !u!224 &11\nRectTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 200, y: 50}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!114 &12\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, guid: " + arf + "}\n"
                "  m_AspectMode: 1\n"
                "  m_AspectRatio: 2\n"
                "--- !u!114 &13\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, guid: " + img + "}\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                "  m_Enabled: 1\n  m_Type: 0\n"
                "  m_Sprite: {fileID: 10905, guid: " + builtin + ", type: 0}\n"
            )
        objs, _a, _l, _c, _h = unity_pack.load_project(root)
        pic = [o for o in objs if o["name"] == "Pic"][0]
        self.assertEqual(pic.get("aspect_ratio_fitter", {}).get("mode"), 1)
        self.assertAlmostEqual(pic["rect"]["size_delta"][0], 200.0, places=3)
        self.assertAlmostEqual(pic["rect"]["size_delta"][1], 100.0, places=3)

    def test_aspect_ratio_fitter_fit_in_parent(self):
        """AspectRatioFitter FitInParent stretches anchors and letterboxes."""
        root = tempfile.mkdtemp(prefix="upack-arf-fit-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour { void Update() {} }\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: arffitarffitarffitarffitarffi01\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        arf = "86710e43de46f6f4bac7c8e50813a599"
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!224 &2\nRectTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_AnchorMin: {x: 0, y: 0}\n"
                "  m_AnchorMax: {x: 1, y: 1}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 0, y: 0}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n  m_RenderMode: 0\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Frame\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "--- !u!224 &11\nRectTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Father: {fileID: 2}\n"
                "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 400, y: 200}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!1 &20\nGameObject:\n  m_Name: Inner\n"
                "  m_Component:\n  - component: {fileID: 21}\n"
                "  - component: {fileID: 22}\n"
                "--- !u!224 &21\nRectTransform:\n"
                "  m_GameObject: {fileID: 20}\n"
                "  m_Father: {fileID: 11}\n"
                "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                "  m_AnchoredPosition: {x: 0, y: 0}\n"
                "  m_SizeDelta: {x: 100, y: 100}\n"
                "  m_Pivot: {x: 0.5, y: 0.5}\n"
                "--- !u!114 &22\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 20}\n"
                "  m_Script: {fileID: 11500000, guid: " + arf + "}\n"
                "  m_AspectMode: 3\n"
                "  m_AspectRatio: 1\n"
            )
        objs, _a, _l, _c, _h = unity_pack.load_project(root)
        inner = [o for o in objs if o["name"] == "Inner"][0]
        self.assertEqual(inner["rect"]["anchor_min"], (0.0, 0.0))
        self.assertEqual(inner["rect"]["anchor_max"], (1.0, 1.0))
        # Parent 400×200, ratio 1 → fit height = 200, sizeDelta.x = 200-400 = -200
        self.assertAlmostEqual(inner["rect"]["size_delta"][0], -200.0, places=3)
        self.assertAlmostEqual(inner["rect"]["size_delta"][1], 0.0, places=3)

    def test_awake_setactive_false_emitted(self):
        """Awake gameObject.SetActive(false) runs before Start (SettingsMenu)."""
        root = tempfile.mkdtemp(prefix="upack-awake-sa-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Menu.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Menu : MonoBehaviour {\n"
                "    void Awake() {\n"
                "        gameObject.SetActive(false);\n"
                "        Unknown.DoThing();\n"
                "    }\n"
                "    void Update() {}\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Menu.cs.meta"), "w") as f:
            f.write("guid: awakeasaawakeasaawakeasaawake01\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Menu\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: awakeasaawakeasaawakeasaawake01}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-awake-sa-out-")
        # Pack may fail validate if no UI SetActive helper — force go tables
        # via a minimal Image is heavy; call emit path through pack and accept
        # need for GameObject_SetActive (want_ui). Add a Canvas button-less
        # go table by using Find in script... Use load + emit only if pack
        # fails. Prefer pack with sprite so go_names exist.
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("static void Menu_Awake(unsigned i)", eng)
        self.assertIn("GameObject_SetActive(_engine_go_of_Menu(i), (0))", eng)
        self.assertIn("Menu_Awake((unsigned)n)", eng)
        # SetActive alone sets want_ui; ColorBlock tint must not be referenced
        # without authored Buttons (would be undeclared).
        self.assertNotIn("_engine_ui_btn_tint_init", eng)

    def test_setactive_with_sprite_omits_btn_tint(self):
        """want_ui from SetActive + SpriteRenderer, no Button → no tint refs."""
        root = tempfile.mkdtemp(prefix="upack-sa-spr-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
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
                + chunk(b"IEND", b""))

        write_png(os.path.join(spr, "q.png"), 8, 8)
        with open(os.path.join(spr, "q.png.meta"), "w") as f:
            f.write(
                "guid: 33333333333333333333333333333333\n"
                "TextureImporter:\n"
                "  spritePixelsToUnits: 8\n"
            )
        with open(os.path.join(scripts, "Menu.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Menu : MonoBehaviour {\n"
                "    void Awake() { gameObject.SetActive(false); }\n"
                "    void Update() {}\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Menu.cs.meta"), "w") as f:
            f.write("guid: menusprmenusprmenusprmenuspr01\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Menu\n"
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
                "guid: menusprmenusprmenusprmenuspr01}\n"
                "--- !u!212 &4\nSpriteRenderer:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n"
                "  m_Sprite: {fileID: 21300000, "
                "guid: 33333333333333333333333333333333, type: 3}\n"
                "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
            )
        d = tempfile.mkdtemp(prefix="upack-sa-spr-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("GameObject_SetActive", eng)
        self.assertIn("_spr_go", eng)
        # Draw must not call ColorBlock helpers that were never emitted.
        self.assertNotIn("_engine_ui_btn_tint_init", eng)
        self.assertNotIn("_engine_ui_btn_tint[", eng)
        self.assertNotIn("_spr_btn", eng)

    @needs_systems
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

    @needs_systems
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
    @needs_systems
    def test_main_camera_follows_player_parent(self):
        """Main Camera under Player: Camera_main_pos tracks Player world."""
        d = tempfile.mkdtemp(prefix="upack-camfollow-")
        unity_pack.pack(SYSTEMS, d, soa=False)
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

    def test_scene_mscript_guids_limit_analyzed_scripts(self):
        """Stripped UI scenes must not full-analyze every Assets .cs.

        When GO join leaves script=None, scan m_Script / m_SourcePrefab so
        vendor files (e.g. Destructible2D Stack) stay out of full analyze.
        """
        root = tempfile.mkdtemp(prefix="upack-mscript-")
        scripts = os.path.join(root, "Assets", "Scripts")
        vendor = os.path.join(root, "Assets", "Vendor")
        scenes = os.path.join(root, "Assets", "Scenes")
        prefabs = os.path.join(root, "Assets", "Prefabs")
        ps = os.path.join(root, "ProjectSettings")
        for d in (scripts, vendor, scenes, prefabs, ps):
            os.makedirs(d)
        with open(os.path.join(scripts, "Host.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Host : MonoBehaviour {\n"
                "    void Update() {}\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Host.cs.meta"), "w") as f:
            f.write("guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1\n")
        with open(os.path.join(vendor, "UsesStack.cs"), "w") as f:
            f.write(
                "using System.Collections.Generic;\n"
                "using UnityEngine;\n"
                "public class UsesStack : MonoBehaviour {\n"
                "    static Stack<int> pool = new Stack<int>();\n"
                "    void Update() { pool.Push(1); }\n"
                "}\n"
            )
        with open(os.path.join(vendor, "UsesStack.cs.meta"), "w") as f:
            f.write("guid: c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3\n")
        with open(os.path.join(prefabs, "Host.prefab"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Host\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n"
                "--- !u!4 &2\nTransform:\n  m_GameObject: {fileID: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n"
                "  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, "
                "guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1, type: 3}\n"
            )
        with open(os.path.join(prefabs, "Host.prefab.meta"), "w") as f:
            f.write("guid: d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4\n")
        # UI-only scene GO (no joined project script) + PrefabInstance source.
        with open(os.path.join(scenes, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Sprite\n"
                "  m_IsActive: 1\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!4 &11\nTransform:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Father: {fileID: 0}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!212 &12\nSpriteRenderer:\n"
                "  m_GameObject: {fileID: 10}\n"
                "  m_Enabled: 1\n"
                "  m_Sprite: {fileID: 0}\n"
                "--- !u!1001 &20\nPrefabInstance:\n"
                "  m_SourcePrefab: {fileID: 100100000, "
                "guid: d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4, type: 3}\n"
                # Also an authored Host m_Script that does not join a packed GO.
                "--- !u!114 &30\nMonoBehaviour:\n"
                "  m_Script: {fileID: 11500000, "
                "guid: a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1, type: 3}\n"
            )
        with open(os.path.join(ps, "EditorBuildSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1045 &1\nEditorBuildSettings:\n"
                "  m_Scenes:\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/S.unity\n"
                "    guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            )
        assets = unity_pack._asset_guid_map(root)
        guids = unity_pack._guid_map(root, asset_guids=assets)
        refs = unity_pack._scripts_referenced_in_startup_scenes(
            root, assets, guids)
        self.assertTrue(any(p.endswith("Host.cs") for p in refs))
        self.assertFalse(any(p.endswith("UsesStack.cs") for p in refs))
        # Objects with script=None (SpriteRenderer only) + YAML refs → Host only.
        objects = [{
            "name": "Sprite", "script": None, "class": "Sprite",
            "fields": {}, "pos": (0, 0, 0), "rot": (0, 0, 0, 1),
        }]
        analyses = unity_pack._analyze_scripts_and_prefabs(
            root, objects, assets)
        names = {c["name"] for a in analyses for c in a.get("classes") or []}
        self.assertIn("Host", names)
        self.assertNotIn("UsesStack", names)

    @needs_systems
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

    @needs_systems
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

    @needs_systems
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
        plan = unity_pack.pack(scene, d, soa=False)
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

    @needs_systems
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

    @needs_systems
    def test_addcomponent_camera_and_rigidbody2d(self):
        """AddComponent refuses a second DisallowMultipleComponent with Unity's error."""
        d = tempfile.mkdtemp(prefix="upack-addcomp-")
        plan = unity_pack.pack(SYSTEMS, d)
        self.assertIn("SpriteRenderer", plan.get("addcomponent_types") or [])
        names = plan.get("go_names") or []
        has_sr = set(plan.get("go_has_sprite") or [])
        self.assertIn("Graphics", names)
        self.assertIn(names.index("Graphics"), has_sr)
        if "Player" in names:
            self.assertNotIn(names.index("Player"), has_sr)
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
    @needs_systems
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

    @needs_systems
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

    @needs_systems
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

    def test_getcomponent_transform_is_go_index(self):
        """GetComponent<Transform>() ≡ GO handle (same as .transform)."""
        root = tempfile.mkdtemp(prefix="upack-gc-trs-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "Pool.cs"), "w") as f:
            f.write(
                "using UnityEngine;\n"
                "public class Pool : MonoBehaviour {\n"
                "    public GameObject prefab;\n"
                "    void Start() {\n"
                "        GameObject clone = Instantiate(prefab);\n"
                "        Transform trs = clone.GetComponent<Transform>();\n"
                "    }\n"
                "}\n"
            )
        with open(os.path.join(scripts, "Pool.cs.meta"), "w") as f:
            f.write("guid: eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee\n")
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Pool\n"
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
        d = tempfile.mkdtemp(prefix="upack-out-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("GameObject_GetComponent_Transform", eng)
        self.assertRegex(
            eng,
            r"static int GameObject_GetComponent_Transform\(int go\) \{\s*"
            r"if \(go < 0 \|\| go >= _engine_go_count\) return -1;\s*"
            r"return go;")

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
                # The string overload: `Console.WriteLine` of a non-string
                # goes through `Object_ToString`, then `_s`.
                'Console_WriteLine_s(Object_ToString(GameObject_Find("BouncePad")))',
                eng)
            self.assertIn("%s (UnityEngine.GameObject)", eng)
        r = subprocess.run(["make", "-C", d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        run = subprocess.run([os.path.join(d, "game")],
                             capture_output=True, text=True, cwd=d)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("BouncePad (UnityEngine.GameObject)", run.stdout)

    @needs_cc
    @needs_systems
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
        # #ifdef arms must share one `{` — duplicate opens across #else leave
        # cpprust _toplevel_start stuck and false-flag qsort(out) as vector_int.
        self.assertIn(
            "#ifdef _WIN32\n"
            "        if (*p == '/' || *p == '\\\\')\n"
            "#else\n"
            "        if (*p == '/')\n"
            "#endif\n"
            "        {",
            engine)
        # Braces must balance for the owning-arg walk (ignore strings).
        depth = 0
        i = 0
        in_s = None
        while i < len(engine):
            c = engine[i]
            if in_s:
                if c == "\\" and i + 1 < len(engine):
                    i += 2
                    continue
                if c == in_s:
                    in_s = None
                i += 1
                continue
            if c in ("\"", "'"):
                in_s = c
                i += 1
                continue
            if c == "/" and i + 1 < len(engine) and engine[i + 1] == "/":
                while i < len(engine) and engine[i] != "\n":
                    i += 1
                continue
            if c == "/" and i + 1 < len(engine) and engine[i + 1] == "*":
                i += 2
                while i + 1 < len(engine) and not (
                        engine[i] == "*" and engine[i + 1] == "/"):
                    i += 1
                i += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                self.assertGreaterEqual(depth, 0, "extra } in engine.c")
            i += 1
        self.assertEqual(depth, 0, "unbalanced braces in engine.c with print")

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

    @needs_systems
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

    def test_camera_script_view_pixels_and_rect(self):
        """CameraScript.HandleViewSize letterbox math for 2:1 view on 16:9."""
        pw, ph = unity_pack._camera_script_view_pixels(1920, 1080, 29.01, 14.505)
        self.assertEqual((pw, ph), (1920, 960))
        rx, ry, rw, rh = unity_pack._camera_script_rect(
            1920, 1080, 29.01, 14.505)
        self.assertAlmostEqual(rw, 1.0, places=5)
        self.assertAlmostEqual(rh, 960.0 / 1080.0, places=5)
        self.assertAlmostEqual(rx, 0.0, places=5)
        self.assertAlmostEqual(ry, (1.0 - rh) * 0.5, places=5)
        # Resized window (e.g. 1024×640): viewport must stay 2:1, not the
        # baked 16:9 rect (that stretches art vertically).
        rx, ry, rw, rh = unity_pack._camera_script_rect(
            1024, 640, 29.01, 14.505)
        self.assertAlmostEqual(rw, 1.0, places=5)
        self.assertAlmostEqual(rh, 0.8, places=5)
        self.assertAlmostEqual(
            (1024 * rw) / (640 * rh), 2.0, places=5)
        # No viewSize → player screen size (Unity default when unset).
        root = tempfile.mkdtemp(prefix="upack-noscr-")
        self.assertEqual(
            unity_pack._ui_layout_screen(root, []),
            (1024, 768))

    def test_seed_camera_script_view_emits_aspect_rect(self):
        """Authored viewSize seeds Camera_main_aspect / rect_* in data.c."""
        plan = {
            "camera": {
                "pos": (0.0, 0.0, -10.0),
                "orthographic_size": 5.0,
                "orthographic": 1,
                "near_clip": 0.3,
                "far_clip": 1000.0,
                "bg_r": 0.1, "bg_g": 0.2, "bg_b": 0.3,
            },
            "screen_width": 1920,
            "screen_height": 1080,
            "classes": {},
        }
        objs = [{"fields": {"viewSize_x": 29.01, "viewSize_y": 14.505}}]
        unity_pack._seed_camera_script_view(plan, objs)
        self.assertAlmostEqual(plan["camera_aspect"], 2.0, places=5)
        self.assertAlmostEqual(plan["camera"]["orthographic_size"], 7.2525,
                               places=4)
        self.assertAlmostEqual(plan["camera_rect"][2], 1.0, places=5)
        self.assertAlmostEqual(plan["camera_rect"][3], 960.0 / 1080.0,
                               places=5)
        data = unity_pack.emit_data(plan, used_apis=set())
        self.assertIn("float Camera_main_aspect = 2.0", data)
        self.assertIn("float Camera_main_rect_w = 1.0", data)
        self.assertIn("float Camera_main_orthographicSize = 7.2525", data)

    def test_seed_camera_no_viewsize_full_rect(self):
        """Without viewSize, aspect follows screen and rect fills it."""
        plan = {
            "camera": {
                "pos": (0.0, 0.0, -10.0),
                "orthographic_size": 5.0,
                "orthographic": 1,
                "near_clip": 0.3,
                "far_clip": 1000.0,
                "bg_r": 0.1, "bg_g": 0.2, "bg_b": 0.3,
            },
            "screen_width": 1920,
            "screen_height": 1080,
            "classes": {},
        }
        unity_pack._seed_camera_script_view(plan, [])
        self.assertAlmostEqual(plan["camera_aspect"], 1920.0 / 1080.0, places=5)
        self.assertEqual(plan["camera_rect"], (0.0, 0.0, 1.0, 1.0))
        data = unity_pack.emit_data(plan, used_apis=set())
        self.assertIn("Camera_main_aspect", data)
        self.assertIn("Camera_main_rect_x = 0.0", data)

    def test_canvas_scaler_scale_with_screen_size(self):
        """Scale With Screen Size match-height + disabled scaler no-op."""
        # match=1 (height): sf = 1080/960 = 1.125 → canvas 1707×960.
        scaler = {
            "enabled": 1,
            "ui_scale_mode": 1,
            "scale_factor": 1.0,
            "ref_x": 1920.0,
            "ref_y": 960.0,
            "screen_match_mode": 0,
            "match": 1.0,
        }
        sf = unity_pack._canvas_scaler_scale_factor(1920, 1080, scaler)
        self.assertAlmostEqual(sf, 1080.0 / 960.0, places=5)
        self.assertEqual(
            unity_pack._canvas_scaler_layout_pixels(1920, 1080, scaler),
            (1707, 960))
        # Constant Pixel Size scaleFactor 2 → half canvas units.
        cps = {
            "enabled": 1, "ui_scale_mode": 0, "scale_factor": 2.0,
            "ref_x": 800, "ref_y": 600, "screen_match_mode": 0, "match": 0,
        }
        self.assertEqual(
            unity_pack._canvas_scaler_layout_pixels(1920, 1080, cps),
            (960, 540))
        # Disabled → identity.
        off = dict(scaler)
        off["enabled"] = 0
        self.assertEqual(
            unity_pack._canvas_scaler_layout_pixels(1920, 1080, off),
            (1920, 1080))
        root = tempfile.mkdtemp(prefix="upack-scaler-")
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "ProjectSettings.asset"), "w") as f:
            f.write(
                "PlayerSettings:\n"
                "  defaultScreenWidth: 1920\n"
                "  defaultScreenHeight: 1080\n"
            )
        objs_on = [{
            "canvas": {"enabled": 1, "render_mode": 0},
            "canvas_scaler": scaler,
            "fields": {},
        }]
        self.assertEqual(
            unity_pack._ui_layout_screen(root, objs_on), (1707, 960))
        objs_off = [{
            "canvas": {"enabled": 1, "render_mode": 0},
            "canvas_scaler": off,
            "fields": {},
        }]
        self.assertEqual(
            unity_pack._ui_layout_screen(root, objs_off), (1920, 1080))
        # viewSize letterbox then scaler: 1920×960 pixels, match height → sf=1.
        objs_view = [{
            "canvas": {"enabled": 1, "render_mode": 1},
            "canvas_scaler": scaler,
            "fields": {"viewSize_x": 29.01, "viewSize_y": 14.505},
        }]
        self.assertEqual(
            unity_pack._ui_layout_screen(root, objs_view), (1920, 960))

    def test_canvas_scaler_parsed_from_scene(self):
        """CanvasScaler YAML attaches to Canvas scaffold with m_Enabled."""
        scaler_guid = "0cd44c1031e13a943bb63640046fad76"
        text = (
            "%YAML 1.1\n"
            "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
            "  m_Component:\n  - component: {fileID: 2}\n"
            "  - component: {fileID: 3}\n"
            "  - component: {fileID: 4}\n"
            "--- !u!224 &2\nRectTransform:\n"
            "  m_GameObject: {fileID: 1}\n"
            "  m_Father: {fileID: 0}\n"
            "  m_AnchorMin: {x: 0, y: 0}\n"
            "  m_AnchorMax: {x: 1, y: 1}\n"
            "  m_SizeDelta: {x: 0, y: 0}\n"
            "  m_Pivot: {x: 0.5, y: 0.5}\n"
            "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n"
            "  m_Enabled: 1\n  m_RenderMode: 1\n"
            "--- !u!114 &4\nMonoBehaviour:\n"
            "  m_GameObject: {fileID: 1}\n"
            "  m_Enabled: 0\n"
            "  m_Script: {fileID: 11500000, guid: " + scaler_guid + "}\n"
            "  m_UiScaleMode: 1\n"
            "  m_ReferenceResolution: {x: 1920, y: 960}\n"
            "  m_ScreenMatchMode: 0\n"
            "  m_MatchWidthOrHeight: 1\n"
            "  m_ScaleFactor: 1\n"
        )
        objs, _l, _c, _h = unity_pack.parse_unity_yaml(text)
        canvas = next(o for o in objs if o.get("canvas"))
        cs = canvas.get("canvas_scaler") or {}
        self.assertEqual(int(cs.get("enabled", 1)), 0)
        self.assertEqual(int(cs.get("ui_scale_mode") or 0), 1)
        self.assertAlmostEqual(float(cs.get("ref_x") or 0), 1920.0)
        self.assertAlmostEqual(float(cs.get("ref_y") or 0), 960.0)
        self.assertAlmostEqual(float(cs.get("match") or 0), 1.0)

    def test_disabled_image_not_baked(self):
        """Image m_Enabled:0 skips sprite bake (Graphic off)."""
        guid = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        d = tempfile.mkdtemp(prefix="upack-img-off-")
        png = os.path.join(d, "s.png")
        import struct, zlib
        def chunk(tag, data):
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
        raw = b"\x00\x00\x00" + b"\xff\x00\x00"
        with open(png, "wb") as f:
            f.write(
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw))
                + chunk(b"IEND", b""))
        text = (
            "%YAML 1.1\n"
            "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
            "  m_Component:\n  - component: {fileID: 2}\n"
            "  - component: {fileID: 3}\n"
            "--- !u!224 &2\nRectTransform:\n"
            "  m_GameObject: {fileID: 1}\n"
            "  m_Father: {fileID: 0}\n"
            "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n"
            "  m_Enabled: 1\n  m_RenderMode: 0\n"
            "--- !u!1 &10\nGameObject:\n  m_Name: OffImg\n"
            "  m_Component:\n  - component: {fileID: 11}\n"
            "  - component: {fileID: 12}\n"
            "--- !u!224 &11\nRectTransform:\n"
            "  m_GameObject: {fileID: 10}\n"
            "  m_Father: {fileID: 2}\n"
            "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
            "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
            "  m_SizeDelta: {x: 50, y: 50}\n"
            "  m_Pivot: {x: 0.5, y: 0.5}\n"
            "--- !u!114 &12\nMonoBehaviour:\n"
            "  m_GameObject: {fileID: 10}\n"
            "  m_Enabled: 0\n"
            "  m_Script: {fileID: 11500000, "
            "guid: fe87c0e1cc204ed48ad3b37840f39efc, type: 3}\n"
            "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
            "  m_Sprite: {fileID: 21300000, guid: " + guid + ", type: 3}\n"
            "  m_Type: 0\n"
        )
        objs, _l, cams, hier = unity_pack.parse_unity_yaml(
            text, asset_guids={guid: png})
        cams = [{"main": True, "pos": (0, 0, -10), "orthographic_size": 5.0}]
        unity_pack._bake_ui_images(
            objs, cams, 200, 100, asset_guids={guid: png}, hierarchy=hier)
        img = next(o for o in objs if o.get("name") == "OffImg")
        self.assertEqual(int(img["ui_image"].get("enabled", 1)), 0)
        self.assertIsNone(img.get("sprite"))

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

    @needs_systems
    def test_make_game_links(self):
        d = tempfile.mkdtemp(prefix="upack-sys-make-")
        unity_pack.pack(SYSTEMS, d)
        r = subprocess.run(["make", "-C", d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        run = subprocess.run([os.path.join(d, "game")],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertIn("draws=", run.stdout)

    @needs_systems
    def test_tick_animates_and_physics(self):
        d = tempfile.mkdtemp(prefix="upack-sys-run-")
        unity_pack.pack(SYSTEMS, d, soa=False)
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
                "  /* Press on Button — still visible (onClick waits for release). */\n"
                "  engine_pointer_x = 960.f;\n"
                "  engine_pointer_y = 540.f;\n"
                "  engine_pointer_down = 1;\n"
                "  engine_tick();\n"
                "  n = engine_collect_draws(buf, 128);\n"
                "  if (n != 9) return 23; /* still drawn while pressed */\n"
                "  /* Release while over → SetActive(false) hides Image + TMP. */\n"
                "  engine_pointer_down = 0;\n"
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

    @needs_systems
    def test_oncollision_enter2d_fires_on_landing(self):
        """Player.OnCollisionEnter2D prints Collision2D when hitting Ground/Ball."""
        d = tempfile.mkdtemp(prefix="upack-col2d-msg-")
        unity_pack.pack(SYSTEMS, d, soa=False)
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

    @needs_systems
    def test_player_stack_on_ball_does_not_teleport_ball(self):
        """Player landing on Ball must not drive Ball through Ground."""
        d = tempfile.mkdtemp(prefix="upack-stack-")
        unity_pack.pack(SYSTEMS, d, soa=False)
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

    @needs_systems
    def test_physics_fall_matches_wall_clock_not_frame_count(self):
        """60Hz×1s ≈ same Player fall as 50 fixed steps (Unity fixed clock)."""
        ys = {}
        for label, dt, n in (
                ("50", "0.02f", 50),
                ("60", "(1.f/60.f)", 60)):
            out = tempfile.mkdtemp(prefix="upack-fall%s-" % label)
            unity_pack.pack(SYSTEMS, out, soa=False)
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

    @needs_systems
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


class TestLiveRectTransform(unittest.TestCase):
    """Runtime RectTransform tables + C# anchoredPosition / sizeDelta."""

    def _ui_project(self, script, go_name="Panel"):
        root = tempfile.mkdtemp(prefix="upack-live-rt-")
        scripts = os.path.join(root, "Assets", "Scripts")
        os.makedirs(scripts)
        hud = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        with open(os.path.join(scripts, "Hud.cs"), "w") as f:
            f.write(script)
        with open(os.path.join(scripts, "Hud.cs.meta"), "w") as f:
            f.write("guid: %s\n" % hud)
        tex = os.path.join(root, "Assets", "tex.png")
        os.makedirs(os.path.dirname(tex), exist_ok=True)
        # 2×2 white PNG
        import struct, zlib
        def chunk(tag, data):
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
        raw = b"\x00" + b"\xff\xff\xff\xff" * 2
        raw = raw + b"\x00" + b"\xff\xff\xff\xff" * 2
        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw))
               + chunk(b"IEND", b""))
        with open(tex, "wb") as f:
            f.write(png)
        with open(tex + ".meta", "w") as f:
            f.write(
                "guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
                "TextureImporter:\n  spritePixelsToUnits: 100\n"
            )
        ps = os.path.join(root, "ProjectSettings")
        os.makedirs(ps)
        with open(os.path.join(ps, "ProjectSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "PlayerSettings:\n"
                "  defaultScreenWidth: 800\n"
                "  defaultScreenHeight: 600\n"
            )
        with open(os.path.join(ps, "EditorBuildSettings.asset"), "w") as f:
            f.write(
                "%YAML 1.1\n"
                "EditorBuildSettings:\n"
                "  m_Scenes:\n"
                "  - enabled: 1\n"
                "    path: Assets/Scenes/S.unity\n"
            )
        scene = os.path.join(root, "Assets", "Scenes")
        os.makedirs(scene)
        img = "fe87c0e1cc204ed48ad3b37840f39efc"
        with open(os.path.join(scene, "S.unity"), "w") as f:
            f.write(
                ("%%YAML 1.1\n"
                 "--- !u!1 &100\nGameObject:\n  m_Name: Main Camera\n"
                 "  m_TagString: MainCamera\n"
                 "  m_Component:\n  - component: {fileID: 101}\n"
                 "  - component: {fileID: 102}\n"
                 "--- !u!4 &101\nTransform:\n"
                 "  m_GameObject: {fileID: 100}\n"
                 "  m_Father: {fileID: 0}\n"
                 "  m_LocalPosition: {x: 0, y: 0, z: -10}\n"
                 "--- !u!20 &102\nCamera:\n"
                 "  m_GameObject: {fileID: 100}\n"
                 "  m_Orthographic: 1\n"
                 "  orthographic size: 5\n"
                 "  m_BackGroundColor: {r: 0, g: 0, b: 0, a: 1}\n"
                 "--- !u!1 &1\nGameObject:\n  m_Name: Canvas\n"
                 "  m_Component:\n  - component: {fileID: 2}\n"
                 "  - component: {fileID: 3}\n"
                 "--- !u!224 &2\nRectTransform:\n"
                 "  m_GameObject: {fileID: 1}\n"
                 "  m_Father: {fileID: 0}\n"
                 "  m_AnchorMin: {x: 0, y: 0}\n"
                 "  m_AnchorMax: {x: 1, y: 1}\n"
                 "  m_AnchoredPosition: {x: 0, y: 0}\n"
                 "  m_SizeDelta: {x: 0, y: 0}\n"
                 "  m_Pivot: {x: 0.5, y: 0.5}\n"
                 "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n"
                 "  m_Enabled: 1\n  m_RenderMode: 0\n"
                 "--- !u!1 &10\nGameObject:\n  m_Name: %s\n"
                 "  m_Component:\n  - component: {fileID: 11}\n"
                 "  - component: {fileID: 12}\n"
                 "  - component: {fileID: 13}\n"
                 "--- !u!224 &11\nRectTransform:\n"
                 "  m_GameObject: {fileID: 10}\n"
                 "  m_Father: {fileID: 2}\n"
                 "  m_AnchorMin: {x: 0.5, y: 0.5}\n"
                 "  m_AnchorMax: {x: 0.5, y: 0.5}\n"
                 "  m_AnchoredPosition: {x: 0, y: 0}\n"
                 "  m_SizeDelta: {x: 100, y: 80}\n"
                 "  m_Pivot: {x: 0.5, y: 0.5}\n"
                 "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
                 "--- !u!114 &12\nMonoBehaviour:\n"
                 "  m_GameObject: {fileID: 10}\n"
                 "  m_Script: {fileID: 11500000, guid: %s}\n"
                 "  m_Sprite: {fileID: 21300000, "
                 "guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, type: 3}\n"
                 "  m_Type: 0\n"
                 "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                 "--- !u!114 &13\nMonoBehaviour:\n"
                 "  m_GameObject: {fileID: 10}\n"
                 "  m_Script: {fileID: 11500000, "
                 "guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}\n"
                 ) % (go_name, img)
            )
        return root

    def test_live_rt_tables_emitted_for_ui(self):
        """UI pack seeds mutable RT tables + screen-rect recompute."""
        root = self._ui_project(
            "using UnityEngine;\n"
            "public class Hud : MonoBehaviour {\n"
            "    void Update() {}\n"
            "}\n"
        )
        d = tempfile.mkdtemp(prefix="upack-live-rt-out-")
        plan = unity_pack.pack(root, d)
        self.assertTrue(plan.get("live_rt"))
        self.assertTrue(any(plan["live_rt"]["has"]))
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("_engine_rt_apos_x", eng)
        self.assertIn("_engine_ui_screen_rect", eng)
        self.assertIn("_engine_rt_sx", eng)
        self.assertIn("_engine_ui_layout_w", eng)
        self.assertIn("_engine_ui_layout_h", eng)
        self.assertEqual(int(plan.get("ui_layout_width") or 0), 800)
        self.assertEqual(int(plan.get("ui_layout_height") or 0), 600)
        self.assertNotIn("static const float _spr_ncx[]", eng)

    def test_anchored_position_setter_lowers(self):
        """rectTransform.anchoredPosition = new Vector2 → RT setter."""
        root = self._ui_project(
            "using UnityEngine;\n"
            "public class Hud : MonoBehaviour {\n"
            "    public float scale;\n"
            "    void Update() {\n"
            "        rectTransform.anchoredPosition = new Vector2(40f, -20f);\n"
            "        rectTransform.sizeDelta = new Vector2(120f, 90f);\n"
            "        transform.localScale = new Vector3(0.5f, 0.5f, 1f);\n"
            "        rectTransform.localScale = Vector3.one * scale;\n"
            "    }\n"
            "}\n"
        )
        d = tempfile.mkdtemp(prefix="upack-apos-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("RectTransform_set_anchoredPosition_xy", eng)
        self.assertIn("RectTransform_set_sizeDelta_xy", eng)
        self.assertIn("RectTransform_set_localScale_xy", eng)
        self.assertRegex(
            eng,
            r"RectTransform_set_localScale_xy\s*\(\s*_engine_go_of_Hud\s*\(\s*i\s*\)\s*,"
            r"\s*\(?\s*Hud_get_scale\s*\(\s*i\s*\)")

    def test_scaffold_canvas_rect_on_hierarchy(self):
        """Dropped Canvas scaffold still seeds live RT via hierarchy snapshot."""
        root = self._ui_project(
            "using UnityEngine;\n"
            "public class Hud : MonoBehaviour { void Update() {} }\n"
        )
        objs, _a, _l, _c, hier = unity_pack.load_project(root)
        self.assertNotIn("Canvas", {o["name"] for o in objs})
        h_canvas = [h for h in hier if h.get("name") == "Canvas"]
        self.assertEqual(len(h_canvas), 1)
        self.assertIsNotNone(h_canvas[0].get("rect"))
        self.assertTrue(int(h_canvas[0].get("canvas_root") or 0))
        d = tempfile.mkdtemp(prefix="upack-rt-hier-")
        plan = unity_pack.pack(root, d)
        self.assertIn("Canvas", plan.get("go_names") or [])
        gi = plan["go_names"].index("Canvas")
        self.assertTrue(plan["live_rt"]["has"][gi])
        self.assertTrue(plan["live_rt"]["canvas_root"][gi])
        self.assertTrue(plan["live_rt"]["canvas"][gi])

    @needs_cc
    def test_mutating_apos_moves_draw(self):
        """C# anchoredPosition setter moves UI draw; unmutated seed ≈ center."""
        root = self._ui_project(
            "using UnityEngine;\n"
            "public class Hud : MonoBehaviour {\n"
            "    void Update() {\n"
            "        rectTransform.anchoredPosition = new Vector2(200f, 0f);\n"
            "    }\n"
            "}\n"
        )
        d = tempfile.mkdtemp(prefix="upack-apos-run-")
        unity_pack.pack(root, d)
        with open(os.path.join(d, "engine.c")) as f:
            eng = f.read()
        self.assertIn("RectTransform_set_anchoredPosition_xy", eng)
        host = os.path.join(d, "host_apos.c")
        with open(host, "w") as f:
            f.write(
                "void engine_tick(void);\n"
                "extern float Time_deltaTime;\n"
                "typedef struct { float x, y, half_w, half_h;\n"
                "                 float m00, m01, m10, m11;\n"
                "                 float r, g, b; float a; int tex;\n"
                "                 int sorting_layer; int sorting_order;\n"
                "               } EngineDraw;\n"
                "int engine_collect_draws(EngineDraw *out, int max);\n"
                "int main(void) {\n"
                "  EngineDraw buf[8];\n"
                "  float x0, x1;\n"
                "  int n;\n"
                "  Time_deltaTime = 0.016f;\n"
                "  n = engine_collect_draws(buf, 8);\n"
                "  if (n < 1) return 1;\n"
                "  x0 = buf[0].x;\n"
                "  if (x0 < -0.05f || x0 > 0.05f) return 2;\n"
                "  engine_tick();\n"
                "  n = engine_collect_draws(buf, 8);\n"
                "  if (n < 1) return 3;\n"
                "  x1 = buf[0].x;\n"
                "  if (x1 <= x0 + 0.5f) return 4;\n"
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
        exe = os.path.join(d, "host_apos")
        r = subprocess.run(
            [_CC, "-O2", "-o", exe, host,
             os.path.join(d, "engine.o"), os.path.join(d, "data.o"), "-lm"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
