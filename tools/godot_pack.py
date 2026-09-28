"""godot_pack -- a Godot 4 project packed into engine.c / data.c.

    python3 tools/godot_pack.py <project> [-o DIR] [unity_pack options]

Godot projects are packed by unity_pack's back end: this module produces
the object list and script analyses a Unity project produces, and
everything after import -- field widths, bitfields, handles, SoA
positions, Box2D-Packed -- is the packer's own and is not written twice.
unity_pack calls it for any directory holding a `project.godot`.

* **Scenes.** `.tscn`, `.tres` and `project.godot` are parsed with one
  reader for Godot's text resource format (`parse_resource`). The main
  scene is `run/main_scene` (a `res://` path or a `uid://`), instanced
  scenes are expanded in place with their overrides, sub-resources and
  `.tres` files are resolved, and each node's global transform is composed
  from its parents. Every node is one object; its class is the class its C#
  script declares, or its name when it has none. Exported values the scene
  sets become the object's fields.

* **Physics.** A `RigidBody2D`, `CharacterBody2D`, `AnimatableBody2D`,
  `StaticBody2D` or `Area2D` takes its `CollisionShape2D` child
  (`RectangleShape2D` / `CircleShape2D`) and its `PhysicsMaterial` into the
  body's Rigidbody2D / Collider2D rows, as a Unity GameObject's components
  are; the shape node is not an object of its own. Box2D-Packed runs them in
  its Godot mode (`box2d_unity.emit_glue(..., mode="godot")`): Godot's
  pixels and gravity, its material rules, its damping.

* **Scripts.** A Godot C# node script is presented to unity_pack's analyzer
  as the Unity-shaped C# subset it already lowers (`adapt_csharp`), keeping
  every line where it was, so a csc-style diagnostic names the Godot file's
  own line. Godot API with no packed meaning yet is refused at the use site
  (`error CS8000`), never dropped.

Coordinates are Godot's own -- pixels, y down in 2D -- in the packed
tables, the scripts and the physics world alike. See GODOT_PACK.md.
"""

from __future__ import annotations

import math
import os
import re
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

import tools.cs2cpp as cs2cpp  # noqa: E402
from tools.unity_pack_common import PackError, _progress, _read

__all__ = [
    "GdCall",
    "GODOT_ROOT",
    "BINDINGS",
    "STRING_CALLS",
    "adapt_csharp",
    "analyze_scripts",
    "emit_signal_decls",
    "emit_signal_dispatch",
    "exported_members",
    "godot_display_path",
    "godot_fingerprint_paths",
    "godot_screen_size",
    "is_godot_project",
    "load_godot_scenes",
    "main",
    "parse_resource",
    "parse_tscn",
    "physics_settings",
    "resolve_signals",
    "scene_objects",
    "script_signal_connections",
]

#: The Godot project being packed, for `res://` diagnostics (else None).
GODOT_ROOT = [None]


# ---------------------------------------------------------------------------
# Godot text resource format (.tscn / .tres / project.godot)
# ---------------------------------------------------------------------------

class GdCall(object):
    """A constructor-style value: `Vector2(1, 2)`, `ExtResource("1_a")`."""

    __slots__ = ("name", "args")

    def __init__(self, name, args):
        self.name = name
        self.args = args

    def __eq__(self, other):
        return (isinstance(other, GdCall) and other.name == self.name
                and other.args == self.args)

    def __repr__(self):
        return "%s(%s)" % (self.name, ", ".join(repr(a) for a in self.args))


class _ResourceError(Exception):
    def __init__(self, pos, msg):
        Exception.__init__(self, msg)
        self.pos = pos
        self.msg = msg


_NUM_RE = re.compile(
    r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?|[-+]?(?:inf|nan)\b")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_KEY_RE = re.compile(r"[^\s=\[\]]+")
_WS_RE = re.compile(r"(?:\s+|;[^\n]*)+")


class _Reader(object):
    def __init__(self, text):
        self.text = text
        self.i = 0

    def ws(self):
        m = _WS_RE.match(self.text, self.i)
        if m:
            self.i = m.end()

    def peek(self):
        self.ws()
        return self.text[self.i:self.i + 1]

    def expect(self, ch):
        if self.peek() != ch:
            raise _ResourceError(self.i, "expected %r" % ch)
        self.i += 1

    def string(self):
        # "..." with Godot's escapes; strings may span lines.
        assert self.text[self.i] == '"'
        j = self.i + 1
        out = []
        while True:
            if j >= len(self.text):
                raise _ResourceError(self.i, "unterminated string")
            c = self.text[j]
            if c == '"':
                break
            if c == "\\" and j + 1 < len(self.text):
                e = self.text[j + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(e, e))
                j += 2
                continue
            out.append(c)
            j += 1
        self.i = j + 1
        return "".join(out)

    def value(self):
        c = self.peek()
        if c == '"':
            return self.string()
        if c in "&^" and self.text[self.i + 1:self.i + 2] == '"':
            # &"StringName", ^"NodePath"
            kind = c
            self.i += 1
            s = self.string()
            return GdCall("NodePath", [s]) if kind == "^" else s
        if c == "[":
            self.i += 1
            items = []
            while self.peek() != "]":
                items.append(self.value())
                if self.peek() == ",":
                    self.i += 1
            self.i += 1
            return items
        if c == "{":
            self.i += 1
            d = {}
            while self.peek() != "}":
                k = self.value()
                self.expect(":")
                v = self.value()
                d[k if not isinstance(k, list) else tuple(k)] = v
                if self.peek() == ",":
                    self.i += 1
            self.i += 1
            return d
        m = _NUM_RE.match(self.text, self.i)
        if m:
            self.i = m.end()
            s = m.group(0)
            if re.fullmatch(r"[-+]?\d+", s):
                return int(s)
            return float(s)
        m = _IDENT_RE.match(self.text, self.i)
        if not m:
            raise _ResourceError(self.i, "expected a value")
        name = m.group(0)
        self.i = m.end()
        if name == "true":
            return True
        if name == "false":
            return False
        if name == "null":
            return None
        # Typed collections: Array[int]([1, 2]), Dictionary[K, V]({..})
        if self.text[self.i:self.i + 1] == "[":
            depth = 0
            j = self.i
            while j < len(self.text):
                if self.text[j] == "[":
                    depth += 1
                elif self.text[j] == "]":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            name += self.text[self.i:j + 1]
            self.i = j + 1
        if self.peek() == "(":
            self.i += 1
            args = []
            while self.peek() != ")":
                args.append(self.value())
                if self.peek() == ",":
                    self.i += 1
            self.i += 1
            return GdCall(name, args)
        return GdCall(name, None)  # a bare identifier (rare)


def _line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def parse_resource(text, path="<resource>"):
    """Sections of a Godot text resource, in order.

    Each is ``{"tag", "attrs", "props", "line", "prop_lines"}``; ``attrs``
    are the header's ``key=value`` pairs and ``props`` the ``key = value``
    lines under it, in order. Properties before the first header (none in a
    .tscn; ``config_version`` in project.godot) go in a section tagged
    ``""``. Values are Python values, with `GdCall` for constructors.
    """
    r = _Reader(text)
    sections = [{"tag": "", "attrs": {}, "props": {}, "line": 1,
                 "prop_lines": {}}]
    try:
        while True:
            r.ws()
            if r.i >= len(text):
                break
            if text[r.i] == "[":
                start = r.i
                r.i += 1
                m = _IDENT_RE.match(text, r.i)
                if not m:
                    raise _ResourceError(r.i, "expected a section name")
                r.i = m.end()
                attrs = {}
                while r.peek() != "]":
                    km = _IDENT_RE.match(text, r.i)
                    if not km:
                        raise _ResourceError(r.i, "expected an attribute")
                    r.i = km.end()
                    r.expect("=")
                    attrs[km.group(0)] = r.value()
                r.i += 1
                sections.append({"tag": m.group(0), "attrs": attrs,
                                 "props": {}, "line": _line_of(text, start),
                                 "prop_lines": {}})
                continue
            km = _KEY_RE.match(text, r.i)
            if not km:
                raise _ResourceError(r.i, "expected a property")
            key = km.group(0)
            if key.startswith('"') and key.endswith('"'):
                key = key[1:-1]
            kpos = r.i
            r.i = km.end()
            r.expect("=")
            sections[-1]["props"][key] = r.value()
            sections[-1]["prop_lines"][key] = _line_of(text, kpos)
    except _ResourceError as e:
        raise PackError("%s:%d: error: %s" % (
            godot_display_path(path), _line_of(text, e.pos), e.msg))
    return sections


# ---------------------------------------------------------------------------
# Project layout
# ---------------------------------------------------------------------------

def is_godot_project(root):
    """A `project.godot`, or `.tscn` scenes and no Unity `Assets/` tree."""
    root = os.path.abspath(root)
    if os.path.isfile(os.path.join(root, "project.godot")):
        return True
    if os.path.isdir(os.path.join(root, "Assets")):
        return False
    return bool(_walk(root, (".tscn",)))


_SKIP_DIRS = frozenset((".git", ".godot", ".import", "obj", "bin",
                        "__pycache__", "node_modules", "addons"))


def _walk(root, exts):
    out = []
    for d, dirs, names in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in _SKIP_DIRS)
        for n in sorted(names):
            if n.endswith(exts):
                out.append(os.path.join(d, n))
    return out


