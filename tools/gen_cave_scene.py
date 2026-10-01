#!/usr/bin/env python3
"""Generate the GPU-hierarchy benchmark -- a cave -- for Unity, Godot or Blender.

    python3 tools/gen_cave_scene.py [OUT] [--worms N] [--segments N]
                                    [--bats N] [--holes N]
                                    [--godot | --blender]

Unity (the default, OUT /tmp/cave): a Unity project. --godot (OUT
/tmp/godot_cave_scene): a Godot 4 project, the same cave. --blender (OUT
/tmp/blender_cave_scene): `build_cave.py`, a bpy script that builds the same
cave in Blender (`blender --background --python build_cave.py`), run here when
a `blender` binary is on the PATH to save `cave.blend`. One scene model
(`cave_model`) feeds all three, so they are the same cave: the same tree, the
same transforms, the same curves.

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
import re
import sys
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

    def obj(self, name, pos=(0, 0), scale=1.0, rot_z=0.0, parent=None, tag="Untagged", z=0.0,
            active=True):
        """A GameObject and its Transform; returns (go_id, xf_id, depth)."""
        go, xf = self.ids(), self.ids()
        depth = 0 if parent is None else parent[2] + 1
        self.counts["objects"] += 1
        self.counts["max_depth"] = max(self.counts["max_depth"], depth)
        q = (0.0, 0.0, math.sin(math.radians(rot_z) / 2), math.cos(math.radians(rot_z) / 2))
        self._go = {"go": go, "xf": xf, "name": name, "tag": tag, "comps": [xf],
                    "pos": pos, "scale": scale, "q": q, "z": z, "active": active,
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
                "  m_IsActive: %d\n" % (rec["go"], "".join(
                    "  - component: {fileID: %d}\n" % c for c in rec["comps"]),
                    rec["name"], rec["tag"], 1 if rec["active"] else 0))
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


# ---- the cave: one model ----------------------------------------------------

def cave_model(worms=12, segments=40, bats=20, holes=5, clip_variants=3,
               destructible=False):
    """The cave as data: `nodes` (each: id, name, parent id, local pos (x, y,
    y up, in units), z, rot (degrees, counter-clockwise), scale, and what it
    carries -- sprite, light, particles, animation clip, the GPU mark,
    camera) and `clips` (name -> length and tracks: child path ->
    [(time, z degrees)]). Every target is generated from it."""
    nodes, clips = [], {}

    def add(name, pos=(0.0, 0.0), parent=None, scale=1.0, rot=0.0, z=0.0, **kw):
        n = {"id": len(nodes), "name": name, "parent": parent, "pos": tuple(pos),
             "z": z, "rot": rot, "scale": scale, "sprite": None, "light": None,
             "particles": None, "anim": None, "gpu": False, "camera": False,
             "tag": "Untagged", "explodable": False, "quake": False,
             "active": True, "mesh": None, "fragments": []}
        n.update(kw)
        nodes.append(n)
        return n["id"]

    seg_paths = ["/".join("S%d" % (k + 1) for k in range(i + 1)) for i in range(segments)]
    for v in range(clip_variants):
        period = 1.6 + 0.4 * v
        tracks = []
        for i, path in enumerate(seg_paths):
            amp = 10.0 + 6.0 * (i / max(1, segments - 1))
            ph = i * 0.45 + v
            tracks.append((path, [(period * k / 8.0, amp * math.sin(2 * math.pi * k / 8.0 + ph))
                                  for k in range(9)]))
        clips["Wriggle%d" % v] = {"length": period, "tracks": tracks}
    clips["Flap"] = {"length": 0.4, "tracks": [
        ("WingL", [(t * 0.1, 40 * math.sin(2 * math.pi * t / 4)) for t in range(5)]),
        ("WingR", [(t * 0.1, -40 * math.sin(2 * math.pi * t / 4)) for t in range(5)])]}

    W, H = 32.0, 18.0
    for i in range(48):                                   # rock: the walls
        x = -W / 2 + (i % 24) * (W / 23)
        y = (-H / 2 if i < 24 else H / 2) + (0.6 if i < 24 else -0.6)
        add("Rock%d" % i, (x, y), scale=2.2 + (i * 7 % 5) * 0.3,
            sprite=("Ball", (0.18, 0.15, 0.13, 1), -5))
    add("Pool", (4.0, -H / 2 + 1.2), sprite=("Square", (0.1, 0.3, 0.8, 0.85), -2))
    for k in range(4):
        add("PoolLight%d" % k, (-2.0 + 4.0 * k, -H / 2 + 2.0),
            light=((0.2, 0.45, 1.0), 1.4, 5.0, 0.5))
    for k in range(holes):                                # holes, and water
        h = add("Hole%d" % k, (-6.0 + k * 4.5, H / 2 - 1.2),
                sprite=("Ball", (0.02, 0.02, 0.03, 1), -4))
        add("Drip%d" % k, (0.0, -0.4), parent=h, particles=(30 + 10 * k, 0.5, 1.0))
    big = add("BigHole", (-W / 2 + 3.0, H / 2 - 2.5), scale=4.0,
              sprite=("Ball", (0.02, 0.0, 0.0, 1), -4))
    for k in range(14):                                   # fire at the big hole
        ang = k * 2 * math.pi / 14
        col = (1.0, 0.85, 0.2) if k % 2 else (1.0, 0.25, 0.1)
        add("Fire%d" % k, (-W / 2 + 3.0 + 2.2 * math.cos(ang), H / 2 - 2.5 + 2.2 * math.sin(ang)),
            light=(col, 1.2 + 0.1 * (k % 3), 3.5, 0.3))
    del big
    for w in range(worms):                                # worms
        root = add("Worm%d" % w, (-W / 2 + 3 + (w * 2.3) % (W - 6), -H / 2 + 3 + (w * 1.7) % (H - 7)),
                   rot=(w * 37) % 360, anim="Wriggle%d" % (w % clip_variants), gpu=True)
        parent = root
        for i in range(segments):
            seg = add("S%d" % (i + 1), (0.32 if i else 0.0, 0.0), parent=parent)
            size = 0.55 - 0.3 * (i / max(1, segments - 1)) + 0.05 * math.sin(i * 1.3)
            add("Body", parent=seg, scale=size,
                sprite=("Ball", (0.85 - 0.3 * (i % 2), 0.45 + 0.2 * (w % 3) / 2, 0.55, 1), 2))
            parent = seg
    for b in range(bats):                                 # bats
        body = add("Bat%d" % b, (-W / 2 + 5 + (b * 3.1) % (W - 10), H / 2 - 3 - (b * 1.3) % 6),
                   scale=0.6, anim="Flap", gpu=True, sprite=("Ball", (0.12, 0.08, 0.15, 1), 3),
                   explodable=destructible)
        for side, dx in (("WingL", -0.5), ("WingR", 0.5)):
            add(side, (dx, 0.1), parent=body, scale=0.9, sprite=("Square", (0.15, 0.1, 0.18, 1), 3))
        if destructible:
            # pre-fractured, as Unity's editor fracture leaves it: inactive
            # children, one a Voronoi cell, that explode() frees
            nodes[body]["fragments"] = [
                add("Frag%d" % k, parent=body, active=False, mesh=cell)
                for k, cell in enumerate(voronoi_cells(7, seed=b))]
    add("Main Camera", (0.0, 0.0), z=-10.0, camera=True, tag="MainCamera")
    return nodes, clips


def _clip(poly, a, b, c):
    """`poly` cut to the half-plane a x + b y <= c (Sutherland-Hodgman)."""
    out = []
    for i, p in enumerate(poly):
        q = poly[(i + 1) % len(poly)]
        dp, dq = a * p[0] + b * p[1] - c, a * q[0] + b * q[1] - c
        if dp <= 0:
            out.append(p)
        if (dp < 0) != (dq < 0) and dp != dq:
            t = dp / (dp - dq)
            out.append((p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])))
    return out


def voronoi_cells(n, seed=0):
    """The Voronoi cells of n seeds in the unit square about the origin, each
    a convex polygon (the bisector of every other seed, cut in turn)."""
    seeds = [(((seed * 7919 + k * 104729) % 997) / 997.0 - 0.5,
              ((seed * 6151 + k * 130363) % 991) / 991.0 - 0.5) for k in range(n)]
    cells = []
    for i, si in enumerate(seeds):
        poly = [(-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)]
        for j, sj in enumerate(seeds):
            if i != j and poly:
                a, b = sj[0] - si[0], sj[1] - si[1]
                c = (sj[0] ** 2 + sj[1] ** 2 - si[0] ** 2 - si[1] ** 2) / 2.0
                poly = _clip(poly, a, b, c)
        if len(poly) >= 3:
            cells.append(poly)
    return cells


def model_stats(nodes):
    depth = {}
    explodable = sum(1 for n in nodes if n.get("explodable"))
    for n in nodes:
        depth[n["id"]] = 0 if n["parent"] is None else depth[n["parent"]] + 1
    return {"objects": len(nodes), "max_depth": max(depth.values()),
            "animated": sum(1 for n in nodes if n["anim"]),
            "gpu_roots": sum(1 for n in nodes if n["gpu"]),
            "lights": sum(1 for n in nodes if n["light"]), "explodable": explodable}


def resolve_path(nodes, root_id, path):
    """The node a clip's child path names under `root_id`."""
    cur = root_id
    for part in path.split("/"):
        nxt = [n["id"] for n in nodes if n["parent"] == cur and n["name"] == part]
        if not nxt:
            return None
        cur = nxt[0]
    return cur


