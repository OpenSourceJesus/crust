#!/usr/bin/env python3
"""Generate a Unity project for the GPU-hierarchy benchmark: a cave.

    python3 tools/gen_cave_scene.py [/tmp/cave] [--worms N] [--segments N]
                                    [--bats N] [--holes N]

The scene is built to stress what GAME_LIBRARIES.md's "Whole-program
optimisation" is about -- long chains of parent / child transforms, animated
by authored curves, that no script reads:

* **worms**: a root and a chain of segments `S1/S2/../Sn`, each the child of
  the one before (depth n), spheres tapering along the body. A legacy
  Animation clip on the root turns every segment by a sine wave travelling down
  the body (euler z curves, one a segment, addressed by its child path).
* **bats**: a body with two wing children, flapping by a clip.
* **water**: a ParticleSystem under each hole in the ceiling, falling into a
  pool lit by blue Light2Ds.
* **fire**: red and yellow Light2Ds clustered at a big hole top left.
* **rock**: the cave's walls, static sprites.

Each worm and bat root carries `GpuHierarchy`, an empty MonoBehaviour: the
user's mark that the tree under it may have its animation and transforms
computed on the GPU (no script reads them). It is an ordinary component, so
the project opens in Unity as it is.

Everything is plain Unity YAML (2019+ format) and PNGs written here, with no
dependency outside the standard library.
"""
import argparse
import math
import os
import shutil
import struct
import zlib


# ---- PNG -------------------------------------------------------------------

def write_png(path, w, h, pixel):
    """An 8-bit RGBA PNG; `pixel(x, y)` -> (r, g, b, a)."""
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            raw.extend(pixel(x, y))

    def chunk(kind, data):
        c = struct.pack(">I", len(data)) + kind + data
        return c + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        f.write(chunk(b"IEND", b""))


def circle(x, y, n=64):
    """A white disc with a soft edge."""
    d = math.hypot(x + 0.5 - n / 2, y + 0.5 - n / 2) / (n / 2)
    a = max(0.0, min(1.0, (1.0 - d) * 6))
    return (255, 255, 255, int(a * 255))


# ---- ids ---------------------------------------------------------------------

class Ids(object):
    def __init__(self):
        self.n = 1000

    def __call__(self):
        self.n += 1
        return self.n


def guid(name):
    """A stable 32-hex guid for an asset name."""
    import hashlib
    return hashlib.md5(("cave:" + name).encode()).hexdigest()


# ---- assets ---------------------------------------------------------------------

def sprite_meta(g):
    return ("fileFormatVersion: 2\nguid: %s\nTextureImporter:\n  serializedVersion: 11\n"
            "  textureType: 8\n  spriteMode: 1\n  spritePixelsToUnits: 64\n"
            "  spritePivot: {x: 0.5, y: 0.5}\n  alphaIsTransparency: 1\n" % g)


def script_meta(g):
    return ("fileFormatVersion: 2\nguid: %s\nMonoImporter:\n  serializedVersion: 2\n"
            "  defaultReferences: []\n  executionOrder: 0\n" % g)