def godot_fingerprint_paths(root):
    """Inputs of a Godot pack, for unity_pack's rebuild stamp."""
    return _walk(os.path.abspath(root), (
        ".tscn", ".tres", ".cs", ".gd", ".godot", ".png", ".uid"))


def godot_display_path(path):
    """`res://...` for a file in the project being packed."""
    root = GODOT_ROOT[0]
    if root and path:
        ap = os.path.abspath(path)
        if ap == root or ap.startswith(root + os.sep):
            return "res://" + os.path.relpath(ap, root).replace(os.sep, "/")
    return os.path.basename(path) if path else "<godot>"


def _project_settings(root):
    p = os.path.join(root, "project.godot")
    if not os.path.isfile(p):
        return {}
    out = {}
    for sec in parse_resource(_read(p), p):
        for k, v in sec["props"].items():
            out[(sec["tag"] + "/" + k) if sec["tag"] else k] = v
    return out


def physics_settings(root):
    """The project's 2D physics, as Godot reads it at start (its defaults
    otherwise): gravity (pixels / s^2, y down), the fixed step, default
    linear damping, and the pixels per Box2D metre for Box2D-Packed
    (`[godot_pack] length_units_per_meter`, 64 when unset)."""
    s = _project_settings(os.path.abspath(root))
    g = float(s.get("physics/2d/default_gravity", 980.0))
    gv = _vec(s.get("physics/2d/default_gravity_vector"), 2, (0.0, 1.0))
    ticks = int(s.get("physics/common/physics_ticks_per_second", 60))
    return {
        "gravity": (g * gv[0], g * gv[1]),
        "fixed_dt": 1.0 / max(1, ticks),
        "default_linear_damp": float(
            s.get("physics/2d/default_linear_damp", 0.1)),
        "length_units_per_meter": float(
            s.get("godot_pack/length_units_per_meter", 64.0)),
    }


def godot_screen_size(root):
    """Viewport size from project.godot (Godot 4's default 1152 x 648)."""
    s = _project_settings(os.path.abspath(root))
    w = s.get("display/window/size/viewport_width", 1152)
    h = s.get("display/window/size/viewport_height", 648)
    return int(w), int(h)


