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
import copy
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
    "BOOL_CALLS",
    "STRING_CALLS",
    "adapt_csharp",
    "camera_view_center",
    "clear_color",
    "analyze_scripts",
    "emit_signal_decls",
    "emit_signal_dispatch",
    "exported_members",
    "godot_display_path",
    "godot_fingerprint_paths",
    "godot_screen_size",
    "godot_window_size",
    "input_map",
    "input_plan",
    "emit_input",
    "spawn_classes",
    "spawn_templates",
    "DEFAULT_SPAWN_BUDGET",
    "export_types",
    "godot_ref_id",
    "node_ref_sites",
    "emit_custom_decls",
    "emit_custom_dispatch",
    "lower_emits",
    "resolve_custom_signals",
    "script_signals",
    "script_base",
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
        if name == "Object" and self.peek() == "(":
            # An inline object, as project.godot's input events are:
            # Object(InputEventKey,"device":-1,"keycode":0,...)
            self.i += 1
            self.ws()
            cm = _IDENT_RE.match(self.text, self.i)
            if not cm:
                raise _ResourceError(self.i, "expected a class name")
            d = {"_type": cm.group(0)}
            self.i = cm.end()
            while self.peek() == ",":
                self.i += 1
                if self.peek() == ")":
                    break
                k = self.value()
                self.expect(":")
                d[k] = self.value()
            self.expect(")")
            return d
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
    if path and path.startswith("res://"):
        return path   # already shown as res:// (a diagnostic's second pass)
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


def godot_window_size(root):
    """The window's size: `window_width_override` / `_height_override`
    when set, else the viewport's."""
    s = _project_settings(os.path.abspath(root))
    w, h = godot_screen_size(root)
    return (int(s.get("display/window/size/window_width_override", 0)) or w,
            int(s.get("display/window/size/window_height_override", 0)) or h)


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
                      "where": where, "flags": int(a.get("flags", 0))})
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


# ---------------------------------------------------------------------------
# Node references: GetNode<T>(path), GetParent<T>(), exported node fields
# ---------------------------------------------------------------------------

_NODE_REF_RE = re.compile(
    r"(?<![.\w])(?:this\s*\.\s*)?(GetNode|GetNodeOrNull|GetParent)\s*"
    r"(?:<\s*([\w.]+)\s*>)?\s*\(")


def node_ref_sites(path, text):
    """Each `GetNode<T>("path")` / `GetNodeOrNull<T>(..)` / `GetParent<T>()`
    in a script, in file order: {"start", "end", "type", "path",
    "nullable"}. The path is a literal; T names the node's type."""
    scan = cs2cpp._blank(text)
    sites = []
    for m in _NODE_REF_RE.finditer(scan):
        close = _close_paren(scan, m.end() - 1)
        if close is None:
            _refuse(path, text, m.start(), m.group(1))
        arg = text[m.end():close].strip()
        call, ty = m.group(1), m.group(2)
        if call == "GetParent" and not ty and not arg and re.match(
                r"\s*\.\s*AddChild\s*\(", scan[close + 1:]):
            continue          # GetParent().AddChild(b): _lower_spawning's
        if not ty:
            _refuse(path, text, m.start(), call,
                    "%s<T>(..) with the node's type is packed (T: a script's "
                    "class, or Timer)" % call)
        if call == "GetParent":
            if arg:
                _refuse(path, text, m.start(), call)
            npath = ".."
        else:
            lm = (re.fullmatch(r'"((?:[^"\\]|\\.)*)"', arg)
                  or re.fullmatch(r'new\s+NodePath\s*\(\s*"((?:[^"\\]|'
                                  r'\\.)*)"\s*\)', arg))
            if not lm:
                _refuse(path, text, m.end(), call,
                        "a node path is packed as a string literal")
            npath = lm.group(1)
        sites.append({"start": m.start(), "end": close + 1,
                      "type": ty.split(".")[-1], "path": npath,
                      "nullable": call == "GetNodeOrNull"})
    return sites


def export_types(text):
    """{name: C# type} of a script's `[Export]` members."""
    scan = cs2cpp._blank(text)
    out = {}
    for m in re.finditer(
            r"\[\s*Export\s*(?:\([^\]]*\))?\s*\]\s*"
            r"(?:\[[^\]]*\]\s*)*"
            r"(?:(?:public|private|protected|internal|static|readonly|new)\s+)*"
            r"([\w.]+(?:\s*<[^>]*>)?(?:\s*\[\s*\])?\??)\s+(\w+)", scan):
        out[m.group(2)] = re.sub(r"\s", "", m.group(1)).split(".")[-1]
    return out


def _find_node(root, node, npath):
    """The node *npath* names from *node*, as Node::get_node does: relative
    (`Child/Grand`, `..`), absolute (`/root/<scene root>/..`), or a scene
    unique name (`%Name`). None when there is none."""
    if npath.startswith("%"):
        name = npath[1:].split("/", 1)
        hits = [n for n in _preorder(root) if n.name == name[0]
                and n.props.get("unique_name_in_owner")]
        if len(hits) != 1:
            return None
        cur = hits[0]
        rest = name[1] if len(name) > 1 else ""
    elif npath.startswith("/"):
        parts = [x for x in npath.split("/") if x]
        if len(parts) < 2 or parts[0] != "root" or parts[1] != root.name:
            return None
        cur, rest = root, "/".join(parts[2:])
    else:
        cur, rest = node, npath
    for seg in [x for x in rest.split("/") if x and x != "."]:
        if seg == "..":
            cur = cur.parent
        else:
            cur = next((c for c in cur.children if c.name == seg), None)
        if cur is None:
            return None
    return cur


def _is_timer(node):
    return node.type == "Timer" or bool(
        node.script and script_base(_read(node.script)) == "Timer")


_SHAPE_NODES = frozenset(("CollisionShape2D",))


def _link_hierarchy(root, objects, obj_of, xf2, scene_index, drop):
    """The parent chain as a runtime hierarchy: each object's `xf_id` is its
    node's, and a 2D node under a 2D node object has that parent's
    (`father_id`), its position then local -- its node's `position` -- and
    its world composed each frame from the parent's (unity_pack's live
    transform parents), through the parent's global rotation and scale
    (`godot_parent_basis`; scripts do not rotate or scale nodes). A body or
    an area is simulated in the world and has none, as a `top_level` node
    and one under a non-2D Node do not."""
    for node in _preorder(root):
        if node not in obj_of or obj_of[node] in drop:
            continue
        o = objects[obj_of[node]]
        o["xf_id"] = godot_ref_id(scene_index, node)
        par = node.parent
        # the scene tree's parent, which freeing follows (bodies included)
        if par is not None and par in obj_of and obj_of[par] not in drop:
            o["godot_tree_parent"] = godot_ref_id(scene_index, par)
        o["godot_tree_index"] = (scene_index, obj_of[node])   # tree order
        if (par is None or node not in xf2 or par not in xf2
                or node.props.get("top_level") or par not in obj_of
                or obj_of[par] in drop or node.type in _BODIES_2D
                or o.get("collider2d") or o.get("rigidbody2d")):
            continue
        o["father_id"] = godot_ref_id(scene_index, par)
        (a, b, _tx), (c, d, _ty) = xf2[par]
        o["godot_parent_basis"] = (a, b, c, d)


def _is_a(node, ty):
    """Whether *node* is a Godot *ty* (its type or one it derives from)."""
    if ty in ("Node", "GodotObject", "Object"):
        return True
    chain = (node.type,) + _GODOT_TYPE_CHAIN.get(node.type, ())
    if node.type in _BODIES_2D:
        chain += ("CollisionObject2D",)
    if node.type in _NODE2D_TYPES:
        chain += ("Node2D", "CanvasItem")
    if node.type == "Node2D":
        chain += ("CanvasItem",)
    return ty in chain


def godot_ref_id(scene_index, node):
    """A node's id, as a reference to it is seeded (`mb_ids`)."""
    return "godot:%d:%s" % (scene_index, node.path)


def _node_refs(root, objects, obj_of, scene_index):
    """Each script's node references, resolved for each node that runs it:
    a script class's as the field's instance (`object_refs`), a Timer's as
    its timer (`godot_timer_refs`, a slot once the timers are numbered)."""
    for node in _preorder(root):
        if node not in obj_of:
            continue
        o = objects[obj_of[node]]
        o["mb_ids"] = [godot_ref_id(scene_index, node)]
    for node in _preorder(root):
        if not node.script or node not in obj_of:
            continue
        o = objects[obj_of[node]]
        text = _read(node.script)
        refs = []
        for k, site in enumerate(node_ref_sites(node.script, text)):
            refs.append(("__gn%d" % k, site["type"], site["path"],
                         site["nullable"], ("site", site["start"])))
        types = export_types(text)
        for key, (fpath, line) in node.prop_lines.items():
            v = node.props.get(key)
            if isinstance(v, GdCall) and v.name == "NodePath" \
                    and key in types:
                refs.append((key, types[key], str(v.args[0] if v.args
                                                  else ""), False,
                             ("scene", (fpath, line))))
        if _is_timer(node):
            o.setdefault("godot_timer_refs", {})["__timer_self"] = (
                scene_index, node.path)
        spawned = (_FIRST_TEMPLATE[0] is not None
                   and scene_index >= _FIRST_TEMPLATE[0])
        for field, ty, npath, nullable, where in refs:
            target_scene = scene_index
            if spawned and npath.startswith("/") and _MAIN_ROOT[0]:
                # a spawned scene's absolute path: the main scene's node
                target = _find_node(_MAIN_ROOT[0], node, npath)
                target_scene = 0
            else:
                target = _find_node(root, node, npath)
                if target is None and spawned and _escapes(node, npath):
                    msg = ("\"%s\" from `%s` is above its spawned scene's "
                           "root, whose parent is known only when it is "
                           "added: not packed yet (an absolute path, "
                           "/root/Main/.., is)" % (npath, node.name))
                    if where[0] == "site":
                        _refuse(node.script, text, where[1], "GetNode", msg)
                    raise PackError("%s:%d: error: %s" % (
                        godot_display_path(where[1][0]), where[1][1], msg))

            def err(msg):
                if where[0] == "site":
                    _refuse(node.script, text, where[1], "GetNode", msg)
                raise PackError("%s:%d: error: %s" % (
                    godot_display_path(where[1][0]), where[1][1], msg))
            if target is None:
                if nullable:
                    continue
                err("no node \"%s\" from `%s` (%s)" % (
                    npath, node.name, "/".join(
                        ["/root", root.name] + ([node.path] if node.path
                                                else []))))
            if ty == "Timer":
                if not _is_timer(target):
                    err("\"%s\" is a %s, not a Timer" % (npath, target.type))
                o.setdefault("godot_timer_refs", {})[field] = (
                    target_scene, target.path)
                continue
            tcls = _script_class(target.script) if target.script else None
            if tcls != ty:
                if ty not in _GODOT_BASES:
                    err("\"%s\" from `%s` is %s; a node reference is packed "
                        "as its script's class, a Godot type it is, or Timer"
                        % (npath, node.name,
                           "a %s with script class %s" % (target.type, tcls)
                           if tcls else "a %s without a script"
                           % target.type))
                if not _is_a(target, ty):
                    err("\"%s\" from `%s` is a %s, not a %s" % (
                        npath, node.name, target.type, ty))
                if target not in obj_of or (
                        target.type in _SHAPE_NODES and not target.script):
                    err("\"%s\" from `%s` is a %s, which is not a node of "
                        "its own when packed" % (npath, node.name,
                                                 target.type))
                # A Godot type: the reference is the node's packed class,
                # its script's or (without one) the node's own.
                o.setdefault("godot_ref_classes", {})[field] = objects[
                    obj_of[target]]["class"]
            o.setdefault("object_refs", {})[field] = godot_ref_id(
                target_scene, target)


def _escapes(node, npath):
    """Whether relative *npath* climbs above its scene's root."""
    if npath.startswith("/") or npath.startswith("%"):
        return False
    cur = node
    for seg in [x for x in npath.split("/") if x and x != "."]:
        if seg == "..":
            if cur.parent is None:
                return True
            cur = cur.parent
        else:
            cur = next((c for c in cur.children if c.name == seg), cur)
    return False


def scene_objects(proj, scene_path, scene_index=0, cameras=None):
    """Objects for one scene, in tree order. Its current Camera2D, if it
    has one, is appended to *cameras*."""
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
        spawn_refs = {}
        if node.script:
            cls = _script_class(node.script)
            if node.script not in exports:
                exports[node.script] = exported_members(_read(node.script))
            for k in exports[node.script]:
                if k in props:
                    if isinstance(props[k], GdCall) \
                            and props[k].name == "NodePath":
                        continue      # a node reference (_node_refs)
                    if isinstance(props[k], dict) \
                            and props[k].get("_type") == "PackedScene":
                        # a scene to spawn: a template's index, once the
                        # templates are loaded (load_godot_scenes)
                        spawn_refs[k] = props[k]["_path"]
                        continue
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
            "godot_where": (node.scene, node.line),
            "godot_groups": list(node.groups),
            "godot_scene_refs": spawn_refs,
            # its global rotation and scale: a spawned child's frame
            "godot_global_basis": ((xf2[node][0][0], xf2[node][0][1],
                                    xf2[node][1][0], xf2[node][1][1])
                                   if node in xf2 else (1.0, 0.0, 0.0, 1.0)),
        })
    drop = _fold_physics(proj, root, objects, obj_of, xf2)
    _link_hierarchy(root, objects, obj_of, xf2, scene_index, drop)
    _fold_sprites(root, objects, obj_of, xf2)
    _fold_timers(root, objects, obj_of)
    _node_refs(root, objects, obj_of, scene_index)
    cam = _camera(proj, root, objects, obj_of, xf2, scene_index)
    if cam is not None and cameras is not None:
        cameras.append(cam)
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
    bodies = []
    for node in _preorder(root):
        t = node.type
        if t in _BODIES_3D or t in _SHAPE_NODES_REFUSED:
            raise _scene_error(node, "`%s` (%s) is not packed yet; 2D bodies "
                               "with CollisionShape2D are" % (node.name, t))
        if t == "CollisionShape2D" and not node.script:
            drop.add(obj_of[node])   # a shape outside a body does nothing
        if t not in _BODIES_2D:
            continue
        layer = int(node.props.get("collision_layer", 1)) & 0xffffffff
        mask = int(node.props.get("collision_mask", 1)) & 0xffffffff
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
            o["collider2d"]["godot_layer"] = layer
            o["collider2d"]["godot_mask"] = mask
            bodies.append((node, layer, mask))
    # Two dynamic bodies Godot pushes one way only (one's mask has the
    # other's layer, not the reverse): Box2D's contact pushes both.
    dyn = [(n, la, ma) for n, la, ma in bodies if n.type == "RigidBody2D"
           and not n.props.get("freeze")]
    for k, (a, la, ma) in enumerate(dyn):
        for b, lb, mb in dyn[k + 1:]:
            if bool(ma & lb) != bool(mb & la):
                _scene_warning(
                    a if ma & lb else b,
                    "`%s` and `%s`: Godot pushes only `%s` (its mask has the "
                    "other's layer, not the reverse); Box2D-Packed's contact "
                    "pushes both" % (a.name, b.name,
                                     a.name if ma & lb else b.name),
                    "collision_mask")
    return drop


