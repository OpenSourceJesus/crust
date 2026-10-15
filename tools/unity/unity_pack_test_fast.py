#!/usr/bin/env python3
"""Fast tests for unity_pack's newer fixes.

tests/test_unity_pack.py packs every project through cpprust + crust to
re-validate the emitted C, which costs seconds a pack; its full run takes
about ten minutes. Here the module patches that re-validation out (as
godot_pack_test_fast.py does): a test that needs the engine's behaviour
compiles engine.c with the host compiler and runs it -- which also fails on
anything the validation would, an undeclared helper or a type error. A pack
that holds a C# string (a coost fastring) is still lowered for real.

New fixes add their tests here. Run:

    python3 tools/unity/unity_pack_test_fast.py            # everything
    python3 tools/unity/unity_pack_test_fast.py TestLineRenderer
"""
import contextlib
import io
import os
import shutil
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

import tools.unity.unity_pack as unity_pack  # noqa: E402

_CC = shutil.which("gcc") or shutil.which("cc")
needs_cc = unittest.skipIf(_CC is None, "no C compiler")
# A C# string is a coost fastring: coost cloned beside this repository, or
# COOST_ROOT (as tests/test_unity_pack.py's string tests need).
needs_coost = unittest.skipUnless(
    os.environ.get("COOST_ROOT")
    or os.path.isdir(os.path.join(os.path.dirname(ROOT), "coost")),
    "coost not found: clone https://github.com/crustos/coost beside crust")

_saved_validate = []


def setUpModule():
    saved = unity_pack.validate_emitted_c
    _saved_validate.append(saved)

    def validate(text, *a, **k):
        # a C# string is a coost fastring, a List a std::vector: C++ only
        # cpprust lowers to C
        if "fastring" in text or "std::vector" in text:
            return saved(text, *a, **k)
        return None
    unity_pack.validate_emitted_c = validate


def tearDownModule():
    unity_pack.validate_emitted_c = _saved_validate.pop()


# ---- shared helpers for new tests -------------------------------------------

def project(test, scripts, scene_objects):
    """A Unity project: *scripts* {name: C# source}, and a scene of
    *scene_objects*, each (script name, extra component YAML with `{go}`
    and `{fid}` placeholders, extra MonoBehaviour field YAML, rotation
    quaternion or None). Every object gets a GameObject, a Transform and
    its script; returns the project root."""
    root = tempfile.mkdtemp(prefix="upf-")
    test.addCleanup(shutil.rmtree, root, True)
    sd = os.path.join(root, "Assets", "Scripts")
    os.makedirs(sd)
    guids = {}
    for k, (name, src) in enumerate(sorted(scripts.items())):
        guids[name] = "%032x" % (k + 1)
        with open(os.path.join(sd, name + ".cs"), "w") as f:
            f.write(src)
        with open(os.path.join(sd, name + ".cs.meta"), "w") as f:
            f.write("guid: %s\n" % guids[name])
    scene, fid = "%YAML 1.1\n", 100
    for k, obj in enumerate(scene_objects):
        name, extra, fields, rot = (tuple(obj) + (None, None, None))[:4]
        extra = (extra or "").format(go=fid, fid=fid + 3)
        comps = [fid + 1, fid + 2] + ([fid + 3] if extra else [])
        q = rot or (0, 0, 0, 1)
        scene += ("--- !u!1 &%d\nGameObject:\n  m_Name: %s%d\n  m_Component:\n"
                  % (fid, name, k)
                  + "".join("  - component: {fileID: %d}\n" % c for c in comps)
                  + "--- !u!4 &%d\nTransform:\n  m_GameObject: {fileID: %d}\n"
                    "  m_LocalRotation: {x: %r, y: %r, z: %r, w: %r}\n"
                    "  m_LocalPosition: {x: %d, y: 0, z: 0}\n"
                  % (fid + 1, fid, q[0], q[1], q[2], q[3], k)
                  + "--- !u!114 &%d\nMonoBehaviour:\n  m_GameObject: {fileID: %d}\n"
                    "  m_Script: {fileID: 11500000, guid: %s}\n%s"
                  % (fid + 2, fid, guids[name], fields or "")
                  + extra)
        fid += 10
    os.makedirs(os.path.join(root, "Assets", "Scenes"))
    with open(os.path.join(root, "Assets", "Scenes", "S.unity"), "w") as f:
        f.write(scene)
    return root


def pack(test, root, strict=True):
    """Pack quietly (strict: a stub is an error); the output directory."""
    out = tempfile.mkdtemp(prefix="upf-out-")
    test.addCleanup(shutil.rmtree, out, True)
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        unity_pack.pack(root, out, force=True, strict=strict)
    return out


def run_frames(test, out, frames=1, body="", pre=""):
    """Run the packed engine *frames* ticks at 60 fps (then *body*, C over
    engine_draw.h; *pre*: C before main) and return its stdout lines
    (Debug.Log included)."""
    src = os.path.join(out, "h.c")
    with open(src, "w") as f:
        f.write('#include <stdio.h>\n#include "engine_draw.h"\n'
                "extern float Time_deltaTime;\n" + pre +
                "int main(int c, char **v) { int f;\n"
                "  engine_apply_argv(c, v); Time_deltaTime = 1.f / 60.f;\n"
                "  for (f = 0; f < %d; f++) engine_tick();\n  %s\n  return 0; }\n"
                % (frames, body))
    exe = os.path.join(out, "h")
    r = subprocess.run([_CC, "-O1", "-w", "-I", out, "-o", exe, src,
                        os.path.join(out, "engine.c"), os.path.join(out, "data.c"),
                        "-lm"], capture_output=True, text=True)
    test.assertEqual(r.returncode, 0, r.stderr[-2000:])
    run = subprocess.run([exe, "-logFile", "-"], capture_output=True, text=True,
                         timeout=60)
    test.assertEqual(run.returncode, 0, run.stderr[-2000:])
    return run.stdout.splitlines()


def script(name, body, members=""):
    """A MonoBehaviour *name* whose first Update runs *body* once."""
    return ("using UnityEngine;\npublic class %s : MonoBehaviour {\n%s\n"
            "    int _f;\n    void Update() {\n        _f++; if (_f > 1) return;\n"
            "        %s\n    }\n}\n" % (name, members, body))


# ---- tests --------------------------------------------------------------------

