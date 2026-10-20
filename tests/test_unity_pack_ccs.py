"""The CCSharp backend of unity_pack (tools/unity/unity_pack_ccs.py)."""
import os, subprocess, sys, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tools.unity.unity_pack_ccs as ccs


class TestPrepare(unittest.TestCase):
    def test_a_virtual_message_of_a_base_script_overrides_the_engine(self):
        t = ccs.prepare_scripts({"S.cs": "class S<T> : MonoBehaviour {\n    public virtual void Awake () { }\n    protected abstract void Update ();\n}\n"
                                         "class G : S<G> {\n    public override void Awake () { base.Awake (); }\n}\n"})["S.cs"]
        self.assertIn("public override void Awake ()", t.split("class G")[0])
        self.assertIn("public abstract override void Update ()", t)

    def test_shared_stripped_and_messages_overridden(self):
        t = ccs.prepare_script("[Shared]\npublic class A : MonoBehaviour {\n public void Update() {}\n public void Foo() {}\n}\n")
        self.assertNotIn("[Shared]", t)
        self.assertIn("public override void Update(", t)
        self.assertIn("public void Foo(", t)

    def test_only_messages_of_engine_classes_are_overridden(self):
        t = ccs.prepare_scripts({
            "a.cs": "class Timer {\n public void Start() {}\n}\nclass Hero : Base<Hero> {\n void Update() {}\n protected override void OnEnable() {}\n}\n",
            "b.cs": "class Base<T> : MonoBehaviour { }\nclass Data : ScriptableObject {\n void Awake() {}\n void Update() {}\n}\n"})
        self.assertIn("public void Start()", t["a.cs"])
        self.assertIn("public override void Update()", t["a.cs"])
        self.assertIn("public override void OnEnable()", t["a.cs"])
        self.assertIn("public override void Awake()", t["b.cs"])
        self.assertIn("\n void Update()", t["b.cs"])


class TestReachable(unittest.TestCase):
    def test_follows_named_types_only(self):
        with tempfile.TemporaryDirectory() as d:
            def w(name, text):
                with open(os.path.join(d, name), "w") as f:
                    f.write(text)
                return os.path.join(d, name)
            a = w("A.cs", "class A : MonoBehaviour { Helper h; // Unused\n }")
            h = w("Helper.cs", 'class Helper { Base b; string s = "Unused"; }')
            b = w("Base.cs", "class Base {}")
            u = w("Unused.cs", "class Unused {}")
            self.assertEqual(ccs.reachable_scripts([a], [h, b, u]), [a, h, b])
            x = w("Ext.cs", "static class Ext { public static void Go(this A a) {} }")
            m = w("MarkAttribute.cs", "class MarkAttribute : System.Attribute {}")
            c = w("C.cs", "[Mark] class C : MonoBehaviour {}")
            self.assertEqual(ccs.reachable_scripts([c], [x, m, u]), [c, x, m])


class TestPlugins(unittest.TestCase):
    def test_unclash_renames_a_private_field_named_as_a_method(self):
        t = ccs.unclash("class P {\n\tprivate string a;\n\tprivate void a(int x) { this.a = \"\"; a(1); }\n}\nclass Q { public int a; }\n")
        self.assertIn("private string a__f;", t)
        self.assertIn("this.a__f = \"\"; a(1);", t)
        self.assertIn("class Q { public int a; }", t)

    def test_surface_only_keeps_the_public_api(self):
        t = ccs.surface_only("using System;\npublic class F {\n\tprivate int a;\n\tpublic F() { a = 1; }\n\tpublic int Run(int x) { return a(x); }\n"
                             "\tprivate int a(int x) { return x; }\n\tpublic int Size { get { return a; } }\n\tclass a { }\n}\n")
        self.assertNotIn("private", t)
        self.assertIn("public F() { }", t)
        self.assertIn("public int Run(int x) { throw new System.NotSupportedException", t)
        self.assertIn("public int Size { get { throw", t)