class _Project(object):
    """res:// and uid:// resolution for one Godot project."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.uids = {}
        for p in _walk(self.root, (".tscn", ".tres")):
            m = re.search(r'^\[gd_(?:scene|resource)\b[^\]]*?\buid="(uid://[^"]+)"',
                          _read(p), re.M)
            if m:
                self.uids[m.group(1)] = p
        for p in _walk(self.root, (".uid",)):
            u = _read(p).strip()
            if u.startswith("uid://"):
                self.uids[u] = p[:-len(".uid")]

    def resolve(self, res_path, uid=None, where=None):
        if isinstance(res_path, str) and res_path.startswith("res://"):
            p = os.path.join(self.root, *res_path[len("res://"):].split("/"))
            if os.path.isfile(p):
                return p
        if uid and uid in self.uids:
            return self.uids[uid]
        if isinstance(res_path, str) and res_path.startswith("uid://") \
                and res_path in self.uids:
            return self.uids[res_path]
        raise PackError("%s: error: cannot find %s" % (
            where or godot_display_path(self.root), res_path or uid))


def _main_scenes(proj):
    """The scene(s) to pack: `run/main_scene`, else every scene that no
    other scene instances."""
    main = _project_settings(proj.root).get("application/run/main_scene")
    if main:
        return [proj.resolve(main, uid=main,
                             where="res://project.godot")]
    scenes = _walk(proj.root, (".tscn",))
    instanced = set()
    for p in scenes:
        for sec in parse_resource(_read(p), p):
            if sec["tag"] == "ext_resource" \
                    and sec["attrs"].get("type") == "PackedScene":
                try:
                    instanced.add(proj.resolve(
                        sec["attrs"].get("path"), sec["attrs"].get("uid")))
                except PackError:
                    pass
    return [p for p in scenes if p not in instanced]


# ---------------------------------------------------------------------------
# Scene tree
# ---------------------------------------------------------------------------

_NODE2D_TYPES = frozenset((
    "Node2D", "Sprite2D", "AnimatedSprite2D", "CharacterBody2D",
    "RigidBody2D", "StaticBody2D", "AnimatableBody2D", "Area2D",
    "CollisionShape2D", "CollisionPolygon2D", "Camera2D", "Marker2D",
    "Path2D", "PathFollow2D", "Polygon2D", "Line2D", "TileMap",
    "TileMapLayer", "GPUParticles2D", "CPUParticles2D", "PointLight2D",
    "DirectionalLight2D", "RayCast2D", "ShapeCast2D", "RemoteTransform2D",
    "AudioStreamPlayer2D", "VisibleOnScreenNotifier2D", "Skeleton2D",
    "Bone2D", "Parallax2D", "ParallaxLayer", "MeshInstance2D",
    "MultiMeshInstance2D", "NavigationAgent2D", "NavigationRegion2D",
    "BackBufferCopy", "CanvasGroup", "PinJoint2D", "GrooveJoint2D",
    "DampedSpringJoint2D", "LightOccluder2D", "TouchScreenButton",
))


def _is_3d_type(t):
    return bool(t) and (t.endswith("3D") or t in (
        "Node3D", "MeshInstance3D", "Camera3D", "OmniLight3D",
        "SpotLight3D", "DirectionalLight3D", "Marker3D", "Skeleton3D"))


def _affine2(pos, rot, scale):
    c, s = math.cos(rot), math.sin(rot)
    return ((c * scale[0], -s * scale[1], pos[0]),
            (s * scale[0], c * scale[1], pos[1]))


def _mul2(a, b):
    return tuple(tuple(
        a[r][0] * b[0][k] + a[r][1] * b[1][k] + (a[r][2] if k == 2 else 0.0)
        for k in range(3)) for r in range(2))


def _mat3_mul(a, b):
    return tuple(tuple(sum(a[r][k] * b[k][c] for k in range(3))
                       for c in range(3)) for r in range(3))


def _quat_from_basis(m):
    """(x, y, z, w) of the rotation in basis *m* (rows), scale removed."""
    cols = [math.sqrt(sum(m[r][c] ** 2 for r in range(3))) or 1.0
            for c in range(3)]
    n = [[m[r][c] / cols[c] for c in range(3)] for r in range(3)]
    tr = n[0][0] + n[1][1] + n[2][2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        return ((n[2][1] - n[1][2]) / s, (n[0][2] - n[2][0]) / s,
                (n[1][0] - n[0][1]) / s, 0.25 * s)
    if n[0][0] > n[1][1] and n[0][0] > n[2][2]:
        s = math.sqrt(1.0 + n[0][0] - n[1][1] - n[2][2]) * 2
        return (0.25 * s, (n[0][1] + n[1][0]) / s, (n[0][2] + n[2][0]) / s,
                (n[2][1] - n[1][2]) / s)
    if n[1][1] > n[2][2]:
        s = math.sqrt(1.0 + n[1][1] - n[0][0] - n[2][2]) * 2
        return ((n[0][1] + n[1][0]) / s, 0.25 * s, (n[1][2] + n[2][1]) / s,
                (n[0][2] - n[2][0]) / s)
    s = math.sqrt(1.0 + n[2][2] - n[0][0] - n[1][1]) * 2
    return ((n[0][2] + n[2][0]) / s, (n[1][2] + n[2][1]) / s, 0.25 * s,
            (n[1][0] - n[0][1]) / s)


def _vec(v, n, default):
    if isinstance(v, GdCall) and v.name.startswith("Vector") and v.args:
        vals = [float(a) for a in v.args][:n]
        return tuple(vals + [0.0] * (n - len(vals)))
    return default


class _Node(object):
    __slots__ = ("name", "type", "path", "parent", "props", "prop_lines",
                 "script", "scene", "line", "children", "instance_of",
                 "groups", "conns")

    def __init__(self, name, type_, path, parent, scene, line):
        self.name = name
        self.type = type_
        self.path = path          # "" for the root, "A/B" below it
        self.parent = parent      # _Node or None
        self.props = {}
        self.prop_lines = {}
        self.script = None        # absolute path of the node's script
        self.scene = scene        # file the node (or its override) came from
        self.line = line
        self.children = []
        self.instance_of = None   # an instanced scene's root: its own name
        self.groups = []
        self.conns = None          # the top scene's root: its connections


def _load_tree(proj, path, stack=(), conns=None):
    """The node tree of scene *path*, instanced scenes expanded. Its
    `[connection]`s, and its instanced scenes', are appended to *conns*
    with their nodes resolved."""
    if conns is None:
        conns = []
    if path in stack:
        raise PackError("%s: error: scene instances itself (%s)" % (
            godot_display_path(path),
            " -> ".join(godot_display_path(p) for p in stack + (path,))))
    text = _read(path)
    secs = parse_resource(text, path)
    ext_file, res_value = _resolver(proj, path, secs)
    root = None
    by_path = {}

    for s in secs:
        if s["tag"] != "node":
            continue
        a = s["attrs"]
        where = "%s:%d" % (godot_display_path(path), s["line"])
        name = a.get("name")
        parent_path = a.get("parent")
        if parent_path is None:
            if root is not None:
                raise PackError("%s: error: second root node" % where)
            node_path = ""
            parent = None
        else:
            parent_path = "" if parent_path == "." else parent_path
            if parent_path not in by_path:
                raise PackError("%s: error: parent %r is not in the scene"
                                % (where, a.get("parent")))
            parent = by_path[parent_path]
            node_path = (parent_path + "/" + name) if parent_path else name
        inst = a.get("instance")
        if isinstance(inst, GdCall) and inst.name == "ExtResource":
            _ty, sub = ext_file(inst, where)
            node = _load_tree(proj, sub, stack + (path,), conns)
            _rebase(node, name, node_path, parent)
            _index(node, by_path)
        elif node_path in by_path and "type" not in a:
            # An override of a node an instanced scene brought in.
            node = by_path[node_path]
        else:
            node = _Node(name, a.get("type") or "Node", node_path, parent,
                         path, s["line"])
            by_path[node_path] = node
        if parent is not None and node not in parent.children:
            parent.children.append(node)
        for g in a.get("groups") or []:
            if g not in node.groups:
                node.groups.append(g)
        if root is None and parent is None:
            root = node
        for k, v in s["props"].items():
            if k == "script":
                if v is None:
                    node.script = None
                    continue
                if not (isinstance(v, GdCall) and v.name == "ExtResource"):
                    raise PackError(
                        "%s:%d: error: built-in scripts are not packed; "
                        "save the script to a file" % (
                            godot_display_path(path), s["prop_lines"][k]))
                ty, sp = ext_file(v, where)
                if sp.endswith(".gd"):
                    raise PackError(
                        "%s:%d: error: GDScript is not packed yet (%s); "
                        "unity_pack reads Godot C# scripts" % (
                            godot_display_path(path), s["prop_lines"][k],
                            godot_display_path(sp)))
                node.script = sp
                continue
            ploc = "%s:%d" % (godot_display_path(path), s["prop_lines"][k])
            node.props[k] = res_value(v, ploc)
            node.prop_lines[k] = (path, s["prop_lines"][k])
    if root is None:
        raise PackError("%s: error: no root node" % godot_display_path(path))
    for s in secs:
        if s["tag"] != "connection":
            continue
        a = s["attrs"]
        where = (path, s["line"])
        ends = []
        for key in ("from", "to"):
            np_ = a.get(key)
            np_ = "" if np_ in (".", None) else np_
            if np_ not in by_path:
                raise PackError("%s:%d: error: connection %s %r is not in the "
                                "scene" % (godot_display_path(path), s["line"],
                                           key, a.get(key)))
            ends.append(by_path[np_])
        if a.get("binds") or a.get("unbinds"):
            raise PackError("%s:%d: error: connection binds / unbinds are not "
                            "packed yet" % (godot_display_path(path),
                                            s["line"]))
        if int(a.get("flags", 0)) & ~3:
            raise PackError("%s:%d: error: connection flags %s are not packed "
                            "yet (deferred and persist are)" % (
                                godot_display_path(path), s["line"],
                                a.get("flags")))
        conns.append({"signal": a.get("signal"), "from": ends[0],
                      "to": ends[1], "method": a.get("method"),
                      "where": where})
    root.conns = conns if parent_is_top(stack) else None
    return root


def _resolver(proj, path, secs):
    """(ext_file, res_value) for one resource file: `ExtResource("id")` to
    (type, absolute path), and a property value with its sub-resources and
    `.tres` resources resolved to dicts (`_type`, `_where`, their props)."""
    ext = {}
    subs = {}
    for s in secs:
        a = s["attrs"]
        if s["tag"] == "ext_resource":
            ext[str(a.get("id"))] = (a.get("type"), a.get("path"), a.get("uid"))
        elif s["tag"] == "sub_resource":
            subs[str(a.get("id"))] = s
    done = {}

    def ext_file(call, where):
        rid = str(call.args[0]) if call.args else ""
        if rid not in ext:
            raise PackError("%s: error: no ext_resource id %s" % (where, rid))
        ty, rpath, uid = ext[rid]
        return ty, proj.resolve(rpath, uid, where)

    def res_value(v, where):
        if isinstance(v, GdCall) and v.name == "SubResource":
            rid = str(v.args[0]) if v.args else ""
            if rid not in subs:
                raise PackError("%s: error: no sub_resource id %s"
                                % (where, rid))
            if rid not in done:
                sec = subs[rid]
                d = {"_type": sec["attrs"].get("type"),
                     "_where": (path, sec["line"])}
                done[rid] = d
                for k, pv in sec["props"].items():
                    d[k] = res_value(pv, "%s:%d" % (
                        godot_display_path(path), sec["prop_lines"][k]))
            return done[rid]
        if isinstance(v, GdCall) and v.name == "ExtResource":
            ty, fp = ext_file(v, where)
            if fp.endswith(".tres"):
                return _load_tres(proj, fp)
            return {"_type": ty, "_path": fp, "_where": None}
        if isinstance(v, list):
            return [res_value(x, where) for x in v]
        if isinstance(v, dict):
            return {k: res_value(x, where) for k, x in v.items()}
        return v

    return ext_file, res_value


def _load_tres(proj, path, _cache={}):
    """A `.tres` resource as a dict: its `[resource]` properties, typed by
    the `gd_resource` header, sub-resources resolved."""
    key = (proj.root, path, os.path.getmtime(path))
    if key in _cache:
        return _cache[key]
    secs = parse_resource(_read(path), path)
    _ext_file, res_value = _resolver(proj, path, secs)
    head = next((s for s in secs if s["tag"] == "gd_resource"), None)
    d = {"_type": head["attrs"].get("type") if head else None,
         "_where": (path, head["line"] if head else 1), "_path": path}
    _cache[key] = d
    for s in secs:
        if s["tag"] == "resource":
            for k, v in s["props"].items():
                d[k] = res_value(v, "%s:%d" % (
                    godot_display_path(path), s["prop_lines"][k]))
    return d


def parent_is_top(stack):
    return not stack


def _rebase(node, name, node_path, parent):
    """An instanced scene's root becomes the instancing node. It keeps the
    name it has in its own scene as its class: every instance of crate.tscn
    is one class, `Crate`, however the instances are named."""
    if node.instance_of is None:
        node.instance_of = node.name
    node.name = name
    node.parent = parent

    def walk(n, p):
        n.path = p
        for c in n.children:
            walk(c, p + "/" + c.name if p else c.name)
    walk(node, node_path)


def _index(node, by_path):
    by_path[node.path] = node
    for c in node.children:
        _index(c, by_path)


def _preorder(node):
    yield node
    for c in node.children:
        for n in _preorder(c):
            yield n


# ---------------------------------------------------------------------------
# Scene objects (unity_pack's object dicts)
# ---------------------------------------------------------------------------

def exported_members(text):
    """Names of the `[Export]` fields and properties a C# script declares."""
    scan = cs2cpp._blank(text)
    out = []
    for m in re.finditer(
            r"\[\s*Export\s*(?:\([^\]]*\))?\s*\]\s*"
            r"(?:\[[^\]]*\]\s*)*"
            r"(?:(?:public|private|protected|internal|static|readonly|new)\s+)*"
            r"[\w.]+(?:\s*<[^>]*>)?(?:\s*\[\s*\])?\??\s+(\w+)", scan):
        out.append(m.group(1))
    return out