class TestParticleSystems(unittest.TestCase):
    """A scene with a ParticleSystem, and the script forms that reach it.

    Every such project failed to pack -- the simulation calls `cosf` and
    `sinf`, and `<math.h>` was included by a feature list particles were not
    on -- and the static-call refusal had been switched off, so an invented
    `ParticleSystem.X(..)` emptied its method with only a warning."""

    SCENE = (
        "%%YAML 1.1\n--- !u!1 &1\nGameObject:\n  m_Name: Spark\n  m_Component:\n"
        "  - component: {fileID: 2}\n  - component: {fileID: 3}\n"
        "  - component: {fileID: 4}\n--- !u!4 &2\nTransform:\n"
        "  m_GameObject: {fileID: 1}\n  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
        "--- !u!114 &3\nMonoBehaviour:\n  m_GameObject: {fileID: 1}\n"
        "  m_Script: {fileID: 11500000, guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}\n"
        "%s--- !u!198 &4\nParticleSystem:\n  m_GameObject: {fileID: 1}\n")

    def _project(self, script, field=None):
        root = tempfile.mkdtemp(prefix="upack-ps-")
        self.addCleanup(shutil.rmtree, root, True)
        s = os.path.join(root, "Assets", "Scripts")
        os.makedirs(s)
        with open(os.path.join(s, "Spark.cs"), "w") as f:
            f.write(script)
        with open(os.path.join(s, "Spark.cs.meta"), "w") as f:
            f.write("guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
        sc = os.path.join(root, "Assets", "Scenes")
        os.makedirs(sc)
        with open(os.path.join(sc, "S.unity"), "w") as f:
            f.write(self.SCENE % (("  %s: {fileID: 4}\n" % field) if field else ""))
        return root

    def _pack(self, root):
        out = tempfile.mkdtemp(prefix="upack-ps-out-")
        self.addCleanup(shutil.rmtree, out, True)
        unity_pack.pack(root, out, force=True, strict=True)  # a stub fails
        return out

    def _script(self, members):
        return ("using UnityEngine;\npublic class Spark : MonoBehaviour {\n"
                + members + "\n}\n")

    def test_instance_forms_lower(self):
        for members, field in (
                ("    public ParticleSystem ps;\n"
                 "    public void Update() { ps.Emit(3); }", "ps"),
                # "Color Color": the name means the field, not the type
                ("    public ParticleSystem ParticleSystem;\n"
                 "    public void Update() { ParticleSystem.Emit(3); }",
                 "ParticleSystem"),
                ("    public void Update() { GetComponent<ParticleSystem>().Emit(2); }",
                 None)):
            out = self._pack(self._project(self._script(members), field))
            with open(os.path.join(out, "engine.c")) as f:
                self.assertIn("ParticleSystem_Emit(", f.read(), members)

    def test_a_unity_stub_is_still_a_warning_by_default(self):
        # Godot packs strict by default; Unity keeps its documented warning
        # until the move to cs2cpp is done (UNITY_PACK.md).
        root = self._project(self._script(
            "    public void Update() { Foo.Bar(); }"))
        out = tempfile.mkdtemp(prefix="upack-ps-out-")
        self.addCleanup(shutil.rmtree, out, True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            unity_pack.pack(root, out, force=True)
        self.assertIn("warning CS8000", err.getvalue())

    def test_static_calls_are_refused_as_csc_would(self):
        for call, code in (("ParticleSystem.Emit(0f, 0f)", "CS0120"),
                           ("ParticleSystem.Explode()", "CS0117")):
            root = self._project(self._script(
                "    public void Update() { %s; }" % call))
            with self.assertRaises(unity_pack.PackError) as cm:
                self._pack(root)
            self.assertIn("error %s" % code, cm.exception.message)
            self.assertIn("Spark.cs(3,", cm.exception.message)

    @needs_cc
    def test_emit_stop_play_counts(self):
        root = self._project(self._script("""
    public ParticleSystem ps;
    int f;
    void Start() { ps.Stop(); ps.Clear(); }
    void Update() {
        f++;
        if (f == 2) ps.Emit(3);
        if (f == 5) ps.Play();
        if (f == 1 || f == 4 || f == 65) Debug.Log("n=" + ps.particleCount);
    }"""), "ps")
        out = self._pack(root)
        src = os.path.join(out, "harness.c")
        with open(src, "w") as f:
            f.write('#include "engine_draw.h"\nextern float Time_deltaTime;\n'
                    "int main(int argc, char **argv) { int f;\n"
                    "  engine_apply_argv(argc, argv); Time_deltaTime = 1.f / 60.f;\n"
                    "  for (f = 1; f <= 66; f++) engine_tick(); return 0; }\n")
        exe = os.path.join(out, "h")
        r = subprocess.run([_CC, "-O0", "-w", "-I", out, "-o", exe, src,
                            os.path.join(out, "engine.c"),
                            os.path.join(out, "data.c"), "-lm"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        run = subprocess.run([exe, "-logFile", "-"], capture_output=True,
                             text=True, timeout=60)
        # stopped and cleared; three emitted while stopped (5 s lifetime);
        # then one second of the default 10/s after Play
        self.assertEqual(run.stdout.split(), ["n=0", "n=3", "n=13"])


class TestAnimationCurves(unittest.TestCase):
    """A script's AnimationCurve field: read from the scene and evaluated
    as Unity evaluates it. It used to be packed as a reference to a class
    named `AnimationCurve` -- always null, its keys dropped."""

    TS = [-0.6, 0.0, 0.25, 0.5, 0.9, 1.0, 1.5, 2.25, 3.7]
    CURVES = {
        1: ([(0, 0, 0, 0), (1, 1, 0, 0)], 8, 8),                  # EaseInOut, clamp
        2: ([(0, 0, 0, 3), (1, 2, 1, "Infinity"), (2, 5, -1, 0)], 2, 4),  # Hermite, step; loop / pingpong
    }

    @staticmethod
    def _key(t, v, i, o, weighted=0):
        return ("    - serializedVersion: 3\n      time: %s\n      value: %s\n"
                "      inSlope: %s\n      outSlope: %s\n      tangentMode: 0\n"
                "      weightedMode: %d\n      inWeight: 0.33333334\n"
                "      outWeight: 0.33333334\n" % (t, v, i, o, weighted))

    def _project(self, curves, weighted=0):
        script = ("using UnityEngine;\npublic class Mover : MonoBehaviour {\n"
                  "    public AnimationCurve speed;\n    public int tag;\n    int f;\n"
                  "    void Update() {\n        f++;\n        if (f != 1) return;\n"
                  "        Debug.Log(\"c\" + tag + \" n=\" + speed.length);\n"
                  + "".join("        Debug.Log(\"c\" + tag + \" \" + speed.Evaluate(%rf));\n" % t
                            for t in self.TS)
                  + "    }\n}\n")
        scene = "%YAML 1.1\n"
        for n, (keys, pre, post) in sorted(curves.items()):
            fid = 10 * n
            scene += (
                "--- !u!1 &%d\nGameObject:\n  m_Name: M%d\n  m_Component:\n"
                "  - component: {fileID: %d}\n  - component: {fileID: %d}\n"
                "--- !u!4 &%d\nTransform:\n  m_GameObject: {fileID: %d}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &%d\nMonoBehaviour:\n  m_GameObject: {fileID: %d}\n"
                "  m_Script: {fileID: 11500000, guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}\n"
                "  speed:\n    serializedVersion: 2\n    m_Curve:\n"
                % (fid, n, fid + 1, fid + 2, fid + 1, fid, fid + 2, fid)
                + "".join(self._key(*k, weighted=weighted) for k in keys)
                + "    m_PreInfinity: %d\n    m_PostInfinity: %d\n"
                  "    m_RotationOrder: 4\n  tag: %d\n" % (pre, post, n))
        root = tempfile.mkdtemp(prefix="upack-curve-")
        self.addCleanup(shutil.rmtree, root, True)
        s = os.path.join(root, "Assets", "Scripts")
        os.makedirs(s)
        with open(os.path.join(s, "Mover.cs"), "w") as f:
            f.write(script)
        with open(os.path.join(s, "Mover.cs.meta"), "w") as f:
            f.write("guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
        os.makedirs(os.path.join(root, "Assets", "Scenes"))
        with open(os.path.join(root, "Assets", "Scenes", "S.unity"), "w") as f:
            f.write(scene)
        return root

    def test_reference_matches_unity_known_values(self):
        import tools.unity.unity_pack_curves as uc
        ease = {"keys": [(0, 0, 0, 0), (1, 1, 0, 0)], "pre": 8, "post": 8}
        lin = {"keys": [(0, 0, 1, 1), (1, 1, 1, 1)], "pre": 8, "post": 8}
        # AnimationCurve.EaseInOut is smoothstep; .Linear is the line
        self.assertAlmostEqual(uc.evaluate(ease, 0.25), 0.15625)
        self.assertAlmostEqual(uc.evaluate(lin, 0.5), 0.5)
        self.assertAlmostEqual(uc.evaluate(dict(lin, post=2), 1.25), 0.25)
        self.assertAlmostEqual(uc.evaluate(dict(lin, post=4), 1.25), 0.75)

    @needs_cc
    def test_engine_evaluates_as_the_reference(self):
        import tools.unity.unity_pack_curves as uc
        out = tempfile.mkdtemp(prefix="upack-curve-out-")
        self.addCleanup(shutil.rmtree, out, True)
        unity_pack.pack(self._project(self.CURVES), out, force=True, strict=True)
        src = os.path.join(out, "h.c")
        with open(src, "w") as f:
            f.write('#include "engine_draw.h"\nextern float Time_deltaTime;\n'
                    "int main(int c, char **v) { engine_apply_argv(c, v);\n"
                    "  Time_deltaTime = 1.f / 60.f; engine_tick(); return 0; }\n")
        exe = os.path.join(out, "h")
        r = subprocess.run([_CC, "-O2", "-w", "-I", out, "-o", exe, src,
                            os.path.join(out, "engine.c"),
                            os.path.join(out, "data.c"), "-lm"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        lines = subprocess.run([exe, "-logFile", "-"], capture_output=True,
                               text=True, timeout=60).stdout.split("\n")
        for n, (keys, pre, post) in self.CURVES.items():
            rows = [l.split(" ", 1)[1] for l in lines if l.startswith("c%d " % n)]
            self.assertEqual(rows[0], "n=%d" % len(keys))
            ref = {"keys": [(float(a), float(b), uc._num(str(c)), uc._num(str(d)))
                            for a, b, c, d in keys], "pre": pre, "post": post}
            for t, got in zip(self.TS, rows[1:]):
                self.assertAlmostEqual(float(got), uc.evaluate(ref, t), places=4,
                                       msg="curve %d at t=%g" % (n, t))

    def test_weighted_tangents_are_refused(self):
        with self.assertRaises(unity_pack.PackError) as cm:
            unity_pack.pack(self._project({1: self.CURVES[1]}, weighted=3),
                            tempfile.mkdtemp(prefix="upack-curve-out-"), force=True)
        self.assertIn("weighted", cm.exception.message)


class TestLineRenderer(unittest.TestCase):
    """A LineRenderer: read from the scene, scripted, and drawn as one quad
    a segment. It was not read at all: the scene reader's kind whitelist
    did not name it, so the component vanished."""

    SCRIPT = """using UnityEngine;
public class Drawer : MonoBehaviour {
    public LineRenderer lr;
    void Start() {
        lr.positionCount = 4;
        lr.SetPosition(3, new Vector3(0f, 2f, 0f));
        lr.loop = true;
        lr.startColor = Color.blue;
        Debug.Log("n=" + lr.positionCount + " y3=" + lr.GetPosition(3).y + " w0=" + lr.startWidth);
    }
}
"""

    @staticmethod
    def _key(t, v, i, o):
        return ("      - serializedVersion: 3\n        time: %s\n        value: %s\n"
                "        inSlope: %s\n        outSlope: %s\n        tangentMode: 0\n"
                "        weightedMode: 0\n        inWeight: 0.33333334\n"
                "        outWeight: 0.33333334\n" % (t, v, i, o))

    def _project(self, script, world=1, rot_z=0.0, with_line=True):
        lr = ("--- !u!120 &5\nLineRenderer:\n  m_GameObject: {fileID: 1}\n  m_Enabled: 1\n"
              "  m_SortingOrder: 3\n  m_Positions:\n  - {x: 0, y: 0, z: 0}\n"
              "  - {x: 2, y: 0, z: 0}\n  - {x: 2, y: 2, z: 0}\n  m_Parameters:\n"
              "    serializedVersion: 3\n    widthMultiplier: 0.5\n    widthCurve:\n"
              "      serializedVersion: 2\n      m_Curve:\n"
              + self._key(0, 1, 0, -0.5) + self._key(1, 0.5, -0.5, 0) +
              "      m_PreInfinity: 2\n      m_PostInfinity: 2\n      m_RotationOrder: 4\n"
              "    colorGradient:\n      serializedVersion: 2\n"
              "      key0: {r: 1, g: 1, b: 1, a: 1}\n      key1: {r: 1, g: 0, b: 0, a: 1}\n"
              "      ctime0: 0\n      ctime1: 65535\n      atime0: 0\n      atime1: 65535\n"
              "      m_Mode: 0\n      m_NumColorKeys: 2\n      m_NumAlphaKeys: 2\n"
              "  m_UseWorldSpace: %d\n  m_Loop: 0\n" % world)
        scene = ("%%YAML 1.1\n--- !u!1 &1\nGameObject:\n  m_Name: Drawer\n  m_Component:\n"
                 "  - component: {fileID: 2}\n  - component: {fileID: 3}\n"
                 "  - component: {fileID: 5}\n--- !u!4 &2\nTransform:\n"
                 "  m_GameObject: {fileID: 1}\n  m_LocalRotation: {x: 0, y: 0, z: %s, w: %s}\n"
                 "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                 "--- !u!114 &3\nMonoBehaviour:\n  m_GameObject: {fileID: 1}\n"
                 "  m_Script: {fileID: 11500000, guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}\n"
                 "  lr: {fileID: 5}\n" % (rot_z, (1 - rot_z * rot_z) ** 0.5)) + lr
        if not with_line:
            scene = scene.split("--- !u!120 ")[0].replace(
                "  - component: {fileID: 5}\n", "").replace("  lr: {fileID: 5}\n", "")
        root = tempfile.mkdtemp(prefix="upack-lr-")
        self.addCleanup(shutil.rmtree, root, True)
        d = os.path.join(root, "Assets", "Scripts")
        os.makedirs(d)
        with open(os.path.join(d, "Drawer.cs"), "w") as f:
            f.write(script)
        with open(os.path.join(d, "Drawer.cs.meta"), "w") as f:
            f.write("guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
        os.makedirs(os.path.join(root, "Assets", "Scenes"))
        with open(os.path.join(root, "Assets", "Scenes", "S.unity"), "w") as f:
            f.write(scene)
        return root

    def _run_lines(self, root):
        """Pack, tick once; the log and the untextured quads' lines."""
        out = tempfile.mkdtemp(prefix="upack-lr-out-")
        self.addCleanup(shutil.rmtree, out, True)
        unity_pack.pack(root, out, force=True, strict=True)
        src = os.path.join(out, "h.c")
        with open(src, "w") as f:
            f.write('#include <stdio.h>\n#include "engine_draw.h"\nextern float Time_deltaTime;\n'
                    "int main(int c, char **v) { EngineDraw b[32]; int n, k;\n"
                    "  engine_apply_argv(c, v); Time_deltaTime = 1.f / 60.f;\n"
                    "  engine_tick(); n = engine_collect_draws(b, 32);\n"
                    "  for (k = 0; k < n; k++) if (b[k].tex == -2)\n"
                    "    printf(\"Q %g %g %g %g %g %g %g %g %g %d\\n\", b[k].x, b[k].y,\n"
                    "           b[k].half_w, b[k].half_h, b[k].m00, b[k].m10,\n"
                    "           b[k].r, b[k].g, b[k].b, b[k].sorting_order);\n"
                    "  return 0; }\n")
        exe = os.path.join(out, "h")
        r = subprocess.run([_CC, "-O2", "-w", "-I", out, "-o", exe, src,
                            os.path.join(out, "engine.c"), os.path.join(out, "data.c"),
                            "-lm"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        run = subprocess.run([exe, "-logFile", "-"], capture_output=True,
                             text=True, timeout=60)
        return run.stdout.splitlines()

    @needs_cc
    def test_scripted_line_draws_as_unity_evaluates_it(self):
        import math
        import tools.unity.unity_pack_curves as uc
        import tools.unity.unity_pack_lines as ul
        lines = self._run_lines(self._project(self.SCRIPT))
        self.assertIn("n=4 y3=2 w0=0.5", lines)
        quads = [list(map(float, l.split()[1:])) for l in lines if l.startswith("Q ")]
        # the reference: Unity's rules, independently of the engine
        pts = [(0, 0), (2, 0), (2, 2), (0, 2)]          # 4th set by the script
        width = {"keys": [(0, 1, 0, -0.5), (1, 0.5, -0.5, 0)], "pre": 2, "post": 2}
        grad = {"mode": 0, "colors": [(0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 0.0, 0.0)],
                "alphas": [(0.0, 1.0), (1.0, 1.0)]}     # startColor = blue
        segs = [(pts[k], pts[(k + 1) % 4]) for k in range(4)]   # loop = true
        lens = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in segs]
        self.assertEqual(len(quads), 4)
        run_ = 0.0
        for (a, b), L, q in zip(segs, lens, quads):
            u = (run_ + L / 2) / sum(lens)
            run_ += L
            col = ul.gradient_eval(grad, u)
            want = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, L / 2,
                    0.5 * uc.evaluate(width, u) / 2, (b[0] - a[0]) / L,
                    (b[1] - a[1]) / L, col[0], col[1], col[2], 3]
            for got, exp in zip(q, want):
                self.assertAlmostEqual(got, exp, places=4)

    @needs_cc
    def test_added_line_takes_a_spare_row(self):
        lines = self._run_lines(self._project("""using UnityEngine;
public class Drawer : MonoBehaviour {
    void Start() {
        LineRenderer a = gameObject.AddComponent<LineRenderer>();
        a.positionCount = 3;
        a.SetPosition(2, new Vector3(1f, 2f, 0f));
        a.useWorldSpace = false;
        Debug.Log("n=" + a.positionCount + " y2=" + a.GetPosition(2).y + " ws=" + a.useWorldSpace
                  + " again=" + (gameObject.AddComponent<LineRenderer>() == null)
                  + " same=" + (GetComponent<LineRenderer>() == a));
    }
}
""", with_line=False))
        self.assertIn("n=3 y2=2 ws=False again=True same=True", lines)
        # Unity's new line: two points at the origin (no quad), then the one set
        self.assertEqual(len([l for l in lines if l.startswith("Q ")]), 1)

    @needs_cc
    def test_through_a_singleton_field(self):
        # Slime Jump's Player.Death: `if (Lasso.instance.lineRenderer.enabled)`
        rope = ("using UnityEngine;\npublic class Rope : MonoBehaviour {\n"
                "    public static Rope instance;\n    public LineRenderer lr;\n"
                "    void Awake() { instance = this; }\n}\n")
        user = ("using UnityEngine;\npublic class User : MonoBehaviour {\n"
                "    void Start() {\n        bool was = Rope.instance.lr.enabled;\n"
                "        Rope.instance.lr.enabled = false;\n"
                "        Debug.Log(\"was=\" + was + \" now=\" + Rope.instance.lr.enabled);\n    }\n}\n")
        lr = ("--- !u!120 &{fid}\nLineRenderer:\n  m_GameObject: {{fileID: {go}}}\n  m_Enabled: 1\n"
              "  m_Positions:\n  - {{x: 0, y: 0, z: 0}}\n  - {{x: 1, y: 0, z: 0}}\n  m_UseWorldSpace: 1\n")
        out = run_frames(self, pack(self, project(self, {"Rope": rope, "User": user}, [
            ("Rope", lr, "  lr: {fileID: 103}\n"), ("User",)])), 1)
        self.assertIn("was=True now=False", out)

    def test_gradient_reference(self):
        import tools.unity.unity_pack_lines as ul
        g = {"mode": 0, "colors": [(0.0, 1.0, 1.0, 1.0), (1.0, 1.0, 0.0, 0.0)],
             "alphas": [(0.0, 1.0), (1.0, 0.0)]}
        self.assertEqual(ul.gradient_eval(g, 0.25), (1.0, 0.75, 0.75, 0.75))
        fixed = dict(g, mode=1)                # the first key at or after t
        self.assertEqual(ul.gradient_eval(fixed, 0.25), (1.0, 0.0, 0.0, 0.0))

    @needs_cc
    def test_local_space_on_a_rotated_object_turns_with_it(self):
        import math
        lines = self._run_lines(self._project(
            self.SCRIPT, world=0, rot_z=math.sqrt(0.5)))
        q = [list(map(float, l.split()[1:])) for l in lines if l.startswith("Q ")][0]
        # 90 degrees: the first segment (0,0)-(2,0) is drawn (0,0)-(0,2)
        for got, want in zip(q[:6], (0, 1, 1, q[3], 0, 1)):
            self.assertAlmostEqual(got, want, places=4)


class TestTransformTranslate(unittest.TestCase):
    """`transform.Translate`, and the diagnostics for Transform members.

    Translate was refused with CS1061 -- "'Transform' does not contain a
    definition for 'Translate'" -- which is false: every real Transform
    member the pack does not lower got that. A real one is now "not packed
    yet" (CS8000), an invented one CS1061. Translate itself moves by the
    delta in the object's own axes (Space.Self, the default) or the
    world's; it is the first use of a live-rotation read in an engine with
    no string concatenation, which lost its runtime helpers (the helper
    marker was emitted only for string concatenation)."""

    def _project(self, scripts, rots):
        root = tempfile.mkdtemp(prefix="upack-tr-")
        self.addCleanup(shutil.rmtree, root, True)
        sd = os.path.join(root, "Assets", "Scripts")
        os.makedirs(sd)
        scene, fid = "%YAML 1.1\n", 100
        for k, (name, body) in enumerate(scripts.items()):
            guid = "%032x" % (k + 1)
            with open(os.path.join(sd, name + ".cs"), "w") as f:
                f.write("using UnityEngine;\npublic class %s : MonoBehaviour {\n"
                        "    int f;\n    void Update() {\n        f++; if (f > 1) return;\n"
                        "        %s\n        Debug.Log(\"%s \" + transform.position.x + \" \""
                        " + transform.position.y);\n    }\n}\n" % (name, body, name))
            with open(os.path.join(sd, name + ".cs.meta"), "w") as f:
                f.write("guid: %s\n" % guid)
            q = rots.get(name, (0, 0, 0, 1))
            scene += (
                "--- !u!1 &%d\nGameObject:\n  m_Name: %s\n  m_Component:\n"
                "  - component: {fileID: %d}\n  - component: {fileID: %d}\n"
                "--- !u!4 &%d\nTransform:\n  m_GameObject: {fileID: %d}\n"
                "  m_LocalRotation: {x: %r, y: %r, z: %r, w: %r}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "--- !u!114 &%d\nMonoBehaviour:\n  m_GameObject: {fileID: %d}\n"
                "  m_Script: {fileID: 11500000, guid: %s}\n"
                % (fid, name, fid + 1, fid + 2, fid + 1, fid, q[0], q[1], q[2], q[3],
                   fid + 2, fid, guid))
            fid += 10
        os.makedirs(os.path.join(root, "Assets", "Scenes"))
        with open(os.path.join(root, "Assets", "Scenes", "S.unity"), "w") as f:
            f.write(scene)
        return root

    @needs_cc
    def test_self_and_world_space(self):
        import math
        q90 = (0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4))
        root = self._project({
            "A": "transform.Translate(1f, 0f);",                       # Self
            "B": "transform.Translate(new Vector3(1f, 0f, 0f), Space.World);",
            "C": "transform.Rotate(0f, 0f, 90f); transform.Translate(Vector2.right * 1f);",
        }, {"A": q90, "B": q90})
        out = tempfile.mkdtemp(prefix="upack-tr-out-")
        self.addCleanup(shutil.rmtree, out, True)
        unity_pack.pack(root, out, force=True, strict=True)
        src = os.path.join(out, "h.c")
        with open(src, "w") as f:
            f.write('#include "engine_draw.h"\nextern float Time_deltaTime;\n'
                    "int main(int c, char **v) { engine_apply_argv(c, v);\n"
                    "  Time_deltaTime = 1.f / 60.f; engine_tick(); return 0; }\n")
        exe = os.path.join(out, "h")
        r = subprocess.run([_CC, "-O2", "-w", "-I", out, "-o", exe, src,
                            os.path.join(out, "engine.c"), os.path.join(out, "data.c"),
                            "-lm"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        got = {}
        for l in subprocess.run([exe, "-logFile", "-"], capture_output=True,
                                text=True, timeout=60).stdout.splitlines():
            p = l.split()
            if len(p) == 3:
                got[p[0]] = (float(p[1]), float(p[2]))
        # Unity: a 90-degree object's right is world up; World ignores it;
        # after Rotate the heading is the new one
        for name, want in (("A", (0, 1)), ("B", (1, 0)), ("C", (0, 1))):
            self.assertAlmostEqual(got[name][0], want[0], places=4, msg=name)
            self.assertAlmostEqual(got[name][1], want[1], places=4, msg=name)

    def test_real_member_is_not_packed_yet_invented_is_cs1061(self):
        for body, code, member in (("float z = transform.right.x;", "CS8000", "right"),
                                   ("transform.Fly();", "CS1061", "Fly")):
            with self.assertRaises(unity_pack.PackError) as cm:
                unity_pack.pack(self._project({"P": body}, {}),
                                tempfile.mkdtemp(prefix="upack-tr-out-"), force=True)
            self.assertIn("error %s" % code, cm.exception.message)
            self.assertIn(member, cm.exception.message)




class TestPositionVectors(unittest.TestCase):
    """`transform.position` as a vector value, and Vector2's MoveTowards /
    Lerp / LerpUnclamped / Min / Max, in a 2D pack. Only `.x` / `+=` /
    `= new Vector3` were lowered before: `Vector2.MoveTowards(
    transform.position, ..)` or `transform.position = Vector2.Lerp(..)`
    emptied the method."""

    @needs_cc
    def test_values_follow_unity(self):
        body = (
            # MoveTowards: a step short of the target, then reaching it
            "Vector2 a = Vector2.MoveTowards(new Vector2(0f, 0f), new Vector2(3f, 4f), 2f);"
            " Debug.Log(\"mt \" + a.x + \" \" + a.y);"
            " Vector2 b = Vector2.MoveTowards(new Vector2(0f, 0f), new Vector2(3f, 4f), 9f);"
            " Debug.Log(\"mt2 \" + b.x + \" \" + b.y);"
            # Lerp clamps t, LerpUnclamped does not
            " Vector2 c = Vector2.Lerp(new Vector2(0f, 0f), new Vector2(2f, 4f), 1.5f);"
            " Debug.Log(\"lerp \" + c.x + \" \" + c.y);"
            " Vector2 d = Vector2.LerpUnclamped(new Vector2(0f, 0f), new Vector2(2f, 4f), 1.5f);"
            " Debug.Log(\"lu \" + d.x + \" \" + d.y);"
            " Vector2 e = Vector2.Min(new Vector2(1f, 5f), new Vector2(3f, 2f));"
            " Vector2 g = Vector2.Max(new Vector2(1f, 5f), new Vector2(3f, 2f));"
            " Debug.Log(\"mm \" + e.x + \" \" + e.y + \" \" + g.x + \" \" + g.y);"
            # the position as a value: read, step towards a point, write
            " transform.position = Vector2.MoveTowards(transform.position, new Vector2(10f, 0f), 4f);"
            " Vector2 p = transform.position;"
            " Debug.Log(\"pos \" + p.x + \" \" + p.y);")
        root = project(self, {"P": script("P", body)}, [("P",)])
        lines = run_frames(self, pack(self, root))
        got = dict((l.split(" ", 1)[0], [float(x) for x in l.split()[1:]])
                   for l in lines if " " in l)
        self.assertEqual(got["mt"], [1.2, 1.6])        # 2 along (3, 4) / 5
        self.assertEqual(got["mt2"], [3.0, 4.0])       # within reach: the target
        self.assertEqual(got["lerp"], [2.0, 4.0])      # t clamped to 1
        self.assertEqual(got["lu"], [3.0, 6.0])
        self.assertEqual(got["mm"], [1.0, 2.0, 3.0, 5.0])
        self.assertEqual(got["pos"], [4.0, 0.0])       # from x = 0 (object 0)



class TestInputButtons(unittest.TestCase):
    """Input.GetButton / GetButtonDown / GetButtonUp by name. Only "Jump"
    existed -- any other name was silently never pressed -- and Down / Up
    emptied the method."""

    @needs_cc
    def test_down_held_up_per_frame(self):
        src = ("using UnityEngine;\npublic class P : MonoBehaviour {\n"
               "    int f;\n    void Update() {\n        f++;\n"
               # `? 1 : 0`: "x" + a bool prints 1 / 0 here, not True /
               # False (a separate gap: scalar_kind has no bool)
               "        Debug.Log(\"f\" + f + \" \" + (Input.GetButtonDown(\"Jump\") ? 1 : 0)"
               " + \" \" + (Input.GetButton(\"Jump\") ? 1 : 0) + \" \""
               " + (Input.GetButtonUp(\"Jump\") ? 1 : 0) + \" \""
               " + (Input.GetButton(\"Fire1\") ? 1 : 0));\n    }\n}\n")
        out = pack(self, project(self, {"P": src}, [("P",)]))
        # frames 1..5: Jump up, down, held, released, up; Fire1 held
        lines = run_frames(self, out, 0, body=(
            "extern int engine_input_button_Jump, engine_input_button_Fire1;\n"
            "  { int jump[5] = {0, 1, 1, 0, 0}; for (f = 0; f < 5; f++) {\n"
            "    engine_input_button_Jump = jump[f]; engine_input_button_Fire1 = 1;\n"
            "    engine_tick(); } }"))
        self.assertEqual([l for l in lines if l.startswith("f")], [
            "f1 0 0 0 1",
            "f2 1 1 0 1",       # pressed this frame
            "f3 0 1 0 1",      # held
            "f4 0 0 1 1",      # released this frame
            "f5 0 0 0 1"])



class TestOwnName(unittest.TestCase):
    """`name` / `gameObject.name`: the GameObject's name. It reached C as an
    undeclared `name`, and `name + " hp"` (a literal second) had no string
    concatenation helper."""

    @needs_cc
    def test_each_object_its_own_name(self):
        src = script("P", 'Debug.Log(name + " hp"); Debug.Log("go " + gameObject.name);')
        lines = run_frames(self, pack(self, project(self, {"P": src}, [("P",), ("P",)])))
        self.assertEqual(sorted(l for l in lines if l.endswith(" hp")), ["P0 hp", "P1 hp"])
        self.assertEqual(sorted(l for l in lines if l.startswith("go ")), ["go P0", "go P1"])

    @needs_cc
    @needs_coost
    def test_own_field_named_name_is_the_field(self):
        src = script("P", 'Debug.Log("n " + name);', members='    public string name = "mine";')
        lines = run_frames(self, pack(self, project(self, {"P": src}, [("P",)])))
        self.assertIn("n mine", lines)



class TestSingletonField(unittest.TestCase):
    """`public static GameManager Instance; .. Instance = this;` -- the
    everyday singleton. Its own `Instance = this` reached C as
    `Type_Instance() = i`, and a class used only through its own bare
    `Instance` got no cache at all. A static *field* is null until
    assigned (no FindObjectOfType fallback), so only the first Awake sees
    `Instance == null`, and a duplicate destroys itself."""

    GM = """using UnityEngine;
public class GM : MonoBehaviour {
    public static GM Instance;
    public int score;
    void Awake() {
        if (Instance == null) Debug.Log("first " + name);
        if (Instance != null && Instance != this) { Debug.Log("dup " + name); Destroy(gameObject); return; }
        Instance = this;
    }
    public void Add(int v) { score += v; }
}
"""
    USER = """using UnityEngine;
public class User : MonoBehaviour {
    int f;
    void Update() { f++; if (f > 1) return; GM.Instance.Add(2); Debug.Log("score " + GM.Instance.score); }
}
"""

    @needs_cc
    def test_first_wins_duplicate_destroyed(self):
        root = project(self, {"GM": self.GM, "User": self.USER},
                       [("GM",), ("GM",), ("User",)])
        lines = run_frames(self, pack(self, root))
        self.assertEqual([l for l in lines if l.startswith("first")], ["first GM0"])
        self.assertEqual([l for l in lines if l.startswith("dup")], ["dup GM1"])
        self.assertIn("score 2", lines)



class TestStaticStateAndForeach(unittest.TestCase):
    """Slime Jump forms: a static property over a static backing field
    (`P += x`, `P++`), a `static Bag b = new Bag();` handle made at the
    first tick, and `foreach` over a List<int>. Each emptied its method."""

    BAG = "public class Bag { public int n; }\n"
    P = """using UnityEngine;
using System.Collections.Generic;
public class P : MonoBehaviour {
    static int _pts;
    public static int Points { get { return _pts; } set { _pts = value; } }
    static Bag bag = new Bag();
    List<int> xs = new List<int>();
    int _f;
    void Update() {
        _f++; if (_f > 1) return;
        Points += 3; Points++;
        bag.n = 5;
        xs.Add(2); xs.Add(4);
        int s = 0;
        foreach (int x in xs) s += x;
        Debug.Log("pts " + Points + " bag " + bag.n + " sum " + s);
    }
}
"""

    @needs_cc
    def test_values_follow_csharp(self):
        root = project(self, {"P": self.P, "Bag": self.BAG}, [("P",)])
        self.assertIn("pts 4 bag 5 sum 6", run_frames(self, pack(self, root)))


class TestStaticReference(unittest.TestCase):
    """`static Ach current;` -- a static reference to a packed object, set
    bare in its class and read as `Ach.current` elsewhere (Slime Jump's
    `SpeedAchievement.current` emptied `GameManager.Update`)."""

    ACH = """using UnityEngine;
public class Ach : MonoBehaviour {
    public static Ach current;
    public float left = 3f;
    public float TimeLeft { get { return left; } set { left = value; } }
    void Start() { current = this; }
}
"""
    MGR = """using UnityEngine;
public class Mgr : MonoBehaviour {
    int _f;
    void Update() {
        _f++;
        if (Ach.current == null) { Debug.Log("none " + _f); return; }
        Ach.current.TimeLeft -= 1f;
        if (Ach.current.TimeLeft <= 0) { Ach.current = null; return; }
        Debug.Log("left " + (int)Ach.current.TimeLeft);
    }
}
"""

    @needs_cc
    def test_set_read_and_cleared(self):
        root = project(self, {"Ach": self.ACH, "Mgr": self.MGR},
                       [("Ach",), ("Mgr",)])
        out = run_frames(self, pack(self, root), 5)
        self.assertIn("left 2", out)
        self.assertIn("left 1", out)
        self.assertIn("none 4", out)


class TestSingletonFieldCompare(unittest.TestCase):
    """`Lasso.instance.changeLengthInput == 0` is a read: Slime Jump's
    `Player.DoUpdate` had it lowered as a setter of `= 0) { ... }`."""

    LASSO = """using UnityEngine;
public class Lasso : MonoBehaviour {
    public static Lasso instance;
    public int changeLengthInput;
    public bool isAttached = true;
    void Awake() { instance = this; }
}
"""
    CAM = """using UnityEngine;
public class Cam : MonoBehaviour {
    public static Cam instance;
    public bool followPlayer = true;
    void Awake() { instance = this; }
}
"""
    MGR = """using UnityEngine;
public class Mgr : MonoBehaviour {
    void Update() {
        if (Lasso.instance.isAttached && Lasso.instance.changeLengthInput == 0)
        {
            Cam.instance.followPlayer = false;
        }
        if (!Cam.instance.followPlayer) Debug.Log("follow False");
    }
}
"""

    @needs_cc
    def test_compare_is_a_read(self):
        root = project(self, {"Lasso": self.LASSO, "Cam": self.CAM,
                              "Mgr": self.MGR},
                       [("Lasso",), ("Cam",), ("Mgr",)])
        out = run_frames(self, pack(self, root), 2)
        self.assertIn("follow False", out)


class TestPlayerUpdateForms(unittest.TestCase):
    """Slime Jump's `Player.DoUpdate` forms: a compound write keeps its
    value whole (`x -= 3 - 1` is 8, not 6), a Vector2 field's `*=`, `.x` of
    a Vector2 property, per-frame shader-parameter statements dropped, and
    a TMP text write that stops the player only if it runs."""

    MGR = """using UnityEngine;
using TMPro;
public class Mgr : MonoBehaviour {
    public TMP_Text label;
    public SpriteRenderer sr;
    public float x = 10f;
    public Vector2 v = new Vector2(1f, 2f);
    public static Vector2 Stick { get { return new Vector2(0.5f, 0f); } }
    void Update() {
        x -= 3f - 1f;
        v *= 2f;
        if (x < 0f) label.text = "never";
        Material mat = new Material(sr.sharedMaterial);
        mat.SetInt("_g", 1);
        sr.sharedMaterial = mat;
        Debug.Log("x " + (int)x + " v " + (int)v.y + " s " + (int)(Stick.x * 10f));
    }
}
"""

    @needs_cc
    def test_forms(self):
        root = project(self, {"Mgr": self.MGR}, [("Mgr",)])
        # not strict: strict refuses the shader and TMP statements instead
        out = pack(self, root, strict=False)
        self.assertIn("x 8 v 4 s 5", run_frames(self, out, 1))
        # frame 6: x < 0 runs the TMP write; `label` is unassigned
        with self.assertRaises(AssertionError) as cm:
            run_frames(self, out, 6)
        self.assertIn("NullReferenceException", str(cm.exception))
        self.assertIn("Mgr.cs:12", str(cm.exception))


class TestEmbeddedStruct(unittest.TestCase):
    """A [Serializable] struct a component embeds (Slime Jump's
    `AnimationEntry jumpAnimationEntry`) is authored as a nested mapping:
    each value is a row of the struct's class, the field its index. The
    rows are shared by copies, so a write to one is refused."""

    ENTRY = """using System;
[Serializable]
public struct Entry {
    public string stateName;
    public int layer;
    public float length;
    public bool Is(string s) { return stateName == s; }
    public float Twice() { return length * 2f; }
}
"""
    MGR = """using UnityEngine;
public class Mgr : MonoBehaviour {
    public Entry jump;
    public Entry land;
    void Update() {
        if (jump.Is("Jump")) Debug.Log("jump " + jump.layer + " " + (int)land.Twice());
        else Debug.Log("other " + land.layer);
    }
}
"""
    FIELDS = ("  jump:\n    stateName: %s\n    layer: %d\n    length: 0.5\n"
              "  land:\n    stateName: Land\n    layer: %d\n    length: %s\n")

    @needs_cc
    def test_values_per_object(self):
        root = project(self, {"Entry": self.ENTRY, "Mgr": self.MGR}, [
            ("Mgr", None, self.FIELDS % ("Jump", 1, 2, "3.5")),
            ("Mgr", None, self.FIELDS % ("Idle", 4, 5, "1"))])
        out = run_frames(self, pack(self, root), 1)
        self.assertIn("jump 1 7", out)
        self.assertIn("other 5", out)

    def test_prefab_override_reaches_nested_value(self):
        doc = ("MonoBehaviour:\n  jump:\n    stateName: Jump\n    layer: 1\n"
               "  land:\n    layer: 2\n")
        got = unity_pack._set_yaml_property(doc, "land.layer", "7")
        self.assertIn("  land:\n    layer: 7\n", got)
        self.assertIn("    layer: 1\n", got)
        self.assertEqual(unity_pack._embedded_values(got)["land"]["fields"],
                         {"layer": 7})

    def test_write_is_refused(self):
        mgr = self.MGR.replace("void Update() {",
                               "void Update() {\n        jump.layer = 3;")
        root = project(self, {"Entry": self.ENTRY, "Mgr": mgr}, [
            ("Mgr", None, self.FIELDS % ("Jump", 1, 2, "3.5"))])
        with self.assertRaises(unity_pack.PackError) as cm:
            pack(self, root)
        self.assertIn("embedded struct", str(cm.exception))


class TestAnimatorStateMachine(unittest.TestCase):
    """An Animator field runs its controller: `Play` switches state at the
    animator update (after Update), `IsName` reads the current one, the
    clip's float curve writes the script's field, and the exit-time
    transition returns to the default state."""

    MGR = """using UnityEngine;
public class Mgr : MonoBehaviour {
    public Animator anim;
    public Vector2 multiplySize = Vector2.one;
    int f;
    void Update() {
        f++;
        if (f == 2) anim.Play("Squash");
        if (f == 2 || f == 3 || f == 5 || f == 20)
            Debug.Log("f" + f + " " + (anim.GetCurrentAnimatorStateInfo(0).IsName("Squash") ? 1 : 0) + " " + (int)(multiplySize.x * 100f + 0.5f));
    }
}
"""
    CTRL = """%YAML 1.1
--- !u!91 &9100000
AnimatorController:
  m_Name: C
  m_AnimatorParameters: []
  m_AnimatorLayers:
  - serializedVersion: 5
    m_Name: Base Layer
    m_StateMachine: {fileID: 1107000}
    m_Mask: {fileID: 0}
    m_BlendingMode: 0
    m_SyncedLayerIndex: -1
    m_DefaultWeight: 0
--- !u!1107 &1107000
AnimatorStateMachine:
  m_Name: Base Layer
  m_ChildStates:
  - serializedVersion: 1
    m_State: {fileID: 1102001}
  - serializedVersion: 1
    m_State: {fileID: 1102002}
  m_ChildStateMachines: []
  m_AnyStateTransitions: []
  m_EntryTransitions: []
  m_DefaultState: {fileID: 1102001}
--- !u!1102 &1102001
AnimatorState:
  m_Name: Idle
  m_Speed: 1
  m_CycleOffset: 0
  m_Transitions: []
  m_Motion: {fileID: 0}
--- !u!1102 &1102002
AnimatorState:
  m_Name: Squash
  m_Speed: 1
  m_CycleOffset: 0
  m_Transitions:
  - {fileID: 1101003}
  m_Motion: {fileID: 7400000, guid: CLIPGUID, type: 2}
--- !u!1101 &1101003
AnimatorStateTransition:
  m_Conditions: []
  m_DstState: {fileID: 1102001}
  m_Mute: 0
  m_IsExit: 0
  m_TransitionDuration: 0
  m_TransitionOffset: 0
  m_ExitTime: 1
  m_HasExitTime: 1
"""
    CLIP = """%YAML 1.1
--- !u!74 &7400000
AnimationClip:
  m_Name: Squash
  m_PositionCurves: []
  m_FloatCurves:
  - serializedVersion: 2
    curve:
      serializedVersion: 2
      m_Curve:
      - serializedVersion: 3
        time: 0
        value: 1
        inSlope: -5
        outSlope: -5
        weightedMode: 0
      - serializedVersion: 3
        time: 0.1
        value: 0.5
        inSlope: -5
        outSlope: -5
        weightedMode: 0
    attribute: multiplySize.x
    path: 
    classID: 114
  m_PPtrCurves: []
  m_AnimationClipSettings:
    m_StopTime: 0.1
    m_LoopTime: 0
"""
    ANIMATOR = ("--- !u!95 &{fid}\nAnimator:\n  m_GameObject: {{fileID: {go}}}\n"
                "  m_Enabled: 1\n  m_Controller: {{fileID: 9100000, guid: "
                "c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0, type: 2}}\n")

    def _project(self, ctrl=None):
        root = project(self, {"Mgr": self.MGR}, [
            ("Mgr", self.ANIMATOR,
             "  anim: {fileID: 103}\n  multiplySize: {x: 1, y: 1}\n")])
        d = os.path.join(root, "Assets", "Animations")
        os.makedirs(d)
        for name, guid, text in (
                ("C.controller", "c0" * 16,
                 (ctrl or self.CTRL).replace("CLIPGUID", "d0" * 16)),
                ("Squash.anim", "d0" * 16, self.CLIP)):
            with open(os.path.join(d, name), "w") as f:
                f.write(text)
            with open(os.path.join(d, name + ".meta"), "w") as f:
                f.write("guid: %s\n" % guid)
        return root

    @needs_cc
    def test_play_isname_curve_and_exit(self):
        out = run_frames(self, pack(self, self._project()), 20)
        # f2: Play is pending; f3/f5: Squash after 1 and 3 animator ticks
        # (value 1 - 5t); f20: back in Idle through the exit transition
        self.assertIn("f2 0 100", out)
        self.assertIn("f3 1 92", out)
        self.assertIn("f5 1 75", out)
        self.assertTrue(any(l.startswith("f20 0 ") for l in out), out)

    @needs_cc
    def test_speed_zero_freezes(self):
        """`animator.speed = 0` (Slime Jump's Player.Death) stops the clock."""
        self.MGR = self.MGR.replace(
            'if (f == 2) anim.Play("Squash");',
            'if (f == 2) anim.Play("Squash");\n        if (f == 3) anim.speed = 0;'
            '\n        if (f == 5) Debug.Log("speed " + anim.speed);')
        out = run_frames(self, pack(self, self._project()), 5)
        self.assertIn("f3 1 92", out)
        self.assertIn("f5 1 92", out)
        self.assertIn("speed 0", out)

    def test_parameters_are_refused(self):
        ctrl = self.CTRL.replace(
            "  m_AnimatorParameters: []\n",
            "  m_AnimatorParameters:\n  - m_Name: Speed\n    m_Type: 1\n")
        with self.assertRaises(unity_pack.PackError) as cm:
            pack(self, self._project(ctrl))
        self.assertIn("parameters", str(cm.exception))


class TestColliderBounds(unittest.TestCase):
    """`Collider2D.bounds` of a field (Slime Jump's wall probe): the world
    AABB of a rotated, offset box and of a capsule."""

    MGR = """using UnityEngine;
public class Mgr : MonoBehaviour {
    public BoxCollider2D box;
    public Collider2D cap;
    int f;
    void Update() {
        f++; if (f > 1) return;
        Vector2 c = (Vector2) box.bounds.center + Vector2.down * box.bounds.extents.y;
        Debug.Log("box " + R(box.bounds.center.x) + " " + R(box.bounds.center.y) + " " + R(box.bounds.extents.x) + " " + R(box.bounds.max.y) + " " + R(box.bounds.size.x) + " " + R(c.y));
        Debug.Log("cap " + R(cap.bounds.min.x) + " " + R(cap.bounds.extents.y));
    }
    static int R(float v) { return Mathf.RoundToInt(v * 100f); }
}
"""
    BOX = ("--- !u!61 &{fid}\nBoxCollider2D:\n  m_GameObject: {{fileID: {go}}}\n"
           "  m_Enabled: 1\n  m_Offset: {{x: 0.5, y: 0}}\n  m_Size: {{x: 2, y: 1}}\n")
    CAP = ("--- !u!70 &{fid}\nCapsuleCollider2D:\n  m_GameObject: {{fileID: {go}}}\n"
           "  m_Enabled: 1\n  m_Offset: {{x: 0, y: 0}}\n  m_Size: {{x: 1, y: 3}}\n"
           "  m_Direction: 0\n")

    @needs_cc
    def test_box_and_capsule(self):
        log = "using UnityEngine;\npublic class Log : MonoBehaviour { }\n"
        root = project(self, {"Mgr": self.MGR, "Log": log}, [
            ("Mgr", self.BOX, "  box: {fileID: 103}\n  cap: {fileID: 113}\n",
             (0.0, 0.0, 0.70710678, 0.70710678)),
            ("Log", self.CAP)])
        # no Box2D in this harness: nothing moves
        out = run_frames(self, pack(self, root), 1,
                         pre="void engine_box2d_step(float dt) { (void)dt; }\n")
        # turned 90 degrees: the 2 x 1 box offset (0.5, 0) is 1 x 2 at (0, 0.5)
        self.assertIn("box 0 50 50 150 100 -50", out)
        # the capsule at x = 1: 1 wide, 3 tall
        self.assertIn("cap 50 150", out)


class TestPrefabInstantiateAt(unittest.TestCase):
    """`Instantiate(prefabField, pos, Quaternion.identity)` of an unplaced
    prefab (Slime Jump's dust clouds): clones at *pos*; a destroyed clone's
    slot is reused."""

    @needs_cc
    def test_clone_at_position(self):
        mgr = ("using UnityEngine;\npublic class Mgr : MonoBehaviour {\n"
               "    public Cloud prefab;\n    Vector2 at;\n    int f;\n"
               "    void Update() {\n        f++;\n"
               "        if (f <= 2) { at = new Vector2(f * 3, 2f); "
               "Instantiate(prefab, at, Quaternion.identity); }\n    }\n}\n")
        cloud = ("using UnityEngine;\npublic class Cloud : MonoBehaviour {\n"
                 "    public float life;\n    float t;\n"
                 "    void Start() { Debug.Log(\"cloud \" + Mathf.RoundToInt("
                 "transform.position.x) + \" \" + Mathf.RoundToInt("
                 "transform.position.y) + \" \" + Mathf.RoundToInt(life)); "
                 "Destroy(gameObject, 0.04f); }\n"
                 "    void Update() { t += Time.deltaTime; }\n"
                 "    void OnDestroy() { Debug.Log(\"gone \" + "
                 "Mathf.RoundToInt(t * 60f)); }\n}\n")
        root = project(self, {"Cloud": cloud, "Mgr": mgr}, [
            ("Mgr", None, "  prefab: {fileID: 12, guid: %032x, type: 3}\n"
             % 99)])
        with open(os.path.join(root, "Assets", "Cloud.prefab"), "w") as f:
            f.write("%%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: Cloud\n"
                    "  m_Component:\n  - component: {fileID: 11}\n"
                    "  - component: {fileID: 12}\n"
                    "--- !u!4 &11\nTransform:\n  m_GameObject: {fileID: 10}\n"
                    "  m_LocalPosition: {x: 7, y: 7, z: 0}\n"
                    "--- !u!114 &12\nMonoBehaviour:\n  m_GameObject: {fileID: 10}\n"
                    "  m_Script: {fileID: 11500000, guid: %032x}\n"
                    "  life: 5\n" % 1)
        with open(os.path.join(root, "Assets", "Cloud.prefab.meta"), "w") as f:
            f.write("guid: %032x\n" % 99)
        out = run_frames(self, pack(self, root), 6)
        self.assertEqual([l for l in out if l.startswith("cloud")],
                         ["cloud 3 2 5", "cloud 6 2 5"])
        # `Destroy(gameObject, 0.04f)`: gone after its 2nd or 3rd Update
        gone = [l for l in out if l.startswith("gone")]
        self.assertEqual(len(gone), 2, out)
        self.assertTrue(all(l in ("gone 2", "gone 3") for l in gone), out)


class TestSpriteRendererColor(unittest.TestCase):
    """`sr.color = sr.color.SetAlpha(a)` through a SpriteRenderer field
    (Slime Jump's dust cloud fade): the authored tint, then the set one."""

    @needs_cc
    def test_fade(self):
        ext = ("using UnityEngine;\npublic static class ColorExtensions {\n"
               "    public static Color SetAlpha(this Color c, float a) {\n"
               "        return new Color(c.r, c.g, c.b, a);\n    }\n}\n")
        mgr = ("using UnityEngine;\npublic class Mgr : MonoBehaviour {\n"
               "    public SpriteRenderer sr;\n    int f;\n"
               "    void Update() {\n        f++;\n"
               "        Debug.Log(\"a\" + f + \" \" + Mathf.RoundToInt("
               "sr.color.a * 100f) + \" \" + Mathf.RoundToInt(sr.color.g * 100f));\n"
               "        sr.color = sr.color.SetAlpha(0.25f);\n    }\n}\n")
        spr = ("--- !u!212 &{fid}\nSpriteRenderer:\n  m_GameObject: {{fileID: {go}}}\n"
               "  m_Enabled: 1\n  m_Color: {{r: 1, g: 0.5, b: 1, a: 0.5}}\n"
               "  m_Sprite: {{fileID: 0}}\n")
        root = project(self, {"ColorExtensions": ext, "Mgr": mgr}, [
            ("Mgr", spr, "  sr: {fileID: 103}\n")])
        out = run_frames(self, pack(self, root), 2)
        self.assertIn("a1 50 50", out)
        self.assertIn("a2 25 50", out)

    @needs_cc
    def test_respawn_resets_through_locals(self):
        """Slime Jump's Respawn loops: `b.spriteRenderer.color =
        b.spriteRenderer.color.Multiply(f)` (a mutate-and-return
        extension), `b.trs.position = b.initPos`, `b.gameObject
        .activeInHierarchy` on a local of a packed class, then `= new T[0]`
        on a static array and on one of a struct (no storage). `Multiply`
        is also VectorExtensions', told apart by the receiver's type."""
        ext = ("using UnityEngine;\npublic static class ColorExtensions {\n"
               "    public static Color Multiply(this Color c, float f) {\n"
               "        c.r *= f;\n        c.g *= f;\n        c.b *= f;\n"
               "        return c;\n    }\n}\n")
        vext = ("using UnityEngine;\npublic static class VectorExtensions {\n"
                "    public static Vector3 Multiply(this Vector3 v, float f) {\n"
                "        return v * f * 3;\n    }\n}\n")
        rec = ("public struct Rec { public int n;\n"
               "    public Rec Multiply(Rec o) { return o; }\n}\n")
        box = ("using UnityEngine;\npublic class Box : MonoBehaviour {\n"
               "    public SpriteRenderer spriteRenderer;\n"
               "    public Transform trs;\n    public Vector2 initPos;\n"
               "    public static Box[] instances = new Box[0];\n"
               "    public static Rec[] recs = new Rec[0];\n"
               "    void Awake() { transform.SetParent(null); }\n"
               "    void LateUpdate() { Debug.Log(\"x \" + Mathf.RoundToInt("
               "transform.position.x) + \" g \" + Mathf.RoundToInt("
               "spriteRenderer.color.g * 100f)); }\n}\n")
        mgr = ("using UnityEngine;\npublic class Mgr : MonoBehaviour {\n"
               "    int f;\n    void Update() {\n        f++; if (f > 1) return;\n"
               "        Box.instances = FindObjectsOfType<Box>();\n"
               "        for (int i = 0; i < Box.instances.Length; i ++) {\n"
               "            Box b = Box.instances[i];\n"
               "            b.spriteRenderer.color = b.spriteRenderer.color.Multiply(0.5f);\n"
               "            b.trs.position = b.initPos;\n"
               "            if (b.gameObject.activeInHierarchy) Debug.Log(\"act\");\n"
               "        }\n"
               "        Box.instances = new Box[0];\n"
               "        Debug.Log(\"n \" + Box.instances.Length);\n"
               "        Box.recs = new Rec[0];\n    }\n}\n")
        spr = ("--- !u!212 &{fid}\nSpriteRenderer:\n  m_GameObject: {{fileID: {go}}}\n"
               "  m_Enabled: 1\n  m_Color: {{r: 1, g: 0.8, b: 1, a: 1}}\n"
               "  m_Sprite: {{fileID: 0}}\n")
        root = project(self, {"ColorExtensions": ext, "VectorExtensions": vext,
                              "Rec": rec, "Box": box, "Mgr": mgr}, [
            ("Mgr",), ("Box", spr, "  spriteRenderer: {fileID: 113}\n"
                       "  trs: {fileID: 111}\n  initPos: {x: 5, y: 2}\n")])
        out = run_frames(self, pack(self, root), 1)
        self.assertEqual([l for l in out if l[:2] in ("x ", "ac", "n ")],
                         ["act", "n 0", "x 5 g 40"])

    @needs_cc
    def test_enabled(self):
        """`spriteRenderer.enabled = false` (Slime Jump's Player.Death)."""
        mgr = ("using UnityEngine;\npublic class Mgr : MonoBehaviour {\n"
               "    public SpriteRenderer sr;\n    int f;\n"
               "    void Update() {\n        f++;\n"
               "        Debug.Log(\"e\" + f + \" \" + (sr.enabled ? 1 : 0));\n"
               "        sr.enabled = f > 1;\n    }\n}\n")
        spr = ("--- !u!212 &{fid}\nSpriteRenderer:\n  m_GameObject: {{fileID: {go}}}\n"
               "  m_Enabled: 1\n  m_Sprite: {{fileID: 0}}\n")
        root = project(self, {"Mgr": mgr}, [("Mgr", spr, "  sr: {fileID: 103}\n")])
        out = run_frames(self, pack(self, root), 3)
        for want in ("e1 1", "e2 0", "e3 1"):
            self.assertIn(want, out)


class TestHandleEulerZ(unittest.TestCase):
    """`t.eulerAngles += Vector3.forward * d` and `t.eulerAngles =
    Vector3.zero` through a Transform field (Slime Jump's lasso swing)."""

    @needs_cc
    def test_step_and_reset(self):
        mgr = ("using UnityEngine;\npublic class Mgr : MonoBehaviour {\n"
               "    public Transform other;\n    int f;\n    void Update() {\n"
               "        f++;\n        if (f <= 2) other.eulerAngles += Vector3.forward * 30f / 2f * 3f;\n"
               "        if (f == 3) other.eulerAngles = Vector3.zero;\n    }\n}\n")
        log = ("using UnityEngine;\npublic class Log : MonoBehaviour {\n"
               "    int f;\n    void LateUpdate() {\n        f++;\n"
               "        Debug.Log(\"z\" + f + \" \" + Mathf.RoundToInt(transform.eulerAngles.z));\n"
               "    }\n}\n")
        root = project(self, {"Mgr": mgr, "Log": log}, [
            ("Mgr", None, "  other: {fileID: 111}\n"), ("Log",)])
        out = run_frames(self, pack(self, root), 3)
        for want in ("z1 45", "z2 90", "z3 0"):
            self.assertIn(want, out)

    @needs_cc
    def test_through_singleton(self):
        """`Hook.instance.trs.eulerAngles += ..` (`Lasso.instance.hookTrs`)."""
        hook = ("using UnityEngine;\npublic class Hook : MonoBehaviour {\n"
                "    public static Hook instance;\n    public Transform trs;\n"
                "    void Awake() { instance = this; }\n}\n")
        mgr = ("using UnityEngine;\npublic class Mgr : MonoBehaviour {\n"
               "    int f;\n    void Update() {\n        f++;\n"
               "        if (f <= 2) Hook.instance.trs.eulerAngles += "
               "Vector3.forward * 30f;\n    }\n}\n")
        log = ("using UnityEngine;\npublic class Log : MonoBehaviour {\n"
               "    int f;\n    void LateUpdate() {\n        f++;\n"
               "        Debug.Log(\"z\" + f + \" \" + Mathf.RoundToInt(transform.eulerAngles.z));\n"
               "    }\n}\n")
        root = project(self, {"Hook": hook, "Mgr": mgr, "Log": log}, [
            ("Mgr",), ("Hook", None, "  trs: {fileID: 121}\n"), ("Log",)])
        out = run_frames(self, pack(self, root), 2)
        for want in ("z1 30", "z2 60"):
            self.assertIn(want, out)


class TestDropAchievementsSounds(unittest.TestCase):
    """Slime Jump's StartJump: achievement and sound statements become `;`
    (lines kept); a HandleAchieve whose result is read stays."""

    def test_drop(self):
        src = ("void StartJump () {\n"
               "\tif (a) JumpAchievement.Instance.HandleAchieve ();\n"
               "\tJumpAchievement.JumpCount ++;\n"
               "\tAudioClip jumpSound = jumpSounds[Random.Range(0, 3)];\n"
               "\tSoundEffect s = AudioManager.instance.MakeSoundEffect("
               "jumpSound, Vector3.zero, v);\n"
               "\ts.audioSource.pitch = r.Get(Random.value);\n"
               "\ts.audioSource.spatialBlend = 0;\n"
               "\tbool b = WinAchievement.Instance.HandleAchieve();\n}\n")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            out = unity_pack._desugar_drop_achievements_sounds(src, "P.cs")
        self.assertEqual(out.count("\n"), src.count("\n"))
        self.assertIn("if (a) ;", out)
        for gone in ("JumpCount", "jumpSound", "audioSource", "SoundEffect"):
            self.assertNotIn(gone, out)
        self.assertIn("WinAchievement.Instance.HandleAchieve()", out)
        self.assertIn("lines 2, 3, 4, 5, 6, 7", err.getvalue())


class TestLocalNamedI(unittest.TestCase):
    """`for (int i = 0; ..) speed += 1f;`: the packed instance index is `i`,
    so the loop counter took its place and each pass bumped instance
    0, 1 (A0 11, A1 22) instead of this one twice."""

    @needs_cc
    def test_loop_counter_i(self):
        a = ("using UnityEngine;\npublic class A : MonoBehaviour {\n"
             "    public float speed;\n    int f;\n"
             "    void Update() { f++; if (f > 1) return;\n"
             "        for (int i = 0; i < 2; i++) speed += 1f;\n"
             "        Debug.Log(name + \" \" + speed); }\n}\n")
        root = project(self, {"A": a}, [("A", None, "  speed: 10\n"),
                                        ("A", None, "  speed: 20\n")])
        out = run_frames(self, pack(self, root), 1)
        self.assertIn("A0 12", out)
        self.assertIn("A1 22", out)


class TestStopSite(unittest.TestCase):
    """A runtime stop in a helper (`SpriteRenderer.bounds` of a renderer
    crust does not draw) names the C# call: script, line, column."""

    @needs_cc
    def test_helper_stop_has_site(self):
        a = ("using UnityEngine;\npublic class A : MonoBehaviour {\n"
             "    public SpriteRenderer sr;\n"
             "    void Update() {\n"
             "        float x = sr.bounds.extents.x;\n"
             "        Debug.Log(\"x \" + x);\n    }\n}\n")
        extra = ("--- !u!212 &{fid}\nSpriteRenderer:\n"
                 "  m_GameObject: {{fileID: {go}}}\n"
                 "  m_Sprite: {{fileID: 0}}\n")
        root = project(self, {"A": a}, [("A", extra, "  sr: {fileID: 103}\n")])
        out = pack(self, root)
        with open(os.path.join(out, "h.c"), "w") as f:
            f.write('#include "engine_draw.h"\nextern float Time_deltaTime;\n'
                    "int main(int c, char **v) { engine_apply_argv(c, v);\n"
                    "  Time_deltaTime = 1.f / 60.f; engine_tick(); return 0; }\n")
        exe = os.path.join(out, "h")
        r = subprocess.run([_CC, "-O1", "-w", "-I", out, "-o", exe,
                            os.path.join(out, "h.c"),
                            os.path.join(out, "engine.c"),
                            os.path.join(out, "data.c"), "-lm"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        run = subprocess.run([exe, "-logFile", "-"], capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(run.returncode, 70, run.stderr[-2000:])
        self.assertIn("SpriteRenderer.bounds", run.stderr)
        self.assertIn("A.Update () (at Assets/Scripts/A.cs:5)", run.stderr)


class TestTwoScriptsOneGameObject(unittest.TestCase):
    """Slime Jump's Player GO also carries AffectedByVortex: every script
    on a GO was folded into the first one's object (fields merged, the
    second had no row, `player.affectedByVortex` was null)."""

    @needs_cc
    def test_second_script_is_its_own_row(self):
        a = ("using UnityEngine;\npublic class A : MonoBehaviour {\n"
             "    public float speed;\n    public B b;\n    int f;\n"
             "    void Update() { f++; if (f > 1) return;\n"
             "        Debug.Log(\"s \" + speed + \" \" + b.speed + \" \" + "
             "GetComponent<B>().speed); }\n}\n")
        b = ("using UnityEngine;\npublic class B : MonoBehaviour {\n"
             "    public float speed;\n"
             "    void Start() { Debug.Log(\"b start \" + speed); }\n}\n")
        extra = ("--- !u!114 &{fid}\nMonoBehaviour:\n"
                 "  m_GameObject: {{fileID: {go}}}\n"
                 "  m_Script: {{fileID: 11500000, guid: %032x}}\n"
                 "  speed: 7\n" % 2)
        root = project(self, {"A": a, "B": b}, [
            ("A", extra, "  speed: 3\n  b: {fileID: 103}\n")])
        out = run_frames(self, pack(self, root), 1)
        self.assertIn("b start 7", out)
        self.assertIn("s 3 7 7", out)


class TestNullFieldRead(unittest.TestCase):
    """`other.v` through a null reference (Slime Jump's unset
    `affectedByVortex.velocity`) read out of bounds; it is Unity's NRE.
    This pack has no script unwinding, so it stops (exit 70) before the
    `read` log."""

    @needs_cc
    def test_nre_stops(self):
        o = ("using UnityEngine;\npublic class O : MonoBehaviour {\n"
             "    public Vector2 v;\n}\n")
        a = ("using UnityEngine;\npublic class A : MonoBehaviour {\n"
             "    public O other;\n    int f;\n"
             "    void Update() {\n        f++;\n"
             "        Debug.Log(\"tick \" + f);\n"
             "        float x = other.v.x;\n"
             "        Debug.Log(\"read \" + x);\n    }\n}\n")
        root = project(self, {"O": o, "A": a}, [("A", None, "  other: {fileID: 0}\n"),
                                                ("O",)])
        out = pack(self, root)
        with open(os.path.join(out, "h.c"), "w") as f:
            f.write('#include "engine_draw.h"\nextern float Time_deltaTime;\n'
                    "int main(int c, char **v) { engine_apply_argv(c, v);\n"
                    "  Time_deltaTime = 1.f / 60.f;\n"
                    "  engine_tick(); engine_tick(); return 0; }\n")
        exe = os.path.join(out, "h")
        r = subprocess.run([_CC, "-O1", "-w", "-I", out, "-o", exe,
                            os.path.join(out, "h.c"),
                            os.path.join(out, "engine.c"),
                            os.path.join(out, "data.c"), "-lm"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        run = subprocess.run([exe, "-logFile", "-"], capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(run.returncode, 70, run.stderr[-2000:])
        self.assertIn("NullReferenceException", run.stderr)
        self.assertIn("A.Update () (at Assets/Scripts/A.cs:8)", run.stderr)
        self.assertIn("tick 1", run.stdout)
        self.assertNotIn("read", run.stdout)

    @needs_cc
    def test_nre_stack_trace(self):
        """Unity's trace: the faulting method, then each caller at the
        line it called from, namespace-qualified."""
        o = ("using UnityEngine;\npublic class O : MonoBehaviour {\n"
             "    public Vector2 v;\n}\n")
        a = ("using UnityEngine;\nnamespace Game {\n"
             "public class A : MonoBehaviour {\n    public O other;\n"
             "    float Read() {\n        return other.v.x;\n    }\n"
             "    void Update() {\n        float x = Read();\n"
             "        Debug.Log(\"read \" + x);\n    }\n}\n}\n")
        root = project(self, {"O": o, "A": a}, [("A", None, "  other: {fileID: 0}\n"),
                                                ("O",)])
        out = pack(self, root)
        with open(os.path.join(out, "h.c"), "w") as f:
            f.write('#include "engine_draw.h"\nextern float Time_deltaTime;\n'
                    "int main(int c, char **v) { engine_apply_argv(c, v);\n"
                    "  Time_deltaTime = 1.f / 60.f;\n"
                    "  engine_tick(); return 0; }\n")
        exe = os.path.join(out, "h")
        r = subprocess.run([_CC, "-O1", "-w", "-I", out, "-o", exe,
                            os.path.join(out, "h.c"),
                            os.path.join(out, "engine.c"),
                            os.path.join(out, "data.c"), "-lm"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        run = subprocess.run([exe, "-logFile", "-"], capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(run.returncode, 70, run.stderr[-2000:])
        self.assertIn("NullReferenceException: Object reference not set to an "
                      "instance of an object\n"
                      "Game.A.Read () (at Assets/Scripts/A.cs:6)\n"
                      "Game.A.Update () (at Assets/Scripts/A.cs:9)\n",
                      run.stderr)


class TestMethodGroupStub(unittest.TestCase):
    """`Ev.Add(Show, t)` (Slime Jump's Achievement.OnAchieve) reached
    crust as an undeclared `Show`; it is a CS8000 stub."""

    def test_method_group(self):
        ev = ("using UnityEngine;\nusing System;\npublic class Ev : MonoBehaviour {\n"
              "    public static void Add(Action a, float t) { }\n}\n")
        a = ("using UnityEngine;\npublic class A : MonoBehaviour {\n"
             "    void Show() { }\n"
             "    void Update() { Ev.Add(Show, Time.time); }\n}\n")
        root = project(self, {"Ev": ev, "A": a}, [("A",)])
        out = tempfile.mkdtemp(prefix="upf-out-")
        self.addCleanup(shutil.rmtree, out, True)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            unity_pack.pack(root, out, force=True)
        self.assertIn("`A.Update` is not lowered yet (`Show`: Method group",
                      err.getvalue())


class TestVector2FieldAssign(unittest.TestCase):
    """A Vector2 field store was `set_x(..); set_y(..);`: an unbraced
    `if` / `else` around it did not compile (Slime Jump's `blasterLaunchVel
    *= ..`), and `v = new Vector2(v.y, v.x)` read the new x for y."""

    @needs_cc
    def test_swap_and_unbraced_else(self):
        src = ("using UnityEngine;\npublic class V : MonoBehaviour {\n"
               "    public Vector2 vel = new Vector2(1f, 2f);\n"
               "    bool flip = true;\n"
               "    void Update() {\n"
               "        if (flip)\n            vel = new Vector2(vel.y, vel.x);\n"
               "        else\n            vel *= 2f;\n        flip = false;\n"
               "        Debug.Log(\"v \" + Mathf.RoundToInt(vel.x) + \" \" + "
               "Mathf.RoundToInt(vel.y));\n    }\n}\n")
        root = project(self, {"V": src}, [("V", None, "  vel: {x: 1, y: 2}\n")])
        out = run_frames(self, pack(self, root), 2)
        self.assertIn("v 2 1", out)
        self.assertIn("v 4 2", out)


class TestSetWorldScaleOne(unittest.TestCase):
    """`trs.SetWorldScale(Vector3.one.SetX(x))` on a Transform field and
    `Hook.instance.trs.SetWorldScale(Vector3.one)` (Slime Jump's DoUpdate)."""

    @needs_cc
    def test_field_and_singleton(self):
        ext = ("using UnityEngine;\npublic static class Extensions {\n"
               "    public static Vector3 SetX(this Vector3 v, float x) "
               "{ v.x = x; return v; }\n"
               "    public static void SetWorldScale(this Transform t, Vector3 s)"
               " { t.localScale = s; }\n}\n")
        hook = ("using UnityEngine;\npublic class Hook : MonoBehaviour {\n"
                "    public static Hook instance;\n    public Transform trs;\n"
                "    void Awake() { instance = this; }\n}\n")
        mgr = ("using UnityEngine;\npublic class Mgr : MonoBehaviour {\n"
               "    public Transform colliderTrs;\n    public float xSize = 2f;\n"
               "    void Update() {\n"
               "        colliderTrs.SetWorldScale(Vector3.one.SetX(xSize));\n"
               "        Hook.instance.trs.SetWorldScale(Vector3.one);\n    }\n}\n")
        log = ("using UnityEngine;\npublic class Log : MonoBehaviour {\n"
               "    void LateUpdate() { Debug.Log(name + \" \" + Mathf.RoundToInt("
               "transform.localScale.x * 10f) + \" \" + Mathf.RoundToInt("
               "transform.localScale.y * 10f)); }\n}\n")
        root = project(self, {"Extensions": ext, "Hook": hook, "Mgr": mgr,
                              "Log": log}, [
            ("Mgr", None, "  colliderTrs: {fileID: 121}\n  xSize: 2\n"),
            ("Hook", None, "  trs: {fileID: 131}\n"), ("Log",), ("Log",)])
        out = run_frames(self, pack(self, root), 1)
        self.assertIn("Log2 20 10", out)
        self.assertIn("Log3 10 10", out)


class TestInheritedUpdatables(unittest.TestCase):
    """Slime Jump's update loop: `UpdateWhileEnabled.OnEnable` registers in
    `GM.updatables`, and the pack dispatches `DoUpdate` on each. A class
    inherited what it ran (`GCam : Cam : Single<Cam> : UWE`): a base was
    its own packed array, so GCam had no OnEnable / DoUpdate, `base.M()`
    was dropped, and a generic base's header named no base at all."""

    SCRIPTS = {
        "IUpdatable": "public interface IUpdatable { void DoUpdate(); }\n",
        "GM": """using UnityEngine;
public class GM : MonoBehaviour {
    public static IUpdatable[] updatables = new IUpdatable[0];
    void Update() {
        for (int i = 0; i < updatables.Length; i++) { IUpdatable u = updatables[i]; u.DoUpdate(); }
        Debug.Log("gm");
    }
}
""",
        "UWE": """using UnityEngine;
public class UWE : MonoBehaviour, IUpdatable {
    public virtual void OnEnable() { GM.updatables = GM.updatables.Add(this); }
    public virtual void DoUpdate() { }
}
""",
        "Single": """using UnityEngine;
public class Single<T> : UWE where T : UWE {
    public bool persistant;
    public virtual void Awake() { Debug.Log("single " + persistant); }
}
""",
        "Cam": """using UnityEngine;
public class Cam : Single<Cam> {
    public override void DoUpdate() { HandlePosition(); }
    public virtual void HandlePosition() { Debug.Log("cam pos"); }
}
""",
        "GCam": """using UnityEngine;
public class GCam : Cam {
    public override void Awake() { base.Awake(); Debug.Log("gcam awake"); }
    public override void HandlePosition() { Debug.Log("gcam pos"); base.HandlePosition(); }
}
""",
    }

    @needs_cc
    def test_inherited_members_run(self):
        root = project(self, self.SCRIPTS,
                       [("GM",), ("GCam", None, "  persistant: 1\n")])
        # GM.Update's loop leaves its body: engine_tick runs that dispatch
        lines = run_frames(self, pack(self, root), frames=2)
        self.assertEqual(lines[:2], ["single True", "gcam awake"])
        self.assertEqual(lines.count("gm"), 2)
        self.assertEqual(lines.count("gcam pos"), 2)
        self.assertEqual(lines.count("cam pos"), 2)



class TestOtherPosition(unittest.TestCase):
    """`target.position` through a Transform field, read as a vector value:
    it stopped at a member of the field's read and emptied the method
    (`target.position.x` was already lowered)."""

    @needs_cc
    def test_reads_the_other_objects_position(self):
        watcher = script("W", "Vector2 p = target.position; "
                              "Vector2 d = Vector2.MoveTowards(transform.position, target.position, 0.25f); "
                              'Debug.Log("t " + p.x + " " + p.y + " m " + d.x);',
                         members="    public Transform target;")
        dummy = script("D", "")
        # object 1 (D) sits at x = 1; its Transform is fileID 111
        root = project(self, {"W": watcher, "D": dummy},
                       [("W", None, "  target: {fileID: 111}\n"), ("D",)])
        lines = run_frames(self, pack(self, root))
        self.assertIn("t 1 0 m 0.25", lines)




class TestParentRotationScale(unittest.TestCase):
    """A child's world position is parent * (T R S): its offset turned by
    the parent's world rotation and stretched by its scale. It was the
    parents' positions summed -- a rotated or scaled parent left its
    children where an unrotated one would."""

    def _scene(self, rot_deg, scale):
        import math
        q = (0.0, 0.0, math.sin(math.radians(rot_deg) / 2), math.cos(math.radians(rot_deg) / 2))
        src = script("Kid", 'Debug.Log("w " + transform.position.x + " " + transform.position.y);')
        root = tempfile.mkdtemp(prefix="upf-xf-")
        self.addCleanup(shutil.rmtree, root, True)
        d = os.path.join(root, "Assets", "Scripts")
        os.makedirs(d)
        with open(os.path.join(d, "Kid.cs"), "w") as f:
            f.write(src)
        with open(os.path.join(d, "Kid.cs.meta"), "w") as f:
            f.write("guid: %032x\n" % 1)
        scene = ("%%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: Parent\n  m_Component:\n"
                 "  - component: {fileID: 11}\n--- !u!4 &11\nTransform:\n  m_GameObject: {fileID: 10}\n"
                 "  m_LocalRotation: {x: 0, y: 0, z: %r, w: %r}\n  m_LocalPosition: {x: 3, y: 0, z: 0}\n"
                 "  m_LocalScale: {x: %r, y: %r, z: 1}\n  m_Children:\n  - {fileID: 21}\n  m_Father: {fileID: 0}\n"
                 "--- !u!1 &20\nGameObject:\n  m_Name: Kid\n  m_Component:\n  - component: {fileID: 21}\n"
                 "  - component: {fileID: 22}\n--- !u!4 &21\nTransform:\n  m_GameObject: {fileID: 20}\n"
                 "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n  m_LocalPosition: {x: 1, y: 0, z: 0}\n"
                 "  m_LocalScale: {x: 1, y: 1, z: 1}\n  m_Children: []\n  m_Father: {fileID: 11}\n"
                 "--- !u!114 &22\nMonoBehaviour:\n  m_GameObject: {fileID: 20}\n"
                 "  m_Script: {fileID: 11500000, guid: %032x}\n" % (q[2], q[3], scale, scale, 1))
        os.makedirs(os.path.join(root, "Assets", "Scenes"))
        with open(os.path.join(root, "Assets", "Scenes", "S.unity"), "w") as f:
            f.write(scene)
        lines = run_frames(self, pack(self, root))
        return [float(v) for v in [l for l in lines if l.startswith("w ")][0].split()[1:]]

    @needs_cc
    def test_unity_semantics(self):
        # the parent at (3, 0); the child at local (1, 0)
        for rot, scale, want in ((0, 1, (4, 0)), (90, 1, (3, 1)),
                                 (0, 2, (5, 0)), (90, 2, (3, 2))):
            x, y = self._scene(rot, scale)
            self.assertAlmostEqual(x, want[0], places=4, msg=(rot, scale))
            self.assertAlmostEqual(y, want[1], places=4, msg=(rot, scale))


DESTRUCTION = os.environ.get("UNITY_2D_DESTRUCTION") or os.path.join(
    os.path.dirname(ROOT), "Unity-2D-Destruction")
needs_destruction = unittest.skipUnless(
    os.path.isdir(DESTRUCTION),
    "Unity-2D-Destruction not found: clone https://github.com/crustos/"
    "Unity-2D-Destruction beside crust (or set UNITY_2D_DESTRUCTION)")


@needs_destruction
class TestUnity2DDestruction(unittest.TestCase):
    """The crust port of Unity-2D-Destruction (its runtime scripts; the
    Editor code is not part of it). The files that translate stay
    translating; every other one is refused with a diagnostic, never a
    crash of the translator -- so the port's frontier is measured, and
    moves only forward. See UNITY_PACK.md, "Unity-2D-Destruction"."""

    BASE = "unity2DDestruction/Assets/2D_Destruction"
    TRANSLATE = (
        "Scripts/MaxInstancesAttribute.cs",
        "Unity-delaunay/Delaunay/Edge.cs",
        "Unity-delaunay/Delaunay/ICoord.cs",
        "Unity-delaunay/Delaunay/LR.cs",
        "Unity-delaunay/geom/Circle.cs",
        "Unity-delaunay/geom/LineSegment.cs",
        "Unity-delaunay/geom/Polygon.cs",
        "Unity-delaunay/geom/Winding.cs",
        "Unity-delaunay/utils/IDisposable.cs",
    )

    def _runtime_files(self):
        import glob
        base = os.path.join(DESTRUCTION, self.BASE)
        return sorted(f for f in glob.glob(os.path.join(base, "**", "*.cs"),
                                           recursive=True)
                      if os.sep + "Editor" + os.sep not in f)

    def test_these_translate(self):
        import tools.csrust as csrust
        for rel in self.TRANSLATE:
            path = os.path.join(DESTRUCTION, self.BASE, rel)
            with open(path, encoding="utf-8-sig") as f:
                csrust.translate(f.read(), path=os.path.basename(path))

    def test_the_rest_is_refused_not_crashed(self):
        import tools.csrust as csrust
        import tools.cs2cpp as cs2cpp
        import tools.cpprust as cpprust
        for path in self._runtime_files():
            with open(path, encoding="utf-8-sig") as f:
                src = f.read()
            try:
                csrust.translate(src, path=os.path.basename(path))
            except (cs2cpp.CsError, cpprust.CppError):
                pass                    # a refusal, with a line and a reason


import tools.unity.unity_pack_hybrid as unity_pack_hybrid  # noqa: E402

_hybrid_ok, _hybrid_why = unity_pack_hybrid.available()
needs_hybrid = unittest.skipUnless(_hybrid_ok, "--hybrid needs: " + _hybrid_why)


@needs_hybrid
class TestManagedShim(unittest.TestCase):
    """The managed UnityEngine (tools/unity/unity_pack_managed/UnityShim.cs) gives Unity's answers when it runs on DotNetAnywhere.  The packer is not in
    this: a console program is built against the shim and run.  The expected values are Unity's, worked by hand."""

    PROGRAM = """
using System;
using UnityEngine;
public class Check {
    static int R(float f) { return Mathf.RoundToInt(f); }
    public static void Main() {
        Vector3 v = Quaternion.Euler(0, 90, 0) * Vector3.forward;
        Console.WriteLine("a=" + R(v.x * 1000) + "," + R(v.y * 1000) + "," + R(v.z * 1000));
        Vector3 e = Quaternion.Euler(30, 60, 90).eulerAngles;
        Console.WriteLine("b=" + R(e.x) + "," + R(e.y) + "," + R(e.z));
        Console.WriteLine("c=" + R(Mathf.DeltaAngle(10f, 350f)) + "," + R(Mathf.Repeat(5.5f, 2f) * 10) + "," + R(Mathf.PingPong(2.5f, 2f) * 10));
        Vector2Int vi = new Vector2Int(3, 4) * 2 + Vector2Int.one;
        Vector3Int wi = new Vector3Int(1, 2, 3) + (Vector3Int)vi;
        Console.WriteLine("d=" + vi.x + "," + vi.y + "," + R(((Vector2)vi).magnitude * 100) + "," + wi.x + "," + wi.y + "," + wi.z);
        Vector3 le = Quaternion.LookRotation(Vector3.right, Vector3.up).eulerAngles;
        Console.WriteLine("e=" + R(le.x) + "," + R(le.y) + "," + R(le.z));
        Color c = Color.Lerp(Color.black, Color.white, 0.25f);
        Console.WriteLine("f=" + R(c.r * 100) + "," + R(c.a * 100));
        Vector3 cr = Vector3.Cross(Vector3.right, Vector3.up);
        Console.WriteLine("g=" + R(cr.z) + "," + R(Vector3.SignedAngle(Vector3.right, Vector3.up, Vector3.forward)) + "," + R(Vector2.SignedAngle(Vector2.right, Vector2.down)));
        Quaternion half = Quaternion.Slerp(Quaternion.identity, Quaternion.Euler(0, 0, 90), 0.5f);
        Console.WriteLine("h=" + R(half.eulerAngles.z) + "," + R(Quaternion.Angle(Quaternion.identity, Quaternion.Euler(0, 80, 0))));
        Rect rc = new Rect(1, 2, 4, 6);
        Bounds bd = new Bounds(new Vector3(1, 1, 1), new Vector3(2, 4, 6));
        Console.WriteLine("i=" + R(rc.center.x * 10) + "," + R(rc.center.y * 10) + "," + (rc.Contains(new Vector2(2, 3)) ? 1 : 0) + "," + R(bd.max.z) + "," + (bd.Contains(Vector3.zero) ? 1 : 0));
        Vector3 p = Vector3.ProjectOnPlane(new Vector3(1, 2, 3), Vector3.up);
        Vector3 rf = Vector3.Reflect(new Vector3(1, -1, 0), Vector3.up);
        Console.WriteLine("j=" + R(p.x) + "," + R(p.y) + "," + R(p.z) + "," + R(rf.x) + "," + R(rf.y) + "," + R(Mathf.MoveTowards(1f, 5f, 2f)) + "," + R(Mathf.LerpAngle(350f, 10f, 0.5f)));
        Quaternion inv = Quaternion.Inverse(Quaternion.Euler(10, 20, 30)) * Quaternion.Euler(10, 20, 30);
        Vector3 ft = Quaternion.FromToRotation(Vector3.up, Vector3.right) * Vector3.up;
        Console.WriteLine("k=" + R(Quaternion.Angle(inv, Quaternion.identity) * 100) + "," + R(ft.x * 1000) + "," + R(ft.y * 1000) + "," + (Mathf.Approximately(0.1f + 0.2f, 0.3f) ? 1 : 0));
        // LookRotation takes each of its four quaternion branches somewhere here: forward and up must land where they were asked to
        Vector3[] axes = { Vector3.right, Vector3.left, Vector3.up, Vector3.down, Vector3.forward, Vector3.back };
        int bad = 0, tested = 0;
        for (int xi = -1; xi <= 1; xi++) for (int yi = -1; yi <= 1; yi++) for (int zi = -1; zi <= 1; zi++) {
            if (xi == 0 && yi == 0 && zi == 0) continue;
            Vector3 f = new Vector3(xi, yi, zi).normalized;
            foreach (Vector3 up in axes) {
                if (Vector3.Cross(up, f).sqrMagnitude < 0.01f) continue;
                Quaternion q = Quaternion.LookRotation(f, up);
                Vector3 u = Vector3.Cross(f, Vector3.Cross(up, f).normalized);
                if ((q * Vector3.forward - f).magnitude > 1e-4f || (q * Vector3.up - u).magnitude > 1e-4f || Mathf.Abs(Quaternion.Dot(q, q) - 1f) > 1e-4f) bad++;
                tested++;
            }
        }
        Console.WriteLine("l=" + (bad == 0 && tested > 100 ? "ok" : "bad " + bad + " of " + tested));
    }
}
"""
    EXPECTED = ["a=1000,0,0", "b=30,60,90", "c=-20,15,15", "d=7,9,1140,8,11,3", "e=0,90,0", "f=25,100", "g=1,90,-90", "h=45,80", "i=30,50,1,4,1",
                "j=1,0,3,1,1,3,360", "k=0,1000,0,1", "l=ok"]

    def test_the_maths_gives_unitys_answers_on_dotnetanywhere(self):
        corlib = unity_pack_hybrid.prepare_corlib()
        dna = os.path.join(unity_pack_hybrid.build_dir(), "dna")
        if not os.path.exists(dna):
            unity_pack_hybrid._dna_run([])
        work = tempfile.mkdtemp(prefix="upack-shim-")
        self.addCleanup(shutil.rmtree, work, True)
        src = os.path.join(work, "Check.cs")
        with open(src, "w") as f:
            f.write(self.PROGRAM)
        exe = os.path.join(work, "Check.exe")
        ok, msg = unity_pack_hybrid.compile_managed([unity_pack_hybrid.SHIM, src], exe, corlib)
        self.assertTrue(ok, msg)
        shutil.copy(corlib, work)
        r = subprocess.run([dna, exe], capture_output=True, text=True, cwd=work, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout[-800:] + r.stderr[-800:])
        lines = [l for l in r.stdout.splitlines() if "=" in l and not l.startswith("Total")]
        self.assertEqual(lines, self.EXPECTED)

    @needs_cc
    def test_the_maths_matches_the_packers_own_c(self):
        """a method gives the same answer managed as lowered: the shim's Mathf against the C unity_pack lowers the same calls to
        (tools/unity/unity_pack_runtime.py), over a sweep of inputs, as results scaled by 1000 and rounded"""
        import tools.unity.unity_pack_runtime as rt
        funcs = [("Repeat", 2), ("PingPong", 2), ("DeltaAngle", 2), ("SmoothStep", 3), ("MoveTowards", 3), ("InverseLerp", 3),
                 ("LerpUnclamped", 3), ("Clamp01", 1), ("Round", 1), ("Approximately", 2)]
        two = [-725.5, -360.0, -10.0, -0.5, 0.0, 2.5, 180.0, 359.9, 725.5]
        three = [-3.0, 0.0, 0.25, 1.0, 4.5]
        sets = {1: [(a,) for a in two], 2: [(a, b) for a in two for b in two], 3: [(a, b, c) for a in three for b in three for c in three]}

        def lit(v):
            return repr(float(v)) + "f"
        c_lines, cs_lines = [], []
        for name, n in funcs:
            for args in sets[n]:
                if name in ("Repeat", "PingPong") and args[1] <= 0.0:
                    continue                                    # (a length that is not positive: undefined, in Unity too)
                if name == "InverseLerp" and args[0] == args[1]:
                    continue
                key = "%s%s" % (name, args)
                call_c = "Mathf_%s(%s)" % (name, ", ".join(lit(a) for a in args))
                call_cs = "Mathf.%s(%s)" % (name, ", ".join(lit(a) for a in args))
                if name == "Approximately":
                    c_lines.append('  printf("%s=%%ld\\n", (long)(%s != 0));' % (key, call_c))
                    cs_lines.append('        Console.WriteLine("%s=" + (%s ? 1 : 0));' % (key, call_cs))
                else:
                    c_lines.append('  printf("%s=%%ld\\n", (long)floor((double)(%s) * 1000.0 + 0.5));' % (key, call_c))
                    cs_lines.append('        Console.WriteLine("%s=" + (long)Math.Floor((double)(%s) * 1000.0 + 0.5));' % (key, call_cs))
        helpers = rt.closure(["Mathf_" + n for n, _k in funcs])
        c_src = ("#include <stdio.h>\n#include <math.h>\n" + "\n".join(rt.helper_c(h) for h in helpers)
                 + "\nint main(void) {\n" + "\n".join(c_lines) + "\n  return 0;\n}\n")
        cs_src = "using System;\nusing UnityEngine;\npublic class Sweep {\n    public static void Main() {\n" + "\n".join(cs_lines) + "\n    }\n}\n"
        work = tempfile.mkdtemp(prefix="upack-shim-")
        self.addCleanup(shutil.rmtree, work, True)
        with open(os.path.join(work, "ref.c"), "w") as f:
            f.write(c_src)
        r = subprocess.run([_CC, "-O0", "-w", "-o", os.path.join(work, "ref"), os.path.join(work, "ref.c"), "-lm"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        want = subprocess.run([os.path.join(work, "ref")], capture_output=True, text=True).stdout.splitlines()
        corlib = unity_pack_hybrid.prepare_corlib()
        dna = os.path.join(unity_pack_hybrid.build_dir(), "dna")
        if not os.path.exists(dna):
            unity_pack_hybrid._dna_run([])
        with open(os.path.join(work, "Sweep.cs"), "w") as f:
            f.write(cs_src)
        exe = os.path.join(work, "Sweep.exe")
        ok, msg = unity_pack_hybrid.compile_managed([unity_pack_hybrid.SHIM, os.path.join(work, "Sweep.cs")], exe, corlib)
        self.assertTrue(ok, msg)
        shutil.copy(corlib, work)
        r = subprocess.run([dna, exe], capture_output=True, text=True, cwd=work, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-800:] + r.stderr[-800:])
        got = [l for l in r.stdout.splitlines() if "=" in l and not l.startswith("Total")]
        self.assertGreater(len(want), 150)
        # (a result that differs by one scaled unit is float rounding between sinf-free C and the managed double path; more is a real difference)
        bad = []
        for w, g in zip(want, got):
            kw, vw = w.rsplit("=", 1)
            kg, vg = g.rsplit("=", 1)
            if kw != kg or abs(int(vw) - int(vg)) > 1:
                bad.append("%s  C:%s managed:%s" % (kw, vw, vg))
        self.assertEqual(len(want), len(got))
        self.assertEqual(bad, [], "the shim's Mathf differs from the C unity_pack lowers it to")


class _PackHarness(unittest.TestCase):
    """pack a one-script project, play it, probe values."""

    SCENE = (
        "%%YAML 1.1\n--- !u!1 &1\nGameObject:\n  m_Name: Spark\n  m_Component:\n"
        "  - component: {fileID: 2}\n  - component: {fileID: 3}\n"
        "--- !u!4 &2\nTransform:\n  m_GameObject: {fileID: 1}\n"
        "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
        "--- !u!114 &3\nMonoBehaviour:\n  m_GameObject: {fileID: 1}\n"
        "  m_Script: {fileID: 11500000, guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}\n%s")

    def _project(self, script, values="  hp: 5\n  speed: 3\n"):
        root = tempfile.mkdtemp(prefix="upack-hy-")
        self.addCleanup(shutil.rmtree, root, True)
        s = os.path.join(root, "Assets", "Scripts")
        os.makedirs(s)
        with open(os.path.join(s, "Spark.cs"), "w") as f:
            f.write(script)
        with open(os.path.join(s, "Spark.cs.meta"), "w") as f:
            f.write("guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
        sc = os.path.join(root, "Assets", "Scenes")
        os.makedirs(sc)
        with open(os.path.join(sc, "S.unity"), "w") as f:
            f.write(self.SCENE % values)
        return root

    def _pack(self, root, **kw):
        out = tempfile.mkdtemp(prefix="upack-hy-out-")
        self.addCleanup(shutil.rmtree, out, True)
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                unity_pack.pack(root, out, force=True, **kw)
        except unity_pack.PackError as e:
            e.stderr = err.getvalue()
            raise
        return out, err.getvalue()

    def _play(self, out, frames=4, probe=None, setup="", each="", post=""):
        """link the player (DotNetAnywhere in it, if the pack is hybrid) and run `frames` ticks from another directory: it must find its
        managed assembly and corlib.dll beside itself, wherever it is started.  *setup* is C run before the first tick (set an engine_input_ global), *each* before every tick (`f` is the frame, from 1).  *probe* is a C float expression over engine.c's own
        (static) functions, `Spark_get_pos_y(0)`, printed with %.4f after each tick (a list of them: comma-separated): the harness then
        includes engine.c itself."""
        with open(os.path.join(out, "harness.c"), "w") as f:
            if probe:
                probes = [probe] if isinstance(probe, str) else list(probe)
                with open(os.path.join(out, "engine.c")) as ef:
                    etext = ef.read()
                # (the host defines the pointer: a player whose UI ticks needs it)
                host = "".join("%s;\n" % d.replace("extern ", "", 1) for d in re.findall(r"^extern (?:float|int|unsigned char) engine_pointer_\w+;", etext, re.M))
                f.write('#include <stdio.h>\n#include "engine.c"\n' + host + post +
                        "int main(int argc, char **argv) { int f;\n"
                        "  engine_apply_argv(argc, argv); Time_deltaTime = 1.f / 60.f; %s\n"
                        '  for (f = 1; f <= %d; f++) { %s engine_tick(); printf("%s\\n", %s); } return 0; }\n'
                        % (setup, frames, each, ",".join(["%.4f"] * len(probes)), ", ".join("(double)(%s)" % q for q in probes)))
            else:
                f.write('#include "engine_draw.h"\nextern float Time_deltaTime;\n'
                        "int main(int argc, char **argv) { int f;\n"
                        "  engine_apply_argv(argc, argv); Time_deltaTime = 1.f / 60.f;\n"
                        "  for (f = 1; f <= %d; f++) engine_tick(); return 0; }\n" % frames)

        def run(cmd):
            r = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        objs, libs = unity_pack_hybrid.link_inputs(out, _CC, run, lambda m: None)
        exe = os.path.join(out, "h")
        run([_CC, "-O0", "-w", "-I", out, "-o", exe, os.path.join(out, "harness.c")]
            + ([] if probe else [os.path.join(out, "engine.c")]) + [os.path.join(out, "data.c")] + objs + libs + ["-lm"])
        other = tempfile.mkdtemp(prefix="upack-hy-cwd-")
        self.addCleanup(shutil.rmtree, other, True)
        r = subprocess.run([exe, "-logFile", "-"], cwd=other, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-1000:])
        return r.stdout.split("\n")[:-1]

    def _script(self, members):
        return "using UnityEngine;\nusing System;\npublic class Spark : MonoBehaviour {\n" + members + "\n}\n"



@needs_cc
@needs_hybrid
class TestHybrid(_PackHarness):
    """--hybrid: a script method the packer cannot lower runs as managed code on DotNetAnywhere, from its own C# source, over the same packed state.

    Without it such a method is an empty stub and a CS8000 warning (the player prints `total=0` below).  With it the method runs: it reads
    what native code wrote and native code reads what it wrote.  And a class whose managed code cannot be built keeps its stub: --hybrid never
    changes what a pack that worked did."""

    # Update is lowered to C; Tally has a lambda, which the lowering cannot take.  hp starts at 5 and Update takes one off first.
    TALLY = """
    public int hp = 3;
    public float speed = 2f;
    public int total;
    void Update() {
        transform.position += new Vector3(speed * Time.deltaTime, 0, 0);
        hp -= 1;
        Tally();
        Debug.Log("hp=" + hp + " total=" + total);
    }
    public void Tally() {
        Func<int, int> sq = x => x * x + hp;
        total = sq(3) + sq(4);
    }"""

    VEC = """
    public float a; public float b;
    Vector2 Base(Vector2 v) { return v + new Vector2(1, 1); }
    void Update() {
        Vector2 r = Twice(new Vector2(3, 4));
        a = r.x; b = r.y;
    }
    Vector2 Twice(Vector2 v) {
        Func<float, float> f = x => x * 2f;
        Vector2 w = Base(v);
        return new Vector2(f(w.x), f(w.y));
    }"""

    def test_vector2_crosses_the_bridge_both_ways(self):
        """Twice is a stub (a lambda), run managed: a Vector2 goes in, one comes back, and it calls the lowered Base (a Vector2 each way)."""
        out, err = self._pack(self._project(self._script(self.VEC)), hybrid=True)
        self.assertNotIn("CS8000", err)
        self.assertEqual(self._play(out, 2, ["Spark_get_a(0)", "Spark_get_b(0)"]), ["8.0000,10.0000"] * 2)

    VEC_FIELD = """
    public Vector2 ms = new Vector2(1f, 1f);
    public float a;
    void Update() {
        ms = new Vector2(ms.x + 1f, ms.y * 2f);
        a = ms.x + ms.y;
    }"""

    def test_a_vector2_field_is_a_managed_property_over_its_two_floats(self):
        """the same class, packed lowered and packed managed, gives the same numbers: the embedded Vector2 reads and writes through its x/y accessors"""
        probes = ["Spark_get_a(0)", "Spark_get_ms_x(0)", "Spark_get_ms_y(0)"]
        root = self._project(self._script(self.VEC_FIELD))
        low, _ = self._pack(root)
        man, err = self._pack(root, hybrid=True, managed="Spark")
        self.assertNotIn("stays lowered", err)
        self.assertIn("hybrid: 1 managed method(s)", err)
        self.assertEqual(self._play(man, 3, probes), self._play(low, 3, probes))
        self.assertEqual(self._play(man, 3, probes)[2], "12.0000,4.0000,8.0000")

    COMPILE_FAIL = """
    Sprite[] frames;
    public int n;
    void Update() { n += 1; }
    public void Play(Sprite[] nf) { if (nf == frames) return; frames = nf; n += 10; }
    """

    def test_a_method_whose_lowered_c_does_not_compile_is_retried_not_fatal(self):
        """`nf == frames` on two arrays lowers to C crust refuses.  A plain pack still fails there (nothing changes for it); with --hybrid the
        method is treated as one the lowering could not do (managed code where that builds, else a CS8000 stub) and the rest of the class packs."""
        root = self._project(self._script(self.COMPILE_FAIL))
        with self.assertRaises(unity_pack.PackError) as plain:
            self._pack(root)
        self.assertIn("comparison between distinct pointer types", str(plain.exception))
        out, err = self._pack(root, hybrid=True)
        self.assertIn("the lowered C does not compile", err)
        self.assertIn("`Spark.Play` is not lowered yet", err)
        # Update (lowered fine) still runs; Play is the empty stub the warning names
        self.assertEqual(self._play(out, 3, ["Spark_get_n(0)"]), ["1.0000", "2.0000", "3.0000"])

    SPR_SCRIPT = """
    public SpriteRenderer sr;
    public float a;
    public float b;
    void Update() {
        Func<int, int> f = q => q;
        a = sr.color.g;
        sr.color = new Color(0.25f, sr.color.g * 0.5f, 1f, 0.5f);
        b = sr.color.a;
    }"""

    def test_a_spriterenderer_field_has_its_tint_managed(self):
        """the one thing the engine keeps for a renderer, its colour: managed code reads and writes it through the engine's own functions,
        and gets the same numbers the lowered code gets (the field is a GameObject index in both)"""
        scene = self.SCENE.replace("  - component: {fileID: 3}\n", "  - component: {fileID: 3}\n  - component: {fileID: 4}\n")
        values = ("  hp: 5\n  sr: {fileID: 4}\n--- !u!212 &4\nSpriteRenderer:\n  m_GameObject: {fileID: 1}\n"
                  "  m_Enabled: 1\n  m_Color: {r: 1, g: 0.5, b: 1, a: 1}\n  m_Sprite: {fileID: 0}\n")
        self.SCENE = scene
        root = self._project(self._script(self.SPR_SCRIPT), values)
        out, err = self._pack(root, hybrid=True)
        self.assertIn("hybrid: 1 managed method(s)", err)
        self.assertEqual(self._play(out, 3, ["Spark_get_a(0)", "Spark_get_b(0)"]),
                         ["0.5000,0.5000", "0.2500,0.5000", "0.1250,0.5000"])

    def test_a_spriterenderer_member_the_engine_has_no_storage_for_stays_lowered(self):
        """flipX, sprite, sortingOrder: the packed engine keeps none, so the managed SpriteRenderer has none and the method keeps its stub and says why"""
        scene = self.SCENE.replace("  - component: {fileID: 3}\n", "  - component: {fileID: 3}\n  - component: {fileID: 4}\n")
        values = ("  hp: 5\n  sr: {fileID: 4}\n--- !u!212 &4\nSpriteRenderer:\n  m_GameObject: {fileID: 1}\n"
                  "  m_Enabled: 1\n  m_Color: {r: 1, g: 0.5, b: 1, a: 1}\n  m_Sprite: {fileID: 0}\n")
        self.SCENE = scene
        body = self.SPR_SCRIPT.replace("a = sr.color.g;", "a = sr.color.g; sr.flipX = true;")
        out, err = self._pack(self._project(self._script(body), values), hybrid=True)
        self.assertNotIn("hybrid: 1 managed method(s)", err)
        self.assertIn("flipX", err)

    XF_FIELD = """
    public Transform gt;
    public float a;
    void Update() {
        gt.position = new Vector3(gt.position.x + 1f, 2f, 0f);
        a = gt.position.x + gt.position.y;
    }"""

    def test_a_transform_field_is_the_world_position_of_its_object(self):
        """a Transform field naming this object: managed code reads and writes its position (the lowered code stubs the write)"""
        root = self._project(self._script(self.XF_FIELD), "  hp: 5\n  gt: {fileID: 2}\n")
        out, err = self._pack(root, hybrid=True)
        self.assertIn("hybrid: 1 managed method(s)", err)
        self.assertEqual(self._play(out, 3, ["Spark_get_a(0)", "Spark_get_pos_x(0)", "Spark_get_pos_y(0)"]),
                         ["3.0000,1.0000,2.0000", "4.0000,2.0000,2.0000", "5.0000,3.0000,2.0000"])

    def test_a_transform_field_asked_for_more_than_its_position_stays_lowered(self):
        root = self._project(self._script("public Transform gt;\npublic float a;\nvoid Update() { a = gt.position.x + gt.localPosition.x; Func<int,int> f = q => q; }"),
                             "  hp: 5\n  gt: {fileID: 2}\n")
        out, err = self._pack(root, hybrid=True)
        self.assertIn("Transform field (gt)", err)

    RB_SCRIPT = """
    public Rigidbody2D rb;
    public Vector2 ms = new Vector2(2f, 0f);
    public float a;
    void Update() {
        rb.linearVelocity = new Vector2(ms.x, rb.linearVelocity.y);
        rb.AddForce(new Vector2(0f, 50f), ForceMode2D.Force);
        rb.gravityScale = 0.5f;
        rb.mass = 2f;
        a = rb.mass + rb.position.x + rb.gravityScale;
    }"""

    def _rb_project(self, script):
        root = self._project(self._script(script), "  hp: 5\n  rb: {fileID: 4}\n")
        sc = os.path.join(root, "Assets", "Scenes", "S.unity")
        with open(sc) as f:
            t = f.read().replace("  - component: {fileID: 3}\n", "  - component: {fileID: 3}\n  - component: {fileID: 4}\n", 1)
        with open(sc, "w") as f:
            f.write(t + "--- !u!50 &4\nRigidbody2D:\n  m_GameObject: {fileID: 1}\n  m_BodyType: 0\n  m_Mass: 1\n  m_GravityScale: 1\n")
        return root

    def test_a_rigidbody2d_field_runs_managed_like_it_runs_lowered(self):
        """velocity, AddForce, gravityScale, mass and position of a Rigidbody2D, and an embedded Vector2: the same numbers lowered and managed"""
        root = self._rb_project(self.RB_SCRIPT)
        low, _ = self._pack(root)
        man, err = self._pack(root, hybrid=True, managed="Spark")
        self.assertNotIn("stays lowered", err)
        self.assertIn("hybrid: 1 managed method(s)", err)
        probes = ["Spark_get_a(0)", "_Rigidbody2D_vel_x[0]", "_Rigidbody2D_vel_y[0]", "Rigidbody2D_position_x(0)", "Rigidbody2D_position_y(0)"]
        stub = "void engine_box2d_step(void) { }\n"      # (no Box2D in this harness: a body keeps what the script sets)
        got, want = self._play(man, 12, probes, post=stub), self._play(low, 12, probes, post=stub)
        self.assertEqual(got, want)
        self.assertNotEqual(got[-1].split(",")[1], "0.0000", "the velocity was set")

    KB = """
    public float held; public float pressed; public float conn;
    void Update() {
        conn = Keyboard.current == null ? 0f : 1f;
        if (Keyboard.current == null) return;
        if (Keyboard.current.spaceKey.isPressed) held += 1f;
        if (Keyboard.current.spaceKey.wasPressedThisFrame) pressed += 1f;
        Func<float, float> f = q => q;
    }"""

    def test_keyboard_current_reads_the_engines_own_key_state(self):
        """the Input System keyboard in managed code: connected or not, isPressed and wasPressedThisFrame, as the lowered code reads them"""
        root = self._project("using UnityEngine.InputSystem;\n" + self._script(self.KB))
        man, err = self._pack(root, hybrid=True, managed="Spark")
        self.assertNotIn("stays lowered", err)
        self.assertIn("hybrid: 1 managed method(s)", err)
        probes = ["Spark_get_conn(0)", "Spark_get_held(0)", "Spark_get_pressed(0)"]
        # no keyboard for the first frame, then it is connected and the space bar is down on frames 3-4 and 6
        each = "engine_keyboard_connected = f > 1; engine_keyboard_space = (f == 3 || f == 4 || f == 6);"
        got = self._play(man, 7, probes, each=each)
        # (the lambda keeps this method from lowering: the numbers are what the same code does lowered, worked out by hand:
        # held on frames 3, 4, 6; pressed on 3 and 6)
        self.assertEqual(got[0], "0.0000,0.0000,0.0000")
        self.assertEqual(got[-1], "1.0000,3.0000,2.0000")

    HELPERS = """
    public float a;
    void Update() { a = Calc(3f); }
    float Calc(float v) { Func<float, float> f = q => Util.Sq(q); return f(v) + new Acc(2f).Add(v); }"""

    def _helper_project(self, helper_src):
        root = self._project(self._script(self.HELPERS))
        with open(os.path.join(root, "Assets", "Scripts", "Util.cs"), "w") as f:
            f.write(helper_src)
        return root

    def test_project_helper_classes_are_built_into_the_managed_assembly(self):
        """Calc is a stub (a lambda) that names a static utility and a plain class from other files: both are compiled in, and it runs."""
        root = self._helper_project(
            "using UnityEngine;\npublic static class Util { public static float Sq(float x) { return x * x; } }\n"
            "public class Acc { float b; public Acc(float b) { this.b = b; } public float Add(float v) { return v + b; } }\n")
        out, err = self._pack(root, hybrid=True)
        self.assertNotIn("CS8000", err)
        self.assertEqual(self._play(out, 2, ["Spark_get_a(0)"]), ["14.0000"] * 2)

    def test_an_extension_method_pulls_in_its_file_and_a_tuple_in_it_compiles(self):
        """`v.Twice()` names no class: the file that declares the extension is compiled in, and a (float, int) method in it builds (ValueTuple)"""
        root = self._helper_project(
            "using UnityEngine;\npublic static class Util {\n"
            "    public static float Sq(this float x) { return x * x; }\n"
            "    public static (float, int) Pair(float f, int n) { return (f, n); }\n}\n"
            "public class Acc { float b; public Acc(float b) { this.b = b; } public float Add(float v) { return v + b; } }\n")
        with open(os.path.join(root, "Assets", "Scripts", "Spark.cs"), "w") as f:
            f.write(self._script("""
    public float a;
    void Update() { a = Calc(3f); }
    float Calc(float v) { Func<float, float> f = q => q.Sq(); return f(v) + new Acc(2f).Add(v); }"""))
        out, err = self._pack(root, hybrid=True)
        self.assertNotIn("CS8000", err)
        self.assertEqual(self._play(out, 2, ["Spark_get_a(0)"]), ["14.0000"] * 2)

    def test_a_helper_the_shim_cannot_build_leaves_the_method_a_stub(self):
        root = self._helper_project(
            "using UnityEngine;\npublic static class Util { public static float Sq(float x) { return Application.isPlaying ? x * x : 0f; } }\n"
            "public class Acc { float b; public Acc(float b) { this.b = b; } public float Add(float v) { return v + b; } }\n")
        out, err = self._pack(root, hybrid=True)
        self.assertIn("warning CS8000", err)
        self.assertIn("hybrid: the managed code does not compile", err)

    INPUT = """
    public float a; public float d; public float k;
    void Update() {
        a = Input.GetAxis("Horizontal") * 2f;
        d += Input.GetButtonDown("Jump") ? 10f : 0f;
        k += Input.GetKey("a") ? 1f : 0f;
    }"""

    def test_managed_input_reads_the_engines_axes_buttons_and_keys(self):
        """the same script, lowered and managed: axis 0.5, Jump held from the start (Down on the first frame only), key 'a' held."""
        root = self._project(self._script(self.INPUT))
        probe = ["Spark_get_a(0)", "Spark_get_d(0)", "Spark_get_k(0)"]
        setup = "engine_input_axis_Horizontal = 0.5f; engine_input_button_Jump = 1; engine_input_key['a'] = 1;"
        want = ["1.0000,10.0000,1.0000", "1.0000,10.0000,2.0000", "1.0000,10.0000,3.0000"]
        plain, _ = self._pack(root)
        self.assertEqual(self._play(plain, 3, probe, setup), want)
        managed, err = self._pack(root, managed="*")
        self.assertNotIn("stays lowered", err)
        self.assertEqual(self._play(managed, 3, probe, setup), want)

    CAMERA_SCENE = (
        "--- !u!1 &9000\nGameObject:\n  m_Name: Main Camera\n  m_Component:\n  - component: {fileID: 9001}\n  - component: {fileID: 9002}\n"
        "  m_TagString: MainCamera\n--- !u!4 &9001\nTransform:\n  m_GameObject: {fileID: 9000}\n  m_LocalPosition: {x: 1, y: 2, z: -10}\n"
        "  m_Father: {fileID: 0}\n--- !u!20 &9002\nCamera:\n  m_GameObject: {fileID: 9000}\n  m_Orthographic: 1\n  m_OrthographicSize: 5\n")

    CAMERA = """
    public float a;
    void Update() {
        transform.position = new Vector3(Camera.main.orthographicSize + Camera.main.transform.position.x,
                                         Camera.main.transform.position.y + Camera.main.nearClipPlane,
                                         RenderSettings.ambientLight.r + Camera.main.transform.position.z);
    }"""

    def test_managed_camera_and_ambient_light_read_what_the_lowered_engine_reads(self):
        root = self._project(self._script(self.CAMERA))
        with open(os.path.join(root, "Assets", "Scenes", "S.unity"), "a") as f:
            f.write(self.CAMERA_SCENE)
        probe = ["Spark_get_pos_x(0)", "Spark_get_pos_y(0)", "Spark_get_pos_z(0)"]
        setup = "RenderSettings_ambient_r = 0.25f;"
        plain, _ = self._pack(root)
        want = self._play(plain, 2, probe, setup)
        self.assertEqual(want[0].split(",")[0], "6.0000")           # (5 + 1: Unity's own answer)
        managed, err = self._pack(root, managed="*")
        self.assertNotIn("stays lowered", err)
        self.assertEqual(self._play(managed, 2, probe, setup), want)

    def test_managed_camera_writes_reach_the_engines_globals(self):
        body = """
    public float a;
    void Update() {
        Camera.main.orthographicSize = 3f;
        Camera.main.transform.position = new Vector3(7f, 8f, -9f);
        RenderSettings.ambientLight = new Color(0.5f, 0.25f, 0.125f);
        transform.position = new Vector3(0f, 0f, 0f);
    }"""
        root = self._project(self._script(body))
        with open(os.path.join(root, "Assets", "Scenes", "S.unity"), "a") as f:
            f.write(self.CAMERA_SCENE)
        out, err = self._pack(root, managed="*")
        self.assertNotIn("stays lowered", err)
        got = self._play(out, 1, ["Camera_main_orthographicSize", "Camera_main_pos_x", "Camera_main_pos_y", "Camera_main_pos_z",
                                  "RenderSettings_ambient_r", "RenderSettings_ambient_g", "RenderSettings_ambient_b"])
        self.assertEqual(got, ["3.0000,7.0000,8.0000,-9.0000,0.5000,0.2500,0.1250"])

    def test_a_camera_member_the_shim_lacks_leaves_the_method_a_stub(self):
        body = """
    public float a;
    void Update() {
        a = Camera.main.fieldOfView;
        transform.position = new Vector3(a, 0f, 0f);
    }"""
        root = self._project(self._script(body))
        with open(os.path.join(root, "Assets", "Scenes", "S.unity"), "a") as f:
            f.write(self.CAMERA_SCENE)
        out, err = self._pack(root, managed="*")
        # (the packer cannot lower Camera.main.fieldOfView either: the method stays the stub it was, with the managed reason)
        self.assertIn("warning CS8000", err)
        self.assertIn("'Camera' does not contain a definition for 'fieldOfView'", err)

    KEYS = """
    public float k; public float dn; public float up; public float db;
    void Update() {
        if (Input.GetKey(KeyCode.A)) k += 1f;
        if (Input.GetKeyDown(KeyCode.Space)) dn += 1f;
        if (Input.GetKeyUp(KeyCode.B)) up += 1f;
        if (Input.GetKeyDown("b")) db += 1f;
    }"""

    def test_managed_key_codes_and_key_down_up(self):
        """KeyCode and GetKeyDown / GetKeyUp are stubs in the lowered pack; managed they work, over a latch run at the top of every tick:
        A is held throughout, B is pressed on frame 1 and released on 2, space is pressed on 2 and released on 3."""
        root = self._project(self._script(self.KEYS))
        out, err = self._pack(root, hybrid=True)
        self.assertNotIn("stays lowered", err)
        got = self._play(out, 3, ["Spark_get_k(0)", "Spark_get_dn(0)", "Spark_get_up(0)", "Spark_get_db(0)"],
                         "engine_input_key['a'] = 1;", "engine_input_key['b'] = (f == 1); engine_input_key[' '] = (f == 2);")
        self.assertEqual(got, ["1.0000,0.0000,0.0000,1.0000", "2.0000,1.0000,1.0000,1.0000", "3.0000,1.0000,1.0000,1.0000"])

    def _physics_project(self, body):
        """Spark with a 3D Rigidbody (so the pack has Physics.gravity)"""
        root = self._project(self._script(body))
        sc = os.path.join(root, "Assets", "Scenes", "S.unity")
        with open(sc) as f:
            text = f.read()
        text = text.replace("  - component: {fileID: 3}\n", "  - component: {fileID: 3}\n  - component: {fileID: 4}\n", 1)
        with open(sc, "w") as f:
            f.write(text + "--- !u!54 &4\nRigidbody:\n  m_GameObject: {fileID: 1}\n  m_Mass: 1\n  m_UseGravity: 1\n")
        return root

    GRAVITY = """
    public float a;
    void Update() {
        a = Physics.gravity.y;
        transform.position = new Vector3(Physics.gravity.x, a, Physics.gravity.z);
    }"""

    def test_managed_physics_gravity_reads_the_engines_gravity(self):
        root = self._physics_project(self.GRAVITY)
        probe = ["Spark_get_pos_x(0)", "Spark_get_pos_y(0)", "Spark_get_pos_z(0)"]
        plain, _ = self._pack(root)
        want = self._play(plain, 1, probe, "Physics_gravity_x = 1.5f; Physics_gravity_z = -2.5f;")
        self.assertEqual(want[0].split(",")[0], "1.5000")
        managed, err = self._pack(root, managed="*")
        self.assertNotIn("stays lowered", err)
        self.assertEqual(self._play(managed, 1, probe, "Physics_gravity_x = 1.5f; Physics_gravity_z = -2.5f;"), want)

    def test_managed_physics_gravity_can_be_set(self):
        body = """
    public float a;
    void Update() {
        Physics.gravity = new Vector3(1f, -2f, 3f);
        transform.position = new Vector3(0f, 0f, 0f);
    }"""
        out, err = self._pack(self._physics_project(body), managed="*")
        self.assertNotIn("stays lowered", err)
        self.assertEqual(self._play(out, 1, ["Physics_gravity_x", "Physics_gravity_y", "Physics_gravity_z"]), ["1.0000,-2.0000,3.0000"])

    HOLDER = ("--- !u!1 &10\nGameObject:\n  m_Name: Holder\n  m_Component:\n  - component: {fileID: 11}\n  - component: {fileID: 12}\n"
              "--- !u!4 &11\nTransform:\n  m_GameObject: {fileID: 10}\n  m_LocalPosition: {x: 5, y: 6, z: 0}\n  m_Father: {fileID: 0}\n"
              "--- !u!114 &12\nMonoBehaviour:\n  m_GameObject: {fileID: 10}\n"
              "  m_Script: {fileID: 11500000, guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}\n")

    def _hierarchy_project(self, body):
        """Spark, at local (1, 2, 0), under Holder (a script object too) at (5, 6, 0)"""
        root = self._project(self._script(body))
        s = os.path.join(root, "Assets", "Scripts")
        with open(os.path.join(s, "Holder.cs"), "w") as f:
            f.write("using UnityEngine;\npublic class Holder : MonoBehaviour {\n public float n;\n void Update() {\n n += 1f;\n }\n}\n")
        with open(os.path.join(s, "Holder.cs.meta"), "w") as f:
            f.write("guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n")
        sc = os.path.join(root, "Assets", "Scenes", "S.unity")
        with open(sc) as f:
            text = f.read().replace("m_LocalPosition: {x: 0, y: 0, z: 0}", "m_LocalPosition: {x: 1, y: 2, z: 0}\n  m_Father: {fileID: 11}", 1)
        with open(sc, "w") as f:
            f.write(text + self.HOLDER)
        return root

    HIER = """
    public float a;
    void Update() {
        a += 1f;
        if (transform.parent == null) a += 100f;
        transform.localPosition = new Vector3(transform.localPosition.x + 1f, 2f, 0f);
        transform.position = new Vector3(transform.position.x + 1f, 7f, 0f);
        if (a > 2.5f) transform.SetParent(null);
    }"""

    def test_managed_transform_hierarchy_matches_the_lowered_engine(self):
        """position is the world one under a parent, localPosition the local one, parent is null-testable and SetParent(null) detaches:
        the same script, lowered and managed, over four frames (the object is detached on the third)."""
        root = self._hierarchy_project(self.HIER)
        probe = ["Spark_get_pos_x(0)", "Spark_get_pos_y(0)", "Spark_get_a(0)"]
        plain, _ = self._pack(root)
        want = self._play(plain, 4, probe)
        self.assertEqual(len(set(want)), 4)               # (it moved every frame)
        self.assertEqual(want[0].split(",")[2], "1.0000")   # (it has a parent: no +100)
        managed, err = self._pack(root, managed="Spark")
        self.assertNotIn("stays lowered", err)
        self.assertIn("hybrid: 1 managed method(s)", err)
        self.assertEqual(self._play(managed, 4, probe), want)

    def test_a_parent_the_engine_has_no_functions_for_leaves_the_stub(self):
        """a stub (a lambda) that names transform.parent, in a pack whose engine never emitted the parent functions: declined, not broken"""
        body = """
    public float a;
    void Update() {
        a += 1f;
        Probe();
        transform.position = new Vector3(a, 0f, 0f);
    }
    void Probe() {
        Func<float, float> f = x => x + 1f;
        if (transform.parent == null) a = f(a);
    }"""
        out, err = self._pack(self._project(self._script(body)), hybrid=True)
        self.assertIn("warning CS8000", err)
        self.assertIn("hybrid:", err)

    RANDOM = """
    public float a; public float b; public float c;
    void Update() {
        Random.InitState(7 + (int)a);
        a += 1f;
        b = Random.value + Random.Range(1, 100);
        c = Random.Range(0.5f, 2.5f);
    }"""

    def test_managed_random_draws_the_engines_own_sequence(self):
        root = self._project(self._script(self.RANDOM))
        probe = ["Spark_get_a(0)", "Spark_get_b(0)", "Spark_get_c(0)"]
        plain, _ = self._pack(root)
        want = self._play(plain, 3, probe)
        self.assertEqual(len(set(want)), 3)
        managed, err = self._pack(root, managed="*")
        self.assertNotIn("stays lowered", err)
        self.assertEqual(self._play(managed, 3, probe), want)

    def test_random_in_a_stub_pulls_the_engines_generator_in(self):
        """Roll is a stub (a lambda), and nothing lowered uses Random: the packer is asked for the generator all the same, and managed Roll
        draws the same numbers as lowered code that calls Random.Range directly."""
        stub = """
    public float a; public float b;
    void Update() {
        Roll();
        transform.position = new Vector3(a, b, 0f);
    }
    void Roll() {
        Func<int, int> twice = x => x * 2;
        Random.InitState(5);
        a = twice(Random.Range(1, 50)); b = Random.value;
    }"""
        direct = """
    public float a; public float b;
    void Update() {
        Random.InitState(5);
        a = Random.Range(1, 50) * 2; b = Random.value;
        transform.position = new Vector3(a, b, 0f);
    }"""
        probe = ["Spark_get_a(0)", "Spark_get_b(0)"]
        want = self._play(self._pack(self._project(self._script(direct)))[0], 2, probe)
        out, err = self._pack(self._project(self._script(stub)), hybrid=True)
        self.assertNotIn("CS8000", err)
        self.assertEqual(self._play(out, 2, probe), want)

    REFS = """
    public Holder target;
    public float a;
    void Update() {
        if (target == null) { a = -1f; return; }
        a = target.n * 10f;
        target.Bump(2);
        target.n = target.n + 0.5f;
        transform.position = new Vector3(target.transform.position.x, a, 0f);
    }"""

    def _refs_project(self, body, values="  hp: 5\n  speed: 3\n  target: {fileID: 12}\n"):
        """Spark holds a reference (m_target) to Holder (a script object at (5, 6, 0))"""
        root = self._project(self._script(body), values=values)
        s = os.path.join(root, "Assets", "Scripts")
        with open(os.path.join(s, "Holder.cs"), "w") as f:
            f.write("using UnityEngine;\npublic class Holder : MonoBehaviour {\n public float n = 4f;\n public int bump;\n"
                    " public void Bump(int by) { bump += by; }\n void Update() {\n n += 1f;\n }\n}\n")
        with open(os.path.join(s, "Holder.cs.meta"), "w") as f:
            f.write("guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n")
        with open(os.path.join(root, "Assets", "Scenes", "S.unity"), "a") as f:
            f.write(self.HOLDER)
        return root

    def test_managed_class_reaches_another_script_object_through_a_field(self):
        """a handle field: its fields, a lowered method of it, its transform, and the null test, as in the lowered pack"""
        root = self._refs_project(self.REFS)
        probe = ["Spark_get_a(0)", "Holder_get_n(0)", "Holder_get_bump(0)", "Spark_get_pos_x(0)"]
        plain, _ = self._pack(root)
        want = self._play(plain, 3, probe)
        self.assertEqual(len(set(want)), 3)
        managed, err = self._pack(root, managed="Spark")
        self.assertNotIn("stays lowered", err)
        self.assertIn("hybrid: 1 managed method(s)", err)
        self.assertEqual(self._play(managed, 3, probe), want)

    COMP = """
    public float a;
    void Update() {
        Holder h = GetComponent<Holder>();
        if (h == null) { a = -1f; return; }
        a = h.n;
        h.Bump(1);
    }"""

    def test_managed_get_component_finds_a_script_on_the_same_object(self):
        """Holder sits on Spark's object (a second MonoBehaviour); GetComponent<Holder>() is the one the lowered engine finds"""
        root = self._project(self._script(self.COMP))
        s = os.path.join(root, "Assets", "Scripts")
        with open(os.path.join(s, "Holder.cs"), "w") as f:
            f.write("using UnityEngine;\npublic class Holder : MonoBehaviour {\n public float n = 4f;\n public int bump;\n"
                    " public void Bump(int by) { bump += by; }\n void Update() {\n n += 1f;\n }\n}\n")
        with open(os.path.join(s, "Holder.cs.meta"), "w") as f:
            f.write("guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n")
        sc = os.path.join(root, "Assets", "Scenes", "S.unity")
        with open(sc) as f:
            text = f.read().replace("  - component: {fileID: 3}\n", "  - component: {fileID: 3}\n  - component: {fileID: 4}\n", 1)
        with open(sc, "w") as f:
            f.write(text + "--- !u!114 &4\nMonoBehaviour:\n  m_GameObject: {fileID: 1}\n"
                    "  m_Script: {fileID: 11500000, guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}\n")
        probe = ["Spark_get_a(0)", "Holder_get_n(0)", "Holder_get_bump(0)"]
        plain, _ = self._pack(root)
        want = self._play(plain, 3, probe)
        self.assertEqual(len(set(want)), 3)
        self.assertNotEqual(want[0].split(",")[0], "-1.0000")
        managed, err = self._pack(root, managed="Spark")
        self.assertNotIn("stays lowered", err)
        self.assertIn("hybrid: 1 managed method(s)", err)
        self.assertEqual(self._play(managed, 3, probe), want)

    FIND = """
    public float a;
    void Update() {
        GameObject h = GameObject.Find("Holder");
        if (h == null) { a = -1f; return; }
        Holder c = h.GetComponent<Holder>();
        a = c.n;
        c.Bump(1);
        if (gameObject.activeSelf) a += 100f;
        h.SetActive(c.bump < 2);
    }"""

    def test_managed_find_game_object_and_active(self):
        root = self._refs_project(self.FIND, "  hp: 5\n  speed: 3\n")
        probe = ["Spark_get_a(0)", "Holder_get_bump(0)", "GameObject_activeSelf(_engine_go_of_Holder(0))"]
        plain, _ = self._pack(root)
        want = self._play(plain, 3, probe)
        self.assertNotIn("-1.0000", want[0])
        managed, err = self._pack(root, managed="Spark")
        self.assertNotIn("stays lowered", err)
        self.assertIn("hybrid: 1 managed method(s)", err)
        self.assertEqual(self._play(managed, 3, probe), want)

    def test_managed_screen_size_is_the_engines(self):
        body = """
    public float a;
    void Update() {
        a = Screen.width + Screen.height * 0.001f;
        transform.position = new Vector3(a, 0f, 0f);
    }"""
        out, err = self._pack(self._project(self._script(body)), managed="*")
        self.assertNotIn("stays lowered", err)
        self.assertEqual(self._play(out, 1, ["Spark_get_a(0)"], "Screen_width = 640; Screen_height = 480;"), ["640.4800"])

    PARTIAL = """
    public float a; public float b;
    const float K = 3f;
    void Update() {
        a += K;
        Helper();
        transform.position = new Vector3(a, b, 0f);
    }
    void Helper() {
        if (!Application.isPlaying) a += 100f;
        b = a * 2f;
    }"""

    def test_a_class_that_does_not_build_whole_still_moves_the_methods_that_do(self):
        """Helper uses Application (not in the shim), Update does not: Update runs managed (calling Helper through the bridge, with the
        constant K as its own), Helper stays lowered, and the numbers are the lowered pack's"""
        root = self._project(self._script(self.PARTIAL))
        probe = ["Spark_get_a(0)", "Spark_get_b(0)"]
        plain, _ = self._pack(root)
        want = self._play(plain, 3, probe)
        self.assertEqual(want[2], "9.0000,18.0000")
        managed, err = self._pack(root, managed="*")
        self.assertIn("managed: Spark.Helper stays lowered", err)
        self.assertNotIn("Spark.Update stays lowered", err)
        self.assertIn("hybrid: 1 managed method(s)", err)
        self.assertEqual(self._play(managed, 3, probe), want)

    def test_a_methods_own_source_is_found_before_the_packers_rewrites(self):
        import tools.unity.unity_pack_common as common
        path = os.path.join(tempfile.gettempdir(), "Orig.cs")
        text = ("using UnityEngine;\npublic class Orig : MonoBehaviour {\n  int Add(int a, int b) { return a + b; }\n"
                "  int Add(int a) { return a; }\n  void Go() { if (true) { transform.Rotate(Vector3.up); } }\n}\n"
                "public class Other { void Go() { } }\n")
        common.SOURCE_ORIGINAL[os.path.abspath(path)] = text
        try:
            self.assertEqual(unity_pack_hybrid.original_body(path, "Orig", "Add", 2).strip(), "return a + b;")
            self.assertEqual(unity_pack_hybrid.original_body(path, "Orig", "Add", 1).strip(), "return a;")
            self.assertIn("transform.Rotate(Vector3.up);", unity_pack_hybrid.original_body(path, "Orig", "Go", 0))
            self.assertIsNone(unity_pack_hybrid.original_body(path, "Orig", "Add", 3))          # (no such overload)
            self.assertIsNone(unity_pack_hybrid.original_body(path, "Orig", "Missing", 0))
            self.assertEqual(unity_pack_hybrid.original_body(path, "Other", "Go", 0).strip(), "")  # (the class asked for, not the first)
        finally:
            common.SOURCE_ORIGINAL.pop(os.path.abspath(path), None)

    def test_a_stub_runs_as_managed_code_over_the_packed_state(self):
        out, err = self._pack(self._project(self._script(self.TALLY)), hybrid=True)
        self.assertNotIn("CS8000", err)
        self.assertIn("hybrid: 1 managed method(s)", err)
        # sq(3) + sq(4) = 25 + 2 * hp: native Update's hp, read by managed code; its total, read by native code
        self.assertEqual(self._play(out), ["hp=4 total=33", "hp=3 total=31", "hp=2 total=29", "hp=1 total=27"])

    # --managed: every method of this class lowers to C; a selected class runs as managed code instead, and does the same
    LOWERS = """
    public int hp = 3;
    public int total;
    void Update() {
        hp -= 1;
        Bump();
        Debug.Log("hp=" + hp + " total=" + total);
    }
    public void Bump() { total += hp * 2; }"""
    LOWERS_OUT = ["hp=4 total=8", "hp=3 total=14", "hp=2 total=18", "hp=1 total=20"]

    def test_managed_runs_a_class_that_lowers_fine_as_managed_code(self):
        root = self._project(self._script(self.LOWERS))
        plain, _err = self._pack(root)
        self.assertEqual(self._play(plain), self.LOWERS_OUT)          # the lowered C: what the managed class has to do as well
        self.assertFalse(os.path.exists(os.path.join(plain, "hybrid.managed.dll")))
        for sel in ("*", {"Spark"}):
            out, err = self._pack(root, managed=sel)
            self.assertIn("hybrid: 2 managed method(s) in 1 class(es): Spark.Bump, Spark.Update", err)
            self.assertTrue(os.path.exists(os.path.join(out, "hybrid.managed.dll")))
            with open(os.path.join(out, "engine.c")) as f:
                engine = f.read()
            self.assertIn("ccs_b_Spark_Update", engine)               # the lowered body is gone: the function calls its managed twin
            self.assertNotIn("hp -= 1", engine)
            self.assertEqual(self._play(out), self.LOWERS_OUT)

    # Bouncer of examples/unity_pack/SystemsScene: Time.time, Mathf.Sin and a Vector2 assigned to the (Vector3) position
    BOUNCE = """
    public float baseY;
    public float amp;
    public float speed;
    void Update() {
        transform.position = new Vector2(transform.position.x, baseY + Mathf.Sin(Time.time * speed) * amp);
    }"""

    def test_managed_class_reads_time_and_assigns_a_vector2_to_the_position(self):
        root = self._project(self._script(self.BOUNCE), values="  baseY: 2\n  amp: 1.5\n  speed: 3\n")
        probe = "Spark_get_pos_y(0)"
        plain, _err = self._pack(root)
        lowered = self._play(plain, 8, probe)
        out, err = self._pack(root, managed="*")
        self.assertIn("hybrid: 1 managed method(s) in 1 class(es): Spark.Update", err)
        managed = self._play(out, 8, probe)
        self.assertEqual(len(managed), 8)
        self.assertGreater(len(set(managed)), 4)                      # it moves (a constant would pass the comparison below too)
        for a, b in zip(lowered, managed):
            self.assertAlmostEqual(float(a), float(b), places=3)

    # static methods and overloads cross the boundary too (an overload is told apart by its C symbol)
    STATICS = """
    public int hp = 3;
    public int total;
    static int Sq(int x) { return x * x; }
    static int Sq(int x, int y) { return x * x + y; }
    void Add(int a) { total += a; }
    void Add(int a, int b) { total += a + b; }
    void Update() {
        hp -= 1;
        Add(Sq(hp));
        Add(Sq(hp, 1), 0);
        Debug.Log("hp=" + hp + " total=" + total);
    }"""
    STATICS_OUT = ["hp=4 total=33", "hp=3 total=52", "hp=2 total=61", "hp=1 total=64"]

    def test_managed_takes_static_methods_and_overloads(self):
        # (the packer does not lower a call to the class's own static `Sq(`: Update is a stub in a plain pack, so the expected output is by hand)
        root = self._project(self._script(self.STATICS))
        plain, err = self._pack(root)
        self.assertIn("`Spark.Update` is not lowered yet", err)
        # --hybrid: Update runs managed and calls the lowered statics and overloads across the boundary
        out, err = self._pack(root, hybrid=True)
        self.assertIn("hybrid: 1 managed method(s) in 1 class(es): Spark.Update", err)
        self.assertEqual(self._play(out), self.STATICS_OUT)
        # --managed: all five methods run managed (Update, Add x2, Sq x2)
        out, err = self._pack(root, managed="*")
        self.assertNotIn("stays lowered", err)
        self.assertIn("hybrid: 5 managed method(s) in 1 class(es): Spark.Add x2, Spark.Sq x2, Spark.Update", err)
        self.assertEqual(self._play(out), self.STATICS_OUT)

    def test_managed_leaves_the_classes_it_does_not_name_lowered(self):
        out, err = self._pack(self._project(self._script(self.LOWERS)), managed={"Other"})
        self.assertIn("no script class Other", err)
        for name in ("hybrid_glue.c", "hybrid.managed.dll", "hybrid.ffi.json"):
            self.assertFalse(os.path.exists(os.path.join(out, name)), name)
        self.assertEqual(self._play(out), self.LOWERS_OUT)

    def test_managed_class_the_shim_cannot_build_stays_lowered(self):
        # Application.isPlaying lowers to C but the managed UnityEngine has no Application: the class (one method) does not build managed, and is
        # what a plain pack gives, with the reason printed
        one = """
    public int hp = 3;
    public int total;
    void Update() {
        hp -= 1;
        if (!Application.isPlaying) total += 1;
        total += hp * 2;
        Debug.Log("hp=" + hp + " total=" + total);
    }"""
        root = self._project(self._script(one))
        plain, _err = self._pack(root)
        want = self._play(plain)
        self.assertEqual(want[0], "hp=4 total=8")
        out, err = self._pack(root, managed="*")
        self.assertIn("managed: Spark stays lowered: the managed code does not compile", err)
        self.assertIn("Application", err)
        self.assertFalse(os.path.exists(os.path.join(out, "hybrid_glue.c")))
        self.assertEqual(self._play(out), want)

    def test_managed_uses_roslyn_when_ccs_is_built_and_mcs_when_forced(self):
        if unity_pack_hybrid.ccs_dll() is None or shutil.which("mcs") is None:
            self.skipTest("needs both CCSharp (built) and mcs")
        saved = os.environ.get("UNITY_PACK_MANAGED_COMPILER")
        try:
            os.environ.pop("UNITY_PACK_MANAGED_COMPILER", None)
            self.assertEqual(unity_pack_hybrid.compiler_name(), "roslyn")
            os.environ["UNITY_PACK_MANAGED_COMPILER"] = "mcs"
            self.assertEqual(unity_pack_hybrid.compiler_name(), "mcs")
            out, err = self._pack(self._project(self._script(self.LOWERS)), managed="*")
            self.assertEqual(self._play(out), self.LOWERS_OUT)
        finally:
            if saved is None:
                os.environ.pop("UNITY_PACK_MANAGED_COMPILER", None)
            else:
                os.environ["UNITY_PACK_MANAGED_COMPILER"] = saved

    def test_without_hybrid_it_is_still_a_stub(self):
        out, err = self._pack(self._project(self._script(self.TALLY)))
        self.assertIn("warning CS8000", err)
        self.assertIn("`Spark.Tally` is not lowered yet", err)
        for name in ("hybrid_glue.c", "hybrid.managed.dll", "hybrid.ffi.json", "hybrid_generated.cs"):
            self.assertFalse(os.path.exists(os.path.join(out, name)), name)
        self.assertEqual(self._play(out), ["hp=4 total=0", "hp=3 total=0", "hp=2 total=0", "hp=1 total=0"])

    def test_a_pack_with_nothing_to_stub_has_no_hybrid_pieces(self):
        out, err = self._pack(self._project(self._script(
            "    public int hp;\n    void Update() { hp += 1; Debug.Log(\"hp=\" + hp); }")), hybrid=True)
        self.assertNotIn("hybrid", err)
        for name in ("hybrid_glue.c", "hybrid.managed.dll", "hybrid.ffi.json"):
            self.assertFalse(os.path.exists(os.path.join(out, name)), name)

    def test_managed_code_that_does_not_compile_keeps_its_stub(self):
        # `Foo` is not in the managed UnityEngine: the class does not build, so it is exactly what a plain pack gives, with the reason added
        root = self._project(self._script(
            "    public int hp = 3;\n    public int total;\n"
            "    void Update() { hp -= 1; Tally(); Debug.Log(\"hp=\" + hp + \" total=\" + total); }\n"
            "    public void Tally() { total = Foo.Bar(hp); }"))
        out, err = self._pack(root, hybrid=True)
        self.assertIn("warning CS8000", err)
        self.assertIn("hybrid: the managed code does not compile", err)
        self.assertFalse(os.path.exists(os.path.join(out, "hybrid_glue.c")))
        self.assertEqual(self._play(out, 2), ["hp=4 total=0", "hp=3 total=0"])

    def test_strict_is_met_when_managed_code_takes_the_method_and_refused_when_not(self):
        out, err = self._pack(self._project(self._script(self.TALLY)), hybrid=True, strict=True)     # no error: nothing is dropped
        self.assertNotIn("CS8000", err)
        root = self._project(self._script("    public int hp;\n    public void Update() { hp = Foo.Bar(hp); }"))
        with self.assertRaises(unity_pack.PackError) as cm:
            self._pack(root, hybrid=True, strict=True)
        self.assertIn("error CS8000", cm.exception.message)
        self.assertIn("hybrid:", cm.exception.message)

    def test_a_field_the_engine_stores_out_of_reach_is_not_copied(self):
        # `label` is a string, which the packed engine keeps in a table managed code has no accessor for: a managed copy would silently
        # disagree with it, so the class stays a stub
        root = self._project(self._script(
            "    public string label = \"abc\";\n    public int total;\n"
            "    void Update() { Tally(); Debug.Log(\"total=\" + total); }\n"
            "    public void Tally() { Func<int, int> f = v => v * 10; total = f(label.Length); }"))
        out, err = self._pack(root, hybrid=True)
        self.assertIn("warning CS8000", err)
        self.assertIn("`string label`", err)
        self.assertFalse(os.path.exists(os.path.join(out, "hybrid_glue.c")))

    def test_managed_code_calls_a_lowered_method_and_uses_bool_and_float_fields(self):
        root = self._project(self._script("""
    public int hp = 3;
    public int total;
    public float speed = 2f;
    public bool alive = true;
    void Update() { Tally(); Debug.Log("hp=" + hp + " total=" + total + " alive=" + (alive ? 1 : 0)); }
    public void Bump(int by) { hp += by; }
    public void Tally() {
        Func<int, int> twice = n => n * 2;
        Bump(twice(2));
        total = hp * 10;
        if (hp > 8) alive = false;
    }"""), values="  hp: 5\n  speed: 3\n  alive: 1\n")
        out, err = self._pack(root, hybrid=True)
        self.assertNotIn("CS8000", err)
        # each tick: Bump(4) is a lowered method called from managed code; alive goes false once hp passes 8
        self.assertEqual(self._play(out, 3), ["hp=9 total=90 alive=0", "hp=13 total=130 alive=0", "hp=17 total=170 alive=0"])

    def test_managed_code_may_index_an_array_with_a_uint_or_a_long(self):
        # an unsigned or wide index is a conv.u / conv.i before the element opcode: DotNetAnywhere used to leave its evaluation stack misaligned
        # and crash the player (fixed in its JIT, JIT_NARROW_INDEX_BELOW), where this method must simply run
        root = self._project(self._script("""
    public int total;
    void Update() { Tally(); Debug.Log("total=" + total); }
    public void Tally() {
        int[] xs = new int[4];
        Func<int, int> sq = n => n * n;
        for (uint k = 0; k < 4; k++) xs[k] = sq((int)k + 1);
        uint j = 2; long m = 3;
        total = xs[j] * 100 + xs[m];
    }"""), values="  total: 0\n")
        out, err = self._pack(root, hybrid=True)
        self.assertNotIn("CS8000", err)
        self.assertEqual(self._play(out, 2), ["total=916", "total=916"])

    @unittest.skipUnless(shutil.which("make"), "needs make")
    def test_make_builds_a_hybrid_player_that_runs_its_managed_assembly(self):
        out, err = self._pack(self._project(self._script(self.TALLY)), hybrid=True)
        mk = subprocess.run(["make", "-C", out, "game", "CC=" + _CC, "DNA_BUILD=" + unity_pack_hybrid.build_dir()],
                            capture_output=True, text=True)
        self.assertEqual(mk.returncode, 0, mk.stdout[-800:] + mk.stderr[-1200:])
        game = os.path.join(out, "game")
        for name in ("corlib.dll", "hybrid.managed.dll"):
            self.assertTrue(os.path.exists(os.path.join(out, name)), name)
        other = tempfile.mkdtemp(prefix="upack-hy-cwd-")
        self.addCleanup(shutil.rmtree, other, True)
        r = subprocess.run([game], cwd=other, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertIn("ticks=60", r.stdout)
        # Update calls the managed Tally on every tick, so a player that cannot load its assembly must stop, saying why
        env = dict(os.environ, UNITY_PACK_MANAGED_DLL=os.path.join(other, "missing.dll"))
        r = subprocess.run([game], cwd=other, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 70)
        self.assertIn("cannot load the managed assembly", r.stderr)
        # and `make clean` takes what it built away
        subprocess.run(["make", "-C", out, "clean"], capture_output=True, text=True)
        self.assertFalse(os.path.exists(game))
        self.assertFalse(os.path.exists(os.path.join(out, "hybrid_glue.o")))

    def test_without_the_toolchain_the_stub_is_reported_with_the_reason(self):
        saved = os.environ.get("DNA_HOME")
        os.environ["DNA_HOME"] = os.path.join(tempfile.gettempdir(), "no-such-dotnetanywhere")
        try:
            out, err = self._pack(self._project(self._script(self.TALLY)), hybrid=True)
        finally:
            if saved is None:
                del os.environ["DNA_HOME"]
            else:
                os.environ["DNA_HOME"] = saved
        self.assertIn("warning CS8000", err)
        self.assertIn("hybrid: DotNetAnywhere not found", err)
        self.assertFalse(os.path.exists(os.path.join(out, "hybrid_glue.c")))


def _euler_q(x, y, z):
    """Unity's Quaternion.Euler(x, y, z) (degrees), worked independently of the packer: z applied first, then x, then y: the product qy*qx*qz, as (x, y, z, w)."""
    import math
    def ax(a, ix):
        h = math.radians(a) / 2
        q = [0.0, 0.0, 0.0, math.cos(h)]
        q[ix] = math.sin(h)
        return q
    def mul(a, b):
        return [a[3] * b[0] + a[0] * b[3] + a[1] * b[2] - a[2] * b[1],
                a[3] * b[1] - a[0] * b[2] + a[1] * b[3] + a[2] * b[0],
                a[3] * b[2] + a[0] * b[1] - a[1] * b[0] + a[2] * b[3],
                a[3] * b[3] - a[0] * b[0] - a[1] * b[1] - a[2] * b[2]]
    return mul(mul(ax(y, 1), ax(x, 0)), ax(z, 2))


@needs_cc
@needs_hybrid
class TestRotation(_PackHarness):
    """transform.rotation / eulerAngles in a managed class run over the engine's own rotation arrays, and agree with the lowered engine
    and with Unity's ZXY Euler order."""

    PROBE = ["_Spark_rot_x[0]", "_Spark_rot_y[0]", "_Spark_rot_z[0]", "_Spark_rot_w[0]"]

    def _rows(self, out, frames):
        return [[float(v) for v in r.split(",")] for r in self._play(out, frames, self.PROBE)]

    def _same_rotation(self, a, b, tol=2e-3):
        # (q and -q are one rotation; the probe prints %.4f)
        dot = abs(sum(p * q for p, q in zip(a, b)))
        self.assertGreater(dot, 1 - tol, "%s vs %s" % (a, b))

    EULER_PLUS = """
    public float angle;
    void Update() {
        angle += 15f;
        transform.eulerAngles = new Vector3(10f, 20f, 30f);
        transform.eulerAngles += new Vector3(0, 0, angle);
    }"""

    def test_euler_angles_plus_equals_follows_unitys_order(self):
        out, _ = self._pack(self._project(self._script(self.EULER_PLUS)))
        for f, row in enumerate(self._rows(out, 3), 1):
            self._same_rotation(row, _euler_q(10, 20, 30 + 15 * f))

    def test_managed_euler_angles_plus_equals_is_unitys(self):
        out, err = self._pack(self._project(self._script(self.EULER_PLUS)), managed="*")
        self.assertNotIn("stays lowered", err)
        for f, row in enumerate(self._rows(out, 3), 1):
            self._same_rotation(row, _euler_q(10, 20, 30 + 15 * f))

    def test_managed_rotation_matches_unity_and_the_lowered_engine(self):
        body = ("public float angle;\n    void Update() { angle += 15f; "
                "transform.rotation = Quaternion.Euler(10f, 20f, angle); }")
        root = self._project(self._script(body))
        plain, _ = self._pack(root)
        managed, err = self._pack(root, managed="*")
        self.assertNotIn("stays lowered", err)
        low, mg = self._rows(plain, 4), self._rows(managed, 4)
        for f, (a, b) in enumerate(zip(low, mg), 1):
            self._same_rotation(a, b)
            self._same_rotation(b, _euler_q(10, 20, 15 * f))

    def test_a_class_with_no_rotation_storage_is_declined_not_broken(self):
        engine = ("static float Spark_get_pos_x(unsigned i) { return 0; }\nstatic void Spark_set_pos_x(unsigned i, float v) {}\n"
                  "static float Spark_get_pos_y(unsigned i) { return 0; }\nstatic void Spark_set_pos_y(unsigned i, float v) {}\n")
        cand = {"sym": "Spark_Update", "name": "Update", "body": "transform.rotation = Quaternion.identity;"}
        g = unity_pack_hybrid.ClassGen({"classes": []}, engine, {"name": "Spark", "fields": []}, [cand], [])
        self.assertIn("rotation", g.problem or "")
        self.assertEqual(g.src, "")


@needs_cc
class TestRuntimeCreation(_PackHarness):
    """`new GameObject(..)` and `AddComponent<Script>()`: objects a script makes at run time, from the spare slots the packer budgets."""

    KID = "using UnityEngine;\npublic class Kid : MonoBehaviour {\n    public int v;\n    void Update() { v++; }\n}\n"

    def _kid_project(self, spark_members, kid=None):
        root = self._project(self._script(spark_members), "  hp: 5\n")
        d = os.path.join(root, "Assets", "Scripts")
        with open(os.path.join(d, "Kid.cs"), "w") as f:
            f.write(kid or self.KID)
        with open(os.path.join(d, "Kid.cs.meta"), "w") as f:
            f.write("guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n")
        sc = os.path.join(root, "Assets", "Scenes", "S.unity")
        with open(sc) as f:
            t = f.read()
        with open(sc, "w") as f:
            f.write(t + "--- !u!1 &10\nGameObject:\n  m_Name: K\n  m_Component:\n  - component: {fileID: 11}\n  - component: {fileID: 12}\n"
                    "--- !u!4 &11\nTransform:\n  m_GameObject: {fileID: 10}\n  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                    "--- !u!114 &12\nMonoBehaviour:\n  m_GameObject: {fileID: 10}\n"
                    "  m_Script: {fileID: 11500000, guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}\n  v: 1\n")
        return root

    def test_a_new_gameobject_takes_a_component_and_it_runs(self):
        root = self._kid_project("""
    public int hp;
    void Update() {
        GameObject g = new GameObject("kid");
        Kid k = g.AddComponent<Kid>();
        k.v = 3;
        hp += k.v;
    }""")
        out, err = self._pack(root)
        self.assertNotIn("CS8000", err)
        # the first frame makes the one spare Kid (3 more on hp) and it ticks from then on; the pool is then full: no more are made
        got = self._play(out, 3, ["Spark_get_hp(0)", "_Kid_inst_count", "Kid_get_v(1)"])
        self.assertEqual(got, ["8.0000,2.0000,3.0000", "8.0000,2.0000,4.0000", "8.0000,2.0000,5.0000"])

    def test_a_new_objects_position_through_its_transform(self):
        """a new object with a component moves that component's row; a bare one keeps a position of its own; both read back"""
        root = self._kid_project("""
    public float px;
    public float qy;
    void Update() {
        GameObject g = new GameObject("with");
        Kid k = g.AddComponent<Kid>();
        g.transform.position = new Vector3(4f, 5f, 0f);
        Transform t = new GameObject("bare").transform;
        t.position = new Vector3(7f, 9f, 0f);
        px = k.transform.position.x + g.transform.position.x;
        qy = t.position.y;
    }""")
        out, err = self._pack(root)
        self.assertNotIn("CS8000", err)
        self.assertEqual(self._play(out, 1, ["Spark_get_px(0)", "Spark_get_qy(0)", "Kid_get_pos_x(1)", "Kid_get_pos_y(1)"]), ["8.0000,9.0000,4.0000,5.0000"])

    def test_the_chained_form_and_the_name(self):
        root = self._kid_project("""
    public int hp;
    public string n;
    void Update() {
        Kid k = new GameObject("chained").AddComponent<Kid>();
        hp += k.v + 1;
    }""")
        out, err = self._pack(root)
        self.assertNotIn("CS8000", err)
        self.assertEqual(self._play(out, 1, ["Spark_get_hp(0)"]), ["6.0000"])


class TestLayerMaskAwake(unittest.TestCase):
    """Slime Jump's Player.Awake: `enabled` of an authored-enabled script,
    `Physics2D.GetLayerCollisionMask(gameObject.layer)` from the authored
    matrix, and the LayerMaskExtensions `Remove` (an unknown name is bit 31,
    `1 << -1`). Layer 4's word is 0x800000f0; less Water (4) and bit 31."""

    @needs_cc
    def test_mask_remove(self):
        a = ("using UnityEngine;\nusing Extensions;\n"
             "public class A : MonoBehaviour {\n    LayerMask m;\n"
             "    void Start() {\n        if (!enabled) return;\n"
             "        m = Physics2D.GetLayerCollisionMask(gameObject.layer);\n"
             "        m = m.Remove(\"Water\", \"Nope\");\n"
             "        Debug.Log(\"m \" + (int)m + \" \" + gameObject.layer);\n"
             "    }\n}\n")
        # Slime Jump's LayerMaskExtensions, the two methods used
        ext = ("using UnityEngine;\nnamespace Extensions\n{\n"
               "\tpublic static class LayerMaskExtensions\n\t{\n"
               "\t\tpublic static LayerMask FromLayerNames (params string[] layerNames)\n"
               "\t\t{\n\t\t\tLayerMask ret = (LayerMask) 0;\n"
               "\t\t\tforeach (string name in layerNames)\n"
               "\t\t\t\tret |= (1 << LayerMask.NameToLayer(name));\n"
               "\t\t\treturn ret;\n\t\t}\n"
               "\t\tpublic static LayerMask Remove (this LayerMask original, params string[] layerNames)\n"
               "\t\t{\n\t\t\tLayerMask invertedOriginal = ~original;\n"
               "\t\t\treturn ~(invertedOriginal | FromLayerNames(layerNames));\n"
               "\t\t}\n\t}\n}\n")
        root = project(self, {"A": a, "LayerMaskExtensions": ext}, [("A",)])
        scene = os.path.join(root, "Assets", "Scenes", "S.unity")
        with open(scene) as f:
            text = f.read().replace("GameObject:\n", "GameObject:\n  m_Layer: 4\n")
        with open(scene, "w") as f:
            f.write(text)
        os.makedirs(os.path.join(root, "ProjectSettings"))
        with open(os.path.join(root, "ProjectSettings",
                               "Physics2DSettings.asset"), "w") as f:
            f.write("  m_LayerCollisionMatrix: %s\n"
                    % ("ffffffff" * 4 + "f0000080" + "ffffffff" * 27))
        out = run_frames(self, pack(self, root), 1)
        self.assertIn("m 224 4", out)

    def test_enabled_writes(self):
        """Slime Jump's Death / Respawn: `Other.instance.enabled`, a handle
        field's and its own `enabled` written; OnEnable / OnDisable run at
        the write, a disabled script does not update, and an authored
        `m_Enabled: 0` script wakes but is not enabled."""
        a = ("using UnityEngine;\npublic class A : MonoBehaviour {\n"
             "    public static A instance;\n"
             "    void Awake() { instance = this; }\n"
             "    void OnEnable() { Debug.Log(\"a on\"); }\n"
             "    void OnDisable() { Debug.Log(\"a off\"); }\n"
             "    void Update() { Debug.Log(\"a upd\"); }\n}\n")
        b = ("using UnityEngine;\npublic class B : MonoBehaviour {\n"
             "    public A a;\n"
             "    void Start() {\n"
             "        A.instance.enabled = false;\n"
             "        if (!a.enabled) Debug.Log(\"b sees off\");\n"
             "        a.enabled = true;\n    }\n"
             "    void Update() { Debug.Log(\"b upd\"); enabled = false; }\n}\n")
        c = ("using UnityEngine;\npublic class C : MonoBehaviour {\n"
             "    void Awake() { Debug.Log(\"c awake\"); }\n"
             "    void OnEnable() { Debug.Log(\"c on\"); }\n"
             "    void Update() { Debug.Log(\"c upd\"); }\n}\n")
        root = project(self, {"A": a, "B": b, "C": c}, [
            ("A",), ("B", None, "  a: {fileID: 102}\n"),
            ("C", None, "  m_Enabled: 0\n")])
        out = run_frames(self, pack(self, root), 2)
        logs = [l for l in out if l.startswith(("a ", "b ", "c "))]
        self.assertEqual(logs, ["a on", "c awake", "a off", "b sees off",
                                "a on", "a upd", "b upd", "a upd"])


def _run_rc(test, out, frames=1):
    """Like run_frames, but a stop is the result: (exit code, stdout,
    stderr)."""
    src = os.path.join(out, "h.c")
    with open(src, "w") as f:
        f.write('#include "engine_draw.h"\nextern float Time_deltaTime;\n'
                "int main(int c, char **v) { int f; engine_apply_argv(c, v);\n"
                "  Time_deltaTime = 1.f / 60.f;\n"
                "  for (f = 0; f < %d; f++) engine_tick(); return 0; }\n"
                % frames)
    exe = os.path.join(out, "h")
    r = subprocess.run([_CC, "-O1", "-w", "-I", out, "-o", exe, src,
                        os.path.join(out, "engine.c"),
                        os.path.join(out, "data.c"), "-lm"],
                       capture_output=True, text=True)
    test.assertEqual(r.returncode, 0, r.stderr[-2000:])
    run = subprocess.run([exe, "-logFile", "-"], capture_output=True,
                         text=True, timeout=60)
    return run.returncode, run.stdout, run.stderr


class TestPlayerAwakeItems(unittest.TestCase):
    """Slime Jump's Player.Awake item loop, over a `Gear` class: a field
    array stored from GetComponentsInChildren<Gear>,
    `item.gameObject.activeSelf` and `item.OnGain(this)` on a typed local.
    The result holds bare Gear row indices, so finding a subclass stops
    instead of misreading it. Named `Item` it is Slime Jump's item system,
    out of scope: the store goes and the loop runs no pass."""

    A = ("using UnityEngine;\npublic class A : MonoBehaviour {\n"
         "    public Transform itemsParent;\n    public int got;\n"
         "    Item[] items;\n    void Awake() {\n"
         "        items = itemsParent.GetComponentsInChildren<Item>();\n"
         "        for (int i = 0; i < items.Length; i ++) {\n"
         "            Item item = items[i];\n"
         "            if (item.gameObject.activeSelf)\n"
         "                item.OnGain (this);\n        }\n"
         "        Debug.Log(\"got \" + got);\n    }\n}\n")
    ITEM = ("using UnityEngine;\npublic class Item : MonoBehaviour {\n"
            "    public int v;\n"
            "    public virtual void OnGain (A a) { a.got += v; }\n}\n")

    def _root(self, scripts, child):
        root = project(self, scripts, [("A", None, "  itemsParent: {fileID: 101}\n"),
                                       (child, None, "  v: 5\n")])
        sc = os.path.join(root, "Assets", "Scenes", "S.unity")
        with open(sc) as f:
            text = f.read().replace(
                "Transform:\n  m_GameObject: {fileID: 110}\n",
                "Transform:\n  m_GameObject: {fileID: 110}\n"
                "  m_Father: {fileID: 101}\n")
        with open(sc, "w") as f:
            f.write(text)
        return root

    def _gear(self, extra=()):
        s = {"A": self.A.replace("Item", "Gear"),
             "Gear": self.ITEM.replace("Item", "Gear")}
        s.update(extra)
        return s

    @needs_cc
    def test_items_loop(self):
        root = self._root(self._gear(), "Gear")
        self.assertIn("got 5", run_frames(self, pack(self, root), 1))

    @needs_cc
    def test_item_system_dropped(self):
        root = self._root({"A": self.A, "Item": self.ITEM}, "Item")
        self.assertIn("got 0", run_frames(self, pack(self, root, False), 1))

    @needs_cc
    def test_subclass_found_stops(self):
        b = ("using UnityEngine;\npublic class Blaster : Gear {\n"
             "    public override void OnGain (A a) { a.got += 100; }\n}\n")
        root = self._root(self._gear({"Blaster": b}), "Blaster")
        rc, out, err = _run_rc(self, pack(self, root, strict=False))
        self.assertEqual(rc, 70, out + err)
        self.assertIn("GetComponentsInChildren<Gear> found a Blaster", err)
        self.assertIn("A.Awake () (at Assets/Scripts/A.cs:7)", err)
        self.assertNotIn("got", out)


class TestChildColliderBody(unittest.TestCase):
    """Slime Jump's player: its collider is on a child GameObject with no
    Rigidbody2D, which Unity puts on the nearest ancestor's body -- left
    static, the player fell through the floor."""

    def test_nearest_ancestor_rb(self):
        import tools.unity.unity_pack_physics as phys
        box = {"kind": "box", "enabled": 1}
        plan = {"rigidbody2d": [{"owner_class": "P", "owner_inst": 0,
                                 "body_type": 0}],
                "classes": {
                    "P": {"instances": [{"xf_id": "10"}]},
                    "Mid": {"instances": [{"xf_id": "20", "father_id": "10"}]},
                    "C": {"instances": [
                        {"xf_id": "30", "father_id": "20", "collider2d": box},
                        {"xf_id": "40", "collider2d": box}]}}}
        cols = phys._build_collider2d_tables(plan)
        self.assertEqual([(c["owner_inst"], c["rb2d"], c["body_type"])
                          for c in cols], [(0, 0, 0), (1, -1, 2)])


class TestSharedTransformRow(unittest.TestCase):
    """Slime Jump's Player GameObject also carries a `Test` script: its
    Graphics child followed the Test row, which nothing moves, so the
    sprite hung at the spawn while the Rigidbody2D fell."""

    def test_child_follows_rigidbody_row(self):
        plan = {"classes": {
            "Test": {"instances": [{"xf_id": "10"}]},
            "Player": {"instances": [{"xf_id": "10", "rigidbody2d": {"body_type": 0}}],
                       "fields": [{"name": "t", "ty": "Transform"}]},
            "Graphics": {"instances": [{"xf_id": "20", "father_id": "10"}]}}}
        plan["classes"]["Player"]["instances"][0]["object_refs"] = {"t": "10"}
        unity_pack._attach_transform_parents(plan)
        g = plan["classes"]["Graphics"]["instances"][0]
        self.assertEqual((g["xf_parent_class"], g["xf_parent_inst"]),
                         ("Player", 0))
        unity_pack._resolve_transform_field_targets(plan)
        self.assertEqual(plan["transform_field_targets"][("Player", "t")],
                         [(1, 0, "Player")])


class TestLiveScaleSpriteHalf(unittest.TestCase):
    """Slime Jump's walls (scale 2, a Transform field's target, so a live
    localScale): the sprite half held the authored scale and the draw
    multiplied by the localScale again, twice Unity's size."""

    def _draws(self, meta="", rot=(0, 1)):
        """A scale-2 wall (8 px at 8 px per unit) with *meta* in its sprite's
        importer, turned by the quaternion (z, w) *rot*: its draws'
        `half hw hh at x y`."""
        import struct
        import zlib
        root = tempfile.mkdtemp(prefix="upack-ls-")
        self.addCleanup(shutil.rmtree, root, True)
        sd = os.path.join(root, "Assets", "Scripts")
        os.makedirs(sd)
        for name, src, guid in (
                ("Wall", "using UnityEngine;\npublic class Wall : MonoBehaviour"
                 " { }\n", "a" * 32),
                ("Holder", "using UnityEngine;\npublic class Holder :"
                 " MonoBehaviour {\n    public Transform t;\n    void Update()"
                 " { if (t == null) Debug.Log(\"none\"); }\n}\n", "b" * 32)):
            with open(os.path.join(sd, name + ".cs"), "w") as f:
                f.write(src)
            with open(os.path.join(sd, name + ".cs.meta"), "w") as f:
                f.write("guid: %s\n" % guid)

        def chunk(tag, body):
            return (struct.pack(">I", len(body)) + tag + body
                    + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff))
        with open(os.path.join(sd, "q.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n"
                    + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 6, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff" * 32) * 8))
                    + chunk(b"IEND", b""))
        with open(os.path.join(sd, "q.png.meta"), "w") as f:
            f.write("guid: %s\nTextureImporter:\n  spritePixelsToUnits: 8\n"
                    % ("c" * 32) + meta)
        os.makedirs(os.path.join(root, "Assets", "Scenes"))
        with open(os.path.join(root, "Assets", "Scenes", "S.unity"), "w") as f:
            f.write(
                "%%YAML 1.1\n"
                "--- !u!1 &1\nGameObject:\n  m_Name: Wall\n"
                "  m_Component:\n  - component: {fileID: 2}\n"
                "  - component: {fileID: 3}\n  - component: {fileID: 4}\n"
                "--- !u!4 &2\nTransform:\n  m_GameObject: {fileID: 1}\n"
                "  m_LocalRotation: {x: 0, y: 0, z: %s, w: %s}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
                "  m_LocalScale: {x: 2, y: 2, z: 1}\n"
                "--- !u!114 &3\nMonoBehaviour:\n  m_GameObject: {fileID: 1}\n"
                "  m_Script: {fileID: 11500000, guid: %s}\n"
                "--- !u!212 &4\nSpriteRenderer:\n  m_GameObject: {fileID: 1}\n"
                "  m_Enabled: 1\n  m_Sprite: {fileID: 21300000, guid: %s,"
                " type: 3}\n  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
                "--- !u!1 &10\nGameObject:\n  m_Name: Holder\n"
                "  m_Component:\n  - component: {fileID: 11}\n"
                "  - component: {fileID: 12}\n"
                "--- !u!4 &11\nTransform:\n  m_GameObject: {fileID: 10}\n"
                "  m_LocalPosition: {x: 5, y: 0, z: 0}\n"
                "--- !u!114 &12\nMonoBehaviour:\n  m_GameObject: {fileID: 10}\n"
                "  m_Script: {fileID: 11500000, guid: %s}\n"
                "  t: {fileID: 2}\n"
                % (rot[0], rot[1], "a" * 32, "c" * 32, "b" * 32))
        out = pack(self, root, strict=False)
        return [ln.replace("-0.000", "0.000") for ln in run_frames(
            self, out, 1, body=(
            "EngineDraw b[8]; int n = engine_collect_draws(b, 8), k;\n"
            "  for (k = 0; k < n; k++) if (b[k].tex >= 0)"
            " printf(\"half %g %g at %.3f %.3f\\n\", b[k].half_w, b[k].half_h,"
            " b[k].x, b[k].y);"))]

    @needs_cc
    def test_scaled_target_draws_unity_size(self):
        # 8 px at 8 px per unit, scale 2: 2 units wide
        self.assertEqual(self._draws(), ["half 1 1 at 0.000 0.000"])

    @needs_cc
    def test_pivot_offsets_the_quad_in_its_frame(self):
        # Slime Jump's platforms (pivot 0.52, 0.56) drew centred, 0.12 high.
        # Bottom-centre pivot (alignment 7), turned 90 degrees: the quad's
        # centre is 1 along the turned +y, i.e. at (-1, 0)
        h = 0.5 ** 0.5
        self.assertEqual(self._draws("  alignment: 7\n", (h, h)),
                         ["half 1 1 at -1.000 0.000"])
        self.assertEqual(self._draws(
            "  alignment: 9\n  spritePivot: {x: 0.25, y: 0.5}\n"),
            ["half 1 1 at 0.500 0.000"])


class TestUnpackedClassCall(unittest.TestCase):
    """`g.Use();` on a class crust packs no methods of (Slime Jump's
    `World.Instance.SetPieces()`, `fallerObject.Awake()`): Unity's NRE for
    a null receiver, else a stop at the call -- not a stubbed method."""

    @needs_cc
    def test_null_receiver_nre(self):
        a = ("using UnityEngine;\npublic class A : MonoBehaviour {\n"
             "    public Gadget g;\n    int f;\n"
             "    void Update() {\n        f++; Debug.Log(\"tick \" + f);\n"
             "        g.Use ();\n    }\n}\n")
        g = ("using UnityEngine;\npublic class Gadget : MonoBehaviour {\n"
             "    public void Use () { Debug.Log(\"used\"); }\n}\n")
        root = project(self, {"A": a, "Gadget": g},
                       [("A", None, "  g: {fileID: 0}\n")])
        rc, out, err = _run_rc(self, pack(self, root, strict=False), 2)
        self.assertIn("tick 1", out)
        self.assertNotIn("used", out)
        self.assertIn("A.Update () (at Assets/Scripts/A.cs:7)", err)


class TestSpriteSwap(unittest.TestCase):
    """`s = r.sprite;` / `r.sprite = s;` (Player's toggle images, Lasso's
    hook, SavePoint's touched sprite): a `Sprite` is its texture, and a
    SpriteRenderer swapped to another draws it at that sprite's size."""

    @needs_cc
    def test_swap_and_back(self):
        import struct
        import zlib
        a = ("using UnityEngine;\npublic class A : MonoBehaviour {\n"
             "    public SpriteRenderer sr;\n    public Sprite other;\n"
             "    Sprite orig;\n    int f;\n"
             "    void Start() { orig = sr.sprite; }\n"
             "    void Update() {\n        f++;\n"
             "        if (f == 1) sr.sprite = other;\n"
             "        else if (f == 2) sr.sprite = orig;\n    }\n}\n")
        root = project(self, {"A": a}, [(
            "A", "--- !u!212 &{fid}\nSpriteRenderer:\n  m_GameObject: {{fileID: {go}}}\n"
            "  m_Enabled: 1\n  m_Sprite: {{fileID: 21300000, guid: %s, type: 3}}\n"
            "  m_Color: {{r: 1, g: 1, b: 1, a: 1}}\n" % ("c" * 32),
            "  sr: {fileID: 103}\n"
            "  other: {fileID: 21300000, guid: %s, type: 3}\n" % ("d" * 32))])
        sd = os.path.join(root, "Assets", "Scripts")

        def chunk(tag, body):
            return (struct.pack(">I", len(body)) + tag + body
                    + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff))
        for name, w, guid in (("q", 8, "c"), ("r", 16, "d")):
            with open(os.path.join(sd, name + ".png"), "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n"
                        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, 8, 8, 6, 0, 0, 0))
                        + chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff" * 4 * w) * 8))
                        + chunk(b"IEND", b""))
            with open(os.path.join(sd, name + ".png.meta"), "w") as f:
                f.write("guid: %s\nTextureImporter:\n  spritePixelsToUnits: 8\n"
                        % (guid * 32))
        out = pack(self, root)
        draw = ("EngineDraw b[8]; int n = engine_collect_draws(b, 8), k;\n"
                "  for (k = 0; k < n; k++) if (b[k].tex >= 0)"
                " printf(\"w %d half %g %g\\n\", engine_texture_width(b[k].tex),"
                " b[k].half_w, b[k].half_h);")
        self.assertEqual(run_frames(self, out, 1, body=draw), ["w 16 half 1 0.5"])
        self.assertEqual(run_frames(self, out, 2, body=draw), ["w 8 half 0.5 0.5"])

    @needs_cc
    def test_null_image_nre(self):
        a = ("using UnityEngine;\nusing UnityEngine.UI;\n"
             "public class A : MonoBehaviour {\n"
             "    public Image img;\n    Sprite s;\n"
             "    void Update() {\n"
             "        s = img.sprite;\n"
             "        Debug.Log(\"after\");\n    }\n}\n")
        root = project(self, {"A": a}, [("A", None, "  img: {fileID: 0}\n")])
        rc, so, se = _run_rc(self, pack(self, root))
        self.assertIn("A.Update () (at Assets/Scripts/A.cs:7)", se)
        self.assertNotIn("after", so)


class TestAutoProperties(unittest.TestCase):
    """`{ get; set; }` is a field to the packer: it used to give a property no storage, so every method that read one was left a CS8000 stub."""

    def test_an_auto_property_becomes_a_field_on_the_same_line(self):
        src = ("class A {\n  public int Hp { get; private set; }\n  public static A Instance { get; private set; }\n"
               "  float Speed { get; set; } = 2f;\n  public List<int> Xs { get; } = new List<int>();\n}")
        out = unity_pack._auto_properties_to_fields(src)
        self.assertEqual(out.count("\n"), src.count("\n"))
        self.assertIn("public int Hp;", out)
        self.assertIn("public static A Instance;", out)
        self.assertIn("float Speed = 2f;", out)
        self.assertIn("public List<int> Xs = new List<int>();", out)

    def test_a_property_with_a_body_a_comment_and_a_string_are_left_alone(self):
        src = ("class A {\n  public bool Boss { get { return k != null; } }\n  // int Fake { get; set; }\n"
               "  string s = \"int Q { get; set; }\";\n}")
        self.assertEqual(unity_pack._auto_properties_to_fields(src), src)


class TestSerializableGenericBase(unittest.TestCase):
    """`FloatRange : Range<float>` is a plain [Serializable] value: its
    generic project base gives it fields, it is no component."""

    def test_a_generic_project_base_is_a_value_not_a_component(self):
        root = tempfile.mkdtemp(prefix="upack-derive-")
        srcs = {
            "Range": "public class Range<T> { public T min; public T max; }",
            "FloatRange": "[System.Serializable]\npublic class FloatRange : "
                          "Range<float>, IComparable { }",
            "Comp": "using UnityEngine;\npublic class Comp : MonoBehaviour { }",
            "Mid": "public class Mid : Comp { }",
        }
        tmap = {}
        for name, src in srcs.items():
            tmap[name] = os.path.join(root, name + ".cs")
            with open(tmap[name], "w") as f:
                f.write(src + "\n")
        derives = {n: unity_pack._derives_engine_type(
            unity_pack.analyze_script(p), tmap) for n, p in tmap.items()}
        self.assertEqual(derives, {"Range": False, "FloatRange": False,
                                   "Comp": True, "Mid": True})


class TestPrefabVariants(unittest.TestCase):
    """A prefab variant places its base (negative component fileIDs) with
    its overrides, as Unity does."""

    def test_a_variant_places_its_base_with_the_override(self):
        root = tempfile.mkdtemp(prefix="upack-variant-")
        base = os.path.join(root, "Base.prefab")
        var = os.path.join(root, "Variant.prefab")
        with open(base, "w") as f:
            f.write("%YAML 1.1\n"
                    "--- !u!1 &100\nGameObject:\n  m_Name: Base\n  m_IsActive: 1\n"
                    "--- !u!4 &101\nTransform:\n  m_GameObject: {fileID: 100}\n"
                    "  m_Father: {fileID: 0}\n"
                    "--- !u!212 &-5\nSpriteRenderer:\n  m_GameObject: {fileID: 100}\n"
                    "  m_Sprite: {fileID: 0}\n")
        with open(var, "w") as f:
            f.write("%YAML 1.1\n--- !u!1001 &900\nPrefabInstance:\n"
                    "  m_Modification:\n    m_TransformParent: {fileID: 0}\n"
                    "    m_Modifications:\n"
                    "    - target: {fileID: -5, guid: b0, type: 3}\n"
                    "      propertyPath: m_Sprite\n      value: \n"
                    "      objectReference: {fileID: -7, guid: ef, type: 3}\n"
                    "  m_SourcePrefab: {fileID: 100100000, guid: b0, type: 3}\n")
        scene = ("%YAML 1.1\n--- !u!1001 &50\nPrefabInstance:\n"
                 "  m_Modification:\n    m_TransformParent: {fileID: 0}\n"
                 "    m_Modifications: []\n"
                 "  m_SourcePrefab: {fileID: 100100000, guid: cd, type: 3}\n")
        out = unity_pack._expand_unstripped_prefab_instances(
            scene, {"b0": base, "cd": var})
        mask = unity_pack._FILE_ID_MASK
        sid = (((-5 ^ 900) & mask) ^ 50) & mask
        self.assertRegex(out, r"--- !u!212 &%d\n[^-]*m_Sprite: \{fileID: -7, "
                              r"guid: ef, type: 3\}" % sid)
        self.assertIn("m_Name: Base", out)


class TestRectField(unittest.TestCase):
    """A `Rect` field (CameraScript.viewRect): its authored value, and
    `.size =` / `.center =` writes moving it (CameraScript.HandlePosition
    and HandleViewSize) -- not a handle an int stands for."""

    @needs_cc
    def test_size_then_center(self):
        root = project(self, {"Cam": script(
            "Cam", "Debug.Log(\"a \" + viewRect.x + \" \" + viewRect.width);\n"
            "        viewRect.size = viewSize;\n"
            "        viewRect.center = transform.position;\n"
            "        Debug.Log(\"r \" + viewRect.x + \" \" + viewRect.y + \" \""
            " + viewRect.width + \" \" + viewRect.height + \" \" + viewRect.center.x);",
            "    public Rect viewRect;\n    public Vector2 viewSize;")},
            [("Cam", None, "  viewRect:\n    serializedVersion: 2\n    x: 1\n"
              "    y: 2\n    width: 3\n    height: 4\n"
              "  viewSize: {x: 4, y: 2}\n")])
        out = pack(self, root)
        # the object sits at x 0 (project's first), so centred on (0, 0)
        self.assertEqual(run_frames(self, out, 1), ["a 1 3", "r -2 -1 4 2 0"])


class TestCameraField(unittest.TestCase):
    """A `Camera` field on the camera the engine drives (the scene's
    MainCamera, else its first: CameraScript.HandleViewSize): `aspect`,
    `orthographicSize` and `rect` reach the engine's globals."""

    CAMERA = ("--- !u!20 &{fid}\nCamera:\n  m_GameObject: {{fileID: {go}}}\n"
              "  m_Orthographic: 1\n  m_OrthographicSize: 5\n")

    def _project(self, n, main_tag=None):
        root = project(self, {"Cam": script(
            "Cam", "float sa = (float) Screen.width / Screen.height;\n"
            "        camera.aspect = viewSize.x / viewSize.y;\n"
            "        camera.orthographicSize = Mathf.Max(viewSize.x / 2 / camera.aspect, viewSize.y / 2);\n"
            "        Rect r = new Rect();\n"
            "        r.size = new Vector2(camera.aspect / sa, Mathf.Min(1, sa / camera.aspect));\n"
            "        r.center = Vector2.one / 2;\n"
            "        camera.rect = r;\n"
            "        Debug.Log(\"c \" + camera.aspect + \" \" + camera.orthographicSize"
            " + \" \" + camera.rect.width + \" \" + camera.rect.height * 3);",
            "    public new Camera camera;\n    public Vector2 viewSize;")},
            [("Cam", self.CAMERA, "  camera: {fileID: %d}\n  viewSize: {x: 4, y: 2}\n"
              % (103 + 10 * k)) for k in range(n)])
        if main_tag is not None:
            scene = os.path.join(root, "Assets", "Scenes", "S.unity")
            with open(scene) as f:
                text = f.read()
            with open(scene, "w") as f:
                f.write(text.replace("m_Name: Cam%d\n" % main_tag,
                                     "m_Name: Cam%d\n  m_TagString: MainCamera\n" % main_tag))
        return root

    @needs_cc
    def test_handle_view_size(self):
        out = pack(self, self._project(1))
        # 1024x768 (4:3) screen, 2:1 view: a 1.5-wide rect clipped to the
        # screen's width, 2/3 of its height (letterboxed)
        self.assertEqual(run_frames(self, out, 1), ["c 2 1 1 2"])

    @needs_cc
    def test_camera_under_a_parent_follows_its_own_transform(self):
        # Slime Jump's Game Camera: a child whose own script moves it
        root = project(self, {
            "Rig": script("Rig", "transform.position = new Vector3(0, 0, transform.position.z);"),
            "Cam": script("Cam", "transform.position = new Vector3(7, 2, transform.position.z);")},
            [("Rig",), ("Cam", self.CAMERA)])
        scene = os.path.join(root, "Assets", "Scenes", "S.unity")
        with open(scene) as f:
            text = f.read()
        with open(scene, "w") as f:
            f.write(text.replace("  m_LocalPosition: {x: 1, y: 0, z: 0}\n",
                                 "  m_LocalPosition: {x: 1, y: 0, z: 0}\n"
                                 "  m_Father: {fileID: 101}\n"))
        out = pack(self, root)
        self.assertEqual(run_frames(self, out, 2, body=(
            "EngineCamera cams[4]; int n = engine_collect_cameras(cams, 4);\n"
            '  printf("cam %d %g %g\\n", n, cams[0].x, cams[0].y);')), ["cam 1 7 2"])

    def test_a_camera_the_engine_does_not_drive_stays_a_stub(self):
        # Cam0's camera is not the scene's: Cam1 holds the MainCamera tag
        with self.assertRaisesRegex(unity_pack.PackError, "CS8000: `Cam.Update` is not lowered"):
            pack(self, self._project(2, main_tag=1))


class TestRigidbodyRowWins(unittest.TestCase):
    """Two scripts on one GameObject each keep a position copy; physics
    moves the Rigidbody owner's, so `x.trs.position` reads that one
    (GameCamera following `Player.instance.trs`, beside AffectedByVortex)."""

    @needs_cc
    def test_a_transform_read_follows_the_body(self):
        a = ("using UnityEngine;\npublic class A : MonoBehaviour {\n"
             "    float x;\n    void Update() { x = transform.position.x; }\n}\n")
        pl = ("using UnityEngine;\npublic class P : MonoBehaviour {\n"
              "    public Transform trs;\n    public Rigidbody2D rb;\n    float x;\n"
              "    void Update() { x = transform.position.x; }\n}\n")
        r = ("using UnityEngine;\npublic class R : MonoBehaviour {\n"
             "    public P p;\n"
             "    void Start() { transform.SetParent(null); }\n"
             "    void Update() { Vector2 v = p.trs.position; Debug.Log(v.x); }\n}\n")
        extra = ("--- !u!114 &{fid}\nMonoBehaviour:\n  m_GameObject: {{fileID: {go}}}\n"
                 "  m_Script: {{fileID: 11500000, guid: %032x}}\n"
                 "--- !u!50 &104\nRigidbody2D:\n  m_GameObject: {{fileID: {go}}}\n"
                 "  m_BodyType: 0\n  m_Mass: 1\n  m_GravityScale: 0\n" % 1)
        root = project(self, {"A": a, "P": pl, "R": r}, [
            ("P", extra, "  trs: {fileID: 101}\n  rb: {fileID: 104}\n"),
            ("R", None, "  p: {fileID: 102}\n")])
        sc = os.path.join(root, "Assets", "Scenes", "S.unity")
        with open(sc) as f:
            t = f.read()
        with open(sc, "w") as f:
            f.write(t.replace("  - component: {fileID: 103}\n",
                              "  - component: {fileID: 103}\n  - component: {fileID: 104}\n", 1))
        out = pack(self, root)
        got = run_frames(self, out, 1, body="engine_rb2d_set_pos(0, 7.f, 0.f); engine_tick();",
                         pre="void engine_rb2d_set_pos(int rb, float x, float y);\n"
                             "void engine_box2d_step(void) { }\n")
        self.assertEqual(got[-1], "7")

    @needs_cc
    def test_simulated_takes_the_body_out(self):
        """`rigid.simulated = false` (Slime Jump's Player.Death): the glue's
        live gate disables the body until it is set again."""
        pl = ("using UnityEngine;\npublic class P : MonoBehaviour {\n"
              "    public Rigidbody2D rb;\n    int f;\n"
              "    void Update() { f++; rb.simulated = f != 1;"
              " Debug.Log(\"s\" + f + \" \" + (rb.simulated ? 1 : 0)); }\n}\n")
        rb = ("--- !u!50 &{fid}\nRigidbody2D:\n  m_GameObject: {{fileID: {go}}}\n"
              "  m_BodyType: 0\n  m_Mass: 1\n  m_GravityScale: 0\n")
        root = project(self, {"P": pl}, [("P", rb, "  rb: {fileID: 103}\n")])
        got = run_frames(self, pack(self, root), 1, body=(
            'printf("live %d\\n", engine_rb2d_live(0)); engine_tick();'
            ' printf("live %d\\n", engine_rb2d_live(0));'),
            pre="int engine_rb2d_live(int rb);\n"
                "void engine_box2d_step(void) { }\n")
        self.assertEqual([l for l in got if l[:1] in "sl"],
                         ["s1 0", "live 0", "s2 1", "live 1"])


class TestMultipleCameras(unittest.TestCase):
    """Every enabled camera renders, lowest depth first: its own view,
    viewport and clear, and only the layers in its culling mask; the main
    camera's view follows its GameObject (GameCamera.HandlePosition)."""

    @needs_cc
    def test_passes_by_depth_and_mask(self):
        import struct
        import zlib
        noop = "using UnityEngine;\npublic class N : MonoBehaviour { void Update() {} }\n"
        sprite = ("using UnityEngine;\npublic class S : MonoBehaviour {\n"
                  "    float x;\n    void Update() { x = transform.position.x; }\n}\n")
        mover = ("using UnityEngine;\npublic class M : MonoBehaviour {\n"
                 "    void Update() { transform.position = new Vector3(5f, 0f, -10f); }\n}\n")
        spr = ("--- !u!212 &{fid}\nSpriteRenderer:\n  m_GameObject: {{fileID: {go}}}\n"
               "  m_Enabled: 1\n  m_Sprite: {{fileID: 21300000, guid: %s, type: 3}}\n"
               "  m_Color: {{r: 1, g: 1, b: 1, a: 1}}\n" % ("c" * 32))

        def cam(depth, bits, clear, x0, size):
            return ("--- !u!20 &{fid}\nCamera:\n  m_GameObject: {{fileID: {go}}}\n"
                    "  m_Enabled: 1\n  m_ClearFlags: %d\n"
                    "  m_NormalizedViewPortRect:\n    serializedVersion: 2\n"
                    "    x: %s\n    y: 0\n    width: %s\n    height: 1\n"
                    "  orthographic: 1\n  orthographic size: %s\n  m_Depth: %s\n"
                    "  m_CullingMask:\n    serializedVersion: 2\n    m_Bits: %d\n"
                    % (clear, x0, 1 - x0, size, depth, bits))
        root = project(self, {"N": noop, "S": sprite, "M": mover}, [
            ("S", spr), ("S", spr), ("M", cam(0, 1, 2, 0, 5)),
            ("N", cam(-1, 256, 3, 0.5, 2))])
        scene = os.path.join(root, "Assets", "Scenes", "S.unity")
        with open(scene) as f:
            text = f.read()
        with open(scene, "w") as f:
            f.write(text.replace("m_Name: S1\n", "m_Name: S1\n  m_Layer: 8\n")
                    .replace("m_Name: M2\n", "m_Name: M2\n  m_TagString: MainCamera\n")
                    .replace("{x: 2, y: 0, z: 0}", "{x: 2, y: 0, z: -10}")
                    .replace("{x: 3, y: 0, z: 0}", "{x: 3, y: 0, z: -10}"))
        sd = os.path.join(root, "Assets", "Scripts")

        def chunk(tag, body):
            return (struct.pack(">I", len(body)) + tag + body
                    + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff))
        with open(os.path.join(sd, "q.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n"
                    + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 6, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff" * 32) * 8))
                    + chunk(b"IEND", b""))
        with open(os.path.join(sd, "q.png.meta"), "w") as f:
            f.write("guid: %s\nTextureImporter:\n  spritePixelsToUnits: 8\n" % ("c" * 32))
        out = pack(self, root)
        passes = (
            "EngineCamera cm[4]; EngineDraw b[8];\n"
            "  int nc = engine_collect_cameras(cm, 4), k, j, n;\n"
            "  for (k = 0; k < nc; k++) {\n"
            "    engine_draw_camera(k); n = engine_collect_draws(b, 8);\n"
            "    printf(\"cam %g size %g clear %d rect %g:\", cm[k].x, cm[k].half_h,"
            " cm[k].clear, cm[k].rect_x);\n"
            "    for (j = 0; j < n; j++) printf(\" %g\", b[j].x);\n"
            "    printf(\"\\n\");\n  }\n"
            "  engine_draw_camera(-1); printf(\"all %d\\n\", engine_collect_draws(b, 8));")
        self.assertEqual(run_frames(self, out, 1, body=passes), [
            "cam 3 size 2 clear 0 rect 0.5: 1",     # depth -1: layer 8 only
            "cam 5 size 5 clear 1 rect 0: 0",       # the main camera, moved
            "all 1"])                                # no pass: the main's view


class TestHeartBar(unittest.TestCase):
    """Slime Jump's Player.Respawn / TakeDamage heart bar: a layout group's
    children counted, destroyed and cloned through a Transform field, laid
    out again, and dimmed through their Image."""

    P = """using UnityEngine;
using UnityEngine.UI;
public static class ColorExtensions {
    public static Color SetAlpha (this Color c, float a) { return new Color(c.r, c.g, c.b, a); }
}
public class P : MonoBehaviour {
    public uint maxHp;
    public Transform hpBarTrs;
    float hp;
    void Start() { Respawn(); Respawn(); TakeDamage(1); }
    void Respawn() {
        hp = maxHp;
        if (hpBarTrs.childCount > 1)
            for (int i = 1; i < maxHp; i ++)
                DestroyImmediate(hpBarTrs.GetChild(0).gameObject);
        for (int i = 0; i < hp; i ++)
        {
            if (i > hpBarTrs.childCount - 1)
                Instantiate(hpBarTrs.GetChild(0).gameObject, hpBarTrs);
            else
            {
                Image image = hpBarTrs.GetChild(i).GetComponent<Image>();
                image.color = image.color.SetAlpha(1);
            }
        }
    }
    public void TakeDamage (float amount) {
        float prevHp = hp;
        hp = Mathf.Clamp(hp - amount, 0, maxHp);
        if (prevHp > hp && hp > 0)
            for (int i = 0; i < prevHp - hp; i ++)
            {
                Image image = hpBarTrs.GetChild((int) hp - i).GetComponent<Image>();
                image.color = image.color.SetAlpha(0.25f);
            }
    }
}
"""

    @staticmethod
    def _go(fid, name, comps, extra=""):
        return ("--- !u!1 &%d\nGameObject:\n  m_Name: %s\n  m_IsActive: 1\n%s"
                "  m_Component:\n%s" % (fid, name, extra, "".join(
                    "  - component: {fileID: %d}\n" % c for c in comps)))

    @staticmethod
    def _rt(fid, go, father, apos, size, pivot, kids=()):
        return ("--- !u!224 &%d\nRectTransform:\n  m_GameObject: {fileID: %d}\n"
                "  m_Father: {fileID: %d}\n  m_Children:%s\n"
                "  m_AnchorMin: {x: 0, y: %d}\n  m_AnchorMax: {x: 0, y: %d}\n"
                "  m_AnchoredPosition: {x: %s, y: %s}\n"
                "  m_SizeDelta: {x: %s, y: %s}\n  m_Pivot: {x: %s, y: %s}\n"
                % (fid, go, father, "".join("\n  - {fileID: %d}" % k for k in kids)
                   or " []", father != 0, father != 0, apos[0], apos[1],
                   size[0], size[1], pivot[0], pivot[1]))

    @needs_cc
    def test_respawn_clones_lays_out_and_dims(self):
        import struct
        import zlib
        root = tempfile.mkdtemp(prefix="upf-hearts-")
        self.addCleanup(shutil.rmtree, root, True)
        sd = os.path.join(root, "Assets", "Scripts")
        os.makedirs(sd)
        os.makedirs(os.path.join(root, "Assets", "Scenes"))
        with open(os.path.join(sd, "P.cs"), "w") as f:
            f.write(self.P)
        with open(os.path.join(sd, "P.cs.meta"), "w") as f:
            f.write("guid: %032x\n" % 1)

        def chunk(tag, body):
            return (struct.pack(">I", len(body)) + tag + body
                    + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff))
        with open(os.path.join(sd, "heart.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n"
                    + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 6, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff" * 32) * 8))
                    + chunk(b"IEND", b""))
        with open(os.path.join(sd, "heart.png.meta"), "w") as f:
            f.write("guid: %s\nTextureImporter:\n  spritePixelsToUnits: 100\n" % ("c" * 32))
        mb = ("--- !u!114 &%d\nMonoBehaviour:\n  m_GameObject: {fileID: %d}\n"
              "  m_Enabled: 1\n  m_Script: {fileID: 11500000, guid: %s, type: 3}\n")
        scene = "".join((
            "%YAML 1.1\n",
            self._go(1, "Canvas", (2, 3)),
            self._rt(2, 1, 0, (400, 300), (800, 600), (0.5, 0.5), (11,)),
            "--- !u!223 &3\nCanvas:\n  m_GameObject: {fileID: 1}\n  m_Enabled: 1\n"
            "  m_RenderMode: 0\n",
            self._go(10, "Bar", (11, 12)),
            self._rt(11, 10, 2, (5, -100), (300, 90), (0, 1), (21,)),
            mb % (12, 10, "30649d3a9faa99c48a7b1166b86bf2a0"),
            "  m_Padding:\n    m_Left: 0\n    m_Right: 0\n    m_Top: 0\n    m_Bottom: 0\n"
            "  m_ChildAlignment: 0\n  m_Spacing: 10\n  m_ChildForceExpandWidth: 0\n"
            "  m_ChildForceExpandHeight: 0\n  m_ChildControlWidth: 0\n"
            "  m_ChildControlHeight: 0\n",
            self._go(20, "Heart", (21, 23)),
            self._rt(21, 20, 11, (45, -45), (90, 90), (0.5, 0.5)),
            mb % (23, 20, "fe87c0e1cc204ed48ad3b37840f39efc"),
            "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n"
            "  m_Sprite: {fileID: 21300000, guid: %s, type: 3}\n" % ("c" * 32),
            self._go(30, "Player", (31, 32)),
            "--- !u!4 &31\nTransform:\n  m_GameObject: {fileID: 30}\n  m_Father: {fileID: 0}\n",
            mb % (32, 30, "%032x" % 1), "  maxHp: 3\n  hpBarTrs: {fileID: 11}\n",
            self._go(40, "Main Camera", (41, 42), "  m_TagString: MainCamera\n"),
            "--- !u!4 &41\nTransform:\n  m_GameObject: {fileID: 40}\n  m_Father: {fileID: 0}\n"
            "  m_LocalPosition: {x: 0, y: 0, z: -10}\n",
            "--- !u!20 &42\nCamera:\n  m_GameObject: {fileID: 40}\n  m_Enabled: 1\n"
            "  orthographic: 1\n  orthographic size: 5\n"))
        with open(os.path.join(root, "Assets", "Scenes", "S.unity"), "w") as f:
            f.write(scene)
        out = pack(self, root)
        lines = run_frames(self, out, 1, body=(
            "EngineDraw b[8]; int n = engine_collect_draws(b, 8), j;\n"
            "  for (j = 0; j < n; j++) printf(\"%.3f %g\\n\", b[j].x, b[j].a);"))
        # the second Respawn keeps the last clone and clones it twice; one
        # heart (90 px) + spacing (10 px) apart; TakeDamage dims the third
        self.assertEqual(lines, ["-6.016 1", "-4.714 1", "-3.411 0.25"])


class TestTilemapTiles(unittest.TestCase):
    """Tilemap tiles baked into world-space sprite draws."""

    def test_a_tile_lands_in_its_cell_through_a_flipped_tilemap(self):
        yaml = (
            "%YAML 1.1\n"
            "--- !u!1 &10\nGameObject:\n  m_Name: Grid\n  m_IsActive: 1\n"
            "--- !u!4 &11\nTransform:\n  m_GameObject: {fileID: 10}\n"
            "  m_LocalPosition: {x: 1, y: 0, z: 0}\n  m_Father: {fileID: 0}\n"
            "--- !u!156049354 &12\nGrid:\n  m_GameObject: {fileID: 10}\n"
            "  m_CellSize: {x: 2, y: 1, z: 1}\n  m_CellLayout: 0\n  m_CellSwizzle: 0\n"
            "--- !u!1 &20\nGameObject:\n  m_Name: Tilemap\n  m_IsActive: 1\n"
            "--- !u!4 &21\nTransform:\n  m_GameObject: {fileID: 20}\n"
            "  m_LocalScale: {x: -1, y: 1, z: 1}\n  m_Father: {fileID: 11}\n"
            "--- !u!1839735485 &22\nTilemap:\n  m_GameObject: {fileID: 20}\n"
            "  m_Tiles:\n"
            "  - first: {x: 1, y: 0, z: 0}\n    second:\n      m_TileSpriteIndex: 0\n"
            "      m_TileMatrixIndex: 0\n      m_TileColorIndex: 0\n"
            "  - first: {x: 0, y: 0, z: 0}\n    second:\n      m_TileSpriteIndex: 1\n"
            "      m_TileMatrixIndex: 0\n      m_TileColorIndex: 0\n"
            "  m_TileSpriteArray:\n"
            "  - m_RefCount: 1\n    m_Data: {fileID: 5, guid: aa, type: 3}\n"
            "  - m_RefCount: 1\n    m_Data: {fileID: 0}\n"
            "  m_TileMatrixArray:\n  - m_RefCount: 2\n    m_Data:\n      e00: 1\n"
            "      e11: 1\n  m_TileColorArray:\n"
            "  - m_RefCount: 2\n    m_Data: {r: 1, g: 0.5, b: 1, a: 1}\n"
            "  m_Color: {r: 1, g: 1, b: 1, a: 0.5}\n"
            "  m_TileAnchor: {x: 0.5, y: 0.5, z: 0}\n"
            "--- !u!483693784 &23\nTilemapRenderer:\n  m_GameObject: {fileID: 20}\n"
            "  m_Enabled: 1\n  m_SortingOrder: -100\n")
        unity_pack.parse_unity_yaml(yaml)
        tiles = unity_pack.parse_unity_yaml.tiles
        self.assertEqual(len(tiles), 1)   # the null-sprite tile is not drawn
        t = tiles[0]
        # cell 1 * width 2 + anchor 0.5 * 2 = 3, mirrored, then the Grid's x 1
        self.assertAlmostEqual(t["x"], -2.0)
        self.assertAlmostEqual(t["y"], 0.5)
        self.assertAlmostEqual(t["m00"], -1.0)
        self.assertEqual((t["sprite_file_id"], t["sprite_guid"]), (5, "aa"))
        self.assertEqual((t["g"], t["a"], t["sorting_order"]), (0.5, 0.5, -100))

    @needs_cc
    def test_a_tie_in_layer_and_order_draws_the_farther_first(self):
        """Slime Jump's background Tilemaps share layer and order at z 0, -1
        and -2: Unity draws the farther first, a sprite between them in z
        between them."""
        import struct
        import zlib
        root = project(self, {"A": "using UnityEngine;\npublic class A :"
                              " MonoBehaviour { }\n"}, [("A",)])
        sd = os.path.join(root, "Assets", "Scripts")

        def chunk(tag, body):
            return (struct.pack(">I", len(body)) + tag + body
                    + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff))
        for w, guid in ((4, "b"), (8, "c"), (16, "d")):
            with open(os.path.join(sd, "t%d.png" % w), "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n"
                        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, 8, 8, 6, 0, 0, 0))
                        + chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff" * 4 * w) * 8))
                        + chunk(b"IEND", b""))
            with open(os.path.join(sd, "t%d.png.meta" % w), "w") as f:
                f.write("guid: %s\nTextureImporter:\n  spritePixelsToUnits: 8\n"
                        % (guid * 32))

        def tilemap(fid, z, guid):
            return (
                "--- !u!1 &%d\nGameObject:\n  m_Name: TM%d\n  m_IsActive: 1\n"
                "--- !u!4 &%d\nTransform:\n  m_GameObject: {fileID: %d}\n"
                "  m_LocalPosition: {x: 0, y: 0, z: %s}\n  m_Father: {fileID: 0}\n"
                "--- !u!1839735485 &%d\nTilemap:\n  m_GameObject: {fileID: %d}\n"
                "  m_Tiles:\n  - first: {x: 0, y: 0, z: 0}\n    second:\n"
                "      m_TileSpriteIndex: 0\n      m_TileMatrixIndex: 0\n"
                "      m_TileColorIndex: 0\n  m_TileSpriteArray:\n"
                "  - m_RefCount: 1\n    m_Data: {fileID: 21300000, guid: %s, type: 3}\n"
                "  m_TileMatrixArray:\n  - m_RefCount: 1\n    m_Data:\n      e00: 1\n"
                "      e11: 1\n  m_TileColorArray:\n"
                "  - m_RefCount: 1\n    m_Data: {r: 1, g: 1, b: 1, a: 1}\n"
                "--- !u!483693784 &%d\nTilemapRenderer:\n  m_GameObject: {fileID: %d}\n"
                "  m_Enabled: 1\n  m_SortingOrder: -100\n"
                % (fid, fid, fid + 1, fid, z, fid + 2, fid, guid * 32, fid + 3, fid))
        with open(os.path.join(root, "Assets", "Scenes", "S.unity"), "a") as f:
            f.write(tilemap(500, "-1", "d") + tilemap(600, "0", "c")
                    + "--- !u!1 &700\nGameObject:\n  m_Name: Spr\n  m_IsActive: 1\n"
                    "  m_Component:\n  - component: {fileID: 701}\n"
                    "  - component: {fileID: 702}\n"
                    "--- !u!4 &701\nTransform:\n  m_GameObject: {fileID: 700}\n"
                    "  m_LocalPosition: {x: 0, y: 0, z: -0.5}\n  m_Father: {fileID: 0}\n"
                    "--- !u!212 &702\nSpriteRenderer:\n  m_GameObject: {fileID: 700}\n"
                    "  m_Enabled: 1\n  m_SortingOrder: -100\n"
                    "  m_Sprite: {fileID: 21300000, guid: %s, type: 3}\n"
                    "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n" % ("b" * 32))
        out = pack(self, root, strict=False)
        self.assertEqual(run_frames(self, out, 1, body=(
            "EngineDraw b[8]; int n = engine_collect_draws(b, 8), k;\n"
            "  for (k = 0; k < n; k++) if (b[k].tex >= 0)"
            " printf(\"w %d\\n\", engine_texture_width(b[k].tex));")),
            ["w 8", "w 4", "w 16"])


class TestStretchedImageBake(unittest.TestCase):
    def test_never_past_the_source(self):
        """A Simple Image's rect far larger than its sprite (Slime Jump's
        Background) bakes at most the sprite's pixels, not the rect's."""
        w, h, rgba = unity_pack._bake_stretched_rgba(
            bytes(range(16)), 2, 2, 40000.0, 1.0)
        self.assertEqual((w, h, len(rgba)), (2, 1, 8))

    def test_bakes_at_screen_pixels(self):
        """Slime Jump's 29010×14505 CanvasScaler: a 6000×3000-unit panel
        bakes at screen pixels (pixel_scale), still draws at rect size."""
        img = {"xf_id": 2, "father_id": 1, "pos": (0, 0, 0),
               "rect": {"anchor_min": (0.5, 0.5), "anchor_max": (0.5, 0.5),
                        "size_delta": (6000.0, 3000.0)},
               "ui_image": {"has_sprite": True, "builtin": True,
                            "image_type": 1}}
        objs = [{"xf_id": 1, "canvas": {"render_mode": 0}}, img]
        unity_pack._bake_ui_images(objs, [], 29010, 14505, pixel_scale=0.1)
        sp = img["sprite"]
        self.assertEqual((sp["tex_w"], sp["tex_h"]), (600, 300))
        self.assertAlmostEqual(sp["nhw"], 3000.0 / 29010)


class TestCanvasCamera(unittest.TestCase):
    def test_pass_of_the_canvas_camera(self):
        """Screen Space Camera: no camera renders as Overlay (Slime Jump's
        Game Manager canvas); a camera's canvas draws in that camera's pass
        only (Game Camera's, not World Map Camera's)."""
        img = lambda cam: {"xf_id": 2, "father_id": 1, "pos": (0, 0, 0),
                           "rect": {"size_delta": (10.0, 10.0)},
                           "ui_image": {"has_sprite": True, "builtin": True}}
        plan = {"cameras": [{"cam_file_id": "5"},
                            {"cam_file_id": "7", "renders": False},
                            {"cam_file_id": "9"}]}
        for cam, layer in ((0, -1), (9, -4), (7, -2)):
            o = img(cam)
            unity_pack._bake_ui_images(
                [{"xf_id": 1, "canvas": {"render_mode": 1,
                                         "camera_file_id": cam}}, o],
                [], 1920, 960)
            self.assertEqual(unity_pack._draw_layer(plan, None, o["sprite"]),
                             layer)

    def test_own_canvas_scaler(self):
        """A root canvas whose CanvasScaler gives it a tenth of the layout
        screen's units: a 100-unit child is 1000 layout units wide."""
        by_xf = {"1": {"xf_id": 1, "canvas": {"render_mode": 0},
                       "canvas_units": 0.1},
                 "2": {"xf_id": 2, "father_id": 1,
                       "rect": {"size_delta": (100.0, 50.0)}}}
        r = unity_pack._ui_screen_rect(by_xf["2"], by_xf, 2000, 1000, {})
        self.assertEqual((round(r[2]), round(r[3])), (1000, 500))


class TestInlinedHelperFloats(unittest.TestCase):
    @needs_cc
    def test_inlined_body_literals(self):
        """Slime Jump's MathfExtensions.Remap: the inlined Lerp's `1f`."""
        ext = ("public static class MathfX {\n"
               "    public static float Lerp(float a, float b, float t) {"
               " return (1f - t) * a + b * t; }\n"
               "    public static float Remap(float i0, float i1, float o0,"
               " float o1, float v) {\n"
               "        float t = (v - i0) / (i1 - i0);\n"
               "        return Lerp(o0, o1, t);\n    }\n}\n")
        mgr = ("using UnityEngine;\npublic class Mgr : MonoBehaviour {\n"
               "    void Start() { Debug.Log(\"r \" + Mathf.RoundToInt("
               "MathfX.Remap(0, 10, 0, 100, 5))); }\n}\n")
        root = project(self, {"MathfX": ext, "Mgr": mgr}, [("Mgr",)])
        out = run_frames(self, pack(self, root), 1)
        self.assertIn("r 50", out)


class TestWorldScene(unittest.TestCase):
    @needs_cc
    def test_world_shapes(self):
        """What Slime Jump's World scene reached: `zone.Gen()` beside two
        static `Gen(..)`, `instance.moveInput = amt` (a float) beside
        another class's Vector2 `moveInput`, and a `float[]` field (a
        vector of its numbers per instance; the pack still builds)."""
        zone = ("using UnityEngine;\npublic class Zone : MonoBehaviour {\n"
                "    public int n;\n"
                "    public static int Gen(int k) { return k; }\n"
                "    public static int Gen(float k) { return 0; }\n"
                "    public int Gen() { n = n + 1; return n; }\n}\n")
        mapc = ("using UnityEngine;\npublic class Map : MonoBehaviour {\n"
                "    public Vector2 moveInput;\n}\n")
        mgr = ("using UnityEngine;\npublic class Mgr : MonoBehaviour {\n"
               "    public static Mgr instance;\n    public Zone zone;\n"
               "    public float[] vols = new float[0];\n    float moveInput;\n"
               "    float v;\n"
               "    void Awake() { instance = this; }\n"
               "    void SetMove(float amt) { instance.moveInput = amt; }\n"
               "    void Vol() { v = vols[0]; }\n"
               "    void Start() { SetMove(3); zone.Gen(); Debug.Log(\"g \""
               " + zone.Gen() + \" m \" + moveInput); }\n}\n")
        root = project(self, {"Zone": zone, "Map": mapc, "Mgr": mgr}, [
            ("Mgr", None, "  zone: {fileID: 112}\n"), ("Zone",), ("Map",)])
        out = run_frames(self, pack(self, root), 1)
        self.assertIn("g 2 m 3", out)


class TestOtherInstanceMap(unittest.TestCase):
    def test_members_on_the_row(self):
        """Slime Jump's `vortex.affectedByVortexVelocitiesDict.Clear()`:
        another object's map is its class's table at the object's row."""
        import tools.cs2cpp as cs2cpp
        out = cs2cpp.lower_map_members_named(
            "V_d[v].Clear(); n = V_d[v].Count; V_d[v].Remove(k);",
            (), {}, {"V_d"})
        self.assertEqual(
            out, "V_d[v].clear(); n = V_d[v].size(); V_d[v].erase(k);")


class TestWorldPieceStreaming(unittest.TestCase):
    """Slime Jump's World.SetPieces / DoUpdate / GetSurroundingPieces,
    music left out: a `WorldPiece[,]` table, a `params WorldPiece[]`
    method returning an array, another piece's `WorldPiece[]` field, a
    `new List<T>(field)` copy, Rect extension methods. Three inactive
    10-wide pieces; the camera sees piece 0 and, grown by the load range,
    piece 1's edge -- so 0 and 1 load, 2 stays off, as in Unity."""

    S = {"RectExt": """using UnityEngine;
public static class RectExt {
    public static bool Intersects (this Rect a, Rect b, bool equal = true) {
        return a.xMin <= b.xMax && a.xMax >= b.xMin && a.yMin <= b.yMax && a.yMax >= b.yMin; }
    public static bool Intersects (this Rect a, Rect b, float pad, float pad2, bool equal = true) {
        return false; }
    public static Rect Grow (this Rect rect, Vector2 amount) {
        Vector2 center = rect.center; rect.size += amount; rect.center = center; return rect; }
}
""", "WorldPiece": """using UnityEngine;
public class WorldPiece : MonoBehaviour {
    public Vector2Int location;
    public Rect worldBoundsRect;
    public WorldPiece[] piecesToLoadAndUnloadWithMe = new WorldPiece[0];
}
""", "Cam": """using UnityEngine;
public class Cam : MonoBehaviour {
    public Transform trs;
    public Rect viewRect;
    public static Cam instance;
    void Awake () { instance = this; viewRect.size = new Vector2(8, 4); viewRect.center = trs.position; }
}
""", "World": """using UnityEngine;
using System.Collections.Generic;
public class World : MonoBehaviour {
    public Dictionary<Vector2Int, WorldPiece> piecesDict = new Dictionary<Vector2Int, WorldPiece>();
    public WorldPiece[,] pieces;
    public Vector2Int maxPieceLocation;
    public Transform piecesParent;
    public Transform player;
    public GameObject p0;
    public GameObject p1;
    public GameObject p2;
    public List<WorldPiece> activePieces = new List<WorldPiece>();
    public float[] vols = new float[0];
    WorldPiece piecePlayerIsIn;
    void Start () {
        Debug.Log("vols " + vols.Length + " " + vols[1]);
        pieces = new WorldPiece[maxPieceLocation.x + 1, maxPieceLocation.y + 1];
        piecePlayerIsIn = null;
        for (int i = 0; i < piecesParent.childCount; i ++) {
            WorldPiece piece = piecesParent.GetChild(i).GetComponent<WorldPiece>();
            piecesDict.Add(piece.location, piece);
            pieces[piece.location.x, piece.location.y] = piece;
            if (piecePlayerIsIn == null && piece.worldBoundsRect.Contains(player.position))
                piecePlayerIsIn = piece;
        }
    }
    void Update () {
        List<WorldPiece> prevActivePieces = new List<WorldPiece>(activePieces);
        activePieces.Clear();
        LoadPiece (piecePlayerIsIn);
        WorldPiece[] surroundingPieces = GetSurroundingPieces(piecePlayerIsIn);
        for (int i = 0; i < surroundingPieces.Length; i ++) {
            WorldPiece surroundingPiece = surroundingPieces[i];
            Rect loadCheckRect = surroundingPiece.worldBoundsRect.Grow(Vector2.one * 2);
            if (prevActivePieces.Contains(surroundingPiece))
                loadCheckRect = loadCheckRect.Grow(Vector2.one * 2);
            if (Cam.instance.viewRect.Intersects(loadCheckRect)) {
                LoadPiece (surroundingPiece);
                for (int i2 = 0; i2 < surroundingPiece.piecesToLoadAndUnloadWithMe.Length; i2 ++)
                    LoadPiece (surroundingPiece.piecesToLoadAndUnloadWithMe[i2]);
            }
        }
        for (int i = 0; i < prevActivePieces.Count; i ++) {
            WorldPiece prevActivePiece = prevActivePieces[i];
            if (!activePieces.Contains(prevActivePiece))
                prevActivePiece.gameObject.SetActive(false);
        }
        Debug.Log("on " + (p0.activeSelf ? 1 : 0) + (p1.activeSelf ? 1 : 0)
            + (p2.activeSelf ? 1 : 0) + " prev " + prevActivePieces.Count);
    }
    void LoadPiece (WorldPiece piece) {
        piece.gameObject.SetActive(true);
        activePieces.Add(piece);
    }
    public virtual WorldPiece[] GetSurroundingPieces (params WorldPiece[] innerPieces) {
        List<WorldPiece> output = new List<WorldPiece>();
        for (int i = 0; i < innerPieces.Length; i ++) {
            WorldPiece piece = innerPieces[i];
            if (piece.location.x < maxPieceLocation.x)
                output.Add(pieces[piece.location.x + 1, piece.location.y]);
            if (piece.location.x > 0)
                output.Add(pieces[piece.location.x - 1, piece.location.y]);
            for (int i2 = 0; i2 < innerPieces.Length; i2 ++)
                output.Remove(innerPieces[i2]);
        }
        return output.ToArray();
    }
}
"""}

    def _scene(self, guid):
        def go(fid, name, comps, active=1):
            return ("--- !u!1 &%d\nGameObject:\n  m_Name: %s\n  m_IsActive: %d\n"
                    "  m_Component:\n%s" % (fid, name, active, "".join(
                        "  - component: {fileID: %d}\n" % c for c in comps)))

        def xf(fid, gof, father=0, x=0, z=0, kids=()):
            return ("--- !u!4 &%d\nTransform:\n  m_GameObject: {fileID: %d}\n"
                    "  m_Father: {fileID: %d}\n"
                    "  m_LocalPosition: {x: %d, y: 0, z: %d}\n"
                    "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
                    "  m_LocalScale: {x: 1, y: 1, z: 1}\n  m_Children:\n%s"
                    % (fid, gof, father, x, z, "".join(
                        "  - {fileID: %d}\n" % k for k in kids) or "  []\n"))

        def mb(fid, gof, cls, fields=""):
            return ("--- !u!114 &%d\nMonoBehaviour:\n  m_GameObject: {fileID: %d}\n"
                    "  m_Enabled: 1\n  m_Script: {fileID: 11500000, guid: %s,"
                    " type: 3}\n%s" % (fid, gof, guid[cls], fields))

        s = "%YAML 1.1\n" + go(1000, "Pieces", [1001]) + xf(
            1001, 1000, kids=[201, 211, 221])
        for k in range(3):
            f = 200 + 10 * k
            s += go(f, "Piece%d" % k, [f + 1, f + 2], active=0)
            s += xf(f + 1, f, father=1001, x=10 * k)
            s += mb(f + 2, f, "WorldPiece",
                    "  location: {x: %d, y: 0}\n  worldBoundsRect:\n"
                    "    serializedVersion: 2\n    x: %d\n    y: -5\n"
                    "    width: 10\n    height: 10\n"
                    "  piecesToLoadAndUnloadWithMe:\n  - {fileID: %d}\n"
                    % (k, 10 * k - 5, f + 2))
        s += go(300, "World", [301, 302]) + xf(301, 300)
        s += mb(302, 300, "World", "  maxPieceLocation: {x: 2, y: 0}\n"
                "  piecesParent: {fileID: 1001}\n  player: {fileID: 401}\n"
                "  p0: {fileID: 200}\n  p1: {fileID: 210}\n  p2: {fileID: 220}\n"
                "  vols:\n  - 0.5\n  - 2.25\n")
        s += go(400, "Player", [401]) + xf(401, 400)
        s += go(500, "Cam", [501, 502]) + xf(501, 500, z=-10)
        return s + mb(502, 500, "Cam", "  trs: {fileID: 501}\n")

    @needs_cc
    def test_pieces_near_the_camera_load(self):
        root = tempfile.mkdtemp(prefix="upf-")
        self.addCleanup(shutil.rmtree, root, True)
        sd = os.path.join(root, "Assets", "Scripts")
        os.makedirs(sd)
        guid = {n: "%032x" % (k + 1) for k, n in enumerate(sorted(self.S))}
        for n, src in self.S.items():
            with open(os.path.join(sd, n + ".cs"), "w") as f:
                f.write(src)
            with open(os.path.join(sd, n + ".cs.meta"), "w") as f:
                f.write("guid: %s\n" % guid[n])
        os.makedirs(os.path.join(root, "Assets", "Scenes"))
        with open(os.path.join(root, "Assets", "Scenes", "S.unity"), "w") as f:
            f.write(self._scene(guid))
        out = run_frames(self, pack(self, root), 2)
        self.assertIn("vols 2 2.25", out)
        self.assertEqual([l for l in out if l.startswith("on ")],
                         ["on 110 prev 0", "on 110 prev 3"])


class TestInterfaceRefs(unittest.TestCase):
    """Slime Jump's Hazard: `IDestructable d = other.GetComponentInParent
    <IDestructable>(); if (d != null) d.TakeDamage(dmg, dir, null);` -- an
    interface reference names its implementor's class and instance, and a
    call through it reaches that class's method."""

    SRC = {
        "IHurt": "using UnityEngine;\npublic interface IHurt {\n"
                 "    void Hurt(float amount, Victim by);\n}\n",
        "Victim": "using UnityEngine;\npublic class Victim : MonoBehaviour, IHurt {\n"
                  "    public float hp = 3;\n    public void Hurt(float amount, Victim by) {\n"
                  "        hp -= amount; Debug.Log(\"victim \" + hp + \" \" + (by == null)); }\n}\n",
        "Rock": "using UnityEngine;\npublic class Rock : MonoBehaviour, IHurt {\n"
                "    public void Hurt(float amount, Victim by) { Debug.Log(\"rock \" + amount); }\n}\n",
    }

    def _root(self, apply_arg="d"):
        spike = ("using UnityEngine;\npublic class Spike : MonoBehaviour {\n"
                 "    public GameObject target;\n    public Victim v;\n    int _f;\n"
                 "    void Update() { _f++; if (_f > 1) return;\n"
                 "        IHurt d = target.GetComponentInParent<IHurt>();\n"
                 "        if (d != null) Apply(%s, 2);\n    }\n"
                 "    void Apply(IHurt d, float amount) { d.Hurt(amount, null); }\n}\n"
                 % apply_arg)
        return project(self, dict(self.SRC, Spike=spike), [
            ("Victim",), ("Rock",),
            ("Spike", None, "  target: {fileID: 100}\n  v: {fileID: 102}\n"),
            ("Spike", None, "  target: {fileID: 110}\n  v: {fileID: 102}\n")])

    @needs_cc
    def test_dispatch(self):
        out = run_frames(self, pack(self, self._root()), 2)
        self.assertEqual(sorted(out), ["rock 2", "victim 1 True"])

    def test_a_class_handle_for_an_interface_stubs(self):
        with self.assertRaisesRegex(unity_pack.PackError,
                                    r"CS8000: `Spike.Update`.*ToIHurt\("):
            pack(self, self._root("v"))


class TestSerializedFloats(unittest.TestCase):
    """Slime Jump's Spike: `damage: Infinity` -- and `1e-05` -- as Unity
    serializes them, not dropped to the field's default 0."""

    @needs_cc
    def test_infinity_and_exponent(self):
        src = ("using UnityEngine;\npublic class Spike : MonoBehaviour {\n"
               "    public float damage; public float tiny; public float bad;\n"
               "    void Start() { Debug.Log(\"d \" + (damage > 1e30f) + \" t \" + (tiny * 100000f)"
               " + \" n \" + (bad != bad)); }\n}\n")
        out = run_frames(self, pack(self, project(self, {"Spike": src}, [
            ("Spike", None, "  damage: Infinity\n  tiny: 1e-05\n  bad: NaN\n")])), 1)
        self.assertEqual(out, ["d True t 1 n True"])


class TestRigidbody2DLocal(unittest.TestCase):
    """Slime Jump's Player.Death: `Rigidbody2D r = go.GetComponent<..>();`
    then `r.worldCenterOfMass` (Box2D-Packed's body centre)."""

    @needs_cc
    def test_world_center_of_mass(self):
        rb = ("--- !u!50 &{fid}\nRigidbody2D:\n  m_GameObject: {{fileID: {go}}}\n"
              "  m_BodyType: 0\n  m_Mass: 1\n  m_GravityScale: 1\n")
        src = script("P", "Rigidbody2D r = gameObject.GetComponent<Rigidbody2D>(); "
                     "Vector2 c = r.worldCenterOfMass - (Vector2) transform.position; "
                     "Debug.Log(\"c \" + c.x + \" \" + c.y);")
        glue = ("void engine_box2d_step(void) { }\n"
                "void engine_rb2d_world_com(int rb, float *x, float *y) { *x = 7.f + rb; *y = 3.f; }\n")
        out = run_frames(self, pack(self, project(self, {"P": src}, [("P", rb)])), 1, pre=glue)
        self.assertEqual(out, ["c 7 3"])


class TestStaticPropertyIncrement(unittest.TestCase):
    """Slime Jump's `GameManager.DeathCount ++;`: a one-statement getter
    (inlined for reads) and a two-statement setter -- the setter of the
    getter's value plus one, not `(expr)++`."""

    @needs_cc
    def test_increment_through_the_setter(self):
        gm = ("using UnityEngine;\npublic class Gm : MonoBehaviour {\n"
              "    public static int saves;\n"
              "    public static int Count { get { return P.store; } set { P.store = value; saves ++; } }\n}\n")
        src = script("P", "Gm.Count ++; Gm.Count ++; Debug.Log(\"n \" + Gm.Count + \" \" + Gm.saves);",
                     "    public static int store;")
        out = run_frames(self, pack(self, project(self, {"P": src, "Gm": gm}, [("P",), ("Gm",)])), 1)
        self.assertEqual(out, ["n 2 2"])


class TestDictTryGetValue(unittest.TestCase):
    """Slime Jump's SaveAndLoadManager.GetInt: `if (saveData.intDict
    .TryGetValue(key, out output)) return output; else return value;` on
    a static instance of a plain class."""

    @needs_cc
    def test_found_and_default(self):
        sd = ("using System.Collections.Generic;\npublic class SaveData {\n"
              "    public Dictionary<string, int> intDict = new Dictionary<string, int>();\n}\n")
        gm = ("using UnityEngine;\npublic class Gm : MonoBehaviour {\n"
              "    static SaveData saveData = new SaveData();\n"
              "    public static int GetInt (string key, int value = 0) {\n"
              "        int output = 0;\n"
              "        if (saveData.intDict.TryGetValue(key, out output))\n"
              "            return output;\n"
              "        else\n"
              "            return value;\n    }\n"
              "    public static void SetInt (string key, int value) { saveData.intDict[key] = value; }\n}\n")
        src = script("P", "Gm.SetInt(\"a\", 5); Debug.Log(\"g \" + Gm.GetInt(\"a\", 9) + \" \" + Gm.GetInt(\"b\", 9));")
        out = run_frames(self, pack(self, project(self, {"P": src, "Gm": gm, "SaveData": sd},
                                                  [("P",), ("Gm",)])), 1)
        self.assertEqual(out, ["g 5 9"])


class TestInstantiateUnpackedChild(unittest.TestCase):
    """Slime Jump's Player.Respawn: `Instantiate(hpBarTrs.GetChild(0).gameObject,
    hpBarTrs)` of a child with no script. The method lowers; the clone stops
    the player if it runs (crust clones packed scripts' rows only)."""

    CHILD = ("--- !u!1 &900\nGameObject:\n  m_Name: Heart\n  m_Component:\n"
             "  - component: {fileID: 901}\n--- !u!4 &901\nTransform:\n"
             "  m_GameObject: {fileID: 900}\n  m_Father: {fileID: 101}\n"
             "  m_LocalPosition: {x: 0, y: 0, z: 0}\n")

    def _out(self, n):
        root = project(self, {"P": script("P", "Debug.Log(\"a\"); if (n > 0) Instantiate(bar.GetChild(0).gameObject, bar); "
                                               "Debug.Log(\"b\");", "    public Transform bar;\n    public int n;")},
                       [("P", None, "  bar: {fileID: 101}\n  n: %d\n" % n)])
        with open(os.path.join(root, "Assets", "Scenes", "S.unity"), "a") as f:
            f.write(self.CHILD)
        return pack(self, root)

    @needs_cc
    def test_not_reached_runs(self):
        self.assertEqual(run_frames(self, self._out(0), 1), ["a", "b"])

    @needs_cc
    def test_reached_stops(self):
        out = self._out(1)
        with open(os.path.join(out, "h.c"), "w") as f:
            f.write('#include "engine_draw.h"\nextern float Time_deltaTime;\n'
                    "int main(int c, char **v) { engine_apply_argv(c, v); Time_deltaTime = 1.f / 60.f;"
                    " engine_tick(); return 0; }\n")
        exe = os.path.join(out, "h")
        r = subprocess.run([_CC, "-O1", "-w", "-I", out, "-o", exe, os.path.join(out, "h.c"),
                            os.path.join(out, "engine.c"), os.path.join(out, "data.c"), "-lm"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        run = subprocess.run([exe, "-logFile", "-"], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 70)
        self.assertIn("`Heart` has no packed script to clone", run.stderr)


class TestGetterTransformEuler(unittest.TestCase):
    """Slime Jump's Player.Respawn: `fw.trs.eulerAngles = Vector3.forward *
    fw.initRot;` through another object's Transform field."""

    @needs_cc
    def test_euler_z_set(self):
        o = ("using UnityEngine;\npublic class O : MonoBehaviour {\n"
             "    public Transform trs;\n    public float initRot;\n}\n")
        src = script("P", "transform.SetParent(null); O o = ot; o.trs.eulerAngles = Vector3.forward * o.initRot; "
                     "o.trs.position = new Vector2(1f, 2f); "
                     "Quaternion q = o.trs.rotation; Debug.Log(\"q \" + q.z.ToString(\"F3\") + \" \" + q.w.ToString(\"F3\"));",
                     "    public O ot;")
        out = run_frames(self, pack(self, project(self, {"P": src, "O": o}, [
            ("P", None, "  ot: {fileID: 112}\n"), ("O", None, "  trs: {fileID: 111}\n  initRot: 90\n")])), 1)
        self.assertEqual(out, ["q 0.707 0.707"])


class TestShallowUnstoredField(unittest.TestCase):
    """Slime Jump's Player.Respawn: `fw.currWaypointTrs = fw.waypoints[k].trs;`
    on a FollowWaypoints, a class crust packs no methods of (only on a prefab)
    whose `List<Waypoint>` it does not store. The statement stops the player
    if it runs; the method around it lowers."""

    W = ("using UnityEngine;\nusing System.Collections.Generic;\n"
         "public class W : MonoBehaviour {\n"
         "    [System.Serializable] public struct Wp { public Transform trs; }\n"
         "    public List<Wp> pts = new List<Wp>();\n    public Transform cur;\n"
         "    void Awake() { cur = pts[0].trs; }\n}\n")

    @needs_cc
    def test_statement_traps_method_lowers(self):
        src = script("P", "W w = ws; if (n > 0) { w.cur = w.pts[0].trs; } Debug.Log(\"ok\");",
                     "    public W ws;\n    public int n;")
        root = project(self, {"P": src, "W": self.W}, [("P", None, "  n: 0\n")])
        pd = os.path.join(root, "Assets", "Prefabs")
        os.makedirs(pd)
        with open(os.path.join(pd, "Wp.prefab"), "w") as f:
            f.write("%%YAML 1.1\n--- !u!1 &1\nGameObject:\n  m_Name: Wp\n  m_Component:\n"
                    "  - component: {fileID: 2}\n  - component: {fileID: 3}\n"
                    "--- !u!4 &2\nTransform:\n  m_GameObject: {fileID: 1}\n"
                    "--- !u!114 &3\nMonoBehaviour:\n  m_GameObject: {fileID: 1}\n"
                    "  m_Script: {fileID: 11500000, guid: %032x}\n" % 2)
        with open(os.path.join(pd, "Wp.prefab.meta"), "w") as f:
            f.write("guid: %032x\n" % 99)
        self.assertEqual(run_frames(self, pack(self, root, strict=False), 1), ["ok"])


class TestInstantiateFieldParent(unittest.TestCase):
    """Slime Jump's Player.Death: `Explodable c = Instantiate(explodable, null);`
    -- a scene object's component field, cloned with a parent argument."""

    @needs_cc
    def test_clone_of_a_component_field(self):
        boom = ("using UnityEngine;\npublic class Boom : MonoBehaviour {\n"
                "    public float v;\n}\n")
        src = script("P", "Boom c = Instantiate(b, null); c.transform.position = new Vector3(5f, 2f, 0f); "
                     "c.v = 3f; Debug.Log(\"c \" + c.transform.position.x + \" \" + c.v + \" b \" + b.v"
                     " + \" \" + b.transform.position.x);", "    public Boom b;")
        out = run_frames(self, pack(self, project(self, {"P": src, "Boom": boom}, [
            ("P", None, "  b: {fileID: 112}\n"), ("Boom", None, "  v: 1\n")])), 1)
        self.assertEqual(out, ["c 5 3 b 1 1"])

    @needs_cc
    def test_clone_placed_at_a_transform_field(self):
        boom = ("using UnityEngine;\npublic class Boom : MonoBehaviour {\n}\n")
        src = script("P", "trs.position = new Vector3(4f, 6f, 0f); Boom c = Instantiate(b, null); "
                     "c.transform.position = trs.position; "
                     "Debug.Log(\"c \" + c.transform.position.x + \" \" + c.transform.position.y);",
                     "    public Boom b;\n    public Transform trs;")
        out = run_frames(self, pack(self, project(self, {"P": src, "Boom": boom}, [
            ("P", None, "  b: {fileID: 112}\n  trs: {fileID: 101}\n"), ("Boom", None, "")])), 1)
        self.assertEqual(out, ["c 4 6"])

    @needs_cc
    def test_list_field_of_the_clone_to_array(self):
        boom = ("using UnityEngine;\nusing System.Collections.Generic;\n"
                "public class Boom : MonoBehaviour {\n"
                "    public List<GameObject> frags = new List<GameObject>();\n"
                "    public void Go() { frags.Add(gameObject); frags.Add(gameObject); }\n}\n")
        src = script("P", "Boom c = Instantiate(b, null); c.Go(); fs = c.frags.ToArray(); "
                     "Debug.Log(\"n \" + fs.Length + \" \" + b.frags.Count + \" \" + (fs[1] == c.gameObject));",
                     "    public Boom b;\n    GameObject[] fs = new GameObject[0];")
        out = run_frames(self, pack(self, project(self, {"P": src, "Boom": boom}, [
            ("P", None, "  b: {fileID: 112}\n"), ("Boom", None, "")])), 1)
        self.assertEqual(out, ["n 2 0 True"])


class TestTmpTextSet(unittest.TestCase):
    """Slime Jump's Sign: `label.text = msg;` with *msg* an authored string
    (a quoted YAML scalar Unity wrapped over lines) on a TMP authored
    empty. The text is baked at pack time (newlines, word wrap, auto
    size) and swapped in; null empties it; a text nobody baked stops."""

    def test_empty_text_keeps_canvas_layer(self):
        # Tips Text under Tips Panel: drawn on the canvas's sorting layer
        from tools.unity.unity_pack_sprites import _apply_sprite_sorting
        es = {"sorting_layer_id": 77, "sorting_order": 1}
        _apply_sprite_sorting([{"ui_tmp": {"empty_sprite": es}}],
                              [{"unique_id": 0}, {"unique_id": 77}])
        self.assertEqual(es["sorting_layer"], 1)

    FONT = os.path.join(ROOT, "examples", "unity_pack", "Slime Jump", "Assets",
                        "Others", "Fonts", "Montserrat-Regular SDF.asset")
    SIGN = """using UnityEngine;
using TMPro;
public class Sign : MonoBehaviour {
    public string msg;
    public TMP_Text label;
    int f;
    void Update () {
        f++;
        if (f == 1) { label.text = msg; Debug.Log("set " + msg); }
        if (f == 2) label.text = null;
        if (f == 4) { msg = "zz"; label.text = msg; }
    }
}
"""

    def setUp(self):
        if not os.path.isfile(self.FONT):
            self.skipTest("Montserrat-Regular SDF font missing")

    def test_rasterize_lines(self):
        font = unity_pack._load_tmp_font_asset(self.FONT)

        def bands(w, h, rgba):
            ink = [any(rgba[(y * w + x) * 4 + 3] for x in range(w))
                   for y in range(h)]
            return sum(1 for y in range(h) if ink[y] and (y == 0 or not ink[y - 1]))
        w, h, rgba, _sx, _sy = unity_pack._rasterize_tmp_text(
            font, "Hold A\nHold W", 1000, (1, 1, 1, 1), 300, 200, 2, 512, 0,
            wrap=True, auto_size=(0, 1000))
        # auto size fits the box (ink may overhang the advance a pixel)
        self.assertLessEqual(max(abs(w - 300), abs(h - 200)), 2)
        self.assertEqual(bands(w, h, rgba), 2)
        w, h, rgba, _sx, _sy = unity_pack._rasterize_tmp_text(
            font, "Hold Hold", 40, (1, 1, 1, 1), 120, 120, 1, 256, 0, wrap=True)
        self.assertEqual(bands(w, h, rgba), 2)  # wrapped at the space

    def _scene(self, sign_guid, font_guid):
        tmp = "f4688fdb7df04437aeb418b961361dc5"
        return (
            "%YAML 1.1\n"
            "--- !u!1 &1\nGameObject:\n  m_Name: Main Camera\n"
            "  m_TagString: MainCamera\n  m_IsActive: 1\n"
            "  m_Component:\n  - component: {fileID: 2}\n"
            "  - component: {fileID: 3}\n"
            "--- !u!4 &2\nTransform:\n  m_GameObject: {fileID: 1}\n"
            "  m_Father: {fileID: 0}\n"
            "  m_LocalPosition: {x: 0, y: 0, z: -10}\n"
            "--- !u!20 &3\nCamera:\n  m_GameObject: {fileID: 1}\n"
            "  orthographic: 1\n  orthographic size: 5\n"
            "--- !u!1 &10\nGameObject:\n  m_Name: Canvas\n  m_IsActive: 1\n"
            "  m_Component:\n  - component: {fileID: 11}\n"
            "  - component: {fileID: 12}\n"
            "--- !u!224 &11\nRectTransform:\n"
            "  m_GameObject: {fileID: 10}\n  m_Father: {fileID: 0}\n"
            "  m_AnchorMin: {x: 0, y: 0}\n  m_AnchorMax: {x: 1, y: 1}\n"
            "  m_SizeDelta: {x: 0, y: 0}\n  m_Pivot: {x: 0.5, y: 0.5}\n"
            "--- !u!223 &12\nCanvas:\n  m_GameObject: {fileID: 10}\n"
            "  m_Enabled: 1\n  m_RenderMode: 0\n"
            "--- !u!1 &20\nGameObject:\n  m_Name: Tip\n  m_IsActive: 1\n"
            "  m_Component:\n  - component: {fileID: 21}\n"
            "  - component: {fileID: 22}\n"
            "--- !u!224 &21\nRectTransform:\n"
            "  m_GameObject: {fileID: 20}\n  m_Father: {fileID: 11}\n"
            "  m_AnchorMin: {x: 0.5, y: 0.5}\n  m_AnchorMax: {x: 0.5, y: 0.5}\n"
            "  m_AnchoredPosition: {x: 0, y: 0}\n"
            "  m_SizeDelta: {x: 300, y: 200}\n  m_Pivot: {x: 0.5, y: 0.5}\n"
            "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
            "--- !u!114 &22\nMonoBehaviour:\n"
            "  m_GameObject: {fileID: 20}\n  m_Enabled: 1\n"
            "  m_Script: {fileID: 11500000, guid: " + tmp + "}\n"
            "  m_text: \n"
            "  m_fontAsset: {fileID: 11400000, guid: " + font_guid
            + ", type: 2}\n"
            "  m_fontSize: 36\n  m_enableAutoSizing: 1\n"
            "  m_fontSizeMin: 0\n  m_fontSizeMax: 32767\n"
            "  m_fontColor: {r: 1, g: 1, b: 1, a: 1}\n"
            "  m_HorizontalAlignment: 2\n  m_VerticalAlignment: 512\n"
            "  m_TextWrappingMode: 1\n  m_overflowMode: 0\n"
            "--- !u!1 &30\nGameObject:\n  m_Name: Sign\n  m_IsActive: 1\n"
            "  m_Component:\n  - component: {fileID: 31}\n"
            "  - component: {fileID: 32}\n"
            "--- !u!4 &31\nTransform:\n  m_GameObject: {fileID: 30}\n"
            "  m_Father: {fileID: 0}\n"
            "--- !u!114 &32\nMonoBehaviour:\n"
            "  m_GameObject: {fileID: 30}\n  m_Enabled: 1\n"
            "  m_Script: {fileID: 11500000, guid: " + sign_guid + "}\n"
            "  msg: 'Hold A and D to move.\n\n    Hold W to jump.'\n"
            "  label: {fileID: 22}\n")

    @needs_cc
    def test_sign(self):
        root = tempfile.mkdtemp(prefix="upf-")
        self.addCleanup(shutil.rmtree, root, True)
        sd = os.path.join(root, "Assets", "Scripts")
        fd = os.path.join(root, "Assets", "Fonts")
        os.makedirs(sd)
        os.makedirs(fd)
        os.makedirs(os.path.join(root, "Assets", "Scenes"))
        sign_guid, font_guid = "%032x" % 1, "%032x" % 2
        shutil.copy(self.FONT, os.path.join(fd, "Font.asset"))
        # a Dynamic font Unity saved with its glyphs cleared draws from this
        ttf = self.FONT.replace(" SDF.asset", ".ttf")
        for ext in ("", ".meta"):
            shutil.copy(ttf + ext, os.path.join(fd, "Font.ttf" + ext))
        for path, guid, src in (("Scripts/Sign.cs", sign_guid, self.SIGN),
                                ("Fonts/Font.asset", font_guid, None)):
            if src:
                with open(os.path.join(root, "Assets", path), "w") as f:
                    f.write(src)
            with open(os.path.join(root, "Assets", path + ".meta"), "w") as f:
                f.write("guid: %s\n" % guid)
        with open(os.path.join(root, "Assets", "Scenes", "S.unity"), "w") as f:
            f.write(self._scene(sign_guid, font_guid))
        out = pack(self, root)
        draws = ("EngineDraw b[16]; int n = engine_collect_draws(b, 16), k;"
                 " for (k = 0; k < n; k++) printf(\"d %d\\n\", b[k].tex);")
        d = [[l for l in run_frames(self, out, f, draws) if l[:2] == "d "]
             for f in (0, 1, 2)]
        self.assertEqual(len(d[0]), 1)
        self.assertNotEqual(d[1], d[0])         # the text's bake
        self.assertEqual(d[2], d[0])            # null: empty again
        log = run_frames(self, out, 1)
        k = log.index("set Hold A and D to move.")
        self.assertEqual(log[k + 1], "Hold W to jump.")
        with self.assertRaises(AssertionError) as cm:
            run_frames(self, out, 4)
        self.assertIn("Sign.cs:11: `TMP_Text.text` is not lowered",
                      str(cm.exception))


class TestTmpTextLive(TestTmpTextSet):
    """Player.Death's `GameManager.instance.deathCounterText.text = "" +
    DeathCount;`: a text no pack can bake draws a quad per glyph over the
    TMP's rect; one it cannot lay out (a newline) stops."""
    SIGN = """using UnityEngine;
using TMPro;
public class Sign : MonoBehaviour {
    public static Sign instance;
    public TMP_Text label;
    static int n;
    int f;
    void Awake () { instance = this; }
    void Update () {
        f++;
        n += 1234567;
        if (f == 2) Sign.instance.label.text = "" + n;
        if (f == 4) label.text = "a\\n" + n;
    }
}
"""

    def test_rasterize_lines(self):
        pass

    def test_empty_text_keeps_canvas_layer(self):
        pass

    @needs_cc
    def test_sign(self):
        root = tempfile.mkdtemp(prefix="upf-")
        self.addCleanup(shutil.rmtree, root, True)
        for d in ("Scripts", "Fonts", "Scenes"):
            os.makedirs(os.path.join(root, "Assets", d))
        sign_guid, font_guid = "%032x" % 1, "%032x" % 2
        shutil.copy(self.FONT, os.path.join(root, "Assets", "Fonts", "Font.asset"))
        ttf = self.FONT.replace(" SDF.asset", ".ttf")
        for ext in ("", ".meta"):
            shutil.copy(ttf + ext, os.path.join(root, "Assets", "Fonts",
                                                "Font.ttf" + ext))
        for path, guid, src in (("Scripts/Sign.cs", sign_guid, self.SIGN),
                                ("Fonts/Font.asset", font_guid, None)):
            if src:
                with open(os.path.join(root, "Assets", path), "w") as f:
                    f.write(src)
            with open(os.path.join(root, "Assets", path + ".meta"), "w") as f:
                f.write("guid: %s\n" % guid)
        with open(os.path.join(root, "Assets", "Scenes", "S.unity"), "w") as f:
            f.write(self._scene(sign_guid, font_guid))
        out = pack(self, root)
        draws = ("EngineDraw b[16]; int n = engine_collect_draws(b, 16), k;"
                 " for (k = 0; k < n; k++) printf(\"d %d %.3f %.3f\\n\","
                 " b[k].tex, b[k].x, b[k].half_w);")
        d = [[l.split()[1:] for l in run_frames(self, out, f, draws)
              if l[:2] == "d "] for f in (0, 2)]
        self.assertEqual(len(d[0]), 1)
        base, *gs = d[1]                        # "2469134": a quad per digit
        self.assertEqual(base[0], d[0][0][0])   # the rect, drawn empty
        self.assertEqual([g[0] for g in gs].count(gs[0][0]), 1)  # "2" once
        self.assertEqual(len(gs), 7)
        xs = [float(g[1]) for g in gs]
        self.assertEqual(xs, sorted(xs))
        self.assertLess(abs((xs[0] + xs[-1]) / 2 - float(base[1])),
                        float(gs[0][2]))        # centered
        with self.assertRaises(AssertionError) as cm:
            run_frames(self, out, 4)
        self.assertIn("Sign.cs:13: `TMP_Text.text` is not lowered",
                      str(cm.exception))


if __name__ == "__main__":
    unittest.main()