class TestLoader(unittest.TestCase):
    def test_scene_to_csharp_without_cs2cpp(self):
        import tools.unity.unity_pack as up
        root = os.path.join(ROOT, "tests/fixtures/MiniScene")
        assets = up._asset_guid_map(root)
        objs, _l, cams, _h = up._load_scenes_lights_cameras(root, assets)
        prefabs = ccs.collect_prefabs(root, objs, assets)
        self.assertEqual(ccs.eligible(objs, cams, prefabs), (True, ""))
        self.assertIn("new GameObject(", ccs.scene_source(objs, cams, {}, prefabs))
        self.assertFalse([m for m in sys.modules if m in ("tools.cs2cpp", "tools.cpprust", "tools.csrust")])

    def test_scene_references_resolve_through_the_objects_ids(self):
        ids = {"10": "o1", "11": "o1", "20": "o2"}
        r = {"object_refs": {"go": "10", "tr": "11", "img": "20", "asset": "999"}, "object_ref_arrays": {"all": ["10", "0", "20"]}}
        decl = {"go": "UnityEngine.GameObject", "tr": "UnityEngine.Transform", "img": "UnityEngine.UI.Image", "asset": "UnityEngine.Sprite",
                "all": "UnityEngine.GameObject[]"}
        self.assertEqual(ccs.scene_refs("s", decl, r, ids), ["s.go = o1;", "s.img = o2.GetComponent<UnityEngine.UI.Image>();", "s.tr = o1.transform;",
                                                             "s.all = new UnityEngine.GameObject[] { o1, null, o2 };"])

    def test_vectors_enums_and_rects_take_their_scene_values(self):
        decl = {"location": "UnityEngine.Vector2Int", "box": "UnityEngine.Rect", "kind": "NS.Kind", "p": "UnityEngine.Vector3"}
        r = {"fields": {"location_x": 24.0, "location_y": 3.0, "kind": 2}, "vec3_fields": {"p": (1.0, 2.0, 3.0)},
             "struct_values": {"box": {"fields": {"x": 5, "y": 6, "width": 7, "height": 8}}}}
        self.assertEqual(ccs.scalar_values("o", decl, r, {"Kind": {"kind": "enum"}}),
                         ["o.kind = (NS.Kind)2;", "o.box = new Rect(5.0f, 6.0f, 7.0f, 8.0f);", "o.location = new Vector2Int(24, 3);",
                          "o.p = new Vector3(1.0f, 2.0f, 3.0f);"])

    def test_an_array_of_embedded_values_gets_an_object_per_element(self):
        classes = {"Zone": {"kind": "class", "base": None, "fields": {"name": "string", "collider": "UnityEngine.Collider2D"}}}
        r = {"struct_arrays": {"zones": [{"str_fields": {"name": "Cave"}, "object_refs": {"collider": "12"}}]}}
        out = ccs.embedded_arrays("w", {"zones": "NS.Zone[]"}, r, classes, {"12": "o3", ":managed": True}, True)
        self.assertEqual(out[0], "w.zones = new NS.Zone[1];")
        self.assertIn('.collider = o3.GetComponent<UnityEngine.Collider2D>(); ', out[1])
        self.assertIn('.name = "Cave"; w.zones[0] = ', out[1])

    def test_nested_scene_values_are_assigned_through_the_field_path(self):
        classes = {"Clamped": {"base": None, "fields": {"value": "float", "range": "NS.Range", "label": "string"}},
                   "Range": {"base": None, "fields": {"min": "float", "max": "float"}}}
        sv = {"fields": {"value": 1}, "str_fields": {"value": "1", "label": "hi"}, "vec2_fields": {},
              "struct_values": {"range": {"fields": {"max": 5.5}, "str_fields": {}, "vec2_fields": {}}}}
        self.assertEqual(ccs.nested_values("s.c", "NS.Clamped", sv, classes),
                         ['s.c.value = 1.0f;', 's.c.label = "hi";', 's.c.range.max = 5.5f;'])

    def test_script_order_comes_from_the_meta(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cs = os.path.join(d, "A.cs")
            open(cs, "w").write("class A {}")
            open(cs + ".meta", "w").write("fileFormatVersion: 2\nMonoImporter:\n  executionOrder: -5\n")
            self.assertEqual(ccs.script_order(cs), -5)
            self.assertEqual(ccs.script_order(os.path.join(d, "B.cs")), 0)

    def test_a_prefab_child_finds_its_parent_in_the_prefab(self):
        pf = {"objects": [{"name": "root", "xf_id": 1}, {"name": "child", "xf_id": 2, "father_id": 1}]}
        self.assertEqual(ccs.eligible([], [], [pf]), (True, ""))
        self.assertFalse(ccs.eligible([{"name": "orphan", "xf_id": 3, "father_id": 1}], [], [pf])[0])


@unittest.skipUnless(ccs.available(), "CCSharp not beside this repo")
class TestMiniScene(unittest.TestCase):
    def test_runs(self):
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([sys.executable, os.path.join(ROOT, "tools/unity/unity_pack.py"),
                                os.path.join(ROOT, "tests/fixtures/MiniScene"), "-o", d],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr[-500:])
            out = subprocess.run([os.path.join(d, "game")], capture_output=True, text=True).stdout
            self.assertIn("ticks=60 draws=3", out)

    def test_host_interface(self):
        host = ('#include <stdio.h>\n#include "engine_draw.h"\nint main(void){static EngineDraw d[8];EngineCamera c[2];\n'
                'for(int i=0;i<60;i++)engine_tick();int n=engine_collect_draws(d,8);\n'
                'printf("%d %d %d %.2f\\n",n,engine_collect_cameras(c,2),engine_texture_count(),d[2].x);return 0;}\n')
        with tempfile.TemporaryDirectory() as d:
            subprocess.run([sys.executable, os.path.join(ROOT, "tools/unity/unity_pack.py"),
                            os.path.join(ROOT, "tests/fixtures/MiniScene"), "-o", d], check=True, capture_output=True)
            with open(os.path.join(d, "host.c"), "w") as f:
                f.write(host)
            subprocess.run(["gcc", "-w", "-I", d, "-o", os.path.join(d, "host"), os.path.join(d, "host.c"),
                            os.path.join(d, "engine.c"), "-lm"], check=True)
            out = subprocess.run([os.path.join(d, "host")], capture_output=True, text=True).stdout
            self.assertEqual(out.strip(), "3 1 1 1.50")

    def test_several_scripts_and_get_component(self):
        host = ('#include <stdio.h>\n#include "engine_draw.h"\nint main(void){static EngineDraw d[8];\n'
                'for(int i=0;i<60;i++)engine_tick();int n=engine_collect_draws(d,8);\n'
                'printf("%d %d %.2f\\n",n,d[n-1].sorting_order,d[n-1].x);return 0;}\n')
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([sys.executable, os.path.join(ROOT, "tools/unity/unity_pack.py"),
                                os.path.join(ROOT, "tests/fixtures/ComponentScene"), "-o", d], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr[-800:])
            with open(os.path.join(d, "host.c"), "w") as f:
                f.write(host)
            subprocess.run(["gcc", "-w", "-I", d, "-o", os.path.join(d, "host"), os.path.join(d, "host.c"),
                            os.path.join(d, "engine.c"), "-lm"], check=True)
            out = subprocess.run([os.path.join(d, "host")], capture_output=True, text=True).stdout
            self.assertEqual(out.strip(), "3 120 1.50")      # Hero: Flasher adds 2 a frame to its sorting order; Player moves it 1.5 in a second

    def test_prefab_instantiate_and_destroy(self):
        host = ('#include <stdio.h>\n#include "engine_draw.h"\nint main(void){static EngineDraw d[512];\n'
                'for(int i=0;i<600;i++)engine_tick();int n=engine_collect_draws(d,512);\n'
                'printf("%d %d\\n",n,engine_texture_count());return 0;}\n')
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([sys.executable, os.path.join(ROOT, "tools/unity/unity_pack.py"),
                                os.path.join(ROOT, "tests/fixtures/PrefabScene"), "-o", d], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr[-800:])
            with open(os.path.join(d, "host.c"), "w") as f:
                f.write(host)
            subprocess.run(["gcc", "-w", "-I", d, "-o", os.path.join(d, "host"), os.path.join(d, "host.c"),
                            os.path.join(d, "engine.c"), "-lm"], check=True)
            out = subprocess.run([os.path.join(d, "host")], capture_output=True, text=True)
            # a bullet a frame for 600 frames, each living 30 updates: 600 made (more than the 256 slots of its class), 29 alive at the end
            self.assertEqual((out.returncode, out.stdout.strip()), (0, "29 1"))


if __name__ == "__main__":
    unittest.main()