def _script_class(path):
    """Godot's rule: a C# node script declares the class its file names."""
    stem = os.path.splitext(os.path.basename(path))[0]
    scan = cs2cpp._blank(_read(path))
    for kind, name, _s, _b, _c in cs2cpp._find_types(scan):
        if kind == "class" and name == stem:
            return name
    raise PackError(
        "%s(1,1): error CS0246: a Godot C# script must declare the class "
        "its file is named for (`%s`)" % (godot_display_path(path), stem))


def _field_value(v, node, key, cls):
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float, str)):
        return v
    fpath, line = node.prop_lines[key]
    if isinstance(v, dict) and "_type" in v:
        what = v["_type"] or "resource"
    elif isinstance(v, GdCall):
        what = v.name
    else:
        what = type(v).__name__
    raise PackError(
        "%s:%d: error: `%s.%s` is a %s; exported %s values are not packed "
        "yet (numbers, bools and strings are)" % (
            godot_display_path(fpath), line, cls, key, what, what))


def scene_objects(proj, scene_path, scene_index=0):
    """Objects for one scene, in tree order."""
    root = _load_tree(proj, scene_path)
    objects = []
    obj_of = {}  # node -> its object
    xf2 = {}   # node -> global 2D affine
    xf3 = {}   # node -> (basis rows, origin)
    exports = {}
    for node in _preorder(root):
        p = node.parent
        t = node.type
        props = node.props
        if _is_3d_type(t):
            tr = props.get("transform")
            basis = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
            origin = (0.0, 0.0, 0.0)
            if isinstance(tr, GdCall) and tr.name == "Transform3D" \
                    and tr.args and len(tr.args) == 12:
                v = [float(x) for x in tr.args]
                basis = ((v[0], v[1], v[2]), (v[3], v[4], v[5]),
                         (v[6], v[7], v[8]))
                origin = (v[9], v[10], v[11])
            local = (basis, origin)
            if p in xf3 and not props.get("top_level"):
                pb, po = xf3[p]
                g_basis = _mat3_mul(pb, basis)
                g_origin = tuple(po[r] + sum(pb[r][k] * origin[k]
                                             for k in range(3))
                                 for r in range(3))
                glob = (g_basis, g_origin)
            else:
                glob = local
            xf3[node] = glob
            pos = glob[1]
            local_pos = origin
            rot = _quat_from_basis(glob[0])
            local_rot = _quat_from_basis(basis)
            local_scale = tuple(
                math.sqrt(sum(basis[r][c] ** 2 for r in range(3)))
                for c in range(3))
        else:
            lp = _vec(props.get("position"), 2, (0.0, 0.0))
            lr = float(props.get("rotation") or 0.0)
            ls = _vec(props.get("scale"), 2, (1.0, 1.0))
            spatial = t in _NODE2D_TYPES or "position" in props
            local = _affine2(lp, lr, ls)
            if p in xf2 and not props.get("top_level"):
                glob = _mul2(xf2[p], local)
            else:
                glob = local
            if spatial:
                xf2[node] = glob
            pos = (glob[0][2], glob[1][2], 0.0)
            local_pos = (lp[0], lp[1], 0.0)
            gr = math.atan2(glob[1][0], glob[0][0])
            rot = (0.0, 0.0, math.sin(gr / 2), math.cos(gr / 2))
            local_rot = (0.0, 0.0, math.sin(lr / 2), math.cos(lr / 2))
            local_scale = (ls[0], ls[1], 1.0)
        fields = {}
        if node.script:
            cls = _script_class(node.script)
            if node.script not in exports:
                exports[node.script] = exported_members(_read(node.script))
            for k in exports[node.script]:
                if k in props:
                    fields[k] = _field_value(props[k], node, k, cls)
        else:
            cls = node.instance_of or node.name
        obj_of[node] = len(objects)
        objects.append({
            "name": node.name,
            "class": cls,
            "script": node.script,
            "pos": tuple(float(x) for x in pos),
            "local_pos": tuple(float(x) for x in local_pos),
            "rot": rot,
            "local_rot": local_rot,
            "local_scale": local_scale,
            "fields": fields,
            "sprite": None,
            "active": 1,
            "scene": scene_index,
            "godot_type": t,
            "godot_path": node.path,
            "godot_groups": list(node.groups),
        })
    drop = _fold_physics(proj, root, objects, obj_of, xf2)
    _wire_signals(root, objects, obj_of, scene_index)
    return [o for i, o in enumerate(objects) if i not in drop]


_BODIES_2D = frozenset(("RigidBody2D", "CharacterBody2D", "StaticBody2D",
                        "AnimatableBody2D", "Area2D"))
_BODIES_3D = frozenset(("RigidBody3D", "CharacterBody3D", "StaticBody3D",
                        "AnimatableBody3D", "Area3D", "VehicleBody3D",
                        "PhysicalBone3D", "SoftBody3D"))
_SHAPE_NODES_REFUSED = frozenset(("CollisionPolygon2D", "CollisionShape3D",
                                  "CollisionPolygon3D"))


def _scene_error(node, msg, key=None):
    if key is not None and key in node.prop_lines:
        fpath, line = node.prop_lines[key]
    else:
        fpath, line = node.scene, node.line
    return PackError("%s:%d: error: %s" % (godot_display_path(fpath), line,
                                           msg))


def _scene_warning(node, msg, key=None):
    if key is not None and key in node.prop_lines:
        fpath, line = node.prop_lines[key]
    else:
        fpath, line = node.scene, node.line
    sys.stderr.write("godot_pack: %s:%d: warning: %s\n" % (
        godot_display_path(fpath), line, msg))


def _material(node):
    """(friction, bounce, rough, absorbent) of a body's PhysicsMaterial."""
    m = node.props.get("physics_material_override")
    if not isinstance(m, dict):
        return 1.0, 0.0, 0, 0   # PhysicsMaterial's defaults
    return (float(m.get("friction", 1.0)), float(m.get("bounce", 0.0)),
            1 if m.get("rough") else 0, 1 if m.get("absorbent") else 0)


def _collider(body, shape_node, bxf, sxf):
    """A CollisionShape2D as a packed Collider2D row of *body*."""
    shape = shape_node.props.get("shape")
    kind = shape.get("_type") if isinstance(shape, dict) else None
    if kind not in ("RectangleShape2D", "CircleShape2D"):
        raise _scene_error(
            shape_node, "`%s` is a %s; RectangleShape2D and CircleShape2D are "
            "packed" % (shape_node.name, kind), "shape")
    if shape_node.props.get("one_way_collision"):
        raise _scene_error(shape_node, "one-way collision is not packed yet",
                           "one_way_collision")
    # Rotated like the shape; offset from the body origin, in the shape's
    # frame, as Box2D-Packed rotates it back.
    rz = math.atan2(sxf[1][0], sxf[0][0])
    c, s = math.cos(rz), math.sin(rz)
    dx, dy = sxf[0][2] - bxf[0][2], sxf[1][2] - bxf[1][2]
    sx = math.hypot(sxf[0][0], sxf[1][0])
    sy = math.hypot(sxf[0][1], sxf[1][1])
    if kind == "RectangleShape2D":
        size = _vec(shape.get("size"), 2, (20.0, 20.0))
        hw, hh = abs(size[0]) * sx * 0.5, abs(size[1]) * sy * 0.5
    else:
        hw = hh = float(shape.get("radius", 10.0)) * max(sx, sy)
    friction, bounce, rough, absorbent = _material(body)
    return {
        "kind": "box" if kind == "RectangleShape2D" else "circle",
        "enabled": 1,
        "is_trigger": 1 if body.type == "Area2D" else 0,
        "ox": c * dx + s * dy, "oy": -s * dx + c * dy,
        "cos_z": c, "sin_z": s, "hw": hw, "hh": hh,
        "friction": friction, "bounciness": bounce,
        # Box2D-Packed's Godot mode reads the combine columns as flags.
        "friction_combine": rough, "bounce_combine": absorbent,
    }