def anim_clip(name, tracks, length, wrap_loop=True):
    """A legacy AnimationClip with euler curves: `tracks` is [(path,
    [(time, z_degrees)])]."""
    out = ["%YAML 1.1", "%TAG !u! tag:unity3d.com,2011:", "--- !u!74 &7400000",
           "AnimationClip:", "  m_ObjectHideFlags: 0", "  m_Name: %s" % name,
           "  serializedVersion: 6", "  m_Legacy: 1", "  m_Compressed: 0",
           "  m_UseHighQualityCurve: 1", "  m_RotationCurves: []",
           "  m_CompressedRotationCurves: []", "  m_EulerCurves:"]
    for path, keys in tracks:
        out += ["  - curve:", "      serializedVersion: 2", "      m_Curve:"]
        for t, z in keys:
            out += ["      - serializedVersion: 3", "        time: %g" % t,
                    "        value: {x: 0, y: 0, z: %g}" % z,
                    "        inSlope: {x: 0, y: 0, z: 0}",
                    "        outSlope: {x: 0, y: 0, z: 0}", "        tangentMode: 0",
                    "        weightedMode: 0",
                    "        inWeight: {x: 0.33333334, y: 0.33333334, z: 0.33333334}",
                    "        outWeight: {x: 0.33333334, y: 0.33333334, z: 0.33333334}"]
        out += ["      m_PreInfinity: 2", "      m_PostInfinity: 2",
                "      m_RotationOrder: 4", "    path: %s" % path]
    out += ["  m_PositionCurves: []", "  m_ScaleCurves: []", "  m_FloatCurves: []",
            "  m_PPtrCurves: []", "  m_SampleRate: 60", "  m_WrapMode: %d" % (2 if wrap_loop else 0),
            "  m_AnimationClipSettings:", "    serializedVersion: 2",
            "    m_StartTime: 0", "    m_StopTime: %g" % length,
            "    m_LoopTime: %d" % (1 if wrap_loop else 0), "  m_Events: []"]
    return "\n".join(out) + "\n"


# ---- scene ----------------------------------------------------------------------

