#!/usr/bin/env python3
"""godot_pack_test_fast.py -- the Godot front end's tests, fast.

    python3 tools/godot_pack_test_fast.py
    python3 tools/godot_pack_test_fast.py TestInput      # one class
    python3 tools/godot_pack_test_fast.py -k vector      # by name

Each test writes a small Godot project and packs it with
tools/unity_pack.py, but skips what makes a pack slow and is not about
Godot: the emitted C is not re-validated through cpprust + crust (about 2 s
a pack), and it is compiled with gcc -O0 rather than built as an -O3 player.
The emitted C is the same text either way (the `.cpp` twin is a copy), so a
test here sees what a player runs; `tests/test_unity_pack.py` (TestGodot)
keeps packing through the whole pipeline, and is what proves the C stays in
the crust subset.

What is covered: Sprite2D rows and their draw list, Camera2D views and
following (and a y-down frame through gles2_view.c when EGL is there),
input actions, Timers and the scripts' own signals, node references
(GetNode, exported node fields, Timer control), collision layers, the
runtime hierarchy, Vector2 arithmetic and methods, the 2D GPU path
(--gpu-batch: the batch shader's frame, the sprite effects), a freed node's
body leaving the world (with a
Box2D-Packed checkout: its library is built once a run, about 10 s), and
the resource reader's inline objects.

Requires gcc for the tests that run the engine; they skip without it.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import tools.godot_pack as godot  # noqa: E402
import tools.unity_pack as unity_pack  # noqa: E402

_CC = shutil.which("gcc") or shutil.which("cc")
needs_cc = unittest.skipIf(_CC is None, "no C compiler")


# ---------------------------------------------------------------------------
# Packing, fast
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _fast():
    """unity_pack without the cpprust + crust re-validation of its output
    (the emitted C is then written as emitted), and quiet: the packer's
    progress and warnings are captured, returned as the context's value."""
    saved = unity_pack.validate_emitted_c
    unity_pack.validate_emitted_c = lambda *a, **k: None
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            yield err
    finally:
        unity_pack.validate_emitted_c = saved


def load(project):
    """(objects, cameras) as the back end gets them."""
    with _fast():
        objs, _an, _lights, cams, _hier = unity_pack.load_project(project)
    return objs, cams


def pack(test, project):
    """Pack *project* into a temporary directory; its path."""
    out = tempfile.mkdtemp(prefix="gpf-")
    test.addCleanup(shutil.rmtree, out, True)
    with _fast():
        unity_pack.pack(project, out, force=True)
    return out


def run_c(test, out, source, extra_libs=(), args=()):
    """Compile *source* (a main over engine_draw.h) with the pack's engine.c
    and data.c at -O0, run it, and return its stdout lines."""
    src = os.path.join(out, "harness.c")
    with open(src, "w") as f:
        f.write(source)
    exe = os.path.join(out, "harness")
    r = subprocess.run(
        [_CC, "-O0", "-w", "-I", out, "-o", exe, src,
         os.path.join(out, "engine.c"), os.path.join(out, "data.c")]
        + list(extra_libs) + ["-lm"], capture_output=True, text=True)
    test.assertEqual(r.returncode, 0, r.stderr[-2000:])
    run = subprocess.run([exe] + list(args), capture_output=True, text=True,
                         timeout=60)
    test.assertEqual(run.returncode, 0, run.stderr)
    return run.stdout.splitlines()


_BOX2D_ROOT = unity_pack.find_box2d_root()
needs_box2d = unittest.skipUnless(
    _BOX2D_ROOT is not None and _CC is not None,
    "2D physics is Box2D-Packed: clone https://github.com/crustos/box2d "
    "beside this repository")
#: Box2D-Packed built once for the run: (library, include directory).
_BOX2D_LIB = []


def pack_physics(test, project):
    """Pack *project* with its Box2D-Packed glue; its directory."""
    out = tempfile.mkdtemp(prefix="gpf-")
    test.addCleanup(shutil.rmtree, out, True)
    with _fast():
        unity_pack.pack(project, out, force=True, box2d_root=_BOX2D_ROOT)
    return out


def run_physics(test, out, source):
    """run_c with the glue and Box2D-Packed: the library is built once (a
    player build, about 10 s), then linked with each pack's glue."""
    if not _BOX2D_LIB:
        d = tempfile.mkdtemp(prefix="gpf-box2d-")
        with _fast():
            unity_pack.build_player_executable(
                out, "lib", box2d_root=_BOX2D_ROOT)
        shutil.copytree(os.path.join(out, "box2d"), os.path.join(d, "box2d"))
        _BOX2D_LIB.append((os.path.join(d, "box2d", "libbox2d.a"),
                           os.path.join(d, "box2d", "box2d_src", "include")))
    lib, inc = _BOX2D_LIB[0]
    glue = os.path.join(out, "physics_box2d.o")
    r = subprocess.run([_CC, "-O1", "-std=c17", "-w", "-I", inc, "-c", "-o",
                        glue, os.path.join(out, "physics_box2d.c")],
                       capture_output=True, text=True)
    test.assertEqual(r.returncode, 0, r.stderr[-2000:])
    return run_c(test, out, source, extra_libs=(glue, lib, "-lpthread"))


def refusal(test, project):
    """The PackError packing *project* raises."""
    with test.assertRaises(unity_pack.PackError) as cm:
        load(project)
    return str(cm.exception)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def png(path, w, h, px):
    """An 8-bit RGBA PNG; px(x, y) with y from the top, as Godot's."""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
    raw = b"".join(b"\0" + bytes(c for x in range(w) for c in px(x, y))
                   for y in range(h))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw))
                + chunk(b"IEND", b""))


SCRIPTS = {
    "Mover.cs": ("using Godot;\n"
                 "public partial class Mover : Sprite2D {\n"
                 "    public override void _Process(double delta) {\n"
                 "        Position = new Vector2(Position.X + 10, "
                 "Position.Y);\n"
                 "    }\n"
                 "}\n"),
    "Gone.cs": ("using Godot;\n"
                "public partial class Gone : Sprite2D {\n"
                "    public override void _Ready() { QueueFree(); }\n"
                "}\n"),
}