def write_sprites(dirname):
    os.makedirs(dirname, exist_ok=True)
    for name, fn in (("Ball", lambda x, y: circle(x, y)),
                     ("Square", lambda x, y: (255, 255, 255, 255))):
        write_png(os.path.join(dirname, name + ".png"), 64, 64, fn)


GPU_NOTE = ("The tree under this object may have its animation and transforms\n"
            "/// computed on the GPU: no script reads them (crust's\n"
            "/// GAME_LIBRARIES.md, \"Whole-program optimisation\").")


# ---- Unity ----------------------------------------------------------------------

def destruction_fork():
    """Unity-2D-Destruction's 2D_Destruction folder: UNITY_2D_DESTRUCTION, or
    the fork cloned beside crust (as tools/unity_pack_test_fast.py finds it)."""
    root = os.environ.get("UNITY_2D_DESTRUCTION") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "Unity-2D-Destruction")
    d = os.path.join(root, "unity2DDestruction", "Assets", "2D_Destruction")
    return d if os.path.isdir(d) else None


def _meta_guid(path):
    with open(path + ".meta") as f:
        return re.search(r"(?m)^guid:\s*(\w+)", f.read()).group(1)


QUAKE_CS = """using UnityEngine;

/// Explodes its own bat after `delay` seconds: each bat is a GpuHierarchy
/// tree, handed back to the CPU by Explodable.explode before it shatters.
/// (One a bat, delays 0.5 s apart, rather than one script holding an array
/// of Explodables: a call on an array element of component references is
/// not packed yet.)
public class TimedExplode : MonoBehaviour
{
    public float delay = 0.5f;
    float t;
    bool done;

    void Update()
    {
        if (done) return;
        t += Time.deltaTime;
        if (t >= delay)
        {
            done = true;
            GetComponent<Explodable>().explode();
        }
    }
}
"""