class Scene(object):
    def __init__(self):
        self.ids = Ids()
        self.parts = ["%YAML 1.1", "%TAG !u! tag:unity3d.com,2011:"]
        self.counts = {"objects": 0, "max_depth": 0, "lights": 0, "animated": 0,
                       "gpu_roots": 0}

    def obj(self, name, pos=(0, 0), scale=1.0, rot_z=0.0, parent=None, tag="Untagged", z=0.0):
        """A GameObject and its Transform; returns (go_id, xf_id, depth)."""
        go, xf = self.ids(), self.ids()
        depth = 0 if parent is None else parent[2] + 1
        self.counts["objects"] += 1
        self.counts["max_depth"] = max(self.counts["max_depth"], depth)
        q = (0.0, 0.0, math.sin(math.radians(rot_z) / 2), math.cos(math.radians(rot_z) / 2))
        self._go = {"go": go, "xf": xf, "name": name, "tag": tag, "comps": [xf],
                    "pos": pos, "scale": scale, "q": q, "z": z,
                    "father": parent[1] if parent else 0, "children": []}
        rec = self._go
        self._open = getattr(self, "_open", {})
        self._open[go] = rec
        if parent:
            self._open[parent[0]]["children"].append(xf)
        return (go, xf, depth)

    def component(self, owner, text_fn):
        cid = self.ids()
        self._open[owner[0]]["comps"].append(cid)
        self.parts.append(text_fn(cid, owner[0]))
        return cid

    def sprite(self, owner, sprite_guid, color, order=0):
        r, g, b, a = color
        return self.component(owner, lambda cid, go: (
            "--- !u!212 &%d\nSpriteRenderer:\n  m_GameObject: {fileID: %d}\n  m_Enabled: 1\n"
            "  m_SortingLayerID: 0\n  m_SortingOrder: %d\n"
            "  m_Sprite: {fileID: 21300000, guid: %s, type: 3}\n"
            "  m_Color: {r: %g, g: %g, b: %g, a: %g}\n  m_FlipX: 0\n  m_FlipY: 0\n"
            "  m_DrawMode: 0\n" % (cid, go, order, sprite_guid, r, g, b, a)))

    def script(self, owner, script_guid, fields=""):
        return self.component(owner, lambda cid, go: (
            "--- !u!114 &%d\nMonoBehaviour:\n  m_GameObject: {fileID: %d}\n  m_Enabled: 1\n"
            "  m_Script: {fileID: 11500000, guid: %s, type: 3}\n%s"
            % (cid, go, script_guid, fields)))

    def light2d(self, owner, color, intensity, radius, inner=0.0):
        self.counts["lights"] += 1
        r, g, b = color
        return self.component(owner, lambda cid, go: (
            "--- !u!114 &%d\nMonoBehaviour:\n  m_GameObject: {fileID: %d}\n  m_Enabled: 1\n"
            "  m_Script: {fileID: 11500000, guid: 073797afb82c5a1438f328866b10b3f0, type: 3}\n"
            "  m_LightType: 3\n  m_Color: {r: %g, g: %g, b: %g, a: 1}\n  m_Intensity: %g\n"
            "  m_PointLightInnerRadius: %g\n  m_PointLightOuterRadius: %g\n"
            "  m_PointLightInnerAngle: 360\n  m_PointLightOuterAngle: 360\n"
            "  m_FalloffIntensity: 0.5\n  m_ApplyToSortingLayers: 00000000\n"
            % (cid, go, r, g, b, intensity, inner, radius)))

    def animation(self, owner, clip_guid):
        self.counts["animated"] += 1
        return self.component(owner, lambda cid, go: (
            "--- !u!111 &%d\nAnimation:\n  m_GameObject: {fileID: %d}\n  m_Enabled: 1\n"
            "  serializedVersion: 3\n  m_Animation: {fileID: 7400000, guid: %s, type: 2}\n"
            "  m_Animations:\n  - {fileID: 7400000, guid: %s, type: 2}\n"
            "  m_WrapMode: 2\n  m_PlayAutomatically: 1\n  m_AnimatePhysics: 0\n"
            "  m_CullingType: 0\n" % (cid, go, clip_guid, clip_guid)))

    def particles(self, owner, rate, speed, gravity):
        return self.component(owner, lambda cid, go: (
            "--- !u!198 &%d\nParticleSystem:\n  m_GameObject: {fileID: %d}\n"
            "  serializedVersion: 6\n  lengthInSec: 5\n  looping: 1\n  playOnAwake: 1\n"
            "  InitialModule:\n    enabled: 1\n    startLifetime:\n      scalar: 3\n"
            "    startSpeed:\n      scalar: %g\n    startSize:\n      scalar: 0.12\n"
            "    startColor:\n      maxColor: {r: 0.4, g: 0.7, b: 1, a: 0.9}\n"
            "    gravityModifier:\n      scalar: %g\n    maxNumParticles: 400\n"
            "  EmissionModule:\n    enabled: 1\n    rateOverTime:\n      scalar: %g\n"
            "  ShapeModule:\n    enabled: 1\n    type: 4\n    angle: 5\n    radius: 0.1\n"
            % (cid, go, speed, gravity, rate)))

    def finish(self):
        for rec in self._open.values():
            q = rec["q"]
            self.parts.append(
                "--- !u!1 &%d\nGameObject:\n  m_ObjectHideFlags: 0\n  serializedVersion: 6\n"
                "  m_Component:\n%s  m_Layer: 0\n  m_Name: %s\n  m_TagString: %s\n"
                "  m_IsActive: 1\n" % (rec["go"], "".join(
                    "  - component: {fileID: %d}\n" % c for c in rec["comps"]),
                    rec["name"], rec["tag"]))
            self.parts.append(
                "--- !u!4 &%d\nTransform:\n  m_GameObject: {fileID: %d}\n"
                "  m_LocalRotation: {x: %r, y: %r, z: %r, w: %r}\n"
                "  m_LocalPosition: {x: %r, y: %r, z: %r}\n"
                "  m_LocalScale: {x: %r, y: %r, z: 1}\n  m_Children:\n%s"
                "  m_Father: {fileID: %d}\n"
                % (rec["xf"], rec["go"], q[0], q[1], q[2], q[3], rec["pos"][0], rec["pos"][1], rec["z"],
                   rec["scale"], rec["scale"],
                   "".join("  - {fileID: %d}\n" % c for c in rec["children"]) or "  []\n",
                   rec["father"]))
        return "\n".join(self.parts) + "\n"


# ---- the cave -------------------------------------------------------------------