def _rigidbody(body, settings):
    """A body node as a packed Rigidbody2D row (None for a static body)."""
    t = body.type
    props = body.props
    if t in ("StaticBody2D", "Area2D"):
        v = _vec(props.get("constant_linear_velocity"), 2, (0.0, 0.0))
        if v != (0.0, 0.0):
            raise _scene_error(body, "constant_linear_velocity is not packed "
                               "yet", "constant_linear_velocity")
        return None
    if t in ("CharacterBody2D", "AnimatableBody2D"):
        # Moved by its script; pushes, is not pushed.
        return {"body_type": 1, "mass": 1.0, "gravity_scale": 0.0,
                "linear_damping": 0.0, "vel_x": 0.0, "vel_y": 0.0}
    freeze_mode = int(props.get("freeze_mode", 0))  # 0 static, 1 kinematic
    body_type = (2 if freeze_mode == 0 else 1) if props.get("freeze") else 0
    damp = float(props.get("linear_damp", 0.0))
    if int(props.get("linear_damp_mode", 0)) == 0:   # combine
        damp += settings["default_linear_damp"]
    v = _vec(props.get("linear_velocity"), 2, (0.0, 0.0))
    if body_type == 0 and not props.get("lock_rotation"):
        _scene_warning(
            body, "`%s` is a RigidBody2D that may rotate; Box2D-Packed locks "
            "body rotation (set lock_rotation = true to say so)" % body.name)
    return {"body_type": body_type,
            "mass": float(props.get("mass", 1.0)),
            "gravity_scale": float(props.get("gravity_scale", 1.0)),
            "linear_damping": damp, "vel_x": v[0], "vel_y": v[1]}


def _fold_physics(proj, root, objects, obj_of, xf2):
    """Body nodes take their CollisionShape2D child as their collider, as a
    Unity GameObject has its components. Returns the object indices of the
    shape nodes, which are not objects of their own."""
    settings = physics_settings(proj.root)
    drop = set()
    for node in _preorder(root):
        t = node.type
        if t in _BODIES_3D or t in _SHAPE_NODES_REFUSED:
            raise _scene_error(node, "`%s` (%s) is not packed yet; 2D bodies "
                               "with CollisionShape2D are" % (node.name, t))
        if t == "CollisionShape2D" and not node.script:
            drop.add(obj_of[node])   # a shape outside a body does nothing
        if t not in _BODIES_2D:
            continue
        for key in ("collision_layer", "collision_mask"):
            if int(node.props.get(key, 1)) != 1:
                raise _scene_error(node, "%s is not packed yet (every body is "
                                   "on layer 1, masking layer 1)" % key, key)
        if t == "Area2D":
            for key in ("gravity_space_override", "linear_damp_space_override"):
                if int(node.props.get(key, 0)) != 0:
                    raise _scene_error(node, "Area2D %s is not packed yet"
                                       % key, key)
        shapes = [c for c in node.children if c.type == "CollisionShape2D"
                  and not c.props.get("disabled")
                  and c.props.get("shape") is not None]
        if len(shapes) > 1:
            raise _scene_error(shapes[1], "`%s` has a second CollisionShape2D;"
                               " one shape a body is packed" % node.name)
        o = objects[obj_of[node]]
        o["rigidbody2d"] = _rigidbody(node, settings)
        if shapes:
            o["collider2d"] = _collider(node, shapes[0], xf2[node],
                                        xf2[shapes[0]])
    return drop


# ---------------------------------------------------------------------------
# Signals: body_entered / body_exited / area_entered / area_exited
# ---------------------------------------------------------------------------

#: The physics signals packed, as Godot names them and as C# events.
_SIGNALS = {
    "body_entered": ("BodyEntered", 0, "body"),
    "body_exited": ("BodyExited", 2, "body"),
    "area_entered": ("AreaEntered", 0, "area"),
    "area_exited": ("AreaExited", 2, "area"),
}
_EVENT_SIGNAL = {v[0]: k for k, v in _SIGNALS.items()}
#: Godot signals as C# events that are not packed yet.
_EVENTS_REFUSED = frozenset((
    "BodyShapeEntered", "BodyShapeExited", "AreaShapeEntered",
    "AreaShapeExited", "Timeout", "Pressed", "Toggled", "AnimationFinished",
    "Finished", "TreeEntered", "TreeExiting", "TreeExited", "Ready",
    "Renamed", "VisibilityChanged", "InputEvent", "MouseEntered",
    "MouseExited", "SleepingStateChanged", "ScreenEntered", "ScreenExited",
    "ChildEnteredTree", "ChildExitingTree", "ValueChanged", "TextChanged",
    "FocusEntered", "FocusExited", "GuiInput", "Resized",
))
#: What `body is T` can name besides the project's classes.
_GODOT_TYPE_CHAIN = {
    "RigidBody2D": ("RigidBody2D", "PhysicsBody2D"),
    "CharacterBody2D": ("CharacterBody2D", "PhysicsBody2D"),
    "StaticBody2D": ("StaticBody2D", "PhysicsBody2D"),
    "AnimatableBody2D": ("AnimatableBody2D", "StaticBody2D", "PhysicsBody2D"),
    "Area2D": ("Area2D",),
}
_TYPE_CHAIN_TAIL = ("CollisionObject2D", "Node2D", "CanvasItem", "Node",
                    "GodotObject")


def _method_bodies(scan):
    """(name, params, body start, body end) of each method in *scan*."""
    out = []
    for m in re.finditer(r"\b(\w+)\s*\(([^()]*)\)\s*\{", scan):
        if m.group(1) in ("if", "for", "while", "switch", "catch", "using",
                          "lock", "foreach", "fixed"):
            continue
        depth = 0
        for j in range(m.end() - 1, len(scan)):
            if scan[j] == "{":
                depth += 1
            elif scan[j] == "}":
                depth -= 1
                if depth == 0:
                    out.append((m.group(1), m.group(2), m.end(), j))
                    break
    return out


def script_signal_connections(path, text):
    """`BodyEntered += Handler;` in a script's `_Ready` / `_EnterTree`:
    [(signal, method, index)]. A packed signal wired any other way, or
    anywhere else, and a Godot signal not packed yet, are refused."""
    scan = cs2cpp._blank(text)
    declared = set(re.findall(
        r"\b[\w.]+(?:\s*<[^>]*>)?(?:\s*\[\s*\])?\??\s+(\w+)\s*[;=({]", scan))
    bodies = _method_bodies(scan)
    out = []
    for m in re.finditer(r"(?<![.\w])(?:this\s*\.\s*)?([A-Z]\w*)\s*([-+])=",
                         scan):
        event = m.group(1)
        if event in declared:
            continue
        if event in _EVENTS_REFUSED:
            _refuse(path, text, m.start(), None,
                    "the %s signal is not packed yet (BodyEntered, "
                    "BodyExited, AreaEntered and AreaExited are)" % event)
        if event not in _EVENT_SIGNAL:
            continue
        where = next((b for b in bodies if b[2] <= m.start() < b[3]), None)
        allowed = ("_Ready", "_EnterTree") if m.group(2) == "+" else (
            "_ExitTree",)
        rm = re.compile(r"\s*(\w+)\s*;").match(scan, m.end())
        if where is None or where[0] not in allowed or not rm:
            _refuse(path, text, m.start(), None,
                    "`%s %s=` is packed as `%s %s= Method;` in %s" % (
                        event, m.group(2), event, m.group(2),
                        " or ".join(allowed)))
        if m.group(2) == "+":
            out.append((_EVENT_SIGNAL[event], rm.group(1), m.start(),
                        rm.end()))
        else:
            out.append((None, rm.group(1), m.start(), rm.end()))
    return out


def _script_method_params(script, method):
    """Parameter lists of *method* in *script* (all overloads)."""
    scan = cs2cpp._blank(_read(script))
    return [ps for name, ps, _s, _e in _method_bodies(scan) if name == method]