def build(out, worms=12, segments=40, bats=20, holes=5, clip_variants=3,
          destructible=False):
    """The Unity project (the model through `Scene`). With `destructible`,
    Unity-2D-Destruction's runtime library is copied in, the bats explode
    (Rigidbody2D, BoxCollider2D, Explodable), and `Quake` sets them off."""
    fork = destruction_fork() if destructible else None
    if destructible and fork is None:
        raise SystemExit("--destructible needs Unity-2D-Destruction: clone "
                         "https://github.com/crustos/Unity-2D-Destruction beside "
                         "crust, or set UNITY_2D_DESTRUCTION")
    nodes, clips = cave_model(worms, segments, bats, holes, clip_variants, destructible)
    if os.path.isdir(out):
        shutil.rmtree(out)
    a = os.path.join(out, "Assets")
    for d in ("Scripts", "Animations", "Scenes"):
        os.makedirs(os.path.join(a, d))
    write_sprites(os.path.join(a, "Sprites"))
    sg = {}
    for name in ("Ball", "Square"):
        sg[name] = guid(name)
        with open(os.path.join(a, "Sprites", name + ".png.meta"), "w") as f:
            f.write(sprite_meta(sg[name]))
    if fork:
        # the library's runtime scripts, with their metas (guids); its own
        # GpuHierarchy -- the one Explodable.explode releases
        dst = os.path.join(a, "2D_Destruction")
        for sub in ("Scripts", "Unity-delaunay", "clipper_library"):
            shutil.copytree(os.path.join(fork, sub), os.path.join(dst, sub),
                            ignore=shutil.ignore_patterns("Editor", "Editor.meta"))
        gpu_guid = _meta_guid(os.path.join(dst, "Scripts", "GpuHierarchy.cs"))
        expl_guid = _meta_guid(os.path.join(dst, "Scripts", "Explodable.cs"))
        quake_guid = guid("TimedExplode")
        with open(os.path.join(a, "Scripts", "TimedExplode.cs"), "w") as f:
            f.write(QUAKE_CS)
        with open(os.path.join(a, "Scripts", "TimedExplode.cs.meta"), "w") as f:
            f.write(script_meta(quake_guid))
    else:
        gpu_guid = guid("GpuHierarchy")
        with open(os.path.join(a, "Scripts", "GpuHierarchy.cs"), "w") as f:
            f.write("using UnityEngine;\n\n/// %s\npublic class GpuHierarchy : MonoBehaviour { }\n"
                    % GPU_NOTE)
        with open(os.path.join(a, "Scripts", "GpuHierarchy.cs.meta"), "w") as f:
            f.write(script_meta(gpu_guid))
    clip_guid = {}
    for name, c in clips.items():
        clip_guid[name] = guid(name)
        with open(os.path.join(a, "Animations", name + ".anim"), "w") as f:
            f.write(anim_clip(name, c["tracks"], c["length"]))
        with open(os.path.join(a, "Animations", name + ".anim.meta"), "w") as f:
            f.write("fileFormatVersion: 2\nguid: %s\n" % clip_guid[name])
    s = Scene()
    h = {}
    explodables = []
    mat_guid = None
    if any(n["mesh"] for n in nodes):
        os.makedirs(os.path.join(a, "Materials"))
        mat_guid = guid("BatFragment")
        with open(os.path.join(a, "Materials", "BatFragment.mat"), "w") as f:
            f.write("%%YAML 1.1\n%%TAG !u! tag:unity3d.com,2011:\n--- !u!21 &2100000\n"
                    "Material:\n  m_Name: BatFragment\n  m_SavedProperties:\n    m_TexEnvs:\n"
                    "    - _MainTex:\n        m_Texture: {fileID: 2800000, guid: %s, type: 3}\n"
                    "        m_Scale: {x: 1, y: 1}\n        m_Offset: {x: 0, y: 0}\n" % sg["Ball"])
        with open(os.path.join(a, "Materials", "BatFragment.mat.meta"), "w") as f:
            f.write("fileFormatVersion: 2\nguid: %s\n" % mat_guid)
    for n in nodes:
        h[n["id"]] = s.obj(n["name"], n["pos"], scale=n["scale"], rot_z=n["rot"],
                           parent=h.get(n["parent"]), tag=n["tag"], z=n["z"],
                           active=n["active"])
    for n in nodes:
        o = h[n["id"]]
        if n["mesh"]:
            cell = n["mesh"]
            tris = [i for k in range(1, len(cell) - 1) for i in (0, k, k + 1)]
            mesh_id = s.ids()
            s.parts.append("--- !u!43 &%d\n" % mesh_id + _mesh_yaml(
                "%s_%d" % (n["name"], n["id"]), cell,
                [(x + 0.5, y + 0.5) for x, y in cell], tris))
            s.component(o, lambda cid, go, m=mesh_id: (
                "--- !u!33 &%d\nMeshFilter:\n  m_GameObject: {fileID: %d}\n"
                "  m_Mesh: {fileID: %d}\n" % (cid, go, m)))
            s.component(o, lambda cid, go: (
                "--- !u!23 &%d\nMeshRenderer:\n  m_GameObject: {fileID: %d}\n  m_Enabled: 1\n"
                "  m_Materials:\n  - {fileID: 2100000, guid: %s, type: 2}\n"
                "  m_SortingLayerID: 0\n  m_SortingOrder: 3\n" % (cid, go, mat_guid)))
            s.component(o, lambda cid, go, c=cell: (
                "--- !u!60 &%d\nPolygonCollider2D:\n  m_GameObject: {fileID: %d}\n  m_Enabled: 1\n"
                "  m_Offset: {x: 0, y: 0}\n  m_Points:\n    m_Paths:\n    - %s\n"
                % (cid, go, "\n      ".join("- {x: %r, y: %r}" % (x, y) for x, y in c).replace("- ", "", 1)
                   if False else _paths_yaml(c))))
            s.component(o, lambda cid, go: (
                "--- !u!50 &%d\nRigidbody2D:\n  m_GameObject: {fileID: %d}\n"
                "  m_BodyType: 0\n  m_Simulated: 1\n  m_Mass: 0.2\n  m_GravityScale: 1\n"
                % (cid, go)))
        if n["gpu"]:
            s.script(o, gpu_guid)
            s.counts["gpu_roots"] += 1
        if n["anim"]:
            s.animation(o, clip_guid[n["anim"]])
        if n["sprite"]:
            shape, col, order = n["sprite"]
            s.sprite(o, sg[shape], col, order=order)
        if n["light"]:
            col, inten, rad, inner = n["light"]
            s.light2d(o, col, inten, rad, inner)
        if n["particles"]:
            rate, speed, grav = n["particles"]
            s.particles(o, rate=rate, speed=speed, gravity=grav)
        if n["explodable"]:
            # Explodable requires a Rigidbody2D (kinematic: it flies with the
            # bat's own motion), and fractures along its collider
            s.component(o, lambda cid, go: (
                "--- !u!50 &%d\nRigidbody2D:\n  m_GameObject: {fileID: %d}\n"
                "  m_BodyType: 1\n  m_Simulated: 1\n  m_Mass: 1\n  m_GravityScale: 1\n"
                % (cid, go)))
            s.component(o, lambda cid, go: (
                "--- !u!61 &%d\nBoxCollider2D:\n  m_GameObject: {fileID: %d}\n  m_Enabled: 1\n"
                "  m_IsTrigger: 0\n  m_Offset: {x: 0, y: 0}\n  m_Size: {x: 1, y: 1}\n"
                % (cid, go)))
            frags = [h[f][0] for f in n["fragments"]]
            explodables.append(s.script(o, expl_guid, fields=(
                "  allowRuntimeFragmentation: %d\n  extraPoints: 6\n  subshatterSteps: 0\n"
                "  fragmentLayer: Default\n  sortingLayerName: Default\n  orderInLayer: 3\n"
                "  shatterType: 1\n  fragments:%s\n" % (
                    0 if frags else 1,
                    "".join("\n  - {fileID: %d}" % g for g in frags) or " []"))))
            s.script(o, quake_guid, fields="  delay: %g\n" % (0.5 * len(explodables)))
        if n["camera"]:
            s.component(o, lambda cid, go: (
                "--- !u!20 &%d\nCamera:\n  m_GameObject: {fileID: %d}\n  m_Enabled: 1\n"
                "  m_ClearFlags: 2\n  m_BackGroundColor: {r: 0.02, g: 0.02, b: 0.04, a: 1}\n"
                "  orthographic: 1\n  orthographic size: 9.5\n" % (cid, go)))
    with open(os.path.join(a, "Scenes", "Cave.unity"), "w") as f:
        f.write(s.finish())
    return model_stats(nodes)