def build(out, worms=12, segments=40, bats=20, holes=5, clip_variants=3):
    if os.path.isdir(out):
        shutil.rmtree(out)
    a = os.path.join(out, "Assets")
    for d in ("Sprites", "Scripts", "Animations", "Scenes"):
        os.makedirs(os.path.join(a, d))
    # sprites
    sg = {}
    for name, fn in (("Ball", lambda x, y: circle(x, y)),
                     ("Square", lambda x, y: (255, 255, 255, 255))):
        p = os.path.join(a, "Sprites", name + ".png")
        write_png(p, 64, 64, fn)
        sg[name] = guid(name)
        with open(p + ".meta", "w") as f:
            f.write(sprite_meta(sg[name]))
    # the user's GPU mark
    gpu_guid = guid("GpuHierarchy")
    with open(os.path.join(a, "Scripts", "GpuHierarchy.cs"), "w") as f:
        f.write("using UnityEngine;\n\n"
                "/// The tree under this object may have its animation and transforms\n"
                "/// computed on the GPU: no script reads them (crust's\n"
                "/// GAME_LIBRARIES.md, \"Whole-program optimisation\").\n"
                "public class GpuHierarchy : MonoBehaviour { }\n")
    with open(os.path.join(a, "Scripts", "GpuHierarchy.cs.meta"), "w") as f:
        f.write(script_meta(gpu_guid))
    # clips: a travelling sine wave down a worm; a bat's flap
    seg_paths = ["/".join("S%d" % (k + 1) for k in range(i + 1)) for i in range(segments)]
    worm_clips = []
    for v in range(clip_variants):
        period = 1.6 + 0.4 * v
        tracks = []
        for i, path in enumerate(seg_paths):
            amp = 10.0 + 6.0 * (i / max(1, segments - 1))
            ph = i * 0.45 + v
            keys = [(period * k / 8.0, amp * math.sin(2 * math.pi * k / 8.0 + ph))
                    for k in range(9)]
            tracks.append((path, keys))
        name = "Wriggle%d" % v
        g = guid(name)
        with open(os.path.join(a, "Animations", name + ".anim"), "w") as f:
            f.write(anim_clip(name, tracks, period))
        with open(os.path.join(a, "Animations", name + ".anim.meta"), "w") as f:
            f.write("fileFormatVersion: 2\nguid: %s\n" % g)
        worm_clips.append(g)
    flap_tracks = [("WingL", [(t * 0.1, 40 * math.sin(2 * math.pi * t / 4)) for t in range(5)]),
                   ("WingR", [(t * 0.1, -40 * math.sin(2 * math.pi * t / 4)) for t in range(5)])]
    flap = guid("Flap")
    with open(os.path.join(a, "Animations", "Flap.anim"), "w") as f:
        f.write(anim_clip("Flap", flap_tracks, 0.4))
    with open(os.path.join(a, "Animations", "Flap.anim.meta"), "w") as f:
        f.write("fileFormatVersion: 2\nguid: %s\n" % flap)

    s = Scene()
    W, H = 32.0, 18.0                     # the cave, in units
    # rock: the walls and floor, static
    for i in range(48):
        x = -W / 2 + (i % 24) * (W / 23)
        y = (-H / 2 if i < 24 else H / 2) + (0.6 if i < 24 else -0.6)
        r = s.obj("Rock%d" % i, (x, y), scale=2.2 + (i * 7 % 5) * 0.3)
        s.sprite(r, sg["Ball"], (0.18, 0.15, 0.13, 1), order=-5)
    # the pool, and its blue lights
    pool = s.obj("Pool", (4.0, -H / 2 + 1.2), scale=1.0)
    s.sprite(pool, sg["Square"], (0.1, 0.3, 0.8, 0.85), order=-2)
    for k in range(4):
        l = s.obj("PoolLight%d" % k, (-2.0 + 4.0 * k, -H / 2 + 2.0))
        s.light2d(l, (0.2, 0.45, 1.0), 1.4, 5.0, 0.5)
    # holes: water falling into the pool
    for k in range(holes):
        h = s.obj("Hole%d" % k, (-6.0 + k * 4.5, H / 2 - 1.2))
        s.sprite(h, sg["Ball"], (0.02, 0.02, 0.03, 1), order=-4)
        e = s.obj("Drip%d" % k, (0, -0.4), parent=h)
        s.particles(e, rate=30 + 10 * k, speed=0.5, gravity=1.0)
    # the big hole top left, and its fire
    big = s.obj("BigHole", (-W / 2 + 3.0, H / 2 - 2.5), scale=4.0)
    s.sprite(big, sg["Ball"], (0.02, 0.0, 0.0, 1), order=-4)
    for k in range(14):
        ang = k * 2 * math.pi / 14
        col = (1.0, 0.85, 0.2) if k % 2 else (1.0, 0.25, 0.1)
        l = s.obj("Fire%d" % k, (-W / 2 + 3.0 + 2.2 * math.cos(ang), H / 2 - 2.5 + 2.2 * math.sin(ang)))
        s.light2d(l, col, 1.2 + 0.1 * (k % 3), 3.5, 0.3)
    # worms: chains of spheres, segment i the child of segment i - 1
    for w in range(worms):
        x0 = -W / 2 + 3 + (w * 2.3) % (W - 6)
        y0 = -H / 2 + 3 + (w * 1.7) % (H - 7)
        root = s.obj("Worm%d" % w, (x0, y0), rot_z=(w * 37) % 360)
        s.script(root, gpu_guid)
        s.counts["gpu_roots"] += 1
        s.animation(root, worm_clips[w % len(worm_clips)])
        parent = root
        for i in range(segments):
            size = 0.55 - 0.3 * (i / max(1, segments - 1)) + 0.05 * math.sin(i * 1.3)
            seg = s.obj("S%d" % (i + 1), (0.32 if i else 0.0, 0.0), parent=parent)
            s._open[seg[0]]["scale"] = 1.0           # each a unit step along the parent
            dot = s.obj("Body", (0, 0), scale=size, parent=seg)
            s.sprite(dot, sg["Ball"],
                     (0.85 - 0.3 * (i % 2), 0.45 + 0.2 * (w % 3) / 2, 0.55, 1), order=2)
            parent = seg
    # bats: a body and two wings
    for b in range(bats):
        x = -W / 2 + 5 + (b * 3.1) % (W - 10)
        y = H / 2 - 3 - (b * 1.3) % 6
        body = s.obj("Bat%d" % b, (x, y), scale=0.6)
        s.script(body, gpu_guid)
        s.counts["gpu_roots"] += 1
        s.animation(body, flap)
        s.sprite(body, sg["Ball"], (0.12, 0.08, 0.15, 1), order=3)
        for side, dx in (("WingL", -0.5), ("WingR", 0.5)):
            wing = s.obj(side, (dx, 0.1), scale=0.9, parent=body)
            s.sprite(wing, sg["Square"], (0.15, 0.1, 0.18, 1), order=3)
    # a dim global light, and the camera
    # at z = -10, as Unity's 2D template: the sprites at z = 0 are in front
    # of its near plane (at z = 0 every one of them was culled)
    cam = s.obj("Main Camera", (0, 0), tag="MainCamera", z=-10.0)
    s.component(cam, lambda cid, go: (
        "--- !u!20 &%d\nCamera:\n  m_GameObject: {fileID: %d}\n  m_Enabled: 1\n"
        "  m_ClearFlags: 2\n  m_BackGroundColor: {r: 0.02, g: 0.02, b: 0.04, a: 1}\n"
        "  orthographic: 1\n  orthographic size: 9.5\n" % (cid, go)))
    with open(os.path.join(a, "Scenes", "Cave.unity"), "w") as f:
        f.write(s.finish())
    return s.counts


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", nargs="?", default="/tmp/cave")
    ap.add_argument("--worms", type=int, default=12)
    ap.add_argument("--segments", type=int, default=40)
    ap.add_argument("--bats", type=int, default=20)
    ap.add_argument("--holes", type=int, default=5)
    a = ap.parse_args()
    c = build(a.out, a.worms, a.segments, a.bats, a.holes)
    print("%s: %d objects, hierarchy depth %d, %d animated roots (%d marked "
          "GpuHierarchy), %d 2D lights" % (a.out, c["objects"], c["max_depth"],
                                           c["animated"], c["gpu_roots"], c["lights"]))


if __name__ == "__main__":
    main()