# ---------------------------------------------------------------------------
# Sprite2D: the draw list's SpriteRenderer rows
# ---------------------------------------------------------------------------

#: CanvasItem types whose modulate, visibility and z_index pass to children.
_CANVAS_ITEM_TYPES = _NODE2D_TYPES

#: Drawing nodes not packed yet: refused, not drawn as nothing.
_DRAWING_REFUSED = {
    "AnimatedSprite2D": "AnimatedSprite2D is not packed yet; a Sprite2D "
                        "with hframes / vframes and frame is",
    "Polygon2D": "Polygon2D is not packed yet",
    "Line2D": "Line2D is not packed yet",
    "MeshInstance2D": "MeshInstance2D is not packed yet",
    "MultiMeshInstance2D": "MultiMeshInstance2D is not packed yet",
    "TileMap": "TileMap is not packed yet",
    "TileMapLayer": "TileMapLayer is not packed yet",
    "GPUParticles2D": "GPUParticles2D is not packed yet",
    "CPUParticles2D": "CPUParticles2D is not packed yet",
    "NinePatchRect": "NinePatchRect is not packed yet",
    "TextureRect": "TextureRect is not packed yet",
}

#: Godot's z_index range (RenderingServer::CANVAS_ITEM_Z_MIN / _MAX).
_Z_MIN, _Z_MAX = -4096, 4096


def _color(v, default=(1.0, 1.0, 1.0, 1.0)):
    if isinstance(v, GdCall) and v.name == "Color" and len(v.args) in (3, 4):
        c = [float(x) for x in v.args]
        return tuple(c) if len(c) == 4 else tuple(c) + (1.0,)
    return default


def _rect2(v):
    """(x, y, w, h) of a `Rect2(..)` / `Rect2i(..)`, else None."""
    if isinstance(v, GdCall) and v.name in ("Rect2", "Rect2i") \
            and len(v.args) == 4:
        return tuple(float(x) for x in v.args)
    return None


def _canvas_parent(node):
    """The CanvasItem a node's modulate, visibility and z are relative to."""
    p = node.parent
    return p if p is not None and p.type in _CANVAS_ITEM_TYPES else None


def _inherited(node):
    """(modulate, visible, z_index) of *node* as Godot draws it: modulate
    and visibility from every CanvasItem ancestor, z relative to its parent
    while z_as_relative."""
    chain = []
    n = node
    while n is not None:
        chain.append(n)
        n = _canvas_parent(n)
    mod = [1.0, 1.0, 1.0, 1.0]
    visible = True
    for n in chain:
        c = _color(n.props.get("modulate"))
        mod = [a * b for a, b in zip(mod, c)]
        if n.props.get("visible") is False:
            visible = False
    z = 0
    for n in reversed(chain):        # root first
        zi = int(n.props.get("z_index", 0))
        if n.props.get("z_as_relative", True) is False:
            z = zi
        else:
            z += zi
    z = max(_Z_MIN, min(_Z_MAX, z))
    return tuple(mod), visible, z


def _sprite_texture(node):
    """(png path, crop (x, y, w, h) from the top-left, or None) of a
    Sprite2D's `texture`: a PNG, or an AtlasTexture region of one."""
    tex = node.props.get("texture")
    if not isinstance(tex, dict):
        return None, None
    kind = tex.get("_type")
    crop = None
    if kind == "AtlasTexture":
        m = _rect2(tex.get("margin"))
        if m and any(m):
            raise _scene_error(node, "AtlasTexture margin is not packed yet",
                               "texture")
        crop = _rect2(tex.get("region"))
        tex = tex.get("atlas")
        if not isinstance(tex, dict):
            raise _scene_error(node, "AtlasTexture has no atlas", "texture")
        kind = tex.get("_type")
    if kind not in ("Texture2D", "CompressedTexture2D", "ImageTexture") \
            or not tex.get("_path"):
        raise _scene_error(node, "a %s texture is not packed yet; a PNG "
                           "Texture2D (or an AtlasTexture of one) is"
                           % kind, "texture")
    path = tex["_path"]
    if not path.lower().endswith(".png"):
        raise _scene_error(node, "%s: only PNG textures are packed yet"
                           % godot_display_path(path), "texture")
    return path, crop


def _load_texture(node, path, _cache={}):
    """(w, h, bottom-up RGBA) of a PNG, refused at *node*'s texture line
    when unity_pack's decoder cannot read it."""
    from tools.unity_pack_sprites import _load_png_rgba
    key = (path, os.path.getmtime(path))
    if key not in _cache:
        try:
            _cache[key] = _load_png_rgba(path)
        except (PackError, IOError, ValueError) as e:
            raise _scene_error(node, "%s: %s" % (
                godot_display_path(path),
                str(e).replace(path, "").rstrip(": ")
                or "cannot read the PNG"), "texture")
    return _cache[key]