def project(test, scene, settings="", scripts=None, subs="", viewport=None):
    """A Godot project: main.tscn's nodes under a `Main` Node2D, tb.png (4x2:
    a red top row over a blue one), sheet.png (8x4: 4 x 2 frames of 2x2,
    red = 30 x frame), the scripts, and project.godot's *settings*."""
    d = tempfile.mkdtemp(prefix="gpf-proj-")
    test.addCleanup(shutil.rmtree, d, True)
    text = ('config_version=5\n\n[application]\n\n'
            'run/main_scene="res://main.tscn"\n')
    if viewport:
        text += ('\n[display]\n\nwindow/size/viewport_width=%d\n'
                 'window/size/viewport_height=%d\n\n[rendering]\n\n'
                 'environment/defaults/default_clear_color='
                 'Color(0, 0, 0, 1)\n' % viewport)
    with open(os.path.join(d, "project.godot"), "w") as f:
        f.write(text + settings)
    png(os.path.join(d, "tb.png"), 4, 2,
        lambda x, y: (255, 0, 0, 255) if y == 0 else (0, 0, 255, 255))
    png(os.path.join(d, "sheet.png"), 8, 4,
        lambda x, y: ((x // 2 + 4 * (y // 2)) * 30, 0, 0, 255))
    ext = ('[ext_resource type="Texture2D" path="res://tb.png" id="1"]\n'
           '[ext_resource type="Texture2D" path="res://sheet.png" id="2"]\n')
    all_scripts = dict(SCRIPTS)
    all_scripts.update(scripts or {})
    for i, (name, src) in enumerate(sorted(all_scripts.items())):
        with open(os.path.join(d, name), "w") as f:
            f.write(src)
        ext += ('[ext_resource type="Script" path="res://%s" id="s_%s"]\n'
                % (name, name[:-3]))
    with open(os.path.join(d, "main.tscn"), "w") as f:
        f.write('[gd_scene format=3]\n\n' + ext + '\n' + subs +
                '[node name="Main" type="Node2D"]\n\n' + scene)
    return d


def scene_line(project_dir, needle):
    """The main.tscn line holding *needle* (diagnostics name it)."""
    with open(os.path.join(project_dir, "main.tscn")) as f:
        for n, line in enumerate(f, 1):
            if needle in line:
                return n
    raise AssertionError(needle)


# ---------------------------------------------------------------------------
# The resource reader
# ---------------------------------------------------------------------------

class TestResourceReader(unittest.TestCase):

    def test_inline_objects(self):
        # project.godot's input events: Object(Class, "key": value, ..)
        secs = godot.parse_resource(
            '[input]\n\njump={\n"deadzone": 0.5,\n"events": ['
            'Object(InputEventKey,"device":-1,"physical_keycode":32,'
            '"script":null)\n, Object(InputEventJoypadMotion,"device":-1,'
            '"axis":0,"axis_value":-1.0,"script":null)\n]\n}\n', "p")
        jump = secs[1]["props"]["jump"]
        self.assertEqual(jump["deadzone"], 0.5)
        self.assertEqual(jump["events"], [
            {"_type": "InputEventKey", "device": -1,
             "physical_keycode": 32, "script": None},
            {"_type": "InputEventJoypadMotion", "device": -1, "axis": 0,
             "axis_value": -1.0, "script": None}])


# ---------------------------------------------------------------------------
# Sprite2D
# ---------------------------------------------------------------------------

SPRITES = (
    '[node name="Plain" type="Sprite2D" parent="."]\n'
    'position = Vector2(100, 50)\ntexture = ExtResource("1")\n\n'
    '[node name="Corner" type="Sprite2D" parent="."]\n'
    'position = Vector2(10, 20)\nscale = Vector2(2, 3)\n'
    'texture = ExtResource("1")\ncentered = false\n'
    'offset = Vector2(1, 1)\n\n'
    '[node name="Tinted" type="Node2D" parent="."]\n'
    'modulate = Color(0.5, 1, 1, 0.5)\nz_index = 2\n\n'
    '[node name="Flip" type="Sprite2D" parent="Tinted"]\n'
    'position = Vector2(30, 0)\nrotation = 1.5707964\n'
    'texture = ExtResource("1")\nflip_h = true\n'
    'self_modulate = Color(1, 0.5, 1, 1)\nz_index = -1\n\n'
    '[node name="Frame" type="Sprite2D" parent="."]\n'
    'position = Vector2(0, 200)\ntexture = ExtResource("2")\n'
    'hframes = 4\nvframes = 2\nframe = 5\nz_index = -3\n\n'
    '[node name="Atlas" type="Sprite2D" parent="."]\n'
    'texture = SubResource("at")\n\n'
    '[node name="Hidden" type="Sprite2D" parent="."]\n'
    'visible = false\ntexture = ExtResource("1")\n\n'
    '[node name="Mover" type="Sprite2D" parent="."]\n'
    'position = Vector2(0, 300)\ntexture = ExtResource("1")\n'
    'script = ExtResource("s_Mover")\n\n'
    '[node name="Gone" type="Sprite2D" parent="."]\n'
    'texture = ExtResource("1")\nscript = ExtResource("s_Gone")\n')
ATLAS = ('[sub_resource type="AtlasTexture" id="at"]\n'
         'atlas = ExtResource("2")\nregion = Rect2(2, 0, 4, 2)\n\n')

DUMP_DRAWS = r'''
#include <stdio.h>
#include "engine_draw.h"
extern float Time_deltaTime;
int main(void) {
    EngineDraw b[64]; int i, n;
    Time_deltaTime = 0.0166667f;
    engine_tick(); engine_tick();
    n = engine_collect_draws(b, 64);
    for (i = 0; i < n; i++)
        printf("%g %g %d\n", b[i].x, b[i].y, b[i].sorting_layer);
    return 0;
}
'''


class TestSprites(unittest.TestCase):

    def near(self, a, b):
        for x, y in zip(a, b):
            self.assertAlmostEqual(x, y, places=5)

    def test_rows(self):
        objs, _cams = load(project(self, SPRITES, subs=ATLAS))
        sp = {o["name"]: o["sprite"] for o in objs if o.get("sprite")}
        self.assertEqual(sorted(sp), ["Atlas", "Corner", "Flip", "Frame",
                                      "Gone", "Hidden", "Mover", "Plain"])
        plain = sp["Plain"]
        self.near((plain["half_w"], plain["half_h"]), (2.0, 1.0))
        # Texture row 0 (the image's bottom) at local -y; Godot's y is
        # down, so local y is mirrored: the red top row is drawn on top.
        self.near((plain["m00"], plain["m01"], plain["m10"], plain["m11"]),
                  (1.0, 0.0, 0.0, -1.0))
        self.assertEqual(plain["tex_rgba"][:3], b"\x00\x00\xff")
        # Not centered, offset (1, 1), scaled 2 x 3: the 4x2 rect's centre
        # is (3, 2) from the origin, (6, 6) in the world.
        c = sp["Corner"]
        self.near((c["half_w"], c["half_h"], c["draw_off_x"],
                   c["draw_off_y"]), (4.0, 3.0, 6.0, 6.0))
        # Turned 90 degrees and flipped in x; the parent's modulate times
        # its own self_modulate.
        f = sp["Flip"]
        self.near((f["m00"], f["m01"], f["m10"], f["m11"]),
                  (0.0, 1.0, -1.0, 0.0))
        self.near((f["r"], f["g"], f["b"], f["a"]), (0.5, 0.5, 1.0, 0.5))
        # frame 5 of 4 x 2: column 1 of row 1; the atlas region's first
        # pixel is frame 1's.
        self.assertEqual((sp["Frame"]["tex_w"], sp["Frame"]["tex_h"]),
                         (2, 2))
        self.assertEqual(sp["Frame"]["tex_rgba"][0], 150)
        self.assertEqual((sp["Atlas"]["tex_w"], sp["Atlas"]["tex_h"]),
                         (4, 2))
        self.assertEqual(sp["Atlas"]["tex_rgba"][0], 30)
        self.assertEqual(sp["Hidden"]["enabled"], 0)
        # z_index relative to the parent's: -3, 0 and 2 - 1 are layers 0, 1
        # and 2; tree order within a layer.
        self.assertEqual(
            [(n, sp[n]["sorting_layer"]) for n in ("Frame", "Plain", "Flip")],
            [("Frame", 0), ("Plain", 1), ("Flip", 2)])
        self.assertLess(sp["Plain"]["sorting_order"],
                        sp["Corner"]["sorting_order"])

    def test_refusals_name_the_scene_line(self):
        cases = [
            ('[node name="S" type="Sprite2D" parent="."]\n'
             'texture = ExtResource("2")\nhframes = 2\nframe = 2\n',
             "frame = 2", "frame 2 is outside 2 x 1 frames"),
            ('[node name="S" type="Sprite2D" parent="."]\n'
             'texture = ExtResource("1")\nhframes = 3\n',
             'texture = ExtResource("1")',
             "the frame rect (0, 0, 1.33333, 2) is not whole pixels"),
            ('[node name="S" type="Sprite2D" parent="."]\n'
             'texture = ExtResource("1")\nshow_behind_parent = true\n',
             "show_behind_parent", "show_behind_parent is not packed yet"),
            ('[node name="A" type="AnimatedSprite2D" parent="."]\n'
             'sprite_frames = SubResource("at")\n',
             "sprite_frames", "`A`: AnimatedSprite2D is not packed yet"),
            ('[sub_resource type="GradientTexture2D" id="g"]\n'
             '[node name="S" type="Sprite2D" parent="."]\n'
             'texture = SubResource("g")\n',
             'SubResource("g")\n', "a GradientTexture2D texture is not "
             "packed yet"),
        ]
        for nodes, needle, want in cases:
            d = project(self, nodes, subs=ATLAS)
            self.assertIn("main.tscn:%d: error: %s" % (
                scene_line(d, needle), want), refusal(self, d))
        d = project(self, '[node name="S" type="Sprite2D" parent="."]\n'
                    'texture = ExtResource("9")\n',
                    subs='[ext_resource type="Texture2D" '
                    'path="res://icon.svg" id="9"]\n\n')
        with open(os.path.join(d, "icon.svg"), "w") as f:
            f.write("<svg/>")
        self.assertIn("error: res://icon.svg: only PNG textures are packed "
                      "yet", refusal(self, d))

    @needs_cc
    def test_draw_list(self):
        out = pack(self, project(self, SPRITES, subs=ATLAS))
        # Painter's order; Hidden is not drawn, nor Gone once freed; Mover
        # has moved 10 px a frame; Corner is drawn at its rect's centre.
        self.assertEqual(run_c(self, out, DUMP_DRAWS), [
            "0 200 0",      # Frame
            "100 50 1",     # Plain
            "16 26 1",      # Corner
            "0 0 1",        # Atlas
            "20 300 1",     # Mover
            "30 0 2",       # Flip
        ])


# ---------------------------------------------------------------------------
# Camera2D
# ---------------------------------------------------------------------------

CORNER = ('[node name="TL" type="Sprite2D" parent="."]\n'
          'texture = ExtResource("1")\ncentered = false\n'
          'position = Vector2(8, 4)\nscale = Vector2(4, 4)\n\n')
VIEW = (96, 64)     # gles2_view.c's FBO: one Godot pixel, one pixel

CAMERA_POS = r'''
#include <stdio.h>
#include "engine_draw.h"
extern float Time_deltaTime, Camera_main_pos_x, Camera_main_pos_y;
int main(void) {
    EngineDraw b[8]; int i;
    Time_deltaTime = 0.0166667f;
    for (i = 0; i < 7; i++) {
        engine_tick(); engine_collect_draws(b, 8);
        printf("%g %g\n", Camera_main_pos_x, Camera_main_pos_y);
    }
    return 0;
}
'''


class TestCamera(unittest.TestCase):

    def test_views(self):
        _objs, cams = load(project(self, CORNER, viewport=VIEW))
        # No Camera2D: the viewport's rect from the origin, y down.
        self.assertEqual(len(cams), 1)
        self.assertEqual(cams[0]["name"], "(viewport)")
        self.assertEqual(cams[0]["pos"][:2], (48.0, 32.0))
        self.assertEqual(cams[0]["orthographic_size"], 32.0)
        self.assertEqual((cams[0]["bg_r"], cams[0]["bg_g"]), (0.0, 0.0))
        # The first enabled Camera2D; zoom 2 halves the view.
        _objs, cams = load(project(
            self, CORNER +
            '[node name="Off" type="Camera2D" parent="."]\n'
            'enabled = false\n\n'
            '[node name="Cam" type="Camera2D" parent="."]\n'
            'position = Vector2(24, 16)\nzoom = Vector2(2, 2)\n',
            viewport=VIEW))
        self.assertEqual(cams[0]["name"], "Cam")
        self.assertEqual(cams[0]["pos"][:2], (24.0, 16.0))
        self.assertEqual(cams[0]["godot_view"]["half"], (24.0, 16.0))

    def test_view_center_is_godots(self):
        # Camera2D::get_camera_transform: anchored, clamped left then right,
        # top then bottom, then offset.
        view = {"half": (48.0, 32.0), "drag_center": True,
                "offset": (5.0, 0.0), "limits": (0.0, 0.0, 100.0, 50.0)}
        c = godot.camera_view_center
        self.assertEqual(c(view, 30.0, 7.0), (53.0, 18.0))
        self.assertEqual(c(view, 90.0, 40.0), (57.0, 18.0))
        view["drag_center"] = False
        self.assertEqual(c(view, 1.0, -9.0), (54.0, 18.0))

    def test_refusals_name_the_scene_line(self):
        cases = [
            ('position_smoothing_enabled = true\n',
             "position_smoothing_enabled",
             "Camera2D position_smoothing_enabled is not packed yet"),
            ('rotation = 0.5\nignore_rotation = false\n', "ignore_rotation",
             "a rotated Camera2D (ignore_rotation = false) is not packed "
             "yet"),
            ('zoom = Vector2(0, 1)\n', "zoom =",
             "Camera2D zoom must be positive"),
        ]
        for props, needle, want in cases:
            d = project(self, '[node name="C" type="Camera2D" parent="."]\n'
                        + props, viewport=VIEW)
            self.assertIn("main.tscn:%d: error: %s" % (
                scene_line(d, needle), want), refusal(self, d))

    @needs_cc
    def test_follows_what_moves_it(self):
        # A Camera2D child of a scripted node follows it, 7 px below, to
        # limit_right less half the view, then offset 5; limit_top holds
        # the view's top at 0.
        out = pack(self, project(
            self, '[node name="Player" type="Sprite2D" parent="."]\n'
            'texture = ExtResource("1")\nscript = ExtResource("s_Mover")\n\n'
            '[node name="Cam" type="Camera2D" parent="Player"]\n'
            'position = Vector2(0, 7)\noffset = Vector2(5, 0)\n'
            'limit_right = 100\nlimit_top = 0\n', viewport=VIEW))
        with open(os.path.join(out, "data.c")) as f:
            self.assertIn("int Camera_main_y_down = 1;", f.read())
        self.assertEqual(" ".join(run_c(self, out, CAMERA_POS)),
                         "15 32 25 32 35 32 45 32 55 32 57 32 57 32")

    @needs_cc
    def test_follows_a_parent_another_script_moves(self):
        # the camera's parent has no script; a Driver moves it through a
        # reference each frame, and the camera (its child, 4 px right)
        # follows through the runtime hierarchy
        driver = ("using Godot;\n"
                  "public partial class Driver : Node2D {\n"
                  "    private Node2D _rig;\n"
                  "    private int _f;\n"
                  "    public override void _Ready() {\n"
                  "        _rig = GetNode<Node2D>(\"../Rig\");\n"
                  "    }\n"
                  "    public override void _Process(double delta) {\n"
                  "        _f = _f + 1;\n"
                  "        _rig.Position = new Vector2(_f * 10, 0);\n"
                  "    }\n"
                  "}\n")
        out = pack(self, project(
            self, '[node name="Rig" type="Node2D" parent="."]\n\n'
            '[node name="Cam" type="Camera2D" parent="Rig"]\n'
            'position = Vector2(4, 0)\n\n'
            '[node name="Driver" type="Node2D" parent="."]\n'
            'script = ExtResource("s_Driver")\n',
            scripts={"Driver.cs": driver}, viewport=VIEW))
        self.assertEqual(" ".join(run_c(self, out, CAMERA_POS)).split()[::2],
                         ["14", "24", "34", "44", "54", "64", "74"])

    @needs_cc
    def test_gles2_frame_is_y_down(self):
        """gles2_view.c through a zoom-2 Camera2D: the 4x2 red-over-blue
        sprite at (8, 4) x 4 is the rect (16, 8) - (48, 24) on screen, red
        on top. Skips without EGL / GLES2 or a software rasteriser."""
        out = pack(self, project(
            self, CORNER + '[node name="Cam" type="Camera2D" parent="."]\n'
            'position = Vector2(24, 16)\nzoom = Vector2(2, 2)\n',
            viewport=VIEW))
        view = os.path.join(ROOT, "examples", "unity_pack", "gles2_view.c")
        exe = os.path.join(out, "view")
        r = subprocess.run(
            [_CC, "-O0", "-w", "-I", out, "-o", exe, view,
             os.path.join(out, "engine.c"), os.path.join(out, "data.c"),
             "-lEGL", "-lGLESv2", "-lm"], capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest("cannot link GLES2 view: %s" % r.stderr[-300:])
        env = dict(os.environ, EGL_PLATFORM="surfaceless",
                   LIBGL_ALWAYS_SOFTWARE="1")
        ppm = os.path.join(out, "frame.ppm")
        run = subprocess.run([exe, ppm], capture_output=True, text=True,
                             env=env)
        if run.returncode != 0 or not os.path.isfile(ppm):
            self.skipTest("GLES run failed (no soft rasteriser?)")
        with open(ppm, "rb") as f:
            _magic, dims, _maxv, px = f.read().split(b"\n", 3)
        w, h = map(int, dims.split())
        self.assertEqual((w, h), VIEW)

        def at(x, y):
            i = (y * w + x) * 3
            return tuple(px[i:i + 3])
        for xy in ((17, 9), (46, 15)):
            self.assertEqual(at(*xy), (255, 0, 0), xy)
        for xy in ((17, 17), (46, 23)):
            self.assertEqual(at(*xy), (0, 0, 255), xy)
        for xy in ((15, 9), (17, 25), (50, 9), (17, 6)):
            self.assertEqual(at(*xy), (0, 0, 0), xy)


# ---------------------------------------------------------------------------
# Input actions
# ---------------------------------------------------------------------------

def _event(kind, **kv):
    return "Object(%s,%s,\"script\":null)" % (kind, ",".join(
        '"%s":%s' % (k, str(v).lower() if isinstance(v, bool) else v)
        for k, v in kv.items()))


def _action(name, deadzone, *events):
    return '%s={\n"deadzone": %s,\n"events": [%s]\n}\n' % (
        name, deadzone, "\n, ".join(events))


INPUT_MAP = "\n[input]\n\n" + "".join((
    _action("jump", 0.5,
            _event("InputEventKey", device=-1, physical_keycode=32),
            _event("InputEventJoypadButton", device=-1, button_index=0)),
    _action("move_left", 0.2,
            _event("InputEventKey", device=-1, physical_keycode=65),
            _event("InputEventJoypadMotion", device=-1, axis=0,
                   axis_value=-1.0)),
    _action("move_right", 0.2,
            _event("InputEventKey", device=-1, physical_keycode=68),
            _event("InputEventJoypadMotion", device=-1, axis=0,
                   axis_value=1.0)),
    _action("dash", 0.5,
            _event("InputEventKey", device=-1, keycode=68,
                   shift_pressed=True)),
))

PLAYER = """using Godot;

public partial class Player : Node2D
{
    private int _frame;

    public override void _Process(double delta)
    {
        _frame = _frame + 1;
        Vector2 v = Input.GetVector("move_left", "move_right", "ui_up", "ui_down");
        float ax = Input.GetAxis("move_left", "move_right");
        if (Input.IsActionJustPressed("jump")) { GD.Print("f", _frame, " jump down"); }
        if (Input.IsActionJustReleased("jump")) { GD.Print("f", _frame, " jump up"); }
        if (Input.IsActionPressed("dash")) { GD.Print("f", _frame, " dash"); }
        if (Input.IsKeyPressed(Key.Escape)) { GD.Print("f", _frame, " esc"); }
        GD.Print("f", _frame, " v=", v.X, ",", v.Y, " ax=", ax, " s=",
                 Input.GetActionStrength("move_right"), " joy=",
                 Input.GetJoyAxis(0, JoyAxis.LeftY));
    }
}
"""

#: Frame by frame: Space on 2-3, D on 3-4, Left Shift on 4, Down on 3,
#: Escape on 5; the left stick (-0.6, up 0.5) on 6, x 0.1 on 7; joypad A on 7.
DRIVE = r'''
#include <stdio.h>
#include "engine_draw.h"
extern float Time_deltaTime;
extern int engine_keyboard_connected, engine_keyboard_space,
    engine_keyboard_d, engine_keyboard_leftShift, engine_keyboard_escape,
    engine_keyboard_downArrow;
extern int engine_gamepad_connected, engine_gamepad_button[15];
extern float engine_gamepad_axis[6];
int main(void) {
    int f;
    Time_deltaTime = 0.0166667f;
    engine_keyboard_connected = 1;
    engine_gamepad_connected = 1;
    for (f = 1; f <= 7; f++) {
        engine_keyboard_space = (f == 2 || f == 3);
        engine_keyboard_d = (f == 3 || f == 4);
        engine_keyboard_leftShift = (f == 4);
        engine_keyboard_downArrow = (f == 3);
        engine_keyboard_escape = (f == 5);
        engine_gamepad_axis[0] = f == 6 ? -0.6f : (f == 7 ? 0.1f : 0.f);
        engine_gamepad_axis[1] = f == 6 ? 0.5f : 0.f;   /* host: y up */
        engine_gamepad_button[0] = (f == 7);
        engine_tick();
    }
    return 0;
}
'''


class TestInput(unittest.TestCase):

    def _project(self, script=PLAYER, settings=INPUT_MAP):
        return project(
            self, '[node name="Player" type="Node2D" parent="."]\n'
            'script = ExtResource("s_Player")\n',
            settings=settings, scripts={"Player.cs": script})

    def test_input_map(self):
        d = self._project()
        m = godot.input_map(d)
        # Godot 4.4's built-ins, with the toggle deadzone; the project's own.
        self.assertEqual(m["ui_left"]["deadzone"], 0.5)
        self.assertEqual(len(m["ui_left"]["events"]), 3)
        self.assertEqual(m["move_left"]["deadzone"], 0.2)
        self.assertEqual([e["_type"] for e in m["jump"]["events"]],
                         ["InputEventKey", "InputEventJoypadButton"])

    @needs_cc
    def test_actions_by_godots_rules(self):
        out = pack(self, self._project())
        self.assertEqual(run_c(self, out, DRIVE), [
            "f1 v=0,0 ax=0 s=0 joy=0",
            "f2 jump down",
            "f2 v=0,0 ax=0 s=0 joy=0",
            # D and Down: (1, 1) past length 1, normalized
            "f3 v=0.707107,0.707107 ax=1 s=1 joy=0",
            "f4 jump up",
            # Shift+D is `dash`; D alone is still move_right (the action's
            # modifiers are a subset of the key's)
            "f4 dash",
            "f4 v=1,0 ax=1 s=1 joy=0",
            "f5 esc",
            "f5 v=0,0 ax=0 s=0 joy=0",
            # stick (-0.6, up 0.5): move_left's strength (0.6 - 0.2) / 0.8;
            # GetVector's raw (-0.6, -0.5) over the deadzones' mean 0.35
            "f6 v=-0.509419,-0.424516 ax=-0.5 s=0 joy=-0.5",
            # x 0.1 is inside the deadzone; joypad A presses jump
            "f7 jump down",
            "f7 v=0,0 ax=0 s=0 joy=0",
        ])

    def test_refusals_name_the_script_column(self):
        def body(stmt):
            return ("using Godot;\npublic partial class Player : Node2D {\n"
                    "    public override void _Process(double delta) {\n"
                    "        %s\n    }\n}\n" % stmt)
        cases = [
            ('if (Input.IsActionPressed("fly")) { }',
             '(4,35): error CS8000: the InputMap has no action "fly"'),
            ('string a = "jump"; if (Input.IsActionPressed(a)) { }',
             "(4,54): error CS8000: an action name must be a string literal"),
            ('if (Input.IsMouseButtonPressed(MouseButton.Left)) { }',
             "(4,13): error CS8000: `Input.IsMouseButtonPressed` (Godot API) "
             "is not packed yet"),
            ('float x = Input.GetJoyAxis(1, JoyAxis.LeftX);',
             "(4,36): error CS8000: joypad device 1 is not packed yet"),
            ('if (Input.IsKeyPressed(Key.F1)) { }',
             "(4,32): error CS8000: `Key.F1` is not a Key that is packed"),
            ('if (Input.IsActionPressed("jump", true)) { }',
             "(4,35): error CS8000: exact_match is not packed yet"),
        ]
        for stmt, want in cases:
            self.assertIn("res://Player.cs" + want,
                          refusal(self, self._project(body(stmt))))
        # An event no host reports is refused at its project.godot line.
        d = self._project(body('float a = Input.GetAxis("move_left", '
                               '"move_right");'),
                          INPUT_MAP.replace('"physical_keycode":65',
                                            '"physical_keycode":4194332'))
        err = refusal(self, d)
        self.assertRegex(err, r"res://project\.godot:\d+: error: action "
                         r"'move_left': key 4194332 is not one a host "
                         r"reports yet")

    def test_input_callbacks_are_refused(self):
        d = self._project("using Godot;\npublic partial class Player : "
                          "Node2D {\n    public override void "
                          "_Input(InputEvent e) { }\n}\n")
        self.assertIn("res://Player.cs(3,", refusal(self, d))


# ---------------------------------------------------------------------------
# Signals: Timers and the scripts' own
# ---------------------------------------------------------------------------

SIG_SCRIPTS = {
    "Player.cs": """using Godot;

public partial class Player : Node2D
{
    [Signal] public delegate void HitEventHandler(int damage, float at, bool crit);
    [Signal] public delegate void ScoredEventHandler();

    private int _frame;

    public override void _Ready()
    {
        Scored += OnScored;
    }

    public override void _Process(double delta)
    {
        _frame = _frame + 1;
        if (_frame == 2) { EmitSignal(SignalName.Hit, 3, 1.5f, true); }
        if (_frame == 4) { EmitSignalHit(7, 2.5f, false); }
        if (_frame == 5) { EmitSignal("Scored"); }
    }

    private void OnScored()
    {
        GD.Print("scored f", _frame);
    }
}
""",
    "Hud.cs": """using Godot;

public partial class Hud : Node2D
{
    private int _hp = 10;

    private void OnPlayerHit(int damage, float at, bool crit)
    {
        _hp = _hp - damage;
        GD.Print("hit ", damage, " at ", at, " hp=", _hp, " crit=", crit);
    }

    private void OnSpawn()
    {
        GD.Print("spawn");
    }

    private void OnOnce()
    {
        GD.Print("once");
    }
}
""",
    "Beat.cs": """using Godot;

public partial class Beat : Timer
{
    private int _n;

    public override void _Ready()
    {
        Timeout += OnTimeout;
    }

    private void OnTimeout()
    {
        _n = _n + 1;
        GD.Print("beat ", _n);
    }
}
""",
}

SIG_SCENE = """[node name="Player" type="Node2D" parent="."]
script = ExtResource("s_Player")

[node name="Hud" type="Node2D" parent="."]
script = ExtResource("s_Hud")

[node name="SpawnTimer" type="Timer" parent="."]
wait_time = 0.04
autostart = true

[node name="Once" type="Timer" parent="."]
wait_time = 0.06
one_shot = true
autostart = true

[node name="Idle" type="Timer" parent="."]

[node name="Beat" type="Timer" parent="."]
wait_time = 0.09
autostart = true
script = ExtResource("s_Beat")

[connection signal="Hit" from="Player" to="Hud" method="OnPlayerHit"]
[connection signal="timeout" from="SpawnTimer" to="Hud" method="OnSpawn"]
[connection signal="timeout" from="Once" to="Hud" method="OnOnce"]
[connection signal="timeout" from="Idle" to="Hud" method="OnOnce"]
"""

TICKS = r"""
#include <stdio.h>
#include "engine_draw.h"
extern float Time_deltaTime;
int main(void) {
    int f;
    Time_deltaTime = 1.f / 60.f;
    for (f = 1; f <= 8; f++) { printf("-- f%d\n", f); engine_tick(); }
    return 0;
}
"""


class TestSignals(unittest.TestCase):

    def _project(self, scene=SIG_SCENE, edits=()):
        scripts = dict(SIG_SCRIPTS)
        for name, old, new in edits:
            if name == "main.tscn":
                self.assertIn(old, scene)
                scene = scene.replace(old, new)
            else:
                self.assertIn(old, scripts[name])
                scripts[name] = scripts[name].replace(old, new)
        return project(self, scene, scripts=scripts)

    @needs_cc
    def test_timers_and_own_signals(self):
        out = pack(self, self._project())
        self.assertEqual(run_c(self, out, TICKS), [
            "-- f1", "-- f2",
            # EmitSignal(SignalName.Hit, ..): a scene connection, with its
            # arguments
            "hit 3 at 1.5 hp=7 crit=True",
            # SpawnTimer, 0.04 s: past 0 on frame 3, then 5 and 8 (the
            # overshoot carries over, as Timer's time_left += wait_time)
            "-- f3", "spawn",
            # EmitSignalHit(..), and the one-shot Timer, once; Timers tick
            # after the scripts' _Process
            "-- f4", "hit 7 at 2.5 hp=0 crit=False", "once",
            # EmitSignal("Scored"): the script's own `Scored += OnScored`
            "-- f5", "scored f5", "spawn",
            # a Timer script's own Timeout; Idle never started
            "-- f6", "beat 1",
            "-- f7", "-- f8", "spawn",
        ])

    def test_refusals(self):
        cases = [
            ([("Player.cs", "ScoredEventHandler()",
               "ScoredEventHandler(string who)")],
             "signal Scored's parameter `string who`: int, float and bool "
             "parameters are packed"),
            ([("Player.cs", 'EmitSignal("Scored")', 'EmitSignal("Missed")')],
             "res://Player.cs(20,28): error CS8000: the script declares no "
             "[Signal] Missed"),
            ([("Player.cs", "EmitSignalHit(7, 2.5f, false)",
               "EmitSignalHit(7)")],
             "res://Player.cs(19,28): error CS8000: signal Hit takes 3 "
             "argument(s), not 1"),
            ([("main.tscn", 'method="OnSpawn"', 'method="OnPlayerHit"')],
             "error: timeout passes (); `OnPlayerHit` takes (int, float, "
             "bool)"),
            ([("main.tscn", 'method="OnPlayerHit"]',
               'method="OnPlayerHit" flags=1]')],
             "error: a deferred connection of Hit is not packed yet"),
            ([("main.tscn", "wait_time = 0.04\n",
               "wait_time = 0.04\nprocess_callback = 0\n")],
             "error: a Timer on the physics process is not packed yet"),
            ([("Beat.cs", "_n = _n + 1;", "_n = _n + 1; Autostart = true;")],
             "error CS8000: `Timer.Autostart` (Godot API) is not packed yet"),
            ([("main.tscn", 'signal="timeout" from="Once"',
               'signal="tree_exited" from="Once"')],
             "error: the tree_exited signal is not packed yet"),
        ]
        for edits, want in cases:
            self.assertIn(want, refusal(self, self._project(edits=edits)))


# ---------------------------------------------------------------------------
# Node references
# ---------------------------------------------------------------------------

REF_SCRIPTS = {
    "Main.cs": """using Godot;

public partial class Main : Node2D
{
    public int Round = 1;
}
""",
    "Player.cs": """using Godot;

public partial class Player : Node2D
{
    public int Hp = 10;

    public void TakeDamage(int d)
    {
        Hp = Hp - d;
        GD.Print("player hp=", Hp);
    }
}
""",
    "Spawner.cs": """using Godot;

public partial class Spawner : Node2D
{
    [Export] public Player Target;
    private Timer _timer;
    private Player _player;
    private Main _main;
    private int _frame;

    public override void _Ready()
    {
        _timer = GetNode<Timer>("SpawnTimer");
        _player = GetNode<Player>("../Player");
        _main = GetParent<Main>();
        Player none = GetNodeOrNull<Player>("Nope");
        GD.Print("px=", _player.Position.X, " gy=", _player.GlobalPosition.Y);
        GD.Print("ready none=", none == null, " round=", _main.Round, " same=", Target == _player);
    }

    public override void _Process(double delta)
    {
        _frame = _frame + 1;
        if (_frame == 2) { _timer.Start(0.045f); GD.Print("start left=", _timer.TimeLeft); }
        if (_frame == 3) { _timer.OneShot = false; _main.Round = _main.Round + 1; }
        if (_frame == 9) { _timer.Stop(); GD.Print("stopped=", _timer.IsStopped()); }
    }

    private void OnSpawn()
    {
        _player.TakeDamage(2);
        GD.Print("spawn f", _frame, " round=", _main.Round, " hp=", _player.Hp, " wait=", _timer.WaitTime);
    }
}
""",
}

REF_SCENE = """[node name="Player" type="Node2D" parent="."]
position = Vector2(5, 7)
script = ExtResource("s_Player")

[node name="Spawner" type="Node2D" parent="."]
script = ExtResource("s_Spawner")
Target = NodePath("../Player")

[node name="SpawnTimer" type="Timer" parent="Spawner"]
one_shot = true

[connection signal="timeout" from="Spawner/SpawnTimer" to="Spawner" method="OnSpawn"]
"""

ENEMY_SCRIPT = """using Godot;

public partial class Enemy : Node2D
{
    private Timer _cd;

    public override void _Ready()
    {
        _cd = GetNode<Timer>("Cooldown");
        Player p = GetNode<Player>("/root/Main/Player");
        Boss b = GetNode<Boss>("%Boss");
        GD.Print(Name, " wait=", _cd.WaitTime, " hp=", p.Hp, " boss=", b.Level);
    }
}
"""

ENEMY_TSCN = """[gd_scene format=3]

[ext_resource type="Script" path="res://Enemy.cs" id="1"]

[node name="Enemy" type="Node2D"]
script = ExtResource("1")

[node name="Cooldown" type="Timer" parent="."]
wait_time = 0.5
"""


class TestNodeRefs(unittest.TestCase):

    def _project(self, edits=()):
        scripts = dict(REF_SCRIPTS)
        scene = REF_SCENE
        for name, old, new in edits:
            if name == "main.tscn":
                self.assertIn(old, scene)
                scene = scene.replace(old, new)
            else:
                self.assertIn(old, scripts[name])
                scripts[name] = scripts[name].replace(old, new)
        d = project(self, scene, scripts=scripts)
        # the Main node runs Main.cs (project() writes a bare root)
        path = os.path.join(d, "main.tscn")
        with open(path) as f:
            text = f.read()
        with open(path, "w") as f:
            f.write(text.replace('[node name="Main" type="Node2D"]\n',
                                 '[node name="Main" type="Node2D"]\n'
                                 'script = ExtResource("s_Main")\n'))
        return d

    @needs_cc
    def test_references_timers_and_calls(self):
        out = pack(self, self._project())
        lines = run_c(self, out, TICKS.replace("f <= 8", "f <= 10"))
        self.assertEqual(lines, [
            "-- f1",
            # another node's position through a reference
            "px=5 gy=7",
            # GetNodeOrNull of no node is null; GetParent<Main>; the
            # exported NodePath is the node GetNode reaches
            "ready none=True round=1 same=True",
            # Timer.start(t): wait_time = t, time_left = wait_time
            "-- f2", "start left=0.045",
            "-- f3",
            # its timeout calls the Spawner, which calls the Player's method
            # and reads its field, and the Main's (written at f3)
            "-- f4", "player hp=8", "spawn f4 round=2 hp=8 wait=0.045",
            # one_shot = false at f3: it runs again
            "-- f5", "-- f6",
            "-- f7", "player hp=6", "spawn f7 round=2 hp=6 wait=0.045",
            "-- f8",
            "-- f9", "stopped=True",
            "-- f10",
        ])

    @needs_cc
    def test_each_instance_resolves_its_own(self):
        d = project(self, (
            '[node name="Player" type="Node2D" parent="."]\n'
            'script = ExtResource("s_Player")\n\n'
            '[node name="Boss" type="Node2D" parent="."]\n'
            'unique_name_in_owner = true\n'
            'script = ExtResource("s_Boss")\n\n'
            '[node name="E1" parent="." instance=ExtResource("e")]\n\n'
            '[node name="E2" parent="." instance=ExtResource("e")]\n\n'
            '[node name="Cooldown" parent="E2"]\nwait_time = 0.25\n'),
            scripts={"Player.cs": REF_SCRIPTS["Player.cs"],
                     "Enemy.cs": ENEMY_SCRIPT,
                     "Boss.cs": "using Godot;\npublic partial class Boss : "
                                "Node2D {\n    public int Level = 3;\n}\n"},
            subs='[ext_resource type="PackedScene" path="res://enemy.tscn" '
                 'id="e"]\n\n')
        with open(os.path.join(d, "enemy.tscn"), "w") as f:
            f.write(ENEMY_TSCN)
        out = pack(self, d)
        # E1's Cooldown and E2's are two Timers; /root/.. and %Boss
        self.assertEqual(sorted(run_c(self, out, TICKS.replace(
            "f <= 8", "f <= 1"))[1:]), [
            "E1 wait=0.5 hp=10 boss=3",
            "E2 wait=0.25 hp=10 boss=3"])

    @needs_cc
    def test_positions_and_godot_types(self):
        """Another node's position as a vector, read and assigned; and
        GetNode<Node2D> / <Sprite2D>: the node's packed class, a script's
        or (without one) the node's own."""
        mover = """using Godot;

public partial class Mover : Node2D
{
    private Player _p;
    private Node2D _any;
    private int _frame;

    public override void _Ready()
    {
        _p = GetNode<Player>("../Player");
        _any = GetNode<Node2D>("../Player");
        Vector2 at = _p.Position;
        GD.Print(Name, " sees ", _p.Name, " at ", at.X, ",", at.Y, " any=", _any.GlobalPosition.X);
    }

    public override void _Process(double delta)
    {
        _frame = _frame + 1;
        if (_frame == 1) { _p.Position = new Vector2(1, 2); }
        if (_frame == 2) { _p.Position += new Vector2(10, 0); }
        if (_frame == 3) { GetNode<Sprite2D>("../Crate").GlobalPosition = new Vector2(_p.Position.X * 2, _p.Position.Y * 2); _p.Position *= 0.5f; }
        Vector2 c = GetNode<Sprite2D>("../Crate").Position;
        GD.Print("f", _frame, " p=", _p.Position.X, ",", _p.Position.Y, " crate=", c.X, ",", c.Y);
    }
}
"""
        d = project(self, (
            '[node name="Player" type="Node2D" parent="."]\n'
            'position = Vector2(5, 7)\nscript = ExtResource("s_Player")\n\n'
            '[node name="Crate" type="Sprite2D" parent="."]\n'
            'position = Vector2(-1, -1)\n\n'
            '[node name="Mover" type="Node2D" parent="."]\n'
            'script = ExtResource("s_Mover")\n'),
            scripts={"Player.cs": REF_SCRIPTS["Player.cs"],
                     "Mover.cs": mover})
        out = pack(self, d)
        self.assertEqual(run_c(self, out, TICKS.replace("f <= 8", "f <= 3")), [
            "-- f1", "Mover sees Player at 5,7 any=5",
            "f1 p=1,2 crate=-1,-1",
            "-- f2", "f2 p=11,2 crate=-1,-1",
            "-- f3", "f3 p=5.5,1 crate=22,4"])

    def test_member_refusals(self):
        body = ("using Godot;\npublic partial class Mover : Node2D {\n"
                "    private Player _p;\n"
                "    public override void _Ready() {\n"
                "        _p = GetNode<Player>(\"../Player\");\n"
                "        %s\n    }\n}\n")
        scene = ('[node name="Player" type="Node2D" parent="."]\n'
                 'script = ExtResource("s_Player")\n\n'
                 '[node name="Crate" type="Sprite2D" parent="."]\n\n'
                 '[node name="Mover" type="Node2D" parent="."]\n'
                 'script = ExtResource("s_Mover")\n')
        cases = [
            ("_p.Rotation = 1.0f;",
             "(6,9): error CS8000: `Player.Rotation` through a reference is "
             "not packed yet"),
            ("_p.Position.X = 3;",
             "(6,9): error CS8000: a component of another node's Position "
             "is not assigned in C# (CS1612)"),
            ("_p.Name = \"x\";", "(6,9): error CS8000: `Player.Name =`"),
            ('Node2D n = GetNode<Node2D>("../Crate"); '
             'n = GetNode<Node2D>("../Player");',
             "error CS8000: `n` is given nodes of classes Crate and Player"),
            ('Area2D a = GetNode<Area2D>("../Crate");',
             '"../Crate" from `Mover` is a Sprite2D, not a Area2D'),
        ]
        for stmt, want in cases:
            d = project(self, scene, scripts={
                "Player.cs": REF_SCRIPTS["Player.cs"],
                "Mover.cs": body % stmt})
            self.assertIn(want, refusal(self, d))
        # two nodes running one script whose reference reaches two classes
        two = ("using Godot;\npublic partial class Mover : Node2D {\n"
               "    public override void _Ready() {\n"
               "        GD.Print(GetNode<Node2D>(\"T\").Position.X);\n"
               "    }\n}\n")
        d = project(self, (
            '[node name="M1" type="Node2D" parent="."]\n'
            'script = ExtResource("s_Mover")\n\n'
            '[node name="T" type="Node2D" parent="M1"]\n\n'
            '[node name="M2" type="Node2D" parent="."]\n'
            'script = ExtResource("s_Mover")\n\n'
            '[node name="T" type="Sprite2D" parent="M2"]\n'
            'script = ExtResource("s_Player")\n'),
            scripts={"Player.cs": REF_SCRIPTS["Player.cs"], "Mover.cs": two})
        self.assertIn("error CS8000: GetNode<Node2D> resolves to Player and T "
                      "from the nodes running this script; one class is "
                      "packed", refusal(self, d))

    def test_refusals(self):
        cases = [
            ([("Spawner.cs", 'GetNode<Player>("../Player")',
               '(Player)GetNode("../Player")')],
             "res://Spawner.cs(14,27): error CS8000: GetNode<T>(..) with "
             "the node's type is packed"),
            ([("Spawner.cs", 'GetNode<Player>("../Player")',
               'GetNode<Player>(PlayerPath)')],
             "(14,35): error CS8000: a node path is packed as a string "
             "literal"),
            ([("Spawner.cs", 'GetNode<Player>("../Player")',
               'GetNode<Player>("../Enemy")')],
             '(14,19): error CS8000: no node "../Enemy" from `Spawner` '
             '(/root/Main/Spawner)'),
            ([("Spawner.cs", 'GetNode<Player>("../Player")',
               'GetNode<Player>("SpawnTimer")')],
             '"SpawnTimer" from `Spawner` is a Timer without a script; a '
             'node reference is packed as its script\'s class, a Godot '
             'type it is, or Timer'),
            ([("Spawner.cs", 'GetNode<Timer>("SpawnTimer")',
               'GetNode<Timer>("../Player")')],
             '(13,18): error CS8000: "../Player" is a Node2D, not a Timer'),
            ([("Spawner.cs", "_main = GetParent<Main>();",
               "_main = GetParent<Main>(); _timer.Timeout += OnSpawn;")],
             "error CS8000: a Timer's timeout is connected in the scene"),
            ([("main.tscn", 'NodePath("../Player")', 'NodePath("../Ghost")')],
             'main.tscn:%d: error: no node "../Ghost" from `Spawner`'),
            ([("Spawner.cs", "_timer.OneShot = false;",
               "_timer.WaitTime += 1;")],
             "error CS8000: `Timer.WaitTime +=` (Godot API) is not packed "
             "yet"),
        ]
        for edits, want in cases:
            d = self._project(edits)
            if "%d" in want:
                want = want % scene_line(d, "Ghost")
            self.assertIn(want, refusal(self, d))


# ---------------------------------------------------------------------------
# Collision layers
# ---------------------------------------------------------------------------

LAYER_SCRIPTS = {
    "Probe.cs": """using Godot;

public partial class Probe : Node2D
{
    private void OnZone(Node2D body)
    {
        GD.Print("zone saw ", body.Name);
    }
}
""",
    "Ball.cs": """using Godot;

public partial class Ball : RigidBody2D
{
    private int _n;

    public override void _PhysicsProcess(double delta)
    {
        _n = _n + 1;
        if (_n == 90) { GD.Print(Name, " y=", Position.Y); }
    }
}
""",
}


def _ball(name, x, extra=""):
    return ('[node name="%s" type="RigidBody2D" parent="."]\n'
            'position = Vector2(%d, 0)\nlock_rotation = true\n%s'
            'script = ExtResource("s_Ball")\n\n'
            '[node name="Shape" type="CollisionShape2D" parent="%s"]\n'
            'shape = SubResource("ball")\n\n' % (name, x, extra, name))


LAYER_SUBS = ('[sub_resource type="RectangleShape2D" id="floor"]\n'
              'size = Vector2(1000, 20)\n\n'
              '[sub_resource type="CircleShape2D" id="ball"]\nradius = 10.0\n\n'
              '[sub_resource type="RectangleShape2D" id="zone"]\n'
              'size = Vector2(1000, 60)\n\n')

LAYER_SCENE = (
    '[node name="Floor" type="StaticBody2D" parent="."]\n'
    'position = Vector2(0, 200)\ncollision_layer = 2\ncollision_mask = 0\n\n'
    '[node name="Shape" type="CollisionShape2D" parent="Floor"]\n'
    'shape = SubResource("floor")\n\n'
    + _ball("BallA", -100)
    + _ball("BallB", 0, "collision_mask = 2\n")
    + _ball("BallC", 100, "collision_layer = 4\ncollision_mask = 2\n") +
    '[node name="Zone" type="Area2D" parent="."]\n'
    'position = Vector2(0, 170)\ncollision_layer = 0\ncollision_mask = 4\n\n'
    '[node name="Shape" type="CollisionShape2D" parent="Zone"]\n'
    'shape = SubResource("zone")\n\n'
    '[node name="Probe" type="Node2D" parent="."]\n'
    'script = ExtResource("s_Probe")\n\n'
    '[connection signal="body_entered" from="Zone" to="Probe" '
    'method="OnZone"]\n')

TICKS_100 = TICKS.replace("f <= 8", "f <= 100").replace(
    'printf("-- f%d\\n", f); ', "")


class TestLayers(unittest.TestCase):

    def test_rows_and_the_one_sided_warning(self):
        d = project(self, LAYER_SCENE, scripts=LAYER_SCRIPTS, subs=LAYER_SUBS)
        with _fast() as err:
            objs = unity_pack.load_project(d)[0]
        col = {o["name"]: o["collider2d"] for o in objs if o.get("collider2d")}
        self.assertEqual({n: (c["godot_layer"], c["godot_mask"])
                          for n, c in col.items()},
                         {"Floor": (2, 0), "BallA": (1, 1), "BallB": (1, 2),
                          "BallC": (4, 2), "Zone": (0, 4)})
        # A and B: A's mask has B's layer, B's has not A's
        self.assertIn("warning: `BallA` and `BallB`: Godot pushes only "
                      "`BallA`", err.getvalue())

    @needs_box2d
    def test_layers_by_godots_rules(self):
        out = pack_physics(self, project(self, LAYER_SCENE,
                                         scripts=LAYER_SCRIPTS,
                                         subs=LAYER_SUBS))
        lines = run_physics(self, out, TICKS_100)
        # the Floor is layer 2: BallA (mask 1) falls through it, BallB and
        # BallC (mask 2) rest on it (its top 190, radius 10); the Zone's
        # mask is 4, BallC's layer
        self.assertEqual(lines[0], "zone saw BallC")
        ys = {l.split()[0]: float(l.split("y=")[1]) for l in lines
              if "y=" in l}
        self.assertGreater(ys["BallA"], 400.0)
        self.assertAlmostEqual(ys["BallB"], 180.0, places=1)
        self.assertAlmostEqual(ys["BallC"], 180.0, places=1)


# ---------------------------------------------------------------------------
# The runtime hierarchy
# ---------------------------------------------------------------------------

HIER_SCRIPTS = {
    "Mover.cs": """using Godot;

public partial class Mover : Node2D
{
    public override void _Process(double delta)
    {
        Position = new Vector2(Position.X + 10, Position.Y);
    }
}
""",
    "Watcher.cs": """using Godot;

public partial class Watcher : Node2D
{
    public int Seen;
}
""",
    "Spy.cs": """using Godot;

public partial class Spy : Node2D
{
    private Watcher _w;
    private int _frame;

    public override void _Ready()
    {
        _w = GetNode<Watcher>("../Holder/Mover/Watcher");
    }

    public override void _Process(double delta)
    {
        _frame = _frame + 1;
        if (_frame == 3) { _w.GlobalPosition = new Vector2(0, 0); }
        GD.Print("f", _frame, " w global=", _w.GlobalPosition.X, ",", _w.GlobalPosition.Y, " local=", _w.Position.X, ",", _w.Position.Y);
    }
}
""",
    "Ball.cs": """using Godot;

public partial class Ball : RigidBody2D
{
    private int _n;

    public override void _PhysicsProcess(double delta)
    {
        _n = _n + 1;
        if (_n == 4) { GD.Print("ball ", GlobalPosition.X, ",", GlobalPosition.Y); }
    }
}
""",
}

HIER_SCENE = """[node name="Holder" type="Node2D" parent="."]
position = Vector2(10, 0)
scale = Vector2(2, 2)

[node name="Mover" type="Node2D" parent="Holder"]
position = Vector2(50, 0)
script = ExtResource("s_Mover")

[node name="Tail" type="Sprite2D" parent="Holder/Mover"]
position = Vector2(5, 0)
texture = ExtResource("1")

[node name="Cam" type="Camera2D" parent="Holder/Mover/Tail"]

[node name="Watcher" type="Node2D" parent="Holder/Mover"]
position = Vector2(0, 3)
script = ExtResource("s_Watcher")

[node name="Ball" type="RigidBody2D" parent="Holder/Mover"]
position = Vector2(0, 50)
gravity_scale = 0.0
lock_rotation = true
script = ExtResource("s_Ball")

[node name="Shape" type="CollisionShape2D" parent="Holder/Mover/Ball"]
shape = SubResource("c")

[node name="Spy" type="Node2D" parent="."]
script = ExtResource("s_Spy")
"""

HIER_DRIVE = r"""
#include <stdio.h>
#include "engine_draw.h"
extern float Time_deltaTime, Camera_main_pos_x;
int main(void) {
    EngineDraw b[8]; int f, n;
    Time_deltaTime = 1.f / 60.f;
    for (f = 1; f <= 4; f++) {
        engine_tick();
        n = engine_collect_draws(b, 8);
        printf("f%d tail=%g,%g half=%g cam=%g\n", f, n ? b[0].x : -1.f,
               n ? b[0].y : -1.f, n ? b[0].half_w : -1.f, Camera_main_pos_x);
    }
    return 0;
}
"""


class TestHierarchy(unittest.TestCase):

    def test_links(self):
        d = project(self, HIER_SCENE, scripts=HIER_SCRIPTS,
                    subs='[sub_resource type="CircleShape2D" id="c"]\n'
                         'radius = 4.0\n\n')
        objs = {o["name"]: o for o in load(d)[0]}
        # a child's parent is its node's; its position is local, in the
        # parent's frame (the parent's global rotation and scale)
        self.assertEqual(objs["Mover"]["father_id"], "godot:0:Holder")
        self.assertEqual(objs["Mover"]["godot_parent_basis"],
                         (2.0, 0.0, 0.0, 2.0))
        self.assertEqual(objs["Tail"]["father_id"], "godot:0:Holder/Mover")
        # a body is simulated in the world: no parent
        self.assertNotIn("father_id", objs["Ball"])
        # the scene root is a Node2D too: Holder's parent
        self.assertEqual(objs["Holder"]["father_id"], "godot:0:")
        self.assertEqual(objs["Holder"]["xf_id"], "godot:0:Holder")

    @needs_box2d
    def test_children_follow_and_global_positions(self):
        d = project(self, HIER_SCENE, scripts=HIER_SCRIPTS,
                    subs='[sub_resource type="CircleShape2D" id="c"]\n'
                         'radius = 4.0\n\n')
        out = pack_physics(self, d)
        self.assertEqual(run_physics(self, out, HIER_DRIVE), [
            # Mover's world: Holder (10, 0) + 2 x its local (60, 0); Spy
            # runs after it, and reads Watcher's through it: + 2 x (0, 3)
            "f1 w global=130,6 local=0,3",
            # Tail: Mover's + 2 x (5, 0), drawn scaled 2; the camera, a
            # child of Tail, follows it
            "f1 tail=140,0 half=4 cam=140",
            "f2 w global=150,6 local=0,3",
            "f2 tail=160,0 half=4 cam=160",
            # Watcher put at the world's origin: its local is what does
            # that under Mover (170, 0) scaled 2 -- (-85, 0)
            "f3 w global=0,0 local=-85,0",
            "f3 tail=180,0 half=4 cam=180",
            # the body did not move with its parent: Holder + 2 x (50, 50)
            # (its _PhysicsProcess, before frame 4's _Process)
            "ball 110,100",
            # and Watcher moves with Mover from where it was put
            "f4 w global=20,0 local=-85,0",
            "f4 tail=200,0 half=4 cam=200",
        ])


# ---------------------------------------------------------------------------
# A freed node's body
# ---------------------------------------------------------------------------

FREE_SCRIPTS = {
    "Floor.cs": """using Godot;

public partial class Floor : StaticBody2D
{
    private int _n;

    public override void _PhysicsProcess(double delta)
    {
        _n = _n + 1;
        if (_n == 30) { QueueFree(); }
    }
}
""",
    "Ball.cs": LAYER_SCRIPTS["Ball.cs"].replace(
        "if (_n == 90)", "if (_n == 29 || _n == 60)").replace(
        'GD.Print(Name, " y=", Position.Y);',
        'GD.Print(Name, " n=", _n, " y=", Position.Y);'),
    "Zone.cs": """using Godot;

public partial class Zone : Area2D
{
    private int _n;

    public override void _PhysicsProcess(double delta)
    {
        _n = _n + 1;
        if (_n == 10) { QueueFree(); }
    }
}
""",
}

FREE_SCENE = (
    '[node name="Floor" type="StaticBody2D" parent="."]\n'
    'position = Vector2(0, 200)\nscript = ExtResource("s_Floor")\n\n'
    '[node name="Shape" type="CollisionShape2D" parent="Floor"]\n'
    'shape = SubResource("floor")\n\n'
    '[node name="Plank" type="StaticBody2D" parent="Floor"]\n'
    'position = Vector2(1200, 0)\n\n'
    '[node name="Shape" type="CollisionShape2D" parent="Floor/Plank"]\n'
    'shape = SubResource("floor")\n\n'
    '[node name="Ball" type="RigidBody2D" parent="."]\n'
    'position = Vector2(0, 175)\nlock_rotation = true\n'
    'script = ExtResource("s_Ball")\n\n'
    '[node name="Shape" type="CollisionShape2D" parent="Ball"]\n'
    'shape = SubResource("ball")\n\n'
    '[node name="Ball2" type="RigidBody2D" parent="."]\n'
    'position = Vector2(1100, 175)\nlock_rotation = true\n'
    'script = ExtResource("s_Ball")\n\n'
    '[node name="Shape" type="CollisionShape2D" parent="Ball2"]\n'
    'shape = SubResource("ball")\n\n'
    '[node name="Keep" type="StaticBody2D" parent="."]\n'
    'position = Vector2(300, 200)\n\n'
    '[node name="Shape" type="CollisionShape2D" parent="Keep"]\n'
    'shape = SubResource("ball")\n\n'
    '[node name="Rest" type="RigidBody2D" parent="."]\n'
    'position = Vector2(300, 175)\nlock_rotation = true\n'
    'script = ExtResource("s_Ball")\n\n'
    '[node name="Shape" type="CollisionShape2D" parent="Rest"]\n'
    'shape = SubResource("ball")\n\n'
    '[node name="Zone" type="Area2D" parent="Rest"]\n'
    'script = ExtResource("s_Zone")\n\n'
    '[node name="Shape" type="CollisionShape2D" parent="Rest/Zone"]\n'
    'shape = SubResource("zone")\n')


class TestFreed(unittest.TestCase):

    @needs_box2d
    def test_a_freed_body_leaves_the_world(self):
        out = pack_physics(self, project(self, FREE_SCENE,
                                         scripts=FREE_SCRIPTS,
                                         subs=LAYER_SUBS))
        with open(os.path.join(out, "engine.c")) as f:
            self.assertIn("!_engine_go_destroyed[go]", f.read())
        lines = run_physics(self, out, TICKS_100)
        ys = {tuple(l.split()[:2]): float(l.split("y=")[1]) for l in lines}
        # the Floor, freed on its 30th step, no longer holds the Ball: it
        # rests on it (its top 190, radius 10) and then falls 30 steps
        self.assertAlmostEqual(ys[("Ball", "n=29")], 180.0, places=1)
        self.assertGreater(ys[("Ball", "n=60")], 290.0)
        # the Plank, a child of the Floor, is freed with it (queue_free
        # frees the subtree): Ball2 on it falls too
        self.assertAlmostEqual(ys[("Ball2", "n=29")], 180.0, places=1)
        self.assertGreater(ys[("Ball2", "n=60")], 290.0)
        # freeing the Area2D (a sensor) under Rest does not drop Rest
        self.assertAlmostEqual(ys[("Rest", "n=60")], 180.0, places=1)


# ---------------------------------------------------------------------------
# Vector2 arithmetic and methods
# ---------------------------------------------------------------------------

VEC_SCRIPT = """using Godot;

public partial class Vec : Node2D
{
    private Vector2 _vel = new Vector2(3, 4);

    public override void _Ready()
    {
        Vector2 a = new Vector2(1, 2);
        Vector2 b = a + a;
        Vector2 c = b * 2f;
        Vector2 d = -c;
        Vector2 e = (a - b) / 2f + _vel * 0.5f;
        Vector2 f = 2f * a * new Vector2(3, -1);
        GD.Print("ops ", b.X, ",", b.Y, " ", c.X, ",", c.Y, " ", d.X, ",", d.Y, " ", e.X, ",", e.Y, " ", f.X, ",", f.Y);
        f += a;
        f -= new Vector2(1, 1);
        f *= 0.5f;
        f /= 2f;
        GD.Print("assign ", f.X, ",", f.Y);
        Position = Position + _vel * 2f;
        Position += Vector2.One;
        GD.Print("pos ", Position.X, ",", Position.Y, " eq=", Position == new Vector2(17, 29), " ne=", Position != a);
        Vector2 n = new Vector2(3, 4).Normalized();
        Vector2 tiny = new Vector2(0.000001f, 0).Normalized();
        GD.Print("norm ", n.X, ",", n.Y, " tiny=", tiny.X, " len=", new Vector2(3, 4).Length(), " lsq=", _vel.LengthSquared());
        Vector2 x = Vector2.Right;
        Vector2 y = Vector2.Down;
        GD.Print("dot ", x.Dot(y), " cross=", x.Cross(y), " dist=", Vector2.Zero.DistanceTo(_vel), " d2=", Vector2.Zero.DistanceSquaredTo(_vel));
        GD.Print("angle ", y.Angle() > 1.5707f && y.Angle() < 1.5709f, " to=", x.AngleTo(y) > 1.5707f && x.AngleTo(y) < 1.5709f, " up=", Vector2.Up.Y);
        Vector2 dir = Vector2.Zero.DirectionTo(new Vector2(0, 5));
        Vector2 l = Vector2.Zero.Lerp(new Vector2(10, 20), 1.5f);
        Vector2 m1 = Vector2.Zero.MoveToward(new Vector2(10, 0), 3f);
        Vector2 m2 = Vector2.Zero.MoveToward(new Vector2(1, 0), 3f);
        GD.Print("dir ", dir.X, ",", dir.Y, " lerp=", l.X, ",", l.Y, " mt=", m1.X, ",", m2.X);
        Vector2 r = x.Rotated(0.5f);
        Vector2 lim = new Vector2(6, 8).LimitLength(5f);
        Vector2 keep = new Vector2(0.3f, 0.4f).LimitLength();
        Vector2 ab = new Vector2(-1, 2).Abs();
        GD.Print("rot ", r.X > 0.8775f && r.X < 0.8776f, " lim=", lim.X, ",", lim.Y, " keep=", keep.X, " abs=", ab.X, ",", ab.Y);
        Vector2 near = new Vector2(1.0000005f, 1);
        GD.Print("approx ", new Vector2(0.0000001f, 0).IsZeroApprox(), " ", near.IsEqualApprox(Vector2.One), " exact=", near == Vector2.One);
        GD.Print("chain ", (Vector2.Left * 3f).Normalized().Rotated(0f).X);
    }
}
"""


class TestVectors(unittest.TestCase):

    def _project(self, script=VEC_SCRIPT):
        return project(self, '[node name="Vec" type="Node2D" parent="."]\n'
                       'position = Vector2(10, 20)\n'
                       'script = ExtResource("s_Vec")\n',
                       scripts={"Vec.cs": script})

    @needs_cc
    def test_arithmetic_and_methods_by_godots_rules(self):
        out = pack(self, self._project())
        with _fast() as err:
            pass
        self.assertEqual(run_c(self, out, TICKS.replace("f <= 8", "f <= 1")
                               .replace('printf("-- f%d\\n", f); ', "")), [
            # a + a, * 2, unary -, (a - b) / 2 + v * 0.5, s * a * b
            "ops 2,4 4,8 -4,-8 1,1 6,-4",
            # += a, -= (1,1), *= 0.5, /= 2: ((6,-4)+(1,2)-(1,1))/4
            "assign 1.5,-0.75",
            # Position + v * 2, += One: (10,20) + (6,8) + (1,1); == is exact
            "pos 17,29 eq=True ne=True",
            # a vector 1e-6 long still normalizes (only zero stays zero)
            "norm 0.6,0.8 tiny=1 len=5 lsq=25",
            "dot 0 cross=1 dist=5 d2=25",
            # Down's angle is pi/2 (y down); Up is (0, -1)
            "angle True to=True up=-1",
            # Lerp is not clamped; MoveToward stops at the target
            "dir 0,1 lerp=15,30 mt=3,1",
            "rot True lim=3,4 keep=0.3 abs=1,2",
            # IsEqualApprox's relative 1e-6; == is exact
            "approx True True exact=False",
            "chain -1",
        ])

    def test_method_refusals(self):
        body = ("using Godot;\npublic partial class Vec : Node2D {\n"
                "    public override void _Ready() {\n        %s\n    }\n}\n")
        cases = [
            ("Vector2 v = Vector2.Inf;",
             "(4,21): error CS8000: `Vector2.Inf` (Godot API) is not packed "
             "yet"),
            ("float d = Vector2.Zero.Dot();",
             "error CS8000: Vector2.Dot takes 1 argument(s)"),
        ]
        for stmt, want in cases:
            self.assertIn(want, refusal(self, self._project(body % stmt)))


# ---------------------------------------------------------------------------
# The 2D GPU path (--gpu-batch): one atlas, one draw call, the effects
# ---------------------------------------------------------------------------

GPU_SCENE = """[node name="Rot" type="Sprite2D" parent="."]
position = Vector2(12, 12)
rotation = 1.5707964
scale = Vector2(3, 3)
texture = ExtResource("1")
flip_h = true

[node name="VFlip" type="Sprite2D" parent="."]
position = Vector2(30, 10)
scale = Vector2(3, 3)
texture = ExtResource("1")
flip_v = true

[node name="Frame" type="Sprite2D" parent="."]
position = Vector2(50, 10)
scale = Vector2(4, 4)
texture = ExtResource("2")
hframes = 4
vframes = 2
frame = 6

[node name="Region" type="Sprite2D" parent="."]
position = Vector2(70, 10)
scale = Vector2(3, 3)
texture = ExtResource("2")
region_enabled = true
region_rect = Rect2(0, 0, 6, 4)

[node name="Corner" type="Sprite2D" parent="."]
position = Vector2(4, 30)
scale = Vector2(3, 3)
texture = ExtResource("1")
centered = false
offset = Vector2(1, 1)
modulate = Color(1, 1, 1, 0.5)

[node name="Under" type="Sprite2D" parent="."]
position = Vector2(40, 40)
scale = Vector2(5, 5)
texture = ExtResource("2")
z_index = 2

[node name="Over" type="Sprite2D" parent="."]
position = Vector2(44, 42)
scale = Vector2(3, 3)
texture = ExtResource("1")
self_modulate = Color(0, 1, 0, 1)
z_index = 1

[node name="Holder" type="Node2D" parent="."]
position = Vector2(70, 40)
rotation = 0.5
scale = Vector2(2, 2)

[node name="Mover" type="Sprite2D" parent="Holder"]
texture = ExtResource("1")
script = ExtResource("s_Mover")
"""

FX_SCRIPTS = {
    "Hurt.cs": """using Godot;

public partial class Hurt : Node2D
{
    private int _f;

    public override void _Process(double delta)
    {
        _f = _f + 1;
        if (_f == 1) { SpriteEffects2D.Set(this, SpriteEffect2D.Flash, 1f); }
        if (_f == 1) { SpriteEffects2D.Set(GetNode<Enemy>("../Enemy"), SpriteEffect2D.Grayscale, 0.5f); }
        if (_f == 2) { SpriteEffects2D.Clear(this); }
    }
}
""",
    "Enemy.cs": ("using Godot;\npublic partial class Enemy : Sprite2D\n{\n"
                 "    public int Hp = 3;\n}\n"),
}

FX_SCENE = """[node name="Body" type="Node2D" parent="."]
position = Vector2(20, 20)
script = ExtResource("s_Hurt")

[node name="Look" type="Sprite2D" parent="Body"]
texture = ExtResource("1")

[node name="Enemy" type="Sprite2D" parent="."]
position = Vector2(60, 20)
texture = ExtResource("1")
script = ExtResource("s_Enemy")

[node name="Plain" type="Sprite2D" parent="."]
position = Vector2(80, 20)
texture = ExtResource("1")
"""

FX_DUMP = r"""
#include <stdio.h>
#include "engine_draw.h"
extern float Time_deltaTime;
int main(void) {
    EngineGpuSprite s[16]; int f, n, k;
    Time_deltaTime = 1.f / 60.f;
    for (f = 1; f <= 2; f++) {
        engine_tick();
        n = engine_collect_gpu_sprites(s, 16);
        for (k = 0; k < n; k++)
            printf("f%d x=%g fx=%d arg=%d\n", f, s[k].x, s[k].effect, s[k].arg);
    }
    return 0;
}
"""


def _fx_project(test):
    scripts = dict(FX_SCRIPTS)
    with open(os.path.join(ROOT, "examples", "unity_pack",
                           "SpriteEffects2D.Godot.cs")) as f:
        scripts["SpriteEffects2D.cs"] = f.read()
    return project(test, FX_SCENE, scripts=scripts, viewport=VIEW)


def pack_gpu(test, project_dir):
    out = tempfile.mkdtemp(prefix="gpf-")
    test.addCleanup(shutil.rmtree, out, True)
    with _fast():
        unity_pack.pack(project_dir, out, force=True, gpu_batch=True)
    return out


def gles3_frame(test, out, batch, ticks=None):
    """gles3_view.c's frame of *out* (per sprite, or -DBATCH: the atlas and
    one draw call), as (w, h, RGB bytes); skips without GLES 3.1."""
    e = os.path.join(ROOT, "examples", "unity_pack")
    exe = os.path.join(out, "view_b" if batch else "view_s")
    cmd = [_CC, "-O0", "-w", "-I", out, "-I", e, "-I",
           os.path.join(e, "include"), "-o", exe,
           os.path.join(e, "gles3_view.c"), os.path.join(out, "engine.c"),
           os.path.join(out, "data.c"), "-lEGL", "-lGLESv2", "-lm"]
    if batch:
        cmd.insert(1, "-DBATCH")
    if ticks is not None:
        cmd.insert(1, "-DTICKS_BEFORE_DRAW=%d" % ticks)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        test.skipTest("cannot link the GLES3 view: %s" % r.stderr[-300:])
    ppm = exe + ".ppm"
    env = dict(os.environ, EGL_PLATFORM="surfaceless",
               LIBGL_ALWAYS_SOFTWARE="1")
    run = subprocess.run([exe, ppm], capture_output=True, text=True, env=env)
    if run.returncode != 0 or not os.path.isfile(ppm):
        test.skipTest("no GLES 3.1 rasteriser: %s"
                      % (run.stderr or run.stdout)[-300:])
    with open(ppm, "rb") as f:
        _magic, dims, _maxv, px = f.read().split(b"\n", 3)
    w, h = map(int, dims.split())
    return w, h, px


def _at(frame, x, y):
    w, _h, px = frame
    i = (y * w + x) * 3
    return tuple(px[i:i + 3])


class TestGpuBatch(unittest.TestCase):

    @needs_cc
    def test_effects_on_a_node_and_its_children(self):
        out = pack_gpu(self, _fx_project(self))
        # Set(this): Body's child sprite (Look, x 20) flashes, and Clear
        # clears it; Set(a reference): Enemy (x 60) at 0.5 (128); Plain none
        self.assertEqual(run_c(self, out, FX_DUMP), [
            "f1 x=20 fx=1 arg=255", "f1 x=60 fx=2 arg=128",
            "f1 x=80 fx=0 arg=0",
            "f2 x=20 fx=0 arg=0", "f2 x=60 fx=2 arg=128",
            "f2 x=80 fx=0 arg=0"])

    @needs_cc
    def test_one_draw_call_draws_the_same_frame(self):
        """The batch shader's frame is the per-sprite renderer's, byte for
        byte: rotation with flip_h, flip_v, sprite-sheet frames, a region,
        offset / centered, modulate alpha, z_index over tree order, a
        sprite under a scaled, turned parent that moves."""
        out = pack_gpu(self, project(self, GPU_SCENE, viewport=VIEW))
        self.assertTrue(os.path.isfile(os.path.join(out, "atlas0.png")))
        per_sprite = gles3_frame(self, out, batch=False)
        batch = gles3_frame(self, out, batch=True)
        self.assertEqual(per_sprite[:2], (96, 64))
        self.assertEqual(batch[2], per_sprite[2])
        # Rot: turned 90 degrees clockwise, the image's top (red) is to the
        # right, its bottom (blue) to the left
        self.assertEqual(_at(batch, 14, 12), (255, 0, 0))
        self.assertEqual(_at(batch, 10, 12), (0, 0, 255))

    @needs_cc
    def test_the_shader_draws_the_effects(self):
        out = pack_gpu(self, _fx_project(self))
        frame = gles3_frame(self, out, batch=True, ticks=1)
        self.assertEqual(_at(frame, 19, 19), (255, 255, 255))  # flashed
        r, g, b = _at(frame, 59, 19)                             # half gray
        self.assertTrue(r < 255 and g > 0 and g == b, (r, g, b))
        self.assertEqual(_at(frame, 79, 19), (255, 0, 0))        # untouched


if __name__ == "__main__":
    unittest.main()
