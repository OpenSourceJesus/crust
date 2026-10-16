"""The CCSharp backend of unity_pack (tools/unity/unity_pack_ccs.py)."""
import os, subprocess, sys, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tools.unity.unity_pack_ccs as ccs


class TestPrepare(unittest.TestCase):
    def test_shared_stripped_and_messages_overridden(self):
        t = ccs.prepare_script("[Shared]\npublic class A : MonoBehaviour {\n public void Update() {}\n public void Foo() {}\n}\n")
        self.assertNotIn("[Shared]", t)
        self.assertIn("public override void Update(", t)
        self.assertIn("public void Foo(", t)


@unittest.skipUnless(ccs.available(), "CCSharp not beside this repo")
class TestMiniScene(unittest.TestCase):
    def test_runs(self):
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([sys.executable, os.path.join(ROOT, "tools/unity/unity_pack.py"),
                                os.path.join(ROOT, "tests/fixtures/MiniScene"), "--engine", "-o", d],
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
                            os.path.join(ROOT, "tests/fixtures/MiniScene"), "--engine", "-o", d], check=True, capture_output=True)
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
                                os.path.join(ROOT, "tests/fixtures/ComponentScene"), "--engine", "-o", d], capture_output=True, text=True)
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
                                os.path.join(ROOT, "tests/fixtures/PrefabScene"), "--engine", "-o", d], capture_output=True, text=True)
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