def _wire_signals(root, objects, obj_of, scene_index):
    """Resolve the scene's and its scripts' signal connections onto the
    sending objects (`godot_signals`) and the handlers' objects
    (`godot_handlers`)."""
    conns = list(root.conns or [])
    for node in _preorder(root):
        if not node.script:
            continue
        text = _read(node.script)
        for sig, method, idx, _end in script_signal_connections(
                node.script, text):
            if sig:
                conns.append({"signal": sig, "from": node, "to": node,
                              "method": method,
                              "where": (node.script, _line_of(text, idx))})
    for c in conns:
        fpath, line = c["where"]
        at = "%s:%d" % (godot_display_path(fpath), line)
        sig, src, dst, method = c["signal"], c["from"], c["to"], c["method"]
        if sig not in _SIGNALS:
            raise PackError("%s: error: the %s signal is not packed yet "
                            "(body_entered, body_exited, area_entered and "
                            "area_exited are)" % (at, sig))
        kind = _SIGNALS[sig][2]
        ok = src.type == "Area2D" or (kind == "body"
                                      and src.type == "RigidBody2D")
        if not ok:
            raise PackError("%s: error: `%s` is a %s, which has no %s signal"
                            " packed" % (at, src.name, src.type, sig))
        if src.type == "RigidBody2D" and not (
                src.props.get("contact_monitor")
                and int(src.props.get("max_contacts_reported", 0)) > 0):
            sys.stderr.write(
                "godot_pack: %s: warning: `%s` does not report contacts "
                "(contact_monitor and max_contacts_reported), so Godot never "
                "sends %s; not connected\n" % (at, src.name, sig))
            continue
        if src.type == "Area2D":
            if src.props.get("monitorable") is False:
                raise PackError("%s: error: an Area2D that is not monitorable "
                                "is not packed yet" % at)
            if src.props.get("monitoring") is False:
                sys.stderr.write(
                    "godot_pack: %s: warning: `%s` is not monitoring, so "
                    "Godot never sends %s; not connected\n"
                    % (at, src.name, sig))
                continue
        if not objects[obj_of[src]].get("collider2d"):
            sys.stderr.write(
                "godot_pack: %s: warning: `%s` has no CollisionShape2D, so "
                "Godot never sends %s; not connected\n" % (at, src.name, sig))
            continue
        if not dst.script:
            raise PackError("%s: error: `%s` has no script to receive %s"
                            % (at, dst.name, sig))
        params = _script_method_params(dst.script, method)
        if len(params) != 1 or len([x for x in params[0].split(",")
                                    if x.strip()]) != 1:
            raise PackError(
                "%s: error: %s needs one method `%s(Node %s)` in %s" % (
                    at, sig, method, kind, godot_display_path(dst.script)))
        so = objects[obj_of[src]]
        so.setdefault("godot_signals", []).append({
            "signal": sig, "kind": _SIGNALS[sig][1], "other": kind,
            "target": (scene_index, dst.path), "method": method})
        do = objects[obj_of[dst]]
        hs = do.setdefault("godot_handlers", [])
        if method not in hs:
            hs.append(method)


def resolve_signals(plan):
    """The plan's signal connections, sender and target as (class, index)
    of the packed instances, for `emit_signal_dispatch`."""
    where = {}
    for cname, cl in sorted(plan["classes"].items()):
        for i, o in enumerate(cl.get("instances") or []):
            if "godot_path" in o:
                where[(o.get("scene", 0), o["godot_path"])] = (cname, i, o)
    out = []
    for (_k, (cname, i, o)) in sorted(where.items()):
        for sg in o.get("godot_signals") or []:
            tc, ti, _to = where[sg["target"]]
            out.append({"from_class": cname, "from_inst": i,
                        "kind": sg["kind"], "other": sg["other"],
                        "to_class": tc, "to_inst": ti,
                        "method": sg["method"], "signal": sg["signal"]})
    return out


#: The node a physics signal passes, in a handler: its collider's index.
#: `body is T`, `body.IsInGroup(g)` and `body.Name` are these calls.
BINDINGS = [
    cs2cpp.Binding("GodotSignals.IsA", "GodotSignals_IsA"),
    cs2cpp.Binding("GodotSignals.InGroup", "GodotSignals_InGroup"),
    cs2cpp.Binding("GodotSignals.NameOf", "GodotSignals_NameOf"),
]


#: Calls that return a string, for string concatenation (which may run
#: before or after the bindings, so both spellings).
STRING_CALLS = ("GodotSignals_NameOf(", "GodotSignals.NameOf(")


def emit_signal_decls(p):
    """Prototypes, before the script methods that call them."""
    p("/* Godot: the node a physics signal passes (godot_pack.py) */")
    p("static int GodotSignals_IsA(int ci, const char *type);")
    p("static int GodotSignals_InGroup(int ci, const char *group);")
    p("static const char *GodotSignals_NameOf(int ci);")
    p("")


def _c_str(s):
    return '"%s"' % str(s).replace("\\", "\\\\").replace('"', '\\"')


def emit_signal_dispatch(p, plan, col2d_list, c_ident, go_checks,
                         script_guard):
    """GodotSignals_* over the collider table, and `_godot_signal`, which
    `_col2d_send_msg` calls for each pair's Enter (0) and Exit (2).

    *c_ident* is unity_pack's C identifier for a class; *go_checks* is True
    when the engine tracks destroyed objects (`_engine_go_of_<Class>`);
    *script_guard* wraps a script call (unity_pack's setjmp guard)."""
    owners = {}
    for cname, cl in plan["classes"].items():
        for i, o in enumerate(cl.get("instances") or []):
            owners[(cname, i)] = o
    rows = []
    for ci, c in enumerate(col2d_list):
        o = owners.get((c["owner_class"], c["owner_inst"])) or {}
        chain = (c["owner_class"],) + _GODOT_TYPE_CHAIN.get(
            o.get("godot_type"), ()) + _TYPE_CHAIN_TAIL
        rows.append((ci, c, o, chain))
    n = max(1, len(rows))
    p("/* Godot: the node a physics signal passes is its collider's index */")
    p("static const char *const _godot_col2d_name[%d] = { %s };" % (
        n, ", ".join(_c_str(o.get("name") or "") for _ci, _c, o, _ch in rows)
        or '""'))
    p("static int GodotSignals_IsA(int ci, const char *type) {")
    p("    switch (ci) {")
    for ci, _c, _o, chain in rows:
        p("    case %d: return %s;" % (ci, " || ".join(
            "strcmp(type, %s) == 0" % _c_str(t) for t in chain)))
    p("    default: return 0;")
    p("    }")
    p("}")
    p("static int GodotSignals_InGroup(int ci, const char *group) {")
    p("    switch (ci) {")
    for ci, _c, o, _ch in rows:
        gs = o.get("godot_groups") or []
        if gs:
            p("    case %d: return %s;" % (ci, " || ".join(
                "strcmp(group, %s) == 0" % _c_str(g) for g in gs)))
    p("    default: return 0;")
    p("    }")
    p("}")
    p("static const char *GodotSignals_NameOf(int ci) {")
    p("    if (ci < 0 || ci >= %d) return \"\";" % len(rows))
    p("    return _godot_col2d_name[ci];")
    p("}")
    p("")
    signals = plan.get("godot_signals") or []
    col_of = {}
    for ci, c, _o, _ch in rows:
        col_of.setdefault((c["owner_class"], c["owner_inst"]), ci)

    def go_of(cname, inst):
        gi = (owners.get((cname, inst)) or {}).get("go_index")
        return -1 if gi is None else int(gi)

    def alive(cname, inst):
        gi = go_of(cname, inst)
        return None if gi < 0 else "!_engine_go_destroyed[%d]" % gi

    p("/* Godot signals: body_entered / body_exited / area_entered / "
      "area_exited */")
    if go_checks:
        # A freed node sends and receives nothing more.
        p("static const int _godot_col2d_go[%d] = { %s };" % (n, ", ".join(
            str(go_of(c["owner_class"], c["owner_inst"]))
            for _ci, c, _o, _ch in rows) or "-1"))
    p("static void _godot_signal(int ci_self, int ci_other, int kind) {")
    p("    int other_area = _Collider2D_is_trigger[ci_other] != 0;")
    if go_checks:
        p("    int og = _godot_col2d_go[ci_other];")
        p("    if (og >= 0 && _engine_go_destroyed[og]) return;")
    p("    (void)other_area;")
    p("    switch (ci_self) {")
    by_ci = {}
    for sg in signals:
        ci = col_of.get((sg["from_class"], sg["from_inst"]))
        if ci is not None:
            by_ci.setdefault(ci, []).append(sg)
    for ci in sorted(by_ci):
        p("    case %d:" % ci)
        for sg in by_ci[ci]:
            cond = ["kind == %d" % sg["kind"],
                    "other_area" if sg["other"] == "area" else "!other_area"]
            if go_checks:
                for ck in (alive(sg["from_class"], sg["from_inst"]),
                           alive(sg["to_class"], sg["to_inst"])):
                    if ck and ck not in cond:
                        cond.append(ck)
            p("        /* %s.%s -> %s.%s */" % (
                sg["from_class"], sg["signal"], sg["to_class"], sg["method"]))
            p("        if (%s) {" % " && ".join(cond))
            for line in script_guard("%s_%s(%du, ci_other);" % (
                    c_ident(sg["to_class"]), sg["method"], sg["to_inst"])):
                p("            " + line)
            p("        }")
        p("        break;")
    p("    default: break;")
    p("    }")
    p("}")
    p("")


