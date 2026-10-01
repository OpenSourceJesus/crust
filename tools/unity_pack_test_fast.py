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

    python3 tools/unity_pack_test_fast.py            # everything
    python3 tools/unity_pack_test_fast.py TestLineRenderer
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import tools.unity_pack as unity_pack  # noqa: E402

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
        # a C# string is a coost fastring, C++ only cpprust lowers to C
        if "fastring" in text:
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


def run_frames(test, out, frames=1, body=""):
    """Run the packed engine *frames* ticks at 60 fps (then *body*, C over
    engine_draw.h) and return its stdout lines (Debug.Log included)."""
    src = os.path.join(out, "h.c")
    with open(src, "w") as f:
        f.write('#include <stdio.h>\n#include "engine_draw.h"\n'
                "extern float Time_deltaTime;\n"
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
        import tools.unity_pack_curves as uc
        ease = {"keys": [(0, 0, 0, 0), (1, 1, 0, 0)], "pre": 8, "post": 8}
        lin = {"keys": [(0, 0, 1, 1), (1, 1, 1, 1)], "pre": 8, "post": 8}
        # AnimationCurve.EaseInOut is smoothstep; .Linear is the line
        self.assertAlmostEqual(uc.evaluate(ease, 0.25), 0.15625)
        self.assertAlmostEqual(uc.evaluate(lin, 0.5), 0.5)
        self.assertAlmostEqual(uc.evaluate(dict(lin, post=2), 1.25), 0.25)
        self.assertAlmostEqual(uc.evaluate(dict(lin, post=4), 1.25), 0.75)

    @needs_cc
    def test_engine_evaluates_as_the_reference(self):
        import tools.unity_pack_curves as uc
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

    def _project(self, script, world=1, rot_z=0.0):
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

    @needs_cc
    def test_scripted_line_draws_as_unity_evaluates_it(self):
        import math
        import tools.unity_pack_curves as uc
        import tools.unity_pack_lines as ul
        out = tempfile.mkdtemp(prefix="upack-lr-out-")
        self.addCleanup(shutil.rmtree, out, True)
        unity_pack.pack(self._project(self.SCRIPT), out, force=True, strict=True)
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
        lines = run.stdout.splitlines()
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

    def test_gradient_reference(self):
        import tools.unity_pack_lines as ul
        g = {"mode": 0, "colors": [(0.0, 1.0, 1.0, 1.0), (1.0, 1.0, 0.0, 0.0)],
             "alphas": [(0.0, 1.0), (1.0, 0.0)]}
        self.assertEqual(ul.gradient_eval(g, 0.25), (1.0, 0.75, 0.75, 0.75))
        fixed = dict(g, mode=1)                # the first key at or after t
        self.assertEqual(ul.gradient_eval(fixed, 0.25), (1.0, 0.0, 0.0, 0.0))

    def test_local_space_on_a_rotated_object_is_refused(self):
        with self.assertRaises(unity_pack.PackError) as cm:
            unity_pack.pack(self._project(self.SCRIPT, world=0, rot_z=0.3),
                            tempfile.mkdtemp(prefix="upack-lr-out-"), force=True)
        self.assertIn("local space", cm.exception.message)


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
        "Unity-delaunay/Delaunay/ICoord.cs",
        "Unity-delaunay/Delaunay/LR.cs",
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


if __name__ == "__main__":
    unittest.main()