def _mesh_yaml(name, verts, uv, tris):
    """A Mesh body as Unity serialises it (crust's unity_pack_mesh writes the
    same format it reads)."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import tools.unity_pack_mesh as um
    return um.write_mesh(name, verts, uv, tris)


def _paths_yaml(cell):
    return "\n      ".join(("- " if k == 0 else "  ") + "{x: %r, y: %r}" % (x, y)
                           for k, (x, y) in enumerate(cell)).replace("  {", "- {")


# ---- Godot 4 ----------------------------------------------------------------------

PX = 64.0       # pixels a unit: the sprites are 64 px at 64 px a unit in Unity


def _gd(v):
    return ("%.6g" % v).replace("inf", "INF")


def build_godot(out, worms=12, segments=40, bats=20, holes=5, clip_variants=3):
    """The same cave as a Godot 4 project. Godot's 2D is pixels with y down:
    a position is (x, -y) * PX, a rotation (and every curve key) is negated
    and in radians."""
    nodes, clips = cave_model(worms, segments, bats, holes, clip_variants)
    if os.path.isdir(out):
        shutil.rmtree(out)
    write_sprites(os.path.join(out, "sprites"))
    os.makedirs(os.path.join(out, "scripts"))
    with open(os.path.join(out, "scripts", "GpuHierarchy.cs"), "w") as f:
        f.write("using Godot;\n\n/// %s\npublic partial class GpuHierarchy : Node2D { }\n" % GPU_NOTE)
    with open(os.path.join(out, "project.godot"), "w") as f:
        f.write('config_version=5\n\n[application]\n\nconfig/name="Cave"\n'
                'run/main_scene="res://main.tscn"\nconfig/features=PackedStringArray("4.2", "C#")\n\n'
                '[dotnet]\n\nproject/assembly_name="Cave"\n')
    with open(os.path.join(out, "Cave.csproj"), "w") as f:
        f.write('<Project Sdk="Godot.NET.Sdk/4.2.0">\n  <PropertyGroup>\n'
                '    <TargetFramework>net6.0</TargetFramework>\n'
                '    <EnableDynamicLoading>true</EnableDynamicLoading>\n  </PropertyGroup>\n</Project>\n')
    sub = []
    for name, c in clips.items():
        lines = ['[sub_resource type="Animation" id="Anim_%s"]' % name,
                 'resource_name = "%s"' % name, "length = %s" % _gd(c["length"]),
                 "loop_mode = 1"]
        for k, (path, keys) in enumerate(c["tracks"]):
            lines += ['tracks/%d/type = "value"' % k, "tracks/%d/imported = false" % k,
                      "tracks/%d/enabled = true" % k,
                      'tracks/%d/path = NodePath("%s:rotation")' % (k, path),
                      "tracks/%d/interp = 1" % k, "tracks/%d/loop_wrap = true" % k,
                      "tracks/%d/keys = {" % k,
                      '"times": PackedFloat32Array(%s),' % ", ".join(_gd(t) for t, _ in keys),
                      '"transitions": PackedFloat32Array(%s),' % ", ".join("1" for _ in keys),
                      '"update": 0,',
                      '"values": [%s]' % ", ".join(_gd(-math.radians(z)) for _, z in keys),
                      "}"]
        sub.append("\n".join(lines))
        sub.append('[sub_resource type="AnimationLibrary" id="Lib_%s"]\n_data = {\n"%s": '
                   'SubResource("Anim_%s")\n}' % (name, name, name))
    path_of = {}
    body = []
    for n in nodes:
        par = n["parent"]
        path_of[n["id"]] = n["name"] if par is None else path_of[par] + "/" + n["name"]
        parent_attr = "." if par is None else path_of[par]
        kind = ("Camera2D" if n["camera"] else "PointLight2D" if n["light"] else
                "CPUParticles2D" if n["particles"] else "Sprite2D" if n["sprite"] else "Node2D")
        x, y = n["pos"]
        lines = ['[node name="%s" type="%s" parent="%s"]' % (n["name"], kind, parent_attr)]
        if n["gpu"]:
            lines.append('script = ExtResource("3_gpu")')
        lines.append("position = Vector2(%s, %s)" % (_gd(x * PX), _gd(-y * PX)))
        if n["rot"]:
            lines.append("rotation = %s" % _gd(-math.radians(n["rot"])))
        if n["scale"] != 1.0 and not n["light"]:
            lines.append("scale = Vector2(%s, %s)" % (_gd(n["scale"]), _gd(n["scale"])))
        if n["sprite"]:
            shape, (r, g, b, a), order = n["sprite"]
            lines += ['texture = ExtResource("%s")' % ("1_ball" if shape == "Ball" else "2_square"),
                      "modulate = Color(%s, %s, %s, %s)" % tuple(_gd(v) for v in (r, g, b, a)),
                      "z_index = %d" % order]
        if n["light"]:
            (r, g, b), inten, rad, _inner = n["light"]
            lines += ['texture = ExtResource("1_ball")',
                      "color = Color(%s, %s, %s, 1)" % (_gd(r), _gd(g), _gd(b)),
                      "energy = %s" % _gd(inten),
                      "texture_scale = %s" % _gd(rad * 2.0)]
        if n["particles"]:
            rate, speed, grav = n["particles"]
            lines += ["amount = %d" % int(rate * 3), "lifetime = 3.0",
                      "direction = Vector2(0, 1)", "spread = 5.0",
                      "gravity = Vector2(0, %s)" % _gd(9.81 * PX * grav),
                      "initial_velocity_min = %s" % _gd(speed * PX),
                      "initial_velocity_max = %s" % _gd(speed * PX),
                      "scale_amount_min = 0.12", "scale_amount_max = 0.12",
                      "color = Color(0.4, 0.7, 1, 0.9)"]
        if n["camera"]:
            lines.append("zoom = Vector2(%s, %s)" % (_gd(648.0 / (19.0 * PX)), _gd(648.0 / (19.0 * PX))))
        body.append("\n".join(lines))
        if n["anim"]:
            body.append('[node name="AnimationPlayer" type="AnimationPlayer" parent="%s"]\n'
                        'root_node = NodePath("..")\nlibraries = {\n"": SubResource("Lib_%s")\n}\n'
                        'autoplay = "%s"' % (path_of[n["id"]], n["anim"], n["anim"]))
    head = ['[gd_scene load_steps=%d format=3]' % (4 + len(sub)),
            '[ext_resource type="Texture2D" path="res://sprites/Ball.png" id="1_ball"]',
            '[ext_resource type="Texture2D" path="res://sprites/Square.png" id="2_square"]',
            '[ext_resource type="Script" path="res://scripts/GpuHierarchy.cs" id="3_gpu"]']
    root = '[node name="Cave" type="Node2D"]'
    # the cave's top-level nodes are children of the scene root "Cave"
    with open(os.path.join(out, "main.tscn"), "w") as f:
        f.write("\n\n".join(head + sub + [root] + body) + "\n")
    return model_stats(nodes)


# ---- Blender ------------------------------------------------------------------------

BLENDER_SCRIPT = r"""# Generated by crust's tools/gen_cave_scene.py: builds the cave benchmark.
#   blender --background --python build_cave.py -- [--save cave.blend]
import json
import math
import sys