def load_godot_scenes(root):
    """(objects, scene list) for the project's main scene(s)."""
    proj = _Project(root)
    GODOT_ROOT[0] = proj.root
    scenes = _main_scenes(proj)
    if not scenes:
        raise PackError("no .tscn scenes under %s" % proj.root)
    _progress("packing %d Godot scene(s)" % len(scenes))
    objects = []
    for si, p in enumerate(scenes):
        _progress("  scene %d/%d %s" % (si + 1, len(scenes),
                                        godot_display_path(p)))
        objects.extend(scene_objects(proj, p, si))
    names = [{"name": os.path.splitext(os.path.basename(p))[0],
              "path": godot_display_path(p)} for p in scenes]
    return objects, names


# ---------------------------------------------------------------------------
# Godot C# -> the Unity-shaped subset unity_pack lowers
# ---------------------------------------------------------------------------

#: Godot node and object types a script can derive from.
_GODOT_BASES = frozenset(_NODE2D_TYPES | {
    "GodotObject", "Object", "RefCounted", "Resource", "Node", "CanvasItem",
    "Node3D", "CharacterBody3D", "RigidBody3D", "StaticBody3D", "Area3D",
    "MeshInstance3D", "Camera3D", "Control", "Label", "Button", "Panel",
    "Container", "CanvasLayer", "Timer", "AnimationPlayer",
    "AudioStreamPlayer", "Viewport", "SubViewport", "Window",
})

_LIFECYCLE = {
    "_Ready": ("Start", None),
    "_EnterTree": ("Awake", None),
    "_ExitTree": ("OnDestroy", None),
    "_Process": ("Update", "Time.deltaTime"),
    "_PhysicsProcess": ("FixedUpdate", "Time.fixedDeltaTime"),
}

#: Godot API with no packed meaning yet: refused where it is used.
_REFUSED = [
    (r"\bGetNode(?:OrNull)?\s*[<(]", "GetNode"),
    (r"\bGetParent\s*[<(]", "GetParent"),
    (r"\bGetChildren?\s*[<(]", "GetChild"),
    (r"\bGetTree\s*\(", "GetTree"),
    (r"\bAddChild\s*\(", "AddChild"),
    (r"\bEmitSignal\s*\(", "EmitSignal"),
    (r"\bConnect\s*\(", "Connect"),
    (r"\[\s*Signal\s*\]", "[Signal]"),
    (r"\bInput\s*\.\s*\w+", "Input"),
    (r"\b_(?:Unhandled)?(?:Key)?Input\s*\(", "_Input"),
    (r"\b_Draw\s*\(", "_Draw"),
    (r"(?<![.\w])(?:this\s*\.\s*)?(?:Global)?(?:Rotation|RotationDegrees|Scale|Skew|Transform)\b",
     "Rotation / Scale / Transform"),
    (r"(?<![.\w])(?:this\s*\.\s*)?(?:Velocity|MoveAndSlide|MoveAndCollide|IsOnFloor)\b",
     "CharacterBody2D"),
    (r"(?<![.\w])(?:this\s*\.\s*)?(?:Visible|Modulate|ZIndex|Show|Hide)\b",
     "CanvasItem"),
    (r"\bGD\s*\.\s*(?!Print\b)\w+", "GD"),
    (r"\bPackedScene\b|\bResourceLoader\b|\bInstantiate\s*[<(]", "PackedScene"),
    (r"\bGodot\s*\.\s*Collections\b", "Godot.Collections"),
]


def _refuse(path, text, idx, what, message=None):
    raise PackError(cs2cpp.cs_diag(
        path, text, idx, "CS8000",
        message or "`%s` (Godot API) is not packed yet" % what,
        display_path=godot_display_path))


def _apply(text, edits):
    """Splice (start, end, new) edits; each keeps the newlines it replaces,
    so every later line stays where it was."""
    out = []
    last = 0
    for s, e, new in sorted(edits):
        if s < last:
            continue
        out.append(text[last:s])
        out.append(new + "\n" * text.count("\n", s, e))
        last = e
    out.append(text[last:])
    return "".join(out)


def _pad(new, old_len):
    """*new* padded to *old_len* so columns after it are unmoved too."""
    return new + " " * max(0, old_len - len(new))


def adapt_csharp(path, text, project_types=(), handlers=()):
    """Godot C# *text* as the Unity-shaped subset, lines unchanged.

    *project_types* are the classes the project declares: a base named
    there is the project's own, anything else a script derives from with
    `using Godot` is a Godot type. *handlers* are the script's methods a
    physics signal calls: each is public, and its node parameter is the
    other collider's index, used through `is`, `IsInGroup` and `Name`.
    """
    project_types = set(project_types)
    scan = cs2cpp._blank(text)
    # A member the script declares itself is its own, not Godot's.
    declared = set(re.findall(
        r"\b[\w.]+(?:\s*<[^>]*>)?(?:\s*\[\s*\])?\??\s+(\w+)\s*[;=({]", scan))
    for pat, what in _REFUSED:
        for m in re.finditer(pat, scan):
            word = re.search(r"\w+(?=\s*[<(]?\s*$)|\w+$",
                             m.group(0).rstrip("<( \t"))
            if word and word.group(0) in declared:
                continue
            _refuse(path, text, m.start(), what)

    edits = []
    # `BodyEntered += OnBodyEntered;` is wiring, which godot_pack resolved.
    for _sig, _method, s0, s1 in script_signal_connections(path, text):
        edits.append((s0, s1, " " * (s1 - s0)))
    handler_params = {}
    for h in handlers:
        hm = list(re.finditer(
            r"\b((?:(?:public|private|protected|internal|virtual)\s+)*)"
            r"void\s+%s\s*\(\s*([\w.]+\??)\s+(\w+)\s*\)" % re.escape(h), scan))
        if len(hm) != 1:
            _refuse(path, text, hm[1].start() if hm else 0,
                    "signal handler `%s` declared %d times" % (h, len(hm)))
        m = hm[0]
        edits.append((m.start(1), m.end(1), _pad("public ", m.end(1) - m.start(1))))
        edits.append((m.start(2), m.end(2), _pad("int", m.end(2) - m.start(2))))
        handler_params[h] = m.group(3)
    uses_godot = re.search(r"\busing\s+Godot\s*;", scan) is not None
    for m in re.finditer(r"\busing\s+Godot\s*;", scan):
        edits.append((m.start(), m.end(),
                      _pad("using UnityEngine;", m.end() - m.start())))

    # Node classes: `public partial class Player : Node2D` -> MonoBehaviour.
    # `partial` is Godot's source generator's; any other partial class is
    # left for cs2cpp to refuse.
    for m in re.finditer(
            r"\b((?:(?:public|internal|sealed|abstract|partial)\s+)*)"
            r"class\s+(\w+)\s*:\s*((?:Godot\s*\.\s*)?\w+)", scan):
        base = re.sub(r"\s", "", m.group(3))
        base_name = base.split(".")[-1]
        godot_base = base.startswith("Godot.") or (
            base_name in _GODOT_BASES) or (
            uses_godot and base_name not in project_types
            and base_name[:1].isupper() and not base_name.startswith("I"))
        if not godot_base:
            continue
        mods = re.sub(r"\bpartial\s+", "", m.group(1))
        new = "%sclass %s : MonoBehaviour" % (mods, m.group(2))
        edits.append((m.start(), m.end(), _pad(new, m.end() - m.start())))

    # Godot attributes with no packed meaning: [Export], [ExportGroup(..)],
    # [GlobalClass], [Tool], [Icon(..)]. Exported auto-properties become
    # fields (their values come from the scene, as a field's do).
    for m in re.finditer(
            r"\[\s*(?:Export\w*|GlobalClass|Tool|Icon)\s*(?:\([^\]]*\))?\s*\]",
            scan):
        edits.append((m.start(), m.end(), " " * (m.end() - m.start())))
        pm = re.compile(
            r"\s*(?:\[[^\]]*\]\s*)*"
            r"(?:(?:public|private|protected|internal|static|new)\s+)*"
            r"[\w.]+(?:\s*<[^>]*>)?(?:\s*\[\s*\])?\??\s+\w+\s*"
            r"(\{\s*get\s*;\s*(?:(?:private|protected|internal)\s+)?set\s*;\s*\})"
            r"(\s*=)?").match(scan, m.end())
        if pm:
            s, e = pm.span(1)
            new = "" if pm.group(2) else ";"
            edits.append((s, e, _pad(new, e - s)))

    # Lifecycle: `public override void _Process(double delta) {`
    for m in re.finditer(
            r"\b(?:(?:public|protected|private|internal)\s+)?override\s+"
            r"void\s+(_\w+)\s*\(([^)]*)\)\s*\{", scan):
        name = m.group(1)
        if name not in _LIFECYCLE:
            continue
        unity, delta = _LIFECYCLE[name]
        params = m.group(2).strip()
        new = "public void %s() {" % unity
        if delta:
            pm = re.fullmatch(r"(?:double|float)\s+(\w+)", params)
            if not pm:
                _refuse(path, text, m.start(), "%s(%s)" % (name, params))
            new += " float %s = (float)%s;" % (pm.group(1), delta)
        edits.append((m.start(), m.end(), new))

    # GD.Print(a, b) -> Console.WriteLine("" + a + b): Godot concatenates.
    for m in re.finditer(r"\bGD\s*\.\s*Print\s*\(", scan):
        close = _close_paren(scan, m.end() - 1)
        if close is None:
            continue
        args = cs2cpp.split_call_args(text[m.end():close])
        joined = " + ".join(['""'] + ["(%s)" % a.strip() for a in args
                                      if a.strip()])
        edits.append((m.start(), close + 1,
                      "System.Console.WriteLine(%s)" % joined))

    # QueueFree() on this node -> Destroy(gameObject)
    for m in re.finditer(r"(?<![.\w])(?:this\s*\.\s*)?QueueFree\s*\(\s*\)",
                         scan):
        edits.append((m.start(), m.end(), "Destroy(gameObject)"))

    text = _apply(text, edits)

    # Second pass, on the rewritten text: position and vector members.
    scan = cs2cpp._blank(text)
    edits = []
    for m in re.finditer(
            r"(?<![.\w])(?:this\s*\.\s*)?(GlobalPosition|Position)\b", scan):
        edits.append((m.start(), m.end(),
                      "transform.position" if m.group(1) == "GlobalPosition"
                      else "transform.localPosition"))
    consts = {"Zero": "Vector%d.zero", "One": "Vector%d.one",
              "Up": "new Vector%d(0, -1%s)", "Down": "new Vector%d(0, 1%s)",
              "Left": "new Vector%d(-1, 0%s)",
              "Right": "new Vector%d(1, 0%s)"}
    for m in re.finditer(r"\bVector([23])\s*\.\s*(Zero|One|Up|Down|Left|Right)\b",
                         scan):
        d = int(m.group(1))
        tpl = consts[m.group(2)]
        new = tpl % ((d,) if "%s" not in tpl else (d, ", 0" if d == 3 else ""))
        edits.append((m.start(), m.end(), new))
    text = _apply(text, edits)

    # Vector components are X / Y / Z in Godot, x / y / z in the subset.
    scan = cs2cpp._blank(text)
    vec_names = set(re.findall(r"\bVector[23]\s+(\w+)\b", scan))
    names = ["transform\\s*\\.\\s*(?:localPosition|position)"] + [
        re.escape(n) for n in sorted(vec_names)]
    edits = []
    for m in re.finditer(r"(?<![.\w])(%s)\s*\.\s*([XYZ])\b" % "|".join(names),
                         scan):
        s = m.start(2)
        edits.append((s, s + 1, m.group(2).lower()))
    text = _apply(text, edits)
    return _lower_signal_params(path, text, handler_params)