def _sprite(node, xf):
    """A Sprite2D as a packed SpriteRenderer row, in Godot's pixels.

    The texture is a PNG, cropped by an AtlasTexture's region, then by
    `region_rect`, then to `frame` of `hframes` x `vframes`, as Godot's
    Sprite2D::_get_rects does. `offset` and `centered` place the rect;
    `flip_h` / `flip_v` mirror the texture in place."""
    from tools.unity_pack_sprites import _crop_rgba
    props = node.props
    path, atlas = _sprite_texture(node)
    if path is None:
        return None
    for key in ("material", "use_parent_material"):
        if props.get(key):
            raise _scene_error(node, "a CanvasItem %s is not packed yet"
                               % key, key)
    for key in ("show_behind_parent", "y_sort_enabled", "clip_children"):
        if props.get(key):
            raise _scene_error(node, "%s is not packed yet" % key, key)
    tw, th, rgba = _load_texture(node, path)
    # the source rect, top-left origin as Godot's
    base = atlas or (0.0, 0.0, float(tw), float(th))
    if props.get("region_enabled"):
        r = _rect2(props.get("region_rect")) or (0.0, 0.0, 0.0, 0.0)
        base = (base[0] + r[0], base[1] + r[1], r[2], r[3])
    hf = int(props.get("hframes", 1))
    vf = int(props.get("vframes", 1))
    if hf < 1 or vf < 1:
        raise _scene_error(node, "hframes and vframes must be at least 1",
                           "hframes" if hf < 1 else "vframes")
    frame = int(props.get("frame", 0))
    fc = props.get("frame_coords")
    if isinstance(fc, GdCall) and fc.name in ("Vector2i", "Vector2") \
            and "frame" not in props:
        frame = int(fc.args[1]) * hf + int(fc.args[0])
    if not 0 <= frame < hf * vf:
        raise _scene_error(node, "frame %d is outside %d x %d frames"
                           % (frame, hf, vf), "frame")
    fw, fh = base[2] / hf, base[3] / vf
    sx0 = base[0] + (frame % hf) * fw
    sy0 = base[1] + (frame // hf) * fh
    if (fw != int(fw) or fh != int(fh) or sx0 != int(sx0)
            or sy0 != int(sy0)):
        raise _scene_error(node, "the frame rect (%g, %g, %g, %g) is not "
                           "whole pixels" % (sx0, sy0, fw, fh), "texture")
    if fw < 1 or fh < 1 or sx0 < 0 or sy0 < 0 or sx0 + fw > tw \
            or sy0 + fh > th:
        raise _scene_error(node, "the frame rect (%g, %g, %g, %g) is "
                           "outside the %dx%d texture" % (
                               sx0, sy0, fw, fh, tw, th), "texture")
    fw, fh, sx0, sy0 = int(fw), int(fh), int(sx0), int(sy0)
    if (sx0, sy0, fw, fh) != (0, 0, tw, th):
        # unity_pack's pixels are bottom-up; its crop's y is too
        fw, fh, rgba = _crop_rgba(rgba, tw, th, sx0, th - sy0 - fh, fw, fh)
    # Local rect: `offset`, less half the frame when centered. Its centre is
    # the draw's, a constant offset from the node in the node's own frame.
    off = _vec(props.get("offset"), 2, (0.0, 0.0))
    cx = off[0] + (0.0 if props.get("centered", True) else fw * 0.5)
    cy = off[1] + (0.0 if props.get("centered", True) else fh * 0.5)
    (a, b, _tx), (c, d, _ty) = xf
    sxl = math.hypot(a, c)
    syl = math.hypot(b, d)
    if sxl < 1e-12 or syl < 1e-12:
        return None      # scaled to nothing: Godot draws nothing
    # The local-to-world basis, unit columns (the half extents carry the
    # scale). unity_pack's quad puts texture row 0 -- the image's bottom --
    # at local -y, which in Godot's y-down pixels is up: mirror local y so
    # the image's top is up. flip_h / flip_v mirror in place, as Godot's
    # negative dst size does.
    fx = -1.0 if props.get("flip_h") else 1.0
    fy = 1.0 if props.get("flip_v") else -1.0
    mod, visible, z = _inherited(node)
    smod = _color(props.get("self_modulate"))
    return {
        "godot": True,
        "sprite_guid": godot_display_path(path) + (
            "" if (sx0, sy0, fw, fh) == (0, 0, tw, th)
            else "#%d,%d,%d,%d" % (sx0, sy0, fw, fh)),
        "sprite_file_id": 0,
        "has_sprite": True,
        "tex_path": path,
        "tex_w": fw,
        "tex_h": fh,
        "tex_rgba": rgba,
        "pixels_per_unit": 1.0,
        "border": (0.0, 0.0, 0.0, 0.0),
        "half_w": fw * sxl * 0.5,
        "half_h": fh * syl * 0.5,
        "scale_x": sxl,
        "scale_y": syl,
        "m00": fx * a / sxl, "m10": fx * c / sxl,
        "m01": fy * b / syl, "m11": fy * d / syl,
        # the rect's centre, from the node's origin, in world pixels
        "draw_off_x": a * cx + b * cy,
        "draw_off_y": c * cx + d * cy,
        "r": mod[0] * smod[0], "g": mod[1] * smod[1],
        "b": mod[2] * smod[2], "a": mod[3] * smod[3],
        "enabled": 1 if visible else 0,
        "lit": 0,
        "godot_z": z,
    }


def _fold_sprites(root, objects, obj_of, xf2):
    """Each Sprite2D's texture as its object's SpriteRenderer row; drawing
    nodes not packed yet are refused at their scene line."""
    for node in _preorder(root):
        if node.type in _DRAWING_REFUSED:
            has = [k for k in ("sprite_frames", "texture", "tile_set",
                               "polygon", "points", "mesh", "multimesh")
                   if node.props.get(k) is not None]
            if has or node.type in ("GPUParticles2D", "CPUParticles2D"):
                raise _scene_error(node, "`%s`: %s" % (
                    node.name, _DRAWING_REFUSED[node.type]),
                    has[0] if has else None)
            continue
        if node.type != "Sprite2D" or node not in obj_of:
            continue
        sp = _sprite(node, xf2[node])
        if sp is not None:
            objects[obj_of[node]]["sprite"] = sp


def _order_sprites(objects):
    """Godot's painter's order: z_index, then tree order. The draw list
    sorts by (sorting layer, sorting order), a layer being 8 bits and an
    order 16 on the GPU, so a layer is a z's rank among the z values in
    use and an order is the sprite's place in the tree."""
    sprites = [o["sprite"] for o in objects if o.get("sprite")]
    zs = sorted(set(sp["godot_z"] for sp in sprites))
    if len(zs) > 256:
        raise PackError("%d distinct z_index values are drawn; 256 are "
                        "packed" % len(zs))
    if len(sprites) > 65536:
        raise PackError("%d Sprite2D nodes; 65536 are packed"
                        % len(sprites))
    rank = {z: i for i, z in enumerate(zs)}
    for i, sp in enumerate(sprites):
        sp["sorting_layer"] = rank[sp["godot_z"]]
        sp["sorting_order"] = i - 32768


# ---------------------------------------------------------------------------
# Camera2D: the view the draw list is seen through
# ---------------------------------------------------------------------------

#: Camera2D limits' defaults (Godot's +-10000000: no limit).
_NO_LIMIT = 10000000


def clear_color(root):
    """`rendering/environment/defaults/default_clear_color` (Godot's grey)."""
    s = _project_settings(os.path.abspath(root))
    c = _color(s.get("rendering/environment/defaults/default_clear_color"),
               (0.3, 0.3, 0.3, 1.0))
    return c[:3]


def camera_view_center(view, px, py):
    """Camera2D::get_camera_transform's screen centre for a camera node at
    (px, py): the screen rect anchored there, clamped to the limits (left,
    then right; top, then bottom), then moved by `offset`. *view* is a
    camera record's `godot_view`."""
    hw, hh = view["half"]
    x0 = px - (hw if view["drag_center"] else 0.0)
    y0 = py - (hh if view["drag_center"] else 0.0)
    left, top, right, bottom = view["limits"]
    if x0 < left:
        x0 = left
    if x0 + 2 * hw > right:
        x0 = right - 2 * hw
    if y0 < top:
        y0 = top
    if y0 + 2 * hh > bottom:
        y0 = bottom - 2 * hh
    return x0 + view["offset"][0] + hw, y0 + view["offset"][1] + hh


def _camera_record(name, view, center, bg):
    return {
        "name": name,
        "pos": (center[0], center[1], -10.0),
        "rot": (0.0, 0.0, 0.0, 1.0),
        "local_pos": (0.0, 0.0, 0.0),
        "father_id": None,
        "xf_id": None,
        "main": True,
        "orthographic": 1,
        "orthographic_size": view["half"][1],
        "bg_r": bg[0], "bg_g": bg[1], "bg_b": bg[2],
        "near_clip": 0.3,
        "far_clip": 1000.0,
        "godot_view": view,
    }


def viewport_camera(root):
    """The view with no Camera2D: the viewport's rect from the origin, y
    down."""
    w, h = godot_screen_size(root)
    view = {"half": (w * 0.5, h * 0.5), "drag_center": False,
            "offset": (0.0, 0.0),
            "limits": (-_NO_LIMIT, -_NO_LIMIT, _NO_LIMIT, _NO_LIMIT)}
    return _camera_record("(viewport)", view,
                          camera_view_center(view, 0.0, 0.0),
                          clear_color(root))


def _camera(proj, root, objects, obj_of, xf2, scene_index):
    """The scene's current Camera2D -- the first enabled one in the tree,
    as Godot makes current -- as a camera record, or None.

    It is followed at runtime when it can move: a camera with a script
    follows its own node; one without follows its nearest ancestor with a
    script or a body, keeping its offset from it."""
    cam = next((n for n in _preorder(root) if n.type == "Camera2D"
                and n.props.get("enabled", True) is not False
                and n in obj_of), None)
    if cam is None:
        return None
    props = cam.props
    for key in ("position_smoothing_enabled", "rotation_smoothing_enabled",
                "drag_horizontal_enabled", "drag_vertical_enabled"):
        if props.get(key):
            raise _scene_error(cam, "Camera2D %s is not packed yet" % key,
                               key)
    if props.get("custom_viewport") is not None:
        raise _scene_error(cam, "Camera2D custom_viewport is not packed yet",
                           "custom_viewport")
    xf = xf2[cam]
    if props.get("ignore_rotation", True) is False \
            and abs(math.atan2(xf[1][0], xf[0][0])) > 1e-9:
        raise _scene_error(cam, "a rotated Camera2D (ignore_rotation = "
                           "false) is not packed yet", "ignore_rotation")
    zoom = _vec(props.get("zoom"), 2, (1.0, 1.0))
    if zoom[0] <= 0 or zoom[1] <= 0:
        raise _scene_error(cam, "Camera2D zoom must be positive", "zoom")
    w, h = godot_screen_size(proj.root)
    limits = (
        float(props.get("limit_left", -_NO_LIMIT)),
        float(props.get("limit_top", -_NO_LIMIT)),
        float(props.get("limit_right", _NO_LIMIT)),
        float(props.get("limit_bottom", _NO_LIMIT)))
    if props.get("limit_enabled", True) is False:
        limits = (-_NO_LIMIT, -_NO_LIMIT, _NO_LIMIT, _NO_LIMIT)
    view = {
        "half": (w * 0.5 / zoom[0], h * 0.5 / zoom[1]),
        "drag_center": int(props.get("anchor_mode", 1)) == 1,
        "offset": _vec(props.get("offset"), 2, (0.0, 0.0)),
        "limits": limits,
    }
    px, py = xf[0][2], xf[1][2]
    rec = _camera_record(cam.name, view, camera_view_center(view, px, py),
                         clear_color(proj.root))
    # What moves it: its own scripted node, else its parent -- whose world
    # position the runtime hierarchy composes through all its ancestors, so
    # the camera follows whichever of them moves. Not under a 2D node, or
    # top_level, it stays where it is.
    target = cam if cam.script else None
    par = cam.parent
    if target is None and par is not None and par in obj_of \
            and par in xf2 and not props.get("top_level"):
        target = par
    if target is not None:
        rec["father_id"] = godot_ref_id(scene_index, target)
        tx, ty = (xf2[target][0][2], xf2[target][1][2]) \
            if target in xf2 else (0.0, 0.0)
        # The camera node's world offset from what moves it.
        rec["local_pos"] = (px - tx, py - ty, 0.0)
    rec["scene"] = scene_index
    return rec


# ---------------------------------------------------------------------------
# Input: the InputMap's actions, the keys and the first joypad
# ---------------------------------------------------------------------------

#: Godot keycodes (core/os/keyboard.h) -> the host's key names, the
#: `engine_keyboard_<name>` a window host sets (a Shift / Ctrl / Alt without
#: a location is either side).
_K = 1 << 22
_HOST_KEYS = dict(
    [(ord(c), c.lower()) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
    + [(ord(str(d)), "digit%d" % d) for d in range(10)]
    + [(0x20, "space"), (_K | 0x01, "escape"), (_K | 0x02, "tab"),
       (_K | 0x04, "backspace"), (_K | 0x05, "enter"),
       (_K | 0x0F, "leftArrow"), (_K | 0x10, "upArrow"),
       (_K | 0x11, "rightArrow"), (_K | 0x12, "downArrow"),
       (_K | 0x15, "Shift"), (_K | 0x16, "Ctrl"), (_K | 0x18, "Alt")])

#: C# `Key.X` names -> keycodes.
_CS_KEYS = dict(
    [(c, ord(c)) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
    + [("Key%d" % d, ord(str(d))) for d in range(10)]
    + [("Space", 0x20), ("Escape", _K | 0x01), ("Tab", _K | 0x02),
       ("Backspace", _K | 0x04), ("Enter", _K | 0x05),
       ("Left", _K | 0x0F), ("Up", _K | 0x10), ("Right", _K | 0x11),
       ("Down", _K | 0x12), ("Shift", _K | 0x15), ("Ctrl", _K | 0x16),
       ("Alt", _K | 0x18)])

#: JoyButton -> the host's button (GLFW's layout: A B X Y LB RB Back Start
#: Guide LThumb RThumb Up Right Down Left).
_JOY_BUTTONS = {0: 0, 1: 1, 2: 2, 3: 3, 4: 6, 5: 8, 6: 7, 7: 9, 8: 10,
                9: 4, 10: 5, 11: 11, 12: 13, 13: 14, 14: 12}
_CS_JOY_BUTTONS = {
    "A": 0, "B": 1, "X": 2, "Y": 3, "Back": 4, "Guide": 5, "Start": 6,
    "LeftStick": 7, "RightStick": 8, "LeftShoulder": 9, "RightShoulder": 10,
    "DpadUp": 11, "DpadDown": 12, "DpadLeft": 13, "DpadRight": 14}
_CS_JOY_AXES = {"LeftX": 0, "LeftY": 1, "RightX": 2, "RightY": 3,
                "TriggerLeft": 4, "TriggerRight": 5}

#: InputMap::DEFAULT_DEADZONE (a project's action) and
#: DEFAULT_TOGGLE_DEADZONE (the built-in ui_* actions).
_DEADZONE = 0.2
_BUILTIN_DEADZONE = 0.5


def _key(code):
    return {"_type": "InputEventKey", "keycode": code}


def _joy_button(b):
    return {"_type": "InputEventJoypadButton", "button_index": b}


def _joy_motion(axis, value):
    return {"_type": "InputEventJoypadMotion", "axis": axis,
            "axis_value": value}


#: Godot 4.4's built-in actions (InputMap::get_builtins) that have events a
#: host reports; KP Enter, Page Up / Down, Home, End are not reported.
_BUILTIN_ACTIONS = {
    "ui_accept": [_key(_K | 0x05), _key(0x20)],
    "ui_select": [_joy_button(3), _key(0x20)],
    "ui_cancel": [_key(_K | 0x01)],
    "ui_focus_next": [_key(_K | 0x02)],
    "ui_left": [_key(_K | 0x0F), _joy_button(13), _joy_motion(0, -1.0)],
    "ui_right": [_key(_K | 0x11), _joy_button(14), _joy_motion(0, 1.0)],
    "ui_up": [_key(_K | 0x10), _joy_button(11), _joy_motion(1, -1.0)],
    "ui_down": [_key(_K | 0x12), _joy_button(12), _joy_motion(1, 1.0)],
}


def input_map(root):
    """{action: {"deadzone", "events", "where"}}: the built-in ui_* actions,
    then the project's `[input]` section (which overrides them)."""
    out = {n: {"deadzone": _BUILTIN_DEADZONE, "events": list(ev),
               "where": None}
           for n, ev in _BUILTIN_ACTIONS.items()}
    p = os.path.join(os.path.abspath(root), "project.godot")
    if not os.path.isfile(p):
        return out
    for sec in parse_resource(_read(p), p):
        if sec["tag"] != "input":
            continue
        for name, v in sec["props"].items():
            if not isinstance(v, dict):
                continue
            out[name] = {"deadzone": float(v.get("deadzone", _DEADZONE)),
                         "events": [e for e in v.get("events") or []
                                    if isinstance(e, dict)],
                         "where": (p, sec["prop_lines"][name])}
    return out


class _InputPlan(object):
    """The actions a project's scripts read: each a slot of the engine's
    tables, with its events compiled to rows."""

    def __init__(self, root):
        self.root = root
        self.map = None
        self.slots = {}      # action (or a "<key ..>" pseudo-action) -> slot
        self.actions = []    # [(name, deadzone, rows)]
        self.keys = []       # host key names the rows read
        self.joypad = False

    def _map(self):
        if self.map is None:
            self.map = input_map(self.root)
        return self.map

    def _err(self, where, msg):
        if where is None:
            return PackError("<godot>: error: %s" % msg)
        return PackError("%s:%d: error: %s" % (
            godot_display_path(where[0]), where[1], msg))

    def _row(self, ev, name, where):
        """One event as a row: (kind, index, sign, modifier keys): kind 1 a
        key (index: the host's), 2 a joypad button, 3 a joypad axis."""
        t = ev.get("_type")
        dev = int(ev.get("device", -1))
        if t in ("InputEventJoypadButton", "InputEventJoypadMotion") \
                and dev not in (-1, 0):
            raise self._err(where, "action %r: joypad device %d is not "
                            "packed yet (the first joypad is)" % (name, dev))
        if t == "InputEventKey":
            code = int(ev.get("keycode") or 0) or int(
                ev.get("physical_keycode") or 0) or int(
                ev.get("key_label") or 0)
            host = _HOST_KEYS.get(code)
            if host is None:
                raise self._err(where, "action %r: key %s is not one a host "
                                "reports yet" % (name, code))
            if ev.get("meta_pressed") or ev.get(
                    "command_or_control_autoremap"):
                raise self._err(where, "action %r: a Meta / Command modifier "
                                "is not packed yet" % name)
            loc = int(ev.get("location", 0))
            if host in ("Shift", "Ctrl", "Alt"):
                sides = {0: ("left", "right"), 1: ("left",),
                         2: ("right",)}[loc]
                keys = [s + host for s in sides]
            else:
                keys = [host]
            mods = [m for m in ("Shift", "Ctrl", "Alt")
                    if ev.get(m.lower() + "_pressed")]
            return (1, keys, 0.0, mods)
        if t == "InputEventJoypadButton":
            b = int(ev.get("button_index", -1))
            if b not in _JOY_BUTTONS:
                raise self._err(where, "action %r: joypad button %d is not "
                                "packed yet" % (name, b))
            self.joypad = True
            return (2, _JOY_BUTTONS[b], 0.0, [])
        if t == "InputEventJoypadMotion":
            a = int(ev.get("axis", -1))
            if not 0 <= a <= 5:
                raise self._err(where, "action %r: joypad axis %d is not "
                                "packed yet" % (name, a))
            self.joypad = True
            return (3, a, float(ev.get("axis_value", 0.0)), [])
        raise self._err(where, "action %r: a %s is not packed yet (keys and "
                        "the first joypad are)" % (name, t))

    def _add(self, name, deadzone, events, where):
        rows = []
        for ev in events:
            kind, idx, sign, mods = self._row(ev, name, where)
            if kind == 1:
                for k in idx + ["left" + m for m in mods] + [
                        "right" + m for m in mods]:
                    if k not in self.keys:
                        self.keys.append(k)
            rows.append((kind, idx, sign, mods))
        self.slots[name] = len(self.actions)
        self.actions.append((name, deadzone, rows))
        return self.slots[name]

    def action(self, name):
        """The slot of InputMap action *name*, or None when there is none."""
        if name in self.slots:
            return self.slots[name]
        act = self._map().get(name)
        if act is None:
            return None
        return self._add(name, act["deadzone"], act["events"], act["where"])

    def deadzone(self, slot):
        return self.actions[slot][1]

    def key(self, code):
        """A pseudo-action pressed while key *code* is (`IsKeyPressed`)."""
        name = "<key %d>" % code
        if name not in self.slots:
            self._add(name, 0.5, [_key(code)], None)
        return self.slots[name]

    def joy_button(self, b):
        name = "<joy button %d>" % b
        if name not in self.slots:
            self._add(name, 0.5, [_joy_button(b)], None)
        return self.slots[name]


#: The project being analyzed's input plan (analyze_scripts resets it).
_INPUT = [None]

#: Node references across scripts, for the adapter (analyze_scripts fills
#: it): "members" {class: names it declares}; "classes" {(script, field):
#: the packed classes a Godot-typed reference resolves to}; "pos_written"
#: the classes a script moves through a reference.
_REFS = [{"members": {}, "classes": {}, "pos_written": set()}]


def _declared_names(scan):
    return set(re.findall(
        r"\b[\w.]+(?:\s*<[^>]*>)?(?:\s*\[\s*\])?\??\s+(\w+)\s*[;=({]",
        scan))


def input_plan():
    """For unity_pack: the actions the scripts read, or None."""
    ip = _INPUT[0]
    if ip is None or not ip.actions:
        return None
    return {"actions": ip.actions, "keys": list(ip.keys),
            "joypad": ip.joypad}


_INPUT_CALLS = {
    # C# member: (GodotInput call, number of action arguments)
    "IsActionPressed": ("Pressed", 1),
    "IsActionJustPressed": ("JustPressed", 1),
    "IsActionJustReleased": ("JustReleased", 1),
    "GetActionStrength": ("Strength", 1),
    "GetActionRawStrength": ("RawStrength", 1),
    "GetAxis": ("Axis", 2),
    "GetVector": ("Vector", 4),
}


def _lower_input(path, text, scan, edits):
    """`Input.IsActionPressed("jump")` and its kind as GodotInput calls on
    the action's slot. Each action is a string literal the InputMap has."""
    ip = _INPUT[0]
    for m in re.finditer(r"(?<![\w.])(?:Godot\s*\.\s*)?Input\s*\.\s*(\w+)",
                         scan):
        member = m.group(1)
        close = None
        rest = re.match(r"\s*\(", scan[m.end():])
        if rest:
            close = _close_paren(scan, m.end() + rest.end() - 1)
        if member not in _INPUT_CALLS and member not in (
                "IsKeyPressed", "IsPhysicalKeyPressed", "IsKeyLabelPressed",
                "IsJoyButtonPressed", "GetJoyAxis"):
            _refuse(path, text, m.start(), "Input.%s" % member)
        if close is None or ip is None:
            _refuse(path, text, m.start(), "Input.%s" % member)
        args = [a.strip() for a in cs2cpp.split_call_args(
            text[m.end() + rest.end():close])]
        at = m.end() + rest.end()

        def lit(a):
            lm = re.fullmatch(r'"((?:[^"\\]|\\.)*)"', a)
            if not lm:
                _refuse(path, text, at, "Input.%s" % member,
                        "an action name must be a string literal")
            return lm.group(1)

        def enum(a, kind, table):
            em = re.fullmatch(r"(?:Godot\s*\.\s*)?%s\s*\.\s*(\w+)" % kind, a)
            if not em or em.group(1) not in table:
                _refuse(path, text, at, "Input.%s" % member,
                        "`%s` is not a %s that is packed" % (a, kind))
            return table[em.group(1)]

        def device(a):
            if a not in ("0", "-1"):
                _refuse(path, text, at, "Input.%s" % member,
                        "joypad device %s is not packed yet (0 is)" % a)

        if member in _INPUT_CALLS:
            call, n = _INPUT_CALLS[member]
            extra = args[n:]
            if len(args) < n:
                _refuse(path, text, at, "Input.%s" % member)
            slots = []
            for a in args[:n]:
                name = lit(a)
                s = ip.action(name)
                if s is None:
                    _refuse(path, text, at, "Input.%s" % member,
                            "the InputMap has no action \"%s\"" % name)
                slots.append(str(s))
            if call == "Vector":
                if extra and extra[0] not in ("-1", "-1f", "-1.0f",
                                              "-1.0"):
                    dz = extra[0]
                else:
                    dz = repr(0.25 * sum(ip.deadzone(int(s))
                                         for s in slots)) + "f"
                slots.append(dz)
            elif extra and extra[0] not in ("false",):
                _refuse(path, text, at, "Input.%s" % member,
                        "exact_match is not packed yet")
            new = "GodotInput.%s(%s)" % (call, ", ".join(slots))
        elif member == "GetJoyAxis":
            device(args[0] if args else "")
            ip.joypad = True
            new = "GodotInput.JoyAxis(%d)" % enum(
                args[1] if len(args) > 1 else "", "JoyAxis", _CS_JOY_AXES)
        elif member == "IsJoyButtonPressed":
            device(args[0] if args else "")
            b = enum(args[1] if len(args) > 1 else "", "JoyButton",
                     _CS_JOY_BUTTONS)
            new = "GodotInput.Pressed(%d)" % ip.joy_button(b)
        else:
            code = enum(args[0] if args else "", "Key", _CS_KEYS)
            new = "GodotInput.Pressed(%d)" % ip.key(code)
        edits.append((m.start(), close + 1, new))


def emit_input(p, plan):
    """The engine's input: each action's pressed / strength / raw strength,
    evaluated once a tick (`_godot_input_latch`) over the host's keys and
    first joypad, by Godot's rules (Input's action cache and
    InputEventJoypadMotion::action_match)."""
    gi = plan["godot_input"]
    actions = gi["actions"]
    na = len(actions)
    rows = [(a, r) for a, (_n, _dz, rs) in enumerate(actions) for r in rs]
    ne = max(1, len(rows))
    starts, counts = [], []
    for a, (_n, _dz, rs) in enumerate(actions):
        starts.append(sum(len(x[2]) for x in actions[:a]))
        counts.append(len(rs))
    p("/* Godot input: the InputMap's actions (godot_pack.py) */")
    p("static const int _gin_start[%d] = { %s };" % (
        na, ", ".join(str(v) for v in starts)))
    p("static const int _gin_count[%d] = { %s };" % (
        na, ", ".join(str(v) for v in counts)))
    p("static const float _gin_dz[%d] = { %s };" % (
        na, ", ".join("%sf" % repr(float(dz)) for _n, dz, _r in actions)))
    p("static const int _gin_kind[%d] = { %s };" % (
        ne, ", ".join(str(r[0]) for _a, r in rows) or "0"))
    p("static const int _gin_index[%d] = { %s };" % (
        ne, ", ".join(str(r[1] if r[0] != 1 else 0) for _a, r in rows)
        or "0"))
    p("static const float _gin_sign[%d] = { %s };" % (
        ne, ", ".join("%sf" % repr(float(r[2])) for _a, r in rows)
        or "0.0f"))
    p("static unsigned char _gin_now[%d], _gin_was[%d];" % (na, na))
    p("static float _gin_str[%d], _gin_raw[%d];" % (na, na))

    def held(k):
        return "engine_keyboard_%s" % k
    p("/* a key row: its key (either side, for a Shift / Ctrl / Alt without a")
    p("   location) and the modifiers the event asks for */")
    p("static int _gin_key_row(int e) {")
    p("    if (!engine_keyboard_connected) return 0;")
    p("    switch (e) {")
    for e, (_a, r) in enumerate(rows):
        if r[0] != 1:
            continue
        cond = "(%s)" % " || ".join(held(k) for k in r[1])
        for m in r[3]:
            cond += " && (%s || %s)" % (held("left" + m), held("right" + m))
        p("    case %d: return %s;" % (e, cond))
    p("    default: return 0;")
    p("    }")
    p("}")
    if gi["joypad"]:
        p("/* JoyAxis: the host's y axes are up, Godot's down */")
        p("static float GodotInput_JoyAxis(int a) {")
        p("    switch (a) {")
        p("    case 0: return _gp_axis(0);")
        p("    case 1: return 0.f - _gp_axis(1); /* not -0 at rest */")
        p("    case 2: return _gp_axis(2);")
        p("    case 3: return 0.f - _gp_axis(3);")
        p("    case 4: return _gp_axis(4);")
        p("    case 5: return _gp_axis(5);")
        p("    default: return 0.f;")
        p("    }")
        p("}")
    p("static void _godot_input_latch(void) {")
    p("    int a, e, e1;")
    p("    for (a = 0; a < %d; a = a + 1) {" % na)
    p("        int pressed = 0;")
    p("        float str = 0.f, raw = 0.f, dz = _gin_dz[a];")
    p("        _gin_was[a] = _gin_now[a];")
    p("        e1 = _gin_start[a] + _gin_count[a];")
    p("        for (e = _gin_start[a]; e < e1; e = e + 1) {")
    p("            int on = 0;")
    p("            float s = 0.f, r = 0.f;")
    p("            if (_gin_kind[e] == 1) {")
    p("                on = _gin_key_row(e);")
    p("                s = on ? 1.f : 0.f;")
    p("                r = s;")
    if gi["joypad"]:
        p("            } else if (_gin_kind[e] == 2) {")
        p("                on = _gp_btn(_gin_index[e]);")
        p("                s = on ? 1.f : 0.f;")
        p("                r = s;")
        p("            } else if (_gin_kind[e] == 3) {")
        p("                /* InputEventJoypadMotion::action_match */")
        p("                float v = GodotInput_JoyAxis(_gin_index[e]);")
        p("                float av = v < 0.f ? -v : v;")
        p("                int same = ((_gin_sign[e] < 0.f) == (v < 0.f))"
          " || v == 0.f;")
        p("                on = same && av >= dz;")
        p("                if (on) {")
        p("                    s = dz >= 1.f ? 1.f : (av - dz) / (1.f - dz);")
        p("                    if (s < 0.f) s = 0.f;")
        p("                    if (s > 1.f) s = 1.f;")
        p("                }")
        p("                r = same ? av : 0.f;")
    p("            }")
    p("            if (on) pressed = 1;")
    p("            if (s > str) str = s;")
    p("            if (r > raw) raw = r;")
    p("        }")
    p("        _gin_now[a] = (unsigned char)pressed;")
    p("        _gin_str[a] = str;")
    p("        _gin_raw[a] = raw;")
    p("    }")
    p("}")
    p("static int GodotInput_Pressed(int a) { return _gin_now[a] != 0; }")
    p("static int GodotInput_JustPressed(int a) {")
    p("    return _gin_now[a] && !_gin_was[a];")
    p("}")
    p("static int GodotInput_JustReleased(int a) {")
    p("    return !_gin_now[a] && _gin_was[a];")
    p("}")
    p("static float GodotInput_Strength(int a) { return _gin_str[a]; }")
    p("static float GodotInput_RawStrength(int a) { return _gin_raw[a]; }")
    p("static float GodotInput_Axis(int neg, int pos) {")
    p("    return _gin_str[pos] - _gin_str[neg];")
    p("}")
    p("/* Input::get_vector: raw strengths, a circular deadzone, length <= 1 */")
    p("static Vector2 GodotInput_Vector(int nx, int px, int ny, int py,"
      " float dz) {")
    p("    float x = _gin_raw[px] - _gin_raw[nx];")
    p("    float y = _gin_raw[py] - _gin_raw[ny];")
    p("    float len = sqrtf(x * x + y * y);")
    p("    float k;")
    p("    if (len <= dz) return Vector2_make(0.f, 0.f);")
    p("    if (len > 1.f) return Vector2_make(x / len, y / len);")
    p("    k = (len - dz) / (1.f - dz) / len;")
    p("    return Vector2_make(x * k, y * k);")
    p("}")
    p("")


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
    # The script's own [Signal]s, and a Timer's timeout when it is one: the
    # events Godot's source generator gives it.
    events = dict(_EVENT_SIGNAL)
    events.update({n: n for n in script_signals(text)})
    if script_base(text) == "Timer":
        events["Timeout"] = "timeout"
    out = []
    for m in re.finditer(r"(?<![.\w])(?:this\s*\.\s*)?([A-Z]\w*)\s*([-+])=",
                         scan):
        event = m.group(1)
        if event in declared and event not in events:
            continue
        if event in _EVENTS_REFUSED and event not in events:
            _refuse(path, text, m.start(), None,
                    "the %s signal is not packed yet (BodyEntered, "
                    "BodyExited, AreaEntered, AreaExited, a Timer's Timeout "
                    "and the script's own [Signal]s are)" % event)
        if event not in events:
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
            out.append((events[event], rm.group(1), m.start(), rm.end()))
        else:
            out.append((None, rm.group(1), m.start(), rm.end()))
    return out


def _script_method_params(script, method):
    """Parameter lists of *method* in *script* (all overloads)."""
    scan = cs2cpp._blank(_read(script))
    return [ps for name, ps, _s, _e in _method_bodies(scan) if name == method]


#: A signal's parameter types, as a handler's C parameters are lowered.
_SIGNAL_PARAM_TYPES = {"int": "int", "float": "float", "bool": "int",
                       "string": "const char *"}


def script_signals(text):
    """{name: [(C# type, parameter)]} of a script's `[Signal]` delegates:
    `[Signal] public delegate void HitEventHandler(int damage);` is `Hit`."""
    out = {}
    scan = cs2cpp._blank(text)
    for m in re.finditer(
            r"\[\s*Signal\s*\]\s*(?:(?:public|private|protected|internal)"
            r"\s+)*delegate\s+void\s+(\w+?)EventHandler\s*\(([^)]*)\)\s*;",
            scan):
        params = []
        for a in m.group(2).split(","):
            a = a.strip()
            if a:
                ty, _sp, name = a.rpartition(" ")
                params.append((ty.strip(), name))
        out[m.group(1)] = params
    return out


def script_base(text):
    """The Godot type a node script derives from (its first class's base)."""
    m = re.search(r"\bclass\s+\w+\s*:\s*(?:Godot\s*\.\s*)?(\w+)",
                  cs2cpp._blank(text))
    return m.group(1) if m else None


def _custom_signal(src, sig, at):
    """The parameters of signal *sig* of node *src* -- a Timer's timeout (no
    parameters) or its script's own [Signal] -- else None."""
    if sig == "timeout" and (src.type == "Timer" or (
            src.script and script_base(_read(src.script)) == "Timer")):
        return []
    if src.script:
        sigs = script_signals(_read(src.script))
        if sig in sigs:
            for ty, name in sigs[sig]:
                if ty not in _SIGNAL_PARAM_TYPES:
                    raise PackError(
                        "%s: error: signal %s's parameter `%s %s`: int, float, "
                        "bool and string parameters are packed" % (at, sig, ty, name))
            return sigs[sig]
    return None


def _wire_custom(objects, obj_of, scene_index, src, dst, sig, method,
                 params, at, flags):
    """A connection of a Timer's timeout or a script's own signal."""
    if flags & 1:
        raise PackError("%s: error: a deferred connection of %s is not "
                        "packed yet" % (at, sig))
    if not dst.script:
        raise PackError("%s: error: `%s` has no script to receive %s"
                        % (at, dst.name, sig))
    got = _script_method_params(dst.script, method)
    if len(got) != 1:
        raise PackError("%s: error: %s needs one method `%s` in %s (found %d)"
                        % (at, sig, method, godot_display_path(dst.script),
                           len(got)))
    have = [x.strip().rpartition(" ")[0].strip()
            for x in got[0].split(",") if x.strip()]
    want = [ty for ty, _n in params]
    if have != want:
        raise PackError("%s: error: %s passes (%s); `%s` takes (%s)" % (
            at, sig, ", ".join(want), method, ", ".join(have)))
    so = objects[obj_of[src]]
    so.setdefault("godot_custom", []).append({
        "signal": sig, "target": (scene_index, dst.path), "method": method})
    do = objects[obj_of[dst]]
    hs = do.setdefault("godot_custom_handlers", [])
    if method not in hs:
        hs.append(method)


def _fold_timers(root, objects, obj_of):
    """Each Timer node's wait_time, one_shot and autostart on its object."""
    for node in _preorder(root):
        is_timer = node.type == "Timer" or (
            node.script and script_base(_read(node.script)) == "Timer")
        if not is_timer or node not in obj_of:
            continue
        props = node.props
        if int(props.get("process_callback", 1)) != 1:
            raise _scene_error(node, "a Timer on the physics process is not "
                               "packed yet (idle is)", "process_callback")
        if props.get("ignore_time_scale"):
            raise _scene_error(node, "Timer ignore_time_scale is not packed "
                               "yet", "ignore_time_scale")
        wait = float(props.get("wait_time", 1.0))
        if wait <= 0:
            raise _scene_error(node, "Timer wait_time must be positive",
                               "wait_time")
        objects[obj_of[node]]["godot_timer"] = {
            "wait": wait, "one_shot": 1 if props.get("one_shot") else 0,
            "autostart": 1 if props.get("autostart") else 0}


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
            params = _custom_signal(src, sig, at)
            if params is not None:
                _wire_custom(objects, obj_of, scene_index, src, dst, sig,
                             method, params, at, c.get("flags", 0))
                continue
            raise PackError("%s: error: the %s signal is not packed yet "
                            "(body_entered, body_exited, area_entered, "
                            "area_exited, a Timer's timeout and a script's "
                            "own [Signal]s are)" % (at, sig))
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
] + [cs2cpp.Binding("GodotPrint.Raw", "GodotPrint_Raw"),
      cs2cpp.Binding("GodotPrint.Err", "GodotPrint_Err")] + [
    cs2cpp.Binding("GodotInput." + n, "GodotInput_" + n) for n in (
    "Pressed", "JustPressed", "JustReleased", "Strength", "RawStrength",
    "Axis", "Vector", "JoyAxis")] + [
    cs2cpp.Binding("GodotVec." + n, "GodotVec_" + n) for n in (
        "Length", "LengthSquared", "Normalized", "Angle", "Abs",
        "IsZeroApprox", "Dot", "Cross", "DistanceTo", "DistanceSquaredTo",
        "AngleTo", "DirectionTo", "IsEqualApprox", "Rotated", "LimitLength",
        "Lerp", "MoveToward")] + [
    cs2cpp.Binding("GodotTimer." + n, "GodotTimer_" + n) for n in (
        "Start", "Stop", "IsStopped", "TimeLeft", "WaitTime", "OneShot",
        "Paused", "SetWaitTime", "SetOneShot", "SetPaused")]


#: C functions that return a C# bool, printed True / False.
BOOL_CALLS = frozenset((
    "GodotSignals_IsA", "GodotSignals_InGroup",
    "GodotInput_Pressed", "GodotInput_JustPressed", "GodotInput_JustReleased",
    "GodotTimer_IsStopped", "GodotTimer_OneShot", "GodotTimer_Paused",
    "GodotVec_IsZeroApprox", "GodotVec_IsEqualApprox"))


#: Calls that return a string, for string concatenation (which may run
#: before or after the bindings, so both spellings).
STRING_CALLS = ("GodotSignals_NameOf(", "GodotSignals.NameOf(",
                "GodotSignals.OwnName(", "GodotNodeName_")


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
    if plan.get("physics2d_layers"):
        # Area2D::collides_with: an area sees what its mask has the layer of
        p("    if (_Collider2D_is_trigger[ci_self]")
        p("        && !(_Collider2D_layer_bits[ci_other]"
          " & _Collider2D_mask_bits[ci_self]))")
        p("        return;")
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


# ---------------------------------------------------------------------------
# Spawning: PackedScene templates
# ---------------------------------------------------------------------------

#: The main scene's node tree, and the first template's scene index: a
#: spawned scene's absolute paths (`/root/Main/..`) are the main scene's.
_MAIN_ROOT = [None]
_FIRST_TEMPLATE = [None]

#: {(script, field): the classes the scenes an `[Export] PackedScene` field
#: is set to spawn} (load_godot_scenes fills it).
_SPAWN_FIELDS = [{}]

#: The project's PackedScene templates (load_godot_scenes fills it):
#: {scene path: {"class": the root's class, "index": its instance index}}.
_TEMPLATES = [{}]

_SCENE_LOAD_RE = re.compile(
    r'(?:\bGD|\bResourceLoader)\s*\.\s*Load\s*<\s*(?:Godot\s*\.\s*)?'
    r'PackedScene\s*>\s*\(\s*"([^"]*)"\s*\)'
    r'|\(\s*(?:Godot\s*\.\s*)?PackedScene\s*\)\s*(?:GD|ResourceLoader)'
    r'\s*\.\s*Load\s*\(\s*"([^"]*)"\s*\)'
    r'|(?:\bGD|\bResourceLoader)\s*\.\s*Load\s*\(\s*"([^"]*)"\s*\)\s*as\s+'
    r'(?:Godot\s*\.\s*)?PackedScene\b')


def script_scene_loads(text):
    """[(offset, res path)] of a script's `GD.Load<PackedScene>("res://..")`
    (and ResourceLoader's, a cast, an `as`): a scene to spawn, by a literal
    path."""
    out = []
    for m in _SCENE_LOAD_RE.finditer(text):
        path = m.group(1) or m.group(2) or m.group(3)
        out.append((m.start(), m.end(), path))
    return out


def _load_templates(proj, objects, first_index):
    """Every scene a script can spawn -- an `[Export] PackedScene`'s, a
    literal `GD.Load<PackedScene>` -- as template objects: its nodes,
    dormant (`godot_template`: not processed, drawn nor simulated), each
    spawn a copy of its root. Scenes its templates spawn are loaded too.
    Sets `_TEMPLATES` and each export's value (the template root's index)."""
    _TEMPLATES[0] = {}
    loaded = {}
    order = []

    def wanted(objs):
        for o in objs:
            for path in (o.get("godot_scene_refs") or {}).values():
                yield path
            if o.get("script"):
                text = _read(o["script"])
                for start, _e, rp in script_scene_loads(text):
                    try:
                        yield proj.resolve(rp)
                    except PackError:
                        line = text.count("\n", 0, start) + 1
                        raise PackError("%s:%d: error: no scene %s" % (
                            godot_display_path(o["script"]), line, rp))

    queue = [(path, None) for path in wanted(objects)]
    while queue:
        path, by = queue.pop(0)
        if path in loaded:
            continue
        _progress("  template %s" % godot_display_path(path))
        tobjs = scene_objects(proj, path, first_index + len(order), None)
        _check_template(path, tobjs)
        for o in tobjs:
            o["godot_template"] = path
        loaded[path] = tobjs
        order.append(path)
        objects.extend(tobjs)
        for more in wanted(tobjs):
            if more == path:
                raise PackError(
                    "%s: error: `%s` spawns its own scene, which is not "
                    "packed yet" % (godot_display_path(path),
                                    tobjs[0]["name"]))
            queue.append((more, path))
    # A spawned physics body's rows -- its collider, its Rigidbody2D, its
    # Box2D body -- are fixed when the project is packed: a pool of dormant
    # copies of it, as many as may be live at once, which a spawn takes
    # (and a freed one gives back). After every template, so the templates'
    # indices are unchanged.
    for t in order:
        tobjs = loaded[t]
        root = tobjs[0]
        n = _max_instances(root.get("script")) or DEFAULT_SPAWN_BUDGET
        for node in tobjs:
            # a body (the root) and a Timer: rows fixed at pack time
            if not (node.get("collider2d") or node.get("rigidbody2d")
                    or node.get("godot_timer")):
                continue
            for k in range(n):
                c = copy.deepcopy(node)
                c["godot_pool"] = t
                c["godot_path"] = "%s#%d" % (node["godot_path"], k)
                c["xf_id"] = "%s#%d" % (node.get("xf_id"), k)
                c["mb_ids"] = ["%s#%d" % (m, k)
                               for m in node.get("mb_ids") or []]
                for key in ("father_id", "godot_tree_parent", "object_refs",
                            "godot_parent_basis", "godot_custom",
                            "godot_timer_refs"):
                    c.pop(key, None)
                # its own physics signals (a spawned body's handler is its
                # own script's): wired to itself
                for sg in c.get("godot_signals") or []:
                    sg["target"] = (c.get("scene", 0), c["godot_path"])
                objects.append(c)
    # a template root's index in its class: the objects of that class
    # before it (the plan lists a class's instances in object order)
    seen = {}
    row = {}
    for o in objects:
        k = seen.get(o["class"], 0)
        seen[o["class"]] = k + 1
        row[id(o)] = k
    for t in order:
        tobjs = loaded[t]
        by_id = {}
        for j, o in enumerate(tobjs):
            if o.get("xf_id"):
                by_id[o["xf_id"]] = j
        nodes = []
        for j, o in enumerate(tobjs):
            parent = by_id.get(o.get("godot_tree_parent"), -1)
            # a reference to another node of this scene: the clone's
            refs = {f: by_id[tid] for f, tid in (o.get("object_refs")
                                                 or {}).items()
                    if tid in by_id}
            by_path = {x["godot_path"]: q for q, x in enumerate(tobjs)}
            conns = [{"signal": g["signal"], "to": by_path[g["target"][1]],
                      "method": g["method"]}
                     for g in o.get("godot_custom") or []
                     if g["target"][1] in by_path]
            timer_refs = {f: by_path[tgt[1]] for f, tgt in (
                o.get("godot_timer_refs") or {}).items()
                if tgt[0] == o.get("scene") and tgt[1] in by_path}
            nodes.append({"class": o["class"], "index": row[id(o)],
                          "parent": parent, "refs": refs,
                          "name": o["name"], "conns": conns,
                          "timer_refs": timer_refs})
        _TEMPLATES[0][t] = {"class": tobjs[0]["class"],
                            "index": row[id(tobjs[0])],
                            "name": tobjs[0]["name"], "nodes": nodes,
                            "path": t}
    _SPAWN_FIELDS[0] = {}
    for o in objects:
        for field, path in (o.get("godot_scene_refs") or {}).items():
            o["fields"][field] = _TEMPLATES[0][path]["index"]
            _SPAWN_FIELDS[0].setdefault((o.get("script"), field), set()).add(
                _TEMPLATES[0][path]["class"])


#: A spawned class's spare instances when it names no `[MaxInstances(N)]`.
DEFAULT_SPAWN_BUDGET = 64


def spawn_classes():
    """{class: {"name": a spawned node's name}} of the classes a spawn
    clones: every node of every template."""
    out = {}
    for t in _TEMPLATES[0].values():
        for nd in t["nodes"]:
            out.setdefault(nd["class"], {"name": nd["name"]})
    return out


def spawn_templates():
    """The templates, in a fixed order: [{"class", "index", "nodes"}] --
    nodes in tree order, each {"class", "index" (its template row),
    "parent" (a node's position, -1 the root), "refs" {field: node}}."""
    return [_TEMPLATES[0][k] for k in sorted(_TEMPLATES[0])]


def _max_instances(script):
    """A script class's `[MaxInstances(N)]` (the project's own attribute,
    as unity_pack reads it), or None."""
    if not script:
        return None
    m = re.search(r"\[\s*MaxInstances\s*\(\s*(\d+)\s*\)\s*\]",
                  _read(script))
    return int(m.group(1)) if m else None


def _check_template(path, tobjs):
    """What spawning packs yet: a scene whose physics body, if any, is its
    root, and whose physics signals go to that body's own script."""
    def at(o):
        fpath, line = o.get("godot_where") or (path, 1)
        return "%s:%d" % (godot_display_path(fpath), line)
    if not tobjs:
        raise PackError("%s:1: error: an empty scene cannot be spawned"
                        % godot_display_path(path))
    for o in tobjs[1:]:
        if o.get("collider2d") or o.get("rigidbody2d"):
            raise PackError(
                "%s: error: `%s` is a physics body below a spawned scene's "
                "root, which is not packed yet (a body as the root is)" % (
                    at(o), o["name"]))
    for o in tobjs:
        for sg in o.get("godot_signals") or []:
            if sg["target"][1] != o["godot_path"]:
                raise PackError(
                    "%s: error: `%s` is a spawned body whose %s goes to "
                    "another node; a spawned body's physics signals to its "
                    "own script are packed, not yet to another node" % (
                        at(o), o["name"], sg["signal"]))


def load_godot_scenes(root, cameras=None):
    """(objects, scene list) for the project's main scene(s). The scenes'
    current Camera2Ds -- else the viewport's own view -- are appended to
    *cameras*."""
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
        objects.extend(scene_objects(proj, p, si, cameras))
    _MAIN_ROOT[0] = _load_tree(proj, scenes[0])
    _FIRST_TEMPLATE[0] = len(scenes)
    _load_templates(proj, objects, len(scenes))
    _order_sprites(objects)
    if cameras is not None and not cameras:
        cameras.append(viewport_camera(proj.root))
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
    (r"\bGetChildren?\s*[<(]", "GetChild"),
    (r"\bGetTree\s*\(", "GetTree"),
    (r"\bConnect\s*\(", "Connect"),
    (r"(?:\boverride\s+void\s+)?\b_(?:Unhandled)?(?:Key|Shortcut)?Input\s*\(",
     "_Input"),
    (r"\b_Draw\s*\(", "_Draw"),
    (r"(?<![.\w])(?:this\s*\.\s*)?(?:Global)?(?:Rotation|RotationDegrees|Scale|Skew|Transform)\b",
     "Rotation / Scale / Transform"),
    (r"(?<![.\w])(?:this\s*\.\s*)?(?:Velocity|MoveAndSlide|MoveAndCollide|IsOnFloor)\b",
     "CharacterBody2D"),
    (r"(?<![.\w])(?:this\s*\.\s*)?(?:Visible|Modulate|ZIndex|Show|Hide)\b",
     "CanvasItem"),
    (r"\bGD\s*\.\s*(?!(?:Print|PrintS|PrintT|PrintRaw|PrintErr|Str)\b)\w+",
     "GD"),
    (r"\bResourceLoader\b", "ResourceLoader"),
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


def adapt_csharp(path, text, project_types=(), handlers=(),
                 custom_handlers=()):
    """Godot C# *text* as the Unity-shaped subset, lines unchanged.

    *project_types* are the classes the project declares: a base named
    there is the project's own, anything else a script derives from with
    `using Godot` is a Godot type. *handlers* are the script's methods a
    physics signal calls: each is public, and its node parameter is the
    other collider's index, used through `is`, `IsInGroup` and `Name`.
    """
    project_types = set(project_types)
    scan = cs2cpp._blank(text)
    text, scan = _lower_spawning(path, text, scan)
    # A member the script declares itself is its own, not Godot's.
    declared = set(re.findall(
        r"\b[\w.]+(?:\s*<[^>]*>)?(?:\s*\[\s*\])?\??\s+(\w+)\s*[;=({]", scan))
    for pat, what in _REFUSED:
        for m in re.finditer(pat, scan):
            word = re.search(r"\w+(?=\s*[<(]?\s*$)|\w+$",
                             m.group(0).rstrip("<( \t"))
            # A member the script declares is its own -- unless it
            # overrides Godot's callback, which Godot would call.
            if word and word.group(0) in declared \
                    and not m.group(0).lstrip().startswith("override"):
                continue
            _refuse(path, text, m.start(), what)

    if _INPUT[0] is None and GODOT_ROOT[0]:
        _INPUT[0] = _InputPlan(GODOT_ROOT[0])
    # SpriteEffects2D (the GPU path's effect byte): this node is its
    # gameObject, then Unity's desugaring (tools/unity_pack_gpu2d.py)
    # (unity_pack's pack() may have desugared the project's files already:
    # `__sprite_fx(this, ..)`)
    if "SpriteEffect" in text or "__sprite_fx" in text:
        import tools.unity_pack_gpu2d as _gfx
        text = re.sub(r"((?:SpriteEffects2D\s*\.\s*(?:Set|Clear)|"
                      r"(?<![\w.])__sprite_fx)\s*\(\s*)this\b(?!\s*\.)",
                      r"\1gameObject", text)
        text = _gfx.desugar_effects(text)
        scan = cs2cpp._blank(text)
    # Input first, as its own pass: its calls sit inside others (GD.Print).
    edits = []
    _lower_input(path, text, scan, edits)
    if edits:
        text = _apply(text, edits)
        scan = cs2cpp._blank(text)
    text, scan = _lower_node_refs(path, text, scan)
    text, scan = _lower_ref_members(path, text, scan)
    text, scan = _lower_own_position_vectors(path, text, scan)
    text, scan = _lower_vector_members(path, text, scan)
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
    _lower_custom_signals(path, text, scan, edits, custom_handlers)
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

    # GD.Print(a, b) -> Console.WriteLine("" + a + b): Godot concatenates;
    # PrintS / PrintT put a space / a tab between; PrintRaw adds no newline;
    # PrintErr prints to stderr; GD.Str(a, b) is the concatenation itself.
    seps = {"Print": None, "PrintRaw": None, "PrintErr": None, "Str": None,
            "PrintS": '" "', "PrintT": '"\\t"'}
    calls = {"Print": "System.Console.WriteLine(%s)",
             "PrintS": "System.Console.WriteLine(%s)",
             "PrintT": "System.Console.WriteLine(%s)",
             "PrintRaw": "GodotPrint.Raw(%s)",
             "PrintErr": "GodotPrint.Err(%s)",
             "Str": "(%s)"}
    gd_re = re.compile(r"\bGD\s*\.\s*(Print|PrintS|PrintT|PrintRaw|PrintErr|"
                       r"Str)\s*\(")
    # innermost first -- GD.Print(GD.Str(..)) -- a pass at a time, as the
    # other edits of this pass must not overlap them
    text = _apply(text, edits)
    edits = []
    for _pass in range(64):
        scan = cs2cpp._blank(text)
        gd_edits = []
        for m in gd_re.finditer(scan):
            close = _close_paren(scan, m.end() - 1)
            if close is None or gd_re.search(scan, m.end(), close):
                continue
            args = [a.strip() for a in cs2cpp.split_call_args(
                text[m.end():close]) if a.strip()]
            # Parentheses only where an argument needs them: a literal
            # inside them, "(frame ", would be typed by counting its
            # parentheses.
            parts = [a if _SIMPLE_ARG.match(a) else "(%s)" % a for a in args]
            sep = seps[m.group(1)]
            if sep and parts:
                parts = [x for k, a in enumerate(parts)
                         for x in ((sep, a) if k else (a,))]
            gd_edits.append((m.start(), close + 1,
                             calls[m.group(1)] % " + ".join(['""'] + parts)))
        if not gd_edits:
            break
        text = _apply(text, gd_edits)
    scan = cs2cpp._blank(text)

    # StringName is a string here (the C# code converts between them)
    for m in re.finditer(r"(?<![\w.])(?:Godot\s*\.\s*)?StringName\b(?!\s*\.)",
                         scan):
        edits.append((m.start(), m.end(), _pad("string", m.end() - m.start())
                      if m.end() - m.start() >= 6 else "string"))

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
    # ... and another node's, through a reference to its script's class.
    ref_types = set(project_types) | {
        c for cs in _REFS[0]["classes"].values() for c in cs}
    refs = {m.group(2) for m in re.finditer(
        r"(?<![\w.])(\w+)\s+(\w+)\s*[;=,)]", scan)
        if m.group(1) in ref_types}
    if refs:
        for m in re.finditer(
                r"(?<![.\w])(?:this\s*\.\s*)?(%s)\s*\.\s*"
                r"(GlobalPosition|Position)\b" % "|".join(
                    re.escape(r) for r in sorted(refs)), scan):
            edits.append((m.start(2), m.end(2),
                          "transform.position" if m.group(2) ==
                          "GlobalPosition" else "transform.localPosition"))
    consts = {"Zero": "Vector%d.zero", "One": "Vector%d.one",
              "Up": "new Vector%d(0, -1%s)", "Down": "new Vector%d(0, 1%s)",
              "Left": "new Vector%d(-1, 0%s)",
              "Right": "new Vector%d(1, 0%s)"}
    # (Vector2's constants are _lower_vector_members'; these are Vector3's)
    for m in re.finditer(r"\bVector(3)\s*\.\s*(Zero|One|Up|Down|Left|Right)\b",
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
    for m in re.finditer(r"(?<![\w])(%s)\s*\.\s*([XYZ])\b" % "|".join(names),
                         scan):
        s = m.start(2)
        edits.append((s, s + 1, m.group(2).lower()))
    # ... and of a vector that is not a name: `new Vector2(..).Y`, a
    # GodotVec / GodotInput.Vector result, `(a + b).X`
    for m in re.finditer(r"\)\s*\.\s*([XY])\b(?!\s*\()", scan):
        depth, q = 0, m.start()
        while q >= 0:
            if scan[q] == ")":
                depth += 1
            elif scan[q] == "(":
                depth -= 1
                if depth == 0:
                    break
            q -= 1
        head = scan[:q].rstrip() if q >= 0 else ""
        if (not re.search(r"[\w.>\]]$", head)
                or re.search(r"(?:new\s+Vector2|GodotVec\s*\.\s*\w+|"
                             r"GodotInput\s*\.\s*Vector)$", head)):
            edits.append((m.start(1), m.start(1) + 1, m.group(1).lower()))
    text = _apply(text, edits)
    return _lower_signal_params(path, text, handler_params)


_TIMER_PROPS = ("WaitTime", "OneShot", "Paused")


def _lower_node_refs(path, text, scan):
    """Node references as fields the scene seeds: each `GetNode<T>(..)` is
    a hidden field `__gnK` of the script's class (declared on the class's
    own line), T's own type -- a script class's instance, or for a Timer its
    timer (an int) -- with the Timer's members as GodotTimer calls; a Timer
    script's own members too. Returns (text, scan)."""
    sites = node_ref_sites(path, text)
    is_timer = script_base(text) == "Timer"
    timer_names = set()
    edits = []
    for k, site in enumerate(sites):
        edits.append((site["start"], site["end"], "__gn%d" % k))
        if site["type"] == "Timer":
            timer_names.add("__gn%d" % k)
    for m in re.finditer(
            r"\bvar(?=\s+(\w+)\s*=\s*(?:this\s*\.\s*)?GetNode(?:OrNull)?"
            r"\s*<\s*(?:Godot\s*\.\s*)?Timer\s*>)", scan):
        edits.append((m.start(), m.end(), "int"))
        timer_names.add(m.group(1))
    for m in re.finditer(
            r"(?<![\w.])(?:Godot\s*\.\s*)?Timer(?=\s*\??\s+(\w+)\s*[;=,)])",
            scan):
        edits.append((m.start(), m.end(), _pad("int", m.end() - m.start())))
        timer_names.add(m.group(1))
    # public: a reference the scene sets is a serialized field (Unity's
    # rule, which unity_pack keeps: a private one is not seeded)
    project = set(_REFS[0]["members"])

    def ref_class(field, at, what):
        """The packed class a Godot-typed reference *field* resolves to."""
        got = _REFS[0]["classes"].get((path, field)) or set()
        if len(got) != 1:
            _refuse(path, text, at, what,
                    "%s resolves to %s from the nodes running this script; "
                    "one class is packed" % (what, " and ".join(sorted(got))
                                            if got else "no node"))
        return next(iter(got))
    site_types = []
    retype = {}           # variable -> class
    for k, site in enumerate(sites):
        ty = site["type"]
        if ty != "Timer" and ty not in project:
            ty = ref_class("__gn%d" % k, site["start"], "GetNode<%s>" % ty)
            am = re.search(r"(?<![\w.])(\w+)\s*=\s*$", scan[:site["start"]])
            if am:
                if retype.get(am.group(1), ty) != ty:
                    _refuse(path, text, site["start"], "GetNode",
                            "`%s` is given nodes of classes %s and %s" % (
                                am.group(1), retype[am.group(1)], ty))
                retype[am.group(1)] = ty
        site_types.append(ty)
    for field, fty in export_types(text).items():
        if fty != "Timer" and fty not in project and (
                path, field) in _REFS[0]["classes"]:
            retype[field] = ref_class(field, 0, field)
    for name, cls in retype.items():
        for m in re.finditer(r"(?<![\w.])(?:Godot\s*\.\s*)?(\w+)(?=\s*\??\s+%s"
                             r"\s*[;=,)])" % re.escape(name), scan):
            if m.group(1) in _GODOT_BASES:
                edits.append((m.start(), m.end(), cls))
    decls = "".join(" public %s __gn%d;" % (
        "int" if ty == "Timer" else ty, k)
        for k, ty in enumerate(site_types))
    if is_timer:
        decls += " public int __timer_self;"
    if decls:
        stem = os.path.splitext(os.path.basename(path))[0]
        cm = re.search(r"\bclass\s+%s\b[^{;]*\{" % re.escape(stem), scan)
        if cm is None:
            _refuse(path, text, 0, "GetNode")
        edits.append((cm.end(), cm.end(), decls))
    if not edits:
        return text, scan
    text = _apply(text, edits)
    scan = cs2cpp._blank(text)

    # Timer members, on a Timer reference or a Timer script's own node.
    declared = set(re.findall(
        r"\b[\w.]+(?:\s*<[^>]*>)?\??\s+(\w+)\s*[;=({]", scan)) - timer_names
    targets = []
    if timer_names:
        targets += [(m, m.group(1), m.group(2)) for m in re.finditer(
            r"(?<![.\w])(?:this\s*\.\s*)?(%s)\s*\.\s*(\w+)" % "|".join(
                re.escape(n) for n in sorted(timer_names)), scan)]
    if is_timer:
        targets += [(m, "__timer_self", m.group(1)) for m in re.finditer(
            r"(?<![.\w])(?:this\s*\.\s*)?(Start|Stop|IsStopped|TimeLeft|"
            r"WaitTime|OneShot|Paused|Autostart)\b", scan)
            if m.group(1) not in declared]
    edits = []
    for m, ref, member in targets:
        after = scan[m.end():]
        if member in ("Start", "Stop", "IsStopped"):
            om = re.match(r"\s*\(", after)
            close = _close_paren(scan, m.end() + om.end() - 1) if om else None
            if close is None:
                _refuse(path, text, m.start(), "Timer.%s" % member)
            arg = text[m.end() + om.end():close].strip()
            if member == "Start":
                new = "GodotTimer.Start(%s, %s)" % (ref, arg or "-1f")
            elif arg:
                _refuse(path, text, m.start(), "Timer.%s" % member)
            else:
                new = "GodotTimer.%s(%s)" % (member, ref)
            edits.append((m.start(), close + 1, new))
        elif member == "TimeLeft" or member in _TIMER_PROPS:
            am = re.match(r"\s*([-+*/]?)=(?!=)", after)
            if am and (am.group(1) or member == "TimeLeft"):
                _refuse(path, text, m.start(), "Timer.%s %s=" % (
                    member, am.group(1)))
            if am:
                end = _statement_end_plain(scan, m.end() + am.end())
                expr = text[m.end() + am.end():end].strip()
                edits.append((m.start(), end, "GodotTimer.Set%s(%s, %s)" % (
                    member, ref, expr)))
            else:
                edits.append((m.start(), m.end(), "GodotTimer.%s(%s)" % (
                    member, ref)))
        elif member == "Timeout" and ref != "__timer_self":
            _refuse(path, text, m.start(), "Timer.Timeout",
                    "a Timer's timeout is connected in the scene (or by "
                    "its own script's `Timeout +=`)")
        else:
            _refuse(path, text, m.start(), "Timer.%s" % member)
    if edits:
        text = _apply(text, edits)
        scan = cs2cpp._blank(text)
    return text, scan


def _lower_ref_members(path, text, scan):
    """What a script does through a reference to another node: the node's
    class's own members stay; its `Position` / `GlobalPosition` as a vector
    is read from its components, and assigned (`=`, `+=`, `-=`, `*=`, `/=`)
    through them; its `Name` is its node's. Anything else is refused -- it
    was left for the lowering to drop. And this node's own `Name`."""
    known = set(_REFS[0]["members"]) | {
        c for cs in _REFS[0]["classes"].values() for c in cs}
    refs = {m.group(2): m.group(1) for m in re.finditer(
        r"(?<![\w.])(\w+)\s*\??\s+(\w+)\s*[;=,)]", scan)
        if m.group(1) in known}
    edits = []
    serial = [0]
    for writes in (False, True) if refs else ():
        if writes and edits:
            # vector reads first, so a write's value has its own lowered
            text = _apply(text, edits)
            scan = cs2cpp._blank(text)
            edits = []
        for m in re.finditer(
                r"(?<![.\w])(?:this\s*\.\s*)?(%s)\s*\.\s*(\w+)" % "|".join(
                    re.escape(r) for r in sorted(refs, key=len, reverse=True)),
                scan):
            ref, member = m.group(1), m.group(2)
            cls = refs[ref]
            after = scan[m.end():]
            if member in _REFS[0]["members"].get(cls, ()):
                continue
            if writes and member not in ("Position", "GlobalPosition"):
                continue          # checked on the first pass
            if member in ("Position", "GlobalPosition"):
                if re.match(r"\s*\.\s*[XYZ]\b", after):
                    if re.match(r"\s*\.\s*[XYZ]\s*[-+*/]?=(?!=)", after):
                        _refuse(path, text, m.start(), "%s.%s" % (cls, member),
                                "a component of another node's %s is not "
                                "assigned in C# (CS1612); assign the vector"
                                % member)
                    continue
                am = re.match(r"\s*([-+*/]?)=(?!=)", after)
                comp = "%s.%s" % (ref, member)
                if bool(am) != writes:
                    continue
                if am:
                    end = _statement_end_plain(scan, m.end() + am.end())
                    expr = text[m.end() + am.end():end].strip()
                    var = "__rp%d" % serial[0]
                    serial[0] += 1
                    op = am.group(1)
                    # one `ref.Position = new Vector2(x, y);`, which the
                    # lowering sets through the node's setters (or, for the
                    # global position under parents, their inverse)
                    if op in ("*", "/"):
                        # a vector scaled by a number (component-wise: the
                        # subset has no vector operators)
                        if not re.fullmatch(r"[\d.]+f?|[A-Za-z_]\w*", expr):
                            _refuse(path, text, m.start(), "%s %s=" % (
                                comp, op), "`%s %s=` is packed with a number "
                                "or a float variable" % (comp, op))
                        new = ("{ float %s = %s; %s = new Vector2(%s.X %s %s, "
                               "%s.Y %s %s); }" % (var, expr, comp, comp, op,
                                                   var, comp, op, var))
                    elif op:
                        new = ("{ Vector2 %s = %s; %s = new Vector2(%s.X %s "
                               "%s.X, %s.Y %s %s.Y); }" % (
                                   var, expr, comp, comp, op, var,
                                   comp, op, var))
                    else:
                        new = ("{ Vector2 %s = %s; %s = new Vector2(%s.X, "
                               "%s.Y); }" % (var, expr, comp, var, var))
                    semi = end + 1 if end < len(scan) and scan[end] == ";" \
                        else end
                    edits.append((m.start(), semi, new))
                    _REFS[0]["pos_written"].add(cls)
                else:
                    edits.append((m.start(), m.end(), "new Vector2(%s.X, %s.Y)"
                                  % (comp, comp)))
                continue
            if member == "Name":
                if re.match(r"\s*[-+*/]?=(?!=)", after):
                    _refuse(path, text, m.start(), "%s.Name =" % cls)
                edits.append((m.start(), m.end(), "%s.GodotName" % ref))
                continue
            _refuse(path, text, m.start(), "%s.%s" % (cls, member),
                    "`%s.%s` through a reference is not packed yet (the "
                    "node's class's own members, Position, GlobalPosition "
                    "and Name are)" % (cls, member))
    # This node's own Name: its node's, fixed (renaming is not packed).
    if "Name" not in _declared_names(scan):
        for m in re.finditer(r"(?<![.\w])(?:this\s*\.\s*)?Name\b(?!\s*\()",
                             scan):
            if re.match(r"\s*[-+*/]?=(?!=)", scan[m.end():]):
                _refuse(path, text, m.start(), "Name =")
            edits.append((m.start(), m.end(), "GodotSignals.OwnName()"))
    if edits:
        text = _apply(text, edits)
        scan = cs2cpp._blank(text)
    return text, scan


def _lower_own_position_vectors(path, text, scan):
    """This node's `Position` / `GlobalPosition` as a whole vector: read, it
    is `new Vector2(P.X, P.Y)`; assigned any vector expression (or `+=`,
    `-=`, `*=`, `/=`), the value goes through a temporary whose components
    are set -- what the lowering's position setters take. (Vector2
    arithmetic itself is unity_pack_vectors'.) `P = new Vector2(x, y);` is
    left as it is."""
    if {"Position", "GlobalPosition"} & _declared_names(scan):
        return text, scan
    pat = r"(?<![.\w])(?:this\s*\.\s*)?(Position|GlobalPosition)\b"
    edits = []
    for m in re.finditer(pat, scan):
        after = scan[m.end():]
        if re.match(r"\s*\.\s*[XYZ]\b", after) or re.match(
                r"\s*[-+*/]?=(?!=)", after):
            continue
        edits.append((m.start(), m.end(), "new Vector2(%s.X, %s.Y)" % (
            m.group(1), m.group(1))))
    if edits:
        text = _apply(text, edits)
        scan = cs2cpp._blank(text)
    edits = []
    serial = 0
    for m in re.finditer(pat, scan):
        am = re.match(r"\s*([-+*/]?)=(?!=)", scan[m.end():])
        if not am:
            continue
        end = _statement_end_plain(scan, m.end() + am.end())
        expr = text[m.end() + am.end():end].strip()
        op, prop = am.group(1), m.group(1)
        if not op and re.fullmatch(r"new\s+Vector2\s*\(.*\)", expr, re.S) \
                and _close_paren(scan, m.end() + am.end()
                                 + scan[m.end() + am.end():].index("(")) \
                == end - 1 - (len(scan[m.end() + am.end():end])
                              - len(scan[m.end() + am.end():end].rstrip())):
            continue
        var = "__tp%d" % serial
        serial += 1
        value = expr if not op else "new Vector2(%s.X, %s.Y) %s (%s)" % (
            prop, prop, op, expr)
        semi = end + 1 if end < len(scan) and scan[end] == ";" else end
        edits.append((m.start(), semi,
                      "{ Vector2 %s = %s; %s = new Vector2(%s.X, %s.Y); }"
                      % (var, value, prop, var, var)))
    if edits:
        text = _apply(text, edits)
        scan = cs2cpp._blank(text)
    return text, scan


#: Godot's Vector2 methods packed: name -> (arguments, a default for a
#: missing last one). GodotSharp's semantics, in unity_pack_vectors.
_VECTOR2_METHODS = {
    "Length": (0, None), "LengthSquared": (0, None), "Normalized": (0, None),
    "Angle": (0, None), "Abs": (0, None), "IsZeroApprox": (0, None),
    "Dot": (1, None), "Cross": (1, None), "DistanceTo": (1, None),
    "DistanceSquaredTo": (1, None), "AngleTo": (1, None),
    "DirectionTo": (1, None), "IsEqualApprox": (1, None),
    "Rotated": (1, None), "LimitLength": (1, "1.0f"),
    "Lerp": (2, None), "MoveToward": (2, None),
}
_VECTOR2_CONSTANTS = {"Zero": (0, 0), "One": (1, 1), "Up": (0, -1),
                      "Down": (0, 1), "Left": (-1, 0), "Right": (1, 0)}
#: Types whose static methods share names with Vector2's.
_NOT_VECTOR_RECEIVERS = frozenset(("Mathf", "Math", "GD", "Godot", "GodotVec"))


def _receiver_start(scan, dot):
    """The start of the postfix chain ending just before *dot* (the `.` of
    `recv.Method(`): names, `.`, `(..)` / `[..]` groups, a leading `new T`."""
    k = dot
    while True:
        j = k
        while j > 0 and scan[j - 1].isspace():
            j -= 1
        if j == 0:
            return None if j == k else j
        c = scan[j - 1]
        if c in ")]":
            op = "(" if c == ")" else "["
            depth = 0
            q = j - 1
            while q >= 0:
                if scan[q] in ")]":
                    depth += 1
                elif scan[q] in "([":
                    depth -= 1
                    if depth == 0:
                        break
                q -= 1
            if q < 0 or scan[q] != op:
                return None
            k = q
            continue
        m = re.search(r"[A-Za-z_]\w*$", scan[:j])
        if m:
            k = m.start()
            nm = re.search(r"new\s+$", scan[:k])
            if nm:
                return nm.start()
            q = k
            while q > 0 and scan[q - 1].isspace():
                q -= 1
            if q > 0 and scan[q - 1] == ".":
                k = q - 1
                continue
            return k
        return None if k == dot else k


def _lower_vector_members(path, text, scan):
    """Godot's Vector2 constants and methods: `Vector2.Up` is
    `new Vector2(0, -1)`, `v.Normalized()` is `GodotVec.Normalized(v)` (the
    engine's GodotVec_Normalized) -- whatever the receiver, a name, a call,
    `(a - b)`, `_p.Position`. Returns (text, scan)."""
    edits = []
    for m in re.finditer(r"(?<![\w.])(?:Godot\s*\.\s*)?Vector2\s*\.\s*(\w+)\b"
                         r"(?!\s*\()", scan):
        if m.group(1) in _VECTOR2_CONSTANTS:
            x, y = _VECTOR2_CONSTANTS[m.group(1)]
            edits.append((m.start(), m.end(), "new Vector2(%d, %d)" % (x, y)))
        elif m.group(1) not in ("X", "Y"):
            _refuse(path, text, m.start(), "Vector2.%s" % m.group(1))
    if edits:
        text = _apply(text, edits)
        scan = cs2cpp._blank(text)
    # innermost first: a receiver may itself hold a call (a.Normalized()
    # .Rotated(t)), so rewrite one call a pass until none is left
    names = "|".join(sorted(_VECTOR2_METHODS, key=len, reverse=True))
    for _guard in range(200):
        best = None
        for m in re.finditer(r"\.\s*(%s)\s*\(" % names, scan):
            start = _receiver_start(scan, m.start())
            if start is None:
                continue
            recv = text[start:m.start()].strip()
            if recv in _NOT_VECTOR_RECEIVERS:
                continue
            close = _close_paren(scan, m.end() - 1)
            if close is None:
                continue
            best = (start, m, close, recv)
            break
        if best is None:
            break
        start, m, close, recv = best
        name = m.group(1)
        want, dflt = _VECTOR2_METHODS[name]
        args = [a.strip() for a in cs2cpp.split_call_args(
            text[m.end():close]) if a.strip()]
        if dflt is not None and len(args) == want - 1:
            args.append(dflt)
        if len(args) != want:
            _refuse(path, text, m.start(), "Vector2.%s" % name,
                    "Vector2.%s takes %d argument(s)" % (name, want))
        new = "GodotVec.%s(%s)" % (name, ", ".join([recv] + args))
        text = text[:start] + _pad(new, close + 1 - start) + text[close + 1:] \
            if len(new) <= close + 1 - start else \
            text[:start] + new + text[close + 1:]
        scan = cs2cpp._blank(text)
    return text, scan


def _statement_end_plain(scan, k):
    """The `;` ending the statement at *k* (outside brackets)."""
    depth = 0
    while k < len(scan):
        c = scan[k]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return k
            depth -= 1
        elif c == ";" and depth == 0:
            return k
        k += 1
    return k


def _template_of(res_path):
    """The template of scene *res_path* (`res://..`), or None."""
    if not GODOT_ROOT[0] or not res_path.startswith("res://"):
        return None
    ap = os.path.join(GODOT_ROOT[0], *res_path[len("res://"):].split("/"))
    return _TEMPLATES[0].get(ap)


def _lower_spawning(path, text, scan):
    """PackedScene spawning, as the subset's Instantiate and godot_pack's
    GodotTree: a scene is its template root's index (an int); `T b =
    scene.Instantiate<T>()` (or `(T)scene.Instantiate()`, `.. as T`) is
    `T b = Instantiate(scene)`, a clone of the template's root; AddChild
    puts a node under this one, a reference, GetParent(), or the scene's
    root. Returns (text, scan)."""
    if not re.search(r"\bPackedScene\b|\bInstantiate\b|\bAddChild\b", scan):
        return text, scan
    edits = []
    local_class = {}          # a local set from a literal load -> class
    # literal loads: the template root's index
    for start, end, rp in script_scene_loads(text):
        t = _template_of(rp)
        if t is None:
            _refuse(path, text, start, "GD.Load",
                    "no scene %s is spawned from here" % rp)
        lm = re.search(r"(?:\bvar|\bPackedScene)\s+(\w+)\s*=\s*$",
                       scan[:start])
        # (a local of the scene only when the load is all its value -- not
        # `var d = GD.Load<PackedScene>(..).Instantiate<T>()`)
        if lm and re.match(r"\s*;", scan[end:]):
            local_class[lm.group(1)] = t["class"]
            edits.append((lm.start(), lm.start() + len(lm.group(0))
                          - len(lm.group(0).lstrip()) + (
                              3 if lm.group(0).lstrip().startswith("var")
                              else 11), "int"))
        edits.append((start, end, str(t["index"])))
    for m in re.finditer(r"(?<![\w.])(?:Godot\s*\.\s*)?PackedScene"
                         r"(?=\s*\??\s+\w+\s*[;=,)])", scan):
        edits.append((m.start(), m.end(), _pad("int", m.end() - m.start())))
    if edits:
        text = _apply(text, edits)
        scan = cs2cpp._blank(text)
        edits = []

    def spawns(recv, at):
        """The classes the scene *recv* spawns."""
        recv = re.sub(r"^this\s*\.\s*", "", recv.strip())
        if recv in local_class:
            return {local_class[recv]}
        if recv.isdigit():
            return {t["class"] for t in _TEMPLATES[0].values()
                    if t["index"] == int(recv)}
        got = _SPAWN_FIELDS[0].get((path, recv))
        if not got:
            _refuse(path, text, at, "Instantiate",
                    "`%s` is set to no scene on the nodes running this "
                    "script (an [Export] PackedScene the scene sets, or "
                    "GD.Load<PackedScene>(\"res://..\"))" % recv)
        return got

    recv = r"((?:this\s*\.\s*)?\w+)"
    forms = [
        # T b = s.Instantiate<T>();   var b = s.Instantiate<T>();
        re.compile(r"(?<![\w.])(?:var|(\w+))\s+(\w+)\s*=\s*" + recv +
                   r"\s*\.\s*Instantiate\s*<\s*(\w+)\s*>\s*\(\s*\)"),
        # T b = (T)s.Instantiate();
        re.compile(r"(?<![\w.])(?:var|(\w+))\s+(\w+)\s*=\s*\(\s*(\w+)\s*\)"
                   r"\s*" + recv + r"\s*\.\s*Instantiate\s*\(\s*\)"),
        # T b = s.Instantiate() as T;
        re.compile(r"(?<![\w.])(?:var|(\w+))\s+(\w+)\s*=\s*" + recv +
                   r"\s*\.\s*Instantiate\s*\(\s*\)\s*as\s+(\w+)"),
    ]
    done = []
    for k, f in enumerate(forms):
        for m in f.finditer(scan):
            if k == 1:
                decl, name, ty, src = (m.group(1), m.group(2), m.group(3),
                                       m.group(4))
            else:
                decl, name, src, ty = (m.group(1), m.group(2), m.group(3),
                                       m.group(4))
            if decl and decl != ty:
                _refuse(path, text, m.start(), "Instantiate",
                        "`%s %s` is given a %s" % (decl, name, ty))
            got = spawns(src, m.start())
            if ty not in got:
                _refuse(path, text, m.start(), "Instantiate",
                        "`%s` spawns %s, not a %s" % (
                            src, " or ".join(sorted(got)), ty))
            edits.append((m.start(), m.end(), "%s %s = Instantiate(%s)" % (
                ty, name, src)))
            done.append(m.start())
    for m in re.finditer(r"\bInstantiate\b", scan):
        if not any(d <= m.start() <= d + 400 for d in done):
            _refuse(path, text, m.start(), "Instantiate",
                    "a scene is spawned into a local: `T b = "
                    "scene.Instantiate<T>();` (or `(T)scene.Instantiate()`, "
                    "`scene.Instantiate() as T`), then AddChild(b)")
    # AddChild(child): under this node, a reference, GetParent(), the root
    for m in re.finditer(r"(?:(\.)\s*)?\bAddChild\s*\(", scan):
        close = _close_paren(scan, m.end() - 1)
        args = [a.strip() for a in cs2cpp.split_call_args(
            text[m.end():close])] if close is not None else []
        if len(args) != 1 or not re.fullmatch(r"\w+", args[0]):
            _refuse(path, text, m.start(), "AddChild",
                    "AddChild takes a spawned node's local (AddChild(b))")
        child = args[0]
        if not m.group(1):
            start = m.start()
            if re.search(r"(?<![\w.])this\s*\.\s*$", scan[:start]):
                start = re.search(r"this\s*\.\s*$", scan[:start]).start()
            edits.append((start, close + 1,
                          "GodotTree.Add(this, %s)" % child))
            continue
        rs = _receiver_start(scan, m.start(1))
        recv_text = text[rs:m.start(1)].strip() if rs is not None else ""
        compact = re.sub(r"\s+", "", recv_text)
        if compact == "GetParent()":
            new = "GodotTree.AddToParent(this, %s)" % child
        elif compact in ("GetTree().Root", "GetTree().CurrentScene"):
            new = "GodotTree.AddToRoot(%s)" % child
        elif recv_text:
            new = "GodotTree.Add(%s, %s)" % (recv_text, child)
        else:
            _refuse(path, text, m.start(), "AddChild")
        edits.append((rs, close + 1, new))
    if edits:
        text = _apply(text, edits)
        scan = cs2cpp._blank(text)
    return text, scan


def _lower_custom_signals(path, text, scan, edits, custom_handlers):
    """A script's own [Signal]s: the delegate declarations go, each
    `EmitSignal(SignalName.Hit, ..)` / `EmitSignal("Hit", ..)` /
    `EmitSignalHit(..)` is `GodotSignals.Emit_Hit(..)` (the engine's
    dispatch, on this node), and a method a connection calls is public (kept
    as the others' roots are)."""
    sigs = script_signals(text)
    for m in re.finditer(
            r"\[\s*Signal\s*\]\s*(?:(?:public|private|protected|internal)"
            r"\s+)*delegate\s+void\s+(\w+?)EventHandler\s*\(([^)]*)\)\s*;",
            scan):
        for ty, name in sigs.get(m.group(1), ()):
            if ty not in _SIGNAL_PARAM_TYPES:
                _refuse(path, text, m.start(2), None,
                        "signal %s's parameter `%s %s`: int, float, bool and "
                        "string parameters are packed" % (m.group(1), ty, name))
        edits.append((m.start(), m.end(), " " * (m.end() - m.start())))
    for m in re.finditer(r"(?<![.\w])(?:this\s*\.\s*)?EmitSignal(\w*)\s*\(",
                         scan):
        close = _close_paren(scan, m.end() - 1)
        if close is None:
            _refuse(path, text, m.start(), "EmitSignal")
        args = [a.strip() for a in cs2cpp.split_call_args(
            text[m.end():close]) if a.strip()]
        if m.group(1):
            name, rest = m.group(1), args
        else:
            first = args[0] if args else ""
            nm = (re.fullmatch(r"SignalName\s*\.\s*(\w+)", first)
                  or re.fullmatch(r'"(\w+)"', first))
            if not nm:
                _refuse(path, text, m.start(), "EmitSignal",
                        "EmitSignal's signal is packed as SignalName.X or a "
                        "string literal")
            name, rest = nm.group(1), args[1:]
        if name not in sigs:
            _refuse(path, text, m.start(), "EmitSignal",
                    "the script declares no [Signal] %s (its own signals are "
                    "the ones packed)" % name)
        if len(rest) != len(sigs[name]):
            _refuse(path, text, m.start(), "EmitSignal",
                    "signal %s takes %d argument(s), not %d" % (
                        name, len(sigs[name]), len(rest)))
        edits.append((m.start(), close + 1, "GodotSignals.Emit_%s(%s)" % (
            name, ", ".join(rest))))
    for h in custom_handlers:
        for m in re.finditer(
                r"\b((?:(?:public|private|protected|internal|virtual)\s+)*)"
                r"void\s+%s\s*\(" % re.escape(h), scan):
            if "public" not in m.group(1):
                edits.append((m.start(1), m.end(1), _pad(
                    "public ", m.end(1) - m.start(1))
                    if m.group(1) else "public "))


def lower_emits(text, idn):
    """unity_pack's lowering: `GodotSignals.Emit_Hit(..)` in a method of
    class *idn* is its dispatch on this instance, `i`."""
    text = re.sub(r"GodotSignals\.OwnName\s*\(\s*\)",
                  "GodotNodeName_%s(i)" % idn, text)
    text = re.sub(r"GodotSignals\.Emit_(\w+)\s*\(\s*\)",
                  r"GodotEmit_%s_\1(i)" % idn, text)
    return re.sub(r"GodotSignals\.Emit_(\w+)\s*\(",
                  r"GodotEmit_%s_\1(i, " % idn, text)


def resolve_custom_signals(plan):
    """Timers and the scripts' own signals: {"decls": {(class, signal):
    [C types]}, "emits": [connections], "timers": [(class, inst, timer)]},
    or None when the project has none."""
    where = {}
    for cname, cl in sorted(plan["classes"].items()):
        for i, o in enumerate(cl.get("instances") or []):
            if "godot_path" in o:
                where[(o.get("scene", 0), o["godot_path"])] = (cname, i, o)
    decls, emits, timers = {}, [], []
    for (_k, (cname, i, o)) in sorted(where.items()):
        if o.get("script"):
            for sig, params in script_signals(_read(o["script"])).items():
                decls[(cname, sig)] = [_SIGNAL_PARAM_TYPES.get(ty, "int")
                                       for ty, _n in params]
        if o.get("godot_timer"):
            decls[(cname, "timeout")] = []
            timers.append((cname, i, o["godot_timer"]))
        for sg in o.get("godot_custom") or []:
            tc, ti, _to = where[sg["target"]]
            emits.append({"from_class": cname, "from_inst": i,
                          "signal": sg["signal"], "to_class": tc,
                          "to_inst": ti, "method": sg["method"]})
    if not decls:
        return None
    # A Timer reference is its timer's slot; a missing one is -1.
    slot = {}
    for k, (cname, i, _t) in enumerate(timers):
        o = plan["classes"][cname]["instances"][i]
        slot[(o.get("scene", 0), o["godot_path"])] = k
    for (_k, (cname, i, o)) in sorted(where.items()):
        for field, tgt in (o.get("godot_timer_refs") or {}).items():
            o.setdefault("fields", {})[field] = slot.get(tgt, -1)
    return {"decls": decls, "emits": emits, "timers": timers}


def emit_custom_decls(p, plan, c_ident):
    """Prototypes of each signal's dispatch, before the scripts that emit."""
    gc = plan["godot_custom"]
    p("/* Godot: Timer timeouts and the scripts' own signals (godot_pack.py) */")
    for (cname, sig), types in sorted(gc["decls"].items()):
        p("static void GodotEmit_%s_%s(unsigned i%s);" % (
            c_ident(cname), sig, "".join(
                ", %s a%d" % (t, k) for k, t in enumerate(types))))
    timers = gc["timers"]
    if timers:
        n = len(timers)
        p("/* Timers (Godot's Timer): a reference to one is its index here */")
        p("static float _godot_timer_wait[%d] = { %s };" % (n, ", ".join(
            "%sf" % repr(float(t["wait"])) for _c, _i, t in timers)))
        p("static int _godot_timer_one_shot[%d] = { %s };" % (
            n, ", ".join(str(t["one_shot"]) for _c, _i, t in timers)))
        p("static int _godot_timer_paused[%d];" % n)
        p("/* autostart: started at ready, time_left = wait_time; else -1 */")
        p("static float _godot_timer_left[%d] = { %s };" % (n, ", ".join(
            "%sf" % repr(float(t["wait"]) if t["autostart"] else -1.0)
            for _c, _i, t in timers)))
        p("static int _godot_timer_on[%d] = { %s };" % (n, ", ".join(
            str(t["autostart"]) for _c, _i, t in timers)))
        p("/* a spawned Timer is processed from the frame after it is added */")
        p("static int _godot_timer_fresh[%d];" % n)
    p("static int _godot_timer_ok(int t) { return t >= 0 && t < %d; }"
      % len(timers))
    arr = bool(timers)

    def body(expr, dflt):
        return expr if arr else dflt
    p("static void GodotTimer_Start(int t, float sec) {")
    p("    if (!_godot_timer_ok(t)) return;")
    if arr:
        p("    if (sec > 0.f) _godot_timer_wait[t] = sec;")
        p("    _godot_timer_left[t] = _godot_timer_wait[t];")
        p("    _godot_timer_on[t] = 1;")
    else:
        p("    (void)sec;")
    p("}")
    p("static void GodotTimer_Stop(int t) {")
    p("    if (!_godot_timer_ok(t)) return;")
    if arr:
        p("    _godot_timer_left[t] = -1.f;")
        p("    _godot_timer_on[t] = 0;")
    p("}")
    p("static float GodotTimer_TimeLeft(int t) {")
    p("    if (!_godot_timer_ok(t)) return 0.f;")
    p("    return %s;" % body(
        "_godot_timer_left[t] > 0.f ? _godot_timer_left[t] : 0.f", "0.f"))
    p("}")
    p("static int GodotTimer_IsStopped(int t) {")
    p("    return GodotTimer_TimeLeft(t) <= 0.f;")
    p("}")
    for prop, arrn, cty in (("WaitTime", "wait", "float"),
                            ("OneShot", "one_shot", "int"),
                            ("Paused", "paused", "int")):
        p("static %s GodotTimer_%s(int t) {" % (cty, prop))
        p("    if (!_godot_timer_ok(t)) return 0;")
        p("    return %s;" % body("_godot_timer_%s[t]" % arrn, "0"))
        p("}")
        p("static void GodotTimer_Set%s(int t, %s v) {" % (prop, cty))
        p("    if (!_godot_timer_ok(t)) return;")
        p("    %s" % body("_godot_timer_%s[t] = %s;" % (
            arrn, "v != 0" if cty == "int" else "v"), "(void)v;"))
        p("}")
    p("")


def emit_custom_dispatch(p, plan, c_ident, want_go_tables):
    """Each signal's dispatch: its connections from the emitting instance,
    in connection order, each while sender and receiver are not freed; and
    the Timers, ticked each frame after the scripts' _Process, as a
    Timer's internal process (`time_left < 0`: timeout, and again after
    wait_time unless one_shot)."""
    gc = plan["godot_custom"]
    owners = {}
    for cname, cl in plan["classes"].items():
        for i, o in enumerate(cl.get("instances") or []):
            owners[(cname, i)] = o

    def alive(cname, inst):
        gi = (owners.get((cname, inst)) or {}).get("go_index")
        return None if gi is None or not plan.get(
            "_godot_destroy") else "!_engine_go_destroyed[%d]" % int(gi)

    def guard(call):
        if not want_go_tables:
            return [call]
        return ["_engine_in_script = 1;",
                "if (setjmp(_engine_script_jmp) == 0)",
                "    " + call,
                "_engine_in_script = 0;"]
    for (cname, sig), types in sorted(gc["decls"].items()):
        args = "".join(", a%d" % k for k in range(len(types)))
        p("static void GodotEmit_%s_%s(unsigned i%s) {" % (
            c_ident(cname), sig, "".join(
                ", %s a%d" % (t, k) for k, t in enumerate(types))))
        by_inst = {}
        for e in gc["emits"]:
            if e["from_class"] == cname and e["signal"] == sig:
                by_inst.setdefault(e["from_inst"], []).append(e)
        for k in range(len(types)):
            p("    (void)a%d;" % k)
        dyn = [(ck, c) for ck, c in enumerate(plan.get("godot_conns") or [])
               if c["from_class"] == cname and c["signal"] == sig]
        for ck, c in dyn:
            # a spawned scene's connection: its receiver, when spawned
            p("    if (i < %du && _godot_conn_%d[i] >= 0) {" % (c["cap"], ck))
            p("        unsigned to = (unsigned)_godot_conn_%d[i];" % ck)
            call = "%s_%s(to%s);" % (c_ident(c["to_class"]), c["method"], args)
            if plan.get("_godot_destroy"):
                p("        if (!_engine_go_destroyed[_engine_go_of_%s(to)]) {"
                  % c_ident(c["to_class"]))
                for line in guard(call):
                    p("            " + line)
                p("        }")
            else:
                for line in guard(call):
                    p("        " + line)
            p("    }")
        if not by_inst and not dyn:
            p("    (void)i;")
        else:
            p("    switch (i) {")
            for inst in sorted(by_inst):
                p("    case %du:" % inst)
                for e in by_inst[inst]:
                    cond = [c for c in (alive(cname, inst),
                                        alive(e["to_class"], e["to_inst"]))
                            if c]
                    p("        /* %s.%s -> %s.%s */" % (
                        cname, sig, e["to_class"], e["method"]))
                    call = "%s_%s(%du%s);" % (c_ident(e["to_class"]),
                                              e["method"], e["to_inst"], args)
                    if cond:
                        p("        if (%s) {" % " && ".join(dict.fromkeys(cond)))
                        for line in guard(call):
                            p("            " + line)
                        p("        }")
                    else:
                        for line in guard(call):
                            p("        " + line)
                p("        break;")
            p("    default: break;")
            p("    }")
        p("}")
    timers = gc["timers"]
    if timers:
        p("static void _godot_timers_tick(float dt) {")
        for k, (cname, inst, _t) in enumerate(timers):
            live = alive(cname, inst)
            p("    if (_godot_timer_fresh[%d]) {" % k)
            p("        _godot_timer_fresh[%d] = 0;" % k)
            p("    } else if (_godot_timer_on[%d] && !_godot_timer_paused[%d]%s) {"
              % (k, k, (" && " + live) if live else ""))
            p("        _godot_timer_left[%d] = _godot_timer_left[%d] - dt;"
              % (k, k))
            p("        if (_godot_timer_left[%d] < 0.f) {" % k)
            p("            if (!_godot_timer_one_shot[%d]) {" % k)
            p("                _godot_timer_left[%d] = _godot_timer_left[%d]"
              " + _godot_timer_wait[%d];" % (k, k, k))
            p("            } else {")
            p("                _godot_timer_on[%d] = 0; /* stop() */" % k)
            p("                _godot_timer_left[%d] = -1.f;" % k)
            p("            }")
            p("            GodotEmit_%s_timeout(%du);" % (c_ident(cname), inst))
            p("        }")
            p("    }")
        p("}")
    p("")


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


_SIMPLE_ARG = re.compile(
    r'^(?:"(?:[^"\\\n]|\\.)*"|-?\d+(?:\.\d*)?[fF]?|[A-Za-z_][\w.]*)$')


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
    _INPUT[0] = _InputPlan(os.path.abspath(root))
    members = {}
    for p in _walk(os.path.abspath(root), (".cs",)):
        scan = cs2cpp._blank(_read(p))
        for _k, name, _s, _b, _c in cs2cpp._find_types(scan):
            members.setdefault(name, set()).update(_declared_names(scan))
    classes = {}
    for o in objects:
        for field, cls in (o.get("godot_ref_classes") or {}).items():
            classes.setdefault((o.get("script"), field), set()).add(cls)
    _REFS[0] = {"members": members, "classes": classes,
                "pos_written": set()}
    handlers = {}
    for o in objects:
        if o.get("script"):
            handlers.setdefault(o["script"], set()).update(
                o.get("godot_handlers") or ())
    custom = {}
    for o in objects:
        if o.get("script"):
            custom.setdefault(o["script"], set()).update(
                o.get("godot_custom_handlers") or ())
    analyses = []
    for p in scripts:
        adapted = adapt_csharp(p, _read(p), project_types,
                               sorted(handlers.get(p, ())),
                               sorted(custom.get(p, ())))
        a = analyze_script(p, text=adapted)
        if "GodotPrint." in adapted:
            # GD.PrintRaw / PrintErr: the engine's GodotPrint_* (stdio)
            a["apis"] = set(a["apis"]) | {"GodotPrint"}
        analyses.append(a)
    moved = _REFS[0]["pos_written"]
    if _TEMPLATES[0] and analyses:
        # a template is dormant (destroyed from the start), and a spawn
        # reuses a freed row: the subset's Destroy
        analyses[0]["apis"] = set(analyses[0]["apis"]) | {"Object.Destroy"}
    for a in analyses:
        if any(c["name"] in moved for c in a.get("classes") or ()):
            a["writes_pos"] = True
    gi = input_plan()
    if gi and analyses:
        # The host's keys and joypad the actions read, as unity_pack's
        # Keyboard.current / Gamepad.current give them.
        a0 = analyses[0]
        a0["apis"] = set(a0["apis"]) | {"Keyboard.current", "Vector2"}
        a0["keyboard_keys"] = set(a0.get("keyboard_keys") or ()) | set(
            gi["keys"])
        if gi["joypad"]:
            a0["apis"].add("Gamepad.current")
    have = {c["name"] for a in analyses for c in a["classes"]}
    for o in objects:
        if o["class"] in have:
            continue
        analyses.append({
            "path": "<scene:%s>" % o["name"],
            "apis": set(),
            "spawns": False,
            "uses_z": abs(o["pos"][2]) > 1e-6,
            "writes_pos": o["class"] in moved,
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
    # The project is the argument that is neither an option nor an
    # option's value: `--box2d PATH project` used to take PATH.
    project, skip = None, False
    for a in args:
        if skip:
            skip = False
        elif a in ("-o", "--box2d", "--coost"):
            skip = True
        elif not a.startswith("-"):
            project = a
            break
    if project is None or not is_godot_project(project):
        sys.stderr.write(
            "usage: godot_pack.py <godot project> [-o DIR] [--force] "
            "[--strict] [--physics-inject] [--box2d PATH] [--coost PATH] "
            "[--gpu-batch]\n"
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