import bpy

MODEL = json.loads(%(model)r)
FPS = 60

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.fps = FPS
scene.frame_start, scene.frame_end = 1, 240
col = bpy.data.collections.new("Cave")
scene.collection.children.link(col)
mats = {}


def material(rgba):
    key = tuple(round(v, 4) for v in rgba)
    if key not in mats:
        m = bpy.data.materials.new("C%%d" %% len(mats))
        m.diffuse_color = key
        mats[key] = m
    return mats[key]


objs = {}
for n in MODEL["nodes"]:
    name = n["unique"]
    if n["camera"]:
        cam = bpy.data.cameras.new(name)
        cam.type = "ORTHO"
        cam.ortho_scale = 19.0 * 16.0 / 9.0
        o = bpy.data.objects.new(name, cam)
        scene.camera = o
    elif n["light"]:
        (r, g, b), inten, rad, _inner = n["light"]
        ld = bpy.data.lights.new(name, "POINT")
        ld.color = (r, g, b)
        ld.energy = inten * 100.0
        ld.shadow_soft_size = rad * 0.25
        o = bpy.data.objects.new(name, ld)
    elif n["sprite"]:
        shape, rgba, _order = n["sprite"]
        if shape == "Ball":
            bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, segments=16, ring_count=8)
        else:
            bpy.ops.mesh.primitive_plane_add(size=1.0)
        o = bpy.context.active_object
        o.name = name
        o.data.materials.append(material(rgba))
        for c in o.users_collection:
            c.objects.unlink(o)
    else:
        o = bpy.data.objects.new(name, None)
        o.empty_display_size = 0.2
    col.objects.link(o)
    if n["parent"] is not None:
        o.parent = objs[n["parent"]]
    x, y = n["pos"]
    # Blender is z up: the cave lies in x / y, the camera looks down -z
    o.location = (x, y, -n["z"] if n["camera"] else 0.0)
    o.rotation_euler = (0.0, 0.0, math.radians(n["rot"]))
    o.scale = (n["scale"],) * 3
    if n["gpu"]:
        o["GpuHierarchy"] = True
    if n["particles"]:
        rate, speed, grav = n["particles"]
        emit = o.modifiers.new("Drip", "PARTICLE_SYSTEM") if o.type == "MESH" else None
        if emit is None:
            bpy.ops.mesh.primitive_plane_add(size=0.2)
            e = bpy.context.active_object
            e.name = name + "_emitter"
            for c in e.users_collection:
                c.objects.unlink(e)
            col.objects.link(e)
            e.parent = o
            e.rotation_euler = (math.pi, 0.0, 0.0)       # emits downwards
            e.modifiers.new("Drip", "PARTICLE_SYSTEM")
            ps = e.particle_systems[0].settings
            ps.count = int(rate * 4)
            ps.frame_start, ps.frame_end = 1, 240
            ps.lifetime = 3 * FPS
            ps.normal_factor = speed
            ps.effector_weights.gravity = grav
            ps.particle_size = 0.06
    objs[n["id"]] = o