def _lower_signal_params(path, text, handler_params):
    """In each handler, the node parameter's `is T`, `IsInGroup(g)` and
    `Name` as GodotSignals calls; any other use is refused."""
    scan = cs2cpp._blank(text)
    edits = []
    for name, ps, b0, b1 in _method_bodies(scan):
        param = handler_params.get(name)
        if not param:
            continue
        pe = re.escape(param)
        body = scan[b0:b1]
        done = []
        for pat, fmt in (
                (r"(?<![.\w])%s\s+is\s+not\s+([A-Z]\w*)\b" % pe,
                 '!GodotSignals.IsA(%s, "%s")'),
                (r"(?<![.\w])%s\s+is\s+([A-Z]\w*)\b" % pe,
                 'GodotSignals.IsA(%s, "%s")')):
            for m in re.finditer(pat, body):
                if any(a <= m.start() < b for a, b in done):
                    continue
                if re.match(r"\s*[A-Za-z_]", body[m.end():]):
                    _refuse(path, text, b0 + m.start(), None,
                            "`%s is %s <name>` is not packed yet (`%s is %s` "
                            "is)" % (param, m.group(1), param, m.group(1)))
                done.append((m.start(), m.end()))
                edits.append((b0 + m.start(), b0 + m.end(),
                              fmt % (param, m.group(1))))
        for m in re.finditer(r"(?<![.\w])%s\s*\.\s*IsInGroup\s*\(" % pe, body):
            done.append((m.start(), m.end()))
            edits.append((b0 + m.start(), b0 + m.end(),
                          "GodotSignals.InGroup(%s, " % param))
        for m in re.finditer(r"(?<![.\w])%s\s*\.\s*Name\b" % pe, body):
            if re.match(r"\s*(?:[=!]=|\.)", body[m.end():]):
                _refuse(path, text, b0 + m.start(), None,
                        "`%s.Name` compared or used further is not packed "
                        "yet; use `is` or `IsInGroup`" % param)
            done.append((m.start(), m.end()))
            edits.append((b0 + m.start(), b0 + m.end(),
                          "GodotSignals.NameOf(%s)" % param))
        for m in re.finditer(r"(?<![.\w])%s\b" % pe, body):
            if not any(a <= m.start() < b for a, b in done):
                _refuse(path, text, b0 + m.start(), None,
                        "`%s` is the node a signal passes: `is`, `IsInGroup` "
                        "and `Name` are packed, this use is not yet" % param)
    return _apply(text, edits)


def _close_paren(scan, open_idx):
    depth = 0
    for j in range(open_idx, len(scan)):
        if scan[j] == "(":
            depth += 1
        elif scan[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return None


# ---------------------------------------------------------------------------
# unity_pack entry points
# ---------------------------------------------------------------------------

def analyze_scripts(root, objects, analyze_script):
    """unity_pack's analyses for a Godot project: each scene script as the
    Unity-shaped subset (`adapt_csharp`) through *analyze_script*, plus one
    for every scriptless class, as for a Unity scene."""
    GODOT_ROOT[0] = os.path.abspath(root)
    project_types = set()
    for p in _walk(os.path.abspath(root), (".cs",)):
        for _k, name, _s, _b, _c in cs2cpp._find_types(cs2cpp._blank(_read(p))):
            project_types.add(name)
    scripts = sorted({o["script"] for o in objects if o.get("script")})
    _progress("analyzing %d Godot C# script(s)" % len(scripts))
    handlers = {}
    for o in objects:
        if o.get("script"):
            handlers.setdefault(o["script"], set()).update(
                o.get("godot_handlers") or ())
    analyses = [analyze_script(p, text=adapt_csharp(
        p, _read(p), project_types, sorted(handlers.get(p, ()))))
        for p in scripts]
    have = {c["name"] for a in analyses for c in a["classes"]}
    for o in objects:
        if o["class"] in have:
            continue
        analyses.append({
            "path": "<scene:%s>" % o["name"],
            "apis": set(),
            "spawns": False,
            "uses_z": abs(o["pos"][2]) > 1e-6,
            "writes_pos": False,
            "classes": [{
                "name": o["class"], "kind": "class",
                "fields": [{"ty": "int", "name": k} for k in o["fields"]],
                "methods": [], "refs": [], "path": None,
            }],
            "literals": [],
        })
        have.add(o["class"])
    return analyses


def parse_tscn(text, path=None):
    """Objects of one .tscn. With *path*, resources resolve against the
    project around it; with only *text*, the scene stands alone."""
    import tempfile
    if path is not None:
        root = os.path.dirname(os.path.abspath(path))
        while root != os.path.dirname(root) and not os.path.isfile(
                os.path.join(root, "project.godot")):
            root = os.path.dirname(root)
        if not os.path.isfile(os.path.join(root, "project.godot")):
            root = os.path.dirname(os.path.abspath(path))
        return scene_objects(_Project(root), path)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "scene.tscn")
        with open(p, "w") as f:
            f.write(text)
        return scene_objects(_Project(d), p)


def main(argv=None):
    """`godot_pack.py <project> [unity_pack options]`: unity_pack's CLI,
    for a Godot project."""
    args = list(sys.argv[1:] if argv is None else argv)
    project = next((a for a in args if not a.startswith("-")), None)
    if project is None or not is_godot_project(project):
        sys.stderr.write(
            "usage: godot_pack.py <godot project> [-o DIR] [--force] "
            "[--strict] [--physics-inject] [--box2d PATH]\n"
            "  (a directory holding project.godot)\n")
        return 2
    import tools.unity_pack as unity_pack
    saved = sys.argv
    sys.argv = [saved[0]] + args
    try:
        return unity_pack.main()
    finally:
        sys.argv = saved


if __name__ == "__main__":
    sys.exit(main() or 0)