# the clips: each track's target resolved when generated (names repeat across
# worms, and Blender renames duplicates), keyed at FPS, looping by Cycles
for target, keys, length in MODEL["anims"]:
    o = objs[target]
    for t, z in keys:
        o.rotation_euler[2] = math.radians(z)
        o.keyframe_insert(data_path="rotation_euler", index=2, frame=1 + t * FPS)
    fc = o.animation_data.action.fcurves.find("rotation_euler", index=2)
    if fc is not None:
        fc.modifiers.new("CYCLES")

if "--save" in sys.argv:
    bpy.ops.wm.save_as_mainfile(filepath=sys.argv[sys.argv.index("--save") + 1])
"""


def build_blender(out, worms=12, segments=40, bats=20, holes=5, clip_variants=3):
    """The same cave as a bpy script (and `cave.blend` when Blender is here)."""
    import json
    import subprocess
    nodes, clips = cave_model(worms, segments, bats, holes, clip_variants)
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)
    for n in nodes:
        n["unique"] = "%s_%d" % (n["name"].replace(" ", "_"), n["id"])
    anims = []
    for n in nodes:
        if n["anim"]:
            c = clips[n["anim"]]
            for path, keys in c["tracks"]:
                t = resolve_path(nodes, n["id"], path)
                if t is not None:
                    anims.append((t, keys, c["length"]))
    model = json.dumps({"nodes": nodes, "anims": anims}, separators=(",", ":"))
    script = os.path.join(out, "build_cave.py")
    with open(script, "w") as f:
        f.write(BLENDER_SCRIPT % {"model": model})
    blender = shutil.which("blender")
    if blender:
        subprocess.run([blender, "--background", "--python", script, "--",
                        "--save", os.path.join(out, "cave.blend")], check=True)
    st = model_stats(nodes)
    st["blend"] = bool(blender)
    st["keyed_tracks"] = len(anims)
    return st


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", nargs="?", default=None)
    ap.add_argument("--worms", type=int, default=12)
    ap.add_argument("--segments", type=int, default=40)
    ap.add_argument("--bats", type=int, default=20)
    ap.add_argument("--holes", type=int, default=5)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--godot", action="store_true", help="a Godot 4 project")
    g.add_argument("--blender", action="store_true", help="a Blender (bpy) build script")
    ap.add_argument("--destructible", action="store_true",
                    help="Unity: the bats explode (Unity-2D-Destruction)")
    a = ap.parse_args()
    if a.destructible and (a.godot or a.blender):
        ap.error("--destructible is for the Unity project")
    if a.godot:
        out, fn, what = a.out or "/tmp/godot_cave_scene", build_godot, "Godot 4 project"
    elif a.blender:
        out, fn, what = a.out or "/tmp/blender_cave_scene", build_blender, "Blender build script"
    else:
        out, fn, what = a.out or "/tmp/cave", build, "Unity project"
    if a.destructible:
        c = build(out, a.worms, a.segments, a.bats, a.holes, destructible=True)
    else:
        c = fn(out, a.worms, a.segments, a.bats, a.holes)
    extra = ""
    if c.get("explodable"):
        extra = "; %d explodable (one every 0.5 s, TimedExplode)" % c["explodable"]
    if a.blender:
        extra = ("; cave.blend saved" if c["blend"] else
                 "; no blender on the PATH: run `blender --background --python "
                 "build_cave.py -- --save cave.blend`")
    print("%s (%s): %d objects, hierarchy depth %d, %d animated roots (%d marked "
          "GpuHierarchy), %d lights%s" % (out, what, c["objects"], c["max_depth"],
                                         c["animated"], c["gpu_roots"], c["lights"], extra))


if __name__ == "__main__":
    main()
